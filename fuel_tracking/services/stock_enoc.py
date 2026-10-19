# fuel_tracking/services/stock_enoc.py
"""
ENOC → registre stock, SANS nouvelle intégration : lit FuelEnocMovement, déjà alimentée toutes les
5 minutes par le connecteur existant (sync_enoc_fuel_movements → enoc_mongo_service.fetch_fuel_movements,
MongoDB ENOC en lecture seule). Aucune connexion ENOC supplémentaire n'est ouverte ici.

Correspondance :
  relevé de niveau (level_before / level_after + unité) → fuel_stock_observation (source ENOC,
      source_event_id = <request_code>:before | :after)
  mouvement / livraison                                → fuel_movement (source ENOC, source_event_id = request_code)
  photo / ticket / bon                                 → proof_reference / ticket_reference / delivery_note
  technicien                                           → source_user
  identifiant ENOC                                     → source_event_id (aucun doublon : contrainte unique)

Les quantités sont relues dans le payload brut (raw_payload) : une quantité absente reste NULL (la table
FuelEnocMovement met 0 par défaut, ce que le registre ne reprend pas).
Une mise à jour côté ENOC met à jour le registre en traçant l'ancienne valeur (fuel_registry_correction).
"""
from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from fuel_tracking.services import stock_import_files as F
from fuel_tracking.services import stock_registry as R
from fuel_tracking.services import tank_conversion as TC

SUPPLIER_KEYS = ("supplier", "fournisseur", "provider", "supplier_name", "delivery_company")
TICKET_KEYS = ("ticket", "ticket_number", "ticket_fms", "efms_ticket", "fms_ticket", "ticket_efms", "num_ticket")
DELIVERY_NOTE_KEYS = ("delivery_note_number", "delivery_note", "bon_livraison", "bl_number", "num_bl")
PROOF_KEYS = ("photo", "photos", "photo_url", "photos_urls", "images", "image", "attachments", "attachment",
              "proof", "proofs", "preuve", "pictures", "picture", "files", "documents")
DELIVERED_QTY_KEYS = ("quantity_added_liters", "delivered_quantity_liters", "quantity_delivered", "delivery_note_quantity_liters")


def _docs(raw: dict) -> list[dict]:
    out = [d for d in ((raw or {}).get("operation"), (raw or {}).get("request")) if isinstance(d, dict)]
    out.append(raw or {})
    return out


def first_value(raw: dict, keys) -> object | None:
    for doc in _docs(raw):
        for k in keys:
            v = doc.get(k)
            if v not in (None, "", [], {}):
                return v
    return None


def proof_value(raw: dict) -> str | None:
    v = first_value(raw, PROOF_KEYS)
    if v is None:
        return None
    if isinstance(v, list):
        items = [x.get("url") or x.get("path") or x.get("name") or x.get("_id") if isinstance(x, dict) else x for x in v]
        return ", ".join(str(x) for x in items if x)[:512] or None
    if isinstance(v, dict):
        return str(v.get("url") or v.get("path") or v.get("name") or v)[:512]
    return str(v)[:512]


def unit_of(u) -> str | None:
    s = str(u or "").strip().lower()
    if s in ("l", "litre", "litres", "liter", "liters", "lt"):
        return "L"
    if s in ("cm", "centimetre", "centimetres", "centimeter", "centimeters"):
        return "CM"
    return None


def raw_key(m) -> str:
    """Valeur brute ENOC normalisée (clé de la configuration de classification)."""
    v = re.sub(r"\s+", " ", str(m.operation_type or "").strip().upper())
    return v or "(VIDE)"


