# Consulting transactional email

All application mail uses `email_delivery.py` and Resend's HTTPS `/emails` API.
Required secrets: `RESEND_API_KEY` and `FROM_EMAIL=hello@alphasourceconsulting.com`.
Optional: `FROM_NAME=alphaSource Consulting` (also the default).
Never put the API key in frontend Vite variables or source control.

## Covered paths

- Analyzer follow-up: existing branded HTML/plain text and inline logo.
- Analyzer team notice: original upload and unified analysis attachments retained;
  recipient remains `analyzer@alphasourceconsulting.com`. Sender now uses FROM_EMAIL
  instead of the former hardcoded `info@alphasourceai.com` address.
- Secure-upload request: existing local branded HTML/plain text, expiry and magic link.
  `UPLOAD_PORTAL_SENDGRID_TEMPLATE_ID` is obsolete; no remote SendGrid template is used.
- Agreement client request, BA countersign request, client/company final-copy notices.
  Existing lifecycle and fail-before-commit handling are retained.

API acceptance requires a provider message ID. This is not inbox delivery proof.
No automatic HTTP retry or SendGrid fallback occurs. Timeout/429/5xx/malformed success
is an unknown outcome requiring operator review before a fresh business attempt.
Errors contain only a fixed code and HTTP status; never log payloads, capability URLs,
provider response bodies, signature images, attachments or credentials.
Messages larger than 40 MB including encoded attachments fail before sending;
required attachments are never dropped. Disable click/open tracking in Resend domain
settings to preserve sensitive agreement/upload links.

## Hosted cutover

1. Verify the domain in the separate Consulting Resend team using the exact DNS
   records supplied by that account. Keep Google MX and existing authentication.
2. Add a sending-only key scoped to the Consulting domain to both Render services:
   `alphasource-consulting-admin-api` and `Operations Analyzer`. Set FROM_EMAIL and
   FROM_NAME consistently. The legacy `dental-analysis-tools` service is suspended;
   configure Resend before ever resuming it.
3. Run unit tests, compile checks and independent release review, then deploy the
   same reviewed commit to both active API services. No frontend deployment required.
4. Supabase project `xwcouvdwkqkspjzdvfxw` (Analyzer Tool): switch custom SMTP to
   `smtp.resend.com`, port `465`, username `resend`, password = a dedicated scoped
   Resend key; sender `hello@alphasourceconsulting.com`, name alphaSource Consulting.
   Keep redirect allowlists, password rules and email templates unchanged.
5. Verify authorized controlled mail in Resend and the destination mailbox for
   analyzer attachments, secure upload, agreement/client/BA/final copy, invite/reset.
   Provider acceptance and a healthy API alone are insufficient for this step.
6. Remove obsolete SendGrid credentials from Consulting services only after that
   proof. Keep historical SendGrid DNS/tracked links until an account-wide retirement
   audit confirms no remaining consumers; other products are migrating separately.

The Free daily/monthly allowance is shared by application API mail and Supabase SMTP.
Review actual account limits and usage before cutover; bursts of multi-message
agreement/analyzer workflows count separately. Do not automatically upgrade the plan.

## Local checks (no messages sent)

```sh
python -m unittest discover -s tests -p 'test_email*.py' -v
python -m py_compile email_delivery.py analysis_utils.py consulting_agreements.py upload_portal.py admin_api.py
git diff --check
```
