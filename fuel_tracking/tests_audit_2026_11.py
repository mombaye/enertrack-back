"""Non-régression des défauts trouvés à l'audit Suivi carburant du 2026-11-11."""
from datetime import date, timedelta
from decimal import Decimal as D
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from fuel_tracking.models import (
    FuelConsommationMonthly, FuelConsommationSyncRun, FuelEnocSyncRun, FuelStockSnapshot, FuelStockSyncRun,
)
from fuel_tracking.services import cph_engine as E
from fuel_tracking.tests import OCT1, fact, outdoor

STOCK = "fuel_tracking.management.commands.sync_fuel_stock"
CONSO = "fuel_tracking.management.commands.sync_fuel_consommation"


def api():
    c = APIClient()
    c.force_authenticate(get_user_model().objects.create_user("audit", "audit@x.sn", "p", role="analyst"))
    return c


class StockListIgnoresArchivedSnapshotsTests(TestCase):
    def test_monthly_archives_do_not_triple_sites(self):
        for y, m in ((None, None), (2026, 10), (2026, 11)):
            FuelStockSnapshot.objects.create(site_id="DKR_0001", snapshot_year=y, snapshot_month=m,
                                             has_genset=True, stock_snowflake_l=500 if y is None else 100)
        body = api().get("/api/fuel-tracking/stock/", {"has_genset": "true"}).json()
        self.assertEqual(body["kpis"]["sites_avec_ge"], 1)
        self.assertEqual(body["kpis"]["total_sites"], 1)


class BadgesIgnoreRunningSyncTests(TestCase):
    def test_enoc_badge_uses_last_finished_run(self):
        FuelEnocSyncRun.objects.create(status=FuelEnocSyncRun.Status.SUCCESS, rows_fetched=10)
        FuelEnocSyncRun.objects.create(status=FuelEnocSyncRun.Status.RUNNING)
        body = api().get("/api/fuel-tracking/stock/").json()
        self.assertTrue(body["sources"]["enoc"]["connected"])

    def test_stock_badge_uses_last_finished_run(self):
        FuelStockSyncRun.objects.create(status=FuelStockSyncRun.Status.SUCCESS)
        FuelStockSyncRun.objects.create(status=FuelStockSyncRun.Status.RUNNING)
        self.assertTrue(api().get("/api/fuel-tracking/stock/").json()["sources"]["snowflake"]["connected"])


class StockSyncEnocOutageTests(TestCase):
    def _sync(self, **kw):
        sf = {"DKR_0001": {"has_genset": False, "stock_snowflake_l": 300, "capacity_snowflake_l": 1000}}
        with mock.patch(f"{STOCK}.fetch_stock_snapshot", return_value=sf), \
             mock.patch(f"{STOCK}.fetch_genset_reference", **kw.get("genset", {"return_value": {}})), \
             mock.patch(f"{STOCK}.fetch_stock_readings", **kw.get("readings", {"return_value": {}})) as readings:
            call_command("sync_fuel_stock", *kw.get("args", ()), stdout=StringIO())
        return readings

    def test_mongo_outage_keeps_previous_enoc_values(self):
        FuelStockSnapshot.objects.create(site_id="DKR_0001", has_genset=True, has_genset_enoc=True, nb_ge_enoc=1,
                                         stock_enoc_l=420, stock_enoc_date=date(2026, 11, 10))
        self._sync(genset={"side_effect": OSError("mongo down")}, readings={"side_effect": OSError("mongo down")})
        row = FuelStockSnapshot.objects.get(snapshot_year__isnull=True)
        self.assertTrue(row.has_genset_enoc)
        self.assertTrue(row.has_genset)  # le site reste dans le périmètre carburant
        self.assertEqual(float(row.stock_enoc_l), 420)

    def test_month_snapshot_reads_enoc_as_of_month_end(self):
        readings = self._sync(args=("--month", "2026-09"))
        readings.assert_called_once_with(as_of=date(2026, 9, 30))


class ConsoSyncEnocAndPartialFailureTests(TestCase):
    def _run(self, **kw):
        with mock.patch(f"{CONSO}.fetch_genset_reference", **kw.get("genset", {"return_value": {}})), \
             mock.patch(f"{CONSO}.fetch_estimated_consumption", **kw.get("estim", {"return_value": {}})), \
             mock.patch(f"{CONSO}.call_command"), \
             mock.patch(f"{CONSO}.fetch_monthly_consumption", **kw["sf"]):
            call_command("sync_fuel_consommation", *kw.get("args", ("--month", "2026-10")), stdout=StringIO())

    def test_mongo_outage_keeps_previous_enoc_genset_and_estimate(self):
        FuelConsommationMonthly.objects.create(month_year="2026-10", year=2026, month=10, site_id="DKR_0001",
                                               has_genset_enoc=True, nb_ge_enoc=1, has_genset=True,
                                               conso_estimee_enoc_l=D("77"), conso_estimee_nb_releves=4)
        self._run(genset={"side_effect": OSError("down")}, estim={"side_effect": OSError("down")},
                  sf={"return_value": {"DKR_0001": {"has_genset": False, "conso_snowflake_l": 10}}})
        row = FuelConsommationMonthly.objects.get(site_id="DKR_0001")
        self.assertTrue(row.has_genset_enoc)
        self.assertTrue(row.has_genset)
        self.assertEqual(row.conso_estimee_enoc_l, D("77"))

    def test_one_month_failing_is_not_success(self):
        def sf(year, month):
            if month == 11:
                raise RuntimeError("timeout")
            return {"DKR_0001": {"conso_snowflake_l": 10}}
        self._run(sf={"side_effect": sf}, args=("--from-month", "2026-10", "--to-month", "2026-11"))
        run = FuelConsommationSyncRun.objects.get()
        self.assertEqual(run.status, FuelConsommationSyncRun.Status.FAILED)
        self.assertIn("2026-11", run.error_message)


class RuntimeAvailabilityOnRunningMonthTests(SimpleTestCase):
    def test_unpublished_days_do_not_count_against_availability(self):
        days = [OCT1 + timedelta(days=i) for i in range(30)]
        facts = {d: fact(dse_runtime_h=4) for d in days[:10]}
        ev = E.evaluate_runtime_sources(outdoor(), facts, days, data_until=days[9])
        self.assertEqual(ev[E.RT_DSE]["availability"], 1)
        self.assertIsNone(ev[E.RT_DSE]["rejection"])
        # Sans data_until (période close) : règle inchangée, 10/30 < 50 %.
        self.assertIsNotNone(E.evaluate_runtime_sources(outdoor(), facts, days)[E.RT_DSE]["rejection"])


class GridClassificationTests(SimpleTestCase):
    def test_non_is_off_grid_like_the_cph_engine(self):
        from fuel_tracking.services.site_energy_profile import _grid

        for raw in ("NON", "None", "no"):
            self.assertEqual(_grid(raw), "Off-grid", raw)
            self.assertTrue(E.is_off_grid(raw))
        self.assertEqual(_grid("ON GRID"), "Grid")
