# AGENTS.md — Sponsor Scout

AI-assisted sponsorship discovery and outreach for college events. Read this file first; it is the map of the repo.
Project root = the directory containing this file. All commands below run from the root.

## 1. What the app does

Given an **event brief** (name, college, city, date, attendance, audience, sponsorship types wanted), the app:

1. **Discovers** candidate companies via LLM-written search queries → `SearchProvider` → filtering → LLM classification.
2. **Researches** each company by crawling a few public same-site pages and extracting *verbatim, source-linked facts*.
3. **Scores** sponsorship fit 0–100 with a transparent, rule-based rubric (the LLM never scores).
4. **Finds contacts** — only emails literally present on public pages. Never guesses.
5. **Drafts** a personalised email in which every company-specific claim cites a fact id → source URL.
6. Runs **quality checks**, then requires **human approval**, then **sends via Gmail** (OAuth) with duplicate/rate protection.
7. **Tracks** drafts, sends, replies (manually marked), conversion; everything is audit-logged.

It runs completely offline in **demo/mock mode** (fictional companies, deterministic mock LLM, mock mailer). That is the default when no keys are configured.

## 2. Tech stack

- Python 3.12, **Flask 3** (server-rendered Jinja templates, no SPA, no bundler, no build step)
- **SQLite** (WAL) via stdlib `sqlite3`; thread-local connections; no ORM
- `requests` for all HTTP (Gemini REST, Brave/Google search, Gmail REST, page fetching)
- `cryptography` (Fernet) for OAuth token encryption at rest
- Tests: stdlib **`unittest`** (pytest is *not* installed/used)
- Frontend: Jinja + one CSS file + ~40 lines vanilla JS (`app/static/`); CSP forbids inline scripts/styles
- Runtime deps are only in `requirements.txt` (Flask, requests, cryptography). Keep it that way unless there is a strong reason.

> The original brief asked for Next.js + PostgreSQL. It was built as Flask + SQLite because the build sandbox had no package registry. See §14 for the port path.

## 3. Commands

| Task | Command |
|---|---|
| Install | `pip install -r requirements.txt` (or `make install`) |
| Dev server + worker (port 5000, login `demo`) | `python run.py` (or `make dev`) |
| **Run all tests** | `python -m unittest discover -s tests -t .` (or `make test`) — 145 tests, ~4 s, no credentials |
| Run one test file / test | `python -m unittest tests.test_sending` / `python -m unittest tests.test_sending.DuplicateProtectionTests.test_plus_tag_variant_is_same_mailbox` |
| Syntax/import sanity | `make check` |
| Seed demo event only | `python -m app.cli seed` |
| Seed + run discovery/research synchronously | `python -m app.cli seed --full` |
| Drain the job queue synchronously | `python -m app.cli run-jobs` |
| Worker only | `WORKER_ENABLED=0` on the web process, then `python -m app.cli worker` |
| List Gemini models for your key | `python -m app.cli models` |
| Build | **None.** No compile/bundle step. |
| Production | `gunicorn -w 1 --threads 8 'app:create_app()'` with `APP_ENV=production SECRET_KEY=… ADMIN_PASSWORD=…` (gunicorn is not in requirements.txt; install it) |

Always run the full suite after changes. Scripts must be run with the repo root as cwd (`python -m …`); running a file directly needs `PYTHONPATH=.`.

## 4. Repository layout

