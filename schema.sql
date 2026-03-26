-- Echo Document Delivery — Universal Multi-Tenant Schema

CREATE TABLE IF NOT EXISTS tenants (
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
  email_provider TEXT DEFAULT 'resend',
  email_from TEXT DEFAULT '',
  sms_provider TEXT DEFAULT 'twilio',
  sms_from TEXT DEFAULT '',
  api_key TEXT NOT NULL,
  resend_api_key TEXT DEFAULT '',
  twilio_sid TEXT DEFAULT '',
  twilio_token TEXT DEFAULT '',
  twilio_phone TEXT DEFAULT '',
  default_payment_terms TEXT DEFAULT 'net_30',
  auto_email_on_generate INTEGER DEFAULT 0,
  include_view_link INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  doc_type TEXT NOT NULL DEFAULT 'INVOICE',
  doc_number TEXT,
  source_id TEXT,
  customer_name TEXT,
  customer_email TEXT,
  customer_phone TEXT,
  customer_address TEXT,
  r2_key TEXT,
  view_token TEXT UNIQUE,
  total REAL DEFAULT 0,
  currency TEXT DEFAULT 'USD',
  metadata TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deliveries (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id),
  tenant_id TEXT NOT NULL REFERENCES tenants(id),
  channel TEXT NOT NULL,
  status TEXT DEFAULT 'pending',
  delivered_to TEXT,
  delivered_at TEXT,
  error_message TEXT,
  provider_id TEXT,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS document_views (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id),
  viewed_at TEXT DEFAULT (datetime('now')),
  ip_hash TEXT,
  user_agent TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_documents_tenant ON documents(tenant_id);
CREATE INDEX IF NOT EXISTS idx_documents_token ON documents(view_token);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents(doc_type);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_document ON deliveries(document_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_tenant ON deliveries(tenant_id);
CREATE INDEX IF NOT EXISTS idx_document_views_doc ON document_views(document_id);
CREATE INDEX IF NOT EXISTS idx_tenants_slug ON tenants(slug);
CREATE INDEX IF NOT EXISTS idx_tenants_api_key ON tenants(api_key);
