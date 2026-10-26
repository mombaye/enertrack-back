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
    names = {s["feuille"].strip().lower() for s in sheets}
    if {"tank db", "barèmes"} <= names or {"calculateur", "tank db"} <= names:
        return "TANK_MODELS"
    heads = {h for s in sheets for h in s["colonnes_normalisees"]}
    if "relevé en cm" in heads or "site gardé par" in heads:
        name = file_name.lower()
        return "SCSS" if "scss" in name else "LOLIFORCE" if "loli" in name else "LOLIFORCE_OU_SCSS"
    if "n° ticket fms" in heads or "qtt initiale mesurée sur site (cm)" in heads:
        return "HISTORICAL_REFUELING"
    if "forme géométrique cuve" in heads or "id du site" in heads or CLOSING_SHEET.lower() in names:
        return "CLOSING"
    return None


GUARD_SOURCES = ("LOLIFORCE", "SCSS")
MIN_YEAR = 2020


def plausible(d: date) -> bool:
    from datetime import timedelta

    from django.utils import timezone

    return d.year >= MIN_YEAR and d <= timezone.now().date() + timedelta(days=1)


# ── Closing mensuel : lecture par BLOCS (les en-têtes se répètent : stock initial puis stock final) ──────────────────
CLOSING_SHEET = "Synthese conso fuel"


def _first_idx(header: list[str], prefix: str, lo: int = 0, hi: int | None = None) -> int | None:
    for i in range(lo, len(header) if hi is None else hi):
        if header[i].startswith(prefix):
            return i
    return None


def closing_stock_rows(rows) -> tuple[list[dict], dict]:
    """
    Lit la feuille de synthèse du Closing en tenant compte des en-têtes répétés :
      bloc INITIAL  : « date de relevé initiale », « qtité laissée à la date du relevé » (stock initial), puis les mouvements de la période
                      (« qtité totale livrée », « qté rajoutée ou retirée… », « qté volée », « qté vidangée… ») ;
      bloc FINAL    : « date de relevé finale », « qtité laissée à la date du relevé » (stock final), « qtité cons considéréé (l) ».
    Retourne (lignes par site, diagnostic de colonnes). Aucune écriture.
    """
    header, h_idx, out = None, None, []
    for n, row in enumerate(rows, start=1):
        if header is None:
            if n > 60:
                break
            normed = [F._norm(v) for v in (row or [])]
            if "id du site" in normed and any(h.startswith("date de relevé initiale") for h in normed):
                header, h_idx = normed, {}
                i0 = _first_idx(header, "date de relevé initiale")
                i1 = _first_idx(header, "date de relevé finale", i0 + 1)
                i_end = _first_idx(header, "test date", i1 + 1) or len(header)
                h_idx = {
                    "site": header.index("id du site"), "nb_ge": _first_idx(header, "nombre de ge installé"),
                    "date_initiale": i0, "stock_initial": _first_idx(header, "qtité laissée à la date du relevé", i0 + 1, i1),
                    "livre": _first_idx(header, "qtité totale livrée", i0 + 1, i1),
                    "rajout_retrait": _first_idx(header, "qté rajoutée ou retirée", i0 + 1, i1),
                    "vole": _first_idx(header, "qté volée", i0 + 1, i1),
                    "vidange": _first_idx(header, "qté vidangée", i0 + 1, i1),
                    "date_finale": i1, "stock_final": _first_idx(header, "qtité laissée à la date du relevé", i1 + 1, i_end),
                    "conso_closing": _first_idx(header, "qtité cons considéréé", i1 + 1, i_end),
                    "conso_niveau": _first_idx(header, "qtité consommée sur la période de relevé niveau fuel", i1 + 1, i_end),
                }
            continue
        if not row:
            continue
        sid = row[h_idx["site"]] if h_idx["site"] < len(row) else None
        if not isinstance(sid, str) or "_" not in sid:
            continue
        get = lambda k: row[h_idx[k]] if h_idx.get(k) is not None and h_idx[k] < len(row) else None  # noqa: E731
        out.append({
            "site": sid.strip(), "ligne": n,
            "date_initiale": F.parse_date(get("date_initiale")), "stock_initial_l": F.parse_number(get("stock_initial")),
            "date_finale": F.parse_date(get("date_finale")), "stock_final_l": F.parse_number(get("stock_final")),
            "livre_l": F.parse_number(get("livre")), "rajout_retrait_l": F.parse_number(get("rajout_retrait")),
            "vole_l": F.parse_number(get("vole")), "vidange_l": F.parse_number(get("vidange")),
            "conso_closing_l": F.parse_number(get("conso_closing")), "conso_niveau_l": F.parse_number(get("conso_niveau")),
            "nb_ge": F.parse_number(get("nb_ge")),
        })
    diag = {k: (None if v is None else (header[v] if header and v < len(header) else None)) for k, v in (h_idx or {}).items()} if header else None
    return out, {"colonnes_utilisees": diag, "en_tete_trouve": header is not None}