```
AGENTS.md  README.md  Makefile  requirements.txt  .env.example  .gitignore  run.py
app/
  __init__.py       create_app(): config → services → blueprint → worker thread
  config.py         Settings dataclass, env loading, startup validation (prod refuses weak config)
  services.py       build_services(): wires providers by config; registers job handlers
  db.py             SCHEMA + Database (thread-local sqlite3, tx() context manager)
  repo.py           Data access helpers (events, companies, leads, contacts, drafts, audit, stats)
  jobs.py           DB-backed JobQueue + Worker threads; Defer / PermanentError
  pipeline.py       Orchestration: discover → research(cache) → score → generate_email → requality
  discovery.py      Query generation, search, host filtering, LLM classification
  prompts.py        ALL LLM prompts + JSON schemas (discovery, classify, synthesis, email)
  scoring.py        Rule-based fit score (RUBRIC) — no LLM
  emailgen.py       Fact selection, ask suggestion, draft finalisation, signature handling
  quality.py        Pre-send checks → status ok | needs_review | blocked
  sending.py        Approval gate, send guards, duplicate/rate protection, outcome marking
  normalize.py      Company/domain/email normalisation + validation (basis of dedupe)
  validators.py     Form validation (parse_event)
  security.py       Login decorator, CSRF, in-memory RateLimiter, security headers/CSP
  logging_setup.py  JSON logs + secret redaction (log_event)
  web.py            All routes (blueprint `web`)
  cli.py            init-db | seed [--full] | models | worker | run-jobs
  llm/              base.py (LLMProvider, JSON parse/validate) · gemini.py · mock.py · openai_stub.py
  search/           base.py · web.py (Brave, Google CSE) · mock.py
  research/         fetcher.py (safe HTTP + crawl) · extract.py (facts/people/quote verify) · contacts.py
  mailer/           base.py · gmail.py (OAuth+send) · accounts.py (encrypted tokens) · mock.py
  fixtures/mock_data.py   FICTIONAL demo companies + their fake web pages (demo mode only)
  templates/        base, macros, login, dashboard, campaign, leads, lead_detail
  static/           app.css, app.js
tests/              unittest suites + helpers.py (AppCase, ScriptedLLM, LogCapture)
data/               SQLite file lives here at runtime (gitignored)
```

## 5. Architecture

```
Browser ─► Flask routes (web.py: login · CSRF · rate limit)
              │ enqueue                                    ▲ polls /api/jobs
              ▼                                            │
        jobs table ──► Worker threads ──► handlers (services.py)
                                           ├ discover        → discovery.py → SearchProvider + LLMProvider
                                           ├ research        → pipeline.research_lead → fetcher/extract/contacts + LLM synthesis → scoring
                                           ├ generate_email  → emailgen.py + LLMProvider → quality.py
                                           └ send_email      → sending.send_now → EmailProvider (Gmail | Mock)
              all state in SQLite (repo.py); every action audited
```

- **Provider seams** (swap without touching callers): `LLMProvider` (`generate`, `generate_json`), `SearchProvider` (`search`), `EmailProvider` (`send`), `Fetcher` (`fetch`). `services.build_services()` is the only place that picks implementations.
- **Jobs**: `QUEUED → RUNNING → COMPLETED | FAILED`. `enqueue(..., dedupe_key=…)` collapses duplicate pending work. Handlers may raise `Defer(seconds)` (re-queue without consuming an attempt; used for send throttling) or `PermanentError` (no retry). Stale `RUNNING` jobs are re-queued at worker start.
- **Single process assumption**: in-memory rate limiter + SQLite + in-process worker. Scale with threads, not processes.

## 6. Database (`app/db.py`, SQLite, plain SQL)

Tables: `events`, `companies`, `research_sources`, `company_research`, `contacts`, `campaigns` (1:1 with event), `leads` (event×company), `email_drafts`, `email_sends`, `jobs`, `audit_logs`, `oauth_accounts`.

Constraints that implement the product rules — **do not remove or weaken**:
- `companies`: `UNIQUE(domain)`, `UNIQUE(normalized_name)`
- `leads`: `UNIQUE(event_id, company_id)`; `contacts`: `UNIQUE(company_id, normalized_email)`
- `company_research`: `UNIQUE(company_id, research_version)` → research cache key
- `email_sends`: **partial unique indexes** on live statuses (`SENDING/SENT/REPLIED/BOUNCED`): one per `normalized_email` globally, one per `(campaign_id, company_id)`. A `FAILED` send frees the slot.
- JSON columns only for flexible research data: `profile_json`, `facts_json`, `stats_json`, `score_json`, `personalization_json`, `evidence_json`, `quality_json`.
- **No migration system.** Schema is `CREATE … IF NOT EXISTS`. For schema changes either add an idempotent migration step in `Database.init()` or delete `data/*.db` in dev.

Draft statuses: `DRAFT`, `NEEDS_REVIEW`, `APPROVED`, `SENT`, `FAILED`, `REPLIED`, `BOUNCED`. `leads.email_status` mirrors the latest draft status (`NONE` before any draft). `leads.research_status`: `NOT_STARTED/QUEUED/RUNNING/COMPLETED/FAILED`. `leads.contact_status`: `unknown/found/not_found`.

## 7. HTTP routes (`app/web.py`)

All routes except `/login` and `/healthz` require login. Every POST requires a CSRF token (`csrf_token` form field or `X-CSRF-Token` header) and is rate-limited per IP by bucket (`login`, `send`, `expensive`, `post` in `security.LIMITS`).

