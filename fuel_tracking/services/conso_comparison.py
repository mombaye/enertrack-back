# fuel_tracking/services/conso_comparison.py
"""
« Suivis Consommations » : UN tableau, UNE période exacte (jours entiers 00:00:00 → 23:59:59), UNE ligne par site, trois consommations côte à côte :

  Consommation réelle stock (L)            bilan physique des stocks et mouvements validés (FuelStockBalance, règle bilan-v2)
  Consommation instrumentée Snowflake (L) capteurs / niveau de cuve (VW_FUEL_REPORT), sur les jours exploitables
  Consommation théorique CPH (L)           runtime GE qualifié × puissance qualifiée × courbe CPH (moteur CPH-PRP-50HZ)

Écarts (même période pour les trois) :  écart réel vs instrumentée = instrumentée − réelle ;  écart réel vs théorique = théorique − réelle.
Pourcentage = écart / consommation réelle, « non applicable » (référence quasi nulle) sous FUEL_COMPARE_MIN_REFERENCE_L. Une donnée absente reste NULL.
Aucune simulation : si la consommation réelle n'existe pas, aucun écart n'est calculé.
"""
from __future__ import annotations

import csv
import io
from datetime import date
from decimal import Decimal

from django.conf import settings

from fuel_tracking.services.period_utils import day_bounds, latest_balances_by_site
from fuel_tracking.services.stock_period_balance import alert_codes_of

# ── Libellés métier ─────────────────────────────────────────────────────────────────────────────────────────────────
LABELS = {
    "reel": "Consommation réelle stock (L)",
    "instrumentee": "Consommation instrumentée Snowflake (L)",
    "theorique": "Consommation théorique CPH (L)",
}
HELP = {
    "reel": "Consommation obtenue à partir du bilan physique des stocks et mouvements validés : stock initial + livraisons externes + Carte Total + transferts entrants + rajouts − "
            "transferts sortants − retraits − vols − vidanges − stock final. Une donnée absente reste vide, jamais 0.",
    "instrumentee": "Consommation déduite des capteurs / mesure de niveau Snowflake. Ce n'est PAS la consommation réelle certifiée.",
    "theorique": "Estimation à partir du runtime GE qualifié, de la puissance qualifiée et de la courbe CPH. Vide si le runtime, la puissance ou la courbe manquent.",
    "ecart_instrumentee": "Consommation instrumentée − consommation réelle stock. Positif : le capteur voit plus de consommation que le bilan de stock.",
    "ecart_theorique": "Consommation théorique CPH − consommation réelle stock. Positif : le CPH attend plus de consommation que le bilan de stock.",
    "estimation_avec_reserve": "Valeur calculée mais comportant une source ou une conversion non certifiée (cuve sans plaque, cuve proposée, stock ancien, livraison issue du Closing…).",
    "non_calculable": "Valeur non calculée faute de prérequis suffisants.",
}

# Statut de la consommation réelle (qualité du bilan de stock)
CERTIFIABLE, ESTIMATION, A_CONTROLER, DONNEES_INCOMPLETES, NON_CALCULABLE = (
    "CERTIFIABLE", "ESTIMATION_AVEC_RESERVE", "A_CONTROLER", "DONNEES_INCOMPLETES", "NON_CALCULABLE")
REEL_STATUTS = (CERTIFIABLE, ESTIMATION, A_CONTROLER, DONNEES_INCOMPLETES, NON_CALCULABLE)
_A_CONTROLER_CAUSES = {"CONFLIT_ENOC_CLOSING", "CONSO_NEGATIVE", "MOUVEMENTS_A_CONTROLER", "TRANSFERT_DESEQUILIBRE", "RELEVES_INCOHERENTS"}
_INCOMPLETES_CAUSES = {"DONNEES_STOCK_INCOMPLETES", "CM_SANS_CUVE", "STOCK_TROP_ANCIEN"}

