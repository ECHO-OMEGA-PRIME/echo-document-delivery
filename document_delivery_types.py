"""Shared TypedDict aliases and type aliases for echo-document-delivery (strict mypy).

Domain row shapes (RealDictCursor), PG connect kwargs, API response fragments,
and branding/document HTML template shapes used by ``app.py``.

All public TypedDict classes carry showroom docstrings (Args-style field notes).
"""
from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

# --------------------------------------------------------------------------- #
# Domain / status literals
# --------------------------------------------------------------------------- #

ServiceName = Literal["echo-document-delivery"]
HealthStatus = Literal["healthy", "degraded"]
RootStatus = Literal["operational"]
DeliveryChannel = Literal["email", "sms"]
DeliveryStatus = Literal["sent", "failed"]
DocType = str  # invoice / estimate / receipt / proposal / … (free-form uppercased)
JSONObject = dict[str, Any]
JSONList = list[Any]
JSONValue = Any


# --------------------------------------------------------------------------- #
# Postgres connect kwargs
# --------------------------------------------------------------------------- #


class PgConnectKwargs(TypedDict):
    """Keyword arguments accepted by ``psycopg2.connect`` for fleet Postgres.

    Fields:
        host: Postgres host (``PGHOST``, default ``localhost``).
        user: Role name (``PGUSER``, default ``echo``).
        password: Role password (``PGPASSWORD``).
        dbname: Database name (``PGDATABASE``, default ``echo``).
    """

    host: str
    user: str
    password: str
    dbname: str


# --------------------------------------------------------------------------- #
# Domain rows (RealDictCursor shapes; total=False for partial SELECTs)
# --------------------------------------------------------------------------- #


class TenantRow(TypedDict, total=False):
    """Postgres row shape for ``document_delivery.tenants``.

    Includes branding, delivery provider credentials, and lifecycle timestamps.
    Partial SELECTs are valid because ``total=False``.
    """

    id: str
    name: str
    slug: str
    company_name: str
    company_phone: str
    company_email: str
    company_tagline: str
    company_website: str
    company_city: str
    primary_color: str
    accent_color: str
    logo_url: str
    email_from: str
    sms_from: str
    api_key: str
    resend_api_key: str
    twilio_sid: str
    twilio_token: str
    twilio_phone: str
    default_payment_terms: str
    auto_email_on_generate: bool
    include_view_link: bool
    created_at: Any
    updated_at: Any


class DocumentRow(TypedDict, total=False):
    """Postgres row shape for ``document_delivery.documents``.

    Optional joined fields (``tenant_slug``, ``view_count``, ``last_viewed_at``,
    ``deliveries``) appear on list/detail endpoints only.
    """

    id: str
    tenant_id: str
    doc_type: str
    doc_number: str
    source_id: str
    customer_name: str
    customer_email: str
    customer_phone: str
    customer_address: str
    object_key: str
    view_token: str
    total: Any
    currency: str
    metadata: Any
    html_body: str
    created_at: Any
    tenant_slug: NotRequired[str]
    view_count: NotRequired[int]
    last_viewed_at: NotRequired[Any]
    deliveries: NotRequired[list[JSONObject]]


class DeliveryRow(TypedDict, total=False):
    """Postgres row shape for ``document_delivery.deliveries`` audit log.

    Optional document fields are present when the list endpoint JOINs documents.
    """

    id: str
    document_id: str
    tenant_id: str
    channel: str
    status: str
    delivered_to: str
    delivered_at: Any
    error_message: str
    provider_id: str
    created_at: Any
    doc_type: NotRequired[str]
    doc_number: NotRequired[str]
    customer_name: NotRequired[str]


class DocumentViewRow(TypedDict, total=False):
    """Postgres row shape for ``document_delivery.document_views``.

    IP is stored as a short hash only (privacy); user-agent is truncated at insert.
    """

    id: str
    document_id: str
    viewed_at: Any
    ip_hash: str | None
    user_agent: str | None


class CountRow(TypedDict, total=False):
    """Aggregate scalar count/total result from SQL ``COUNT(*)`` queries."""

    count: Any
    total: Any


class TypeCountRow(TypedDict, total=False):
    """Per-document-type aggregate used by analytics ``by_type``."""

    doc_type: str
    count: Any


