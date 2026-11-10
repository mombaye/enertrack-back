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


from fuel_tracking.tasks import _exclusive  # noqa: E402 — même verrou anti-chevauchement que les synchros carburant


# Planifiée toutes les 30 min. Constaté en prod le 2026-11-09 : sans limite de temps ni verrou, 4 exécutions
# lancées à 30-40 min d'écart tournaient encore des heures après (elles se bloquaient mutuellement sur les mêmes
# tables) et occupaient les 4 processus du worker → plus aucune autre tâche, certification figée à 1 %.
# Désormais : une seule exécution à la fois (les suivantes sont ignorées) et arrêt forcé au bout de 45 min.
@shared_task(bind=True, name="financial.sync_financial_conso_auto", soft_time_limit=40 * 60, time_limit=45 * 60)
@_exclusive("financial_conso_sync", 50 * 60)
def sync_financial_conso_auto(self):
    from django.core.management import call_command

    try:
        call_command("sync_financial_conso_auto")
    except Exception:
        logger.exception("[financial] Échec sync_financial_conso_auto planifiée")
