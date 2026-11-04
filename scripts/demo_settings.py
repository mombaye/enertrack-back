"""Réglages de DÉMO LOCALE (SQLite, données synthétiques). Jamais utilisés en production : activés uniquement par scripts/demo_ui.sh."""
from pathlib import Path

from enertrack_backend.settings import *  # noqa

_DEMO = Path(__file__).resolve().parent.parent / ".demo"
_DEMO.mkdir(exist_ok=True)
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(_DEMO / "demo.sqlite3")}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_TASK_ALWAYS_EAGER = True
FUEL_IMPORT_ROOT = str(_DEMO / "import_root")
ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE = True
DEBUG = True
