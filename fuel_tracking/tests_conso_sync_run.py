"""Synchro conso Snowflake : un run qui s'arrête en cours de route est enregistré en échec avec sa cause.

Constaté en prod le 2026-11-11 : chaque exécution planifiée était tuée par sa limite de temps (15 min) et restait
RUNNING, puis « interrompue » une heure plus tard. Aucun run SUCCESS, donc voyant Snowflake rouge sans cause visible.
"""
from io import StringIO
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from fuel_tracking.models import FuelConsommationSyncRun

CMD = "fuel_tracking.management.commands.sync_fuel_consommation"


class ConsoSyncRunStatusTests(TestCase):
    def _run(self, **patches):
        with mock.patch(f"{CMD}.fetch_genset_reference", return_value={}), \
             mock.patch(f"{CMD}.fetch_estimated_consumption", return_value={}), \
             mock.patch(f"{CMD}.call_command"), \
             mock.patch(f"{CMD}.fetch_monthly_consumption", **patches):
            call_command("sync_fuel_consommation", month="2026-11", stdout=StringIO())

    def test_time_limit_marks_run_failed_with_reason(self):
        with self.assertRaises(SoftTimeLimitExceeded):
            self._run(side_effect=SoftTimeLimitExceeded())
        run = FuelConsommationSyncRun.objects.get()
        self.assertEqual(run.status, FuelConsommationSyncRun.Status.FAILED)
        self.assertIsNotNone(run.finished_at)
        self.assertIn("Délai dépassé", run.error_message)

    def test_time_limit_is_not_swallowed_by_enoc_fallback(self):
        with mock.patch(f"{CMD}.fetch_genset_reference", side_effect=SoftTimeLimitExceeded()), \
             self.assertRaises(SoftTimeLimitExceeded):
            call_command("sync_fuel_consommation", month="2026-11", stdout=StringIO())
        self.assertEqual(FuelConsommationSyncRun.objects.get().status, FuelConsommationSyncRun.Status.FAILED)

    def test_unexpected_error_marks_run_failed(self):
        with mock.patch(f"{CMD}.FuelEnocMovement.objects") as enoc:
            enoc.filter.side_effect = RuntimeError("boom")
            with self.assertRaises(RuntimeError):
                self._run(return_value={"S1": {"conso_snowflake_l": 10}})
        run = FuelConsommationSyncRun.objects.get()
        self.assertEqual(run.status, FuelConsommationSyncRun.Status.FAILED)
        self.assertIn("boom", run.error_message)

    def test_snowflake_error_still_recorded_as_before(self):
        self._run(side_effect=RuntimeError("sf down"))
        run = FuelConsommationSyncRun.objects.get()
        self.assertEqual(run.status, FuelConsommationSyncRun.Status.FAILED)
        self.assertIn("sf down", run.error_message)

    def test_success_unchanged(self):
        self._run(return_value={"S1": {"conso_snowflake_l": 10, "nb_jours_data": 3}})
        self.assertEqual(FuelConsommationSyncRun.objects.get().status, FuelConsommationSyncRun.Status.SUCCESS)


class ConsoSyncTaskLimitsTests(SimpleTestCase):
    def test_limits_leave_room_for_two_months_and_lock_outlives_hard_limit(self):
        from fuel_tracking.tasks import sync_fuel_consommation_current_month as t

        self.assertGreaterEqual(t.soft_time_limit, 30 * 60)
        self.assertGreater(t.time_limit, t.soft_time_limit)