def suggest_category(m, supplier: str | None) -> str:
    """Suggestion affichée à l'administrateur (JAMAIS appliquée sans configuration explicite)."""
    op = re.sub(r"[^A-Z]", "", str(m.operation_type or "").upper())
    sup = str(supplier or "").upper()
    if "PONCTION" in op or "TRANSFER" in op:
        return "TRANSFERT_SORTANT"
    if op == "VOL" or "THEFT" in op:
        return "VOL"
    if "VIDANGE" in op or "DRAIN" in op:
        return "VIDANGE"
    if "RETRAIT" in op or "WITHDRAW" in op:
        return "RETRAIT"
    if "TOTAL" in sup or "CARTE" in op:
        return "CARTE_TOTAL"
    if "LIVRAISON" in op or "DELIVERY" in op or "EYDON" in sup:
        return "LIVRAISON_CONFIRMEE"
    if any(k in op for k in ("AJOUT", "ADD", "APPOINT", "REFUEL", "RAVITAILLEMENT", "REMPLISSAGE")):
        return "AJOUT"
    if "RELEVE" in op or "JAUGE" in op or "LEVEL" in op:
        return "RELEVE_STOCK"
    return "INCONNU"


def active_mappings() -> dict:
    from fuel_tracking.models import FuelEnocTypeMapping

    return {m.raw_value: m for m in FuelEnocTypeMapping.objects.filter(active=True)}


MOVEMENT_OF = {"AJOUT": "ADD", "TRANSFERT_ENTRANT": "TRANSFER_IN", "TRANSFERT_SORTANT": "TRANSFER_OUT", "RETRAIT": "WITHDRAWAL",
               "VOL": "THEFT", "VIDANGE": "DRAIN", "CARTE_TOTAL": "DELIVERY_TOTAL_CARD"}
REAL_MOVEMENTS = {"LIVRAISON_CONFIRMEE", "AJOUT", "TRANSFERT_ENTRANT", "TRANSFERT_SORTANT", "RETRAIT", "VOL", "VIDANGE"}


def classify(m, supplier: str | None, has_operation: bool, mappings: dict) -> tuple[str, str]:
    if not has_operation:
        return "DEMANDE_LIVRAISON", "Demande ENOC sans opération réalisée (règle fixe : une demande n'est jamais une livraison)"
    key = raw_key(m)
    mp = mappings.get(key)
    if mp is None:
        return "INCONNU", f"Type ENOC « {key} » non configuré (file d'exceptions)"
    return mp.category, f"Configuration ENOC « {key} » → {mp.category} (version {mp.version})"


STATUS_MAP = {"done": "CLOSED", "rejected": "REJECTED", "pending": "DECLARED", "validated": "DECLARED"}


QUALIFIED_CATEGORY = {
    "DEMANDE_CARTE_TOTAL": "DEMANDE_LIVRAISON", "RAVITAILLEMENT_DECLARE_AVEC_PREUVE": "CARTE_TOTAL",
    "RAVITAILLEMENT_DECLARE_INCOMPLET": "CARTE_TOTAL", "LIVRAISON_A_CONFIRMER": "LIVRAISON_DECLAREE",
    "LIVRAISON_DECLAREE_A_RAPPROCHER": "LIVRAISON_DECLAREE", "PONCTION_A_QUALIFIER": "INCONNU", "TYPE_NON_RECONNU": "INCONNU",
}


def _apply_qualification(fields: dict, q) -> dict:
    """Le registre suit la qualification canonique : impact stock = ÉLIGIBLE_STOCK_RÉEL seulement (jamais le type brut)."""
    if q is None:
        fields["retained_in_stock"] = False
        return fields
    eligible = q.execution_state == "ELIGIBLE_STOCK_REEL"
    fields["retained_in_stock"] = bool(q.stock_impact and eligible)
    if eligible:
        fields["source_category"], fields["movement_type"] = "LIVRAISON_CONFIRMEE", "DELIVERY"
    elif q.business_classification == "REJETEE":
        fields["status"] = "REJECTED"
    elif q.execution_state in ("DEMANDE", "APPROUVEE"):
        fields["source_category"], fields["movement_type"] = "DEMANDE_LIVRAISON", None
    elif q.type_family in ("PONCTION", "AUTRE") and fields["source_category"] not in ("INCONNU", "DEMANDE_LIVRAISON"):
        pass  # sens configuré par l'administrateur (classification versionnée) conservé ; impact stock toujours NON
    else:
        fields["source_category"] = QUALIFIED_CATEGORY.get(q.business_classification, fields["source_category"])
        fields["movement_type"] = "DELIVERY_TOTAL_CARD" if fields["source_category"] == "CARTE_TOTAL" else None
    fields["quantity_l"] = q.actual_quantity_l
    fields["mapping_rule"] = f"{fields['mapping_rule']} · {q.execution_state} : {q.reason}"[:255]
    fields["proof_reference"] = ", ".join(q.proof_urls_or_ids or [])[:512] or fields.get("proof_reference")
    keep_unknown = q.type_family in ("PONCTION", "AUTRE")  # seuls ces types restent « à classifier »
    fields["alerts"] = [a for a in fields["alerts"] if keep_unknown or a["code"] != "ENOC_TYPE_UNKNOWN"] + \
        [{"code": f"ENOC_{q.execution_state}", "message": q.reason}]
    return fields


