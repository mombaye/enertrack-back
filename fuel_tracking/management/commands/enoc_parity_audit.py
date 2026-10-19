"""
Audit de parité ENOC → eGrid (LECTURE SEULE) : connecteur, transformation, champs reçus / perdus / renommés / ignorés,
contrôle sur au moins 20 demandes récentes (types et statuts variés), matrice
« champ ENOC | champ eGrid | règle de transformation | taux de présence | écart constaté ».

    python manage.py enoc_parity_audit                                  # Markdown sur la sortie standard
    python manage.py enoc_parity_audit --limit 30 --out /mnt/Suivi_carburant_prod/parite_enoc.md
    python manage.py enoc_parity_audit --mongo                          # + comparaison directe avec MongoDB ENOC (lecture seule)
"""
from collections import Counter, defaultdict
from decimal import Decimal
from io import StringIO

from django.core.management.base import BaseCommand

from fuel_tracking.services import enoc_canonical as C

# (libellé du questionnaire, champ canonique eGrid, clé de K / spécial, champ de la table à plat FuelEnocMovement, règle)
MATRIX = [
    ("Identifiant de demande", "enoc_request_id", "request_id", "request_id", "request._id (texte)"),
    ("Référence", "enoc_reference", "reference", "request_code", "request_code tel quel"),
    ("site_id", "site_id / site_id_resolved", "site_id", "site_id", "brut conservé ; résolu sur Gestion des sites"),
    ("Pays", "country", "country", None, "brut si présent, sinon NULL"),
    ("Zone", "zone", "zone", "zone", "brut"),
    ("Type affiché", "raw_movement_type", "type", "operation_type", "brut tel quel (opération puis demande)"),
    ("Statut", "raw_status", "@status", "status", "request.status brut ; NULL si opération sans demande (jamais inventé)"),
    ("Quantité demandée", "requested_quantity_l", "requested", "requested_quantity_liters", "décimal ; absent → NULL"),
    ("Quantité approuvée", "approved_quantity_l", "approved", "approved_quantity_liters", "décimal ; absent → NULL"),
    ("Quantité réalisée", "actual_quantity_l", "actual", "quantity_added_liters", "OPÉRATION uniquement ; absent → NULL"),
    ("Unité", "quantity_unit", "unit", None, "champ unité ; sinon implicite si le champ quantité est en litres (*_liters)"),
    ("Date prévue", "planned_at", "planned", None, "request.date_refueling"),
    ("Date réelle de ravitaillement", "actual_operation_at", "actual_at", "operation_date", "OPÉRATION uniquement"),
    ("Date de demande", "requested_at", "requested_at", "created_at_source", "request.action.created_at / created_at"),
    ("Date d'approbation", "approved_at", "approved_at", "validated_at_source", "request.action.validated_at"),
    ("Date de clôture", "closed_at", "closed_at", "done_at_source", "action.done_at / done_at / closed_at"),
    ("Niveau avant (L)", "tank_level_before_l", "@lvl_before_l", "level_before", "level_before si unité = L (ou champ *_liters)"),
    ("Niveau après (L)", "tank_level_after_l", "@lvl_after_l", "level_after", "level_after si unité = L (ou champ *_liters)"),
    ("Niveau avant (cm)", "tank_level_before_cm", "@lvl_before_cm", "level_before", "level_before si unité = cm (ou champ *_cm)"),
    ("Niveau après (cm)", "tank_level_after_cm", "@lvl_after_cm", "level_after", "level_after si unité = cm (ou champ *_cm)"),
    ("Compteur GE avant", "generator_hourmeter_before_h", "hm_before", "hour_meter_before", "décimal"),
    ("Compteur GE après", "generator_hourmeter_after_h", "hm_after", "hour_meter_after", "décimal"),
    ("Commentaire", "comment", "comment", "comment", "opération + demande (les deux conservés)"),
    ("Technicien", "technician_name", "technician", "technician_name", "brut"),
    ("Valideur", "validator_name", "validator", "validated_by", "request.action.validated_by"),
    ("Clôture par", "closer_name", "closer", "done_by", "action.done_by / operation.created_by"),
    ("Pièces jointes / photos", "proof_count + proof_urls_or_ids", "@proofs", None,
     "recherche récursive des clés photo/image/attachment/proof/document/file… (demande + opération)"),
    ("Fournisseur", "supplier_name", "supplier", "supplier", "brut"),
    ("Bon de livraison", "delivery_note_number", "delivery_note", "delivery_note_number", "brut"),
    ("Référence camion", "vehicle_or_truck_reference", "vehicle", None, "brut"),
]
CANON_ATTR = {"site_id / site_id_resolved": "site_id", "proof_count + proof_urls_or_ids": "proof_count"}