| Method | Path | Purpose |
|---|---|---|
| GET/POST | `/login`, POST `/logout` | Single admin password (`ADMIN_PASSWORD`) |
| GET | `/` | Dashboard: totals + events + new-event form |
| POST | `/events` | Create event + campaign, enqueue `discover` |
| GET | `/campaigns/<eid>` | Campaign page (pipeline counts, jobs, top leads) |
| POST | `/campaigns/<eid>/find-sponsors` | Re-run discovery |
| POST | `/campaigns/<eid>/bulk` | `action=` `research_all` · `generate_high_fit` · `send_approved` · `pause` · `resume` |
| GET | `/leads?event=&q=&filter=high` | Leads table |
| GET | `/leads/<lid>` | Lead detail: score + evidence, research, contacts, sources, email editor, activity |
| POST | `/leads/<lid>/research` (`force=1` = refresh), `/generate`, `/contact` | Enqueue research / draft; add a manually verified contact |
| POST | `/drafts/<did>/save` · `/approve` (`acknowledge=1` for warnings) · `/unapprove` · `/send` · `/mark` (`outcome=REPLIED|BOUNCED`) · `/reopen` | Draft lifecycle |
| GET | `/gmail/connect`, `/gmail/callback` | Google OAuth (state + PKCE) |
| POST | `/gmail/demo-connect`, `/gmail/disconnect` | Demo sender (mock mode only) / disconnect |
| GET | `/api/jobs?event=` | JSON job summary + stats (used by `app.js` polling) |
| GET | `/healthz` | Liveness |

## 8. Pipelines

### 8.1 Lead discovery (`discovery.py`, `pipeline.discover`)
1. LLM `discovery_queries` (prompt in `prompts.py`) → ≤12 queries; on LLM failure → `fallback_queries()`.
2. `SearchProvider.search()` per query; results grouped by normalised domain; `NON_COMPANY_HOSTS`/`.edu`/`.gov` etc. dropped.
3. LLM `candidate_classify` in batches of 8 → `is_company`, `relevance` (0–1), `industry`, `reason_relevant`. Domains the model invents that were not in search results are ignored. Accept if relevance ≥ 0.4.
4. `Repo.upsert_company` dedupes on domain then normalised name; `ensure_lead` dedupes per event. New leads get a `research` job each.
- `reason_relevant` is model-written from snippets and is **labelled unverified** in the UI. Only extracted facts are evidence.

### 8.2 Research (`pipeline.research_company`, `research/*`)
1. **Cache**: reuse `company_research` for `(company, RESEARCH_VERSION)` if younger than `RESEARCH_TTL_DAYS`, unless `force`. Shared across events. Bump `RESEARCH_VERSION` when extraction logic changes.
2. `crawl_company`: homepage, then ranked same-site links (sponsor/partner/student/community/…; fallback common paths), ≤ `MAX_PAGES_PER_COMPANY`.
3. `HttpFetcher`: robots.txt honoured, UA identifies the bot, per-host delay, ≤1.5 MB body, ≤3 manually-checked redirects, HTML only, **SSRF guard** (`is_public_host`: every resolved IP must be global). Parsed text capped at 400k chars; email finding is linear-time (a quadratic regex once froze workers — regression test exists).
4. `extract_facts`: sentence-level keyword rules → categories (`sponsorship`, `hackathon`, `student_program`, `university_partnership`, `credits`, `ambassador`, `developer_program`, `india`, `delhi_ncr`, `hiring`, `launch`). Each fact = verbatim sentence + `source_url` + id `F1…`.
5. LLM `research_synthesis` produces summary/industry/products/launches. **Every product/launch needs a `quote` that appears verbatim on the cited page** (`verify_quote`) or it is discarded and counted in `stats.claims_rejected`. LLM failure → fallback summary from page text.
6. `discover_contacts` (see §8.4), store sources/contacts/research in one transaction, then `score_lead` per lead.

### 8.3 Fit scoring (`scoring.py`)
Student Audience 20 · Technology Relevance 20 · Sponsorship History 20 · India Presence 15 · Developer/Community 15 · Event Scale 10 = 100. Output: `{score, breakdown{label,score,max,reasons}, reasons[], evidence[{claim,source,fact_id}]}`. Nothing found ⇒ 0 and a `unknown: …` reason. Thresholds are hand-tuned heuristics (see §13). `HIGH_FIT_THRESHOLD` (70) defines "high fit"; `MIN_FIT_FOR_EMAIL` (40) triggers a quality warning.

