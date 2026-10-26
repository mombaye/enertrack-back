# fuel_tracking/services/stock_period_balance.py
"""
Bilan stock par site et période EXACTE (observation_start → observation_end ; semaine, quinzaine, mois ou intervalle libre).

    conso_reelle_l = stock_initial_l + livraisons_truck_l + ravitaillements_carte_total_l + transferts_entrants_l + rajouts_l
                     − transferts_sortants_l − retraits_l − vols_l − vidanges_l − stock_final_l

Trois résultats distincts (statut_bilan) : PRET_A_CERTIFIER, ESTIMATION_AVEC_RESERVE, NON_CALCULABLE, avec un score de confiance
explicable (0-100) et la cause principale. NULL reste NULL : stock initial ou final inconnu → conso NULL, NON_CALCULABLE.

Stocks (jamais « le plus proche » par défaut) :
  - relevé EXACT (même jour que la borne) d'abord ; sinon relevé ANTÉRIEUR dans la tolérance : 1-3 j (fresh_stock_days) exploitable,
    4-14 j (reserve_stock_days) estimation avec réserve, > 14 j non calculable. Un relevé postérieur à la borne n'est jamais utilisé
    silencieusement (listé comme candidat écarté).
  - valeur : litre direct > cm converti (cuve validée, plaque) > cm estimé cuve géométrique sans plaque > cm estimé cuve proposée
    (score de compatibilité ≥ seuil). Les estimations sont de confiance FAIBLE et ne donnent jamais « prêt à certifier ».
  - à tier égal : relevé certifiable avant estimation, puis priorité de source (closing > gardiennage > ENOC qualifié > autre).
Mouvements : comptés entre le relevé initial et le relevé final retenus ; ceux survenus entre le relevé final et la fin de période
sont listés (réserve). ENOC : seuls les mouvements intégrés (statut éligible, date comptable) ; Ponction = transfert interne
(impact net nul au niveau pays) ; mouvements de fichiers déjà couverts par ENOC écartés (DOUBLON_POTENTIEL).
La certification est une action explicite du Fuel Manager (historique non modifiable).
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

RULE_VERSION = "bilan-v2"
D0 = Decimal("0")
SOURCE_RANK = {"CLOSING": 1, "LOLIFORCE": 2, "SCSS": 2, "ENOC": 3}
SOURCE_LABEL = {"CLOSING": "Closing mensuel validé", "LOLIFORCE": "Gardiennage Loliforce validé", "SCSS": "Gardiennage SCSS validé",
                "ENOC": "Relevé ENOC qualifié"}
COMPONENTS = ("truck_l", "carte_total_l", "transferts_entrants_l", "transferts_sortants_l", "vols_l", "vidanges_l",
              "retraits_l", "rajouts_l", "livraisons_externes_l")
OTHER_COMPONENT = {"DELIVERY": "livraisons_externes_l", "DELIVERY_EYDON": "livraisons_externes_l",
                   "DELIVERY_TOTAL_CARD": "carte_total_l", "ADD": "rajouts_l", "TRANSFER_IN": "transferts_entrants_l",
                   "TRANSFER_OUT": "transferts_sortants_l", "WITHDRAWAL": "retraits_l", "THEFT": "vols_l", "DRAIN": "vidanges_l"}
DELIVERY_COMPS = ("livraisons_externes_l", "carte_total_l")
CERTIFIABLE = ("PRET_A_CERTIFIER", "ESTIMATION_AVEC_RESERVE")
DEFAULT_WEIGHTS = {"stock_initial": 25, "stock_final": 25, "litre_direct_ou_cuve_validee": 20, "mouvements_complets": 15,
                   "stock_recent": 10, "sans_alerte_majeure": 5}
ESTIMATE_MODES = ("CM_CONVERTI_CUVE_GEOMETRIQUE_RESERVE", "CM_CONVERTI_CUVE_PROPOSEE_RESERVE")
VALUE_RANK = {"LITRE_DIRECT": 1, "CM_CONVERTI_CUVE_VALIDEE": 2, "CM_CONVERTI_CUVE_GEOMETRIQUE_RESERVE": 3,
              "CM_CONVERTI_CUVE_PROPOSEE_RESERVE": 4}


class BalanceError(ValueError):
    pass


def cfg() -> dict:
    return {
        "fresh_days": int(getattr(settings, "FUEL_BALANCE_FRESH_STOCK_DAYS", 3)),
        "reserve_days": int(getattr(settings, "FUEL_BALANCE_RESERVE_STOCK_DAYS", 14)),
        "weights": {**DEFAULT_WEIGHTS, **(getattr(settings, "FUEL_BALANCE_SCORE_WEIGHTS", None) or {})},
        "ready": int(getattr(settings, "FUEL_BALANCE_SCORE_READY", 80)),
        "reserve": int(getattr(settings, "FUEL_BALANCE_SCORE_RESERVE", 50)),
        "horizon_days": int(getattr(settings, "FUEL_ORDER_HORIZON_DAYS", 30)),
        "dup_qty_pct": float(getattr(settings, "FUEL_BALANCE_DUPLICATE_QTY_PCT", 2)),
    }


def _f(v):
    return float(v) if v is not None else None


def _iso(v):
    return v.isoformat() if v else None


def _day(dt):
    return timezone.localtime(dt).date() if timezone.is_aware(dt) else dt.date()


# ── Stocks candidats ──────────────────────────────────────────────────────────────────────────────────────
def _candidate(o: dict, boundary, final: bool, c: dict) -> dict:
    """Relevé candidat : utilisable ou non (motif), âge, tier de fraîcheur, nature de la valeur. Mesure brute toujours conservée."""
    t = o["observation_datetime"]
    quality = o.get("reading_quality_status")
    reason = None
    certifiable_liters = o["level_liters"] is not None and o["conversion_status"] == "VALID" and o["validation_status"] == "VALIDATED"
    estimated = o.get("estimated_liters") is not None and o.get("estimation_mode") in ESTIMATE_MODES \
        and o["validation_status"] in ("PENDING", "VALIDATED")
    if certifiable_liters:
        litres, mode, est = float(o["level_liters"]), quality or ("LITRE_DIRECT" if o["level_unit"] == "L" else "CM_CONVERTI_CUVE_VALIDEE"), False
    elif estimated:
        litres, mode, est = float(o["estimated_liters"]), o["estimation_mode"], True
    else:
        litres, mode, est = None, quality, False
        if o["validation_status"] == "REJECTED":
            reason = "relevé rejeté (valeur invalide)"
        elif o["level_unit"] not in ("L", "CM"):
            reason = "UNITE_INCONNUE : unité non reconnue"
        elif o["level_liters"] is None and o["level_unit"] == "CM":
            refus = (o.get("estimation_detail") or {}).get("refus")
            reason = "CONVERSION_CUVE_MANQUANTE : niveau en cm sans cuve exploitable — aucun litre calculé" + (f" ({refus})" if refus else "")
        elif o["validation_status"] != "VALIDATED":
            reason = f"relevé non validé ({o['validation_status']})"
        else:
            reason = f"conversion non retenue ({o['conversion_status']})"
    if reason is None and o["source_type"] == "ENOC" and not o.get("eligible_for_stock"):
        reason = f"relevé ENOC non qualifié (statut {o.get('business_status') or 'inconnu'})"
    age = (_day(boundary) - _day(t)).days
    if age == 0:
        tier = "EXACT"
    elif age > 0:
        tier = "FRAIS" if age <= c["fresh_days"] else "RESERVE" if age <= c["reserve_days"] else "TROP_ANCIEN"
    else:
        tier = "POSTERIEUR"
    if reason is None and tier == "TROP_ANCIEN":
        reason = f"relevé trop ancien ({age} j > {c['reserve_days']} j)"
    if reason is None and tier == "POSTERIEUR":
        reason = "relevé postérieur à la borne : non utilisé silencieusement"
    certifiable = (not est) and mode in ("LITRE_DIRECT", "CM_CONVERTI_CUVE_VALIDEE")
    return {
        "observation_id": o["id"], "source": o["source_type"], "source_libelle": SOURCE_LABEL.get(o["source_type"], f"Autre source : {o['source_type']}"),
        "rang_priorite": SOURCE_RANK.get(o["source_type"], 4), "date": _iso(t), "age_jours": abs(age), "tier": tier,
        "valeur_brute": o["level_raw"], "unite_origine": o["level_unit"], "niveau_cm": _f(o.get("level_cm")),
        "litres": litres, "estimation": est, "mode_valeur": mode, "qualite_releve": quality,
        "methode_conversion": o["conversion_method"], "modele_cuve": o.get("tank_model_ref"),
        "regle_conversion": o.get("conversion_rule"), "cuve": o.get("tank_id"),
        "confiance": "FAIBLE" if est else (o.get("confidence") or ("MOYENNE" if o["source_type"] == "ENOC" else "HAUTE" if certifiable else "FAIBLE")),
        "detail_estimation": o.get("estimation_detail") if est else None, "contexte_source": o.get("source_context") or None,
        "certifiable": certifiable, "dans_periode": tier in ("EXACT",) or (tier == "POSTERIEUR" and not final),
        "ecart_borne_h": round(abs((t - boundary).total_seconds()) / 3600, 1),
        "utilisable": reason is None, "motif_rejet": reason,
    }


def _tier_rank(tier: str) -> int:
    return {"EXACT": 0, "FRAIS": 1, "RESERVE": 2}.get(tier, 9)


def _select(obs: list[dict], boundary, final: bool, tanks: list, c: dict) -> tuple[dict | None, list[dict], list[str]]:  # noqa: C901
    alerts = []
    cands = [_candidate(o, boundary, final, c) for o in obs]
    usable = [x for x in cands if x["utilisable"]]
    key = lambda x: (_tier_rank(x["tier"]), 0 if x["certifiable"] else 1, x["rang_priorite"],  # noqa: E731
                     VALUE_RANK.get(x["mode_valeur"], 9), x["age_jours"])
    usable.sort(key=key)
    cands.sort(key=lambda x: (_tier_rank(x["tier"]), x["age_jours"], x["rang_priorite"]))
    validated_tanks = [t for t in tanks if t.validated]
    chosen = None
    if usable and len(validated_tanks) > 1 and str(usable[0].get("cuve") or "").upper() != "SITE":  # « SITE » = stock total (Closing)
        best = usable[0]
        per_tank = {}
        for x in usable:
            if x["source"] == best["source"] and x["date"][:10] == best["date"][:10] and x["cuve"] and str(x["cuve"]).upper() not in per_tank:
                per_tank[str(x["cuve"]).upper()] = x
        missing = [t.tank_id for t in validated_tanks if str(t.tank_id).upper() not in per_tank]
        if missing:
            alerts.append(f"Site à {len(validated_tanks)} cuves : relevé {best['source_libelle']} manquant pour la cuve {', '.join(missing)}.")
        else:
            parts = list(per_tank.values())
            chosen = {**best, "litres": sum(p["litres"] for p in parts), "cuves": [p["cuve"] for p in parts],
                      "observation_ids": [p["observation_id"] for p in parts], "estimation": any(p["estimation"] for p in parts),
                      "certifiable": all(p["certifiable"] for p in parts)}
            if chosen["estimation"]:
                chosen["confiance"] = "FAIBLE"
    elif usable:
        chosen = dict(usable[0])
    if chosen:
        others = [x for x in usable if x["source"] != chosen["source"] and x["tier"] == chosen["tier"]]
        cap = sum(float(t.check_capacity_l() or 0) for t in validated_tanks) or None
        tol = max(100.0, 0.10 * cap) if cap else 100.0
        for o in others:
            if abs(o["litres"] - chosen["litres"]) > tol and len(validated_tanks) <= 1:
                alerts.append(f"ECART_STOCK_CANDIDATS : {chosen['source_libelle']} {chosen['litres']:.0f} L vs {o['source_libelle']} "
                              f"{o['litres']:.0f} L (écart > {tol:.0f} L) — valeur retenue selon la priorité, à contrôler.")
        tier_txt = {"EXACT": "relevé du jour de la borne", "FRAIS": f"relevé antérieur de {chosen['age_jours']} j (frais ≤ {c['fresh_days']} j)",
                    "RESERVE": f"relevé antérieur de {chosen['age_jours']} j (réserve, ≤ {c['reserve_days']} j)"}[chosen["tier"]]
        chosen["motif_selection"] = (
            f"{chosen['source_libelle']} (priorité {chosen['rang_priorite']}) — {tier_txt} ; valeur {chosen['mode_valeur']}"
            + (" (ESTIMATION, confiance faible)" if chosen["estimation"] else "")
            + (f" ; {len(usable) - 1} autre(s) candidat(s) utilisable(s) écarté(s) par la priorité" if len(usable) > 1 else ""))
    return chosen, cands, alerts


# ── Calcul ────────────────────────────────────────────────────────────────────────────────────────────────
def compute_balances(start, end, site_ids=None, persist=True) -> list[dict]:
    """Calcule (et enregistre) le bilan de chaque site pour la période exacte [start, end]."""
    from core.models import Site
    from fuel_tracking.models import FuelEnocQualifiedMovement as EQ, FuelMovement, FuelStockObservation as Obs, FuelTankMaster
    from fuel_tracking.services.site_energy_profile import energy_profiles
    from fuel_tracking.services.stock_enoc import check_transfer_balance
    from fuel_tracking.services.stock_registry import site_tanks

    if end <= start:
        raise BalanceError("La fin de période doit être postérieure au début.")
    c = cfg()
    pad = timedelta(days=c["reserve_days"] + 1)
    lo, hi = start - pad, end + pad
    # Relevés : fenêtre élargie pour voir (et signaler) les relevés trop anciens plutôt que de les ignorer en silence
    obs_lo = start - timedelta(days=c["reserve_days"] + 31)
    obs_qs = Obs.objects.filter(observation_datetime__range=(obs_lo, hi)).exclude(source_type="SNOWFLAKE")
    enoc_qs = EQ.objects.filter(Q(accounting_at__range=(lo, hi)) | Q(accounting_at__isnull=True, actual_operation_at__range=(lo, hi)))
    mv_qs = FuelMovement.objects.filter(movement_datetime__range=(lo, hi))
    if site_ids is not None:
        site_ids = list(site_ids)
        obs_qs = obs_qs.filter(site_id__in=site_ids)
        enoc_qs = enoc_qs.filter(Q(site_id_resolved__in=site_ids) | Q(ponction_source_site_resolved__in=site_ids))
        mv_qs = mv_qs.filter(site_id__in=site_ids)

    obs_by_site = defaultdict(list)
    for o in obs_qs.values("id", "site_id", "tank_id", "source_type", "observation_datetime", "level_raw", "level_value", "level_unit",
                           "level_cm", "level_liters", "conversion_method", "conversion_status", "conversion_rule", "tank_model_ref",
                           "validation_status", "eligible_for_stock", "business_status", "reading_quality_status", "confidence",
                           "estimated_liters", "estimation_mode", "estimation_detail", "source_context"):
        obs_by_site[o["site_id"]].append(o)
    enoc_by_site = defaultdict(list)
    for q in enoc_qs:
        for sid in {q.site_id_resolved, q.ponction_source_site_resolved if q.type_family == "PONCTION" else None} - {None}:
            enoc_by_site[sid].append(q)
    mv_by_site = defaultdict(list)
    for m in mv_qs:
        mv_by_site[m.site_id].append(m)

    if site_ids is None:
        tank_sites = set(FuelTankMaster.objects.filter(active=True).exclude(validation_status="REJETEE").values_list("site_id", flat=True))
        site_ids = sorted(set(obs_by_site) | set(enoc_by_site) | set(mv_by_site) | tank_sites)
    sites = {s["site_id"]: s for s in Site.objects.filter(site_id__in=site_ids).values("site_id", "name", "zone", "country", "installed_typology")}
    profiles = energy_profiles(site_ids)
    tanks = site_tanks(site_ids)
    unbalanced = {b["transfer_id"] for b in check_transfer_balance()}
    rows = [_site_balance(sid, start, end, c, obs_by_site.get(sid, []), enoc_by_site.get(sid, []), mv_by_site.get(sid, []),
                          tanks.get(sid, []), sites.get(sid), profiles.get(sid, {}), unbalanced) for sid in site_ids]
    if persist:
        _persist(rows, start, end)
    return rows


def _movements(site_id, enoc, movements, t0, t1, end, c, unbalanced):  # noqa: C901
    comps = {k: D0 for k in COMPONENTS}
    counted, excluded, after_final = [], [], []
    st = {"unknown": 0, "pending_fm": 0, "historical": 0, "integrated": 0, "defects": 0, "undated": 0, "unbalanced": []}
    defect_kinds = set()

    def inside(dt):
        return dt is not None and t0 < dt <= t1

    for q in enoc:
        when = q.accounting_at or q.actual_operation_at
        ref = {"source": "ENOC", "reference": q.enoc_reference, "date": _iso(when), "type": q.raw_movement_type, "statut_brut": q.raw_status,
               "statut_metier": q.business_status, "qualite": q.quality_status_global or q.quality_status, "date_source": q.date_source_used,
               "lien_enoc_id": q.pk, "source_record_id": q.source_record_id}
        if when is not None and t1 < when <= end and q.integration_status == "INTEGRE":
            after_final.append({**ref, "quantite_l": _f(q.integrated_quantity_l)})
        if not inside(when):
            continue
        if q.integration_status == "INTEGRE":
            if q.type_family == "PONCTION":
                if q.fuel_manager_alert == "EN_ATTENTE":
                    st["pending_fm"] += 1
                continue  # comptée via ses deux écritures de transfert
            if q.site_id_resolved != site_id or q.integrated_quantity_l is None:
                continue
            comp = "carte_total_l" if q.type_family == "TOTAL_CARD" else "truck_l"
            comps[comp] += q.integrated_quantity_l
            if comp == "truck_l":
                comps["livraisons_externes_l"] += q.integrated_quantity_l
            st["integrated"] += 1
            if q.quality_status_global in ("DEFAUTS_MINEURS", "DEFAUTS_MAJEURS"):
                st["defects"] += 1
                defect_kinds.update(k for k, ok in (("niveaux", not q.has_levels_before_after), ("preuve", not q.has_proof),
                                                    ("compteur", not q.has_hourmeter), ("écart de niveau", q.level_coherence_status == "ECART")) if ok)
            counted.append({**ref, "composante": comp, "quantite_l": _f(q.integrated_quantity_l)})
            continue
        if q.business_status == "HISTORIQUE_NON_VERIFIABLE":
            st["historical"] += 1
            excluded.append({**ref, "motif": "Historique non vérifiable (ancien connecteur) : exclu du bilan."})
            continue
        if q.execution_state in ("DEMANDE", "APPROUVEE", "REJETEE"):
            continue
        if q.type_family != "PONCTION" and q.site_id_resolved != site_id:
            continue
        st["unknown"] += 1
        excluded.append({**ref, "motif": f"Mouvement exécuté non intégrable ({q.non_integration_cause}) : impact inconnu, à contrôler."})

    enoc_deliveries = [m for m in counted if m["composante"] in DELIVERY_COMPS + ("truck_l",)]
    for m in movements:
        when = m.movement_datetime
        base = {"source": m.source_type, "reference": m.transfer_id or m.source_event_id, "date": _iso(when), "type": m.movement_type,
                "qualite": m.quality_status}
        if m.source_type == "ENOC":
            if m.movement_type in ("TRANSFERT_INTERSITE_SORTANT", "TRANSFERT_INTERSITE_ENTRANT") and m.retained_in_stock:
                if when is not None and t1 < when <= end:
                    after_final.append({**base, "quantite_l": _f(m.impact_stock_l)})
                if not inside(when):
                    continue
                if m.transfer_id in unbalanced:
                    st["unbalanced"].append(m.transfer_id)
                    excluded.append({**base, "motif": "Transfert techniquement déséquilibré (somme des impacts ≠ 0) : bilan non calculable."})
                    continue
                comp = "transferts_entrants_l" if m.movement_type.endswith("ENTRANT") else "transferts_sortants_l"
                comps[comp] += abs(m.impact_stock_l or D0)
                st["integrated"] += 1
                counted.append({**base, "composante": comp, "quantite_l": _f(m.impact_stock_l), "contrepartie": m.counterpart_site_id})
            continue
        if not inside(when):
            if when is None and m.retained_in_stock:
                st["undated"] += 1
            continue
        comp = OTHER_COMPONENT.get(m.movement_type or "")
        if m.status == "REJECTED" or m.source_category == "DEMANDE_LIVRAISON":
            continue
        if m.retained_in_stock and m.status == "VALIDATED" and m.quantity_l is not None and comp:
            qty = abs(m.quantity_l)
            dup = None
            if comp in DELIVERY_COMPS:  # même livraison déjà couverte par ENOC : jamais comptée deux fois
                for e in enoc_deliveries:
                    de = datetime.fromisoformat(e["date"])
                    if abs(( when - de).total_seconds()) <= 86400 and abs(Decimal(str(e["quantite_l"])) - qty) <= max(Decimal("5"), qty * Decimal(str(c["dup_qty_pct"])) / 100):
                        dup = e
                        break
            if dup is not None:
                excluded.append({**base, "quantite_l": _f(qty), "motif": f"DOUBLON_POTENTIEL : livraison déjà intégrée via ENOC ({dup['reference']}) — non recomptée."})
                continue
            comps[comp] += qty
            st["integrated"] += 1
            counted.append({**base, "composante": comp, "quantite_l": _f(qty)})
        else:
            st["unknown"] += 1
            excluded.append({**base, "motif": "Mouvement sans quantité validée ou type non classifié : impact inconnu, à contrôler."})
    return comps, counted, excluded, after_final, st, sorted(defect_kinds)


def _closing_reference(ini, fin) -> dict | None:
    """Totaux de période du Closing, si les deux stocks retenus viennent de la même ligne de Closing (même période)."""
    if not (ini and fin) or ini.get("source") != "CLOSING" or fin.get("source") != "CLOSING":
        return None
    a, b = ini.get("contexte_source") or {}, fin.get("contexte_source") or {}
    if a.get("type") == "closing_periode" and a.get("periode") and a.get("periode") == b.get("periode"):
        return a
    return None


def ponctions_globales(start, end) -> dict:
    """Ponctions de la période, sur les mouvements eux-mêmes (indépendant des fenêtres de stock de chaque site) :
    entrées − sorties doit valoir 0 (chaque transfert a deux écritures liées de somme nulle)."""
    from django.db.models import Sum

    from fuel_tracking.models import FuelMovement

    qs = FuelMovement.objects.filter(retained_in_stock=True, movement_datetime__range=(start, end),
                                     movement_type__in=("TRANSFERT_INTERSITE_SORTANT", "TRANSFERT_INTERSITE_ENTRANT"))
    ent = qs.filter(movement_type="TRANSFERT_INTERSITE_ENTRANT").aggregate(v=Sum("impact_stock_l"))["v"] or D0
    sor = qs.filter(movement_type="TRANSFERT_INTERSITE_SORTANT").aggregate(v=Sum("impact_stock_l"))["v"] or D0
    return {"entrantes_l": float(abs(ent)), "sortantes_l": float(abs(sor)), "impact_global_l": round(float(ent + sor), 3)}


def _score(c, ini, fin, st, major_alerts, has_movement_defects) -> tuple[int, list[dict]]:
    w = c["weights"]
    present = [x for x in (ini, fin) if x]
    detail = []

    def add(key, ok, why_ok, why_ko):
        detail.append({"critere": key, "points_max": w[key], "points": w[key] if ok else 0, "motif": why_ok if ok else why_ko})

    add("stock_initial", bool(ini), "Stock initial exploitable.", "Stock initial absent ou inexploitable.")
    add("stock_final", bool(fin), "Stock final exploitable.", "Stock final absent ou inexploitable.")
    add("litre_direct_ou_cuve_validee", bool(present) and all(x["certifiable"] for x in present),
        "Stocks en litres directs ou cuve validée.", "Stock estimé (cuve géométrique sans plaque / proposée) ou absent.")
    add("mouvements_complets", not has_movement_defects and not st["unknown"] and not st["historical"],
        "Mouvements complets (ou aucun mouvement).", "Mouvement(s) avec niveau / preuve / compteur manquant, écart, historique ou impact inconnu.")
    add("stock_recent", bool(ini and fin) and ini["tier"] in ("EXACT", "FRAIS") and fin["tier"] in ("EXACT", "FRAIS"),
        f"Stocks à ≤ {c['fresh_days']} j des bornes.", f"Stock absent ou ancien (> {c['fresh_days']} j).")
    add("sans_alerte_majeure", not major_alerts, "Aucune alerte majeure.", "Alerte majeure : " + "; ".join(major_alerts[:2]))
    return sum(d["points"] for d in detail), detail


def _site_balance(site_id, start, end, c, obs, enoc, movements, tanks, site, profile, unbalanced) -> dict:  # noqa: C901
    motifs, alerts, reserves, major = [], [], [], []
    cause = None
    ini, ini_cands, a1 = _select(obs, start, False, tanks, c)
    fin, fin_cands, a2 = _select([o for o in obs if o["observation_datetime"] <= end + timedelta(days=1)], end, True, tanks, c)
    alerts += a1 + a2
    if ini and fin and datetime.fromisoformat(fin["date"]) <= datetime.fromisoformat(ini["date"]):
        motifs.append("Le relevé final retenu n'est pas postérieur au relevé initial : bilan impossible.")
        fin, cause = None, "RELEVES_INCOHERENTS"
    for label, chosen, cands in (("initial", ini, ini_cands), ("final", fin, fin_cands)):
        if chosen:
            continue
        old = [x for x in cands if x["tier"] == "TROP_ANCIEN"]
        nocuve = [x for x in cands if x["motif_rejet"] and "CONVERSION_CUVE_MANQUANTE" in x["motif_rejet"]]
        why = (f"relevé(s) présent(s) mais non utilisables, voir candidats" if cands else "aucun relevé dans la tolérance")
        motifs.append(f"Stock {label} absent : {why}.")
        if not cause:
            cause = "STOCK_TROP_ANCIEN" if old and not nocuve else "CM_SANS_CUVE" if nocuve else "DONNEES_STOCK_INCOMPLETES"
    if any(x["motif_rejet"] and "CONVERSION_CUVE_MANQUANTE" in x["motif_rejet"] for x in ini_cands + fin_cands):
        alerts.append("CONVERSION_CUVE_MANQUANTE : relevé(s) en cm sans cuve exploitable rattachée au site (aucun litre inventé).")
    for x, label in ((ini, "initial"), (fin, "final")):
        if x:
            if x["estimation"]:
                reserves.append(f"Stock {label} ESTIMÉ ({x['mode_valeur']}, confiance faible).")
                if str(x["mode_valeur"]).endswith("GEOMETRIQUE_RESERVE"):
                    reserves.append(f"Stock {label} : volume calculé à partir des dimensions de cuve, plaque non confirmée.")
            if x["tier"] == "RESERVE":
                reserves.append(f"Stock {label} ancien de {x['age_jours']} j (4-{c['reserve_days']} j) : estimation avec réserve.")
            elif x["tier"] == "FRAIS" and x["age_jours"]:
                reserves.append(f"Stock {label} antérieur de {x['age_jours']} j à la borne.")
            if x["rang_priorite"] >= 3:
                reserves.append(f"Stock {label} issu de {x['source_libelle']} (source moins fiable).")

    t0 = datetime.fromisoformat(ini["date"]) if ini else start
    t1 = datetime.fromisoformat(fin["date"]) if fin else end
    comps, counted, excluded, after_final, st, defect_kinds = _movements(site_id, enoc, movements, t0, t1, end, c, unbalanced)
    if st["unbalanced"]:
        cause = cause or "TRANSFERT_DESEQUILIBRE"
        motifs.append(f"Transfert(s) déséquilibré(s) : {', '.join(sorted(set(st['unbalanced']))[:3])}.")
    if st["unknown"]:
        cause = cause or "MOUVEMENTS_A_CONTROLER"
        motifs.append(f"{st['unknown']} mouvement(s) exécuté(s) dont l'impact n'est pas établi : consommation réelle non calculée.")
    if st["pending_fm"]:
        major.append(f"{st['pending_fm']} Ponction(s) en attente de contrôle Fuel Manager (écart de niveau).")
    if after_final:
        major.append(f"{len(after_final)} mouvement(s) entre le relevé final ({_iso(t1)[:10]}) et la fin de période : non comptés, à vérifier.")
    if st["historical"]:
        reserves.append(f"{st['historical']} mouvement(s) historique(s) non vérifiable(s) exclu(s).")
    if st["undated"]:
        reserves.append(f"{st['undated']} mouvement(s) sans date comptable exclu(s).")
    if st["defects"]:
        reserves.append(f"{st['defects']} mouvement(s) intégré(s) avec contrôles manquants ({', '.join(defect_kinds)}).")
    if any("ECART_STOCK_CANDIDATS" in a for a in alerts):
        major.append("Écart important entre relevés candidats.")
    if ini and fin and ini["litres"] == fin["litres"] and not any(comps[k] for k in ("livraisons_externes_l", "carte_total_l", "transferts_entrants_l", "rajouts_l")):
        major.append("Stock initial et final identiques sans aucun mouvement : consommation nulle suspecte (relevé reporté ou capteur figé ?).")
    if site is None:
        cause = "SITE_NON_RECONNU"
        motifs.insert(0, "Site absent de Gestion des sites : bilan non calculable.")

    # Contrôle croisé : quand les DEUX stocks viennent du même Closing, ses totaux de période (livré, rajouts/retraits, volé, vidangé) disent
    # ce qui est réellement entré / sorti. Des entrées eGrid (ENOC) inférieures = livraison(s) non retrouvée(s) → consommation fausse par défaut.
    closing_ref = _closing_reference(ini, fin)
    if closing_ref:
        eg = (comps["livraisons_externes_l"] + comps["carte_total_l"] + comps["transferts_entrants_l"] + comps["rajouts_l"]
              - comps["transferts_sortants_l"] - comps["retraits_l"] - comps["vols_l"] - comps["vidanges_l"])
        cl = Decimal(str(closing_ref.get("livre_l") or 0)) + Decimal(str(closing_ref.get("rajout_retrait_l") or 0)) \
            - Decimal(str(closing_ref.get("vole_l") or 0)) - Decimal(str(closing_ref.get("vidange_l") or 0))
        gap, tol = eg - cl, max(Decimal(20), Decimal("0.05") * abs(cl))
        ref_out = {"mouvements_nets_egrid_l": _f(eg), "mouvements_nets_closing_l": _f(cl), "ecart_l": _f(gap),
                   "conso_closing_l": closing_ref.get("conso_closing_l")}
        for x in (ini, fin):
            x["reference_closing"] = ref_out
        if gap < -tol:
            cause = cause or "ENTREES_NON_RETROUVEES"
            motifs.append(f"Le Closing compte {_f(cl):.0f} L d'entrées nettes sur la période, eGrid (ENOC) seulement {_f(eg):.0f} L : "
                          f"livraison(s) non retrouvée(s) dans ENOC ({_f(-gap):.0f} L) — consommation non calculée (conso du Closing : "
                          f"{closing_ref.get('conso_closing_l') if closing_ref.get('conso_closing_l') is None else round(closing_ref['conso_closing_l'])} L).")
        elif gap > tol:
            major.append(f"Entrées eGrid (ENOC) supérieures de {_f(gap):.0f} L à celles du Closing ({_f(cl):.0f} L) : livraison non confirmée au Closing ?")

    conso = None
    can = ini and fin and not cause
    if can:
        conso = (Decimal(str(ini["litres"])) + comps["livraisons_externes_l"] + comps["carte_total_l"] + comps["transferts_entrants_l"]
                 + comps["rajouts_l"] - comps["transferts_sortants_l"] - comps["retraits_l"] - comps["vols_l"] - comps["vidanges_l"]
                 - Decimal(str(fin["litres"])))
        if conso < 0:
            motifs.append(f"Consommation calculée négative ({conso:.0f} L) : mouvement manquant ou relevé erroné — non retenue.")
            conso, cause = None, "CONSO_NEGATIVE"
    score, detail = _score(c, ini, fin, st, major, st["defects"] > 0)
    if conso is None:
        statut = "NON_CALCULABLE"
        cause = cause or "DONNEES_STOCK_INCOMPLETES"
    elif score < c["reserve"]:
        statut, conso, cause = "NON_CALCULABLE", None, "SCORE_INSUFFISANT"
        motifs.append(f"Score de confiance {score} < {c['reserve']} : non calculable.")
    elif score >= c["ready"] and not reserves and not major:
        statut = "PRET_A_CERTIFIER"
    else:
        statut = "ESTIMATION_AVEC_RESERVE"
        cause = "ESTIMATION" if any(x and x["estimation"] for x in (ini, fin)) else "RESERVES"
    days = (t1 - t0).total_seconds() / 86400 if (ini and fin) else None
    conso_j = (conso / Decimal(str(days))).quantize(Decimal("0.001")) if conso is not None and days and days > 0 else None

    entrees_ext = comps["livraisons_externes_l"] + comps["carte_total_l"]
    volume_transfere = comps["transferts_sortants_l"]
    out = {
        "country": (site or {}).get("country"), "site_id": site_id, "site_name": (site or {}).get("name"), "zone": (site or {}).get("zone"),
        "typologie": (site or {}).get("installed_typology"), "profil_energetique": profile.get("profil"),
        "observation_start": start, "observation_end": end,
        "stock_initial_l": Decimal(str(ini["litres"])) if ini else None, "source_stock_initial": ini["source"] if ini else None,
        "date_stock_initial": t0 if ini else None, "stock_initial_detail": ini or {},
        "stock_final_l": Decimal(str(fin["litres"])) if fin else None, "source_stock_final": fin["source"] if fin else None,
        "date_stock_final": t1 if fin else None, "stock_final_detail": fin or {},
        **comps, "consommation_reelle_l": conso, "conso_journaliere_l": conso_j,
        "statut_bilan": statut, "resultat_bilan": statut, "cause_principale": cause, "score_confiance": score, "score_detail": detail,
        "reserves": list(dict.fromkeys(reserves)), "estimation": any(x and x["estimation"] for x in (ini, fin)),
        "age_stock_initial_j": ini["age_jours"] if ini else None, "age_stock_final_j": fin["age_jours"] if fin else None,
        "entrees_externes_nettes_l": entrees_ext, "volume_transfere_l": volume_transfere,
        "impact_ponctions_l": comps["transferts_entrants_l"] - comps["transferts_sortants_l"],
        "volume_brut_integre_l": entrees_ext + volume_transfere,
        "motifs": motifs + list(dict.fromkeys(reserves)) + major, "alertes": list(dict.fromkeys(alerts + major)),
        "candidats": {"initial": ini_cands[:20], "final": fin_cands[:20]},
        "mouvements": counted, "mouvements_exclus": excluded + [{**m, "motif": "Mouvement après le relevé final : non compté."} for m in after_final],
        "periode_effective_debut": t0, "periode_effective_fin": t1, "rule_version": RULE_VERSION,
    }
    out["proposition"] = _proposition(out, tanks, c)
    return out


def _proposition(r: dict, tanks, c: dict) -> dict:
    """Données préparées pour la future recommandation de commande. INDICATIF : aucune commande n'est créée."""
    fin = r["stock_final_l"]
    est = r["statut_bilan"] != "PRET_A_CERTIFIER"
    out = {"indicatif": True, "commande_automatique": False, "stock_final_l": _f(fin), "fraicheur_stock_final_j": r["age_stock_final_j"],
           "conso_reference_jour_l": _f(r["conso_journaliere_l"]), "score_confiance": r["score_confiance"],
           "alertes": (r["alertes"] + r["reserves"])[:10], "horizon_jours": c["horizon_days"], "quantite_indicative_l": None,
           "avertissement": "ESTIMATION — VALIDATION FUEL MANAGER REQUISE" if est else None}
    if fin is not None and r["conso_journaliere_l"] is not None:
        need = r["conso_journaliere_l"] * c["horizon_days"] - fin
        cap = sum(Decimal(str(t.check_capacity_l() or 0)) for t in tanks if t.validated) or None
        if cap:
            need = min(need, cap - fin)
            out["capacite_l"] = _f(cap)
        out["quantite_indicative_l"] = _f(max(need, D0).quantize(Decimal("1")))
    return out