def _movement_fields(m, mappings: dict | None = None) -> dict:
    raw = m.raw_payload or {}
    supplier = first_value(raw, SUPPLIER_KEYS)
    supplier = str(supplier).strip()[:128] if supplier else None
    has_operation = bool(raw.get("operation")) or (raw.get("request") is None and m.status == "done")
    category, rule = classify(m, supplier, has_operation, mappings if mappings is not None else active_mappings())
    if category == "LIVRAISON_CONFIRMEE":
        mtype = "DELIVERY_EYDON" if "EYDON" in str(supplier or "").upper() else "DELIVERY"
    else:
        mtype = MOVEMENT_OF.get(category)
    qty = F.parse_number(first_value(raw, DELIVERED_QTY_KEYS)) if has_operation else None
    requested = F.parse_number(raw.get("approved_quantity_liters")) or F.parse_number(raw.get("requested_quantity_liters"))
    ticket = first_value(raw, TICKET_KEYS)
    note = first_value(raw, DELIVERY_NOTE_KEYS)
    proof = proof_value(raw)
    src_status = (m.status or "").lower() or None
    when = m.operation_date or m.done_at_source or m.created_at_source
    alerts = []
    if category == "INCONNU":
        alerts.append({"code": "ENOC_TYPE_UNKNOWN", "message": f"Type d'opération ENOC non classifié : « {raw_key(m)} » (valeur brute)."})
    if category == "DEMANDE_LIVRAISON":
        alerts.append({"code": "DELIVERY_REQUESTED_ONLY", "message": f"Demande ENOC sans opération réalisée (statut {m.status}) : non retenue dans le stock."})
    if category == "CARTE_TOTAL":
        alerts.append({"code": "TOTAL_CARD", "message": "Carte Total : information de contrôle / preuve, jamais une livraison automatique."})
    if has_operation and qty is None and category in REAL_MOVEMENTS:
        alerts.append({"code": "MOVEMENT_WITHOUT_QUANTITY", "message": "Opération ENOC sans quantité."})
    if category in ("LIVRAISON_CONFIRMEE", "AJOUT") and has_operation:
        if not ticket and not note:
            alerts.append({"code": "DELIVERY_WITHOUT_TICKET", "message": "Livraison sans ticket ni bon de livraison."})
        if not proof:
            alerts.append({"code": "DELIVERY_WITHOUT_PROOF", "message": "Livraison sans photo / preuve."})
    retained = (category in REAL_MOVEMENTS and src_status != "rejected" and bool(m.site_id) and when is not None and qty is not None)
    if category == "LIVRAISON_CONFIRMEE" and not (ticket or note or proof):
        retained = False
    return dict(
        site_id=(m.site_id or "").strip()[:64], movement_type=mtype, movement_datetime=when,
        requested_quantity_l=TC.to_decimal(requested), quantity_l=TC.to_decimal(qty),
        level_before_raw=_raw_level(m.level_before, m.level_before_unit), level_after_raw=_raw_level(m.level_after, m.level_after_unit),
        hour_meter_before=m.hour_meter_before, hour_meter_after=m.hour_meter_after, supplier=supplier,
        ticket_reference=str(ticket)[:128] if ticket else None, delivery_note=str(note)[:128] if note else None,
        proof_reference=proof, source_user=(m.technician_name or m.done_by or m.created_by or None),
        source_status=src_status, status=STATUS_MAP.get(src_status or "", "DECLARED"), alerts=alerts,
        comment=(m.comment or None), counterpart_site_id=_counterpart(m),
        raw_operation_type=raw_key(m)[:128], source_category=category, mapping_rule=rule[:255], retained_in_stock=retained,
    )


