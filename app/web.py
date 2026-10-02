"""HTTP routes (server-rendered dashboard). All state-changing routes are POST + CSRF-protected + login-protected."""
import hmac
import secrets
from datetime import datetime
from urllib.parse import urlparse

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, session, url_for)

from .logging_setup import log_event
from .mailer.base import MailError
from .mailer.gmail import new_pkce
from .normalize import is_valid_email
from .security import bucket_for, check_csrf, login_required, LIMITS
from .sending import ApprovalError, SendBlocked
from .validators import ValidationError, clean_text, parse_event

bp = Blueprint("web", __name__)
ACTOR = "admin"


def svc():
    return current_app.extensions["svc"]


def register_filters(app):
    @app.template_filter("dt")
    def dt(v):
        try:
            return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ").strftime("%d %b %Y, %H:%M UTC")
        except (TypeError, ValueError):
            return v or ""

    @app.template_filter("nicedate")
    def nicedate(v):
        try:
            return datetime.strptime(v, "%Y-%m-%d").strftime("%d %b %Y")
        except (TypeError, ValueError):
            return v or ""


@bp.app_context_processor
def inject():
    s = svc()
    acc = s.accounts.get_active(s.mailer.name)
    return {"mail_provider": s.mailer.name, "mail_account": {"email": acc["email"]} if acc else None,
            "events_nav": s.repo.list_events()[:8], "settings": s.settings}


@bp.before_request
def guard():
    if request.endpoint == "static":
        return
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        lim = LIMITS[bucket_for(request.path)]
        if not current_app.extensions["limiter"].allow(f"{request.remote_addr}:{bucket_for(request.path)}", *lim):
            abort(429, "Too many requests; slow down.")
        check_csrf()


def safe_next(default):
    n = request.form.get("next") or request.args.get("next") or ""
    p = urlparse(n)
    return n if n.startswith("/") and not n.startswith("//") and not p.netloc and not p.scheme else default


@bp.errorhandler(400)
@bp.errorhandler(429)
def handled(e):
    flash(e.description if hasattr(e, "description") else "Request error", "error")
    return redirect(safe_next(url_for("web.dashboard"))), 303


@bp.get("/healthz")
def healthz():
    return jsonify(ok=True)


# ----------------------------------------------------------------- auth
@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if hmac.compare_digest(pw.encode(), svc().settings.admin_password.encode()):
            session.clear()
            session["user"] = ACTOR
            session.permanent = True
            log_event("login_success")
            return redirect(safe_next(url_for("web.dashboard")))
        log_event("login_failed", ip=request.remote_addr)
        flash("Wrong password.", "error")
    return render_template("login.html", dev=not svc().settings.is_production)


@bp.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("web.login"))


# ----------------------------------------------------------------- dashboard / events
@bp.get("/")
@login_required
def dashboard():
    s = svc()
    events = s.repo.list_events()
    for e in events:
        e["stats"] = s.repo.stats(e["id"], s.settings.high_fit_threshold)
    return render_template("dashboard.html", stats=s.repo.stats(None, s.settings.high_fit_threshold), events=events,
                           jobs=s.queue.summary(None))


@bp.post("/events")
@login_required
def create_event():
    s = svc()
    try:
        data = parse_event(request.form)
    except ValidationError as e:
        for k, v in e.errors.items():
            flash(f"{k.replace('_', ' ')}: {v}", "error")
        return redirect(url_for("web.dashboard"))
    eid = s.repo.create_event(data, s.settings.campaign_daily_limit, ACTOR)
    s.queue.enqueue("discover", {"event_id": eid, "actor": ACTOR}, dedupe_key=f"discover:{eid}", event_id=eid, label="Find sponsors")
    flash("Event created. Finding sponsors now.", "ok")
    return redirect(url_for("web.campaign", eid=eid))


# ----------------------------------------------------------------- campaign
def _event_or_404(eid):
    ev = svc().repo.get_event(eid)
    if not ev:
        abort(404)
    return ev


