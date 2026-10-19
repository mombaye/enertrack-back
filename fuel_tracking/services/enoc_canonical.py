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

QUALIFICATION_VERSION = "enoc-q-v1"

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


# ── Lecture brute ──────────────────────────────────────────────────────────────────────────────────────────
def _docs(raw: dict) -> dict[str, dict]:
    raw = raw or {}
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
    for name in FIELD_ORDER.get(field, ORDER_OP_FIRST):
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

    for name in ("request", "operation"):
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

    has_request = bool(docs["request"])
    has_operation = bool(docs["operation"])
    raw_type = take("type", lambda v: text(v, 128))
    raw_status = text(docs["request"].get("status"), 64) if has_request and not _empty(docs["request"].get("status")) else None
    if raw_status:
        src["raw_status"] = "request.status"
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
        "field_sources": src,
        "_has_request": has_request,
        "_has_operation": has_operation,
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

    if not c.get("_has_request") and c.get("_has_operation"):
        alert("OPERATION_SANS_DEMANDE", "Opération ENOC sans demande liée : statut brut absent, exécution déduite de l'opération.")
    for side, v in (c.get("_level_unknown_unit") or {}).items():
        alert("UNITE_NIVEAU_INCONNUE", f"Niveau {('avant' if side == 'before' else 'après')} {v} sans unité reconnue : conservé brut.")
    if c.get("operations_count", 0) > 1:
        alert("PLUSIEURS_OPERATIONS", f"{c['operations_count']} opérations ENOC pour cette demande : la plus récente est utilisée.")

    def result(classification, state, reason, recon="NON_REQUIS"):
        return {"type_family": fam, "business_classification": classification, "execution_state": state,
                "stock_impact": False, "consumption_impact": False, "reason": reason, "missing_conditions": missing,
                "alerts": alerts, "blocking": any(a["blocking"] for a in alerts), "reconciliation_status": recon,
                "qualification_version": QUALIFICATION_VERSION}

    if status in REJECTED_STATUSES:
        return result("REJETEE", "REJETEE", f"Demande ENOC au statut « {c.get('raw_status')} » : aucun impact stock.")
    if fam == "PONCTION":
        return result("PONCTION_A_QUALIFIER", "NATURE_A_QUALIFIER", REASON_PONCTION)

    closed = status in CLOSED_STATUSES or (not c.get("_has_request") and c.get("_has_operation"))
    if status and not closed and status not in OPEN_STATUSES | APPROVED_STATUSES:
        alert("STATUT_INCONNU", f"Statut ENOC « {c.get('raw_status')} » non reconnu : traité comme non terminé.")
    open_state = "APPROUVEE" if status in APPROVED_STATUSES else "DEMANDE"
    if fam == "AUTRE":
        label = c.get("raw_movement_type") or "(vide)"
        if not closed:
            return result("TYPE_NON_RECONNU", open_state, f"Demande ENOC de type « {label} » non reconnu : demande, aucun impact stock.")
        return result("TYPE_NON_RECONNU", "NATURE_A_QUALIFIER", f"Type ENOC « {label} » conservé sans interprétation : nature à qualifier.")
    if not closed:
        if fam == "TOTAL_CARD":
            return result("DEMANDE_CARTE_TOTAL", open_state, "Carte Total non terminée : demande, aucun impact stock ni consommation.")
        return result("LIVRAISON_A_CONFIRMER", open_state, "Truck non terminé : livraison à confirmer, aucun impact stock.")

    qty = c.get("actual_quantity_l")
    if not c.get("site_id"):
        missing.append("site_id")
    if qty is None or qty <= 0:
        missing.append("quantité réelle > 0")
    if c.get("quantity_unit") != "L":
        missing.append("unité = L")
    if not c.get("actual_operation_at"):
        missing.append("date réelle")
    if not c.get("proof_count"):
        missing.append("preuve / photo")

    if qty is not None and qty < 0:
        alert("QUANTITE_NEGATIVE", f"Quantité réelle négative ({qty}).", blocking=True)
    when = c.get("actual_operation_at")
    if when and when > now + timedelta(days=1):
        alert("DATE_FUTURE", f"Date réelle dans le futur ({when:%d/%m/%Y}).", blocking=True)
    b, a = c.get("tank_level_before_l"), c.get("tank_level_after_l")
    if b is not None and a is not None and qty is not None and qty > 0:
        if a < b:
            alert("NIVEAU_APRES_INFERIEUR", f"Niveau après ({a} L) < niveau avant ({b} L) pour un ravitaillement.", blocking=True)
        else:
            gap = abs((a - b) - qty)
            tol = max(Decimal("100"), Decimal("0.10") * qty)
            if gap > tol:
                alert("ECART_NIVEAU_QUANTITE", f"Hausse de niveau {a - b} L ≠ quantité réelle {qty} L (écart {gap} L > {tol} L) : à examiner au rapprochement.")
    appr = c.get("approved_quantity_l")
    if appr and qty and qty > appr * Decimal("1.2"):
        alert("QUANTITE_SUP_APPROUVEE", f"Quantité réelle {qty} L > 120 % de la quantité approuvée ({appr} L).")
    blocking = any(x["blocking"] for x in alerts)

    if "quantité réelle > 0" in missing:
        state = "QUANTITE_REELLE_MANQUANTE"
    elif "preuve / photo" in missing:
        state = "PREUVE_MANQUANTE"
    elif missing or blocking:
        state = "TERMINEE"
    else:
        state = "A_RAPPROCHER"

    if fam == "TOTAL_CARD":
        if state == "QUANTITE_REELLE_MANQUANTE":
            return result("DEMANDE_CARTE_TOTAL", state, "Carte Total sans quantité réelle : demande, aucun impact stock ni consommation.")
        if state == "A_RAPPROCHER":
            return result("RAVITAILLEMENT_DECLARE_AVEC_PREUVE", state, REASON_TC_OK, recon="A_RAPPROCHER")
        why = "Non éligible : " + ", ".join(missing + [x["message"] for x in alerts if x["blocking"]])
        return result("RAVITAILLEMENT_DECLARE_INCOMPLET", state, f"Carte Total terminée. {why}.")
    # TRUCK
    if state == "A_RAPPROCHER":
        extra = [lbl for lbl, key in (("fournisseur", "supplier_name"), ("bon de livraison", "delivery_note_number"),
                                       ("référence camion", "vehicle_or_truck_reference")) if not c.get(key)]
        reason = REASON_TRUCK_OK + (f" (à compléter si possible : {', '.join(extra)})" if extra else "")
        return result("LIVRAISON_DECLAREE_A_RAPPROCHER", state, reason, recon="A_RAPPROCHER")
    why = ", ".join(missing + [x["message"] for x in alerts if x["blocking"]])
    return result("LIVRAISON_A_CONFIRMER", state, f"Truck terminé mais incomplet : {why}.")


