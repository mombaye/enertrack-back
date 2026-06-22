# ml/features/zones.py
from __future__ import annotations

import re
import unicodedata


def _clean(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.upper().strip()
    value = re.sub(r"[^A-Z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


ZONE_ALIASES = {
    "DKR": "DKR",
    "DAKAR": "DKR",
    "DAK": "DKR",

    "THIES": "THIES",
    "THI": "THIES",
    "THS": "THIES",

    "DIOURBEL": "DIOURBEL",
    "DBL": "DIOURBEL",
    "DIO": "DIOURBEL",
    "TOUBA": "DIOURBEL",
    "MBACKE": "DIOURBEL",
    "LOUGA": "LOUGA",
    "LOU": "LOUGA",

    "KAOLACK": "KAOLACK",
    "KAO": "KAOLACK",

    "ZIGUINCHOR": "ZIGUINCHOR",
    "ZIG": "ZIGUINCHOR",

    "SAINT LOUIS": "SAINT-LOUIS",
    "SAINT-LOUIS": "SAINT-LOUIS",
    "ST LOUIS": "SAINT-LOUIS",
    "ST-LOUIS": "SAINT-LOUIS",
    "SLO": "SAINT-LOUIS",

    "TAMBACOUNDA": "TAMBACOUNDA",
    "TAM": "TAMBACOUNDA",

    "KOLDA": "KOLDA",
    "KOL": "KOLDA",

    "FATICK": "FATICK",
    "FAT": "FATICK",

    "MATAM": "MATAM",
    "MAT": "MATAM",

    "KAFFRINE": "KAFFRINE",
    "KAF": "KAFFRINE",

    "SEDHIOU": "SEDHIOU",
    "SED": "SEDHIOU",

    "KEDOUGOU": "KEDOUGOU"
}


SITE_NAME_ZONE_HINTS = [
    ("TOUBA", "DIOURBEL"),
    ("MBACKE", "DIOURBEL"),
    ("MBOUSSO", "DIOURBEL"),
    ("DAROU", "DIOURBEL"),
    ("TIVAOUANE", "THIES"),
    ("MBOUR", "THIES"),
    ("CAMBERENE", "DKR"),
    ("LAYENE", "DKR"),
    ("PIKINE", "DKR"),
    ("GUEDIAWAYE", "DKR"),
    ("RUFISQUE", "DKR"),
]


def normalize_zone(zone: str | None, site_name: str | None = None, site_id: str | None = None) -> str:
    """Normalise les zones pour météo + événements.

    Exemples : DAKAR, Dakar, DKR -> DKR ; DBL, Touba, Mbacké -> DIOURBEL.
    """
    raw_zone = _clean(zone)
    name = _clean(site_name)
    sid = _clean(site_id)

    for needle, normalized in SITE_NAME_ZONE_HINTS:
        if needle in name or needle in sid:
            return normalized

    if raw_zone in ZONE_ALIASES:
        return ZONE_ALIASES[raw_zone]

    # Cas où la zone contient plusieurs tokens : "ZONE DAKAR", "REGION THIES", etc.
    for alias, normalized in ZONE_ALIASES.items():
        if alias and alias in raw_zone:
            return normalized

    return raw_zone or "DKR"