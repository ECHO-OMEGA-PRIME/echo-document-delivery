from __future__ import annotations

import os


# App imports intentionally fail closed when infrastructure credentials are
# absent. Pure unit/documentation tests use explicit, process-local fixtures so
# they exercise the module without weakening production configuration.
os.environ.setdefault("PGPASSWORD", "test-db-password")
os.environ.setdefault("MINIO_ACCESS_KEY", "test-minio-access")
os.environ.setdefault("MINIO_SECRET_KEY", "test-minio-secret")
