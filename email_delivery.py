"""Transactional mail transport. Never log message bodies or provider error bodies."""

import base64
import json
import logging
import os
import re
from email.utils import formataddr

import requests

logger = logging.getLogger(__name__)
RESEND_ENDPOINT = "https://api.resend.com/emails"
MAX_MESSAGE_BYTES = 40_000_000


class EmailDeliveryError(Exception):
    def __init__(self, code, status_code=None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def email_configured():
    return bool(os.getenv("RESEND_API_KEY", "").strip() and os.getenv("FROM_EMAIL", "").strip())


def attachment(content, filename, content_type, *, content_id=None):
    item = {
        "content": base64.b64encode(content).decode("ascii"),
        "filename": filename,
        "content_type": content_type,
    }
    if content_id:
        item["content_id"] = content_id
    return item


def _address(value):
    if not isinstance(value, str) or not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", value):
        raise EmailDeliveryError("email_address_invalid")
    return value


def send_transactional_email(to_email, subject, *, plain_text, html_content=None, attachments=None):
    if not email_configured():
        raise EmailDeliveryError("email_not_configured")
    sender = _address(os.environ["FROM_EMAIL"].strip())
    name = os.getenv("FROM_NAME", "alphaSource Consulting").strip() or "alphaSource Consulting"
    if any(char in name for char in "\r\n"):
        raise EmailDeliveryError("email_sender_invalid")
    payload = {
        "from": formataddr((name, sender)),
        "to": [_address(to_email)],
        "subject": subject,
        "text": plain_text,
    }
    if html_content:
        payload["html"] = html_content
    if attachments:
        payload["attachments"] = attachments
    # Required attachments must never be silently dropped to fit the provider limit.
    if len(json.dumps(payload).encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise EmailDeliveryError("email_too_large")
    try:
        response = requests.post(
            RESEND_ENDPOINT,
            headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY'].strip()}"},
            json=payload,
            timeout=(5, 20),
            allow_redirects=False,
        )
    except requests.RequestException:
        logger.warning("email delivery failed provider=resend code=email_delivery_unknown")
        raise EmailDeliveryError("email_delivery_unknown") from None
    if not 200 <= response.status_code < 300:
        code = "email_delivery_unknown" if response.status_code >= 500 or response.status_code == 429 else "email_rejected"
        logger.warning("email delivery failed provider=resend code=%s status=%s", code, response.status_code)
        raise EmailDeliveryError(code, response.status_code)
    try:
        message_id = response.json().get("id")
    except (ValueError, AttributeError):
        message_id = None
    if not isinstance(message_id, str) or not message_id.strip():
        raise EmailDeliveryError("email_delivery_unknown", response.status_code)
    # Accepted by Resend does not mean delivered to the recipient's inbox.
    return {"statusCode": response.status_code, "provider": "resend", "messageId": message_id}
