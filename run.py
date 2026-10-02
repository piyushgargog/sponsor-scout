"""Start the app (dev server + in-process worker). Production: use gunicorn with a single process, see README."""
import os

from app import create_app
from app.cli import seed_demo_event
from app.config import Settings

if __name__ == "__main__":
    settings = Settings.from_env()
    app = create_app(settings)
    if not settings.is_production:
        seed_demo_event(app.extensions["svc"])
    port = int(os.environ.get("PORT", "5000"))
    print(f"\n  Sponsor Scout running at http://localhost:{port}  (login password in development: ADMIN_PASSWORD or 'demo')\n")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