### 8.4 Contact discovery (`research/contacts.py`)
Emails come only from page text/`mailto:` links. Excludes `noreply/privacy/legal/security/careers/press/support…`. Department/priority from local part (sponsorship 1 > partnerships 2 > community 3 > devrel 4 > marketing 5 > campus 6 > founder 7 > generic 8). Confidence = base + role specificity + relevant page + mailto + same-domain; external-domain addresses capped at 0.5. Names/roles attached only from nearby text. No contact ⇒ `contact_status='not_found'` and email generation is refused (a human may add a verified contact via `/leads/<id>/contact`).

### 8.5 Email generation (`emailgen.py`, `quality.py`, `pipeline.generate_email`)
1. `select_facts` (≤8, ranked by category) → `suggest_ask` maps evidence → one of the event's requested sponsorship types.
2. `prompts.email_prompt` embeds event, benefits, contact, ask options and the numbered VERIFIED FACTS; rules forbid inventing facts/numbers/links.
3. LLM returns `{subject, body, personalization[{sentence, fact_ids, why}], cta, suggested_ask}`. `finalize_draft` drops unknown fact ids, builds `evidence[]` from real facts, strips any model signature and appends the configured sender signature.
4. `quality.evaluate` → checks: recipient (block), length 100–180 words (block <40/>300), evidence (block if none), numbers not in brief/facts (warn), greeting vs verified contact, spam phrases/links/caps/generic openers, fit score, contact confidence. Any block ⇒ cannot be approved; any warn ⇒ `NEEDS_REVIEW` (approval requires `acknowledge`).
5. Regeneration updates the draft in place (version++), never after a send.

### 8.6 Sending (`sending.py`)
`approve` (re-runs quality) → `request_send` enqueues `send_email` → `send_now` checks: approved-by-human, valid recipient (placeholder domains blocked when `MAIL_PROVIDER=gmail`), campaign ACTIVE, **daily cap**, **min interval** (`Defer`), **company cooldown** across campaigns, then inserts an `email_sends` row (`SENDING`) — the partial unique indexes make duplicates fail atomically — calls the provider, records message/thread id, audit-logs. Provider failure ⇒ `FAILED` row + draft `FAILED` (human can `reopen`). Editing a draft resets approval.

## 9. Gemini integration (`app/llm/gemini.py`)

- REST `POST /v1beta/models/{model}:generateContent`, key in `x-goog-api-key` **header** (never URL). JSON mode = `responseMimeType: application/json`; schema is described in the prompt and **validated locally** (`validate_schema`), with one repair retry. No `thinkingConfig`/schema params are sent, to stay compatible across model generations.
- `GEMINI_MODEL` empty ⇒ `resolve_model()` calls `models.list` and `rank_models()` picks the best **stable** text model (newest version, pro > flash > flash-lite), falling back to previews. Set `GEMINI_MODEL` to pin an exact id (e.g. a `gemini-3.x` id if your key lists it). A 404 produces a message pointing to `python -m app.cli models`. Never fake an unavailable model.
- Retries 429/5xx with backoff; logs `llm_request` / `llm_failure` (no prompts, no keys).
- Add providers by implementing `LLMProvider` and registering in `llm/__init__.py:get_llm`. `OpenAIProvider` is an **intentional stub**. `MockProvider` is task-aware (`task=` + `context=` kwargs are ignored by real providers) and builds outputs only from the supplied context.

## 10. Gmail OAuth integration (`app/mailer/`)

- Authorization-code flow with **PKCE** and a session-bound `state`; scopes `openid email gmail.send` only; `access_type=offline`, `prompt=select_account consent`.
- `/gmail/connect` → Google → `/gmail/callback` → `GmailProvider.complete_oauth` verifies the `gmail.send` scope was granted, fetches the account email, stores tokens via `AccountStore` (**Fernet-encrypted, key derived from `SECRET_KEY`**; changing `SECRET_KEY` makes tokens undecryptable ⇒ reconnect).
- `send` builds an RFC-822 message (`EmailMessage` rejects header injection), refreshes the access token when expired or on 401 (once), maps `invalid_grant` to `MailAuthError` and deactivates the account.
- Everything Gmail-specific is behind `EmailProvider`; `MockMailer` records to an in-memory `outbox` (demo/tests).
- Replies/bounces are **not** auto-detected (needs restricted read scope); they are marked manually.

