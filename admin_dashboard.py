import html
import inspect
import json
import logging
import os
import re
import time
import uuid
import math
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse
import requests
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

import streamlit as st
import streamlit.components.v1 as components
from sqlalchemy import func, or_, text
from fpdf import FPDF

from database import SessionLocal
from models import ClientSubmission, Upload, UploadPortalFile, User, delete_user, get_db, update_submission_status
from supabase_utils import (
    persist_upload_file,
    update_upload_file_upload_id,
    sign_in_admin,
    is_admin_user,
    send_admin_password_reset,
    update_password_with_recovery_token,
    verify_password_recovery_token,
    _get_supabase_admin_client,
    SUPABASE_URL,
)
from upload_portal import PortalError, create_upload_request

MST_FALLBACK = timezone(timedelta(hours=-7), name="MST")
MST_TZ = ZoneInfo("America/Denver") if ZoneInfo else MST_FALLBACK
try:
    _BUTTON_SUPPORTS_WIDTH = "width" in inspect.signature(st.button).parameters
except (TypeError, ValueError):
    _BUTTON_SUPPORTS_WIDTH = False


class AdminPerfTracker:
    def __init__(self) -> None:
        self.start = time.perf_counter()
        self.first_db_logged = False

    def log(self, step: str) -> None:
        elapsed_ms = (time.perf_counter() - self.start) * 1000
        logging.info("admin_step=%s ms=%.1f", step, elapsed_ms)

    def mark_first_db_query(self) -> None:
        if not self.first_db_logged:
            self.first_db_logged = True
            self.log("first_db_query")

def try_first_db_query(fn, attempts: int = 3, sleep_s: float = 0.5):
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            logging.exception(
                "[db] first_query_failed attempt=%s/%s type=%s msg=%s",
                attempt,
                attempts,
                type(exc).__name__,
                str(exc)[:200],
            )
            if attempt < attempts:
                time.sleep(sleep_s * attempt)
            else:
                raise


def normalize_email(raw_email: str) -> str:
    if not raw_email:
        return ""
    return raw_email.strip().lower()

class AdminCancelledError(BaseException):
    pass

def _check_admin_cancel(where: str, run_id: str) -> None:
    if st.session_state.get("admin_cancel_requested"):
        logging.info("[analysis] canceled run_id=%s where=%s source=admin", run_id, where)
        raise AdminCancelledError("cancel_requested")

_UNSET = object()

def _update_submission_ghl_fields(db, submission_id, ghl_cid=_UNSET, submitted_at=_UNSET, error_msg=_UNSET) -> None:
    fields = []
    params = {"id": str(submission_id)}
    if ghl_cid is not _UNSET:
        fields.append("ghl_cid = :ghl_cid")
        params["ghl_cid"] = ghl_cid
    if submitted_at is not _UNSET:
        fields.append("ghl_analyzer_submitted_at = :submitted_at")
        params["submitted_at"] = submitted_at
    if error_msg is not _UNSET:
        fields.append("ghl_analyzer_submitted_error = :error_msg")
        params["error_msg"] = error_msg
    if not fields:
        return
    stmt = text(f"update client_submissions set {', '.join(fields)} where id = :id")
    db.execute(stmt, params)
    db.commit()

def _ghl_update_analyzer_submitted(cid: str) -> tuple[bool, str]:
    if not cid:
        return False, "missing cid"
    base_url = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com").rstrip("/")
    token = os.getenv("GHL_BEARER_TOKEN", "")
    version = os.getenv("GHL_API_VERSION", "2021-07-28")
    field_id = os.getenv("GHL_ANALYZER_SUBMITTED_FIELD_ID", "").strip()
    if not token:
        return False, "missing bearer token"
    if not field_id:
        return False, "missing analyzer field id"
    url = f"{base_url}/contacts/{cid}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Version": version,
    }
    payload = {"customFields": [{"id": field_id, "value": ["Submitted"]}]}
    try:
        response = requests.put(url, headers=headers, json=payload, timeout=10)
    except requests.RequestException:
        return False, "request failed"
    if response.status_code in (200, 201, 202, 204):
        return True, ""
    if response.status_code in (404, 405):
        alt_url = f"{base_url}/contacts"
        alt_payload = {"id": cid, "customFields": [{"id": field_id, "value": ["Submitted"]}]}
        try:
            alt_response = requests.put(alt_url, headers=headers, json=alt_payload, timeout=10)
        except requests.RequestException:
            return False, "request failed"
        if alt_response.status_code in (200, 201, 202, 204):
            return True, ""
        return False, f"status {alt_response.status_code}"
    return False, f"status {response.status_code}"

def _ghl_add_tag(cid: str, tag_name: str) -> tuple[bool, str]:
    if not cid:
        return False, "missing cid"
    if not tag_name:
        return False, "missing tag name"
    base_url = os.getenv("GHL_BASE_URL", "https://services.leadconnectorhq.com").rstrip("/")
    token = os.getenv("GHL_BEARER_TOKEN", "")
    version = os.getenv("GHL_API_VERSION", "2021-07-28")
    location_id = os.getenv("LOCATION_ID", "").strip()
    if not token:
        return False, "missing bearer token"
    if not location_id:
        logging.warning("[ghl] add_tag missing location id for cid %s", cid)
        return False, "missing location id"
    url = f"{base_url}/contacts/{cid}/tags"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Version": version,
        "LocationId": location_id,
    }
    payload = {"tags": [tag_name]}
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=10)
    except requests.RequestException:
        return False, "request failed"
    if response.status_code in (200, 201, 202, 204):
        return True, ""
    body = (response.text or "").strip().replace("\n", " ")
    truncated = body[:300]
    logging.warning(
        "[ghl] add_tag failed cid=%s tag=%s status=%s body=%s",
        cid,
        tag_name,
        response.status_code,
        truncated,
    )
    return False, f"status {response.status_code} body {truncated}"


def _token_ttl_minutes() -> int:
    raw = os.getenv("PORTAL_TOKEN_TTL_MINUTES", "60")
    try:
        return max(1, int(raw))
    except ValueError:
        return 60


def _parse_date_input(value: str) -> datetime.date:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def format_mst(dt: datetime) -> str:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    mst = dt.astimezone(MST_TZ)
    return mst.strftime("%m-%d-%Y %H:%M MST")


def _format_admin_dt(value: object):
    if not value:
        return None
    dt = None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            try:
                dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return raw
    else:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_mst(dt)


def _parse_analysis_json(value: object):
    if not value:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (TypeError, json.JSONDecodeError):
            return None
    return None

def _wrap_long_tokens(text: str, max_token_length: int = 28) -> str:
    if not text:
        return ""
    parts = re.split(r"(\s+)", text)
    wrapped = []
    for part in parts:
        if not part or part.isspace():
            wrapped.append(part)
            continue
        if len(part) <= max_token_length:
            wrapped.append(part)
            continue
        chunks = [part[i:i + max_token_length] for i in range(0, len(part), max_token_length)]
        wrapped.append(" ".join(chunks))
    return "".join(wrapped)

def _sanitize_pdf_text(value: object, field_label: str | None = None) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        try:
            text = value.decode("utf-8", errors="replace")
        except Exception:
            text = bytes(value).decode("latin-1", errors="replace")
    else:
        text = value if isinstance(value, str) else str(value)
    text = (
        text.replace("\u2022", "- ")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201C", '"')
        .replace("\u201D", '"')
        .replace("\u00A0", " ")
    )
    cleaned = re.sub(r"[\r\n\t]+", " ", text)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = _wrap_long_tokens(cleaned)
    return cleaned.encode("latin-1", "replace").decode("latin-1")

def find_repo_root(start_dir: str) -> str:
    current = start_dir
    for _ in range(7):
        if (
            os.path.isdir(os.path.join(current, "public"))
            and os.path.isdir(os.path.join(current, "raleway"))
        ):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return start_dir

def _safe_path_component(value: str) -> str:
    if not value:
        return "unknown"
    safe = value.strip().replace("/", "_").replace("\\", "_").replace(" ", "_")
    return safe

def _extract_supabase_report_path(pdf_url: str, bucket: str = "consulting-uploads") -> str:
    if not pdf_url:
        return ""
    if pdf_url.startswith("reports/"):
        return pdf_url
    if pdf_url.startswith(f"{bucket}/"):
        return pdf_url[len(bucket) + 1:]
    try:
        parsed = urlparse(pdf_url)
        path = unquote(parsed.path or "")
    except Exception:
        path = pdf_url
    markers = [
        f"/storage/v1/object/public/{bucket}/",
        f"/storage/v1/object/sign/{bucket}/",
        f"/storage/v1/object/{bucket}/",
        f"/{bucket}/",
    ]
    for marker in markers:
        if marker in path:
            return path.split(marker, 1)[1]
    return ""

def _extract_opportunities(payload: dict) -> list:
    items = []
    if not payload:
        return items
    deduplicated = payload.get("deduplicated_issues", [])
    if not isinstance(deduplicated, list):
        return items
    for issue in deduplicated:
        if not isinstance(issue, dict):
            continue
        title = (issue.get("title") or "").strip()
        impact = (issue.get("impact") or "").strip()
        recommendation = (issue.get("recommendation") or "").strip()
        if title or impact or recommendation:
            items.append(
                {
                    "title": title,
                    "impact": impact,
                    "recommendation": recommendation,
                }
            )
    return items

def _extract_trends(payload: dict) -> list:
    items = []
    if not payload:
        return items
    trends = payload.get("all_trends", [])
    if not isinstance(trends, list):
        return items
    for trend in trends:
        if isinstance(trend, dict):
            text = trend.get("text") or ""
        else:
            text = str(trend)
        text = text.strip()
        if text:
            items.append(text)
    return items

def _extract_key_trends(payload: dict) -> list:
    if not payload:
        return []
    try:
        from analysis_utils import extract_compelling_insights
        return extract_compelling_insights(payload, max_insights=5)
    except Exception:
        return []

def _safe_pdf_multi_cell(pdf: FPDF, text: str, field_label: str, height: int = 6, width: float = 0) -> None:
    raw_value = text
    raw_text = "" if raw_value is None else (raw_value if isinstance(raw_value, str) else str(raw_value))
    safe_text = _sanitize_pdf_text(raw_value, field_label=field_label)
    safe_width = 0.0
    try:
        safe_width = float(width or 0)
    except (TypeError, ValueError):
        safe_width = 0.0
    if not math.isfinite(safe_width):
        safe_width = 0.0
    if safe_width <= 0:
        full_line_width = pdf.w - pdf.l_margin - pdf.r_margin
        if not math.isfinite(full_line_width):
            full_line_width = 0.0
        if pdf.get_x() > (pdf.l_margin + 1):
            pdf.set_x(pdf.l_margin)
        safe_width = full_line_width
    if safe_width <= 1:
        logging.error(
            "[pdf] invalid width field=%s width=%s computed=%.2f x=%.2f y=%.2f page=%s l_margin=%.2f r_margin=%.2f",
            field_label,
            width,
            safe_width,
            pdf.get_x(),
            pdf.get_y(),
            pdf.page_no(),
            pdf.l_margin,
            pdf.r_margin,
        )
        safe_width = 10.0
    try:
        pdf.multi_cell(safe_width, height, safe_text)
    except Exception as exc:
        snippet = safe_text if len(safe_text) <= 200 else f"{safe_text[:200]}..."
        logging.error(
            "[pdf] render failed field=%s error=%s text=%s",
            field_label,
            str(exc),
            snippet,
        )
        raise

def _pdf_output_bytes(pdf: FPDF) -> bytes:
    out = pdf.output(dest="S")
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    if isinstance(out, str):
        return out.encode("latin-1", errors="replace")
    return str(out).encode("latin-1", errors="replace")

def _render_pdf_metadata_row(
    pdf: FPDF,
    label: str,
    value: object,
    field_label: str,
    label_width: float = 40,
    height: int = 6,
    min_value_width: float = 30,
    label_color: tuple[int, int, int] | None = None,
    value_color: tuple[int, int, int] | None = None,
    label_size: int = 9,
    value_size: int = 10,
    start_x: float | None = None,
    font_family: str = "Helvetica",
) -> None:
    safe_label = _sanitize_pdf_text(label)
    safe_value = _sanitize_pdf_text(value or "-", field_label=field_label)
    left_x = pdf.l_margin if start_x is None else start_x
    full_width = pdf.w - left_x - pdf.r_margin
    if not math.isfinite(full_width) or full_width <= 0:
        full_width = label_width + min_value_width
    label_width = min(label_width, full_width)
    value_width = full_width - label_width
    start_y = pdf.get_y()

    pdf.set_x(left_x)
    if label_color:
        pdf.set_text_color(*label_color)
    pdf.set_font(font_family, "", label_size)
    if value_width < min_value_width:
        pdf.cell(full_width, height, f"{safe_label}:", ln=1)
        if value_color:
            pdf.set_text_color(*value_color)
        pdf.set_font(font_family, "", value_size)
        try:
            _safe_pdf_multi_cell(pdf, safe_value, field_label, height=height, width=full_width)
        except Exception:
            logging.error(
                "[pdf] metadata render failed field=%s label_w=%.2f value_w=%.2f full_w=%.2f used_w=%.2f x=%.2f y=%.2f",
                field_label,
                label_width,
                value_width,
                full_width,
                full_width,
                pdf.get_x(),
                pdf.get_y(),
            )
            raise
        pdf.set_x(pdf.l_margin)
        return

    pdf.cell(label_width, height, f"{safe_label}:", ln=0)
    if value_color:
        pdf.set_text_color(*value_color)
    pdf.set_font(font_family, "", value_size)
    pdf.set_xy(left_x + label_width, start_y)
    try:
        _safe_pdf_multi_cell(pdf, safe_value, field_label, height=height, width=value_width)
    except Exception:
        logging.error(
            "[pdf] metadata render failed field=%s label_w=%.2f value_w=%.2f full_w=%.2f x=%.2f y=%.2f",
            field_label,
            label_width,
            value_width,
            full_width,
            pdf.get_x(),
            pdf.get_y(),
        )
        raise
    pdf.set_x(pdf.l_margin)