@bp.get("/campaigns/<int:eid>")
@login_required
def campaign(eid):
    s = svc()
    ev = _event_or_404(eid)
    leads = s.repo.list_leads(eid)
    return render_template("campaign.html", ev=ev, camp=s.repo.campaign_for_event(eid), stats=s.repo.stats(eid, s.settings.high_fit_threshold),
                           leads=leads[:6], jobs=s.queue.list_for_event(eid, 12), jsum=s.queue.summary(eid),
                           failed=[l for l in leads if l["research_status"] == "FAILED"])


@bp.post("/campaigns/<int:eid>/find-sponsors")
@login_required
def find_sponsors(eid):
    _event_or_404(eid)
    svc().queue.enqueue("discover", {"event_id": eid, "actor": ACTOR}, dedupe_key=f"discover:{eid}", event_id=eid, label="Find sponsors")
    flash("Discovery queued.", "ok")
    return redirect(url_for("web.campaign", eid=eid))


@bp.post("/campaigns/<int:eid>/bulk")
@login_required
def bulk(eid):
    s = svc()
    _event_or_404(eid)
    action, n = request.form.get("action"), 0
    leads = s.repo.list_leads(eid)
    if action == "research_all":
        for l in leads:
            if l["research_status"] in ("NOT_STARTED", "FAILED"):
                s.pipeline.enqueue_research(l["id"], eid)
                n += 1
        flash(f"Queued research for {n} companies.", "ok")
    elif action == "generate_high_fit":
        for l in leads:
            if (l["fit_score"] or 0) >= s.settings.high_fit_threshold and l["contact_status"] == "found" and l["email_status"] == "NONE":
                s.queue.enqueue("generate_email", {"lead_id": l["id"], "actor": ACTOR}, dedupe_key=f"gen:{l['id']}", event_id=eid,
                                label=f"Draft email for {l['company_name']}")
                n += 1
        flash(f"Queued {n} email drafts for high-fit leads with a contact.", "ok")
    elif action == "send_approved":
        for l in leads:
            d = s.repo.latest_draft_for_lead(l["id"])
            if d and d["status"] == "APPROVED":
                try:
                    s.sender.request_send(d["id"], ACTOR)
                    n += 1
                except SendBlocked as e:
                    flash(str(e), "error")
                    break
        flash(f"Queued {n} approved emails for sending (throttled and capped per campaign).", "ok")
    elif action in ("pause", "resume"):
        s.db.run("UPDATE campaigns SET status=? WHERE event_id=?", ("PAUSED" if action == "pause" else "ACTIVE", eid))
        flash(f"Campaign {action}d.", "ok")
    return redirect(url_for("web.campaign", eid=eid))


# ----------------------------------------------------------------- leads
@bp.get("/leads")
@login_required
def leads():
    s = svc()
    events = s.repo.list_events()
    if not events:
        return redirect(url_for("web.dashboard"))
    eid = request.args.get("event", type=int) or events[0]["id"]
    ev = _event_or_404(eid)
    rows = s.repo.list_leads(eid)
    q = (request.args.get("q") or "").lower().strip()
    flt = request.args.get("filter", "")
    if q:
        rows = [r for r in rows if q in r["company_name"].lower() or q in (r["industry"] or "").lower()]
    if flt == "high":
        rows = [r for r in rows if (r["fit_score"] or 0) >= s.settings.high_fit_threshold]
    for r in rows:
        r["draft"] = s.repo.latest_draft_for_lead(r["id"])
    return render_template("leads.html", ev=ev, rows=rows, q=q, flt=flt, jsum=s.queue.summary(eid))


