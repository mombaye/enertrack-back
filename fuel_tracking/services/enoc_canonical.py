# fuel_tracking/services/enoc_canonical.py
"""
Mouvements Fuel & GE ENOC → contrat canonique eGrid → qualification métier → rapprochement.

Source : FuelEnocMovement.raw_payload (documents Mongo ENOC fuel_requests / fuel_operations, lus par le connecteur
existant en lecture seule — jamais modifiés). Rien n'est deviné : chaque valeur canonique vient d'un champ brut
dont le chemin est conservé (field_sources) ; une valeur absente reste NULL (jamais 0).

Deux axes distincts (instruction « type brut ≠ impact stock ») :
  A. type brut ENOC  → type_family (TOTAL_CARD, PONCTION, TRUCK, AUTRE) + raw_movement_type tel quel ;
  B. état d'exécution / preuve → execution_state (DEMANDE, APPROUVEE, TERMINEE, QUANTITE_REELLE_MANQUANTE,
     PREUVE_MANQUANTE, A_RAPPROCHER, ELIGIBLE_STOCK_REEL, REJETEE, NATURE_A_QUALIFIER).

Aucun mouvement n'entre automatiquement dans le stock réel : ÉLIGIBLE_STOCK_RÉEL n'est atteint que par un
rapprochement explicite (reconcile), tracé (utilisateur, date, motif, élément de rapprochement).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

QUALIFICATION_VERSION = "enoc-q-v2"

# ── Champs bruts candidats (premier trouvé : opération, puis demande, puis payload aplati du connecteur) ─────
K = {
    "request_id": ("_id", "request_id"),
    "reference": ("request_code", "reference", "code"),
    "site_id": ("site_id",),
    "site_name": ("site_name",),
    "country": ("country", "pays", "country_code"),
    "zone": ("zone", "region"),
    "type": ("operation_type", "request_type", "type_operation", "type"),
    "requested": ("requested_quantity_liters", "quantity_requested", "requested_quantity", "requested_liters"),
    "approved": ("approved_quantity_liters", "validated_quantity_liters", "quantity_approved", "approved_quantity"),
    "actual": ("quantity_added_liters", "delivered_quantity_liters", "quantity_delivered", "actual_quantity_liters",
               "quantity_liters", "quantity_added", "quantity"),
    "unit": ("quantity_unit", "unit", "unite"),
    "planned": ("date_refueling", "planned_date", "scheduled_at", "date_prevue"),
    "actual_at": ("operation_date", "refueling_date", "date_operation", "date_ravitaillement", "done_at"),
    "requested_at": ("action.created_at", "created_at"),
    "approved_at": ("action.validated_at", "validated_at", "approved_at"),
    "closed_at": ("action.done_at", "done_at", "closed_at", "completed_at"),
    "level_before": ("level_before",),
    "level_before_unit": ("level_before_unit",),
    "level_after": ("level_after",),
    "level_after_unit": ("level_after_unit",),
    "level_before_l": ("level_before_liters", "level_before_l", "tank_level_before_l"),
    "level_after_l": ("level_after_liters", "level_after_l", "tank_level_after_l"),
    "level_before_cm": ("level_before_cm", "tank_level_before_cm"),
    "level_after_cm": ("level_after_cm", "tank_level_after_cm"),
    "hm_before": ("hour_meter_before", "hourmeter_before", "compteur_avant"),
    "hm_after": ("hour_meter_after", "hourmeter_after", "compteur_apres"),
    "technician": ("technician_name", "technicien"),
    "validator": ("action.validated_by", "validated_by", "validator_name"),
    "closer": ("action.done_by", "done_by", "closed_by", "created_by"),
    "comment": ("comment", "commentaire", "comments"),
    "supplier": ("supplier", "supplier_name", "fournisseur", "provider", "delivery_company"),
    "delivery_note": ("delivery_note_number", "delivery_note", "bon_livraison", "bl_number", "num_bl"),
    "vehicle": ("truck_reference", "truck_number", "truck", "vehicle", "vehicle_number", "plate", "plate_number",
                "immatriculation", "camion", "citerne"),
}
# Documents consultés et ordre de priorité par champ : la date réelle, la quantité réelle et le niveau après ne
# peuvent venir QUE de l'opération (exécution) ; la demande ne fournit jamais une valeur « réelle ».
ORDER_OP_ONLY = ("operation",)
ORDER_REQ_FIRST = ("request", "operation", "flat")
ORDER_OP_FIRST = ("operation", "request", "flat")
FIELD_ORDER = {
    "actual": ORDER_OP_ONLY, "actual_at": ORDER_OP_ONLY, "unit": ORDER_OP_FIRST,
    "level_after": ORDER_OP_FIRST, "level_after_unit": ORDER_OP_FIRST, "level_after_l": ORDER_OP_FIRST,
    "level_after_cm": ORDER_OP_FIRST, "hm_after": ORDER_OP_FIRST, "closer": ("request", "operation"),
    "request_id": ("request", "operation"), "requested_at": ("request",), "approved_at": ("request",),
    "planned": ("request",), "requested": ("request", "flat"), "approved": ("request", "flat"),
}
PROOF_KEY_RE = re.compile(r"(photo|image|picture|attach|proof|preuve|piece_jointe|pieces|document|upload|scan|fichier|files?$)", re.I)
CLOSED_STATUSES = {"done", "closed", "completed", "termine", "terminé", "cloture", "clôturé"}
APPROVED_STATUSES = {"validated", "approved", "approuve", "approuvé", "valide", "validé"}
OPEN_STATUSES = {"pending", "open", "created", "draft", "en_attente", "submitted"}
REJECTED_STATUSES = {"rejected", "cancelled", "canceled", "annule", "annulé", "refused", "refuse", "refusé"}
REASON_TC_OK = "Ravitaillement ENOC déclaré et justifié ; rapprochement stock requis"
REASON_TRUCK_OK = "Livraison Truck déclarée et justifiée ; rapprochement stock requis"
REASON_PONCTION = "Signification métier de Ponction non validée"
# Règle Ponction validée par Steve : site_id principal = DESTINATAIRE ; « Site source » (section Ponction) = SOURCE.
PONC = {
    "source_site": ("source_site_id", "site_source_id", "site_source", "source_site", "from_site_id", "site_id_source",
                    "origin_site_id", "site_origine", "source"),
    # Clés réelles ENOC (audit prod 10/2026) : level_before_ponction[_l|_cm], level_after_ponction[_l|_cm], level_unit
    "lvl_before": ("level_before_ponction", "source_level_before", "level_before_source", "niveau_avant_source",
                   "level_before", "niveau_avant"),
    "lvl_before_unit": ("level_unit", "source_level_before_unit", "level_before_source_unit", "level_before_unit",
                        "unit", "unite"),
    "lvl_after": ("level_after_ponction", "source_level_after", "level_after_source", "niveau_apres_source",
                  "level_after", "niveau_apres"),
    "lvl_after_unit": ("level_unit", "source_level_after_unit", "level_after_source_unit", "level_after_unit",
                       "unit", "unite"),
    "lvl_before_l": ("level_before_ponction_l", "level_before_ponction_liters", "source_level_before_liters",
                     "source_level_before_l", "level_before_liters", "level_before_l"),
    "lvl_after_l": ("level_after_ponction_l", "level_after_ponction_liters", "source_level_after_liters",
                    "source_level_after_l", "level_after_liters", "level_after_l"),
    "lvl_before_cm": ("level_before_ponction_cm", "source_level_before_cm", "level_before_cm"),
    "lvl_after_cm": ("level_after_ponction_cm", "source_level_after_cm", "level_after_cm"),
    "note": ("transfer_note_number", "transfer_note", "bon_transfert", "bon_de_transfert", "transfer_slip_number",
             "transfer_slip", "numero_bon", "bon_number", "reference_bon", "bon"),
}


def ponction_settings() -> dict:
    from django.conf import settings

    return {"gap_min_l": Decimal(str(getattr(settings, "FUEL_PONCTION_GAP_MIN_L", 20))),
            "gap_pct": Decimal(str(getattr(settings, "FUEL_PONCTION_GAP_PCT", 10)))}


def transfer_id_for(reference: str | None) -> str | None:
    return f"ENOC-PONCTION-{reference}" if reference else None


# ── Lecture brute ──────────────────────────────────────────────────────────────────────────────────────────
def is_legacy_rest(raw: dict) -> bool:
    """Ligne enregistrée par l'ancien connecteur API REST ENOC (GET /fuel/integrations/enertrack/operations) :
    raw_payload = l'élément REST lui-même (pas de documents Mongo request / operation séparés)."""
    raw = raw or {}
    return bool(raw) and "request" not in raw and "operation" not in raw