def _generate_pdf_bytes(metadata: dict, sections: dict, notes: str, version: int) -> bytes:
    background = (10, 21, 71)
    card = (15, 30, 93)
    border = (34, 48, 106)
    primary = (230, 235, 255)
    secondary = (201, 211, 255)
    subtle = (107, 119, 201)

    class StyledPDF(FPDF):
        def __init__(self, bg_color: tuple[int, int, int]):
            super().__init__()
            self._bg_color = bg_color
            self.total_pages = None
            self.footer_font_family = "Helvetica"
            self.footer_subtle = subtle
            self.footer_secondary = secondary

        def header(self) -> None:
            self.set_fill_color(*self._bg_color)
            self.rect(0, 0, self.w, self.h, "F")

        def footer(self) -> None:
            if not self.total_pages or self.page_no() != self.total_pages:
                return
            font_family = self.footer_font_family or "Helvetica"
            line_height = 5
            self.set_y(-18)
            self.set_font(font_family, "", 9)
            self.set_text_color(*self.footer_subtle)
            prefix = "Need help or have questions? Email: "
            email = "info@alphasourceai.com"
            self.set_x(self.l_margin)
            try:
                self.write(line_height, _sanitize_pdf_text(prefix))
                self.set_text_color(*self.footer_secondary)
                self.write(line_height, _sanitize_pdf_text(email), link=f"mailto:{email}")
            except Exception:
                self.set_text_color(*self.footer_subtle)
                self.cell(0, line_height, _sanitize_pdf_text(f"{prefix}{email}"), ln=1)
            else:
                self.ln(line_height)
            self.set_text_color(*self.footer_subtle)
            footer_line = "alphaSource Consulting — All rights reserved."
            if font_family == "Helvetica":
                footer_line = _sanitize_pdf_text(footer_line)
            self.cell(0, line_height, footer_line, ln=0)

    pdf = StyledPDF(background)
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(auto=True, margin=28)

    repo_root = find_repo_root(os.path.abspath(os.path.dirname(__file__)))
    regular_family = "Raleway"
    bold_family = "RalewayBold"
    font_path = os.path.join(repo_root, "raleway", "static", "Raleway-Regular.ttf")
    bold_font_path = os.path.join(repo_root, "raleway", "static", "Raleway-Bold.ttf")
    has_regular = os.path.exists(font_path)
    has_bold = os.path.exists(bold_font_path)
    has_bold_face = False
    if has_regular:
        pdf.add_font(regular_family, "", font_path, uni=True)
        if has_bold:
            try:
                pdf.add_font(bold_family, "", bold_font_path, uni=True)
                has_bold_face = True
            except Exception:
                has_bold_face = False
    else:
        regular_family = "Helvetica"
        has_bold_face = False
    font_family = regular_family
    pdf.footer_font_family = regular_family

    pdf.add_page()

    content_width = pdf.w - pdf.l_margin - pdf.r_margin

    def ensure_space(min_height: float) -> None:
        if pdf.get_y() + min_height > pdf.h - pdf.b_margin:
            pdf.add_page()

    def section_title(title: str, underline: bool = False) -> None:
        ensure_space(10)
        pdf.set_text_color(*primary)
        pdf.set_font(font_family, "", 12)
        pdf.cell(0, 8, _sanitize_pdf_text(title), ln=1)
        if underline:
            y = pdf.get_y()
            pdf.set_draw_color(*border)
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
        pdf.ln(1)

    def estimate_text_height(value: object, width: float, line_height: float, size: int) -> float:
        safe = _sanitize_pdf_text(value or "")
        if not safe:
            return line_height
        pdf.set_font(font_family, "", size)
        words = safe.split()
        space_w = pdf.get_string_width(" ")
        lines = 1
        current_w = 0.0
        for word in words:
            word_w = pdf.get_string_width(word)
            if current_w == 0:
                current_w = word_w
                continue
            if current_w + space_w + word_w <= width:
                current_w += space_w + word_w
            else:
                lines += 1
                current_w = word_w
        return lines * line_height

    def kv_row(
        label: str,
        value: object,
        field_label: str,
        label_width: float,
        line_height: float,
        label_color: tuple[int, int, int],
        value_color: tuple[int, int, int],
        label_size: int = 9,
        value_size: int = 9,
        start_x: float | None = None,
    ) -> None:
        _render_pdf_metadata_row(
            pdf,
            label,
            value,
            field_label,
            label_width=label_width,
            height=line_height,
            label_color=label_color,
            value_color=value_color,
            label_size=label_size,
            value_size=value_size,
            start_x=start_x,
            font_family=font_family,
        )

    BULLET_FONT_SIZE = 10
    BULLET_LINE_HEIGHT = 6

    def render_bullet(
        text: str,
        field_label: str,
        font_family_override: str | None = None,
        size: int = BULLET_FONT_SIZE,
    ) -> None:
        safe_text = text.strip()
        if not safe_text:
            return
        pdf.set_text_color(*secondary)
        bullet_family = font_family_override or font_family
        pdf.set_font(bullet_family, "", size)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(
            content_width,
            BULLET_LINE_HEIGHT,
            _sanitize_pdf_text(f"- {safe_text}"),
            align="L",
        )
        pdf.ln(1)

    def render_opportunity(item: object, idx: int) -> None:
        if isinstance(item, dict):
            title_text = (item.get("title") or "").strip()
            impact_text = (item.get("impact") or "").strip()
            rec_text = (item.get("recommendation") or "").strip()
            parts = []
            if title_text:
                parts.append(("issue", f"Issue: {title_text}"))
            if impact_text:
                parts.append(("impact", f"Impact: {impact_text}"))
            if rec_text:
                parts.append(("recommendation", f"Recommendation: {rec_text}"))
            for label, text in parts:
                if label == "issue" and has_bold_face:
                    render_bullet(
                        text,
                        f"opportunity:{idx}:{label}",
                        font_family_override=bold_family,
                    )
                else:
                    render_bullet(text, f"opportunity:{idx}:{label}")
            return
        combined = str(item).strip() if item is not None else ""
        if combined:
            render_bullet(combined, f"opportunity:{idx}")

    logo_path = os.path.join(repo_root, "public", "logo with bg color 1128.png")
    if os.path.exists(logo_path):
        logo_y = pdf.get_y()
        try:
            pdf.image(logo_path, x=pdf.l_margin, y=logo_y, w=85)
            pdf.set_y(logo_y + 24)
        except Exception:
            pass

    pdf.set_text_color(*primary)
    pdf.set_font(font_family, "", 22)
    pdf.cell(0, 12, _sanitize_pdf_text("Your Detailed Analysis Report"), ln=1)
    pdf.ln(1)

    section_title("Client Details")
    report_date = datetime.utcnow()
    report_date_text = f"{report_date:%b} {report_date.day}, {report_date:%Y}"
    details = [
        ("Client Name", metadata.get("client_name")),
        ("Office/Group", metadata.get("office_name")),
        ("Client Email", metadata.get("client_email")),
        ("Tool", metadata.get("tool_name")),
        ("Date", report_date_text),
    ]
    detail_padding_x = 6
    detail_padding_y = 4
    detail_gap = 1
    detail_label_width = 40
    detail_line_height = 6
    detail_value_width = content_width - (detail_padding_x * 2) - detail_label_width
    detail_height = detail_padding_y * 2
    for _, value in details:
        detail_height += estimate_text_height(value, detail_value_width, detail_line_height, 9)
    if len(details) > 1:
        detail_height += detail_gap * (len(details) - 1)
    ensure_space(detail_height + 2)
    detail_card_y = pdf.get_y()
    pdf.set_fill_color(*card)
    pdf.set_draw_color(*border)
    pdf.rect(pdf.l_margin, detail_card_y, content_width, detail_height, "FD")
    pdf.set_xy(pdf.l_margin + detail_padding_x, detail_card_y + detail_padding_y)
    for row_idx, (label, value) in enumerate(details, start=1):
        kv_row(
            label,
            value,
            f"metadata:{label}",
            label_width=detail_label_width,
            line_height=detail_line_height,
            label_color=subtle,
            value_color=secondary,
            label_size=9,
            value_size=9,
            start_x=pdf.l_margin + detail_padding_x,
        )
        if row_idx < len(details):
            pdf.ln(detail_gap)
    pdf.ln(12)

    section_specs = [
        ("opportunities", "Improvement Opportunities"),
        ("trends", "Trends"),
        ("key_trends", "Key Trends Identified"),
    ]
    for key, title in section_specs:
        items = sections.get(key) or []
        if not items:
            continue
        section_title(title, underline=True)
        if key == "opportunities":
            for idx, item in enumerate(items, start=1):
                render_opportunity(item, idx)
        else:
            for idx, item in enumerate(items, start=1):
                render_bullet(str(item).strip(), f"{key}:{idx}")
        pdf.ln(1)

    if notes:
        section_title("Additional Notes")
        pdf.set_text_color(*secondary)
        pdf.set_font(font_family, "", 10)
        _safe_pdf_multi_cell(
            pdf,
            notes,
            "notes:body",
            height=6,
            width=content_width,
        )
        pdf.ln(1)

    pdf.total_pages = pdf.page_no()

    return _pdf_output_bytes(pdf)

def _upload_pdf_report(pdf_bytes: bytes, object_path: str) -> tuple[str, str]:
    client = _get_supabase_admin_client()
    if not client:
        return "", "Supabase admin client is not configured"
    bucket = "consulting-uploads"
    try:
        client.storage.from_(bucket).upload(
            object_path,
            pdf_bytes,
            {"content-type": "application/pdf", "upsert": False},
        )
    except Exception as exc:
        return "", str(exc)

    public_url = ""
    try:
        response = client.storage.from_(bucket).get_public_url(object_path)
        if isinstance(response, dict):
            public_url = response.get("publicURL") or response.get("public_url") or ""
        elif isinstance(response, str):
            public_url = response
    except Exception:
        public_url = ""

    if not public_url and SUPABASE_URL:
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{object_path}"

    return public_url, ""

def _create_report_signed_url(path: str, expires_in: int = 3600) -> str | None:
    if not path:
        return None
    client = _get_supabase_admin_client()
    if not client:
        logging.warning("[pdf] signed url missing client path=%s", path)
        return None
    bucket = "consulting-uploads"
    try:
        response = client.storage.from_(bucket).create_signed_url(path, expires_in)
    except Exception as exc:
        logging.warning(
            "[pdf] signed url failed path=%s err=%s",
            path,
            str(exc),
        )
        return None
    signed_url = ""
    if isinstance(response, dict):
        signed_url = response.get("signedURL") or response.get("signedUrl") or ""
    elif isinstance(response, str):
        signed_url = response
    if signed_url:
        return signed_url
    logging.warning("[pdf] signed url empty path=%s", path)
    return None


def _render_email_html(raw_email: str, height: int = 24) -> None:
    if not raw_email:
        st.write("-")
        return
    safe_email = html.escape(raw_email).replace("@", "&#64;")
    components.html(
        f"""
        <html>
            <head>
                <style>
                    body {{
                        margin: 0;
                        padding: 0;
                        background: transparent;
                        font-family: 'Raleway', system-ui, -apple-system, sans-serif;
                    }}
                    .as-email {{
                        color: #1A2460;
                        font-weight: 600;
                        font-size: 0.95rem;
                        text-decoration: none;
                    }}
                </style>
            </head>
            <body>
                <span class="as-email">{safe_email}</span>
            </body>
        </html>
        """,
        height=height,
        scrolling=False,
    )


def _ghl_contact_url(cid: str) -> str:
    if not cid:
        return ""
    location_id = os.getenv("LOCATION_ID", "").strip()
    if not location_id:
        return ""
    return (
        "https://app.gohighlevel.com/v2/location/"
        f"{quote(location_id)}/contacts/detail/{quote(cid)}"
    )


def _ensure_admin_state() -> None:
    if "is_admin_logged_in" not in st.session_state:
        st.session_state.is_admin_logged_in = False
    if "admin_session" not in st.session_state:
        st.session_state.admin_session = None
    if "admin_user" not in st.session_state:
        st.session_state.admin_user = None


