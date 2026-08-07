# fuel_tracking/tasks.py
"""
Tâches Celery planifiées (voir CELERY_BEAT_SCHEDULE dans settings.py) pour
la synchronisation automatique de la Consommation carburant sur le mois en
cours — exécutées toutes les 5 min pour rattraper rapidement les nouvelles
données Snowflake/ENOC sans attendre une intervention manuelle. Chaque
commande gère déjà sa propre traçabilité (FuelConsommationSyncRun /
FuelEnocSyncRun) et n'écrase que le mois courant (upsert par site).
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
