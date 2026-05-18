from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

from fpdf import FPDF

from supabase_utils import SUPABASE_URL, _get_supabase_admin_client

PDF_STORAGE_BUCKET = "consulting-uploads"
FINANCIAL_CONTEXT_KEYWORDS = (
    "production",
    "revenue",
    "income",
    "collections",
    "collection",
    "writeoff",
    "writeoffs",
    "adjustment",
    "adjustments",
    "cost",
    "costs",
    "profit",
    "gross",
    "net",
    "ebitda",
    "overhead",
    "labor",
    "payroll",
    "salary",
    "salaries",
    "wage",
    "wages",
    "expense",
    "expenses",
    "fee",
    "fees",
    "payment",
    "payments",
    "ar",
    "accounts receivable",
    "financial",
    "cash flow",
    "value",
)
MARKDOWN_ARTIFACT_PATTERN = re.compile(r"[*_`]+")
FINANCIAL_NUMBER_PATTERN = re.compile(
    r"(?<![\w$,\d])(?:\d{1,3}(?:,\d{3})+|\d{4,})(?:\.\d+)?(?![\w%])"
)


def _wrap_long_tokens(text: str, max_token_length: int = 28) -> str:
    if not text:
        return ""
    parts = re.split(r"(\s+)", text)
    wrapped = []
    for part in parts:
        if not part or part.isspace():
            wrapped.append(part)
            continue
        if "@" in part or len(part) <= max_token_length:
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


def format_client_report_text(value: object, *, force_currency: bool = False) -> str:
    text = "" if value is None else str(value)
    if not text.strip():
        return ""
    text = clean_client_report_text(text)
    return format_client_report_financial_values(text, force_currency=force_currency)


