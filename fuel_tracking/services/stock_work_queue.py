# fuel_tracking/services/stock_work_queue.py
"""
« À traiter — actions requises » (Suivis Stock) : chaque compteur ouvre une liste filtrée dont chaque ligne
dit QUOI est en anomalie, QUELLE règle a échoué et QUELLE action faire — jamais un simple « à valider ».

Les relevés sont regroupés par site (dernier relevé concerné + nombre de relevés) pour que la liste reste
lisible ; les cuves et les mouvements sont listés un par un.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from fuel_tracking.services import stock_registry as R

STALE_DAYS = 7

CATEGORIES = [
    ("cuves_a_confirmer", "Cuves à confirmer", "Cuves proposées (Closing, refueling, modèle de la bibliothèque) ou capacité connue sans cotes."),
    ("cuves_incompletes", "Cuves incomplètes", "Aucune information cuve exploitable : forme, cotes et capacité à relever."),
    ("plusieurs_cuves", "Plusieurs cuves à identifier", "Sites à plusieurs cuves dont les cuves ou les relevés ne sont pas identifiés."),
    ("releves_non_convertibles", "Relevés cm non convertibles", "Relevés en cm sans cuve validée ou sans capacité de plaque confirmée."),
    ("releves_depassement", "Relevés dépassant la capacité", "Volume au-dessus de la capacité (+ 5 %) ou niveau au-dessus de la hauteur utile."),
    ("conflits_gardiennage", "Conflits Loliforce / SCSS", "Deux relevés à moins de 48 h dont l'écart dépasse max(100 L ; 10 % capacité)."),
    ("gardiennage_a_configurer", "Gardiennage à configurer", "Relevés de gardiennage d'un site sans périmètre configuré ou d'un autre prestataire."),
    ("mouvements_enoc_inconnus", "Mouvements ENOC non classifiés", "Type d'opération ENOC absent de la configuration (valeur brute visible)."),
    ("mouvements_sans_preuve", "Mouvements sans preuve", "Livraisons / appoints sans photo ni pièce justificative."),
    ("sites_sans_releve_recent", "Sites sans relevé récent", f"Dernier relevé de plus de {STALE_DAYS} jours."),
]

SOURCE_LABEL = {"ENOC": "ENOC", "LOLIFORCE": "Loliforce", "SCSS": "SCSS", "CLOSING": "Closing",
                "HISTORICAL_REFUELING": "Historique refueling", "MANUAL": "Saisie manuelle", "TERRAIN": "Terrain"}
SHAPE_LABEL = {"h-circ": "cylindre horizontal", "h-elli": "elliptique horizontal", "h-oblo": "oblongue horizontale",
               "v-cyl": "cylindre vertical", "v-tronc": "tronconique", "rect": "parallélépipède"}


def _f(v):
    return float(v) if v is not None else None


def _tank_raw(t) -> str:
    spec = R.TC.TankSpec.from_model(t)
    dims = " × ".join(f"{v:g}" for v in (spec.d1, spec.d2, spec.d3) if v)
    parts = [SHAPE_LABEL.get(spec.family or "", "forme inconnue")]
    if dims:
        parts.append(f"cotes {dims} cm")
    if t.capacity_plate_l:
        parts.append(f"plaque {float(t.capacity_plate_l):.0f} L")
    elif t.capacity_l:
        parts.append(f"capacité déclarée {float(t.capacity_l):.0f} L")
    if t.tank_model_id:
        parts.append(f"modèle {t.tank_model.tank_model_id} ({t.get_model_match_status_display().lower()})")
    return ", ".join(parts)


def _tank_item(t, anomaly: str, expected: str, action: str) -> dict:
    return {
        "site_id": t.site_id, "date": t.observed_at or t.created_at, "source": SOURCE_LABEL.get(t.source_type, t.source_type),
        "anomalie": anomaly, "valeur_brute": _tank_raw(t), "attendu": expected, "regle": t.validation_reason or "—",
        "action": action, "action_type": "OPEN_TANK", "ref": {"tank_id": t.id, "tank": t.tank_id}, "count": 1,
        "message": f"{t.site_id} — cuve {t.tank_id} ({_tank_raw(t)}) — {anomaly}. Action : {action}.",
    }


def _obs_item(o, codes: list[str], count: int) -> dict:
    main = R.issue_detail(codes[0]) if codes else R.issue_detail("REVIEW")
    unit = {"CM": "cm", "L": "L"}.get(o.level_unit or "", "")
    raw = f"{o.level_raw}{'' if not unit or unit in (o.level_raw or '') else ' ' + unit}" if o.level_raw else "—"
    src = SOURCE_LABEL.get(o.source_type, o.source_type)
    expected = f"capacité {float(o.capacity_l):.0f} L" if o.capacity_l else main["regle"]
    return {
        "site_id": o.site_id, "date": o.observation_datetime, "source": src, "anomalie": main["anomalie"],
        "valeur_brute": raw, "attendu": expected, "regle": main["regle"], "action": main["action"],
        "action_type": main["action_type"], "ref": {"observation_id": o.id}, "count": count,
        "codes": codes,
        "message": f"{o.site_id} — Relevé {src} : {raw} — {main['anomalie']}"
                   + (f" ({count} relevés concernés)" if count > 1 else "") + f". Action : {main['action'].lower()}.",
    }


def _grouped_obs(qs, code_filter=None) -> list[dict]:
    by_site = {}
    for o in qs.order_by("site_id", "-observation_datetime", "-id"):
        codes = [c for c in (o.issue_codes or [])]
        if code_filter and not (set(codes) & code_filter):
            continue
        cur = by_site.get(o.site_id)
        if cur is None:
            by_site[o.site_id] = [o, codes, 1]
        else:
            cur[2] += 1
    items = []
    for o, codes, n in by_site.values():
        if code_filter:
            codes = [c for c in codes if c in code_filter] + [c for c in codes if c not in code_filter]
        items.append(_obs_item(o, codes, n))
    return items


def build(category: str) -> list[dict]:
    from fuel_tracking.models import FuelMovement, FuelStockObservation as Obs, FuelTankMaster

    tanks = FuelTankMaster.objects.filter(active=True).select_related("tank_model")
    if category == "cuves_a_confirmer":
        items = []
        for t in tanks.filter(validation_status__in=["PROPOSEE", "A_CONFIRMER"]).order_by("site_id", "tank_id"):
            if t.validation_status == "A_CONFIRMER":
                items.append(_tank_item(t, "capacité connue mais forme / cotes insuffisantes", "forme + cotes reconnues par le calculateur",
                                        "Relever la forme et les cotes intérieures de la cuve (ou confirmer un modèle de la bibliothèque)"))
            else:
                items.append(_tank_item(t, "cuve proposée, non validée", "forme + cotes + capacité de plaque cohérente (≤ 5 %)",
                                        "Contrôler la cuve proposée puis la valider (ou la corriger)"))
        for t in tanks.filter(validation_status="VALIDEE", model_match_status="PROPOSED").order_by("site_id", "tank_id"):
            items.append(_tank_item(t, f"modèle {t.tank_model.tank_model_id} proposé (cotes compatibles), non confirmé",
                                    "modèle confirmé pour utiliser sa capacité de plaque",
                                    "Confirmer ou écarter le modèle proposé"))
        return items
    if category == "cuves_incompletes":
        return [_tank_item(t, "aucune information cuve exploitable", "forme, cotes et capacité de plaque",
                           "Renseigner la cuve du site (forme, cotes, capacité de plaque, preuve)")
                for t in tanks.filter(validation_status="INCOMPLETE").order_by("site_id", "tank_id")]
    if category == "plusieurs_cuves":
        items = [_tank_item(t, f"{t.number_of_tanks_on_site or 'plusieurs'} cuves mentionnées sans identifiant",
                            "une fiche par cuve, numérotée sur site", "Identifier chaque cuve du site (numéro, forme, cotes)")
                 for t in tanks.filter(validation_status="PLUSIEURS_CUVES_A_IDENTIFIER").order_by("site_id", "tank_id")]
        seen = {i["site_id"] for i in items}
        items += [i for i in _grouped_obs(Obs.objects.filter(validation_status="PENDING"), {"MULTI_TANK_WITHOUT_ID"})
                  if i["site_id"] not in seen]
        return items
    if category == "releves_non_convertibles":
        return _grouped_obs(Obs.objects.filter(validation_status="PENDING", level_unit="CM",
                                               conversion_status__in=["PENDING_TANK_MAPPING", "GEOMETRIC_ONLY"]))
    if category == "releves_depassement":
        return _grouped_obs(Obs.objects.filter(validation_status="REJECTED"), {"OVER_CAPACITY", "LEVEL_ABOVE_HEIGHT"})
    if category == "conflits_gardiennage":
        return _grouped_obs(Obs.objects.filter(validation_status="PENDING"), {"STOCK_CONFLICT"})
    if category == "gardiennage_a_configurer":
        return _grouped_obs(Obs.objects.filter(validation_status="PENDING"), {"NOT_CONFIGURED", "PROVIDER_MISMATCH"})
    if category == "mouvements_enoc_inconnus":
        out = []
        for m in FuelMovement.objects.filter(source_type="ENOC", source_category="INCONNU").order_by("-movement_datetime"):
            out.append({
                "site_id": m.site_id, "date": m.movement_datetime, "source": "ENOC", "anomalie": "type d'opération ENOC non classifié",
                "valeur_brute": f"type « {m.raw_operation_type} », statut {m.source_status or '—'}, quantité {_f(m.quantity_l) if m.quantity_l is not None else '—'} L",
                "attendu": "type configuré (livraison confirmée, demande, ajout, transfert…)", "regle": m.mapping_rule or "—",
                "action": f"Classifier le type ENOC « {m.raw_operation_type} »", "action_type": "MAP_ENOC",
                "ref": {"movement_id": m.id, "raw_value": m.raw_operation_type}, "count": 1,
                "message": f"{m.site_id} — Mouvement ENOC « {m.raw_operation_type} » non classifié. Action : classifier ce type.",
            })
        return out
    if category == "mouvements_sans_preuve":
        out = []
        for m in FuelMovement.objects.exclude(status__in=["REJECTED"]).order_by("-movement_datetime"):
            if not any(a.get("code") == "DELIVERY_WITHOUT_PROOF" for a in m.alerts or []) or m.proof_reference:
                continue
            qty = _f(m.quantity_l)
            out.append({
                "site_id": m.site_id, "date": m.movement_datetime, "source": SOURCE_LABEL.get(m.source_type, m.source_type),
                "anomalie": "livraison / appoint sans preuve",
                "valeur_brute": f"{m.get_movement_type_display() if m.movement_type else 'type inconnu'}, "
                                f"{'quantité ' + format(qty, '.0f') + ' L' if qty is not None else 'demandée ' + (format(_f(m.requested_quantity_l), '.0f') + ' L' if m.requested_quantity_l is not None else '—')}"
                                f", ticket {m.ticket_reference or '—'}",
                "attendu": "photo, bon de livraison ou référence de preuve", "regle": "Livraison confirmée : site, date, quantité et preuve",
                "action": "Joindre la preuve à la source (ENOC / fichier) ou rejeter le mouvement", "action_type": "OPEN_HISTORY",
                "ref": {"movement_id": m.id}, "count": 1,
                "message": f"{m.site_id} — {m.get_movement_type_display() if m.movement_type else 'Mouvement'} sans preuve. Action : joindre la preuve.",
            })
        return out
    if category == "sites_sans_releve_recent":
        limit = timezone.now() - timedelta(days=STALE_DAYS)
        latest = {}
        for o in Obs.objects.exclude(source_type__in=("SNOWFLAKE", "CLOSING")).order_by("site_id", "-observation_datetime", "-id"):
            latest.setdefault(o.site_id, o)
        out = []
        for sid, o in sorted(latest.items()):
            if o.observation_datetime >= limit:
                continue
            age = (timezone.now() - o.observation_datetime).days
            src = SOURCE_LABEL.get(o.source_type, o.source_type)
            out.append({
                "site_id": sid, "date": o.observation_datetime, "source": src, "anomalie": f"dernier relevé il y a {age} jours",
                "valeur_brute": o.level_raw or "—", "attendu": f"relevé de moins de {STALE_DAYS} jours",
                "regle": f"Fraîcheur ≤ {STALE_DAYS} jours (relevé ancien conservé, signalé)",
                "action": "Demander un relevé au prestataire / au technicien", "action_type": "OPEN_HISTORY",
                "ref": {"observation_id": o.id}, "count": 1,
                "message": f"{sid} — dernier relevé {src} il y a {age} jours. Action : demander un nouveau relevé.",
            })
        return out
    raise ValueError(f"Catégorie inconnue : {category}")


def counters() -> list[dict]:
    out = []
    for key, label, desc in CATEGORIES:
        out.append({"key": key, "label": label, "description": desc, "count": len(build(key))})
    return out
