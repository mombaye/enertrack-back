"""
Import « Remontées terrain mensuelles » (source TERRAIN_MENSUEL) — fichier COMPLÉMENTAIRE.

Règles (cahier des charges §4.3) :
  - niveaux en L ou cm (conversion de cuve par le moteur habituel, jamais ici) ; cellule vide = NULL, jamais 0 ;
  - unités inconnues, nombres impossibles (non numériques, négatifs) et sites inconnus signalés ligne par ligne ;
  - un relevé existant n'est jamais écrasé : même événement = doublon ignoré (idempotence par empreinte) ;
  - traçabilité : fichier, onglet, ligne, date d'import, utilisateur (lot) conservés ;
  - source NON prioritaire : rang « autre source », et les mouvements importés ne sont PAS retenus dans le stock
    (retained_in_stock = False, statut DECLARED) tant qu'un humain ne les a pas validés — aucun double comptage avec ENOC.
"""
from __future__ import annotations

import re

from fuel_tracking.services import stock_import_files as F
from fuel_tracking.services import stock_registry as R
from fuel_tracking.services import tank_conversion as TC

SOURCE = "TERRAIN_MENSUEL"
RULE = "terrain-mensuel-v1"

COLS = ("country", "site_id", "site_name", "observation_start", "observation_end", "observation_date", "tank_identifier",
        "stock_opening_value", "stock_opening_unit", "stock_closing_value", "stock_closing_unit", "delivery_l", "transfer_in_l",
        "transfer_out_l", "theft_l", "drain_l", "withdrawal_l", "generator_hour_meter", "comment", "justificatif",
        "observation_status", "source_operator", "source_file_reference")
MOVEMENTS = (("delivery_l", "DELIVERY", "Livraison"), ("transfer_in_l", "TRANSFER_IN", "Transfert entrant"),
             ("transfer_out_l", "TRANSFER_OUT", "Transfert sortant"), ("theft_l", "THEFT", "Vol"),
             ("drain_l", "DRAIN", "Vidange"), ("withdrawal_l", "WITHDRAWAL", "Retrait"))
UNITS = {"l": "L", "litre": "L", "litres": "L", "liter": "L", "liters": "L", "cm": "CM", "centimetre": "CM", "centimetres": "CM",
         "centimètre": "CM", "centimètres": "CM"}


def _key(h) -> str:
    return re.sub(r"[\s\-]+", "_", str(h or "").strip().lower())


