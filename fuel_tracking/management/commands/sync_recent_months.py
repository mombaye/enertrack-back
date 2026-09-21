# fuel_tracking/management/commands/sync_recent_months.py
"""
Synchronise les N derniers mois complets (défaut : 2 = M-1 et M courant)
en enchaînant ENOC → Consommation Snowflake → CPH → Réévaluation financière.

Appelé au démarrage du conteneur (docker-compose command / Dockerfile CMD)
pour garantir que la base est à jour sans intervention manuelle — les mois
couverts sont calculés dynamiquement, pas hardcodés.

Usage:
    python manage.py sync_recent_months              # M-1 et M courant
    python manage.py sync_recent_months --months 3   # M-2, M-1 et M courant
    python manage.py sync_recent_months --months 1   # M courant seulement
"""
from datetime import date
from django.core.management.base import BaseCommand


def _months_back(n: int) -> list[str]:
    """Retourne une liste de n mois [M-(n-1), ..., M courant] au format YYYY-MM."""
    today = date.today()
    months = []
    for i in range(n - 1, -1, -1):
        month = today.month - i
        year = today.year
        while month <= 0:
            month += 12
            year -= 1
        months.append(f"{year}-{month:02d}")
    return months


class Command(BaseCommand):
    help = "Synchronise les N derniers mois (ENOC + Snowflake conso + CPH) — mois calculés dynamiquement"

    def add_arguments(self, parser):
        parser.add_argument(
            "--months",
            type=int,
            default=2,
            help="Nombre de mois à couvrir en remontant depuis le mois courant (défaut : 2)",
        )
        parser.add_argument(
            "--skip-financial",
            action="store_true",
            help="Ne pas relancer recompute_financial_evaluations à la fin",
        )

    def handle(self, *args, **options):
        from django.core.management import call_command

        n = options["months"]
        months = _months_back(n)
        from_month = months[0]
        to_month = months[-1]

        self.stdout.write(f"\n[sync_recent_months] Couverture : {from_month} → {to_month} ({n} mois)")

        # 1. ENOC (mois par mois — sync_enoc_fuel_movements n'accepte qu'un seul mois)
        self.stdout.write("  ── ENOC fuel movements ──")
        for m in months:
            try:
                call_command("sync_enoc_fuel_movements", month=m)
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  ENOC {m} : {e}"))

        # 2. Consommation Snowflake (plage from → to en une seule commande)
        self.stdout.write("  ── Consommation Snowflake ──")
        try:
            call_command("sync_fuel_consommation", from_month=from_month, to_month=to_month)
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  Conso Snowflake : {e}"))

        # 3. CPH Running Time (plage from → to)
        self.stdout.write("  ── CPH Running Time ──")
        try:
            call_command("sync_fuel_cph", from_month=from_month, to_month=to_month)
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  CPH : {e}"))

        # 4. Réévaluation financière sur la même fenêtre
        if not options["skip_financial"]:
            self.stdout.write("  ── Réévaluation financière ──")
            try:
                call_command("recompute_financial_evaluations", months=n)
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  Financial : {e}"))

        self.stdout.write(self.style.SUCCESS(f"\n[sync_recent_months] Terminé ({from_month} → {to_month})\n"))
