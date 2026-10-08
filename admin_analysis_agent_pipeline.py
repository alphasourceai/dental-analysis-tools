"""Bounded agent-style stages for internal document analysis.

The specialist remains the configured analysis provider. This module never
persists source text, evidence snippets, or model output in its trace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


SOURCE_WINDOW_CHARS = 6000
NUMBER_PATTERN = re.compile(r"(?<![\w])\$?-?\d[\d,]*(?:\.\d+)?%?")
PATIENT_NAME_PATTERN = re.compile(r"\b(?i:patient|member)\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b")
DIRECT_IDENTIFIER_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"\b\d{3}[-.\s]\d{2}[-.\s]\d{4}\b|"
    r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AnalysisPlan:
    tool_type: str
    source_window: str
    source_format: str


def supervise(tool_type: str, source_format: str, extracted_text: str) -> AnalysisPlan:
    """Bind a request to one supported specialist and its actual provider window."""
    if tool_type not in {"financial", "ar", "claims"}:
        raise ValueError("unsupported_analysis_tool")
    source_window = extracted_text[:SOURCE_WINDOW_CHARS].strip()
    if not source_window:
        raise ValueError("empty_analysis_source")
    return AnalysisPlan(tool_type, source_window, source_format)


def retrieve_evidence(plan: AnalysisPlan) -> dict[str, Any]:
    """Index only the text visible to the specialist; return no source content."""
    normalized = _normalize_text(plan.source_window)
    numbers = _numbers(plan.source_window)
    return {
        "normalized_source": normalized,
        "numbers": numbers,
        "source_chars": len(plan.source_window),
        "source_lines": len(plan.source_window.splitlines()),
    }


def investigate_root_causes(candidate: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Keep model-suggested causes as hypotheses, never established facts."""
    findings = candidate.get("rankedFindings") if isinstance(candidate, dict) else None
    if not isinstance(findings, list):
        return []
    return [
        {
            "findingRank": finding.get("rank"),
            "hypothesis": str(finding.get("rootCauseHypothesis") or "").strip(),
            "followUpQuestion": str(finding.get("followUpQuestion") or "").strip(),
            "status": (
                "hypothesis"
                if str(finding.get("rootCauseHypothesis") or "").strip()
                else "unresolved"
            ),
            "reviewRequired": True,
        }
        for finding in findings
        if isinstance(finding, dict)
    ]


