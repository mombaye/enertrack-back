# fuel_tracking/management/commands/auto_import_stan.py
"""
Appelé automatiquement par entrypoint.sh au démarrage du container.

Stratégie :
  - Nouveau fichier Stan détecté (nom différent du sentinel) → importe pour
    TOUS les mois présents dans FuelConsommationMonthly.
  - Même fichier qu'au dernier démarrage → importe uniquement les mois qui
    n'ont pas encore de données Stan (facturation_avec_ge_fichier IS NULL
    pour tous leurs sites) → backfill automatique.
  - Tout couvert → skip immédiat, démarrage non ralenti.

Sentinel : data_imports/stan/.last_import  (contient juste le nom de fichier)
"""
import re
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

from fuel_tracking.models import FuelConsommationMonthly

STAN_DIR = Path(settings.BASE_DIR) / "data_imports" / "stan"
SENTINEL = STAN_DIR / ".last_import"


def _read_sentinel() -> str | None:
    if not SENTINEL.exists():
        return None
    try:
        return SENTINEL.read_text().strip() or None
    except OSError:
        return None


def _write_sentinel(filename: str) -> None:
    SENTINEL.write_text(filename)


def _months_without_stan() -> list[str]:
    """Mois qui n'ont aucune ligne avec facturation_avec_ge_fichier renseigné."""
    all_months = list(
        FuelConsommationMonthly.objects
        .values_list("month_year", flat=True)
        .distinct()
        .order_by("month_year")
    )
    return [
        m for m in all_months
        if not FuelConsommationMonthly.objects.filter(
            month_year=m,
            facturation_avec_ge_fichier__isnull=False,
        ).exists()
    ]


class Command(BaseCommand):
    help = (
        "Auto-import Stan au démarrage : nouveau fichier → tous les mois ; "
        "même fichier → backfill des mois manquants uniquement."
    )

    def handle(self, *args, **options):
        if not STAN_DIR.exists():
            self.stdout.write("  [Stan] Dossier data_imports/stan/ absent — import ignoré.")
            return

        candidates = sorted(
            STAN_DIR.glob("*.xlsx"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            self.stdout.write("  [Stan] Aucun fichier .xlsx dans data_imports/stan/ — import ignoré.")
            return

        latest = candidates[0]
        sentinel = _read_sentinel()
        new_file = sentinel != latest.name

        if new_file:
            # Nouveau fichier → importer tous les mois existants
            months = list(
                FuelConsommationMonthly.objects
                .values_list("month_year", flat=True)
                .distinct()
                .order_by("month_year")
            )
            if not months:
                self.stdout.write("  [Stan] Aucune donnée en base — import ignoré.")
                return
            self.stdout.write(
                f"  [Stan] Nouveau fichier : {latest.name} → import pour {len(months)} mois : {', '.join(months)}"
            )
        else:
            # Même fichier → backfill uniquement les mois sans Stan
            months = _months_without_stan()
            if not months:
                self.stdout.write(f"  [Stan] {latest.name} — tous les mois sont couverts, aucune action.")
                return
            self.stdout.write(
                f"  [Stan] {latest.name} — backfill de {len(months)} mois sans Stan : {', '.join(months)}"
            )

        for month in months:
            self.stdout.write(f"  [Stan] → {month} ...")
            call_command("import_facturation_par_site", file=str(latest), month=month)

        _write_sentinel(latest.name)
        self.stdout.write(
            self.style.SUCCESS(
                f"  [Stan] Import terminé ({len(months)} mois) — sentinel mis à jour."
            )
        )
