# fuel_tracking/services/stock_balance.py
"""
Bilan stock réel d'un site (formule inchangée) :

    conso_reelle = stock_initial + livraisons_eligibles + transferts_entree_eligibles
                   − transferts_sortie_eligibles − vols_valides − vidanges_valides − stock_final

Bornes = stock RETENU (relevé validé, converti avec une cuve validée) au début et à la fin de la période ; les mouvements
sont comptés strictement entre ces deux relevés. Seuls les mouvements ÉLIGIBLES au stock réel entrent dans le calcul.
Si une composante est inconnue — stock initial ou final absent, OU mouvement exécuté dans la fenêtre dont l'impact
n'est pas encore établi (ENOC à rapprocher / incomplet / nature à qualifier, mouvement historique sans quantité) —
le résultat est NULL, jamais 0. Les consommations théorique (CPH) et instrumentée (Snowflake) sont affichées à côté,
jamais fusionnées.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

D0 = Decimal("0")
SIGN = {"DELIVERY": "livraisons", "DELIVERY_EYDON": "livraisons", "DELIVERY_TOTAL_CARD": "livraisons", "ADD": "livraisons",
        "TRANSFER_IN": "transferts_entree", "TRANSFER_OUT": "transferts_sortie", "WITHDRAWAL": "transferts_sortie",
        "THEFT": "vols", "DRAIN": "vidanges"}


def _dt(d: date, end=False) -> datetime:
    return timezone.make_aware(datetime.combine(d, time.max if end else time.min))


def _f(v):
    return float(v) if v is not None else None


def site_balance(site_id: str, start: date, end: date, with_reference=True) -> dict:
    from fuel_tracking.models import FuelEnocQualifiedMovement, FuelMovement
    from fuel_tracking.services.stock_registry import fuel_stock_retained

    reasons, alerts = [], []
    ini = fuel_stock_retained([site_id], as_of=_dt(start, end=True)).get(site_id)  # dernier stock retenu au plus tard le 1er jour
    fin = fuel_stock_retained([site_id], as_of=_dt(end, end=True)).get(site_id)
    ini_ok = bool(ini and ini.get("stock_l") is not None)
    fin_ok = bool(fin and fin.get("stock_l") is not None)
    if not ini_ok:
        reasons.append("Stock initial inconnu : aucun stock retenu au début de la période.")
    if not fin_ok:
        reasons.append("Stock final inconnu : aucun stock retenu à la fin de la période.")
    t0 = ini["observation_datetime"] if ini_ok else _dt(start)
    t1 = fin["observation_datetime"] if fin_ok else _dt(end, end=True)
    if ini_ok and fin_ok and t1 <= t0:
        reasons.append("Le stock final retenu n'est pas postérieur au stock initial : aucun relevé validé dans la période.")

    comps = {"livraisons": D0, "transferts_entree": D0, "transferts_sortie": D0, "vols": D0, "vidanges": D0}
    counted, unknown = [], []
    # ENOC : seuls les mouvements INTÉGRÉS comptent, à leur date comptable (réelle, sinon validation) ;
    # un mouvement exécuté non intégrable dans la période rend le bilan inconnu (jamais 0).
    # Ponction intégrée : comptée via ses deux écritures de transfert ; non intégrée : inconnue aux DEUX sites.
    from django.db.models import Q as DQ

    from fuel_tracking.services.enoc_canonical import OPEN_STATUSES

    for q in FuelEnocQualifiedMovement.objects.filter(DQ(site_id_resolved=site_id) | DQ(ponction_source_site_resolved=site_id)):
        if q.integration_status == "INTEGRE":
            if q.type_family == "PONCTION":
                continue  # comptée via ses deux écritures de transfert (registre ci-dessous)
            when = q.accounting_at
            if q.site_id_resolved != site_id or when is None or not (t0 < when <= t1) or q.integrated_quantity_l is None:
                continue
            comps["livraisons"] += q.integrated_quantity_l
            counted.append({"source": "ENOC", "ref": q.enoc_reference, "date": when, "type": q.raw_movement_type,
                            "quantite_l": _f(q.integrated_quantity_l), "composante": "livraisons",
                            "certification": q.reconciliation_status, "qualite": q.quality_status,
                            "date_source": q.date_source_used})
            continue
        # Non intégré : impact inconnu seulement s'il a été exécuté (quantité / opération) et daté dans la période ;
        # une simple demande (jamais exécutée) n'a pas d'impact stock.
        if q.execution_state in ("REJETEE", "DEMANDE", "APPROUVEE"):
            continue
        if q.type_family == "PONCTION" and (q.raw_status or "").lower() in OPEN_STATUSES:
            continue
        when = q.accounting_at or q.actual_operation_at
        if when is None or not (t0 < when <= t1):
            continue
        if q.type_family != "PONCTION" and q.site_id_resolved != site_id:
            continue
        unknown.append({"source": "ENOC", "ref": q.enoc_reference, "date": when, "type": q.raw_movement_type,
                        "etat": q.integration_status, "motif": q.reason})
    # Transferts inter-sites ENOC (Ponction comptabilisée) : écriture signée du site (−source / +destinataire).
    for mv in FuelMovement.objects.filter(site_id=site_id, source_type="ENOC", movement_type="TRANSFERT_INTERSITE", retained_in_stock=True,
                                          movement_datetime__gt=t0, movement_datetime__lte=t1):
        comp = "transferts_entree" if mv.movement_direction == "IN" else "transferts_sortie"
        comps[comp] += abs(mv.impact_stock_l or D0)
        counted.append({"source": "ENOC", "ref": mv.transfer_id, "date": mv.movement_datetime, "type": f"TRANSFERT_INTERSITE {mv.movement_direction}",
                        "quantite_l": _f(mv.impact_stock_l), "composante": comp, "certification": "INTEGRE",
                        "qualite": mv.quality_status, "date_source": mv.date_source_used})
    # Autres mouvements du registre (historique refueling, saisies) : retenus et validés seulement.
    for mv in FuelMovement.objects.filter(site_id=site_id, movement_datetime__gt=t0, movement_datetime__lte=t1).exclude(source_type="ENOC"):
        comp = SIGN.get(mv.movement_type or "")
        if mv.status == "REJECTED" or mv.source_category in ("DEMANDE_LIVRAISON", "CARTE_TOTAL"):
            continue
        if mv.retained_in_stock and mv.status == "VALIDATED" and mv.quantity_l is not None and comp:
            comps[comp] += abs(mv.quantity_l)
            counted.append({"source": mv.source_type, "ref": mv.source_event_id, "date": mv.movement_datetime,
                            "type": mv.movement_type, "quantite_l": _f(mv.quantity_l), "composante": comp, "certification": "VALIDE"})
        else:
            unknown.append({"source": mv.source_type, "ref": mv.source_event_id, "date": mv.movement_datetime, "type": mv.movement_type,
                            "etat": mv.status, "motif": "Mouvement sans quantité réalisée validée : impact stock inconnu."})
    if unknown:
        reasons.append(f"{len(unknown)} mouvement(s) exécuté(s) dans la période dont l'impact stock n'est pas établi (à rapprocher / à qualifier).")

    conso = None
    if not reasons:
        conso = (Decimal(str(ini["stock_l"])) + comps["livraisons"] + comps["transferts_entree"] - comps["transferts_sortie"]
                 - comps["vols"] - comps["vidanges"] - Decimal(str(fin["stock_l"])))
        if conso < 0:
            alerts.append(f"Consommation négative ({conso:.0f} L) : mouvement manquant ou relevé erroné — bilan incohérent.")

    if conso is None:
        level = "NON_CALCULABLE"
    elif alerts:
        level = "INCOHERENT"
    elif all(c["certification"] in ("CERTIFIE", "VALIDE") for c in counted) and all(
            x["conversion_method"] in ("CALIBRATED", "DIRECT_LITERS", "TANK_CURVE") for x in (ini, fin)):
        level = "CERTIFIE"
    else:
        level = "CALCULE_NON_CERTIFIE"

    out = {
        "site_id": site_id, "periode": {"debut": start, "fin": end},
        "periode_effective": {"debut": t0 if ini_ok else None, "fin": t1 if fin_ok else None},
        "stock_initial": _stock(ini) if ini_ok else None, "stock_final": _stock(fin) if fin_ok else None,
        "composantes_l": {k: _f(v) for k, v in comps.items()},
        "mouvements_comptes": counted, "mouvements_inconnus": unknown[:50],
        "conso_reelle_l": _f(conso), "certification": level, "motifs": reasons, "alertes": alerts,
        "formule": "stock_initial + livraisons_eligibles + transferts_entree_eligibles − transferts_sortie_eligibles − vols_valides − vidanges_valides − stock_final",
    }
    if with_reference:
        out["references"] = reference_consumptions(site_id, start, end)
    return out


def _stock(x: dict) -> dict:
    return {"litres": _f(x.get("stock_l")), "date": x.get("observation_datetime"), "source": x.get("source_type"),
            "methode": x.get("conversion_method"), "releve_id": x.get("observation_id")}


def reference_consumptions(site_id: str, start: date, end: date) -> dict:
    """Consommation théorique CPH et instrumentée Snowflake sur la même période (affichées séparément, jamais fusionnées)."""
    out = {"conso_theorique_cph_l": None, "conso_instrumentee_snowflake_l": None, "statut_cph": None, "motif": None}
    try:
        from fuel_tracking.services.cph_service import compute_period

        if (end - start).days > 92:
            start = end - timedelta(days=92)
            out["motif"] = "Références limitées aux 93 derniers jours de la période."
        rows = compute_period(start, end, site_ids=[site_id], include_out_of_scope=False)
        r = next((x for x in rows if x.get("site_id") == site_id), None)
        if r:
            out.update(conso_theorique_cph_l=_f(r.get("conso_theorique_l")), conso_instrumentee_snowflake_l=_f(r.get("conso_mesuree_l")),
                       statut_cph=r.get("cph_status") or r.get("statut_cph"))
        else:
            out["motif"] = "Site hors périmètre du calcul CPH (pas de GE confirmé ou pas de données)."
    except Exception as e:  # une source indisponible ne casse pas le bilan stock
        out["motif"] = f"Références indisponibles ({type(e).__name__})."
    return out