def _render_admin_css() -> None:
    css = """
        <style>
        .stApp,
        [data-testid="stAppViewContainer"] {
            background: #F8F9FD !important;
            color: #0A1547 !important;
        }
        [data-testid="stHeader"] {
            background: rgba(248, 249, 253, 0.92) !important;
            border-bottom: 1px solid rgba(10, 21, 71, 0.08);
        }
        [data-testid="block-container"] {
            color: #0A1547 !important;
        }
        .stApp h1,
        .stApp h2,
        .stApp h3,
        .stApp h4,
        .stApp h5,
        .stApp h6,
        .stApp p,
        .stApp label {
            color: #0A1547 !important;
        }
        .stApp a {
            color: #02ABE0 !important;
        }
        .title-container h1 {
            color: #0A1547 !important;
            letter-spacing: 0;
        }
        .section-header {
            align-items: center;
            background: #FFFFFF;
            border: 1px solid rgba(10, 21, 71, 0.10);
            border-radius: 14px;
            display: flex;
            gap: 0.55rem;
            margin-bottom: 1rem;
            padding: 0.8rem 0.95rem;
            box-shadow: 0 14px 34px rgba(10, 21, 71, 0.06);
        }
        .section-icon {
            color: #A380F6;
            height: 1.15rem;
            width: 1.15rem;
        }
        .section-title {
            color: #0A1547;
            font-weight: 700;
        }
        [data-testid="stForm"],
        [data-testid="stExpander"],
        [data-testid="stFileUploader"],
        [data-testid="stDataFrame"] {
            background: #FFFFFF !important;
            border: 1px solid rgba(10, 21, 71, 0.10) !important;
            border-radius: 14px !important;
            box-shadow: 0 14px 34px rgba(10, 21, 71, 0.06) !important;
        }
        [data-testid="stForm"] {
            padding: 1rem 1.1rem !important;
        }
        [data-testid="stTextInput"] input,
        [data-testid="stTextArea"] textarea,
        [data-testid="stNumberInput"] input,
        [data-testid="stSelectbox"] div[data-baseweb="select"] > div,
        [data-testid="stMultiSelect"] div[data-baseweb="select"] > div {
            background: #FFFFFF !important;
            border-color: rgba(10, 21, 71, 0.14) !important;
            color: #0A1547 !important;
            box-shadow: none !important;
        }
        [data-testid="stTextInput"] input:focus,
        [data-testid="stTextArea"] textarea:focus,
        [data-testid="stNumberInput"] input:focus {
            border-color: #A380F6 !important;
            box-shadow: 0 0 0 2px rgba(163, 128, 246, 0.18) !important;
        }
        [data-testid="stTextInput"] input::placeholder,
        [data-testid="stTextArea"] textarea::placeholder {
            color: #7B829E !important;
        }
        .stButton > button,
        .stDownloadButton > button,
        [data-testid="stFormSubmitButton"] button {
            background: #FFFFFF !important;
            border: 1px solid rgba(10, 21, 71, 0.14) !important;
            color: #0A1547 !important;
            box-shadow: 0 8px 18px rgba(10, 21, 71, 0.06) !important;
        }
        .stButton > button:hover,
        .stDownloadButton > button:hover,
        [data-testid="stFormSubmitButton"] button:hover {
            border-color: rgba(163, 128, 246, 0.55) !important;
            color: #0A1547 !important;
            background: #F7F4FF !important;
        }
        .stButton > button[kind="primary"],
        [data-testid="stFormSubmitButton"] button[kind="primary"] {
            background: #A380F6 !important;
            border-color: #A380F6 !important;
            color: #FFFFFF !important;
        }
        .stButton > button:disabled,
        .stDownloadButton > button:disabled,
        [data-testid="stFormSubmitButton"] button:disabled {
            background: #F1F3F8 !important;
            border-color: rgba(10, 21, 71, 0.08) !important;
            color: #8B92A9 !important;
            opacity: 1 !important;
        }
        [data-testid="stMetric"] {
            background: #FFFFFF;
            border: 1px solid rgba(10, 21, 71, 0.10);
            border-radius: 14px;
            padding: 0.8rem 0.95rem;
            box-shadow: 0 14px 34px rgba(10, 21, 71, 0.06);
        }
        [data-testid="stAlert"] {
            border-radius: 12px !important;
            border-color: rgba(10, 21, 71, 0.10) !important;
            color: #0A1547 !important;
        }
        details > summary {
            background-color: #FFFFFF !important;
            color: #0A1547 !important;
            border: 1px solid rgba(10, 21, 71, 0.10) !important;
            border-radius: 10px !important;
            padding: 0.55rem 0.7rem !important;
        }
        details > summary:hover,
        details > summary:focus,
        details > summary:active,
        details > summary:focus-visible {
            background-color: #F7F4FF !important;
            color: #0A1547 !important;
            border-color: rgba(163, 128, 246, 0.35) !important;
        }
        .client-submissions-scope [data-testid="column"] p {
            font-size: 0.8rem !important;
            line-height: 1.25 !important;
            margin: 0.2rem 0 !important;
            white-space: nowrap !important;
            overflow: hidden !important;
            text-overflow: ellipsis !important;
        }
        .client-submissions-scope [data-testid="column"] .stMarkdown strong {
            font-size: 0.85rem !important;
            font-weight: 600 !important;
        }
        .client-submissions-scope [data-testid="column"] p {
            color: #0A1547;
        }
        .client-submissions-scope .as-subcard,
        .client-submissions-scope .as-subcard p {
            color: #1A2460;
        }
        .client-submissions-scope .stButton > button {
            padding: 0.15rem 0.3rem !important;
            font-size: 0.75rem !important;
            min-height: 1.6rem !important;
            height: 1.6rem !important;
            line-height: 1 !important;
            border-radius: 4px !important;
        }
        .client-submissions-scope [data-testid="column"] {
            padding: 0.15rem 0.4rem !important;
        }
        .client-submissions-scope [data-testid="stTextArea"] p,
        .client-submissions-scope .stAlert p,
        .client-submissions-scope .stWarning p,
        .client-submissions-scope .stError p,
        .client-submissions-scope .stSuccess p,
        .client-submissions-scope .stInfo p {
            font-size: 1rem !important;
            white-space: normal !important;
        }
        .client-submissions-scope [class*="st-key-delete_btn_"] {
            display: flex;
            justify-content: center;
        }
        .client-submissions-scope [class*="st-key-delete_btn_"] button {
            background: transparent !important;
            border: 1px solid transparent !important;
            border-radius: 999px !important;
            box-shadow: none !important;
            color: #5E6684 !important;
            font-size: 0.9rem !important;
            height: 2rem !important;
            line-height: 1 !important;
            min-height: 2rem !important;
            min-width: 2rem !important;
            padding: 0 !important;
            width: 2rem !important;
        }
        .client-submissions-scope [class*="st-key-delete_btn_"] button:hover {
            background: rgba(2, 171, 224, 0.08) !important;
            border-color: rgba(2, 171, 224, 0.18) !important;
            color: #02ABE0 !important;
        }
        .as-delete-header {
            text-align: center;
            white-space: nowrap;
        }
        .as-facts-strip {
            display: grid;
            gap: 0.45rem;
            grid-template-columns: repeat(7, minmax(0, 1fr));
            margin: 0.55rem 0 0.85rem 0;
        }
        .as-fact {
            background: #FFFFFF;
            border: 1px solid rgba(10, 21, 71, 0.10);
            border-radius: 12px;
            box-shadow: 0 8px 18px rgba(10, 21, 71, 0.04);
            padding: 0.52rem 0.58rem;
        }
        .as-fact-label {
            color: #5E6684;
            font-size: 0.62rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            line-height: 1.15;
            margin-bottom: 0.18rem;
            text-transform: uppercase;
        }
        .as-fact-value {
            color: #0A1547;
            font-size: 1rem;
            font-weight: 750;
            line-height: 1.15;
        }
        .as-fact-value--navy {
            color: #0A1547;
        }
        .as-fact-value--deep {
            color: #1A2460;
        }
        .as-fact-value--cyan {
            color: #02ABE0;
        }
        .as-fact-value--green {
            color: #02D99D;
        }
        .as-fact-value--lilac {
            color: #A380F6;
        }
        @media (max-width: 1100px) {
            .as-facts-strip {
                grid-template-columns: repeat(auto-fit, minmax(96px, 1fr));
            }
        }
        .as-client-card {
            padding: 1rem 1.1rem;
        }
        .as-client-card .as-email {
            color: #1A2460 !important;
            font-size: 1rem;
        }
        .as-detail-value {
            color: #1A2460;
            font-size: 0.92rem;
            font-weight: 550;
            line-height: 1.25;
            word-break: break-word;
        }
        .client-submissions-scope .as-muted {
            color: #5E6684 !important;
        }
        .client-submissions-scope .as-detail-value {
            color: #1A2460 !important;
        }
        .client-submissions-scope .as-compact-label {
            display: block;
            max-width: 100%;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .as-admin-upload-label {
            align-items: center;
            color: #1A2460;
            display: flex;
            gap: 0.55rem;
            margin: 0.95rem 0 0.45rem 0;
        }
        .as-admin-upload-marker {
            background: #02ABE0;
            border-radius: 999px;
            box-shadow: 0 0 0 4px rgba(2, 171, 224, 0.10);
            display: inline-block;
            height: 0.52rem;
            width: 0.52rem;
        }
        .as-admin-upload-title {
            color: #1A2460;
            font-size: 1rem;
            font-weight: 700;
            line-height: 1.2;
        }
        .as-upload-header {
            color: #5E6684;
            font-size: 0.75rem;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            display: block;
            max-width: 100%;
            line-height: 1.2;
        }
        .as-uploads-scope [class*="st-key-as_upload_legend"] {
            margin: 0 0 0.4rem 0;
        }
        .as-uploads-scope [class*="st-key-as_upload_legend"] button {
            min-width: 32px !important;
            width: 32px !important;
            min-height: 32px !important;
            height: 32px !important;
            padding: 0 !important;
            display: inline-flex !important;
            align-items: center !important;
            justify-content: center !important;
            line-height: 1 !important;
        }
        .as-uploads-scope [class*="st-key-as_upload_legend"] button:disabled {
            opacity: 1 !important;
            cursor: default !important;
        }
        .as-uploads-scope .as-upload-legend-label {
            color: #5E6684;
            font-size: 0.75rem;
            letter-spacing: 0.02em;
            white-space: nowrap;
            margin-top: 0.2rem;
        }
        .as-uploads-scope [class*="st-key-as_upload_legend_box_"],
        .as-uploads-scope [class*="st-key-as_upload_legend_box_"] > div,
        .as-uploads-scope [class*="st-key-as_upload_legend_box_"] > div > div {
            padding: 0.6rem 0.7rem !important;
            border: 1px solid rgba(10, 21, 71, 0.10) !important;
            background: #FFFFFF !important;
            border-radius: 10px !important;
            margin-bottom: 0.6rem !important;
            box-sizing: border-box !important;
        }
        .as-uploads-scope .as-upload-legend-title {
            font-size: 0.68rem;
            letter-spacing: 0.10em;
            color: #5E6684;
            text-transform: uppercase;
            margin: 0 0 0.35rem 0;
        }
        .as-uploads-scope [class*="st-key-as_upload_actions_"],
        .as-uploads-scope [class*="st-key-as_upload_actions_"] > div,
        .as-uploads-scope [class*="st-key-as_upload_actions_"] > div > div {
            background-color: transparent !important;
            background: transparent !important;
            box-shadow: none !important;
            border: 0 !important;
            outline: 0 !important;
            padding: 0 !important;
            margin: 0 !important;
            min-width: 32px !important;
            width: 32px !important;
            min-height: 32px !important;
            height: 32px !important;
            border-radius: 6px !important;
            display: flex !important;
            align-items: center !important;
            justify-content: center !important;
            overflow: hidden !important;
        }
        .as-uploads-scope [class*="st-key-as_upload_actions_"] *,
        .as-uploads-scope [class*="st-key-as_upload_actions_"] *::before,
        .as-uploads-scope [class*="st-key-as_upload_actions_"] *::after {
            background-color: transparent !important;
            background: transparent !important;
            box-shadow: none !important;
            border: 0 !important;
            outline: 0 !important;
            padding: 0 !important;
            margin: 0 !important;
        }
        .as-uploads-scope [class*="st-key-as_upload_actions_"] button,
        .as-uploads-scope [class*="st-key-as_upload_actions_"] button *,
        .as-uploads-scope [class*="st-key-as_upload_actions_"] button::before,
        .as-uploads-scope [class*="st-key-as_upload_actions_"] button::after {
            background-color: transparent !important;
            background: transparent !important;
            box-shadow: none !important;
            border: 0 !important;
            outline: 0 !important;
            padding: 0 !important;
            margin: 0 !important;
        }
        .as-uploads-scope [class*="st-key-as_upload_actions_"] button {
            min-width: 32px !important;
            width: 32px !important;
            min-height: 32px !important;
            height: 32px !important;
            border-radius: 6px !important;
            border: 1px solid rgba(2, 171, 224, 0.40) !important;
            display: inline-flex !important;
            align-items: center !important;
            justify-content: center !important;
            overflow: hidden !important;
            cursor: pointer !important;
        }
        .as-uploads-scope [class*="st-key-as_upload_actions_"] button:hover {
            background: rgba(2, 171, 224, 0.08) !important;
        }
        .as-upload-card {
            background: #FFFFFF;
            border: 1px solid rgba(10, 21, 71, 0.10);
            border-radius: 12px;
            padding: 0.85rem 0.9rem;
            margin: 0.75rem 0;
            box-shadow: 0 10px 24px rgba(10, 21, 71, 0.05);
        }
        .as-upload-title {
            color: #0A1547;
            font-size: 0.95rem;
            font-weight: 650;
            line-height: 1.25;
            margin-bottom: 0.55rem;
            word-break: break-word;
        }
        .as-upload-meta-value {
            color: #0A1547;
            font-size: 0.86rem;
            line-height: 1.25;
            word-break: break-word;
        }
        .as-upload-action-label {
            color: #5E6684;
            font-size: 0.72rem;
            letter-spacing: 0.08em;
            margin: 0.65rem 0 0.25rem 0;
            text-transform: uppercase;
        }
        .as-uploads-scope .as-upload-card .stButton > button {
            min-height: 2rem !important;
            height: auto !important;
            padding: 0.25rem 0.7rem !important;
            font-size: 0.82rem !important;
            line-height: 1.15 !important;
            white-space: nowrap !important;
        }
        .as-pdf-action-link,
        .as-pdf-action-disabled {
            align-items: center;
            border-radius: 4px;
            display: inline-flex;
            font-size: 0.82rem;
            justify-content: center;
            min-height: 2rem;
            padding: 0.25rem 0.7rem;
            text-decoration: none !important;
            width: 100%;
        }
        .as-pdf-action-link {
            background: #FFFFFF;
            border: 1px solid rgba(2, 171, 224, 0.40);
            color: #0A1547 !important;
        }
        .as-pdf-action-link:hover {
            background: rgba(2, 171, 224, 0.08);
        }
        .as-pdf-action-disabled {
            background: #F1F3F8;
            border: 1px solid rgba(10, 21, 71, 0.08);
            color: #8B92A9;
            opacity: 0.72;
        }
        .stRadio div[role="radiogroup"] {
            gap: 0.4rem;
        }
        .stRadio label {
            background: #FFFFFF;
            border: 1px solid rgba(10, 21, 71, 0.10);
            border-radius: 999px;
            padding: 0.35rem 0.8rem;
            font-size: 0.85rem;
            color: #0A1547;
            box-shadow: 0 8px 18px rgba(10, 21, 71, 0.05);
        }
        .stRadio label:hover {
            background: #F7F4FF;
            border-color: rgba(163, 128, 246, 0.45);
        }
        .stRadio label:has(input:checked) {
            background: #A380F6;
            border-color: #A380F6;
            color: #FFFFFF !important;
        }
        .stRadio label:has(input:checked) * {
            color: #FFFFFF !important;
        }
        .as-card {
            background: #FFFFFF;
            border: 1px solid rgba(10, 21, 71, 0.10);
            border-radius: 16px;
            padding: 0.85rem 1rem;
            margin-bottom: 1rem;
            box-shadow: 0 14px 34px rgba(10, 21, 71, 0.06);
            color: #0A1547;
        }
        .as-subcard {
            background: #FBFCFF;
            border: 1px solid rgba(10, 21, 71, 0.08);
            border-radius: 12px;
            padding: 0.65rem 0.8rem;
            margin: 0.65rem 0;
            color: #0A1547;
        }
        .as-card-divider {
            border-top: 1px solid rgba(10, 21, 71, 0.10);
            margin: 0.6rem 0 0.75rem 0;
        }
        .as-row-divider {
            border-top: 1px solid rgba(10, 21, 71, 0.08);
            margin: 0.4rem 0 0.9rem 0;
        }
        .as-muted {
            color: #5E6684;
            font-size: 0.75rem;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            margin-bottom: 0.2rem;
        }
        .as-pill {
            display: inline-block;
            padding: 0.1rem 0.55rem;
            border-radius: 999px;
            background: rgba(2, 217, 157, 0.12);
            border: 1px solid rgba(2, 217, 157, 0.35);
            color: #02D99D;
            font-size: 0.75rem;
            font-weight: 600;
        }
        .as-email {
            color: #1A2460;
            font-weight: 600;
            font-size: 0.95rem;
            word-break: break-word;
            text-decoration: none;
        }
        .as-email:hover {
            text-decoration: none;
        }
        .client-submissions-scope .as-email,
        .client-submissions-scope .as-email * {
            color: #1A2460 !important;
            text-decoration: none !important;
        }
        .client-submissions-scope a[href^="mailto:"] {
            pointer-events: none !important;
            cursor: default !important;
            color: #1A2460 !important;
            text-decoration: none !important;
        }
        .as-card details {
            background: #FFFFFF;
            border: 1px solid rgba(10, 21, 71, 0.10);
            border-radius: 10px;
            padding: 0.35rem 0.6rem;
            margin-top: 0.5rem;
        }
        .as-subcard details {
            background: #FFFFFF;
            border: 1px solid rgba(10, 21, 71, 0.08);
            border-radius: 10px;
            padding: 0.35rem 0.6rem;
            margin-top: 0.55rem;
        }
        </style>
        """
    st.markdown(css, unsafe_allow_html=True)


def _query_value(params: dict | None, key: str) -> str:
    if not params:
        return ""
    value = params.get(key)
    if isinstance(value, (list, tuple)):
        return str(value[0]) if value else ""
    return str(value or "")


def _set_page_query(page: str) -> None:
    try:
        if hasattr(st, "query_params"):
            st.query_params.clear()
            st.query_params["page"] = page
        else:
            st.experimental_set_query_params(page=page)
    except Exception as exc:
        logging.warning("[auth] unable to update page query param: %s", str(exc))


def _url_with_page(base_url: str, page: str) -> str:
    raw_url = base_url.strip()
    parsed = urlparse(raw_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        scheme = "http" if raw_url.startswith(("localhost", "127.0.0.1", "0.0.0.0")) else "https"
        parsed = urlparse(f"{scheme}://{raw_url.lstrip('/')}")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = page
    path = parsed.path or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, urlencode(query), ""))


