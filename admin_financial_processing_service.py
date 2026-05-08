from __future__ import annotations

import csv
import logging
import os
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from io import BytesIO, StringIO
from typing import Any, Callable, Optional

from supabase_utils import _get_supabase_admin_client

logger = logging.getLogger("uvicorn.error")

MAX_PROVIDER_RETRIES = 2
PROVIDER_RETRY_BACKOFF_SECONDS = 0.75
TRANSIENT_PROVIDER_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
PROVIDER_UNAVAILABLE_MESSAGE = (
    "Analysis unavailable from this model due to temporary provider capacity. "
    "Other model results were processed."
)
MAX_XLSX_SHEETS = 10
MAX_XLSX_ROWS_PER_SHEET = 500
MAX_XLSX_COLUMNS_PER_ROW = 50
MAX_XLSX_CELL_CHARS = 500

CancelChecker = Callable[[], bool]


class AdminFinancialProcessingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class AdminFinancialProcessingCanceled(Exception):
    pass


def download_upload_file_bytes(bucket: str, object_path: str) -> bytes:
    if not bucket or not object_path:
        raise AdminFinancialProcessingError(
            "missing_storage_location",
            "Stored file location is missing.",
        )

    client = _get_supabase_admin_client()
    if not client:
        raise AdminFinancialProcessingError(
            "storage_not_configured",
            "File storage is not configured.",
        )

    try:
        response = client.storage.from_(bucket).download(object_path)
    except Exception as exc:
        logger.warning(
            "[admin_analysis] storage download failed bucket=%s object_path=%s error_type=%s",
            bucket,
            object_path,
            type(exc).__name__,
        )
        raise AdminFinancialProcessingError(
            "storage_download_failed",
            "Unable to download stored financial file.",
        ) from exc

    file_bytes = _response_to_bytes(response)
    if not file_bytes:
        raise AdminFinancialProcessingError(
            "storage_download_empty",
            "Stored financial file is empty.",
        )
    return file_bytes


def extract_csv_text(file_bytes: bytes) -> str:
    text = _decode_csv_bytes(file_bytes)
    rows = list(csv.reader(StringIO(text)))
    if not rows:
        raise AdminFinancialProcessingError(
            "empty_csv",
            "CSV file did not contain any rows.",
        )

    rendered_rows = []
    for row in rows:
        rendered_rows.append(" | ".join(str(value).strip() for value in row))
    data_input = "\n".join(rendered_rows).strip()
    if not data_input:
        raise AdminFinancialProcessingError(
            "empty_csv",
            "CSV file did not contain readable data.",
        )
    return data_input


def extract_xlsx_text(file_bytes: bytes) -> str:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise AdminFinancialProcessingError(
            "xlsx_dependency_missing",
            "XLSX processing is not configured.",
        ) from exc

    try:
        workbook = load_workbook(
            filename=BytesIO(file_bytes),
            read_only=True,
            data_only=True,
        )
    except Exception as exc:
        raise AdminFinancialProcessingError(
            "xlsx_read_failed",
            "XLSX file could not be read.",
        ) from exc

    rendered_sheets = []
    try:
        for sheet in workbook.worksheets[:MAX_XLSX_SHEETS]:
            rendered_rows = [f"Sheet: {_render_xlsx_cell(sheet.title)}"]
            for row_index, row in enumerate(
                sheet.iter_rows(
                    max_row=MAX_XLSX_ROWS_PER_SHEET,
                    max_col=MAX_XLSX_COLUMNS_PER_ROW,
                    values_only=True,
                ),
                start=1,
            ):
                rendered_cells = [_render_xlsx_cell(value) for value in row]
                while rendered_cells and not rendered_cells[-1]:
                    rendered_cells.pop()
                if not rendered_cells:
                    continue
                rendered_rows.append(" | ".join(rendered_cells))
                if row_index >= MAX_XLSX_ROWS_PER_SHEET:
                    break

            if len(rendered_rows) > 1:
                rendered_sheets.append("\n".join(rendered_rows))
    finally:
        workbook.close()

    data_input = "\n\n".join(rendered_sheets).strip()
    if not data_input:
        raise AdminFinancialProcessingError(
            "empty_xlsx",
            "XLSX file did not contain readable data.",
        )
    return data_input


