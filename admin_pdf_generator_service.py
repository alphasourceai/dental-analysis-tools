from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime
from typing import Any, Optional

from fpdf import FPDF

from supabase_utils import SUPABASE_URL, _get_supabase_admin_client

PDF_STORAGE_BUCKET = "consulting-uploads"


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


def sanitize_pdf_text(value: object) -> str:
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


def safe_path_component(value: str) -> str:
    if not value:
        return "unknown"
    safe = value.strip().replace("/", "_").replace("\\", "_").replace(" ", "_")
    return safe or "unknown"


def _repo_root() -> str:
    start_dir = os.path.abspath(os.path.dirname(__file__))
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


def safe_pdf_multi_cell(pdf: FPDF, text: str, field_label: str, height: int = 6, width: float = 0) -> None:
    safe_text = sanitize_pdf_text(text)
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
            "[pdf] invalid width field=%s width=%s computed=%.2f x=%.2f y=%.2f page=%s",
            field_label,
            width,
            safe_width,
            pdf.get_x(),
            pdf.get_y(),
            pdf.page_no(),
        )
        safe_width = 10.0
    try:
        pdf.multi_cell(safe_width, height, safe_text)
    except Exception as exc:
        logging.error("[pdf] render failed field=%s error_type=%s", field_label, type(exc).__name__)
        raise


def pdf_output_bytes(pdf: FPDF) -> bytes:
    out = pdf.output(dest="S")
    if isinstance(out, (bytes, bytearray)):
        return bytes(out)
    if isinstance(out, str):
        return out.encode("latin-1", errors="replace")
    return str(out).encode("latin-1", errors="replace")


def render_pdf_metadata_row(
    pdf: FPDF,
    label: str,
    value: object,
    field_label: str,
    label_width: float = 40,
    height: int = 6,
    min_value_width: float = 30,
    label_color: Optional[tuple[int, int, int]] = None,
    value_color: Optional[tuple[int, int, int]] = None,
    label_size: int = 9,
    value_size: int = 10,
    start_x: Optional[float] = None,
    font_family: str = "Helvetica",
) -> None:
    safe_label = sanitize_pdf_text(label)
    safe_value = sanitize_pdf_text(value or "-")
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
        safe_pdf_multi_cell(pdf, safe_value, field_label, height=height, width=full_width)
        pdf.set_x(pdf.l_margin)
        return

    pdf.cell(label_width, height, f"{safe_label}:", ln=0)
    if value_color:
        pdf.set_text_color(*value_color)
    pdf.set_font(font_family, "", value_size)
    pdf.set_xy(left_x + label_width, start_y)
    safe_pdf_multi_cell(pdf, safe_value, field_label, height=height, width=value_width)
    pdf.set_x(pdf.l_margin)


