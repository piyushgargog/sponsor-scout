"""Configuration layer. Everything important is an environment variable (see .env.example)."""
import os
import shutil
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


def _bool(name, default=True):
    v = os.environ.get(name, "")
    return default if v == "" else v.strip().lower() not in ("0", "false", "no", "off")


LLM_PROVIDER_ALIASES = {"gemini_cli": "cli", "gemini-cli": "cli", "antigravity": "cli", "agy": "cli", "google": "cli"}


def _llm_provider_name() -> str:
    """LLM_PROVIDER, else gemini (API key) if a key is set, else the offline mock."""
    name = (os.environ.get("LLM_PROVIDER", "") or ("gemini" if os.environ.get("GEMINI_API_KEY") else "mock")).lower()
    return LLM_PROVIDER_ALIASES.get(name, name)


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

    llm_provider: str = "mock"          # mock | cli (Google account via agy/gemini CLI) | gemini (API key) | openai (stub)
    llm_cli_bin: str = "agy"
    llm_cli_flavor: str = ""            # agy | gemini; empty = detect from the binary name
    llm_cli_model: str = ""             # empty = the account/CLI default model
    llm_cli_timeout_seconds: int = 180
    llm_cli_concurrency: int = 1
    llm_cli_pass_env: str = ""          # extra env var NAMES the CLI child may inherit (comma separated)
    llm_fit_analysis: bool = True       # advisory LLM analysis next to the rule-based score
    llm_email_review: bool = True       # advisory LLM check for unsupported claims in drafts
    gemini_api_key: str = ""            # OPTIONAL: only for LLM_PROVIDER=gemini
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
            llm_provider=_llm_provider_name(),
            llm_cli_bin=e("LLM_CLI_BIN", "") or "agy",
            llm_cli_flavor=e("LLM_CLI_FLAVOR", "").lower(),
            llm_cli_model=e("LLM_CLI_MODEL", ""),
            llm_cli_timeout_seconds=_int("LLM_CLI_TIMEOUT_SECONDS", 180),
            llm_cli_concurrency=max(1, _int("LLM_CLI_CONCURRENCY", 1)),
            llm_cli_pass_env=e("LLM_CLI_PASS_ENV", ""),
            llm_fit_analysis=_bool("LLM_FIT_ANALYSIS"),
            llm_email_review=_bool("LLM_EMAIL_REVIEW"),
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
        if self.llm_provider not in ("mock", "cli", "gemini", "openai"):
            raise RuntimeError(f"Unknown LLM_PROVIDER '{self.llm_provider}' (use mock, cli, gemini or openai).")
        if self.llm_provider == "gemini" and not self.gemini_api_key:
            raise RuntimeError("LLM_PROVIDER=gemini (API-key mode) requires GEMINI_API_KEY. To use your Google AI Pro "
                               "account instead, set LLM_PROVIDER=cli and sign in to the CLI.")
        if self.llm_provider == "cli":
            from .llm.cli import FLAVORS
            if self.llm_cli_flavor and self.llm_cli_flavor not in FLAVORS:
                raise RuntimeError(f"LLM_CLI_FLAVOR must be one of {FLAVORS}.")
            if not shutil.which(self.llm_cli_bin):
                raise RuntimeError(f"LLM_PROVIDER=cli but '{self.llm_cli_bin}' is not on PATH. Install the CLI and sign in "
                                   "with Google, or set LLM_CLI_BIN to its full path.")
        if self.mail_provider == "gmail" and not (self.google_client_id and self.google_client_secret):
            raise RuntimeError("MAIL_PROVIDER=gmail requires GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.")
        if self.mail_provider == "mock":
            self.send_interval_seconds = 0
        return warnings
