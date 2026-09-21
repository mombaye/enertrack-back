# fuel_tracking/tasks.py
"""
Tâches Celery planifiées (voir CELERY_BEAT_SCHEDULE dans settings.py) pour
la synchronisation automatique du module Suivi Carburant — exécutées
périodiquement pour rattraper rapidement les nouvelles données Snowflake/
ENOC sans attendre une intervention manuelle. Chaque commande gère déjà sa
propre traçabilité (FuelConsommationSyncRun / FuelEnocSyncRun /
FuelCphSyncRun / FuelStockSyncRun) et n'écrase que les mois concernés
(upsert par site) ou l'état courant (Stock, pas de notion de mois).

sync_fuel_cph et sync_fuel_stock ont longtemps été absentes d'ici (2026-08)
— jamais planifiées, seulement lancées manuellement pendant les tests —
d'où les colonnes Type de GE/Running Time/Énergie site vides et l'onglet
Stock jamais alimenté en prod malgré Consommation/ENOC qui tournaient bien.

Chaque tâche mensuelle couvre M ET M-1 (from_month=M-1, to_month=M) :
les données Snowflake/ENOC du mois précédent peuvent encore arriver pendant
les premiers jours du mois suivant (finalisations tardives, retards réseau),
et les tâches ne tournaient qu'en "mois courant" — d'où les 0 visibles dès
le 1er du mois suivant sur le mois qui venait de se terminer.
"""
import logging
from datetime import date

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


def _prev_month(month_str: str) -> str:
    """Retourne le mois précédent au format YYYY-MM."""
    year, month = int(month_str[:4]), int(month_str[5:7])
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


@shared_task(bind=True, name="fuel_tracking.sync_fuel_consommation_current_month")
def sync_fuel_consommation_current_month(self):
    from django.core.management import call_command

    current = timezone.now().strftime("%Y-%m")
    prev = _prev_month(current)
    try:
        call_command("sync_fuel_consommation", from_month=prev, to_month=current)
    except Exception:
        logger.exception(
            "[fuel_tracking] Échec sync_fuel_consommation planifiée (%s → %s)", prev, current
        )


@shared_task(bind=True, name="fuel_tracking.sync_enoc_fuel_movements_current_month")
def sync_enoc_fuel_movements_current_month(self):
    from django.core.management import call_command

    current = timezone.now().strftime("%Y-%m")
    prev = _prev_month(current)
    try:
        # sync_enoc_fuel_movements accepte --month (un seul mois) — on enchaîne
        # les deux appels pour couvrir M-1 puis M.
        call_command("sync_enoc_fuel_movements", month=prev)
        call_command("sync_enoc_fuel_movements", month=current)
    except Exception:
        # sync_enoc_fuel_movements enregistre déjà l'échec dans FuelEnocSyncRun
        # avant de relever l'exception — on l'attrape ici juste pour ne pas
        # faire échouer bruyamment la tâche planifiée toutes les 5 min.
        logger.exception(
            "[fuel_tracking] Échec sync_enoc_fuel_movements planifiée (%s → %s)", prev, current
        )


@shared_task(bind=True, name="fuel_tracking.sync_fuel_cph_current_month")
def sync_fuel_cph_current_month(self):
    from django.core.management import call_command

    current = timezone.now().strftime("%Y-%m")
    prev = _prev_month(current)
    try:
        call_command("sync_fuel_cph", from_month=prev, to_month=current)
    except Exception:
        logger.exception(
            "[fuel_tracking] Échec sync_fuel_cph planifiée (%s → %s)", prev, current
        )


@shared_task(bind=True, name="fuel_tracking.sync_fuel_stock_current")
def sync_fuel_stock_current(self):
    from django.core.management import call_command

    try:
        call_command("sync_fuel_stock")
    except Exception:
        logger.exception("[fuel_tracking] Échec sync_fuel_stock planifiée")
