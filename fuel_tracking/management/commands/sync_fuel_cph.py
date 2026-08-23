# fuel_tracking/management/commands/sync_fuel_cph.py
"""
Synchronise l'estimation CPH (télémétrie GFMS_DATA_TRACKER_NC) — écrit le
détail journalier dans FuelCphGeDaily puis met à jour l'agrégat mensuel
(conso_estimee_cph_l et champs associés) sur les lignes FuelConsommationMonthly
DÉJÀ existantes. Cette commande ne crée JAMAIS de ligne FuelConsommationMonthly
— la présence/absence d'un site pour un mois donné vient de
sync_fuel_consommation, qui doit avoir tourné avant (dans le même
scheduler/cron, juste avant cette commande).

Usage:
    python manage.py sync_fuel_cph --month 2026-07
    python manage.py sync_fuel_cph --from-month 2026-01 --to-month 2026-07
    python manage.py sync_fuel_cph --month 2026-07 --sites=MBR_2148,STL_0633 --dry-run
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from fuel_tracking.models import FuelCphGeDaily, FuelCphSyncRun, FuelConsommationMonthly
from fuel_tracking.services.fuel_cph_service import compute_monthly_cph_estimates

DAILY_UPDATE_FIELDS = [
    "country", "data_id",
    "ge_intervals", "valid_battery_intervals",
    "dg_runtime_interval_h", "dg_runtime_interval_status", "dg_runtime_controller_h",
    "dg_runtime_business_h", "dg_runtime_business_source",
    "site_load_energy_kwh", "battery_dc_energy_kwh", "battery_charge_ac_energy_kwh",
    "total_ge_energy_kwh", "average_ge_power_kw",
    "pge_kva", "power_factor", "spc_l_per_kwh",
    "cph_estimated_lph", "estimated_consumption_l", "ge_load_percent",
    "calculation_status", "synced_at",
]

MONTHLY_UPDATE_FIELDS = [
    "conso_estimee_cph_l", "cph_l_per_h_moy", "cph_nb_jours_ok",
    "cph_nb_jours_calcules", "cph_calculation_status", "cph_status_breakdown",
    "cph_runtime_h_total", "cph_runtime_source", "cph_ge_type",
    "cph_pge_kva", "cph_power_factor", "cph_spc_l_per_kwh",
    "cph_site_load_energy_kwh", "cph_battery_dc_energy_kwh",
    "cph_battery_ac_energy_kwh", "cph_total_ge_energy_kwh",
]


def parse_month(value: str) -> tuple[int, int]:
    year, month = str(value).split("-")
    return int(year), int(month)


def months_between(from_month: str, to_month: str) -> list[tuple[int, int]]:
    y1, m1 = parse_month(from_month)
    y2, m2 = parse_month(to_month)
    start = y1 * 12 + (m1 - 1)
    end = y2 * 12 + (m2 - 1)
    return [(idx // 12, (idx % 12) + 1) for idx in range(start, end + 1)]


class Command(BaseCommand):
    help = "Synchronise l'estimation CPH (télémétrie) — détail journalier + agrégat mensuel."

    def add_arguments(self, parser):
        parser.add_argument("--month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--from-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--to-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--sites", type=str, default=None, help="Liste de site_id séparés par des virgules (restreint le scan Snowflake — utile pour tester/déployer progressivement).")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        month = options.get("month")
        from_month = options.get("from_month")
        to_month = options.get("to_month")
        site_ids = [s.strip() for s in options["sites"].split(",")] if options.get("sites") else None
        dry_run = options["dry_run"]

        if month:
            from_month = to_month = month

        if not from_month or not to_month:
            self.stdout.write(self.style.ERROR("Préciser --month YYYY-MM, ou --from-month/--to-month YYYY-MM."))
            return

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  SYNC CPH (télémétrie GFMS_DATA_TRACKER_NC) → EnerTrack")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  From month : {from_month}")
        self.stdout.write(f"  To month   : {to_month}")
        self.stdout.write(f"  Sites      : {', '.join(site_ids) if site_ids else 'tout le périmètre Sénégal'}")
        self.stdout.write(f"  Dry run    : {dry_run}")
        self.stdout.write("═" * 80 + "\n")

        sync_run = None
        if not dry_run:
            sync_run = FuelCphSyncRun.objects.create(
                month_from=from_month, month_to=to_month,
                status=FuelCphSyncRun.Status.RUNNING,
            )

        total_sites_fetched = 0
        last_error = None

        for year, mo in months_between(from_month, to_month):
            month_year = f"{year:04d}-{mo:02d}"
            self.stdout.write(f"\n── {month_year} ──")

            self.stdout.write("  Requête Snowflake (GFMS_DATA_TRACKER_NC / GENSET_REPORT) + paramètres GE...")
            try:
                result = compute_monthly_cph_estimates(year, mo, site_ids=site_ids)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Erreur pour {month_year} : {e}"))
                last_error = str(e)
                continue

            daily_rows = result["daily"]
            monthly = result["monthly"]
            self.stdout.write(f"  {len(monthly)} site(s), {len(daily_rows)} journée(s)-site.")
            total_sites_fetched += len(monthly)

            status_totals: dict[str, int] = {}
            for row in daily_rows:
                status_totals[row["calculation_status"]] = status_totals.get(row["calculation_status"], 0) + 1
            for status, count in sorted(status_totals.items(), key=lambda kv: -kv[1]):
                self.stdout.write(f"    {status:<40} {count}")

            if dry_run:
                for site_id, agg in list(monthly.items())[:10]:
                    self.stdout.write(
                        f"    {site_id} | conso_cph={agg['conso_estimee_cph_l']} L | "
                        f"jours OK={agg['cph_nb_jours_ok']}/{agg['cph_nb_jours_calcules']} | "
                        f"statut={agg['cph_calculation_status']}"
                    )
                self.stdout.write(self.style.WARNING(f"  DRY RUN — rien écrit ({len(daily_rows)} ligne(s) journalières, {len(monthly)} agrégat(s) mensuel(s))."))
                continue

            daily_objects = [
                FuelCphGeDaily(
                    country=row.get("country"),
                    site_id=row["site_id"],
                    date=row["date"],
                    data_id=row.get("data_id"),
                    ge_intervals=row["ge_intervals"],
                    valid_battery_intervals=row["valid_battery_intervals"],
                    dg_runtime_interval_h=row["dg_runtime_interval_h"],
                    dg_runtime_interval_status=row["dg_runtime_interval_status"],
                    dg_runtime_controller_h=row["dg_runtime_controller_h"],
                    dg_runtime_business_h=row.get("dg_runtime_business_h"),
                    dg_runtime_business_source=row.get("dg_runtime_business_source"),
                    site_load_energy_kwh=row["site_load_energy_kwh"],
                    battery_dc_energy_kwh=row["battery_dc_energy_kwh"],
                    battery_charge_ac_energy_kwh=row["battery_charge_ac_energy_kwh"],
                    total_ge_energy_kwh=row["total_ge_energy_kwh"],
                    average_ge_power_kw=row["average_ge_power_kw"],
                    pge_kva=row["pge_kva"],
                    power_factor=row["power_factor"],
                    spc_l_per_kwh=row["spc_l_per_kwh"],
                    cph_estimated_lph=row["cph_estimated_lph"],
                    estimated_consumption_l=row["estimated_consumption_l"],
                    ge_load_percent=row["ge_load_percent"],
                    calculation_status=row["calculation_status"],
                    synced_at=timezone.now(),
                )
                for row in daily_rows
            ]

            with transaction.atomic():
                if daily_objects:
                    FuelCphGeDaily.objects.bulk_create(
                        daily_objects,
                        batch_size=1000,
                        update_conflicts=True,
                        unique_fields=["site_id", "date"],
                        update_fields=DAILY_UPDATE_FIELDS,
                    )

                existing_by_site = {
                    fc.site_id: fc
                    for fc in FuelConsommationMonthly.objects.filter(month_year=month_year, site_id__in=monthly.keys())
                }
                to_update = []
                missing_sites = []
                for site_id, agg in monthly.items():
                    fc = existing_by_site.get(site_id)
                    if fc is None:
                        missing_sites.append(site_id)
                        continue
                    fc.conso_estimee_cph_l = agg["conso_estimee_cph_l"]
                    fc.cph_l_per_h_moy = agg["cph_l_per_h_moy"]
                    fc.cph_nb_jours_ok = agg["cph_nb_jours_ok"]
                    fc.cph_nb_jours_calcules = agg["cph_nb_jours_calcules"]
                    fc.cph_calculation_status = agg["cph_calculation_status"]
                    fc.cph_status_breakdown = agg["cph_status_breakdown"]
                    fc.cph_runtime_h_total = agg["cph_runtime_h_total"]
                    fc.cph_runtime_source = agg["cph_runtime_source"]
                    fc.cph_ge_type = agg["cph_ge_type"]
                    fc.cph_pge_kva = agg["cph_pge_kva"]
                    fc.cph_power_factor = agg["cph_power_factor"]
                    fc.cph_spc_l_per_kwh = agg["cph_spc_l_per_kwh"]
                    fc.cph_site_load_energy_kwh = agg["cph_site_load_energy_kwh"]
                    fc.cph_battery_dc_energy_kwh = agg["cph_battery_dc_energy_kwh"]
                    fc.cph_battery_ac_energy_kwh = agg["cph_battery_ac_energy_kwh"]
                    fc.cph_total_ge_energy_kwh = agg["cph_total_ge_energy_kwh"]
                    to_update.append(fc)

                if to_update:
                    FuelConsommationMonthly.objects.bulk_update(to_update, MONTHLY_UPDATE_FIELDS, batch_size=500)

            self.stdout.write(self.style.SUCCESS(f"  {len(daily_objects)} ligne(s) journalière(s) écrite(s), {len(to_update)} agrégat(s) mensuel(s) mis à jour."))
            if missing_sites:
                preview = ", ".join(missing_sites[:10]) + ("…" if len(missing_sites) > 10 else "")
                self.stdout.write(self.style.WARNING(
                    f"  {len(missing_sites)} site(s) avec CPH mais sans ligne FuelConsommationMonthly pour "
                    f"{month_year} (sync_fuel_consommation doit tourner avant) — ignorés : {preview}"
                ))

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(f"\n  Terminé — {total_sites_fetched} site(s) traité(s) au total.\n"))
            sync_run.sites_fetched = total_sites_fetched
            sync_run.finished_at = timezone.now()
            if total_sites_fetched > 0:
                sync_run.status = FuelCphSyncRun.Status.SUCCESS
            else:
                sync_run.status = FuelCphSyncRun.Status.FAILED
                sync_run.error_message = last_error or "Aucune donnée CPH récupérée pour la période demandée."
            sync_run.save()
