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


GUARD_SOURCES = ("LOLIFORCE", "SCSS")
IMPORT_SOURCE_LABEL = {
    "CLOSING": "Closing mensuel — stocks et mouvements de période",
    "LOLIFORCE": "Loliforce — relevés de stock terrain",
    "SCSS": "SCSS — relevés de stock terrain",
    "HISTORICAL_REFUELING": "Historique refueling — source historique",
    "TANK_MODELS": "Modèles de cuves — calculateur et conversion cm vers litres",
}


def guard_dry_run(file_bytes: bytes, file_name: str, resolver) -> dict:
    """Simule le contrôle ligne à ligne de l'import Loliforce / SCSS (mêmes règles que `process_guard_file`), SANS rien écrire."""
    from collections import Counter
    from datetime import timedelta

    from django.utils import timezone

    today = timezone.now().date()
    skipped: dict = {}
    codes, rejected_examples, sites, unknown, dates = Counter(), [], set(), {}, []
    n_lines = n_rejected = n_l = n_cm = n_both = 0
    for sheet, n, raw, rec in F.iter_guard_rows(file_bytes, file_name, skipped):
        n_lines += 1
        reasons = []
        site_raw = str(rec.get("site_id") or rec.get("site_name") or "").strip()
        site_id = resolver.resolve(rec.get("site_id")) if rec.get("site_id") else resolver.resolve_by_name(rec.get("site_name"))
        sites.add(site_raw)
        if site_id is None:
            reasons.append(("UNKNOWN_SITE", f"Site « {site_raw} » absent de Gestion des sites"))
            unknown[site_raw] = unknown.get(site_raw, 0) + 1
        d = F.parse_date(rec.get("date"))
        if d is None:
            reasons.append(("MISSING_DATE", "Date de relevé absente ou illisible"))
        elif d > today + timedelta(days=1):
            reasons.append(("FUTURE_DATE", f"Date dans le futur ({d:%d/%m/%Y})"))
        elif d.year < 2020:
            reasons.append(("IMPLAUSIBLE_DATE", f"Date invraisemblable ({d:%d/%m/%Y})"))
        else:
            dates.append(d)
        cm, lt = F.parse_level_cell(rec.get("cm"), "CM"), F.parse_level_cell(rec.get("l"), "L")
        if lt.readings and cm.readings:
            n_both += 1
        readings = lt.readings or cm.readings
        n_l += sum(1 for r in readings if r.unit == "L")
        n_cm += sum(1 for r in readings if r.unit == "CM")
        if not readings:
            errs = [p.error for p in (cm, lt) if p.error]
            reasons += [(e["code"], e["message"]) for e in errs] or [("MISSING_LEVEL", "Aucun niveau relevé (cm et litres vides)")]
        if reasons:
            n_rejected += 1
            for c, _ in reasons:
                codes[c] += 1
            if len(rejected_examples) < 10:
                rejected_examples.append({"feuille": sheet, "ligne": n, "site": site_raw, "motifs": [m for _, m in reasons]})
    known = len([x for x in sites if x and x not in unknown])
    return {
        "lignes_lues": n_lines, "lignes_rejetees": n_rejected, "lignes_acceptees": n_lines - n_rejected,
        "releves_en_litres": n_l, "releves_en_cm": n_cm, "lignes_avec_deux_unites": n_both,
        "unite_detectee": "cm + litres" if n_l and n_cm else "litres" if n_l else "cm" if n_cm else None,
        "motifs_rejet": dict(codes.most_common()), "exemples_rejets": rejected_examples,
        "sites_distincts": len(sites), "sites_reconnus": known, "sites_non_reconnus": len(unknown),
        "sites_non_reconnus_detail": dict(sorted(unknown.items(), key=lambda kv: -kv[1])[:15]),
        "periode_detectee": [min(dates).isoformat(), max(dates).isoformat()] if dates else None,
        "feuilles_ignorees": skipped,
    }


def analyze(file_bytes: bytes, file_name: str, source_type: str | None = None) -> dict:
    if not source_type:  # source non indiquée : détection par les colonnes / le nom, puis analyse avec le bon mapping
        first = _analyze(file_bytes, file_name, None)
        guess = first["source_detectee"]
        guess = "LOLIFORCE" if guess == "LOLIFORCE_OU_SCSS" else guess
        if guess:
            res = _analyze(file_bytes, file_name, guess)
            res["source_detectee"] = first["source_detectee"]
            return res
        return first
    return _analyze(file_bytes, file_name, source_type)


def _analyze(file_bytes: bytes, file_name: str, source_type: str | None = None) -> dict:
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
    controle = guard_dry_run(file_bytes, file_name, resolver) if src in GUARD_SOURCES else None
    retained = [x["feuille"] for x in sheets if not x["ignoree"] and x["lignes_de_donnees"]]
    all_dates = [d for x in sheets if x["plage_dates"] and not x["ignoree"] for d in x["plage_dates"]]
    return {
        "libelle_source": IMPORT_SOURCE_LABEL.get(src or ""), "feuilles_retenues": retained,
        "periode_detectee": [min(all_dates), max(all_dates)] if all_dates else None,
        "lignes_total": sum(x["lignes_de_donnees"] for x in sheets if not x["ignoree"]),
        "controle_lignes": controle,
        "fichier": file_name, "empreinte_sha256": hashlib.sha256(file_bytes).hexdigest(), "taille_octets": len(file_bytes),
        "source_detectee": guess, "source_analysee": src, "version_mapping": MAPPING_VERSIONS.get(src or ""),
        "feuilles": [{k: v for k, v in s.items() if k != "colonnes_normalisees"} for s in sheets],
        "couverture_sites": {"distincts": len(all_sites), "reconnus": recognized, "inconnus": len(all_sites) - recognized,
                             "note": "Loliforce et SCSS ne couvrent pas forcément les mêmes sites : mesure propre à ce fichier."},
        "lecture_seule": True,
    }
