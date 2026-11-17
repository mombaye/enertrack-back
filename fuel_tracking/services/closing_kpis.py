# fuel_tracking/services/closing_kpis.py
"""
KPI de synthèse du fichier Closing (en-tête de la feuille « Synthese conso fuel ») — mêmes indicateurs, même vocabulaire.

  - mois de RÉFÉRENCE (FUEL_CLOSING_REFERENCE_PERIODS, ex. septembre 2026) : valeurs LUES dans le fichier, telles quelles
    (enregistrées à l'import dans le résumé du lot : summary["stocks_closing"]["kpis_closing"]) ;
  - autres mois (octobre 2026 …) : valeurs CALCULÉES par EnerTrack à partir des bilans de stock enregistrés, avec les formules
    du fichier (vérifiées sur le Closing de septembre 2026) :
      Quantité laissée dans les cuves (début / fin)  = Σ stock initial / Σ stock final            (AT12 / CJ12)
      Quantité commandée et livrée entre les relevés = Σ livraisons (Eydon + Carte Total)          (BH+BJ+BK+BL+BM)
      Autre quantité (rajouts / retraits)            = Σ (rajouts − retraits ± ponctions)          (BG12)
      Quantité consommée entre les dates de relevés  = Σ variation de stock                        (EK12)
      Quantité perdue                                = Σ vols + Σ vidanges                         (CA12 + CB12)
      Conso moyenne / jour                           = moyenne des L/jour par site                  (ER12)
      Estimation conso du mois                       = Σ L/jour × nb jours du mois (nb jours entre relevés si le relevé
                                                       initial est après le 10 du mois)               (EZ12)
      Estimation conso du mois précédent             = Σ L/jour du mois précédent × nb jours du mois (FC12)
      Estimation stock fin de mois                   = Σ max(0 ; stock final − L/jour × jours jusqu'à la fin du mois) (CL12)
      Estimation livraison du mois                   = Σ livraisons + Σ rajouts / retraits          (EZ11)
      PROJECTION du mois suivant                     = Σ L/jour du mois × nb jours du mois suivant  (méthode du fichier :
                                                       c'est ainsi que le mois suivant calcule « l'estimation du mois précédent »)
Les commandes Aktivco (commandé / non livré) et le stock d'appoint ne sont pas dans les bilans : « non disponible » (jamais 0).
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

SHEET = "Synthese conso fuel"

# clé → (début du libellé en minuscules, côté de la valeur, rang de l'occurrence, ordre des occurrences)
SPEC = {
    "nb_sites_ge": ("nbre de site avec ge", -1, 0, "col"),
    "stock_initial_l": ("quantité laissée dans les cuves", -1, 0, "col"),
    "stock_final_l": ("quantité laissée dans les cuves", -1, 1, "col"),
    "appoint_initial_l": ("quantité laissée stock d", -1, 0, "col"),
    "appoint_final_l": ("quantité laissée stock d", -1, 1, "col"),
    "commande_mois_suivant_l": ("qtité commandée", -1, 0, "row"),
    "commande_non_livree_l": ("qtité commandée", -1, 0, "row"),
    "livree_l": ("quantité commandée et livrée entre", -1, 0, "row"),
    "autre_l": ("autre quantité livrée", -1, 0, "row"),
    "conso_moyenne_jour_l": ("conso moyenne / jour", -1, 0, "row"),
    "conso_releves_l": ("quantité consommée entre les dates", -1, 0, "row"),
    "perdue_l": ("quantité perdue", -1, 0, "row"),
    "check_l": ("check", -1, 0, "row"),
    "nb_sites": ("nbre de site", -1, 1, "col"),
    "nb_jours": ("nbre de jour", 1, 0, "row"),
    "ecart_theorique_l": ("ecart conso théorique", 1, 0, "row"),
    "vol_probable_l": ("probable quantité volée", 1, 0, "row"),
    "estimation_conso_mois_l": ("estimation conso fuel", 1, 0, "row"),
    "estimation_conso_prec_l": ("estimation conso fuel", 1, 1, "row"),
    "estimation_stock_fin_l": ("estimation stock fin", 1, 0, "row"),
    "estimation_stock_fin_prec_l": ("estimation stock fin", 1, 1, "row"),
    "estimation_livraison_l": ("estimation livraison fuel", 1, 0, "row"),
}
MOIS = ["JANVIER", "FÉVRIER", "MARS", "AVRIL", "MAI", "JUIN", "JUILLET", "AOÛT", "SEPTEMBRE", "OCTOBRE", "NOVEMBRE", "DÉCEMBRE"]


def _norm(v) -> str:
    return " ".join(str(v or "").replace("\n", " ").split()).strip()


def parse_kpis(rows) -> dict:
    """Bloc de KPI de l'en-tête du Closing (14 premières lignes) : {clé: {"libelle", "valeur"}}. Lecture seule, valeurs telles quelles."""
    grid = []
    for n, row in enumerate(rows):
        if n > 14:
            break
        grid.append(list(row or []))
    hits: dict[str, list[tuple[int, int, str]]] = {}
    for r, row in enumerate(grid):
        for c, v in enumerate(row):
            if not isinstance(v, str):
                continue
            low = _norm(v).lower()
            for key, (prefix, _side, _rank, _order) in SPEC.items():
                if low.startswith(prefix) and not (key == "nb_sites" and low != "nbre de site"):
                    if key.startswith("commande") and ("non-livrée" in low) != (key == "commande_non_livree_l"):
                        continue
                    if key == "check_l" and low != "check":
                        continue
                    hits.setdefault(key, []).append((r, c, _norm(v)))
    out = {}
    for key, (prefix, side, rank, order) in SPEC.items():
        occ = sorted(hits.get(key, []), key=(lambda x: (x[1], x[0])) if order == "col" else (lambda x: (x[0], x[1])))
        seen, uniq = set(), []
        for r, c, label in occ:
            k = c if order == "col" else r
            if k not in seen:
                seen.add(k)
                uniq.append((r, c, label))
        if key == "nb_sites":   # « Nbre de site » seul (pas « … avec GE »)
            uniq = [x for x in uniq if x[2].lower() == "nbre de site"]
            rank = 0
        if len(uniq) <= rank:
            continue
        r, c, label = uniq[rank]
        row = grid[r]
        v = row[c + side] if 0 <= c + side < len(row) else None
        out[key] = {"libelle": label, "valeur": float(v) if isinstance(v, (int, float)) else None}
    return out


