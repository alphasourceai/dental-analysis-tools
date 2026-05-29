from __future__ import annotations

import base64
import hashlib
import html
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

try:
    import fitz
except Exception:  # pragma: no cover - dependency is validated at runtime.
    fitz = None

from supabase_utils import _get_supabase_admin_client

logger = logging.getLogger("consulting_agreements")

AGREEMENT_DOCUMENT_TYPE = "baa_privacy_agreement"
AGREEMENT_TEMPLATE_VERSION = os.getenv(
    "AGREEMENTS_BAA_TEMPLATE_VERSION",
    "baa_privacy_agreement_v2",
)
AGREEMENT_TEMPLATE_PATH = Path(
    os.getenv(
        "AGREEMENTS_BAA_TEMPLATE_PATH",
        str(Path(__file__).resolve().parent / "agreement_templates" / "baa_privacy_agreement.docx"),
    )
)
AGREEMENTS_BUCKET = os.getenv("SUPABASE_CONSULTING_AGREEMENTS_BUCKET", "consulting-agreements")
AGREEMENTS_SIGNED_URL_TTL_SECONDS = max(
    60,
    int(os.getenv("AGREEMENTS_SIGNED_URL_TTL_SECONDS", "600")),
)
AGREEMENTS_EMAIL_LINK_TTL_SECONDS = max(
    300,
    int(os.getenv("AGREEMENTS_EMAIL_LINK_TTL_SECONDS", "604800")),
)
AGREEMENTS_TOKEN_TTL_DAYS = max(1, int(os.getenv("AGREEMENTS_SIGNER_TOKEN_TTL_DAYS", "7")))
MAX_SIGNATURE_IMAGE_BYTES = 2 * 1024 * 1024


class AgreementServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_text(value: object) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_email(value: object) -> str:
    email = (normalize_text(value) or "").lower()
    if not email or len(email) > 254 or "@" not in email:
        return ""
    return email


def parse_agreement_date(value: object, field_name: str = "effectiveDate") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = normalize_text(value)
    if not text:
        raise AgreementServiceError(f"missing_{field_name}", f"{field_name} is required.")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise AgreementServiceError(
            f"invalid_{field_name}",
            f"{field_name} must use YYYY-MM-DD.",
        ) from exc


def format_date_long(value: object) -> str:
    parsed = parse_agreement_date(value)
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def normalize_admin_agreement_payload(body: dict[str, Any]) -> dict[str, Any]:
    source = body if isinstance(body, dict) else {}
    client_email = normalize_email(source.get("clientEmail") or source.get("client_email"))
    signer_email = normalize_email(
        source.get("signerEmail")
        or source.get("signer_email")
        or source.get("adminEmail")
        or source.get("admin_email")
        or client_email
    )
    effective_date = parse_agreement_date(
        source.get("effectiveDate") or source.get("effective_date"),
        "effectiveDate",
    )
    client_user_id = normalize_text(source.get("clientUserId") or source.get("client_user_id"))

    payload = {
        "client_email": client_email,
        "client_user_id": client_user_id,
        "client_legal_name": normalize_text(source.get("clientLegalName") or source.get("client_legal_name")),
        "office_name": normalize_text(source.get("officeName") or source.get("office_name")),
        "org_type": normalize_text(source.get("orgType") or source.get("org_type")),
        "phone": normalize_text(source.get("phone")),
        "state": normalize_text(source.get("state")),
        "effective_date": effective_date,
        "document_type": normalize_text(source.get("documentType") or source.get("document_type")) or AGREEMENT_DOCUMENT_TYPE,
        "signer_name": normalize_text(source.get("signerName") or source.get("signer_name")),
        "signer_email": signer_email,
        "signer_title": normalize_text(source.get("signerTitle") or source.get("signer_title")),
        "ba_signer_name": normalize_text(source.get("baSignerName") or source.get("ba_signer_name")),
        "ba_signer_title": normalize_text(source.get("baSignerTitle") or source.get("ba_signer_title")),
        "ba_signer_email": normalize_email(source.get("baSignerEmail") or source.get("ba_signer_email")),
        "ba_signature_mode": normalize_text(source.get("baSignatureMode") or source.get("ba_signature_mode")),
    }
    validate_admin_agreement_payload(payload)
    return payload


