# financial/management/commands/report_missing_conso.py
"""
Rapporte, pour le périmètre Aktivco (Snowflake CLIENT='AktivCo', voir
financial/services/site_scope_snowflake.py) et une plage de mois, quels
sites×mois n'ont aucune donnée Grid/ACM/Solaire synchronisée dans
FinancialConsoMonthly, et lesquels ont une ligne partielle (une source
manquante). Sert à diagnostiquer les trous de couverture Snowflake sur
/financial/suivi-conso — voir échange du 2026-08.

Usage:
    python manage.py report_missing_conso --from-month 2026-01 --to-month 2026-08
    python manage.py report_missing_conso --month 2026-08 --site DBL_0572
"""
from django.core.management.base import BaseCommand

from financial.models import FinancialConsoMonthly
from financial.services.site_scope_snowflake import fetch_aktivco_site_scope


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
    help = "Rapporte les sites×mois Aktivco sans donnée Grid/ACM/Solaire synchronisée (trous Snowflake)."

    def add_arguments(self, parser):
        parser.add_argument("--month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--from-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--to-month", type=str, default=None, help="YYYY-MM")
        parser.add_argument("--site", type=str, default=None, help="Limiter à un site_id précis")

    def handle(self, *args, **options):
        month = options.get("month")
        from_month = options.get("from_month")
        to_month = options.get("to_month")
        site_filter = options.get("site")

        if month:
            from_month = to_month = month

        if not from_month or not to_month:
            self.stdout.write(self.style.ERROR("Préciser --month YYYY-MM, ou --from-month/--to-month YYYY-MM."))
            return

        months = months_between(from_month, to_month)

        self.stdout.write("\n" + "═" * 80)
        self.stdout.write("  RAPPORT COUVERTURE CONSO FINANCIER (Grid/ACM/Solaire)")
        self.stdout.write("═" * 80)

        aktivco_sites = fetch_aktivco_site_scope(country="Senegal")
        monitored_by_site = {s["site_id"]: s["site_monitored"] for s in aktivco_sites}
        site_ids = sorted(monitored_by_site.keys())
        if site_filter:
            site_ids = [s for s in site_ids if s == site_filter]

        total_combos = len(site_ids) * len(months)
        self.stdout.write(f"  Périmètre : {len(site_ids)} site(s) × {len(months)} mois = {total_combos} combinaison(s)\n")

        existing = {
            (r["site__site_id"], r["year"], r["month"]): r
            for r in FinancialConsoMonthly.objects.filter(
                site__site_id__in=site_ids,
            ).values("site__site_id", "year", "month", "fms_grid_kwh", "fms_acm_kwh", "solar_kwh")
        }

        no_row_at_all = []
        missing_grid = []
        missing_acm = []
        missing_solar = []

        for sid in site_ids:
            for yr, mo in months:
                key = (sid, yr, mo)
                row = existing.get(key)
                if row is None:
                    no_row_at_all.append((sid, yr, mo))
                    continue
                if row["fms_grid_kwh"] is None:
                    missing_grid.append((sid, yr, mo))
                if row["fms_acm_kwh"] is None:
                    missing_acm.append((sid, yr, mo))
                if row["solar_kwh"] is None:
                    missing_solar.append((sid, yr, mo))

        # sites_no_row : sites concernés par AU MOINS UN mois manquant (peut
        # avoir de la donnée sur d'autres mois) — utilisé pour la liste
        # affichée plus bas. sites_fully_empty : sites SANS AUCUNE donnée sur
        # TOUTE la période demandée (aucun row, aucun mois) — c'est CE sous-
        # ensemble qui corrèle avec SITE_MONITORED, pas sites_no_row.
        sites_no_row = sorted({s for s, _, _ in no_row_at_all})
        sites_with_any_row = {s for (s, _, _) in existing.keys()}
        sites_fully_empty = sorted(set(site_ids) - sites_with_any_row)
        sites_with_any_data = sorted(sites_with_any_row)

        self.stdout.write(f"  Aucune ligne du tout (ni Grid ni ACM ni Solaire) : {len(no_row_at_all)} / {total_combos}")
        self.stdout.write(f"    → {len(sites_no_row)} site(s) distinct(s) concerné(s) par au moins un mois manquant")
        self.stdout.write(f"    → {len(sites_fully_empty)} site(s) SANS AUCUNE donnée sur toute la période")
        self.stdout.write(f"  Lignes existantes mais Grid manquant    : {len(missing_grid)}")
        self.stdout.write(f"  Lignes existantes mais ACM manquant     : {len(missing_acm)}")
        self.stdout.write(f"  Lignes existantes mais Solaire manquant : {len(missing_solar)}\n")

        # ── Croisement SITE_MONITORED — vérifié le 2026-08 : les sites sans
        # AUCUNE donnée sur toute la période sont quasi toujours SITE_
        # MONITORED='0' côté Snowflake (pas de capteur/compteur raccordé au
        # GFMS, trou d'instrumentation physique — rien à corriger côté sync).
        no_data_monitored = sum(1 for s in sites_fully_empty if monitored_by_site.get(s))
        no_data_unmonitored = sum(1 for s in sites_fully_empty if not monitored_by_site.get(s))
        has_data_monitored = sum(1 for s in sites_with_any_data if monitored_by_site.get(s))
        has_data_unmonitored = sum(1 for s in sites_with_any_data if not monitored_by_site.get(s))

        self.stdout.write("  Croisement SITE_MONITORED (Snowflake) :")
        self.stdout.write(f"    Sites avec AU MOINS une donnée : {len(sites_with_any_data)}  (monitored={has_data_monitored}, non-monitored={has_data_unmonitored})")
        self.stdout.write(f"    Sites SANS AUCUNE donnée       : {len(sites_fully_empty)}  (monitored={no_data_monitored}, non-monitored={no_data_unmonitored})")
        if no_data_unmonitored:
            self.stdout.write(self.style.WARNING(
                f"    → {no_data_unmonitored} site(s) sans donnée ET non-monitored : "
                "trou d'instrumentation physique (pas de capteur GFMS), pas un souci de sync."
            ))
        if no_data_monitored:
            self.stdout.write(self.style.ERROR(
                f"    → {no_data_monitored} site(s) sans donnée MAIS monitored='1' : "
                "anomalie à investiguer (devrait avoir de la donnée)."
            ))
        self.stdout.write("")

        if site_filter:
            self.stdout.write(f"  Détail pour {site_filter} (SITE_MONITORED={monitored_by_site.get(site_filter)}) :")
            for yr, mo in months:
                row = existing.get((site_filter, yr, mo))
                if row is None:
                    self.stdout.write(f"    {yr}-{mo:02d} : AUCUNE donnée")
                else:
                    self.stdout.write(
                        f"    {yr}-{mo:02d} : grid={row['fms_grid_kwh']} "
                        f"acm={row['fms_acm_kwh']} solar={row['solar_kwh']}"
                    )
        else:
            self.stdout.write("  Sites sans AUCUNE donnée sur toute la période (20 premiers) :")
            for sid in sites_fully_empty[:20]:
                mon = "monitored" if monitored_by_site.get(sid) else "non-monitored"
                self.stdout.write(f"    {sid}  ({mon})")

        self.stdout.write("\n" + "═" * 80 + "\n")