def _counterpart(m) -> str | None:
    p = m.ponction if isinstance(m.ponction, dict) else {}
    site = str(m.site_id or "").upper()
    for k in ("source_site_id", "from_site_id", "site_source", "destination_site_id", "to_site_id", "site_destination"):
        v = p.get(k)
        if v and str(v).upper() != site:
            return str(v)[:64]
    return None


def _raw_level(v, unit) -> str | None:
    if v is None:
        return None
    return f"{float(v):g} {unit or '?'}"


TRACKED_MOVEMENT_FIELDS = ("site_id", "movement_type", "movement_datetime", "requested_quantity_l", "quantity_l",
                           "supplier", "ticket_reference", "delivery_note", "proof_reference", "source_status", "status",
                           "source_category", "mapping_rule", "retained_in_stock", "raw_operation_type")


def sync_enoc_to_registry(since=None, user=None, only_ids=None) -> dict:
    """Projette FuelEnocMovement dans le registre (mouvements + relevés). Idempotent."""
    from fuel_tracking.models import FuelEnocMovement, FuelImportBatch, FuelImportRow, FuelMovement, FuelStockObservation

    qs = FuelEnocMovement.objects.all().order_by("operation_date", "id")
    if since is not None:
        qs = qs.filter(updated_at__gte=since)
    if only_ids is not None:
        qs = qs.filter(pk__in=list(only_ids))
    resolver = R.SiteResolver()
    movements = list(qs)
    site_ids = {resolver.resolve(m.site_id) for m in movements} - {None}
    tanks = R.site_tanks(site_ids)
    existing_mv = {mv.source_event_id: mv for mv in FuelMovement.objects.filter(source_type="ENOC", source_event_id__in=[m.source_id for m in movements])}
    obs_ids = [f"{m.source_id}:{k}" for m in movements for k in ("before", "after")]
    existing_obs = {o.source_event_id: o for o in FuelStockObservation.objects.filter(source_type="ENOC", source_event_id__in=obs_ids)}
    stats = {"lus": len(movements), "mouvements_crees": 0, "mouvements_mis_a_jour": 0, "releves_crees": 0,
             "releves_mis_a_jour": 0, "inchanges": 0, "site_inconnu": 0, "codes": {}, "categories": {}}
    batch = None
    mappings = active_mappings()

    def get_batch():
        nonlocal batch
        if batch is None:
            batch = FuelImportBatch.objects.create(source_type="ENOC", file_name="FuelEnocMovement (connecteur ENOC existant)",
                                                   file_hash=f"enoc-{timezone.now():%Y%m%d%H%M%S%f}", origin="ENOC",
                                                   status="PROCESSING")
        return batch

    from fuel_tracking.models import FuelEnocQualifiedMovement
    from fuel_tracking.services.enoc_canonical import sync_qualified

    stats["qualification"] = sync_qualified(movements)
    qualified = {q.enoc_movement_id: q for q in FuelEnocQualifiedMovement.objects.filter(enoc_movement_id__in=[m.pk for m in movements])}
    with transaction.atomic():
        for m in movements:
            site_id = resolver.resolve(m.site_id)
            changed_any = False
            messages = []
            if site_id is None:
                stats["site_inconnu"] += 1
                messages.append({"code": "UNKNOWN_SITE", "message": f"Site ENOC « {m.site_id} » absent de Gestion des sites."})
            fields = _apply_qualification(_movement_fields(m, mappings), qualified.get(m.pk))
            stats["categories"][fields["source_category"]] = stats["categories"].get(fields["source_category"], 0) + 1
            if site_id:
                fields["site_id"] = site_id
            mv = existing_mv.get(m.source_id)
            row = None
            if site_id is None:
                if mv is None and not FuelImportRow.objects.filter(batch__source_type="ENOC", site_id_raw=(m.site_id or "")[:128],
                                                                   row_fingerprint=F.fingerprint("enoc", m.source_id)).exists():
                    FuelImportRow.objects.create(batch=get_batch(), sheet_name="ENOC", row_number=m.pk, raw_payload={"source_id": m.source_id, "site_id": m.site_id},
                                                 site_id_raw=(m.site_id or "")[:128], validation_status="REJECTED", messages=messages,
                                                 row_fingerprint=F.fingerprint("enoc", m.source_id))
                continue
            if mv is None:
                row = _row(get_batch(), m, site_id, fields["alerts"])
                mv = FuelMovement.objects.create(source_type="ENOC", source_event_id=m.source_id[:128], batch=get_batch(), import_row=row, **fields)
                stats["mouvements_crees"] += 1
                changed_any = True
            else:
                diffs = {f: (getattr(mv, f), fields[f]) for f in TRACKED_MOVEMENT_FIELDS if str(getattr(mv, f) or "") != str(fields[f] or "")
                         and not (f.endswith("_l") and _same_num(getattr(mv, f), fields[f]))
                         and not (f == "movement_datetime" and getattr(mv, f) == fields[f])}
                if diffs:
                    for f, (old, new) in diffs.items():
                        R.log_correction("MOVEMENT", mv.pk, "SYSTEM", "Mise à jour à la source ENOC", site_id=site_id, field=f,
                                         old=old, new=new, author_label="ENOC")
                    if mv.status == "VALIDATED" and "quantity_l" in diffs:
                        fields["status"] = "DECLARED"
                        fields["validation_reason"] = "Quantité modifiée côté ENOC après validation : à revalider."
                    elif mv.status in ("VALIDATED",):
                        fields.pop("status")
                    for f, v in fields.items():
                        setattr(mv, f, v)
                    mv.save()
                    stats["mouvements_mis_a_jour"] += 1
                    changed_any = True
            for code in (a["code"] for a in fields["alerts"]):
                stats["codes"][code] = stats["codes"].get(code, 0) + 1

            # Relevés de niveau avant / après
            for kind, value, unit, when, ok_status in (
                ("before", m.level_before, m.level_before_unit, m.created_at_source or m.operation_date, ("validated", "done")),
                ("after", m.level_after, m.level_after_unit, m.operation_date or m.done_at_source, ("done",)),
            ):
                if value is None or when is None:
                    continue
                event_id = f"{m.source_id}:{kind}"
                u = unit_of(unit)
                flags = []
                if (m.status or "").lower() not in ok_status:
                    flags.append(R.flag("ENOC_NOT_CLOSED", f"Opération ENOC au statut « {m.status} » : relevé à valider."))
                extra = [] if u else [{"code": "UNKNOWN_UNIT", "message": f"Unité ENOC inconnue : {unit!r}."}]
                inp = R.ObservationInput(
                    source_type="ENOC", source_event_id=event_id, site_id=site_id, observation_datetime=when,
                    level_raw=f"{value} {unit or ''}".strip(), level_value=float(value), level_unit=u, tank_id=None,
                    hour_meter=float(m.hour_meter_before if kind == "before" else m.hour_meter_after) if (m.hour_meter_before if kind == "before" else m.hour_meter_after) is not None else None,
                    source_user=m.technician_name or (m.created_by if kind == "before" else m.done_by),
                    source_reference=f"ENOC {m.request_code or m.source_id} ({'avant' if kind == 'before' else 'après'} opération)",
                    proof_reference=fields["proof_reference"], extra_messages=extra, review_flags=flags,
                )
                new, _msgs = R.build_observation(inp, tanks.get(site_id, []), None)
                old = existing_obs.get(event_id)
                if old is None:
                    new.batch = get_batch()
                    new.import_row = row or _row(get_batch(), m, site_id, fields["alerts"])
                    row = new.import_row
                    new.save()
                    stats["releves_crees"] += 1
                    changed_any = True
                else:
                    key_old = (R._f(old.level_value), old.level_unit, old.observation_datetime, R._flags(old.review_flags))
                    key_new = (R._f(new.level_value), new.level_unit, new.observation_datetime, R._flags(new.review_flags))
                    if key_old != key_new:
                        R.log_correction("OBSERVATION", old.pk, "SYSTEM", "Mise à jour à la source ENOC", site_id=site_id,
                                         field="level/unit/datetime/flags", old=key_old, new=key_new, author_label="ENOC")
                        for f in ("level_raw", "level_value", "level_unit", "level_liters", "conversion_method", "conversion_status",
                                  "conversion_message", "capacity_l", "hour_meter", "source_user", "proof_reference",
                                  "observation_datetime", "review_flags", "validation_status", "validation_reason",
                                  "volume_geometric_l", "volume_calibrated_l", "fill_pct", "conversion_rule", "tank_model_ref",
                                  "issue_codes", "level_cm"):
                            setattr(old, f, getattr(new, f))
                        old.validated_by = None
                        old.validated_at = None
                        old.save()
                        stats["releves_mis_a_jour"] += 1
                        changed_any = True
            if not changed_any:
                stats["inchanges"] += 1
        if batch is not None:
            from fuel_tracking.services.stock_import_pipeline import _decide_status, _finalize_counts

            _finalize_counts(batch)
            batch.status, batch.status_message = _decide_status(batch)
            batch.summary = stats
            batch.processed_at = timezone.now()
            batch.save()
    stats["lot"] = batch.pk if batch else None
    return stats