def validate_admin_agreement_payload(payload: dict[str, Any]) -> None:
    if payload.get("document_type") != AGREEMENT_DOCUMENT_TYPE:
        raise AgreementServiceError(
            "unsupported_document_type",
            "Only BAA/Privacy agreements are supported.",
        )
    if not payload.get("client_email"):
        raise AgreementServiceError("missing_clientEmail", "clientEmail is required.")
    if not payload.get("client_legal_name"):
        raise AgreementServiceError("missing_clientLegalName", "clientLegalName is required.")
    if not payload.get("state"):
        raise AgreementServiceError("missing_state", "state is required.")
    if not payload.get("effective_date"):
        raise AgreementServiceError("missing_effectiveDate", "effectiveDate is required.")
    if not payload.get("signer_email"):
        raise AgreementServiceError("missing_signerEmail", "signerEmail is required.")
    if not payload.get("ba_signer_name"):
        raise AgreementServiceError("missing_baSignerName", "baSignerName is required.")
    if not payload.get("ba_signer_title"):
        raise AgreementServiceError("missing_baSignerTitle", "baSignerTitle is required.")
    if not payload.get("ba_signer_email"):
        raise AgreementServiceError("missing_baSignerEmail", "baSignerEmail is required.")
    for field_name, max_length in (
        ("client_legal_name", 255),
        ("office_name", 255),
        ("org_type", 100),
        ("phone", 100),
        ("state", 100),
        ("signer_name", 255),
        ("signer_email", 254),
        ("signer_title", 255),
        ("ba_signer_name", 255),
        ("ba_signer_title", 255),
        ("ba_signer_email", 254),
        ("ba_signature_mode", 100),
    ):
        value = payload.get(field_name)
        if value and len(str(value)) > max_length:
            raise AgreementServiceError(
                f"invalid_{field_name}",
                f"{field_name} must be {max_length} characters or fewer.",
            )


def payload_template_values(payload: dict[str, Any]) -> dict[str, Any]:
    effective_date = payload.get("effective_date")
    effective_date_iso = effective_date.isoformat() if isinstance(effective_date, date) else str(effective_date or "")
    return {
        "clientEmail": payload.get("client_email"),
        "clientLegalName": payload.get("client_legal_name"),
        "officeName": payload.get("office_name"),
        "orgType": payload.get("org_type"),
        "phone": payload.get("phone"),
        "state": payload.get("state"),
        "effectiveDate": effective_date_iso,
        "effectiveDateDisplay": format_date_long(effective_date),
        "documentType": payload.get("document_type") or AGREEMENT_DOCUMENT_TYPE,
        "signerName": payload.get("signer_name"),
        "signerEmail": payload.get("signer_email"),
        "signerTitle": payload.get("signer_title"),
        "baSignerName": payload.get("ba_signer_name"),
        "baSignerTitle": payload.get("ba_signer_title"),
        "baSignerEmail": payload.get("ba_signer_email"),
        "baSignatureMode": payload.get("ba_signature_mode"),
    }


def build_template_snapshot(payload: dict[str, Any], source_template_sha256: Optional[str]) -> dict[str, Any]:
    return {
        "templateName": AGREEMENT_DOCUMENT_TYPE,
        "templateVersion": AGREEMENT_TEMPLATE_VERSION,
        "sourceTemplatePath": str(AGREEMENT_TEMPLATE_PATH),
        "sourceTemplateSha256": source_template_sha256,
        "generatedAt": utcnow().isoformat(),
        "values": payload_template_values(payload),
        "rendering": {
            "source": "docx_template",
            "signaturePage": "workflow_certificate",
            "dynamicFields": [
                "[Client Name]",
                "[State]",
                "Effective Date",
            ],
        },
    }


