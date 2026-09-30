# fuel_tracking/management/commands/sync_fuel_daily_facts.py
"""
Synchronise les faits bruts Suivi Carburant / CPH (Snowflake, lecture seule)
au grain (country, data_id, date) : inventaire SITE_ESCO_CURRENT puis faits
journaliers des sites avec GE (DG_COUNT > 0).

Usage :
    python manage.py sync_fuel_daily_facts --start 2026-10-01 --end 2026-10-12
    python manage.py sync_fuel_daily_facts --days 3            # J-3 → aujourd'hui
    python manage.py sync_fuel_daily_facts --start 2026-09-01 --end 2026-09-30 --sites DKR_0001,THS_0002 --dry-run
"""
from datetime import date, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from fuel_tracking.models import FuelDailyFactsSyncRun, FuelSiteDailyFacts, FuelSiteInventory
from fuel_tracking.services import fuel_daily_facts_snowflake as SF


class Command(BaseCommand):
    help = "Synchronise les faits journaliers Snowflake nécessaires au calcul CPH (lecture seule)."

    def add_arguments(self, parser):
        parser.add_argument("--start", type=date.fromisoformat)
        parser.add_argument("--end", type=date.fromisoformat)
        parser.add_argument("--days", type=int, help="Fenêtre glissante : J-N → aujourd'hui.")
        parser.add_argument("--country", default=None, help="Pays Snowflake (défaut : FUEL_CPH_COUNTRIES).")
        parser.add_argument("--sites", default=None, help="site_id séparés par des virgules.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        today = timezone.localdate()
        if opts["days"] is not None:
            d_start, d_end = today - timedelta(days=opts["days"]), today
        else:
            d_start, d_end = opts["start"], opts["end"]
        if not d_start or not d_end or d_end < d_start:
            raise CommandError("Préciser --start/--end (YYYY-MM-DD, fin ≥ début) ou --days N.")
        countries = [opts["country"]] if opts["country"] else list(getattr(settings, "FUEL_CPH_COUNTRIES", ["Senegal"]))
        site_filter = {s.strip() for s in opts["sites"].split(",")} if opts["sites"] else None
        dry = opts["dry_run"]

        run = None if dry else FuelDailyFactsSyncRun.objects.create(date_from=d_start, date_to=d_end)
        written = 0
        try:
            for country in countries:
                inventory = SF.fetch_inventory(country)
                self.stdout.write(f"{country} : {len(inventory)} DATA_ID dans SITE_ESCO_CURRENT")
                if not dry:
                    FuelSiteInventory.objects.bulk_create(
                        [FuelSiteInventory(**row, synced_at=timezone.now()) for row in inventory],
                        batch_size=1000, update_conflicts=True, unique_fields=["country", "data_id"],
                        update_fields=["site_id", "site_name", "grid_supply", "dg_count", "synced_at"],
                    )
                ge_rows = [r for r in inventory if (r["dg_count"] or 0) > 0
                           and (site_filter is None or r["site_id"] in site_filter)]
                site_by_data_id = {r["data_id"]: r["site_id"] for r in ge_rows}
                self.stdout.write(f"  {len(ge_rows)} DATA_ID avec GE retenus")

                for w_start, w_end in SF.date_windows(d_start, d_end):
                    facts = SF.fetch_daily_facts(list(site_by_data_id), w_start, w_end)
                    self.stdout.write(f"  {w_start} → {w_end} : {len(facts)} ligne(s) site/jour")
                    if dry:
                        continue
                    objs = [FuelSiteDailyFacts(country=country, site_id=site_by_data_id[f["data_id"]],
                                               synced_at=timezone.now(), **f) for f in facts]
                    with transaction.atomic():
                        FuelSiteDailyFacts.objects.filter(
                            country=country, data_id__in=list(site_by_data_id), date__gte=w_start, date__lte=w_end,
                        ).delete()
                        FuelSiteDailyFacts.objects.bulk_create(objs, batch_size=2000)
                    written += len(objs)
        except Exception as e:
            if run:
                run.status = FuelDailyFactsSyncRun.Status.FAILED
                run.error_message = str(e)
                run.rows_written = written
                run.finished_at = timezone.now()
                run.save()
            raise
        if run:
            run.status = FuelDailyFactsSyncRun.Status.SUCCESS
            run.rows_written = written
            run.finished_at = timezone.now()
            run.save()
        self.stdout.write(self.style.SUCCESS(f"Terminé — {written} ligne(s) écrite(s)." if not dry else "DRY RUN — rien écrit."))
