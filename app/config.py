"""Configuration layer. Everything important is an environment variable (see .env.example)."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader (no extra dependency). Real environment variables win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.split(" #")[0].strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


def _int(name, default):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass
class Settings:
    app_env: str = "development"
    secret_key: str = ""
    admin_password: str = ""
    database_url: str = "sqlite:///data/app.db"

    llm_provider: str = "mock"
    gemini_api_key: str = ""
    gemini_model: str = ""

    search_provider: str = "mock"
    brave_api_key: str = ""
    google_cse_key: str = ""
    google_cse_cx: str = ""

    mail_provider: str = "mock"
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:5000/gmail/callback"

    sender_name: str = "Your Name"
    sender_role: str = "Sponsorship Team"
    sender_org: str = "Your College Tech Club"

    research_version: str = "v1"
    research_ttl_days: int = 30
    max_pages_per_company: int = 8
    fetch_delay_seconds: float = 1.0
    high_fit_threshold: int = 70
    min_fit_for_email: int = 40
    campaign_daily_limit: int = 20
    send_interval_seconds: int = 30
    company_cooldown_days: int = 90
    worker_threads: int = 2
    worker_enabled: bool = True
    log_level: str = "INFO"
    max_discovery_candidates: int = 40

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def db_path(self) -> str:
        url = self.database_url
        if url.startswith("sqlite:///"):
            p = url[len("sqlite:///"):]
            return p if os.path.isabs(p) else str(ROOT / p)
        raise NotImplementedError(
            "This MVP ships a SQLite backend only (DATABASE_URL=sqlite:///...). "
            "The schema in app/db.py is plain relational SQL and ports to PostgreSQL; see README 'Known limitations'."
        )

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        e = os.environ.get
        s = cls(
            app_env=e("APP_ENV", "development"),
            secret_key=e("SECRET_KEY", ""),
            admin_password=e("ADMIN_PASSWORD", ""),
            database_url=e("DATABASE_URL", "") or "sqlite:///data/app.db",
            llm_provider=(e("LLM_PROVIDER", "") or ("gemini" if e("GEMINI_API_KEY") else "mock")).lower(),
            gemini_api_key=e("GEMINI_API_KEY", ""),
            gemini_model=e("GEMINI_MODEL", ""),
            search_provider=(e("SEARCH_PROVIDER", "") or "mock").lower(),
            brave_api_key=e("BRAVE_API_KEY", ""),
            google_cse_key=e("GOOGLE_CSE_KEY", ""),
            google_cse_cx=e("GOOGLE_CSE_CX", ""),
            mail_provider=(e("MAIL_PROVIDER", "") or ("gmail" if e("GOOGLE_CLIENT_ID") else "mock")).lower(),
            google_client_id=e("GOOGLE_CLIENT_ID", ""),
            google_client_secret=e("GOOGLE_CLIENT_SECRET", ""),
            google_redirect_uri=e("GOOGLE_REDIRECT_URI", "") or "http://localhost:5000/gmail/callback",
            sender_name=e("SENDER_NAME", "Your Name"),
            sender_role=e("SENDER_ROLE", "Sponsorship Team"),
            sender_org=e("SENDER_ORG", "Your College Tech Club"),
            research_version=e("RESEARCH_VERSION", "v1"),
            research_ttl_days=_int("RESEARCH_TTL_DAYS", 30),
            max_pages_per_company=_int("MAX_PAGES_PER_COMPANY", 8),
            fetch_delay_seconds=_float("FETCH_DELAY_SECONDS", 1.0),
            high_fit_threshold=_int("HIGH_FIT_THRESHOLD", 70),
            min_fit_for_email=_int("MIN_FIT_FOR_EMAIL", 40),
            campaign_daily_limit=_int("CAMPAIGN_DAILY_LIMIT", 20),
            send_interval_seconds=_int("SEND_INTERVAL_SECONDS", 30),
            company_cooldown_days=_int("COMPANY_COOLDOWN_DAYS", 90),
            worker_threads=_int("WORKER_THREADS", 2),
            worker_enabled=e("WORKER_ENABLED", "1").lower() not in ("0", "false", "no"),
            log_level=e("LOG_LEVEL", "INFO"),
        )
        return s

    def validate_for_runtime(self) -> list[str]:
        """Returns warnings; raises in production when security-critical settings are missing."""
        warnings = []
        if self.is_production:
            if len(self.secret_key) < 32:
                raise RuntimeError("SECRET_KEY must be at least 32 characters in production.")
            if not self.admin_password:
                raise RuntimeError("ADMIN_PASSWORD must be set in production.")
        else:
            if not self.secret_key:
                self.secret_key = "dev-only-secret-key-change-me-0123456789"
                warnings.append("SECRET_KEY not set; using an insecure development key.")
            if not self.admin_password:
                self.admin_password = "demo"
                warnings.append('ADMIN_PASSWORD not set; development login password is "demo".')
        if self.llm_provider == "gemini" and not self.gemini_api_key:
            raise RuntimeError("LLM_PROVIDER=gemini requires GEMINI_API_KEY.")
        if self.mail_provider == "gmail" and not (self.google_client_id and self.google_client_secret):
            raise RuntimeError("MAIL_PROVIDER=gmail requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
        if self.mail_provider == "mock":
            self.send_interval_seconds = 0
        return warnings
