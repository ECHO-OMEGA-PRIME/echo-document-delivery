# Echo Document Delivery

FastAPI service for generating, viewing, and delivering tenant-branded
documents. The canonical Python runtime is version **1.0.3-docs**; the
repository also retains the original Cloudflare Worker source for migration
history.

## Purpose

The service turns structured invoice, estimate, receipt, and service-document
payloads into branded HTML, persists document metadata in PostgreSQL, stores
delivery assets in MinIO, and records email/SMS delivery attempts. Tenant data
is separated by an **X-Tenant-Key**; administrative tenant-management routes
use **X-Admin-Key** or **X-Echo-API-Key**.

## Run / verify in 5 minutes

Prerequisites are Python 3.11+, PostgreSQL, and MinIO-compatible object storage.

~~~bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
export PGHOST=127.0.0.1 PGUSER=echo PGDATABASE=echo
export PGPASSWORD='<configured database credential>'
export MINIO_ACCESS_KEY='<configured access key>'
export MINIO_SECRET_KEY='<configured secret key>'
export ADMIN_API_KEY='<configured admin key>'
python -m pytest -q
mypy app.py document_delivery_types.py
uvicorn app:app --host 127.0.0.1 --port 8116
~~~

For an existing configured environment, **./verify.sh** runs compile, unit,
health, route, and optional database-backed smoke checks. Never put real
credentials in the repository or command history.

## Config / environment

Required at startup:

| Variable | Purpose |
| --- | --- |
| PGPASSWORD | PostgreSQL authentication |
| MINIO_ACCESS_KEY | MinIO access identifier |
| MINIO_SECRET_KEY | MinIO secret |

**ADMIN_API_KEY** is required for administrative requests. If it is absent,
the service remains bootable for health diagnostics but every admin operation
returns HTTP 503. Wrong or missing presented keys return HTTP 401. PostgreSQL
host, port, user, and database names may use their documented non-secret
defaults.

## HTTP endpoints

- **GET /health** — liveness and dependency diagnostics.
- **POST /tenants**, **GET /tenants** — admin-only tenant management.
- **POST /documents/generate** — generate a tenant-scoped document.
- **GET /view/{token}** — public tokenized document view.
- **GET /documents**, **GET /documents/{doc_id}** — tenant-scoped reads.
- **POST /deliver/email**, **POST /deliver/sms** — tenant delivery.
- **GET /deliveries**, **GET /analytics** — tenant reporting.
- **GET /settings**, **PUT /settings** — tenant settings.

OpenAPI documentation is available at **/docs** when the service is running.

## Authentication model

Administrative routes accept **X-Admin-Key** or **X-Echo-API-Key** and compare
the presented value with the configured key using a timing-safe comparison.
Tenant routes require the issued **X-Tenant-Key**. Public view tokens are opaque
and must not be logged.

## SDK integration

Fleet capabilities use the **echo.documentdelivery** namespace and target the
FORGE runtime. Keep capability registration aligned with the route table after
any API change.

## Verification

~~~bash
python -m compileall -q .
python -m pytest -q
mypy app.py document_delivery_types.py
~~~

The test suite verifies API documentation, typed public models, route coverage,
HTML escaping, and fail-closed credential behavior. Version references in docs
and runtime should remain synchronized with **1.0.3-docs**.

See [PYTHON_RUNTIME.md](PYTHON_RUNTIME.md) for canonical-source ownership, required credential configuration, and the staging-first deployment gate.
