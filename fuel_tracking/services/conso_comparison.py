# fuel_tracking/services/conso_comparison.py
"""
« Suivis Consommations » : UN tableau, UNE période exacte (jours entiers 00:00:00 → 23:59:59), UNE ligne par site. CINQ mesures distinctes, jamais confondues :

  1. Variation de stock (L)                    bilan mathématique des stocks et mouvements (jamais appelée « consommation réelle » automatiquement)
  2. Consommation Closing (L)                  consommation déclarée historiquement dans le Closing (contrôle)
  3. Consommation instrumentée Snowflake (L)   mesure capteur, indépendante du stock
  4. Consommation théorique CPH (L)            complète / partielle (runtime GE × CPH de la courbe)
  5. Consommation réelle certifiée (L)         variation de stock validée EXPLICITEMENT par le Fuel Manager (vide tant que non certifiée)

Écarts (périodes strictement identiques) : écart = autre mesure − variation de stock sauf mention contraire ; variation − Closing ; variation − target.
Une donnée absente reste NULL (jamais 0). Aucune valeur n'est inventée ni substituée.
"""
from __future__ import annotations

import csv
import io
from datetime import date

from django.conf import settings

from fuel_tracking.services import fuel_target as TG
from fuel_tracking.services import gap_explanation as GX
from fuel_tracking.services.period_utils import day_bounds, latest_balances_by_site
from fuel_tracking.services.stock_period_balance import RULE_VERSION, alert_codes_of

# ── Libellés et aides (définition · formule · unité · période · source · limites) ───────────────────────────────────────────────────
LABELS = {
    "variation": "Variation de stock (L)",
    "closing": "Consommation Closing (L)",
    "instrumentee": "Consommation Snowflake (L)",
    "cph_complete": "Consommation théorique CPH complète (L)",
    "cph_partielle": "Consommation théorique CPH partielle (L)",
    "certifiee": "Consommation réelle certifiée (L)",
    "target": "Target fuel période (L)",
}
HELP = {
    "variation": "Définition : résultat mathématique du bilan de stock. Formule : stock initial + livraisons + transferts entrants + rajouts − transferts sortants − retraits − vols − "
                 "vidanges − stock final. Unité : L. Période : exacte sélectionnée. Source : relevés de stock (Closing, gardiennage, terrain) et mouvements ENOC. "
                 "Limite : n'est PAS une consommation réelle tant qu'elle n'est pas certifiée. Négative = anomalie.",
    "closing": "Consommation déclarée dans le fichier Closing (référence de contrôle historique). Elle ne remplace ni la variation de stock ni la certification.",
    "instrumentee": "Mesure déduite des capteurs de niveau Snowflake sur les jours exploitables. Indépendante du stock, jamais utilisée pour le remplacer ; "
                    "comparée à la variation seulement sur une période strictement identique.",
    "cph_complete": "Σ jours (runtime GE × CPH L/h de la courbe), uniquement si TOUS les jours de la période sont calculés (runtime qualifié, puissance exploitable, "
                    "puissance nominale connue, courbe valide).",
    "cph_partielle": "Somme des seuls jours calculés. Elle ne couvre pas toute la période : à ne jamais lire comme une consommation théorique totale.",
    "certifiee": "Variation de stock validée explicitement par le Fuel Manager (action « Certifier le bilan »). Vide tant que le statut n'est pas CONSOMMATION_REELLE_CERTIFIEE.",
    "target": "Target fuel mensuel du site (L/mois), proratisé au nombre de jours sélectionnés, mois par mois. Valeur historique du Closing : à confirmer pour l'année de la période. "
              "Jamais déduit d'une puissance, d'un modèle de GE, d'une courbe CPH, d'un target load ou énergie.",
    "ecart_closing": "Variation de stock − Consommation Closing (L). % = écart / Closing, seulement si les deux existent et que le Closing est non nul.",
    "ecart_target": "Variation de stock − Target fuel période (L). % = écart / target, seulement si les deux existent et que le target est non nul.",
    "ecart_instrumentee": "Variation de stock − Consommation instrumentée (L), sur période strictement identique.",
    "ecart_cph": "Variation de stock − CPH complète (L). Jamais calculé avec la CPH partielle.",
}

# Statuts du bilan
NON_CALCULABLE, VARIATION_CALCULEE, ESTIMATION, PRET, CERTIFIEE = (
    "NON_CALCULABLE", "VARIATION_CALCULEE", "ESTIMATION_AVEC_RESERVE", "PRET_A_CERTIFIER", "CONSOMMATION_REELLE_CERTIFIEE")
STATUTS_BILAN = (NON_CALCULABLE, VARIATION_CALCULEE, ESTIMATION, PRET, CERTIFIEE)
_BLOCKING_CAUSES = {"DONNEES_STOCK_INCOMPLETES", "CM_SANS_CUVE", "STOCK_TROP_ANCIEN", "CONFLIT_ENOC_CLOSING", "MOUVEMENTS_A_CONTROLER", "TRANSFERT_DESEQUILIBRE",
                    "RELEVES_INCOHERENTS", "SITE_NON_RECONNU", "SCORE_INSUFFISANT"}
_CERTIFIED = ("CERTIFIE", "CERTIFIE_AVEC_RESERVE")


def _f(v, places=3):
    return None if v is None else round(float(v), places)


def _cfg(name, default):
    return getattr(settings, name, default)


def bilan_status(b) -> str:
    if b is None:
        return NON_CALCULABLE
    if b.niveau_certification in _CERTIFIED and b.statut_bilan != "NON_CALCULABLE":
        return CERTIFIEE
    if b.cause_principale == "CONSO_NEGATIVE" and b.variation_stock_l is not None:
        return VARIATION_CALCULEE                                    # visible mais anomalie : jamais certifiable
    if b.statut_bilan == "NON_CALCULABLE" or b.cause_principale in _BLOCKING_CAUSES:
        return NON_CALCULABLE
    if b.statut_bilan == "PRET_A_CERTIFIER":
        return PRET
    if b.statut_bilan == "ESTIMATION_AVEC_RESERVE":
        return ESTIMATION
    return VARIATION_CALCULEE


def variation_of(b, statut: str) -> float | None:
    """Variation de stock affichée : valeur du bilan (négative conservée), jamais pour un bilan non calculable."""
    if b is None or statut == NON_CALCULABLE:
        return None
    v = b.variation_stock_l if b.variation_stock_l is not None else b.consommation_reelle_l      # anciens bilans (avant recalcul)
    return _f(v)