# Statut global (comparaison des trois consommations)
COHERENT, A_JUSTIFIER, A_INVESTIGUER = "COHERENT", "A_JUSTIFIER", "A_INVESTIGUER"
G_RESERVE, G_INCOMPLETES, G_CPH_NON_CALCULE, G_INSTR_ABSENTE = "ESTIMATION_AVEC_RESERVE", "DONNEES_INCOMPLETES", "CPH_NON_CALCULE", "MESURE_INSTRUMENTEE_ABSENTE"
GLOBAL_STATUTS = (COHERENT, A_JUSTIFIER, A_INVESTIGUER, G_RESERVE, G_INCOMPLETES, G_CPH_NON_CALCULE, G_INSTR_ABSENTE)
_SEVERITY = {A_INVESTIGUER: 0, A_JUSTIFIER: 1, G_INCOMPLETES: 2, G_RESERVE: 3, G_CPH_NON_CALCULE: 4, G_INSTR_ABSENTE: 5, COHERENT: 6}


def _f(v, places=3):
    return None if v is None else round(float(v), places)


def _cfg(name, default):
    return getattr(settings, name, default)


def reel_status(b) -> str:
    """Statut de la consommation réelle à partir du bilan (jamais de valeur sur A_CONTROLER / DONNEES_INCOMPLETES / NON_CALCULABLE)."""
    if b is None:
        return NON_CALCULABLE
    if b.statut_bilan == "PRET_A_CERTIFIER":
        return CERTIFIABLE
    if b.statut_bilan == "ESTIMATION_AVEC_RESERVE":
        return ESTIMATION
    if b.cause_principale in _A_CONTROLER_CAUSES:
        return A_CONTROLER
    if b.cause_principale in _INCOMPLETES_CAUSES or b.cause_principale is None:
        return DONNEES_INCOMPLETES
    return NON_CALCULABLE


def _source_text(b) -> str | None:
    if b is None:
        return None
    lab = {"CLOSING": "Closing", "LOLIFORCE": "Loliforce", "SCSS": "SCSS", "ENOC": "ENOC", "HISTORICAL_REFUELING": "Historique refueling"}
    si, sf = lab.get(b.source_stock_initial, b.source_stock_initial), lab.get(b.source_stock_final, b.source_stock_final)
    stocks = None if not (si or sf) else (f"Stocks : {si} → {sf}" if si != sf else f"Stocks : {si}")
    mv = sorted({lab.get(m.get("source"), m.get("source")) for m in (b.mouvements or []) if m.get("source")})
    return " · ".join(x for x in (stocks, f"Mouvements : {', '.join(mv)}" if mv else None) if x)


def _pct(gap, base) -> tuple[float | None, str | None]:
    """(pourcentage, message). Référence = consommation réelle ; sous le seuil de référence quasi nulle : pas de pourcentage."""
    if gap is None:
        return None, None
    if base is None or abs(float(base)) < float(_cfg("FUEL_COMPARE_MIN_REFERENCE_L", 20)):
        return None, "Référence quasi nulle"
    return round(float(gap) / float(base) * 100, 1), None


def _gap_status(gap: Decimal, base: Decimal, thresholds: dict) -> str:
    """Seuils configurables (FuelRapprochementThreshold) : COHERENT / A_JUSTIFIER / A_INVESTIGUER."""
    from fuel_tracking.services.cph_engine import R_A_JUSTIFIER, R_OK, _thresholds_status

    st = _thresholds_status(gap, abs(base), thresholds)
    return COHERENT if st == R_OK else A_JUSTIFIER if st == R_A_JUSTIFIER else A_INVESTIGUER


def _action(b, statut_reel, statut_global, instr_ok, cph_ok) -> str:
    from fuel_tracking.services.stock_reports import action_for

    if statut_reel in (A_CONTROLER, DONNEES_INCOMPLETES, NON_CALCULABLE):
        return action_for(b) if b is not None else "Calculer le bilan de stock de la période (aucun bilan pour ce site)."
    parts = []
    if statut_reel == ESTIMATION:
        parts.append(action_for(b))
    if statut_global == A_INVESTIGUER:
        parts.append("Investiguer l'écart (stocks, mouvements, sonde, runtime GE) avant toute certification.")
    elif statut_global == A_JUSTIFIER:
        parts.append("Faire justifier l'écart par l'exploitation.")
    if not instr_ok:
        parts.append("Vérifier la sonde / la remontée Snowflake.")
    if not cph_ok:
        parts.append("Compléter runtime, puissance ou courbe (Contrôle CPH).")
    if statut_reel == CERTIFIABLE and statut_global == COHERENT:
        parts.append("Certification manuelle par le Fuel Manager.")
    return " ".join(dict.fromkeys(parts)) or "—"


