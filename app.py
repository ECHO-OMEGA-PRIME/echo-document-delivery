#!/usr/bin/env python3
"""Echo Document Delivery — FORGE multi-tenant document generate/view/deliver service.

ShadowGlass migration of the legacy Cloudflare Worker ``echo-document-delivery``
(**NO CLOUDFLARE**: Workers → FastAPI, D1 → Postgres schema ``document_delivery``,
R2 → MinIO/S3 bucket ``echo-prime-media``).

Provides:

* Tenant registry with branding + provider credentials (Resend email, Twilio SMS)
* Branded document generation (invoice / estimate / receipt / proposal, …)
* Secure view tokens with view-tracking (hashed IP)
* Email / SMS delivery audit trail
* Tenant analytics and settings

Strictly typed (mypy --strict). Run ``verify_types.sh`` for the gate.

**Run (unit):** ``uvicorn app:app --host 0.0.0.0 --port 8116``
**Repo path:** ``SYSTEMS/echo_document_delivery/``
**Runtime WD:** ``/home/forge/echo-document-delivery``
**SDK caps:** ``echo.documentdelivery.*``

Version: see ``__version__``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os

from credential_config import required_env
import re
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Any, cast

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from psycopg2.extensions import connection as PgConnection
from psycopg2.extensions import cursor as PgCursor
from pydantic import BaseModel, Field

from document_delivery_types import (
    AnalyticsPayload,
    CompanyBrand,
    DeliveryResult,
    DocumentGenerateResult,
    DocumentHtmlData,
    DocumentListResult,
    DocumentRow,
    HealthPayload,
    JSONObject,
    OkResult,
    PgConnectKwargs,
    RootPayload,
    TenantCreateResult,
    TenantRow,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [document-delivery] %(levelname)s %(message)s",
)
logger = logging.getLogger("document_delivery")

__version__ = "1.0.3-docs"

PG: PgConnectKwargs = {
    "host": os.environ.get("PGHOST", "localhost"),
    "user": os.environ.get("PGUSER", "echo"),
    "password": required_env("PGPASSWORD"),
    "dbname": os.environ.get("PGDATABASE", "echo"),
}

MINIO_ENDPOINT: str = os.environ.get("MINIO_ENDPOINT", "http://192.168.1.139:9000")
MINIO_ACCESS: str = required_env("MINIO_ACCESS_KEY")
MINIO_SECRET: str = required_env("MINIO_SECRET_KEY")
MINIO_BUCKET: str = os.environ.get("MINIO_BUCKET", "echo-prime-media")
ADMIN_API_KEY: str = os.environ.get("ADMIN_API_KEY", "").strip()

boto3: Any
Config: Any
try:
    import boto3 as _boto3
    from botocore.client import Config as _Config

    boto3 = _boto3
    Config = _Config
    HAS_MINIO = True
except ImportError:
    boto3 = None
    Config = None
    HAS_MINIO = False
    logger.warning("boto3 not available; document HTML is stored in Postgres only")

requests: Any
try:
    import requests as _requests

    requests = _requests
    HAS_REQUESTS = True
except ImportError:
    requests = None
    HAS_REQUESTS = False

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS document_delivery;

CREATE TABLE IF NOT EXISTS document_delivery.tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT UNIQUE NOT NULL,
    company_name TEXT NOT NULL,
    company_phone TEXT DEFAULT '',
    company_email TEXT DEFAULT '',
    company_tagline TEXT DEFAULT '',
    company_website TEXT DEFAULT '',
    company_city TEXT DEFAULT '',
    primary_color TEXT DEFAULT '#0D2847',
    accent_color TEXT DEFAULT '#FFD700',
    logo_url TEXT DEFAULT '',
    email_from TEXT DEFAULT '',
    sms_from TEXT DEFAULT '',
    api_key TEXT UNIQUE NOT NULL,
    resend_api_key TEXT DEFAULT '',
    twilio_sid TEXT DEFAULT '',
    twilio_token TEXT DEFAULT '',
    twilio_phone TEXT DEFAULT '',
    default_payment_terms TEXT DEFAULT 'net_30',
    auto_email_on_generate BOOLEAN NOT NULL DEFAULT false,
    include_view_link BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS document_delivery.documents (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES document_delivery.tenants(id) ON DELETE CASCADE,
    doc_type TEXT NOT NULL,
    doc_number TEXT NOT NULL,
    source_id TEXT DEFAULT '',
    customer_name TEXT NOT NULL,
    customer_email TEXT DEFAULT '',
    customer_phone TEXT DEFAULT '',
    customer_address TEXT DEFAULT '',
    object_key TEXT NOT NULL,
    view_token TEXT UNIQUE NOT NULL,
    total NUMERIC(12,2) DEFAULT 0,
    currency TEXT DEFAULT 'USD',
    metadata JSONB DEFAULT '{}'::jsonb,
    html_body TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_dd_documents_tenant ON document_delivery.documents(tenant_id);
CREATE INDEX IF NOT EXISTS ix_dd_documents_type ON document_delivery.documents(doc_type);

CREATE TABLE IF NOT EXISTS document_delivery.document_views (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document_delivery.documents(id) ON DELETE CASCADE,
    viewed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ip_hash TEXT,
    user_agent TEXT
);
CREATE INDEX IF NOT EXISTS ix_dd_views_document ON document_delivery.document_views(document_id);

CREATE TABLE IF NOT EXISTS document_delivery.deliveries (
    id TEXT PRIMARY KEY,
    document_id TEXT DEFAULT '',
    tenant_id TEXT NOT NULL REFERENCES document_delivery.tenants(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,
    delivered_to TEXT DEFAULT '',
    delivered_at TIMESTAMPTZ,
    error_message TEXT DEFAULT '',
    provider_id TEXT DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_dd_deliveries_tenant ON document_delivery.deliveries(tenant_id);
CREATE INDEX IF NOT EXISTS ix_dd_deliveries_doc ON document_delivery.deliveries(document_id);
"""


@contextmanager
def _db() -> Iterator[PgConnection]:
    """Yield a committed Postgres connection for document delivery operations.

    Opens with :data:`PG` settings, yields the connection, commits on clean
    exit, and always closes. Callers should use ``with _db() as con``.

    Yields:
        psycopg2 connection bound to the Echo fleet database.
    """
    con: PgConnection = psycopg2.connect(**PG)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _uid(prefix: str = "edd") -> str:
    """Return a compact unique id with a short type prefix.

    Args:
        prefix: Logical entity prefix (e.g. ``edd``, ``ten``, ``doc``, ``del``).

    Returns:
        String id like ``prefix_<18 hex chars>``.
    """
    return f"{prefix}_{uuid.uuid4().hex[:18]}"