def _pct(gap, base, min_ref=0.0) -> tuple[float | None, str | None]:
    if gap is None:
        return None, None
    if base is None or base == 0 or abs(float(base)) < min_ref:
        return None, "Référence nulle ou quasi nulle"
    return round(float(gap) / float(base) * 100, 1), None


def _extreme(a, b) -> bool:
    """Écart extrême entre deux mesures censées être du même ordre de grandeur (signalé « à investiguer », jamais corrigé ni remplacé)."""
    if a is None or b is None:
        return False
    big, small = max(abs(a), abs(b)), min(abs(a), abs(b))
    min_l = float(_cfg("FUEL_COMPARE_EXTREME_MIN_L", 100))
    ratio = float(_cfg("FUEL_COMPARE_EXTREME_RATIO", 5))
    return (big - small) >= min_l and (small == 0 or big / small >= ratio)


def _gap(a, b):
    return None if a is None or b is None else round(a - b, 1)


def _closing_of(b) -> dict | None:
    """
    Retourne un dict {"conso_closing_l": <valeur>} pour le bilan b.

    Priorité :
    1. Champ first-class b.conso_closing_l (migration 0057+) — source la plus fiable.
    2. Fallback legacy : lecture dans stock_initial_detail / stock_final_detail JSON
       (bilans calculés avant la migration 0057, ou recalcul en attente).
    """
    # 1. Champ first-class (préféré)
    if b is not None and getattr(b, "conso_closing_l", None) is not None:
        return {"conso_closing_l": b.conso_closing_l}

    # 2. Fallback legacy JSON
    for d in ((b.stock_initial_detail or {}), (b.stock_final_detail or {})):
        ref = d.get("reference_closing")
        if ref and ref.get("conso_closing_l") is not None:
            return ref
    for d in ((b.stock_initial_detail or {}), (b.stock_final_detail or {})):
        ctx = d.get("source_context") or {}
        if ctx.get("conso_closing_l") is not None:
            return {"conso_closing_l": ctx["conso_closing_l"]}
    return None


def _source_text(src) -> str | None:
    lab = {"CLOSING": "Closing", "LOLIFORCE": "Loliforce", "SCSS": "SCSS", "ENOC": "ENOC", "HISTORICAL_REFUELING": "Historique refueling"}
    return lab.get(src, src)


def _certif(b) -> dict:
    out = {"valeur_l": None, "par": None, "date": None, "commentaire": None, "niveau": b.niveau_certification if b else None}
    if b is None or b.niveau_certification not in _CERTIFIED:
        return out
    c = b.certifications.order_by("-created_at").first()
    out.update(valeur_l=_f(b.variation_stock_l if b.variation_stock_l is not None else b.consommation_reelle_l), par=getattr(c, "user_label", None) or None,
               date=c.created_at.isoformat() if c else None, commentaire=getattr(c, "comment", None) or None)
    return out


def _energy_typology(b, cph) -> str | None:
    parts = [(b.profil_energetique if b is not None else None), (cph or {}).get("kind")]
    return " · ".join(dict.fromkeys(str(p) for p in parts if p)) or None


def _action(b, statut) -> str:
    from fuel_tracking.services.stock_reports import action_for

    if b is None:
        return "Calculer le bilan de stock de la période (aucun bilan pour ce site)."
    if statut == CERTIFIEE:
        return "Aucune : bilan certifié."
    if statut == PRET:
        return "Certification par le Fuel Manager."
    return action_for(b)


