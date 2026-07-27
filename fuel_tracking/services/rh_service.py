# fuel_tracking/services/rh_service.py
"""
Calcul des Running Hours (RH) GE par site sur une période.

Cascade (SQL/Snowflake prioritaire, ENOC en secours) :
  1. Compteur cumulatif DSE (GENSET_REPORT.TOTAL_DG_RUNNING_HOUR, delta MAX-MIN)
  2. Statut GE haute fréquence (GFMS_METRICS.GE_STATUS, sites avec DSE)
  3. Statut redresseur haute fréquence (GFMS_METRICS.RECTIFIER_STATUS, sites sans DSE)
  4. Repli : relevé compteur horaire ENOC (FuelEnocMovement.hour_meter_before/after)

Le mapping site_id -> DATA_ID et la détection avec/sans DSE (CONTROLLER_VENDOR)
viennent tous les deux de Snowflake (SITE_ESCO_CURRENT / SITE_DG).
"""
import logging
from decimal import Decimal

from certification.services.snowflake_service import SnowflakeService

logger = logging.getLogger(__name__)


class RhCalculationService:

    SOURCE_DSE_COUNTER = "SNOWFLAKE_DSE_COUNTER"
    SOURCE_GE_STATUS    = "SNOWFLAKE_GE_STATUS"
    SOURCE_RECTIFIER    = "SNOWFLAKE_RECTIFIER_STATUS"
    SOURCE_ENOC         = "ENOC_HOUR_METER"
    SOURCE_NONE         = "NO_DATA"

    def __init__(self):
        self.sf = SnowflakeService()

    def compute_rh_batch(self, site_ids: list[str], date_debut, date_fin) -> dict[str, dict]:
        """
        Retourne {site_id: {"rh_hours": Decimal|None, "source": str, "avec_dse": bool|None}}
        """
        result = {
            sid: {"rh_hours": None, "source": self.SOURCE_NONE, "avec_dse": None}
            for sid in site_ids
        }
        if not site_ids:
            return result

        try:
            data_id_map = self.sf.get_data_id_map(site_ids)
            vendor_map = self.sf.get_controller_vendor_map(site_ids)
            grid_status_map = self.sf.get_grid_status_map(site_ids)
        except Exception as e:
            logger.warning("[RH] Snowflake indisponible, repli ENOC total: %s", e)
            self._apply_enoc_fallback(result, site_ids, date_debut, date_fin)
            return result

        data_ids = list(data_id_map.values())

        counter_deltas  = self.sf.get_genset_counter_delta(data_ids, date_debut, date_fin)
        ge_status_hours = self.sf.get_status_on_hours(data_ids, date_debut, date_fin, "GE_STATUS")
        rect_hours      = self.sf.get_status_on_hours(data_ids, date_debut, date_fin, "RECTIFIER_STATUS")

        # Borne de plausibilité : un mois ne peut physiquement pas dépasser
        # (nb jours × 24h), + marge de sécurité pour les effets de bord. Certains
        # relevés GENSET_REPORT.TOTAL_DG_RUNNING_HOUR remontent une valeur
        # aberrante isolée (constaté : plusieurs sites avec un delta ~1 190 000h
        # sur un seul mois) — sans ce garde-fou, MAX-MIN capte cet outlier et
        # produit un RH délirant au lieu de retomber sur GE Status/Redresseur/ENOC.
        max_plausible_hours = (date_fin - date_debut).days * 24 + 48

        for site_id, data_id in data_id_map.items():
            entry = result[site_id]
            is_off_grid = (grid_status_map.get(site_id) or "").strip().lower() == "off grid"

            # Une lecture compteur valide prouve la présence d'un DSE fonctionnel,
            # même si le référentiel SITE_DG.CONTROLLER_VENDOR est vide (trou de donnée fréquent).
            cd = counter_deltas.get(data_id)
            has_valid_counter = bool(
                cd and cd["delta"] is not None and 0 < cd["delta"] <= max_plausible_hours
            )

            vendor_is_dse = (vendor_map.get(site_id) or "").strip().lower() == "deepsea"
            avec_dse = has_valid_counter or vendor_is_dse
            entry["avec_dse"] = avec_dse

            if has_valid_counter:
                entry["rh_hours"] = Decimal(str(cd["delta"]))
                entry["source"] = self.SOURCE_DSE_COUNTER
                continue

            gs = ge_status_hours.get(data_id)
            if avec_dse and gs and gs["heures"] is not None:
                entry["rh_hours"] = Decimal(str(gs["heures"]))
                entry["source"] = self.SOURCE_GE_STATUS
                continue

            # Le redresseur n'est un proxy fiable du GE que sur un site Off-Grid
            # (sans secteur, le redresseur ne peut être alimenté que par le GE ou le solaire).
            rh = rect_hours.get(data_id)
            if not avec_dse and is_off_grid and rh and rh["heures"] is not None:
                entry["rh_hours"] = Decimal(str(rh["heures"]))
                entry["source"] = self.SOURCE_RECTIFIER
                continue

            if gs and gs["heures"] is not None:
                entry["rh_hours"] = Decimal(str(gs["heures"]))
                entry["source"] = self.SOURCE_GE_STATUS

        missing = [sid for sid in site_ids if result[sid]["rh_hours"] is None]
        if missing:
            self._apply_enoc_fallback(result, missing, date_debut, date_fin)

        return result

    def _apply_enoc_fallback(self, result: dict, site_ids: list[str], date_debut, date_fin) -> None:
        from django.db.models import Q

        from fuel_tracking.models import FuelEnocMovement

        # Historique complet jusqu'à la fin de période (pas de borne basse) : le
        # relevé "initial" peut dater d'un mois antérieur si le site n'a été
        # visité qu'une fois pendant la période cible — exiger 2 relevés
        # strictement dans le mois calendaire ratait ~90% des sites en secours
        # ENOC (un seul ravitaillement/mois est la norme, pas l'exception).
        # Même logique que "Date de Relevé Initiale/Finale" du fichier Ops
        # (le relevé réel le plus proche, pas une fenêtre calendaire rigide).
        movements = (
            FuelEnocMovement.objects
            .filter(site_id__in=site_ids, operation_date__date__lte=date_fin)
            .filter(Q(hour_meter_before__isnull=False) | Q(hour_meter_after__isnull=False))
            .order_by("site_id", "operation_date")
        )

        by_site: dict[str, list] = {}
        for m in movements:
            by_site.setdefault(m.site_id, []).append(m)

        def reading_value(m):
            return m.hour_meter_after if m.hour_meter_after is not None else m.hour_meter_before

        for site_id, site_movements in by_site.items():
            # "Final" : dernier relevé DANS la période cible — preuve qu'il y a
            # eu une visite ce mois-ci, sinon on n'a aucune évidence d'activité.
            in_period = [m for m in site_movements if date_debut <= m.operation_date.date() <= date_fin]
            if not in_period:
                continue

            final_m = in_period[-1]
            final_value = reading_value(final_m)
            if final_value is None:
                continue

            # "Initial" : dernier relevé valide strictement avant le relevé
            # final, même antérieur à la période (site visité une seule fois).
            initial_value = None
            initial_m = None
            for m in reversed([m for m in site_movements if m.operation_date < final_m.operation_date]):
                v = reading_value(m)
                if v is not None:
                    initial_value = v
                    initial_m = m
                    break

            if initial_value is None:
                continue

            delta = final_value - initial_value

            # Garde-fou de plausibilité (même principe que le compteur DSE) : le
            # relevé initial pouvant dater d'un mois antérieur, la borne se base
            # sur l'écart réel entre les deux relevés, pas sur le mois cible seul.
            span_days = max((final_m.operation_date.date() - initial_m.operation_date.date()).days, 1)
            max_plausible = span_days * 24 + 48

            if delta and 0 < delta <= max_plausible:
                result[site_id]["rh_hours"] = delta
                result[site_id]["source"] = self.SOURCE_ENOC
