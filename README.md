# Sponsor Scout: AI event-sponsor discovery and outreach

> Continuing development with an AI coding agent such as a coding agent? Start with [`AGENTS.md`](AGENTS.md): architecture, invariants, status, known issues and next steps. `make test`, `make dev` etc. are in the `Makefile`.

Give it a college event. It finds companies that could sponsor it, researches each one from public pages, scores sponsorship fit with an explainable rubric, finds public contacts, drafts a personalised email whose every factual claim links to a source, and sends through Gmail **only after a human approves it**.

```
Event → Discover → Filter → Research → Score → Contacts → Draft → Review/Approve → Gmail send → Track
```

It runs fully offline in **demo mode** (fictional companies, mock LLM, mock mailer), so you can try the whole flow with no keys.

## Quick start

```bash
pip install -r requirements.txt
python run.py                     # http://localhost:5000  (dev password: demo)
python -m unittest discover -s tests -t .   # 189 tests, no credentials needed
```

In the UI: **Dashboard → Create event and find sponsors** → watch the job bar → open a lead → *Generate email* → *Approve* → *Send*. In demo mode click **Connect demo sender** first; nothing leaves your machine.

## Architecture

```mermaid
flowchart LR
  UI[Dashboard<br/>Flask + Jinja] --> API[Routes<br/>auth · CSRF · rate limit]
  API --> Q[(Job queue<br/>SQLite table)]
  Q --> W[Worker threads]
  W --> D[Discovery]
  W --> R[Research]
  W --> G[Email generation]
  W --> S[Sender]
  D --> SP{{SearchProvider<br/>Web · Mock}}
  D --> L{{LLMProvider<br/>CLI (Google account) · Gemini API · Mock · OpenAI stub}}
  R --> F[Fetcher<br/>robots.txt · SSRF guard]
  R --> L
  G --> L
  S --> M{{EmailProvider<br/>Gmail · Mock}}
  D & R & G & S --> DB[(SQLite<br/>relational schema)]
  API --> DB
```

| Concern | Where |
|---|---|
| LLM abstraction (`generate`, `generate_json`) | `app/llm/` (`cli.py` Google-account CLI route, `gemini.py` API-key route, Mock, OpenAI stub) |
| Search abstraction | `app/search/` (Brave and Google CSE adapters, Mock) |
| Discovery, filtering | `app/discovery.py` |
| Fetch, extract facts, contacts | `app/research/` |
| Fit score (rule-based, explainable) | `app/scoring.py` |
| Email generation, quality gate | `app/emailgen.py`, `app/quality.py` |
| Approval, send guards, duplicate protection | `app/sending.py`, partial unique indexes in `app/db.py` |
| Gmail OAuth (PKCE), encrypted token store | `app/mailer/` |
| Job queue and worker | `app/jobs.py` |
| Routes, security | `app/web.py`, `app/security.py` |

### How hallucinations are prevented
- **Facts are extracted deterministically**: each is a verbatim sentence from a fetched page plus its URL (`F1`, `F2`, ...).
- The LLM only **summarises**. Any product or launch claim it returns must carry a quote that exists word for word on the cited page, or it is discarded (and counted in the UI).
- **The score never uses the LLM.** Every evidence line in a score comes from the extracted facts.
- **Contacts are only emails that literally appear on public pages.** Nothing is guessed. No email means `contact_status = not_found`.
- The email model must cite fact ids per personalised sentence. Unknown ids are dropped; no valid citation means the draft is **blocked** from approval. Numbers in the body that appear in neither the event brief nor the cited facts raise a warning.
- Lead detail shows "why each claim was written", with the fact and its source URL.
- **AI commentary is advisory.** The model may add a fit commentary and a fact-check of each draft, but a "strength" is kept only if it cites real fact ids, a flagged sentence counts only if it literally appears in the email, and these checks can only add warnings, never score, approve or unblock anything.

### Draft statuses
`DRAFT` · `NEEDS_REVIEW` (quality warnings; approval requires ticking an acknowledgement) · `APPROVED` · `SENT` · `FAILED` · `REPLIED` · `BOUNCED`. Blocking problems (no valid recipient, no evidence, absurd length) cannot be approved at all. Editing a draft resets approval.

### Duplicate and send protection
- Company: unique `domain` and `normalized_name` (legal suffixes stripped, case and punctuation folded).
- Mailbox: normalised (case, `+tag`, Gmail dots) and enforced by a partial unique index on live sends. It holds even if app logic is bypassed.
- One live send per company per campaign (partial unique index). 90-day cooldown across campaigns (`COMPANY_COOLDOWN_DAYS`).
- Campaign daily cap (`CAMPAIGN_DAILY_LIMIT`), minimum gap between sends (`SEND_INTERVAL_SECONDS`, via job deferral), pause/resume per campaign.
- Every email carries a plain opt-out line. There is nothing that evades spam filtering.

## Environment variables
See `.env.example`. Key ones:

