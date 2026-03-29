/**
 * Echo Document Delivery — Universal Multi-Tenant Document Delivery Worker
 *
 * Any business configures a tenant (branding, email/SMS providers) once.
 * Then generates, views, emails, and SMSes documents forever — zero code changes.
 *
 * Supports: INVOICE, ESTIMATE, WORK_ORDER, RECEIPT, STATEMENT, PROPOSAL, PURCHASE_ORDER
 *
 * Auth: Each tenant gets an API key. Public view endpoints use token-based auth.
 */

import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { PDFDocument, rgb, StandardFonts } from 'pdf-lib';

const ALLOWED_ORIGINS = ['https://echo-ept.com','https://www.echo-ept.com','https://echo-op.com','https://profinishusa.com','https://bgat.echo-op.com'];

interface Env {
  DB: D1Database;
  R2: R2Bucket;
  ADMIN_API_KEY: string;
}

type DocType = 'INVOICE' | 'ESTIMATE' | 'WORK_ORDER' | 'RECEIPT' | 'STATEMENT' | 'PROPOSAL' | 'PURCHASE_ORDER';

interface CompanyConfig {
  name: string;
  phone: string;
  email: string;
  tagline: string;
  website: string;
  city: string;
  primaryColor: string;
  accentColor: string;
  logoUrl: string;
}

interface DocItem {
  description: string;
  qty: number;
  rate: number;
  amount: number;
}

interface GenerateRequest {
  doc_type: DocType;
  doc_number: string;
  date?: string;
  due_date?: string;
  service_date?: string;
  customer_name: string;
  customer_email?: string;
  customer_phone?: string;
  customer_address?: string;
  job_title?: string;
  service_type?: string;
  items: DocItem[];
  subtotal: number;
  tax_rate?: number;
  tax_amount?: number;
  total: number;
  amount_paid?: number;
  scope_items?: string[];
  notes?: string;
  payment_terms?: string;
  source_id?: string;
  currency?: string;
  metadata?: Record<string, any>;
}

const app = new Hono<{ Bindings: Env }>();
const uid = () => crypto.randomUUID().replace(/-/g, '').slice(0, 16);

app.use('*', cors({
  origin: (o) => ALLOWED_ORIGINS.includes(o) ? o : ALLOWED_ORIGINS[0],
  allowMethods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
}));
// Security headers middleware
app.use('*', async (c, next) => {
  await next();
  c.res.headers.set('X-Content-Type-Options', 'nosniff');
  c.res.headers.set('X-Frame-Options', 'DENY');
  c.res.headers.set('X-XSS-Protection', '1; mode=block');
  c.res.headers.set('Referrer-Policy', 'strict-origin-when-cross-origin');
  c.res.headers.set('Strict-Transport-Security', 'max-age=31536000; includeSubDomains');
});


// ─── Rate Limiting (in-memory, 120 req/min per IP on writes) ─────────
const rlMap = new Map<string, { count: number; window: number }>();
app.use('*', async (c, next) => {
  if (c.req.method === 'POST' || c.req.method === 'PUT' || c.req.method === 'DELETE') {
    const ip = c.req.header('CF-Connecting-IP') || 'unknown';
    const window = Math.floor(Date.now() / 60000);
    const entry = rlMap.get(ip);
    if (entry && entry.window === window) {
      if (entry.count >= 120) return c.json({ error: 'Rate limit exceeded', retry_after: 60 }, 429);
      entry.count++;
    } else {
      rlMap.set(ip, { count: 1, window });
    }
    // GC old entries every ~100 requests
    if (rlMap.size > 500) {
      for (const [k, v] of rlMap) { if (v.window < window - 1) rlMap.delete(k); }
    }
  }
  return next();
});

// ─── Root ────────────────────────────────────────────────
app.get('/', (c) => c.json({ service: 'echo-document-delivery', version: '1.0.0', status: 'operational' }));

// ─── Health ──────────────────────────────────────────────
app.get('/health', (c) => c.json({ status: 'healthy', service: 'echo-document-delivery', version: '1.0.0', timestamp: new Date().toISOString() }));

// ─── Tenant Auth Middleware ──────────────────────────────
async function getTenant(db: D1Database, apiKey: string | undefined): Promise<any | null> {
  if (!apiKey) return null;
  return db.prepare('SELECT * FROM tenants WHERE api_key = ?').bind(apiKey).first();
}

function requireTenant(c: any, tenant: any) {
  if (!tenant) return c.json({ error: 'Invalid or missing X-Tenant-Key header' }, 401);
  return null;
}

function requireAdmin(c: any) {
  const key = c.req.header('X-Admin-Key') || c.req.header('X-Echo-API-Key');
  if (!key || key !== c.env.ADMIN_API_KEY) return c.json({ error: 'Admin access required' }, 401);
  return null;
}

// ═══════════════════════════════════════════════════════════
// ─── TENANT MANAGEMENT (admin only) ──────────────────────
// ═══════════════════════════════════════════════════════════

app.post('/tenants', async (c) => {
  const denied = requireAdmin(c);
  if (denied) return denied;
  const b = await c.req.json();
  if (!b.name || !b.slug || !b.company_name) return c.json({ error: 'name, slug, company_name required' }, 400);

  const id = uid();
  const apiKey = 'edd_' + crypto.randomUUID().replace(/-/g, '');

  await c.env.DB.prepare(`INSERT INTO tenants (id, name, slug, company_name, company_phone, company_email, company_tagline, company_website, company_city, primary_color, accent_color, logo_url, email_from, sms_from, api_key, resend_api_key, twilio_sid, twilio_token, twilio_phone, default_payment_terms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`).bind(
    id, b.name, b.slug, b.company_name,
    b.company_phone || '', b.company_email || '', b.company_tagline || '', b.company_website || '', b.company_city || '',
    b.primary_color || '#0D2847', b.accent_color || '#FFD700', b.logo_url || '',
    b.email_from || '', b.sms_from || '', apiKey,
    b.resend_api_key || '', b.twilio_sid || '', b.twilio_token || '', b.twilio_phone || '',
    b.default_payment_terms || 'net_30'
  ).run();

  return c.json({ ok: true, tenant_id: id, api_key: apiKey, slug: b.slug });
});

app.get('/tenants', async (c) => {
  const denied = requireAdmin(c);
  if (denied) return denied;
  const rows = await c.env.DB.prepare('SELECT id, name, slug, company_name, company_email, created_at FROM tenants ORDER BY created_at DESC').all();
  return c.json(rows.results);
});

app.get('/tenants/:id', async (c) => {
  const denied = requireAdmin(c);
  if (denied) return denied;
  const t = await c.env.DB.prepare('SELECT * FROM tenants WHERE id = ?').bind(c.req.param('id')).first();
  if (!t) return c.json({ error: 'Tenant not found' }, 404);
  return c.json(t);
});

app.put('/tenants/:id', async (c) => {
  const denied = requireAdmin(c);
  if (denied) return denied;
  const b = await c.req.json();
  const id = c.req.param('id');
  const fields: string[] = [];
  const values: any[] = [];
  const allowed = ['name', 'company_name', 'company_phone', 'company_email', 'company_tagline', 'company_website', 'company_city', 'primary_color', 'accent_color', 'logo_url', 'email_provider', 'email_from', 'sms_provider', 'sms_from', 'resend_api_key', 'twilio_sid', 'twilio_token', 'twilio_phone', 'default_payment_terms', 'auto_email_on_generate', 'include_view_link'];
  for (const k of allowed) {
    if (k in b) { fields.push(`${k} = ?`); values.push(b[k]); }
  }
  if (fields.length === 0) return c.json({ error: 'No valid fields to update' }, 400);
  fields.push("updated_at = datetime('now')");
  values.push(id);
  await c.env.DB.prepare(`UPDATE tenants SET ${fields.join(', ')} WHERE id = ?`).bind(...values).run();
  return c.json({ ok: true });
});

// ═══════════════════════════════════════════════════════════
// ─── DOCUMENT GENERATION (tenant auth) ───────────────────
// ═══════════════════════════════════════════════════════════

