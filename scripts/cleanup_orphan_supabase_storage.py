#!/usr/bin/env python3
"""Dry-run-first orphan cleanup for alphaSource Consulting Supabase Storage.

This utility intentionally avoids supabase-py because local imports can hang in
storage3/pyiceberg. It uses direct Supabase Storage REST calls and only deletes
objects that are not referenced by live database rows.
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
from urllib import error, request
from urllib.parse import quote, unquote, urlparse

from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.exc import SQLAlchemyError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SUPPORTED_BUCKETS = ("consulting-agreements", "consulting-uploads")
UPLOADS_BUCKET = "consulting-uploads"
AGREEMENTS_BUCKET = "consulting-agreements"
LIST_LIMIT = 1000
DELETE_BATCH_SIZE = 1000
SAMPLE_LIMIT = 25

AGREEMENT_STORAGE_COLUMNS = (
    "source_template_path",
    "draft_pdf_path",
    "signed_pdf_path",
    "signature_image_path",
    "client_signature_image_path",
    "ba_signature_image_path",
)


@dataclass
class StoragePlan:
    buckets: tuple[str, ...]
    keep_email: str
    live_refs: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    objects: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    preserved_by_db: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    preserved_by_keep_email: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    orphans: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    skipped_reference_sources: list[str] = field(default_factory=list)
    list_errors: list[str] = field(default_factory=list)


def chunked(values: Iterable[str], size: int) -> Iterable[list[str]]:
    chunk: list[str] = []
    for value in values:
        chunk.append(value)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def require_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def normalize_supabase_url(value: str) -> str:
    return value.rstrip("/")


def normalize_email(value: Optional[str]) -> str:
    return (value or "").strip().casefold()


def clean_object_path(value: Optional[str]) -> str:
    path = unquote((value or "").strip())
    while path.startswith("/"):
        path = path[1:]
    return path


def storage_path_variants(value: Optional[str], bucket: str) -> set[str]:
    raw_value = (value or "").strip()
    if not raw_value:
        return set()

    values = {raw_value, clean_object_path(raw_value)}
    if raw_value.startswith("http://") or raw_value.startswith("https://"):
        try:
            path = unquote(urlparse(raw_value).path or "")
        except Exception:
            path = ""
        markers = (
            f"/storage/v1/object/public/{bucket}/",
            f"/storage/v1/object/sign/{bucket}/",
            f"/storage/v1/object/{bucket}/",
            f"/{bucket}/",
        )
        for marker in markers:
            if marker in path:
                values.add(path.split(marker, 1)[1])
                values.add(f"{bucket}/{path.split(marker, 1)[1]}")
    clean_values: set[str] = set()
    for item in values:
        clean_item = clean_object_path(item)
        if not clean_item:
            continue
        clean_values.add(clean_item)
        if clean_item.startswith(f"{bucket}/"):
            clean_values.add(clean_object_path(clean_item[len(bucket) + 1 :]))
        else:
            clean_values.add(f"{bucket}/{clean_item}")
    return {item for item in clean_values if item}


def object_path_from_storage_value(value: Optional[str], bucket: str) -> str:
    variants = storage_path_variants(value, bucket)
    if not variants:
        return ""
    for path in sorted(variants, key=len):
        if not path.startswith(f"{bucket}/"):
            return path
    return sorted(variants, key=len)[0]


def object_path_from_report_url(value: Optional[str], bucket: str = UPLOADS_BUCKET) -> str:
    raw_value = (value or "").strip()
    if not raw_value:
        return ""
    if raw_value.startswith("reports/"):
        return clean_object_path(raw_value)
    return object_path_from_storage_value(raw_value, bucket)


def add_ref(refs: dict[str, set[str]], bucket: str, path: Optional[str]) -> None:
    for object_path in storage_path_variants(path, bucket):
        refs[bucket].add(object_path)


def object_path_contains_keep_email(path: str, bucket: str, keep_email: str) -> bool:
    normalized_keep_email = normalize_email(keep_email)
    if not normalized_keep_email:
        return False
    return any(normalized_keep_email in normalize_email(variant) for variant in storage_path_variants(path, bucket))


def object_matches_live_reference(path: str, bucket: str, live_refs: set[str]) -> bool:
    return bool(storage_path_variants(path, bucket) & live_refs)


def has_columns(inspector: Any, table_name: str, *columns: str) -> bool:
    if not inspector.has_table(table_name):
        return False
    existing = {column["name"] for column in inspector.get_columns(table_name)}
    return all(column in existing for column in columns)


def fetch_reference_rows(conn: Any, sql: str) -> list[Any]:
    return list(conn.execute(text(sql)).fetchall())


def collect_live_db_references(database_url: str, selected_buckets: tuple[str, ...]) -> tuple[dict[str, set[str]], list[str]]:
    refs: dict[str, set[str]] = defaultdict(set)
    skipped: list[str] = []
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=15000",
        },
    )
    inspector = sa_inspect(engine)
    selected = set(selected_buckets)

    try:
        with engine.connect() as conn:
            if has_columns(inspector, "upload_files", "bucket", "object_path"):
                rows = fetch_reference_rows(
                    conn,
                    "select bucket, object_path from upload_files where bucket is not null and object_path is not null",
                )
                for bucket, object_path in rows:
                    bucket_text = str(bucket or "").strip()
                    if bucket_text in selected:
                        add_ref(refs, bucket_text, object_path)
            else:
                skipped.append("upload_files.bucket/object_path")

            if has_columns(inspector, "uploads", "pdf_url"):
                rows = fetch_reference_rows(conn, "select pdf_url from uploads where pdf_url is not null")
                for (pdf_url,) in rows:
                    for bucket in selected:
                        object_path = object_path_from_report_url(str(pdf_url or ""), bucket)
                        if object_path:
                            add_ref(refs, bucket, object_path)
            else:
                skipped.append("uploads.pdf_url")

            existing_agreement_columns = [
                column
                for column in AGREEMENT_STORAGE_COLUMNS
                if has_columns(inspector, "consulting_agreements", column)
            ]
            missing_agreement_columns = sorted(set(AGREEMENT_STORAGE_COLUMNS) - set(existing_agreement_columns))
            for column in missing_agreement_columns:
                skipped.append(f"consulting_agreements.{column}")
            if existing_agreement_columns:
                select_list = ", ".join(existing_agreement_columns)
                rows = fetch_reference_rows(
                    conn,
                    f"select {select_list} from consulting_agreements",
                )
                for row in rows:
                    for value in row:
                        for bucket in selected:
                            object_path = object_path_from_storage_value(str(value or ""), bucket)
                            if object_path:
                                add_ref(refs, bucket, object_path)
            elif not inspector.has_table("consulting_agreements"):
                skipped.append("consulting_agreements")
    except SQLAlchemyError as exc:
        raise RuntimeError(f"DB reference lookup failed: {type(exc).__name__}: {exc}") from exc
    finally:
        engine.dispose()

    return refs, skipped


def storage_headers(service_role_key: str) -> dict[str, str]:
    return {
        "apikey": service_role_key,
        "Authorization": f"Bearer {service_role_key}",
        "Content-Type": "application/json",
    }


def storage_json_request(
    *,
    supabase_url: str,
    service_role_key: str,
    method: str,
    path: str,
    payload: Optional[dict[str, Any]] = None,
) -> Any:
    url = f"{normalize_supabase_url(supabase_url)}{path}"
    data = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    req = request.Request(
        url,
        data=data,
        headers=storage_headers(service_role_key),
        method=method,
    )
    try:
        with request.urlopen(req, timeout=30) as response:
            body = response.read()
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Storage REST {method} {path} failed: HTTP {exc.code}: {body}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"Storage REST {method} {path} failed: {exc.reason}") from exc
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError:
        return body.decode("utf-8", errors="replace")


def storage_list_prefix(
    *,
    supabase_url: str,
    service_role_key: str,
    bucket: str,
    prefix: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload = {
            "prefix": prefix,
            "limit": LIST_LIMIT,
            "offset": offset,
            "sortBy": {"column": "name", "order": "asc"},
        }
        page = storage_json_request(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            method="POST",
            path=f"/storage/v1/object/list/{quote(bucket, safe='')}",
            payload=payload,
        )
        if not isinstance(page, list):
            raise RuntimeError(f"Storage list for bucket {bucket} returned unexpected response type")
        rows.extend(page)
        if len(page) < LIST_LIMIT:
            break
        offset += len(page)
    return rows


def join_storage_path(prefix: str, name: str) -> str:
    clean_prefix = clean_object_path(prefix).rstrip("/")
    clean_name = clean_object_path(name)
    if clean_prefix and (clean_name == clean_prefix or clean_name.startswith(f"{clean_prefix}/")):
        return clean_name
    if clean_prefix:
        return f"{clean_prefix}/{clean_name}"
    return clean_name


def is_folder_item(item: dict[str, Any]) -> bool:
    if not item.get("name"):
        return False
    metadata = item.get("metadata")
    return item.get("id") is None and (metadata is None or metadata == {})


def list_bucket_objects(*, supabase_url: str, service_role_key: str, bucket: str) -> set[str]:
    objects: set[str] = set()
    visited_prefixes: set[str] = set()

    def walk(prefix: str) -> None:
        clean_prefix = clean_object_path(prefix).rstrip("/")
        if clean_prefix in visited_prefixes:
            return
        visited_prefixes.add(clean_prefix)
        for item in storage_list_prefix(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            bucket=bucket,
            prefix=clean_prefix,
        ):
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            object_path = join_storage_path(clean_prefix, name).rstrip("/")
            if not object_path:
                continue
            if is_folder_item(item):
                walk(object_path)
            else:
                objects.add(object_path)

    walk("")
    return objects


def delete_storage_objects(
    *,
    supabase_url: str,
    service_role_key: str,
    bucket: str,
    paths: Iterable[str],
) -> tuple[int, list[str]]:
    deleted = 0
    failures: list[str] = []
    for batch in chunked(sorted(paths), DELETE_BATCH_SIZE):
        try:
            storage_json_request(
                supabase_url=supabase_url,
                service_role_key=service_role_key,
                method="DELETE",
                path=f"/storage/v1/object/{quote(bucket, safe='')}",
                payload={"prefixes": batch},
            )
            deleted += len(batch)
        except RuntimeError as exc:
            failures.append(f"{bucket}: {type(exc).__name__}: {str(exc)[:500]}")
    return deleted, failures


def build_storage_plan(
    *,
    supabase_url: str,
    service_role_key: str,
    database_url: str,
    selected_buckets: tuple[str, ...],
    keep_email: str,
) -> StoragePlan:
    normalized_keep_email = normalize_email(keep_email)
    plan = StoragePlan(buckets=selected_buckets, keep_email=normalized_keep_email)
    live_refs, skipped_sources = collect_live_db_references(database_url, selected_buckets)
    plan.live_refs.update(live_refs)
    plan.skipped_reference_sources.extend(skipped_sources)

    for bucket in selected_buckets:
        try:
            plan.objects[bucket] = list_bucket_objects(
                supabase_url=supabase_url,
                service_role_key=service_role_key,
                bucket=bucket,
            )
        except RuntimeError as exc:
            plan.list_errors.append(f"{bucket}: {exc}")
            plan.objects[bucket] = set()
        live_refs_for_bucket = plan.live_refs.get(bucket, set())
        for object_path in sorted(plan.objects[bucket]):
            if object_path_contains_keep_email(object_path, bucket, normalized_keep_email):
                plan.preserved_by_keep_email[bucket].add(object_path)
            elif object_matches_live_reference(object_path, bucket, live_refs_for_bucket):
                plan.preserved_by_db[bucket].add(object_path)
            else:
                plan.orphans[bucket].add(object_path)
    return plan


def print_storage_plan(plan: StoragePlan, *, dry_run: bool) -> None:
    print("alphaSource Consulting orphan Supabase Storage cleanup")
    print(f"Mode: {'DRY RUN' if dry_run else 'CONFIRMED DELETE'}")
    print(f"Keep email path preservation: {plan.keep_email}")
    print("Buckets: " + ", ".join(plan.buckets))
    print("This script uses direct Supabase Storage REST calls. It does not import supabase-py.")
    print("This script does not touch GCS secure-upload objects or remote Stripe.")
    print()
    for bucket in plan.buckets:
        object_count = len(plan.objects.get(bucket, set()))
        ref_count = len(plan.live_refs.get(bucket, set()))
        db_preserved_paths = sorted(plan.preserved_by_db.get(bucket, set()))
        keep_preserved_paths = sorted(plan.preserved_by_keep_email.get(bucket, set()))
        orphan_paths = sorted(plan.orphans.get(bucket, set()))
        print(f"{bucket}:")
        print(f"  storage objects listed: {object_count}")
        print(f"  live DB references: {ref_count}")
        print(f"  preserved by DB reference: {len(db_preserved_paths)}")
        print(f"  preserved by keep-email path: {len(keep_preserved_paths)}")
        print(f"  orphan objects planned for deletion: {len(orphan_paths)}")
        if keep_preserved_paths:
            print(f"  sample preserved keep-email paths (first {min(SAMPLE_LIMIT, len(keep_preserved_paths))}):")
            for path in keep_preserved_paths[:SAMPLE_LIMIT]:
                print(f"    {path}")
        else:
            print("  sample preserved keep-email paths: none")
        if orphan_paths:
            print(f"  sample orphan paths (first {min(SAMPLE_LIMIT, len(orphan_paths))}):")
            for path in orphan_paths[:SAMPLE_LIMIT]:
                print(f"    {path}")
        else:
            print("  sample orphan paths: none")
        print()
    if plan.skipped_reference_sources:
        print("Skipped DB reference sources:")
        for source in sorted(set(plan.skipped_reference_sources)):
            print(f"  {source}")
        print()
    if plan.list_errors:
        print("Storage list errors:")
        for list_error in plan.list_errors:
            print(f"  {list_error}")
        print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry-run-first orphan cleanup for alphaSource Consulting Supabase Storage."
    )
    parser.add_argument("--keep-email", required=True, help="Email whose storage paths must always be preserved.")
    parser.add_argument("--bucket", action="append", choices=SUPPORTED_BUCKETS, help="Bucket to scan. Can be repeated.")
    parser.add_argument("--all-buckets", action="store_true", help="Scan all supported alphaSource Consulting buckets.")
    parser.add_argument("--dry-run", action="store_true", help="Preview planned deletions. This is the default mode.")
    parser.add_argument("--confirm", action="store_true", help="Delete orphaned Supabase Storage objects.")
    return parser.parse_args()


def selected_buckets_from_args(args: argparse.Namespace) -> tuple[str, ...]:
    if args.all_buckets and args.bucket:
        raise RuntimeError("Use either --all-buckets or --bucket, not both.")
    if args.all_buckets:
        return SUPPORTED_BUCKETS
    if args.bucket:
        return tuple(dict.fromkeys(args.bucket))
    return SUPPORTED_BUCKETS


def main() -> int:
    args = parse_args()
    if args.confirm and args.dry_run:
        print("Error: use either --dry-run or --confirm, not both.", file=sys.stderr)
        return 2

    try:
        selected_buckets = selected_buckets_from_args(args)
        keep_email = normalize_email(args.keep_email)
        if not keep_email or "@" not in keep_email:
            raise RuntimeError("--keep-email must be a valid email address.")
        supabase_url = require_env("SUPABASE_URL")
        service_role_key = require_env("SUPABASE_SERVICE_ROLE_KEY")
        database_url = require_env("DATABASE_URL")
        dry_run = not args.confirm
        plan = build_storage_plan(
            supabase_url=supabase_url,
            service_role_key=service_role_key,
            database_url=database_url,
            selected_buckets=selected_buckets,
            keep_email=keep_email,
        )
        print_storage_plan(plan, dry_run=dry_run)
        if plan.list_errors:
            print("Storage listing failed for at least one bucket; no objects were deleted.", file=sys.stderr)
            return 1
        if dry_run:
            print("Dry run only. No Supabase Storage objects were deleted.")
            return 0

        total_deleted = 0
        failures: list[str] = []
        for bucket in selected_buckets:
            deleted, bucket_failures = delete_storage_objects(
                supabase_url=supabase_url,
                service_role_key=service_role_key,
                bucket=bucket,
                paths=plan.orphans.get(bucket, set()),
            )
            total_deleted += deleted
            failures.extend(bucket_failures)
        print("Supabase Storage deletion complete:")
        print(f"  deleted: {total_deleted}")
        print(f"  failed batches: {len(failures)}")
        for failure in failures[:20]:
            print(f"    {failure}")
        if len(failures) > 20:
            print(f"    ... {len(failures) - 20} more failures")
        return 1 if failures else 0
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
