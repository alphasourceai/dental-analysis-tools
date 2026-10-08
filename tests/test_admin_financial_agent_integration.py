import importlib
import json
import os
import sys
import types
import unittest
from unittest.mock import patch


sys.modules.setdefault(
    "supabase_utils",
    types.SimpleNamespace(_get_supabase_admin_client=lambda: None),
)
service = importlib.import_module("admin_financial_processing_service")


class AdminFinancialAgentIntegrationTests(unittest.TestCase):
    def provider_response(self, evidence_value="$123,456", *, confidence="medium"):
        structured = {
            "schemaVersion": "internal_analysis_v1",
            "toolType": "ar",
            "executiveSummary": {
                "summary": "Older balances need review.",
                "primaryConcern": "Aged balances",
                "recommendedFocus": "Review follow-up workflow",
            },
            "rankedFindings": [{
                "rank": 1,
                "title": "Older balances",
                "category": "collections",
                "severity": "medium",
                **({"confidence": confidence} if confidence is not None else {}),
                "evidence": [{"label": "A/R over 90 days", "value": evidence_value}],
                "operationalImplication": "Older balances slow collections.",
                "rootCauseHypothesis": "Follow-up capacity may be constrained.",
                "recommendedAction": "Review the aging queue.",
                "followUpQuestion": "Who owns follow-up?",
                "clientFacingSummary": "Review aged receivables.",
            }],
        }
        return (
            "ISSUE: Older balances\nIMPACT: Collections\n"
            "RECOMMENDATION: Review the queue\n---TRENDS---\n"
            f"{service.STRUCTURED_ANALYSIS_START}\n"
            f"{json.dumps(structured)}\n"
            f"{service.STRUCTURED_ANALYSIS_END}"
        )

    def run_analysis(self, mode, evidence_value="$123,456", *, confidence="medium"):
        with patch.dict(os.environ, {
            "ADMIN_ANALYSIS_PIPELINE_MODE": mode,
            "ADMIN_ANALYSIS_PROVIDER_MODE": "xai_only",
        }), patch.object(service, "_xai_analysis", return_value=self.provider_response(evidence_value, confidence=confidence)):
            return service.run_financial_csv_analysis(
                "A/R over 90 days | $123,456\nPayer A | 42%\n",
                tool_type="ar",
            )

    def test_agentic_result_contains_review_stages(self):
        result = self.run_analysis("agentic")
        self.assertTrue(result["agentic_verification"]["approved"])
        self.assertEqual(result["analysisPipelineMode"], "agentic")
        self.assertEqual(result["agentic_review"]["rootCauses"][0]["status"], "hypothesis")
        self.assertEqual(result["agentic_review"]["recommendations"][0]["findingRank"], 1)
        self.assertTrue(result["agentic_review"]["consultantReviewRequired"])

    def test_unverified_result_is_not_returned(self):
        with self.assertRaises(service.AdminFinancialProcessingError) as error:
            self.run_analysis("agentic", "$999,999")
        self.assertEqual(error.exception.code, "analysis_verification_failed")

    def test_missing_confidence_is_not_defaulted_to_medium(self):
        with self.assertRaises(service.AdminFinancialProcessingError) as error:
            self.run_analysis("agentic", confidence=None)
        self.assertEqual(error.exception.code, "analysis_verification_failed")

    def test_legacy_result_shape_remains_unchanged(self):
        result = self.run_analysis("legacy")
        self.assertNotIn("agentic_review", result)
        self.assertNotIn("agentic_verification", result)


if __name__ == "__main__":
    unittest.main()
