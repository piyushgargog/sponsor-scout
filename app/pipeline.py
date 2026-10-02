"""Orchestration: discover -> research (cached) -> score -> contacts -> generate email."""
from . import emailgen, quality
from .discovery import discover_candidates
from .jobs import PermanentError
from .llm.base import LLMError
from .logging_setup import log_event
from .prompts import FIT_ANALYSIS_SCHEMA, SYNTHESIS_SCHEMA, SYSTEM_RULES, fit_analysis_prompt, synthesis_prompt
from .research.contacts import contact_is_usable, discover_contacts
from .research.extract import extract_facts, extract_people, verify_quote
from .research.fetcher import classify_page_kind, crawl_company
from .scoring import score_lead
from .util import iso_ago, jdump, now_iso


class ResearchError(Exception):
    pass


class Pipeline:
    def __init__(self, repo, settings, llm, search, fetcher, queue):
        self.repo, self.db, self.s = repo, repo.db, settings
        self.llm, self.search, self.fetcher, self.queue = llm, search, fetcher, queue

    # ------------------------------------------------------------------ discovery
    def discover(self, event_id: int, actor="system", research=True) -> dict:
        ev = self.repo.get_event(event_id)
        if not ev:
            raise PermanentError("event not found")
        res = discover_candidates(ev, self.llm, self.search, self.s.max_discovery_candidates)
        created = dup = 0
        lead_ids = []
        for c in res["accepted"]:
            try:
                cid, new_company = self.repo.upsert_company(c["company_name"], c["website"], c["industry"], c["description"],
                                                            c["reason_relevant"], c["source_urls"])
            except ValueError:
                continue
            lid, new_lead = self.repo.ensure_lead(event_id, cid)
            lead_ids.append(lid)
            if new_lead:
                created += 1
                log_event("lead_created", event_id=event_id, lead_id=lid, company=c["company_name"], new_company=new_company)
            else:
                dup += 1
        if research:
            for lid in lead_ids:
                self.enqueue_research(lid, event_id)
        self.repo.audit(actor, "discovery_run", "event", event_id, created=created, duplicates=dup, rejected=len(res["rejected"]))
        return {"queries": len(res["queries"]), "candidates": res["candidates"], "accepted": len(res["accepted"]),
                "leads_created": created, "duplicates_skipped": dup, "rejected": len(res["rejected"])}

    def enqueue_research(self, lead_id, event_id, force=False):
        lead = self.repo.get_lead(lead_id)
        if lead["research_status"] not in ("RUNNING",):
            self.repo.update_lead(lead_id, research_status="QUEUED")
        return self.queue.enqueue("research", {"lead_id": lead_id, "force": force}, dedupe_key=f"research:{lead_id}",
                                  event_id=event_id, label=f"Research {lead['company_name']}")

    # ------------------------------------------------------------------ research
    def research_company(self, company_id: int, force=False) -> dict:
        co = self.repo.get_company(company_id)
        ver = self.s.research_version
        cached = self.repo.get_research(company_id, ver)
        if cached and cached["status"] == "COMPLETED" and not force and cached["researched_at"] >= iso_ago(days=self.s.research_ttl_days):
            log_event("research_cache_hit", company_id=company_id, version=ver)
            cached["cached"] = True
            return cached
        log_event("research_started", company_id=company_id, company=co["name"], forced=force)
        pages = crawl_company(self.fetcher, co["website"], self.s.max_pages_per_company)
        if not pages:
            self._store_research(company_id, ver, "FAILED", {}, [], {}, "No pages could be fetched (unreachable, blocked by robots.txt, or non-HTML).")
            log_event("research_failed", company_id=company_id)
            raise ResearchError("No public pages could be fetched for this company.")
        facts = extract_facts(pages)
        people = extract_people(pages)
        profile = self._synthesize(co, pages)
        profile["homepage_url"] = pages[0].url
        profile["people"] = [{k: p[k] for k in ("name", "role", "source_url")} for p in people]
        profile["pages"] = [{"url": p.url, "kind": classify_page_kind(p.url), "title": p.title} for p in pages]
        contacts = discover_contacts(pages, co["domain"], people)
        with self.db.tx():
            for pg in pages:
                self.db.run("INSERT INTO research_sources(company_id,url,title,kind,fetched_at,content_hash,excerpt) VALUES(?,?,?,?,?,?,?) "
                            "ON CONFLICT(company_id,url) DO UPDATE SET title=excluded.title, fetched_at=excluded.fetched_at, "
                            "content_hash=excluded.content_hash, excerpt=excluded.excerpt",
                            (company_id, pg.url, pg.title, classify_page_kind(pg.url), pg.fetched_at, pg.content_hash, pg.text[:600]))
            for c in contacts:
                self.repo.upsert_contact(company_id, c)
            stats = {"pages": len(pages), "facts": len(facts), "contacts": len(contacts), "claims_rejected": profile.pop("claims_rejected", 0)}
            self._store_research(company_id, ver, "COMPLETED", profile, facts, stats, None)
            if profile.get("industry") and profile["industry"] != "unknown":
                self.db.run("UPDATE companies SET industry=CASE WHEN industry IS NULL OR industry='unknown' THEN ? ELSE industry END WHERE id=?",
                            (profile["industry"], company_id))
        log_event("research_completed", company_id=company_id, **stats)
        return {"status": "COMPLETED", "profile": profile, "facts": facts, "stats": stats, "cached": False}

    def _store_research(self, company_id, ver, status, profile, facts, stats, error):
        self.db.run("INSERT INTO company_research(company_id,research_version,status,profile_json,facts_json,stats_json,error,researched_at) "
                    "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(company_id,research_version) DO UPDATE SET status=excluded.status, "
                    "profile_json=excluded.profile_json, facts_json=excluded.facts_json, stats_json=excluded.stats_json, "
                    "error=excluded.error, researched_at=excluded.researched_at",
                    (company_id, ver, status, jdump(profile), jdump(facts), jdump(stats), error, now_iso()))

    def _synthesize(self, co, pages) -> dict:
        """LLM summary; every product/launch claim must carry a quote that is verbatim on the cited page."""
        payload = [{"url": p.url, "text": p.text} for p in pages[:6]]
        base = {"summary": "unknown", "industry": co.get("industry") or "unknown", "products": [], "launches": [], "claims_rejected": 0}
        try:
            out = self.llm.generate_json(synthesis_prompt(co, payload), system=SYSTEM_RULES, schema=SYNTHESIS_SCHEMA, task="research_synthesis",
                                         context={"company_name": co["name"], "pages": payload, "industry": co.get("industry")})
        except LLMError as e:
            log_event("research_synthesis_fallback", company_id=co["id"], error=str(e))
            first = [s for s in pages[0].text.split("\n") if len(s) > 30][:2]
            base["summary"] = " ".join(first) or "unknown"
            return base
        rejected = 0
        for key in ("products", "launches"):
            for item in out.get(key) or []:
                if verify_quote(item.get("quote", ""), item.get("source_url", ""), pages):
                    base[key].append({"text": item.get("text") or item["quote"], "quote": item["quote"], "source_url": item["source_url"]})
                else:
                    rejected += 1
                    log_event("claim_rejected", company_id=co["id"], kind=key, reason="quote not found on cited page")
        base["summary"] = (out.get("summary") or "unknown").strip()[:500]
        ind = (out.get("industry") or "").strip()
        base["industry"] = ind if ind and ind != "unknown" else base["industry"]
        base["claims_rejected"] = rejected
        return base

    def research_lead(self, lead_id: int, force=False) -> dict:
        lead = self.repo.get_lead(lead_id)
        if not lead:
            raise PermanentError("lead not found")
        self.repo.update_lead(lead_id, research_status="RUNNING")
        try:
            research = self.research_company(lead["company_id"], force=force)
        except ResearchError as e:
            self.repo.update_lead(lead_id, research_status="FAILED")
            raise PermanentError(str(e))
        except Exception:
            self.repo.update_lead(lead_id, research_status="FAILED")
            raise
        result = self.score_lead(lead_id, research)
        if self.s.llm_fit_analysis:
            self.analyze_fit(lead_id, result, research)
        return {"cached": research.get("cached", False)}

    def analyze_fit(self, lead_id: int, result: dict, research: dict) -> dict | None:
        """Advisory LLM commentary stored next to the rule-based score (`score_json.analysis`). It never changes the score,
        and a 'strength' is kept only if it cites fact ids that exist in the verified facts."""
        facts = research.get("facts") or []
        if not facts:
            return None
        lead = self.repo.get_lead(lead_id)
        ev, co = self.repo.get_event(lead["event_id"]), self.repo.get_company(lead["company_id"])
        top = emailgen.select_facts(facts, 20) or facts[:12]
        try:
            out = self.llm.generate_json(fit_analysis_prompt(ev, co, result, top), system=SYSTEM_RULES, schema=FIT_ANALYSIS_SCHEMA,
                                         task="fit_analysis", context={"event": ev, "company": co, "score": result, "facts": top})
        except LLMError as e:
            log_event("fit_analysis_skipped", lead_id=lead_id, error=str(e))
            return None
        by_id = {f["id"]: f for f in top}
        strengths = []
        for item in out.get("strengths") or []:
            ids = [i for i in item.get("fact_ids", []) if i in by_id]
            if ids and str(item.get("text", "")).strip():
                strengths.append({"text": item["text"].strip()[:300], "fact_ids": ids,
                                  "sources": sorted({by_id[i]["source_url"] for i in ids})})
        priority = str(out.get("priority", "")).strip().lower()
        analysis = {"summary": str(out.get("summary", "")).strip()[:400], "priority": priority if priority in ("high", "medium", "low") else "unknown",
                    "recommended_angle": str(out.get("recommended_angle", "")).strip()[:300], "strengths": strengths,
                    "concerns": [str(c).strip()[:200] for c in (out.get("concerns") or []) if str(c).strip()][:6],
                    "dropped_uncited": len(out.get("strengths") or []) - len(strengths), "model": self.llm.model or self.llm.name}
        result["analysis"] = analysis
        self.repo.update_lead(lead_id, score_json=jdump(result))
        return analysis

    def score_lead(self, lead_id: int, research: dict | None = None):
        lead = self.repo.get_lead(lead_id)
        ev = self.repo.get_event(lead["event_id"])
        co = self.repo.get_company(lead["company_id"])
        research = research or self.repo.get_research(lead["company_id"], self.s.research_version)
        result = score_lead(ev, co, research)
        # contacts saved by older research runs may predate today's rules; manual entries are trusted as given
        contacts = [c for c in self.repo.contacts_for(lead["company_id"])
                    if (c["source_url"] or "").startswith("manual") or contact_is_usable(c["email"], co["domain"])]
        primary = contacts[0] if contacts else None
        self.repo.update_lead(lead_id, fit_score=result["score"], score_json=jdump(result), research_status="COMPLETED",
                              contact_status="found" if primary else "not_found", primary_contact_id=primary["id"] if primary else None)
        return result

    def add_manual_contact(self, lead_id: int, email: str, name: str | None, role: str | None, actor: str) -> int:
        lead = self.repo.get_lead(lead_id)
        cid = self.repo.upsert_contact(lead["company_id"], {"email": email, "name": name or None, "role": role or None, "department": "manual",
                                                            "source_url": f"manual entry by {actor}", "confidence": 0.9, "priority": 0})
        self.repo.update_lead(lead_id, contact_status="found", primary_contact_id=cid)
        self.repo.audit(actor, "contact_added_manually", "lead", lead_id, contact_id=cid)
        return cid

    # ------------------------------------------------------------------ email
    def _sender(self):
        return {"name": self.s.sender_name, "role": self.s.sender_role, "org": self.s.sender_org}

    def generate_email(self, lead_id: int, actor="system") -> int:
        lead = self.repo.get_lead(lead_id)
        if not lead:
            raise PermanentError("lead not found")
        existing = self.repo.latest_draft_for_lead(lead_id)
        if existing and existing["status"] in ("SENT", "REPLIED", "BOUNCED"):
            raise PermanentError("An email was already sent to this lead.")
        research = self.repo.get_research(lead["company_id"], self.s.research_version)
        if not research or research["status"] != "COMPLETED":
            raise PermanentError("Research this company before generating an email.")
        contact = self.repo.get_contact(lead["primary_contact_id"])
        if not contact:
            raise PermanentError("No public contact was found for this company (contact_status=not_found). Add one manually if you have a verified address.")
        ev, co = self.repo.get_event(lead["event_id"]), self.repo.get_company(lead["company_id"])
        try:
            d = emailgen.generate_draft(self.llm, ev, co, contact, research["facts"], self._sender())
        except emailgen.EmailGenError as e:
            raise PermanentError(str(e))
        chosen = emailgen.select_facts(research["facts"])
        extra = quality.llm_review(self.llm, d, emailgen.event_context(ev), chosen) if self.s.llm_email_review else []
        q = quality.evaluate(d, emailgen.event_context(ev), co, contact, chosen, lead["fit_score"], self.s.min_fit_for_email,
                             real_send=self.s.mail_provider == "gmail", extra_checks=extra)
        status = "DRAFT" if q["status"] == "ok" else "NEEDS_REVIEW"
        with self.db.tx():
            if existing:
                self.db.run("UPDATE email_drafts SET contact_id=?, subject=?, body=?, personalization_json=?, evidence_json=?, cta=?, suggested_ask=?, "
                            "quality_json=?, model=?, version=version+1, approved_by=NULL, approved_at=NULL, error=NULL WHERE id=?",
                            (contact["id"], d["subject"], d["body"], jdump(d["personalization"]), jdump(d["evidence"]), d["cta"], d["suggested_ask"],
                             jdump(q), self.llm.model or self.llm.name, existing["id"]))
                did = existing["id"]
            else:
                did = self.db.run("INSERT INTO email_drafts(lead_id,contact_id,subject,body,personalization_json,evidence_json,cta,suggested_ask,"
                                  "quality_json,status,model,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (lead_id, contact["id"], d["subject"], d["body"], jdump(d["personalization"]), jdump(d["evidence"]), d["cta"],
                                   d["suggested_ask"], jdump(q), status, self.llm.model or self.llm.name, now_iso(), now_iso()))
            self.repo.set_draft_status(did, status, lead_id)
            self.repo.audit(actor, "email_generated", "draft", did, lead_id=lead_id, quality=q["status"], model=self.llm.model or self.llm.name)
        log_event("email_generated", draft_id=did, lead_id=lead_id, quality=q["status"], words=q["word_count"])
        return did

    def requality(self, draft_id: int):
        """Re-run quality checks on a (possibly human-edited) draft."""
        d = self.repo.get_draft(draft_id)
        lead = self.repo.get_lead(d["lead_id"])
        research = self.repo.get_research(lead["company_id"], self.s.research_version) or {"facts": []}
        ev, co, contact = self.repo.get_event(lead["event_id"]), self.repo.get_company(lead["company_id"]), self.repo.get_contact(d["contact_id"])
        chosen = emailgen.select_facts(research["facts"])
        q = quality.evaluate(d, emailgen.event_context(ev), co, contact, chosen, lead["fit_score"], self.s.min_fit_for_email,
                             real_send=self.s.mail_provider == "gmail", extra_checks=quality.carry_llm_checks(d.get("quality"), d["body"]))
        return q
