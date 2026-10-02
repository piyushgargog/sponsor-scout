"""CLI: python -m app.cli [init-db | seed [--full] | create-event FILE.json [--run] | llm-check | models | worker | run-jobs]"""
import sys
import time

from .config import Settings

DEMO_EVENT = {
    "name": "XYZ Tech Fest 2026", "college": "XYZ University", "city": "Delhi", "country": "India", "event_date": "2026-11-20",
    "expected_attendance": 500, "event_type": "technology",
    "audience": ["B.Tech students", "AI/ML students", "developers", "startup enthusiasts"],
    "requirements": ["cash sponsorship", "API credits", "cloud credits", "swag", "speakers", "workshop partners"],
    "description": "Annual student tech fest with hackathon, workshops and talks.",
    "benefits": "logo placement across the event, a demo or workshop slot, and direct access to attendees",
    "categories": [], "keywords": [],
}


def seed_demo_event(svc) -> int:
    """Creates the demo event once (no leads: click 'Find sponsors' to run the pipeline)."""
    ex = svc.db.row("SELECT id FROM events WHERE name=?", (DEMO_EVENT["name"],))
    if ex:
        return ex["id"]
    return svc.repo.create_event(DEMO_EVENT, svc.settings.campaign_daily_limit, "seed")


def _llm_check(llm, settings):
    """Smoke-test the configured LLM provider (for the CLI route this also proves you are signed in)."""
    from .llm.base import LLMError
    print(f"LLM_PROVIDER={settings.llm_provider} provider={llm.name} model={llm.model or 'default'}")
    try:
        if hasattr(llm, "version"):
            print("CLI version:", llm.version())
        out = llm.generate("Reply with exactly the single word: OK", temperature=0)
        print("Reply:", out.strip()[:80])
        print("OK: the provider answered.")
    except LLMError as e:
        sys.exit(f"FAILED: {e}")


def load_event_file(path: str) -> dict:
    """Read an event preset (same keys as the web form) and validate it. Fields still starting with REPLACE are rejected."""
    import json
    from .validators import ValidationError, parse_event
    with open(path, encoding="utf-8") as f:
        raw = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    raw = {k: str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v for k, v in raw.items()}  # form parser expects text
    todo = [k for k, v in raw.items() if "REPLACE" in json.dumps(v)]
    if todo:
        sys.exit("Fill in these fields in " + path + " first: " + ", ".join(todo))
    try:
        return parse_event(raw)
    except ValidationError as e:
        sys.exit(f"Invalid event file: {e}")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "help"
    settings = Settings.from_env()
    try:
        settings.validate_for_runtime()
    except RuntimeError as e:
        sys.exit(f"Configuration error: {e}")
    from .logging_setup import setup_logging
    setup_logging(settings.log_level)
    from .services import build_services
    svc = build_services(settings)
    if cmd == "init-db":
        print("Database ready at", settings.db_path)
    elif cmd == "seed":
        eid = seed_demo_event(svc)
        if "--full" in argv:
            svc.pipeline.discover(eid, "seed")
            svc.queue.run_all()
            print("Ran discovery + research for demo event", eid)
        print("Demo event id:", eid)
    elif cmd == "create-event":
        if len(argv) < 3:
            sys.exit("usage: python -m app.cli create-event FILE.json [--run]")
        data = load_event_file(argv[2])
        eid = svc.repo.create_event(data, settings.campaign_daily_limit, "cli")
        if "--run" in argv:
            print("Finding sponsors (this calls your LLM + search provider; it can take several minutes)...")
            print(svc.pipeline.discover(eid, "cli"))
            print("Processed", svc.queue.run_all(), "jobs. Open the app and go to Leads.")
        else:
            svc.queue.enqueue("discover", {"event_id": eid, "actor": "cli"}, dedupe_key=f"discover:{eid}", event_id=eid, label="Find sponsors")
            print("Event created; discovery queued. Start `python run.py` and the worker will process it.")
        print("Event id:", eid)
    elif cmd == "llm-check":
        _llm_check(svc.llm, settings)
    elif cmd == "models":
        from .llm.gemini import GeminiProvider, rank_models
        if not settings.gemini_api_key:
            sys.exit("Set GEMINI_API_KEY first.")
        g = GeminiProvider(settings.gemini_api_key)
        models = g.list_models()
        print("Models that support generateContent (best-first auto-pick order):")
        for n in rank_models(models):
            print("  ", n)
        print("\nAll models returned by the API:", ", ".join(sorted(m["name"].removeprefix("models/") for m in models)))
    elif cmd == "worker":
        from .jobs import Worker
        w = Worker(svc.queue, settings.worker_threads)
        w.start()
        print("Worker running; Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            w.stop()
    elif cmd == "run-jobs":
        print("Processed", svc.queue.run_all(), "jobs")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
