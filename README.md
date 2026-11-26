# NaijaBasket

An asset-light online grocery aggregation PWA for Greater Lagos. NaijaBasket owns
no inventory, no stores and no vehicles: it routes a consumer's basket to a vetted
partner supermarket, dispatches a third-party courier, and holds the customer's
money in escrow until the goods are physically handed over and a 4-digit PIN is
presented at the door.

The business model and rules are defined by `NaijaBasket PRD.docx`; every number
in this codebase traces back to that document.

---

## The flow

```
Consumer PWA                 Partner supermarket           3PL courier
────────────                 ───────────────────           ───────────
browse → cart ≥ ₦15,000  →   accept + pick (15-min SLA) →  auto-dispatch  →
pay (Paystack/Flutterwave)   confirm packed                 deliver        →
        │                                                          │
        └──────────── escrow HELD ─────────────────────────────────┘
                              PIN verified at the door
                                      ↓
              funds split: merchant payout + rider payout + platform revenue
```

Nothing is trusted from the client. Cart totals, delivery radius, SLA clocks,
commission and the escrow ledger are all computed server-side.

---

## What it does

| PRD rule | Where it lives |
|---|---|
| **FR-01** Minimum order ₦15,000, single-store baskets, upselling to the ₦35,000 target AOV | [cart.py](file:///c:/Users/user/OneDrive/Desktop/NaijaBasket/app/services/cart.py) |
| **FR-02** 15-minute merchant picking SLA with warning → automatic escalation and rating penalty | [sla.py](file:///c:/Users/user/OneDrive/Desktop/NaijaBasket/app/services/sla.py) |
| **FR-03** Automated 3PL dispatch with sandbox fallback | [dispatch.py](file:///c:/Users/user/OneDrive/Desktop/NaijaBasket/app/integrations/dispatch.py) |
| **FR-04** Escrow hold, PIN-verified release, and double-entry payout ledger | [escrow.py](file:///c:/Users/user/OneDrive/Desktop/NaijaBasket/app/services/escrow.py) |
| **FR-05** Food-safety segregation — chemicals never share a pick lane with food, NAFDAC numbers required for food | [safety.py](file:///c:/Users/user/OneDrive/Desktop/NaijaBasket/app/services/safety.py) |
| **FR-06** WhatsApp abandoned-cart recovery with a discount token, delivered via the WhatsApp Cloud API and tracked to read | [services/whatsapp.py](file:///c:/Users/user/OneDrive/Desktop/NaijaBasket/app/services/whatsapp.py), [integrations/whatsapp.py](file:///c:/Users/user/OneDrive/Desktop/NaijaBasket/app/integrations/whatsapp.py) |

### Unit economics

Per order: `R = GMV × commission_rate + convenience_fee + subscription_allocation`
and `Cv = gateway_fee + logistics/processing`, giving contribution `Mc = R − Cv`.

The PRD benchmark is asserted in code (`flask --app wsgi benchmark`) and in
[tests/test_economics.py](file:///c:/Users/user/OneDrive/Desktop/NaijaBasket/tests/test_economics.py):

| Metric | Value |
|---|---|
| AOV | ₦35,000 |
| Take rate (clamped 3%–8%) | 5.5% |
| Commission + convenience fee | ₦1,925 + ₦300 = ₦2,225 |
| Gateway (1.5%, capped ₦2,000) + processing | ₦700 |
| **Net contribution** | **₦1,525 (4.36% of GMV)** |

Subscription passes (₦5,000 for 10 orders) are amortised into the platform's
revenue rather than charged to the merchant.

---

## Stack

- **Python 3.13 / Flask 3** application factory, SQLAlchemy 2, Flask-Login, Flask-WTF, Flask-Migrate (Alembic), Flask-Limiter
- **PostgreSQL** in production (managed, via `DATABASE_URL`); SQLite for local development and for the Vercel serverless demo
- **Gunicorn** (`gthread`) behind Cloudflare, deployed as a Docker image; the same code runs as a single Vercel Python function
- **Paystack / Flutterwave** payments and webhooks; **Kwik / Gokada** dispatch
- **Sentry** (optional) for error reporting
- Server-rendered Jinja templates + a service-worker-backed PWA shell (installable, offline fallback page)

Every third-party integration has a deterministic sandbox adapter, so the whole
product runs end-to-end with **no API keys at all**.

---

## Project layout

```
app/
  __init__.py        application factory, error handlers, CLI commands
  config.py          every business constant and environment knob
  constants.py       categories, safety classes, order/escrow state machines
  models.py          User, PartnerStore, Product, Cart, Order, LedgerEntry, …
  security.py        PBKDF2 passwords, AES-256-GCM PII, PIN hashing
  seed.py            demo stores, products, users, orders, abandoned cart
  blueprints/        storefront, auth, checkout, orders, subscription,
                     merchant, admin, webhooks, api
  services/          cart, orders, escrow, sla, safety, economics, geo, whatsapp
  integrations/      payments, dispatch + WhatsApp (real adapters, sandbox fallback)
  templates/ static/ Jinja UI and PWA assets
migrations/          Alembic revisions
tests/               pytest suite
scheduler.py         always-on worker for the two time-driven sweeps
wsgi.py              WSGI entrypoint
Dockerfile render.yaml Procfile vercel.json
```

---

## Local development

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env          # then edit if you have live keys
.\.venv\Scripts\python.exe -m flask --app wsgi init-db
.\.venv\Scripts\python.exe -m flask --app wsgi seed
.\.venv\Scripts\python.exe -m flask --app wsgi run --debug
```

Open http://127.0.0.1:5000. `.env` is loaded automatically.

### Demo accounts (from the seed)

All four use the password `Password123!`.

| Role | Email |
|---|---|
| Consumer | `consumer@naijabasket.ng` |
| Consumer (subscriber) | `subscriber@naijabasket.ng` |
| Merchant | `merchant@naijabasket.ng` |
| Admin | `admin@naijabasket.ng` |

---

## CLI

| Command | Purpose |
|---|---|
| `flask --app wsgi init-db` | Create tables directly (local development only) |
| `flask --app wsgi db upgrade` | Apply Alembic migrations (what production runs) |
| `flask --app wsgi seed` | Load demo data |
| `flask --app wsgi reset-db --yes` | Drop and recreate every table |
| `flask --app wsgi sla-check` | One FR-02 escalation sweep |
| `flask --app wsgi cart-recovery` | One FR-06 recovery sweep (sends the template and records the BSP receipt) |
| `flask --app wsgi benchmark` | Print the PRD unit-economics benchmark |

## HTTP surface

- `/` `/catalog` `/how-it-works` — storefront; `/offline` — PWA fallback
- `/auth/register|login|logout|profile`
- `/checkout/`, `/checkout/pay`, `/checkout/callback`, `/checkout/sandbox/<ref>`
- `/orders/`, `/orders/<ref>/release` (PIN release), `/cancel`
- `/subscription/`
- `/merchant/` dashboard, orders, inventory, dispatch board
- `/admin/dashboard|partners|finance|orders`, plus maintenance endpoints
- `/webhooks/paystack`, `/webhooks/flutterwave` (signature-verified, CSRF-exempt)
- `/webhooks/whatsapp` — `GET` answers Meta's subscription handshake, `POST` applies
  delivery/read receipts (`X-Hub-Signature-256`-verified, CSRF-exempt)
- `/api/health`, `/api/config`, `/api/catalog`, `/api/cart/*`, `/api/orders/*`

---

## Configuration

All settings are environment variables documented in
[.env.example](file:///c:/Users/user/OneDrive/Desktop/NaijaBasket/.env.example).
The important ones:

| Variable | Notes |
|---|---|
| `SECRET_KEY` | **Required in production** — the app refuses to boot without it (the serverless profile falls back to a demo key) |
| `ENCRYPTION_KEY` | **Required in production** — 64 hex chars; rotating it makes stored addresses unreadable (the serverless profile derives one from `SECRET_KEY`) |
| `DATABASE_URL` | Managed Postgres connection string; defaults to SQLite |
| `FLASK_ENV` | `development` / `testing` / `production` / `serverless` (auto-selected on Vercel) |
| `SESSION_COOKIE_SECURE=1` | Required behind Cloudflare HTTPS |
| `PROXY_FIX_HOPS` | Trusted proxy hops; `1` on a PaaS behind Cloudflare, `0` locally |
| `PAYSTACK_*` / `FLUTTERWAVE_*` | Blank ⇒ deterministic sandbox gateway |
| `KWIK_API_KEY` / `GOKADA_API_KEY` | Blank ⇒ deterministic sandbox courier |
| `WHATSAPP_TOKEN` / `WHATSAPP_PHONE_NUMBER_ID` | Blank ⇒ deterministic sandbox BSP that records the send |
| `WHATSAPP_APP_SECRET` / `WHATSAPP_VERIFY_TOKEN` | Verify inbound receipts and answer Meta's webhook handshake |
| `WHATSAPP_TEMPLATE_NAME` / `WHATSAPP_TEMPLATE_LANG` | Approved recovery template to send |
| `RATELIMIT_STORAGE_URI` | Shared Redis counter store; blank ⇒ per-process memory |
| `SENTRY_DSN` | Blank ⇒ error reporting disabled |
| `MIN_ORDER_VALUE`, `TARGET_AOV`, `CONVENIENCE_FEE`, `MERCHANT_SLA_MINUTES`, `DELIVERY_RADIUS_KM`, … | Business rules, PRD defaults |

---

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Covering the cart floor and single-store routing, order creation, escrow
hold/release/refund and PIN lockout, SLA warning→breach transitions, food-safety
lane segregation, webhook signatures, the WhatsApp recovery receipt state machine
(forward-only, `failed` terminal), role-based access, and page availability.

---

## Deployment

`render.yaml` is a one-click Render blueprint: a Docker web service, managed
Postgres 16, a shared environment group, and the two cron jobs. Migrations run
in `preDeployCommand` (`flask --app wsgi db upgrade`) before traffic shifts.

Cloudflare fronts the service for DNS, WAF and caching. Two things matter there:
**disable Rocket Loader** (it breaks the checkout and service-worker JavaScript)
and keep `SESSION_COOKIE_SECURE=1` with `PROXY_FIX_HOPS` set so gateway callback
URLs are built as `https://`.

`Procfile` and `Dockerfile` also cover Railway and Fly.io.

### Vercel (serverless)

The whole app deploys to Vercel as one Python function with **no other backend
service**. Vercel exports `VERCEL=1`, which auto-selects the `serverless`
profile: the database lives in the function's only writable path (`/tmp`),
connection pooling is disabled, and the demo dataset is bootstrapped on cold
start. The seed accounts above therefore work on a pristine deploy with nothing
to configure and nothing to run.

```powershell
npm i -g vercel
vercel            # preview deploy
vercel --prod     # production deploy
```

[vercel.json](file:///c:/Users/user/OneDrive/Desktop/NaijaBasket/vercel.json)
routes every path to `wsgi.py`; there is no separate frontend to build — the Jinja
templates and PWA assets ship inside the same function.

**Ephemeral by design.** `/tmp` is per-container and resets when the container is
recycled, so demo data (new orders, registrations) does not survive a cold start —
it simply re-seeds on the next one. To persist data, set `DATABASE_URL` to a
managed Postgres (Neon, Render, etc.); the same profile then uses it in place of
`/tmp`. The two time-driven sweeps do not run on Vercel (serverless has no
background worker and Hobby cron is once-per-day); trigger
`flask --app wsgi sla-check` / `cart-recovery` manually when needed.

### The two scheduled sweeps

FR-02 and FR-06 are time-driven and cannot run inside a request. Pick **one**
mechanism, never both:

- **`scheduler.py`** — an always-on worker for platforms with no native cron
  (Railway/Fly). Intervals via `SLA_INTERVAL_SECONDS` / `RECOVERY_INTERVAL_SECONDS`.
- **Render cron services** — the two `type: cron` entries in `render.yaml`
  running `sla-check` (every minute) and `cart-recovery` (every 5 minutes).

---

## Security notes

- Passwords: PBKDF2-SHA256, 600,000 iterations.
- PII (addresses): AES-256-GCM at rest, keyed by `ENCRYPTION_KEY` — NDPA 2023 posture.
- Delivery PINs: salted SHA-256, 3 attempts before the order is locked.
- Webhooks: HMAC-SHA512 (Paystack) / `verif-hash` (Flutterwave), and webhook amounts
  are reconciled against the order total — a mismatch triggers an automatic refund.
  WhatsApp receipts are HMAC-SHA256-verified (`X-Hub-Signature-256`) against `WHATSAPP_APP_SECRET`.
- Rate limits on login (per-IP **and** per-targeted-mailbox) and on all three webhook endpoints.

---

## Known gaps

- **Product imagery**: the catalogue now uses real, publicly hosted photography
  (Wikimedia Commons CDN) instead of a generator endpoint. Moving those assets
  into Cloudflare R2 is still the production step, so the storefront owns its
  own copies rather than hotlinking a third party.
- **WhatsApp templates**: the recovery flow sends a named template; the template
  itself must be submitted for Meta approval before `WHATSAPP_TOKEN` goes live.

---

Engineered by [Emmanuel Ogezi](https://emmanuelogezi.cv) — The Creative Director.
