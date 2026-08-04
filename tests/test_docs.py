"""Doc quality + pure-helper tests for echo-document-delivery (no live server)."""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app.py"

# Public route handlers / models that must ship with docstrings
PUBLIC_NAMES = {
    "root",
    "health",
    "tenants_create",
    "tenants_list",
    "tenants_get",
    "tenants_update",
    "documents_generate",
    "view_document",
    "documents_list",
    "documents_get",
    "deliver_email",
    "deliver_sms",
    "deliveries",
    "analytics",
    "settings_get",
    "settings_put",
    "email_pdf",
    "TenantCreate",
    "DocumentGenerate",
    "EmailDelivery",
    "SmsDelivery",
}


def _load_app_module():
    """Import app.py without requiring a running server."""
    name = "echo_document_delivery_app_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, APP_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_module_has_version_and_title():
    mod = _load_app_module()
    ver = getattr(mod, "__version__", "")
    assert ver.startswith("1.0."), ver
    assert (
        "docs" in ver
        or "typed" in ver
        or ver.startswith("1.0.0")
        or ver.startswith("1.0.1")
        or ver.startswith("1.0.2")
        or ver.startswith("1.0.3")
    ), ver
    assert mod.app.title == "Echo Document Delivery"
    assert mod.app.version == mod.__version__


def test_every_public_symbol_has_docstring():
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    found: dict[str, str | None] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in PUBLIC_NAMES:
                found[node.name] = ast.get_docstring(node)
    missing = sorted(PUBLIC_NAMES - set(found))
    assert not missing, f"public symbols missing from AST: {missing}"
    undoc = sorted(n for n, d in found.items() if not d or len(d.strip()) < 20)
    assert not undoc, f"public symbols without showroom docstrings: {undoc}"


def test_all_top_level_functions_have_docstrings():
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"))
    undoc = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not ast.get_docstring(node):
                undoc.append(node.name)
    assert undoc == [], f"undocumented top-level defs: {undoc}"


def test_readme_has_required_sections():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for section in (
        "## Purpose",
        "## Run / verify in 5 minutes",
        "## Config / environment",
        "## HTTP endpoints",
        "X-Tenant-Key",
        "X-Admin-Key",
        "/documents/generate",
        "/health",
        "echo.documentdelivery",
        "1.0.",
        "mypy",
        "1.0.3-docs",
    ):
        assert section in text, f"README missing section/snippet: {section}"


def test_types_module_public_classes_have_docstrings():
    types_path = ROOT / "document_delivery_types.py"
    tree = ast.parse(types_path.read_text(encoding="utf-8"))
    undoc = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            doc = ast.get_docstring(node)
            if not doc or len(doc.strip()) < 20:
                undoc.append(node.name)
    assert undoc == [], f"undocumented public TypedDicts: {undoc}"


def test_money_and_company_helpers():
    mod = _load_app_module()
    assert mod._money(1234.5) == "1,234.50"
    assert mod._money("bad") == "0.00"
    tenant = {
        "company_name": "Acme",
        "company_phone": "1",
        "company_email": "a@b.c",
        "company_tagline": "tag",
        "company_website": "https://x",
        "company_city": "Midland",
        "primary_color": "#111111",
        "accent_color": "#222222",
        "logo_url": "",
    }
    co = mod._company(tenant)
    assert co["name"] == "Acme"
    assert co["primaryColor"] == "#111111"
    assert len(mod._hash_ip("127.0.0.1")) == 16
    assert mod._uid("doc").startswith("doc_")


def test_document_html_escapes_and_renders():
    mod = _load_app_module()
    html = mod._document_html(
        {
            "type": "INVOICE",
            "docNumber": "INV-1",
            "date": "2026-07-13",
            "dueDate": "",
            "serviceDate": "",
            "customerName": "<script>x</script>",
            "customerAddress": "",
            "customerEmail": "c@x",
            "customerPhone": "",
            "jobTitle": "Job",
            "serviceType": "",
            "items": [{"description": "Labor", "quantity": 1, "rate": 10, "amount": 10}],
            "subtotal": 10,
            "taxAmount": 0,
            "amountPaid": 0,
            "total": 10,
            "currency": "USD",
            "scopeItems": ["scope A"],
            "notes": "note",
            "company": {
                "name": "Co",
                "tagline": "t",
                "primaryColor": "#0D2847",
                "accentColor": "#FFD700",
                "city": "X",
                "phone": "1",
                "email": "e@x",
            },
        }
    )
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert "INV-1" in html
    assert "Labor" in html
    assert "scope A" in html


def test_route_table_covers_migration_surface():
    mod = _load_app_module()
    paths = {getattr(r, "path", None) for r in mod.app.routes}
    for needed in (
        "/",
        "/health",
        "/tenants",
        "/tenants/{tenant_id}",
        "/documents/generate",
        "/view/{token}",
        "/documents",
        "/documents/{doc_id}",
        "/deliver/email",
        "/deliver/sms",
        "/deliveries",
        "/analytics",
        "/settings",
        "/deliver/email-pdf",
    ):
        assert needed in paths, f"missing route: {needed}"
