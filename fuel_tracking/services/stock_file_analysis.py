# fuel_tracking/services/stock_file_analysis.py
"""
Analyse PRÉALABLE d'un fichier de stock terrain (LECTURE SEULE : rien n'est importé, aucune donnée n'est écrite) :
feuilles, ligne d'en-tête, colonnes, mapping appliqué (colonne → champ eGrid), colonnes non reconnues, lignes de données,
plage de dates, aperçu. Loliforce et SCSS ne sont jamais supposés couvrir les mêmes sites : la couverture est mesurée
fichier par fichier (sites distincts, reconnus / inconnus de Gestion des sites).
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime

from fuel_tracking.services import stock_import_files as F

MAPPING_VERSIONS = {
    "LOLIFORCE": "gardiennage-v1", "SCSS": "gardiennage-v1", "CLOSING": "closing-v1",
    "HISTORICAL_REFUELING": "refueling-v1", "TANK_MODELS": "tank-models-v1",
}
MAPPINGS = {
    "LOLIFORCE": F.GUARD_COLS, "SCSS": F.GUARD_COLS, "HISTORICAL_REFUELING": F.REFUEL_COLS,
    "CLOSING": {**F.TANK_COLS, **{c: f"(contexte) {c}" for c in F.CLOSING_EXTRA}},
}
SITE_FIELDS = {"site_id"}
DATE_FIELDS = {"date"}


def _cell(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return None if v is None else str(v)[:60]


def guess_source(sheets: list[dict], file_name: str) -> str | None:
    heads = {h for s in sheets for h in s["colonnes_normalisees"]}
    if "relevé en cm" in heads or "site gardé par" in heads:
        name = file_name.lower()
        return "SCSS" if "scss" in name else "LOLIFORCE" if "loli" in name else "LOLIFORCE_OU_SCSS"
    if "n° ticket fms" in heads or "qtt initiale mesurée sur site (cm)" in heads:
        return "HISTORICAL_REFUELING"
    if "forme géométrique cuve" in heads or "id du site" in heads:
        return "CLOSING"
    return None


def analyze(file_bytes: bytes, file_name: str, source_type: str | None = None) -> dict:
    from core.models import Site
    from fuel_tracking.services.stock_registry import SiteResolver

    resolver = SiteResolver()
    sheets, all_sites = [], set()
    for name, rows in F.iter_sheets(file_bytes, file_name):
        header, header_row, n_rows, dates, sample, sites = None, None, 0, [], [], set()
        mapping = MAPPINGS.get((source_type or "").upper(), {})
        for n, row in enumerate(rows, start=1):
            vals = list(row or [])
            normed = [F._norm(v) for v in vals]
            known = sum(1 for h in normed if h in mapping) if mapping else 0
            if header is None and (known >= 2 or (not mapping and sum(1 for v in vals if isinstance(v, str) and v.strip()) >= 4)):
                header, header_row = normed, n
                continue
            if header is None or not any(v not in (None, "") for v in vals):
                continue
            first = vals[0]
            if isinstance(first, str) and first.strip().upper().startswith("SEMAINE"):
                continue
            if mapping and sum(1 for h in normed if h in mapping) >= 2:  # en-tête répété (blocs hebdomadaires)
                header = normed
                continue
            n_rows += 1
            rec = {header[i]: vals[i] for i in range(min(len(header), len(vals))) if header[i]}
            if len(sample) < 3:
                sample.append({k: _cell(v) for k, v in list(rec.items())[:12]})
            for h, field in mapping.items():
                if field in DATE_FIELDS and rec.get(h) is not None:
                    d = F.parse_date(rec.get(h))
                    if d:
                        dates.append(d)
                if field in SITE_FIELDS and rec.get(h) not in (None, ""):
                    sites.add(str(rec[h]).strip())
        cols = [h for h in (header or []) if h]
        sheets.append({
            "feuille": name, "ligne_en_tete": header_row, "lignes_de_donnees": n_rows, "colonnes": cols, "colonnes_normalisees": cols,
            "colonnes_mappees": {h: mapping[h] for h in cols if h in mapping},
            "colonnes_non_reconnues": [h for h in cols if mapping and h not in mapping],
            "ignoree": F.SKIPPED_GUARD_SHEETS.get(name.strip().lower()),
            "plage_dates": [min(dates).isoformat(), max(dates).isoformat()] if dates else None,
            "sites_distincts": len(sites), "apercu": sample,
        })
        if not F.SKIPPED_GUARD_SHEETS.get(name.strip().lower()):
            all_sites |= sites
    known_ids = {s.upper() for s in Site.objects.values_list("site_id", flat=True)}
    recognized = sum(1 for s in all_sites if s.upper() in known_ids or resolver.resolve(s))
    guess = guess_source(sheets, file_name)
    src = (source_type or "").upper() or None
    return {
        "fichier": file_name, "empreinte_sha256": hashlib.sha256(file_bytes).hexdigest(), "taille_octets": len(file_bytes),
        "source_detectee": guess, "source_analysee": src, "version_mapping": MAPPING_VERSIONS.get(src or ""),
        "feuilles": [{k: v for k, v in s.items() if k != "colonnes_normalisees"} for s in sheets],
        "couverture_sites": {"distincts": len(all_sites), "reconnus": recognized, "inconnus": len(all_sites) - recognized,
                             "note": "Loliforce et SCSS ne couvrent pas forcément les mêmes sites : mesure propre à ce fichier."},
        "lecture_seule": True,
    }