def _row(cph: dict | None, b, d_from: date, d_to: date, days: int, target, thresholds_used: dict) -> dict:
    site_id = (cph or {}).get("site_id") or b.site_id
    statut = bilan_status(b)
    var = variation_of(b, statut)
    certif = _certif(b) if b is not None else {"valeur_l": None, "par": None, "date": None, "commentaire": None, "niveau": None}
    if statut == CERTIFIEE:
        certif["valeur_l"] = var
    else:
        certif["valeur_l"] = None                                     # vide tant que non certifiée

    # Closing
    ref = _closing_of(b) if b is not None else None
    conso_closing = _f(ref.get("conso_closing_l")) if ref else None
    ecart_cl = _gap(var, conso_closing)
    pct_cl, na_cl = _pct(ecart_cl, conso_closing)

    # Target
    tgt_l = TG.prorated_target(target.target_mensuel_l, d_from, d_to) if target is not None else None
    tgt_f = _f(tgt_l)
    if target is None:
        statut_target = TG.INDISPONIBLE
    elif not TG.confirmed_for(target, d_from, d_to):
        statut_target = TG.A_CONFIRMER
    else:
        statut_target = TG.classify(var, tgt_f) or TG.A_CONFIRMER
    position = TG.classify(var, tgt_f)
    ecart_tg = _gap(var, tgt_f)
    pct_tg, na_tg = _pct(ecart_tg, tgt_f)

    # Instrumentée : couverture = jours mesurés / jours de la période
    cmp_ = (cph or {}).get("comparaison") or {}
    instr_total, jours_mesure = cmp_.get("conso_mesuree_l"), cmp_.get("jours_mesure") or 0
    n_days = (cph or {}).get("days") or days
    instr_cov = round(jours_mesure / n_days * 100, 1) if n_days else None
    instr_ok = instr_total is not None and instr_cov is not None and instr_cov >= float(_cfg("FUEL_COMPARE_INSTR_MIN_COVERAGE_PCT", 80))
    instr_l = _f(instr_total)
    strict_instr = instr_ok and jours_mesure == n_days                  # comparaison seulement sur période strictement identique
    ecart_in = _gap(var, instr_l) if strict_instr else None
    pct_in, na_in = _pct(ecart_in, instr_l, float(_cfg("FUEL_COMPARE_MIN_REFERENCE_L", 20)))

    # CPH : complète séparée de partielle
    cph_status = (cph or {}).get("cph_status")
    complete = _f((cph or {}).get("conso_theorique_l")) if cph_status == "COMPLET" else None
    partielle = _f((cph or {}).get("conso_partielle_l")) if cph_status == "PARTIEL" else None
    cph_cov = round(((cph or {}).get("cph_days") or 0) / n_days * 100, 1) if (cph and n_days) else None
    ecart_cp = _gap(var, complete)
    pct_cp, na_cp = _pct(ecart_cp, complete, float(_cfg("FUEL_COMPARE_MIN_REFERENCE_L", 20)))

    motifs = []
    if b is None:
        motifs.append("Aucun bilan de stock calculé pour ce site sur la période exacte.")
    else:
        motifs.extend((b.motifs or [])[:3])
        if statut != NON_CALCULABLE and b.reserves:
            motifs.append(b.reserves[0])
    if statut == VARIATION_CALCULEE and var is not None and var < 0:
        motifs.insert(0, f"Variation de stock négative ({var:.0f} L) : anomalie à contrôler.")

    alerts = sorted(alert_codes_of(b.alertes)) if b is not None else []
    cause = b.cause_principale if b is not None else "AUCUN_BILAN"

    # Stock identique suspect : stock initial = stock final > 0 sans aucun mouvement → stock probablement non mis à jour (jamais présenté comme « 0 L consommé » fiable)
    if b is not None and statut != NON_CALCULABLE and var is not None and abs(var) < 0.5 and b.stock_initial_l is not None and b.stock_final_l is not None \
            and b.stock_initial_l == b.stock_final_l and b.stock_initial_l > 0 \
            and not any((x or 0) for x in (b.livraisons_externes_l, b.carte_total_l, b.transferts_entrants_l, b.transferts_sortants_l, b.rajouts_l, b.retraits_l, b.vols_l, b.vidanges_l)):
        alerts = alerts + ["STOCK_IDENTIQUE_SUSPECT"]
        motifs.insert(0, "Stock initial = stock final sans aucun mouvement : stock probablement non mis à jour (variation de 0 L à confirmer sur le terrain).")

    # Alertes de comparaison (valeurs conservées, signalées à investiguer)
    cmp_alerts = []
    if var is not None and instr_ok and _extreme(var, instr_l):
        cmp_alerts.append("INSTRUMENTEE_ECART_EXTREME")
    if var is not None and complete is not None and _extreme(var, complete):
        cmp_alerts.append("CPH_ECART_EXTREME")
    if var is not None and complete == 0 and var > float(_cfg("FUEL_COMPARE_EXTREME_MIN_L", 100)):
        cmp_alerts.append("CPH_NULLE_VARIATION_POSITIVE")
    cmp_alerts = list(dict.fromkeys(cmp_alerts))

    # Indépendance du contrôle Closing : si les deux stocks ET les mouvements viennent du Closing, l'écart nul est mécanique (pas une validation indépendante)
    closing_indep = None
    if ref is not None and b is not None:
        closing_indep = not (b.source_stock_initial == "CLOSING" and b.source_stock_final == "CLOSING" and (ref.get("decision") in (None, "COMPLEMENT_CLOSING", "CONFLIT", "CLOSING_FAIT_FOI")))
    explication = GX.explain(b, var, conso_closing, ref) if b is not None else GX.explain(None, None, None, None)
    row = {
        "site_id": site_id, "site_name": (cph or {}).get("site_name") or (b.site_name if b else None), "country": (cph or {}).get("country") or (b.country if b else None),
        "zone": (cph or {}).get("zone") or (b.zone if b else None), "typologie": (b.typologie if b else None) or (cph or {}).get("site_type"),
        "typologie_energie": _energy_typology(b, cph), "modele_ge": (cph or {}).get("ge_label"), "puissance_nominale_kva": _f((cph or {}).get("ge_kva")),
        "source_puissance": (cph or {}).get("power_source_main"),
        "periode": {"debut": d_from.isoformat(), "fin": d_to.isoformat(), "jours": days},
        "stock": {
            "initial_l": _f(b.stock_initial_l) if b else None, "source_initial": _source_text(b.source_stock_initial) if b else None,
            "entrees_l": None if b is None else _f(sum((x or 0) for x in (b.livraisons_externes_l, b.carte_total_l, b.transferts_entrants_l, b.rajouts_l))),
            "carte_total_l": _f(b.carte_total_l) if b else None, "truck_l": _f(b.truck_l) if b else None, "livraisons_externes_l": _f(b.livraisons_externes_l) if b else None,
            "ponctions_entrantes_l": _f(b.transferts_entrants_l) if b else None, "ponctions_sortantes_l": _f(b.transferts_sortants_l) if b else None,
            "rajouts_l": _f(b.rajouts_l) if b else None, "retraits_l": _f(b.retraits_l) if b else None, "vols_l": _f(b.vols_l) if b else None, "vidanges_l": _f(b.vidanges_l) if b else None,
            "final_l": _f(b.stock_final_l) if b else None, "source_final": _source_text(b.source_stock_final) if b else None,
            "variation_l": var, "statut": statut, "statut_moteur": b.statut_bilan if b else None, "cause": cause,
            "score": b.score_confiance if b else None, "reserves": (b.reserves or []) if b else [], "alertes": alerts, "alertes_comparaison": cmp_alerts, "bilan_id": b.pk if b else None,
            "motif": " · ".join(motifs) or None, "action": _action(b, statut),
        },
        "closing": {"conso_l": conso_closing, "ecart_l": ecart_cl, "ecart_pct": pct_cl, "pct_na": na_cl, "controle_independant": closing_indep, "explication": explication},
        "target": {"periode_l": tgt_f, "mensuel_l": _f(target.target_mensuel_l) if target else None, "source": target.source_target if target else None,
                   "statut": statut_target, "position": position, "annee_reference": target.reference_year if target else None,
                   "ecart_l": ecart_tg, "ecart_pct": pct_tg, "pct_na": na_tg},
        "instrumentee": {"conso_l": instr_l if instr_ok else None, "jours_mesure": jours_mesure, "jours": n_days, "couverture_pct": instr_cov, "disponible": instr_ok,
                         "ecart_l": ecart_in, "ecart_pct": pct_in, "pct_na": na_in},
        "cph": {"complete_l": complete, "partielle_l": partielle, "statut": cph_status, "jours_cph": (cph or {}).get("cph_days"), "couverture_pct": cph_cov,
                "ecart_l": ecart_cp, "ecart_pct": pct_cp, "pct_na": na_cp, "motif_code": ((cph or {}).get("motif_cph") or {}).get("code"),
                "runtime_h": _f((cph or {}).get("runtime_total_h"), 1), "runtime_source": (cph or {}).get("runtime_source_main"),
                "runtime_disponibilite": _f((cph or {}).get("runtime_source_availability"), 2), "power_source": (cph or {}).get("power_source_main"),
                "charge_pct": _f((cph or {})["charge_moy"] * 100 if (cph or {}).get("charge_moy") is not None else None, 1), "cph_l_h": _f((cph or {}).get("cph_moy_l_h"), 2),
                "niveau_mediane": None, "nb_mesures": None},          # renseignés avec la cascade de médianes (étape CPH)
        "certification": {"valeur_l": certif["valeur_l"], "par": certif["par"], "date": certif["date"], "commentaire": certif["commentaire"], "niveau": certif["niveau"]},
        "hors_referentiel": bool((cph or {}).get("hors_referentiel")),
    }
    return row


