"""
Shared analysis utilities for dental operations analysis.
Used by both app.py (public page) and admin_dashboard.py (admin page).
"""

import os
import base64
import textwrap
import logging
import sendgrid
from sendgrid.helpers.mail import Mail, Attachment, FileContent, FileName, FileType, Disposition, ContentId, TrackingSettings, ClickTracking
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from io import BytesIO
from typing import Callable, Optional
import pymupdf as fitz


DEFAULT_PDF_OCR_RENDER_SCALE = 2.0


class PdfTextExtractionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class PdfTextExtractionCanceled(Exception):
    pass


def extract_text_from_pdf(
    uploaded_file,
    *,
    enable_ocr: bool = True,
    max_pages: Optional[int] = None,
    max_ocr_pages: Optional[int] = None,
    max_chars: Optional[int] = None,
    ocr_render_scale: float = DEFAULT_PDF_OCR_RENDER_SCALE,
    cancel_checker: Optional[Callable[[], bool]] = None,
    raise_on_empty: bool = False,
):
    """Extract text from PDF file, using bounded OCR fallback for image-based pages."""
    _raise_if_pdf_extraction_canceled(cancel_checker)
    try:
        pdf_bytes = uploaded_file.read()
    except Exception as exc:
        raise PdfTextExtractionError(
            "pdf_read_failed",
            "PDF file could not be read.",
        ) from exc

    if not pdf_bytes:
        if raise_on_empty:
            raise PdfTextExtractionError(
                "empty_pdf_text",
                "PDF did not contain readable text.",
            )
        return ""

    _raise_if_pdf_extraction_canceled(cancel_checker)
    rendered_pages = []
    extracted_chars = 0
    ocr_pages = 0
    ocr_dependencies = None

    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if getattr(doc, "needs_pass", False):
                raise PdfTextExtractionError(
                    "pdf_read_failed",
                    "PDF file could not be read.",
                )

            for page_index, page in enumerate(doc):
                _raise_if_pdf_extraction_canceled(cancel_checker)
                if max_pages is not None and page_index >= max_pages:
                    break
                if max_chars is not None and extracted_chars >= max_chars:
                    break

                page_text = _normalize_pdf_text(page.get_text("text") or "")

                if (
                    not page_text
                    and enable_ocr
                    and (max_ocr_pages is None or ocr_pages < max_ocr_pages)
                ):
                    _raise_if_pdf_extraction_canceled(cancel_checker)
                    if ocr_dependencies is None:
                        ocr_dependencies = _load_pdf_ocr_dependencies()
                    page_text = _extract_pdf_page_ocr_text(
                        page,
                        ocr_dependencies,
                        ocr_render_scale=ocr_render_scale,
                    )
                    ocr_pages += 1
                    _raise_if_pdf_extraction_canceled(cancel_checker)

                if not page_text:
                    continue

                if max_chars is not None:
                    remaining_chars = max_chars - extracted_chars
                    if remaining_chars <= 0:
                        break
                    if len(page_text) > remaining_chars:
                        page_text = page_text[:remaining_chars].rstrip()

                if page_text:
                    rendered_pages.append(page_text)
                    extracted_chars += len(page_text)
    except PdfTextExtractionCanceled:
        raise
    except PdfTextExtractionError:
        raise
    except Exception as exc:
        raise PdfTextExtractionError(
            "pdf_read_failed",
            "PDF file could not be read.",
        ) from exc

    _raise_if_pdf_extraction_canceled(cancel_checker)
    text = "\n\n".join(rendered_pages).strip()
    if not text:
        if raise_on_empty:
            raise PdfTextExtractionError(
                "empty_pdf_text",
                "PDF did not contain readable text. Upload a clearer PDF or a file with selectable text.",
            )
        return ""
    return text


def _normalize_pdf_text(value: str) -> str:
    lines = [" ".join(line.strip().split()) for line in value.splitlines()]
    return "\n".join(line for line in lines if line)


def _load_pdf_ocr_dependencies():
    try:
        from PIL import Image
        import pytesseract
    except ImportError as exc:
        raise PdfTextExtractionError(
            "pdf_ocr_dependency_missing",
            "PDF OCR processing is temporarily unavailable.",
        ) from exc
    return Image, pytesseract


