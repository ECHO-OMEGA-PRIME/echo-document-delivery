from __future__ import annotations

import ast
import importlib
import os
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from credential_config import required_env


_SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL|AUTH)", re.IGNORECASE)


class RequiredEnvironmentTests(unittest.TestCase):
    def test_missing_and_blank_values_fail_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "REQUIRED_TEST_SECRET"):
                required_env("REQUIRED_TEST_SECRET")
        with patch.dict(os.environ, {"REQUIRED_TEST_SECRET": "   "}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "REQUIRED_TEST_SECRET"):
                required_env("REQUIRED_TEST_SECRET")

    def test_configured_value_is_returned_unchanged(self) -> None:
        with patch.dict(os.environ, {"REQUIRED_TEST_SECRET": "fixture-value"}, clear=True):
            self.assertEqual(required_env("REQUIRED_TEST_SECRET"), "fixture-value")

    def test_runtime_has_no_nonempty_secret_env_default(self) -> None:
        findings: list[str] = []
        root = Path(__file__).resolve().parents[1]
        for source in sorted(root.glob("*.py")):
            if source.name.startswith("test"):
                continue
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {"get", "getenv"} or len(node.args) < 2:
                    continue
                name, default = node.args[0], node.args[1]
                if (
                    isinstance(name, ast.Constant)
                    and isinstance(name.value, str)
                    and _SECRET_NAME.search(name.value)
                    and isinstance(default, ast.Constant)
                    and isinstance(default.value, str)
                    and default.value
                ):
                    findings.append(f"{source.name}:{node.lineno}:{name.value}")
        self.assertEqual(findings, [], "non-empty credential defaults remain")

    def test_admin_auth_fails_closed_and_accepts_only_the_configured_key(self) -> None:
        base_env = {
            "PGPASSWORD": "test-db-password",
            "MINIO_ACCESS_KEY": "test-minio-access",
            "MINIO_SECRET_KEY": "test-minio-secret",
        }
        with patch.dict(os.environ, base_env, clear=True):
            sys.modules.pop("app", None)
            app = importlib.import_module("app")
            with self.assertRaises(HTTPException) as missing:
                app._require_admin(None, None)
            self.assertEqual(missing.exception.status_code, 503)

        configured_env = {**base_env, "ADMIN_API_KEY": "test-admin-key"}
        with patch.dict(os.environ, configured_env, clear=True):
            app = importlib.reload(app)
            with self.assertRaises(HTTPException) as wrong:
                app._require_admin("wrong-key", None)
            self.assertEqual(wrong.exception.status_code, 401)
            self.assertIsNone(app._require_admin("test-admin-key", None))


if __name__ == "__main__":
    unittest.main()