CANONICAL_FIELDS = ("enoc_request_id", "enoc_reference", "site_id", "site_name", "country", "zone", "raw_movement_type",
                    "raw_status", "requested_quantity_l", "approved_quantity_l", "actual_quantity_l", "quantity_unit",
                    "planned_at", "actual_operation_at", "requested_at", "approved_at", "closed_at", "tank_level_before_l",
                    "tank_level_after_l", "tank_level_before_cm", "tank_level_after_cm", "generator_hourmeter_before_h",
                    "generator_hourmeter_after_h", "technician_name", "validator_name", "closer_name", "comment",
                    "proof_count", "proof_urls_or_ids", "supplier_name", "delivery_note_number",
                    "vehicle_or_truck_reference", "operations_count", "field_sources")
# Valeurs qui, si elles changent à la source, invalident un rapprochement déjà fait.
FINGERPRINT_FIELDS = ("site_id", "raw_movement_type", "raw_status", "actual_quantity_l", "quantity_unit", "actual_operation_at",
                      "tank_level_before_l", "tank_level_after_l", "proof_count", "supplier_name", "delivery_note_number")
QUALIF_FIELDS = ("type_family", "business_classification", "execution_state", "stock_impact", "consumption_impact",
                 "reason", "missing_conditions", "alerts", "blocking", "qualification_version")


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
            q_vals = qualify(c, now)
            fp = fingerprint(c)
            values = {k: c[k] for k in CANONICAL_FIELDS}
            values.update(site_id_resolved=resolver.resolve(c["site_id"]) if c["site_id"] else None, source_fingerprint=fp,
                          synced_at=m.synced_at)
            q = existing.get(m.pk)
            if q is not None and q.reconciliation_status in ("RAPPROCHE", "CERTIFIE", "REJETE"):
                if q.source_fingerprint == fp:
                    # Rapprochement humain conservé : seule la partie source est rafraîchie.
                    q_vals = {k: getattr(q, k) for k in QUALIF_FIELDS}
                    q_vals["reconciliation_status"] = q.reconciliation_status
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


# ── Rapprochement (seule voie vers ÉLIGIBLE_STOCK_RÉEL) ─────────────────────────────────────────────────────
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
    """decision : « rapprocher » (→ ÉLIGIBLE_STOCK_RÉEL, certifié si bon de livraison) ou « rejeter »."""
    from fuel_tracking.services import stock_registry as R

    reason = (reason or "").strip()
    if not reason:
        raise ReconciliationError("Motif obligatoire.")
    if decision == "rapprocher":
        if q.execution_state != "A_RAPPROCHER":
            raise ReconciliationError(f"Mouvement au statut {q.execution_state} : seul un mouvement À_RAPPROCHER peut être rapproché.")
        if q.blocking:
            raise ReconciliationError("Alerte bloquante présente : rapprochement impossible.")
        if not (evidence or "").strip():
            raise ReconciliationError("Élément de rapprochement obligatoire (relevé terrain cohérent, stock, bon fournisseur…).")
        new_recon = "CERTIFIE" if q.delivery_note_number else "RAPPROCHE"
        old = (q.execution_state, q.reconciliation_status)
        q.execution_state, q.reconciliation_status, q.stock_impact, q.consumption_impact = "ELIGIBLE_STOCK_REEL", new_recon, True, True
    elif decision == "rejeter":
        old = (q.execution_state, q.reconciliation_status)
        q.execution_state, q.reconciliation_status, q.stock_impact, q.consumption_impact = "REJETEE", "REJETE", False, False
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