def _extract_pdf_page_ocr_text(page, ocr_dependencies, *, ocr_render_scale: float) -> str:
    Image, pytesseract = ocr_dependencies
    try:
        scale = ocr_render_scale if ocr_render_scale and ocr_render_scale > 0 else DEFAULT_PDF_OCR_RENDER_SCALE
        matrix = fitz.Matrix(scale, scale)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        with Image.open(BytesIO(pixmap.tobytes("png"))) as image:
            return _normalize_pdf_text(pytesseract.image_to_string(image) or "")
    except Exception as exc:
        if type(exc).__name__ == "TesseractNotFoundError":
            raise PdfTextExtractionError(
                "pdf_ocr_dependency_missing",
                "PDF OCR processing is temporarily unavailable.",
            ) from exc
        raise PdfTextExtractionError(
            "pdf_ocr_failed",
            "PDF OCR text could not be extracted.",
        ) from exc


def _raise_if_pdf_extraction_canceled(cancel_checker: Optional[Callable[[], bool]]) -> None:
    if not cancel_checker:
        return
    try:
        if cancel_checker():
            raise PdfTextExtractionCanceled()
    except PdfTextExtractionCanceled:
        raise
    except Exception as exc:
        if type(exc).__name__ == "PublicAnalyzerCanceled":
            raise PdfTextExtractionCanceled() from exc
        raise


def get_analysis_prompt(doc_type="general"):
    """Generate a detailed prompt based on document type"""
    base_prompt = """You are an expert dental operations consultant with deep knowledge of practice management, revenue cycle, and operational efficiency.

IMPORTANT FORMATTING RULES:
- Use PLAIN TEXT only - no LaTeX, no math formatting, no special markup
- Write dollar amounts as plain text: $10,000 not $10,000$ or \\$10,000
- Do not use asterisks for emphasis or formatting
- Keep all text on single lines without special characters

Analyze the provided data and identify improvement opportunities AND key trends.

SECTION 1 - IMPROVEMENT OPPORTUNITIES:
Identify AT LEAST 3-5 high-level strategic areas for improvement. Format each as:
ISSUE: [Brief, strategic title - keep it high-level, not overly specific]
IMPACT: [Why this matters - financial, operational, or compliance impact]
RECOMMENDATION: [General recommended action]

Focus on strategic opportunities in:
- Revenue cycle optimization
- Operational efficiency
- Cost management
- Patient experience
- Compliance and risk management
- Technology utilization
- Staff productivity

SECTION 2 - KEY TRENDS:
After the improvement opportunities, add a separator line "---TRENDS---" and then identify 3-5 quantitative trends from the data. Format each as:
TREND: [Specific trend with numbers/percentages/timeframes]

Examples of trends to look for:
- Cost increases/decreases over time (e.g., "Dental supplies increased 5% over past 90 days")
- Payment timing changes (e.g., "Average days to payment extended from 38 to 44 days over last 12 months")
- Volume trends (e.g., "Patient visits declined 12% quarter-over-quarter")
- Revenue patterns (e.g., "Collections rate dropped from 94% to 89% in Q3")
- Expense patterns (e.g., "Lab costs up 15% year-over-year")

Be specific with numbers, percentages, and timeframes when identifying trends."""

    if doc_type == "public_financial_preview":
        base_prompt += """

CLIENT-FACING NUMBER FORMATTING:
- Use concise, client-friendly language.
- All dollar amounts must use $ and comma separators.
- Dollar amounts must not include cents.
- Format raw financial values like this:
  - 240060.77 -> $240,061
  - 314170.43 -> $314,170
  - 3295272.15 -> $3,295,272
  - 901025.36 -> $901,025
- Percentages should remain percentages, e.g. 27%, not $27.
- Counts, dates, months, percentages, and ratios should not be converted to dollars."""

    return base_prompt


_MODEL_CONFIG = None

def get_model_config():
    global _MODEL_CONFIG
    if _MODEL_CONFIG is not None:
        return _MODEL_CONFIG
    openai_model = os.getenv("OPENAI_MODEL", "gpt-5-chat-latest")
    xai_model = os.getenv("XAI_MODEL", "grok-4.3")
    anthropic_model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    _MODEL_CONFIG = {
        "openai": openai_model,
        "xai": xai_model,
        "anthropic": anthropic_model,
    }
    return _MODEL_CONFIG

