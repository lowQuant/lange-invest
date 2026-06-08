"""WSGI entry point for PythonAnywhere (or any WSGI host).

lange-invest is a FastAPI (ASGI) app; PythonAnywhere's web tab serves WSGI, so we
adapt the ASGI app to WSGI with a2wsgi. Copy the body of this file into the WSGI
configuration file that PythonAnywhere shows in the Web tab, adjusting PROJECT_HOME.

Checklist on PythonAnywhere:
  1. Web tab → set the Virtualenv to the venv where you `pip install -r requirements.txt`
     (must include a2wsgi).
  2. Create PROJECT_HOME/.env with your secrets (see .env.example): at minimum
     SESSION_SECRET and either LANGE_DB_URI or the AWS_*/BUCKET_NAME S3 vars.
  3. (Optional) Web tab → Static files → URL /static/  →  PROJECT_HOME/app/static
  4. Reload the web app.
  5. In a Bash console (venv active, cd PROJECT_HOME):
        python scripts/manage_users.py add --username admin --role admin
"""
import os
import sys
from pathlib import Path

# Adjust to your clone path:
PROJECT_HOME = "/home/lowquant/lange-invest"

if PROJECT_HOME not in sys.path:
    sys.path.insert(0, PROJECT_HOME)

# Load environment variables from the project's .env so every os.getenv sees them.
from dotenv import load_dotenv  # noqa: E402
load_dotenv(Path(PROJECT_HOME) / ".env")

# Adapt the FastAPI (ASGI) app to a WSGI callable named `application`.
from a2wsgi import ASGIMiddleware  # noqa: E402
from app.main import app as asgi_app  # noqa: E402

application = ASGIMiddleware(asgi_app)

# Connect ArcticDB once per worker so the first request isn't slow. Safe to skip —
# the app also connects lazily on first use.
try:
    from app.engine import ensure_connected  # noqa: E402
    ensure_connected()
except Exception:
    pass