app.post('/documents/generate', async (c) => {
  const tenantKey = c.req.header('X-Tenant-Key');
  const tenant = await getTenant(c.env.DB, tenantKey);
  const denied = requireTenant(c, tenant);
  if (denied) return denied;

  const b: GenerateRequest = await c.req.json();
  if (!b.doc_type || !b.doc_number || !b.customer_name || !b.items?.length) {
    return c.json({ error: 'doc_type, doc_number, customer_name, and items[] required' }, 400);
  }

  const company: CompanyConfig = {
    name: tenant.company_name,
    phone: tenant.company_phone,
    email: tenant.company_email,
    tagline: tenant.company_tagline,
    website: tenant.company_website,
    city: tenant.company_city,
    primaryColor: tenant.primary_color,
    accentColor: tenant.accent_color,
    logoUrl: tenant.logo_url,
  };

  const docData = {
    type: b.doc_type.toUpperCase() as DocType,
    docNumber: b.doc_number,
    date: b.date || new Date().toISOString().split('T')[0],
    dueDate: b.due_date,
    serviceDate: b.service_date,
    customerName: b.customer_name,
    customerEmail: b.customer_email || '',
    customerPhone: b.customer_phone || '',
    customerAddress: b.customer_address || '',
    jobTitle: b.job_title || '',
    serviceType: b.service_type || '',
    items: b.items,
    subtotal: b.subtotal || 0,
    taxRate: b.tax_rate || 0,
    taxAmount: b.tax_amount || 0,
    total: b.total || 0,
    amountPaid: b.amount_paid || 0,
    scopeItems: b.scope_items || [],
    notes: b.notes || '',
    paymentTerms: b.payment_terms || '',
    company,
  };

  const html = buildDocumentHTML(docData);

  // Store to R2 with organized tenant path
  const now = new Date();
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const safeName = (b.customer_name || 'customer').replace(/[^a-zA-Z0-9]/g, '_').slice(0, 30);
  const r2Key = `${tenant.slug}/documents/${year}/${month}/${b.doc_type.toLowerCase()}/${b.doc_number}_${safeName}.html`;
  await c.env.R2.put(r2Key, html, { httpMetadata: { contentType: 'text/html' } });

  // Create document record
  const docId = uid();
  const viewToken = crypto.randomUUID();
  // Store full document data as metadata so PDF endpoint can reconstruct
  const storedMetadata = JSON.stringify({
    items: b.items,
    subtotal: b.subtotal || 0,
    tax_rate: b.tax_rate || 0,
    tax_amount: b.tax_amount || 0,
    amount_paid: b.amount_paid || 0,
    due_date: b.due_date || '',
    service_date: b.service_date || '',
    job_title: b.job_title || '',
    service_type: b.service_type || '',
    notes: b.notes || '',
    payment_terms: b.payment_terms || '',
    scope_items: b.scope_items || [],
    ...(b.metadata || {}),
  });
  await c.env.DB.prepare(
    `INSERT INTO documents (id, tenant_id, doc_type, doc_number, source_id, customer_name, customer_email, customer_phone, customer_address, r2_key, view_token, total, currency, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))`
  ).bind(docId, tenant.id, b.doc_type.toUpperCase(), b.doc_number, b.source_id || '', b.customer_name, b.customer_email || '', b.customer_phone || '', b.customer_address || '', r2Key, viewToken, b.total || 0, b.currency || 'USD', storedMetadata).run();

  const workerUrl = new URL(c.req.url).origin;
  const viewUrl = `${workerUrl}/view/${viewToken}`;

  // Auto-email if tenant configured it
  if (tenant.auto_email_on_generate && b.customer_email) {
    try {
      await sendEmailDelivery(c.env, tenant, company, docId, b.customer_email, `${docData.type.replace('_', ' ')} ${b.doc_number} from ${company.name}`, '', viewUrl);
    } catch {}
  }

  return c.json({
    ok: true,
    document_id: docId,
    doc_number: b.doc_number,
    r2_key: r2Key,
    view_url: viewUrl,
    view_token: viewToken,
  });
});

// ═══════════════════════════════════════════════════════════
// ─── PUBLIC DOCUMENT VIEW (token-based, no auth) ─────────
// ═══════════════════════════════════════════════════════════

app.get('/view/:token', async (c) => {
  const token = c.req.param('token');
  const doc = await c.env.DB.prepare('SELECT d.*, t.slug as tenant_slug FROM documents d JOIN tenants t ON d.tenant_id = t.id WHERE d.view_token = ?').bind(token).first() as any;
  if (!doc) return c.html('<h1 style="text-align:center;margin-top:80px;font-family:sans-serif;color:#666">Document not found</h1>', 404);

  // Track view
  const viewId = uid();
  const ipHash = await hashIP(c.req.header('CF-Connecting-IP') || 'unknown');
  await c.env.DB.prepare("INSERT INTO document_views (id, document_id, viewed_at, ip_hash, user_agent) VALUES (?, ?, datetime('now'), ?, ?)").bind(viewId, doc.id, ipHash, (c.req.header('User-Agent') || '').slice(0, 200)).run();

  // Load from R2
  const obj = await c.env.R2.get(doc.r2_key);
  if (!obj) return c.html('<h1 style="text-align:center;margin-top:80px;font-family:sans-serif;color:#666">Document expired or removed</h1>', 404);
  const html = await obj.text();

  // Inject API base for email/SMS buttons
  const apiBase = new URL(c.req.url).origin;
  const injectedHtml = html.replace("window.DOC_API_BASE || ''", `'${apiBase}'`);

  return c.html(injectedHtml);
});

// ═══════════════════════════════════════════════════════════
// ─── DOCUMENT LISTING (tenant auth) ─────────────────────
// ═══════════════════════════════════════════════════════════

app.get('/documents', async (c) => {
  const tenantKey = c.req.header('X-Tenant-Key');
  const tenant = await getTenant(c.env.DB, tenantKey);
  const denied = requireTenant(c, tenant);
  if (denied) return denied;

  const type = c.req.query('type');
  const limit = parseInt(c.req.query('limit') || '50');
  const offset = parseInt(c.req.query('offset') || '0');
  let sql = 'SELECT d.*, (SELECT COUNT(*) FROM document_views dv WHERE dv.document_id = d.id) as view_count, (SELECT MAX(dv.viewed_at) FROM document_views dv WHERE dv.document_id = d.id) as last_viewed_at FROM documents d WHERE d.tenant_id = ?';
  const params: any[] = [tenant.id];
  if (type) { sql += ' AND d.doc_type = ?'; params.push(type.toUpperCase()); }
  sql += ' ORDER BY d.created_at DESC LIMIT ? OFFSET ?';
  params.push(limit, offset);
  const rows = await c.env.DB.prepare(sql).bind(...params).all();

  const countSql = type
    ? 'SELECT COUNT(*) as total FROM documents WHERE tenant_id = ? AND doc_type = ?'
    : 'SELECT COUNT(*) as total FROM documents WHERE tenant_id = ?';
  const countParams = type ? [tenant.id, type.toUpperCase()] : [tenant.id];
  const count = await c.env.DB.prepare(countSql).bind(...countParams).first() as any;

  return c.json({ documents: rows.results, total: count?.total || 0 });
});

app.get('/documents/:id', async (c) => {
  const tenantKey = c.req.header('X-Tenant-Key');
  const tenant = await getTenant(c.env.DB, tenantKey);
  const denied = requireTenant(c, tenant);
  if (denied) return denied;

  const doc = await c.env.DB.prepare('SELECT * FROM documents WHERE id = ? AND tenant_id = ?').bind(c.req.param('id'), tenant.id).first();
  if (!doc) return c.json({ error: 'Not found' }, 404);

  const views = await c.env.DB.prepare('SELECT COUNT(*) as count FROM document_views WHERE document_id = ?').bind(c.req.param('id')).first() as any;
  const deliveries = await c.env.DB.prepare('SELECT * FROM deliveries WHERE document_id = ? ORDER BY created_at DESC').bind(c.req.param('id')).all();

  return c.json({ ...doc, view_count: views?.count || 0, deliveries: deliveries.results });
});

// ═══════════════════════════════════════════════════════════
// ─── EMAIL DELIVERY (tenant auth) ────────────────────────
// ═══════════════════════════════════════════════════════════

