# fuel_tracking/tasks.py
"""
Tâches Celery planifiées (voir CELERY_BEAT_SCHEDULE dans settings.py) pour
la synchronisation automatique du module Suivi Carburant — exécutées
périodiquement pour rattraper rapidement les nouvelles données Snowflake/
ENOC sans attendre une intervention manuelle. Chaque commande gère déjà sa
propre traçabilité (FuelConsommationSyncRun / FuelEnocSyncRun /
FuelDailyFactsSyncRun / FuelStockSyncRun) et n'écrase que les mois concernés
(upsert par site) ou l'état courant (Stock, pas de notion de mois).

Le calcul CPH lui-même n'est pas planifié : il est fait à la demande, sur
la plage exacte choisie, à partir des faits journaliers synchronisés ici
(voir services/cph_engine.py).

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


DAILY_FACTS_LOCK = "fuel_tracking:daily_facts_sync_lock"


def _run_daily_facts_sync(days: int, label: str):
    """
    Synchronisation des faits journaliers sous verrou : la resynchro nocturne (35 j) et la
    synchro horaire (J-3) suppriment/réécrivent les mêmes fenêtres, elles ne doivent pas
    se chevaucher. Cache indisponible → on exécute quand même (pas de blocage silencieux).
    """
    from django.core.cache import cache
    from django.core.management import call_command

    try:
        acquired = cache.add(DAILY_FACTS_LOCK, label, timeout=3 * 3600)
    except Exception:
        acquired = None
    if acquired is False:
        logger.warning("[fuel_tracking] sync_fuel_daily_facts %s ignorée : une synchro est déjà en cours", label)
        return
    try:
        call_command("sync_fuel_daily_facts", days=days)
    except Exception:
        logger.exception("[fuel_tracking] Échec sync_fuel_daily_facts planifiée (%s)", label)
    finally:
        if acquired:
            try:
                cache.delete(DAILY_FACTS_LOCK)
            except Exception:
                pass


@shared_task(bind=True, name="fuel_tracking.sync_fuel_daily_facts_recent")
def sync_fuel_daily_facts_recent(self):
    """Faits journaliers CPH (Snowflake, lecture seule) sur J-3 → aujourd'hui : les
    données GE/redresseur/AC_METER arrivent avec quelques jours de retard."""
    _run_daily_facts_sync(3, "J-3")


@shared_task(bind=True, name="fuel_tracking.resync_fuel_daily_facts_nightly")
def resync_fuel_daily_facts_nightly(self):
    """Resynchro nocturne J-35 → aujourd'hui : rattrape les données arrivées en retard
    (dont la conso mesurée VW_FUEL_REPORT) sans aucune action manuelle."""
    _run_daily_facts_sync(35, "J-35")


@shared_task(bind=True, name="fuel_tracking.check_cph_health_daily")
def check_cph_health_daily(self):
    """Contrôle quotidien : chaque anomalie est journalisée en ERROR (alerting sur les logs)."""
    from fuel_tracking.services.cph_health import cph_health

    try:
        h = cph_health()
    except Exception:
        logger.exception("[fuel_cph][SANTE] contrôle de santé impossible")
        return
    for i in h["issues"]:
        logger.error("[fuel_cph][SANTE][%s] %s : %s", i["niveau"], i["code"], i["message"])
    if h["ok"]:
        logger.info("[fuel_cph][SANTE] OK %s", h["metrics"])


@shared_task(bind=True, name="fuel_tracking.sync_fuel_stock_current")
def sync_fuel_stock_current(self):
    from django.core.management import call_command

    try:
        call_command("sync_fuel_stock")
    except Exception:
        logger.exception("[fuel_tracking] Échec sync_fuel_stock planifiée")


@shared_task(bind=True, name="fuel_tracking.auto_import_stan_periodic")
def auto_import_stan_periodic(self):
    """Détecte un nouveau fichier Stan dans data_imports/stan/ et l'importe.

    La commande est idempotente : même fichier + mois déjà couverts → skip immédiat.
    Nouveau fichier → import de tous les mois ; même fichier + mois manquants → backfill.
    """
    from django.core.management import call_command

    try:
        call_command("auto_import_stan")
    except Exception:
        logger.exception("[fuel_tracking] Échec auto_import_stan planifiée")