## 11. Environment variables

See `.env.example` (authoritative; a consistency check once confirmed every variable read in `config.py` is documented). Summary:

`APP_ENV`, `SECRET_KEY`, `ADMIN_PASSWORD`, `DATABASE_URL` (sqlite only) · `LLM_PROVIDER`, `GEMINI_API_KEY`, `GEMINI_MODEL` · `SEARCH_PROVIDER` (`mock|brave|google_cse`), `BRAVE_API_KEY`, `GOOGLE_CSE_KEY`, `GOOGLE_CSE_CX` · `MAIL_PROVIDER`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` · `SENDER_NAME/ROLE/ORG` · `RESEARCH_VERSION`, `RESEARCH_TTL_DAYS`, `MAX_PAGES_PER_COMPANY`, `FETCH_DELAY_SECONDS`, `HIGH_FIT_THRESHOLD`, `MIN_FIT_FOR_EMAIL`, `CAMPAIGN_DAILY_LIMIT`, `SEND_INTERVAL_SECONDS`, `COMPANY_COOLDOWN_DAYS`, `WORKER_THREADS`, `WORKER_ENABLED`, `LOG_LEVEL`.

Defaults: `LLM_PROVIDER` = gemini if key set else mock; `MAIL_PROVIDER` = gmail if client id set else mock; `SEARCH_PROVIDER` = mock; `SEND_INTERVAL_SECONDS` is forced to 0 for the mock mailer. In production the app refuses to start without a ≥32-char `SECRET_KEY` and an `ADMIN_PASSWORD`. In development an empty `ADMIN_PASSWORD` means `demo`.

**Never commit `.env`. Never read it with tools. Never log or render keys/tokens.**

## 12. Invariants — do not break (each has tests)

1. **No fabrication.** Company claims must trace to an extracted fact (verbatim sentence + URL). Contacts only from public pages; never guess/derive addresses. Unknown ⇒ `unknown`/`not_found`.
2. **The LLM never produces scores or evidence.** It summarises (quote-verified) and writes email prose citing fact ids.
3. **Human approval gate.** Only `APPROVED` drafts with `approved_by` can send; any edit revokes approval; blocked drafts cannot be approved.
4. **Duplicate protection is enforced in the DB**, not only in app code (partial unique indexes).
5. **No hardcoded companies in logic.** Fictional companies live only in `app/fixtures/mock_data.py` / `search/mock.py` (a test greps for this).
6. **Secrets stay server-side and out of logs** (`logging_setup.redact`, encrypted token store, tests grep rendered pages and logs).
7. **Fetcher is polite and safe**: robots.txt, SSRF guard, size/redirect caps.
8. All POSTs: login + CSRF + rate limit; CSP has no `unsafe-inline` (so no inline `<script>`/`style=` in templates — add CSS classes instead).
9. Provider seams stay clean: callers depend on `LLMProvider`/`SearchProvider`/`EmailProvider`, not Gemini/Brave/Gmail types.

When changing safety-critical code, mutation-check it: break the behaviour temporarily and confirm a test fails.

## 13. Current status

**Done and tested offline (145 tests):** full pipeline in demo mode through real HTTP routes (`tests/test_e2e.py` mirrors the original "definition of done"); Gemini provider (mocked HTTP); Brave/Google CSE adapters (mocked HTTP); Gmail OAuth/send/refresh/revocation (mocked HTTP); real `HttpFetcher` against a local server; job queue; duplicate/approval/rate-limit logic; CSRF/auth/secret hygiene.

**Not verified against real services (no network/credentials when built):** live Gemini responses, live Brave/Google search, live Gmail OAuth + send, crawling real websites. Expect small API-shape fixes on first live run. First live test: set `GEMINI_API_KEY` only, run `python -m app.cli models`, then discovery with a real `SEARCH_PROVIDER`, and send 2–3 emails to yourself.

### Known bugs / limitations
- Stack differs from the brief (Flask + SQLite, not Next.js + Postgres). `DATABASE_URL` other than `sqlite:///…` raises `NotImplementedError`.
- **Behind a reverse proxy**, `request.remote_addr` is the proxy IP, so all users share one rate-limit bucket. Needs `werkzeug.middleware.proxy_fix.ProxyFix` configured deliberately.
- Rate limiter is in-memory/per-process; SQLite + in-process worker assume one process.
- No DB migrations (see §6). No pagination on the leads table. No draft diff/history view.
- "Find more sponsors" re-runs the same queries; there is no paging/offset or query diversification beyond what the LLM generates, so repeat runs on real search mostly dedupe to nothing.
- Discovery relevance cut-off (0.4) and the scoring thresholds are uncalibrated heuristics; fact extraction is keyword-based (can miss/over-count; English only; no JavaScript rendering).
- Quality "accuracy" check only verifies numbers; it does not verify names/products in free text (the evidence-id requirement is the main defence).
- `strip_signature` splits on the first `--` line; a body containing a bare `--` line loses the text after it.
- Manual contacts get a fixed confidence 0.9 and a `manual entry by …` source.
- Single admin user; audit actor is always `admin`. `AccountStore.select` and `Repo.list_leads(order=)` are unused stubs.
- Replies/bounces are manual. No unsubscribe/suppression list beyond the per-mailbox send guard and the opt-out line in the email text.
- `OpenAIProvider` is a stub. Mock LLM emails quote facts verbatim and read stiffly by design.