def generate_signer_token() -> tuple[str, str, datetime]:
    token = secrets.token_urlsafe(32)
    token_hash = hash_signer_token(token)
    expires_at = utcnow() + timedelta(days=AGREEMENTS_TOKEN_TTL_DAYS)
    return token, token_hash, expires_at


def hash_signer_token(token: object) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def build_signing_url(token: str) -> str:
    base_url = (
        normalize_text(os.getenv("AGREEMENTS_SIGNER_BASE_URL"))
        or normalize_text(os.getenv("PUBLIC_BASE_URL"))
        or "https://www.alphasourceconsulting.com"
    )
    return f"{base_url.rstrip('/')}/agreements/sign/{quote(token)}"


def source_template_sha256() -> Optional[str]:
    if not AGREEMENT_TEMPLATE_PATH.exists():
        return None
    digest = hashlib.sha256()
    with AGREEMENT_TEMPLATE_PATH.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_agreement_pdf(payload: dict[str, Any]) -> tuple[bytes, str]:
    if not AGREEMENT_TEMPLATE_PATH.exists():
        raise AgreementServiceError(
            "agreement_template_missing",
            f"BAA template missing. Place the approved DOCX at {AGREEMENT_TEMPLATE_PATH}.",
            status=501,
        )
    if not zipfile.is_zipfile(AGREEMENT_TEMPLATE_PATH):
        raise AgreementServiceError(
            "agreement_template_invalid",
            "BAA template must be a valid .docx file.",
            status=500,
        )

    with tempfile.TemporaryDirectory(prefix="consulting-agreement-") as temp_dir:
        temp_path = Path(temp_dir)
        prepared_docx = temp_path / "baa_privacy_agreement_prepared.docx"
        _prepare_docx_template(payload, prepared_docx)
        pdf_path = _convert_docx_to_pdf(prepared_docx, temp_path)
        return pdf_path.read_bytes(), source_template_sha256() or ""


def _prepare_docx_template(payload: dict[str, Any], destination: Path) -> None:
    effective_date = format_date_long(payload["effective_date"])
    replacements = {
        "[Client Name]": payload["client_legal_name"],
        "[State]": payload["state"],
        "[Effective Date]": effective_date,
        "Dec 2, 2025": effective_date,
    }
    xml_replacements = {
        html.escape(key, quote=False): html.escape(str(value or ""), quote=False)
        for key, value in replacements.items()
    }

    with zipfile.ZipFile(AGREEMENT_TEMPLATE_PATH, "r") as source_zip:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as target_zip:
            for item in source_zip.infolist():
                data = source_zip.read(item.filename)
                if item.filename.startswith("word/") and item.filename.endswith(".xml"):
                    text = data.decode("utf-8")
                    for needle, replacement in xml_replacements.items():
                        text = text.replace(needle, replacement)
                    if item.filename == "word/document.xml":
                        text = _remove_template_signature_block_xml(text)
                    data = text.encode("utf-8")
                target_zip.writestr(item, data)


def _remove_template_signature_block_xml(text: str) -> str:
    for pattern in (
        r"Signatures",
        r"Covered(?:(?!</w:p>).)*?Entity(?:(?!</w:p>).)*?Date",
        r"BA(?:(?!</w:p>).)*?alphaSource(?:(?!</w:p>).)*?Date",
    ):
        text = re.sub(
            rf"<w:p\b(?:(?!</w:p>).)*?{pattern}(?:(?!</w:p>).)*?</w:p>",
            "",
            text,
            flags=re.S,
        )
    return text


