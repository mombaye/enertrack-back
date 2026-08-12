# fuel_tracking/management/commands/sync_fuel_stock.py
"""
Synchronise le Stock carburant ACTUEL par site (FuelStockSnapshot) — jointure
Snowflake (VW_FUEL_REPORT, dernier relevé physiquement valide par site sur
30 jours) + ENOC (fuel_level_readings, dernier relevé). Voir
fuel_tracking/models.py::FuelStockSnapshot pour le détail des sources.

Contrairement à sync_fuel_consommation (mensuel, --month/--from-month/--to-month),
le stock n'a pas de notion de mois : chaque exécution remplace entièrement
l'état courant (upsert par site_id).

Usage:
    python manage.py sync_fuel_stock
    python manage.py sync_fuel_stock --dry-run
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from fuel_tracking.models import FuelStockSnapshot, FuelStockSyncRun
from fuel_tracking.services.enoc_mongo_service import fetch_genset_reference, fetch_stock_readings
from fuel_tracking.services.fuel_stock_snowflake import fetch_stock_snapshot


class Command(BaseCommand):
    help = "Synchronise le Stock carburant actuel (Snowflake + ENOC)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  SYNC STOCK CARBURANT (Snowflake + ENOC) → EnerTrack")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  Dry run : {dry_run}\n")

        sync_run = None
        if not dry_run:
            sync_run = FuelStockSyncRun.objects.create(status=FuelStockSyncRun.Status.RUNNING)

        try:
            self.stdout.write("  Référentiel GE ENOC...")
            try:
                genset_ref_enoc = fetch_genset_reference()
                self.stdout.write(f"  {len(genset_ref_enoc)} site(s), "
                                   f"{sum(1 for v in genset_ref_enoc.values() if v['has_genset'])} avec GE.")
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  Référentiel GE ENOC indisponible ({e}) — Snowflake seul utilisé."))
                genset_ref_enoc = {}

            self.stdout.write("  Requête Snowflake (VW_FUEL_REPORT, dernier relevé/site sur 30j)...")
            snowflake_data = fetch_stock_snapshot()
            nb_avec_stock = sum(1 for v in snowflake_data.values() if v["stock_snowflake_l"] is not None)
            self.stdout.write(f"  {len(snowflake_data)} site(s) Snowflake, {nb_avec_stock} avec un relevé de stock.")

            self.stdout.write("  Relevés de niveau ENOC (dernier par site)...")
            try:
                enoc_stock = fetch_stock_readings()
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  Stock ENOC indisponible : {e}"))
                enoc_stock = {}
            self.stdout.write(f"  {len(enoc_stock)} site(s) avec un relevé de stock ENOC.")

            enoc_only_ge_sites = {
                sid for sid, v in genset_ref_enoc.items()
                if v["has_genset"] and sid not in snowflake_data
            }
            all_site_ids = set(snowflake_data) | enoc_only_ge_sites
            self.stdout.write(f"\n  Périmètre total : {len(all_site_ids)} site(s).")

            objects = []
            for site_id in all_site_ids:
                sf = snowflake_data.get(site_id, {})
                ge = genset_ref_enoc.get(site_id, {})
                en_stock = enoc_stock.get(site_id, {})

                has_genset_sf = bool(sf.get("has_genset"))
                has_genset_enoc = bool(ge.get("has_genset"))

                capacity = sf.get("capacity_snowflake_l")
                level = sf.get("stock_snowflake_l")
                pct = None
                if level is not None and capacity:
                    try:
                        pct = round(float(level) / float(capacity) * 100, 1)
                    except (TypeError, ZeroDivisionError):
                        pct = None

                objects.append(FuelStockSnapshot(
                    site_id=site_id,
                    site_name=sf.get("site_name"),
                    country=sf.get("country"),
                    typology=sf.get("typology"),
                    site_type=sf.get("site_type"),
                    dg_count=sf.get("dg_count"),
                    power_supply=sf.get("power_supply"),
                    has_genset_snowflake=has_genset_sf,
                    has_genset_enoc=has_genset_enoc,
                    nb_ge_enoc=ge.get("nb_ge"),
                    has_genset=has_genset_sf or has_genset_enoc,
                    stock_snowflake_l=level,
                    capacity_snowflake_l=capacity,
                    stock_snowflake_pct=pct,
                    stock_snowflake_date=sf.get("stock_snowflake_date"),
                    quality_status=sf.get("quality_status"),
                    stock_enoc_l=en_stock.get("stock_enoc_l"),
                    stock_enoc_date=(en_stock.get("date") or "")[:10] or None,
                    synced_at=timezone.now(),
                ))

            if dry_run:
                for obj in objects[:10]:
                    self.stdout.write(
                        f"    {obj.site_id} | stock_sf={obj.stock_snowflake_l} L / {obj.capacity_snowflake_l} L "
                        f"({obj.stock_snowflake_pct}%) | stock_enoc={obj.stock_enoc_l} L"
                    )
                self.stdout.write(self.style.WARNING(f"\n  DRY RUN — {len(objects)} ligne(s), rien écrit.\n"))
                return

            existing_keys = set(FuelStockSnapshot.objects.values_list("site_id", flat=True))
            created = sum(1 for o in objects if o.site_id not in existing_keys)
            updated = len(objects) - created

            with transaction.atomic():
                FuelStockSnapshot.objects.bulk_create(
                    objects,
                    batch_size=1000,
                    update_conflicts=True,
                    unique_fields=["site_id"],
                    update_fields=[
                        "site_name", "country", "typology", "site_type", "dg_count", "power_supply",
                        "has_genset_snowflake", "has_genset_enoc", "nb_ge_enoc", "has_genset",
                        "stock_snowflake_l", "capacity_snowflake_l", "stock_snowflake_pct",
                        "stock_snowflake_date", "quality_status",
                        "stock_enoc_l", "stock_enoc_date", "synced_at",
                    ],
                )

            # Sites disparus du périmètre (plus GE ni côté Snowflake ni ENOC) —
            # supprimés pour ne pas laisser un stock obsolète affiché.
            stale = FuelStockSnapshot.objects.exclude(site_id__in=all_site_ids)
            nb_stale = stale.count()
            if nb_stale:
                stale.delete()

            self.stdout.write(self.style.SUCCESS(f"\n  Terminé — {created} créée(s), {updated} mise(s) à jour"
                                                   f"{f', {nb_stale} supprimée(s) (hors périmètre)' if nb_stale else ''}.\n"))

            sync_run.sites_fetched = len(all_site_ids)
            sync_run.status = FuelStockSyncRun.Status.SUCCESS if all_site_ids else FuelStockSyncRun.Status.FAILED
            if not all_site_ids:
                sync_run.error_message = "Aucune donnée Snowflake/ENOC récupérée."
            sync_run.finished_at = timezone.now()
            sync_run.save()

        except Exception as exc:
            if sync_run:
                sync_run.status = FuelStockSyncRun.Status.FAILED
                sync_run.error_message = str(exc)
                sync_run.finished_at = timezone.now()
                sync_run.save()
            self.stdout.write(self.style.ERROR(f"\n  Erreur sync stock carburant : {exc}\n"))
            raise