def _global_status(statut_reel, ecart_i, ecart_t, instr_ok, cph_ok) -> str:
    if statut_reel in (A_CONTROLER, DONNEES_INCOMPLETES, NON_CALCULABLE):
        return G_INCOMPLETES
    gaps = [g for g in (ecart_i, ecart_t) if g is not None]
    if not gaps:
        return G_INSTR_ABSENTE if not instr_ok else G_CPH_NON_CALCULE
    worst = min(gaps, key=lambda s: _SEVERITY[s])
    if worst == COHERENT and statut_reel == ESTIMATION:
        return G_RESERVE
    return worst


def _row(cph: dict | None, b, thresholds: dict, days: int) -> dict:
    site_id = (cph or {}).get("site_id") or b.site_id
    statut_reel = reel_status(b)
    reel_l = _f(b.consommation_reelle_l) if (b is not None and statut_reel in (CERTIFIABLE, ESTIMATION)) else None  # jamais de valeur inventée

    # Instrumentée (capteur) : somme des jours mesurés ; exploitable si la couverture de la période atteint le seuil configurable.
    cmp_ = (cph or {}).get("comparaison") or {}
    instr_total, jours_mesure = cmp_.get("conso_mesuree_l"), cmp_.get("jours_mesure") or 0
    n_days = (cph or {}).get("days") or days
    instr_cov = round(jours_mesure / n_days * 100, 1) if n_days else None
    instr_ok = instr_total is not None and instr_cov is not None and instr_cov >= float(_cfg("FUEL_COMPARE_INSTR_MIN_COVERAGE_PCT", 80))
    # Théorique CPH : valeur uniquement si le CPH est calculé sur toute la période (jamais de fallback silencieux).
    statut_cph = (cph or {}).get("statut_cph") or (cph or {}).get("cph_status")
    cph_ok = (cph or {}).get("conso_theorique_l") is not None and (cph or {}).get("cph_status") == "COMPLET"
    cph_cov = round(((cph or {}).get("cph_days") or 0) / n_days * 100, 1) if (cph and n_days) else None

    e_i = e_t = None
    gi = gt = None
    if reel_l is not None:
        base = Decimal(str(reel_l))
        if instr_ok:
            gi = float(instr_total) - reel_l
            e_i = _gap_status(Decimal(str(gi)), base, thresholds)
        if cph_ok:
            gt = float(cph["conso_theorique_l"]) - reel_l
            e_t = _gap_status(Decimal(str(gt)), base, thresholds)
    pct_i, na_i = _pct(gi, reel_l)
    pct_t, na_t = _pct(gt, reel_l)
    statut_global = _global_status(statut_reel, e_i, e_t, instr_ok, cph_ok)

    motifs = []
    if reel_l is None:
        motifs.extend((b.motifs or [])[:2] if b is not None else ["Aucun bilan de stock calculé pour ce site sur la période."])
    else:
        if gi is not None:
            motifs.append(f"Écart vs instrumentée {gi:+.0f} L" + (f" ({pct_i:+.0f} %)" if pct_i is not None else f" ({na_i.lower()})"))
        if gt is not None:
            motifs.append(f"Écart vs théorique {gt:+.0f} L" + (f" ({pct_t:+.0f} %)" if pct_t is not None else f" ({na_t.lower()})"))
        if b is not None and b.reserves:
            motifs.append(b.reserves[0])
    if reel_l is not None and not instr_ok:
        motifs.append("Mesure instrumentée absente ou partielle" + (f" ({jours_mesure}/{n_days} j)" if instr_total is not None else "") + ".")
    if reel_l is not None and not cph_ok:
        mc = (cph or {}).get("motif_cph") or {}
        motifs.append("CPH non calculé" + (f" : {mc.get('code')}" if mc.get("code") else "") + ".")

    row = {
        "site_id": site_id, "site_name": (cph or {}).get("site_name") or b.site_name, "country": (cph or {}).get("country") or (b.country if b else None),
        "zone": (cph or {}).get("zone") or (b.zone if b else None), "typologie": (b.typologie if b else None) or (cph or {}).get("site_type"),
        "configuration": (cph or {}).get("kind") or (b.profil_energetique if b else None),
        "reel": {"conso_l": reel_l, "statut": statut_reel, "statut_bilan": b.statut_bilan if b else None, "cause": b.cause_principale if b else None,
                 "score": b.score_confiance if b else None, "source": _source_text(b), "reserves": (b.reserves if b else []) or [], "bilan_id": b.pk if b else None,
                 "alertes": alert_codes_of(b.alertes) if b else [], "certification": b.niveau_certification if b else None},
        "instrumentee": {"conso_l": _f(instr_total), "jours_mesure": jours_mesure, "jours": n_days, "couverture_pct": instr_cov, "disponible": instr_ok},
        "theorique": {"conso_l": _f(cph.get("conso_theorique_l")) if cph_ok else None, "partielle_l": _f((cph or {}).get("conso_partielle_l")),
                      "statut_cph": statut_cph, "cph_status": (cph or {}).get("cph_status"), "jours_cph": (cph or {}).get("cph_days"), "couverture_pct": cph_cov,
                      "disponible": cph_ok, "motif_code": ((cph or {}).get("motif_cph") or {}).get("code"),
                      "runtime_source": (cph or {}).get("runtime_source_main"), "power_source": (cph or {}).get("power_source_main")},
        "ecart_instrumentee": {"l": _f(gi, 1), "pct": pct_i, "pct_na": na_i, "statut": e_i},
        "ecart_theorique": {"l": _f(gt, 1), "pct": pct_t, "pct_na": na_t, "statut": e_t},
        "statut_global": statut_global, "motif": " · ".join(motifs) or None,
        "action": _action(b, statut_reel, statut_global, instr_ok, cph_ok),
        "hors_referentiel": bool((cph or {}).get("hors_referentiel")),
    }
    return row