app.post('/deliver/email', async (c) => {
  const tenantKey = c.req.header('X-Tenant-Key');
  const tenant = await getTenant(c.env.DB, tenantKey);
  const denied = requireTenant(c, tenant);
  if (denied) return denied;

  const b = await c.req.json();
  const { to, subject, message, document_id } = b;
  if (!to) return c.json({ error: 'to (email) required' }, 400);

  const company: CompanyConfig = {
    name: tenant.company_name, phone: tenant.company_phone, email: tenant.company_email,
    tagline: tenant.company_tagline, website: tenant.company_website, city: tenant.company_city,
    primaryColor: tenant.primary_color, accentColor: tenant.accent_color, logoUrl: tenant.logo_url,
  };

  let viewUrl = b.view_url || '';
  if (document_id && !viewUrl) {
    const doc = await c.env.DB.prepare('SELECT view_token FROM documents WHERE id = ? AND tenant_id = ?').bind(document_id, tenant.id).first() as any;
    if (doc) viewUrl = `${new URL(c.req.url).origin}/view/${doc.view_token}`;
  }

  const result = await sendEmailDelivery(c.env, tenant, company, document_id || '', to, subject || `Document from ${company.name}`, message || '', viewUrl);
  return c.json(result, result.ok ? 200 : 502);
});

// ═══════════════════════════════════════════════════════════
// ─── SMS DELIVERY (tenant auth) ──────────────────────────
// ═══════════════════════════════════════════════════════════

app.post('/deliver/sms', async (c) => {
  const tenantKey = c.req.header('X-Tenant-Key');
  const tenant = await getTenant(c.env.DB, tenantKey);
  const denied = requireTenant(c, tenant);
  if (denied) return denied;

  const b = await c.req.json();
  const { to, body: msgBody, document_id } = b;
  if (!to || !msgBody) return c.json({ error: 'to and body required' }, 400);

  const sid = tenant.twilio_sid;
  const token = tenant.twilio_token;
  const from = tenant.twilio_phone;
  if (!sid || !token || !from) return c.json({ ok: false, error: 'Twilio not configured for this tenant. Update tenant with twilio_sid, twilio_token, twilio_phone.' }, 503);

  const deliveryId = uid();
  try {
    const params = new URLSearchParams({ To: to, From: from, Body: msgBody });
    const resp = await fetch(`https://api.twilio.com/2010-04-01/Accounts/${sid}/Messages.json`, {
      method: 'POST', body: params,
      headers: { 'Authorization': 'Basic ' + btoa(sid + ':' + token), 'Content-Type': 'application/x-www-form-urlencoded' },
    });
    const data = await resp.json() as any;

    await c.env.DB.prepare(
      "INSERT INTO deliveries (id, document_id, tenant_id, channel, status, delivered_to, delivered_at, error_message, provider_id, created_at) VALUES (?, ?, ?, 'sms', ?, ?, datetime('now'), ?, ?, datetime('now'))"
    ).bind(deliveryId, document_id || '', tenant.id, resp.ok ? 'sent' : 'failed', to, resp.ok ? null : (data.message || 'Unknown error'), data.sid || '').run();

    return c.json({ ok: resp.ok, delivery_id: deliveryId, status: resp.status, sms_sid: data.sid });
  } catch (e: any) {
    await c.env.DB.prepare(
      "INSERT INTO deliveries (id, document_id, tenant_id, channel, status, delivered_to, error_message, created_at) VALUES (?, ?, ?, 'sms', 'failed', ?, ?, datetime('now'))"
    ).bind(deliveryId, document_id || '', tenant.id, to, e.message).run();
    return c.json({ ok: false, error: e.message }, 500);
  }
});

// ═══════════════════════════════════════════════════════════
// ─── DELIVERY HISTORY (tenant auth) ─────────────────────
// ═══════════════════════════════════════════════════════════

app.get('/deliveries', async (c) => {
  const tenantKey = c.req.header('X-Tenant-Key');
  const tenant = await getTenant(c.env.DB, tenantKey);
  const denied = requireTenant(c, tenant);
  if (denied) return denied;

  const limit = parseInt(c.req.query('limit') || '50');
  const rows = await c.env.DB.prepare('SELECT dl.*, d.doc_type, d.doc_number, d.customer_name FROM deliveries dl LEFT JOIN documents d ON dl.document_id = d.id WHERE dl.tenant_id = ? ORDER BY dl.created_at DESC LIMIT ?').bind(tenant.id, limit).all();
  return c.json(rows.results);
});

// ═══════════════════════════════════════════════════════════
// ─── ANALYTICS (tenant auth) ────────────────────────────
// ═══════════════════════════════════════════════════════════

app.get('/analytics', async (c) => {
  const tenantKey = c.req.header('X-Tenant-Key');
  const tenant = await getTenant(c.env.DB, tenantKey);
  const denied = requireTenant(c, tenant);
  if (denied) return denied;

  const [totalDocs, totalViews, totalDeliveries, byType, byChannel, recentDocs] = await Promise.all([
    c.env.DB.prepare('SELECT COUNT(*) as count FROM documents WHERE tenant_id = ?').bind(tenant.id).first(),
    c.env.DB.prepare('SELECT COUNT(*) as count FROM document_views dv JOIN documents d ON dv.document_id = d.id WHERE d.tenant_id = ?').bind(tenant.id).first(),
    c.env.DB.prepare('SELECT COUNT(*) as count FROM deliveries WHERE tenant_id = ?').bind(tenant.id).first(),
    c.env.DB.prepare('SELECT doc_type, COUNT(*) as count FROM documents WHERE tenant_id = ? GROUP BY doc_type').bind(tenant.id).all(),
    c.env.DB.prepare("SELECT channel, status, COUNT(*) as count FROM deliveries WHERE tenant_id = ? GROUP BY channel, status").bind(tenant.id).all(),
    c.env.DB.prepare('SELECT id, doc_type, doc_number, customer_name, total, created_at FROM documents WHERE tenant_id = ? ORDER BY created_at DESC LIMIT 10').bind(tenant.id).all(),
  ]);

  return c.json({
    total_documents: (totalDocs as any)?.count || 0,
    total_views: (totalViews as any)?.count || 0,
    total_deliveries: (totalDeliveries as any)?.count || 0,
    by_type: byType.results,
    by_channel: byChannel.results,
    recent_documents: recentDocs.results,
  });
});

// ═══════════════════════════════════════════════════════════
// ─── TENANT SETTINGS (tenant auth — self-service) ────────
// ═══════════════════════════════════════════════════════════

app.get('/settings', async (c) => {
  const tenantKey = c.req.header('X-Tenant-Key');
  const tenant = await getTenant(c.env.DB, tenantKey);
  const denied = requireTenant(c, tenant);
  if (denied) return denied;
  // Return tenant config without sensitive keys
  const { api_key, resend_api_key, twilio_token, ...safe } = tenant;
  return c.json({ ...safe, has_resend: !!resend_api_key, has_twilio: !!(tenant.twilio_sid && twilio_token) });
});

app.put('/settings', async (c) => {
  const tenantKey = c.req.header('X-Tenant-Key');
  const tenant = await getTenant(c.env.DB, tenantKey);
  const denied = requireTenant(c, tenant);
  if (denied) return denied;

  const b = await c.req.json();
  const allowed = ['company_name', 'company_phone', 'company_email', 'company_tagline', 'company_website', 'company_city', 'primary_color', 'accent_color', 'logo_url', 'email_from', 'sms_from', 'resend_api_key', 'twilio_sid', 'twilio_token', 'twilio_phone', 'default_payment_terms', 'auto_email_on_generate', 'include_view_link'];
  const fields: string[] = [];
  const values: any[] = [];
  for (const k of allowed) {
    if (k in b) { fields.push(`${k} = ?`); values.push(b[k]); }
  }
  if (fields.length === 0) return c.json({ error: 'No valid fields' }, 400);
  fields.push("updated_at = datetime('now')");
  values.push(tenant.id);
  await c.env.DB.prepare(`UPDATE tenants SET ${fields.join(', ')} WHERE id = ?`).bind(...values).run();
  return c.json({ ok: true });
});