def run_financial_csv_analysis(
    data_input: str,
    *,
    cancel_checker: Optional[CancelChecker] = None,
    source_format: str = "csv",
) -> dict[str, Any]:
    _raise_if_canceled(cancel_checker)
    model_labels = _get_model_labels()
    provider_specs = [
        ("openai", "OpenAI Analysis", "openai", _openai_analysis),
        ("xai", "xAI Analysis", "xai", _xai_analysis),
        ("anthropic", "AnthropicAI Analysis", "anthropic", _anthropic_analysis),
    ]

    raw_analyses: dict[str, str] = {}
    provider_statuses: dict[str, dict[str, Any]] = {}
    parsed_issues: dict[str, list[dict[str, Any]]] = {}
    parsed_trends: dict[str, list[dict[str, Any]]] = {}
    successful_provider_count = 0

    for provider_name, result_key, label_key, analysis_func in provider_specs:
        _raise_if_canceled(cancel_checker)
        analysis_text, succeeded, error_type = _run_provider_analysis_with_retry(
            provider_name=provider_name,
            analysis_func=analysis_func,
            data_input=data_input,
        )
        _raise_if_canceled(cancel_checker)

        raw_analyses[result_key] = analysis_text
        provider_statuses[provider_name] = {
            "ok": succeeded,
            "errorType": error_type,
        }
        parsed_issues[label_key] = parse_issues_from_analysis(
            analysis_text,
            model_labels[label_key],
        )
        parsed_trends[label_key] = parse_trends_from_analysis(
            analysis_text,
            model_labels[label_key],
        )
        if succeeded:
            successful_provider_count += 1

    if successful_provider_count == 0:
        raise AdminFinancialProcessingError(
            "provider_unavailable",
            "The analyzer providers are temporarily unavailable. Please try again later.",
        )

    all_issues = parsed_issues["openai"] + parsed_issues["xai"] + parsed_issues["anthropic"]
    all_trends = parsed_trends["openai"] + parsed_trends["xai"] + parsed_trends["anthropic"]
    deduplicated_issues = deduplicate_issues(all_issues)

    return {
        "sourceFormat": source_format,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "raw_analyses": raw_analyses,
        "provider_statuses": provider_statuses,
        "parsed_issues": parsed_issues,
        "parsed_trends": parsed_trends,
        "all_trends": all_trends,
        "deduplicated_issues": deduplicated_issues,
        "total_issue_count": len(deduplicated_issues),
    }


def parse_issues_from_analysis(analysis_text: str, source_model: str) -> list[dict[str, Any]]:
    issues = []

    if "---TRENDS---" in analysis_text:
        improvements_section = analysis_text.split("---TRENDS---")[0]
    else:
        improvements_section = analysis_text

    lines = improvements_section.strip().split("\n")
    current_issue: dict[str, Any] = {}

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.upper().startswith("ISSUE:") or (len(line) > 0 and line[0].isdigit() and "." in line[:3]):
            if current_issue:
                issues.append(current_issue)

            issue_title = line.split(":", 1)[-1].strip() if ":" in line else line.split(".", 1)[-1].strip()
            current_issue = {
                "title": issue_title,
                "impact": "",
                "recommendation": "",
                "source": source_model,
                "full_text": line,
            }
        elif line.upper().startswith("IMPACT:"):
            if current_issue:
                current_issue["impact"] = line.split(":", 1)[-1].strip()
                current_issue["full_text"] += "\n" + line
        elif line.upper().startswith("RECOMMENDATION:"):
            if current_issue:
                current_issue["recommendation"] = line.split(":", 1)[-1].strip()
                current_issue["full_text"] += "\n" + line
        elif current_issue:
            current_issue["full_text"] += "\n" + line

    if current_issue:
        issues.append(current_issue)

    return issues


