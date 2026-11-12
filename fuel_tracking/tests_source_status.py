from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from fuel_tracking.models import (FuelConsommationSyncRun, FuelDailyFactsSyncRun, FuelEnocSyncRun, FuelImportBatch,
                                  FuelStockSyncRun)
from fuel_tracking.services import source_status as S


def by_key(st):
    return {r["source"]: r for r in st["sources"]}


class SourceStatusTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    def test_empty_platform_reports_every_source_as_never_run(self):
        st = S.sources_status(self.now)
        self.assertEqual({r["etat"] for r in st["sources"]}, {S.JAMAIS})
        self.assertEqual(len(st["a_traiter"]), 8)

    def test_states_from_sync_journals(self):
        FuelConsommationSyncRun.objects.create(status="SUCCESS", started_at=self.now - timedelta(minutes=6), finished_at=self.now - timedelta(minutes=5))
        FuelStockSyncRun.objects.create(status="SUCCESS", started_at=self.now - timedelta(hours=5), finished_at=self.now - timedelta(hours=5))
        FuelEnocSyncRun.objects.create(status="SUCCESS", started_at=self.now - timedelta(minutes=20), finished_at=self.now - timedelta(minutes=19))
        FuelEnocSyncRun.objects.create(status="FAILED", started_at=self.now - timedelta(minutes=4), finished_at=self.now - timedelta(minutes=3),
                                       error_message="ConnectionError: hôte injoignable\nTraceback…")
        FuelDailyFactsSyncRun.objects.create(date_from=self.now.date(), date_to=self.now.date(), status="SUCCESS",
                                             started_at=self.now - timedelta(minutes=30), finished_at=self.now - timedelta(minutes=20),
                                             error_message="AVERTISSEMENT : Compteur horaire GFMS_DATA_TRACKER_NC illisible")
        k = by_key(S.sources_status(self.now))
        self.assertEqual(k["snowflake_conso"]["etat"], S.OK)
        self.assertEqual(k["snowflake_stock"]["etat"], S.RETARD)
        self.assertEqual(k["enoc"]["etat"], S.ECHEC)
        self.assertEqual(k["enoc"]["erreur"], "ConnectionError: hôte injoignable Traceback…")
        self.assertIsNotNone(k["enoc"]["dernier_succes"])
        self.assertEqual(k["snowflake_faits"]["etat"], S.PARTIEL)

    def test_a_run_stuck_in_progress_is_reported(self):
        FuelStockSyncRun.objects.create(status="RUNNING", started_at=self.now - timedelta(hours=3))
        self.assertEqual(by_key(S.sources_status(self.now))["snowflake_stock"]["etat"], S.BLOQUE)

    @override_settings(FUEL_SOURCE_MAX_AGE_SCSS_DAYS=8)
    def test_files_ok_late_and_rejected(self):
        b = FuelImportBatch.objects.create(source_type="SCSS", file_name="REMONTEES S44.xlsx", file_hash="a" * 64, status="ACCEPTED")
        FuelImportBatch.objects.filter(pk=b.pk).update(received_at=self.now - timedelta(days=2))
        b = FuelImportBatch.objects.create(source_type="CLOSING", file_name="Closing aout.xlsb", file_hash="b" * 64, status="ACCEPTED")
        FuelImportBatch.objects.filter(pk=b.pk).update(received_at=self.now - timedelta(days=70))
        b = FuelImportBatch.objects.create(source_type="LOLIFORCE", file_name="Loliforce.xlsx", file_hash="c" * 64, status="REJECTED",
                                           status_message="Onglet attendu introuvable")
        k = by_key(S.sources_status(self.now))
        self.assertEqual(k["scss"]["etat"], S.OK)
        self.assertEqual(k["closing"]["etat"], S.RETARD)
        self.assertEqual(k["loliforce"]["etat"], S.ECHEC)
        self.assertEqual(k["loliforce"]["erreur"], "Onglet attendu introuvable")

    def test_global_state_is_the_worst_one(self):
        FuelEnocSyncRun.objects.create(status="FAILED", finished_at=self.now, error_message="x")
        self.assertEqual(S.sources_status(self.now)["etat_global"], S.ECHEC)


class FuelReportDatabaseSettingTests(TestCase):
    @override_settings(FUEL_REPORT_DATABASE="DB_GFMS_ANALYTICS_PROD")
    def test_database_comes_from_settings(self):
        from fuel_tracking.services.snowflake_names import fuel_report_database
        self.assertEqual(fuel_report_database(), "DB_GFMS_ANALYTICS_PROD")

    @override_settings(FUEL_REPORT_DATABASE="X; DROP TABLE Y")
    def test_only_a_plain_identifier_is_accepted(self):
        from fuel_tracking.services.snowflake_names import fuel_report_database
        with self.assertRaises(ValueError):
            fuel_report_database()