// ═══════════════════════════════════════════════════════════
// ─── HELPER: Send Email ──────────────────────────────────
// ═══════════════════════════════════════════════════════════

async function sendEmailDelivery(env: Env, tenant: any, company: CompanyConfig, documentId: string, to: string, subject: string, message: string, viewUrl: string) {
  const resendKey = tenant.resend_api_key;
  if (!resendKey) {
    const deliveryId = uid();
    await env.DB.prepare(
      "INSERT INTO deliveries (id, document_id, tenant_id, channel, status, delivered_to, error_message, created_at) VALUES (?, ?, ?, 'email', 'failed', ?, 'No email provider configured', datetime('now'))"
    ).bind(deliveryId, documentId, tenant.id, to).run();
    return { ok: false, error: 'Email provider not configured. Update tenant settings with resend_api_key.' };
  }

  const esc = (s: string) => (s || '').replace(/[<>&"']/g, ch => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[ch] || ch));

  const emailHtml = `<!DOCTYPE html><html><body style="font-family:'Open Sans',Arial,sans-serif;max-width:600px;margin:0 auto;background:#f8f9fa;padding:20px">
<div style="background:${company.primaryColor};color:#fff;padding:20px 30px;border-radius:10px 10px 0 0">
  <h1 style="margin:0;font-size:22px;font-family:Oswald,sans-serif">${esc(company.name)}</h1>
  <p style="margin:4px 0 0;font-size:12px;color:rgba(255,255,255,.7)">${esc(company.tagline)}</p>
</div>
<div style="background:#fff;padding:30px;border:1px solid #e5e7eb;border-top:none">
  ${message ? `<p style="font-size:14px;color:#374151;line-height:1.8;white-space:pre-wrap">${esc(message)}</p>` : ''}
  ${viewUrl ? `<div style="text-align:center;margin:24px 0">
    <a href="${esc(viewUrl)}" style="background:${company.primaryColor};color:#fff;padding:14px 32px;border-radius:8px;text-decoration:none;font-weight:600;font-size:14px;display:inline-block">View Document</a>
  </div>
  <p style="font-size:12px;color:#9CA3AF;text-align:center">Or copy this link: ${esc(viewUrl)}</p>` : ''}
</div>
<div style="padding:16px 30px;text-align:center;font-size:11px;color:#9CA3AF">
  ${esc(company.name)} | ${esc(company.city)} | ${esc(company.phone)} | ${esc(company.email)}
</div>
</body></html>`;

  const deliveryId = uid();
  const fromAddr = tenant.email_from || `${company.name} <noreply@${company.website || 'echo-op.com'}>`;

  try {
    const resp = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${resendKey}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ from: fromAddr, to: [to], subject, html: emailHtml }),
    });
    const data = await resp.json() as any;

    await env.DB.prepare(
      "INSERT INTO deliveries (id, document_id, tenant_id, channel, status, delivered_to, delivered_at, error_message, provider_id, created_at) VALUES (?, ?, ?, 'email', ?, ?, datetime('now'), ?, ?, datetime('now'))"
    ).bind(deliveryId, documentId, tenant.id, resp.ok ? 'sent' : 'failed', to, resp.ok ? null : (data.message || 'Unknown'), data.id || '').run();

    if (resp.ok) return { ok: true, delivery_id: deliveryId, email_id: data.id };
    return { ok: false, error: data.message || 'Email send failed' };
  } catch (e: any) {
    await env.DB.prepare(
      "INSERT INTO deliveries (id, document_id, tenant_id, channel, status, delivered_to, error_message, created_at) VALUES (?, ?, ?, 'email', 'failed', ?, ?, datetime('now'))"
    ).bind(deliveryId, documentId, tenant.id, to, e.message).run();
    return { ok: false, error: e.message };
  }
}

// ═══════════════════════════════════════════════════════════
// ─── HELPER: Hash IP ─────────────────────────────────────
// ═══════════════════════════════════════════════════════════

async function hashIP(ip: string): Promise<string> {
  const data = new TextEncoder().encode(ip + 'echo-doc-salt');
  const hash = await crypto.subtle.digest('SHA-256', data);
  return Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
}

// ═══════════════════════════════════════════════════════════
// ─── SERVER-SIDE PDF GENERATION (pdf-lib) ────────────────
// ═══════════════════════════════════════════════════════════

async function generateDocumentPDF(opts: {
  type: string; docNumber: string; date: string; dueDate?: string; serviceDate?: string;
  customerName: string; customerEmail?: string; customerPhone?: string; customerAddress?: string;
  jobTitle?: string; serviceType?: string;
  items: DocItem[]; subtotal: number; taxRate: number; taxAmount: number; total: number;
  amountPaid?: number; notes?: string; paymentTerms?: string;
  company: CompanyConfig;
}): Promise<Uint8Array> {
  const doc = await PDFDocument.create();
  const page = doc.addPage([612, 792]); // Letter size
  const helvetica = await doc.embedFont(StandardFonts.Helvetica);
  const helveticaBold = await doc.embedFont(StandardFonts.HelveticaBold);
  const co = opts.company;
  const typeLabel = opts.type.replace(/_/g, ' ');
  let y = 740;
  const margin = 50;
  const w = 512;

  // ── Company Header ──
  page.drawRectangle({ x: 0, y: 742, width: 612, height: 50, color: rgb(0.05, 0.16, 0.28) });
  page.drawText(co.name.toUpperCase(), { x: margin, y: 755, size: 18, font: helveticaBold, color: rgb(1, 1, 1) });
  if (co.tagline) page.drawText(co.tagline, { x: margin, y: 744, size: 8, font: helvetica, color: rgb(0.8, 0.8, 0.8) });

  // Right-aligned doc type badge
  const badgeText = typeLabel;
  const badgeW = helveticaBold.widthOfTextAtSize(badgeText, 14);
  page.drawText(badgeText, { x: 612 - margin - badgeW, y: 755, size: 14, font: helveticaBold, color: rgb(1, 0.84, 0) });

  y = 720;

  // ── Doc Info Row ──
  const drawLabel = (x: number, yp: number, label: string, value: string) => {
    page.drawText(label, { x, y: yp, size: 7, font: helvetica, color: rgb(0.5, 0.5, 0.5) });
    page.drawText(value || '--', { x, y: yp - 11, size: 10, font: helveticaBold, color: rgb(0.1, 0.1, 0.1) });
  };

  drawLabel(margin, y, 'DOCUMENT #', opts.docNumber);
  drawLabel(margin + 130, y, 'DATE', opts.date);
  if (opts.dueDate) drawLabel(margin + 260, y, 'DUE DATE', opts.dueDate);
  if (opts.serviceDate) drawLabel(margin + 390, y, 'SERVICE DATE', opts.serviceDate);
  y -= 40;

  // ── Customer Info ──
  page.drawRectangle({ x: margin, y: y - 5, width: w, height: 1, color: rgb(0.85, 0.85, 0.85) });
  y -= 20;
  drawLabel(margin, y, 'BILL TO', opts.customerName);
  if (opts.customerAddress) { page.drawText(opts.customerAddress, { x: margin, y: y - 22, size: 9, font: helvetica, color: rgb(0.3, 0.3, 0.3) }); }
  if (opts.customerEmail) drawLabel(margin + 250, y, 'EMAIL', opts.customerEmail);
  if (opts.customerPhone) drawLabel(margin + 250, y - 20, 'PHONE', opts.customerPhone);
  y -= 55;

  // ── Job Info (if estimate/work order) ──
  if (opts.jobTitle || opts.serviceType) {
    page.drawRectangle({ x: margin, y: y - 5, width: w, height: 1, color: rgb(0.85, 0.85, 0.85) });
    y -= 20;
    if (opts.jobTitle) drawLabel(margin, y, 'PROJECT', opts.jobTitle);
    if (opts.serviceType) drawLabel(margin + 250, y, 'SERVICE TYPE', opts.serviceType);
    y -= 30;
  }

  // ── Line Items Table ──
  page.drawRectangle({ x: margin, y: y - 2, width: w, height: 20, color: rgb(0.05, 0.16, 0.28) });
  const cols = [margin + 5, margin + 300, margin + 380, margin + 450];
  const colHeaders = ['Description', 'Qty', 'Rate', 'Amount'];
  colHeaders.forEach((h, i) => {
    page.drawText(h, { x: cols[i], y: y + 4, size: 8, font: helveticaBold, color: rgb(1, 1, 1) });
  });
  y -= 18;

  for (const item of opts.items) {
    if (y < 120) { /* Would need pagination for very long items — skip for now */ break; }
    y -= 18;
    const desc = (item.description || '').slice(0, 60);
    page.drawText(desc, { x: cols[0], y, size: 9, font: helvetica, color: rgb(0.2, 0.2, 0.2) });
    page.drawText(String(item.qty || 1), { x: cols[1], y, size: 9, font: helvetica, color: rgb(0.2, 0.2, 0.2) });
    page.drawText('$' + (item.rate || 0).toFixed(2), { x: cols[2], y, size: 9, font: helvetica, color: rgb(0.2, 0.2, 0.2) });
    page.drawText('$' + (item.amount || 0).toFixed(2), { x: cols[3], y, size: 9, font: helveticaBold, color: rgb(0.1, 0.1, 0.1) });
    page.drawRectangle({ x: margin, y: y - 4, width: w, height: 0.5, color: rgb(0.9, 0.9, 0.9) });
  }

  // ── Totals ──
  y -= 25;
  page.drawRectangle({ x: margin + 300, y: y - 2, width: w - 300, height: 1, color: rgb(0.7, 0.7, 0.7) });
  y -= 14;
  const drawTotal = (label: string, value: string, bold = false) => {
    const f = bold ? helveticaBold : helvetica;
    const sz = bold ? 11 : 9;
    page.drawText(label, { x: margin + 350, y, size: sz, font: f, color: rgb(0.3, 0.3, 0.3) });
    page.drawText(value, { x: margin + 450, y, size: sz, font: f, color: rgb(0.1, 0.1, 0.1) });
    y -= (bold ? 18 : 15);
  };
  drawTotal('Subtotal', '$' + opts.subtotal.toFixed(2));
  if (opts.taxAmount > 0) drawTotal('Tax (' + (opts.taxRate || 0) + '%)', '$' + opts.taxAmount.toFixed(2));
  drawTotal('TOTAL', '$' + opts.total.toFixed(2), true);
  if ((opts.amountPaid || 0) > 0) {
    drawTotal('Paid', '$' + (opts.amountPaid || 0).toFixed(2));
    drawTotal('Balance Due', '$' + (opts.total - (opts.amountPaid || 0)).toFixed(2), true);
  }

  // ── Notes ──
  if (opts.notes) {
    y -= 10;
    page.drawText('Notes', { x: margin, y, size: 8, font: helveticaBold, color: rgb(0.4, 0.4, 0.4) });
    y -= 12;
    const noteLines = opts.notes.split('\n').slice(0, 4);
    for (const line of noteLines) {
      page.drawText(line.slice(0, 90), { x: margin, y, size: 8, font: helvetica, color: rgb(0.4, 0.4, 0.4) });
      y -= 11;
    }
  }

  // ── Payment Terms ──
  if (opts.paymentTerms) {
    y -= 5;
    page.drawText('Payment Terms: ' + opts.paymentTerms, { x: margin, y, size: 8, font: helvetica, color: rgb(0.5, 0.5, 0.5) });
  }

  // ── Footer ──
  page.drawRectangle({ x: 0, y: 0, width: 612, height: 35, color: rgb(0.96, 0.96, 0.97) });
  const footerText = [co.name, co.city, co.phone, co.email].filter(Boolean).join(' • ');
  page.drawText(footerText, { x: margin, y: 14, size: 7, font: helvetica, color: rgb(0.6, 0.6, 0.6) });

  return doc.save();
}