| Variable | Purpose |
|---|---|
| `APP_ENV` | `development` or `production`. Production refuses to start without `SECRET_KEY` (≥32 chars) and `ADMIN_PASSWORD` |
| `SECRET_KEY` | Signs sessions and derives the key that encrypts OAuth tokens at rest |
| `LLM_PROVIDER` | `mock` (default, offline) · `cli` (your Google account via the CLI, primary) · `gemini` (API key, optional) |
| `LLM_CLI_BIN`, `LLM_CLI_MODEL`, `LLM_CLI_TIMEOUT_SECONDS`, `LLM_CLI_CONCURRENCY` | CLI route settings (see below) |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | **Optional**, only for `LLM_PROVIDER=gemini` |
| `SEARCH_PROVIDER` | `mock` / `brave` (`BRAVE_API_KEY`) / `google_cse` (`GOOGLE_CSE_KEY`, `GOOGLE_CSE_CX`) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` | Gmail OAuth |
| `SENDER_NAME`, `SENDER_ROLE`, `SENDER_ORG` | Signature appended to every email |
| `DATABASE_URL` | `sqlite:///data/app.db` (see limitations) |

Provider defaults: `LLM_PROVIDER` is `gemini` if only a key is set (legacy), else `mock`; `MAIL_PROVIDER` is `gmail` if a client id is set, else `mock`.

## Google AI Pro via the CLI (primary real LLM route)

A Google AI Pro subscription is **not** an API key, and this app does not ask you for one. Instead the app launches Google's official command-line agent as a child process, using **the sign-in you performed yourself**. The CLI keeps its own credentials; the app never sees, stores or forwards them.

```
Application → LLM service → LLMProvider ─┬─ CliProvider  → agy (or gemini) → your signed-in Google account
                                          ├─ GeminiProvider (optional, GEMINI_API_KEY, billed separately)
                                          └─ MockProvider (offline)
```

**Important, verified current state (checked 2026-10-02 from public sources):** Google announced on 2026-05-19 that **Gemini CLI stopped serving Google AI Pro/Ultra and free individual accounts on 2026-06-18**. Its successor for those accounts is **Antigravity CLI (`agy`)**. Gemini CLI remains usable only with paid API keys, Vertex AI or Gemini Code Assist Standard/Enterprise. So the default binary here is `agy`; the legacy `gemini` binary is supported via `LLM_CLI_BIN=gemini` for those other auth modes.

Setup (once, on the machine that runs the app):
1. Install Antigravity CLI from Google's official instructions and run `agy` interactively. Choose **Sign in with Google** with the account that has AI Pro. Complete the browser flow. (Over SSH it prints a URL to open on your own machine.)
2. In `.env` set `LLM_PROVIDER=cli` (leave `GEMINI_API_KEY` empty).
3. Run `make llm-check` (`python -m app.cli llm-check`). It prints the CLI version and sends one tiny prompt. If it says you are not signed in, repeat step 1.

How the app calls it (headless, one call per LLM task): `agy --output-format json [--print-timeout Ns] [--model M] -p "<prompt>"`, reads the JSON envelope (`response`, `error`, `status`) from stdout, and validates any JSON the task asked for locally (one repair retry).

Guard rails (all tested): no `--dangerously-skip-permissions`; the agent is told not to use tools and runs in an empty temp directory; the child process gets an **allow-listed environment**, so `SECRET_KEY`, `GOOGLE_CLIENT_SECRET`, `GEMINI_API_KEY` etc. are not inherited; quota/auth errors are reported as such and **never retried or worked around**; `LLM_CLI_CONCURRENCY=1` by default; prompts and credentials are never logged.

Things to know:
- Usage counts against your subscription's own limits. Discovery plus research plus drafting for ~40 companies is dozens of calls; `LLM_FIT_ANALYSIS=0` and `LLM_EMAIL_REVIEW=0` remove the two optional extra calls per lead/draft. When the quota is hit, jobs fail with a clear message; wait for the reset.
- This route is **not verified against a live account in this repository's build environment** (no network access to Google, no account). The argv/JSON-envelope contract comes from public documentation and community reports. Run `make llm-check` first, then one small discovery, and adjust `app/llm/cli.py` if the CLI's output shape differs.
- Check Google's current terms for your plan to confirm that scripted, local, single-user use of the CLI is acceptable for you. The app keeps use modest (human-approved sends, serial calls) and does not try to circumvent limits or authentication.
- If your CLI route is unavailable, the optional API route is below. Nothing else in the app depends on which provider you pick.

### Optional: Gemini API key
Only used when `LLM_PROVIDER=gemini`. It is a separate, separately billed (or free-tier) product from AI Pro.
1. Create a key in Google AI Studio and put it in `GEMINI_API_KEY`.
2. Run `python -m app.cli models` to list what **your key** can call. The app ranks stable text models first (newest version, pro > flash > flash-lite), then previews.
3. To use a specific model, set `GEMINI_MODEL` to the exact id the command prints. A wrong id produces an error telling you to run the command above; the app never fakes an unavailable model.