## 14. Unfinished work / suggested next steps (priority order)

1. **First live run & hardening** — verify Gemini JSON mode with the real model, Brave/Google adapters, and Gmail OAuth/send end-to-end; fix response-shape surprises; add tests from real (redacted) payloads.
2. **Suppression / unsubscribe list** (table + check in `send_now`) and a visible "do not contact" action.
3. **Reply/bounce detection** — optional `gmail.readonly` (restricted scope; document the verification burden) or a pasted-reply flow.
4. **Schema migrations** — add a tiny versioned-migration runner in `db.py` before any schema change ships to real data.
5. **Reverse-proxy-safe rate limiting** (`ProxyFix`) and optional Redis-backed limiter if multi-process is ever needed.
6. **Discovery quality** — paging/query diversification, per-category quotas, use `categories`/`keywords` better, "previously sponsored similar events" signal from search snippets.
7. **UX** — pagination/sorting on leads, side-by-side draft versions, bulk approve with per-draft review, nicer empty/error states, accessibility pass.
8. **Postgres port (and/or Next.js frontend)** — isolate SQL in `db.py`/`repo.py` (already the only places with SQL besides `jobs.py`, `sending.py`, `pipeline.py` inline statements — consolidate first), swap `sqlite3` for `psycopg`, replace `BEGIN IMMEDIATE`/partial indexes accordingly (Postgres supports partial unique indexes). If adding a Next.js UI, keep Flask as a JSON API and add API-token auth; do not expose provider keys to the browser.
9. **Multi-user** with roles and per-user audit actor.

## 15. Architectural decisions (and why)

- **Deterministic facts + LLM prose**: evidence is extracted by rules and is verifiable; the LLM can only cite ids it was given. Reduces hallucination risk and makes "why did you write this sentence?" answerable (shown on the lead page).
- **Rule-based score**: explainable, testable, no hidden model drift.
- **DB-backed queue + threads** instead of Redis: simplest reliable MVP with one process; handlers are idempotent enough (dedupe keys, cache, unique indexes).
- **Server-rendered Jinja**: no build step, CSP-strict, small attack surface; JS only polls job status.
- **Schema-described JSON, validated locally** rather than provider-specific response schemas: portable across Gemini versions/providers.
- **gmail.send scope only**: least privilege; therefore no reply tracking.
- **Mock providers are first-class**: the whole product runs and is testable offline; `ScriptedLLM` in `tests/helpers.py` injects bad model output to test defences.

## 16. Working conventions for agents

- Run `python -m unittest discover -s tests -t .` before and after changes; keep it green and offline (no real network/credentials in tests — mock `requests`, use `AppCase` from `tests/helpers.py`).
- Prefer editing existing modules over adding abstractions; keep dependencies minimal.
- New LLM prompt ⇒ put it in `prompts.py`, add a task handler to `llm/mock.py`, add a test with `ScriptedLLM` for malformed output.
- New env var ⇒ add to `config.py` **and** `.env.example` **and** §11 here.
- New route ⇒ POST + CSRF + login; add a test; update §7.
- New rubric dimension ⇒ update `RUBRIC` (must still sum to 100), tests in `tests/test_scoring.py`, and the lead-detail template renders it automatically.
- Do not paste real keys into code, tests, logs, or commits. Test fixtures use obviously fake strings on purpose.
- Update this file when behaviour, commands or status change.