def _same_num(a, b) -> bool:
    try:
        return a is not None and b is not None and abs(float(a) - float(b)) < 1e-6
    except (TypeError, ValueError):
        return False


def _row(batch, m, site_id, alerts):
    from fuel_tracking.models import FuelImportRow

    raw = m.raw_payload or {}
    keep = {k: raw.get(k) for k in ("source_id", "request_code", "status", "site_id", "operation_type", "operation_date",
                                    "requested_quantity_liters", "approved_quantity_liters", "quantity_added_liters",
                                    "level_before", "level_before_unit", "level_after", "level_after_unit", "technician_name")}
    status = "WARNING" if alerts else "ACCEPTED"
    if any(a["code"] in ("DELIVERY_REQUESTED_ONLY", "MISSING_MOVEMENT_TYPE") for a in alerts):
        status = "PENDING"
    return FuelImportRow.objects.create(batch=batch, sheet_name="ENOC", row_number=m.pk, raw_payload=keep,
                                        site_id_raw=(m.site_id or "")[:128], site_id_resolved=site_id,
                                        event_datetime=m.operation_date, validation_status=status, messages=alerts,
                                        row_fingerprint=F.fingerprint("enoc", m.source_id))


# ── Audit Eydon (preuve sur les données réelles) ─────────────────────────────

