# fuel_tracking/management/commands/backfill_battery_charge.py
"""
Reprise LÉGÈRE (Snowflake en lecture seule) de la Puissance Médiane Recharge batterie dans les faits journaliers,
selon les instructions de l'équipe data (24/11/2026) : table DB_GFMS_ANALYTICS_PROD.GOLD.BATTERY_REPORT, grain DATA_ID + DATE.

  BATTERY_CHARGE_POWER_MEDIAN_KW           → battery_charge_median_kw   médiane du jour de courant × tension ÷ 1000,
                                                                        mesures à courant et tension positifs ; NULL si
                                                                        aucune charge (jamais 0)
  TELEMETRY_RECEIVED_COUNT                 → battery_telemetry_points   pas de 5 min reçus (288 max par jour)
  BATTERY_CHARGE_POSITIVE_TIMESTAMP_COUNT  → battery_charge_slots       mesures en charge

Une seule requête, sur cette seule table (aucune télémétrie 5 min / Silver). Ne touche à aucune autre colonne et ne
complète que les lignes existantes : un jour absent des faits n'est jamais créé à moitié (le calcul CPH compte les jours
présents) — il est signalé, à reprendre avec sync_fuel_daily_facts. La synchro horaire / nocturne lit déjà ces colonnes ;
cette commande sert à remplir les mois passés sans relancer toute la synchro.

  python manage.py backfill_battery_charge --start 2026-09-01 --end 2026-11-24 [--dry-run]
"""
from __future__ import annotations

import time
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

FIELDS = ("battery_telemetry_points", "battery_charge_slots", "battery_charge_median_kw",
          "battery_charge_median_current_a", "battery_charge_median_voltage_v", "battery_charge_values_kw")


def _same(lo, hi):
    """Doublon contradictoire (deux valeurs différentes pour le même DATA_ID / jour) → NULL, jamais un choix arbitraire."""
    return hi if lo == hi else None


class Command(BaseCommand):
    help = "Reprise de la Puissance Médiane Recharge batterie (GOLD.BATTERY_REPORT, lecture seule) dans les faits journaliers."

    def add_arguments(self, parser):
        parser.add_argument("--start", type=date.fromisoformat, required=True)
        parser.add_argument("--end", type=date.fromisoformat, required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, start, end, dry_run, **opts):
        from fuel_tracking.models import FuelSiteDailyFacts as F, FuelSiteInventory as INV
        from fuel_tracking.services.fuel_daily_facts_snowflake import ANALYTICS, _connect, _dec

        inv = set(INV.objects.exclude(data_id=None).values_list("data_id", flat=True))
        if not inv:
            self.stdout.write("Inventaire Snowflake vide : rien à reprendre.")
            return
        ids = ",".join(str(int(i)) for i in sorted(inv))   # entiers issus de notre base (jamais une saisie utilisateur)
        sql = (f"SELECT DATA_ID, DATE, MIN(TELEMETRY_RECEIVED_COUNT), MAX(TELEMETRY_RECEIVED_COUNT), "
               f"MIN(BATTERY_CHARGE_POSITIVE_TIMESTAMP_COUNT), MAX(BATTERY_CHARGE_POSITIVE_TIMESTAMP_COUNT), "
               f"MIN(BATTERY_CHARGE_POWER_MEDIAN_KW), MAX(BATTERY_CHARGE_POWER_MEDIAN_KW) "
               f"FROM {ANALYTICS}.BATTERY_REPORT WHERE DATA_ID IN ({ids}) AND DATE >= %(s)s AND DATE <= %(e)s "
               f"GROUP BY DATA_ID, DATE")
        vals: dict = {}
        conn = _connect()
        try:
            cur = conn.cursor()
            t = time.time()
            cur.execute(sql, {"s": start, "e": end})
            for did, d, p0, p1, c0, c1, k0, k1 in cur.fetchall():
                pts, pos, kw = _same(p0, p1), _same(c0, c1), _same(k0, k1)
                vals[(int(did), d)] = {"battery_telemetry_points": int(pts) if pts is not None else None,
                                       "battery_charge_slots": int(pos) if pos is not None else None,
                                       "battery_charge_median_kw": _dec(kw)}
        finally:
            conn.close()
        n_kw = sum(1 for v in vals.values() if v["battery_charge_median_kw"] is not None)
        self.stdout.write(f"  BATTERY_REPORT : {len(vals)} jour(s) site lus, {n_kw} avec une puissance de recharge "
                          f"({len({k[0] for k, v in vals.items() if v['battery_charge_median_kw'] is not None})} DATA_ID) "
                          f"en {time.time() - t:.0f} s")
        if dry_run:
            self.stdout.write("Simulation : rien écrit.")
            return
        existing = {(did, d): pk for pk, did, d in
                    F.objects.filter(data_id__in=list(inv), date__gte=start, date__lte=end).values_list("pk", "data_id", "date")}
        upd = [F(pk=existing[k], **v, battery_charge_median_current_a=None, battery_charge_median_voltage_v=None,
                 battery_charge_values_kw=None) for k, v in vals.items() if k in existing]
        absents = sorted({k[1] for k, v in vals.items() if k not in existing and v["battery_charge_median_kw"] is not None})
        with transaction.atomic():
            F.objects.bulk_update(upd, list(FIELDS), batch_size=2000)
        n_upd_kw = sum(1 for o in upd if o.battery_charge_median_kw is not None)
        self.stdout.write(self.style.SUCCESS(
            f"Faits journaliers : {len(upd)} ligne(s) complétée(s), dont {n_upd_kw} avec une puissance de recharge ({start} → {end})."))
        if absents:
            self.stdout.write(self.style.WARNING(
                f"{len(absents)} jour(s) avec une puissance de recharge mais sans ligne de faits (non créées) : "
                f"{absents[0]} → {absents[-1]}. À reprendre avec : python manage.py sync_fuel_daily_facts "
                f"--start {absents[0]} --end {absents[-1]}"))
