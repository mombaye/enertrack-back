# fuel_tracking/services/enoc_mongo_service.py
"""
Accès direct à la base MongoDB d'ENOC (deffar_backend) — contourne l'API REST
ENOC (bloquée par filtrage IP côté ENOC, erreur 403) en lisant directement
les collections `fuel_requests` (demande + validation) et `fuel_operations`
(exécution réelle du ravitaillement), déjà utilisées par la plateforme ENOC
elle-même pour servir cette même API. Lecture seule.

Les deux collections sont reliées par `request_code` :
  - une demande (`fuel_requests`) peut exister sans opération correspondante
    (pas encore ravitaillée) ;
  - une opération (`fuel_operations`) peut exister sans demande explicite
    (ex: PONCTION directe entre deux sites, créée sans passer par une demande).
Les deux cas sont couverts ci-dessous.

Les items retournés ont EXACTEMENT la même forme que ceux attendus par
clean_payload() dans sync_enoc_fuel_movements.py (mêmes clés que l'ancien
payload REST ENOC) — aucune transformation de date/Decimal nécessaire ici,
clean_payload()/parse_dt() s'en chargent déjà.
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class EnocMongoConnectionError(Exception):
    pass


def _connect():
    from pymongo import MongoClient

    uri = (
        f"mongodb://{settings.ENOC_MONGO_USERNAME}:{settings.ENOC_MONGO_PASSWORD}"
        f"@{settings.ENOC_MONGO_HOST}:{settings.ENOC_MONGO_PORT}/{settings.ENOC_MONGO_DB_NAME}"
        "?authSource=admin"
    )
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=8000)
        client.admin.command("ping")
        return client
    except Exception as e:
        raise EnocMongoConnectionError(f"Connexion MongoDB ENOC échouée : {e}") from e


def _jsonable(doc):
    """Rend un document Mongo sérialisable en JSON (ObjectId/datetime -> str) pour raw_payload."""
    from bson import ObjectId
    from datetime import datetime

    if doc is None:
        return None
    if isinstance(doc, ObjectId):
        return str(doc)
    if isinstance(doc, datetime):
        return doc.isoformat()
    if isinstance(doc, dict):
        return {k: _jsonable(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [_jsonable(v) for v in doc]
    return doc


def fetch_fuel_movements() -> list[dict]:
    """
    Combine `fuel_requests` + `fuel_operations` en une liste de mouvements
    "à plat", au format attendu par clean_payload(). Le volume est faible
    (quelques centaines de documents au total) : pas de filtrage par date
    côté Mongo, tout est rapatrié à chaque appel — le upsert Postgres en aval
    (unique_fields=source_system/source_id) gère les mises à jour sans
    dupliquer.
    """
    client = _connect()
    try:
        db = client[settings.ENOC_MONGO_DB_NAME]

        operations_by_code: dict[str, dict] = {}
        for op in db.fuel_operations.find():
            code = op.get("request_code")
            if code:
                operations_by_code[code] = op

        movements: list[dict] = []
        seen_codes: set[str] = set()

        for req in db.fuel_requests.find():
            code = req.get("request_code") or str(req.get("_id"))
            seen_codes.add(code)
            op = operations_by_code.get(code)
            action = req.get("action") or {}

            movements.append({
                "source_id": code,
                "request_id": str(req.get("_id")),
                "request_code": req.get("request_code"),
                "status": req.get("status") or "pending",
                "site_id": req.get("site_id"),
                "site_name": req.get("site_name"),
                "zone": req.get("zone"),
                "ville": req.get("ville"),
                "operation_type": (op or {}).get("operation_type") or req.get("operation_type"),
                "operation_date": (op or {}).get("operation_date") or req.get("date_refueling") or req.get("created_at"),
                "requested_quantity_liters": req.get("requested_quantity_liters"),
                "approved_quantity_liters": req.get("approved_quantity_liters"),
                "quantity_added_liters": (op or {}).get("quantity_added_liters"),
                "level_before": req.get("level_before"),
                "level_before_unit": req.get("level_before_unit"),
                "level_after": (op or {}).get("level_after"),
                "level_after_unit": (op or {}).get("level_after_unit"),
                "hour_meter_before": req.get("hour_meter_before"),
                "hour_meter_after": (op or {}).get("hour_meter_after"),
                "monthly_target_liters": (op or {}).get("monthly_target_liters"),
                "monthly_total_after_liters": (op or {}).get("monthly_total_after_liters"),
                "target_percent_after": (op or {}).get("target_percent_after"),
                "target_status": (op or {}).get("target_status"),
                "is_target_exceeded": (op or {}).get("is_target_exceeded"),
                "ge_snapshot": (op or {}).get("ge_snapshot") or req.get("ge_snapshot") or {},
                "ponction": (op or {}).get("ponction") or req.get("ponction"),
                "technician_name": req.get("technician_name"),
                "technician_phone": req.get("technician_phone"),
                "team": req.get("team"),
                "teammate": req.get("teammate"),
                "rm": req.get("rm"),
                "created_by": action.get("created_by") or req.get("user_name"),
                "validated_by": action.get("validated_by"),
                "done_by": (op or {}).get("created_by"),
                "created_at": action.get("created_at") or req.get("created_at"),
                "validated_at": action.get("validated_at") or req.get("validated_at"),
                "done_at": (op or {}).get("created_at"),
                "source_created_at": req.get("created_at"),
                "source_updated_at": req.get("updated_at"),
                "comment": (op or {}).get("comment") or req.get("comment"),
                "site_context": None,
                "ge_context": None,
                "request": _jsonable(req),
                "operation": _jsonable(op) if op else None,
            })

        # Opérations sans demande correspondante dans fuel_requests (ex: PONCTION directe)
        for code, op in operations_by_code.items():
            if code in seen_codes:
                continue
            movements.append({
                "source_id": code,
                "request_id": op.get("request_id"),
                "request_code": op.get("request_code"),
                "status": "done",
                "site_id": op.get("site_id"),
                "site_name": op.get("site_name"),
                "zone": op.get("zone"),
                "ville": op.get("ville"),
                "operation_type": op.get("operation_type"),
                "operation_date": op.get("operation_date") or op.get("created_at"),
                "requested_quantity_liters": None,
                "approved_quantity_liters": None,
                "quantity_added_liters": op.get("quantity_added_liters"),
                "level_before": None,
                "level_before_unit": None,
                "level_after": op.get("level_after"),
                "level_after_unit": op.get("level_after_unit"),
                "hour_meter_before": None,
                "hour_meter_after": op.get("hour_meter_after"),
                "monthly_target_liters": op.get("monthly_target_liters"),
                "monthly_total_after_liters": op.get("monthly_total_after_liters"),
                "target_percent_after": op.get("target_percent_after"),
                "target_status": op.get("target_status"),
                "is_target_exceeded": op.get("is_target_exceeded"),
                "ge_snapshot": op.get("ge_snapshot") or {},
                "ponction": op.get("ponction"),
                "technician_name": None,
                "technician_phone": None,
                "team": None,
                "teammate": None,
                "rm": None,
                "created_by": op.get("created_by"),
                "validated_by": None,
                "done_by": op.get("created_by"),
                "created_at": op.get("created_at"),
                "validated_at": None,
                "done_at": op.get("created_at"),
                "source_created_at": op.get("created_at"),
                "source_updated_at": None,
                "comment": op.get("comment"),
                "site_context": None,
                "ge_context": None,
                "request": None,
                "operation": _jsonable(op),
            })

        return movements
    finally:
        client.close()


def fetch_genset_reference() -> dict[str, dict]:
    """
    Référentiel "site a un GE ou non" côté ENOC — deux collections, prises en
    union (un site est considéré avec GE si l'une OU l'autre le confirme) :
      - `ge_assets` (registre physique par groupe électrogène, avec statut
        d'installation réel — REMOVED/TO_REMOVE/HS_SITE_PARTIEL exclus,
        seul INSTALLED compte) : la plus fiable, mais peut être en retard
        sur la réalité terrain.
      - `sites.nb_ge` (champ déclaratif du référentiel site) : couverture
        plus large mais moins précise (pas de notion de statut).

    Retourne {site_id: {"has_genset": bool, "nb_ge": int|None, "source": str}}.
    """
    client = _connect()
    try:
        db = client[settings.ENOC_MONGO_DB_NAME]

        installed_sites: set[str] = set(
            db.ge_assets.distinct("site_id", {"asset_status": "INSTALLED"})
        )
        all_asset_sites: set[str] = set(db.ge_assets.distinct("site_id"))

        result: dict[str, dict] = {}

        for doc in db.sites.find({}, {"site_id": 1, "nb_ge": 1}):
            site_id = doc.get("site_id")
            if not site_id:
                continue
            nb_ge = doc.get("nb_ge") or 0
            result[site_id] = {
                "has_genset": bool(nb_ge and nb_ge > 0),
                "nb_ge": nb_ge,
                "source": "sites.nb_ge",
            }

        for site_id in all_asset_sites:
            entry = result.setdefault(site_id, {"has_genset": False, "nb_ge": None, "source": "ge_assets"})
            if site_id in installed_sites:
                entry["source"] = f"{entry['source']}+ge_assets" if entry["has_genset"] else "ge_assets"
                entry["has_genset"] = True

        return result
    finally:
        client.close()


def fetch_estimated_consumption(year: int, month: int) -> dict[str, dict]:
    """
    Estimation de consommation à partir des relevés de niveau de cuve
    (`fuel_level_readings`) — UNIQUEMENT pour les relevés où `level_liters`
    est déjà calculé par ENOC (jamais de conversion cm->litres ici : sans les
    dimensions physiques de chaque cuve, cette conversion serait fausse pour
    les cuves cylindriques horizontales, la forme la plus courante).

    Résultat volontairement marqué "ESTIMÉ", jamais "mesuré" — l'appelant
    (sync_fuel_consommation) doit le stocker dans un champ distinct de
    conso_snowflake_l. Deux réserves fortes à connaître :
      - TOUTE la collection `fuel_level_readings` (5272 relevés au 07/08) a
        source="import"/submitted_by_name="... (import historique)" — c'est
        une capture figée d'un import ponctuel, pas un flux vivant qui
        s'actualise. Rien ne garantit que de nouveaux relevés arriveront.
      - Certains relevés importés portent des valeurs par défaut suspectes
        (level_liters=0.0 ET level_cm=None, identiques sur plusieurs dates
        pour un même site) — exclues ci-dessous (isolées visuellement lors
        de l'exploration du 07/08, ex: site KLD_0001 en juin 2026).

    Garde-fous appliqués :
      - relevés avec level_liters=0 ET level_cm=None écartés (valeur par
        défaut probable, pas une vraie mesure) ;
      - écart minimum de 3 jours entre le 1er et le dernier relevé exigé
        (des relevés importés à quelques millisecondes d'intervalle ne
        représentent pas une évolution réelle dans le temps).

    Principe : pour un site avec >= 2 relevés valides dans le mois,
    conso_estimee = (niveau au 1er relevé) - (niveau au dernier relevé)
                    + (litres ajoutés entre les deux, via fuel_operations)
    Un résultat négatif indique une incohérence (ravitaillement non
    enregistré, relevé erroné...) — retourné à None plutôt qu'un chiffre
    trompeur.

    Retourne {site_id: {"conso_estimee_l": Decimal|None, "nb_releves": int,
    "date_debut": str, "date_fin": str}}.
    """
    from datetime import datetime, timedelta

    d_start = datetime(year, month, 1)
    d_end = (d_start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(seconds=1)
    MIN_SPAN = timedelta(days=3)

    client = _connect()
    try:
        db = client[settings.ENOC_MONGO_DB_NAME]

        readings_by_site: dict[str, list[tuple]] = {}
        cursor = db.fuel_level_readings.find({
            "date_releve": {"$gte": d_start.isoformat(), "$lte": d_end.isoformat()},
            "tanks.level_liters": {"$ne": None},
        })
        for doc in cursor:
            site_id = doc.get("site_id")
            date_str = doc.get("date_releve")
            if not site_id or not date_str:
                continue
            tanks = doc.get("tanks") or []
            # Écarte les valeurs par défaut suspectes (0.0 sans niveau cm associé).
            liters = [
                t.get("level_liters") for t in tanks
                if t.get("level_liters") is not None
                and not (t.get("level_liters") == 0 and t.get("level_cm") is None)
            ]
            if not liters:
                continue
            readings_by_site.setdefault(site_id, []).append((date_str, sum(liters)))

        result: dict[str, dict] = {}
        for site_id, readings in readings_by_site.items():
            readings.sort(key=lambda r: r[0])
            if len(readings) < 2:
                continue
            first_date, first_level = readings[0]
            last_date, last_level = readings[-1]

            try:
                span = datetime.fromisoformat(last_date) - datetime.fromisoformat(first_date)
            except ValueError:
                continue
            if span < MIN_SPAN:
                continue

            refills = db.fuel_operations.aggregate([
                {"$match": {
                    "site_id": site_id,
                    "operation_date": {"$gte": first_date, "$lte": last_date},
                }},
                {"$group": {"_id": None, "total": {"$sum": "$quantity_added_liters"}}},
            ])
            refill_total = next(refills, {}).get("total") or 0

            raw = (first_level - last_level) + refill_total
            result[site_id] = {
                "conso_estimee_l": raw if raw >= 0 else None,
                "nb_releves": len(readings),
                "date_debut": first_date,
                "date_fin": last_date,
            }

        return result
    finally:
        client.close()
