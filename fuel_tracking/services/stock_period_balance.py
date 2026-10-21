# fuel_tracking/services/stock_period_balance.py
"""
Bilan stock par site et période EXACTE (observation_start → observation_end, jamais limité au mois civil).

    consommation_reelle_l = stock_initial_l + livraisons_externes_l + carte_total_l + transferts_entrants_l + rajouts_l
                            − transferts_sortants_l − retraits_l − vols_l − vidanges_l − stock_final_l

- Truck / Eydon et Carte Total sont des livraisons externes ; les Ponctions sont des transferts internes (impact net
  nul au niveau pays, réel au niveau site : −approuvé à la source, +approuvé au destinataire).
- NULL reste NULL : stock initial ou final absent → conso NULL, statut DONNEES_STOCK_INCOMPLETES.
- Stocks : relevés VALIDÉS déjà en base, litres natifs ou conversion cm → L avec une cuve validée (sinon
  CONVERSION_CUVE_MANQUANTE, aucun litre inventé). Priorité : 1. closing mensuel validé, 2. gardiennage validé
  (Loliforce / SCSS), 3. relevé ENOC qualifié (statut éligible), 4. autre source identifiée. Le relevé le plus proche
  de la borne départage une même priorité ; les candidats écartés sont conservés avec leur motif ; écart important
  entre candidats → alerte.
- Mouvements : comptés entre les deux relevés retenus (fenêtre effective), à l'intérieur de la période.
- Jamais « certifié » automatiquement : certification = action explicite du Fuel Manager (historique non modifiable).
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

RULE_VERSION = "bilan-v1"
D0 = Decimal("0")
SOURCE_RANK = {"CLOSING": 1, "LOLIFORCE": 2, "SCSS": 2, "ENOC": 3}
SOURCE_LABEL = {"CLOSING": "Closing mensuel validé", "LOLIFORCE": "Gardiennage Loliforce validé", "SCSS": "Gardiennage SCSS validé",
                "ENOC": "Relevé ENOC qualifié"}
HIGH_METHODS = ("DIRECT_LITERS", "CALIBRATED", "TANK_CURVE")
COMPONENTS = ("truck_l", "carte_total_l", "transferts_entrants_l", "transferts_sortants_l", "vols_l", "vidanges_l",
              "retraits_l", "rajouts_l", "livraisons_externes_l")
# Mouvements du registre hors ENOC (fichiers, saisies) → composante du bilan
OTHER_COMPONENT = {"DELIVERY": "livraisons_externes_l", "DELIVERY_EYDON": "livraisons_externes_l",
                   "DELIVERY_TOTAL_CARD": "carte_total_l", "ADD": "rajouts_l", "TRANSFER_IN": "transferts_entrants_l",
                   "TRANSFER_OUT": "transferts_sortants_l", "WITHDRAWAL": "retraits_l", "THEFT": "vols_l", "DRAIN": "vidanges_l"}
CERTIFIABLE = ("PRET_A_CERTIFIER", "PRET_A_CERTIFIER_AVEC_RESERVE")


class BalanceError(ValueError):
    pass


def window_hours() -> int:
    return int(getattr(settings, "FUEL_BALANCE_STOCK_WINDOW_H", 72))


def _f(v):
    return float(v) if v is not None else None


def _iso(v):
    return v.isoformat() if v else None


def _confidence(o: dict) -> str:
    if o["source_type"] in ("CLOSING", "LOLIFORCE", "SCSS") and o["conversion_method"] in HIGH_METHODS:
        return "HAUTE"
    if o["source_type"] == "ENOC":
        return "MOYENNE"
    return "FAIBLE"


def _candidate(o: dict, boundary, inside: bool) -> dict:
    """Relevé candidat, utilisable ou non (motif). La mesure brute (cm ou L) est toujours conservée."""
    reason = None
    if o["validation_status"] != "VALIDATED":
        reason = f"relevé non validé ({o['validation_status']})"
    elif o["source_type"] == "ENOC" and not o.get("eligible_for_stock"):
        reason = f"relevé ENOC non qualifié (statut {o.get('business_status') or 'inconnu'})"
    elif o["level_liters"] is None and (o["level_unit"] == "CM" or o["conversion_status"] == "PENDING_TANK_MAPPING"):
        reason = "CONVERSION_CUVE_MANQUANTE : niveau en cm sans modèle de cuve validé — aucun litre calculé"
    elif o["level_liters"] is None or o["conversion_status"] != "VALID":
        reason = f"conversion non retenue ({o['conversion_status']})"
    return {
        "observation_id": o["id"], "source": o["source_type"], "source_libelle": SOURCE_LABEL.get(o["source_type"], f"Autre source : {o['source_type']}"),
        "rang_priorite": SOURCE_RANK.get(o["source_type"], 4), "date": _iso(o["observation_datetime"]),
        "valeur_brute": o["level_raw"], "unite_origine": o["level_unit"], "niveau_cm": _f(o.get("level_cm")),
        "litres": _f(o["level_liters"]), "methode_conversion": o["conversion_method"], "modele_cuve": o.get("tank_model_ref"),
        "regle_conversion": o.get("conversion_rule"), "cuve": o.get("tank_id"), "confiance": _confidence(o),
        "dans_periode": inside, "ecart_borne_h": round(abs((o["observation_datetime"] - boundary).total_seconds()) / 3600, 1),
        "utilisable": reason is None, "motif_rejet": reason,
    }


def _select(obs: list[dict], boundary, lo, hi, prefer_inside_after: bool, tanks: list) -> tuple[dict | None, list[dict], list[str]]:  # noqa: C901
    """
    Candidats dans [lo, hi] ; on préfère les relevés DANS la période (après le début pour le stock initial,
    avant la fin pour le stock final), puis la priorité de source, puis la proximité de la borne.
    Site à plusieurs cuves validées : un relevé par cuve, même source, sommés.
    """
    alerts = []
    cands = []
    for o in obs:
        if not (lo <= o["observation_datetime"] <= hi):
            continue
        inside = o["observation_datetime"] >= boundary if prefer_inside_after else o["observation_datetime"] <= boundary
        cands.append(_candidate(o, boundary, inside))
    usable = [c for c in cands if c["utilisable"]]
    key = lambda c: (not c["dans_periode"], c["rang_priorite"], c["ecart_borne_h"])  # noqa: E731
    usable.sort(key=key)
    cands.sort(key=key)
    validated_tanks = [t for t in tanks if t.validated]
    chosen = None
    if usable and len(validated_tanks) > 1:
        best = usable[0]
        per_tank = {}
        for c in usable:
            if c["source"] == best["source"] and c["cuve"] and str(c["cuve"]).upper() not in per_tank:
                per_tank[str(c["cuve"]).upper()] = c
        missing = [t.tank_id for t in validated_tanks if str(t.tank_id).upper() not in per_tank]
        if missing:
            alerts.append(f"Site à {len(validated_tanks)} cuves : relevé {best['source_libelle']} manquant pour la cuve {', '.join(missing)}.")
        else:
            parts = list(per_tank.values())
            chosen = {**best, "litres": sum(p["litres"] for p in parts), "cuves": [p["cuve"] for p in parts],
                      "observation_ids": [p["observation_id"] for p in parts]}
    elif usable:
        chosen = usable[0]
    if chosen:
        others = [c for c in usable if c["source"] != chosen["source"]]
        cap = sum(float(t.check_capacity_l() or 0) for t in validated_tanks) or None
        tol = max(100.0, 0.10 * cap) if cap else 100.0
        for o in others:
            if abs(o["litres"] - chosen["litres"]) > tol and len(validated_tanks) <= 1:
                alerts.append(f"ECART_STOCK_CANDIDATS : {chosen['source_libelle']} {chosen['litres']:.0f} L vs {o['source_libelle']} "
                              f"{o['litres']:.0f} L (écart > {tol:.0f} L) — valeur retenue selon la priorité, à contrôler.")
        chosen["motif_selection"] = (
            f"{chosen['source_libelle']} (priorité {chosen['rang_priorite']}) — "
            + ("dans la période" if chosen["dans_periode"] else "hors période, faute de relevé dans la période")
            + f", à {chosen['ecart_borne_h']} h de la borne"
            + (f" ; {len(usable) - 1} autre(s) candidat(s) utilisable(s) écarté(s) par la priorité" if len(usable) > 1 else ""))
    return chosen, cands, alerts


def compute_balances(start, end, site_ids=None, persist=True) -> list[dict]:
    """Calcule (et enregistre) le bilan de chaque site pour la période exacte [start, end]."""
    from core.models import Site
    from fuel_tracking.models import FuelEnocQualifiedMovement as EQ, FuelMovement, FuelStockObservation as Obs, FuelTankMaster
    from fuel_tracking.services.site_energy_profile import energy_profiles
    from fuel_tracking.services.stock_registry import site_tanks

    if end <= start:
        raise BalanceError("La fin de période doit être postérieure au début.")
    w = timedelta(hours=window_hours())
    lo, hi = start - w, end + w

    obs_qs = Obs.objects.filter(observation_datetime__range=(lo, hi)).exclude(source_type="SNOWFLAKE")
    enoc_qs = EQ.objects.filter(Q(accounting_at__range=(lo, hi)) | Q(accounting_at__isnull=True, actual_operation_at__range=(lo, hi)))
    mv_qs = FuelMovement.objects.filter(movement_datetime__range=(lo, hi))
    if site_ids is not None:
        site_ids = list(site_ids)
        obs_qs = obs_qs.filter(site_id__in=site_ids)
        enoc_qs = enoc_qs.filter(Q(site_id_resolved__in=site_ids) | Q(ponction_source_site_resolved__in=site_ids))
        mv_qs = mv_qs.filter(site_id__in=site_ids)

    obs_by_site = defaultdict(list)
    for o in obs_qs.values("id", "site_id", "tank_id", "source_type", "observation_datetime", "level_raw", "level_value",
                           "level_unit", "level_cm", "level_liters", "conversion_method", "conversion_status", "conversion_rule",
                           "tank_model_ref", "validation_status", "eligible_for_stock", "business_status"):
        obs_by_site[o["site_id"]].append(o)
    enoc_by_site = defaultdict(list)
    for q in enoc_qs:
        for sid in {q.site_id_resolved, q.ponction_source_site_resolved if q.type_family == "PONCTION" else None} - {None}:
            enoc_by_site[sid].append(q)
    mv_by_site = defaultdict(list)
    for m in mv_qs:
        mv_by_site[m.site_id].append(m)

    if site_ids is None:
        tank_sites = set(FuelTankMaster.objects.filter(active=True, validation_status="VALIDEE").values_list("site_id", flat=True))
        site_ids = sorted(set(obs_by_site) | set(enoc_by_site) | set(mv_by_site) | tank_sites)
    sites = {s["site_id"]: s for s in Site.objects.filter(site_id__in=site_ids).values("site_id", "name", "zone", "country",
                                                                                         "installed_typology")}
    profiles = energy_profiles(site_ids)
    tanks = site_tanks(site_ids)
    rows = [_site_balance(sid, start, end, w, obs_by_site.get(sid, []), enoc_by_site.get(sid, []), mv_by_site.get(sid, []),
                          tanks.get(sid, []), sites.get(sid, {}), profiles.get(sid, {})) for sid in site_ids]
    if persist:
        _persist(rows, start, end)
    return rows


def _site_balance(site_id, start, end, w, obs, enoc, movements, tanks, site, profile) -> dict:
    from datetime import datetime

    motifs, alerts, reserves = [], [], []
    # Stock initial : relevé autour du début (± fenêtre), de préférence après le début ; final : autour de la fin.
    ini, ini_cands, a1 = _select(obs, start, start - w, min(start + w, end), True, tanks)
    fin, fin_cands, a2 = _select(obs, end, max(end - w, start), end + w, False, tanks)
    alerts += a1 + a2
    if ini and fin and datetime.fromisoformat(fin["date"]) <= datetime.fromisoformat(ini["date"]):
        motifs.append("Le relevé final retenu n'est pas postérieur au relevé initial : bilan impossible.")
        fin = None
    if not ini:
        motifs.append("Stock initial absent : aucun relevé validé et converti en litres autour du début de période"
                      + (" (relevé(s) présent(s) mais non utilisables, voir candidats)." if ini_cands else "."))
    if not fin:
        motifs.append("Stock final absent : aucun relevé validé et converti en litres autour de la fin de période"
                      + (" (relevé(s) présent(s) mais non utilisables, voir candidats)." if fin_cands else "."))
    if any(c["motif_rejet"] and "CONVERSION_CUVE_MANQUANTE" in c["motif_rejet"] for c in ini_cands + fin_cands):
        alerts.append("CONVERSION_CUVE_MANQUANTE : relevé(s) en cm sans modèle de cuve validé rattaché au site.")
    for c, label in ((ini, "initial"), (fin, "final")):
        if c and (c["rang_priorite"] >= 3 or not c["dans_periode"] or c["confiance"] != "HAUTE"):
            reserves.append(f"Stock {label} : {c['source_libelle']}, confiance {c['confiance']}"
                            + ("" if c["dans_periode"] else ", relevé hors période") + ".")

    t0 = datetime.fromisoformat(ini["date"]) if ini else start
    t1 = datetime.fromisoformat(fin["date"]) if fin else end

    comps = {k: D0 for k in COMPONENTS}
    counted, excluded = [], []
    unknown = pending_fm = 0
    integrated_count = historical = 0

    def inside(dt):
        return dt is not None and t0 < dt <= t1

    for q in enoc:
        when = q.accounting_at or q.actual_operation_at
        if not inside(when):
            continue
        ref = {"source": "ENOC", "reference": q.enoc_reference, "date": _iso(when), "type": q.raw_movement_type,
               "statut_brut": q.raw_status, "statut_metier": q.business_status, "qualite": q.quality_status,
               "date_source": q.date_source_used, "lien_enoc_id": q.pk}
        if q.integration_status == "INTEGRE":
            if q.type_family == "PONCTION":
                if q.fuel_manager_alert == "EN_ATTENTE":
                    pending_fm += 1
                continue  # compté via ses deux écritures de transfert (registre)
            if q.site_id_resolved != site_id or q.integrated_quantity_l is None:
                continue
            comp = "carte_total_l" if q.type_family == "TOTAL_CARD" else "truck_l"
            comps[comp] += q.integrated_quantity_l
            if comp == "truck_l":
                comps["livraisons_externes_l"] += q.integrated_quantity_l
            integrated_count += 1
            if q.quality_status != "COMPLET":
                reserves.append(f"{q.raw_movement_type} {q.enoc_reference} : qualité {q.quality_status}.")
            counted.append({**ref, "composante": comp, "quantite_l": _f(q.integrated_quantity_l)})
            continue
        if q.business_status == "HISTORIQUE_NON_VERIFIABLE":
            historical += 1
            excluded.append({**ref, "motif": "Historique non vérifiable (ancien connecteur) : exclu du bilan."})
            continue
        if q.execution_state in ("DEMANDE", "APPROUVEE", "REJETEE"):
            continue  # demande non exécutée ou rejet : pas de mouvement de stock
        if q.type_family != "PONCTION" and q.site_id_resolved != site_id:
            continue
        unknown += 1
        excluded.append({**ref, "motif": f"Mouvement exécuté non intégrable ({q.non_integration_cause}) : impact inconnu, à contrôler."})

    for m in movements:
        if not inside(m.movement_datetime):
            continue
        base = {"source": m.source_type, "reference": m.transfer_id or m.source_event_id, "date": _iso(m.movement_datetime),
                "type": m.movement_type, "qualite": m.quality_status}
        if m.source_type == "ENOC":
            if m.movement_type == "TRANSFERT_INTERSITE" and m.retained_in_stock:
                comp = "transferts_entrants_l" if m.movement_direction == "IN" else "transferts_sortants_l"
                comps[comp] += abs(m.impact_stock_l or D0)
                integrated_count += 1
                if m.quality_status and m.quality_status != "COMPLET":
                    reserves.append(f"Ponction {m.transfer_id} : qualité {m.quality_status}.")
                counted.append({**base, "composante": comp, "quantite_l": _f(m.impact_stock_l), "contrepartie": m.counterpart_site_id})
            continue
        comp = OTHER_COMPONENT.get(m.movement_type or "")
        if m.status == "REJECTED" or m.source_category in ("DEMANDE_LIVRAISON",):
            continue
        if m.retained_in_stock and m.status == "VALIDATED" and m.quantity_l is not None and comp:
            comps[comp] += abs(m.quantity_l)
            integrated_count += 1
            counted.append({**base, "composante": comp, "quantite_l": _f(m.quantity_l)})
        else:
            unknown += 1
            excluded.append({**base, "motif": "Mouvement sans quantité validée ou type non classifié : impact inconnu, à contrôler."})

    conso = None
    if ini and fin and not unknown:
        conso = (Decimal(str(ini["litres"])) + comps["livraisons_externes_l"] + comps["carte_total_l"] + comps["transferts_entrants_l"]
                 + comps["rajouts_l"] - comps["transferts_sortants_l"] - comps["retraits_l"] - comps["vols_l"] - comps["vidanges_l"]
                 - Decimal(str(fin["litres"])))
    if not (ini and fin):
        statut = "DONNEES_STOCK_INCOMPLETES"
    elif unknown:
        statut = "MOUVEMENTS_A_CONTROLER"
        motifs.append(f"{unknown} mouvement(s) exécuté(s) dont l'impact n'est pas établi : consommation réelle non calculée.")
    elif conso is not None and conso < 0:
        statut = "MOUVEMENTS_A_CONTROLER"
        motifs.append(f"Consommation négative ({conso:.0f} L) : mouvement manquant ou relevé erroné.")
    elif pending_fm:
        statut = "MOUVEMENTS_A_CONTROLER"
        motifs.append(f"{pending_fm} Ponction(s) en attente de contrôle Fuel Manager (écart de niveau).")
    elif historical and not integrated_count:
        statut = "HISTORIQUE_NON_VERIFIABLE"
        motifs.append(f"Seuls {historical} mouvement(s) historique(s) non vérifiable(s) sur la période (exclus du calcul).")
    elif reserves or alerts or historical:
        statut = "PRET_A_CERTIFIER_AVEC_RESERVE"
        if historical:
            reserves.append(f"{historical} mouvement(s) historique(s) non vérifiable(s) exclu(s).")
    else:
        statut = "PRET_A_CERTIFIER"
    days = (t1 - t0).total_seconds() / 86400 if (ini and fin) else None
    return {
        "country": site.get("country"), "site_id": site_id, "site_name": site.get("name"), "zone": site.get("zone"),
        "typologie": site.get("installed_typology"), "profil_energetique": profile.get("profil"),
        "observation_start": start, "observation_end": end,
        "stock_initial_l": Decimal(str(ini["litres"])) if ini else None, "source_stock_initial": ini["source"] if ini else None,
        "date_stock_initial": t0 if ini else None, "stock_initial_detail": ini or {},
        "stock_final_l": Decimal(str(fin["litres"])) if fin else None, "source_stock_final": fin["source"] if fin else None,
        "date_stock_final": t1 if fin else None, "stock_final_detail": fin or {},
        **comps, "consommation_reelle_l": conso,
        "conso_journaliere_l": (conso / Decimal(str(days))).quantize(Decimal("0.001")) if conso is not None and days and days > 0 else None,
        "statut_bilan": statut, "motifs": motifs + reserves, "alertes": alerts,
        "candidats": {"initial": ini_cands[:20], "final": fin_cands[:20]},
        "mouvements": counted, "mouvements_exclus": excluded,
        "periode_effective_debut": t0, "periode_effective_fin": t1, "rule_version": RULE_VERSION,
    }


FINGERPRINT_KEYS = ("stock_initial_l", "stock_final_l", "consommation_reelle_l", "source_stock_initial", "source_stock_final",
                    "date_stock_initial", "date_stock_final", *COMPONENTS)


def fingerprint(row: dict) -> str:
    payload = {k: str(row.get(k)) if row.get(k) is not None else None for k in FINGERPRINT_KEYS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _persist(rows: list[dict], start, end):
    from fuel_tracking.models import FuelStockBalance as B

    existing = {b.site_id: b for b in B.objects.filter(observation_start=start, observation_end=end)
                .prefetch_related("certifications")}
    now = timezone.now()
    with transaction.atomic():
        for r in rows:
            fp = fingerprint(r)
            b = existing.get(r["site_id"])
            values = {**r, "fingerprint": fp, "calculated_at": now}
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
                 "source_stock_final", *COMPONENTS, "consommation_reelle_l", "statut_bilan", "motifs")


def certify(balance, user, decision: str, comment: str):
    """Certification explicite par le Fuel Manager. Commentaire obligatoire si réserve. Historique non modifiable."""
    from fuel_tracking.models import FuelStockBalanceCertification as C

    comment = (comment or "").strip()
    action = {"certifier": "CERTIFIE", "certifier_avec_reserve": "CERTIFIE_AVEC_RESERVE"}.get(decision)
    if action is None:
        raise BalanceError("Décision inconnue (certifier | certifier_avec_reserve).")
    if balance.statut_bilan not in CERTIFIABLE:
        raise BalanceError(f"Bilan au statut {balance.statut_bilan} : seul un bilan prêt à certifier peut être certifié.")
    if action == "CERTIFIE" and balance.statut_bilan != "PRET_A_CERTIFIER":
        raise BalanceError("Bilan avec réserve : utiliser « Certifier avec réserve » (commentaire obligatoire).")
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
    Consommation réelle (bilan stock) / instrumentée (Snowflake) / théorique (CPH) côte à côte, même période exacte.
    Aucun écart calculé si la consommation réelle est NULL.
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
        instr = r.get("conso_mesuree_l")
        theo = r.get("conso_theorique_l")
        motif = []
        if real is None:
            motif.append("Consommation réelle non calculée : aucun écart.")
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
                    "conso_reelle_l": _f(real), "conso_instrumentee_l": _f(instr), "conso_theorique_l": _f(theo),
                    "ecart_reel_instrumentee_l": g_i, "ecart_reel_instrumentee_pct": p_i,
                    "ecart_reel_theorique_l": g_t, "ecart_reel_theorique_pct": p_t,
                    "statut_bilan": b.statut_bilan, "niveau_certification": b.niveau_certification, "motif": " ".join(motif) or None})
    return out