class ChannelCountRow(TypedDict, total=False):
    """Per-channel/status aggregate used by analytics ``by_channel``."""

    channel: str
    status: str
    count: Any


# --------------------------------------------------------------------------- #
# Branding / HTML template shapes
# --------------------------------------------------------------------------- #


class CompanyBrand(TypedDict):
    """Tenant branding block embedded into generated document HTML.

    CamelCase keys match the HTML template contract used by ``_document_html``.
    """

    name: str
    phone: str
    email: str
    tagline: str
    website: str
    city: str
    primaryColor: str
    accentColor: str
    logoUrl: str


class LineItem(TypedDict, total=False):
    """Flexible line-item dict accepted by generate and HTML rendering.

    Supports common aliases (``qty``/``quantity``, ``rate``/``price``/``unit_price``).
    """

    description: str
    name: str
    quantity: Any
    qty: Any
    rate: Any
    price: Any
    unit_price: Any
    amount: Any


class DocumentHtmlData(TypedDict, total=False):
    """Structured payload passed into ``_document_html`` for branded rendering.

    Field names are camelCase to mirror the original CF worker template contract.
    """

    type: str
    docNumber: str
    date: str
    dueDate: str
    serviceDate: str
    customerName: str
    customerEmail: str
    customerPhone: str
    customerAddress: str
    jobTitle: str
    serviceType: str
    items: list[LineItem] | list[JSONObject]
    subtotal: Any
    taxRate: Any
    taxAmount: Any
    total: Any
    amountPaid: Any
    currency: str
    scopeItems: list[str]
    notes: str
    paymentTerms: str
    company: CompanyBrand


# --------------------------------------------------------------------------- #
# API response fragments
# --------------------------------------------------------------------------- #


class RootPayload(TypedDict):
    """JSON body returned by ``GET /`` service identity probe."""

    service: ServiceName
    version: str
    status: RootStatus
    docs: str


class HealthPayload(TypedDict):
    """JSON body returned by ``GET /health`` liveness + dependency probe."""

    ok: bool
    status: HealthStatus
    service: ServiceName
    version: str
    db: bool
    minio: bool
    bucket: str
    timestamp: str


class TenantCreateResult(TypedDict):
    """JSON body returned by successful ``POST /tenants`` (admin)."""

    ok: Literal[True]
    tenant_id: str
    api_key: str
    slug: str


class OkResult(TypedDict):
    """Minimal success envelope used by update endpoints."""

    ok: Literal[True]


class DocumentGenerateResult(TypedDict):
    """JSON body returned by successful ``POST /documents/generate``."""

    ok: Literal[True]
    document_id: str
    doc_number: str
    object_key: str
    r2_key: str
    view_url: str
    view_token: str


class DocumentListResult(TypedDict):
    """JSON body returned by ``GET /documents`` (tenant-scoped list)."""

    documents: list[JSONObject]
    total: int


class DeliveryResult(TypedDict, total=False):
    """JSON body returned by email/SMS delivery endpoints.

    ``ok`` is True on provider acceptance; failures include ``error`` and still
    write a delivery audit row with ``status=failed``.
    """

    ok: bool
    delivery_id: str
    error: str
    email_id: Any
    status: int
    sms_sid: Any


class AnalyticsPayload(TypedDict):
    """JSON body returned by ``GET /analytics`` for a tenant dashboard."""

    total_documents: int
    total_views: int
    total_deliveries: int
    by_type: list[JSONObject]
    by_channel: list[JSONObject]
    recent_documents: list[JSONObject]


class TenantSummary(TypedDict, total=False):
    """Non-secret tenant summary fields for admin list endpoints."""

    id: str
    name: str
    slug: str
    company_name: str
    company_email: str
    created_at: Any


class SettingsPayload(TypedDict, total=False):
    """Safe tenant settings response (secrets redacted to boolean flags).

    ``has_resend`` / ``has_twilio`` replace raw API keys in the JSON surface.
    """

    id: str
    name: str
    slug: str
    company_name: str
    company_phone: str
    company_email: str
    company_tagline: str
    company_website: str
    company_city: str
    primary_color: str
    accent_color: str
    logo_url: str
    email_from: str
    sms_from: str
    twilio_sid: str
    twilio_phone: str
    default_payment_terms: str
    auto_email_on_generate: bool
    include_view_link: bool
    created_at: Any
    updated_at: Any
    has_resend: bool
    has_twilio: bool