## Search setup
Discovery needs a search API. Set `SEARCH_PROVIDER=brave` with `BRAVE_API_KEY`, or `google_cse` with a Programmable Search key and engine id. The app does not scrape search engines. The fetcher respects `robots.txt`, identifies itself, rate-limits per host, caps page size and redirects, and refuses private/loopback addresses (SSRF guard).

## Google OAuth setup (Gmail)
1. Google Cloud Console → new project → **Enable the Gmail API**.
2. **OAuth consent screen** → External → add yourself as a **test user**.
3. **Credentials → OAuth client ID → Web application**. Add the redirect URI `http://localhost:5000/gmail/callback` (and your production URL later).
4. Put the id, secret and redirect URI into `.env`, set `MAIL_PROVIDER=gmail`, restart.
5. Click **Connect Gmail**, choose the account, approve. Only the `gmail.send` scope (plus `openid email`) is requested. No password is ever stored; the refresh token is encrypted at rest.

Note: while the consent screen is in *Testing*, Google expires refresh tokens after about 7 days, so you will need to reconnect. Sending to many recipients from a personal account is subject to Google's own sending limits.

## Database
SQLite file at `data/app.db`, created automatically. Entities: `events`, `companies`, `company_research`, `research_sources`, `contacts`, `campaigns`, `leads`, `email_drafts`, `email_sends`, `jobs`, `audit_logs`, `oauth_accounts`. JSON is used only for flexible research metadata (facts, profile, score breakdown).

## Development
```bash
python run.py                        # dev server + in-process worker, seeds the demo event
python -m app.cli seed --full        # create demo event and run discovery/research synchronously
python -m app.cli llm-check          # smoke-test the configured LLM provider (proves CLI sign-in)
python -m app.cli models             # (API-key mode) list Gemini models for your key
python -m app.cli worker             # worker only (when WORKER_ENABLED=0 on the web process)
```

## Testing
`python -m unittest discover -s tests -t .` (or `make test`) runs 189 offline tests: normalisation, scoring, fact/contact extraction, real-socket HTTP fetcher (robots, redirects, SSRF guard, hostile pages), search adapters, prompt construction, email generation and quality gate, the CLI LLM provider (against a fake CLI executable: argv shape, env isolation, quota/auth classification, timeouts, JSON repair), advisory AI review (hallucinated findings ignored), Gemini API provider (mocked HTTP), Gmail OAuth/send (mocked HTTP), job queue, duplicate protection, approval gate, rate limits, CSRF/auth, secret redaction, and a complete end-to-end walkthrough through the HTTP routes. Safety tests were mutation-checked (removing the duplicate index, the approval check, CSRF, quote verification, the no-guessing rule, or log redaction makes tests fail).

## Production deployment
```bash
pip install gunicorn
APP_ENV=production SECRET_KEY=... ADMIN_PASSWORD=... \
gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 'app:create_app()'
```
- **Run one process.** The rate limiter is in memory and SQLite plus the in-process worker assume a single instance. Scale with threads.
- Terminate TLS in front (nginx/Caddy). Session cookies become `Secure` in production.
- Back up `data/app.db`. Keep `SECRET_KEY` stable; changing it makes stored OAuth tokens undecryptable (you reconnect Gmail).
- Verify your Google OAuth app (sensitive scope) before using it beyond test users.

## Known limitations
- **Stack differs from the brief.** Built as Flask + Jinja + SQLite, not Next.js + PostgreSQL. The build environment had no package registry, and a Node toolchain could not be installed or tested. The schema is plain relational SQL and the data layer is isolated in `app/db.py` and `app/repo.py`, so porting to Postgres is mechanical but not done.
- **Live integrations were not exercised against real services.** The Google CLI route, Gemini API, Brave/Google search, Gmail OAuth and the HTTP fetcher are tested against fakes/mocked HTTP, but this build had no credentials or network access to those services. Expect to fix small API-shape surprises on first live run; start with 2-3 test sends to yourself.
- Demo mode uses **fictional companies** and a deterministic mock LLM. Real model emails will read more naturally; they pass through the same evidence and quality checks.
- The fetcher does not run JavaScript, so JS-only sites yield little. Research is limited to a handful of same-site pages per company, and fact extraction is keyword-rule based, so it can miss or over-count signals.
- Discovery's "why relevant" note is written by the model from search snippets and is labelled unverified in the UI. Only extracted facts count as evidence.
- Replies and bounces are marked manually. Automatic detection would need a Gmail read scope, which is a restricted scope.
- Single admin password, no multi-user roles. The send audit trail records actor `admin`.
- OpenAI provider is an intentional stub.
- Google's AI Pro usage limits are not published as a stable number; heavy discovery runs may exhaust them. The app reports this instead of retrying.
- You are responsible for lawful outreach (consent and opt-out rules in your jurisdiction) and for honouring replies asking you to stop.
