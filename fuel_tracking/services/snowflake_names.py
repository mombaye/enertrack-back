"""Noms d'objets Snowflake réglables par configuration (jamais de secret ici)."""
import re

from django.conf import settings

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,254}$")
DEFAULT_FUEL_REPORT_DATABASE = "DB_GFMS_ANALYTICS_DEV"


def fuel_report_database() -> str:
    """Base de VW_FUEL_REPORT. Le nom est inséré dans le SQL : seul un identifiant Snowflake simple est accepté."""
    name = str(getattr(settings, "FUEL_REPORT_DATABASE", DEFAULT_FUEL_REPORT_DATABASE) or DEFAULT_FUEL_REPORT_DATABASE).strip()
    if not _IDENT.match(name):
        raise ValueError("FUEL_REPORT_DATABASE doit être un identifiant Snowflake simple (lettres, chiffres, _).")
    return name
