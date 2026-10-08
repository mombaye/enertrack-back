# financial/management/commands/sync_financial_conso.py
"""
Synchronise la conso FMS/ACM/Solaire (100% Snowflake depuis le 2026-08 — plus
aucune source SQL Server, cf. financial/services/conso_service.py::_fetch_remote_bulk)
vers FinancialConsoMonthly (Postgres) — même pattern que
fuel_tracking/management/commands/sync_efms_fuel.py.

Objectif : que financial/services/conso_service.py ne fasse plus JAMAIS
d'appel Snowflake live pendant une requête HTTP (SuiviConsoView,
SiteMargeDetailView) — tout est précalculé ici et lu depuis Postgres ensuite.

Périmètre par défaut : sites Aktivco + redevance Grid (marge financière). Pour récupérer les
données des autres sites de Gestion des sites (affichés dans Suivi Conso) :
    python manage.py sync_financial_conso --from-month 2026-01 --to-month 2026-09 --sites-sans-donnees
    python manage.py sync_financial_conso --from-month 2026-01 --to-month 2026-09 --tous-les-sites
    python manage.py sync_financial_conso --month 2026-09 --site DKR_0001 --site BKL_0086
Un bilan liste à la fin les sites toujours sans donnée (absents de Snowflake : non instrumentés).
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from billing.models import ContractSiteLink
from core.models import Site
from financial.models import FinancialConsoMonthly, FinancialConsoSyncRun
from financial.services.conso_service import FinancialConsoService
from financial.services.site_scope_snowflake import fetch_aktivco_site_scope


def parse_month(value: str) -> tuple[int, int]:
    year, month = str(value).split("-")
    return int(year), int(month)


class Command(BaseCommand):
    help = "Synchronise la conso FMS/ACM/Solaire mensuelle vers FinancialConsoMonthly"

    def add_arguments(self, parser):
        parser.add_argument("--month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--from-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--to-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--dry-run", action="store_true")
        # Périmètre élargi (par défaut : Aktivco + redevance Grid, périmètre de la marge financière).
        parser.add_argument("--tous-les-sites", action="store_true",
                            help="Tous les sites de Gestion des sites (core.Site), pas seulement Aktivco + grid_fee.")
        parser.add_argument("--sites-sans-donnees", action="store_true",
                            help="Seulement les sites de Gestion des sites sans aucune donnée Grid/ACM/Solaire sur la période.")
        parser.add_argument("--site", action="append", default=[],
                            help="Site(s) précis (répétable) : --site DKR_0001 --site DBL_0068")

    def handle(self, *args, **options):
        month = options.get("month")
        from_month = options.get("from_month")
        to_month = options.get("to_month")
        dry_run = options["dry_run"]
        all_sites = options["tous_les_sites"]
        missing_only = options["sites_sans_donnees"]
        only_sites = [s.strip() for s in options["site"] if s.strip()]

        if month:
            from_month = month
            to_month = month

        if not from_month or not to_month:
            self.stdout.write(self.style.ERROR(
                "Préciser --month YYYY-MM, ou --from-month/--to-month YYYY-MM."
            ))
            return

        year_start, month_start = parse_month(from_month)
        year_end, month_end = parse_month(to_month)

        sync_run = FinancialConsoSyncRun.objects.create(
            month_from=from_month,
            month_to=to_month,
            status=FinancialConsoSyncRun.Status.RUNNING,
        )

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  SYNC CONSO FINANCIER (100% Snowflake) → EnerTrack")
        self.stdout.write("═" * 80)
        self.stdout.write(f"  From month : {from_month}")
        self.stdout.write(f"  To month   : {to_month}")
        self.stdout.write(f"  Dry run    : {dry_run}")
        self.stdout.write("═" * 80 + "\n")

        try:
            # ── Périmètre : mêmes sites que FinancialEvaluateView ──────────────
            # (Aktivco + grid_fee) — pas tout le catalogue (~3000 sites), seulement
            # ceux réellement suivis en marge financière. C'est le périmètre exact
            # de production : nécessite le catalogue local (core.Site/
            # billing.ContractSiteLink), alimenté via /admin/sites (import Excel).
            site_map: dict[str, int] = {}
            if all_sites or missing_only or only_sites:
                # Périmètre élargi : sites de Gestion des sites (core.Site).
                qs = Site.objects.all()
                if only_sites:
                    qs = qs.filter(site_id__in=only_sites)
                site_map = dict(qs.values_list("site_id", "pk"))
                if missing_only:
                    have = set(
                        FinancialConsoMonthly.objects.filter(
                            site_id__in=site_map.values(),
                            year__gte=year_start, year__lte=year_end,
                        ).exclude(fms_grid_kwh=None, fms_acm_kwh=None, solar_kwh=None)
                        .values_list("site_id", "year", "month")
                    )
                    have_sites = {pk for pk, y, m in have if (year_start, month_start) <= (y, m) <= (year_end, month_end)}
                    site_map = {sid: pk for sid, pk in site_map.items() if pk not in have_sites}
                self.stdout.write("  Périmètre : Gestion des sites"
                                  + (" — sites sans aucune donnée sur la période" if missing_only else "")
                                  + (f" — {len(only_sites)} site(s) demandé(s)" if only_sites else ""))
            else:
                links = ContractSiteLink.objects.filter(
                    site__invoice_payment__iexact="Aktivco",
                    site__grid_fee=True,
                ).select_related("site")
                for link in links:
                    site_map[link.site.site_id] = link.site_id

            site_ids = list(site_map.keys())

            # ── Repli Snowflake : si le catalogue local n'a pas encore été
            # importé (site_map vide), on prend le périmètre Aktivco directement
            # depuis Snowflake (SITES_FILTERED_FIXED.CLIENT = 'AktivCo') — voir
            # financial/services/site_scope_snowflake.py. Volontairement plus
            # large que le vrai périmètre prod (pas de recroisement grid_fee,
            # qui n'existe pas sur Snowflake) : sert la visibilité en local en
            # attendant l'import réel, PAS à utiliser tel quel en prod.
            used_snowflake_scope = False
            if not site_ids and not (all_sites or missing_only or only_sites):
                self.stdout.write(self.style.WARNING(
                    "  Catalogue local vide (core.Site/ContractSiteLink) — "
                    "repli sur le périmètre Aktivco Snowflake (sans grid_fee)."
                ))
                valid_zones = {c[0] for c in Site.ZONE_CHOICES}
                aktivco_sites = fetch_aktivco_site_scope(country="Senegal")
                for row in aktivco_sites:
                    # Le préfixe du site_id (ex: "DKR_2878" -> "DKR") correspond
                    # exactement aux codes ZONE_CHOICES — vérifié le 2026-08.
                    prefix = row["site_id"].split("_", 1)[0].upper()
                    zone = prefix if prefix in valid_zones else None
                    site_obj, created = Site.objects.get_or_create(
                        site_id=row["site_id"],
                        defaults={"name": row["site_name"], "invoice_payment": "AktivCo", "country": "sen", "zone": zone},
                    )
                    if not created and not site_obj.zone and zone:
                        site_obj.zone = zone
                        site_obj.save(update_fields=["zone"])
                    site_map[site_obj.site_id] = site_obj.pk
                site_ids = list(site_map.keys())
                used_snowflake_scope = True

            self.stdout.write(f"  Sites en périmètre : {len(site_ids)}"
                               + ("  (source : Snowflake CLIENT=AktivCo, sans grid_fee)" if used_snowflake_scope
                                  else "  (source : Gestion des sites)" if (all_sites or missing_only or only_sites)
                                  else "  (source : catalogue local, Aktivco + grid_fee)"))

            if not site_ids:
                self.stdout.write(self.style.WARNING("  Aucun site en périmètre — rien à synchroniser."))
                sync_run.status = FinancialConsoSyncRun.Status.SUCCESS
                sync_run.finished_at = timezone.now()
                sync_run.save()
                return

            self.stdout.write("  Requête Snowflake (GRID_REPORT/AC_METER/SOLAR — plus aucune source SQL Server)...")
            remote, source_errors = FinancialConsoService._fetch_remote_bulk(
                site_ids=site_ids,
                year_start=year_start,
                month_start=month_start,
                year_end=year_end,
                month_end=month_end,
            )

            sync_run.snowflake_error = source_errors.get("snowflake")
            sync_run.solar_error = source_errors.get("solar")
            if source_errors.get("snowflake"):
                self.stdout.write(self.style.WARNING(f"  Snowflake (Grid/ACM) injoignable : {source_errors['snowflake']}"))
            if source_errors.get("solar"):
                self.stdout.write(self.style.WARNING(f"  Snowflake (Solaire) injoignable : {source_errors['solar']}"))

            raw_count = len(remote)
            self.stdout.write(f"  Lignes (site, année, mois) récupérées : {raw_count}")

            if dry_run:
                for key, vals in list(remote.items())[:10]:
                    sid, yr, mo = key
                    self.stdout.write(
                        f"  {sid} {yr}-{mo:02d} | grid={vals.get('fms_grid_kwh')} "
                        f"[{vals.get('grid_mode')}] | acm={vals.get('fms_acm_kwh')} | "
                        f"solar={vals.get('solar_kwh')}"
                    )
                sync_run.status = FinancialConsoSyncRun.Status.SUCCESS
                sync_run.rows_fetched = raw_count
                sync_run.finished_at = timezone.now()
                sync_run.save()
                self.stdout.write(self.style.SUCCESS("\n  DRY RUN terminé. Aucune donnée insérée.\n"))
                return

            existing_keys = set(
                FinancialConsoMonthly.objects.filter(
                    site_id__in=site_map.values(),
                ).filter(
                    year__gte=year_start, year__lte=year_end,
                ).values_list("site_id", "year", "month")
            )

            objects = []
            created = 0
            updated = 0
            now = timezone.now()

            for (site_id_str, yr, mo), vals in remote.items():
                site_pk = site_map.get(site_id_str)
                if site_pk is None:
                    continue

                key = (site_pk, yr, mo)
                if key in existing_keys:
                    updated += 1
                else:
                    created += 1

                objects.append(
                    FinancialConsoMonthly(
                        site_id=site_pk,
                        year=yr,
                        month=mo,
                        fms_grid_kwh=vals.get("fms_grid_kwh"),
                        grid_mode=vals.get("grid_mode") or "none",
                        fms_acm_kwh=vals.get("fms_acm_kwh"),
                        solar_kwh=vals.get("solar_kwh"),
                        unavail_hours=vals.get("unavail_hours"),
                        synced_at=now,
                    )
                )

            with transaction.atomic():
                FinancialConsoMonthly.objects.bulk_create(
                    objects,
                    batch_size=1000,
                    update_conflicts=True,
                    unique_fields=["site", "year", "month"],
                    update_fields=[
                        "fms_grid_kwh", "grid_mode", "fms_acm_kwh",
                        "solar_kwh", "unavail_hours", "synced_at",
                    ],
                )

            sync_run.status = FinancialConsoSyncRun.Status.SUCCESS
            sync_run.rows_fetched = raw_count
            sync_run.rows_created = created
            sync_run.rows_updated = updated
            sync_run.finished_at = timezone.now()
            sync_run.save()

            self.stdout.write(self.style.SUCCESS("\n  Synchronisation terminée."))
            self.stdout.write(f"  Créées : {created}")
            self.stdout.write(f"  Mises à jour : {updated}\n")
            self._report(site_ids, remote)

        except Exception as exc:  # noqa: BLE001
            sync_run.status = FinancialConsoSyncRun.Status.FAILED
            sync_run.error_message = str(exc)
            sync_run.finished_at = timezone.now()
            sync_run.save()

            self.stdout.write(self.style.ERROR(f"\n  Erreur sync conso financier : {exc}\n"))
            raise

    def _report(self, site_ids: list[str], remote: dict) -> None:
        """Bilan : sites interrogés, sites avec donnée, sites toujours sans donnée Snowflake."""
        with_data = {sid for (sid, _y, _m), v in remote.items()
                     if any(v.get(k) is not None for k in ("fms_grid_kwh", "fms_acm_kwh", "solar_kwh"))}
        without = sorted(set(site_ids) - with_data)
        self.stdout.write(f"  Sites interrogés : {len(site_ids)}")
        self.stdout.write(f"  Sites avec au moins une donnée Grid/ACM/Solaire : {len(with_data)}")
        self.stdout.write(f"  Sites toujours sans donnée : {len(without)}"
                          + (" (aucun relevé dans Snowflake GRID_REPORT / AC_METER / Solaire : site non"
                             " instrumenté ou non raccordé au GFMS)" if without else ""))
        if without:
            self.stdout.write("    " + ", ".join(without[:50]) + (" …" if len(without) > 50 else ""))