def parse_trends_from_analysis(analysis_text: str, source_model: str) -> list[dict[str, str]]:
    trends = []

    if "---TRENDS---" not in analysis_text:
        return trends

    trends_section = analysis_text.split("---TRENDS---")[1]
    lines = trends_section.strip().split("\n")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if "TREND:" in line.upper():
            trend_text = line.split(":", 1)[-1].strip() if ":" in line else line
            if len(trend_text) > 0 and trend_text[0].isdigit():
                trend_text = trend_text.split(".", 1)[-1].strip()

            if trend_text:
                trends.append(
                    {
                        "text": trend_text,
                        "source": source_model,
                    }
                )

    return trends


def deduplicate_issues(all_issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def similar(a: str, b: str) -> bool:
        return SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.7

    deduplicated = []
    used_indices: set[int] = set()

    for index, issue in enumerate(all_issues):
        if index in used_indices:
            continue

        similar_issues = [issue]
        sources = [issue["source"]]

        for other_index, other_issue in enumerate(all_issues[index + 1 :], start=index + 1):
            if other_index in used_indices:
                continue

            if similar(issue["title"], other_issue["title"]):
                similar_issues.append(other_issue)
                sources.append(other_issue["source"])
                used_indices.add(other_index)

        deduplicated.append(
            {
                "title": issue["title"],
                "impact": issue["impact"],
                "recommendation": issue["recommendation"],
                "sources": sources,
                "count": len(sources),
                "all_versions": similar_issues,
            }
        )
        used_indices.add(index)

    return deduplicated


def _response_to_bytes(response: Any) -> bytes:
    if isinstance(response, bytes):
        return response
    if isinstance(response, bytearray):
        return bytes(response)
    if isinstance(response, str):
        return response.encode("utf-8")

    content = getattr(response, "content", None)
    if isinstance(content, bytes):
        return content
    data = getattr(response, "data", None)
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, str):
        return data.encode("utf-8")

    if isinstance(response, dict):
        dict_data = response.get("data") or response.get("content")
        if isinstance(dict_data, bytes):
            return dict_data
        if isinstance(dict_data, bytearray):
            return bytes(dict_data)
        if isinstance(dict_data, str):
            return dict_data.encode("utf-8")

    return b""


