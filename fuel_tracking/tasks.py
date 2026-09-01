# fuel_tracking/tasks.py
"""
Tâches Celery planifiées (voir CELERY_BEAT_SCHEDULE dans settings.py) pour
la synchronisation automatique du module Suivi Carburant — exécutées
périodiquement pour rattraper rapidement les nouvelles données Snowflake/
ENOC sans attendre une intervention manuelle. Chaque commande gère déjà sa
propre traçabilité (FuelConsommationSyncRun / FuelEnocSyncRun /
FuelCphSyncRun / FuelStockSyncRun) et n'écrase que le mois courant (upsert
par site) ou l'état courant (Stock, pas de notion de mois).

sync_fuel_cph et sync_fuel_stock ont longtemps été absentes d'ici (2026-08)
— jamais planifiées, seulement lancées manuellement pendant les tests —
d'où les colonnes Type de GE/Running Time/Énergie site vides et l'onglet
Stock jamais alimenté en prod malgré Consommation/ENOC qui tournaient bien.
"""
import logging

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="fuel_tracking.sync_fuel_consommation_current_month")
def sync_fuel_consommation_current_month(self):
    from django.core.management import call_command

    month = timezone.now().strftime("%Y-%m")
    try:
        call_command("sync_fuel_consommation", month=month)
    except Exception:
        logger.exception("[fuel_tracking] Échec sync_fuel_consommation planifiée (%s)", month)


@shared_task(bind=True, name="fuel_tracking.sync_enoc_fuel_movements_current_month")
def sync_enoc_fuel_movements_current_month(self):
    from django.core.management import call_command

    month = timezone.now().strftime("%Y-%m")
    try:
        call_command("sync_enoc_fuel_movements", month=month)
    except Exception:
        # sync_enoc_fuel_movements enregistre déjà l'échec dans FuelEnocSyncRun
        # avant de relever l'exception — on l'attrape ici juste pour ne pas
        # faire échouer bruyamment la tâche planifiée toutes les 5 min.
        logger.exception("[fuel_tracking] Échec sync_enoc_fuel_movements planifiée (%s)", month)


@shared_task(bind=True, name="fuel_tracking.sync_fuel_cph_current_month")
def sync_fuel_cph_current_month(self):
    from django.core.management import call_command

    month = timezone.now().strftime("%Y-%m")
    try:
        call_command("sync_fuel_cph", month=month)
    except Exception:
        logger.exception("[fuel_tracking] Échec sync_fuel_cph planifiée (%s)", month)


@shared_task(bind=True, name="fuel_tracking.sync_fuel_stock_current")
def sync_fuel_stock_current(self):
    from django.core.management import call_command

    try:
        call_command("sync_fuel_stock")
    except Exception:
        logger.exception("[fuel_tracking] Échec sync_fuel_stock planifiée")
