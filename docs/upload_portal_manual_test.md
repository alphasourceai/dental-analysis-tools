# Client Upload Portal Manual Test Checklist

## Admin: Create Upload Request
- Confirm `PORTAL_BASE_URL`, `PORTAL_SIGNER_SERVICE_URL`, `PORTAL_SIGNER_API_KEY`, `GCS_BUCKET_NAME`,
  `SENDGRID_API_KEY`, and `FROM_EMAIL` are set.
- Log into the admin dashboard and open **Secure Uploads**.
- Submit a client email and confirm a magic link is delivered.
- Verify a row exists in `upload_portal_requests` with `used_at` null and future `expires_at`.

## Client: Magic Link Validation
- Click the magic link from the email.
- Confirm the portal loads at `/uploads` and shows “Link verified.”
- Verify `upload_portal_requests.used_at` is set and `upload_portal_sessions` contains a new row.
- Attempt to reuse the same link and confirm the portal shows a “link already used” error.
- Adjust `PORTAL_TOKEN_TTL_MINUTES` and confirm expired links are rejected.

## Client: Upload Flow
- Upload a single PDF/CSV/Excel file and confirm progress and success states.
- Upload multiple files and confirm each progress bar updates and finishes.
- Confirm objects land in `gs://<GCS_BUCKET_NAME>/upload-portal/{request_id}/{yyyy-mm-dd}/{random}_{filename}`.
- Verify `upload_portal_files` records are created with `completed_at` set.

## Admin: Review Uploads
- Return to **Secure Uploads** and confirm recent uploads appear.
- Spot check `byte_size` and `content_type` values on recent rows.

## Failure Handling
- Trigger a validation failure and confirm an error state.
- Use an email not present in `users` and confirm `unknown_user_email` is returned on completion.
- Trigger an upload failure (cancel or disconnect) and confirm the UI surfaces an error.
- Review logs for structured events: `portal_token_invalid`, `portal_token_expired`, `portal_session_expired`,
  `portal_signer_failed`, `portal_file_record_failed`, `portal_complete_failed`.

## Security Checks
- Ensure magic link tokens are not stored in plaintext (hashes only).
- Confirm no raw emails or filenames appear in logs.
- Confirm GCS upload URLs are signed PUT URLs and expire.
