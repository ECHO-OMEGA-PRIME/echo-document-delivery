#!/usr/bin/env bash
# Strict type verification for echo-document-delivery (mypy --strict).
set -euo pipefail
cd "$(dirname "$0")"

python3 -m pip install -q --user mypy types-psycopg2 types-requests 2>/dev/null \
  || python3 -m pip install -q mypy types-psycopg2 types-requests 2>/dev/null \
  || true

echo "== mypy --strict (echo-document-delivery) =="
# Isolate from parent SYSTEMS package path confusion via explicit bases + local mypy_path.
python3 -m mypy \
  --config-file mypy.ini \
  --explicit-package-bases \
  document_delivery_types.py \
  app.py

echo "== unit tests =="
if python3 -c "import pytest" 2>/dev/null; then
  PYTHONPATH=. python3 -m pytest tests/ -q --tb=short
else
  PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py' -v
fi

echo "verify_types: OK"