def clean_client_report_text(text: str) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^\s*[-*•]\s+", "", cleaned)
    cleaned = re.sub(
        r"^\s*(trend|key trend|data note|implementation priority|report section to consider)\s*:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = MARKDOWN_ARTIFACT_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip(" -")


def format_client_report_financial_values(text: str, *, force_currency: bool = False) -> str:
    if not text:
        return ""
    lower_text = text.lower()
    has_financial_context = force_currency or any(
        re.search(rf"\b{re.escape(keyword)}\b", lower_text)
        if " " not in keyword
        else keyword in lower_text
        for keyword in FINANCIAL_CONTEXT_KEYWORDS
    )
    if not has_financial_context:
        return text

    def is_part_of_existing_currency(start: int) -> bool:
        if start <= 0:
            return False
        previous = text[start - 1]
        if previous in "$,0123456789":
            return True
        index = start - 1
        while index >= 0 and not text[index].isspace() and text[index] not in "([{;:":
            if text[index] == "$":
                return True
            if text[index] in ".!?":
                return False
            index -= 1
        return False

    def replacement(match: re.Match[str]) -> str:
        start, end = match.span()
        raw_value = match.group(0)
        if is_part_of_existing_currency(start):
            return raw_value

        following = text[end:end + 9].lower()
        if following.startswith("%") or following.startswith(" percent"):
            return raw_value

        numeric_value = raw_value.replace(",", "")
        digits_only = re.sub(r"\D", "", numeric_value)
        if len(digits_only) in (10, 11):
            return raw_value
        if "." not in numeric_value and "," not in raw_value:
            try:
                whole_number = int(numeric_value)
            except ValueError:
                whole_number = 0
            if 1900 <= whole_number <= 2100:
                return raw_value

        try:
            amount = Decimal(numeric_value)
        except (InvalidOperation, ValueError):
            return raw_value
        if amount.is_nan() or amount.is_infinite():
            return raw_value

        rounded = int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return f"${rounded:,}"

    return FINANCIAL_NUMBER_PATTERN.sub(replacement, text)


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
    page_background = (255, 255, 255)
    card = (248, 249, 253)
    border = (218, 223, 235)
    navy = (10, 21, 71)
    body = (35, 44, 72)
    muted = (92, 103, 128)
    lilac = (163, 128, 246)
    teal = (2, 171, 224)
    green = (2, 217, 157)

    class StyledPDF(FPDF):
        def __init__(self, bg_color: tuple[int, int, int]):
            super().__init__()
            self._bg_color = bg_color
            self.footer_font_family = "Helvetica"
            self.footer_muted = muted
            self.footer_border = border

        def header(self) -> None:
            self.set_fill_color(*self._bg_color)
            self.rect(0, 0, self.w, self.h, "F")

        def footer(self) -> None:
            font_family = self.footer_font_family or "Helvetica"
            self.set_y(-16)
            self.set_draw_color(*self.footer_border)
            self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
            self.ln(3)
            self.set_font(font_family, "", 9)
            self.set_text_color(*self.footer_muted)
            self.cell(0, 5, sanitize_pdf_text("alphaSource Consulting | hello@alphasourceconsulting.com"), ln=0)
            self.set_x(-34)
            self.cell(18, 5, sanitize_pdf_text(f"Page {self.page_no()}"), align="R")

    pdf = StyledPDF(page_background)
    pdf.set_margins(17, 16, 17)
    pdf.set_auto_page_break(auto=True, margin=24)

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

    def set_regular(size: int, color: tuple[int, int, int] = body) -> None:
        pdf.set_text_color(*color)
        pdf.set_font(font_family, "", size)

    def set_bold(size: int, color: tuple[int, int, int] = navy) -> None:
        pdf.set_text_color(*color)
        pdf.set_font(bold_family if has_bold_face else font_family, "", size)

    def section_title(title: str, accent: tuple[int, int, int] = lilac) -> None:
        ensure_space(14)
        pdf.ln(2)
        set_bold(13, navy)
        pdf.cell(0, 7, sanitize_pdf_text(title), ln=1)
        y = pdf.get_y()
        pdf.set_draw_color(*accent)
        pdf.set_line_width(0.6)
        pdf.line(pdf.l_margin, y, pdf.l_margin + 32, y)
        pdf.set_line_width(0.2)
        pdf.ln(4)

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

    def draw_card(x: float, y: float, width: float, height: float, fill: tuple[int, int, int] = card) -> None:
        pdf.set_fill_color(*fill)
        pdf.set_draw_color(*border)
        pdf.rect(x, y, width, height, "FD")

    def render_label(label: str, accent: tuple[int, int, int] = lilac) -> None:
        set_bold(8, accent)
        pdf.cell(0, 5, sanitize_pdf_text(label.upper()), ln=1)

    def render_bullet(
        text: str,
        field_label: str,
        size: int = 10,
        accent: tuple[int, int, int] = teal,
    ) -> None:
        safe_text = text.strip()
        if not safe_text:
            return
        ensure_space(12)
        left_x = pdf.get_x()
        start_y = pdf.get_y()
        pdf.set_fill_color(*accent)
        pdf.ellipse(left_x, start_y + 2.3, 1.7, 1.7, "F")
        pdf.set_xy(left_x + 5, start_y)
        set_regular(size, body)
        safe_pdf_multi_cell(pdf, safe_text, field_label, height=5.6, width=content_width - 5)
        pdf.ln(1.2)

    def normalize_badge_value(value: object) -> str:
        text = format_client_report_text(value)
        if not text:
            return ""
        normalized_key = re.sub(r"[^a-z0-9]", "", text.lower())
        known_labels = {
            "high": "High",
            "medium": "Medium",
            "low": "Low",
            "critical": "Critical",
            "revenueleakage": "Revenue Leakage",
            "cashflow": "Cash Flow",
            "workflowefficiency": "Workflow Efficiency",
            "costmanagement": "Cost Management",
            "revenuecycle": "Revenue Cycle",
        }
        if normalized_key in known_labels:
            return known_labels[normalized_key]

        spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
        spaced = re.sub(r"[_-]+", " ", spaced)
        spaced = re.sub(r"\s{2,}", " ", spaced).strip()
        if not spaced:
            return ""
        words = [
            word if word.isupper() and len(word) <= 4 else word[:1].upper() + word[1:].lower()
            for word in spaced.split()
        ]
        return " ".join(words)

    def badge_style(
        label: str,
        value: str,
    ) -> tuple[tuple[int, int, int], tuple[int, int, int], tuple[int, int, int]]:
        label_key = label.lower()
        value_key = re.sub(r"[^a-z0-9]", "", value.lower())
        if label_key == "severity":
            if value_key in {"high", "critical"}:
                return (255, 243, 232), (245, 168, 113), navy
            if value_key == "medium":
                return (244, 239, 255), (196, 177, 255), navy
            return (236, 253, 246), (134, 239, 207), navy
        if label_key == "confidence":
            return (231, 252, 246), (118, 232, 203), navy
        if label_key == "impact":
            return (246, 241, 255), (205, 190, 255), navy
        if label_key == "difficulty":
            return (242, 246, 252), (205, 214, 229), navy
        return (242, 240, 255), (212, 202, 252), navy

    def badge_text_for(label: str, value: str) -> str:
        compact_labels = {
            "Category": "Cat",
            "Severity": "Sev",
            "Confidence": "Conf",
            "Difficulty": "Diff",
        }
        return f"{compact_labels.get(label, label)}: {value}"

    def badge_width(text: str) -> float:
        set_bold(6, navy)
        return min(max(pdf.get_string_width(sanitize_pdf_text(text)) + 5.0, 16), content_width - 12)

    def estimate_badge_row_height(badges: list[tuple[str, str]], max_width: float) -> float:
        if not badges:
            return 0
        x_offset = 0.0
        rows = 1
        gap = 1.5
        for label, value in badges:
            width = min(badge_width(badge_text_for(label, value)), max_width)
            if x_offset and x_offset + width > max_width:
                rows += 1
                x_offset = 0.0
            x_offset += width + gap
        return rows * 5.4 + (rows - 1) * 1.6

    def draw_badge_shape(
        x: float,
        y: float,
        width: float,
        height: float,
        fill: tuple[int, int, int],
        border_color: tuple[int, int, int],
    ) -> None:
        pdf.set_fill_color(*fill)
        pdf.set_draw_color(*border_color)
        radius = min(2.0, width / 2, height / 2)
        try:
            k = pdf.k
            page_height = pdf.h
            curve = 0.5522847498

            def point(px: float, py: float) -> str:
                return f"{px * k:.2f} {(page_height - py) * k:.2f}"

            x1, y1 = x, y
            x2, y2 = x + width, y + height
            c = curve * radius
            commands = [
                f"{point(x1 + radius, y1)} m",
                f"{point(x2 - radius, y1)} l",
                f"{point(x2 - radius + c, y1)} {point(x2, y1 + radius - c)} {point(x2, y1 + radius)} c",
                f"{point(x2, y2 - radius)} l",
                f"{point(x2, y2 - radius + c)} {point(x2 - radius + c, y2)} {point(x2 - radius, y2)} c",
                f"{point(x1 + radius, y2)} l",
                f"{point(x1 + radius - c, y2)} {point(x1, y2 - radius + c)} {point(x1, y2 - radius)} c",
                f"{point(x1, y1 + radius)} l",
                f"{point(x1, y1 + radius - c)} {point(x1 + radius - c, y1)} {point(x1 + radius, y1)} c",
                "h B",
            ]
            pdf._out(" ".join(commands))
        except Exception:
            pdf.rect(x, y, width, height, "FD")

    def render_badge_row(
        badges: list[tuple[str, str]],
        start_x: float,
        max_width: float,
    ) -> None:
        if not badges:
            return
        gap = 1.5
        badge_height = 5.4
        x = start_x
        y = pdf.get_y()
        for label, value in badges:
            text = badge_text_for(label, value)
            width = min(badge_width(text), max_width)
            if x > start_x and x + width > start_x + max_width:
                x = start_x
                y += badge_height + 1.6

            fill, border_color, text_color = badge_style(label, value)
            draw_badge_shape(x, y, width, badge_height, fill, border_color)
            pdf.set_xy(x + 2.5, y + 1.25)
            set_bold(6, text_color)
            pdf.cell(width - 5.0, 3.2, sanitize_pdf_text(text), ln=0)
            x += width + gap

        pdf.set_y(y + badge_height + 1.6)
        pdf.set_x(start_x)

    def render_opportunity(item: object, idx: int, *, format_financial: bool = False) -> None:
        if isinstance(item, dict):
            title_text = (item.get("title") or "").strip()
            impact_text = (item.get("impact") or "").strip()
            rec_text = (item.get("recommendation") or "").strip()
            if format_financial:
                title_text = format_client_report_text(title_text)
                impact_text = format_client_report_text(impact_text)
                rec_text = format_client_report_text(rec_text)
            card_inner_width = content_width - 12
            title_height = estimate_text_height(title_text or f"Opportunity {idx}", card_inner_width, 6, 11)
            impact_height = estimate_text_height(impact_text, card_inner_width, 5.4, 9) if impact_text else 0
            rec_height = estimate_text_height(rec_text, card_inner_width, 5.4, 9) if rec_text else 0
            card_height = 13 + title_height + impact_height + rec_height
            if impact_text:
                card_height += 8
            if rec_text:
                card_height += 8
            ensure_space(card_height + 5)
            card_y = pdf.get_y()
            draw_card(pdf.l_margin, card_y, content_width, card_height, (255, 255, 255))
            pdf.set_fill_color(*lilac)
            pdf.rect(pdf.l_margin, card_y, 2, card_height, "F")
            pdf.set_xy(pdf.l_margin + 6, card_y + 5)
            set_bold(11, navy)
            safe_pdf_multi_cell(pdf, title_text or f"Opportunity {idx}", f"opportunity:{idx}:title", height=6, width=card_inner_width)
            if impact_text:
                pdf.ln(1)
                pdf.set_x(pdf.l_margin + 6)
                render_label("Impact", teal)
                pdf.set_x(pdf.l_margin + 6)
                set_regular(9, body)
                safe_pdf_multi_cell(pdf, impact_text, f"opportunity:{idx}:impact", height=5.4, width=card_inner_width)
            if rec_text:
                pdf.ln(1)
                pdf.set_x(pdf.l_margin + 6)
                render_label("Recommendation", green)
                pdf.set_x(pdf.l_margin + 6)
                set_regular(9, body)
                safe_pdf_multi_cell(pdf, rec_text, f"opportunity:{idx}:recommendation", height=5.4, width=card_inner_width)
            pdf.set_y(card_y + card_height + 4)
            return
        combined = str(item).strip() if item is not None else ""
        if combined:
            if format_financial:
                combined = format_client_report_text(combined)
            render_bullet(combined, f"opportunity:{idx}")

    def structured_dict(key: str) -> dict[str, Any]:
        structured = sections.get("structured")
        if not isinstance(structured, dict):
            return {}
        value = structured.get(key)
        return value if isinstance(value, dict) else {}

    def structured_list(key: str) -> list[Any]:
        structured = sections.get("structured")
        if not isinstance(structured, dict):
            return []
        value = structured.get(key)
        return value if isinstance(value, list) else []

    def render_executive_summary() -> None:
        summary = structured_dict("executive_summary")
        rows = [
            ("Summary", summary.get("summary")),
            ("Primary Concern", summary.get("primaryConcern")),
            ("Recommended Focus", summary.get("recommendedFocus")),
        ]
        rows = [(label, format_client_report_text(value)) for label, value in rows if format_client_report_text(value)]
        if not rows:
            return

        section_title("Executive Summary", accent=lilac)
        card_inner_width = content_width - 12
        card_height = 12 + sum(estimate_text_height(value, card_inner_width, 5.4, 9) + 8 for _, value in rows)
        ensure_space(card_height + 4)
        card_y = pdf.get_y()
        draw_card(pdf.l_margin, card_y, content_width, card_height, card)
        pdf.set_xy(pdf.l_margin + 6, card_y + 6)
        for label, value in rows:
            render_label(label, lilac)
            pdf.set_x(pdf.l_margin + 6)
            set_regular(9, body)
            safe_pdf_multi_cell(pdf, value, f"structured:summary:{label}", height=5.4, width=card_inner_width)
            pdf.ln(1)
            pdf.set_x(pdf.l_margin + 6)
        pdf.set_y(card_y + card_height + 4)

    def render_ranked_finding(item: dict[str, Any], idx: int) -> None:
        title = format_client_report_text(item.get("title")) or f"Finding {idx}"
        rank = item.get("rank") or idx
        category = normalize_badge_value(item.get("category"))
        severity = normalize_badge_value(item.get("severity"))
        confidence = normalize_badge_value(item.get("confidence"))
        impact_category = normalize_badge_value(item.get("estimatedImpactCategory"))
        difficulty = normalize_badge_value(item.get("implementationDifficulty"))
        financial_value = format_client_report_text(item.get("financialValue"), force_currency=True)
        client_summary = format_client_report_text(item.get("clientFacingSummary"))
        operational_implication = format_client_report_text(item.get("operationalImplication"))
        recommended_action = format_client_report_text(item.get("recommendedAction"))
        evidence_items = item.get("evidence") if isinstance(item.get("evidence"), list) else []

        metadata_badges = [
            ("Category", category),
            ("Severity", severity),
            ("Confidence", confidence),
            ("Impact", impact_category),
            ("Difficulty", difficulty),
        ]
        metadata_badges = [(label, value) for label, value in metadata_badges if value]
        content_blocks = [
            ("Client Summary", client_summary),
            ("Operational Implication", operational_implication),
            ("Recommended Action", recommended_action),
        ]
        content_blocks = [(label, value) for label, value in content_blocks if value]

        card_inner_width = content_width - 12
        card_height = 18 + estimate_text_height(title, card_inner_width, 6, 11)
        if metadata_badges:
            card_height += estimate_badge_row_height(metadata_badges, card_inner_width) + 3
        if financial_value:
            card_height += estimate_text_height(f"Financial Value: {financial_value}", card_inner_width, 4.8, 8) + 2
        for _, value in content_blocks:
            card_height += estimate_text_height(value, card_inner_width, 5.2, 9) + 9
        if evidence_items:
            card_height += 8
            for evidence in evidence_items[:5]:
                if isinstance(evidence, dict):
                    evidence_text = " - ".join(
                        format_client_report_text(evidence.get(key), force_currency=(key == "value"))
                        for key in ("label", "value", "sourceHint")
                        if format_client_report_text(evidence.get(key), force_currency=(key == "value"))
                    )
                    if evidence_text:
                        card_height += estimate_text_height(evidence_text, card_inner_width - 5, 5, 8) + 1

        ensure_space(card_height + 5)
        card_y = pdf.get_y()
        draw_card(pdf.l_margin, card_y, content_width, card_height, (255, 255, 255))
        pdf.set_fill_color(*teal)
        pdf.rect(pdf.l_margin, card_y, 2, card_height, "F")
        pdf.set_xy(pdf.l_margin + 6, card_y + 5)
        set_bold(11, navy)
        safe_pdf_multi_cell(pdf, f"{rank}. {title}", f"structured:finding:{idx}:title", height=6, width=card_inner_width)

        if metadata_badges:
            pdf.ln(1.5)
            pdf.set_x(pdf.l_margin + 6)
            render_badge_row(metadata_badges, pdf.l_margin + 6, card_inner_width)

        if financial_value:
            pdf.ln(0.5)
            pdf.set_x(pdf.l_margin + 6)
            set_regular(8, muted)
            safe_pdf_multi_cell(
                pdf,
                f"Financial Value: {financial_value}",
                f"structured:finding:{idx}:meta:Financial Value",
                height=4.8,
                width=card_inner_width,
            )
            pdf.set_x(pdf.l_margin + 6)

        for label, value in content_blocks:
            pdf.ln(1)
            pdf.set_x(pdf.l_margin + 6)
            render_label(label, lilac)
            pdf.set_x(pdf.l_margin + 6)
            set_regular(9, body)
            safe_pdf_multi_cell(pdf, value, f"structured:finding:{idx}:{label}", height=5.2, width=card_inner_width)

        rendered_evidence = []
        for evidence in evidence_items[:5]:
            if not isinstance(evidence, dict):
                continue
            evidence_text = " - ".join(
                format_client_report_text(evidence.get(key), force_currency=(key == "value"))
                for key in ("label", "value", "sourceHint")
                if format_client_report_text(evidence.get(key), force_currency=(key == "value"))
            )
            if evidence_text:
                rendered_evidence.append(evidence_text)
        if rendered_evidence:
            pdf.ln(1)
            pdf.set_x(pdf.l_margin + 6)
            render_label("Evidence", green)
            pdf.set_x(pdf.l_margin + 6)
            for evidence_text in rendered_evidence:
                render_bullet(evidence_text, f"structured:finding:{idx}:evidence", size=8, accent=green)
                pdf.set_x(pdf.l_margin + 6)

        pdf.set_y(card_y + card_height + 4)

    def render_ranked_findings() -> None:
        findings = [item for item in structured_list("ranked_findings") if isinstance(item, dict)]
        if not findings:
            return
        section_title("Ranked Findings", accent=teal)
        for idx, item in enumerate(findings, start=1):
            render_ranked_finding(item, idx)

    def has_ranked_findings() -> bool:
        return any(isinstance(item, dict) for item in structured_list("ranked_findings"))

    def trend_topic_key(text: str) -> str:
        normalized = re.sub(r"[^a-z0-9\s]", " ", text.lower())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        topic_markers = (
            ("gross production", ("gross", "production")),
            ("net production", ("net", "production")),
            ("writeoffs", ("writeoff",)),
            ("adjustments", ("adjustment",)),
            ("collections", ("collection",)),
            ("labor", ("labor",)),
            ("payroll", ("payroll",)),
            ("hygiene", ("hygiene",)),
        )
        for label, markers in topic_markers:
            if all(marker in normalized for marker in markers):
                return label
        return normalized[:90]

    def cleaned_structured_list(key: str, *, max_items: int = 20) -> list[str]:
        items: list[str] = []
        seen: set[str] = set()
        for raw_item in structured_list(key):
            item = format_client_report_text(raw_item)
            if not item:
                continue
            dedupe_key = trend_topic_key(item) if key == "structured_trends" else item.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            items.append(item)
            if len(items) >= max_items:
                break
        return items

    def render_structured_bullet_section(
        title: str,
        key: str,
        field_label: str,
        accent: tuple[int, int, int],
        max_items: int = 20,
    ) -> None:
        items = cleaned_structured_list(key, max_items=max_items)
        if not items:
            return
        section_title(title, accent=accent)
        for idx, item in enumerate(items, start=1):
            render_bullet(item, f"{field_label}:{idx}", accent=accent)
        pdf.ln(1)

    logo_path = os.path.join(root, "public", "logo-dark-text.png")
    header_y = pdf.get_y()
    if os.path.exists(logo_path):
        try:
            pdf.image(logo_path, x=pdf.l_margin, y=header_y, w=34)
        except Exception:
            pass
    else:
        set_bold(10, navy)
        pdf.cell(34, 8, sanitize_pdf_text("alphaSource"), ln=0)

    report_date = datetime.utcnow()
    report_date_text = f"{report_date:%b} {report_date.day}, {report_date:%Y}"
    title_x = pdf.l_margin + 40
    pdf.set_xy(title_x, header_y + 1)
    set_bold(19, navy)
    pdf.cell(0, 9, sanitize_pdf_text("Detailed Analysis Report"), ln=1)
    pdf.set_x(title_x)
    set_regular(9, muted)
    version_text = f"Report version {version}" if version else "Report version -"
    pdf.cell(0, 6, sanitize_pdf_text(f"Generated {report_date_text} | {version_text}"), ln=1)
    pdf.set_y(max(pdf.get_y() + 8, header_y + 22))
    pdf.set_draw_color(*border)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(8)

    section_title("Client Details", accent=lilac)
    details = [
        ("Client Name", metadata.get("client_name")),
        ("Office/Group", metadata.get("office_name")),
        ("Client Email", metadata.get("client_email")),
        ("Tool", metadata.get("tool_name")),
    ]
    detail_padding_x = 7
    detail_padding_y = 5
    detail_gap = 0.8
    detail_label_width = 40
    detail_line_height = 5.6
    detail_value_width = content_width - (detail_padding_x * 2) - detail_label_width
    detail_height = detail_padding_y * 2
    for _, value in details:
        detail_height += estimate_text_height(value, detail_value_width, detail_line_height, 9)
    if len(details) > 1:
        detail_height += detail_gap * (len(details) - 1)
    ensure_space(detail_height + 2)
    detail_card_y = pdf.get_y()
    draw_card(pdf.l_margin, detail_card_y, content_width, detail_height)
    pdf.set_xy(pdf.l_margin + detail_padding_x, detail_card_y + detail_padding_y)
    for row_idx, (label, value) in enumerate(details, start=1):
        kv_row(
            label,
            value,
            f"metadata:{label}",
            label_width=detail_label_width,
            line_height=detail_line_height,
            label_color=muted,
            value_color=navy,
            label_size=9,
            value_size=9,
            start_x=pdf.l_margin + detail_padding_x,
        )
        if row_idx < len(details):
            pdf.ln(detail_gap)
    pdf.ln(10)

    structured_sections = sections.get("structured") if isinstance(sections.get("structured"), dict) else None
    if structured_sections:
        render_executive_summary()
        if has_ranked_findings():
            pdf.add_page()
        render_ranked_findings()

        opportunities = sections.get("opportunities") or []
        if opportunities:
            section_title("Improvement Opportunities", accent=lilac)
            for idx, item in enumerate(opportunities, start=1):
                render_opportunity(item, idx, format_financial=True)

        render_structured_bullet_section("Key Trends", "structured_trends", "structured:trends", teal, max_items=7)
        render_structured_bullet_section("30-Day Action Plan", "action_plan_items", "structured:action_plan", green)
        render_structured_bullet_section("Data Notes / Limitations", "data_notes", "structured:data_notes", lilac)
    else:
        section_specs = [
            ("opportunities", "Improvement Opportunities", lilac),
            ("trends", "Trends", teal),
            ("key_trends", "Key Trends Identified", green),
        ]
        for key, title, accent in section_specs:
            items = sections.get(key) or []
            if not items:
                continue
            section_title(title, accent=accent)
            if key == "opportunities":
                for idx, item in enumerate(items, start=1):
                    render_opportunity(item, idx)
            else:
                for idx, item in enumerate(items, start=1):
                    render_bullet(str(item).strip(), f"{key}:{idx}", accent=accent)
            pdf.ln(1)

    if notes:
        section_title("Additional Notes", accent=lilac)
        note_height = estimate_text_height(notes, content_width - 12, 5.6, 10) + 12
        ensure_space(note_height + 3)
        note_y = pdf.get_y()
        draw_card(pdf.l_margin, note_y, content_width, note_height, card)
        pdf.set_xy(pdf.l_margin + 6, note_y + 6)
        set_regular(10, body)
        safe_pdf_multi_cell(pdf, notes, "notes:body", height=5.6, width=content_width - 12)
        pdf.set_y(note_y + note_height + 2)

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