EYDON_FIELDS = {
    "site": ("site_id",),
    "date/heure": ("operation_date", "date_refueling"),
    "quantité livrée": DELIVERED_QTY_KEYS,
    "fournisseur": SUPPLIER_KEYS,
    "ticket": TICKET_KEYS,
    "bon de livraison": DELIVERY_NOTE_KEYS,
    "preuve (photo)": PROOF_KEYS,
    "technicien": ("technician_name", "created_by"),
    "statut": ("status",),
    "commentaire": ("comment",),
}


def audit_enoc_delivery_fields() -> dict:
    """Taux de remplissage des champs nécessaires aux livraisons Eydon dans les mouvements ENOC synchronisés."""
    from fuel_tracking.models import FuelEnocMovement

    total = 0
    with_operation = 0
    filled = {k: 0 for k in EYDON_FIELDS}
    op_types: dict[str, int] = {}
    statuses: dict[str, int] = {}
    suppliers: dict[str, int] = {}
    keys_seen: dict[str, int] = {}
    for m in FuelEnocMovement.objects.all().iterator():
        total += 1
        raw = m.raw_payload or {}
        if raw.get("operation"):
            with_operation += 1
        op_types[str(m.operation_type)] = op_types.get(str(m.operation_type), 0) + 1
        statuses[str(m.status)] = statuses.get(str(m.status), 0) + 1
        sup = first_value(raw, SUPPLIER_KEYS)
        if sup:
            suppliers[str(sup)] = suppliers.get(str(sup), 0) + 1
        for doc in _docs(raw)[:2]:
            for k in doc:
                keys_seen[k] = keys_seen.get(k, 0) + 1
        for label, keys in EYDON_FIELDS.items():
            if first_value(raw, keys) not in (None, ""):
                filled[label] += 1
    return {"total": total, "avec_operation_realisee": with_operation, "champs_renseignes": filled,
            "types_operation": op_types, "statuts": statuses, "fournisseurs": suppliers,
            "cles_mongo_observees": dict(sorted(keys_seen.items(), key=lambda kv: -kv[1]))}