def _filter(rows: list[dict], p) -> list[dict]:
    def eq(_unused, key, getter):
        nonlocal rows
        v = (p.get(key) or "").strip()
        if v:
            rows = [r for r in rows if getter(r) == v]

    pays = (p.get("pays") or "").strip().lower()
    if pays:
        rows = [r for r in rows if (r["country"] or "").lower() == pays]
    zone = (p.get("zone") or "").strip().lower()
    if zone:
        rows = [r for r in rows if (r["zone"] or "").lower() == zone]
    site = (p.get("site") or "").strip().lower()
    if site:
        rows = [r for r in rows if site in r["site_id"].lower() or site in (r["site_name"] or "").lower()]
    typ = (p.get("typologie") or "").strip().lower()
    if typ:
        rows = [r for r in rows if (r["typologie"] or "").lower() == typ]
    conf = (p.get("configuration") or "").strip().lower()
    if conf:
        rows = [r for r in rows if (r["configuration"] or "").lower() == conf]
    eq(None, "statut_global", lambda r: r["statut_global"])
    eq(None, "qualite_reel", lambda r: r["reel"]["statut"])
    eq(None, "source_runtime", lambda r: r["theorique"]["runtime_source"] or "AUCUNE")
    eq(None, "source_puissance", lambda r: r["theorique"]["power_source"] or "AUCUNE")
    eq(None, "statut_cph", lambda r: r["theorique"]["statut_cph"] or "CPH_NON_CALCULE")
    return rows