def get_model_labels():
    models = get_model_config()
    openai_model = models.get("openai")
    xai_model = models.get("xai")
    anthropic_model = models.get("anthropic")
    return {
        "openai": f"OpenAI ({openai_model})" if openai_model else "OpenAI",
        "xai": f"xAI ({xai_model})" if xai_model else "xAI",
        "anthropic": f"Anthropic ({anthropic_model})" if anthropic_model else "Anthropic",
    }

def log_active_models(run_id=None) -> None:
    models = get_model_config()
    logging.info(
        "[models] run_id=%s openai=%s xai=%s anthropic=%s",
        run_id,
        models.get("openai"),
        models.get("xai"),
        models.get("anthropic"),
    )

def openai_analysis(data_input, doc_type="general"):
    """Run analysis using OpenAI"""
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    
    prompt = get_analysis_prompt(doc_type)
    model_name = get_model_config()["openai"]
    
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Analyze this dental practice data:\n\n{data_input[:6000]}"}
        ],
        temperature=0.3,
        max_tokens=1500
    )
    return response.choices[0].message.content


def xai_analysis(data_input, doc_type="general"):
    """Use xAI's Grok model for analysis"""
    from openai import OpenAI
    
    client = OpenAI(
        api_key=os.getenv("XAI_API_KEY"),
        base_url="https://api.x.ai/v1"
    )
    
    prompt = get_analysis_prompt(doc_type)
    model_name = get_model_config()["xai"]
    
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Analyze this dental practice data:\n\n{data_input[:6000]}"}
        ],
        temperature=0.3,
        max_tokens=1500
    )
    return response.choices[0].message.content


def anthropic_analysis(data_input, doc_type="general"):
    """Use Anthropic's Claude model for analysis"""
    import anthropic
    
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    
    prompt = get_analysis_prompt(doc_type)
    model_name = get_model_config()["anthropic"]
    
    message = client.messages.create(
        model=model_name,
        max_tokens=1500,
        temperature=0.3,
        system=prompt,
        messages=[
            {"role": "user", "content": f"Analyze this dental practice data:\n\n{data_input[:6000]}"}
        ]
    )
    return message.content[0].text


def parse_issues_from_analysis(analysis_text, source_model):
    """Extract individual issues from AI analysis text"""
    issues = []
    
    if '---TRENDS---' in analysis_text:
        improvements_section = analysis_text.split('---TRENDS---')[0]
    else:
        improvements_section = analysis_text
    
    lines = improvements_section.strip().split('\n')
    current_issue = {}
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        if line.upper().startswith('ISSUE:') or (len(line) > 0 and line[0].isdigit() and '.' in line[:3]):
            if current_issue:
                issues.append(current_issue)
            
            issue_title = line.split(':', 1)[-1].strip() if ':' in line else line.split('.', 1)[-1].strip()
            current_issue = {
                'title': issue_title,
                'impact': '',
                'recommendation': '',
                'source': source_model,
                'full_text': line
            }
        elif line.upper().startswith('IMPACT:'):
            if current_issue:
                current_issue['impact'] = line.split(':', 1)[-1].strip()
                current_issue['full_text'] += '\n' + line
        elif line.upper().startswith('RECOMMENDATION:'):
            if current_issue:
                current_issue['recommendation'] = line.split(':', 1)[-1].strip()
                current_issue['full_text'] += '\n' + line
        elif current_issue:
            current_issue['full_text'] += '\n' + line
    
    if current_issue:
        issues.append(current_issue)
    
    return issues


def parse_trends_from_analysis(analysis_text, source_model):
    """Extract trends from AI analysis text"""
    trends = []
    
    if '---TRENDS---' not in analysis_text:
        return trends
    
    trends_section = analysis_text.split('---TRENDS---')[1]
    lines = trends_section.strip().split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        if 'TREND:' in line.upper():
            trend_text = line.split(':', 1)[-1].strip() if ':' in line else line
            if len(trend_text) > 0 and trend_text[0].isdigit():
                trend_text = trend_text.split('.', 1)[-1].strip()
            
            if trend_text:
                trends.append({
                    'text': trend_text,
                    'source': source_model
                })
    
    return trends


