# Echo Document Delivery

> Document generation + multi-channel delivery for ECHO Prime (v1.0.0). Generate a
> document, deliver it by email, email-with-PDF, or SMS, and track opens via a
> tokenized public view link — multi-tenant, a Hono app on Cloudflare Workers.

Private to Echo Prime Technologies.

## Flow

Generate a **document**, **deliver** it over a channel (email / email-PDF / SMS),
and recipients open it through a tokenized **view** link whose opens are recorded
as **document_views**. **Deliveries** and **analytics** give you the audit trail.

## API (auth: `X-Echo-API-Key`; `/view/:token` is public)

| Method | Route | Purpose |
|---|---|---|
| `GET`  | `/` , `/health` | Service info / liveness |
| `GET`  | `/documents` · `GET /documents/:id` | List / fetch documents |
| `POST` | `/documents/generate` | Generate a document |
| `POST` | `/deliver/email` | Deliver by email |
| `POST` | `/deliver/email-pdf` | Deliver as an email with a PDF attachment |
| `POST` | `/deliver/sms` | Deliver by SMS |
| `GET`  | `/deliveries` | Delivery history |
| `GET`  | `/view/:token` | **Public** tokenized view (records an open) |
| `GET`  | `/analytics` | Delivery + view analytics |
| `GET` / `POST` | `/tenants` · `GET|PUT /tenants/:id` | Tenant management |
| `GET` / `PUT` | `/settings` | Tenant settings |

## Storage (D1 — `schema.sql`)

`tenants`, `documents`, `deliveries`, `document_views`.

## Develop

```bash
npm install
npx wrangler dev       # local Worker
npx wrangler deploy    # deploy
```

Email/SMS provider creds and the D1 binding live in `wrangler.toml` / the
Cloudflare dashboard — never commit them.

## License

Proprietary — © Echo Prime Technologies. All rights reserved.