def _num(v, field, messages, allow_negative=False):
    """(valeur | None, ok). Vide → (None, True) ; non numérique ou négatif → (None, False) avec message."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None, True
    x = F.parse_number(v)
    if x is None or (x < 0 and not allow_negative):
        messages.append({"code": "INVALID_NUMBER", "message": f"{field} : valeur impossible ({v!r})."})
        return None, False
    return x, True


def _header(row):
    keys = [_key(c) for c in row]
    return keys if "site_id" in keys and ("observation_start" in keys or "observation_end" in keys or "observation_date" in keys) else None


def process_terrain_file(batch, file_bytes: bytes, file_name: str) -> dict:
    from fuel_tracking.models import FuelImportRow, FuelMovement, FuelStockObservation as Obs
    from fuel_tracking.services.stock_file_analysis import plausible

    stats = {"observations_creees": 0, "mouvements_crees": 0, "doublons": 0, "codes": {}, "feuilles_ignorees": {}, "lignes_vides_ignorees": 0}
    resolver = R.SiteResolver()
    seen_obs = set(Obs.objects.filter(source_type=SOURCE).values_list("source_event_id", flat=True))
    seen_mv = set(FuelMovement.objects.filter(source_type=SOURCE).values_list("source_event_id", flat=True))
    tanks_cache: dict = {}
    any_sheet = False
    for sheet, rows in F.iter_sheets(file_bytes, file_name):
        header = None
        for n, row in enumerate(rows, start=1):
            if header is None:
                header = _header(row)
                continue
            if not row or all(c is None or (isinstance(c, str) and not c.strip()) for c in row):
                stats["lignes_vides_ignorees"] += 1
                continue
            any_sheet = True
            raw = {h: row[i] for i, h in enumerate(header) if h and i < len(row) and h in COLS}
            messages: list = []
            site_raw = str(raw.get("site_id") or "").strip()
            site_id = resolver.resolve(site_raw) if site_raw else None
            ir = FuelImportRow(batch=batch, sheet_name=sheet[:128], row_number=n, raw_payload={k: F._jsonable(v) for k, v in raw.items()},
                               site_id_raw=site_raw[:128], site_id_resolved=site_id)
            if site_id is None:
                messages.append({"code": "UNKNOWN_SITE", "message": f"Site « {site_raw or '(vide)'} » absent de Gestion des sites."})
                ir.validation_status, ir.messages = "REJECTED", messages
                ir.save()
                F._count(stats, messages)
                continue
            d_start, d_end, d_obs = (F.parse_date(raw.get(k)) for k in ("observation_start", "observation_end", "observation_date"))
            for label, dv, rv in (("observation_start", d_start, raw.get("observation_start")), ("observation_end", d_end, raw.get("observation_end")),
                                  ("observation_date", d_obs, raw.get("observation_date"))):
                if rv not in (None, "") and (dv is None or not plausible(dv)):
                    messages.append({"code": "INVALID_DATE", "message": f"{label} illisible ou invraisemblable ({rv!r})."})
                    if label == "observation_start":
                        d_start = None
                    elif label == "observation_end":
                        d_end = None
                    else:
                        d_obs = None
            tank = (str(raw.get("tank_identifier")).strip() or None) if raw.get("tank_identifier") is not None else None
            if site_id not in tanks_cache:
                tanks_cache[site_id] = R.site_tanks([site_id]).get(site_id, [])
            hm, _ok = _num(raw.get("generator_hour_meter"), "generator_hour_meter", messages)
            operator = (str(raw.get("source_operator")).strip()[:255] if raw.get("source_operator") else None)
            proof = (str(raw.get("justificatif")).strip()[:512] if raw.get("justificatif") else None)
            status_txt = str(raw.get("observation_status")).strip() if raw.get("observation_status") else None
            comment = " — ".join(x for x in (str(raw.get("comment")).strip() if raw.get("comment") else None,
                                             f"Statut terrain : {status_txt}" if status_txt else None) if x) or None
            ref = f"{file_name} · {sheet} · ligne {n}" + (f" · réf. {raw.get('source_file_reference')}" if raw.get("source_file_reference") else "")
            created, dup = [], 0
            ir.row_fingerprint = F.fingerprint(site_id, d_start, d_end, d_obs, tank, raw.get("stock_opening_value"), raw.get("stock_closing_value"))
            ir.validation_status = "PENDING"
            ir.save()
            # ── niveaux d'ouverture et de clôture
            for kind, vkey, ukey, d in (("opening", "stock_opening_value", "stock_opening_unit", d_start), ("closing", "stock_closing_value", "stock_closing_unit", d_end)):
                value, ok = _num(raw.get(vkey), vkey, messages)
                if value is None:
                    continue                       # vide = NULL : rien n'est créé, rien n'est inventé
                unit = UNITS.get(str(raw.get(ukey) or "").strip().lower())
                if unit is None:
                    messages.append({"code": "UNKNOWN_UNIT", "message": f"{ukey} : unité inconnue ({raw.get(ukey)!r}) — attendu L ou cm."})
                    continue
                if d is None:
                    messages.append({"code": "MISSING_DATE", "message": f"Date de {'début' if kind == 'opening' else 'fin'} d'observation absente : niveau non importé."})
                    continue
                event_id = "TM:" + F.fingerprint(SOURCE, site_id, d, tank, kind, value, unit)
                if event_id in seen_obs:
                    dup += 1
                    continue
                seen_obs.add(event_id)
                inp = R.ObservationInput(
                    source_type=SOURCE, source_event_id=event_id, site_id=site_id, observation_datetime=F.as_utc_datetime(d),
                    level_raw=f"{value:g} {unit.lower()}", level_value=value, level_unit=unit, tank_id=tank,
                    hour_meter=hm if kind == "closing" else None, source_user=operator, source_reference=ref, comment=comment, proof_reference=proof)
                obs, obs_msgs = R.build_observation(inp, tanks_cache[site_id], None)
                obs.batch, obs.import_row = batch, ir
                obs.save()
                stats["observations_creees"] += 1
                created.append(obs.validation_status)
                messages.extend(m for m in obs_msgs if m not in messages)
            # ── mouvements déclarés (complémentaires : jamais retenus dans le stock automatiquement)
            m_date = d_obs or d_end
            for field, mtype, label in MOVEMENTS:
                qty, ok = _num(raw.get(field), field, messages)
                if qty is None or qty == 0:
                    continue
                if m_date is None:
                    messages.append({"code": "MISSING_DATE", "message": f"{label} : date d'observation absente, mouvement non importé."})
                    continue
                event_id = "TM:" + F.fingerprint(SOURCE, site_id, d_start, d_end, d_obs, tank, mtype, qty)
                if event_id in seen_mv:
                    dup += 1
                    continue
                seen_mv.add(event_id)
                alerts = [{"code": "TERRAIN_COMPLEMENTAIRE", "message": "Mouvement déclaré dans le fichier terrain mensuel : complémentaire, non retenu dans le bilan "
                                                                          "tant qu'il n'est pas validé (aucun double comptage avec ENOC)."}]
                FuelMovement.objects.create(
                    source_type=SOURCE, source_event_id=event_id[:128], batch=batch, import_row=ir, site_id=site_id, tank_id=tank, movement_type=mtype,
                    movement_datetime=F.as_utc_datetime(m_date), quantity_l=TC.to_decimal(qty), hour_meter_after=TC.to_decimal(hm), source_user=operator,
                    source_status=status_txt, status="DECLARED", alerts=alerts, proof_reference=proof, comment=comment, raw_operation_type=f"{field}"[:128],
                    source_category="TERRAIN_COMPLEMENT", mapping_rule=RULE, retained_in_stock=False)
                stats["mouvements_crees"] += 1
                created.append("PENDING")
            if dup and not created:
                messages.append({"code": "DUPLICATE_ALREADY_IMPORTED", "message": "Ligne déjà importée (doublon technique) : ignorée."})
                stats["doublons"] += 1
            elif dup:
                stats["doublons"] += dup
            ir.validation_status = F._row_status(messages, created, bool(dup and not created))
            ir.messages = messages
            ir.save(update_fields=["validation_status", "messages"])
            F._count(stats, messages)
    if not any_sheet:
        stats["codes"]["NO_DATA_ROWS"] = 1
    return stats
