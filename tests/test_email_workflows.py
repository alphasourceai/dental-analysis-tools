"""Exercise the real mail functions without booting database/AI clients."""

import ast
import html
import os
import textwrap
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, mock_open, patch
from urllib.parse import quote

from email_delivery import EmailDeliveryError, attachment

ROOT = Path(__file__).resolve().parents[1]


def mail_functions(filename, names, namespace):
    tree = ast.parse((ROOT / filename).read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == len(names)
    exec(compile(ast.Module(body=functions, type_ignores=[]), filename, "exec"), namespace)
    return namespace


class MailWorkflowTests(unittest.TestCase):
    def test_agreement_requests_and_final_copy_use_same_transport(self):
        send = Mock(return_value={"statusCode": 200, "messageId": "test"})
        names = ["agreement_email_configured", "_send_email", "send_agreement_signature_request_email",
                 "send_agreement_ba_countersign_request_email", "send_agreement_signed_copy_email"]
        ns = mail_functions("consulting_agreements.py", names, {
            "Any": object, "datetime": datetime, "timezone": timezone,
            "email_configured": lambda: True, "send_transactional_email": send,
            "_escape": html.escape, "_email_shell": lambda title, body: body,
            "_email_cta": lambda url, label: url + label,
        })
        for name in names[2:]:
            kwargs = {"client_legal_name": "Example Dental", "signed_at" if "signed_copy" in name else "expires_at": datetime.now(timezone.utc)}
            ns[name]("recipient@example.com", "https://example.com/private-capability", **kwargs)
            self.assertEqual(send.call_args.args[0], "recipient@example.com")
            self.assertIn("private-capability", send.call_args.kwargs["plain_text"])
            self.assertIn("private-capability", send.call_args.kwargs["html_content"])
        self.assertEqual(send.call_count, 3)

    def test_portal_propagates_delivery_failure(self):
        class PortalError(Exception):
            def __init__(self, code, message, status):
                self.code = code
        send = Mock(side_effect=EmailDeliveryError("email_rejected", 403))
        ns = mail_functions("upload_portal.py", ["_send_magic_link_email"], {
            "datetime": datetime, "Optional": __import__("typing").Optional,
            "_get_env": lambda key: "hello@example.com", "email_configured": lambda: True,
            "_token_ttl_minutes": lambda: 30, "_portal_base_url": lambda: "https://example.com",
            "quote": quote, "send_transactional_email": send, "_log_event": Mock(), "PortalError": PortalError,
        })
        with self.assertRaises(PortalError):
            ns["_send_magic_link_email"]("recipient@example.com", "opaque-token", datetime.now(timezone.utc))
        send.assert_called_once()
        self.assertIn("upload_token=opaque-token", send.call_args.kwargs["html_content"])

    def test_analyzer_preserves_original_and_analysis_attachments(self):
        send = Mock()
        ns = mail_functions("analysis_utils.py", ["send_email"], {
            "textwrap": textwrap, "attachment": attachment, "send_transactional_email": send,
        })
        user = dict(first_name="Example", last_name="Client", office_name="Dental", email="client@example.com", org_type="practice")
        results = {"raw_analyses": {key: "Analysis" for key in ["OpenAI Analysis", "xAI Analysis", "AnthropicAI Analysis"]}}
        with patch("builtins.open", mock_open(read_data=b"Analysis attachment")):
            ns["send_email"](user, b"Original", "original.pdf", "application/pdf", results, "Analyzer")
        self.assertEqual(send.call_args.args[0], "analyzer@alphasourceconsulting.com")
        files = send.call_args.kwargs["attachments"]
        self.assertEqual([item["filename"] for item in files], ["unified_analysis.txt", "original.pdf"])
        self.assertEqual(__import__("base64").b64decode(files[1]["content"]), b"Original")


if __name__ == "__main__":
    unittest.main()
