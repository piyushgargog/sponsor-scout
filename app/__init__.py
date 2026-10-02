from datetime import timedelta

from flask import Flask

from .config import Settings
from .jobs import Worker
from .logging_setup import log_event, setup_logging
from .security import RateLimiter, apply_security_headers, csrf_token
from .services import build_services


def create_app(settings: Settings | None = None, services=None, start_worker: bool | None = None) -> Flask:
    settings = settings or Settings.from_env()
    warnings = settings.validate_for_runtime()
    setup_logging(settings.log_level)
    for w in warnings:
        log_event("config_warning", message=w)
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.update(SECRET_KEY=settings.secret_key, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=settings.is_production, PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
                      MAX_CONTENT_LENGTH=256 * 1024)
    svc = services or build_services(settings)
    app.extensions["svc"] = svc
    app.extensions["limiter"] = RateLimiter()
    app.jinja_env.globals["csrf_token"] = csrf_token
    app.after_request(apply_security_headers)

    from .web import bp, register_filters
    register_filters(app)
    app.register_blueprint(bp)

    if start_worker is None:
        start_worker = settings.worker_enabled
    if start_worker:
        worker = Worker(svc.queue, settings.worker_threads)
        worker.start()
        app.extensions["worker"] = worker
    log_event("app_started", env=settings.app_env, llm=svc.llm.name, model=svc.llm.model or "auto", search=svc.search.name,
              mail=svc.mailer.name)
    return app