// ═══════════════════════════════════════════════════════════
// ─── EMAIL WITH PDF ATTACHMENT (one-click send) ──────────
// ═══════════════════════════════════════════════════════════

app.post('/deliver/email-pdf', async (c) => {
  const tenantKey = c.req.header('X-Tenant-Key');
  const tenant = await getTenant(c.env.DB, tenantKey);
  const denied = requireTenant(c, tenant);
  if (denied) return denied;

  const b = await c.req.json();
  const { document_id, to, subject, message } = b;
  if (!document_id) return c.json({ error: 'document_id required' }, 400);

  // Load document record
  const docRec = await c.env.DB.prepare('SELECT * FROM documents WHERE id = ? AND tenant_id = ?').bind(document_id, tenant.id).first() as any;
  if (!docRec) return c.json({ error: 'Document not found' }, 404);

  // Get customer email — use provided 'to' or fall back to document's customer_email
  const recipientEmail = to || docRec.customer_email;
  if (!recipientEmail) return c.json({ error: 'No recipient email. Provide "to" or ensure customer has email on file.' }, 400);

  const company: CompanyConfig = {
    name: tenant.company_name, phone: tenant.company_phone, email: tenant.company_email,
    tagline: tenant.company_tagline, website: tenant.company_website, city: tenant.company_city,
    primaryColor: tenant.primary_color, accentColor: tenant.accent_color, logoUrl: tenant.logo_url,
  };

  // Parse document metadata to rebuild items for PDF
  let items: DocItem[] = [{ description: docRec.doc_type + ' ' + docRec.doc_number, qty: 1, rate: docRec.total || 0, amount: docRec.total || 0 }];
  let metadata: any = {};
  try { metadata = docRec.metadata ? JSON.parse(docRec.metadata) : {}; } catch {}
  if (metadata.items?.length) items = metadata.items;

  // Generate PDF
  const pdfBytes = await generateDocumentPDF({
    type: docRec.doc_type, docNumber: docRec.doc_number, date: docRec.created_at?.split('T')[0] || new Date().toISOString().split('T')[0],
    dueDate: metadata.due_date, serviceDate: metadata.service_date,
    customerName: docRec.customer_name || 'Customer', customerEmail: docRec.customer_email,
    customerPhone: docRec.customer_phone, customerAddress: docRec.customer_address,
    jobTitle: metadata.job_title, serviceType: metadata.service_type,
    items, subtotal: metadata.subtotal || docRec.total || 0, taxRate: metadata.tax_rate || 0,
    taxAmount: metadata.tax_amount || 0, total: docRec.total || 0,
    amountPaid: metadata.amount_paid, notes: metadata.notes, paymentTerms: metadata.payment_terms,
    company,
  });

  // Store PDF in R2
  const safeName = (docRec.customer_name || 'customer').replace(/[^a-zA-Z0-9]/g, '_').slice(0, 30);
  const pdfKey = `${tenant.slug}/pdf/${docRec.doc_type.toLowerCase()}/${docRec.doc_number}_${safeName}.pdf`;
  await c.env.R2.put(pdfKey, pdfBytes, { httpMetadata: { contentType: 'application/pdf' } });

  // Convert to base64 for Resend attachment (chunked to avoid stack overflow)
  let pdfBase64 = '';
  const chunk = 8192;
  for (let i = 0; i < pdfBytes.length; i += chunk) {
    pdfBase64 += String.fromCharCode(...pdfBytes.subarray(i, i + chunk));
  }
  pdfBase64 = btoa(pdfBase64);

  // Check Resend key
  const resendKey = tenant.resend_api_key;
  if (!resendKey) return c.json({ ok: false, error: 'Email provider not configured. Set resend_api_key in tenant settings.' }, 503);

  // Build email
  const esc = (s: string) => (s || '').replace(/[<>&"']/g, ch => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[ch] || ch));
  const typeLabel = docRec.doc_type.replace(/_/g, ' ');
  const emailSubject = subject || `${typeLabel} ${docRec.doc_number} from ${company.name}`;
  const viewUrl = `${new URL(c.req.url).origin}/view/${docRec.view_token}`;
  const filename = `${typeLabel.replace(/\s/g, '_')}_${docRec.doc_number}_${safeName}.pdf`;

  const emailHtml = `<!DOCTYPE html><html><body style="font-family:'Open Sans',Arial,sans-serif;max-width:600px;margin:0 auto;background:#f8f9fa;padding:20px">
<div style="background:${company.primaryColor};color:#fff;padding:20px 30px;border-radius:10px 10px 0 0">
  <h1 style="margin:0;font-size:22px">${esc(company.name)}</h1>
  <p style="margin:4px 0 0;font-size:12px;color:rgba(255,255,255,.7)">${esc(company.tagline)}</p>
</div>
<div style="background:#fff;padding:30px;border:1px solid #e5e7eb;border-top:none">
  <p style="font-size:14px;color:#374151;line-height:1.8">${message ? esc(message) : `Please find your ${typeLabel.toLowerCase()} attached to this email.`}</p>
  <div style="background:#f3f4f6;padding:16px;border-radius:8px;margin:16px 0">
    <table style="width:100%;font-size:13px;color:#374151">
      <tr><td style="padding:4px 0;font-weight:600">Document:</td><td>${esc(typeLabel)} ${esc(docRec.doc_number)}</td></tr>
      <tr><td style="padding:4px 0;font-weight:600">Amount:</td><td style="font-weight:700;color:#1E40AF">$${(docRec.total || 0).toFixed(2)}</td></tr>
      <tr><td style="padding:4px 0;font-weight:600">Customer:</td><td>${esc(docRec.customer_name)}</td></tr>
    </table>
  </div>
  <p style="font-size:12px;color:#6B7280">The PDF is attached to this email. You can also <a href="${esc(viewUrl)}" style="color:${company.primaryColor}">view it online</a>.</p>
</div>
<div style="padding:16px 30px;text-align:center;font-size:11px;color:#9CA3AF">
  ${esc(company.name)} | ${esc(company.city)} | ${esc(company.phone)} | ${esc(company.email)}
</div>
</body></html>`;

  const fromAddr = tenant.email_from || `${company.name} <noreply@${company.website || 'echo-op.com'}>`;
  const deliveryId = uid();

  try {
    const resp = await fetch('https://api.resend.com/emails', {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${resendKey}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        from: fromAddr, to: [recipientEmail], subject: emailSubject, html: emailHtml,
        attachments: [{ filename, content: pdfBase64 }],
      }),
    });
    const data = await resp.json() as any;

    await c.env.DB.prepare(
      "INSERT INTO deliveries (id, document_id, tenant_id, channel, status, delivered_to, delivered_at, error_message, provider_id, created_at) VALUES (?, ?, ?, 'email', ?, ?, datetime('now'), ?, ?, datetime('now'))"
    ).bind(deliveryId, document_id, tenant.id, resp.ok ? 'sent' : 'failed', recipientEmail, resp.ok ? null : (data.message || 'Unknown'), data.id || '').run();

    if (resp.ok) return c.json({ ok: true, delivery_id: deliveryId, email_id: data.id, pdf_r2_key: pdfKey, sent_to: recipientEmail });
    return c.json({ ok: false, error: data.message || 'Email send failed', status: resp.status }, 502);
  } catch (e: any) {
    await c.env.DB.prepare(
      "INSERT INTO deliveries (id, document_id, tenant_id, channel, status, delivered_to, error_message, created_at) VALUES (?, ?, ?, 'email', 'failed', ?, ?, datetime('now'))"
    ).bind(deliveryId, document_id, tenant.id, recipientEmail, e.message).run();
    return c.json({ ok: false, error: e.message }, 500);
  }
});