def _kpis(rows: list[dict]) -> dict:
    calc = [r for r in rows if r["reel"]["conso_l"] is not None]
    instr = [r for r in rows if r["instrumentee"]["disponible"]]
    cph = [r for r in rows if r["theorique"]["disponible"]]
    comp_i = [r for r in calc if r["ecart_instrumentee"]["l"] is not None]
    comp_t = [r for r in calc if r["ecart_theorique"]["l"] is not None]

    def gap(group, key):
        s_gap, s_ref = sum(r[key]["l"] for r in group), sum(r["reel"]["conso_l"] for r in group)
        return {"sites_comparables": len(group), "ecart_l": round(s_gap, 1) if group else None,
                "ecart_pct": round(s_gap / s_ref * 100, 1) if group and s_ref >= float(_cfg("FUEL_COMPARE_MIN_REFERENCE_L", 20)) else None,
                "reel_compare_l": round(s_ref, 1) if group else None}

    n = lambda statut: sum(1 for r in rows if r["reel"]["statut"] == statut)  # noqa: E731
    return {
        "reel": {"total_l": round(sum(r["reel"]["conso_l"] for r in calc), 1) if calc else None, "sites_calculables": len(calc),
                 "sites_certifiables": n(CERTIFIABLE), "sites_avec_reserve": n(ESTIMATION)},
        "instrumentee": {"total_l": round(sum(r["instrumentee"]["conso_l"] for r in instr), 1) if instr else None, "sites_couverts": len(instr),
                         "note": "Mesure capteur Snowflake : ce n'est pas la consommation réelle certifiée."},
        "theorique": {"total_l": round(sum(r["theorique"]["conso_l"] for r in cph), 1) if cph else None, "sites_complets": len(cph),
                      "sites_partiels": sum(1 for r in rows if (r["theorique"]["cph_status"] or "") == "PARTIEL"),
                      "note": "Estimation CPH (runtime × puissance × courbe)."},
        "ecart_instrumentee": gap(comp_i, "ecart_instrumentee"),
        "ecart_theorique": gap(comp_t, "ecart_theorique"),
        "a_traiter": {"sites_non_calculables": sum(1 for r in rows if r["reel"]["statut"] in (NON_CALCULABLE, DONNEES_INCOMPLETES, A_CONTROLER)),
                      "sites_a_controler": n(A_CONTROLER), "sites_donnees_incompletes": n(DONNEES_INCOMPLETES),
                      "conflits_enoc_closing": sum(1 for r in rows if "CONFLIT_ENOC_CLOSING" in r["reel"]["alertes"]),
                      "sites_avec_reserve": n(ESTIMATION)},
        "sites": len(rows),
    }


def build(d_from: date, d_to: date, params=None, with_cph_quality: bool = True) -> dict:
    """Calcule le tableau de comparaison unique pour la période EXACTE [d_from 00:00:00 ; d_to 23:59:59]."""
    from fuel_tracking.services.cph_service import _thresholds, compute_period_summary
    from fuel_tracking.views_cph import _couvertures

    params = params or {}
    start, end = day_bounds(d_from, d_to)
    days = (d_to - d_from).days + 1
    cph_rows = compute_period_summary(d_from, d_to, country=params.get("pays") or None, zone=params.get("zone") or None)
    cph_by_site = {r["site_id"]: r for r in cph_rows}
    balances = latest_balances_by_site(d_from, d_to)
    thresholds = _thresholds()
    rows = []
    for sid in sorted(set(balances) | {s for s, r in cph_by_site.items() if r.get("perimetre", "GE") == "GE"}):
        rows.append(_row(cph_by_site.get(sid), balances.get(sid), thresholds, days))
    rows = _filter(rows, params)
    ordre = (params.get("tri") or "site").strip()
    if ordre == "gravite":
        rows.sort(key=lambda r: (_SEVERITY[r["statut_global"]], r["site_id"]))
    elif ordre == "reel":
        rows.sort(key=lambda r: (r["reel"]["conso_l"] is None, -(r["reel"]["conso_l"] or 0), r["site_id"]))
    elif ordre == "ecart":
        rows.sort(key=lambda r: (-max(abs(r["ecart_instrumentee"]["l"] or 0), abs(r["ecart_theorique"]["l"] or 0)), r["site_id"]))
    else:
        rows.sort(key=lambda r: r["site_id"])
    return {
        "periode": {"debut": d_from.isoformat(), "fin": d_to.isoformat(), "debut_dt": start.isoformat(), "fin_dt": end.isoformat(), "jours": days,
                    "libelle": f"{d_from:%d/%m/%Y} 00:00:00 → {d_to:%d/%m/%Y} 23:59:59",
                    "bilan_disponible": bool(balances), "sites_avec_bilan": len(balances),
                    "sites_calculables": sum(1 for b in balances.values() if b.consommation_reelle_l is not None)},
        "kpis": _kpis(rows), "rows": rows, "labels": LABELS, "aide": HELP,
        "qualite_cph": _couvertures([r for r in cph_rows if r.get("perimetre", "GE") == "GE"]) if with_cph_quality else [],
        "facettes": {
            "pays": sorted({r["country"] for r in rows if r["country"]}), "zones": sorted({r["zone"] for r in rows if r["zone"]}),
            "typologies": sorted({r["typologie"] for r in rows if r["typologie"]}), "configurations": sorted({r["configuration"] for r in rows if r["configuration"]}),
            "statuts_globaux": list(GLOBAL_STATUTS), "qualites_reel": list(REEL_STATUTS),
            "sources_runtime": sorted({r["theorique"]["runtime_source"] or "AUCUNE" for r in rows}),
            "sources_puissance": sorted({r["theorique"]["power_source"] or "AUCUNE" for r in rows}),
            "statuts_cph": ["CPH_CALCULE", "CPH_PARTIEL", "CPH_NON_CALCULE"],
        },
        "seuils": {"couverture_instrumentee_min_pct": float(_cfg("FUEL_COMPARE_INSTR_MIN_COVERAGE_PCT", 80)),
                   "reference_minimale_l": float(_cfg("FUEL_COMPARE_MIN_REFERENCE_L", 20)),
                   "ecarts": {k: float(v) for k, v in thresholds.items()}},
    }