@bp.get("/leads/<int:lid>")
@login_required
def lead_detail(lid):
    s = svc()
    lead = s.repo.get_lead(lid)
    if not lead:
        abort(404)
    ev = s.repo.get_event(lead["event_id"])
    research = s.repo.get_research(lead["company_id"], s.settings.research_version)
    draft = s.repo.latest_draft_for_lead(lid)
    send = s.db.row("SELECT * FROM email_sends WHERE draft_id=? ORDER BY id DESC LIMIT 1", (draft["id"],)) if draft else None
    activity = s.repo.audit_for("lead", lid, 20) + (s.repo.audit_for("draft", draft["id"], 30) if draft else [])
    activity.sort(key=lambda a: a["id"], reverse=True)
    return render_template("lead_detail.html", lead=lead, ev=ev, company=s.repo.get_company(lead["company_id"]), research=research,
                           contacts=s.repo.contacts_for(lead["company_id"]), sources=s.repo.sources_for(lead["company_id"]),
                           draft=draft, send=send, activity=activity,
                           jobs=[j for j in s.queue.list_for_event(lead["event_id"], 40) if f'"lead_id":{lid}' in j["payload_json"]][:5],
                           can_send=bool(s.accounts.get_active(s.mailer.name)))


@bp.post("/leads/<int:lid>/research")
@login_required
def research(lid):
    s = svc()
    lead = s.repo.get_lead(lid) or abort(404)
    s.pipeline.enqueue_research(lid, lead["event_id"], force=request.form.get("force") == "1")
    s.repo.audit(ACTOR, "research_requested", "lead", lid, force=request.form.get("force") == "1")
    flash("Research queued." if request.form.get("force") != "1" else "Refreshing research (cache bypassed).", "ok")
    return redirect(safe_next(url_for("web.lead_detail", lid=lid)))


@bp.post("/leads/<int:lid>/generate")
@login_required
def generate(lid):
    s = svc()
    lead = s.repo.get_lead(lid) or abort(404)
    s.queue.enqueue("generate_email", {"lead_id": lid, "actor": ACTOR}, dedupe_key=f"gen:{lid}", event_id=lead["event_id"],
                    label=f"Draft email for {lead['company_name']}")
    flash("Drafting email.", "ok")
    return redirect(safe_next(url_for("web.lead_detail", lid=lid)))


@bp.post("/leads/<int:lid>/contact")
@login_required
def add_contact(lid):
    s = svc()
    s.repo.get_lead(lid) or abort(404)
    email = clean_text(request.form.get("email"), 254).lower()
    if not is_valid_email(email):
        flash("That is not a valid email address.", "error")
    else:
        s.pipeline.add_manual_contact(lid, email, clean_text(request.form.get("name"), 80), clean_text(request.form.get("role"), 80), ACTOR)
        flash("Contact added (marked as manually entered; you vouch for it).", "ok")
    return redirect(url_for("web.lead_detail", lid=lid))


# ----------------------------------------------------------------- drafts
def _draft_or_404(did):
    d = svc().repo.get_draft(did)
    if not d:
        abort(404)
    return d


@bp.post("/drafts/<int:did>/save")
@login_required
def save_draft(did):
    d = _draft_or_404(did)
    try:
        q = svc().sender.update_draft(did, request.form.get("subject"), request.form.get("body"), ACTOR)
        flash(f"Saved. Quality checks: {q['status'].replace('_', ' ')}. Approval (if any) was reset.", "ok")
    except ApprovalError as e:
        flash(str(e), "error")
    return redirect(url_for("web.lead_detail", lid=d["lead_id"]))


@bp.post("/drafts/<int:did>/approve")
@login_required
def approve(did):
    d = _draft_or_404(did)
    try:
        svc().sender.approve(did, ACTOR, acknowledge=request.form.get("acknowledge") == "1")
        flash("Approved. It will not be sent until you press Send.", "ok")
    except ApprovalError as e:
        flash(str(e), "error")
    return redirect(safe_next(url_for("web.lead_detail", lid=d["lead_id"])))


