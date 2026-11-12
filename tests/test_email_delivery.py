import base64
import os
import unittest
from unittest.mock import Mock, patch

import requests

from email_delivery import EmailDeliveryError, attachment, email_configured, send_transactional_email


class EmailDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "RESEND_API_KEY": "test-key", "FROM_EMAIL": "hello@example.com",
            "FROM_NAME": "alphaSource Consulting",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def send(self, **kwargs):
        return send_transactional_email("client@example.com", "Agreement", plain_text="secure-link-secret", **kwargs)

    @patch("email_delivery.requests.post")
    def test_envelope_text_html_and_attachments(self, post):
        post.return_value = Mock(status_code=200, json=Mock(return_value={"id": "message-1"}))
        files = [attachment(b"original file", "report.pdf", "application/pdf"),
                 attachment(b"logo", "logo.png", "image/png", content_id="brand-logo")]
        result = self.send(html_content='<img src="cid:brand-logo">', attachments=files)
        self.assertEqual(result["messageId"], "message-1")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["from"], "alphaSource Consulting <hello@example.com>")
        self.assertEqual(payload["to"], ["client@example.com"])
        self.assertEqual(payload["text"], "secure-link-secret")
        self.assertEqual(base64.b64decode(payload["attachments"][0]["content"]), b"original file")
        self.assertEqual(payload["attachments"][1]["content_id"], "brand-logo")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(post.call_args.kwargs["timeout"], (5, 20))

    @patch("email_delivery.requests.post")
    def test_missing_config_never_contacts_provider(self, post):
        os.environ.pop("RESEND_API_KEY")
        self.assertFalse(email_configured())
        with self.assertRaisesRegex(EmailDeliveryError, "email_not_configured"):
            self.send()
        post.assert_not_called()

    @patch("email_delivery.requests.post")
    def test_failures_are_sanitized_and_not_retried(self, post):
        for status in [400, 401, 403, 429, 500]:
            with self.subTest(status=status):
                post.reset_mock()
                post.return_value = Mock(status_code=status, text="secure-link-secret")
                with self.assertLogs("email_delivery", level="WARNING") as logs:
                    with self.assertRaises(EmailDeliveryError) as error:
                        self.send()
                self.assertNotIn("secure-link-secret", str(error.exception) + str(logs.output))
                self.assertEqual(error.exception.status_code, status)
                post.assert_called_once()

    @patch("email_delivery.requests.post", side_effect=requests.Timeout("secure-link-secret"))
    def test_timeout_is_unknown_and_sanitized(self, post):
        with self.assertRaisesRegex(EmailDeliveryError, "email_delivery_unknown"):
            self.send()
        post.assert_called_once()

    @patch("email_delivery.requests.post")
    def test_success_requires_provider_id(self, post):
        for body in [{}, {"id": ""}, []]:
            post.return_value = Mock(status_code=200, json=Mock(return_value=body))
            with self.assertRaisesRegex(EmailDeliveryError, "email_delivery_unknown"):
                self.send()

    @patch("email_delivery.requests.post")
    def test_oversize_required_attachments_fail_before_send(self, post):
        with patch("email_delivery.MAX_MESSAGE_BYTES", 200):
            with self.assertRaisesRegex(EmailDeliveryError, "email_too_large"):
                self.send(attachments=[attachment(b"x" * 500, "report.pdf", "application/pdf")])
        post.assert_not_called()

    @patch("email_delivery.requests.post")
    def test_address_injection_fails_before_send(self, post):
        with self.assertRaisesRegex(EmailDeliveryError, "email_address_invalid"):
            send_transactional_email("client@example.com\r\nBcc: other@example.com", "Subject", plain_text="Body")
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