def deduplicate_issues(all_issues):
    """Deduplicate similar issues across models using simple text similarity"""
    from difflib import SequenceMatcher
    
    def similar(a, b):
        """Check if two strings are similar (>70% match)"""
        return SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.7
    
    deduplicated = []
    used_indices = set()
    
    for i, issue in enumerate(all_issues):
        if i in used_indices:
            continue
        
        similar_issues = [issue]
        sources = [issue['source']]
        
        for j, other_issue in enumerate(all_issues[i+1:], start=i+1):
            if j in used_indices:
                continue
            
            if similar(issue['title'], other_issue['title']):
                similar_issues.append(other_issue)
                sources.append(other_issue['source'])
                used_indices.add(j)
        
        dedup_issue = {
            'title': issue['title'],
            'impact': issue['impact'],
            'recommendation': issue['recommendation'],
            'sources': sources,
            'count': len(sources),
            'all_versions': similar_issues
        }
        deduplicated.append(dedup_issue)
        used_indices.add(i)
    
    return deduplicated


def analyze_with_all_models(data_input):
    """Run analysis with all 3 models and return both raw and processed results"""
    openai_result = openai_analysis(data_input)
    xai_result = xai_analysis(data_input)
    anthropic_result = anthropic_analysis(data_input)

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
    
    return {
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
        "total_issue_count": len(deduplicated_issues)
    }


def categorize_issue(title):
    """Categorize an issue title into a strategic category"""
    if any(keyword in title.lower() for keyword in ['revenue', 'collection', 'payment', 'billing', 'ar', 'receivable']):
        return "Revenue Cycle Optimization"
    elif any(keyword in title.lower() for keyword in ['cost', 'expense', 'supply', 'overhead', 'lab']):
        return "Cost Management Opportunities"
    elif any(keyword in title.lower() for keyword in ['claim', 'insurance', 'denial', 'reimbursement']):
        return "Claims Process Enhancement"
    elif any(keyword in title.lower() for keyword in ['staff', 'team', 'productivity', 'efficiency', 'workflow']):
        return "Operational Efficiency Gains"
    elif any(keyword in title.lower() for keyword in ['patient', 'schedule', 'appointment', 'experience']):
        return "Patient Experience Improvement"
    elif any(keyword in title.lower() for keyword in ['technology', 'software', 'system', 'automation']):
        return "Technology & Automation"
    else:
        return "Strategic Growth Opportunities"


def sanitize_streamlit_text(text):
    """
    Sanitize AI-generated text for proper Streamlit markdown rendering.
    Escapes LaTeX math delimiters ($) that cause rendering issues.
    """
    import re
    if not text:
        return text
    
    # Remove LaTeX-style math expressions entirely: $...$
    # These cause Streamlit to render text vertically and with weird formatting
    text = re.sub(r'\$([^$]+)\$', r'\1', text)
    
    # Escape any remaining lone dollar signs to prevent LaTeX interpretation
    # But preserve currency amounts like $500 by not escaping $ followed by digit
    text = re.sub(r'\$(?!\d)', r'\\$', text)
    
    # Remove double asterisks that create unwanted bold/formatting
    text = re.sub(r'\*\*', '', text)
    
    # Remove single asterisks used for italics
    text = re.sub(r'(?<!\*)\*(?!\*)', '', text)
    
    # Clean up any LaTeX artifacts like \$ back to $
    text = text.replace('\\$', '$')
    
    # Remove common LaTeX formatting artifacts
    text = re.sub(r'\\text\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\mathbf\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\mathrm\{([^}]+)\}', r'\1', text)
    
    # Normalize multiple spaces
    text = re.sub(r'\s+', ' ', text)
    
    return text.strip()