FINGERPRINT_KEYS = ("stock_initial_l", "stock_final_l", "consommation_reelle_l", "source_stock_initial", "source_stock_final",
                    "date_stock_initial", "date_stock_final", "score_confiance", *COMPONENTS)


def fingerprint(row: dict) -> str:
    payload = {k: str(row.get(k)) if row.get(k) is not None else None for k in FINGERPRINT_KEYS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _persist(rows: list[dict], start, end):
    from fuel_tracking.models import FuelStockBalance as B

    existing = {b.site_id: b for b in B.objects.filter(observation_start=start, observation_end=end).prefetch_related("certifications")}
    now = timezone.now()
    with transaction.atomic():
        for r in rows:
            fp = fingerprint(r)
            b = existing.get(r["site_id"])
            values = {k: v for k, v in r.items() if k not in ("id", "niveau_certification")}
            values.update(fingerprint=fp, calculated_at=now)
            if b is None:
                b = B(**values)
            else:
                for k, v in values.items():
                    setattr(b, k, v)
            last = b.certifications.all()[0] if b.pk and b.certifications.exists() else None
            if last is None:
                b.niveau_certification = "NON_CERTIFIE"
            elif last.fingerprint == fp:
                b.niveau_certification = last.action
            else:
                b.niveau_certification = "A_RECERTIFIER"
            b.save()
            r["id"], r["niveau_certification"] = b.pk, b.niveau_certification


SNAPSHOT_KEYS = ("site_id", "observation_start", "observation_end", "stock_initial_l", "source_stock_initial", "stock_final_l",
                 "source_stock_final", *COMPONENTS, "consommation_reelle_l", "statut_bilan", "score_confiance", "reserves", "motifs")


def certify(balance, user, decision: str, comment: str):
    """Certification explicite par le Fuel Manager. Une estimation ne peut être que « certifiée avec réserve »."""
    from fuel_tracking.models import FuelStockBalanceCertification as C

    comment = (comment or "").strip()
    action = {"certifier": "CERTIFIE", "certifier_avec_reserve": "CERTIFIE_AVEC_RESERVE"}.get(decision)
    if action is None:
        raise BalanceError("Décision inconnue (certifier | certifier_avec_reserve).")
    if balance.statut_bilan not in CERTIFIABLE:
        raise BalanceError(f"Bilan au statut {balance.statut_bilan} : seul un bilan calculable peut être certifié.")
    if action == "CERTIFIE" and balance.statut_bilan != "PRET_A_CERTIFIER":
        raise BalanceError("Estimation avec réserve : utiliser « Certifier avec réserve » (commentaire obligatoire).")
    if action == "CERTIFIE_AVEC_RESERVE" and not comment:
        raise BalanceError("Commentaire obligatoire pour une certification avec réserve.")
    snapshot = {k: (str(getattr(balance, k)) if not isinstance(getattr(balance, k), (list, dict, type(None))) else getattr(balance, k))
                for k in SNAPSHOT_KEYS}
    with transaction.atomic():
        cert = C.objects.create(balance=balance, action=action, comment=comment, user=user if getattr(user, "is_authenticated", False) else None,
                                user_label=getattr(user, "username", "") or "", fingerprint=balance.fingerprint, snapshot=snapshot)
        balance.niveau_certification = action
        balance.save(update_fields=["niveau_certification"])
    return cert


def reconciliation(start, end, country=None, zone=None) -> list[dict]:
    """
    Consommation réelle / estimée (bilan stock) / instrumentée (Snowflake) / théorique (CPH) côte à côte, même période exacte.
    Aucun écart n'est calculé si la consommation stock est NULL ; une estimation est signalée comme telle.
    """
    from fuel_tracking.models import FuelStockBalance as B
    from fuel_tracking.services.cph_service import compute_period_summary

    balances = B.objects.filter(observation_start=start, observation_end=end)
    if country:
        balances = balances.filter(country__iexact=country)
    if zone:
        balances = balances.filter(zone__iexact=zone)
    balances = list(balances)
    try:
        cph = {r["site_id"]: r for r in compute_period_summary(start.date(), end.date(), country=country, zone=zone)}
        cph_error = None
    except Exception as e:  # une source indisponible ne casse pas l'écran
        cph, cph_error = {}, f"CPH / Snowflake indisponibles ({type(e).__name__})"
    out = []
    for b in balances:
        r = cph.get(b.site_id) or {}
        real = b.consommation_reelle_l
        instr, theo = r.get("conso_mesuree_l"), r.get("conso_theorique_l")
        motif = []
        if real is None:
            motif.append("Consommation stock non calculée : aucun écart.")
        elif b.estimation or b.statut_bilan == "ESTIMATION_AVEC_RESERVE":
            motif.append("Consommation ESTIMÉE (avec réserve), non certifiée.")
        if instr is None:
            motif.append("Consommation instrumentée indisponible.")
        if theo is None:
            motif.append("Consommation théorique CPH non calculable" + (f" ({r.get('cph_status')})" if r.get("cph_status") else "") + ".")
        if cph_error:
            motif.append(cph_error)

        def gap(ref):
            if real is None or ref is None:
                return None, None
            g = Decimal(str(real)) - Decimal(str(ref))
            return _f(g), (_f(g / Decimal(str(ref)) * 100) if ref else None)

        g_i, p_i = gap(instr)
        g_t, p_t = gap(theo)
        out.append({"bilan_id": b.pk, "site_id": b.site_id, "site_name": b.site_name, "zone": b.zone, "country": b.country,
                    "conso_reelle_l": _f(real), "est_estimation": bool(real is not None and (b.estimation or b.statut_bilan != "PRET_A_CERTIFIER")),
                    "conso_instrumentee_l": _f(instr), "conso_theorique_l": _f(theo),
                    "ecart_reel_instrumentee_l": g_i, "ecart_reel_instrumentee_pct": p_i,
                    "ecart_reel_theorique_l": g_t, "ecart_reel_theorique_pct": p_t, "score_confiance": b.score_confiance,
                    "statut_bilan": b.statut_bilan, "niveau_certification": b.niveau_certification, "motif": " ".join(motif) or None})
    return out