def _month_bounds(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _prev_month(first: date) -> str:
    return (first - timedelta(days=1)).strftime("%Y-%m")


def _next_month(last: date) -> str:
    return (last + timedelta(days=1)).strftime("%Y-%m")


def _balances(month: str) -> list:
    from fuel_tracking.models import FuelStockBalance as B

    first, last = _month_bounds(month)
    return list(B.objects.filter(observation_start__date=first, observation_end__date=last))


def _f(v):
    return None if v is None else float(v)


def _daily(b) -> tuple[float | None, int | None]:
    """(L/jour, nb jours entre relevés) d'un bilan : variation / jours entre le relevé initial et le relevé final (EQ, ER du fichier)."""
    if b.variation_stock_l is None or not b.date_stock_initial or not b.date_stock_final:
        return None, None
    days = (b.date_stock_final.date() - b.date_stock_initial.date()).days
    if days <= 0:
        return None, None
    return float(b.variation_stock_l) / days, days


def _perimeter(month: str) -> set[str] | None:
    from fuel_tracking.services.conso_comparison import closing_base_sites
    from fuel_tracking.services.output_app_fuel import sites_without_genset

    base = closing_base_sites()
    return (base - sites_without_genset(month)) if base else None


def computed_kpis(month: str) -> dict:
    """KPI du mois calculés par EnerTrack (bilans enregistrés), mêmes définitions que le fichier Closing."""
    first, last = _month_bounds(month)
    n_days = (last - first).days + 1
    base = _perimeter(month)
    keep = lambda b: base is None or b.site_id.upper() in base  # noqa: E731
    rows = [b for b in _balances(month) if keep(b)]
    prev = {b.site_id: b for b in _balances(_prev_month(first)) if keep(b)}
    calc = [b for b in rows if b.variation_stock_l is not None]
    S = lambda f, xs=calc: round(sum(f(b) or 0 for b in xs), 1) if xs else None  # noqa: E731
    stock_ini, stock_fin = S(lambda b: _f(b.stock_initial_l)), S(lambda b: _f(b.stock_final_l))
    livree = S(lambda b: (_f(b.livraisons_externes_l) or 0) + (_f(b.carte_total_l) or 0))
    autre = S(lambda b: (_f(b.rajouts_l) or 0) - (_f(b.retraits_l) or 0) + (_f(b.transferts_entrants_l) or 0) - (_f(b.transferts_sortants_l) or 0))
    vols, vid = S(lambda b: _f(b.vols_l)), S(lambda b: _f(b.vidanges_l))
    conso = S(lambda b: _f(b.variation_stock_l))
    perdue = None if vols is None else round((vols or 0) + (vid or 0), 1)
    check = None if conso is None else round(stock_ini + livree + autre - stock_fin - conso - perdue, 1)
    daily = [(b, *_daily(b)) for b in calc]
    rates = [d for _b, d, _n in daily if d is not None]
    est_mois = est_stock = 0.0
    for b, d, n in daily:
        if d is None:
            continue
        late_start = b.date_stock_initial.date() > first - timedelta(days=1) + timedelta(days=10)
        est_mois += d * (n if late_start else n_days)
        gap = (last - b.date_stock_final.date()).days
        est_stock += max(0.0, float(b.stock_final_l) - max(0, gap) * d) if b.stock_final_l is not None else 0.0
    est_prec = est_stock_prec = 0.0
    prev_last = first - timedelta(days=1)
    for b in calc:
        p = prev.get(b.site_id)
        d = _daily(p)[0] if p is not None else None
        if d is None:
            continue
        est_prec += d * n_days
        if b.stock_initial_l is not None and b.date_stock_initial:
            gap = (prev_last - b.date_stock_initial.date()).days
            est_stock_prec += max(0.0, float(b.stock_initial_l) - max(0, gap) * d)
    nxt_first, nxt_last = _month_bounds(_next_month(last))
    projection = sum(d for _b, d, _n in daily if d is not None) * ((nxt_last - nxt_first).days + 1) if rates else None
    M, P, N = MOIS[first.month - 1], MOIS[prev_last.month - 1], MOIS[nxt_first.month - 1]
    y, py = first.year, prev_last.year
    v = lambda label, val: {"libelle": label, "valeur": None if val is None else round(val, 3)}  # noqa: E731
    return {
        "nb_sites_ge": v("Nbre de site avec GE", len(rows)),
        "stock_initial_l": v(f"Quantité laissée dans les cuves aux dates de relevés de {P} {py}", stock_ini),
        "stock_final_l": v(f"Quantité laissée dans les cuves aux dates de relevés de {M} {y}", stock_fin),
        "appoint_initial_l": v(f"Quantité laissée stock d'appoint aux dates de relevés de {P} {py}", None),
        "appoint_final_l": v(f"Quantité laissée stock d'appoint aux dates de relevés de {M} {y}", None),
        "commande_mois_suivant_l": v(f"Qtité Commandée {M} {y} pour Conso {N} chez Fournisseur Aktivco", None),
        "commande_non_livree_l": v(f"Qtité Commandée {M} {y} pour Conso {N} mais non-livrée chez Fournisseur Aktivco", None),
        "livree_l": v("Quantité commandée et livrée entre les dates de relevés", livree),
        "autre_l": v("Autre Quantité livrée / rajoutée ou retirée entre les dates de relevés", autre),
        # moyenne des L/jour sur TOUS les sites de la base (0 pour un site sans conso), comme ER12 du fichier
        "conso_moyenne_jour_l": v(f"Conso moyenne / jour {M} {y}", (sum(rates) / max(len(rates), len(base) if base is not None else 0))
                                  if rates else None),
        "conso_releves_l": v("Quantité consommée entre les dates de relevés", conso),
        "perdue_l": v("Quantité perdue", perdue),
        "check_l": v("CHECK", check),
        "nb_sites": v("Nbre de site", len(base) if base is not None else len(rows)),
        "nb_jours": v("Nbre de jour Mois actuel -->", n_days),
        "ecart_theorique_l": v("Ecart Conso Théorique Vs Conso Variation de Stock", vid),
        "vol_probable_l": v("Probable Quantité volée (Investigations à mener)", vols),
        "estimation_conso_mois_l": v(f"Estimation Conso Fuel {M} {y} Sites ESCO (Litres)", est_mois if rates else None),
        "estimation_conso_prec_l": v(f"Estimation Conso Fuel {P} {py} Sites ESCO (Litres)", est_prec if prev else None),
        "estimation_stock_fin_l": v(f"Estimation Stock fin {M} {y}", est_stock if rates else None),
        "estimation_stock_fin_prec_l": v(f"Estimation Stock fin {P} {py}", est_stock_prec if prev else None),
        "estimation_livraison_l": v(f"Estimation Livraison Fuel {M} {y}", None if livree is None else livree + (autre or 0)),
        "projection_mois_suivant_l": v(f"Projection Conso Fuel {N} {nxt_first.year} Sites ESCO (Litres)", projection),
    }


def _file_kpis(month: str) -> dict | None:
    from fuel_tracking.models import FuelImportBatch
    from fuel_tracking.services.stock_period_balance import reference_batches

    for pk in reference_batches(month):
        b = FuelImportBatch.objects.filter(pk=pk).first()
        k = ((b.summary or {}).get("stocks_closing") or {}).get("kpis_closing") if b else None
        if k:
            return {"kpis": k, "fichier": b.file_name}
    return None


def kpis_for_period(d_from: date, d_to: date) -> dict | None:
    """KPI « comme le fichier Closing » pour un mois civil complet ; None pour une autre période."""
    from fuel_tracking.services import stock_period_balance as SB

    first, last = _month_bounds(d_from.strftime("%Y-%m"))
    if d_from != first or d_to != last:
        return None
    month = first.strftime("%Y-%m")
    calc = computed_kpis(month)
    if month in (SB.cfg().get("reference_periods") or []):
        f = _file_kpis(month)
        if f:
            kpis = dict(f["kpis"])
            kpis["projection_mois_suivant_l"] = calc["projection_mois_suivant_l"]   # le fichier ne la donne pas : méthode du fichier
            return {"mois": month, "source": "FICHIER_CLOSING", "fichier": f["fichier"], "kpis": kpis,
                    "note": "Mois de référence : valeurs lues dans l'en-tête du fichier Closing, telles quelles. Projection : "
                            "conso moyenne L/jour du mois × nb de jours du mois suivant (méthode du fichier)."}
    return {"mois": month, "source": "CALCUL_ENERTRACK", "fichier": None, "kpis": calc,
            "note": "Valeurs calculées par EnerTrack à partir des bilans de stock du mois, avec les formules du fichier Closing. "
                    "Commandes Aktivco et stock d'appoint : non disponibles dans les bilans."}


def backfill_file_kpis() -> int:
    """Closing importés avant le 17/11/2026 : relit la copie d'archive pour enregistrer le bloc de KPI de l'en-tête."""
    from pathlib import Path

    from fuel_tracking.models import FuelImportBatch
    from fuel_tracking.services.stock_import_files import iter_sheets

    done = 0
    for b in FuelImportBatch.objects.filter(source_type="CLOSING"):
        s = b.summary or {}
        st = s.get("stocks_closing") or {}
        if "annulation" in s or st.get("format") == "Output App Fuel" or "kpis_closing" in st:
            continue
        if not b.archive_path or not Path(b.archive_path).is_file():
            continue
        k = {}
        for _n, rows in iter_sheets(Path(b.archive_path).read_bytes(), b.file_name, only={SHEET}):
            k = parse_kpis(rows)
        st["kpis_closing"] = k
        b.summary = {**s, "stocks_closing": st}
        b.save(update_fields=["summary"])
        done += 1
    return done