def _filter(rows: list[dict], p) -> list[dict]:
    def eq(value: str, getter):
        nonlocal rows
        v = (value or "").strip().lower()
        if v:
            rows = [r for r in rows if (str(getter(r) or "")).lower() == v]

    if (p.get("perimetre") or "").strip().lower() == "senegal":
        from fuel_tracking.views_bi import in_scope  # même règle que les vues BI (pays vide inclus)
        rows = [r for r in rows if in_scope(r["country"])]
    eq(p.get("pays"), lambda r: r["country"])
    eq(p.get("zone"), lambda r: r["zone"])
    eq(p.get("typologie"), lambda r: r["typologie"])
    eq(p.get("typologie_energie"), lambda r: r["typologie_energie"])
    eq(p.get("statut_bilan"), lambda r: r["stock"]["statut"])
    eq(p.get("source_runtime"), lambda r: r["cph"]["runtime_source"] or "AUCUNE")
    eq(p.get("source_puissance"), lambda r: r["cph"]["power_source"] or "AUCUNE")
    eq(p.get("source_target"), lambda r: r["target"]["source"] or "AUCUNE")
    typology = (p.get("typology") or "").strip()
    if typology and rows:
        # Même vocabulaire que les graphiques de Suivi Conso (typologie du fichier, sinon Snowflake).
        from fuel_tracking.views_bi import site_ids_for_typology
        per = rows[0].get("periode") or {}
        ids = site_ids_for_typology(typology, str(per.get("debut") or "")[:7], str(per.get("fin") or "")[:7])
        rows = [r for r in rows if (r["site_id"] or "").upper() in ids]
    site = (p.get("site") or "").strip().lower()
    if site:
        rows = [r for r in rows if site in r["site_id"].lower() or site in (r["site_name"] or "").lower()]
    corr = (p.get("correspondance") or "").strip().lower()
    if corr == "avec":
        rows = [r for r in rows if r["correspondance"]["utilise"]]
    elif corr == "sans":
        rows = [r for r in rows if not r["correspondance"]["utilise"]]
    cert = (p.get("statut_certification") or "").strip().lower()
    if cert == "certifie":
        rows = [r for r in rows if r["stock"]["statut"] == CERTIFIEE]
    elif cert == "non_certifie":
        rows = [r for r in rows if r["stock"]["statut"] != CERTIFIEE]
    src = (p.get("source_stock") or "").strip().lower()
    if src:
        rows = [r for r in rows if src in {(r["stock"]["source_initial"] or "").lower(), (r["stock"]["source_final"] or "").lower()}]
    q = (p.get("qualite") or "").strip().lower()
    if q == "reserve":
        rows = [r for r in rows if r["stock"]["reserves"]]
    elif q == "sans_reserve":
        rows = [r for r in rows if not r["stock"]["reserves"] and r["stock"]["variation_l"] is not None]
    elif q == "anomalie":
        rows = [r for r in rows if r["stock"]["statut"] in (NON_CALCULABLE, VARIATION_CALCULEE) or r["stock"]["alertes_comparaison"] or "STOCK_IDENTIQUE_SUSPECT" in r["stock"]["alertes"]]
    return rows


def _kpis(rows: list[dict]) -> dict:
    n = len(rows)
    calc = [r for r in rows if r["stock"]["variation_l"] is not None]
    cert = [r for r in rows if r["stock"]["statut"] == CERTIFIEE]
    clo = [r for r in rows if r["closing"]["conso_l"] is not None]
    ins = [r for r in rows if r["instrumentee"]["disponible"]]
    cpc = [r for r in rows if r["cph"]["complete_l"] is not None]
    cpp = [r for r in rows if r["cph"]["partielle_l"] is not None]
    with_t = [r for r in rows if r["target"]["periode_l"] is not None]
    comp = [r for r in with_t if r["stock"]["variation_l"] is not None]
    s = lambda grp, f: round(sum(f(r) for r in grp), 1) if grp else None  # noqa: E731
    sum_var, sum_tgt = sum(r["stock"]["variation_l"] for r in comp), sum(r["target"]["periode_l"] for r in comp)
    cnt = lambda st: sum(1 for r in rows if r["stock"]["statut"] == st)  # noqa: E731
    return {
        "sites": n,
        "variation": {"total_l": s(calc, lambda r: r["stock"]["variation_l"]), "sites": len(calc), "negatives": sum(1 for r in calc if r["stock"]["variation_l"] < 0)},
        "certifiee": {"total_l": s(cert, lambda r: r["certification"]["valeur_l"] or 0), "sites": len(cert)},
        "closing": {"total_l": s(clo, lambda r: r["closing"]["conso_l"]), "sites": len(clo)},
        "instrumentee": {"total_l": s(ins, lambda r: r["instrumentee"]["conso_l"]), "sites": len(ins)},
        "cph_complete": {"total_l": s(cpc, lambda r: r["cph"]["complete_l"]), "sites": len(cpc)},
        "cph_partielle": {"total_l": s(cpp, lambda r: r["cph"]["partielle_l"]), "sites": len(cpp)},
        "target": {
            "total_periode_l": s(with_t, lambda r: r["target"]["periode_l"]), "sites_avec_target": len(with_t), "sites_sans_target": n - len(with_t),
            "target_moyen_site_l": round(sum_tgt / len(comp), 1) if comp else None, "variation_moyenne_site_l": round(sum_var / len(comp), 1) if comp else None,
            "sites_comparables": len(comp),
            # Écart global = (Σ variations comparables − Σ targets comparables) / Σ targets comparables : jamais une moyenne de pourcentages
            "ecart_global_l": round(sum_var - sum_tgt, 1) if comp else None,
            "ecart_global_pct": round((sum_var - sum_tgt) / sum_tgt * 100, 1) if comp and sum_tgt else None,
            "couverture_pct": round(len(with_t) / n * 100, 1) if n else None,
            "a_confirmer": sum(1 for r in with_t if r["target"]["statut"] == TG.A_CONFIRMER),
        },
        "ponctions": {
            "entrantes_l": s(rows, lambda r: r["stock"]["ponctions_entrantes_l"] or 0), "sortantes_l": s(rows, lambda r: r["stock"]["ponctions_sortantes_l"] or 0),
            "ecart_perimetre_l": s(rows, lambda r: (r["stock"]["ponctions_entrantes_l"] or 0) - (r["stock"]["ponctions_sortantes_l"] or 0)),
            "note": "Un écart non nul ici signifie que le site en face d'une Ponction n'a pas de bilan sur la période affichée : l'impact global ENOC reste de 0 L.",
        },
        "alertes": {
            "stock_identique_suspect": sum(1 for r in rows if "STOCK_IDENTIQUE_SUSPECT" in r["stock"]["alertes"]),
            "instrumentee_ecart_extreme": sum(1 for r in rows if "INSTRUMENTEE_ECART_EXTREME" in r["stock"]["alertes_comparaison"]),
            "cph_ecart_extreme": sum(1 for r in rows if "CPH_ECART_EXTREME" in r["stock"]["alertes_comparaison"]),
            "cph_nulle_variation_positive": sum(1 for r in rows if "CPH_NULLE_VARIATION_POSITIVE" in r["stock"]["alertes_comparaison"]),
            "closing_non_independant": sum(1 for r in rows if r["closing"]["controle_independant"] is False),
        },
        "bilans": {"prets": cnt(PRET), "avec_reserve": cnt(ESTIMATION), "non_calculables": cnt(NON_CALCULABLE), "certifies": cnt(CERTIFIEE), "variation_calculee": cnt(VARIATION_CALCULEE)},
        "couverture_cph_pct": round(len(cpc) / n * 100, 1) if n else None,
    }


