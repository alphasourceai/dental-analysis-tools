import unittest

from admin_analysis_agent_pipeline import (
    investigate_root_causes,
    numeric_claims_supported,
    resolve_recommendations,
    retrieve_evidence,
    supervise,
    verify_candidate,
)


class AgentPipelineTests(unittest.TestCase):
    def setUp(self):
        self.plan = supervise(
            "ar",
            "csv",
            "A/R over 90 days | $123,456\nPayer A | 42%\n",
        )
        self.index = retrieve_evidence(self.plan)

    def candidate(self, value):
        return {
            "toolType": "ar",
            "rankedFindings": [
                {
                    "rank": 1,
                    "title": "Older balances",
                    "confidence": "medium",
                    "operationalImplication": "Older balances slow collections.",
                    "rootCauseHypothesis": "Follow-up capacity may be constrained.",
                    "recommendedAction": "Review the aging queue.",
                    "evidence": [{"label": "A/R over 90 days", "value": value}],
                }
            ],
        }

    def test_grounded_numeric_value_is_approved(self):
        result = verify_candidate(self.plan, self.candidate("123456"), self.index)
        self.assertTrue(result["approved"])
        self.assertEqual(result["findings_checked"], 1)

    def test_unsupported_value_is_rejected(self):
        result = verify_candidate(self.plan, self.candidate("$999,999"), self.index)
        self.assertFalse(result["approved"])
        self.assertFalse(result["checks"]["all_findings_grounded"])

    def test_one_unsupported_citation_rejects_finding(self):
        candidate = self.candidate("$123,456")
        candidate["rankedFindings"][0]["evidence"].append(
            {"label": "Unverified", "value": "$999,999"}
        )
        self.assertFalse(verify_candidate(self.plan, candidate, self.index)["approved"])

    def test_number_with_invented_evidence_prose_is_rejected(self):
        candidate = self.candidate("$123,456 for patient Jane Doe")
        result = verify_candidate(self.plan, candidate, self.index)
        self.assertFalse(result["approved"])
        self.assertFalse(result["checks"]["all_findings_grounded"])

    def test_unsupported_client_facing_numbers_are_rejected(self):
        candidate = self.candidate("$123,456")
        candidate["executiveSummary"] = {"summary": "Lost $480,000 last year."}
        candidate["rankedFindings"][0]["financialValue"] = "$480,000"
        result = verify_candidate(self.plan, candidate, self.index)
        self.assertFalse(result["checks"]["all_output_numbers_grounded"])
        self.assertFalse(result["approved"])

    def test_unsupported_plain_text_opportunity_is_rejected(self):
        candidate = self.candidate("$123,456")
        result = verify_candidate(
            self.plan, candidate, self.index,
            additional_outputs=[[{"title": "Lost $480,000"}]],
        )
        self.assertFalse(result["checks"]["all_output_numbers_grounded"])

    def test_patient_identifiers_are_rejected_in_narrative(self):
        candidate = self.candidate("$123,456")
        candidate["rankedFindings"][0]["clientFacingSummary"] = "Patient Jane Doe needs a review."
        result = verify_candidate(self.plan, candidate, self.index)
        self.assertFalse(result["checks"]["no_direct_identifiers"])

    def test_missing_evidence_and_wrong_tool_are_rejected(self):
        candidate = self.candidate("")
        candidate["toolType"] = "claims"
        self.assertFalse(verify_candidate(self.plan, candidate, self.index)["approved"])

    def test_missing_recommendation_is_rejected(self):
        candidate = self.candidate("$123,456")
        candidate["rankedFindings"][0]["recommendedAction"] = ""
        self.assertFalse(verify_candidate(self.plan, candidate, self.index)["approved"])

    def test_unresolved_cause_requires_follow_up_question(self):
        candidate = self.candidate("$123,456")
        finding = candidate["rankedFindings"][0]
        finding["rootCauseHypothesis"] = ""
        finding["followUpQuestion"] = "Which claims have not been followed up?"
        causes = investigate_root_causes(candidate)
        self.assertEqual(causes[0]["status"], "unresolved")
        self.assertTrue(verify_candidate(self.plan, candidate, self.index)["approved"])
        finding["followUpQuestion"] = ""
        self.assertFalse(verify_candidate(self.plan, candidate, self.index)["approved"])

    def test_recommendation_must_match_its_finding(self):
        candidate = self.candidate("$123,456")
        causes = investigate_root_causes(candidate)
        recommendations = resolve_recommendations(candidate, causes)
        recommendations[0]["action"] = "Unrelated action"
        result = verify_candidate(
            self.plan, candidate, self.index, causes, recommendations
        )
        self.assertFalse(result["approved"])
        self.assertFalse(result["checks"]["recommendations_bound"])

    def test_hypothesis_and_action_cannot_introduce_new_metrics(self):
        candidate = self.candidate("$123,456")
        candidate["rankedFindings"][0]["rootCauseHypothesis"] = "A 90% denial rate may explain the balance."
        result = verify_candidate(self.plan, candidate, self.index)
        self.assertFalse(result["checks"]["causal_numbers_grounded"])
        candidate["rankedFindings"][0]["rootCauseHypothesis"] = "Follow-up capacity may be constrained."
        candidate["rankedFindings"][0]["recommendedAction"] = "Cut the balance by 50%."
        result = verify_candidate(self.plan, candidate, self.index)
        self.assertFalse(result["checks"]["recommendation_numbers_grounded"])

    def test_source_window_matches_provider_limit(self):
        plan = supervise("financial", "pdf", "a" * 7000)
        self.assertEqual(len(plan.source_window), 6000)

    def test_trace_does_not_include_source_or_evidence_value(self):
        result = verify_candidate(self.plan, self.candidate("$123,456"), self.index)
        self.assertNotIn("123,456", str(result))
        self.assertNotIn("Payer A", str(result))

    def test_edited_report_cannot_add_unverified_figure(self):
        verified = {"summary": "A/R over 90 days is $123,456."}
        self.assertTrue(numeric_claims_supported({"impact": "Review $123456."}, verified))
        self.assertFalse(numeric_claims_supported({"impact": "Lost $480,000."}, verified))


if __name__ == "__main__":
    unittest.main()
