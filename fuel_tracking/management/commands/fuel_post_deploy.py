"""
Tâches automatiques après chaque déploiement (lancées par l'entrypoint, idempotentes, sans effet si rien n'est à faire) :

  1. Pilotage Grid : reconstruit les tables relationnelles si les jeux de données existent mais que les tables sont vides.
  2. Suivi Carburant : recalcule les bilans de stock des périodes récentes qui datent de l'ancienne règle (sans « variation de stock »),
     pour que les variations négatives et les nouveaux champs soient disponibles sans commande manuelle.

    python manage.py fuel_post_deploy [--periodes 3] [--dry-run]
Aucune certification n'est jamais créée ni modifiée ici ; ENOC et Snowflake ne sont ni lus en écriture ni modifiés.
"""
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Tâches post-déploiement idempotentes (tables Pilotage Grid, recalcul des bilans obsolètes)."

    def add_arguments(self, parser):
        parser.add_argument("--periodes", type=int, default=3, help="Nombre de périodes récentes à examiner.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, periodes, dry_run, **opts):
        w = self.stdout.write
        self._pilotage(w, dry_run)
        self._bilans(w, periodes, dry_run)

    def _pilotage(self, w, dry_run):
        try:
            from pathlib import Path

            from django.conf import settings
            from django.core.management import call_command

            from pilotage_grid.models import PgInvoiceFact, PgSite, PilotageDataset
            from pilotage_grid.services import tables

            initial = Path(settings.BASE_DIR) / "data_imports" / "pilotage_grid" / "pilotage_grid_initial.html"
            if initial.is_file() and not dry_run:   # jeux initiaux versionnés dans le dépôt : chargés seulement s'ils manquent en base
                call_command("pilotage_seed_from_html", only_missing=True, stdout=self.stdout)

            has_data = PilotageDataset.objects.filter(is_active=True, key__in=("paiements", "sites")).exists()
            empty = not PgInvoiceFact.objects.exists() or not PgSite.objects.exists()
            if has_data and empty:
                w("Pilotage Grid : tables vides alors que les jeux de données existent — reconstruction." if not dry_run else "Pilotage Grid : reconstruction nécessaire (dry-run).")
                if not dry_run:
                    w(f"  {tables.rebuild_all()}")
            else:
                w("Pilotage Grid : tables à jour." if has_data else "Pilotage Grid : aucun jeu de données en base (import initial à faire).")
        except Exception as e:  # ne jamais empêcher le démarrage du service
            w(f"Pilotage Grid : vérification impossible ({e}).")

    def _bilans(self, w, periodes, dry_run):
        try:
            from django.db.models import Q

            from fuel_tracking.models import FuelStockBalance as B
            from fuel_tracking.services import stock_period_balance as SB

            periods = list(B.objects.values_list("observation_start", "observation_end").distinct().order_by("-observation_end")[:periodes])
            for start, end in periods:
                stale = B.objects.filter(observation_start=start, observation_end=end, variation_stock_l__isnull=True).filter(
                    Q(consommation_reelle_l__isnull=False) | Q(cause_principale="CONSO_NEGATIVE")).count()
                if not stale:
                    w(f"Bilans {start:%d/%m/%Y} → {end:%d/%m/%Y} : à jour.")
                    continue
                w(f"Bilans {start:%d/%m/%Y} → {end:%d/%m/%Y} : {stale} bilan(s) sans variation de stock — recalcul.")
                if not dry_run:
                    SB.compute_balances(start, end)
        except Exception as e:
            w(f"Bilans : recalcul impossible ({e}).")
