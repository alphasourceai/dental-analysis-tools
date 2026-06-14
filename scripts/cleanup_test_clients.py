#!/usr/bin/env python3
"""Dry-run-first cleanup for alphaSource Consulting client/test data.

This utility removes client/business records that are not tied to the keep
email. It intentionally does not touch admin access/auth configuration,
platform configuration, migrations, or Stripe APIs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

from sqlalchemy import and_, func, inspect as sa_inspect, or_
from sqlalchemy.exc import SQLAlchemyError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from database import SessionLocal, engine  # noqa: E402
from models import (  # noqa: E402
    Admin,
    AdminAnalysisJob,
    AdminAnalysisJobFile,
    AdminAnalysisPhiAcknowledgment,
    AdminAuditEvent,
    AdminUser,
    BillingOverride,
    ClientSubmission,
    ConsultingAgreement,
    StripeCheckoutSession,
    StripeCheckoutSessionUpload,
    StripeCustomer,
    StripeEvent,
    StripePayment,
    StripeSubscription,
    Upload,
    UploadFile,
    UploadPortalFile,
    UploadPortalRequest,
    UploadPortalSession,
    User,
)

KEEP_EMAIL_DEFAULT = "jason@gardner.ltd"
SUPABASE_UPLOADS_BUCKET = "consulting-uploads"
AGREEMENTS_BUCKET = os.getenv("SUPABASE_CONSULTING_AGREEMENTS_BUCKET", "consulting-agreements")
DELETE_CHUNK_SIZE = 500
STORAGE_CHUNK_SIZE = 100


@dataclass(frozen=True)
class StorageObject:
    bucket: str
    path: str
    source: str


@dataclass(frozen=True)
class ExternalStorageObject:
    provider: str
    bucket: str
    path: str
    source: str


@dataclass
class CleanupPlan:
    ids_by_table: dict[str, set[Any]] = field(default_factory=lambda: defaultdict(set))
    skipped_tables: dict[str, str] = field(default_factory=dict)
    ambiguous: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    stripe_event_stats: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    supabase_storage: set[StorageObject] = field(default_factory=set)
    external_storage: set[ExternalStorageObject] = field(default_factory=set)

    def add_ids(self, model: Any, ids: Iterable[Any]) -> set[Any]:
        table_name = model.__tablename__
        normalized = {row_id for row_id in ids if row_id is not None}
        self.ids_by_table[table_name].update(normalized)
        return normalized

    def ids(self, model: Any) -> set[Any]:
        return self.ids_by_table.get(model.__tablename__, set())


class Schema:
    def __init__(self) -> None:
        self.inspector = sa_inspect(engine)
        self._columns: dict[str, set[str]] = {}

    def has_table(self, model: Any) -> bool:
        return self.inspector.has_table(model.__tablename__)

    def columns(self, model: Any) -> set[str]:
        table_name = model.__tablename__
        if table_name not in self._columns:
            if not self.has_table(model):
                self._columns[table_name] = set()
            else:
                self._columns[table_name] = {
                    column["name"] for column in self.inspector.get_columns(table_name)
                }
        return self._columns[table_name]

    def has_columns(self, model: Any, *names: str) -> bool:
        columns = self.columns(model)
        return all(name in columns for name in names)


def normalize_email(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def non_keep_email_filter(column: Any, keep_email: str) -> Any:
    return and_(column.isnot(None), func.lower(column) != keep_email)


def keep_email_filter(column: Any, keep_email: str) -> Any:
    return and_(column.isnot(None), func.lower(column) == keep_email)


def chunked(values: Iterable[Any], size: int = DELETE_CHUNK_SIZE) -> Iterable[list[Any]]:
    chunk: list[Any] = []
    for value in values:
        chunk.append(value)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def select_ids(db: Any, schema: Schema, plan: CleanupPlan, model: Any, where_clause: Any) -> set[Any]:
    if where_clause is None:
        return set()
    if not schema.has_table(model):
        plan.skipped_tables[model.__tablename__] = "missing table"
        return set()
    if not schema.has_columns(model, "id"):
        plan.skipped_tables[model.__tablename__] = "missing id column"
        return set()
    rows = db.query(model.id).filter(where_clause).all()
    return plan.add_ids(model, [row[0] for row in rows])


def select_column_set(db: Any, schema: Schema, model: Any, column_name: str, where_clause: Any) -> set[Any]:
    if where_clause is None:
        return set()
    if not schema.has_table(model) or not schema.has_columns(model, column_name):
        return set()
    column = getattr(model, column_name)
    return {row[0] for row in db.query(column).filter(where_clause).all() if row[0] is not None}


def count_ambiguous(db: Any, schema: Schema, model: Any, where_clause: Any) -> int:
    if not schema.has_table(model):
        return 0
    try:
        return int(db.query(func.count()).select_from(model).filter(where_clause).scalar() or 0)
    except SQLAlchemyError:
        return 0


def _uuid_text_set(values: Iterable[Any]) -> set[str]:
    return {str(value) for value in values if value is not None}


def _id_in(column: Any, values: Iterable[Any]) -> Optional[Any]:
    values_list = list(values)
    if not values_list:
        return None
    return column.in_(values_list)


def _or_nonempty(*clauses: Optional[Any]) -> Optional[Any]:
    filtered = [clause for clause in clauses if clause is not None]
    if not filtered:
        return None
    return or_(*filtered)


def _not_or_all(model: Any, keep_clause: Optional[Any]) -> Any:
    if keep_clause is None:
        return model.id.isnot(None)
    return ~keep_clause


def _safe_select_text_rows(
    db: Any,
    schema: Schema,
    model: Any,
    column_names: tuple[str, ...],
    ids: set[Any],
) -> list[tuple[Any, ...]]:
    if not ids or not schema.has_table(model) or not schema.has_columns(model, "id", *column_names):
        return []
    columns = [getattr(model, name) for name in column_names]
    rows: list[tuple[Any, ...]] = []
    for batch in chunked(ids):
        rows.extend(db.query(model.id, *columns).filter(model.id.in_(batch)).all())
    return rows


def _report_path_from_pdf_url(pdf_url: Optional[str], bucket: str = SUPABASE_UPLOADS_BUCKET) -> str:
    value = (pdf_url or "").strip()
    if not value:
        return ""
    if value.startswith("reports/"):
        return value
    if value.startswith(f"{bucket}/"):
        return value[len(bucket) + 1 :]
    try:
        path = unquote(urlparse(value).path or "")
    except Exception:
        path = value
    markers = [
        f"/storage/v1/object/public/{bucket}/",
        f"/storage/v1/object/sign/{bucket}/",
        f"/storage/v1/object/{bucket}/",
        f"/{bucket}/",
    ]
    for marker in markers:
        if marker in path:
            return path.split(marker, 1)[1]
    return ""


def _event_payload_dict(payload: Optional[str]) -> dict[str, Any]:
    if not payload:
        return {}
    try:
        value = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _walk_json(value: Any) -> Iterable[tuple[Optional[str], Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            yield from _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield None, item


def _stripe_event_signals(payload: Optional[str]) -> tuple[set[str], dict[str, set[str]]]:
    data = _event_payload_dict(payload)
    emails: set[str] = set()
    stripe_ids: dict[str, set[str]] = {
        "customers": set(),
        "checkout_sessions": set(),
        "subscriptions": set(),
        "payment_intents": set(),
        "invoices": set(),
    }
    email_keys = {"email", "client_email", "customer_email", "receipt_email", "billing_email"}
    for key, value in _walk_json(data):
        if isinstance(value, str):
            text = value.strip()
            if key and (key.lower() in email_keys or key.lower().endswith("_email")):
                email = normalize_email(text)
                if email and "@" in email:
                    emails.add(email)
            if text.startswith("cus_"):
                stripe_ids["customers"].add(text)
            elif text.startswith("cs_"):
                stripe_ids["checkout_sessions"].add(text)
            elif text.startswith("sub_"):
                stripe_ids["subscriptions"].add(text)
            elif text.startswith("pi_"):
                stripe_ids["payment_intents"].add(text)
            elif text.startswith("in_"):
                stripe_ids["invoices"].add(text)
    return emails, stripe_ids


def _stripe_text_ids(
    db: Any,
    schema: Schema,
    model: Any,
    column_name: str,
    ids: set[Any],
) -> set[str]:
    return {
        str(value)
        for value in select_column_set(
            db,
            schema,
            model,
            column_name,
            getattr(model, "id").in_(list(ids)) if ids else None,
        )
        if value
    }


def _classify_stripe_events(
    db: Any,
    schema: Schema,
    plan: CleanupPlan,
    *,
    keep_email: str,
    include_ambiguous: bool,
    keep_customer_stripe_ids: set[str],
    keep_checkout_stripe_ids: set[str],
    keep_subscription_stripe_ids: set[str],
    keep_payment_intent_ids: set[str],
    keep_invoice_ids: set[str],
) -> None:
    if not schema.has_table(StripeEvent) or not schema.has_columns(StripeEvent, "id", "payload"):
        plan.skipped_tables[StripeEvent.__tablename__] = "missing table or required columns"
        return

    delete_customer_stripe_ids = _stripe_text_ids(db, schema, StripeCustomer, "stripe_customer_id", plan.ids(StripeCustomer))
    delete_checkout_stripe_ids = _stripe_text_ids(
        db,
        schema,
        StripeCheckoutSession,
        "stripe_checkout_session_id",
        plan.ids(StripeCheckoutSession),
    )
    delete_subscription_stripe_ids = _stripe_text_ids(
        db,
        schema,
        StripeSubscription,
        "stripe_subscription_id",
        plan.ids(StripeSubscription),
    )
    delete_payment_intent_ids = _stripe_text_ids(db, schema, StripePayment, "stripe_payment_intent_id", plan.ids(StripePayment))
    delete_invoice_ids = _stripe_text_ids(db, schema, StripePayment, "stripe_invoice_id", plan.ids(StripePayment))
    delete_invoice_ids.update(
        _stripe_text_ids(db, schema, StripeSubscription, "latest_invoice_id", plan.ids(StripeSubscription))
    )

    event_ids_to_delete: list[Any] = []
    rows = db.query(StripeEvent.id, StripeEvent.payload).all()
    for event_id, payload in rows:
        emails, stripe_ids = _stripe_event_signals(payload)
        related_keep = bool(
            stripe_ids["customers"] & keep_customer_stripe_ids
            or stripe_ids["checkout_sessions"] & keep_checkout_stripe_ids
            or stripe_ids["subscriptions"] & keep_subscription_stripe_ids
            or stripe_ids["payment_intents"] & keep_payment_intent_ids
            or stripe_ids["invoices"] & keep_invoice_ids
        )
        related_delete = bool(
            stripe_ids["customers"] & delete_customer_stripe_ids
            or stripe_ids["checkout_sessions"] & delete_checkout_stripe_ids
            or stripe_ids["subscriptions"] & delete_subscription_stripe_ids
            or stripe_ids["payment_intents"] & delete_payment_intent_ids
            or stripe_ids["invoices"] & delete_invoice_ids
        )
        if keep_email in emails or related_keep:
            plan.stripe_event_stats["stripe_events_preserved_keep_email"] += 1
            continue
        if any(email != keep_email for email in emails) or related_delete:
            event_ids_to_delete.append(event_id)
            plan.stripe_event_stats["stripe_events_planned_delete"] += 1
            continue
        if include_ambiguous:
            event_ids_to_delete.append(event_id)
            plan.stripe_event_stats["stripe_events_ambiguous_delete_if_flagged"] += 1
        else:
            plan.stripe_event_stats["stripe_events_ambiguous_preserved"] += 1
    plan.add_ids(StripeEvent, event_ids_to_delete)


def _collect_storage_paths(db: Any, schema: Schema, plan: CleanupPlan) -> None:
    upload_file_ids = plan.ids(UploadFile)
    for _, bucket, object_path in _safe_select_text_rows(
        db,
        schema,
        UploadFile,
        ("bucket", "object_path"),
        upload_file_ids,
    ):
        if bucket and object_path:
            plan.supabase_storage.add(StorageObject(str(bucket), str(object_path), "upload_files.object_path"))

    upload_ids = plan.ids(Upload)
    for _, pdf_url in _safe_select_text_rows(db, schema, Upload, ("pdf_url",), upload_ids):
        report_path = _report_path_from_pdf_url(pdf_url)
        if report_path:
            plan.supabase_storage.add(StorageObject(SUPABASE_UPLOADS_BUCKET, report_path, "uploads.pdf_url"))

    agreement_ids = plan.ids(ConsultingAgreement)
    agreement_columns = (
        "draft_pdf_path",
        "signed_pdf_path",
        "signature_image_path",
        "client_signature_image_path",
        "ba_signature_image_path",
    )
    for row in _safe_select_text_rows(db, schema, ConsultingAgreement, agreement_columns, agreement_ids):
        for source, path in zip(agreement_columns, row[1:]):
            if path:
                plan.supabase_storage.add(StorageObject(AGREEMENTS_BUCKET, str(path), f"consulting_agreements.{source}"))

    secure_file_ids = plan.ids(UploadPortalFile)
    for _, bucket, object_name in _safe_select_text_rows(
        db,
        schema,
        UploadPortalFile,
        ("gcs_bucket", "object_name"),
        secure_file_ids,
    ):
        if bucket and object_name:
            plan.external_storage.add(
                ExternalStorageObject("gcs", str(bucket), str(object_name), "upload_portal_files.object_name")
            )


def _admin_exclusions(db: Any, schema: Schema) -> tuple[set[Any], set[str]]:
    admin_user_ids = select_column_set(db, schema, AdminUser, "user_id", AdminUser.user_id.isnot(None))
    admin_emails: set[str] = set()
    if schema.has_table(AdminUser) and schema.has_columns(AdminUser, "email"):
        admin_emails.update(
            normalize_email(row[0])
            for row in db.query(AdminUser.email).filter(AdminUser.email.isnot(None)).all()
            if normalize_email(row[0])
        )
    if schema.has_table(Admin) and schema.has_columns(Admin, "email"):
        admin_emails.update(
            normalize_email(row[0])
            for row in db.query(Admin.email).filter(Admin.email.isnot(None)).all()
            if normalize_email(row[0])
        )
    return admin_user_ids, admin_emails


def build_cleanup_plan(
    db: Any,
    *,
    keep_email: str,
    include_audit: bool,
    include_ambiguous_stripe_events: bool,
) -> CleanupPlan:
    schema = Schema()
    plan = CleanupPlan()
    admin_user_ids, admin_emails = _admin_exclusions(db, schema)
    admin_emails.add(keep_email)

    if schema.has_table(User) and schema.has_columns(User, "id", "email"):
        user_filters = [
            non_keep_email_filter(User.email, keep_email),
            ~func.lower(User.email).in_(sorted(admin_emails)),
        ]
        if admin_user_ids:
            user_filters.append(~User.id.in_(list(admin_user_ids)))
        select_ids(db, schema, plan, User, and_(*user_filters))
    else:
        plan.skipped_tables[User.__tablename__] = "missing table or required columns"
    user_ids = plan.ids(User)
    keep_user_ids = select_column_set(db, schema, User, "id", keep_email_filter(User.email, keep_email))
    keep_submission_ids = select_column_set(
        db,
        schema,
        ClientSubmission,
        "id",
        keep_email_filter(ClientSubmission.user_email, keep_email),
    )
    keep_upload_ids = select_column_set(
        db,
        schema,
        Upload,
        "id",
        _or_nonempty(
            keep_email_filter(Upload.user_email, keep_email),
            _id_in(Upload.submission_id, keep_submission_ids),
        ),
    )

    submission_ids = select_ids(
        db,
        schema,
        plan,
        ClientSubmission,
        non_keep_email_filter(ClientSubmission.user_email, keep_email),
    )

    upload_filter = _or_nonempty(
        non_keep_email_filter(Upload.user_email, keep_email),
        _id_in(Upload.submission_id, submission_ids),
    )
    upload_ids = select_ids(db, schema, plan, Upload, upload_filter)

    upload_file_filter = _or_nonempty(
        non_keep_email_filter(UploadFile.user_email, keep_email),
        _id_in(UploadFile.upload_id, upload_ids),
    )
    upload_file_ids = select_ids(db, schema, plan, UploadFile, upload_file_filter)

    job_filter = _or_nonempty(
        non_keep_email_filter(AdminAnalysisJob.client_email, keep_email),
        _id_in(AdminAnalysisJob.submission_id, submission_ids),
    )
    job_ids = select_ids(db, schema, plan, AdminAnalysisJob, job_filter)

    job_file_filter = _or_nonempty(
        _id_in(AdminAnalysisJobFile.job_id, job_ids),
        _id_in(AdminAnalysisJobFile.upload_file_id, upload_file_ids),
        _id_in(AdminAnalysisJobFile.upload_id, upload_ids),
    )
    job_file_ids = select_ids(db, schema, plan, AdminAnalysisJobFile, job_file_filter)

    phi_filter = _or_nonempty(
        _id_in(AdminAnalysisPhiAcknowledgment.job_id, job_ids),
        _id_in(AdminAnalysisPhiAcknowledgment.job_file_id, job_file_ids),
    )
    select_ids(db, schema, plan, AdminAnalysisPhiAcknowledgment, phi_filter)

    agreement_filter = _or_nonempty(
        non_keep_email_filter(ConsultingAgreement.client_email, keep_email),
        _id_in(ConsultingAgreement.client_user_id, user_ids),
    )
    agreement_ids = select_ids(db, schema, plan, ConsultingAgreement, agreement_filter)

    keep_customer_filter = _or_nonempty(
        keep_email_filter(StripeCustomer.client_email, keep_email),
        _id_in(StripeCustomer.user_id, keep_user_ids),
    )
    keep_customer_ids = select_column_set(db, schema, StripeCustomer, "id", keep_customer_filter)
    keep_customer_stripe_ids = {
        str(value)
        for value in select_column_set(db, schema, StripeCustomer, "stripe_customer_id", keep_customer_filter)
        if value
    }

    keep_checkout_filter = _or_nonempty(
        keep_email_filter(StripeCheckoutSession.client_email, keep_email),
        _id_in(StripeCheckoutSession.user_id, keep_user_ids),
        _id_in(StripeCheckoutSession.client_submission_id, keep_submission_ids),
        _id_in(StripeCheckoutSession.upload_id, keep_upload_ids),
        StripeCheckoutSession.stripe_customer_id.in_(list(keep_customer_stripe_ids)) if keep_customer_stripe_ids else None,
    )
    keep_checkout_ids = select_column_set(db, schema, StripeCheckoutSession, "id", keep_checkout_filter)
    keep_checkout_stripe_ids = {
        str(value)
        for value in select_column_set(db, schema, StripeCheckoutSession, "stripe_checkout_session_id", keep_checkout_filter)
        if value
    }
    keep_checkout_customer_stripe_ids = {
        str(value)
        for value in select_column_set(db, schema, StripeCheckoutSession, "stripe_customer_id", keep_checkout_filter)
        if value
    }
    keep_customer_stripe_ids.update(keep_checkout_customer_stripe_ids)
    if keep_checkout_customer_stripe_ids:
        keep_customer_ids.update(
            select_column_set(
                db,
                schema,
                StripeCustomer,
                "id",
                StripeCustomer.stripe_customer_id.in_(list(keep_checkout_customer_stripe_ids)),
            )
        )

    keep_subscription_filter = _or_nonempty(
        keep_email_filter(StripeSubscription.client_email, keep_email),
        _id_in(StripeSubscription.user_id, keep_user_ids),
        _id_in(StripeSubscription.source_checkout_session_id, keep_checkout_ids),
        StripeSubscription.stripe_checkout_session_id.in_(list(keep_checkout_stripe_ids)) if keep_checkout_stripe_ids else None,
        StripeSubscription.stripe_customer_id.in_(list(keep_customer_stripe_ids)) if keep_customer_stripe_ids else None,
    )
    keep_subscription_ids = select_column_set(db, schema, StripeSubscription, "id", keep_subscription_filter)
    keep_subscription_stripe_ids = {
        str(value)
        for value in select_column_set(db, schema, StripeSubscription, "stripe_subscription_id", keep_subscription_filter)
        if value
    }
    keep_subscription_checkout_ids = select_column_set(
        db,
        schema,
        StripeSubscription,
        "source_checkout_session_id",
        keep_subscription_filter,
    )
    keep_checkout_ids.update(keep_subscription_checkout_ids)
    keep_subscription_checkout_stripe_ids = {
        str(value)
        for value in select_column_set(db, schema, StripeSubscription, "stripe_checkout_session_id", keep_subscription_filter)
        if value
    }
    keep_checkout_stripe_ids.update(keep_subscription_checkout_stripe_ids)
    if keep_subscription_checkout_stripe_ids:
        keep_checkout_ids.update(
            select_column_set(
                db,
                schema,
                StripeCheckoutSession,
                "id",
                StripeCheckoutSession.stripe_checkout_session_id.in_(list(keep_subscription_checkout_stripe_ids)),
            )
        )

    keep_payment_filter = _or_nonempty(
        keep_email_filter(StripePayment.client_email, keep_email),
        _id_in(StripePayment.upload_id, keep_upload_ids),
        StripePayment.stripe_checkout_session_id.in_(list(keep_checkout_stripe_ids)) if keep_checkout_stripe_ids else None,
    )
    keep_payment_ids = select_column_set(db, schema, StripePayment, "id", keep_payment_filter)
    keep_payment_intent_ids = {
        str(value)
        for value in select_column_set(db, schema, StripePayment, "stripe_payment_intent_id", keep_payment_filter)
        if value
    }
    keep_invoice_ids = {
        str(value)
        for value in select_column_set(db, schema, StripePayment, "stripe_invoice_id", keep_payment_filter)
        if value
    }
    keep_payment_checkout_stripe_ids = {
        str(value)
        for value in select_column_set(db, schema, StripePayment, "stripe_checkout_session_id", keep_payment_filter)
        if value
    }
    keep_checkout_stripe_ids.update(keep_payment_checkout_stripe_ids)
    if keep_payment_checkout_stripe_ids:
        keep_checkout_ids.update(
            select_column_set(
                db,
                schema,
                StripeCheckoutSession,
                "id",
                StripeCheckoutSession.stripe_checkout_session_id.in_(list(keep_payment_checkout_stripe_ids)),
            )
        )
    keep_checkout_customer_stripe_ids = {
        str(value)
        for value in select_column_set(
            db,
            schema,
            StripeCheckoutSession,
            "stripe_customer_id",
            StripeCheckoutSession.id.in_(list(keep_checkout_ids)) if keep_checkout_ids else None,
        )
        if value
    }
    keep_customer_stripe_ids.update(keep_checkout_customer_stripe_ids)
    if keep_checkout_customer_stripe_ids:
        keep_customer_ids.update(
            select_column_set(
                db,
                schema,
                StripeCustomer,
                "id",
                StripeCustomer.stripe_customer_id.in_(list(keep_checkout_customer_stripe_ids)),
            )
        )
    keep_subscription_filter = _or_nonempty(
        keep_subscription_filter,
        _id_in(StripeSubscription.source_checkout_session_id, keep_checkout_ids),
        StripeSubscription.stripe_checkout_session_id.in_(list(keep_checkout_stripe_ids)) if keep_checkout_stripe_ids else None,
        StripeSubscription.stripe_customer_id.in_(list(keep_customer_stripe_ids)) if keep_customer_stripe_ids else None,
    )
    keep_subscription_ids.update(select_column_set(db, schema, StripeSubscription, "id", keep_subscription_filter))
    keep_subscription_stripe_ids.update(
        {
            str(value)
            for value in select_column_set(db, schema, StripeSubscription, "stripe_subscription_id", keep_subscription_filter)
            if value
        }
    )
    keep_checkout_stripe_ids.update(
        {
            str(value)
            for value in select_column_set(
                db,
                schema,
                StripeCheckoutSession,
                "stripe_checkout_session_id",
                StripeCheckoutSession.id.in_(list(keep_checkout_ids)) if keep_checkout_ids else None,
            )
            if value
        }
    )

    # Preserve the full local Stripe graph for the keep email. Stripe records
    # can point at each other by local UUIDs and remote Stripe IDs, so converge
    # the keep sets before planning local mirror-row deletions.
    for _ in range(8):
        before = (
            len(keep_customer_ids),
            len(keep_customer_stripe_ids),
            len(keep_checkout_ids),
            len(keep_checkout_stripe_ids),
            len(keep_subscription_ids),
            len(keep_subscription_stripe_ids),
            len(keep_payment_ids),
            len(keep_payment_intent_ids),
            len(keep_invoice_ids),
        )

        keep_customer_filter = _or_nonempty(
            keep_email_filter(StripeCustomer.client_email, keep_email),
            _id_in(StripeCustomer.user_id, keep_user_ids),
            _id_in(StripeCustomer.id, keep_customer_ids),
            StripeCustomer.stripe_customer_id.in_(list(keep_customer_stripe_ids)) if keep_customer_stripe_ids else None,
        )
        keep_customer_ids.update(select_column_set(db, schema, StripeCustomer, "id", keep_customer_filter))
        keep_customer_stripe_ids.update(
            {
                str(value)
                for value in select_column_set(db, schema, StripeCustomer, "stripe_customer_id", keep_customer_filter)
                if value
            }
        )

        keep_checkout_filter = _or_nonempty(
            keep_email_filter(StripeCheckoutSession.client_email, keep_email),
            _id_in(StripeCheckoutSession.user_id, keep_user_ids),
            _id_in(StripeCheckoutSession.client_submission_id, keep_submission_ids),
            _id_in(StripeCheckoutSession.upload_id, keep_upload_ids),
            _id_in(StripeCheckoutSession.id, keep_checkout_ids),
            StripeCheckoutSession.stripe_checkout_session_id.in_(list(keep_checkout_stripe_ids))
            if keep_checkout_stripe_ids
            else None,
            StripeCheckoutSession.stripe_customer_id.in_(list(keep_customer_stripe_ids)) if keep_customer_stripe_ids else None,
        )
        keep_checkout_ids.update(select_column_set(db, schema, StripeCheckoutSession, "id", keep_checkout_filter))
        keep_checkout_stripe_ids.update(
            {
                str(value)
                for value in select_column_set(
                    db,
                    schema,
                    StripeCheckoutSession,
                    "stripe_checkout_session_id",
                    keep_checkout_filter,
                )
                if value
            }
        )
        keep_customer_stripe_ids.update(
            {
                str(value)
                for value in select_column_set(
                    db,
                    schema,
                    StripeCheckoutSession,
                    "stripe_customer_id",
                    keep_checkout_filter,
                )
                if value
            }
        )

        keep_subscription_filter = _or_nonempty(
            keep_email_filter(StripeSubscription.client_email, keep_email),
            _id_in(StripeSubscription.user_id, keep_user_ids),
            _id_in(StripeSubscription.id, keep_subscription_ids),
            _id_in(StripeSubscription.source_checkout_session_id, keep_checkout_ids),
            StripeSubscription.stripe_subscription_id.in_(list(keep_subscription_stripe_ids))
            if keep_subscription_stripe_ids
            else None,
            StripeSubscription.stripe_checkout_session_id.in_(list(keep_checkout_stripe_ids))
            if keep_checkout_stripe_ids
            else None,
            StripeSubscription.stripe_customer_id.in_(list(keep_customer_stripe_ids)) if keep_customer_stripe_ids else None,
        )
        keep_subscription_ids.update(select_column_set(db, schema, StripeSubscription, "id", keep_subscription_filter))
        keep_subscription_stripe_ids.update(
            {
                str(value)
                for value in select_column_set(db, schema, StripeSubscription, "stripe_subscription_id", keep_subscription_filter)
                if value
            }
        )
        keep_checkout_ids.update(
            select_column_set(db, schema, StripeSubscription, "source_checkout_session_id", keep_subscription_filter)
        )
        keep_checkout_stripe_ids.update(
            {
                str(value)
                for value in select_column_set(
                    db,
                    schema,
                    StripeSubscription,
                    "stripe_checkout_session_id",
                    keep_subscription_filter,
                )
                if value
            }
        )
        keep_customer_stripe_ids.update(
            {
                str(value)
                for value in select_column_set(db, schema, StripeSubscription, "stripe_customer_id", keep_subscription_filter)
                if value
            }
        )
        keep_invoice_ids.update(
            {
                str(value)
                for value in select_column_set(db, schema, StripeSubscription, "latest_invoice_id", keep_subscription_filter)
                if value
            }
        )

        keep_payment_filter = _or_nonempty(
            keep_email_filter(StripePayment.client_email, keep_email),
            _id_in(StripePayment.id, keep_payment_ids),
            _id_in(StripePayment.upload_id, keep_upload_ids),
            StripePayment.stripe_checkout_session_id.in_(list(keep_checkout_stripe_ids)) if keep_checkout_stripe_ids else None,
            StripePayment.stripe_payment_intent_id.in_(list(keep_payment_intent_ids)) if keep_payment_intent_ids else None,
            StripePayment.stripe_invoice_id.in_(list(keep_invoice_ids)) if keep_invoice_ids else None,
        )
        keep_payment_ids.update(select_column_set(db, schema, StripePayment, "id", keep_payment_filter))
        keep_payment_intent_ids.update(
            {
                str(value)
                for value in select_column_set(db, schema, StripePayment, "stripe_payment_intent_id", keep_payment_filter)
                if value
            }
        )
        keep_invoice_ids.update(
            {
                str(value)
                for value in select_column_set(db, schema, StripePayment, "stripe_invoice_id", keep_payment_filter)
                if value
            }
        )
        keep_checkout_stripe_ids.update(
            {
                str(value)
                for value in select_column_set(db, schema, StripePayment, "stripe_checkout_session_id", keep_payment_filter)
                if value
            }
        )

        after = (
            len(keep_customer_ids),
            len(keep_customer_stripe_ids),
            len(keep_checkout_ids),
            len(keep_checkout_stripe_ids),
            len(keep_subscription_ids),
            len(keep_subscription_stripe_ids),
            len(keep_payment_ids),
            len(keep_payment_intent_ids),
            len(keep_invoice_ids),
        )
        if after == before:
            break

    checkout_ids = select_ids(
        db,
        schema,
        plan,
        StripeCheckoutSession,
        _not_or_all(StripeCheckoutSession, _id_in(StripeCheckoutSession.id, keep_checkout_ids)),
    )

    checkout_upload_keep_filter = _or_nonempty(
        _id_in(StripeCheckoutSessionUpload.checkout_session_id, keep_checkout_ids),
        _id_in(StripeCheckoutSessionUpload.upload_id, keep_upload_ids),
    )
    select_ids(
        db,
        schema,
        plan,
        StripeCheckoutSessionUpload,
        _not_or_all(StripeCheckoutSessionUpload, checkout_upload_keep_filter),
    )
    select_ids(
        db,
        schema,
        plan,
        StripeSubscription,
        _not_or_all(StripeSubscription, _id_in(StripeSubscription.id, keep_subscription_ids)),
    )
    select_ids(
        db,
        schema,
        plan,
        StripePayment,
        _not_or_all(StripePayment, _id_in(StripePayment.id, keep_payment_ids)),
    )
    select_ids(
        db,
        schema,
        plan,
        StripeCustomer,
        _not_or_all(StripeCustomer, _id_in(StripeCustomer.id, keep_customer_ids)),
    )

    _classify_stripe_events(
        db,
        schema,
        plan,
        keep_email=keep_email,
        include_ambiguous=include_ambiguous_stripe_events,
        keep_customer_stripe_ids=keep_customer_stripe_ids,
        keep_checkout_stripe_ids=keep_checkout_stripe_ids,
        keep_subscription_stripe_ids=keep_subscription_stripe_ids,
        keep_payment_intent_ids=keep_payment_intent_ids,
        keep_invoice_ids=keep_invoice_ids,
    )
    checkout_stripe_ids = _stripe_text_ids(db, schema, StripeCheckoutSession, "stripe_checkout_session_id", checkout_ids)

    billing_override_targets = _known_target_filter(
        BillingOverride.target_type,
        BillingOverride.target_id,
        {
            "upload": upload_ids,
            "checkout_session": checkout_ids,
            "client": user_ids,
            "consulting_agreement": agreement_ids,
            "admin_analysis_job": job_ids,
        },
    )
    billing_override_filter = _or_nonempty(
        non_keep_email_filter(BillingOverride.client_email, keep_email),
        billing_override_targets,
    )
    select_ids(db, schema, plan, BillingOverride, billing_override_filter)

    portal_request_ids = select_ids(
        db,
        schema,
        plan,
        UploadPortalRequest,
        non_keep_email_filter(UploadPortalRequest.requester_email, keep_email),
    )
    portal_session_ids = select_ids(
        db,
        schema,
        plan,
        UploadPortalSession,
        _id_in(UploadPortalSession.request_id, portal_request_ids),
    )
    portal_file_filter = _or_nonempty(
        non_keep_email_filter(UploadPortalFile.user_email, keep_email),
        _id_in(UploadPortalFile.user_id, user_ids),
        _id_in(UploadPortalFile.request_id, portal_request_ids),
        _id_in(UploadPortalFile.session_id, portal_session_ids),
    )
    portal_file_ids = select_ids(db, schema, plan, UploadPortalFile, portal_file_filter)

    if include_audit:
        audit_target_filter = _known_target_filter(
            AdminAuditEvent.target_type,
            AdminAuditEvent.target_id,
            {
                "client": user_ids,
                "upload": upload_ids,
                "checkout_session": checkout_ids,
                "consulting_agreement": agreement_ids,
                "admin_analysis_job": job_ids,
                "secure_upload_file": portal_file_ids,
                "secure_upload_request": portal_request_ids,
            },
        )
        audit_filter = _or_nonempty(
            non_keep_email_filter(AdminAuditEvent.client_email, keep_email),
            and_(
                audit_target_filter,
                or_(AdminAuditEvent.client_email.is_(None), func.lower(AdminAuditEvent.client_email) != keep_email),
            )
            if audit_target_filter is not None
            else None,
        )
        select_ids(db, schema, plan, AdminAuditEvent, audit_filter)
    else:
        plan.skipped_tables[AdminAuditEvent.__tablename__] = "audit cleanup disabled"

    _collect_storage_paths(db, schema, plan)
    _collect_ambiguous_counts(db, schema, plan, keep_email, checkout_stripe_ids)
    return plan


def _known_target_filter(target_type_column: Any, target_id_column: Any, targets: dict[str, Iterable[Any]]) -> Optional[Any]:
    clauses = []
    for target_type, ids in targets.items():
        target_ids = sorted(_uuid_text_set(ids))
        if not target_ids:
            continue
        clauses.append(and_(target_type_column == target_type, target_id_column.in_(target_ids)))
    if not clauses:
        return None
    return or_(*clauses)


def _collect_ambiguous_counts(
    db: Any,
    schema: Schema,
    plan: CleanupPlan,
    keep_email: str,
    checkout_stripe_ids: set[Any],
) -> None:
    del checkout_stripe_ids
    plan.ambiguous["billing_overrides_without_client_email"] = count_ambiguous(
        db,
        schema,
        BillingOverride,
        BillingOverride.client_email.is_(None),
    )
    plan.ambiguous["audit_events_without_client_email"] = count_ambiguous(
        db,
        schema,
        AdminAuditEvent,
        AdminAuditEvent.client_email.is_(None),
    )
    plan.ambiguous["upload_portal_files_without_user_email"] = count_ambiguous(
        db,
        schema,
        UploadPortalFile,
        UploadPortalFile.user_email.is_(None),
    )
    plan.ambiguous = {key: value for key, value in plan.ambiguous.items() if value}


DELETE_ORDER = (
    AdminAnalysisPhiAcknowledgment,
    AdminAuditEvent,
    StripeCheckoutSessionUpload,
    StripeEvent,
    StripePayment,
    StripeSubscription,
    BillingOverride,
    UploadPortalFile,
    UploadPortalSession,
    UploadPortalRequest,
    AdminAnalysisJobFile,
    AdminAnalysisJob,
    UploadFile,
    StripeCheckoutSession,
    StripeCustomer,
    ConsultingAgreement,
    Upload,
    ClientSubmission,
    User,
)


INTENTIONALLY_EXCLUDED = (
    "admins",
    "admin_users",
    "migrations",
    "auth settings / Supabase Auth users",
    "platform configuration",
    "Stripe remote customers, sessions, subscriptions, and payments",
    "GCS secure-upload objects (reported only; no GCS deletion helper in this repo)",
)


def execute_db_deletions(db: Any, plan: CleanupPlan) -> dict[str, int]:
    deleted: dict[str, int] = {}
    for model in DELETE_ORDER:
        table_name = model.__tablename__
        ids = list(plan.ids(model))
        if not ids:
            deleted[table_name] = 0
            continue
        total = 0
        for batch in chunked(ids):
            total += db.query(model).filter(model.id.in_(batch)).delete(synchronize_session=False)
        deleted[table_name] = total
    return deleted


def delete_supabase_storage(objects: set[StorageObject]) -> dict[str, Any]:
    """Legacy inline storage deletion.

    Prefer scripts/cleanup_orphan_supabase_storage.py for Supabase Storage
    cleanup. Importing supabase-py can hang locally through storage3/pyiceberg.
    """
    if not objects:
        return {"deleted": 0, "failed": []}
    from supabase_utils import _get_supabase_admin_client

    client = _get_supabase_admin_client()
    if not client:
        return {
            "deleted": 0,
            "failed": [{"bucket": obj.bucket, "path": obj.path, "error": "supabase_client_unavailable"} for obj in objects],
        }

    deleted = 0
    failed: list[dict[str, str]] = []
    by_bucket: dict[str, list[StorageObject]] = defaultdict(list)
    for obj in sorted(objects, key=lambda item: (item.bucket, item.path)):
        by_bucket[obj.bucket].append(obj)

    for bucket, bucket_objects in by_bucket.items():
        for batch in chunked(bucket_objects, STORAGE_CHUNK_SIZE):
            paths = [obj.path for obj in batch]
            try:
                client.storage.from_(bucket).remove(paths)
                deleted += len(paths)
            except Exception as exc:
                for path in paths:
                    failed.append({"bucket": bucket, "path": path, "error": type(exc).__name__})
    return {"deleted": deleted, "failed": failed}


def print_plan(plan: CleanupPlan, *, keep_email: str, dry_run: bool, include_audit: bool, skip_storage: bool) -> None:
    print("alphaSource Consulting test client cleanup")
    print(f"Mode: {'DRY RUN' if dry_run else 'CONFIRMED DELETE'}")
    print(f"Keep email: {keep_email}")
    print(f"Audit cleanup: {'enabled' if include_audit else 'disabled'}")
    print(f"Storage cleanup: {'skipped' if skip_storage else 'planned'}")
    print("This script deletes local app Stripe records only. It does not delete or cancel remote Stripe objects.")
    print(
        "Supabase storage cleanup is skipped by default; use "
        "scripts/cleanup_orphan_supabase_storage.py to avoid local supabase-py import hangs."
    )
    print()
    print("Planned database deletions:")
    for model in DELETE_ORDER:
        table_name = model.__tablename__
        print(f"  {table_name}: {len(plan.ids(model))}")
    if plan.skipped_tables:
        print()
        print("Skipped tables:")
        for table_name, reason in sorted(plan.skipped_tables.items()):
            print(f"  {table_name}: {reason}")
    print()
    print("Stripe event ownership summary:")
    for key in (
        "stripe_events_preserved_keep_email",
        "stripe_events_planned_delete",
        "stripe_events_ambiguous_preserved",
        "stripe_events_ambiguous_delete_if_flagged",
    ):
        print(f"  {key}: {int(plan.stripe_event_stats.get(key, 0))}")
    print()
    print("Supabase storage objects tied to planned deletions:")
    if plan.supabase_storage:
        by_bucket: dict[str, int] = defaultdict(int)
        for obj in plan.supabase_storage:
            by_bucket[obj.bucket] += 1
        for bucket, count in sorted(by_bucket.items()):
            print(f"  {bucket}: {count}")
    else:
        print("  none")
    print()
    print("External storage objects reported only:")
    if plan.external_storage:
        by_provider_bucket: dict[tuple[str, str], int] = defaultdict(int)
        for obj in plan.external_storage:
            by_provider_bucket[(obj.provider, obj.bucket)] += 1
        for (provider, bucket), count in sorted(by_provider_bucket.items()):
            print(f"  {provider}:{bucket}: {count}")
    else:
        print("  none")
    if plan.ambiguous:
        print()
        print("Ambiguous records not deleted automatically:")
        for label, count in sorted(plan.ambiguous.items()):
            print(f"  {label}: {count}")
    print()
    print("Intentionally excluded:")
    for item in INTENTIONALLY_EXCLUDED:
        print(f"  {item}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dry-run-first cleanup for test client data.")
    parser.add_argument("--keep-email", default=KEEP_EMAIL_DEFAULT, help=f"Email to preserve. Default: {KEEP_EMAIL_DEFAULT}")
    parser.add_argument("--dry-run", action="store_true", help="Preview planned deletions. This is the default mode.")
    parser.add_argument("--confirm", action="store_true", help="Actually delete planned database rows and eligible storage objects.")
    audit_group = parser.add_mutually_exclusive_group()
    audit_group.add_argument("--include-audit", dest="include_audit", action="store_true", default=True)
    audit_group.add_argument("--exclude-audit", dest="include_audit", action="store_false")
    parser.add_argument(
        "--include-ambiguous-stripe-events",
        action="store_true",
        help="Also plan/delete stripe_events whose keep-email ownership cannot be determined.",
    )
    storage_group = parser.add_mutually_exclusive_group()
    storage_group.add_argument(
        "--skip-storage",
        dest="skip_storage",
        action="store_true",
        default=True,
        help="Do not delete Supabase storage objects; report only. This is the default.",
    )
    storage_group.add_argument(
        "--include-storage",
        dest="skip_storage",
        action="store_false",
        help=(
            "Run legacy inline Supabase storage deletion after DB cleanup. "
            "This imports supabase-py and may hang locally; prefer cleanup_orphan_supabase_storage.py."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.confirm and args.dry_run:
        print("Error: use either --dry-run or --confirm, not both.", file=sys.stderr)
        return 2

    keep_email = normalize_email(args.keep_email)
    if not keep_email or "@" not in keep_email:
        print("Error: --keep-email must be a valid email address.", file=sys.stderr)
        return 2

    dry_run = not args.confirm
    db = SessionLocal()
    try:
        plan = build_cleanup_plan(
            db,
            keep_email=keep_email,
            include_audit=bool(args.include_audit),
            include_ambiguous_stripe_events=bool(args.include_ambiguous_stripe_events),
        )
        print_plan(
            plan,
            keep_email=keep_email,
            dry_run=dry_run,
            include_audit=bool(args.include_audit),
            skip_storage=bool(args.skip_storage),
        )
        if dry_run:
            print("Dry run only. No rows or storage objects were deleted.")
            return 0

        deleted = execute_db_deletions(db, plan)
        db.commit()
        print()
        print("Database deletion committed:")
        for table_name in [model.__tablename__ for model in DELETE_ORDER]:
            print(f"  {table_name}: {deleted.get(table_name, 0)}")
    except Exception as exc:
        db.rollback()
        print(f"Cleanup failed before commit; rolled back database changes: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    if args.skip_storage:
        print()
        print("Storage deletion skipped. Planned storage objects were reported above.")
        print("Use scripts/cleanup_orphan_supabase_storage.py for orphaned Supabase Storage cleanup.")
        return 0

    print()
    print("Warning: inline Supabase storage deletion imports supabase-py and may hang locally.")
    print("Prefer scripts/cleanup_orphan_supabase_storage.py unless you intentionally need the legacy path.")
    storage_result = delete_supabase_storage(plan.supabase_storage)
    print()
    print("Supabase storage cleanup:")
    print(f"  deleted: {storage_result['deleted']}")
    if storage_result["failed"]:
        print(f"  failed: {len(storage_result['failed'])}")
        for failure in storage_result["failed"][:20]:
            print(f"    {failure['bucket']}/{failure['path']}: {failure['error']}")
        if len(storage_result["failed"]) > 20:
            print(f"    ... {len(storage_result['failed']) - 20} more failures")
    else:
        print("  failed: 0")
    if plan.external_storage:
        print()
        print("External storage was not deleted by this script:")
        for obj in sorted(plan.external_storage, key=lambda item: (item.provider, item.bucket, item.path))[:20]:
            print(f"  {obj.provider}:{obj.bucket}/{obj.path}")
        if len(plan.external_storage) > 20:
            print(f"  ... {len(plan.external_storage) - 20} more external objects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