def normalize_insight_text(text):
    """Fix common formatting issues in AI-generated text like missing spaces"""
    import re
    # First sanitize for Streamlit rendering
    text = sanitize_streamlit_text(text)
    
    # Then fix spacing issues
    text = re.sub(r',([a-zA-Z])', r', \1', text)
    text = re.sub(r'\.(\d)', r'. \1', text)
    text = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', text)
    text = re.sub(r'([a-zA-Z])(\$\d)', r'\1 \2', text)
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def normalize_client_email_insight_text(text):
    """Normalize client email insight copy while preserving currency formatting."""
    import re
    if not text:
        return text

    text = text.replace('\\$', '$')
    text = re.sub(r'\*\*', '', text)
    text = re.sub(r'(?<!\*)\*(?!\*)', '', text)
    text = re.sub(r'\\text\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\mathbf\{([^}]+)\}', r'\1', text)
    text = re.sub(r'\\mathrm\{([^}]+)\}', r'\1', text)
    text = re.sub(r',([a-zA-Z])', r', \1', text)
    text = re.sub(r'(?<!\d)\.(\d)', r'. \1', text)
    text = re.sub(r'(\d)([a-zA-Z])', r'\1 \2', text)
    text = re.sub(r'([a-zA-Z])(\$\d)', r'\1 \2', text)
    text = re.sub(r'([a-z])([A-Z])', r'\1 \2', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return format_client_financial_values(text)


FINANCIAL_CONTEXT_PATTERN = None
FINANCIAL_NUMBER_PATTERN = None


def format_client_financial_values(text):
    """Format likely financial values in client-facing preview insight text."""
    import re

    if not text:
        return text

    global FINANCIAL_CONTEXT_PATTERN, FINANCIAL_NUMBER_PATTERN
    if FINANCIAL_CONTEXT_PATTERN is None:
        financial_keywords = [
            "production",
            "revenue",
            "income",
            "collections?",
            "write-?offs?",
            "adjustments?",
            "costs?",
            "profits?",
            "gross",
            "net",
            "ebitda",
            "overhead",
            "expenses?",
            "fees?",
            "payments?",
            "accounts receivable",
        ]
        FINANCIAL_CONTEXT_PATTERN = re.compile(
            r"\b(?:" + "|".join(financial_keywords) + r")\b|\bAR\b",
            re.IGNORECASE,
        )
    if FINANCIAL_NUMBER_PATTERN is None:
        FINANCIAL_NUMBER_PATTERN = re.compile(
            r"(?<![$\w])(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
        )

    def format_segment(segment):
        if not FINANCIAL_CONTEXT_PATTERN.search(segment):
            return segment

        def replace_match(match):
            raw_number = match.group("number")
            start, end = match.span("number")
            previous_char = segment[start - 1] if start > 0 else ""
            next_char = segment[end] if end < len(segment) else ""
            following_text = segment[end : end + 12].lower()

            if previous_char == "$":
                return raw_number
            if next_char in {"%", "/", "-"} or previous_char in {"/", "-"}:
                return raw_number
            if following_text.startswith(" percent") or following_text.startswith(" percentage"):
                return raw_number
            if _looks_like_year(raw_number):
                return raw_number
            if not _looks_like_financial_amount(raw_number):
                return raw_number

            formatted = _format_currency_without_cents(raw_number)
            return formatted or raw_number

        return FINANCIAL_NUMBER_PATTERN.sub(replace_match, segment)

    segments = re.split(r"([.!?]\s+|\n+)", text)
    return "".join(format_segment(segment) if index % 2 == 0 else segment for index, segment in enumerate(segments))


def _looks_like_year(raw_number):
    normalized = raw_number.replace(",", "")
    if "." in normalized:
        return False
    if not normalized.isdigit():
        return False
    year = int(normalized)
    return 1900 <= year <= 2099


def _looks_like_financial_amount(raw_number):
    normalized = raw_number.replace(",", "")
    if "." in normalized:
        try:
            return Decimal(normalized) >= Decimal("1000")
        except InvalidOperation:
            return False
    if "," in raw_number:
        return True
    if not normalized.isdigit():
        return False
    return int(normalized) >= 1000


def _format_currency_without_cents(raw_number):
    try:
        amount = Decimal(raw_number.replace(",", ""))
    except InvalidOperation:
        return None
    rounded = int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"${rounded:,}"


def extract_compelling_insights(results, max_insights=5, format_financial_values=False):
    """
    Extract 3-5 compelling, specific insights from AI analysis results.
    Prioritizes quantitative trends and specific areas of concern.
    """
    insights = []
    
    all_trends = results.get('all_trends', [])
    deduplicated_issues = results.get('deduplicated_issues', [])
    
    seen_texts = set()
    
    for trend in all_trends:
        text = trend['text'].strip()
        if text and text.lower() not in seen_texts:
            if any(char.isdigit() for char in text) or '%' in text:
                insights.append({
                    'type': 'trend',
                    'text': text,
                    'priority': 1
                })
                seen_texts.add(text.lower())
    
    for issue in deduplicated_issues:
        title = issue.get('title', '').strip()
        impact = issue.get('impact', '').strip()
        
        if title and title.lower() not in seen_texts:
            if impact and len(impact) > 20:
                insight_text = f"{title}: {impact}"
            else:
                insight_text = title
            
            insights.append({
                'type': 'issue',
                'text': insight_text,
                'priority': 2 if issue.get('count', 1) > 1 else 3
            })
            seen_texts.add(title.lower())
    
    insights.sort(key=lambda x: x['priority'])
    
    final_insights = []
    trend_count = 0
    issue_count = 0
    
    for insight in insights:
        if len(final_insights) >= max_insights:
            break
        
        if insight['type'] == 'trend' and trend_count < 3:
            final_insights.append(insight['text'])
            trend_count += 1
        elif insight['type'] == 'issue' and issue_count < 3:
            final_insights.append(insight['text'])
            issue_count += 1
    
    if len(final_insights) < 3:
        for insight in insights:
            if len(final_insights) >= max_insights:
                break
            if insight['text'] not in final_insights:
                final_insights.append(insight['text'])
    
    normalized_insights = []
    for text in final_insights[:max_insights]:
        if format_financial_values:
            normalized_insights.append(normalize_client_email_insight_text(text))
        else:
            normalized_insights.append(normalize_insight_text(text))
    return normalized_insights


def send_followup_email(user_info, tool_name, results):
    """Send follow-up email to user with detailed insights in branded HTML format"""
    sg = sendgrid.SendGridAPIClient(api_key=os.getenv("SENDGRID_API_KEY"))
    
    insights = extract_compelling_insights(results, max_insights=5, format_financial_values=True)
    
    subject = "alphaSource Consulting Analysis Results - Key Insights"
    support_email = "hello@alphasourceconsulting.com"
    logo_attachment = None
    signature_html = '<span style="color:#0A1547;font-weight:700;">alphaSource Consulting</span>'
    logo_path = os.path.join(os.path.dirname(__file__), "public", "logo-dark-text.png")
    if os.path.exists(logo_path):
        with open(logo_path, "rb") as logo_file:
            logo_encoded = base64.b64encode(logo_file.read()).decode()
        logo_cid = "alphasource-consulting-logo"
        logo_attachment = Attachment(
            FileContent(logo_encoded),
            FileName("logo-dark-text.png"),
            FileType("image/png"),
            Disposition("inline"),
            ContentId(logo_cid),
        )
        signature_html = (
            f'<img src="cid:{logo_cid}" alt="alphaSource Consulting" width="220" '
            'style="display:block;max-width:220px;width:220px;height:auto;border:0;outline:none;text-decoration:none;">'
        )
    
    if insights:
        insights_html = ""
        for insight in insights:
            insights_html += f'<li style="margin-bottom:8px;">{insight}</li>'
    else:
        insights_html = """
            <li style="margin-bottom:8px;">Multiple areas of operational improvement identified</li>
            <li style="margin-bottom:8px;">Financial patterns requiring closer examination</li>
            <li style="margin-bottom:8px;">Opportunities for enhanced profitability</li>
        """
    
    plain_text = f"""Hi {user_info['first_name']},

Thank you for submitting your practice financial documents to alphaSource Consulting.

Our analysis has completed its review. Here are the key insights we identified:

{chr(10).join(['- ' + i for i in insights]) if insights else '- Multiple areas of operational improvement identified'}

What's Next?

These findings are an initial preview of what your practice files may be telling us. If you would like a deeper review, our team can prepare a consultant-reviewed Practice Opportunity Review with prioritized findings, recommended next steps, and a 30-day action plan.

We can also support implementation through focused projects or ongoing advisory support for revenue leakage, AR/claims, workflow efficiency, and growth opportunities.

To discuss the best next step, book a consultation or reply to this email.

Book a consultation: https://calendar.app.google/QWQor8w5MqDqGXHv7

alphaSource Consulting
{support_email}"""

    html_content = f'''<!doctype html>
<html lang="en" xmlns="http://www.w3.org/1999/xhtml">
  <head>
    <meta http-equiv="x-ua-compatible" content="ie=edge">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <meta name="format-detection" content="telephone=no,address=no,email=no,date=no,url=no">
    <meta name="color-scheme" content="light only">
    <meta name="supported-color-schemes" content="light">
    <title>Your Financial Analysis Results</title>
    <!--[if mso]>
      <xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml>
    <![endif]-->
    <style>
      @media (max-width: 640px) {{
        .container {{ width: 100% !important; max-width: 100% !important; }}
        .px-24 {{ padding-left: 16px !important; padding-right: 16px !important; }}
      }}
    </style>
  </head>
  <body style="margin:0;padding:0;background:#F8F9FD;">
    <div style="display:none!important;max-height:0;overflow:hidden;opacity:0;visibility:hidden;">
      Your alphaSource Consulting financial analysis is ready. Key insights identified for your practice.
    </div>

    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%!important;min-width:100%!important;background:#F8F9FD;">
      <tr>
        <td align="center">
          <table role="presentation" cellpadding="0" cellspacing="0" width="640" class="container" style="width:640px;max-width:640px;">
            <tr>
              <td class="px-24" style="padding:32px 24px 16px 24px;">
                <a href="https://www.alphasourceconsulting.com" target="_blank" style="text-decoration:none;border:0;outline:0;display:inline-block;">
                  <img src="https://rytlclkkcvvnkoncfaid.supabase.co/storage/v1/object/public/email-assets/Color%20logo%20-%20no%20background.png"
                    alt="alphaSource Consulting" width="300"
                    style="display:block;max-width:300px;width:300px;height:auto;border:0;outline:none;text-decoration:none;">
                </a>
              </td>
            </tr>

            <tr>
              <td class="px-24" style="padding:8px 24px 24px 24px;">
                <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="border-radius:18px;background:#FFFFFF;border:1px solid rgba(10,21,71,0.10);box-shadow:0 14px 34px rgba(10,21,71,0.08);">
                  <tr>
                    <td style="padding:28px 28px 8px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:#0A1547;font-size:22px;line-height:28px;font-weight:800;">
                      Your Financial Analysis Results
                    </td>
                  </tr>

                  <tr>
                    <td style="padding:0 28px 18px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:rgba(10,21,71,0.68);font-size:14px;line-height:22px;">
                      Hi {user_info['first_name']},<br><br>
                      Thank you for submitting your practice financial documents to alphaSource Consulting. Our analysis has completed its review.
                    </td>
                  </tr>

                  <tr>
                    <td style="padding:0 28px 10px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:#0A1547;font-size:16px;line-height:22px;font-weight:800;">
                      Key Insights Identified
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:0 28px 18px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:rgba(10,21,71,0.68);font-size:14px;line-height:22px;">
                      <ul style="margin:10px 0 0 18px;padding:0;color:rgba(10,21,71,0.68);">
                        {insights_html}
                      </ul>
                    </td>
                  </tr>

                  <tr>
                    <td style="padding:0 28px 10px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:#0A1547;font-size:16px;line-height:22px;font-weight:800;">
                      What's Next?
                    </td>
                  </tr>
                  <tr>
                    <td style="padding:0 28px 18px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:rgba(10,21,71,0.68);font-size:14px;line-height:22px;">
                      These findings are an initial preview of what your practice files may be telling us. If you would like a deeper review, our team can prepare a consultant-reviewed Practice Opportunity Review with prioritized findings, recommended next steps, and a 30-day action plan.<br><br>
                      We can also support implementation through focused projects or ongoing advisory support for revenue leakage, AR/claims, workflow efficiency, and growth opportunities.<br><br>
                      To discuss the best next step, book a consultation or reply to this email.
                    </td>
                  </tr>

                  <tr>
                    <td style="padding:6px 28px 18px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:rgba(10,21,71,0.68);font-size:14px;line-height:22px;">
                      {signature_html}
                    </td>
                  </tr>

                  <tr>
                    <td align="left" style="padding:10px 28px 28px 28px;">
                      <!--[if mso]>
                      <v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" href="https://calendar.app.google/QWQor8w5MqDqGXHv7" arcsize="12%" strokecolor="#AD8BF7" strokeweight="1px" fillcolor="#AD8BF7" style="height:44px;v-text-anchor:middle;width:280px;">
                        <w:anchorlock/>
                        <center style="color:#FFFFFF;font-family:Segoe UI, Arial, sans-serif;font-size:14px;font-weight:700;">
                          Book a Consultation
                        </center>
                      </v:roundrect>
                      <![endif]-->
                      <!--[if !mso]><!-- -->
                      <a href="https://calendar.app.google/QWQor8w5MqDqGXHv7" target="_blank"
                         style="display:inline-block;background:#A380F6;color:#FFFFFF;text-decoration:none;
                                font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;font-weight:700;
                                font-size:14px;line-height:14px;padding:15px 22px;border-radius:10px;
                                border:1px solid rgba(163,128,246,0.22);min-width:260px;text-align:center;">
                        Book a Consultation
                      </a>
                      <!--<![endif]-->
                    </td>
                  </tr>

                  <tr>
                    <td style="padding:0 28px 28px 28px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:rgba(10,21,71,0.50);font-size:12px;line-height:18px;">
                      Need help? Email <a href="mailto:{support_email}" style="color:#A380F6;text-decoration:none;">{support_email}</a>
                    </td>
                  </tr>

                </table>
              </td>
            </tr>

            <tr>
              <td class="px-24" style="padding:8px 24px 40px 24px;font-family:-apple-system, Inter, Segoe UI, Roboto, Helvetica, Arial, sans-serif;color:rgba(10,21,71,0.46);font-size:11px;line-height:16px;text-align:left;">
                &copy; alphaSource Consulting · All rights reserved.
              </td>
            </tr>

          </table>
        </td>
      </tr>
    </table>
  </body>
</html>'''
    
    message = Mail(
        from_email=support_email,
        to_emails=user_info['email'],
        subject=subject,
        plain_text_content=plain_text,
        html_content=html_content
    )
    if logo_attachment is not None:
        message.add_attachment(logo_attachment)
    
    message.tracking_settings = TrackingSettings()
    message.tracking_settings.click_tracking = ClickTracking(enable=False, enable_text=False)
    
    sg.send(message)


def send_email(user_info, file_content, file_name, file_type, results, tool_name):
    """Send full analysis email to admin team"""
    sg = sendgrid.SendGridAPIClient(api_key=os.getenv("SENDGRID_API_KEY"))
    
    subject = f"[{tool_name}] {user_info['office_name']} ({user_info['email']})"
    
    raw_analyses = results['raw_analyses']
    all_trends = results.get('all_trends', [])
    
    trends_section = ""
    if all_trends:
        trends_section = "\n\n=== KEY TRENDS IDENTIFIED ===\n"
        unique_trends = []
        for trend in all_trends[:5]:
            if trend['text'] not in [t['text'] for t in unique_trends]:
                unique_trends.append(trend)
        
        for i, trend in enumerate(unique_trends, 1):
            trends_section += f"{i}. {trend['text']} (Source: {trend['source']})\n"
    
    body = textwrap.dedent(f"""
        New file submitted for analysis.
        
        Tool: {tool_name}
        File Name: {file_name}
        File Type: {file_type}
        
        Submitted by:
        First Name: {user_info['first_name']}
        Last Name: {user_info['last_name']}
        Office/Group: {user_info['office_name']}
        Email: {user_info['email']}
        Type: {user_info['org_type']}
        
        --- AI Analysis ---
        
        OpenAI Analysis:
        {raw_analyses["OpenAI Analysis"]}
        
        xAI Analysis:
        {raw_analyses["xAI Analysis"]}
        
        AnthropicAI Analysis:
        {raw_analyses["AnthropicAI Analysis"]}{trends_section}
    """).strip()
    
    with open("/tmp/unified_analysis.txt", "w") as f:
        f.write(f"Analysis Results for {user_info['office_name']}\n")
        f.write(f"\nTool: {tool_name}\n")
        for model, analysis in raw_analyses.items():
            f.write(f"\n{model}:\n{analysis}\n")
    
    with open("/tmp/unified_analysis.txt", "rb") as f:
        file_data = f.read()
        encoded = base64.b64encode(file_data).decode()
        analysis_attachment = Attachment(
            FileContent(encoded),
            FileName("unified_analysis.txt"),
            FileType("text/plain"),
            Disposition("attachment")
        )
    
    original_encoded = base64.b64encode(file_content).decode()
    original_attachment = Attachment(
        FileContent(original_encoded),
        FileName(file_name),
        FileType(file_type),
        Disposition("attachment")
    )
    
    message = Mail(
        from_email="info@alphasourceai.com",
        to_emails="analyzer@alphasourceconsulting.com",
        subject=subject,
        plain_text_content=body
    )
    message.add_attachment(analysis_attachment)
    message.add_attachment(original_attachment)
    sg.send(message)
