# fuel_tracking/management/commands/run_fuel_rapprochement.py
"""
Calcule (ou recalcule) le rapprochement stock mensuel pour tous les sites
(ou une liste restreinte) et met à jour FuelConsommationMonthly en place.

Usage:
    python manage.py run_fuel_rapprochement --month 2026-08
    python manage.py run_fuel_rapprochement --from-month 2026-06 --to-month 2026-08
    python manage.py run_fuel_rapprochement --month 2026-08 --sites=DKR001,THS001
"""
from django.core.management.base import BaseCommand


def _parse_month(value: str) -> tuple[int, int]:
    year, month = str(value).split("-")
    return int(year), int(month)


def _months_between(from_month: str, to_month: str) -> list[tuple[int, int]]:
    y1, m1 = _parse_month(from_month)
    y2, m2 = _parse_month(to_month)
    start = y1 * 12 + (m1 - 1)
    end = y2 * 12 + (m2 - 1)
    return [(idx // 12, (idx % 12) + 1) for idx in range(start, end + 1)]


class Command(BaseCommand):
    help = "Calcule le rapprochement stock mensuel (FuelConsommationMonthly)"

    def add_arguments(self, parser):
        parser.add_argument("--month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--from-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--to-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument(
            "--sites", type=str, default=None,
            help="Liste de site_id séparés par des virgules (restreint le calcul).",
        )

    def handle(self, *args, **options):
        from fuel_tracking.services.fuel_rapprochement_service import run_rapprochement_for_month

        month = options.get("month")
        from_month = options.get("from_month")
        to_month = options.get("to_month")
        site_ids = [s.strip() for s in options["sites"].split(",")] if options.get("sites") else None

        if month:
            from_month = to_month = month

        if not from_month or not to_month:
            self.stdout.write(self.style.ERROR("Préciser --month YYYY-MM, ou --from-month/--to-month YYYY-MM."))
            return

        months = _months_between(from_month, to_month)
        total = 0
        for year, mo in months:
            month_year = f"{year:04d}-{mo:02d}"
            updated = run_rapprochement_for_month(year, mo, site_ids=site_ids)
            self.stdout.write(self.style.SUCCESS(f"  {month_year} : {updated} ligne(s) rapprochées."))
            total += updated

        self.stdout.write(self.style.SUCCESS(f"\nTerminé — {total} ligne(s) au total."))
