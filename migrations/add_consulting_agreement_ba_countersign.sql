-- Add BA countersign lifecycle fields for BAA/Privacy agreements.

alter table consulting_agreements
    add column if not exists ba_signer_token_hash text,
    add column if not exists ba_signer_token_expires_at timestamptz null,
    add column if not exists ba_opened_at timestamptz null,
    add column if not exists client_signed_at timestamptz null,
    add column if not exists client_signature_image_path text null,
    add column if not exists client_signature_sha256 text null,
    add column if not exists ba_signer_authority_confirmed boolean not null default false,
    add column if not exists ba_signer_accepted boolean not null default false,
    add column if not exists ba_signer_ip text null,
    add column if not exists ba_signer_user_agent text null;

create unique index if not exists consulting_agreements_ba_signer_token_hash_key
    on consulting_agreements (ba_signer_token_hash)
    where ba_signer_token_hash is not null;

alter table consulting_agreements
    drop constraint if exists consulting_agreements_status_check;

alter table consulting_agreements
    add constraint consulting_agreements_status_check
    check (status in ('draft', 'sent', 'pending_ba_signature', 'signed', 'voided', 'superseded', 'expired'));
