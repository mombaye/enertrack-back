# fuel_tracking/services/enoc_mongo_service.py
"""
Accès direct à la base MongoDB d'ENOC (deffar_backend) pour les données
référentielles site / GE (marque, KVA, cuve, priorité...).

Contourne l'API REST ENOC (/fuel/integrations/enertrack/operations, port 8001)
qui est en panne — ces données statiques ne dépendent pas des mouvements
carburant et ne devraient de toute façon pas être lues depuis leur payload.

Lecture seule.
"""
import logging

from django.conf import settings
from pymongo import MongoClient

logger = logging.getLogger(__name__)


class EnocMongoConnectionError(Exception):
    pass


class EnocMongoService:

    def __init__(self):
        self.host = getattr(settings, "ENOC_MONGO_HOST", "")
        self.port = int(getattr(settings, "ENOC_MONGO_PORT", 27017))
        self.username = getattr(settings, "ENOC_MONGO_USERNAME", "")
        self.password = getattr(settings, "ENOC_MONGO_PASSWORD", "")
        self.db_name = getattr(settings, "ENOC_MONGO_DB_NAME", "")

    def _connect(self) -> MongoClient:
        try:
            uri = f"mongodb://{self.username}:{self.password}@{self.host}:{self.port}/{self.db_name}?authSource=admin"
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            client.admin.command("ping")
            return client
        except Exception as e:
            raise EnocMongoConnectionError(f"Connexion MongoDB ENOC échouée: {e}") from e

    def check_connection(self) -> bool:
        try:
            client = self._connect()
            client.close()
            return True
        except Exception:
            return False

    def get_sites_ref(self, site_ids: list[str]) -> dict[str, dict]:
        """site_id -> attributs référentiels statiques (collection `sites`)."""
        if not site_ids:
            return {}
        client = self._connect()
        try:
            db = client[self.db_name]
            docs = db.sites.find({"site_id": {"$in": site_ids}})
            result = {}
            for d in docs:
                sid = d.get("site_id")
                if not sid:
                    continue
                result[sid] = {
                    "site_id": sid,
                    "site_name": d.get("site_name"),
                    "priority": d.get("site_priority"),
                    "category": d.get("categorie"),
                    "nb_ge": d.get("nb_ge"),
                    "ge1_power_kva": d.get("puissance_ge1_kva"),
                    "ge2_power_kva": d.get("puissance_ge2_kva"),
                    "fuel_tank_capacity_liters": d.get("volume_cuve_fuel_l"),
                    "load": d.get("load"),
                    "new_load": d.get("new_load"),
                    "new_load_contract_v2": d.get("new_load_contract_v2"),
                    "typology_contractual": d.get("typologie"),
                    "typo_simple": d.get("typo_simple"),
                    "new_typo": d.get("new_typo"),
                    "ongrid_offgrid": d.get("ongrid_offgrid"),
                    "indoor_outdoor_after_passive": d.get("indoor_outdoor_after_passive"),
                    "installation_rms": d.get("installation_rms"),
                    "batch": d.get("batch"),
                    "batch_operational": d.get("batch_operationnel"),
                    "zone": d.get("zone") or d.get("region"),
                    "ville": d.get("ville"),
                    "generator_change": d.get("changement_generateur"),
                    "solar_power_kw": d.get("puissance_total_solaire_kw"),
                    "solar_nominal_power_wc": d.get("puissance_nominale_solaire_wc"),
                    "pv_count": d.get("nbre_pv_installer"),
                    "battery_capacity_ah": d.get("capacite_totale_batterie_ah"),
                    "battery_manufacturer": d.get("fabricant_batteries"),
                    "battery_status": d.get("status_batteries"),
                    "latitude": d.get("latitude"),
                    "longitude": d.get("longitude"),
                }
            return result
        finally:
            client.close()

    def get_ge_assets(self, site_ids: list[str]) -> dict[str, list[dict]]:
        """site_id -> liste des GE actifs (collection `ge_assets`), triés (installés d'abord)."""
        if not site_ids:
            return {}
        client = self._connect()
        try:
            db = client[self.db_name]
            docs = db.ge_assets.find({
                "current_site_id": {"$in": site_ids},
                "asset_status": {"$nin": ["REMOVED", "TO_REMOVE"]},
            })
            by_site: dict[str, list[dict]] = {}
            for d in docs:
                sid = d.get("current_site_id")
                if not sid:
                    continue
                by_site.setdefault(sid, []).append({
                    "ge_id": str(d.get("_id")),
                    "serial": d.get("serial_number"),
                    "brand": d.get("brand"),
                    "model": d.get("model"),
                    "controller": d.get("panel_model"),
                    "status": d.get("asset_status"),
                    "fixed_mobile": d.get("fixe_mobile"),
                    "power_kva": d.get("power_kva"),
                    "tank_capacity_liters": d.get("tank_capacity_liters"),
                    "tank_shape": d.get("tank_shape"),
                    "updated_at": str(d.get("updated_at")) if d.get("updated_at") else None,
                })
            for sid, assets in by_site.items():
                assets.sort(key=lambda a: 0 if a["status"] == "INSTALLED" else 1)
            return by_site
        finally:
            client.close()

    def get_fuel_targets(self, site_ids: list[str]) -> dict[str, float]:
        """site_id -> monthly_target_liters (collection `fuel_targets`, cible Aktivco)."""
        if not site_ids:
            return {}
        client = self._connect()
        try:
            db = client[self.db_name]
            docs = db.fuel_targets.find({"site_id": {"$in": site_ids}, "active": True})
            return {
                d["site_id"]: d.get("monthly_target_liters")
                for d in docs
                if d.get("site_id") and d.get("monthly_target_liters")
            }
        finally:
            client.close()