# ── Dictionnaire réel des types ENOC + configuration versionnée ──────────────

def enoc_type_dictionary() -> list[dict]:
    """Valeurs brutes réellement présentes dans FuelEnocMovement, avec la classification courante et une suggestion."""
    from fuel_tracking.models import FuelEnocMovement, FuelEnocTypeMapping

    mappings = active_mappings()
    history: dict[str, int] = {}
    for raw_value in FuelEnocTypeMapping.objects.values_list("raw_value", flat=True):
        history[raw_value] = history.get(raw_value, 0) + 1
    out: dict[str, dict] = {}
    for m in FuelEnocMovement.objects.all().iterator():
        key = raw_key(m)
        raw = m.raw_payload or {}
        d = out.setdefault(key, {"raw_value": key, "count": 0, "statuts": {}, "avec_operation": 0, "suggestion": {},
                                 "exemples": []})
        d["count"] += 1
        d["statuts"][str(m.status)] = d["statuts"].get(str(m.status), 0) + 1
        if raw.get("operation") or (raw.get("request") is None and m.status == "done"):
            d["avec_operation"] += 1
        sup = first_value(raw, SUPPLIER_KEYS)
        sug = suggest_category(m, str(sup) if sup else None)
        d["suggestion"][sug] = d["suggestion"].get(sug, 0) + 1
        if len(d["exemples"]) < 3:
            d["exemples"].append(m.request_code or m.source_id)
    for key, mp in mappings.items():
        out.setdefault(key, {"raw_value": key, "count": 0, "statuts": {}, "avec_operation": 0, "suggestion": {}, "exemples": []})
    rows = []
    for key, d in sorted(out.items(), key=lambda kv: -kv[1]["count"]):
        mp = mappings.get(key)
        d["suggestion"] = max(d["suggestion"], key=d["suggestion"].get) if d["suggestion"] else None
        d["categorie"] = mp.category if mp else "INCONNU"
        d["version"] = mp.version if mp else None
        d["configure"] = mp is not None
        d["versions"] = history.get(key, 0)
        d["commentaire"] = mp.comment if mp else None
        rows.append(d)
    return rows


def set_enoc_mapping(raw_value: str, category: str, user=None, comment: str | None = None):
    """Nouvelle version de la classification d'une valeur ENOC (l'ancienne reste en historique, inactive)."""
    from django.db import transaction as tx

    from fuel_tracking.models import FuelEnocTypeMapping

    key = re.sub(r"\s+", " ", str(raw_value or "").strip().upper()) or "(VIDE)"
    if category not in FuelEnocTypeMapping.Category.values:
        raise ValueError(f"Catégorie inconnue : {category}")
    if not (comment or "").strip():
        raise ValueError("Motif obligatoire.")
    with tx.atomic():
        prev = FuelEnocTypeMapping.objects.filter(raw_value=key, active=True).first()
        version = (FuelEnocTypeMapping.objects.filter(raw_value=key).order_by("-version").values_list("version", flat=True).first() or 0) + 1
        if prev:
            prev.active = False
            prev.save(update_fields=["active"])
        new = FuelEnocTypeMapping.objects.create(raw_value=key, category=category, version=version, active=True,
                                                 comment=comment, created_by=user if (user and user.is_authenticated) else None)
        R.log_correction("ENOC_MAPPING", new.pk, "CORRECT", comment, field=key, old=prev.category if prev else None,
                         new=f"{category} (v{version})", user=user, author_label=None if user else "système")
    return new
