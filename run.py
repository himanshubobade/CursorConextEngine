"""Entry point: python run.py

Serves the API and, if web/dist exists, the built frontend from the same
origin. During frontend development run `npm run dev` in web/ instead and
use the Vite dev server, which proxies /api back here.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn

from app.config import get_settings


def main() -> int:
    settings = get_settings()
    host = settings.get("server", "host", default="127.0.0.1")
    port = int(settings.get("server", "port", default=8100))
    reload_enabled = "--reload" in sys.argv

    print(f"ContextEngine on http://{host}:{port}")
    uvicorn.run("app.main:app", host=host, port=port, reload=reload_enabled)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
