<!-- SPDX-License-Identifier: BUSL-1.1 -->
# Throw-away Cloud Demo (`deploy/demo/`)

Deployment artifacts for the **per-visitor, scale-to-zero cloud demo**: a
visitor clicks *"Start test version"* on the landing page and gets their own
private URL to a fully working, isolated ProcWorks instance with realistic demo
data — no install. When no demo is running, cost is ~0.

> **This is not the production self-hosting path.** A durable installation uses
> the regular API image (`core/Dockerfile`) with a database (`DATABASE_URL`),
> fronted by the web image / Caddy. See the repository README. Everything here
> is deliberately **in-memory, throw-away, and boot-seeded**.

## How it fits together

```
Visitor ──"Start test version" + Captcha──▶  Landing page (static, CDN)
                                              │  POST /trial
                                              ▼
                                        Demo Broker  (broker/, scale-to-zero)
                                          · verify Captcha
                                          · concurrency / daily budget guard
                                          · create instance via ProvisionPort
                                          │
                                          ▼
                                 Container platform (e.g. Fly.io Machines)
                                   one isolated Micro-VM per visitor
                                   · in-memory, boot-seeded, one URL
                                   · auto-stop on idle (≈0 at rest)
                                          ▲
              Reaper (/admin/reap or reaper/, cron) ── hard-TTL destroy
```

The whole scheme leans on the two additive, default-off boot switches in the
API (both no-ops unless set, no correctness impact):

| Switch | Effect |
|--------|--------|
| `PROCWORKS_LOAD_DEMO=1` | Seed the built-in demo world once at boot (idempotent, only on an empty store). |
| `PROCWORKS_WEB_DIR=<path>` | Serve the static web client from the same process → **one container = whole app = one URL**. |

## Contents

- **`Dockerfile`** — combined demo image (API + static web client, in-memory,
  boot-seeded). Build from the **repository root**:
  ```bash
  docker build -f deploy/demo/Dockerfile -t procworks-demo .
  docker run --rm -p 8000:8000 procworks-demo   # open http://localhost:8000
  ```
- **`fly.toml`** — reference Fly.io Machines config (EU region, scale-to-zero,
  `min_machines_running = 0`, capped Micro-VM, `/health` check). Used for the D1
  manual proof and as the machine template the broker replicates per visitor.
- **`broker/`** — the demo broker (FastAPI).
  - `provision.py` — platform-neutral `ProvisionPort`
    (`create`/`start`/`stop`/`destroy`/`status`/`list_ids`) with an
    **`InMemoryProvisioner`** fake (local dev/tests) and a real **`FlyProvisioner`**
    that provisions **one Fly app per visitor** → unique `https://trial-<id>.fly.dev`.
  - `app.py` — `POST /trial`: **Turnstile Captcha** → budget/concurrency guard →
    provision → return URL. CORS-pinned to the marketing-site origin.
  - `test_broker.py` — local tests (Captcha, provisioner, guard, `/trial`).
    ```bash
    pip install -r broker/requirements.txt pytest httpx
    (cd broker && pytest -q)                     # run the tests
    (cd broker && uvicorn app:app --port 8080)   # or serve against the fake
    curl -X POST localhost:8080/trial -H 'content-type: application/json' \
         -d '{"captcha_token":"dev"}'
    ```