def _num(v):
    return C.num(v)


def _fmt(v, n=40):
    if v is None:
        return "∅"
    s = str(v)
    return (s[: n - 1] + "…") if len(s) > n else s


class Command(BaseCommand):
    help = "Audit de parité ENOC → eGrid (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--out")
        parser.add_argument("--mongo", action="store_true", help="Compare aussi avec MongoDB ENOC (lecture seule).")

    def handle(self, *args, limit=20, out=None, mongo=False, **opts):
        from fuel_tracking.models import FuelEnocMovement

        buf = StringIO()
        w = buf.write
        movements = list(FuelEnocMovement.objects.all().order_by("-source_updated_at", "-operation_date", "-id"))
        extracted = [(m, C.extract(m)) for m in movements]
        n = len(extracted) or 1

        w("# Audit de parité ENOC → eGrid (lecture seule)\n\n## 1. Connecteur\n\n")
        w("- Source : base MongoDB ENOC, collections `fuel_requests` (demande, validation) et `fuel_operations` (exécution), "
          "lecture seule (`enoc_mongo_service.fetch_fuel_movements`). L'API REST ENOC « /fuel/integrations/enertrack/operations » "
          "est bloquée par filtrage IP (403) : non utilisée.\n")
        w("- Fréquence : tâche Celery toutes les 5 minutes (mois courant et précédent) → `FuelEnocMovement`, puis registre stock.\n")
        w("- Transformation : (1) demande + opération(s) jointes par `request_code` ; documents Mongo complets conservés dans "
          "`raw_payload.request` / `raw_payload.operation` / `raw_payload.operations` ; (2) table à plat `FuelEnocMovement` ; "
          "(3) contrat canonique + qualification `FuelEnocQualifiedMovement` (chemin brut de chaque valeur conservé) ; "
          "(4) registre stock (`fuel_movement`, impact stock = ÉLIGIBLE_STOCK_RÉEL seulement).\n")
        w("- Corrections apportées au connecteur : quantités absentes → NULL (avant : 0) ; toutes les opérations d'une demande "
          "conservées (avant : une seule, les autres écrasées).\n\n")
        w(f"Mouvements ENOC en base : **{len(movements)}** — avec demande : {sum(1 for _, c in extracted if c['_has_request'])}, "
          f"avec opération : {sum(1 for _, c in extracted if c['_has_operation'])}, opérations multiples : "
          f"{sum(1 for _, c in extracted if c['operations_count'] > 1)}.\n\n")

        # 2. Matrice
        w("## 2. Matrice de parité (toutes les demandes)\n\n")
        w("| Champ ENOC (chemin brut constaté) | Champ eGrid | Règle de transformation | Taux de présence | Écart constaté |\n|---|---|---|---|---|\n")
        for label, canon, key, flat, rule in MATRIX:
            paths, present, gaps = Counter(), 0, Counter()
            for m, c in extracted:
                attr = CANON_ATTR.get(canon, canon)
                val = c.get(attr)
                if attr == "proof_count":
                    has = bool(val)
                    if has:
                        paths.update((c["field_sources"].get("proofs") or "").split(", "))
                else:
                    has = val is not None
                    src_key = {"@status": "raw_status", "@lvl_before_l": "level_before_l", "@lvl_after_l": "level_after_l",
                               "@lvl_before_cm": "level_before_cm", "@lvl_after_cm": "level_after_cm"}.get(key, key)
                    if has and c["field_sources"].get(src_key):
                        paths[c["field_sources"][src_key]] += 1
                present += has
                if flat:
                    fv = getattr(m, flat, None)
                    if canon in ("requested_quantity_l", "approved_quantity_l", "actual_quantity_l"):
                        if fv is not None and Decimal(fv) == 0 and val is None:
                            gaps["0 en table à plat alors que la source est vide"] += 1
                        elif fv is not None and val is not None and _num(fv) != _num(val):
                            gaps["valeur différente"] += 1
                    if canon == "raw_status" and val is None and fv:
                        gaps[f"statut « {fv} » inventé par le connecteur (opération sans demande)"] += 1
                    if canon == "actual_operation_at" and val is None and fv is not None:
                        gaps["date à plat = date de demande / prévue (pas une date réelle)"] += 1
                    if canon in ("tank_level_before_l", "tank_level_before_cm", "tank_level_after_l", "tank_level_after_cm"):
                        pass
            src = ", ".join(f"{p} ({k})" for p, k in paths.most_common(3) if p) or "aucun champ trouvé"
            gap = "; ".join(f"{k} : {v}" for k, v in gaps.items()) or "—"
            w(f"| {label} — {src} | {canon} | {rule} | {present}/{len(extracted)} ({100 * present / n:.0f} %) | {gap} |\n")

        # 3. Échantillon (≥ 20 demandes récentes, types et statuts variés)
        w(f"\n## 3. Contrôle de parité sur {limit} demandes récentes (types et statuts variés)\n\n")
        groups = defaultdict(list)
        for m, c in extracted:
            groups[(c["raw_movement_type"], c["raw_status"])].append((m, c))
        sample, i = [], 0
        while len(sample) < limit and any(len(g) > i for g in groups.values()):
            for g in groups.values():
                if len(g) > i and len(sample) < limit:
                    sample.append(g[i])
            i += 1
        w("| Réf. | site_id | Type | Statut | Demandé | Approuvé | Réalisé | Unité | Date prévue | Date réelle | Avant L / cm | Après L / cm "
          "| Compteur av./ap. | Technicien | Valideur / clôture | Preuves | Commentaire | Qualification | Écart table à plat |\n")
        w("|" + "---|" * 19 + "\n")
        for m, c in sample:
            q = C.qualify(c)
            gaps = []
            for flat, canon in (("requested_quantity_liters", "requested_quantity_l"), ("approved_quantity_liters", "approved_quantity_l"),
                                ("quantity_added_liters", "actual_quantity_l")):
                fv = getattr(m, flat)
                if fv is not None and Decimal(fv) == 0 and c[canon] is None:
                    gaps.append(f"{flat}=0 (source vide)")
            if c["raw_status"] is None and m.status:
                gaps.append(f"statut à plat « {m.status} » non issu d'ENOC")
            w(f"| {_fmt(c['enoc_reference'], 18)} | {c['site_id'] or '∅'} | {_fmt(c['raw_movement_type'], 14)} | {c['raw_status'] or '∅'} "
              f"| {_fmt(c['requested_quantity_l'])} | {_fmt(c['approved_quantity_l'])} | {_fmt(c['actual_quantity_l'])} | {c['quantity_unit'] or '∅'} "
              f"| {_fmt(c['planned_at'] and c['planned_at'].date())} | {_fmt(c['actual_operation_at'] and c['actual_operation_at'].date())} "
              f"| {_fmt(c['tank_level_before_l'])} / {_fmt(c['tank_level_before_cm'])} | {_fmt(c['tank_level_after_l'])} / {_fmt(c['tank_level_after_cm'])} "
              f"| {_fmt(c['generator_hourmeter_before_h'])} / {_fmt(c['generator_hourmeter_after_h'])} | {_fmt(c['technician_name'], 18)} "
              f"| {_fmt(c['validator_name'], 14)} / {_fmt(c['closer_name'], 14)} | {c['proof_count']} | {_fmt(c['comment'], 30)} "
              f"| {q['business_classification']} · {q['execution_state']} | {'; '.join(gaps) or '—'} |\n")

        # 4. Inventaire des champs bruts
        w("\n## 4. Champs bruts ENOC : utilisés / ignorés\n\n")
        used = {k.split(".")[0] for keys in C.K.values() for k in keys} | {"status"}
        seen = {"request": Counter(), "operation": Counter()}
        for m, _ in extracted:
            raw = m.raw_payload or {}
            for name in ("request", "operation"):
                doc = raw.get(name)
                if isinstance(doc, dict):
                    seen[name].update(k for k, v in doc.items() if not C._empty(v))
        for name in ("request", "operation"):
            w(f"**{name}** — clés renseignées (présence) : \n\n| Clé brute | Présence | Exploitée par eGrid |\n|---|---|---|\n")
            for k, v in seen[name].most_common():
                status = "oui" if (k in used or C.PROOF_KEY_RE.search(k)) else (
                    "conservée en brut (contexte GE / objectif mensuel / ponction)" if k in ("ge_snapshot", "ponction", "monthly_target_liters",
                                                                                          "monthly_total_after_liters", "target_percent_after",
                                                                                          "target_status", "is_target_exceeded")
                    else "ignorée (conservée en brut)")
                w(f"| {k} | {v}/{len(extracted)} | {status} |\n")
            w("\n")
        missing = [label for label, canon, *_ in MATRIX
                   if not any((c.get(CANON_ATTR.get(canon, canon)) not in (None, 0)) for _, c in extracted)]
        w("**Champs du contrat canonique jamais trouvés dans les données ENOC reçues** : "
          + (", ".join(missing) if missing else "aucun") + ".\n")

        # 5. Comparaison directe MongoDB
        if mongo:
            w("\n## 5. Comparaison directe avec MongoDB ENOC (lecture seule)\n\n")
            try:
                from django.conf import settings

                from fuel_tracking.services.enoc_mongo_service import _connect

                client = _connect()
                try:
                    db = client[settings.ENOC_MONGO_DB_NAME]
                    n_req = db.fuel_requests.count_documents({})
                    n_op = db.fuel_operations.count_documents({})
                    multi = list(db.fuel_operations.aggregate([{"$group": {"_id": "$request_code", "n": {"$sum": 1}}},
                                                              {"$match": {"n": {"$gt": 1}}}, {"$count": "c"}]))
                    cols = sorted(db.list_collection_names())
                    w(f"- fuel_requests : {n_req} ; fuel_operations : {n_op} ; demandes à plusieurs opérations : "
                      f"{multi[0]['c'] if multi else 0} ; mouvements eGrid : {len(movements)}.\n")
                    w(f"- Collections Mongo : {', '.join(cols)}.\n")
                    w("- Collections pouvant contenir des preuves (nom évocateur) : "
                      + (", ".join(c for c in cols if C.PROOF_KEY_RE.search(c) or "media" in c.lower()) or "aucune") + ".\n")
                    egrid = {m.source_id for m in movements}
                    absent = [d.get("request_code") for d in db.fuel_requests.find({}, {"request_code": 1}).sort("_id", -1).limit(500)
                              if d.get("request_code") and d.get("request_code") not in egrid]
                    w(f"- Demandes Mongo récentes (500 dernières) absentes d'eGrid : {len(absent)}"
                      + (f" (ex. {', '.join(absent[:10])})" if absent else "") + ".\n")
                finally:
                    client.close()
            except Exception as e:
                w(f"**Connexion MongoDB impossible : {type(e).__name__}** — la parité ci-dessus repose sur les documents bruts "
                  "déjà copiés par le connecteur.\n")

        text = buf.getvalue()
        if out:
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(text)
            self.stdout.write(f"Audit écrit dans {out}")
        else:
            self.stdout.write(text)