def resolve_recommendations(
    candidate: dict[str, Any] | None,
    root_causes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind each proposed action to exactly one ranked finding and cause state."""
    findings = candidate.get("rankedFindings") if isinstance(candidate, dict) else None
    if not isinstance(findings, list):
        return []
    causes_by_rank = {cause["findingRank"]: cause for cause in root_causes}
    return [
        {
            "findingRank": finding.get("rank"),
            "action": str(finding.get("recommendedAction") or "").strip(),
            "rootCauseStatus": causes_by_rank.get(finding.get("rank"), {}).get(
                "status", "unresolved"
            ),
            "reviewRequired": True,
        }
        for finding in findings
        if isinstance(finding, dict)
    ]


def verify_candidate(
    plan: AnalysisPlan,
    candidate: dict[str, Any] | None,
    evidence_index: dict[str, Any],
    root_causes: list[dict[str, Any]] | None = None,
    recommendations: list[dict[str, Any]] | None = None,
    additional_outputs: list[Any] | None = None,
) -> dict[str, Any]:
    """Fail closed unless every finding has a source-grounded evidence value.

    This checks provenance of cited values, not clinical correctness or the
    validity of model-inferred calculations. A consultant still reviews output.
    """
    findings = candidate.get("rankedFindings") if isinstance(candidate, dict) else None
    if root_causes is None:
        root_causes = investigate_root_causes(candidate)
    if recommendations is None:
        recommendations = resolve_recommendations(candidate, root_causes)
    checks = {
        "tool_matches_plan": isinstance(candidate, dict)
        and candidate.get("toolType") == plan.tool_type,
        "has_findings": isinstance(findings, list) and bool(findings),
        "all_findings_grounded": False,
        "has_operational_implications": False,
        "has_recommendations": False,
        "confidence_acceptable": False,
        "root_causes_bound": False,
        "root_causes_qualified": False,
        "recommendations_bound": False,
        "causal_numbers_grounded": False,
        "recommendation_numbers_grounded": False,
        "all_output_numbers_grounded": False,
        "no_direct_identifiers": False,
    }
    checked = 0
    if checks["has_findings"]:
        grounded = []
        implications = []
        action_presence = []
        confidence = []
        for finding in findings:
            citations = finding.get("evidence") if isinstance(finding, dict) else None
            matched = isinstance(citations, list) and bool(citations) and all(
                _evidence_matches_source(item, evidence_index)
                for item in citations
            )
            grounded.append(bool(matched))
            implications.append(
                bool(str(finding.get("operationalImplication") or "").strip())
                if isinstance(finding, dict) else False
            )
            action_presence.append(
                bool(str(finding.get("recommendedAction") or "").strip())
                if isinstance(finding, dict) else False
            )
            confidence.append(
                finding.get("confidence") in {"medium", "high"}
                if isinstance(finding, dict) else False
            )
            checked += 1
        checks["all_findings_grounded"] = all(grounded)
        checks["has_operational_implications"] = all(implications)
        checks["has_recommendations"] = all(action_presence)
        checks["confidence_acceptable"] = all(confidence)
        finding_ranks = [
            finding.get("rank") if isinstance(finding, dict) else None
            for finding in findings
        ]
        checks["root_causes_bound"] = (
            len(root_causes) == len(findings)
            and all(isinstance(cause, dict) for cause in root_causes)
            and [cause.get("findingRank") for cause in root_causes] == finding_ranks
            and all(isinstance(rank, int) and rank > 0 for rank in finding_ranks)
            and len(set(finding_ranks)) == len(finding_ranks)
        )
        checks["root_causes_qualified"] = all(
            isinstance(cause, dict)
            and bool(cause.get("hypothesis") or cause.get("followUpQuestion"))
            and cause.get("status") == (
                "hypothesis" if cause.get("hypothesis") else "unresolved"
            )
            and cause.get("reviewRequired") is True
            for cause in root_causes
        )
        checks["recommendations_bound"] = (
            len(recommendations) == len(findings)
            and all(
                isinstance(finding, dict)
                and isinstance(cause, dict)
                and isinstance(recommendation, dict)
                and recommendation.get("findingRank") == finding.get("rank")
                and recommendation.get("action") == finding.get("recommendedAction")
                and recommendation.get("rootCauseStatus") == cause.get("status")
                and recommendation.get("reviewRequired") is True
                for finding, cause, recommendation in zip(
                    findings, root_causes, recommendations
                )
            )
        )
        checks["causal_numbers_grounded"] = all(
            isinstance(cause, dict) and _numbers(str(cause.get("hypothesis") or "")).issubset(
                evidence_index["numbers"]
            )
            for cause in root_causes
        )
        checks["recommendation_numbers_grounded"] = all(
            isinstance(recommendation, dict) and _numbers(str(recommendation.get("action") or "")).issubset(
                evidence_index["numbers"]
            )
            for recommendation in recommendations
        )

    visible_outputs = [candidate, *(additional_outputs or [])]
    visible_text = list(_all_strings(visible_outputs))
    checks["all_output_numbers_grounded"] = all(
        _numbers(value).issubset(evidence_index["numbers"])
        for value in visible_text
    )
    checks["no_direct_identifiers"] = all(
        not DIRECT_IDENTIFIER_PATTERN.search(value)
        and not PATIENT_NAME_PATTERN.search(value)
        for value in visible_text
    )

    return {
        "approved": all(checks.values()),
        "checks": checks,
        "findings_checked": checked,
        "source_chars": evidence_index["source_chars"],
        "source_lines": evidence_index["source_lines"],
        "stages": [
            "supervisor",
            "data_retriever",
            "specialist",
            "root_cause_investigator",
            "recommendation_agent",
            "verifier",
        ],
    }


def _evidence_matches_source(item: Any, evidence_index: dict[str, Any]) -> bool:
    if not isinstance(item, dict):
        return False
    value = str(item.get("value") or "").strip()
    if not value or len(value) > 180:
        return False

    cited_numbers = _numbers(value)
    normalized = _normalize_text(value)
    if not cited_numbers and len(normalized) < 6:
        return False
    if normalized in evidence_index["normalized_source"]:
        return True
    # A number can be reformatted, but a number plus invented prose is not evidence.
    return bool(
        cited_numbers
        and NUMBER_PATTERN.fullmatch(value)
        and cited_numbers.issubset(evidence_index["numbers"])
    )


def _all_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _all_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_strings(item)


def numeric_claims_supported(submitted: Any, verified_content: Any) -> bool:
    """Keep edited report figures within the already verified analysis text."""
    approved_numbers = set().union(*(_numbers(text) for text in _all_strings(verified_content)))
    return all(
        _numbers(text).issubset(approved_numbers)
        for text in _all_strings(submitted)
    )


def _normalize_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _numbers(value: str) -> set[tuple[Decimal, bool]]:
    found: set[tuple[Decimal, bool]] = set()
    for match in NUMBER_PATTERN.finditer(value):
        raw = match.group().replace("$", "").replace(",", "")
        is_percent = raw.endswith("%")
        try:
            found.add((Decimal(raw.rstrip("%")), is_percent))
        except InvalidOperation:
            continue
    return found
