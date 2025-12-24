# Render Upload Portal Setup

This repo runs a Streamlit app. The upload portal is integrated into the same Streamlit web service
and is available at `/uploads` with API endpoints under `/api/upload-portal/*`.

## Required Render Env Vars
- `PORTAL_BASE_URL`
- `PORTAL_ALLOWED_ORIGINS`
- `PORTAL_TOKEN_TTL_MINUTES`
- `PORTAL_SESSION_TTL_MINUTES`
- `PORTAL_SIGNER_SERVICE_URL`
- `PORTAL_SIGNER_API_KEY`
- `GCS_BUCKET_NAME`
- `SENDGRID_API_KEY`
- `FROM_EMAIL`
- `PORTAL_MAX_FILE_SIZE_MB`
- `PORTAL_ALLOWED_CONTENT_TYPES`
- `PORTAL_RATE_LIMIT_WINDOW_SECONDS`
- `PORTAL_RATE_LIMIT_MAX`

Default origins should include `https://alphasourceai.com` and `https://www.alphasourceai.com`
(and `https://upload.alphasourceai.com` if the portal is hosted on that subdomain).

Default content types (when `PORTAL_ALLOWED_CONTENT_TYPES` is unset): PDF, CSV, XLS, XLSX.

## Render Start Command
No change required. Keep the existing Streamlit start command:
```
streamlit run app.py --server.port=${PORT:-8080} --server.address=0.0.0.0
```

## Local Test
1) Export env vars (at minimum: `PORTAL_BASE_URL`, `PORTAL_SIGNER_SERVICE_URL`, `PORTAL_SIGNER_API_KEY`,
   `GCS_BUCKET_NAME`, `SENDGRID_API_KEY`, `FROM_EMAIL`).
2) Run the app: `streamlit run app.py`.
3) Visit `http://localhost:8080/uploads`.
4) Run the smoke test: `python3 scripts/upload_portal_smoke_test.py`.

## Production Test
1) Visit `https://upload.alphasourceai.com/uploads` (or the Render URL).
2) Hit `https://upload.alphasourceai.com/api/upload-portal/health`.
3) Verify the request flow end-to-end (see `docs/upload_portal_manual_test.md`).

<!-- What changed: Integrated upload portal routes into the Streamlit server, added setup guidance and smoke test. -->
