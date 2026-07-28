# fuel_tracking/services/rms_service.py
"""
Stock Ouv./Clôt. RMS (niveau de cuve télémétrie) par site sur un mois.

Réutilise GFMS_DATA_TRACKER_NC.IM_GENERATOR_FUEL_LEVEL (même table brute que
le RH), au plus proche du 1er et du dernier jour du mois. Le mapping
site_id -> DATA_ID vient de Snowflake (SITE_ESCO_CURRENT), comme pour le RH.
"""
import logging

from certification.services.snowflake_service import SnowflakeService

logger = logging.getLogger(__name__)


class StockRmsService:

    def __init__(self):
        self.sf = SnowflakeService()

    def compute_rms_batch(self, site_ids: list[str], date_debut, date_fin, window_days: int = 3) -> dict[str, dict]:
        """
        Retourne {site_id: {"ouv_rms": Decimal|None, "ouv_rms_at": datetime|None,
                             "clot_rms": Decimal|None, "clot_rms_at": datetime|None}}
        """
        result = {sid: {"ouv_rms": None, "ouv_rms_at": None, "clot_rms": None, "clot_rms_at": None} for sid in site_ids}
        if not site_ids:
            return result

        try:
            data_id_map = self.sf.get_data_id_map(site_ids)
        except Exception as e:
            logger.warning("[StockRMS] Snowflake indisponible, pas de niveau de cuve: %s", e)
            return result

        data_ids = list(data_id_map.values())

        try:
            capacity_by_site = self.sf.get_tank_capacity_map(site_ids)
        except Exception as e:
            logger.warning("[StockRMS] Capacité de cuve indisponible, pas de filtrage: %s", e)
            capacity_by_site = {}
        capacity_by_data_id = {
            data_id_map[sid]: cap for sid, cap in capacity_by_site.items() if sid in data_id_map
        }

        ouv_by_data_id = self.sf.get_fuel_level_near_date(
            data_ids, date_debut, window_days=window_days, capacity_by_data_id=capacity_by_data_id,
        )
        clot_by_data_id = self.sf.get_fuel_level_near_date(
            data_ids, date_fin, window_days=window_days, capacity_by_data_id=capacity_by_data_id,
        )

        for site_id, data_id in data_id_map.items():
            entry = result[site_id]
            ouv = ouv_by_data_id.get(data_id)
            if ouv:
                entry["ouv_rms"] = ouv.get("level")
                entry["ouv_rms_at"] = ouv.get("timestamp")

            clot = clot_by_data_id.get(data_id)
            if clot:
                entry["clot_rms"] = clot.get("level")
                entry["clot_rms_at"] = clot.get("timestamp")

        return result
