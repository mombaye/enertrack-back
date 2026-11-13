"""Recalcul des bilans de stock juste après un import accepté (au lieu d'attendre la tâche de nuit)."""
from datetime import date
from unittest import mock

from django.test import TestCase, override_settings

from fuel_tracking.models import FuelImportBatch
from fuel_tracking.services.auto_balances import months_for_period
from fuel_tracking.services.stock_import_pipeline import schedule_balance_recompute

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "balances-after-import"}}
TODAY = date(2026, 11, 13)


class MonthsForPeriodTests(TestCase):
    def test_closing_septembre(self):
        self.assertEqual(months_for_period(date(2026, 9, 1), date(2026, 9, 30), TODAY), [(date(2026, 9, 1), date(2026, 9, 30))])

    def test_borne_au_premier_du_mois_suivant(self):
        self.assertEqual([m for m, _ in months_for_period(date(2026, 9, 1), date(2026, 10, 1), TODAY)], [date(2026, 10, 1), date(2026, 9, 1)])

    def test_mois_en_cours_exclu(self):
        self.assertEqual(months_for_period(date(2026, 11, 2), date(2026, 11, 10), TODAY), [])
        self.assertEqual([m for m, _ in months_for_period(date(2026, 10, 20), date(2026, 11, 10), TODAY)], [date(2026, 10, 1)])

    def test_au_plus_trois_mois_les_plus_recents(self):
        self.assertEqual([m for m, _ in months_for_period(date(2026, 1, 1), date(2026, 10, 31), TODAY)],
                         [date(2026, 10, 1), date(2026, 9, 1), date(2026, 8, 1)])

    def test_sans_periode_deux_derniers_mois(self):
        self.assertEqual([m for m, _ in months_for_period(None, None, TODAY)], [date(2026, 10, 1), date(2026, 9, 1)])


@override_settings(CACHES=LOCMEM)
class ScheduleTests(TestCase):
    def _batch(self, source_type="CLOSING", status="ACCEPTED", h="a"):
        return FuelImportBatch.objects.create(source_type=source_type, file_name="Closing septembre.xlsb", file_hash=h * 64, status=status,
                                              declared_period_start=date(2026, 9, 1), declared_period_end=date(2026, 9, 30))

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        p = mock.patch("fuel_tracking.services.auto_balances.timezone.localdate", return_value=TODAY)
        p.start()
        self.addCleanup(p.stop)

    def test_import_accepte_lance_le_recalcul_du_mois(self):
        b = self._batch()
        with mock.patch("fuel_tracking.tasks.recompute_balances_after_import.apply_async") as send:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertTrue(schedule_balance_recompute(b))
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["args"], [["2026-09"], b.pk])

    def test_un_seul_recalcul_en_attente_par_mois(self):
        with mock.patch("fuel_tracking.tasks.recompute_balances_after_import.apply_async") as send:
            with self.captureOnCommitCallbacks(execute=True):
                schedule_balance_recompute(self._batch(h="a"))
                schedule_balance_recompute(self._batch(h="b"))
        self.assertEqual(send.call_count, 1)

    def test_a_valider_lance_aussi(self):
        with mock.patch("fuel_tracking.tasks.recompute_balances_after_import.apply_async") as send:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertTrue(schedule_balance_recompute(self._batch(status="VALIDATION_REQUIRED")))
        send.assert_called_once()

    def test_rejete_ou_target_ne_lance_rien(self):
        with mock.patch("fuel_tracking.tasks.recompute_balances_after_import.apply_async") as send:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertFalse(schedule_balance_recompute(self._batch(status="REJECTED", h="c")))
                self.assertFalse(schedule_balance_recompute(self._batch(source_type="TARGET_FUEL", h="d")))
        send.assert_not_called()

    def test_file_indisponible_ne_casse_pas_l_import(self):
        with mock.patch("fuel_tracking.tasks.recompute_balances_after_import.apply_async", side_effect=ConnectionError("broker")):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertTrue(schedule_balance_recompute(self._batch()))


@override_settings(CACHES=LOCMEM)
class TaskTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def test_tache_recalcule_les_mois_demandes(self):
        from fuel_tracking.tasks import recompute_balances_after_import

        with mock.patch("fuel_tracking.services.auto_balances.compute_months", return_value=[{"debut": "2026-09-01"}]) as cm:
            res = recompute_balances_after_import.run(["2026-09"], 7)
        cm.assert_called_once_with([(date(2026, 9, 1), date(2026, 9, 30))])
        self.assertEqual(res, [{"debut": "2026-09-01"}])

    def test_tache_de_nuit_en_cours_reessai(self):
        from django.core.cache import cache
        from fuel_tracking.tasks import recompute_balances_after_import

        cache.set("fuel_tracking:lock:auto_compute_stock_balances", "x", 60)
        with mock.patch.object(recompute_balances_after_import, "retry", side_effect=RuntimeError("retry")) as r, \
             mock.patch("fuel_tracking.services.auto_balances.compute_months") as cm:
            with self.assertRaises(RuntimeError):
                recompute_balances_after_import.run(["2026-09"], 7)
        r.assert_called_once()
        cm.assert_not_called()
