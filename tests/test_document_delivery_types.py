"""Unit tests for echo-document-delivery type contracts and pure helpers."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from typing import get_args, get_type_hints

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_module(name: str, rel: str):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestDocumentDeliveryTypesModule(unittest.TestCase):
    def test_types_importable(self) -> None:
        import document_delivery_types as ddt

        for name in (
            "PgConnectKwargs",
            "TenantRow",
            "DocumentRow",
            "DeliveryRow",
            "CompanyBrand",
            "DocumentHtmlData",
            "HealthPayload",
            "RootPayload",
            "AnalyticsPayload",
            "DocumentGenerateResult",
            "DeliveryResult",
        ):
            self.assertTrue(hasattr(ddt, name), name)

    def test_service_name_literal(self) -> None:
        import document_delivery_types as ddt

        self.assertEqual(set(get_args(ddt.ServiceName)), {"echo-document-delivery"})

    def test_health_status_literal(self) -> None:
        import document_delivery_types as ddt

        self.assertEqual(set(get_args(ddt.HealthStatus)), {"healthy", "degraded"})

    def test_delivery_channel_literal(self) -> None:
        import document_delivery_types as ddt

        self.assertEqual(set(get_args(ddt.DeliveryChannel)), {"email", "sms"})

    def test_pg_connect_kwargs_keys(self) -> None:
        import document_delivery_types as ddt

        hints = get_type_hints(ddt.PgConnectKwargs)
        self.assertEqual(set(hints), {"host", "user", "password", "dbname"})


class TestAppTypedSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load_module("echo_document_delivery_app_types_test", "app.py")

    def test_version_typed_suffix(self) -> None:
        ver = self.mod.__version__
        self.assertTrue(ver.startswith("1.0."), ver)
        self.assertTrue(
            "typed" in ver or "docs" in ver or ver.startswith("1.0."),
            ver,
        )

    def test_helpers_return_types_runtime(self) -> None:
        self.assertEqual(self.mod._money(1234.5), "1,234.50")
        self.assertEqual(self.mod._money("bad"), "0.00")
        co = self.mod._company(
            {
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
        )
        self.assertEqual(co["name"], "Acme")
        self.assertEqual(co["primaryColor"], "#111111")
        self.assertEqual(len(self.mod._hash_ip("127.0.0.1")), 16)

    def test_count_scalar_none_safe(self) -> None:
        class FakeCur:
            def __init__(self, row):
                self._row = row

            def execute(self, *a, **k):
                return None

            def fetchone(self):
                return self._row

        self.assertEqual(self.mod._count_scalar(FakeCur(None), "SELECT 1", ()), 0)
        self.assertEqual(self.mod._count_scalar(FakeCur({"count": 3}), "SELECT 1", ()), 3)
        self.assertEqual(self.mod._count_scalar(FakeCur((7,)), "SELECT 1", ()), 7)

    def test_annotations_on_public_routes(self) -> None:
        hints = get_type_hints(self.mod.root)
        self.assertIn("return", hints)
        health_hints = get_type_hints(self.mod.health)
        self.assertIn("return", health_hints)
        analytics_hints = get_type_hints(self.mod.analytics)
        self.assertIn("return", analytics_hints)


if __name__ == "__main__":
    unittest.main()