def _admin_password_reset_redirect_url() -> str:
    explicit_url = os.getenv("ADMIN_PASSWORD_RESET_REDIRECT_URL", "").strip()
    if explicit_url:
        return explicit_url

    base_url = (
        os.getenv("APP_BASE_URL", "").strip()
        or os.getenv("PUBLIC_BASE_URL", "").strip()
        or os.getenv("PORTAL_BASE_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    )
    if not base_url:
        replit_domain = (
            os.getenv("REPLIT_DOMAINS", "").split(",")[0].strip()
            or os.getenv("REPLIT_DEV_DOMAIN", "").strip()
        )
        if replit_domain:
            base_url = f"https://{replit_domain}"

    if base_url:
        return _url_with_page(base_url, "admin_password_reset")

    port = os.getenv("STREAMLIT_SERVER_PORT") or os.getenv("PORT") or "5000"
    return f"http://localhost:{port}/?page=admin_password_reset"


def _render_password_recovery_hash_bridge() -> None:
    components.html(
        """
        <script>
        (function () {
          try {
            const target = window.parent || window.top || window;
            const url = new URL(target.location.href);
            const hash = target.location.hash ? target.location.hash.substring(1) : "";
            if (!hash) return;

            const hashParams = new URLSearchParams(hash);
            const keys = [
              "access_token",
              "refresh_token",
              "expires_at",
              "expires_in",
              "token_type",
              "type",
              "error",
              "error_code",
              "error_description"
            ];
            let copied = false;
            keys.forEach(function (key) {
              const value = hashParams.get(key);
              if (value) {
                url.searchParams.set("sb_" + key, value);
                copied = true;
              }
            });
            if (!copied) return;

            url.searchParams.set("page", "admin_password_reset");
            url.hash = "";
            target.location.replace(url.toString());
          } catch (err) {
            console.error("Supabase recovery redirect handling failed", err);
          }
        })();
        </script>
        """,
        height=0,
    )


def display_admin_forgot_password() -> None:
    _render_admin_css()
    st.markdown(
        """
        <div class="title-container" style="margin-top: 1.5rem;">
            <h1>Reset Admin Password</h1>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        "<p style='text-align: center; margin-bottom: 2rem; margin-top: 1.5rem;'>Enter your admin email and Supabase Auth will send a password reset link.</p>",
        unsafe_allow_html=True,
    )

    default_email = st.session_state.get("admin_email", "")
    with st.form("admin_forgot_password_form"):
        email = st.text_input("Email", value=default_email, key="admin_reset_email")
        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            submit_button = st.form_submit_button("Send reset link", width="stretch")

        if submit_button:
            redirect_to = _admin_password_reset_redirect_url()
            ok, error = send_admin_password_reset(email, redirect_to)
            if ok:
                st.success("If that email exists in Supabase Auth, a password reset link has been sent.")
                st.caption(f"Recovery links will return to: {redirect_to}")
            else:
                st.error(f"Could not send password reset link: {error}")

    st.markdown("<br>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        if st.button("Back to Admin Login", key="forgot_password_to_login", width="stretch"):
            _set_page_query("admin")
            st.session_state.page = "Admin Dashboard"
            st.rerun()


def display_admin_password_reset(query_params: dict | None = None) -> None:
    _render_admin_css()
    _render_password_recovery_hash_bridge()

    st.markdown(
        """
        <div class="title-container" style="margin-top: 1.5rem;">
            <h1>Set New Password</h1>
        </div>
        """,
        unsafe_allow_html=True,
    )

    recovery_error = _query_value(query_params, "sb_error_description") or _query_value(
        query_params, "error_description"
    )
    if recovery_error:
        st.error(f"Password reset link failed: {unquote(recovery_error)}")

    access_token = (
        _query_value(query_params, "sb_access_token")
        or _query_value(query_params, "access_token")
        or st.session_state.get("admin_password_reset_access_token", "")
    )
    refresh_token = (
        _query_value(query_params, "sb_refresh_token")
        or _query_value(query_params, "refresh_token")
        or st.session_state.get("admin_password_reset_refresh_token", "")
    )
    token_hash = _query_value(query_params, "token_hash") or _query_value(query_params, "sb_token_hash")

    if token_hash and not access_token:
        recovery_session, error = verify_password_recovery_token(token_hash)
        if error:
            st.error(f"Could not verify password reset link: {error}")
        else:
            access_token = recovery_session.get("access_token", "")
            refresh_token = recovery_session.get("refresh_token", "")

    if access_token:
        st.session_state.admin_password_reset_access_token = access_token
        st.session_state.admin_password_reset_refresh_token = refresh_token

    if not access_token:
        st.info("Loading your password reset link. If this message stays visible, request a new reset link.")
        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            if st.button("Request New Link", key="password_reset_request_new", width="stretch"):
                _set_page_query("admin_forgot_password")
                st.session_state.page = "Admin Forgot Password"
                st.rerun()
        return

    with st.form("admin_password_reset_form"):
        new_password = st.text_input("New password", type="password", key="admin_new_password")
        confirm_password = st.text_input("Confirm new password", type="password", key="admin_new_password_confirm")
        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            submit_button = st.form_submit_button("Update password", width="stretch")

        if submit_button:
            if len(new_password or "") < 8:
                st.error("Password must be at least 8 characters.")
            elif new_password != confirm_password:
                st.error("Passwords do not match.")
            else:
                ok, error = update_password_with_recovery_token(access_token, new_password)
                if ok:
                    for key in (
                        "admin_password_reset_access_token",
                        "admin_password_reset_refresh_token",
                        "admin_new_password",
                        "admin_new_password_confirm",
                    ):
                        if key in st.session_state:
                            del st.session_state[key]
                    st.success("Password updated. You can now log in with the new password.")
                else:
                    st.error(f"Could not update password: {error}")

    st.markdown("<br>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        if st.button("Go to Admin Login", key="password_reset_to_login", width="stretch"):
            _set_page_query("admin")
            st.session_state.page = "Admin Dashboard"
            st.rerun()


def _render_admin_login() -> None:
    st.markdown(
        """
        <div class="title-container" style="margin-top: 1.5rem;">
            <h1>Admin Dashboard</h1>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        "<p style='text-align: center; margin-bottom: 2rem; margin-top: 1.5rem;'>Please log in to access the admin dashboard.</p>",
        unsafe_allow_html=True,
    )
    with st.form("admin_login_form"):
        st.markdown(
            """
            <div class="section-header">
                <svg class="section-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
                    <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
                </svg>
                <span class="section-title">Login</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        email = st.text_input("Email", key="admin_email")
        password = st.text_input("Password", type="password", key="admin_password")

        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            submit_button = st.form_submit_button("Login", width="stretch")

        if submit_button:
            auth_result, error = sign_in_admin(email, password)
            if error:
                if "admin_password" in st.session_state:
                    del st.session_state["admin_password"]
                st.error(f"Login failed: {error}")
            else:
                st.session_state.admin_session = {
                    "access_token": auth_result["access_token"],
                    "refresh_token": auth_result.get("refresh_token"),
                }
                st.session_state.admin_user = auth_result.get("user")

                is_admin = is_admin_user(st.session_state.admin_user.get("id"))
                logging.info(
                    "[auth] admin login result user_id=%s is_admin=%s",
                    st.session_state.admin_user.get("id"),
                    is_admin,
                )
                if is_admin:
                    st.session_state.is_admin_logged_in = True
                    if "admin_email" in st.session_state:
                        del st.session_state["admin_email"]
                    if "admin_password" in st.session_state:
                        del st.session_state["admin_password"]
                    st.success("Login successful! Redirecting...")
                    st.rerun()
                else:
                    st.session_state.is_admin_logged_in = False
                    st.error("Not authorized. This Supabase Auth user is not listed in admin_users with role admin.")

    st.markdown(
        """
        <div style="text-align: center; margin-top: 0.75rem;">
            <a href="?page=admin_forgot_password" style="color: #A78BFA; text-decoration: none;">Forgot password?</a>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if st.session_state.admin_user and not st.session_state.is_admin_logged_in:
        col1, col2, col3 = st.columns([1, 1, 1])
        with col2:
            if st.button("Logout", key="admin_logout_unauthorized", width="stretch"):
                st.session_state.admin_session = None
                st.session_state.admin_user = None
                st.session_state.is_admin_logged_in = False
                st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        if st.button("Back to Analyzer", key="back_to_analyzer_from_login", width="stretch"):
            st.session_state.page = "Analyzer"
            st.rerun()


# Admin Dashboard Page
def display_admin_dashboard():
    perf = AdminPerfTracker()
    perf.log("enter")

    _ensure_admin_state()
    _render_admin_css()

    is_authenticated = bool(st.session_state.get("is_admin_logged_in"))
    perf.log("auth_checked")

    if not is_authenticated:
        _render_admin_login()
        perf.log("unauth_rendered")
        st.stop()

    col1, col2 = st.columns([3, 1])
    with col1:
        st.markdown("<h1 style='margin-top: 1.5rem;'>Admin Dashboard</h1>", unsafe_allow_html=True)
    with col2:
        if st.button("Logout", key="logout_button", type="secondary"):
            st.session_state.is_admin_logged_in = False
            st.session_state.admin_session = None
            st.session_state.admin_user = None
            st.session_state.page = "Analyzer"
            st.rerun()

    perf.log("tabs_render_start")

    tab_labels = [
        "Client Submissions",
        "Document Analysis",
        "Admin Management",
        "Secure Uploads",
        "PDF Generator",
    ]
    if "admin_active_tab" not in st.session_state:
        st.session_state.admin_active_tab = tab_labels[0]
    pending_tab = st.session_state.pop("admin_pending_tab", None)
    if pending_tab in tab_labels:
        st.session_state.admin_active_tab = pending_tab
        st.session_state.admin_tab_selector = pending_tab
    active_index = tab_labels.index(st.session_state.admin_active_tab) if st.session_state.admin_active_tab in tab_labels else 0
    selected_tab = st.radio(
        "Admin Navigation",
        tab_labels,
        index=active_index,
        horizontal=True,
        label_visibility="collapsed",
        key="admin_tab_selector",
    )
    if selected_tab != st.session_state.admin_active_tab:
        st.session_state.admin_active_tab = selected_tab

    if selected_tab == "Client Submissions":
        display_client_submissions(perf)
    elif selected_tab == "Document Analysis":
        display_document_analysis(perf)
    elif selected_tab == "Admin Management":
        display_admin_management()
    elif selected_tab == "Secure Uploads":
        display_upload_requests(perf)
    elif selected_tab == "PDF Generator":
        display_pdf_generator(perf)

    perf.log("render_done")


def display_upload_requests(perf: AdminPerfTracker):
    st.markdown("<h3 style='margin-top: 1.5rem;'>Secure Upload Requests</h3>", unsafe_allow_html=True)
    st.markdown(
        "<p>Send a single-use magic link for clients to upload PHI securely.</p>",
        unsafe_allow_html=True,
    )

    with st.form("upload_request_form"):
        client_email = st.text_input("Client email")
        submit_request = st.form_submit_button("Send Magic Link")

    if submit_request:
        try:
            perf.mark_first_db_query()
            result = create_upload_request(client_email)
            st.success("Magic link sent successfully.")
            st.markdown("**Upload Request Details**")
            st.write(f"Request ID: {result.get('request_id')}")
            st.write(f"Expires in {_token_ttl_minutes()} minutes")
        except PortalError as exc:
            st.error(f"Unable to create upload request: {exc.message}")
        except Exception as exc:
            st.error(f"Unexpected error: {exc}")

    st.divider()
    display_uploads_inbox(perf)


def display_uploads_inbox(perf: AdminPerfTracker):
    st.markdown("<h3 style='margin-top: 1.5rem;'>Uploads Inbox</h3>", unsafe_allow_html=True)
    st.markdown(
        "<p>Most recent upload portal files (default 50). Use filters to narrow results.</p>",
        unsafe_allow_html=True,
    )

    completed_only = st.checkbox("Completed only", value=True, key="uploads_inbox_completed_only")
    user_email_filter = st.text_input(
        "Filter by user email (contains)",
        placeholder="name@example.com",
        key="uploads_inbox_email_filter",
    ).strip()

    date_cols = st.columns(2)
    with date_cols[0]:
        start_date_raw = st.text_input(
            "Start date (YYYY-MM-DD)",
            placeholder="2024-01-01",
            key="uploads_inbox_start_date",
        ).strip()
    with date_cols[1]:
        end_date_raw = st.text_input(
            "End date (YYYY-MM-DD)",
            placeholder="2024-01-31",
            key="uploads_inbox_end_date",
        ).strip()

    start_date = _parse_date_input(start_date_raw)
    end_date = _parse_date_input(end_date_raw)

    if start_date_raw and not start_date:
        st.warning("Start date is invalid. Use YYYY-MM-DD.")
    if end_date_raw and not end_date:
        st.warning("End date is invalid. Use YYYY-MM-DD.")
    if start_date and end_date and end_date < start_date:
        st.warning("End date must be on or after the start date.")
        start_date = None
        end_date = None

    perf.mark_first_db_query()
    db = SessionLocal()
    try:
        query = db.query(UploadPortalFile)
        if completed_only:
            query = query.filter(UploadPortalFile.completed_at.isnot(None))
        if user_email_filter:
            query = query.filter(UploadPortalFile.user_email.ilike(f"%{user_email_filter}%"))
        if start_date:
            start_dt = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)
            query = query.filter(UploadPortalFile.created_at >= start_dt)
        if end_date:
            end_dt = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
            query = query.filter(UploadPortalFile.created_at < end_dt)

        rows = (
            query.order_by(UploadPortalFile.created_at.desc())
            .limit(50)
            .all()
        )
        if not rows:
            st.info("No secure uploads recorded yet.")
            return

        items = []
        for row in rows:
            object_name = row.object_name or ""
            gcs_bucket = row.gcs_bucket or ""
            gs_path = f"gs://{gcs_bucket}/{object_name}" if gcs_bucket and object_name else None
            console_url = None
            if gcs_bucket and object_name:
                encoded_object = quote(object_name, safe="/")
                console_url = (
                    "https://console.cloud.google.com/storage/browser/_details/"
                    f"{gcs_bucket}/{encoded_object}"
                )
            items.append(
                {
                    "created_at": format_mst(row.created_at),
                    "completed_at": format_mst(row.completed_at),
                    "user_email": row.user_email,
                    "console_url": console_url,
                    "user_id": str(row.user_id) if row.user_id else None,
                    "original_filename": row.original_filename,
                    "content_type": row.content_type,
                    "byte_size": row.byte_size,
                    "gcs_bucket": row.gcs_bucket,
                    "object_name": row.object_name,
                    "request_id": str(row.request_id),
                    "session_id": str(row.session_id),
                    "gs_path": gs_path,
                }
            )

        st.dataframe(
            items,
            width="stretch",
            column_config={
                "console_url": st.column_config.LinkColumn("Console link", display_text="View file"),
            },
        )
    except Exception:
        st.info("Secure uploads table is not available yet. Complete database setup to view uploads.")
    finally:
        db.close()


def display_client_submissions(perf: AdminPerfTracker):
    st.markdown("<h3 style='margin-top: 1.5rem;'>Client Submissions</h3>", unsafe_allow_html=True)
    st.markdown('<div class="client-submissions-scope">', unsafe_allow_html=True)
    if "client_submissions_search" not in st.session_state:
        st.session_state.client_submissions_search = ""
    search_cols = st.columns([4, 0.7])
    with search_cols[1]:
        if st.button(
            "Clear",
            key="client_submissions_search_clear",
            disabled=not bool((st.session_state.get("client_submissions_search") or "").strip()),
        ):
            st.session_state.client_submissions_search = ""
            st.rerun()
    with search_cols[0]:
        search_term = st.text_input(
            "Search clients",
            placeholder="Search by email, name, office/group, or phone",
            key="client_submissions_search",
        )
    normalized_search = search_term.strip().lower() if search_term else ""

    def _display_text(value: object) -> str:
        if value is None:
            return "—"
        text_value = str(value).strip()
        return text_value if text_value else "—"

    def _display_html(value: object) -> str:
        return html.escape(_display_text(value))

    def _full_name(submission: object) -> str:
        if not submission:
            return "—"
        first_name = (getattr(submission, "first_name", None) or "").strip()
        last_name = (getattr(submission, "last_name", None) or "").strip()
        return f"{first_name} {last_name}".strip() or "—"

    def _status_markup(status: object) -> str:
        status_value = (str(status).strip() if status is not None else "")
        if not status_value:
            return "<span class=\"as-muted\">—</span>"
        status_label = status_value.replace("_", " ").title()
        return f"<span class=\"as-pill\">{html.escape(status_label)}</span>"

    def _detail_markup(label: str, value: object) -> str:
        return (
            f"<div class=\"as-muted as-compact-label\">{html.escape(label)}</div>"
            f"<div class=\"as-detail-value\">{_display_html(value)}</div>"
        )

    def _fact_markup(label: str, value: object, tone: str = "navy") -> str:
        return (
            "<div class=\"as-fact\">"
            f"<div class=\"as-fact-label\">{html.escape(label)}</div>"
            f"<div class=\"as-fact-value as-fact-value--{html.escape(tone)}\">{_display_html(value)}</div>"
            "</div>"
        )

    def _acknowledgement_value(value: object) -> str:
        if value is None:
            return "—"
        return "Yes" if bool(value) else "No"

    perf.mark_first_db_query()
    logging.info("[db] url_present=%s", bool(os.getenv("DATABASE_URL")))

    def _load_client_rows():
        db = next(get_db())
        matching_emails = None
        if normalized_search:
            search_like = f"%{normalized_search}%"
            matching_submission_rows = db.query(ClientSubmission.user_email).filter(
                or_(
                    ClientSubmission.user_email.ilike(search_like),
                    ClientSubmission.first_name.ilike(search_like),
                    ClientSubmission.last_name.ilike(search_like),
                    ClientSubmission.office_name.ilike(search_like),
                    ClientSubmission.phone.ilike(search_like),
                )
            ).distinct().all()
            matching_user_rows = db.query(User.email).filter(
                or_(
                    User.email.ilike(search_like),
                    User.first_name.ilike(search_like),
                    User.last_name.ilike(search_like),
                    User.office_name.ilike(search_like),
                    User.phone.ilike(search_like),
                )
            ).distinct().all()
            matching_emails = {
                row[0] for row in [*matching_submission_rows, *matching_user_rows] if row[0]
            }
            if not matching_emails:
                return db, [], 0, 0, {}, {}, {}, {}

        query = db.query(
            ClientSubmission.user_email.label("email"),
            func.count(ClientSubmission.id).label("submission_count"),
            func.max(ClientSubmission.submitted_at).label("last_submitted_at"),
        )
        if matching_emails is not None:
            query = query.filter(ClientSubmission.user_email.in_(matching_emails))
        clients = query.group_by(ClientSubmission.user_email).order_by(
            func.max(ClientSubmission.submitted_at).desc()
        ).all()
        total_submissions = sum(row.submission_count for row in clients)
        client_emails = [row.email for row in clients if row.email]

        upload_counts = {}
        latest_submissions = {}
        users_by_email = {}
        status_counts = {}
        if client_emails:
            upload_count_rows = db.query(
                ClientSubmission.user_email,
                func.count(Upload.id).label("upload_count"),
            ).outerjoin(
                Upload, Upload.submission_id == ClientSubmission.id
            ).filter(
                ClientSubmission.user_email.in_(client_emails)
            ).group_by(
                ClientSubmission.user_email
            ).all()
            upload_counts = {row[0]: row[1] for row in upload_count_rows}

            latest_rows = db.query(ClientSubmission).filter(
                ClientSubmission.user_email.in_(client_emails)
            ).order_by(
                ClientSubmission.user_email.asc(),
                ClientSubmission.submitted_at.desc(),
            ).all()
            for submission in latest_rows:
                if submission.user_email not in latest_submissions:
                    latest_submissions[submission.user_email] = submission
                status_key = (submission.status or "").strip().lower()
                if status_key:
                    status_counts[status_key] = status_counts.get(status_key, 0) + 1

            users = db.query(User).filter(User.email.in_(client_emails)).all()
            users_by_email = {user.email: user for user in users if user.email}

        total_uploads = sum(upload_counts.values())
        return db, clients, total_submissions, total_uploads, upload_counts, latest_submissions, users_by_email, status_counts

    try:
        (
            db,
            clients,
            total_submissions,
            total_uploads,
            upload_counts,
            latest_submissions,
            users_by_email,
            status_counts,
        ) = try_first_db_query(_load_client_rows)
    except Exception:
        st.error(
            "We couldn't load the dashboard due to a database connection issue. Please try again."
        )
        if st.button("Retry"):
            st.rerun()
        return

    try:
        logging.info(
            "Dashboard query counts: clients=%d, submissions=%d, uploads=%d",
            len(clients),
            total_submissions,
            total_uploads,
        )

        if not clients:
            st.write("No client submissions available")
            return

        pending_count = sum(
            status_counts.get(status_name, 0)
            for status_name in ("pending", "queued", "processing", "in_progress")
        )
        error_count = sum(status_counts.get(status_name, 0) for status_name in ("error", "failed"))
        fact_items = [
            ("Clients", len(clients), "navy"),
            ("Submissions", total_submissions, "deep"),
            ("Uploads", total_uploads, "cyan"),
            ("Completed", status_counts.get("completed", 0), "green"),
            ("Submitted", status_counts.get("submitted", 0), "lilac"),
            ("Pending", pending_count, "deep"),
            ("Error", error_count, "navy"),
        ]
        st.markdown(
            "<div class=\"as-facts-strip\">"
            + "".join(_fact_markup(label, value, tone) for label, value, tone in fact_items)
            + "</div>",
            unsafe_allow_html=True,
        )

        for client in clients:
            client_email = client.email or ""
            client_key = client_email.replace("@", "_at_").replace(".", "_")
            latest_submission = latest_submissions.get(client_email)
            user_record = users_by_email.get(client_email)
            latest_submitted = _format_admin_dt(client.last_submitted_at) if client.last_submitted_at else "-"
            latest_status = getattr(latest_submission, "status", None) if latest_submission else None
            client_upload_count = upload_counts.get(client_email, 0)

            st.markdown('<div class="as-card as-client-card">', unsafe_allow_html=True)
            cols = st.columns([3.2, 1.0, 1.0, 1.8, 0.8])
            with cols[0]:
                st.markdown("<div class=\"as-muted\">Client</div>", unsafe_allow_html=True)
                _render_email_html(client_email)
            with cols[1]:
                st.markdown("<div class=\"as-muted as-compact-label\">Submissions</div>", unsafe_allow_html=True)
                st.markdown(
                    f"<span class=\"as-pill\">{client.submission_count}</span>",
                    unsafe_allow_html=True,
                )
            with cols[2]:
                st.markdown("<div class=\"as-muted as-compact-label\">Uploads</div>", unsafe_allow_html=True)
                st.markdown(
                    f"<span class=\"as-pill\">{client_upload_count}</span>",
                    unsafe_allow_html=True,
                )
            with cols[3]:
                st.markdown(_detail_markup("Latest Submitted", latest_submitted), unsafe_allow_html=True)
            with cols[4]:
                st.markdown("<div class=\"as-muted as-compact-label as-delete-header\">Delete</div>", unsafe_allow_html=True)
                if st.button(
                    "",
                    key=f"delete_btn_{client_key}",
                    help="Delete client",
                    icon=":material/delete:",
                ):
                    st.session_state[f"confirm_delete_{client_key}"] = client_email
                    st.rerun()

            summary_cols = st.columns([1.8, 2.2, 1.4, 1.4, 1.3])
            with summary_cols[0]:
                st.markdown(_detail_markup("Latest Name", _full_name(latest_submission)), unsafe_allow_html=True)
            with summary_cols[1]:
                office_value = getattr(latest_submission, "office_name", None) if latest_submission else None
                st.markdown(_detail_markup("Latest Office/Group", office_value), unsafe_allow_html=True)
            with summary_cols[2]:
                org_value = getattr(latest_submission, "org_type", None) if latest_submission else None
                st.markdown(_detail_markup("Latest Org Type", org_value), unsafe_allow_html=True)
            with summary_cols[3]:
                phone_value = (
                    getattr(latest_submission, "phone", None)
                    if latest_submission and getattr(latest_submission, "phone", None)
                    else getattr(user_record, "phone", None)
                )
                st.markdown(_detail_markup("Latest Phone", phone_value), unsafe_allow_html=True)
            with summary_cols[4]:
                st.markdown(
                    f"<div class=\"as-muted as-compact-label\">Latest Status</div><div>{_status_markup(latest_status)}</div>",
                    unsafe_allow_html=True,
                )

            if st.session_state.get(f"confirm_delete_{client_key}"):
                st.warning("Are you sure you want to delete all records for this client?")
                _render_email_html(client_email)
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("Yes, Delete", key=f"confirm_yes_{client_key}", type="primary"):
                        try:
                            delete_user(db, client_email)
                            st.success("Deleted all records for this client.")
                            del st.session_state[f"confirm_delete_{client_key}"]
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error deleting user: {str(e)}")
                with col2:
                    if st.button("Cancel", key=f"confirm_no_{client_key}"):
                        del st.session_state[f"confirm_delete_{client_key}"]
                        st.rerun()

            st.markdown('<div class="as-card-divider"></div>', unsafe_allow_html=True)

            with st.expander("View submissions", expanded=False):
                st.markdown("<div class=\"as-muted\">Client Email</div>", unsafe_allow_html=True)
                _render_email_html(client_email)
                st.markdown('<div class="as-row-divider"></div>', unsafe_allow_html=True)
                submission_rows = db.query(
                    ClientSubmission,
                    func.count(Upload.id).label("upload_count"),
                ).outerjoin(
                    Upload, Upload.submission_id == ClientSubmission.id
                ).filter(
                    ClientSubmission.user_email == client_email
                ).group_by(
                    ClientSubmission.id
                ).order_by(
                    ClientSubmission.submitted_at.desc()
                ).all()

                if not submission_rows:
                    st.write("No submissions available for this client.")
                else:
                    submission_ids = [row[0].id for row in submission_rows]
                    uploads_by_submission = {}
                    if submission_ids:
                        uploads = db.query(Upload).filter(
                            Upload.submission_id.in_(submission_ids)
                        ).order_by(Upload.upload_time.desc()).all()
                        for upload in uploads:
                            uploads_by_submission.setdefault(upload.submission_id, []).append(upload)

                    for submission_index, submission_row in enumerate(submission_rows):
                        submission = submission_row[0]
                        upload_count = submission_row[1]
                        full_name = _full_name(submission)
                        submission_label = _format_admin_dt(submission.submitted_at) or "-"

                        st.markdown('<div class="as-subcard">', unsafe_allow_html=True)
                        sub_cols = st.columns([1.7, 1.7, 2.2, 1.4])
                        with sub_cols[0]:
                            st.markdown(_detail_markup("Submitted At", submission_label), unsafe_allow_html=True)
                        with sub_cols[1]:
                            st.markdown(_detail_markup("Name", full_name), unsafe_allow_html=True)
                        with sub_cols[2]:
                            st.markdown("<div class=\"as-muted\">Email</div>", unsafe_allow_html=True)
                            _render_email_html(submission.user_email or client_email)
                        with sub_cols[3]:
                            st.markdown(_detail_markup("Phone", getattr(submission, "phone", None)), unsafe_allow_html=True)

                        sub_detail_cols = st.columns([2.2, 1.2, 1.4, 1.0, 1.6])
                        with sub_detail_cols[0]:
                            st.markdown(_detail_markup("Office/Group", submission.office_name), unsafe_allow_html=True)
                        with sub_detail_cols[1]:
                            st.markdown(_detail_markup("Org Type", submission.org_type), unsafe_allow_html=True)
                        with sub_detail_cols[2]:
                            run_id = submission.analysis_run_id or ""
                            run_id_markup = ""
                            if run_id:
                                run_id_markup = (
                                    f"<div class=\"as-muted\" style=\"font-size: 0.75rem;\">"
                                    f"{html.escape(run_id)}</div>"
                                )
                            st.markdown(
                                f"<div class=\"as-muted\">Status</div><div>{_status_markup(submission.status)}</div>{run_id_markup}",
                                unsafe_allow_html=True,
                            )
                        with sub_detail_cols[3]:
                            st.markdown(
                                f"<div class=\"as-muted\">Uploads</div><span class=\"as-pill\">{upload_count}</span>",
                                unsafe_allow_html=True,
                            )
                        with sub_detail_cols[4]:
                            ghl_cid = submission.ghl_cid or ""
                            ghl_url = _ghl_contact_url(ghl_cid)
                            if ghl_cid:
                                if ghl_url:
                                    ghl_markup = (
                                        f"<a href=\"{ghl_url}\" target=\"_blank\" "
                                        f"rel=\"noopener noreferrer\">{html.escape(ghl_cid)}</a>"
                                    )
                                else:
                                    ghl_markup = html.escape(ghl_cid)
                            else:
                                ghl_markup = "<span class=\"as-muted\">—</span>"
                            st.markdown(
                                f"<div class=\"as-muted\">GHL CID</div><div>{ghl_markup}</div>",
                                unsafe_allow_html=True,
                            )

                        with st.expander("Submission audit", expanded=False):
                            audit_cols = st.columns([1.5, 1.7, 1.4, 1.6])
                            with audit_cols[0]:
                                st.markdown(
                                    _detail_markup(
                                        "Financial Ack",
                                        _acknowledgement_value(
                                            getattr(submission, "financial_only_acknowledgement", None)
                                        ),
                                    ),
                                    unsafe_allow_html=True,
                                )
                            with audit_cols[1]:
                                acknowledgement_ts = getattr(submission, "acknowledgement_timestamp", None)
                                st.markdown(
                                    _detail_markup(
                                        "Ack Timestamp",
                                        _format_admin_dt(acknowledgement_ts) if acknowledgement_ts else None,
                                    ),
                                    unsafe_allow_html=True,
                                )
                            with audit_cols[2]:
                                st.markdown(
                                    _detail_markup(
                                        "Ack Version",
                                        getattr(submission, "acknowledgement_version", None),
                                    ),
                                    unsafe_allow_html=True,
                                )
                            with audit_cols[3]:
                                st.markdown(
                                    _detail_markup(
                                        "Ack IP",
                                        getattr(submission, "acknowledgement_ip", None),
                                    ),
                                    unsafe_allow_html=True,
                                )

                        uploads_for_submission = uploads_by_submission.get(submission.id, [])
                        with st.expander(
                            f"Uploads for {submission_label} ({len(uploads_for_submission)})",
                            expanded=False,
                        ):
                            st.markdown('<div class="as-uploads-scope">', unsafe_allow_html=True)
                            if not uploads_for_submission:
                                st.write("No uploads linked to this submission.")
                            else:
                                for row_idx, upload in enumerate(uploads_for_submission):
                                    key_suffix = f"{submission.id}_{upload.id}_{submission_index}_{row_idx}"
                                    summary_state_key = f"show_summary_{key_suffix}"
                                    analysis_state_key = f"show_analysis_{key_suffix}"
                                    analysis_payload = _parse_analysis_json(upload.analysis_data)
                                    has_analysis = bool(analysis_payload)
                                    paid_key = f"paid_toggle_{key_suffix}"
                                    current_paid = bool(getattr(upload, "paid", False))
                                    pdf_url = getattr(upload, "pdf_url", "") or ""
                                    report_path = (
                                        getattr(upload, "report_path", "")
                                        or getattr(upload, "pdf_path", "")
                                        or ""
                                    )
                                    if not report_path and pdf_url:
                                        report_path = _extract_supabase_report_path(pdf_url)
                                    signed_url = None
                                    err = None
                                    if report_path:
                                        try:
                                            signed_url = _create_report_signed_url(report_path)
                                            if not signed_url:
                                                err = "signed_url_unavailable"
                                        except Exception as exc:
                                            err = exc
                                        if err:
                                            logging.warning(
                                                "[pdf] signed url failed upload_id=%s bucket=%s path=%s err=%r",
                                                str(upload.id),
                                                "consulting-uploads",
                                                report_path,
                                                err,
                                            )
                                    if signed_url:
                                        escaped_signed_url = html.escape(signed_url, quote=True)
                                        pdf_status_markup = (
                                            f"<a href=\"{escaped_signed_url}\" target=\"_blank\" "
                                            "rel=\"noopener noreferrer\">PDF available</a>"
                                        )
                                        pdf_action_markup = (
                                            f"<a class=\"as-pdf-action-link\" href=\"{escaped_signed_url}\" "
                                            "target=\"_blank\" rel=\"noopener noreferrer\">PDF</a>"
                                        )
                                    elif report_path:
                                        pdf_status_markup = (
                                            "<span class=\"as-muted\" "
                                            "title=\"Signed link unavailable\">Saved, link unavailable</span>"
                                        )
                                        pdf_action_markup = (
                                            "<span class=\"as-pdf-action-disabled\" "
                                            "title=\"Signed link unavailable\">PDF unavailable</span>"
                                        )
                                    else:
                                        pdf_status_markup = "<span class=\"as-muted\">No PDF</span>"
                                        pdf_action_markup = "<span class=\"as-pdf-action-disabled\">No PDF</span>"

                                    st.markdown('<div class="as-upload-card">', unsafe_allow_html=True)
                                    st.markdown(
                                        f"<div class=\"as-upload-title\">{_display_html(upload.file_name)}</div>",
                                        unsafe_allow_html=True,
                                    )
                                    upload_meta_cols = st.columns([1.5, 1.4, 1.0, 1.4])
                                    with upload_meta_cols[0]:
                                        st.markdown(
                                            f"<div class=\"as-upload-header\">Tool / Type</div>"
                                            f"<div class=\"as-upload-meta-value\">{_display_html(upload.tool_name)}</div>",
                                            unsafe_allow_html=True,
                                        )
                                    with upload_meta_cols[1]:
                                        st.markdown(
                                            f"<div class=\"as-upload-header\">Upload Time</div>"
                                            f"<div class=\"as-upload-meta-value\">"
                                            f"{_display_html(_format_admin_dt(upload.upload_time))}</div>",
                                            unsafe_allow_html=True,
                                        )
                                    with upload_meta_cols[2]:
                                        st.markdown('<div class="as-upload-header">Paid Status</div>', unsafe_allow_html=True)
                                        paid_value = st.checkbox(
                                            "Paid",
                                            value=current_paid,
                                            key=paid_key,
                                        )
                                        if paid_value != current_paid:
                                            paid_db = SessionLocal()
                                            try:
                                                paid_db.query(Upload).filter(Upload.id == upload.id).update(
                                                    {"paid": paid_value}
                                                )
                                                paid_db.commit()
                                                st.rerun()
                                            except Exception as exc:
                                                logging.error("Failed to update paid flag for upload %s: %s", upload.id, str(exc))
                                                paid_db.rollback()
                                            finally:
                                                paid_db.close()
                                    with upload_meta_cols[3]:
                                        st.markdown(
                                            f"<div class=\"as-upload-header\">PDF Status</div>"
                                            f"<div class=\"as-upload-meta-value\">{pdf_status_markup}</div>",
                                            unsafe_allow_html=True,
                                        )

                                    st.markdown('<div class="as-upload-action-label">Actions</div>', unsafe_allow_html=True)
                                    upload_action_cols = st.columns([1, 1, 1, 1.35])
                                    with upload_action_cols[0]:
                                        if st.button(
                                            "Summary",
                                            key=f"open_summary_{key_suffix}",
                                            type="secondary",
                                            disabled=not has_analysis,
                                        ):
                                            st.session_state[summary_state_key] = True
                                            st.rerun()
                                    with upload_action_cols[1]:
                                        if st.button(
                                            "Analysis",
                                            key=f"open_analysis_{key_suffix}",
                                            type="secondary",
                                            disabled=not has_analysis,
                                        ):
                                            st.session_state[analysis_state_key] = True
                                            st.rerun()
                                    with upload_action_cols[2]:
                                        st.markdown(pdf_action_markup, unsafe_allow_html=True)
                                    with upload_action_cols[3]:
                                        if st.button(
                                            "Generate PDF",
                                            key=f"pdf_generate_{key_suffix}",
                                            type="secondary",
                                            disabled=not has_analysis,
                                        ):
                                            st.session_state.admin_pdf_upload_id = str(upload.id)
                                            st.session_state.admin_pdf_client_email = submission.user_email or client_email
                                            st.session_state.admin_pdf_notice = "PDF Generator opened with the selected upload."
                                            st.session_state.admin_pending_tab = "PDF Generator"
                                            st.session_state.admin_pdf_preselect_id = str(upload.id)
                                            st.rerun()
                                    st.markdown("</div>", unsafe_allow_html=True)

                                    if st.session_state.get(summary_state_key, False):
                                        st.markdown("---")
                                        st.markdown(
                                            f"**Admin Summary for {upload.file_name} ({upload.tool_name})**"
                                        )
                                        if analysis_payload:
                                            from analysis_utils import get_model_labels

                                            raw_analyses = analysis_payload.get("raw_analyses", {})
                                            if not isinstance(raw_analyses, dict):
                                                raw_analyses = {}
                                            total_issue_count = analysis_payload.get("total_issue_count", "N/A")
                                            openai_text = raw_analyses.get("OpenAI Analysis", "No OpenAI analysis available.")
                                            xai_text = raw_analyses.get("xAI Analysis", "No xAI analysis available.")
                                            anthropic_text = raw_analyses.get("AnthropicAI Analysis", "No Anthropic analysis available.")
                                            model_labels = get_model_labels()
                                            openai_label = model_labels["openai"]
                                            xai_label = model_labels["xai"]
                                            anthropic_label = model_labels["anthropic"]
                                            admin_summary = f"""
Tool: {upload.tool_name}
File Name: {upload.file_name}

Submitted by:
First Name: {submission.first_name}
Last Name: {submission.last_name}
Office/Group: {submission.office_name}
Email: {submission.user_email}
Phone: {_display_text(getattr(submission, "phone", None))}
Organization Type: {submission.org_type}

Total Issues Identified: {total_issue_count}

=== {openai_label} Analysis ===
{openai_text}

=== {xai_label} Analysis ===
{xai_text}

=== {anthropic_label} Analysis ===
{anthropic_text}
"""

                                            st.download_button(
                                                label="📥 Download Admin Summary",
                                                data=admin_summary,
                                                file_name=f"admin_summary_{submission.user_email}_{upload.file_name}.txt",
                                                mime="text/plain",
                                                key=f"download_summary_{key_suffix}"
                                            )

                                            st.text_area(
                                                "Admin Summary",
                                                admin_summary,
                                                height=400,
                                                key=f"summary_text_{key_suffix}",
                                                disabled=True
                                            )
                                        else:
                                            st.info("No analysis data available.")

                                        if st.button("Close", key=f"close_summary_{key_suffix}"):
                                            st.session_state[summary_state_key] = False
                                            st.rerun()

                                    if st.session_state.get(analysis_state_key, False):
                                        st.markdown("---")
                                        st.markdown(
                                            f"**Analysis for {upload.file_name} ({upload.tool_name})**"
                                        )
                                        if analysis_payload:
                                            raw_analyses = analysis_payload.get("raw_analyses", {})
                                            if not isinstance(raw_analyses, dict):
                                                raw_analyses = {}
                                            total_issue_count = analysis_payload.get("total_issue_count", "N/A")
                                            openai_text = raw_analyses.get("OpenAI Analysis", "No OpenAI analysis available.")
                                            xai_text = raw_analyses.get("xAI Analysis", "No xAI analysis available.")
                                            anthropic_text = raw_analyses.get("AnthropicAI Analysis", "No Anthropic analysis available.")
                                            st.markdown(f"**Total Issues Identified:** {total_issue_count}")

                                            st.markdown("**OpenAI Analysis:**")
                                            st.text_area(
                                                "OpenAI Analysis",
                                                openai_text,
                                                height=200,
                                                key=f"analysis_openai_{key_suffix}",
                                                disabled=True,
                                                label_visibility="collapsed",
                                            )

                                            st.markdown("**xAI Analysis:**")
                                            st.text_area(
                                                "xAI Analysis",
                                                xai_text,
                                                height=200,
                                                key=f"analysis_xai_{key_suffix}",
                                                disabled=True,
                                                label_visibility="collapsed",
                                            )

                                            st.markdown("**Anthropic Analysis:**")
                                            st.text_area(
                                                "Anthropic Analysis",
                                                anthropic_text,
                                                height=200,
                                                key=f"analysis_anthropic_{key_suffix}",
                                                disabled=True,
                                                label_visibility="collapsed",
                                            )
                                        else:
                                            st.info("No analysis data available.")

                                        if st.button("Close", key=f"close_analysis_{key_suffix}"):
                                            st.session_state[analysis_state_key] = False
                                            st.rerun()

                            st.markdown("</div>", unsafe_allow_html=True)
                        st.markdown("</div>", unsafe_allow_html=True)
                        if submission_index < len(submission_rows) - 1:
                            st.markdown('<div class="as-row-divider"></div>', unsafe_allow_html=True)

            st.markdown("</div>", unsafe_allow_html=True)
            st.markdown('<div class="as-row-divider"></div>', unsafe_allow_html=True)
    finally:
        db.close()
        st.markdown("</div>", unsafe_allow_html=True)


def display_document_analysis(perf: AdminPerfTracker):
    """Admin-only document analysis for AR and Insurance Claims"""
    st.markdown("<h3 style='margin-top: 1.5rem;'>Document Analysis</h3>", unsafe_allow_html=True)
    st.info("These analysis tools are available to admin users only. Upload Financial, AR, or Insurance Claims documents for analysis.")

    if "admin_analyzing" not in st.session_state:
        st.session_state.admin_analyzing = False
    if "admin_cancel_requested" not in st.session_state:
        st.session_state.admin_cancel_requested = False
    if "admin_analysis_run_id" not in st.session_state:
        st.session_state.admin_analysis_run_id = ""
    if "admin_analysis_canceled" not in st.session_state:
        st.session_state.admin_analysis_canceled = False
    if "admin_submission_id" not in st.session_state:
        st.session_state.admin_submission_id = ""

    if st.session_state.admin_analysis_canceled:
        st.warning("Analysis canceled. No results were saved.")

    from analysis_utils import (
        openai_analysis,
        xai_analysis,
        anthropic_analysis,
        parse_issues_from_analysis,
        parse_trends_from_analysis,
        deduplicate_issues,
        send_email,
        extract_text_from_pdf,
        get_model_labels,
        log_active_models,
    )
    import pandas as pd

    def _clean_client_value(value: object) -> str:
        return str(value or "").strip()

    def _load_admin_client_options(search_value: str) -> list[dict]:
        search_value = _clean_client_value(search_value)
        db = SessionLocal()
        try:
            candidates = {}

            def _ensure_candidate(email_value: object) -> dict | None:
                normalized = normalize_email(str(email_value or ""))
                if not normalized:
                    return None
                if normalized not in candidates:
                    candidates[normalized] = {
                        "email": normalized,
                        "first_name": "",
                        "last_name": "",
                        "office_name": "",
                        "org_type": "",
                        "phone": "",
                        "ghl_cid": "",
                        "latest_submitted_at": None,
                    }
                return candidates[normalized]

            submission_query = db.query(ClientSubmission)
            user_query = db.query(User)
            if search_value:
                search_like = f"%{search_value}%"
                submission_query = submission_query.filter(
                    or_(
                        ClientSubmission.user_email.ilike(search_like),
                        ClientSubmission.first_name.ilike(search_like),
                        ClientSubmission.last_name.ilike(search_like),
                        ClientSubmission.office_name.ilike(search_like),
                        ClientSubmission.phone.ilike(search_like),
                    )
                )
                user_query = user_query.filter(
                    or_(
                        User.email.ilike(search_like),
                        User.first_name.ilike(search_like),
                        User.last_name.ilike(search_like),
                        User.office_name.ilike(search_like),
                        User.phone.ilike(search_like),
                    )
                )

            submissions = submission_query.order_by(ClientSubmission.submitted_at.desc()).limit(75).all()
            for submission in submissions:
                candidate = _ensure_candidate(submission.user_email)
                if not candidate:
                    continue
                if not candidate["latest_submitted_at"]:
                    candidate["latest_submitted_at"] = submission.submitted_at
                for field in ("first_name", "last_name", "office_name", "org_type", "phone", "ghl_cid"):
                    value = _clean_client_value(getattr(submission, field, ""))
                    if value and not candidate[field]:
                        candidate[field] = value

            users = user_query.order_by(User.email.asc()).limit(75).all()
            for user in users:
                candidate = _ensure_candidate(user.email)
                if not candidate:
                    continue
                for field in ("first_name", "last_name", "office_name", "org_type", "phone"):
                    value = _clean_client_value(getattr(user, field, ""))
                    if value and not candidate[field]:
                        candidate[field] = value

            def _sort_key(candidate: dict) -> float:
                submitted_at = candidate.get("latest_submitted_at")
                if submitted_at and hasattr(submitted_at, "timestamp"):
                    return submitted_at.timestamp()
                return 0.0

            return sorted(candidates.values(), key=_sort_key, reverse=True)[:75]
        except Exception as exc:
            logging.error("Failed to load admin client options: %s", str(exc))
            return []
        finally:
            db.close()

    def _client_option_label(candidate: dict) -> str:
        name = f"{candidate.get('first_name', '')} {candidate.get('last_name', '')}".strip()
        label_parts = [candidate.get("email", "")]
        if name:
            label_parts.append(name)
        if candidate.get("office_name"):
            label_parts.append(candidate["office_name"])
        if candidate.get("phone"):
            label_parts.append(candidate["phone"])
        return " | ".join(part for part in label_parts if part)

    def _render_resolved_client_details(client: dict) -> None:
        st.markdown("**Resolved Client Details**")
        detail_cols = st.columns([1.5, 1.8, 2.1, 1.2, 1.4])
        with detail_cols[0]:
            st.markdown(f"**Name:** {html.escape((client.get('first_name', '') + ' ' + client.get('last_name', '')).strip() or '—')}")
        with detail_cols[1]:
            st.markdown(f"**Email:** {html.escape(client.get('email') or '—')}")
        with detail_cols[2]:
            st.markdown(f"**Office/Group:** {html.escape(client.get('office_name') or '—')}")
        with detail_cols[3]:
            st.markdown(f"**Type:** {html.escape(client.get('org_type') or '—')}")
        with detail_cols[4]:
            st.markdown(f"**Phone:** {html.escape(client.get('phone') or '—')}")

    def _render_admin_analysis_upload_controls():
        st.markdown("---")
        st.markdown("**Upload Documents for Analysis**")

        st.markdown(
            """
                <div class="as-admin-upload-label">
                    <span class="as-admin-upload-marker"></span>
                    <span class="as-admin-upload-title">Financial Analyzer</span>
                </div>
            """,
            unsafe_allow_html=True,
        )
        financial_file = st.file_uploader(
            "Upload Financial Document",
            type=["csv", "xlsx", "pdf"],
            key="admin_financial",
            disabled=st.session_state.admin_analyzing,
        )

        st.markdown(
            """
                <div class="as-admin-upload-label">
                    <span class="as-admin-upload-marker"></span>
                    <span class="as-admin-upload-title">Accounts Receivable Analyzer</span>
                </div>
            """,
            unsafe_allow_html=True,
        )
        ar_file = st.file_uploader(
            "Upload AR Report",
            type=["csv", "xlsx"],
            key="admin_ar",
            disabled=st.session_state.admin_analyzing,
        )

        st.markdown(
            """
                <div class="as-admin-upload-label">
                    <span class="as-admin-upload-marker"></span>
                    <span class="as-admin-upload-title">Insurance Claims Analyzer</span>
                </div>
            """,
            unsafe_allow_html=True,
        )
        claim_file = st.file_uploader(
            "Upload Claim Report",
            type=["csv", "xlsx", "pdf"],
            key="admin_claim",
            disabled=st.session_state.admin_analyzing,
        )

        uploaded_files = {
            "Financial Analyzer": financial_file,
            "AR Analyzer": ar_file,
            "Insurance Claim Analyzer": claim_file,
        }
        uploaded_count = sum(1 for f in uploaded_files.values() if f is not None)
        return uploaded_files, uploaded_count

    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'

    st.markdown("**Client**")
    client_mode = st.radio(
        "Client mode",
        ["Existing client", "New client"],
        horizontal=True,
        key="admin_analysis_client_mode",
    )

    first_name = ""
    last_name = ""
    office_name = ""
    email = ""
    org_type = ""
    phone = ""
    ghl_cid = ""

    if client_mode == "Existing client":
        existing_search = st.text_input(
            "Search existing clients",
            placeholder="Search by email, name, office/group, or phone",
            key="admin_existing_client_search",
        )
        client_options = _load_admin_client_options(existing_search)
        client_options_by_email = {client["email"]: client for client in client_options}
        option_emails = [client["email"] for client in client_options]
        if option_emails:
            current_existing_email = st.session_state.get("admin_existing_client_email")
            if current_existing_email not in option_emails:
                st.session_state.admin_existing_client_email = option_emails[0]
            selected_email = st.selectbox(
                "Select existing client",
                option_emails,
                key="admin_existing_client_email",
                format_func=lambda option: _client_option_label(client_options_by_email.get(option, {"email": option})),
            )
            selected_client = client_options_by_email.get(selected_email, {})
            first_name = _clean_client_value(selected_client.get("first_name"))
            last_name = _clean_client_value(selected_client.get("last_name"))
            office_name = _clean_client_value(selected_client.get("office_name"))
            email = _clean_client_value(selected_client.get("email"))
            org_type = _clean_client_value(selected_client.get("org_type"))
            phone = _clean_client_value(selected_client.get("phone"))
            ghl_cid = _clean_client_value(selected_client.get("ghl_cid"))
            _render_resolved_client_details(selected_client)
        else:
            st.warning("No existing clients found. Search again or switch to New client.")
    else:
        st.markdown("**New Client Details**")
        first_name = st.text_input("Client First Name", key="admin_first_name")
        last_name = st.text_input("Client Last Name", key="admin_last_name")
        office_name = st.text_input("Office/Group Name", key="admin_office_name")
        email = st.text_input("Client Email Address", placeholder="client@example.com", key="admin_email")
        org_type = st.selectbox("Type", ["Location", "Group"], key="admin_org_type")
        phone = st.text_input("Phone (optional)", key="admin_phone")
        ghl_cid = st.text_input("GHL CID (optional)", key="admin_ghl_cid")

    valid_email = re.match(email_pattern, email) if email else False
    client_info_complete = all([first_name, last_name, office_name, email, org_type]) and valid_email

    if email and not valid_email:
        st.error("Please enter a valid email address")

    if not client_info_complete:
        if client_mode == "Existing client":
            st.warning("Select an existing client with complete client details before running analysis.")
        else:
            st.warning("Please complete the client information before running analysis.")
        uploaded_files, uploaded_count = _render_admin_analysis_upload_controls()
        if uploaded_count > 0:
            st.markdown(f"**Documents ready for analysis:** {uploaded_count}")
        st.button(
            "Analyze Documents",
            type="primary",
            disabled=True,
            key="admin_analyze_btn",
        )
    else:
        uploaded_files, uploaded_count = _render_admin_analysis_upload_controls()

        if uploaded_count > 0:
            st.markdown(f"**Documents ready for analysis:** {uploaded_count}")

            progress_bar = None
            progress_text = None

            def update_progress(value: int, label: str) -> None:
                if progress_bar is None or progress_text is None:
                    return
                progress_bar.progress(value)
                progress_text.caption(f"{value}% — {label}")

            analyze_clicked = st.button(
                "Analyze Documents",
                type="primary",
                disabled=st.session_state.admin_analyzing,
                key="admin_analyze_btn",
            )

            stop_clicked = False
            if st.session_state.admin_analyzing:
                stop_clicked = st.button("Stop analysis", type="secondary", key="admin_stop_btn")

            if analyze_clicked:
                st.session_state.admin_analyzing = True
                st.session_state.admin_cancel_requested = False
                st.session_state.admin_analysis_canceled = False
                st.session_state.admin_analysis_run_id = str(uuid.uuid4())
                st.session_state.admin_submission_id = ""
                st.rerun()

            if stop_clicked:
                st.session_state.admin_cancel_requested = True
                st.rerun()

            if st.session_state.admin_analyzing:
                progress_bar = st.progress(0)
                progress_text = st.empty()
                progress_text.caption("0% — Starting")

                run_id = st.session_state.admin_analysis_run_id or str(uuid.uuid4())
                st.session_state.admin_analysis_run_id = run_id

                try:
                    _check_admin_cancel("before_start", run_id)
                    logging.info("[analysis] start run_id=%s source=admin", run_id)
                    log_active_models(run_id)
                    normalized_email = normalize_email(email)
                    normalized_phone = _clean_client_value(phone)
                    logging.info("Normalized email: %s", normalized_email)
                    user_info_dict = {
                        "first_name": first_name,
                        "last_name": last_name,
                        "office_name": office_name,
                        "email": normalized_email,
                        "org_type": org_type,
                        "phone": normalized_phone,
                    }

                    perf.mark_first_db_query()
                    _check_admin_cancel("before_user_upsert", run_id)
                    db = SessionLocal()
                    try:
                        existing_user = db.query(User).filter(User.email == normalized_email).first()
                        if not existing_user:
                            new_user = User(
                                first_name=first_name,
                                last_name=last_name,
                                email=normalized_email,
                                office_name=office_name,
                                org_type=org_type,
                                phone=normalized_phone or None,
                            )
                            db.add(new_user)
                            _check_admin_cancel("before_user_upsert_commit", run_id)
                            db.commit()
                            logging.info("User upsert: created for %s (admin)", normalized_email)
                        else:
                            updated = False
                            if existing_user.first_name != first_name:
                                existing_user.first_name = first_name
                                updated = True
                            if existing_user.last_name != last_name:
                                existing_user.last_name = last_name
                                updated = True
                            if existing_user.office_name != office_name:
                                existing_user.office_name = office_name
                                updated = True
                            if existing_user.org_type != org_type:
                                existing_user.org_type = org_type
                                updated = True
                            if normalized_phone and existing_user.phone != normalized_phone:
                                existing_user.phone = normalized_phone
                                updated = True
                            if updated:
                                _check_admin_cancel("before_user_upsert_commit", run_id)
                                db.commit()
                                logging.info("User upsert: updated for %s (admin)", normalized_email)
                            else:
                                logging.info("User upsert: existing for %s (admin)", normalized_email)
                    except Exception as e:
                        logging.error(f"Error saving user: {str(e)}")
                        db.rollback()
                    finally:
                        db.close()

                    submission_id = st.session_state.get("admin_submission_id") or ""
                    if not submission_id:
                        _check_admin_cancel("before_submission_create", run_id)
                        submission_db = SessionLocal()
                        try:
                            submission = ClientSubmission(
                                user_email=normalized_email,
                                first_name=first_name,
                                last_name=last_name,
                                office_name=office_name,
                                org_type=org_type,
                                phone=normalized_phone or None,
                                source="admin",
                                status="submitted",
                                analysis_run_id=run_id,
                                ghl_cid=ghl_cid.strip() if ghl_cid else None,
                            )
                            submission_db.add(submission)
                            _check_admin_cancel("before_submission_create_commit", run_id)
                            submission_db.commit()
                            submission_db.refresh(submission)
                            submission_id = str(submission.id)
                            st.session_state.admin_submission_id = submission_id
                            logging.info(
                                "[analysis] submission created run_id=%s id=%s source=admin",
                                run_id,
                                submission_id,
                            )
                        except Exception as exc:
                            logging.error(
                                "[analysis] submission create failed run_id=%s: %s",
                                run_id,
                                str(exc),
                            )
                            submission_db.rollback()
                        finally:
                            submission_db.close()

                    analysis_results = {}
                    upload_ids = []
                    all_emails_sent = True
                    for tool_name, file in uploaded_files.items():
                        if file is not None:
                            _check_admin_cancel("before_upload_begin", run_id)
                            update_progress(10, "Upload started")
                            file.seek(0)
                            file_content = file.read()
                            file_name = file.name
                            file_type = file.type

                            upload_file_id = persist_upload_file(
                                file_bytes=file_content,
                                user_email=normalized_email,
                                tool_name=tool_name,
                                original_filename=file_name,
                                content_type=file_type,
                            )
                            update_progress(25, "File stored in Supabase")
                            _check_admin_cancel("after_upload_complete", run_id)

                            _check_admin_cancel("before_extraction", run_id)
                            file.seek(0)

                            if file_type == "application/pdf":
                                text_content = extract_text_from_pdf(file)
                            elif file_type in ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel"]:
                                df = pd.read_excel(file)
                                text_content = df.to_string()
                            else:
                                df = pd.read_csv(file)
                                text_content = df.to_string()
                            update_progress(45, "Text extraction complete")

                            update_progress(70, "AI analysis running")
                            _check_admin_cancel("before_openai", run_id)
                            openai_result = openai_analysis(text_content)
                            _check_admin_cancel("after_openai", run_id)
                            _check_admin_cancel("before_xai", run_id)
                            xai_result = xai_analysis(text_content)
                            _check_admin_cancel("after_xai", run_id)
                            _check_admin_cancel("before_anthropic", run_id)
                            anthropic_result = anthropic_analysis(text_content)
                            _check_admin_cancel("after_anthropic", run_id)

                            model_labels = get_model_labels()
                            openai_issues = parse_issues_from_analysis(openai_result, model_labels["openai"])
                            xai_issues = parse_issues_from_analysis(xai_result, model_labels["xai"])
                            anthropic_issues = parse_issues_from_analysis(anthropic_result, model_labels["anthropic"])

                            openai_trends = parse_trends_from_analysis(openai_result, model_labels["openai"])
                            xai_trends = parse_trends_from_analysis(xai_result, model_labels["xai"])
                            anthropic_trends = parse_trends_from_analysis(anthropic_result, model_labels["anthropic"])

                            all_issues = openai_issues + xai_issues + anthropic_issues
                            all_trends = openai_trends + xai_trends + anthropic_trends

                            deduplicated_issues = deduplicate_issues(all_issues)

                            results = {
                                "raw_analyses": {
                                    "OpenAI Analysis": openai_result,
                                    "xAI Analysis": xai_result,
                                    "AnthropicAI Analysis": anthropic_result,
                                },
                                "parsed_issues": {
                                    "openai": openai_issues,
                                    "xai": xai_issues,
                                    "anthropic": anthropic_issues,
                                },
                                "parsed_trends": {
                                    "openai": openai_trends,
                                    "xai": xai_trends,
                                    "anthropic": anthropic_trends,
                                },
                                "all_trends": all_trends,
                                "deduplicated_issues": deduplicated_issues,
                                "total_issue_count": len(deduplicated_issues),
                            }

                            _check_admin_cancel("before_results_assignment", run_id)
                            analysis_results[tool_name] = results

                            _check_admin_cancel("before_email_send", run_id)
                            update_progress(90, "Emails sending")
                            email_success = True
                            try:
                                send_email(user_info_dict, file_content, file_name, file_type, results, tool_name)
                            except Exception as exc:
                                email_success = False
                                logging.error(
                                    "Admin email failed for %s (%s): %s",
                                    normalized_email,
                                    file_name,
                                    str(exc),
                                )
                            if not email_success:
                                all_emails_sent = False

                            _check_admin_cancel("before_upload_save", run_id)
                            upload_db = SessionLocal()
                            try:
                                analysis_json = json.dumps({
                                    'raw_analyses': results['raw_analyses'],
                                    'deduplicated_issues': results['deduplicated_issues'],
                                    'total_issue_count': results['total_issue_count'],
                                    'all_trends': results.get('all_trends', [])
                                })
                                new_upload = Upload(
                                    file_name=file_name,
                                    tool_name=tool_name,
                                    upload_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    user_email=normalized_email,
                                    analysis_data=analysis_json
                                )
                                upload_db.add(new_upload)
                                _check_admin_cancel("before_upload_commit", run_id)
                                upload_db.commit()
                                logging.info(f"Upload saved (admin): {file_name}")

                                _check_admin_cancel("before_upload_file_link", run_id)
                                update_upload_file_upload_id(upload_file_id, new_upload.id)
                                upload_ids.append(new_upload.id)
                            except Exception as e:
                                logging.error(f"Error saving upload: {str(e)}")
                                upload_db.rollback()
                            finally:
                                upload_db.close()

                    _check_admin_cancel("before_submission_save", run_id)
                    submission_id = submission_id or st.session_state.get("admin_submission_id") or ""
                    if upload_ids and all_emails_sent:
                        submission_db = SessionLocal()
                        try:
                            if submission_id:
                                update_submission_status(
                                    submission_db,
                                    submission_id,
                                    status="completed",
                                    completed_at=datetime.utcnow(),
                                    error_message=None,
                                    errored_at=None,
                                    canceled_at=None,
                                )
                            else:
                                submission = ClientSubmission(
                                    user_email=normalized_email,
                                    first_name=first_name,
                                    last_name=last_name,
                                    office_name=office_name,
                                    org_type=org_type,
                                    phone=normalized_phone or None,
                                    source="admin",
                                    status="completed",
                                    completed_at=datetime.utcnow(),
                                    analysis_run_id=run_id,
                                    ghl_cid=ghl_cid.strip() if ghl_cid else None,
                                )
                                submission_db.add(submission)
                                _check_admin_cancel("before_submission_commit", run_id)
                                submission_db.commit()
                                submission_db.refresh(submission)
                                submission_id = str(submission.id)
                                st.session_state.admin_submission_id = submission_id
                                logging.info(
                                    "Submission snapshot created: %s for %s (admin)",
                                    submission.id,
                                    normalized_email,
                                )

                            if ghl_cid and submission_id:
                                _check_admin_cancel("before_submission_ghl_cid", run_id)
                                try:
                                    _update_submission_ghl_fields(submission_db, submission_id, ghl_cid=ghl_cid.strip())
                                except Exception as exc:
                                    logging.error(
                                        "Failed to set GHL cid for submission %s: %s",
                                        submission_id,
                                        type(exc).__name__,
                                    )

                                _check_admin_cancel("before_ghl_writeback", run_id)
                                success, err = _ghl_update_analyzer_submitted(ghl_cid.strip())
                                _check_admin_cancel("after_ghl_writeback", run_id)
                                if success:
                                    _check_admin_cancel("before_ghl_tag", run_id)
                                    tag_success, tag_err = _ghl_add_tag(ghl_cid.strip(), "analyzer submitted")
                                    if tag_success:
                                        logging.info("GHL tag added for cid %s", ghl_cid.strip())
                                    else:
                                        logging.warning("GHL tag add failed for cid %s: %s", ghl_cid.strip(), tag_err)
                                    try:
                                        _check_admin_cancel("before_submission_ghl_success_update", run_id)
                                        _update_submission_ghl_fields(
                                            submission_db,
                                            submission_id,
                                            submitted_at=datetime.utcnow(),
                                            error_msg=None,
                                        )
                                    except Exception as exc:
                                        logging.error(
                                            "Failed to update GHL writeback status for submission %s: %s",
                                            submission_id,
                                            type(exc).__name__,
                                        )
                                else:
                                    if err == "missing analyzer field id":
                                        logging.warning(
                                            "GHL analyzer field id missing; skipping writeback for cid %s",
                                            ghl_cid.strip(),
                                        )
                                    else:
                                        logging.warning(
                                            "GHL writeback failed for cid %s: %s",
                                            ghl_cid.strip(),
                                            err,
                                        )
                                    try:
                                        _check_admin_cancel("before_submission_ghl_error_update", run_id)
                                        _update_submission_ghl_fields(
                                            submission_db,
                                            submission_id,
                                            error_msg=err,
                                        )
                                    except Exception as exc:
                                        logging.error(
                                            "Failed to record GHL writeback error for submission %s: %s",
                                            submission_id,
                                            type(exc).__name__,
                                        )

                            if submission_id:
                                _check_admin_cancel("before_submission_link_uploads", run_id)
                                submission_db.query(Upload).filter(Upload.id.in_(upload_ids)).update(
                                    {"submission_id": submission_id},
                                    synchronize_session=False
                                )
                                _check_admin_cancel("before_submission_link_commit", run_id)
                                submission_db.commit()
                                logging.info(
                                    "Linked %d uploads to submission_id %s (admin)",
                                    len(upload_ids),
                                    submission_id,
                                )
                        except Exception as e:
                            logging.error(
                                "Error creating submission snapshot for %s (admin): %s",
                                normalized_email,
                                str(e),
                            )
                            submission_db.rollback()
                        finally:
                            submission_db.close()
                    elif upload_ids and not all_emails_sent:
                        logging.warning(
                            "Submission snapshot skipped for %s due to email failure (admin)",
                            normalized_email,
                        )
                        if submission_id:
                            submission_db = SessionLocal()
                            try:
                                update_submission_status(
                                    submission_db,
                                    submission_id,
                                    status="error",
                                    errored_at=datetime.utcnow(),
                                    error_message="email_failed",
                                )
                            except Exception as exc:
                                logging.error(
                                    "[analysis] submission error update failed run_id=%s: %s",
                                    run_id,
                                    str(exc),
                                )
                            finally:
                                submission_db.close()

                    update_progress(100, "Analysis complete")
                    st.session_state.admin_analyzing = False
                    st.session_state.admin_analysis_canceled = False
                    st.session_state.admin_cancel_requested = False
                    st.session_state.admin_analysis_run_id = ""
                    st.session_state.admin_submission_id = ""
                    logging.info("[analysis] finished run_id=%s source=admin", run_id)
                    st.success("Analysis complete! Results have been emailed to the consulting team.")
                    st.rerun()
                except AdminCancelledError:
                    submission_id = st.session_state.get("admin_submission_id") or ""
                    if submission_id:
                        cancel_db = SessionLocal()
                        try:
                            update_submission_status(
                                cancel_db,
                                submission_id,
                                status="canceled",
                                canceled_at=datetime.utcnow(),
                            )
                        except Exception as exc:
                            logging.error(
                                "[analysis] cancel status update failed run_id=%s: %s",
                                run_id,
                                str(exc),
                            )
                        finally:
                            cancel_db.close()
                    st.session_state.admin_analyzing = False
                    st.session_state.admin_analysis_canceled = True
                    st.session_state.admin_cancel_requested = False
                    st.session_state.admin_analysis_run_id = ""
                    st.session_state.admin_submission_id = ""
                    logging.info("[analysis] canceled run_id=%s source=admin", run_id)
                    st.rerun()
                except Exception as exc:
                    submission_id = st.session_state.get("admin_submission_id") or ""
                    if submission_id:
                        error_db = SessionLocal()
                        try:
                            update_submission_status(
                                error_db,
                                submission_id,
                                status="error",
                                errored_at=datetime.utcnow(),
                                error_message=str(exc)[:300],
                            )
                        except Exception as update_exc:
                            logging.error(
                                "[analysis] error status update failed run_id=%s: %s",
                                run_id,
                                str(update_exc),
                            )
                        finally:
                            error_db.close()
                    st.session_state.admin_analyzing = False
                    st.session_state.admin_analysis_canceled = False
                    st.session_state.admin_cancel_requested = False
                    st.session_state.admin_analysis_run_id = ""
                    st.session_state.admin_submission_id = ""
                    logging.error("[analysis] error run_id=%s source=admin: %s", run_id, str(exc))
                    st.rerun()
        else:
            st.info("Upload a Financial, AR, or Insurance Claims document to begin analysis.")
            st.button(
                "Analyze Documents",
                type="primary",
                disabled=True,
                key="admin_analyze_btn",
            )


def display_pdf_generator(perf: AdminPerfTracker):
    st.markdown("<h3 style='margin-top: 1.5rem;'>PDF Generator</h3>", unsafe_allow_html=True)
    st.session_state.pop("admin_pdf_notice", None)

    if "admin_pdf_upload_id" not in st.session_state:
        st.session_state.admin_pdf_upload_id = ""
    if "admin_pdf_client_email" not in st.session_state:
        st.session_state.admin_pdf_client_email = ""

    perf.mark_first_db_query()
    logging.info("[db] url_present=%s", bool(os.getenv("DATABASE_URL")))

    preselected_upload_id = st.session_state.get("admin_pdf_upload_id")
    pending_preselect_id = st.session_state.get("admin_pdf_preselect_id") or ""
    lookup_upload_id = pending_preselect_id or preselected_upload_id
    preselected_email = st.session_state.get("admin_pdf_client_email")

    def _load_pdf_context():
        db = SessionLocal()
        preselected_upload = None
        upload_id_uuid = None
        if lookup_upload_id:
            try:
                upload_id_uuid = uuid.UUID(lookup_upload_id)
            except (ValueError, TypeError):
                upload_id_uuid = None
        if upload_id_uuid is not None:
            preselected_upload = db.query(Upload).filter(Upload.id == upload_id_uuid).first()
        resolved_email = preselected_email
        if preselected_upload and preselected_upload.user_email:
            resolved_email = preselected_upload.user_email
        client_rows = db.query(ClientSubmission.user_email).distinct().order_by(
            ClientSubmission.user_email.asc()
        ).all()
        client_emails = [row[0] for row in client_rows if row[0]]
        return db, preselected_upload, resolved_email, client_emails

    try:
        db, preselected_upload, preselected_email, client_emails = try_first_db_query(_load_pdf_context)
    except Exception:
        st.error(
            "We couldn't load the dashboard due to a database connection issue. Please try again."
        )
        if st.button("Retry"):
            st.rerun()
        return

    try:
        if not client_emails:
            st.info("No client submissions available for PDF generation.")
            return

        client_index = 0
        if preselected_email and preselected_email in client_emails:
            client_index = client_emails.index(preselected_email)
        selected_email = st.selectbox(
            "Client",
            client_emails,
            index=client_index,
            key="pdf_generator_client",
        )

        upload_rows = db.query(Upload).filter(
            Upload.user_email == selected_email
        ).order_by(Upload.upload_time.desc()).all()
        upload_rows = [row for row in upload_rows if row.analysis_data]
        if not upload_rows:
            st.info("No analyzed uploads available for this client.")
            return

        def _upload_label(row: Upload) -> str:
            upload_time = _format_admin_dt(row.upload_time) or "-"
            return f"{row.tool_name} — {row.file_name} ({upload_time})"

        upload_option_ids = [str(row.id) for row in upload_rows]
        id_to_row = {str(row.id): row for row in upload_rows}
        upload_index = 0
        if pending_preselect_id and pending_preselect_id in upload_option_ids:
            upload_index = upload_option_ids.index(pending_preselect_id)
        elif preselected_upload and str(preselected_upload.id) in upload_option_ids:
            upload_index = upload_option_ids.index(str(preselected_upload.id))

        selected_upload_id = st.selectbox(
            "Upload",
            upload_option_ids,
            format_func=lambda oid: _upload_label(id_to_row.get(oid)) if id_to_row.get(oid) else "Unknown upload",
            index=upload_index,
            key="pdf_generator_upload_id",
        )
        selected_upload = id_to_row.get(selected_upload_id)
        if pending_preselect_id:
            st.session_state.admin_pdf_preselect_id = ""
        if not selected_upload:
            st.warning("Selected upload is no longer available.")
            return

        submission = None
        if selected_upload.submission_id:
            submission = db.query(ClientSubmission).filter(
                ClientSubmission.id == selected_upload.submission_id
            ).first()
        if not submission:
            submission = db.query(ClientSubmission).filter(
                ClientSubmission.user_email == selected_email
            ).order_by(ClientSubmission.submitted_at.desc()).first()

        st.markdown("**PDF Builder**")
        metadata_cols = st.columns(2)
        client_name = ""
        office_name = ""
        if submission:
            client_name = f"{submission.first_name or ''} {submission.last_name or ''}".strip()
            office_name = submission.office_name or ""
        with metadata_cols[0]:
            st.markdown(f"**Client First Name:** {submission.first_name if submission else '-'}")
            st.markdown(f"**Client Last Name:** {submission.last_name if submission else '-'}")
            st.markdown(f"**Office/Group Name:** {office_name or '-'}")
        with metadata_cols[1]:
            st.markdown(f"**Client Email:** {selected_email}")
            st.markdown(f"**Tool Name:** {selected_upload.tool_name or '-'}")
            st.markdown(f"**Upload Date/Time:** {_format_admin_dt(selected_upload.upload_time) or '-'}")

        current_paid = bool(getattr(selected_upload, "paid", False))
        paid_value = st.checkbox("Paid", value=current_paid, key=f"pdf_paid_toggle_{selected_upload.id}")
        if paid_value != current_paid:
            paid_db = SessionLocal()
            try:
                paid_db.query(Upload).filter(Upload.id == selected_upload.id).update(
                    {"paid": paid_value}
                )
                paid_db.commit()
                st.rerun()
            except Exception as exc:
                logging.error("Failed to update paid flag for upload %s: %s", selected_upload.id, str(exc))
                paid_db.rollback()
            finally:
                paid_db.close()

        analysis_payload = _parse_analysis_json(selected_upload.analysis_data)
        if not analysis_payload:
            st.warning("Analysis data is missing or unreadable. You can paste content manually below.")

        opportunities = _extract_opportunities(analysis_payload)
        trends = _extract_trends(analysis_payload)
        key_trends = _extract_key_trends(analysis_payload)

        builder_prefix = f"pdf_builder_{selected_upload.id}"
        selected_opportunities = []
        selected_trends = []
        selected_key_trends = []

        include_keys = []
        if opportunities:
            include_keys.extend([f"{builder_prefix}_opp_include_{idx}" for idx in range(len(opportunities))])
        if trends:
            include_keys.extend([f"{builder_prefix}_trend_include_{idx}" for idx in range(len(trends))])
        if key_trends:
            include_keys.extend([f"{builder_prefix}_key_include_{idx}" for idx in range(len(key_trends))])

        if include_keys:
            for key in include_keys:
                st.session_state.setdefault(key, True)
            toggle_cols = st.columns([1, 1])
            with toggle_cols[0]:
                if st.button("Check All", key=f"{builder_prefix}_check_all", type="secondary"):
                    for key in include_keys:
                        st.session_state[key] = True
                    st.rerun()
            with toggle_cols[1]:
                if st.button("Uncheck All", key=f"{builder_prefix}_uncheck_all", type="secondary"):
                    for key in include_keys:
                        st.session_state[key] = False
                    st.rerun()

        st.markdown("#### Improvement Opportunities")
        if opportunities:
            for idx, item in enumerate(opportunities):
                include_key = f"{builder_prefix}_opp_include_{idx}"
                include = st.checkbox(f"Include opportunity {idx + 1}", key=include_key)
                if include:
                    title = st.text_input(
                        "Issue",
                        value=item.get("title") or "",
                        key=f"{builder_prefix}_opp_title_{idx}",
                    )
                    impact = st.text_area(
                        "Impact",
                        value=item.get("impact") or "",
                        key=f"{builder_prefix}_opp_impact_{idx}",
                        height=70,
                    )
                    recommendation = st.text_area(
                        "Recommendation",
                        value=item.get("recommendation") or "",
                        key=f"{builder_prefix}_opp_rec_{idx}",
                        height=70,
                    )
                    selected_opportunities.append(
                        {"title": title, "impact": impact, "recommendation": recommendation}
                    )
        else:
            manual_opps = st.text_area(
                "Opportunities (one per line)",
                key=f"{builder_prefix}_opp_manual",
                height=150,
            )
            if manual_opps.strip():
                for line in manual_opps.splitlines():
                    if line.strip():
                        selected_opportunities.append({"title": line.strip(), "impact": "", "recommendation": ""})

        st.markdown("#### Trends")
        if trends:
            for idx, trend in enumerate(trends):
                include_key = f"{builder_prefix}_trend_include_{idx}"
                include = st.checkbox(f"Include trend {idx + 1}", key=include_key)
                if include:
                    text = st.text_area(
                        "Trend Text",
                        value=trend,
                        key=f"{builder_prefix}_trend_text_{idx}",
                        height=60,
                    )
                    selected_trends.append(text)
        else:
            manual_trends = st.text_area(
                "Trends (one per line)",
                key=f"{builder_prefix}_trend_manual",
                height=120,
            )
            if manual_trends.strip():
                for line in manual_trends.splitlines():
                    if line.strip():
                        selected_trends.append(line.strip())

        st.markdown("#### Key Trends Identified")
        if key_trends:
            for idx, trend in enumerate(key_trends):
                include_key = f"{builder_prefix}_key_include_{idx}"
                include = st.checkbox(f"Include key trend {idx + 1}", key=include_key)
                if include:
                    text = st.text_area(
                        "Key Trend Text",
                        value=trend,
                        key=f"{builder_prefix}_key_text_{idx}",
                        height=60,
                    )
                    selected_key_trends.append(text)
        else:
            manual_key = st.text_area(
                "Key Trends (one per line)",
                key=f"{builder_prefix}_key_manual",
                height=120,
            )
            if manual_key.strip():
                for line in manual_key.splitlines():
                    if line.strip():
                        selected_key_trends.append(line.strip())

        notes = st.text_area(
            "Additional Notes",
            max_chars=2000,
            key=f"{builder_prefix}_notes",
            height=120,
        )
        if st.button("Generate PDF", type="primary", key=f"{builder_prefix}_generate"):
            if not any([selected_opportunities, selected_trends, selected_key_trends, notes.strip()]):
                st.session_state[f"{builder_prefix}_no_selection_warning"] = True
                st.warning("Please make at least one selection to create a report.")
                st.stop()
            current_version = getattr(selected_upload, "pdf_version", 0) or 0
            next_version = current_version + 1
            date_prefix = datetime.utcnow().strftime("%Y-%m-%d")
            safe_email = _safe_path_component(selected_email)
            safe_tool = _safe_path_component(selected_upload.tool_name or "analysis")
            file_name = f"{safe_email}_{safe_tool}_{date_prefix}_v{next_version}.pdf"
            object_path = f"reports/{safe_email}/{date_prefix}/{safe_tool}/{selected_upload.id}/{file_name}"

            metadata = {
                "client_name": client_name or selected_email,
                "office_name": office_name,
                "client_email": selected_email,
                "tool_name": selected_upload.tool_name,
                "upload_time": _format_admin_dt(selected_upload.upload_time) or "-",
            }
            sections = {
                "opportunities": selected_opportunities,
                "trends": selected_trends,
                "key_trends": selected_key_trends,
            }

            try:
                pdf_bytes = _generate_pdf_bytes(metadata, sections, notes, next_version)
            except Exception as exc:
                logging.error(
                    "[pdf] failed upload_id=%s err=%r",
                    selected_upload.id,
                    exc,
                )
                st.error("Unable to generate PDF. Please try again.")
                return

            pdf_url, err = _upload_pdf_report(pdf_bytes, object_path)
            if err:
                logging.error("PDF upload failed for upload %s: %s", selected_upload.id, err)
                st.error("Unable to save PDF to storage.")
                return
            signed_url = _create_report_signed_url(object_path)

            update_db = SessionLocal()
            try:
                update_db.query(Upload).filter(Upload.id == selected_upload.id).update(
                    {
                        "pdf_version": next_version,
                        "pdf_url": pdf_url,
                        "pdf_generated_at": datetime.utcnow(),
                        "paid": paid_value,
                    }
                )
                update_db.commit()
                logging.info("[pdf] generated upload_id=%s version=%s", selected_upload.id, next_version)
                st.session_state["admin_last_report_path"] = object_path
                st.session_state["admin_last_report_signed_url"] = signed_url
                st.session_state["admin_last_report_bytes"] = pdf_bytes
                st.session_state["admin_last_report_file_name"] = file_name
                st.session_state["admin_last_report_upload_id"] = str(selected_upload.id)
                st.success("PDF generated and saved successfully.")
                st.rerun()
            except Exception as exc:
                logging.error("Failed to update upload PDF metadata %s: %s", selected_upload.id, str(exc))
                update_db.rollback()
                st.error("PDF saved but metadata update failed.")
            finally:
                update_db.close()

        if st.session_state.get(f"{builder_prefix}_no_selection_warning"):
            if any([selected_opportunities, selected_trends, selected_key_trends, notes.strip()]):
                st.session_state[f"{builder_prefix}_no_selection_warning"] = False

        last_report_bytes = st.session_state.get("admin_last_report_bytes")
        last_report_file_name = st.session_state.get("admin_last_report_file_name")
        last_report_signed_url = st.session_state.get("admin_last_report_signed_url")
        last_report_upload_id = st.session_state.get("admin_last_report_upload_id")
        if last_report_bytes and last_report_file_name:
            st.markdown("**Latest Generated Report**")
            st.download_button(
                label="Download PDF",
                data=last_report_bytes,
                file_name=last_report_file_name,
                mime="application/pdf",
                key=f"admin_download_pdf_{last_report_upload_id or 'latest'}",
            )
            if last_report_signed_url:
                st.markdown(
                    f'<a href="{last_report_signed_url}" target="_blank" rel="noopener noreferrer">'
                    "Open PDF in new tab</a>",
                    unsafe_allow_html=True,
                )
            else:
                st.warning(
                    "PDF generated and stored, but an open-link could not be created. Use Download PDF."
                )
    finally:
        db.close()


def display_admin_management():
    st.markdown("<h3 style='margin-top: 1.5rem;'>Admin Management</h3>", unsafe_allow_html=True)
    st.info("Admin access is managed through Supabase Auth and the admin_users table.")
