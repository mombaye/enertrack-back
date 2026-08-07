# fuel_tracking/management/commands/sync_fuel_consommation.py
"""
Synchronise la Consommation carburant mensuelle par site (FuelConsommationMonthly)
— jointure Snowflake (DB_GFMS_PROD.GOLD, conso mesurée par capteur) + ENOC
(FuelEnocMovement, quantités réellement ajoutées lors des ravitaillements
validés par le fuel manager). Voir fuel_tracking/models.py::FuelConsommationMonthly
pour le détail des sources.

ENOC doit avoir été synchronisé au préalable via sync_enoc_fuel_movements
(cette commande ne fait AUCUN appel réseau ENOC live — elle lit
FuelEnocMovement déjà en base). Snowflake, lui, est interrogé ici directement.

Usage:
    python manage.py sync_fuel_consommation --month 2026-07
    python manage.py sync_fuel_consommation --from-month 2026-01 --to-month 2026-07
    python manage.py sync_fuel_consommation --month 2026-07 --dry-run
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from fuel_tracking.models import FuelConsommationMonthly, FuelConsommationSyncRun, FuelEnocMovement
from fuel_tracking.services.enoc_mongo_service import fetch_estimated_consumption, fetch_genset_reference
from fuel_tracking.services.fuel_consommation_snowflake import fetch_monthly_consumption


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
    help = "Synchronise la Consommation carburant mensuelle (Snowflake + ENOC déjà synchronisé)"

    def add_arguments(self, parser):
        parser.add_argument("--month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--from-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--to-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        month = options.get("month")
        from_month = options.get("from_month")
        to_month = options.get("to_month")
        dry_run = options["dry_run"]

        if month:
            from_month = to_month = month

        if not from_month or not to_month:
            self.stdout.write(self.style.ERROR("Préciser --month YYYY-MM, ou --from-month/--to-month YYYY-MM."))
            return

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  SYNC CONSOMMATION CARBURANT (Snowflake + ENOC) → EnerTrack")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  From month : {from_month}")
        self.stdout.write(f"  To month   : {to_month}")
        self.stdout.write(f"  Dry run    : {dry_run}")
        self.stdout.write("═" * 80 + "\n")

        sync_run = None
        if not dry_run:
            sync_run = FuelConsommationSyncRun.objects.create(
                month_from=from_month, month_to=to_month,
                status=FuelConsommationSyncRun.Status.RUNNING,
            )

        # Référentiel GE ENOC (sites.nb_ge + ge_assets INSTALLED) — pas
        # dépendant du mois, une seule lecture pour toute la commande.
        # Si ENOC est injoignable, on continue avec Snowflake seul (le flag
        # ENOC reste False partout) plutôt que de bloquer toute la sync.
        try:
            genset_ref_enoc = fetch_genset_reference()
            self.stdout.write(f"  Référentiel GE ENOC : {len(genset_ref_enoc)} site(s), "
                               f"{sum(1 for v in genset_ref_enoc.values() if v['has_genset'])} avec GE.\n")
        except Exception as e:
            self.stdout.write(self.style.WARNING(f"  Référentiel GE ENOC indisponible ({e}) — Snowflake seul utilisé.\n"))
            genset_ref_enoc = {}

        total_created = 0
        total_updated = 0
        total_sites_fetched = 0
        last_error = None

        for year, mo in months_between(from_month, to_month):
            month_year = f"{year:04d}-{mo:02d}"
            self.stdout.write(f"\n── {month_year} ──")

            self.stdout.write("  Requête Snowflake (CONSUMPTION_FUEL / AVGSPECIFICFUELCONSO_L_KWH / capteur)...")
            try:
                snowflake_data = fetch_monthly_consumption(year, mo)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Erreur Snowflake pour {month_year} : {e}"))
                last_error = str(e)
                continue
            self.stdout.write(f"  {len(snowflake_data)} site(s) avec données Snowflake.")
            total_sites_fetched += len(snowflake_data)

            enoc_rows = (
                FuelEnocMovement.objects
                .filter(operation_date__year=year, operation_date__month=mo)
                .exclude(site_id__isnull=True)
                .values("site_id")
                .annotate(
                    qte_demandee=Sum("requested_quantity_liters"),
                    qte_validee=Sum("approved_quantity_liters"),
                    qte_ajoutee=Sum("quantity_added_liters"),
                    nb_demandes=Count("id"),
                )
            )
            enoc_by_site = {row["site_id"]: row for row in enoc_rows}
            self.stdout.write(f"  {len(enoc_by_site)} site(s) avec mouvements ENOC.")

            # Conso ESTIMÉE (pas mesurée) depuis les relevés de niveau ENOC —
            # voir les réserves dans enoc_mongo_service.fetch_estimated_consumption.
            try:
                conso_estimee_by_site = fetch_estimated_consumption(year, mo)
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  Estimation ENOC (relevés de niveau) indisponible : {e}"))
                conso_estimee_by_site = {}
            self.stdout.write(f"  {len(conso_estimee_by_site)} site(s) avec conso ESTIMÉE (relevés de niveau ENOC).")

            # Sites que ENOC signale avec GE mais que Snowflake ne connaît pas
            # (hors périmètre SITE_FILTERED, ou nouveau site pas encore
            # remonté côté Snowflake) — inclus pour une vue complète, mais
            # seulement ceux avec GE (sinon on réintroduit tout le bruit des
            # milliers de sites sans GE qu'on vient de filtrer).
            enoc_only_ge_sites = {
                sid for sid, v in genset_ref_enoc.items()
                if v["has_genset"] and sid not in snowflake_data
            }

            all_site_ids = set(snowflake_data) | set(enoc_by_site) | enoc_only_ge_sites
            if not all_site_ids:
                self.stdout.write(self.style.WARNING(f"  Aucune donnée (Snowflake + ENOC) pour {month_year}, mois sauté."))
                continue

            objects = []
            for site_id in all_site_ids:
                sf = snowflake_data.get(site_id, {})
                en = enoc_by_site.get(site_id, {})
                ge = genset_ref_enoc.get(site_id, {})
                est = conso_estimee_by_site.get(site_id, {})

                conso_l = sf.get("conso_snowflake_l")
                ajoutee_l = en.get("qte_ajoutee")
                ecart = (conso_l - ajoutee_l) if (conso_l is not None and ajoutee_l is not None) else None

                # Estimation Snowflake (delta niveau de cuve TANK_LEVEL_AVG +
                # ravitaillements ENOC entre les 2 relevés) — un résultat
                # négatif indique une incohérence, écarté plutôt qu'affiché.
                niveau_debut = sf.get("niveau_debut")
                niveau_fin = sf.get("niveau_fin")
                conso_estimee_sf = None
                if niveau_debut is not None and niveau_fin is not None:
                    conso_estimee_sf = (niveau_debut - niveau_fin) + (ajoutee_l or 0)
                    if conso_estimee_sf < 0:
                        conso_estimee_sf = None

                has_genset_sf = bool(sf.get("has_genset"))
                has_genset_enoc = bool(ge.get("has_genset"))

                objects.append(FuelConsommationMonthly(
                    month_year=month_year,
                    year=year,
                    month=mo,
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
                    conso_snowflake_l=conso_l,
                    nb_jours_data=sf.get("nb_jours_data") or 0,
                    conso_estimee_snowflake_l=conso_estimee_sf,
                    conso_estimee_snowflake_nb_releves=sf.get("niveau_nb_releves"),
                    conso_estimee_enoc_l=est.get("conso_estimee_l"),
                    conso_estimee_nb_releves=est.get("nb_releves"),
                    conso_specifique_moy_l_kwh=sf.get("conso_specifique_moy_l_kwh"),
                    sensor_status=sf.get("sensor_status"),
                    enoc_qte_demandee_l=en.get("qte_demandee") or 0,
                    enoc_qte_validee_l=en.get("qte_validee") or 0,
                    enoc_qte_ajoutee_l=en.get("qte_ajoutee") or 0,
                    enoc_nb_demandes=en.get("nb_demandes") or 0,
                    ecart_conso_vs_enoc_l=ecart,
                    synced_at=timezone.now(),
                ))

            if dry_run:
                for obj in objects[:10]:
                    self.stdout.write(
                        f"    {obj.site_id} | conso={obj.conso_snowflake_l} L | "
                        f"enoc_ajoutee={obj.enoc_qte_ajoutee_l} L | écart={obj.ecart_conso_vs_enoc_l}"
                    )
                self.stdout.write(self.style.WARNING(f"  DRY RUN — {len(objects)} ligne(s), rien écrit."))
                continue

            existing_keys = set(
                FuelConsommationMonthly.objects.filter(month_year=month_year)
                .values_list("site_id", flat=True)
            )
            created = sum(1 for o in objects if o.site_id not in existing_keys)
            updated = len(objects) - created

            with transaction.atomic():
                FuelConsommationMonthly.objects.bulk_create(
                    objects,
                    batch_size=1000,
                    update_conflicts=True,
                    unique_fields=["month_year", "site_id"],
                    update_fields=[
                        "site_name", "country", "typology", "site_type", "dg_count", "power_supply",
                        "has_genset_snowflake", "has_genset_enoc", "nb_ge_enoc", "has_genset",
                        "conso_snowflake_l", "nb_jours_data",
                        "conso_estimee_snowflake_l", "conso_estimee_snowflake_nb_releves",
                        "conso_estimee_enoc_l", "conso_estimee_nb_releves",
                        "conso_specifique_moy_l_kwh", "sensor_status",
                        "enoc_qte_demandee_l", "enoc_qte_validee_l", "enoc_qte_ajoutee_l",
                        "enoc_nb_demandes", "ecart_conso_vs_enoc_l", "synced_at",
                    ],
                )

            self.stdout.write(self.style.SUCCESS(f"  {created} créée(s), {updated} mise(s) à jour."))
            total_created += created
            total_updated += updated

        if not dry_run:
            self.stdout.write(self.style.SUCCESS(f"\n  Terminé — {total_created} créée(s), {total_updated} mise(s) à jour au total.\n"))
            sync_run.sites_fetched = total_sites_fetched
            sync_run.finished_at = timezone.now()
            if total_sites_fetched > 0:
                sync_run.status = FuelConsommationSyncRun.Status.SUCCESS
            else:
                sync_run.status = FuelConsommationSyncRun.Status.FAILED
                sync_run.error_message = last_error or "Aucune donnée Snowflake récupérée pour la période demandée."
            sync_run.save()
