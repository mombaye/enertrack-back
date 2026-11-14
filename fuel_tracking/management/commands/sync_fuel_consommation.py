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
import time

from celery.exceptions import SoftTimeLimitExceeded
from django.core.management import call_command
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

        from fuel_tracking.services.fuel_consommation_snowflake import FUEL_DATABASE, FUEL_SCHEMA, FUEL_REPORT_DATABASE

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  SYNC CONSOMMATION CARBURANT (Snowflake + ENOC) → EnerTrack")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  From month : {from_month}")
        self.stdout.write(f"  To month   : {to_month}")
        self.stdout.write(f"  Dry run    : {dry_run}")
        self.stdout.write(f"  Snowflake  : {FUEL_DATABASE}.{FUEL_SCHEMA} + {FUEL_REPORT_DATABASE}.{FUEL_SCHEMA} (VW_FUEL_REPORT)")
        self.stdout.write("═" * 80 + "\n")

        self._t0 = time.monotonic()
        sync_run = None
        if not dry_run:
            # Synchros restées RUNNING depuis > 1 h = processus tué ou bloqué : on les clôture en échec (traçabilité honnête).
            from datetime import timedelta
            FuelConsommationSyncRun.objects.filter(
                status=FuelConsommationSyncRun.Status.RUNNING, started_at__lt=timezone.now() - timedelta(hours=1),
            ).update(status=FuelConsommationSyncRun.Status.FAILED, finished_at=timezone.now(),
                     error_message="Synchronisation interrompue : processus arrêté avant la fin (clôturée automatiquement).")
            sync_run = FuelConsommationSyncRun.objects.create(
                month_from=from_month, month_to=to_month,
                status=FuelConsommationSyncRun.Status.RUNNING,
            )

        # Toute sortie anormale (exception, dépassement de la limite de temps Celery) est enregistrée
        # sur le run : sinon il restait RUNNING, puis « interrompue » une heure plus tard, sans cause.
        try:
            self._sync(sync_run, from_month, to_month, dry_run)
        except BaseException as e:
            if sync_run is not None and sync_run.status == FuelConsommationSyncRun.Status.RUNNING:
                if isinstance(e, SoftTimeLimitExceeded):
                    reason = (f"Délai dépassé : synchronisation arrêtée au bout de "
                              f"{int(time.monotonic() - self._t0)} s (limite de la tâche planifiée).")
                else:
                    reason = f"Échec : {type(e).__name__}: {e}"[:1000]
                FuelConsommationSyncRun.objects.filter(pk=sync_run.pk).update(
                    status=FuelConsommationSyncRun.Status.FAILED, finished_at=timezone.now(), error_message=reason,
                )
            raise

    def _step(self, label):
        self.stdout.write(f"  [{int(time.monotonic() - self._t0)} s] {label}")

    def _sync(self, sync_run, from_month, to_month, dry_run):
        # Référentiel GE ENOC (sites.nb_ge + ge_assets INSTALLED) — pas
        # dépendant du mois, une seule lecture pour toute la commande.
        # Si ENOC est injoignable, on continue avec Snowflake seul (le flag
        # ENOC reste False partout) plutôt que de bloquer toute la sync.
        try:
            genset_ref_enoc = fetch_genset_reference()
            self.stdout.write(f"  Référentiel GE ENOC : {len(genset_ref_enoc)} site(s), "
                               f"{sum(1 for v in genset_ref_enoc.values() if v['has_genset'])} avec GE.\n")
        except SoftTimeLimitExceeded:
            raise
        except Exception as e:
            # Panne ENOC passagère : valeurs ENOC déjà en base conservées mois par mois (voir plus bas) plutôt
            # qu'écrasées par « pas de GE » — sinon les sites dont le GE n'est connu que d'ENOC sortaient du périmètre.
            self.stdout.write(self.style.WARNING(f"  Référentiel GE ENOC indisponible ({e}) — valeurs précédentes conservées.\n"))
            genset_ref_enoc = None

        total_created = 0
        total_updated = 0
        total_sites_fetched = 0
        last_error = None
        failed_months = []
        genset_ref_unavailable = genset_ref_enoc is None

        for year, mo in months_between(from_month, to_month):
            month_year = f"{year:04d}-{mo:02d}"
            if genset_ref_unavailable:
                genset_ref_enoc = {
                    r["site_id"]: {"has_genset": r["has_genset_enoc"], "nb_ge": r["nb_ge_enoc"]}
                    for r in FuelConsommationMonthly.objects.filter(month_year=month_year)
                    .values("site_id", "has_genset_enoc", "nb_ge_enoc")
                }
            self.stdout.write(f"\n── {month_year} ──")

            self._step("Requête Snowflake (VW_FUEL_REPORT / GE_PROD_KWH / capteur)...")
            try:
                snowflake_data = fetch_monthly_consumption(year, mo)
            except SoftTimeLimitExceeded:
                raise
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  Erreur Snowflake pour {month_year} : {e}"))
                last_error = str(e)
                failed_months.append(month_year)
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
            self._step("Estimation ENOC (relevés de niveau)...")
            try:
                conso_estimee_by_site = fetch_estimated_consumption(year, mo)
            except SoftTimeLimitExceeded:
                raise
            except Exception as e:
                conso_estimee_by_site = {
                    r["site_id"]: {"conso_estimee_l": r["conso_estimee_enoc_l"], "nb_releves": r["conso_estimee_nb_releves"]}
                    for r in FuelConsommationMonthly.objects.filter(month_year=month_year, conso_estimee_enoc_l__isnull=False)
                    .values("site_id", "conso_estimee_enoc_l", "conso_estimee_nb_releves")
                }
                self.stdout.write(self.style.WARNING(
                    f"  Estimation ENOC (relevés de niveau) indisponible ({e}) — valeurs précédentes conservées."))
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
                # ravitaillements ENOC entre les 2 relevés).
                #
                # 4 cas possibles (jamais de None silencieux) :
                #   cas 1 : delta ≥ 0 et conso_brute ≥ 0 → CONSOMMATION normale
                #   cas 2 : delta < 0 et ravit_enoc = 0  → RAVITAILLEMENT_NON_ENOC_DETECTE
                #           (niveau monte sans ravitaillement ENOC connu)
                #   cas 3 : delta < 0 et ravit_enoc > 0  → INCOHERENCE_DELTA_NEGATIF
                #           (niveau monte malgré ravitaillements ENOC comptés)
                #   cas 4 : delta ≥ 0 mais conso_brute < 0 → INCOHERENCE_RAVIT_EXCESSIF
                #           (ravitaillements ENOC > baisse de cuve)
                # Dans tous les cas : niveau_fin = stock effectif détecté, toujours conservé.
                niveau_debut = sf.get("niveau_debut")
                niveau_fin = sf.get("niveau_fin")
                conso_estimee_sf = None
                conso_estimee_sf_motif = None
                ravitaillement_non_enoc_sf = None

                if niveau_debut is not None and niveau_fin is not None:
                    ravit_enoc = ajoutee_l or 0
                    delta = niveau_debut - niveau_fin   # positif = cuve baisse = conso
                    conso_brute = delta + ravit_enoc

                    if delta < 0 and ravit_enoc == 0:
                        # Cas 2 : niveau monte sans ENOC → ravitaillement non enregistré
                        conso_estimee_sf_motif = "RAVITAILLEMENT_NON_ENOC_DETECTE"
                        ravitaillement_non_enoc_sf = -delta   # volume ajouté non ENOC
                    elif delta < 0:
                        # Cas 3 : niveau monte malgré ravitaillements ENOC
                        conso_estimee_sf_motif = "INCOHERENCE_DELTA_NEGATIF"
                    elif conso_brute < 0:
                        # Cas 4 : ravitaillements ENOC excessifs vs baisse de cuve
                        conso_estimee_sf_motif = "INCOHERENCE_RAVIT_EXCESSIF"
                    else:
                        # Cas 1 : consommation normale
                        conso_estimee_sf = conso_brute

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
                    niveau_debut_snowflake_l=niveau_debut,
                    niveau_fin_snowflake_l=niveau_fin,
                    conso_estimee_snowflake_l=conso_estimee_sf,
                    conso_estimee_sf_motif=conso_estimee_sf_motif,
                    ravitaillement_non_enoc_sf_l=ravitaillement_non_enoc_sf,
                    conso_estimee_snowflake_nb_releves=sf.get("niveau_nb_releves"),
                    conso_estimee_enoc_l=est.get("conso_estimee_l"),
                    conso_estimee_nb_releves=est.get("nb_releves"),
                    conso_specifique_moy_l_kwh=sf.get("conso_specifique_moy_l_kwh"),
                    ge_prod_kwh=sf.get("ge_prod_kwh"),
                    sensor_status=sf.get("sensor_status"),
                    quality_status=sf.get("quality_status"),
                    raw_point_count=sf.get("raw_point_count"),
                    valid_point_count=sf.get("valid_point_count"),
                    isolated_spike_count=sf.get("isolated_spike_count"),
                    over_capacity_point_count=sf.get("over_capacity_point_count"),
                    refill_detected=sf.get("refill_detected") or False,
                    estimated_refill_volume_l=sf.get("estimated_refill_volume_l"),
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
                        f"niv_fin={obj.niveau_fin_snowflake_l} L | "
                        f"conso_est_sf={obj.conso_estimee_snowflake_l} L"
                        + (f" [{obj.conso_estimee_sf_motif}]" if obj.conso_estimee_sf_motif else "")
                        + f" | enoc_ajoutee={obj.enoc_qte_ajoutee_l} L | écart={obj.ecart_conso_vs_enoc_l}"
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
                        "niveau_debut_snowflake_l", "niveau_fin_snowflake_l",
                        "conso_estimee_snowflake_l", "conso_estimee_sf_motif", "ravitaillement_non_enoc_sf_l",
                        "conso_estimee_snowflake_nb_releves",
                        "conso_estimee_enoc_l", "conso_estimee_nb_releves",
                        "conso_specifique_moy_l_kwh", "ge_prod_kwh", "sensor_status",
                        "quality_status", "raw_point_count", "valid_point_count",
                        "isolated_spike_count", "over_capacity_point_count",
                        "refill_detected", "estimated_refill_volume_l",
                        "enoc_qte_demandee_l", "enoc_qte_validee_l", "enoc_qte_ajoutee_l",
                        "enoc_nb_demandes", "ecart_conso_vs_enoc_l", "synced_at",
                    ],
                )

            self.stdout.write(self.style.SUCCESS(f"  {created} créée(s), {updated} mise(s) à jour."))
            total_created += created
            total_updated += updated

            # Snapshot stock fin de mois (M) — affiché dans l'onglet Stock.
            self._step("Snapshot stock mensuel...")
            try:
                call_command("sync_fuel_stock", month=f"{year}-{mo:02d}")
                self.stdout.write(f"  Stock mensuel {year}-{mo:02d} synchronisé.")
            except SoftTimeLimitExceeded:
                raise
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  Sync stock mensuel échoué ({e})."))

        if not dry_run:
            self._step("Fin.")
            self.stdout.write(self.style.SUCCESS(f"\n  Terminé — {total_created} créée(s), {total_updated} mise(s) à jour au total.\n"))
            sync_run.sites_fetched = total_sites_fetched
            sync_run.finished_at = timezone.now()
            if total_sites_fetched > 0 and not failed_months:
                sync_run.status = FuelConsommationSyncRun.Status.SUCCESS
            elif total_sites_fetched > 0:
                # Un mois a échoué (souvent le mois en cours) : ne pas afficher « connecté » alors qu'il n'est plus rafraîchi.
                sync_run.status = FuelConsommationSyncRun.Status.FAILED
                sync_run.error_message = f"Snowflake en échec pour {', '.join(failed_months)} : {last_error}"[:1000]
            else:
                sync_run.status = FuelConsommationSyncRun.Status.FAILED
                sync_run.error_message = last_error or "Aucune donnée Snowflake récupérée pour la période demandée."
            sync_run.save()
