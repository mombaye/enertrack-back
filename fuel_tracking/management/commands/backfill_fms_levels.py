# fuel_tracking/management/commands/backfill_fms_levels.py
"""
Reprise LÉGÈRE (Snowflake en lecture seule) du niveau Fuel Sensor FMS et du compteur horaire GE dans les faits journaliers
(FuelSiteDailyFacts.fms_level_l, dg_total_running_h), sur une plage de dates.

La synchro horaire / nocturne les remplit déjà pour J-3 / J-35 ; cette commande sert une seule fois pour les mois plus
anciens (ex. relevés initiaux de fin septembre). Elle ne lit QUE VW_FUEL_REPORT et GENSET_REPORT (une requête chacune),
ne touche à aucune autre colonne et ne complète que les lignes existantes : un jour absent des faits n'est jamais créé à
moitié (le calcul CPH compte les jours présents) — il est signalé, à reprendre avec sync_fuel_daily_facts.

  python manage.py backfill_fms_levels --start 2026-08-20 --end 2026-11-19
"""
from __future__ import annotations

from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Reprise du niveau FMS et du compteur GE (Snowflake, lecture seule) dans les faits journaliers."

    def add_arguments(self, parser):
        parser.add_argument("--start", type=date.fromisoformat, required=True)
        parser.add_argument("--end", type=date.fromisoformat, required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, start, end, dry_run, **opts):
        import time

        from fuel_tracking.models import FuelSiteDailyFacts as F, FuelSiteInventory as INV
        from fuel_tracking.services.fuel_daily_facts_snowflake import ANALYTICS, FUEL_REPORT, _connect, _dec

        inv = {int(did): (country, sid) for did, country, sid in INV.objects.exclude(data_id=None).values_list("data_id", "country", "site_id")}
        if not inv:
            self.stdout.write("Inventaire Snowflake vide : rien à reprendre.")
            return
        ids = ",".join(str(i) for i in sorted(inv))   # entiers issus de notre base (jamais une saisie utilisateur)
        vals: dict = {}
        conn = _connect()
        try:
            cur = conn.cursor()
            for label, sql, field in (
                ("niveau FMS (VW_FUEL_REPORT)", f"SELECT DATA_ID, DATE, IFF(MIN(LAST_VALID_LEVEL) = MAX(LAST_VALID_LEVEL), MAX(LAST_VALID_LEVEL), NULL) "
                 f"FROM {FUEL_REPORT}.VW_FUEL_REPORT WHERE DATA_ID IN ({ids}) AND DATE >= %(s)s AND DATE <= %(e)s GROUP BY DATA_ID, DATE", "fms_level_l"),
                ("compteur GE (GENSET_REPORT)", f"SELECT DATA_ID, DATE, IFF(MIN(TOTAL_DG_RUNNING_HOUR) = MAX(TOTAL_DG_RUNNING_HOUR), MAX(TOTAL_DG_RUNNING_HOUR), NULL) "
                 f"FROM {ANALYTICS}.GENSET_REPORT WHERE DATA_ID IN ({ids}) AND DATE >= %(s)s AND DATE <= %(e)s GROUP BY DATA_ID, DATE", "dg_total_running_h"),
            ):
                t = time.time()
                cur.execute(sql, {"s": start, "e": end})
                n = 0
                for did, d, v in cur.fetchall():
                    if int(did) in inv:
                        vals.setdefault((int(did), d), {})[field] = _dec(v)
                        n += v is not None
                self.stdout.write(f"  {label} : {n} valeur(s) en {time.time() - t:.0f} s")
        finally:
            conn.close()
        if dry_run:
            self.stdout.write(f"Simulation : {len(vals)} site/jour lus, rien écrit.")
            return
        existing = {(did, d): pk for pk, did, d in F.objects.filter(data_id__in=list(inv), date__gte=start, date__lte=end).values_list("pk", "data_id", "date")}
        upd = [F(pk=existing[k], fms_level_l=v.get("fms_level_l"), dg_total_running_h=v.get("dg_total_running_h"))
               for k, v in vals.items() if k in existing]
        absents = sorted({d for k, d in ((k, k[1]) for k, v in vals.items() if k not in existing and any(x is not None for x in v.values()))})
        with transaction.atomic():
            F.objects.bulk_update(upd, ["fms_level_l", "dg_total_running_h"], batch_size=2000)
        self.stdout.write(self.style.SUCCESS(f"Faits journaliers : {len(upd)} ligne(s) complétée(s) ({start} → {end})."))
        if absents:
            self.stdout.write(self.style.WARNING(
                f"{len(absents)} jour(s) avec une valeur FMS mais sans ligne de faits (non créées) : {absents[0]} → {absents[-1]}. "
                f"À reprendre avec : python manage.py sync_fuel_daily_facts --start {absents[0]} --end {absents[-1]}"))