# ── Exports CSV ─────────────────────────────────────────────────────────────────────────────────────────────────────
EXPORTS = {
    "controle_complet": {"label": "Contrôle complet", "description": "Toutes les colonnes, sources, périodes, réserves, motifs et détails d'audit."},
    "anomalies_fuel": {"label": "Anomalies Fuel", "description": "Seulement les lignes dont le statut global n'est pas COHERENT."},
    "bilan_certification": {"label": "Bilan certification", "description": "Consommation réelle, statut, score, réserves et actions attendues."},
}


def _csv(columns: list[str], lines: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, delimiter=";", extrasaction="ignore")
    w.writeheader()
    for r in lines:
        w.writerow({k: ("" if v is None else v) for k, v in r.items()})
    return "﻿" + buf.getvalue()


def export_csv(kind: str, d_from: date, d_to: date, params) -> tuple[str, str, int]:
    data = build(d_from, d_to, params, with_cph_quality=False)
    rows = data["rows"]
    label = f"{d_from:%Y-%m-%d}_{d_to:%Y-%m-%d}"
    base_cols = ["Site ID", "Nom du site", "Pays", "Zone", "Typologie site", "Configuration énergétique", LABELS["reel"], "Qualité consommation réelle", "Source consommation réelle",
                 LABELS["instrumentee"], "Disponibilité instrumentée (%)", LABELS["theorique"], "Disponibilité CPH (%)", "Écart réel vs instrumentée (L)", "Écart réel vs instrumentée (%)",
                 "Écart réel vs théorique (L)", "Écart réel vs théorique (%)", "Statut global", "Motif", "Action attendue"]

    def base(r):
        return {"Site ID": r["site_id"], "Nom du site": r["site_name"], "Pays": r["country"], "Zone": r["zone"], "Typologie site": r["typologie"],
                "Configuration énergétique": r["configuration"], LABELS["reel"]: r["reel"]["conso_l"], "Qualité consommation réelle": r["reel"]["statut"],
                "Source consommation réelle": r["reel"]["source"], LABELS["instrumentee"]: r["instrumentee"]["conso_l"],
                "Disponibilité instrumentée (%)": r["instrumentee"]["couverture_pct"], LABELS["theorique"]: r["theorique"]["conso_l"],
                "Disponibilité CPH (%)": r["theorique"]["couverture_pct"], "Écart réel vs instrumentée (L)": r["ecart_instrumentee"]["l"],
                "Écart réel vs instrumentée (%)": r["ecart_instrumentee"]["pct"] if r["ecart_instrumentee"]["pct"] is not None else r["ecart_instrumentee"]["pct_na"],
                "Écart réel vs théorique (L)": r["ecart_theorique"]["l"],
                "Écart réel vs théorique (%)": r["ecart_theorique"]["pct"] if r["ecart_theorique"]["pct"] is not None else r["ecart_theorique"]["pct_na"],
                "Statut global": r["statut_global"], "Motif": r["motif"], "Action attendue": r["action"]}

    if kind == "bilan_certification":
        cols = ["Site ID", "Nom du site", "Période", LABELS["reel"], "Qualité consommation réelle", "Statut du bilan", "Cause principale", "Score de confiance", "Réserves",
                "Alertes", "Niveau de certification", "Action attendue"]
        from fuel_tracking.services.period_utils import latest_balances_by_site

        bal = latest_balances_by_site(d_from, d_to)
        lines = []
        for r in rows:
            b = bal.get(r["site_id"])
            if b is None:
                continue
            lines.append({"Site ID": r["site_id"], "Nom du site": r["site_name"], "Période": data["periode"]["libelle"], LABELS["reel"]: r["reel"]["conso_l"],
                          "Qualité consommation réelle": r["reel"]["statut"], "Statut du bilan": b.statut_bilan, "Cause principale": b.cause_principale,
                          "Score de confiance": b.score_confiance, "Réserves": " | ".join(b.reserves or []), "Alertes": " | ".join(b.alertes or []),
                          "Niveau de certification": b.niveau_certification, "Action attendue": r["action"]})
        return f"bilan_certification_{label}.csv", _csv(cols, lines), len(lines)
    if kind == "anomalies_fuel":
        lines = [base(r) for r in rows if r["statut_global"] != COHERENT]
        return f"anomalies_fuel_{label}.csv", _csv(base_cols, lines), len(lines)
    if kind == "controle_complet":
        from fuel_tracking.services.period_utils import latest_balances_by_site

        bal = latest_balances_by_site(d_from, d_to)
        extra = ["Période", "Source runtime", "Source puissance", "Statut CPH", "CPH partiel (L)", "Jours CPH", "Jours mesurés Snowflake", "Jours de la période",
                 "Stock initial (L)", "Source stock initial", "Date stock initial", "Stock final (L)", "Source stock final", "Date stock final", "Livraison externe (L)",
                 "Carte Total (L)", "Transfert entrant (L)", "Rajouts (L)", "Transfert sortant (L)", "Retraits (L)", "Vols (L)", "Vidanges (L)", "Statut du bilan",
                 "Cause principale", "Score de confiance", "Réserves", "Alertes", "Niveau de certification", "Bilan calculé le", "Version des règles"]
        lines = []
        for r in rows:
            b = bal.get(r["site_id"])
            d = base(r)
            d.update({"Période": data["periode"]["libelle"], "Source runtime": r["theorique"]["runtime_source"], "Source puissance": r["theorique"]["power_source"],
                      "Statut CPH": r["theorique"]["statut_cph"], "CPH partiel (L)": r["theorique"]["partielle_l"], "Jours CPH": r["theorique"]["jours_cph"],
                      "Jours mesurés Snowflake": r["instrumentee"]["jours_mesure"], "Jours de la période": r["instrumentee"]["jours"]})
            if b is not None:
                z = lambda v: None if v is None else float(v)  # noqa: E731
                d.update({"Stock initial (L)": z(b.stock_initial_l), "Source stock initial": b.source_stock_initial, "Date stock initial": b.date_stock_initial,
                          "Stock final (L)": z(b.stock_final_l), "Source stock final": b.source_stock_final, "Date stock final": b.date_stock_final,
                          "Livraison externe (L)": z(b.livraisons_externes_l), "Carte Total (L)": z(b.carte_total_l), "Transfert entrant (L)": z(b.transferts_entrants_l),
                          "Rajouts (L)": z(b.rajouts_l), "Transfert sortant (L)": z(b.transferts_sortants_l), "Retraits (L)": z(b.retraits_l), "Vols (L)": z(b.vols_l),
                          "Vidanges (L)": z(b.vidanges_l), "Statut du bilan": b.statut_bilan, "Cause principale": b.cause_principale, "Score de confiance": b.score_confiance,
                          "Réserves": " | ".join(b.reserves or []), "Alertes": " | ".join(b.alertes or []), "Niveau de certification": b.niveau_certification,
                          "Bilan calculé le": b.calculated_at, "Version des règles": b.rule_version})
            lines.append(d)
        return f"controle_complet_{label}.csv", _csv(base_cols + extra, lines), len(lines)
    raise ValueError(f"Export inconnu : {kind}")