// ═══════════════════════════════════════════════════════════
// ─── UNIVERSAL DOCUMENT HTML BUILDER ─────────────────────
// ═══════════════════════════════════════════════════════════

function buildDocumentHTML(opts: {
  type: DocType;
  docNumber: string;
  date: string;
  dueDate?: string;
  serviceDate?: string;
  customerName: string;
  customerEmail?: string;
  customerPhone?: string;
  customerAddress?: string;
  jobTitle?: string;
  serviceType?: string;
  items: DocItem[];
  subtotal: number;
  taxRate: number;
  taxAmount: number;
  total: number;
  amountPaid?: number;
  scopeItems?: string[];
  notes?: string;
  paymentTerms?: string;
  company: CompanyConfig;
}): string {
  const co = opts.company;
  const isEst = opts.type === 'ESTIMATE';
  const isWO = opts.type === 'WORK_ORDER';
  const isReceipt = opts.type === 'RECEIPT';
  const isStatement = opts.type === 'STATEMENT';
  const isPO = opts.type === 'PURCHASE_ORDER';
  const isProposal = opts.type === 'PROPOSAL';
  const badgeColors: Record<string, string> = {
    ESTIMATE: '#22C55E', WORK_ORDER: '#F59E0B', RECEIPT: '#8B5CF6',
    STATEMENT: '#6366F1', INVOICE: '#3B82F6', PROPOSAL: '#EC4899', PURCHASE_ORDER: '#14B8A6',
  };
  const badgeBg = badgeColors[opts.type] || '#3B82F6';
  const typeLabel = opts.type.replace(/_/g, ' ');
  const esc = (s: string) => (s || '').replace(/[<>&"']/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c] || c));

  const lineRows = opts.items.map(i =>
    `<tr><td style="padding:10px 16px;border-bottom:1px solid #E5E7EB;font-size:13px">${esc(i.description)}</td>` +
    `<td style="padding:10px 16px;border-bottom:1px solid #E5E7EB;text-align:center;font-size:13px">${i.qty}</td>` +
    `<td style="padding:10px 16px;border-bottom:1px solid #E5E7EB;text-align:right;font-size:13px">$${(i.rate || 0).toFixed(2)}</td>` +
    `<td style="padding:10px 16px;border-bottom:1px solid #E5E7EB;text-align:right;font-size:13px;font-weight:600">$${(i.amount || 0).toFixed(2)}</td></tr>`
  ).join('');

  const subtotalRow = `<tr><td colspan="3" style="padding:10px 16px;text-align:right;font-weight:600;border-top:2px solid #D1D5DB">Subtotal</td>` +
    `<td style="padding:10px 16px;text-align:right;font-weight:600;border-top:2px solid #D1D5DB">$${opts.subtotal.toFixed(2)}</td></tr>`;
  const taxRow = opts.taxAmount > 0 ? `<tr><td colspan="3" style="padding:8px 16px;text-align:right;font-size:13px;color:#6B7280">Tax (${(opts.taxRate * 100).toFixed(2)}%)</td>` +
    `<td style="padding:8px 16px;text-align:right;font-size:13px;color:#6B7280">$${opts.taxAmount.toFixed(2)}</td></tr>` : '';

  const thirdLabel = opts.dueDate ? 'Due Date' : opts.serviceDate ? 'Service Date' : 'Total';
  const thirdValue = opts.dueDate || opts.serviceDate || `$${opts.total.toFixed(2)}`;
  const totalLabel = isReceipt ? 'AMOUNT PAID' : (isEst || isProposal) ? 'ESTIMATED TOTAL' : isPO ? 'ORDER TOTAL' : 'TOTAL DUE';

  const balanceDue = opts.total - (opts.amountPaid || 0);
  const balanceHtml = opts.amountPaid && opts.amountPaid > 0 && !isReceipt
    ? `<div style="padding:0 40px 16px"><div style="display:flex;justify-content:space-between;padding:14px 20px;background:#ECFDF5;border-radius:8px;border:1px solid #A7F3D0"><span style="font-weight:600;color:#065F46">Amount Paid</span><span style="font-weight:700;color:#065F46">$${opts.amountPaid.toFixed(2)}</span></div>
    <div style="display:flex;justify-content:space-between;padding:14px 20px;background:#FEF2F2;border-radius:8px;border:1px solid #FECACA;margin-top:8px"><span style="font-weight:600;color:#991B1B">Balance Due</span><span style="font-weight:700;color:#991B1B;font-size:18px">$${balanceDue.toFixed(2)}</span></div></div>` : '';

  const scopeHtml = opts.scopeItems?.length
    ? `<div style="padding:0 40px 16px"><div style="font-family:Oswald,sans-serif;font-size:14px;font-weight:600;color:${co.primaryColor};text-transform:uppercase;letter-spacing:1px;padding-bottom:6px;border-bottom:2px solid ${co.primaryColor};margin-bottom:12px">Scope of Work</div><ul style="margin:0;padding-left:20px;font-size:13px;color:#374151;line-height:2">${opts.scopeItems.map(s => `<li>${esc(s)}</li>`).join('')}</ul></div>` : '';

  const notesHtml = opts.notes
    ? `<div style="padding:0 40px 16px"><div style="background:#FFFBEB;padding:14px 18px;border-radius:8px;border-left:4px solid ${co.accentColor};font-size:13px;color:#92400E"><strong>Notes:</strong> ${esc(opts.notes)}</div></div>` : '';

  return `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>${esc(typeLabel)} ${esc(opts.docNumber)} | ${esc(co.name)}</title>
<link href="https://fonts.googleapis.com/css2?family=Oswald:wght@400;500;600;700&family=Open+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:"Open Sans",sans-serif;color:#1f2937;background:#fff;max-width:850px;margin:0 auto}
@media print{
  body{-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important;max-width:100%}
  .no-print{display:none!important}
  @page{margin:0.4in}
}
.delivery-bar{background:#f1f5f9;padding:12px 40px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;border-bottom:1px solid #e2e8f0}
.delivery-bar button{padding:8px 18px;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;gap:6px;transition:all .15s}
.btn-print{background:${co.primaryColor};color:#fff}.btn-print:hover{opacity:.9}
.btn-pdf{background:#059669;color:#fff}.btn-pdf:hover{background:#047857}
.btn-email{background:#2563EB;color:#fff}.btn-email:hover{background:#1D4ED8}
.btn-sms{background:#7C3AED;color:#fff}.btn-sms:hover{background:#6D28D9}
.modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.5);z-index:1000;justify-content:center;align-items:center}
.modal-overlay.active{display:flex}
.modal-form{background:#fff;border-radius:12px;padding:28px;width:420px;max-width:90vw;box-shadow:0 20px 60px rgba(0,0,0,.3)}
.modal-form h3{margin:0 0 16px;font-size:18px}
.modal-form input,.modal-form textarea{width:100%;padding:10px 14px;border:1px solid #d1d5db;border-radius:8px;font-size:14px;margin-bottom:12px;font-family:inherit}
.modal-form textarea{height:80px;resize:vertical}
.modal-form .btn-row{display:flex;gap:8px;justify-content:flex-end}
.status-toast{position:fixed;bottom:24px;right:24px;padding:14px 24px;border-radius:10px;color:#fff;font-weight:600;font-size:14px;z-index:2000;opacity:0;transition:opacity .3s;pointer-events:none}
.status-toast.show{opacity:1}
.status-toast.success{background:#059669}
.status-toast.error{background:#DC2626}
</style>
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.2/html2pdf.bundle.min.js"></script>
</head><body>

<div class="delivery-bar no-print" id="deliveryBar">
  <button class="btn-print" onclick="window.print()">&#128424; Print</button>
  <button class="btn-pdf" onclick="downloadPDF()">&#128196; Save PDF</button>
  <button class="btn-email" onclick="showModal('emailModal')">&#128231; Email</button>
  <button class="btn-sms" onclick="showModal('smsModal')">&#128172; SMS</button>
  <span style="margin-left:auto;font-size:12px;color:#6B7280" id="docMeta">${esc(typeLabel)} ${esc(opts.docNumber)}</span>
</div>

<div class="modal-overlay" id="emailModal">
  <div class="modal-form">
    <h3 style="color:${co.primaryColor}">&#128231; Email ${esc(typeLabel)}</h3>
    <input type="email" id="emailTo" placeholder="Recipient email" value="${esc(opts.customerEmail || '')}">
    <input type="text" id="emailSubject" value="${esc(typeLabel)} ${esc(opts.docNumber)} from ${esc(co.name)}">
    <textarea id="emailMessage" placeholder="Optional message...">Hi ${esc(opts.customerName)},\n\nPlease find your ${esc(typeLabel.toLowerCase())} ${esc(opts.docNumber)} attached.\n\nTotal: $${opts.total.toFixed(2)}${opts.dueDate ? '\nDue: ' + opts.dueDate : ''}\n\nThank you,\n${esc(co.name)}</textarea>
    <div class="btn-row">
      <button onclick="hideModal('emailModal')" style="padding:8px 18px;border:1px solid #d1d5db;border-radius:6px;background:#fff;cursor:pointer">Cancel</button>
      <button onclick="sendEmail()" class="btn-email" style="border:none;border-radius:6px;padding:8px 24px;color:#fff;cursor:pointer">Send Email</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="smsModal">
  <div class="modal-form">
    <h3 style="color:#7C3AED">&#128172; Send via SMS</h3>
    <input type="tel" id="smsTo" placeholder="Phone number (e.g. +14325551234)" value="${esc(opts.customerPhone || '')}">
    <input type="text" id="smsMessage" value="${esc(typeLabel)} ${esc(opts.docNumber)} from ${esc(co.name)}: $${opts.total.toFixed(2)}${opts.dueDate ? ' due ' + opts.dueDate : ''}. View: ">
    <div class="btn-row">
      <button onclick="hideModal('smsModal')" style="padding:8px 18px;border:1px solid #d1d5db;border-radius:6px;background:#fff;cursor:pointer">Cancel</button>
      <button onclick="sendSMS()" class="btn-sms" style="border:none;border-radius:6px;padding:8px 24px;color:#fff;cursor:pointer">Send SMS</button>
    </div>
  </div>
</div>

<div class="status-toast" id="toast"></div>

<div id="documentContent">
<div style="background:${co.primaryColor};color:#fff;padding:24px 40px;display:flex;justify-content:space-between;align-items:center">
  <div>
    <div style="font-family:Oswald,sans-serif;font-size:28px;font-weight:700;line-height:1">${esc(co.name)}</div>
    <div style="font-size:9px;letter-spacing:2.5px;text-transform:uppercase;color:rgba(255,255,255,.7);margin-top:3px">${esc(co.tagline)}${co.city ? ' | ' + esc(co.city) : ''}</div>
  </div>
  <div style="display:flex;align-items:center;gap:24px">
    <div style="text-align:right;font-size:11px;color:rgba(255,255,255,.8);line-height:1.8">
      ${co.phone ? `<div>${esc(co.phone)}</div>` : ''}
      ${co.email ? `<div>${esc(co.email)}</div>` : ''}
      ${co.website ? `<div>${esc(co.website)}</div>` : ''}
    </div>
    <div style="background:${badgeBg};color:#fff;padding:8px 22px;border-radius:6px;font-family:Oswald,sans-serif;font-size:18px;font-weight:600;letter-spacing:2px">${esc(typeLabel)}</div>
  </div>
</div>

<div style="display:flex;border-bottom:2px solid #E5E7EB">
  <div style="flex:1;padding:16px 20px;text-align:center;border-right:1px solid #E5E7EB">
    <div style="font-size:9px;text-transform:uppercase;letter-spacing:1.5px;color:#9CA3AF;margin-bottom:4px">${esc(typeLabel)} Number</div>
    <div style="font-size:16px;font-weight:600;color:${co.primaryColor}">${esc(opts.docNumber)}</div>
  </div>
  <div style="flex:1;padding:16px 20px;text-align:center;border-right:1px solid #E5E7EB">
    <div style="font-size:9px;text-transform:uppercase;letter-spacing:1.5px;color:#9CA3AF;margin-bottom:4px">Date</div>
    <div style="font-size:16px;font-weight:600;color:${co.primaryColor}">${esc(opts.date)}</div>
  </div>
  <div style="flex:1;padding:16px 20px;text-align:center">
    <div style="font-size:9px;text-transform:uppercase;letter-spacing:1.5px;color:#9CA3AF;margin-bottom:4px">${esc(thirdLabel)}</div>
    <div style="font-size:16px;font-weight:600;color:${co.primaryColor}">${esc(thirdValue)}</div>
  </div>
</div>

<div style="padding:20px 40px">
  <div style="font-family:Oswald,sans-serif;font-size:14px;font-weight:600;color:${co.primaryColor};text-transform:uppercase;letter-spacing:1px;padding-bottom:6px;border-bottom:2px solid ${co.primaryColor};margin-bottom:16px">${(isEst || isWO || isProposal) ? 'Service Details' : isPO ? 'Order Details' : 'Bill To'}</div>
  <div style="display:flex;gap:20px;margin-bottom:16px">
    <div style="flex:1;background:#F9FAFB;border-radius:8px;padding:14px 18px">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:${co.accentColor};margin-bottom:6px">Customer</div>
      <p style="font-size:14px;font-weight:600;color:#374151;margin:0">${esc(opts.customerName)}</p>
      ${opts.customerAddress ? `<p style="font-size:13px;color:#6B7280;margin:4px 0 0">${esc(opts.customerAddress)}</p>` : ''}
      ${opts.customerPhone ? `<p style="font-size:13px;color:#6B7280;margin:2px 0 0">${esc(opts.customerPhone)}</p>` : ''}
      ${opts.customerEmail ? `<p style="font-size:13px;color:#6B7280;margin:2px 0 0">${esc(opts.customerEmail)}</p>` : ''}
    </div>
    ${(isEst || isWO || isProposal) && opts.jobTitle ? `<div style="flex:1;background:#F9FAFB;border-radius:8px;padding:14px 18px">
      <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:${co.accentColor};margin-bottom:6px">Project</div>
      <p style="font-size:14px;font-weight:600;color:#374151;margin:0">${esc(opts.jobTitle)}</p>
      ${opts.serviceType ? `<p style="font-size:13px;color:#6B7280;margin:4px 0 0">${esc(opts.serviceType)}</p>` : ''}
    </div>` : ''}
  </div>
</div>

<div style="padding:0 40px 16px">
  <div style="font-family:Oswald,sans-serif;font-size:14px;font-weight:600;color:${co.primaryColor};text-transform:uppercase;letter-spacing:1px;padding-bottom:6px;border-bottom:2px solid ${co.primaryColor};margin-bottom:12px">Cost Breakdown</div>
  <table style="width:100%;border-collapse:collapse">
    <thead><tr>
      <th style="background:${co.primaryColor};color:#fff;font-family:Oswald,sans-serif;font-size:11px;text-transform:uppercase;letter-spacing:1.5px;padding:10px 16px;text-align:left">Description</th>
      <th style="background:${co.primaryColor};color:#fff;font-family:Oswald,sans-serif;font-size:11px;text-transform:uppercase;letter-spacing:1.5px;padding:10px 16px;text-align:center">Qty</th>
      <th style="background:${co.primaryColor};color:#fff;font-family:Oswald,sans-serif;font-size:11px;text-transform:uppercase;letter-spacing:1.5px;padding:10px 16px;text-align:right">Rate</th>
      <th style="background:${co.primaryColor};color:#fff;font-family:Oswald,sans-serif;font-size:11px;text-transform:uppercase;letter-spacing:1.5px;padding:10px 16px;text-align:right">Amount</th>
    </tr></thead>
    <tbody>${lineRows}${subtotalRow}${taxRow}</tbody>
  </table>
  <div style="background:${co.primaryColor};display:flex;justify-content:space-between;align-items:center;padding:12px 20px;border-radius:6px;margin-top:8px">
    <span style="color:#fff;font-family:Oswald,sans-serif;font-size:16px;letter-spacing:1px">${esc(totalLabel)}</span>
    <span style="color:${co.accentColor};font-family:Oswald,sans-serif;font-size:22px;font-weight:700">$${opts.total.toFixed(2)}</span>
  </div>
</div>

${balanceHtml}
${scopeHtml}
${notesHtml}

${opts.paymentTerms ? `<div style="padding:0 40px 20px"><div style="background:#f8f9fa;padding:16px 18px;border-radius:8px;font-size:12px;color:#666">
  <div style="font-weight:700;color:${co.primaryColor};margin-bottom:6px;text-transform:uppercase;letter-spacing:1px;font-size:11px">Payment Terms</div>
  <p style="margin:0">${esc(opts.paymentTerms)}</p>
</div></div>` : ''}

<div style="background:${co.primaryColor};padding:20px 40px;text-align:center;border-top:3px solid ${co.accentColor};margin-top:32px">
  <div style="color:rgba(255,255,255,.8);font-size:11px;letter-spacing:1px">${esc(co.name)}${co.city ? ' &nbsp;|&nbsp; ' + esc(co.city) : ''}${co.phone ? ' &nbsp;|&nbsp; ' + esc(co.phone) : ''}${co.email ? ' &nbsp;|&nbsp; ' + esc(co.email) : ''}</div>
  ${co.tagline ? `<div style="color:${co.accentColor};font-family:Oswald,sans-serif;font-size:12px;letter-spacing:2px;margin-top:8px">${esc(co.tagline)}</div>` : ''}
</div>
</div>

<script>
const DOC_ID = '${esc(opts.docNumber)}';
const DOC_TYPE = '${esc(typeLabel)}';
const CUSTOMER = '${esc(opts.customerName)}';
const API_BASE = window.DOC_API_BASE || '';
const TENANT_KEY = window.DOC_TENANT_KEY || '';

function toast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'status-toast show ' + (type || 'success');
  setTimeout(() => t.className = 'status-toast', 3000);
}

function downloadPDF() {
  const el = document.getElementById('documentContent');
  const filename = DOC_TYPE.replace(/\\s+/g, '_') + '_' + DOC_ID + '_' + CUSTOMER.replace(/[^a-zA-Z0-9]/g, '_') + '.pdf';
  html2pdf().set({
    margin: 0.3, filename: filename,
    image: { type: 'jpeg', quality: 0.98 },
    html2canvas: { scale: 2, useCORS: true, logging: false },
    jsPDF: { unit: 'in', format: 'letter', orientation: 'portrait' },
    pagebreak: { mode: ['avoid-all', 'css', 'legacy'] }
  }).from(el).save().then(() => toast('PDF saved: ' + filename)).catch(e => toast('PDF error: ' + e.message, 'error'));
}

function showModal(id) { document.getElementById(id).classList.add('active'); }
function hideModal(id) { document.getElementById(id).classList.remove('active'); }

function getHeaders() {
  const h = { 'Content-Type': 'application/json' };
  if (TENANT_KEY) h['X-Tenant-Key'] = TENANT_KEY;
  try { const t = localStorage.getItem('fb_token'); if (t) h['Authorization'] = 'Bearer ' + t; } catch {}
  return h;
}

async function sendEmail() {
  const to = document.getElementById('emailTo').value.trim();
  const subject = document.getElementById('emailSubject').value.trim();
  const message = document.getElementById('emailMessage').value.trim();
  if (!to) { toast('Enter recipient email', 'error'); return; }
  hideModal('emailModal');
  toast('Sending email...');
  try {
    const resp = await fetch(API_BASE + '/deliver/email', {
      method: 'POST', headers: getHeaders(),
      body: JSON.stringify({ to, subject, message, document_id: DOC_ID, view_url: window.location.href })
    });
    const data = await resp.json();
    if (data.ok) toast('Email sent to ' + to);
    else toast('Email failed: ' + (data.error || 'unknown'), 'error');
  } catch (e) { toast('Email error: ' + e.message, 'error'); }
}

async function sendSMS() {
  const to = document.getElementById('smsTo').value.trim();
  const message = document.getElementById('smsMessage').value.trim();
  if (!to) { toast('Enter phone number', 'error'); return; }
  hideModal('smsModal');
  toast('Sending SMS...');
  try {
    const fullMsg = message + ' ' + window.location.href;
    const resp = await fetch(API_BASE + '/deliver/sms', {
      method: 'POST', headers: getHeaders(),
      body: JSON.stringify({ to, body: fullMsg, document_id: DOC_ID })
    });
    const data = await resp.json();
    if (data.ok) toast('SMS sent to ' + to);
    else toast('SMS failed: ' + (data.error || 'unknown'), 'error');
  } catch (e) { toast('SMS error: ' + e.message, 'error'); }
}
</script>
</body></html>`;
}


app.onError((err, c) => {
  if (err.message?.includes('JSON')) {
    return c.json({ error: 'Invalid JSON body' }, 400);
  }
  console.error(`[echo-document-delivery] ${err.message}`);
  return c.json({ error: 'Internal server error' }, 500);
});

app.notFound((c) => {
  return c.json({ error: 'Not found' }, 404);
});

export default app;