def _now() -> str:
    """Return the current UTC timestamp as an ISO-8601 string.

    Returns:
        ISO-8601 UTC timestamp suitable for JSON health payloads.
    """
    return datetime.now(timezone.utc).isoformat()


def _minio_client() -> Any:
    """Build a boto3 S3 client pointed at the configured MinIO endpoint.

    Returns:
        Configured ``boto3`` S3 client (short timeouts for FORGE resilience).

    Raises:
        RuntimeError: If ``boto3`` is not installed.
    """
    if not HAS_MINIO or boto3 is None or Config is None:
        raise RuntimeError("boto3 not installed")
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS,
        aws_secret_access_key=MINIO_SECRET,
        config=Config(
            signature_version="s3v4",
            connect_timeout=2,
            read_timeout=3,
            retries={"max_attempts": 1},
        ),
        region_name="us-east-1",
    )


def _put_object(key: str, body: str, content_type: str) -> bool:
    """Store a UTF-8 text object in MinIO (best-effort).

        Ensures the bucket exists, then ``put_object``. On any MinIO failure logs a
        warning and returns ``False`` so callers can rely on Postgres ``html_body``
        fallback.

        Args:
            key: Object key under :data:`MINIO_BUCKET`.
            body: UTF-8 text payload (usually rendered HTML).
            content_type: MIME type (e.g. ``text/html``).

        Returns:
            ``True`` if MinIO accepted the object; ``False`` on skip/failure.
        """
    if not HAS_MINIO:
        return False
    client = _minio_client()
    try:
        try:
            client.head_bucket(Bucket=MINIO_BUCKET)
        except Exception:
            client.create_bucket(Bucket=MINIO_BUCKET)
        client.put_object(
            Bucket=MINIO_BUCKET,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType=content_type,
        )
        return True
    except Exception as exc:
        logger.warning("minio put failed for %s; using Postgres fallback: %s", key, exc)
        return False


def _get_object_text(key: str) -> str | None:
    """Fetch a UTF-8 object from MinIO if available.

        Args:
            key: Object key previously stored via :func:`_put_object`.

        Returns:
            Decoded text, or ``None`` if MinIO is unavailable or the get fails.
        """
    if not HAS_MINIO:
        return None
    try:
        obj = _minio_client().get_object(Bucket=MINIO_BUCKET, Key=key)
        raw = cast(Any, obj["Body"]).read()
        text = cast(bytes, raw).decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return text
    except Exception as exc:
        logger.warning("minio get failed for %s: %s", key, exc)
        return None


def _safe_update(cur: PgCursor, tenant_id: str, body: Mapping[str, Any]) -> bool:
    """Apply an allowlisted partial update to a tenant row.

    Only known branding/provider fields are writable — arbitrary SQL column
    names from request bodies are rejected by omission.

    Args:
        cur: Open psycopg2 cursor.
        tenant_id: Tenant primary key.
        body: Mapping of field → value from the request.

    Returns:
        ``True`` if at least one allowlisted field was updated; else ``False``.
    """
    allowed = [
        "name",
        "company_name",
        "company_phone",
        "company_email",
        "company_tagline",
        "company_website",
        "company_city",
        "primary_color",
        "accent_color",
        "logo_url",
        "email_from",
        "sms_from",
        "resend_api_key",
        "twilio_sid",
        "twilio_token",
        "twilio_phone",
        "default_payment_terms",
        "auto_email_on_generate",
        "include_view_link",
    ]
    fields: list[str] = []
    values: list[Any] = []
    for key in allowed:
        if key in body:
            fields.append(f"{key}=%s")
            values.append(body[key])
    if not fields:
        return False
    fields.append("updated_at=now()")
    values.append(tenant_id)
    cur.execute(
        f"UPDATE document_delivery.tenants SET {', '.join(fields)} WHERE id=%s",
        values,
    )
    return True