def generate_pdf_bytes(metadata: dict[str, Any], sections: dict[str, Any], notes: str, version: int) -> bytes:
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
                self.write(line_height, sanitize_pdf_text(prefix))
                self.set_text_color(*self.footer_secondary)
                self.write(line_height, sanitize_pdf_text(email), link=f"mailto:{email}")
            except Exception:
                self.set_text_color(*self.footer_subtle)
                self.cell(0, line_height, sanitize_pdf_text(f"{prefix}{email}"), ln=1)
            else:
                self.ln(line_height)
            self.set_text_color(*self.footer_subtle)
            self.cell(0, line_height, sanitize_pdf_text("alphaSource Consulting - All rights reserved."), ln=0)

    pdf = StyledPDF(background)
    pdf.set_margins(16, 16, 16)
    pdf.set_auto_page_break(auto=True, margin=28)

    root = _repo_root()
    regular_family = "Raleway"
    bold_family = "RalewayBold"
    font_path = os.path.join(root, "raleway", "static", "Raleway-Regular.ttf")
    bold_font_path = os.path.join(root, "raleway", "static", "Raleway-Bold.ttf")
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
        pdf.cell(0, 8, sanitize_pdf_text(title), ln=1)
        if underline:
            y = pdf.get_y()
            pdf.set_draw_color(*border)
            pdf.line(pdf.l_margin, y, pdf.w - pdf.r_margin, y)
        pdf.ln(1)

    def estimate_text_height(value: object, width: float, line_height: float, size: int) -> float:
        safe = sanitize_pdf_text(value or "")
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
        start_x: Optional[float] = None,
    ) -> None:
        render_pdf_metadata_row(
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

    def render_bullet(
        text: str,
        field_label: str,
        font_family_override: Optional[str] = None,
        size: int = 10,
    ) -> None:
        safe_text = text.strip()
        if not safe_text:
            return
        pdf.set_text_color(*secondary)
        bullet_family = font_family_override or font_family
        pdf.set_font(bullet_family, "", size)
        pdf.set_x(pdf.l_margin)
        safe_pdf_multi_cell(pdf, f"- {safe_text}", field_label, height=6, width=content_width)
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
                    render_bullet(text, f"opportunity:{idx}:{label}", font_family_override=bold_family)
                else:
                    render_bullet(text, f"opportunity:{idx}:{label}")
            return
        combined = str(item).strip() if item is not None else ""
        if combined:
            render_bullet(combined, f"opportunity:{idx}")

    logo_path = os.path.join(root, "public", "logo with bg color 1128.png")
    if os.path.exists(logo_path):
        logo_y = pdf.get_y()
        try:
            pdf.image(logo_path, x=pdf.l_margin, y=logo_y, w=85)
            pdf.set_y(logo_y + 24)
        except Exception:
            pass

    pdf.set_text_color(*primary)
    pdf.set_font(font_family, "", 22)
    pdf.cell(0, 12, sanitize_pdf_text("Your Detailed Analysis Report"), ln=1)
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
        safe_pdf_multi_cell(pdf, notes, "notes:body", height=6, width=content_width)
        pdf.ln(1)

    pdf.total_pages = pdf.page_no()
    return pdf_output_bytes(pdf)


def upload_pdf_report(pdf_bytes: bytes, object_path: str) -> tuple[str, str]:
    client = _get_supabase_admin_client()
    if not client:
        return "", "Supabase admin client is not configured"
    try:
        client.storage.from_(PDF_STORAGE_BUCKET).upload(
            object_path,
            pdf_bytes,
            {"content-type": "application/pdf", "upsert": False},
        )
    except Exception as exc:
        return "", str(exc)

    public_url = ""
    try:
        response = client.storage.from_(PDF_STORAGE_BUCKET).get_public_url(object_path)
        if isinstance(response, dict):
            public_url = response.get("publicURL") or response.get("public_url") or ""
        elif isinstance(response, str):
            public_url = response
    except Exception:
        public_url = ""

    if not public_url and SUPABASE_URL:
        public_url = f"{SUPABASE_URL}/storage/v1/object/public/{PDF_STORAGE_BUCKET}/{object_path}"

    return public_url, ""


def create_report_signed_url(path: str, expires_in: int = 3600) -> tuple[Optional[str], Optional[str]]:
    if not path:
        return None, None
    client = _get_supabase_admin_client()
    if not client:
        logging.warning("[pdf] signed url missing client path=%s", path)
        return None, "signed_url_unavailable"
    try:
        response = client.storage.from_(PDF_STORAGE_BUCKET).create_signed_url(path, expires_in)
    except Exception as exc:
        logging.warning("[pdf] signed url failed path=%s err_type=%s", path, type(exc).__name__)
        return None, "signed_url_unavailable"
    signed_url = ""
    if isinstance(response, dict):
        signed_url = response.get("signedURL") or response.get("signedUrl") or ""
    elif isinstance(response, str):
        signed_url = response
    if signed_url:
        return signed_url, None
    logging.warning("[pdf] signed url empty path=%s", path)
    return None, "signed_url_unavailable"


def cleanup_pdf_report(object_path: str) -> bool:
    if not object_path:
        return False
    client = _get_supabase_admin_client()
    if not client:
        return False
    try:
        client.storage.from_(PDF_STORAGE_BUCKET).remove([object_path])
        return True
    except Exception as exc:
        logging.warning("[pdf] cleanup failed path=%s err_type=%s", object_path, type(exc).__name__)
        return False