- **`landing/trial-button.html`** — self-contained "Start test version" snippet
  for the marketing site (Turnstile widget → POST to broker → redirect to the
  visitor's demo URL). Paste into `site/`; set the broker URL + Turnstile sitekey.
- **`reaper/reaper.py`** — idempotent CLI that destroys expired/orphaned demo
  **apps** (hard-TTL backstop on top of the platform's auto-stop). The same
  sweep is also reachable as `POST /admin/reap` on the broker, so a scheduler
  can trigger it without a second always-on service (see below).
- **`build-image.sh`** — builds the demo image from the **current public release
  repo** (not this private repo) and pushes `registry.fly.io/<app>:demo`, so every
  new instance runs exactly the released, customer-facing code. Automated by
  `.github/workflows/demo-image.yml` (daily + on demand; no-op until the
  `FLY_API_TOKEN` secret is set).
- **`d1-proof.sh`** — the D1 proof: create → reach → stop → start → destroy one
  demo Machine via the Machines REST API (see below).

## Image provenance — always the released code

New instances must always reflect the **current state of the public release
repo** (`procworks-release`), never internal working state. Mechanism:

1. `build-image.sh` clones the release repo (shallow) and builds+pushes
   `registry.fly.io/procworks-demo:demo` via Fly's remote builder.
2. The broker's `DEMO_IMAGE_REF` points at that `:demo` tag; a fresh Fly Machine
   pulls it on create, so every new trial runs whatever the release repo's HEAD
   was at the last build.
3. `.github/workflows/demo-image.yml` runs it **daily** and on
   `workflow_dispatch`, so the demo tracks releases automatically; run the
   workflow (or `build-image.sh`) right after a release for an immediate update.

> The release repo receives `deploy/demo/` only when a release/tag is cut (the
> customer-repo sync is a whitelist). Until the first such sync, `build-image.sh`
> exits with a clear message — cut a release first. (The **D1** build below
> deliberately builds from *this* repo, since it is just the local proof.)

## D2 — the "Start test version" flow

Decided hosting model (**live**):

- **Landing page + broker** — the button stays on `procworks.de` (IONOS,
  unchanged) and calls a **separately hosted, scale-to-zero broker** running as
  its own Fly app `procworks-demo-broker`, reachable at
  **`https://broker.procworks.de`** (IONOS A/AAAA → the Fly app, Fly-issued TLS
  cert). `BROKER_CORS_ORIGINS` is pinned to `https://procworks.de`;
  `CAPTCHA_SECRET` is unset (Captcha-free — rate limits + active cap guard it).
  Set `CAPTCHA_SECRET` + a Turnstile sitekey in `landing/trial-button.html` to
  turn Captcha on later.
- **Per-visitor URL** — **one Fly app per visitor** → `https://trial-<id>.fly.dev`
  (real isolation + unique URL, `min_machines_running = 0` per app ≈ 0 at rest).
  The reaper destroys the whole app at hard TTL.

Broker env: `FLY_API_TOKEN`, `FLY_ORG`, `DEMO_IMAGE_REF`, `FLY_REGION`,
`BROKER_CORS_ORIGINS`, `DEMO_PROVISIONER=fly`, and the limits below.
`CAPTCHA_SECRET` is **optional** (set it to require Turnstile; unset = no Captcha).
Use `DEMO_IMAGE_REF`, **not** `FLY_IMAGE_REF`: Fly injects `FLY_IMAGE_REF` at
runtime as the broker's *own* image, which would make it provision itself.
`DEMO_TTL_SECONDS` (default `7200` = 2 h) is the hard lifetime of a demo: the
broker reaps anything older on each `/trial`, so an abandoned tab is cleaned up
when the next visitor needs a slot — no always-on scheduler required. For
zero-traffic periods there is an optional backstop: set `DEMO_ADMIN_TOKEN` and
have any scheduler `POST /admin/reap` (Bearer token) to run the same sweep on a
cadence, or run `reaper/reaper.py` from a scheduler that has the Fly token (see
[Scheduled-reaper backstop](#scheduled-reaper-backstop-optional)). Set
`DEMO_TTL_SECONDS=0` to disable reaping entirely.

**Demo login.** The demo *image* sets `PROCWORKS_DEMO_MODE=1`, which makes the
SPA auto-login a fresh visitor as the seeded **modeler** and show a one-click
role-switch box (operator/viewer) — so visitors never face an empty login form.
This is a throw-away-demo-only switch; a real deployment leaves it unset and
never exposes any credentials.

### Contact gate (leads) + post-demo survey

A demo is **gated behind a short contact form**: `landing/trial-button.html`
reveals name + company + e-mail + a mandatory consent (and an optional marketing
opt-in) and only then POSTs `/trial`. The broker validates the fields (missing/
invalid → 422) and **relays the lead to the operator by e-mail, storing nothing**
(data minimisation). The lead is relayed *before* provisioning, so every demo
that boots has a delivered lead; if SMTP is configured but the relay fails, the
trial is refused (503) rather than silently dropping the lead. Configure the
relay via the `LEAD_SMTP_*` / `LEAD_MAIL_TO` secrets (see `broker/fly.toml`);
without SMTP the relay is skipped (local/dev).

When the visitor clicks **"Demo beenden"** in the demo banner, the SPA shows a
**2-minute survey** and POSTs it to the broker's `/feedback` (relayed by e-mail
too, best-effort, never blocks the visitor). The SPA only shows this when the
demo image sets `PROCWORKS_DEMO_FEEDBACK_URL` (→ surfaced on `/auth/config`).
Both the lead and the feedback mail carry the **trial id**, so the operator can
correlate them without any server-side store. The survey asks: role/context,
overall satisfaction (1–5), how important the *correctness-by-construction* value
is (1–5), ease of modelling (1–5), adoption intent, and one open-text field.

**DSGVO:** the mandatory consent covers only providing/supporting the test
access (Art. 6 (1) (b)); marketing contact is a **separate, optional** opt-in
(Art. 6 (1) (a)) so the demo does not hinge on it (Kopplungsverbot). No IP is
stored with a lead; the consent text links the Datenschutzerklärung
(`data-privacy-url` on the snippet). A Datenschutzerklärung must be live before
going public.

### Deploying the broker (productive rollout)

The broker runs as its own scale-to-zero Fly app (`broker/Dockerfile`,
`broker/fly.toml`). It needs a Fly token to create/destroy per-visitor apps —
provide it as a **secret**, never in `[env]`. **Run these yourself** (the token
is sensitive and the endpoint is public):

```bash
fly apps create procworks-demo-broker
# Provisioning token as a secret (ideally a deploy/org-scoped token, not a personal one):
fly secrets set FLY_API_TOKEN="$(fly auth token)" -a procworks-demo-broker
# Deploy from THIS directory so the build context has app.py/provision.py:
cd deploy/demo/broker && fly deploy -a procworks-demo-broker
# Smoke test (no Captcha configured -> empty body works):
curl -X POST https://procworks-demo-broker.fly.dev/trial \
     -H 'content-type: application/json' -d '{}'
#   -> {"trial_id":"...","url":"https://trial-....fly.dev","state":"created"}
```

Cost caps are set conservatively in `broker/fly.toml` (`DEMO_MAX_ACTIVE=5`,
`DEMO_MAX_PER_DAY=50`, `DEMO_MAX_PER_IP=3`); raise them once you trust the setup.

**DNS + button (last mile, on `procworks.de` / IONOS):**
1. Point `broker.procworks.de` at the broker (`fly certs add broker.procworks.de
   -a procworks-demo-broker`, then add the shown CNAME/A/AAAA at IONOS). Until
   then the broker is reachable at `https://procworks-demo-broker.fly.dev`.
2. Embed `landing/trial-button.html` into the landing page and set its
   `data-broker-url` to `https://broker.procworks.de/trial` (or the `.fly.dev`
   URL in the interim). No Turnstile needed; to enable it later, set the
   broker's `CAPTCHA_SECRET` and the widget's `data-sitekey`.

### Scheduled-reaper backstop (optional)

The broker already reaps expired demos opportunistically on every `/trial`, and
`min_machines_running = 0` means an abandoned app costs ≈ 0 while it waits. The
only gap is a **zero-traffic** stretch: with no new visitor, an expired app is
not swept until someone shows up. To close it without a second always-on
service, enable the on-demand sweep and point any scheduler at it:

```bash
# Enable the guarded poke endpoint (unset -> 404, so it is off by default):
fly secrets set DEMO_ADMIN_TOKEN="$(openssl rand -hex 16)" -a procworks-demo-broker

# Trigger a sweep (wakes the scale-to-zero broker, reaps, scales back down):
curl -X POST https://broker.procworks.de/admin/reap \
     -H "Authorization: Bearer $DEMO_ADMIN_TOKEN"
#   -> {"destroyed":["trial-..."],"count":1}
```

Drive that `curl` from whatever scheduler you already have — a Fly scheduled
Machine, a cron box, or an uptime pinger. Because the endpoint reuses the
broker's Fly token, no separate deployment (and no second copy of the token) is
needed. If you would rather run a job than poke an HTTP endpoint, the equivalent
CLI is `python reaper/reaper.py` with `DEMO_PROVISIONER=fly` + `FLY_API_TOKEN`
in its environment. Either path runs the same policy over the same
`ProvisionPort` seam and is idempotent, so it is safe to overlap with the
broker's own reaping.

### Observability (`GET /admin/metrics`)

Same guard as `/admin/reap` (`DEMO_ADMIN_TOKEN`; 404 when unset). A read-only
JSON snapshot for a small ops/cost view — the **live** active count comes
straight from the platform (the real cost driver), alongside the caps and
since-start counters:

```bash
curl -s https://broker.procworks.de/admin/metrics \
     -H "Authorization: Bearer $DEMO_ADMIN_TOKEN"
```
```json
{
  "active": 2, "max_active": 5, "ttl_seconds": 7200, "uptime_seconds": 1834,
  "counters": {
    "trials_started": 2, "trials_rejected_cap": 1, "trials_rejected_ratelimit": 0,
    "trials_rejected_gate": 3, "trials_failed": 0, "captcha_rejected": 0,
    "leads_relayed": 2, "lead_relay_failed": 0,
    "feedback_received": 1, "feedback_relayed": 1, "feedback_relay_failed": 0,
    "reaped": 0
  }
}
```

The counters are in-process and reset on a broker restart (best-effort, like the
API's `metrics.py`); they never influence request handling. `active` +
`trials_started` are what a cost view is built from — no fabricated euro figure.
Point any dashboard/pinger at it, or scrape it into your metrics stack.

**Watch `feedback_relay_failed`.** Feedback is best-effort — the visitor always
gets a thank you — and the broker stores nothing, so a broken SMTP channel would
discard every submission unnoticed. Unlike a lead (an undeliverable one answers
503 and refuses the trial, see `lead_relay_failed`), a lost survey is invisible
except through this counter and the matching `broker` log line (`fly logs`,
metadata only — never the answers). Non-zero means feedback is being lost right
now; check the `LEAD_SMTP_*` secrets first (e.g. after a mailbox password
rotation).

### Bounding the number of instances (no Captcha required)

Three independent limits make "infinitely many instances" impossible:

| Limit | Env (default) | How |
|-------|---------------|-----|
| **Concurrent cap** (authoritative) | `DEMO_MAX_ACTIVE` (20) | Before each trial the broker counts the **actually live** demo apps on the platform (`list_ids`) and refuses at the cap. Platform-sourced ⇒ survives a broker restart ⇒ true ceiling. |
| **Per-IP rate limit** | `DEMO_MAX_PER_IP` (3) / `DEMO_IP_WINDOW_SECONDS` (3600) | Sliding window per client IP, so one actor can't drain the budget. |
| **Global daily cap** | `DEMO_MAX_PER_DAY` (500) | Coarse absolute ceiling per UTC day. |

On top, the **reaper** enforces a hard TTL (`DEMO_TTL_SECONDS`), destroying any
demo app older than the TTL, so instances never accumulate — opportunistically
on each `/trial` and, optionally, on a schedule via `POST /admin/reap`. Every
limit returns a friendly HTTP 429.

## D1 — manual Fly proof (create → reach → stop → destroy)

Proves one isolated demo instance runs on a Fly Machine (EU), is reachable,
scales to zero on stop, and can be destroyed. Costs a few cents of compute while
running. Needs your own Fly account (`flyctl` + a token).

```bash
# Run from the REPOSITORY ROOT (the Dockerfile's build context needs core/ + web/).
# 1. Create the app and push the demo image (Fly's REMOTE builder -- no local Docker).
#    NOTE: fly resolves [build].dockerfile in fly.toml RELATIVE TO the config dir,
#    so fly.toml uses `dockerfile = "Dockerfile"` and the context is the cwd.
fly apps create procworks-demo
fly deploy --config deploy/demo/fly.toml --build-only --push --image-label demo
#   -> pushes registry.fly.io/procworks-demo:demo

# 2. Allocate a public IP so <app>.fly.dev routes (the Machines API alone does
#    NOT auto-allocate one when you create a bare Machine):
fly ips allocate-v4 --shared -a procworks-demo
fly ips allocate-v6 -a procworks-demo

# 3. Run the lifecycle proof against the Machines API:
export FLY_API_TOKEN="$(fly auth token)"
FLY_APP_NAME=procworks-demo \
FLY_IMAGE_REF=registry.fly.io/procworks-demo:demo \
deploy/demo/d1-proof.sh
```

The script (and the `FlyProvisioner`) exercise the exact same Machines API
calls, so a green run also validates the broker's provisioning path. Alternative
all-`flyctl` spot check: `fly machine list`, `fly machine stop <id>`,
`fly machine start <id>`, `fly machine destroy <id> --force`, `fly status`.

> **Egress-deny** (SSRF/abuse hardening) is a Fly *networking policy*, not a
> `fly.toml`/Machine-config field — apply it to the demo app out-of-band and
> keep all outbound-target env vars empty (see the Dockerfile).

## Security invariants for a public throw-away demo

These are baked into the `Dockerfile` and must stay true:

- **Licensing dormant** — leave `PROCWORKS_LICENSE_PUBKEY` **unset**. The demo
  seeds five agents; with the free quota of three, enforcement would block the
  demo (HTTP 402).
- **No outbound side effects** — leave `PROCWORKS_SMTP_*` (mail →
  `NullMailSender`) and `PROCWORKS_WEBHOOK_ALLOWLIST` / `PROCWORKS_CONNECTIONS` /
  `PROCWORKS_PUSH_ENDPOINTS` empty, **and block egress at the platform** (SSRF
  / abuse hardening).
- **No persistence, no secrets** — no `DATABASE_URL` (in-memory), fictitious
  demo data only.
- **Capped** — small Micro-VM, request timeouts, a global concurrency/daily
  budget guard in the broker, and a hard TTL enforced by the reaper.

## Status

**Live-verified on Fly (2026-07-14):**
- **D1** — `d1-proof.sh` against `procworks-demo`: create → `GET /health` 200 →
  stop (`stopped`, ~0 at rest) → start → destroy (`destroyed`). ✅
- **D2** — broker `/trial` (real `FlyProvisioner`): creates a per-visitor app,
  allocates a shared v4 (+v6), the visitor `https://trial-<id>.fly.dev/health`
  returns 200 and serves the seeded demo + SPA; the reaper lists the `trial-`
  apps and destroys them. ✅ (The **IP allocation** the app-per-visitor URL needs
  is now done in `FlyProvisioner.create` — a Machines-API app has no public IP.)

Local coverage: `broker/test_broker.py` (Turnstile, provisioner, guard, `/trial`)
with mocked platform/Captcha calls.

**Live since 2026-07-15:** the **broker** runs at `broker.procworks.de` and the
"Start test version" button is on `procworks.de`; a fresh visitor is
auto-logged-in as the seeded modeler. The reaper reads each app's real
**per-app age** (`instance_age_seconds`, Machine `created_at`) and reaps only
genuinely expired demos — opportunistically on `/trial` and, when a scheduler
pokes `POST /admin/reap`, on a cadence.

**Egress-hardened (app layer):** the demo image sets `PROCWORKS_EGRESS_DENY=1`,
so `outbox.assert_url_allowed` refuses **every** webhook/push target (HTTP 403) —
a visitor (a modeler here) cannot register a `POST /v1/webhooks` subscription that
turns the demo into an egress beacon. The stock SSRF guard (I6) only blocks
*internal* targets; this closes arbitrary *public* egress too. The data layer has
no target either: `register_connector` stores only connector metadata (no URL),
so with `PROCWORKS_CONNECTIONS` unset there is nothing to dial. A platform
egress-deny network policy remains defence-in-depth on top.

Still open before/for going fully public:
- wire up **Turnstile** for real (set `CAPTCHA_SECRET` + sitekey; the proof used
  the dev fallback) — optional, the rate limits + active cap guard it today;
- actually **schedule** the reaper poke (`/admin/reap`) on your scheduler of
  choice — the endpoint and token are ready, only the cron itself is an ops step;
- a **cross-replica** budget counter (currently in-process, single replica);
- a platform **egress-deny** network policy as belt-and-suspenders on top of the
  app-layer lockdown above;
- **D4 polish**: the broker exposes `GET /admin/metrics` (active count, caps,
  counters) — a rendered dashboard and a cold-start interstitial are still open.