def _tenant_from_key(cur: PgCursor, tenant_key: str | None) -> TenantRow | None:
    """Resolve a tenant row from the ``X-Tenant-Key`` API key header.

    Args:
        cur: Open RealDict or standard cursor.
        tenant_key: Raw API key from the request header.

    Returns:
        Tenant dict if found; ``None`` if missing/invalid.
    """
    if not tenant_key:
        return None
    cur.execute(
        "SELECT * FROM document_delivery.tenants WHERE api_key=%s",
        (tenant_key,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return cast(TenantRow, dict(row))


def _require_admin(x_admin_key: str | None, x_echo_api_key: str | None) -> None:
    """Enforce admin authentication for tenant-management routes.

        Accepts either ``X-Admin-Key`` or ``X-Echo-API-Key`` and compares the
        supplied value with :func:`hmac.compare_digest`. Missing service
        configuration is an availability failure, never an authentication bypass.

        Args:
            x_admin_key: Value of ``X-Admin-Key`` header, if present.
            x_echo_api_key: Value of ``X-Echo-API-Key`` header, if present.

        Raises:
            HTTPException: 503 when configuration is missing, or 401 when request
            credentials are missing or wrong.
        """
    if not ADMIN_API_KEY:
        raise HTTPException(503, "Admin authentication is not configured")
    key = x_admin_key or x_echo_api_key
    if key and hmac.compare_digest(key, ADMIN_API_KEY):
        return
    raise HTTPException(401, "Admin access required")


def _hash_ip(ip: str) -> str:
    """Hash a client IP for privacy-preserving view analytics.

        Args:
            ip: Client IP (or forwarded header value).

        Returns:
            16-character hex digest (salted SHA-256 prefix).
        """
    return hashlib.sha256((ip + "echo-doc-salt").encode("utf-8")).hexdigest()[:16]


def _company(tenant: TenantRow | Mapping[str, Any]) -> CompanyBrand:
    """Project tenant branding fields into the document HTML template shape.

    Args:
        tenant: Tenant row dict from Postgres.

    Returns:
        Company dict with camelCase keys used by :func:`_document_html`.
    """
    return {
        "name": str(tenant["company_name"]),
        "phone": str(tenant.get("company_phone") or ""),
        "email": str(tenant.get("company_email") or ""),
        "tagline": str(tenant.get("company_tagline") or ""),
        "website": str(tenant.get("company_website") or ""),
        "city": str(tenant.get("company_city") or ""),
        "primaryColor": str(tenant.get("primary_color") or "#0D2847"),
        "accentColor": str(tenant.get("accent_color") or "#FFD700"),
        "logoUrl": str(tenant.get("logo_url") or ""),
    }


def _money(value: Any) -> str:
    """Format a numeric amount as a two-decimal money string.

    Args:
        value: Any value coercible to ``float``.

    Returns:
        Formatted string (e.g. ``1,234.50``) or ``0.00`` on error.
    """
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return "0.00"


def _document_html(data: DocumentHtmlData | Mapping[str, Any]) -> str:
    """Render a branded, printable HTML document from structured data.

    Escapes all user-controlled strings. Includes line items, totals, optional
    scope list/notes, and print/PDF helper buttons that call the email-pdf
    export route.

    Args:
        data: Document payload including ``company``, ``items``, customer
            fields, amounts, and metadata.

    Returns:
        Complete HTML document string.
    """
    esc = html.escape
    company = cast(CompanyBrand | Mapping[str, Any], data["company"])
    items = cast(list[Any], data.get("items") or [])
    rows: list[str] = []
    for item in items:
        item_map = cast(Mapping[str, Any], item)
        desc = esc(str(item_map.get("description") or item_map.get("name") or "Item"))
        qty = item_map.get("quantity", item_map.get("qty", 1))
        rate = item_map.get("rate", item_map.get("price", item_map.get("unit_price", 0)))
        amount = item_map.get("amount", float(qty or 0) * float(rate or 0))
        rows.append(
            f"<tr><td>{desc}</td><td>{esc(str(qty))}</td><td>${_money(rate)}</td>"
            f"<td class='num'>${_money(amount)}</td></tr>"
        )
    scope_raw = cast(list[Any], data.get("scopeItems") or [])
    scope_items = "".join(f"<li>{esc(str(x))}</li>" for x in scope_raw)
    primary = esc(str(company["primaryColor"]))
    accent = esc(str(company["accentColor"]))
    title = esc(str(data["type"]).replace("_", " "))
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} {esc(str(data["docNumber"]))}</title>
<style>
body{{font-family:Arial,sans-serif;margin:0;background:#f6f7f9;color:#111827}}
.bar{{background:{primary};color:white;padding:18px 32px;display:flex;justify-content:space-between}}
.wrap{{max-width:920px;margin:24px auto;background:white;padding:32px;border:1px solid #e5e7eb}}
.muted{{color:#6b7280;font-size:13px}} table{{width:100%;border-collapse:collapse;margin-top:20px}}
th,td{{border-bottom:1px solid #e5e7eb;padding:10px;text-align:left}} th{{background:#f3f4f6}}
.num{{text-align:right}} .total{{font-size:22px;font-weight:700;color:{primary}}}
.btn{{background:{primary};color:white;padding:10px 14px;border-radius:6px;text-decoration:none;margin-right:8px}}
@media print{{.no-print{{display:none}}.wrap{{border:0;margin:0;max-width:none}}}}
</style></head><body>
<div class="bar"><div><strong>{esc(company["name"])}</strong><div class="muted">{esc(company["tagline"])}</div></div>
<div>{title} #{esc(str(data["docNumber"]))}</div></div>
<div class="wrap">
<div class="no-print"><a class="btn" href="javascript:window.print()">Print</a>
<a class="btn" href="#" onclick="downloadPDF();return false">Save PDF</a></div>
<p><strong>Date:</strong> {esc(str(data.get("date") or ""))}
 &nbsp; <strong>Due:</strong> {esc(str(data.get("dueDate") or ""))}
 &nbsp; <strong>Service:</strong> {esc(str(data.get("serviceDate") or ""))}</p>
<h3>Bill To</h3><p>{esc(str(data["customerName"]))}<br>{esc(str(data.get("customerAddress") or ""))}<br>
{esc(str(data.get("customerEmail") or ""))} {esc(str(data.get("customerPhone") or ""))}</p>
<h3>{esc(str(data.get("jobTitle") or data.get("serviceType") or "Details"))}</h3>
<table><thead><tr><th>Description</th><th>Qty</th><th>Rate</th><th class="num">Amount</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<p class="num">Subtotal: ${_money(data.get("subtotal"))}<br>Tax: ${_money(data.get("taxAmount"))}<br>
Paid: ${_money(data.get("amountPaid"))}</p>
<p class="num total">Total: {esc(str(data.get("currency") or "USD"))} ${_money(data.get("total"))}</p>
{f"<h3>Scope</h3><ul>{scope_items}</ul>" if scope_items else ""}
{f"<h3>Notes</h3><p>{esc(str(data.get('notes') or ''))}</p>" if data.get("notes") else ""}
<p class="muted">{esc(company["name"])} | {esc(company["city"])} | {esc(company["phone"])} | {esc(company["email"])}</p>
</div>
<script>
window.DOC_API_BASE = window.DOC_API_BASE || '';
async function downloadPDF(){{ const path = location.pathname.replace('/view/','/deliver/email-pdf?token=');
window.open((window.DOC_API_BASE || '') + path, '_blank'); }}
</script></body></html>"""


def _record_delivery(
    cur: PgCursor,
    tenant_id: str,
    document_id: str,
    channel: str,
    status: str,
    delivered_to: str,
    error: str = "",
    provider_id: str = "",
) -> str:
    """Insert a delivery audit row and return its id.

        Args:
            cur: Open cursor.
            tenant_id: Owning tenant id.
            document_id: Related document id (may be empty string).
            channel: ``email`` or ``sms``.
            status: ``sent`` or ``failed``.
            delivered_to: Destination address/number.
            error: Optional error message when status is failed.
            provider_id: Optional Resend/Twilio provider message id.

        Returns:
            New delivery id string.
        """
    delivery_id = _uid("del")
    cur.execute(
        "INSERT INTO document_delivery.deliveries "
        "(id, document_id, tenant_id, channel, status, delivered_to, delivered_at, "
        "error_message, provider_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            delivery_id,
            document_id or "",
            tenant_id,
            channel,
            status,
            delivered_to,
            datetime.now(timezone.utc) if status == "sent" else None,
            error,
            provider_id,
        ),
    )
    return delivery_id


def _send_email(
    cur: PgCursor,
    tenant: TenantRow | Mapping[str, Any],
    company: CompanyBrand | Mapping[str, Any],
    document_id: str,
    to: str,
    subject: str,
    message: str,
    view_url: str,
) -> DeliveryResult:
    """Send (or attempt) a Resend email for a document and log the delivery.

    Uses the tenant's ``resend_api_key``. On missing provider or transport
    errors, records a ``failed`` delivery and returns ``ok=False``.

    Args:
        cur: Open cursor (delivery rows are written in the same transaction).
        tenant: Tenant row with provider credentials.
        company: Branding dict from :func:`_company`.
        document_id: Document being delivered.
        to: Recipient email.
        subject: Email subject line.
        message: Optional HTML-safe plain message body.
        view_url: Secure document view URL to embed.

    Returns:
        Result dict with ``ok``, ``delivery_id``, and provider fields/errors.
    """
    tenant_id = str(tenant["id"])
    resend_key = str(tenant.get("resend_api_key") or "")
    if not resend_key:
        did = _record_delivery(
            cur, tenant_id, document_id, "email", "failed", to, "No email provider configured"
        )
        return {"ok": False, "delivery_id": did, "error": "Email provider not configured"}
    if not HAS_REQUESTS or requests is None:
        did = _record_delivery(
            cur, tenant_id, document_id, "email", "failed", to, "requests not installed"
        )
        return {"ok": False, "delivery_id": did, "error": "requests not installed"}
    email_html = (
        f"<h2>{html.escape(str(company['name']))}</h2>"
        f"<p>{html.escape(message or '')}</p>"
        f"<p><a href='{html.escape(view_url)}'>View Document</a></p>"
    )
    from_addr = str(tenant.get("email_from") or f"{company['name']} <noreply@echo-op.com>")
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": from_addr,
                "to": [to],
                "subject": subject,
                "html": email_html,
            },
            timeout=20,
        )
        raw_json: Any = resp.json() if resp.content else {}
        data = cast(JSONObject, raw_json) if isinstance(raw_json, dict) else {}
        did = _record_delivery(
            cur,
            tenant_id,
            document_id,
            "email",
            "sent" if resp.ok else "failed",
            to,
            "" if resp.ok else str(data.get("message") or resp.text[:200]),
            str(data.get("id") or ""),
        )
        return {
            "ok": bool(resp.ok),
            "delivery_id": did,
            "email_id": data.get("id"),
            "status": int(resp.status_code),
        }
    except Exception as exc:
        did = _record_delivery(
            cur, tenant_id, document_id, "email", "failed", to, str(exc)[:200]
        )
        return {"ok": False, "delivery_id": did, "error": str(exc)}


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: ensure the ``document_delivery`` schema exists.

    Runs idempotent ``CREATE SCHEMA/TABLE IF NOT EXISTS`` against the fleet DB
    on startup, then yields control for the process lifetime.
    """
    with _db() as con, con.cursor() as cur:
        cur.execute(SCHEMA)
    logger.info("document_delivery schema ready")
    yield


app = FastAPI(
    title="Echo Document Delivery",
    version=__version__,
    description=(
        "Multi-tenant document generate/view/deliver service "
        "(Postgres + MinIO; Resend/Twilio delivery)."
    ),
    lifespan=_lifespan,
)


class TenantCreate(BaseModel):
    """Request body for admin tenant creation.

    Attributes:
        name: Display name for the tenant account.
        slug: URL-safe unique slug used in object key prefixes.
        company_name: Legal/brand name printed on documents.
        company_phone: Optional company phone for document footer.
        company_email: Optional company email for document footer.
        company_tagline: Optional brand tagline under the company name.
        company_website: Optional website URL.
        company_city: Optional city shown in branding block.
        primary_color: Brand primary hex color (default navy).
        accent_color: Brand accent hex color (default gold).
        logo_url: Optional absolute logo image URL.
        email_from: Default From address for Resend deliveries.
        sms_from: Optional SMS sender label.
        resend_api_key: Optional Resend API key for this tenant.
        twilio_sid: Optional Twilio account SID.
        twilio_token: Optional Twilio auth token.
        twilio_phone: Optional Twilio from-number.
        default_payment_terms: Default terms string (e.g. ``net_30``).
    """

    name: str
    slug: str
    company_name: str
    company_phone: str = ""
    company_email: str = ""
    company_tagline: str = ""
    company_website: str = ""
    company_city: str = ""
    primary_color: str = "#0D2847"
    accent_color: str = "#FFD700"
    logo_url: str = ""
    email_from: str = ""
    sms_from: str = ""
    resend_api_key: str = ""
    twilio_sid: str = ""
    twilio_token: str = ""
    twilio_phone: str = ""
    default_payment_terms: str = "net_30"


class DocumentGenerate(BaseModel):
    """Request body for tenant document generation.

    Attributes:
        doc_type: Document kind (invoice, estimate, receipt, proposal, …).
        doc_number: Human-facing document number.
        customer_name: Bill-to name (required).
        items: Line items (description/qty/rate/amount flexible keys).
        source_id: Optional upstream job/CRM id.
        customer_email: Optional customer email for prefill/delivery.
        customer_phone: Optional customer phone.
        customer_address: Optional multi-line address.
        date: Issue date (defaults to today when omitted).
        due_date: Optional due date string.
        service_date: Optional service/performance date.
        job_title: Optional job title heading.
        service_type: Optional service category label.
        subtotal: Pre-tax subtotal.
        tax_rate: Tax rate (fraction or percent depending on client convention).
        tax_amount: Explicit tax amount.
        total: Grand total.
        amount_paid: Amount already paid (receipts).
        currency: ISO currency code (default ``USD``).
        scope_items: Optional scope-of-work bullet strings.
        notes: Free-form notes rendered on the document.
        payment_terms: Payment terms override for this document.
        metadata: Arbitrary JSON stored with the document row.
    """

    doc_type: str
    doc_number: str
    customer_name: str
    items: list[dict[str, Any]]
    source_id: str = ""
    customer_email: str = ""
    customer_phone: str = ""
    customer_address: str = ""
    date: str | None = None
    due_date: str = ""
    service_date: str = ""
    job_title: str = ""
    service_type: str = ""
    subtotal: float = 0
    tax_rate: float = 0
    tax_amount: float = 0
    total: float = 0
    amount_paid: float = 0
    currency: str = "USD"
    scope_items: list[str] = Field(default_factory=list)
    notes: str = ""
    payment_terms: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmailDelivery(BaseModel):
    """Request body for email delivery of a document link.

    Attributes:
        to: Recipient email address.
        subject: Optional subject (defaults to company document subject).
        message: Optional message body above the view link.
        document_id: Optional document to attach/resolve view URL for.
        view_url: Optional prebuilt view URL.
    """

    to: str
    subject: str = ""
    message: str = ""
    document_id: str = ""
    view_url: str = ""


class SmsDelivery(BaseModel):
    """Request body for SMS delivery.

    Attributes:
        to: Destination phone number (E.164 recommended).
        body: SMS text body.
        document_id: Optional related document for audit linkage.
    """

    to: str
    body: str
    document_id: str = ""


@app.get("/")
def root() -> RootPayload:
    """Service identity probe (no auth).

    Returns:
        Dict with service name, version, and operational status.
    """
    return {
        "service": "echo-document-delivery",
        "version": __version__,
        "status": "operational",
        "docs": "/docs",
    }


@app.get("/health")
def health() -> HealthPayload:
    """Liveness + dependency probe for orchestrators and SDK health caps.

    Attempts ``SELECT 1`` against Postgres and reports MinIO client availability.

    Returns:
        Health payload with ``ok``, ``status`` (healthy/degraded), ``db``,
        ``minio``, ``bucket``, ``version``, and UTC ``timestamp``.
    """
    db_ok = False
    try:
        with _db() as con, con.cursor() as cur:
            cur.execute("SELECT 1")
        db_ok = True
    except Exception as exc:
        logger.warning("health db failed: %s", exc)
    return {
        "ok": db_ok,
        "status": "healthy" if db_ok else "degraded",
        "service": "echo-document-delivery",
        "version": __version__,
        "db": db_ok,
        "minio": HAS_MINIO,
        "bucket": MINIO_BUCKET,
        "timestamp": _now(),
    }


@app.post("/tenants")
def tenants_create(
    b: TenantCreate,
    x_admin_key: str | None = Header(default=None),
    x_echo_api_key: str | None = Header(default=None),
) -> TenantCreateResult:
    """Create a tenant and issue a tenant API key (admin).

    Requires admin headers. Generates a unique tenant id and ``edd_`` API key.

    Args:
        b: Tenant create payload (name, slug, company branding, providers).
        x_admin_key: ``X-Admin-Key`` header.
        x_echo_api_key: ``X-Echo-API-Key`` header.

    Returns:
        ``ok``, ``tenant_id``, ``api_key``, and ``slug``.
    """
    _require_admin(x_admin_key, x_echo_api_key)
    tenant_id = _uid("ten")
    api_key = "edd_" + uuid.uuid4().hex
    with _db() as con, con.cursor() as cur:
        cur.execute(
            "INSERT INTO document_delivery.tenants "
            "(id,name,slug,company_name,company_phone,company_email,company_tagline,"
            "company_website,company_city,primary_color,accent_color,logo_url,email_from,"
            "sms_from,api_key,resend_api_key,twilio_sid,twilio_token,twilio_phone,default_payment_terms) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                tenant_id,
                b.name,
                b.slug,
                b.company_name,
                b.company_phone,
                b.company_email,
                b.company_tagline,
                b.company_website,
                b.company_city,
                b.primary_color,
                b.accent_color,
                b.logo_url,
                b.email_from,
                b.sms_from,
                api_key,
                b.resend_api_key,
                b.twilio_sid,
                b.twilio_token,
                b.twilio_phone,
                b.default_payment_terms,
            ),
        )
    return {"ok": True, "tenant_id": tenant_id, "api_key": api_key, "slug": b.slug}


@app.get("/tenants")
def tenants_list(
    x_admin_key: str | None = Header(default=None),
    x_echo_api_key: str | None = Header(default=None),
) -> list[JSONObject]:
    """List tenants (admin) with non-secret summary fields.

    Args:
        x_admin_key: ``X-Admin-Key`` header.
        x_echo_api_key: ``X-Echo-API-Key`` header.

    Returns:
        List of tenant summary dicts ordered by ``created_at`` desc.
    """
    _require_admin(x_admin_key, x_echo_api_key)
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id,name,slug,company_name,company_email,created_at "
            "FROM document_delivery.tenants ORDER BY created_at DESC"
        )
        return [cast(JSONObject, dict(r)) for r in cur.fetchall()]


@app.get("/tenants/{tenant_id}")
def tenants_get(
    tenant_id: str,
    x_admin_key: str | None = Header(default=None),
    x_echo_api_key: str | None = Header(default=None),
) -> JSONObject:
    """Fetch a single tenant row by id (admin).

    Args:
        tenant_id: Tenant primary key.
        x_admin_key: ``X-Admin-Key`` header.
        x_echo_api_key: ``X-Echo-API-Key`` header.

    Returns:
        Full tenant row (includes secrets — admin only).

    Raises:
        HTTPException: 404 if the tenant does not exist.
    """
    _require_admin(x_admin_key, x_echo_api_key)
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM document_delivery.tenants WHERE id=%s", (tenant_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Tenant not found")
        return cast(JSONObject, dict(row))


@app.put("/tenants/{tenant_id}")
async def tenants_update(
    tenant_id: str,
    request: Request,
    x_admin_key: str | None = Header(default=None),
    x_echo_api_key: str | None = Header(default=None),
) -> OkResult:
    """Partially update allowlisted tenant fields (admin).

    Args:
        tenant_id: Tenant primary key.
        request: JSON body of fields to update.
        x_admin_key: ``X-Admin-Key`` header.
        x_echo_api_key: ``X-Echo-API-Key`` header.

    Returns:
        ``{"ok": true}`` on success.

    Raises:
        HTTPException: 400 when no allowlisted fields are present.
    """
    _require_admin(x_admin_key, x_echo_api_key)
    raw_body: Any = await request.json()
    if not isinstance(raw_body, dict):
        raise HTTPException(400, "JSON object body required")
    body = cast(JSONObject, raw_body)
    with _db() as con, con.cursor() as cur:
        if not _safe_update(cur, tenant_id, body):
            raise HTTPException(400, "No valid fields to update")
    return {"ok": True}


@app.post("/documents/generate")
def documents_generate(
    b: DocumentGenerate,
    request: Request,
    x_tenant_key: str | None = Header(default=None),
) -> DocumentGenerateResult:
    """Generate a branded document, persist HTML, and issue a view token.

    Requires ``X-Tenant-Key``. Renders HTML, stores to MinIO (best-effort) and
    Postgres ``html_body``, and optionally auto-emails when the tenant has
    ``auto_email_on_generate`` and a customer email.

    Args:
        b: Document generate payload (type, number, customer, items, totals).
        request: FastAPI request (used for absolute view URL).
        x_tenant_key: Tenant API key header.

    Returns:
        Document id, object key, view URL/token, and ``ok`` flag.

    Raises:
        HTTPException: 401 for missing/invalid tenant key.
    """
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        tenant = _tenant_from_key(cast(PgCursor, cur), x_tenant_key)
        if not tenant:
            raise HTTPException(401, "Invalid or missing X-Tenant-Key header")
        company = _company(tenant)
        date_s = b.date or datetime.now(timezone.utc).date().isoformat()
        doc_data: DocumentHtmlData = {
            "type": b.doc_type.upper(),
            "docNumber": b.doc_number,
            "date": date_s,
            "dueDate": b.due_date,
            "serviceDate": b.service_date,
            "customerName": b.customer_name,
            "customerEmail": b.customer_email,
            "customerPhone": b.customer_phone,
            "customerAddress": b.customer_address,
            "jobTitle": b.job_title,
            "serviceType": b.service_type,
            "items": b.items,
            "subtotal": b.subtotal,
            "taxRate": b.tax_rate,
            "taxAmount": b.tax_amount,
            "total": b.total,
            "amountPaid": b.amount_paid,
            "currency": b.currency,
            "scopeItems": b.scope_items,
            "notes": b.notes,
            "paymentTerms": b.payment_terms
            or str(tenant.get("default_payment_terms") or ""),
            "company": company,
        }
        rendered = _document_html(doc_data)
        safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", b.customer_name or "customer")[:30]
        now = datetime.now(timezone.utc)
        object_key = (
            f"{tenant['slug']}/documents/{now.year}/{now.month:02d}/"
            f"{b.doc_type.lower()}/{b.doc_number}_{safe_name}.html"
        )
        _put_object(object_key, rendered, "text/html")
        doc_id = _uid("doc")
        view_token = str(uuid.uuid4())
        metadata: JSONObject = {
            "items": b.items,
            "subtotal": b.subtotal,
            "tax_rate": b.tax_rate,
            "tax_amount": b.tax_amount,
            "amount_paid": b.amount_paid,
            "due_date": b.due_date,
            "service_date": b.service_date,
            "job_title": b.job_title,
            "service_type": b.service_type,
            "notes": b.notes,
            "payment_terms": b.payment_terms,
            "scope_items": b.scope_items,
            **(b.metadata or {}),
        }
        cur.execute(
            "INSERT INTO document_delivery.documents "
            "(id,tenant_id,doc_type,doc_number,source_id,customer_name,customer_email,"
            "customer_phone,customer_address,object_key,view_token,total,currency,metadata,html_body) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                doc_id,
                tenant["id"],
                b.doc_type.upper(),
                b.doc_number,
                b.source_id,
                b.customer_name,
                b.customer_email,
                b.customer_phone,
                b.customer_address,
                object_key,
                view_token,
                b.total,
                b.currency,
                json.dumps(metadata),
                rendered,
            ),
        )
        view_url = str(request.base_url).rstrip("/") + f"/view/{view_token}"
        if tenant.get("auto_email_on_generate") and b.customer_email:
            _send_email(
                cast(PgCursor, cur),
                tenant,
                company,
                doc_id,
                b.customer_email,
                f"{b.doc_type.replace('_', ' ')} {b.doc_number} from {company['name']}",
                "",
                view_url,
            )
        return {
            "ok": True,
            "document_id": doc_id,
            "doc_number": b.doc_number,
            "object_key": object_key,
            "r2_key": object_key,
            "view_url": view_url,
            "view_token": view_token,
        }


@app.get("/view/{token}", response_class=HTMLResponse)
def view_document(token: str, request: Request) -> HTMLResponse:
    """Public document view by unguessable token; records a view event.

    Loads HTML from MinIO with Postgres fallback, injects ``DOC_API_BASE`` for
    the print/PDF helper, and logs a hashed IP + user-agent view row.

    Args:
        token: Document ``view_token`` UUID.
        request: Client request (IP / UA extraction).

    Returns:
        HTMLResponse of the document, or 404 HTML when not found.
    """
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT d.*, t.slug AS tenant_slug FROM document_delivery.documents d "
            "JOIN document_delivery.tenants t ON d.tenant_id=t.id WHERE d.view_token=%s",
            (token,),
        )
        raw_doc = cur.fetchone()
        if not raw_doc:
            return HTMLResponse("<h1>Document not found</h1>", status_code=404)
        doc = cast(DocumentRow, dict(raw_doc))
        ip = request.headers.get("x-forwarded-for") or (
            request.client.host if request.client else "unknown"
        )
        cur.execute(
            "INSERT INTO document_delivery.document_views (id,document_id,ip_hash,user_agent) "
            "VALUES (%s,%s,%s,%s)",
            (
                _uid("view"),
                doc["id"],
                _hash_ip(ip),
                (request.headers.get("user-agent") or "")[:200],
            ),
        )
        rendered = _get_object_text(str(doc.get("object_key") or "")) or str(
            doc.get("html_body") or ""
        )
        api_base = str(request.base_url).rstrip("/")
        return HTMLResponse(rendered.replace("window.DOC_API_BASE || ''", f"'{api_base}'"))


@app.get("/documents")
def documents_list(
    x_tenant_key: str | None = Header(default=None),
    type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> DocumentListResult:
    """List documents for the authenticated tenant with view stats.

    Args:
        x_tenant_key: Tenant API key header.
        type: Optional document type filter (e.g. ``INVOICE``).
        limit: Page size (capped at 200).
        offset: Pagination offset.

    Returns:
        ``{"documents": [...], "total": N}``.

    Raises:
        HTTPException: 401 for missing/invalid tenant key.
    """
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        tenant = _tenant_from_key(cast(PgCursor, cur), x_tenant_key)
        if not tenant:
            raise HTTPException(401, "Invalid or missing X-Tenant-Key header")
        params: list[Any] = [tenant["id"]]
        where = "WHERE d.tenant_id=%s"
        if type:
            where += " AND d.doc_type=%s"
            params.append(type.upper())
        params.extend([min(limit, 200), max(offset, 0)])
        cur.execute(
            "SELECT d.*, "
            "(SELECT COUNT(*) FROM document_delivery.document_views dv WHERE dv.document_id=d.id) AS view_count, "
            "(SELECT MAX(dv.viewed_at) FROM document_delivery.document_views dv WHERE dv.document_id=d.id) AS last_viewed_at "
            f"FROM document_delivery.documents d {where} ORDER BY d.created_at DESC LIMIT %s OFFSET %s",
            params,
        )
        docs = [cast(JSONObject, dict(r)) for r in cur.fetchall()]
        count_params: list[Any] = [tenant["id"]]
        count_where = "WHERE tenant_id=%s"
        if type:
            count_where += " AND doc_type=%s"
            count_params.append(type.upper())
        cur.execute(
            f"SELECT COUNT(*) AS total FROM document_delivery.documents {count_where}",
            count_params,
        )
        count_row = cur.fetchone()
        total = int(count_row["total"]) if count_row and count_row.get("total") is not None else 0
        return {"documents": docs, "total": total}


@app.get("/documents/{doc_id}")
def documents_get(
    doc_id: str,
    x_tenant_key: str | None = Header(default=None),
) -> JSONObject:
    """Get one document plus view count and delivery history.

    Args:
        doc_id: Document primary key.
        x_tenant_key: Tenant API key header.

    Returns:
        Document dict with ``view_count`` and ``deliveries`` list.

    Raises:
        HTTPException: 401/404 as appropriate.
    """
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        tenant = _tenant_from_key(cast(PgCursor, cur), x_tenant_key)
        if not tenant:
            raise HTTPException(401, "Invalid or missing X-Tenant-Key header")
        cur.execute(
            "SELECT * FROM document_delivery.documents WHERE id=%s AND tenant_id=%s",
            (doc_id, tenant["id"]),
        )
        raw_doc = cur.fetchone()
        if not raw_doc:
            raise HTTPException(404, "Not found")
        cur.execute(
            "SELECT COUNT(*) AS count FROM document_delivery.document_views WHERE document_id=%s",
            (doc_id,),
        )
        count_row = cur.fetchone()
        view_count = (
            int(count_row["count"])
            if count_row and count_row.get("count") is not None
            else 0
        )
        cur.execute(
            "SELECT * FROM document_delivery.deliveries WHERE document_id=%s ORDER BY created_at DESC",
            (doc_id,),
        )
        out = cast(JSONObject, dict(raw_doc))
        out["view_count"] = view_count
        out["deliveries"] = [cast(JSONObject, dict(r)) for r in cur.fetchall()]
        return out


@app.post("/deliver/email")
def deliver_email(
    b: EmailDelivery,
    request: Request,
    x_tenant_key: str | None = Header(default=None),
) -> Response:
    """Send an email delivery for a document (tenant Resend config).

    Resolves ``view_url`` from ``document_id`` when not supplied.

    Args:
        b: Email delivery body (``to``, subject/message, optional document_id).
        request: Used to build absolute view URLs.
        x_tenant_key: Tenant API key header.

    Returns:
        JSON result; HTTP 200 on success, 502 when provider send fails.
    """
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        tenant = _tenant_from_key(cast(PgCursor, cur), x_tenant_key)
        if not tenant:
            raise HTTPException(401, "Invalid or missing X-Tenant-Key header")
        view_url = b.view_url
        if b.document_id and not view_url:
            cur.execute(
                "SELECT view_token FROM document_delivery.documents WHERE id=%s AND tenant_id=%s",
                (b.document_id, tenant["id"]),
            )
            row = cur.fetchone()
            if row:
                view_url = (
                    str(request.base_url).rstrip("/") + f"/view/{row['view_token']}"
                )
        result = _send_email(
            cast(PgCursor, cur),
            tenant,
            _company(tenant),
            b.document_id,
            b.to,
            b.subject or f"Document from {tenant['company_name']}",
            b.message,
            view_url,
        )
        return Response(
            json.dumps(result),
            media_type="application/json",
            status_code=200 if result.get("ok") else 502,
        )


@app.post("/deliver/sms")
def deliver_sms(
    b: SmsDelivery,
    x_tenant_key: str | None = Header(default=None),
) -> Response:
    """Send an SMS delivery via tenant Twilio credentials.

    Args:
        b: SMS body with ``to``, ``body``, optional ``document_id``.
        x_tenant_key: Tenant API key header.

    Returns:
        JSON result; 503 if Twilio is not configured, 500 on transport error.
    """
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        tenant = _tenant_from_key(cast(PgCursor, cur), x_tenant_key)
        if not tenant:
            raise HTTPException(401, "Invalid or missing X-Tenant-Key header")
        tenant_id = str(tenant["id"])
        sid = str(tenant.get("twilio_sid") or "")
        token = str(tenant.get("twilio_token") or "")
        from_num = str(tenant.get("twilio_phone") or tenant.get("sms_from") or "")
        if not (sid and token and from_num):
            did = _record_delivery(
                cast(PgCursor, cur),
                tenant_id,
                b.document_id,
                "sms",
                "failed",
                b.to,
                "Twilio not configured",
            )
            return Response(
                json.dumps(
                    {
                        "ok": False,
                        "delivery_id": did,
                        "error": "Twilio not configured for this tenant",
                    }
                ),
                media_type="application/json",
                status_code=503,
            )
        if not HAS_REQUESTS or requests is None:
            did = _record_delivery(
                cast(PgCursor, cur),
                tenant_id,
                b.document_id,
                "sms",
                "failed",
                b.to,
                "requests not installed",
            )
            return Response(
                json.dumps(
                    {
                        "ok": False,
                        "delivery_id": did,
                        "error": "requests not installed",
                    }
                ),
                media_type="application/json",
                status_code=503,
            )
        try:
            auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
            resp = requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
                data={"To": b.to, "From": from_num, "Body": b.body},
                headers={"Authorization": f"Basic {auth}"},
                timeout=20,
            )
            raw_json: Any = resp.json() if resp.content else {}
            data = cast(JSONObject, raw_json) if isinstance(raw_json, dict) else {}
            did = _record_delivery(
                cast(PgCursor, cur),
                tenant_id,
                b.document_id,
                "sms",
                "sent" if resp.ok else "failed",
                b.to,
                "" if resp.ok else str(data.get("message") or resp.text[:200]),
                str(data.get("sid") or ""),
            )
            payload: JSONObject = {
                "ok": bool(resp.ok),
                "delivery_id": did,
                "status": int(resp.status_code),
                "sms_sid": data.get("sid"),
            }
            return Response(
                json.dumps(payload),
                media_type="application/json",
                status_code=200 if resp.ok else 502,
            )
        except Exception as exc:
            did = _record_delivery(
                cast(PgCursor, cur),
                tenant_id,
                b.document_id,
                "sms",
                "failed",
                b.to,
                str(exc)[:200],
            )
            return Response(
                json.dumps({"ok": False, "delivery_id": did, "error": str(exc)}),
                media_type="application/json",
                status_code=500,
            )


@app.get("/deliveries")
def deliveries(
    x_tenant_key: str | None = Header(default=None),
    limit: int = 50,
) -> list[JSONObject]:
    """List recent deliveries for the authenticated tenant.

    Args:
        x_tenant_key: Tenant API key header.
        limit: Max rows (capped at 200).

    Returns:
        List of delivery rows joined with document type/number/customer.
    """
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        tenant = _tenant_from_key(cast(PgCursor, cur), x_tenant_key)
        if not tenant:
            raise HTTPException(401, "Invalid or missing X-Tenant-Key header")
        cur.execute(
            "SELECT dl.*, d.doc_type, d.doc_number, d.customer_name "
            "FROM document_delivery.deliveries dl "
            "LEFT JOIN document_delivery.documents d ON dl.document_id=d.id "
            "WHERE dl.tenant_id=%s ORDER BY dl.created_at DESC LIMIT %s",
            (tenant["id"], min(limit, 200)),
        )
        return [cast(JSONObject, dict(r)) for r in cur.fetchall()]


def _count_scalar(cur: PgCursor, sql: str, params: tuple[Any, ...] | list[Any]) -> int:
    """Execute a COUNT query and return a safe int (0 if no row)."""
    cur.execute(sql, params)
    row = cur.fetchone()
    if not row:
        return 0
    # RealDictCursor rows use named keys; plain tuples use index 0.
    if isinstance(row, dict):
        val = row.get("count", row.get("total", 0))
    else:
        val = row[0]
    return int(val or 0)


@app.get("/analytics")
def analytics(x_tenant_key: str | None = Header(default=None)) -> AnalyticsPayload:
    """Aggregate document, view, and delivery analytics for a tenant.

    Args:
        x_tenant_key: Tenant API key header.

    Returns:
        Totals, breakdowns by type/channel, and recent documents.
    """
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        tenant = _tenant_from_key(cast(PgCursor, cur), x_tenant_key)
        if not tenant:
            raise HTTPException(401, "Invalid or missing X-Tenant-Key header")
        tid = str(tenant["id"])
        pc = cast(PgCursor, cur)
        total_docs = _count_scalar(
            pc,
            "SELECT COUNT(*) AS count FROM document_delivery.documents WHERE tenant_id=%s",
            (tid,),
        )
        total_views = _count_scalar(
            pc,
            "SELECT COUNT(*) AS count FROM document_delivery.document_views dv "
            "JOIN document_delivery.documents d ON dv.document_id=d.id WHERE d.tenant_id=%s",
            (tid,),
        )
        total_deliveries = _count_scalar(
            pc,
            "SELECT COUNT(*) AS count FROM document_delivery.deliveries WHERE tenant_id=%s",
            (tid,),
        )
        cur.execute(
            "SELECT doc_type, COUNT(*) AS count FROM document_delivery.documents "
            "WHERE tenant_id=%s GROUP BY doc_type",
            (tid,),
        )
        by_type = [cast(JSONObject, dict(r)) for r in cur.fetchall()]
        cur.execute(
            "SELECT channel,status,COUNT(*) AS count FROM document_delivery.deliveries "
            "WHERE tenant_id=%s GROUP BY channel,status",
            (tid,),
        )
        by_channel = [cast(JSONObject, dict(r)) for r in cur.fetchall()]
        cur.execute(
            "SELECT id,doc_type,doc_number,customer_name,total,created_at "
            "FROM document_delivery.documents "
            "WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 10",
            (tid,),
        )
        return {
            "total_documents": total_docs,
            "total_views": total_views,
            "total_deliveries": total_deliveries,
            "by_type": by_type,
            "by_channel": by_channel,
            "recent_documents": [cast(JSONObject, dict(r)) for r in cur.fetchall()],
        }


@app.get("/settings")
def settings_get(x_tenant_key: str | None = Header(default=None)) -> JSONObject:
    """Return safe tenant settings (secrets redacted to booleans).

    Strips ``api_key``, ``resend_api_key``, and ``twilio_token``; exposes
    ``has_resend`` / ``has_twilio`` instead.

    Args:
        x_tenant_key: Tenant API key header.

    Returns:
        Safe tenant settings dict.
    """
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        tenant = _tenant_from_key(cast(PgCursor, cur), x_tenant_key)
        if not tenant:
            raise HTTPException(401, "Invalid or missing X-Tenant-Key header")
        safe = cast(JSONObject, dict(tenant))
        safe.pop("api_key", None)
        resend = safe.pop("resend_api_key", "")
        twilio_token = safe.pop("twilio_token", "")
        safe["has_resend"] = bool(resend)
        safe["has_twilio"] = bool(safe.get("twilio_sid") and twilio_token)
        return safe


@app.put("/settings")
async def settings_put(
    request: Request,
    x_tenant_key: str | None = Header(default=None),
) -> OkResult:
    """Update allowlisted tenant settings as the authenticated tenant.

    Args:
        request: JSON body of fields to update.
        x_tenant_key: Tenant API key header.

    Returns:
        ``{"ok": true}`` on success.

    Raises:
        HTTPException: 400/401 as appropriate.
    """
    raw_body: Any = await request.json()
    if not isinstance(raw_body, dict):
        raise HTTPException(400, "JSON object body required")
    body = cast(JSONObject, raw_body)
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        tenant = _tenant_from_key(cast(PgCursor, cur), x_tenant_key)
        if not tenant:
            raise HTTPException(401, "Invalid or missing X-Tenant-Key header")
        if not _safe_update(cast(PgCursor, cur), str(tenant["id"]), body):
            raise HTTPException(400, "No valid fields")
        return {"ok": True}


@app.post("/deliver/email-pdf")
@app.get("/deliver/email-pdf")
def email_pdf(
    token: str | None = None,
    document_id: str | None = None,
    x_tenant_key: str | None = Header(default=None),
) -> Response:
    """Export printable document HTML for email/PDF workflows.

    Legacy CF route produced a PDF; without a PDF renderer this returns the
    stored HTML as an attachment (``Content-Disposition``) so clients can print
    or convert offline.

    Args:
        token: Optional public view token (no tenant key required).
        document_id: Optional document id (requires ``X-Tenant-Key``).
        x_tenant_key: Tenant key when resolving by ``document_id``.

    Returns:
        HTML attachment Response.

    Raises:
        HTTPException: 401/404 when lookup fails.
    """
    with _db() as con, con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if token:
            cur.execute(
                "SELECT * FROM document_delivery.documents WHERE view_token=%s",
                (token,),
            )
        else:
            tenant = _tenant_from_key(cast(PgCursor, cur), x_tenant_key)
            if not tenant:
                raise HTTPException(401, "Invalid or missing X-Tenant-Key header")
            cur.execute(
                "SELECT * FROM document_delivery.documents WHERE id=%s AND tenant_id=%s",
                (document_id, tenant["id"]),
            )
        raw_doc = cur.fetchone()
        if not raw_doc:
            raise HTTPException(404, "Document not found")
        doc = cast(DocumentRow, dict(raw_doc))
        body = str(doc.get("html_body") or "").encode("utf-8")
        # The legacy route creates a PDF. This service returns a printable HTML payload
        # when no PDF renderer is configured, preserving delivery semantics without CF.
        doc_number = str(doc.get("doc_number") or "document")
        return Response(
            body,
            media_type="text/html",
            headers={
                "Content-Disposition": f"attachment; filename={doc_number}.html"
            },
        )