def _convert_docx_to_pdf(docx_path: Path, output_dir: Path) -> Path:
    converter = shutil.which("soffice") or shutil.which("libreoffice")
    if not converter:
        raise AgreementServiceError(
            "docx_pdf_converter_missing",
            "DOCX-to-PDF conversion is not available. Install LibreOffice or configure a rendering service before enabling agreement rendering.",
            status=501,
        )

    try:
        result = subprocess.run(
            [
                converter,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(output_dir),
                str(docx_path),
            ],
            check=False,
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise AgreementServiceError(
            "docx_pdf_render_timeout",
            "Agreement PDF rendering timed out.",
            status=504,
        ) from exc

    pdf_path = output_dir / f"{docx_path.stem}.pdf"
    if result.returncode != 0 or not pdf_path.exists():
        logger.error(
            "[agreements] docx conversion failed code=%s stderr=%s",
            result.returncode,
            (result.stderr or b"").decode("utf-8", errors="ignore")[:500],
        )
        raise AgreementServiceError(
            "docx_pdf_render_failed",
            "Agreement PDF rendering failed.",
            status=502,
        )
    return pdf_path


def build_signed_agreement_pdf(
    draft_pdf_bytes: bytes,
    *,
    agreement_id: str,
    payload_values: dict[str, Any],
    client_signer_name: str,
    client_signer_title: str,
    client_signed_at: datetime,
    client_signer_ip: Optional[str],
    client_signer_user_agent: Optional[str],
    client_authority_confirmed: bool,
    client_accepted: bool,
    client_signature: dict[str, Any],
    ba_signer_name: str,
    ba_signer_title: str,
    ba_signer_email: str,
    ba_signed_at: datetime,
    ba_signer_ip: Optional[str],
    ba_signer_user_agent: Optional[str],
    ba_authority_confirmed: bool,
    ba_accepted: bool,
    ba_signature: dict[str, Any],
) -> bytes:
    if fitz is None:
        raise AgreementServiceError(
            "pdf_dependency_missing",
            "PyMuPDF is required to generate signed agreement PDFs.",
            status=500,
        )
    try:
        document = fitz.open(stream=draft_pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise AgreementServiceError(
            "draft_pdf_invalid",
            "Draft agreement PDF could not be opened.",
            status=500,
        ) from exc

    try:
        page = document.new_page(width=612, height=792)
        margin = 54
        title_rect = fitz.Rect(margin, 48, 612 - margin, 92)
        page.insert_textbox(
            title_rect,
            "Signature Certificate",
            fontsize=18,
            fontname="helv",
            color=(0.04, 0.08, 0.28),
        )
        certificate_lines = [
            "Document: BUSINESS ASSOCIATE AGREEMENT (BAA)",
            f"Agreement ID: {agreement_id}",
            f"Template version: {AGREEMENT_TEMPLATE_VERSION}",
            f"Client legal name: {payload_values.get('clientLegalName') or ''}",
            f"Client email: {payload_values.get('clientEmail') or ''}",
            f"Effective date: {payload_values.get('effectiveDateDisplay') or ''}",
        ]
        page.insert_textbox(
            fitz.Rect(margin, 100, 612 - margin, 174),
            "\n".join(certificate_lines),
            fontsize=9,
            fontname="helv",
            color=(0.04, 0.08, 0.28),
            lineheight=1.25,
        )

        def insert_party_section(
            y: int,
            heading: str,
            lines: list[str],
            signature: dict[str, Any],
        ) -> None:
            page.insert_textbox(
                fitz.Rect(margin, y, 612 - margin, y + 24),
                heading,
                fontsize=12,
                fontname="helv",
                color=(0.04, 0.08, 0.28),
            )
            page.insert_textbox(
                fitz.Rect(margin, y + 30, 612 - margin, y + 138),
                "\n".join(lines),
                fontsize=8,
                fontname="helv",
                color=(0.04, 0.08, 0.28),
                lineheight=1.12,
            )
            signature_bytes = signature.get("buffer")
            if not signature_bytes:
                return
            page.insert_textbox(
                fitz.Rect(margin, y + 146, 612 - margin, y + 164),
                "Captured signature:",
                fontsize=8,
                fontname="helv",
                color=(0.04, 0.08, 0.28),
            )
            page.insert_image(
                fitz.Rect(margin, y + 166, margin + 230, y + 232),
                stream=signature_bytes,
                keep_proportion=True,
            )

        insert_party_section(
            180,
            "Client / Covered Entity",
            [
                f"Name: {client_signer_name}",
                f"Title: {client_signer_title}",
                f"Email: {payload_values.get('signerEmail') or ''}",
                f"Signed at: {client_signed_at.isoformat()}",
                f"Authority confirmed: {'Yes' if client_authority_confirmed else 'No'}",
                f"Agreement accepted: {'Yes' if client_accepted else 'No'}",
                f"IP address: {client_signer_ip or 'Unknown'}",
                f"User agent: {(client_signer_user_agent or 'Unknown')[:180]}",
                f"Signature SHA-256: {client_signature.get('sha256') or ''}",
            ],
            client_signature,
        )
        insert_party_section(
            470,
            "BA / alphaSource Consulting",
            [
                f"Name: {ba_signer_name}",
                f"Title: {ba_signer_title}",
                f"Email: {ba_signer_email}",
                f"Signed at: {ba_signed_at.isoformat()}",
                f"Authority confirmed: {'Yes' if ba_authority_confirmed else 'No'}",
                f"Agreement accepted: {'Yes' if ba_accepted else 'No'}",
                f"IP address: {ba_signer_ip or 'Unknown'}",
                f"User agent: {(ba_signer_user_agent or 'Unknown')[:180]}",
                f"Signature SHA-256: {ba_signature.get('sha256') or ''}",
            ],
            ba_signature,
        )
        try:
            return document.tobytes(garbage=4, deflate=True)
        except TypeError:
            return document.write()
    finally:
        document.close()


def parse_signature_image(value: object) -> dict[str, Any]:
    source = normalize_text(value)
    if not source:
        raise AgreementServiceError("signature_required", "A signature image is required.")
    match = re.match(r"^data:(image/(?:png|jpeg|jpg|webp));base64,([a-z0-9+/=\s]+)$", source, re.I)
    if not match:
        raise AgreementServiceError("signature_invalid", "Signature image must be a PNG, JPEG, or WEBP data URL.")
    mime = match.group(1).lower()
    if mime == "image/jpg":
        mime = "image/jpeg"
    try:
        buffer = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
    except Exception as exc:
        raise AgreementServiceError("signature_invalid", "Signature image could not be decoded.") from exc
    if not buffer:
        raise AgreementServiceError("signature_invalid", "Signature image could not be decoded.")
    if len(buffer) > MAX_SIGNATURE_IMAGE_BYTES:
        raise AgreementServiceError("signature_too_large", "Signature image is too large.")
    extension = "png" if mime == "image/png" else "webp" if mime == "image/webp" else "jpg"
    return {
        "buffer": buffer,
        "mime": mime,
        "extension": extension,
        "sha256": hashlib.sha256(buffer).hexdigest(),
    }


def upload_agreement_file(path: str, content: bytes, content_type: str, *, upsert: bool = False) -> None:
    client = _get_supabase_admin_client()
    if not client:
        raise AgreementServiceError(
            "supabase_storage_not_configured",
            "Supabase storage is not configured.",
            status=500,
        )
    file_options = {"content-type": content_type}
    if upsert:
        file_options["upsert"] = "true"
    try:
        client.storage.from_(AGREEMENTS_BUCKET).upload(
            path,
            content,
            file_options,
        )
    except Exception as exc:
        _log_agreement_storage_upload_failure(path, content, content_type, upsert, exc)
        raise AgreementServiceError(
            "agreement_storage_upload_failed",
            "Agreement file upload failed.",
            status=502,
        ) from exc


def _log_agreement_storage_upload_failure(
    path: str,
    content: bytes,
    content_type: str,
    upsert: bool,
    exc: Exception,
) -> None:
    parts = [part for part in str(path or "").split("/") if part]
    agreement_id = parts[1] if len(parts) >= 2 and parts[0] == "agreements" else None
    category = parts[-2] if len(parts) >= 2 else "root"
    basename = parts[-1] if parts else "unknown"
    error_message = re.sub(r"\s+", " ", str(getattr(exc, "message", None) or exc)).strip()[:240]
    logger.warning(
        "[agreements_storage] upload failed agreement_id=%s object=%s/%s content_type=%s extension=%s bytes=%s upsert=%s error_type=%s status=%s code=%s message=%s",
        agreement_id or "unknown",
        category,
        basename,
        content_type,
        basename.rsplit(".", 1)[-1] if "." in basename else "unknown",
        len(content or b""),
        bool(upsert),
        type(exc).__name__,
        getattr(exc, "status", None) or getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        error_message,
    )


def download_agreement_file(path: str) -> bytes:
    client = _get_supabase_admin_client()
    if not client:
        raise AgreementServiceError(
            "supabase_storage_not_configured",
            "Supabase storage is not configured.",
            status=500,
        )
    try:
        response = client.storage.from_(AGREEMENTS_BUCKET).download(path)
    except Exception as exc:
        raise AgreementServiceError(
            "agreement_storage_download_failed",
            "Agreement file download failed.",
            status=502,
        ) from exc
    if isinstance(response, bytes):
        return response
    data = getattr(response, "data", None)
    if isinstance(data, bytes):
        return data
    if isinstance(response, bytearray):
        return bytes(response)
    raise AgreementServiceError(
        "agreement_storage_download_failed",
        "Agreement file download failed.",
        status=502,
    )


def create_agreement_signed_url(path: Optional[str], expires_in: Optional[int] = None) -> Optional[str]:
    object_path = normalize_text(path)
    if not object_path:
        return None
    client = _get_supabase_admin_client()
    if not client:
        raise AgreementServiceError(
            "supabase_storage_not_configured",
            "Supabase storage is not configured.",
            status=500,
        )
    ttl = max(60, int(expires_in or AGREEMENTS_SIGNED_URL_TTL_SECONDS))
    try:
        response = client.storage.from_(AGREEMENTS_BUCKET).create_signed_url(object_path, ttl)
    except Exception as exc:
        raise AgreementServiceError(
            "agreement_signed_url_failed",
            "Unable to create agreement download URL.",
            status=502,
        ) from exc
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return response.get("signedURL") or response.get("signedUrl") or response.get("signed_url")
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return data.get("signedURL") or data.get("signedUrl") or data.get("signed_url")
    return None


def agreement_email_configured() -> bool:
    return bool(os.getenv("SENDGRID_API_KEY") and os.getenv("FROM_EMAIL"))


def send_agreement_signature_request_email(
    to_email: str,
    signing_url: str,
    *,
    client_legal_name: str,
    expires_at: datetime,
) -> dict[str, Any]:
    if not agreement_email_configured():
        raise AgreementServiceError(
            "agreement_email_not_configured",
            "SendGrid agreement email configuration is missing.",
            status=500,
        )
    subject = "BAA/Privacy Agreement signature requested"
    expires_label = expires_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return _send_email(
        to_email,
        subject,
        plain_text=(
            "alphaSource Consulting BAA/Privacy Agreement\n\n"
            f"Please review and sign the agreement for {client_legal_name}.\n\n"
            f"Secure signing link: {signing_url}\n"
            f"This link expires on {expires_label}.\n\n"
            "alphaSource Consulting"
        ),
        html_content=_email_shell(
            "BAA/Privacy Agreement signature requested",
            f"""
            <p>Please review and sign the BAA/Privacy Agreement for <strong>{_escape(client_legal_name)}</strong>.</p>
            <p><a class="cta" href="{_escape(signing_url)}">Review and sign</a></p>
            <p>This secure link expires on <strong>{_escape(expires_label)}</strong>.</p>
            """,
        ),
    )


def send_agreement_ba_countersign_request_email(
    to_email: str,
    signing_url: str,
    *,
    client_legal_name: str,
    expires_at: datetime,
) -> dict[str, Any]:
    if not agreement_email_configured():
        raise AgreementServiceError(
            "agreement_email_not_configured",
            "SendGrid agreement email configuration is missing.",
            status=500,
        )
    subject = "BAA/Privacy Agreement countersignature requested"
    expires_label = expires_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return _send_email(
        to_email,
        subject,
        plain_text=(
            "alphaSource Consulting BAA/Privacy Agreement\n\n"
            f"The client signature for {client_legal_name} has been captured. Please review and countersign.\n\n"
            f"Secure signing link: {signing_url}\n"
            f"This link expires on {expires_label}.\n\n"
            "alphaSource Consulting"
        ),
        html_content=_email_shell(
            "BAA/Privacy Agreement countersignature requested",
            f"""
            <p>The client signature for <strong>{_escape(client_legal_name)}</strong> has been captured.</p>
            <p><a class="cta" href="{_escape(signing_url)}">Review and countersign</a></p>
            <p>This secure link expires on <strong>{_escape(expires_label)}</strong>.</p>
            """,
        ),
    )


def send_agreement_signed_copy_email(
    to_email: str,
    signed_url: str,
    *,
    client_legal_name: str,
    signed_at: datetime,
    company_copy: bool = False,
) -> dict[str, Any]:
    if not agreement_email_configured():
        return {"skipped": True, "reason": "sendgrid_not_configured"}
    subject = "Signed BAA/Privacy Agreement available"
    signed_label = signed_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    audience_line = (
        "A signed BAA/Privacy Agreement is available for company records."
        if company_copy
        else "Your signed BAA/Privacy Agreement is available."
    )
    return _send_email(
        to_email,
        subject,
        plain_text=(
            f"{audience_line}\n\n"
            f"Client: {client_legal_name}\n"
            f"Signed at: {signed_label}\n"
            f"Secure download link: {signed_url}\n\n"
            "alphaSource Consulting"
        ),
        html_content=_email_shell(
            "Signed BAA/Privacy Agreement available",
            f"""
            <p>{_escape(audience_line)}</p>
            <p><strong>Client:</strong> {_escape(client_legal_name)}<br>
            <strong>Signed at:</strong> {_escape(signed_label)}</p>
            <p><a class="cta" href="{_escape(signed_url)}">Open signed agreement</a></p>
            <p>The download link is time-limited.</p>
            """,
        ),
    )


def _send_email(to_email: str, subject: str, *, plain_text: str, html_content: str) -> dict[str, Any]:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import ClickTracking, Mail, TrackingSettings

    message = Mail(
        from_email=os.getenv("FROM_EMAIL"),
        to_emails=to_email,
        subject=subject,
        plain_text_content=plain_text,
        html_content=html_content,
    )
    message.tracking_settings = TrackingSettings()
    message.tracking_settings.click_tracking = ClickTracking(enable=False, enable_text=False)
    response = SendGridAPIClient(api_key=os.getenv("SENDGRID_API_KEY")).send(message)
    return {"statusCode": getattr(response, "status_code", None) or getattr(response, "statusCode", None)}


def _email_shell(title: str, body_html: str) -> str:
    return f"""
    <html>
      <body style="margin:0;padding:0;background:#F8F9FD;font-family:Arial,sans-serif;color:#0A1547;">
        <table width="100%" cellpadding="0" cellspacing="0" role="presentation" style="background:#F8F9FD;padding:28px 16px;">
          <tr>
            <td align="center">
              <table width="560" cellpadding="0" cellspacing="0" role="presentation" style="background:#ffffff;border-radius:18px;padding:28px;border:1px solid rgba(10,21,71,0.10);">
                <tr><td style="font-size:22px;line-height:1.25;font-weight:800;color:#0A1547;">{_escape(title)}</td></tr>
                <tr><td style="padding-top:16px;font-size:15px;line-height:1.65;color:rgba(10,21,71,0.72);">{body_html}</td></tr>
                <tr><td style="padding-top:18px;border-top:1px solid rgba(10,21,71,0.08);font-size:12px;color:rgba(10,21,71,0.48);">alphaSource Consulting · All rights reserved.</td></tr>
              </table>
            </td>
          </tr>
        </table>
      </body>
    </html>
    """.strip()


def _escape(value: object) -> str:
    return html.escape(str(value or ""), quote=True)
