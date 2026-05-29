-- Add BAA/Privacy agreement signing foundation.
-- Agreement files must be stored in a private Supabase bucket named consulting-agreements.

create extension if not exists pgcrypto;

create table if not exists consulting_agreements (
    id uuid primary key default gen_random_uuid(),
    client_email text not null,
    client_user_id uuid null references users(id) on delete set null,
    client_legal_name text not null,
    office_name text null,
    org_type text null,
    phone text null,
    state text not null,
    effective_date date not null,
    document_type text not null default 'baa_privacy_agreement',
    status text not null default 'draft',
    is_current boolean not null default false,
    template_version text null,
    template_snapshot jsonb not null default '{}'::jsonb,
    source_template_path text null,
    source_template_sha256 text null,
    draft_pdf_path text null,
    signed_pdf_path text null,
    signer_token_hash text unique,
    signer_token_expires_at timestamptz null,
    sent_at timestamptz null,
    opened_at timestamptz null,
    signer_name text null,
    signer_email text not null,
    signer_title text null,
    signer_authority_confirmed boolean not null default false,
    signer_accepted boolean not null default false,
    signed_at timestamptz null,
    signer_ip text null,
    signer_user_agent text null,
    signature_image_path text null,
    signature_sha256 text null,
    ba_signer_name text null,
    ba_signer_title text null,
    ba_signer_email text null,
    ba_signature_mode text null,
    ba_signed_at timestamptz null,
    ba_signature_image_path text null,
    ba_signature_sha256 text null,
    created_by_admin_id text null,
    created_by_admin_email text null,
    sent_by_admin_id text null,
    sent_by_admin_email text null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    voided_at timestamptz null,
    voided_by_admin_id text null,
    voided_by_admin_email text null,
    void_reason text null,
    superseded_at timestamptz null,
    superseded_by_agreement_id uuid null references consulting_agreements(id) on delete set null,
    constraint consulting_agreements_document_type_check
        check (document_type in ('baa_privacy_agreement')),
    constraint consulting_agreements_status_check
        check (status in ('draft', 'sent', 'signed', 'voided', 'superseded', 'expired'))
);

create index if not exists consulting_agreements_client_email_lower_idx
    on consulting_agreements (lower(client_email));

create index if not exists consulting_agreements_status_idx
    on consulting_agreements (status);

create unique index if not exists consulting_agreements_signer_token_hash_key
    on consulting_agreements (signer_token_hash)
    where signer_token_hash is not null;

create index if not exists consulting_agreements_document_type_idx
    on consulting_agreements (document_type);

create index if not exists consulting_agreements_is_current_idx
    on consulting_agreements (is_current);

create index if not exists consulting_agreements_client_document_created_idx
    on consulting_agreements (lower(client_email), document_type, created_at desc);

create unique index if not exists consulting_agreements_one_current_per_client_document_idx
    on consulting_agreements (lower(client_email), document_type)
    where is_current = true;