def closing_base_sites() -> set[str]:
    """Base de référence = sites présents dans le fichier Closing (mêmes sites que le fichier). Vide si aucun Closing n'a été importé."""
    from fuel_tracking.models import FuelStockObservation as Obs

    return {s.upper() for s in Obs.objects.filter(source_type="CLOSING").values_list("site_id", flat=True).distinct() if s}


def _months_between(d_from: date, d_to: date) -> list[str]:
    out, y, m = [], d_from.year, d_from.month
    while (y, m) <= (d_to.year, d_to.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def closing_file_lines(d_from: date, d_to: date) -> list[dict] | None:
    """
    Lignes du Closing, EXACTEMENT comme dans le fichier : TOUTES les lignes du fichier du mois comptable demandé, quelle que soit la date de relevé
    propre à chaque site (un site relevé début octobre appartient au Closing de septembre). Source : FuelClosingLine (dernier import du mois) ;
    repli sur l'ancienne copie du lot pour les Closing importés avant cette version. None si aucun Closing ne porte ces mois.
    """
    from fuel_tracking.services import closing_lines as CL

    months = _months_between(d_from, d_to)
    out, found = [], False
    missing = []
    for m in months:
        rows = CL.lines_for_month(m)
        if not rows:
            missing.append(m)
            continue
        found = True
        out.extend({"site": x.site_raw, "site_id": x.site_id, "debut": x.date_initiale.isoformat() if x.date_initiale else None,
                    "fin": x.date_finale.isoformat() if x.date_finale else None, "conso_l": None if x.conso_closing_l is None else float(x.conso_closing_l),
                    "mois": m, "ligne": x.ligne, "region": x.region, "id_en_double": x.id_en_double} for x in rows)
    import calendar as _cal

    for m in missing:                     # mois sans lignes conservées : repli sur l'ancienne copie du lot, mois par mois
        y, mo = int(m[:4]), int(m[5:7])
        legacy = _closing_lines_legacy(date(y, mo, 1), date(y, mo, _cal.monthrange(y, mo)[1]))
        if legacy is not None:            # [] = des Closing conservés existent mais aucun ne porte ce mois (≠ None : aucun Closing conservé)
            found = True
            out.extend(legacy)
    return out if found else None


def _closing_lines_legacy(d_from: date, d_to: date) -> list[dict] | None:
    """
    Lignes du fichier Closing, EXACTEMENT comme dans le fichier (colonne « qtité cons considéréé (l) », tous les sites,
    même non reconnus dans Gestion des sites), pour les lignes dont la période Closing se termine entre d_from et d_to.
    Une ligne sans date finale prend le mois le plus fréquent de son fichier. Plusieurs versions du Closing pour un même
    mois : la dernière importée remplace les précédentes. None si aucun Closing importé ne porte ces lignes
    (fichiers importés avant cette version : à relancer).
    """
    import calendar as _cal
    from collections import Counter

    from fuel_tracking.models import FuelClosingLine, FuelImportBatch as B

    # Un lot dont les lignes sont conservées (FuelClosingLine) est lu UNIQUEMENT par son mois comptable : sa copie ancienne (répartie par date de
    # relevé de chaque site) ne doit plus rattacher ses lignes d'octobre à octobre — c'est le même fichier, le même total.
    migres = set(FuelClosingLine.objects.values_list("batch_id", flat=True).distinct())   # lots déjà lus par leurs lignes conservées
    lo = d_from.replace(day=1).isoformat()
    hi = d_to.replace(day=_cal.monthrange(d_to.year, d_to.month)[1]).isoformat()
    by_month: dict[str, list[dict]] = {}
    found = False
    for pk, summary in (B.objects.filter(source_type="CLOSING", status__in=(B.Status.ACCEPTED, B.Status.VALIDATION_REQUIRED))
                        .order_by("received_at", "pk").values_list("pk", "summary")):
        if pk in migres:
            continue
        lines = (summary or {}).get("lignes_closing")
        if not lines:
            continue
        found = True
        months = Counter(str(x["fin"])[:7] for x in lines if x.get("fin"))
        dominant = months.most_common(1)[0][0] if months else None
        # Toutes les lignes d'un fichier appartiennent à SON mois comptable (le mois le plus fréquent des dates finales), jamais à la date de relevé
        # propre à chaque site : même fichier, même total, que l'on regarde septembre ou septembre → octobre.
        per_month: dict[str, list[dict]] = {dominant: list(lines)} if dominant else {}
        by_month.update(per_month)   # version la plus récente du fichier pour chaque mois
    if not found:
        return None
    return [x for m, xs in sorted(by_month.items()) if lo[:7] <= m <= hi[:7] for x in xs]


def closing_declared(d_from: date, d_to: date) -> dict[str, dict]:
    """
    Conso Closing par site (identifiant reconnu, en majuscules) telle que dans le fichier. Repli pour les Closing importés
    avant la conservation des lignes : valeur du contexte des stocks importés (ligne dont la période se termine dans la période).
    """
    import calendar as _cal

    from fuel_tracking.models import FuelStockObservation as Obs

    lines = closing_file_lines(d_from, d_to)
    out: dict[str, dict] = {}
    if lines is not None:
        for x in lines:
            if x.get("site_id") and x.get("conso_l") is not None:
                out[x["site_id"].upper()] = {"conso_l": _f(x["conso_l"]), "periode": [x.get("debut"), x.get("fin")]}
        return out
    from fuel_tracking.models import FuelClosingLine, FuelImportBatch as B

    if FuelClosingLine.objects.exists() or any((sm or {}).get("lignes_closing") for sm in B.objects.filter(source_type="CLOSING").values_list("summary", flat=True)):
        return out        # des Closing sont conservés mais aucun ne porte cette période : pas de valeur « déclarée » reconstituée par date de relevé (même fichier = même total)
    lo = d_from.replace(day=1)
    hi = d_to.replace(day=_cal.monthrange(d_to.year, d_to.month)[1])
    start, end = day_bounds(lo, hi)
    for sid, ctx in (Obs.objects.filter(source_type="CLOSING", observation_datetime__gte=start, observation_datetime__lte=end)
                     .order_by("pk").values_list("site_id", "source_context")):
        ctx = ctx or {}
        per = ctx.get("periode") or []
        val = ctx.get("conso_closing_l")
        if ctx.get("type") != "closing_periode" or len(per) != 2 or val is None or not sid:
            continue
        if not (lo.isoformat() <= str(per[1])[:10] <= hi.isoformat()):
            continue
        out[sid.upper()] = {"conso_l": _f(val), "periode": [str(per[0])[:10], str(per[1])[:10]]}
    return out


def build(d_from: date, d_to: date, params=None, with_cph_quality: bool = True) -> dict:
    """Calcule le tableau de comparaison unique pour la période EXACTE [d_from 00:00:00 ; d_to 23:59:59]."""
    from fuel_tracking.services.cph_health import freshness
    from fuel_tracking.services.cph_service import compute_period_summary
    from fuel_tracking.views_cph import _couvertures

    params = params or {}
    start, end = day_bounds(d_from, d_to)
    days = (d_to - d_from).days + 1
    cph_rows = compute_period_summary(d_from, d_to, country=params.get("pays") or None, zone=params.get("zone") or None)
    cph_by_site = {r["site_id"]: r for r in cph_rows}
    balances = latest_balances_by_site(d_from, d_to)
    targets = TG.active_targets()
    ids = sorted(set(balances) | {s for s, r in cph_by_site.items() if r.get("perimetre", "GE") == "GE"})
    rows = [_row(cph_by_site.get(sid), balances.get(sid), d_from, d_to, days, targets.get(TG.norm_site(sid)), {}) for sid in ids]
    # Closing : valeur du bilan quand ses deux stocks viennent de la même ligne de Closing (écart calculable),
    # sinon valeur déclarée dans le fichier pour la ligne du site qui se termine dans la période (affichée, sans écart).
    # Fichier conservé ligne à ligne : la valeur affichée est TOUJOURS celle du fichier (absente du fichier = absente).
    file_lines = closing_file_lines(d_from, d_to)
    declared = closing_declared(d_from, d_to)
    for r in rows:
        cl = r["closing"]
        dec = declared.get(r["site_id"].upper())
        if file_lines is not None:
            same_period = cl["conso_l"] is not None          # le bilan repose sur les deux stocks de cette ligne de Closing
            cl["conso_l"], cl["source"] = (dec["conso_l"], "fichier") if dec else (None, None)
            cl["periode_closing"] = dec["periode"] if dec else None
            if same_period and dec:
                cl["ecart_l"] = _gap(r["stock"]["variation_l"], cl["conso_l"])
                cl["ecart_pct"], cl["pct_na"] = _pct(cl["ecart_l"], cl["conso_l"])
            else:                                            # périodes différentes : pas d'écart
                cl["ecart_l"] = cl["ecart_pct"] = cl["pct_na"] = None
            continue
        if cl["conso_l"] is not None:
            cl["source"] = "bilan"
            continue
        cl["source"] = "fichier" if dec else None
        if dec:
            cl["conso_l"], cl["periode_closing"] = dec["conso_l"], dec["periode"]
    # Correspondance avec la base du fichier Closing : un site sans correspondance est AFFICHÉ mais jamais utilisé dans les totaux et indicateurs.
    base = closing_base_sites()
    for r in rows:
        ok = (not base) or r["site_id"].upper() in base
        r["correspondance"] = {"base_closing": ok, "utilise": ok,
                               "motif": None if ok else "Site absent du fichier Closing (base de référence) : affiché pour information, exclu des totaux et des indicateurs."}
    rows = _filter(rows, params)
    order = (params.get("tri") or "site").strip()
    sev = {NON_CALCULABLE: 0, VARIATION_CALCULEE: 1, ESTIMATION: 2, PRET: 3, CERTIFIEE: 4}
    if order == "gravite":
        rows.sort(key=lambda r: (sev[r["stock"]["statut"]], r["site_id"]))
    elif order == "variation":
        rows.sort(key=lambda r: (r["stock"]["variation_l"] is None, -(abs(r["stock"]["variation_l"] or 0)), r["site_id"]))
    elif order == "ecart_target":
        rows.sort(key=lambda r: (r["target"]["ecart_l"] is None, -abs(r["target"]["ecart_l"] or 0), r["site_id"]))
    else:
        rows.sort(key=lambda r: r["site_id"])
    fr = freshness()
    last = fr["facts_last_date"]
    non_couverts = max(0, min(days, (d_to - last).days)) if last else days
    uniq = lambda f: sorted({f(r) for r in rows if f(r)})  # noqa: E731
    fichier = None
    if file_lines is not None:
        vals = [x for x in file_lines if x.get("conso_l") is not None]
        fichier = {"total_l": round(sum(float(x["conso_l"]) for x in vals), 1) if vals else None, "sites": len(vals),
                   "lignes": len(file_lines), "sites_non_reconnus": sum(1 for x in vals if not x.get("site_id"))}
    return {
        "closing_fichier": fichier,
        "fraicheur": {
            "snowflake_derniere_date": last.isoformat() if last else None, "age_jours": fr["facts_age_days"], "seuil_jours": fr["stale_after_days"],
            "perime": bool(fr["facts_stale"]), "synchro_en_echec": bool(fr["last_sync_failed"]), "erreur_synchro": fr["last_sync_error"],
            "jours_non_couverts": non_couverts, "periode_incomplete": non_couverts > 0,
        },
        "periode": {"debut": d_from.isoformat(), "fin": d_to.isoformat(), "debut_dt": start.isoformat(), "fin_dt": end.isoformat(), "jours": days,
                    "libelle": f"{d_from:%d/%m/%Y} 00:00:00 → {d_to:%d/%m/%Y} 23:59:59",
                    "bilan_disponible": bool(balances), "sites_avec_bilan": len(balances),
                    "sites_calculables": sum(1 for b in balances.values() if bilan_status(b) != NON_CALCULABLE), "regle": RULE_VERSION},
        "kpis": {**_kpis([r for r in rows if r["correspondance"]["utilise"]]),
                 "sans_correspondance": {"sites": sum(1 for r in rows if not r["correspondance"]["utilise"]), "base_closing_sites": len(base),
                                         "note": "Sites hors du fichier Closing : affichés, non utilisés dans les totaux."}},
        "rows": rows, "labels": LABELS, "aide": HELP,
        "qualite_cph": [c for c in _couvertures([r for r in cph_rows if r.get("perimetre", "GE") == "GE"]) if c.get("code") != "rapprochement"] if with_cph_quality else [],
        "facettes": {
            "pays": uniq(lambda r: r["country"]), "zones": uniq(lambda r: r["zone"]), "typologies": uniq(lambda r: r["typologie"]),
            "typologies_energie": uniq(lambda r: r["typologie_energie"]), "statuts_bilan": list(STATUTS_BILAN),
            "sources_stock": uniq(lambda r: r["stock"]["source_initial"]), "sources_runtime": uniq(lambda r: r["cph"]["runtime_source"]),
            "sources_puissance": uniq(lambda r: r["cph"]["power_source"]), "sources_target": uniq(lambda r: r["target"]["source"]),
        },
        "seuils": {"couverture_instrumentee_min_pct": float(_cfg("FUEL_COMPARE_INSTR_MIN_COVERAGE_PCT", 80)), "tolerance_target_pct": TG.tolerance_pct(),
                   "reference_minimale_l": float(_cfg("FUEL_COMPARE_MIN_REFERENCE_L", 20))},
    }


# ── Exports CSV ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
EXPORTS = {
    "controle_complet": {"label": "Contrôle complet", "description": "Toutes les colonnes : stock, comparaisons, CPH, certification, sources et motifs."},
    "anomalies_fuel": {"label": "Anomalies Fuel", "description": "Bilans non certifiables : non calculables et variations à contrôler (dont négatives)."},
    "priorites_donnees": {"label": "Priorités données", "description": "Stocks manquants, cuves à traiter, conflits, mouvements incomplets, conversions impossibles."},
    "rapport_imports": {"label": "Rapport imports", "description": "Imports acceptés, rejetés, doublons et motifs (hash, date, utilisateur, version de règle)."},
}
PRIORITY_CAUSES = {
    "DONNEES_STOCK_INCOMPLETES": "Stock initial ou final manquant", "CM_SANS_CUVE": "Cuve à traiter / conversion cm vers litres impossible",
    "STOCK_TROP_ANCIEN": "Relevé de stock trop éloigné de la borne", "CONFLIT_ENOC_CLOSING": "Conflit ENOC / Closing",
    "MOUVEMENTS_A_CONTROLER": "Mouvement incomplet ou à contrôler", "TRANSFERT_DESEQUILIBRE": "Ponction déséquilibrée",
    "RELEVES_INCOHERENTS": "Relevés de stock incohérents (conflit de relevés)", "SITE_NON_RECONNU": "Site non reconnu", "SCORE_INSUFFISANT": "Score de confiance insuffisant",
    "CONSO_NEGATIVE": "Variation de stock négative", "AUCUN_BILAN": "Aucun bilan de stock pour ce site (aucune donnée de stock)",
}


def _csv(columns: list[str], lines: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, delimiter=";", extrasaction="ignore")
    w.writeheader()
    for r in lines:
        w.writerow({k: ("" if v is None else v) for k, v in r.items()})
    return "﻿" + buf.getvalue()


def _flat(r: dict) -> dict:
    s, cl, tg, ins, cp, ce = r["stock"], r["closing"], r["target"], r["instrumentee"], r["cph"], r["certification"]
    pc = lambda g: g["ecart_pct"] if g["ecart_pct"] is not None else g.get("pct_na")  # noqa: E731
    return {
        "Correspondance base Closing": "oui" if r["correspondance"]["utilise"] else "non (affiché, exclu des totaux)",
        "Pays": r["country"], "Zone": r["zone"], "Site ID": r["site_id"], "Nom du site": r["site_name"], "Typologie site": r["typologie"], "Typologie énergie": r["typologie_energie"],
        "Modèle GE": r["modele_ge"], "Puissance nominale GE (kVA)": r["puissance_nominale_kva"], "Source puissance GE": r["source_puissance"],
        "Date début": r["periode"]["debut"], "Date fin": r["periode"]["fin"], "Nombre de jours": r["periode"]["jours"],
        "Stock initial (L)": s["initial_l"], "Source stock initial": s["source_initial"], "Entrées carburant (L)": s["entrees_l"], "Carte Total (L)": s["carte_total_l"],
        "Truck (L)": s["truck_l"], "Livraisons externes (L)": s["livraisons_externes_l"], "Ponctions entrantes (L)": s["ponctions_entrantes_l"],
        "Ponctions sortantes (L)": s["ponctions_sortantes_l"], "Rajouts (L)": s["rajouts_l"], "Retraits (L)": s["retraits_l"], "Vols (L)": s["vols_l"], "Vidanges (L)": s["vidanges_l"],
        "Stock final (L)": s["final_l"], "Source stock final": s["source_final"], LABELS["variation"]: s["variation_l"], "Statut bilan": s["statut"], "Score": s["score"],
        "Réserve / motif": s["motif"], "Alertes": " | ".join(s["alertes"]), "Alertes de comparaison": " | ".join(s["alertes_comparaison"]), "Action attendue": s["action"],
        LABELS["closing"]: cl["conso_l"], "Contrôle Closing indépendant": ("oui" if cl["controle_independant"] else "non" if cl["controle_independant"] is False else ""), "Écart variation vs Closing (L)": cl["ecart_l"], "Écart variation vs Closing (%)": pc(cl),
        "Explication de l'écart Closing": " | ".join(f"{i['label']}" + (f" ({i['montant_l']:+.0f} L)" if i["montant_l"] is not None else "") for i in cl["explication"]["items"]),
        "Écart Closing expliqué (L)": cl["explication"]["explique_l"], "Écart Closing inexpliqué (L)": cl["explication"]["residuel_l"],
        LABELS["target"]: tg["periode_l"], "Target mensuel (L/mois)": tg["mensuel_l"], "Source target": tg["source"], "Statut target": tg["statut"], "Position vs target": tg["position"],
        "Écart variation vs target (L)": tg["ecart_l"], "Écart variation vs target (%)": pc(tg),
        LABELS["instrumentee"]: ins["conso_l"], "Couverture instrumentée (%)": ins["couverture_pct"], "Écart variation vs instrumentée (L)": ins["ecart_l"],
        "Écart variation vs instrumentée (%)": pc(ins), LABELS["cph_complete"]: cp["complete_l"], LABELS["cph_partielle"]: cp["partielle_l"], "Couverture CPH (%)": cp["couverture_pct"],
        "Écart variation vs CPH complète (L)": cp["ecart_l"], "Écart variation vs CPH complète (%)": pc(cp),
        "Runtime retenu (h)": cp["runtime_h"], "Source runtime": cp["runtime_source"], "Disponibilité runtime": cp["runtime_disponibilite"], "Source puissance": cp["power_source"],
        "Niveau médiane": cp["niveau_mediane"], "Nombre de mesures": cp["nb_mesures"], "Taux de charge (%)": cp["charge_pct"], "CPH retenu (L/h)": cp["cph_l_h"],
        "Motif CPH non calculée": cp["motif_code"],
        LABELS["certifiee"]: ce["valeur_l"], "Validée par": ce["par"], "Date validation": ce["date"], "Commentaire certification": ce["commentaire"],
    }


def export_csv(kind: str, d_from: date, d_to: date, params) -> tuple[str, str, int]:
    label = f"{d_from:%Y-%m-%d}_{d_to:%Y-%m-%d}"
    if kind == "rapport_imports":
        from fuel_tracking.models import FuelImportBatch as B

        cols = ["Type fichier", "Nom fichier", "Empreinte (SHA-256)", "Date import", "Utilisateur", "Lignes lues", "Lignes acceptées", "Lignes avec alerte", "Lignes rejetées",
                "Doublons", "Statut", "Motif / message", "Version de règle"]
        lines = [{"Type fichier": b.source_type, "Nom fichier": b.file_name, "Empreinte (SHA-256)": b.file_hash, "Date import": b.received_at.isoformat(),
                  "Utilisateur": getattr(b.received_by, "email", None) or getattr(b.received_by, "username", None), "Lignes lues": b.rows_total, "Lignes acceptées": b.rows_accepted,
                  "Lignes avec alerte": b.rows_warning, "Lignes rejetées": b.rows_rejected, "Doublons": b.rows_duplicate, "Statut": b.status, "Motif / message": b.status_message,
                  "Version de règle": b.mapping_version} for b in B.objects.select_related("received_by").order_by("-received_at")[:500]]
        return "rapport_imports.csv", _csv(cols, lines), len(lines)
    data = build(d_from, d_to, params, with_cph_quality=False)
    rows = data["rows"]
    if kind == "anomalies_fuel":
        lines = [_flat(r) for r in rows if r["stock"]["statut"] in (NON_CALCULABLE, VARIATION_CALCULEE)]
        cols = list(_flat(rows[0]).keys()) if rows else []
        return f"anomalies_fuel_{label}.csv", _csv(cols, lines), len(lines)
    if kind == "priorites_donnees":
        cols = ["Site ID", "Nom du site", "Zone", "Priorité", "Cause", "Statut bilan", "Motif", "Action attendue"]
        lines = []
        for r in rows:
            st = r["stock"]
            if st["cause"] in PRIORITY_CAUSES and st["statut"] in (NON_CALCULABLE, VARIATION_CALCULEE):
                prio, cause = PRIORITY_CAUSES[st["cause"]], st["cause"]
            elif "STOCK_IDENTIQUE_SUSPECT" in st["alertes"]:
                prio, cause = "Stock identique suspect (stock probablement non mis à jour)", "STOCK_IDENTIQUE_SUSPECT"
            elif st["alertes_comparaison"]:
                prio, cause = "Écart extrême entre mesures à investiguer", " | ".join(st["alertes_comparaison"])
            else:
                continue
            lines.append({"Site ID": r["site_id"], "Nom du site": r["site_name"], "Zone": r["zone"], "Priorité": prio, "Cause": cause, "Statut bilan": st["statut"],
                          "Motif": st["motif"], "Action attendue": st["action"]})
        return f"priorites_donnees_{label}.csv", _csv(cols, lines), len(lines)
    if kind == "controle_complet":
        lines = [_flat(r) for r in rows]
        cols = list(_flat(rows[0]).keys()) if rows else []
        return f"controle_complet_{label}.csv", _csv(cols, lines), len(lines)
    raise ValueError(f"Export inconnu : {kind}")


def evolution(limit: int = 12) -> dict:
    """Série par période (les `limit` plus récentes, ordre chronologique) : somme des variations de stock et des consommations Closing.
    Un total est None quand aucun site n'a la valeur (jamais 0 inventé). Instrumentée/CPH ne sont pas historisées : absentes de la série."""
    from fuel_tracking.models import FuelStockBalance as B
    from fuel_tracking.services.period_utils import available_periods, to_day

    wanted = {(date.fromisoformat(p["debut"]), date.fromisoformat(p["fin"])): p for p in available_periods(limit)}
    acc = {k: {"var": None, "n_var": 0, "clo": None, "n_clo": 0} for k in wanted}
    for b in B.objects.filter(observation_end__gte=min((k[1] for k in wanted), default=date.max)).iterator():
        a = acc.get((to_day(b.observation_start), to_day(b.observation_end)))
        if a is None:
            continue
        if b.variation_stock_l is not None:
            a["var"] = (a["var"] or 0.0) + float(b.variation_stock_l)
            a["n_var"] += 1
        ref = _closing_of(b)
        c = _f(ref.get("conso_closing_l")) if ref else None
        if c is not None:
            a["clo"] = (a["clo"] or 0.0) + c
            a["n_clo"] += 1
    out = []
    for (ds, de), p in sorted(wanted.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        a = acc[(ds, de)]
        out.append({"debut": p["debut"], "fin": p["fin"], "jours": (de - ds).days + 1, "sites": p["sites"],
                    "variation_l": None if a["var"] is None else round(a["var"], 1), "sites_variation": a["n_var"],
                    "closing_l": None if a["clo"] is None else round(a["clo"], 1), "sites_closing": a["n_clo"]})
    return {"periodes": out}
