#!/usr/bin/env bash
# Echo Document Delivery — 5-minute verify (static + optional live HTTP smoke).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
FAIL=0
SPAWNED=0
PID=""
TMP_DIR="$(mktemp -d -t echo-document-delivery-verify.XXXXXX)"
chmod 700 "$TMP_DIR"

pass() { echo "  PASS  $*"; }
fail() { echo "  FAIL  $*"; FAIL=$((FAIL + 1)); }

cleanup() {
  if [[ "$SPAWNED" -eq 1 && -n "${PID:-}" ]]; then
    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true
  fi
  rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT

echo "== echo-document-delivery verify =="
echo "ROOT=$ROOT"

for name in PGPASSWORD MINIO_ACCESS_KEY MINIO_SECRET_KEY ADMIN_API_KEY; do
  if [[ -z "${!name:-}" ]]; then
    fail "$name must be configured explicitly"
  fi
done
if [[ "$FAIL" -ne 0 ]]; then
  echo "VERIFY FAILED ($FAIL missing configuration values)"
  exit 1
fi

echo "-- py_compile --"
if python3 -m py_compile "$ROOT/app.py"; then
  pass "py_compile app.py"
else
  fail "py_compile app.py"
fi

echo "-- unit tests --"
if (cd "$ROOT" && python3 -m pytest -q tests/ 2>&1); then
  pass "pytest"
else
  fail "pytest"
fi

echo "-- import / version --"
if (cd "$ROOT" && python3 -c 'import app; assert app.__version__.startswith("1.0."); print(app.__version__)'); then
  pass "import app + __version__"
else
  fail "import app"
fi

# Live base: env override, or spawn ephemeral uvicorn on free port
BASE="${ECHO_DOCUMENT_DELIVERY_BASE:-}"
if [[ -z "$BASE" ]]; then
  # Prefer existing live service only if it really is document-delivery
  if curl -sf -m 2 http://127.0.0.1:8116/health 2>/dev/null | grep -q 'echo-document-delivery'; then
    BASE="http://127.0.0.1:8116"
    echo "Using live unit on :8116"
  else
    PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
    export PGHOST="${PGHOST:-localhost}"
    export PGUSER="${PGUSER:-echo}"
    export PGPASSWORD
    export PGDATABASE="${PGDATABASE:-echo}"
    export MINIO_ACCESS_KEY
    export MINIO_SECRET_KEY
    export ADMIN_API_KEY
    echo "Spawning uvicorn on 127.0.0.1:$PORT"
    (cd "$ROOT" && python3 -m uvicorn app:app --host 127.0.0.1 --port "$PORT" --log-level warning) &
    PID=$!
    SPAWNED=1
    BASE="http://127.0.0.1:$PORT"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      if curl -sf -m 1 "$BASE/health" >/dev/null 2>&1; then
        break
      fi
      sleep 0.4
    done
  fi
fi

echo "BASE=$BASE"
ADMIN_KEY="$ADMIN_API_KEY"

echo "-- health --"
H="$(curl -sS -m 5 "$BASE/health" || true)"
echo "$H" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d.get("service")=="echo-document-delivery"; assert "version" in d; print("status=", d.get("status"), "db=", d.get("db"), "version=", d.get("version"))' \
  && pass "GET /health" || fail "GET /health: $H"

echo "-- root --"
curl -sS -m 5 "$BASE/" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d.get("service")=="echo-document-delivery" and d.get("status")=="operational"' \
  && pass "GET /" || fail "GET /"

# Full CRUD smoke only when DB is healthy
DB_OK="$(echo "$H" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("db") is True)' 2>/dev/null || echo False)"
if [[ "$DB_OK" == "True" ]]; then
  echo "-- tenant create + generate + view --"
  TEN="$(curl -sS -m 8 -X POST "$BASE/tenants" \
    -H "Content-Type: application/json" -H "X-Admin-Key: $ADMIN_KEY" \
    -d "{\"name\":\"Verify Co\",\"slug\":\"verify-co-$(date +%s)\",\"company_name\":\"Verify Co LLC\",\"company_email\":\"verify@example.com\"}" || true)"
  echo "$TEN" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d.get("ok") and d.get("api_key") and d.get("tenant_id"); print(d["api_key"])' > "$TMP_DIR/tenant_key" \
    && chmod 600 "$TMP_DIR/tenant_key" && pass "POST /tenants" || fail "POST /tenants"
  TKEY="$(cat "$TMP_DIR/tenant_key" 2>/dev/null || true)"
  if [[ -n "$TKEY" ]]; then
    GEN="$(curl -sS -m 10 -X POST "$BASE/documents/generate" \
      -H "Content-Type: application/json" -H "X-Tenant-Key: $TKEY" \
      -d '{"doc_type":"invoice","doc_number":"INV-VERIFY-1","customer_name":"Acme","items":[{"description":"Labor","quantity":1,"rate":50,"amount":50}],"subtotal":50,"total":50,"currency":"USD"}' || true)"
    echo "$GEN" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert d.get("ok") and d.get("view_token"); print(d["view_token"])' > "$TMP_DIR/view_token" \
      && chmod 600 "$TMP_DIR/view_token" && pass "POST /documents/generate" || fail "POST /documents/generate"
    TOK="$(cat "$TMP_DIR/view_token" 2>/dev/null || true)"
    if [[ -n "$TOK" ]]; then
      VIEW="$(curl -sS -m 5 -o "$TMP_DIR/view.html" -w '%{http_code}' "$BASE/view/$TOK" || true)"
      [[ "$VIEW" == "200" ]] && grep -q 'Acme\|Labor\|INV-VERIFY' "$TMP_DIR/view.html" \
        && pass "GET /view/{token}" || fail "GET /view/{token}: http=$VIEW"
    fi
    AN="$(curl -sS -m 5 -H "X-Tenant-Key: $TKEY" "$BASE/analytics" || true)"
    echo "$AN" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert "total_documents" in d and d["total_documents"]>=1' \
      && pass "GET /analytics" || fail "GET /analytics: $AN"
    ST="$(curl -sS -m 5 -H "X-Tenant-Key: $TKEY" "$BASE/settings" || true)"
    echo "$ST" | python3 -c 'import sys,json; d=json.load(sys.stdin); assert "company_name" in d and "api_key" not in d' \
      && pass "GET /settings" || fail "GET /settings: $ST"
  fi
else
  echo "  SKIP  DB not healthy — tenant/generate smoke skipped (static checks still count)"
fi

echo "-- unit status (best-effort) --"
if systemctl is-active --quiet echo-document-delivery 2>/dev/null; then
  pass "systemd unit active"
else
  echo "  SKIP  systemd unit not active on this host (ok for ephemeral uvicorn)"
fi

if [[ "$FAIL" -eq 0 ]]; then
  echo "VERIFY OK"
  exit 0
fi
echo "VERIFY FAILED ($FAIL checks)"
exit 1