def _decode_csv_bytes(file_bytes: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return file_bytes.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise AdminFinancialProcessingError(
        "csv_decode_failed",
        "CSV file could not be decoded.",
    )


def _render_xlsx_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        text = value.isoformat()
    else:
        text = str(value)
    text = " ".join(text.strip().split())
    if len(text) > MAX_XLSX_CELL_CHARS:
        return f"{text[:MAX_XLSX_CELL_CHARS]}..."
    return text


def _get_model_config() -> dict[str, str]:
    return {
        "openai": os.getenv("OPENAI_MODEL", "gpt-5-chat-latest"),
        "xai": os.getenv("XAI_MODEL", "grok-4-1-fast-reasoning"),
        "anthropic": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
    }


def _get_model_labels() -> dict[str, str]:
    models = _get_model_config()
    return {
        "openai": f"OpenAI ({models.get('openai')})" if models.get("openai") else "OpenAI",
        "xai": f"xAI ({models.get('xai')})" if models.get("xai") else "xAI",
        "anthropic": (
            f"Anthropic ({models.get('anthropic')})"
            if models.get("anthropic")
            else "Anthropic"
        ),
    }


def _get_analysis_prompt() -> str:
    return """You are an expert dental operations consultant with deep knowledge of practice management, revenue cycle, and operational efficiency.

IMPORTANT FORMATTING RULES:
- Use PLAIN TEXT only - no LaTeX, no math formatting, no special markup
- Write dollar amounts as plain text: $10,000 not $10,000$ or escaped dollar amounts
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


def _openai_analysis(data_input: str) -> str:
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("missing_openai_api_key")

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=_get_model_config()["openai"],
        messages=[
            {"role": "system", "content": _get_analysis_prompt()},
            {"role": "user", "content": f"Analyze this dental practice data:\n\n{data_input[:6000]}"},
        ],
        temperature=0.3,
        max_tokens=1500,
    )
    return response.choices[0].message.content


def _xai_analysis(data_input: str) -> str:
    from openai import OpenAI

    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        raise RuntimeError("missing_xai_api_key")

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.x.ai/v1",
    )
    response = client.chat.completions.create(
        model=_get_model_config()["xai"],
        messages=[
            {"role": "system", "content": _get_analysis_prompt()},
            {"role": "user", "content": f"Analyze this dental practice data:\n\n{data_input[:6000]}"},
        ],
        temperature=0.3,
        max_tokens=1500,
    )
    return response.choices[0].message.content


def _anthropic_analysis(data_input: str) -> str:
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("missing_anthropic_api_key")

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=_get_model_config()["anthropic"],
        max_tokens=1500,
        temperature=0.3,
        system=_get_analysis_prompt(),
        messages=[
            {"role": "user", "content": f"Analyze this dental practice data:\n\n{data_input[:6000]}"}
        ],
    )
    return message.content[0].text


def _run_provider_analysis_with_retry(
    *,
    provider_name: str,
    analysis_func: Callable[[str], str],
    data_input: str,
) -> tuple[str, bool, Optional[str]]:
    last_error_type: Optional[str] = None
    for attempt in range(1, MAX_PROVIDER_RETRIES + 2):
        try:
            analysis_text = analysis_func(data_input)
            if not isinstance(analysis_text, str) or not analysis_text.strip():
                raise ValueError("empty_provider_response")
            if attempt > 1:
                logger.info(
                    "[admin_analysis] provider recovered provider=%s attempt=%s",
                    provider_name,
                    attempt,
                )
            return analysis_text, True, None
        except Exception as exc:
            transient = _is_transient_provider_error(exc)
            last_error_type = _safe_provider_error_type(exc)
            logger.warning(
                "[admin_analysis] provider failed provider=%s attempt=%s transient=%s error_type=%s",
                provider_name,
                attempt,
                transient,
                last_error_type,
            )
            if not transient or attempt > MAX_PROVIDER_RETRIES:
                break
            time.sleep(PROVIDER_RETRY_BACKOFF_SECONDS * attempt)

    logger.warning("[admin_analysis] provider unavailable provider=%s", provider_name)
    return PROVIDER_UNAVAILABLE_MESSAGE, False, last_error_type


def _is_transient_provider_error(exc: Exception) -> bool:
    status_code = _provider_status_code(exc)
    if status_code in TRANSIENT_PROVIDER_STATUS_CODES:
        return True

    safe_text = f"{type(exc).__name__} {str(exc)}".lower()
    transient_markers = (
        "rate limit",
        "rate_limit",
        "overload",
        "overloaded",
        "temporarily unavailable",
        "temporary provider capacity",
        "timeout",
        "timed out",
        "server error",
        "internal server",
        "internal_server_error",
        "service unavailable",
        "bad gateway",
        "gateway timeout",
        "connection reset",
    )
    return any(marker in safe_text for marker in transient_markers)


def _provider_status_code(exc: Exception) -> Optional[int]:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value

    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if isinstance(value, int):
        return value
    return None


def _safe_provider_error_type(exc: Exception) -> str:
    status_code = _provider_status_code(exc)
    if status_code is not None:
        return f"{type(exc).__name__}:{status_code}"
    return type(exc).__name__


def _raise_if_canceled(cancel_checker: Optional[CancelChecker]) -> None:
    if cancel_checker and cancel_checker():
        raise AdminFinancialProcessingCanceled()