@bp.post("/drafts/<int:did>/unapprove")
@login_required
def unapprove(did):
    d = _draft_or_404(did)
    svc().sender.unapprove(did, ACTOR)
    return redirect(url_for("web.lead_detail", lid=d["lead_id"]))


@bp.post("/drafts/<int:did>/send")
@login_required
def send(did):
    d = _draft_or_404(did)
    try:
        svc().sender.request_send(did, ACTOR)
        flash("Send queued.", "ok")
    except SendBlocked as e:
        flash(str(e), "error")
    return redirect(safe_next(url_for("web.lead_detail", lid=d["lead_id"])))


@bp.post("/drafts/<int:did>/mark")
@login_required
def mark(did):
    d = _draft_or_404(did)
    try:
        svc().sender.mark_outcome(did, request.form.get("outcome", ""), ACTOR)
    except ApprovalError as e:
        flash(str(e), "error")
    return redirect(url_for("web.lead_detail", lid=d["lead_id"]))


@bp.post("/drafts/<int:did>/reopen")
@login_required
def reopen(did):
    d = _draft_or_404(did)
    svc().sender.retry_failed(did, ACTOR)
    return redirect(url_for("web.lead_detail", lid=d["lead_id"]))


# ----------------------------------------------------------------- Gmail
@bp.get("/gmail/connect")
@login_required
def gmail_connect():
    s = svc()
    if s.mailer.name != "gmail":
        flash("Gmail is not configured (MAIL_PROVIDER=mock). Use the demo account, or set GOOGLE_CLIENT_ID/SECRET.", "error")
        return redirect(url_for("web.dashboard"))
    state = secrets.token_urlsafe(24)
    verifier, challenge = new_pkce()
    session["oauth_state"], session["oauth_verifier"] = state, verifier
    return redirect(s.mailer.authorization_url(state, challenge))


@bp.get("/gmail/callback")
@login_required
def gmail_callback():
    s = svc()
    if request.args.get("error"):
        flash("Google authorization was cancelled or denied.", "error")
        return redirect(url_for("web.dashboard"))
    state, expected = request.args.get("state", ""), session.pop("oauth_state", "")
    verifier = session.pop("oauth_verifier", "")
    if not expected or not hmac.compare_digest(state, expected):
        flash("OAuth state mismatch; please try connecting again.", "error")
        return redirect(url_for("web.dashboard"))
    try:
        email = s.mailer.complete_oauth(request.args.get("code", ""), verifier)
        s.repo.audit(ACTOR, "gmail_connected", "account", None, email=email)
        flash(f"Connected {email}.", "ok")
    except MailError as e:
        flash(str(e), "error")
    return redirect(url_for("web.dashboard"))


@bp.post("/gmail/demo-connect")
@login_required
def demo_connect():
    s = svc()
    if s.mailer.name != "mock":
        abort(404)
    s.accounts.save("mock", "demo@mock.local", "demo", "demo", "2999-01-01T00:00:00Z", "demo")
    flash("Demo sending account connected. Nothing is sent to real recipients in demo mode.", "ok")
    return redirect(safe_next(url_for("web.dashboard")))


@bp.post("/gmail/disconnect")
@login_required
def gmail_disconnect():
    s = svc()
    acc = s.accounts.get_active(s.mailer.name)
    if acc:
        s.accounts.deactivate(acc["id"])
        s.repo.audit(ACTOR, "gmail_disconnected", "account", acc["id"])
    return redirect(url_for("web.dashboard"))


# ----------------------------------------------------------------- JSON for polling
@bp.get("/api/jobs")
@login_required
def api_jobs():
    s = svc()
    eid = request.args.get("event", type=int)
    summ = s.queue.summary(eid)
    st = s.repo.stats(eid, s.settings.high_fit_threshold) if eid else s.repo.stats(None, s.settings.high_fit_threshold)
    return jsonify(summary=summ, active=summ["QUEUED"] + summ["RUNNING"], stats=st)