def _docs(raw: dict) -> dict[str, dict]:
    raw = raw or {}
    if is_legacy_rest(raw):
        return {"rest": raw, "request": {}, "operation": {}, "flat": {}}
    req = raw.get("request") if isinstance(raw.get("request"), dict) else None
    op = raw.get("operation") if isinstance(raw.get("operation"), dict) else None
    flat = {k: v for k, v in raw.items() if k not in ("request", "operation", "operations")}
    return {"request": req or {}, "operation": op or {}, "flat": flat}


def _get(doc: dict, path: str):
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() in ("", "NA", "N/A", "na", "n/a", "null", "None")) or v in ([], {})


def pick(docs: dict[str, dict], field: str) -> tuple[object | None, str | None]:
    """(valeur, chemin brut) du premier champ candidat non vide, dans l'ordre des documents autorisés."""
    order = ("rest",) if docs.get("rest") else FIELD_ORDER.get(field, ORDER_OP_FIRST)
    for name in order:
        doc = docs.get(name) or {}
        for key in K[field]:
            v = _get(doc, key)
            if not _empty(v):
                return v, f"{name}.{key}"
    return None, None


def num(v) -> Decimal | None:
    if _empty(v) or isinstance(v, bool):
        return None
    if isinstance(v, dict):
        v = v.get("$numberDecimal") or v.get("value")
    try:
        d = Decimal(str(v).replace(",", ".").replace(" ", "").replace(" ", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def dt(v):
    if _empty(v):
        return None
    if isinstance(v, dict):
        v = v.get("$date") or v.get("date")
    if isinstance(v, datetime):
        d = v
    else:
        s = str(v).strip()
        d = parse_datetime(s) or parse_datetime(s.replace("Z", "+00:00"))
        if d is None:
            for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"):
                try:
                    d = datetime.strptime(s[:19], fmt)
                    break
                except ValueError:
                    continue
    if d is None:
        return None
    return timezone.make_aware(d, dt_timezone.utc) if timezone.is_naive(d) else d


def unit_of(u) -> str | None:
    s = str(u or "").strip().lower()
    if s in ("l", "litre", "litres", "liter", "liters", "lt", "ltr"):
        return "L"
    if s in ("cm", "centimetre", "centimetres", "centimeter", "centimeters"):
        return "CM"
    return None


def proofs(docs: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Pièces jointes / photos trouvées dans la demande et l'opération : (identifiants ou URL, chemins bruts)."""
    found, paths = [], []

    def leaves(v):
        if isinstance(v, str):
            if v.strip():
                yield v.strip()
        elif isinstance(v, list):
            for x in v:
                yield from leaves(x)
        elif isinstance(v, dict):
            picked = v.get("url") or v.get("path") or v.get("file_url") or v.get("name") or v.get("filename") or v.get("_id") or v.get("id")
            if picked:
                yield str(picked)
            else:
                for x in v.values():
                    yield from leaves(x)

    def walk(node, prefix):
        if isinstance(node, dict):
            for k, v in node.items():
                path = f"{prefix}.{k}"
                if PROOF_KEY_RE.search(str(k)) and isinstance(v, (str, list, dict)) and not _empty(v):
                    items = [x for x in leaves(v) if len(x) > 2]
                    if items:
                        found.extend(items)
                        paths.append(path)
                        continue
                if isinstance(v, dict):
                    walk(v, path)

    for name in ("rest", "request", "operation"):
        walk(docs.get(name) or {}, name)
    uniq = list(dict.fromkeys(found))
    return uniq, paths


def type_family(raw_type: str | None) -> str:
    k = re.sub(r"[^A-Z]", "", str(raw_type or "").upper())
    if "PONCTION" in k:
        return "PONCTION"
    if "TRUCK" in k or "CAMION" in k:
        return "TRUCK"
    if "TOTAL" in k or "CARTE" in k:
        return "TOTAL_CARD"
    return "AUTRE"


def ponction_sections(docs: dict[str, dict]) -> list[tuple[dict, str]]:
    """Sections « Ponction » brutes, par priorité : opération, demande, élément REST historique.
    ENOC répartit les champs (ex. niveau avant dans la demande, niveau après dans l'opération)."""
    out = []
    for name in ("operation", "request", "rest"):
        v = (docs.get(name) or {}).get("ponction")
        if isinstance(v, dict) and v:
            out.append((v, f"{name}.ponction"))
    return out


def ponction_section(docs: dict[str, dict]) -> tuple[dict, str | None]:
    """Première section « Ponction » brute (compatibilité)."""
    secs = ponction_sections(docs)
    return secs[0] if secs else ({}, None)


def extract_ponction(docs: dict[str, dict], src: dict) -> dict:
    secs = ponction_sections(docs)
    out = {"ponction_source_site_id": None, "source_level_before_l": None, "source_level_after_l": None,
           "source_level_before_cm": None, "source_level_after_cm": None, "transfer_note": None, "_ponction_section": bool(secs),
           "_source_level_unknown": {}}
    if not secs:
        return out

    def first(field):
        for sec, base in secs:
            for k in PONC[field]:
                v = sec.get(k)
                if not _empty(v):
                    return v, f"{base}.{k}"
        return None, None

    v, path = first("source_site")
    if isinstance(v, dict):
        v = v.get("site_id") or v.get("id") or v.get("code")
    if not _empty(v):
        out["ponction_source_site_id"] = str(v).strip()[:128]
        src["ponction_source_site"] = path
    for side in ("before", "after"):
        lit, p_l = first(f"lvl_{side}_l")
        cm, p_cm = first(f"lvl_{side}_cm")
        val, p_v = first(f"lvl_{side}")
        unit, _ = first(f"lvl_{side}_unit")
        u = unit_of(unit)
        lit, cm, val = num(lit), num(cm), num(val)
        if val is not None and lit is None and cm is None:
            if u == "L" or (u is None and p_v and ("liter" in p_v or "litre" in p_v)):
                lit, p_l = val, p_v
            elif u == "CM" or (u is None and p_v and p_v.endswith("_cm")):
                cm, p_cm = val, p_v
            else:
                out["_source_level_unknown"][side] = val
        if lit is not None:
            out[f"source_level_{side}_l"] = lit
            src[f"source_level_{side}_l"] = p_l
        if cm is not None:
            out[f"source_level_{side}_cm"] = cm
            src[f"source_level_{side}_cm"] = p_cm
    note, p_n = first("note")
    if not _empty(note):
        out["transfer_note"] = str(note).strip()[:255]
        src["transfer_note"] = p_n
    else:  # bon de transfert joint comme pièce dans la section Ponction
        for sec, base in secs:
            items, paths = proofs({"request": sec})
            if items:
                out["transfer_note"] = ", ".join(items)[:255]
                src["transfer_note"] = ", ".join(p.replace("request", base, 1) for p in paths)
                break
    return out


# ── Extraction canonique ───────────────────────────────────────────────────────────────────────────────────
def extract(m) -> dict:
    raw = m.raw_payload or {}
    docs = _docs(raw)
    src: dict[str, str] = {}

    def take(field, conv=None):
        v, path = pick(docs, field)
        if v is None:
            return None
        out = conv(v) if conv else v
        if out is not None:
            src[field] = path
        return out

    def text(v, n=255):
        s = str(v).strip()
        return s[:n] if s else None

    legacy = bool(docs.get("rest"))
    status_doc = "rest" if legacy else "request"
    has_request = bool(docs["request"]) or legacy
    # API REST historique (endpoint « operations ») : opération exécutée si quantité ou date d'opération présente.
    has_operation = bool(docs["operation"]) or (legacy and any(not _empty(docs["rest"].get(k))
                                                               for k in ("quantity_added_liters", "operation_date")))
    raw_type = take("type", lambda v: text(v, 128))
    sv = docs[status_doc].get("status")
    raw_status = text(sv, 64) if has_request and not _empty(sv) else None
    if raw_status:
        src["raw_status"] = f"{status_doc}.status"
    actual = take("actual", num)
    unit = take("unit", lambda v: unit_of(v) or text(v, 16))
    if actual is not None and unit is None and src.get("actual", "").endswith(("_liters", "_litres")):
        unit = "L"
        src["unit"] = f"implicite : {src['actual']} (champ en litres)"

    levels = {}
    for side in ("before", "after"):
        value = take(f"level_{side}", num)
        u = take(f"level_{side}_unit", unit_of)
        lit = take(f"level_{side}_l", num)
        cm = take(f"level_{side}_cm", num)
        if value is not None and u == "L" and lit is None:
            lit = value
            src[f"level_{side}_l"] = f"{src[f'level_{side}']} ({src.get(f'level_{side}_unit')} = L)"
        if value is not None and u == "CM" and cm is None:
            cm = value
            src[f"level_{side}_cm"] = f"{src[f'level_{side}']} ({src.get(f'level_{side}_unit')} = cm)"
        levels[side] = (lit, cm, value if (value is not None and u is None) else None)

    comments = []
    for name in ("operation", "request"):
        for key in K["comment"]:
            v = docs[name].get(key)
            if not _empty(v) and str(v).strip() not in comments:
                comments.append(str(v).strip())
                src.setdefault("comment", f"{name}.{key}")
    proof_items, proof_paths = proofs(docs)
    if proof_paths:
        src["proofs"] = ", ".join(proof_paths)
    ops = raw.get("operations") if isinstance(raw.get("operations"), list) else ([docs["operation"]] if has_operation else [])

    return {
        "enoc_request_id": take("request_id", lambda v: text(v, 128)),
        "enoc_reference": take("reference", lambda v: text(v, 128)) or (m.source_id or None),
        "site_id": take("site_id", lambda v: text(v, 128)),
        "site_name": take("site_name", text),
        "country": take("country", lambda v: text(v, 64)),
        "zone": take("zone", lambda v: text(v, 128)),
        "raw_movement_type": raw_type,
        "raw_status": raw_status,
        "requested_quantity_l": take("requested", num),
        "approved_quantity_l": take("approved", num),
        "actual_quantity_l": actual,
        "quantity_unit": unit if actual is not None else None,
        "planned_at": take("planned", dt),
        "actual_operation_at": take("actual_at", dt),
        "requested_at": take("requested_at", dt),
        "approved_at": take("approved_at", dt),
        "closed_at": take("closed_at", dt),
        "tank_level_before_l": levels["before"][0],
        "tank_level_after_l": levels["after"][0],
        "tank_level_before_cm": levels["before"][1],
        "tank_level_after_cm": levels["after"][1],
        "generator_hourmeter_before_h": take("hm_before", num),
        "generator_hourmeter_after_h": take("hm_after", num),
        "technician_name": take("technician", text),
        "validator_name": take("validator", text),
        "closer_name": take("closer", text),
        "comment": " | ".join(comments) or None,
        "proof_count": len(proof_items),
        "proof_urls_or_ids": proof_items[:50],
        "supplier_name": take("supplier", text),
        "delivery_note_number": take("delivery_note", lambda v: text(v, 128)),
        "vehicle_or_truck_reference": take("vehicle", lambda v: text(v, 128)),
        "operations_count": len(ops),
        **extract_ponction(docs, src),
        "field_sources": src,
        "_has_request": has_request,
        "_has_operation": has_operation,
        "_legacy_rest": legacy,
        "_level_unknown_unit": {s: levels[s][2] for s in ("before", "after") if levels[s][2] is not None},
    }


# ── Qualification (axe B, indépendante du type brut) ─────────────────────────────────────────────────────
def qualify(c: dict, now=None) -> dict:
    now = now or timezone.now()
    fam = type_family(c.get("raw_movement_type"))
    status = (c.get("raw_status") or "").strip().lower()
    alerts: list[dict] = []
    missing: list[str] = []

    def alert(code, msg, blocking=False):
        alerts.append({"code": code, "message": msg, "blocking": blocking})

    if c.get("_legacy_rest"):
        alert("SOURCE_API_REST_HISTORIQUE", "Ligne issue de l'ancienne API REST ENOC (absente de la source Mongo actuelle) : "
                                            "élément REST brut utilisé, à confirmer avec ENOC.")
    if not c.get("_has_request") and c.get("_has_operation"):
        alert("OPERATION_SANS_DEMANDE", "Opération ENOC sans demande liée : statut brut absent, exécution déduite de l'opération.")
    for side, v in (c.get("_level_unknown_unit") or {}).items():
        alert("UNITE_NIVEAU_INCONNUE", f"Niveau {('avant' if side == 'before' else 'après')} {v} sans unité reconnue : conservé brut.")
    if c.get("operations_count", 0) > 1:
        alert("PLUSIEURS_OPERATIONS", f"{c['operations_count']} opérations ENOC pour cette demande : la plus récente est utilisée.")

    st = c["_status_map"] = map_status(c)

    def result(classification, state, reason, recon="NON_REQUIS", **extra):
        out = {**st, "type_family": fam, "business_classification": classification, "execution_state": state,
               "stock_impact": False, "consumption_impact": False, "reason": reason, "missing_conditions": missing,
               "alerts": alerts, "blocking": any(a["blocking"] for a in alerts), "reconciliation_status": recon,
               "qualification_version": QUALIFICATION_VERSION, "transfer_id": None, "transfer_status": "NON_APPLICABLE",
               "source_variation_l": None, "control_gap_l": None, "fuel_manager_alert": "AUCUNE",
               "integration_status": "NON_INTEGRABLE", "non_integration_cause": None, "quality_status": None,
               "quality_missing": [], "accounting_at": None, "date_source_used": "AUCUNE_DATE", "integrated_quantity_l": None}
        out.update(extra)
        return out

    if status in REJECTED_STATUSES:
        extra = {"transfer_status": "REJETE", "transfer_id": transfer_id_for(c.get("enoc_reference"))} if fam == "PONCTION" else {}
        return result("REJETEE", "REJETEE", f"Demande ENOC au statut « {c.get('raw_status')} » : aucun impact stock.",
                      non_integration_cause="REJETEE", **extra)
    if fam == "PONCTION":
        return qualify_ponction(c, status, alerts, missing, alert, result)
    if fam == "AUTRE":
        label = c.get("raw_movement_type") or "(vide)"
        closed = status in CLOSED_STATUSES or (not c.get("_has_request") and c.get("_has_operation"))
        state = "NATURE_A_QUALIFIER" if closed else ("APPROUVEE" if status in APPROVED_STATUSES else "DEMANDE")
        return result("TYPE_NON_RECONNU", state, f"Type ENOC « {label} » conservé sans interprétation : non intégré.",
                      non_integration_cause="TYPE_NON_RECONNU")
    return qualify_delivery(c, fam, status, now, alerts, missing, alert, result)


# ── Intégration eGrid sur données minimales (ajustement validé : exploiter l'historique ENOC) ────────────────
CAUSE_LABEL = {
    "REJETEE": "Demande rejetée dans ENOC",
    "HISTORIQUE_SANS_STATUT": "Historique ENOC (ancien connecteur) : statut non vérifiable",
    "STATUT_NON_VALIDE": "Statut ENOC non validé (en attente / inconnu / « done » sans validation tracée)",
    "TYPE_NON_RECONNU": "Type ENOC non reconnu",
    "DEMANDE_NON_EXECUTEE": "Demande validée sans opération réalisée (pas de quantité réelle)",
    "SITE_NON_IDENTIFIE": "Site absent ou inconnu de Gestion des sites",
    "SITE_SOURCE_ABSENT": "Site source de la Ponction absent ou inconnu",
    "QUANTITE_ABSENTE": "Quantité absente ou ≤ 0",
    "UNITE_NON_LITRE": "Unité de quantité non confirmée en litres",
    "DATE_ABSENTE": "Date requise absente",
    "INCOHERENCE_BLOQUANTE": "Incohérence bloquante (quantité négative, date future, même site…)",
}
CAUSE_ORDER = tuple(CAUSE_LABEL)


# ── Mapping figé des statuts ENOC (traçable, configurable) ────────────────────────────────────────────────
STATUS_MAPPING_VERSION = "enoc-status-v1"
BUSINESS_STATUS_LABEL = {
    "VALIDEE_EXECUTABLE": "Validée (exécutable)",
    "VALIDEE_EXECUTEE": "Validée puis exécutée",
    "HISTORIQUE_NON_VERIFIABLE": "Historique non vérifiable (ancien connecteur)",
    "REJETEE": "Rejetée",
    "EN_ATTENTE": "En attente de validation",
    "STATUT_NON_VERIFIABLE": "Statut absent / inconnu / non vérifiable",
}


def status_settings() -> dict:
    from django.conf import settings

    validated = getattr(settings, "FUEL_ENOC_VALIDATED_STATUSES", None) or ("validated", "validee", "validée")
    return {"validated": {str(x).strip().lower() for x in validated} - {"done"},
            "done_is_executed": bool(getattr(settings, "FUEL_ENOC_DONE_IS_EXECUTED", True)),
            "done_requires_validation": bool(getattr(settings, "FUEL_ENOC_DONE_REQUIRES_VALIDATION", True))}


def map_status(c: dict) -> dict:
    """
    Statut brut ENOC → statut métier eGrid (règle tracée) :
      validated (ENOC actuel)                         → VALIDEE_EXECUTABLE   éligible
      done (ENOC actuel), validation tracée           → VALIDEE_EXECUTEE     éligible (FUEL_ENOC_DONE_IS_EXECUTED)
      tout statut issu de l'ancien connecteur REST    → HISTORIQUE_NON_VERIFIABLE (rejected → REJETEE)
      rejected                                        → REJETEE
      pending / open…                                 → EN_ATTENTE
      absent / inconnu / done sans validation tracée  → STATUT_NON_VERIFIABLE
    """
    cfg = status_settings()
    raw = (c.get("raw_status") or "").strip().lower()
    if c.get("_legacy_rest"):
        prov = "API_REST_HISTORIQUE"
    elif not c.get("_has_request"):
        prov = "ENOC_OPERATION_SANS_DEMANDE"
    else:
        prov = "ENOC_MONGO"

    def out(business, rule):
        return {"source_provenance": prov, "business_status": business, "eligible_for_stock":
                business in ("VALIDEE_EXECUTABLE", "VALIDEE_EXECUTEE"), "status_mapping_rule": f"{STATUS_MAPPING_VERSION} : {rule}"[:255]}

    if raw in REJECTED_STATUSES:
        return out("REJETEE", f"« {raw} » → REJETEE")
    if prov == "API_REST_HISTORIQUE":
        return out("HISTORIQUE_NON_VERIFIABLE", f"« {raw or 'aucun'} » créé par l'ancien connecteur REST : non vérifiable dans ENOC actuel")
    if raw in cfg["validated"]:
        return out("VALIDEE_EXECUTABLE", f"« {raw} » ENOC → VALIDEE_EXECUTABLE")
    if raw == "done":
        if not cfg["done_is_executed"]:
            return out("STATUT_NON_VERIFIABLE", "« done » non retenu comme exécution validée (FUEL_ENOC_DONE_IS_EXECUTED=false)")
        if cfg["done_requires_validation"] and not (c.get("approved_at") or c.get("validator_name")):
            return out("STATUT_NON_VERIFIABLE", "« done » ENOC sans trace de validation (date ou valideur) : non vérifiable")
        return out("VALIDEE_EXECUTEE", "« done » ENOC avec validation tracée → étape exécutée après validation")
    if raw in OPEN_STATUSES:
        return out("EN_ATTENTE", f"« {raw} » → EN_ATTENTE")
    return out("STATUT_NON_VERIFIABLE", f"statut « {raw or 'absent'} » inconnu ou absent")


def integration_settings() -> dict:
    from django.conf import settings

    return {
            "gap_min_l": Decimal(str(getattr(settings, "FUEL_PONCTION_GAP_MIN_L", 20))),
            "gap_pct": Decimal(str(getattr(settings, "FUEL_PONCTION_GAP_PCT", 10)))}


def accounting_date(c: dict) -> tuple[object | None, str]:
    """Date comptable : 1) date réelle d'opération, 2) date de validation, 3) aucune (jamais inventée)."""
    if c.get("actual_operation_at"):
        return c["actual_operation_at"], "DATE_REELLE"
    if c.get("approved_at"):
        return c["approved_at"], "DATE_VALIDATION"
    return None, "AUCUNE_DATE"


def _integration(fam, causes: list[str], hnv: bool) -> dict:
    if not causes:
        return {"integration_status": "INTEGRE", "non_integration_cause": None}
    if hnv:
        return {"integration_status": "HISTORIQUE_NON_VERIFIABLE", "non_integration_cause": "HISTORIQUE_SANS_STATUT"}
    first = min(causes, key=CAUSE_ORDER.index)
    return {"integration_status": "NON_INTEGRABLE", "non_integration_cause": first}


def qualify_delivery(c: dict, fam: str, status: str, now, alerts: list, missing: list, alert, result) -> dict:
    """
    TRUCK / CARTE TOTAL — intégré au stock si : statut validé (validated / done), site identifié,
    quantité réelle > 0 L, date (Truck : date réelle ; Carte Total : date réelle sinon date de validation).
    Niveaux, photo et compteur ne bloquent pas : ils produisent un statut de qualité.
    """
    cfg = integration_settings()
    st = c.get("_status_map") or map_status(c)
    legacy_no_status = st["business_status"] == "HISTORIQUE_NON_VERIFIABLE"
    causes: list[str] = []

    def need(cause, label):
        causes.append(cause)
        missing.append(label)

    if legacy_no_status:
        need("HISTORIQUE_SANS_STATUT", f"statut vérifiable (historique ancien connecteur, « {c.get('raw_status') or 'aucun'} »)")
    elif not st["eligible_for_stock"]:
        need("STATUT_NON_VALIDE", f"statut validé (actuel : {c.get('raw_status') or 'aucun'} → {st['business_status']})")
    if not c.get("site_id") or not c.get("_site_resolved", c.get("site_id")):
        need("SITE_NON_IDENTIFIE", "site identifié" + (f" (« {c['site_id']} » inconnu)" if c.get("site_id") else ""))
    qty = c.get("actual_quantity_l")
    if not c.get("_has_operation"):
        need("DEMANDE_NON_EXECUTEE", "opération réalisée (quantité réellement introduite)")
    elif qty is None or qty <= 0:
        need("QUANTITE_ABSENTE", "quantité réelle > 0 L")
    elif c.get("quantity_unit") != "L":
        need("UNITE_NON_LITRE", "unité = L")
    when, date_src = accounting_date(c)
    if fam == "TRUCK" and not c.get("actual_operation_at"):
        need("DATE_ABSENTE", "date réelle")
        when, date_src = (c.get("approved_at"), "DATE_VALIDATION") if c.get("approved_at") else (None, "AUCUNE_DATE")
    elif when is None:
        need("DATE_ABSENTE", "date (réelle ou de validation)")
    if qty is not None and qty < 0:
        alert("QUANTITE_NEGATIVE", f"Quantité réelle négative ({qty}).", blocking=True)
    if when and when > now + timedelta(days=1):
        alert("DATE_FUTURE", f"Date dans le futur ({when:%d/%m/%Y}).", blocking=True)
    if any(a["blocking"] for a in alerts):
        causes.append("INCOHERENCE_BLOQUANTE")
    appr = c.get("approved_quantity_l")
    if appr and qty and qty > appr * Decimal("1.2"):
        alert("QUANTITE_SUP_APPROUVEE", f"Quantité réelle {qty} L > 120 % de la quantité approuvée ({appr} L).")

    # Qualité (jamais bloquante)
    qmiss = []
    has_b = c.get("tank_level_before_l") is not None or c.get("tank_level_before_cm") is not None
    has_a = c.get("tank_level_after_l") is not None or c.get("tank_level_after_cm") is not None
    if not has_b:
        qmiss.append("niveau avant")
    if not has_a:
        qmiss.append("niveau après")
    if not c.get("proof_count"):
        qmiss.append("photo / preuve")
    if c.get("generator_hourmeter_before_h") is None:
        qmiss.append("compteur avant")
    if c.get("generator_hourmeter_after_h") is None:
        qmiss.append("compteur après")
    gap_flag = False
    b, a = c.get("tank_level_before_l"), c.get("tank_level_after_l")
    if b is not None and a is not None and qty is not None and qty > 0:
        gap = abs((a - b) - qty)
        tol = max(cfg["gap_min_l"], cfg["gap_pct"] / Decimal("100") * qty)
        if a < b:
            gap_flag = True
            alert("ECART_NIVEAU", f"Niveau après ({a} L) < niveau avant ({b} L) pour un ravitaillement de {qty} L : à contrôler.")
        elif gap > tol:
            gap_flag = True
            alert("ECART_NIVEAU", f"Hausse de niveau {a - b} L ≠ quantité {qty} L (écart {gap} L > {tol} L) : à contrôler.")
    elif has_b and has_a:
        qmiss.append("écart non calculable (niveaux en cm)")
    minimal = any(x not in ("STATUT_NON_VALIDE", "HISTORIQUE_SANS_STATUT", "DEMANDE_NON_EXECUTEE") for x in causes)
    if minimal or "DEMANDE_NON_EXECUTEE" in causes:
        quality = "DONNEES_MINIMALES_MANQUANTES"
    elif gap_flag:
        quality = "ECART_NIVEAU"
    elif not (has_b and has_a):
        quality = "NIVEAUX_MANQUANTS"
    elif not c.get("proof_count"):
        quality = "PREUVE_MANQUANTE"
    else:
        quality = "COMPLET"
    integ = _integration(fam, causes, legacy_no_status)
    extra = dict(integ, quality_status=quality, quality_missing=qmiss, accounting_at=when, date_source_used=date_src,
                 integrated_quantity_l=qty if integ["integration_status"] == "INTEGRE" else None)
    label = "Carte Total" if fam == "TOTAL_CARD" else "Truck"
    if integ["integration_status"] == "INTEGRE":
        src = "date réelle" if date_src == "DATE_REELLE" else "date de validation"
        out = result("CARTE_TOTAL_INTEGREE" if fam == "TOTAL_CARD" else "LIVRAISON_TRUCK_INTEGREE", "ELIGIBLE_STOCK_REEL",
                     f"{label} intégré : +{qty} L ({src}), qualité {quality}"
                     + (f" — manquant (non bloquant) : {', '.join(qmiss)}" if qmiss else "") + ".",
                     recon="A_RAPPROCHER", **extra)
        out["stock_impact"] = out["consumption_impact"] = True
        return out
    why = ", ".join(missing + [x["message"] for x in alerts if x["blocking"]])
    if "DEMANDE_NON_EXECUTEE" in causes or "STATUT_NON_VALIDE" in causes and not c.get("_has_operation"):
        state = "APPROUVEE" if status in APPROVED_STATUSES else "DEMANDE"
    elif "QUANTITE_ABSENTE" in causes:
        state = "QUANTITE_REELLE_MANQUANTE"
    else:
        state = "TERMINEE"
    cls = ("DEMANDE_CARTE_TOTAL" if fam == "TOTAL_CARD" else "LIVRAISON_A_CONFIRMER") if state in ("DEMANDE", "APPROUVEE") \
        else ("RAVITAILLEMENT_DECLARE_INCOMPLET" if fam == "TOTAL_CARD" else "LIVRAISON_A_CONFIRMER")
    return result(cls, state, f"{label} non intégré — {CAUSE_LABEL[integ['non_integration_cause']]} : {why}.", **extra)


def qualify_ponction(c: dict, status: str, alerts: list, missing: list, alert, result) -> dict:
    """
    Ponction : site_id principal = DESTINATAIRE, « Site source » = SOURCE, volume comptable = quantité APPROUVÉE.
    Transfert créé (double écriture liée par transfer_id, somme = 0) si : statut validé, site source, site
    destinataire, quantité approuvée > 0, date de validation. Niveaux, bon de transfert, compteur et photos
    ne bloquent pas : statut de qualité COMPLET / CONTROLE_PARTIEL / ECART_NIVEAU.
    """
    cfg = ponction_settings()
    ref = c.get("enoc_reference")
    tid = transfer_id_for(ref)
    src_site = c.get("_source_site_resolved", c.get("ponction_source_site_id"))
    dst_site = c.get("_site_resolved", c.get("site_id"))
    qty = c.get("approved_quantity_l")
    st = c.get("_status_map") or map_status(c)
    legacy_no_status = st["business_status"] == "HISTORIQUE_NON_VERIFIABLE"
    causes: list[str] = []

    def need(cause, label):
        causes.append(cause)
        missing.append(label)

    if legacy_no_status:
        need("HISTORIQUE_SANS_STATUT", f"statut vérifiable (historique ancien connecteur, « {c.get('raw_status') or 'aucun'} »)")
    elif not st["eligible_for_stock"]:
        need("STATUT_NON_VALIDE", f"statut ENOC validé (actuel : {c.get('raw_status') or 'aucun'} → {st['business_status']})")
    if not c.get("ponction_source_site_id"):
        need("SITE_SOURCE_ABSENT", "site source")
    elif not src_site:
        need("SITE_SOURCE_ABSENT", f"site source « {c['ponction_source_site_id']} » inconnu de Gestion des sites")
    if not c.get("site_id"):
        need("SITE_NON_IDENTIFIE", "site destinataire (site_id de la demande)")
    elif not dst_site:
        need("SITE_NON_IDENTIFIE", f"site destinataire « {c['site_id']} » inconnu de Gestion des sites")
    if src_site and dst_site and str(src_site).upper() == str(dst_site).upper():
        alert("PONCTION_MEME_SITE", "Site source = site destinataire : transfert impossible.", blocking=True)
        causes.append("INCOHERENCE_BLOQUANTE")
    if qty is None or qty <= 0:
        need("QUANTITE_ABSENTE", "quantité approuvée > 0 L")
    if not c.get("approved_at"):
        need("DATE_ABSENTE", "date de validation")
    if not tid:
        need("INCOHERENCE_BLOQUANTE", "référence ENOC (transfer_id)")
    when, date_src = accounting_date(c)
    for side, v in (c.get("_source_level_unknown") or {}).items():
        alert("UNITE_NIVEAU_SOURCE_INCONNUE", f"Niveau source {('avant' if side == 'before' else 'après')} {v} sans unité reconnue.")

    # Qualité (jamais bloquante) — le volume approuvé fait foi
    qmiss = []
    has_b = c.get("source_level_before_l") is not None or c.get("source_level_before_cm") is not None
    has_a = c.get("source_level_after_l") is not None or c.get("source_level_after_cm") is not None
    if not has_b:
        qmiss.append("niveau source avant")
    if not has_a:
        qmiss.append("niveau source après")
    if not c.get("transfer_note"):
        qmiss.append("bon de transfert")
    if not c.get("validator_name"):
        qmiss.append("valideur")
    variation = gap = None
    fm = "AUCUNE"
    b, a = c.get("source_level_before_l"), c.get("source_level_after_l")
    if b is not None and a is not None and qty is not None and qty > 0:
        variation = a - b
        gap = abs(variation + qty)
        threshold = max(cfg["gap_min_l"], cfg["gap_pct"] / Decimal("100") * qty)
        if gap > threshold:
            alert("ECART_NIVEAU_PONCTION_A_CONTROLER",
                  f"Variation source {variation} L pour {qty} L approuvés : écart de contrôle {gap} L > seuil {threshold} L "
                  "(max(20 L ; 10 %)). Volume approuvé comptabilisé tel quel ; contrôle Fuel Manager requis.")
            fm = "EN_ATTENTE"
    elif has_b and has_a:
        qmiss.append("écart non calculable (niveaux en cm)")
        alert("NIVEAU_SOURCE_EN_CM", "Niveaux du site source en cm : variation en litres non calculée (contrôle manuel).")
    minimal = any(x in ("SITE_SOURCE_ABSENT", "SITE_NON_IDENTIFIE", "QUANTITE_ABSENTE", "DATE_ABSENTE") for x in causes)
    if minimal:
        quality = "DONNEES_MINIMALES_MANQUANTES"
    elif fm == "EN_ATTENTE":
        quality = "ECART_NIVEAU"
    elif has_b and has_a:
        quality = "COMPLET"
    else:
        quality = "CONTROLE_PARTIEL"
    integ = _integration("PONCTION", causes, legacy_no_status)
    extra = dict(integ, transfer_id=tid, source_variation_l=variation, control_gap_l=gap, fuel_manager_alert=fm,
                 quality_status=quality, quality_missing=qmiss, accounting_at=when, date_source_used=date_src,
                 integrated_quantity_l=qty if integ["integration_status"] == "INTEGRE" else None)
    if integ["integration_status"] != "INTEGRE":
        why = ", ".join(missing + [x["message"] for x in alerts if x["blocking"]])
        return result("PONCTION_A_COMPLETER", "TRANSFERT_A_COMPLETER",
                      f"Transfert non intégré — {CAUSE_LABEL[integ['non_integration_cause']]} : {why}.",
                      transfer_status="TRANSFERT_A_COMPLETER", **extra)
    out = result("PONCTION_COMPTABILISEE", "ELIGIBLE_STOCK_REEL",
                 f"Ponction intégrée : −{qty} L au site source {src_site}, +{qty} L au site destinataire {dst_site} "
                 f"(quantité approuvée, {tid}), qualité {quality}"
                 + (f" — manquant (non bloquant) : {', '.join(qmiss)}" if qmiss else "") + ".",
                 transfer_status="COMPTABILISE", **extra)
    out["stock_impact"] = out["consumption_impact"] = True
    return out


CANONICAL_FIELDS = ("enoc_request_id", "enoc_reference", "site_id", "site_name", "country", "zone", "raw_movement_type",
                    "raw_status", "requested_quantity_l", "approved_quantity_l", "actual_quantity_l", "quantity_unit",
                    "planned_at", "actual_operation_at", "requested_at", "approved_at", "closed_at", "tank_level_before_l",
                    "tank_level_after_l", "tank_level_before_cm", "tank_level_after_cm", "generator_hourmeter_before_h",
                    "generator_hourmeter_after_h", "technician_name", "validator_name", "closer_name", "comment",
                    "proof_count", "proof_urls_or_ids", "supplier_name", "delivery_note_number",
                    "vehicle_or_truck_reference", "operations_count", "ponction_source_site_id", "source_level_before_l",
                    "source_level_after_l", "source_level_before_cm", "source_level_after_cm", "transfer_note", "field_sources")
# Valeurs qui, si elles changent à la source, invalident un rapprochement déjà fait.
FINGERPRINT_FIELDS = ("site_id", "raw_movement_type", "raw_status", "actual_quantity_l", "quantity_unit", "actual_operation_at",
                      "tank_level_before_l", "tank_level_after_l", "proof_count", "supplier_name", "delivery_note_number",
                      "approved_quantity_l", "approved_at", "validator_name", "ponction_source_site_id", "source_level_before_l",
                      "source_level_after_l", "source_level_before_cm", "source_level_after_cm", "transfer_note")
QUALIF_FIELDS = ("type_family", "business_classification", "execution_state", "stock_impact", "consumption_impact",
                 "reason", "missing_conditions", "alerts", "blocking", "qualification_version", "transfer_id",
                 "transfer_status", "source_variation_l", "control_gap_l", "fuel_manager_alert", "integration_status",
                 "non_integration_cause", "quality_status", "quality_missing", "accounting_at", "date_source_used",
                 "integrated_quantity_l", "source_provenance", "business_status", "status_mapping_rule", "eligible_for_stock")


def fingerprint(c: dict) -> str:
    payload = {k: (str(c.get(k)) if c.get(k) is not None else None) for k in FINGERPRINT_FIELDS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def sync_qualified(movements=None, user=None) -> dict:
    """Extrait + qualifie chaque FuelEnocMovement. Idempotent ; un rapprochement n'est conservé que si la source n'a pas changé."""
    from fuel_tracking.models import FuelEnocMovement, FuelEnocQualifiedMovement as Q
    from fuel_tracking.services import stock_registry as R

    qs = movements if movements is not None else FuelEnocMovement.objects.all()
    existing = {q.enoc_movement_id: q for q in Q.objects.filter(enoc_movement_id__in=[m.pk for m in qs])}
    resolver = R.SiteResolver()
    now = timezone.now()
    stats = {"lus": 0, "crees": 0, "mis_a_jour": 0, "inchanges": 0, "rapprochements_annules": 0}
    with transaction.atomic():
        for m in qs:
            stats["lus"] += 1
            c = extract(m)
            c["_site_resolved"] = resolver.resolve(c["site_id"]) if c["site_id"] else None
            c["_source_site_resolved"] = resolver.resolve(c["ponction_source_site_id"]) if c["ponction_source_site_id"] else None
            q_vals = qualify(c, now)
            fp = fingerprint(c)
            values = {k: c[k] for k in CANONICAL_FIELDS}
            values.update(site_id_resolved=c["_site_resolved"], ponction_source_site_resolved=c["_source_site_resolved"],
                          source_fingerprint=fp, synced_at=m.synced_at)
            q = existing.get(m.pk)
            if q is not None and q.fuel_manager_alert == "TRAITEE" and q_vals.get("fuel_manager_alert") == "EN_ATTENTE" \
                    and q.source_fingerprint == fp:
                q_vals["fuel_manager_alert"] = "TRAITEE"  # alerte déjà traitée par le Fuel Manager, source inchangée
            if q is not None and q.reconciliation_status in ("RAPPROCHE", "CERTIFIE", "REJETE"):
                if q.source_fingerprint == fp:
                    # Décision humaine conservée (source inchangée) : elle prime sur la qualification automatique.
                    q_vals["reconciliation_status"] = q.reconciliation_status
                    if q.reconciliation_status == "REJETE":
                        q_vals.update(execution_state="REJETEE", stock_impact=False, consumption_impact=False,
                                      integration_status="NON_INTEGRABLE", non_integration_cause="REJETEE",
                                      integrated_quantity_l=None)
                        if q_vals.get("transfer_status") != "NON_APPLICABLE":
                            q_vals["transfer_status"] = "REJETE"
                    else:
                        q_vals.update(execution_state="ELIGIBLE_STOCK_REEL", stock_impact=True, consumption_impact=True)
                else:
                    R.log_correction("ENOC_MOVEMENT", q.pk, "SYSTEM", "Donnée ENOC modifiée après rapprochement : à rapprocher de nouveau",
                                     site_id=values["site_id_resolved"], field="reconciliation_status",
                                     old=q.reconciliation_status, new=q_vals["reconciliation_status"], author_label="ENOC")
                    stats["rapprochements_annules"] += 1
                    q.reconciled_by, q.reconciled_at = None, None
            values.update(q_vals)
            if q is None:
                q = Q(enoc_movement=m, qualified_at=now, **values)
                q.save()
                stats["crees"] += 1
                continue
            changed = {k: v for k, v in values.items() if _differs(getattr(q, k), v)}
            if not changed:
                stats["inchanges"] += 1
                continue
            for k, v in changed.items():
                setattr(q, k, v)
            q.qualified_at = now
            q.save()
            stats["mis_a_jour"] += 1
    return stats


def _differs(a, b) -> bool:
    if isinstance(a, Decimal) or isinstance(b, Decimal):
        try:
            return (a is None) != (b is None) or (a is not None and Decimal(str(a)) != Decimal(str(b)))
        except InvalidOperation:
            return str(a) != str(b)
    return a != b


# ── Rapprochement (certification d'un mouvement intégré, ou rejet humain) ─────────────────────────────────────────────────────
class ReconciliationError(ValueError):
    pass


def reconciliation_context(q) -> dict:
    """Éléments de rapprochement : relevés terrain (hors ENOC) autour de la date réelle, niveaux ENOC, preuves."""
    from fuel_tracking.models import FuelStockObservation as O

    out = {"releves_terrain": [], "niveau_enoc_avant_l": _f(q.tank_level_before_l), "niveau_enoc_apres_l": _f(q.tank_level_after_l),
           "quantite_reelle_l": _f(q.actual_quantity_l), "preuves": q.proof_urls_or_ids, "preuve_fournisseur": bool(q.delivery_note_number)}
    if q.site_id_resolved and q.actual_operation_at:
        lo, hi = q.actual_operation_at - timedelta(hours=72), q.actual_operation_at + timedelta(hours=72)
        for o in (O.objects.filter(site_id=q.site_id_resolved, observation_datetime__range=(lo, hi))
                  .exclude(source_type__in=("ENOC", "SNOWFLAKE", "CLOSING")).order_by("observation_datetime")[:10]):
            out["releves_terrain"].append({"id": o.pk, "source": o.source_type, "date": o.observation_datetime, "brut": o.level_raw,
                                           "litres": _f(o.level_liters), "statut": o.validation_status,
                                           "position": "avant" if o.observation_datetime <= q.actual_operation_at else "après"})
    return out


def _f(v):
    return float(v) if v is not None else None


def reconcile(q, user, decision: str, reason: str, evidence: str | None = None):
    """decision : « rapprocher » (mouvement intégré → RAPPROCHE, CERTIFIE si bon de livraison), « rejeter » (retire
    du stock), « acquitter » (alerte Fuel Manager)."""
    from fuel_tracking.services import stock_registry as R

    reason = (reason or "").strip()
    if not reason:
        raise ReconciliationError("Motif obligatoire.")
    if decision == "rapprocher":
        if q.integration_status != "INTEGRE" or q.reconciliation_status != "A_RAPPROCHER":
            raise ReconciliationError("Seul un mouvement intégré non encore rapproché peut être rapproché (certification).")
        if q.blocking:
            raise ReconciliationError("Alerte bloquante présente : rapprochement impossible.")
        if not (evidence or "").strip():
            raise ReconciliationError("Élément de rapprochement obligatoire (relevé terrain cohérent, stock, bon fournisseur…).")
        new_recon = "CERTIFIE" if q.delivery_note_number else "RAPPROCHE"
        old = (q.execution_state, q.reconciliation_status)
        q.execution_state, q.reconciliation_status, q.stock_impact, q.consumption_impact = "ELIGIBLE_STOCK_REEL", new_recon, True, True
    elif decision == "acquitter":
        if q.fuel_manager_alert != "EN_ATTENTE":
            raise ReconciliationError("Aucune alerte Fuel Manager en attente sur ce mouvement.")
        q.fuel_manager_alert = "TRAITEE"
        q.fuel_manager_reviewed_by = user if (user and getattr(user, "is_authenticated", False)) else None
        q.fuel_manager_reviewed_at = timezone.now()
        q.fuel_manager_comment = reason
        q.save()
        R.log_correction("ENOC_MOVEMENT", q.pk, "VALIDATE", reason, site_id=q.site_id_resolved, field="fuel_manager_alert",
                         old="EN_ATTENTE", new="TRAITEE", user=user)
        return q
    elif decision == "rejeter":
        old = (q.execution_state, q.reconciliation_status)
        q.execution_state, q.reconciliation_status, q.stock_impact, q.consumption_impact = "REJETEE", "REJETE", False, False
        q.integration_status, q.non_integration_cause, q.integrated_quantity_l = "NON_INTEGRABLE", "REJETEE", None
        if q.transfer_status != "NON_APPLICABLE":
            q.transfer_status = "REJETE"
    else:
        raise ReconciliationError("Décision inconnue (rapprocher | rejeter).")
    q.reconciled_by = user if (user and getattr(user, "is_authenticated", False)) else None
    q.reconciled_at = timezone.now()
    q.reconciliation_reason = reason
    q.reconciliation_evidence = (evidence or "").strip() or None
    q.save()
    R.log_correction("ENOC_MOVEMENT", q.pk, "VALIDATE" if decision == "rapprocher" else "REJECT", reason, site_id=q.site_id_resolved,
                     field="execution_state/reconciliation", old=old, new=(q.execution_state, q.reconciliation_status), user=user)
    return q


# ── Volumes d'intégration eGrid (retour attendu après déploiement) ─────────────────────────────────────────
def integration_report() -> dict:
    """Volumes calculés sur la qualification en base (lecture seule)."""
    from django.db.models import Count, Sum

    from fuel_tracking.models import FuelEnocQualifiedMovement as Q

    def vol(qs):
        a = qs.aggregate(n=Count("id"), l=Sum("integrated_quantity_l"))
        return {"nombre": a["n"], "litres": float(a["l"]) if a["l"] is not None else None}

    integ = Q.objects.filter(integration_status="INTEGRE")
    fam = {"PONCTION": "ponctions_integrees", "TRUCK": "truck_integres", "TOTAL_CARD": "carte_total_integrees"}
    out = {key: vol(integ.filter(type_family=f)) for f, key in fam.items()}
    out["integres_total"] = vol(integ)
    out["integres_complets"] = vol(integ.filter(quality_status="COMPLET"))
    out["integres_niveaux_manquants"] = vol(integ.filter(quality_status__in=("NIVEAUX_MANQUANTS", "CONTROLE_PARTIEL")))
    out["integres_ecart_niveau"] = vol(integ.filter(quality_status="ECART_NIVEAU"))
    out["integres_preuve_manquante"] = vol(integ.filter(quality_status="PREUVE_MANQUANTE"))
    out["integres_ecart_non_calculable_cm"] = integ.filter(quality_missing__icontains="niveaux en cm").count()
    out["integres_par_qualite_et_type"] = {
        f"{r['type_family']}:{r['quality_status']}": r["n"]
        for r in integ.values("type_family", "quality_status").annotate(n=Count("id")).order_by("type_family", "quality_status")}
    out["integres_par_source_de_date"] = {r["date_source_used"]: r["n"] for r in integ.values("date_source_used").annotate(n=Count("id"))}
    rest = Q.objects.exclude(integration_status="INTEGRE")
    out["non_integrables_par_cause"] = {
        f"{r['non_integration_cause'] or 'AUTRE'} ({CAUSE_LABEL.get(r['non_integration_cause'] or '', 'autre')})": r["n"]
        for r in rest.values("non_integration_cause").annotate(n=Count("id")).order_by("-n")}
    out["non_integrables_par_type_et_cause"] = {
        f"{r['type_family']}:{r['non_integration_cause'] or 'AUTRE'}": r["n"]
        for r in rest.values("type_family", "non_integration_cause").annotate(n=Count("id")).order_by("type_family", "-n")}
    out["historiques_non_verifiables"] = Q.objects.filter(integration_status="HISTORIQUE_NON_VERIFIABLE").count()
    # Détail par statut (mapping figé) : validated / done ENOC vérifié / done historique exclu / autres
    def by_status(qs):
        return {f"{r['raw_status'] or '(aucun)'} → {r['business_status'] or '?'} [{r['source_provenance'] or '?'}]": r["n"]
                for r in qs.values("raw_status", "business_status", "source_provenance").annotate(n=Count("id")).order_by("-n")}
    out["integres_par_statut"] = {
        "validated": integ.filter(business_status="VALIDEE_EXECUTABLE").count(),
        "done_enoc_verifie": integ.filter(business_status="VALIDEE_EXECUTEE").count(),
        "autres": integ.exclude(business_status__in=("VALIDEE_EXECUTABLE", "VALIDEE_EXECUTEE")).count(),
    }
    out["done_historique_exclu"] = Q.objects.filter(business_status="HISTORIQUE_NON_VERIFIABLE", raw_status__iexact="done").count()
    out["statuts_detail"] = by_status(Q.objects.all())
    out["total_mouvements"] = Q.objects.count()
    return out