def closing_formula(r: dict) -> tuple[float | None, float | None, bool | None]:
    """(conso calculée, conso du Closing, cohérent ?) — None si une valeur manque."""
    if r["stock_initial_l"] is None or r["stock_final_l"] is None:
        return None, None, None
    ref = r["conso_closing_l"] if r["conso_closing_l"] is not None else r["conso_niveau_l"]
    calc = r["stock_initial_l"] + (r["livre_l"] or 0) + (r["rajout_retrait_l"] or 0) - (r["vole_l"] or 0) - (r["vidange_l"] or 0) - r["stock_final_l"]
    if ref is None:
        return calc, None, None
    return calc, ref, abs(calc - ref) <= max(20.0, 0.05 * abs(ref))


def closing_stock_preview(file_bytes: bytes, file_name: str, resolver) -> dict | None:
    """Aperçu (sans rien importer) des stocks initial / final et des mouvements du Closing + contrôle de cohérence automatique."""
    from statistics import median

    for name, rows in F.iter_sheets(file_bytes, file_name, only={CLOSING_SHEET}):
        data, diag = closing_stock_rows(rows)
        if not diag["en_tete_trouve"]:
            return {"feuille": name, "en_tete_trouve": False}
        both = [r for r in data if r["stock_initial_l"] is not None and r["stock_final_l"] is not None]
        coherent = checked = 0
        diffs = []
        for r in both:
            ref = r["conso_closing_l"] if r["conso_closing_l"] is not None else r["conso_niveau_l"]
            if ref is None:
                continue
            calc = r["stock_initial_l"] + (r["livre_l"] or 0) + (r["rajout_retrait_l"] or 0) - (r["vole_l"] or 0) - (r["vidange_l"] or 0) - r["stock_final_l"]
            checked += 1
            tol = max(20.0, 0.05 * abs(ref))
            if abs(calc - ref) <= tol:
                coherent += 1
            elif len(diffs) < 5:
                diffs.append({"site": r["site"], "calcule_l": round(calc, 1), "conso_closing_l": ref})
        di = [r["date_initiale"] for r in data if r["date_initiale"] and plausible(r["date_initiale"])]
        df = [r["date_finale"] for r in data if r["date_finale"] and plausible(r["date_finale"])]
        q = lambda k: [r[k] for r in data if r[k] is not None]  # noqa: E731
        rng = lambda v: None if not v else {"min": round(min(v), 1), "mediane": round(median(v), 1), "max": round(max(v), 1)}  # noqa: E731
        known = {r["site"] for r in data if resolver.resolve(r["site"])}
        return {
            "feuille": name, "en_tete_trouve": True, "colonnes_utilisees": diag["colonnes_utilisees"],
            "sites_lus": len({r["site"] for r in data}), "sites_reconnus": len(known),
            "stock_initial_present": len(q("stock_initial_l")), "stock_final_present": len(q("stock_final_l")),
            "sites_avec_les_deux_stocks_et_dates": sum(1 for r in both if r["date_initiale"] and r["date_finale"]),
            "dates_initiales": [min(di).isoformat(), max(di).isoformat()] if di else None,
            "dates_finales": [min(df).isoformat(), max(df).isoformat()] if df else None,
            "stock_initial_l": rng(q("stock_initial_l")), "stock_final_l": rng(q("stock_final_l")), "livre_l": rng(q("livre_l")),
            "controle_coherence": {"sites_verifies": checked, "formule_coherente": coherent,
                                   "formule": "stock initial + livré + rajouts/retraits − volé − vidangé − stock final ≈ conso du Closing (±5 % ou 20 L)",
                                   "exemples_ecarts": diffs},
            "apercu": [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in r.items()} for r in data[:3]],
        }
    return None
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
    declared, odd_dates = Counter(), set()
    n_lines = n_rejected = n_l = n_cm = n_both = n_empty = 0
    for sheet, n, raw, rec in F.iter_guard_rows(file_bytes, file_name, skipped):
        if rec.get("cm") in (None, "") and rec.get("l") in (None, "") and rec.get("date") in (None, ""):
            n_empty += 1  # ligne de modèle pré-remplie (site seul) : ni date ni niveau — pas un relevé, pas une erreur
            continue
        n_lines += 1
        g = F.GUARD_ALIASES.get(" ".join(str(rec.get("guard") or "").split()).upper())
        if g:
            declared[g] += 1
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
        if d is not None and not plausible(d):
            odd_dates.add(d.isoformat())
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
        "lignes_modele_vides_ignorees": n_empty, "dates_invraisemblables": sorted(odd_dates),
        "gardiennage_declare": dict(declared), "feuilles_ignorees": skipped,
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
        odd_dates: list = []
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
                    if d and plausible(d):
                        dates.append(d)
                    elif d:
                        odd_dates.append(d)
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
            "dates_invraisemblables": sorted({d.isoformat() for d in odd_dates})[:10] if odd_dates else [],
        })
        if not F.SKIPPED_GUARD_SHEETS.get(name.strip().lower()):
            all_sites |= sites
    known_ids = {s.upper() for s in Site.objects.values_list("site_id", flat=True)}
    recognized = sum(1 for s in all_sites if s.upper() in known_ids or resolver.resolve(s))
    guess = guess_source(sheets, file_name)
    src = (source_type or "").upper() or None
    effective = src or ("LOLIFORCE" if guess == "LOLIFORCE_OU_SCSS" else guess)
    controle = guard_dry_run(file_bytes, file_name, resolver) if effective in GUARD_SOURCES else None
    closing = closing_stock_preview(file_bytes, file_name, resolver) if effective == "CLOSING" else None
    retained = [x["feuille"] for x in sheets if not x["ignoree"] and x["lignes_de_donnees"]]
    all_dates = [d for x in sheets if x["plage_dates"] and not x["ignoree"] for d in x["plage_dates"]]  # dates plausibles uniquement
    return {
        "libelle_source": IMPORT_SOURCE_LABEL.get(src or ""), "feuilles_retenues": retained,
        "periode_detectee": [min(all_dates), max(all_dates)] if all_dates else None,
        "lignes_total": sum(x["lignes_de_donnees"] for x in sheets if not x["ignoree"]),
        "controle_lignes": controle, "closing_stocks": closing,
        "fichier": file_name, "empreinte_sha256": hashlib.sha256(file_bytes).hexdigest(), "taille_octets": len(file_bytes),
        "source_detectee": guess, "source_analysee": src, "version_mapping": MAPPING_VERSIONS.get(src or ""),
        "feuilles": [{k: v for k, v in s.items() if k != "colonnes_normalisees"} for s in sheets],
        "couverture_sites": {"distincts": len(all_sites), "reconnus": recognized, "inconnus": len(all_sites) - recognized,
                             "note": "Loliforce et SCSS ne couvrent pas forcément les mêmes sites : mesure propre à ce fichier."},
        "lecture_seule": True,
    }
