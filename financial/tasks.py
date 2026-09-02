# financial/tasks.py
"""
Tâche Celery planifiée (voir CELERY_BEAT_SCHEDULE dans settings.py) pour la
synchronisation automatique de la conso financière (Snowflake Grid/ACM +
Solaire) — jamais planifiée jusqu'ici (2026-09), seulement lancée
manuellement pendant les tests locaux, d'où le badge Snowflake "connecté"
en local mais pas en prod : FinancialConsoSyncRun n'y avait jamais tourné
avec succès, même principe que le trou découvert sur sync_fuel_cph/
sync_fuel_stock (voir fuel_tracking/tasks.py).
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="financial.sync_financial_conso_auto")
def sync_financial_conso_auto(self):
    from django.core.management import call_command

    try:
        call_command("sync_financial_conso_auto")
    except Exception:
        logger.exception("[financial] Échec sync_financial_conso_auto planifiée")
