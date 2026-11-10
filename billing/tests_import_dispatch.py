"""Import facture : la tâche part sur une file écoutée, sinon échec explicite (jamais « En file d'attente… » sans fin)."""
import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from billing.models import ImportBatch
from enertrack_backend import task_dispatch
from enertrack_backend.celery import app as celery_app
from enertrack_backend.task_dispatch import NoWorkerAvailable, pick_queue

URL = "/api/sonatel-billing/batches/import/"


class PickQueueTests(SimpleTestCase):
    def test_preferred_queue_when_consumed(self):
        self.assertEqual(pick_queue("interactive", queues={"celery", "interactive"}), "interactive")

    def test_falls_back_when_preferred_has_no_worker(self):
        self.assertEqual(pick_queue("interactive", queues={"celery"}), "celery")

    def test_raises_when_no_worker_at_all(self):
        with self.assertRaises(NoWorkerAvailable):
            pick_queue("interactive", queues=set())

    def test_unknown_state_keeps_default_routing(self):
        with mock.patch.object(task_dispatch, "consumed_queues", return_value=None):
            self.assertIsNone(pick_queue("interactive"))

    def test_consumed_queues_reads_worker_replies(self):
        replies = {"interactive@h": [{"name": "interactive"}], "celery@h": [{"name": "celery"}, {"name": "interactive"}]}
        with mock.patch.object(celery_app.control, "inspect") as insp:
            insp.return_value.active_queues.return_value = replies
            self.assertEqual(task_dispatch.consumed_queues(), {"celery", "interactive"})

    def test_certification_subtasks_not_routed_to_interactive(self):
        router = celery_app.amqp.router
        for name, expected in [
            ("certification.certify_single_invoice", "celery"),
            ("certification.finalize_certification_batch", "celery"),
            ("certification.launch_certification_batch", "interactive"),
            ("billing.import_invoices", "interactive"),
        ]:
            route = router.route({}, name)
            self.assertEqual(route["queue"].name, expected, name)


class ImportDispatchViewTests(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        self.addCleanup(self.override.disable)
        user = get_user_model().objects.create_user(username="importer", password="x")
        self.client = APIClient()
        self.client.force_authenticate(user)

    def _post(self):
        f = SimpleUploadedFile("octobre.xlsx", b"fake", content_type="application/octet-stream")
        return self.client.post(URL, {"file": f, "echeance": "2026-11-15"}, format="multipart")

    def test_no_worker_marks_batch_failed_with_clear_message(self):
        with mock.patch.object(task_dispatch, "consumed_queues", return_value=set()), \
             mock.patch("billing.tasks.import_invoices_task.apply_async") as send:
            resp = self._post()
        self.assertEqual(resp.status_code, 503)
        send.assert_not_called()
        batch = ImportBatch.objects.get(pk=resp.data["batch_id"])
        self.assertEqual(batch.task_status, ImportBatch.TaskStatus.FAILURE)
        self.assertIn("arrêté", batch.task_message)

    def test_falls_back_to_default_queue_when_interactive_worker_missing(self):
        with mock.patch.object(task_dispatch, "consumed_queues", return_value={"celery"}), \
             mock.patch("billing.tasks.import_invoices_task.apply_async") as send:
            send.return_value.id = "task-1"
            resp = self._post()
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(send.call_args.kwargs["queue"], "celery")
        batch = ImportBatch.objects.get(pk=resp.data["batch"]["id"])
        self.assertEqual(batch.task_id, "task-1")
        self.assertEqual(batch.task_status, ImportBatch.TaskStatus.PENDING)


class BeatScheduleExpiryTests(SimpleTestCase):
    """Une tâche planifiée qui n'a pas pu démarrer à temps expire au lieu de s'empiler dans la file."""

    def test_every_periodic_task_expires_before_its_next_run(self):
        from django.conf import settings

        self.assertTrue(settings.CELERY_BEAT_SCHEDULE)
        for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
            expires = entry.get("options", {}).get("expires")
            self.assertIsNotNone(expires, name)
            self.assertGreater(expires, 0, name)
            if name.endswith("-5min"):
                self.assertLess(expires, 5 * 60, name)


class ScheduledTasksAreBoundedTests(SimpleTestCase):
    """Chaque tâche planifiée a une limite de temps : une exécution bloquée ne peut plus garder un processus des heures
    (2026-11-09 : 4 synchros financières bloquées occupaient tout le worker, certification figée à 1 %)."""

    def test_every_scheduled_task_has_a_hard_time_limit(self):
        from django.conf import settings

        from enertrack_backend.celery import app

        app.loader.import_default_modules()
        for name, entry in settings.CELERY_BEAT_SCHEDULE.items():
            task = app.tasks[entry["task"]]
            self.assertTrue(task.time_limit, f"{name} ({entry['task']}) sans time_limit")

    def test_financial_sync_does_not_overlap(self):
        from unittest import mock

        from financial.tasks import sync_financial_conso_auto

        with mock.patch("django.core.cache.cache.add", return_value=False), \
             mock.patch("django.core.management.call_command") as cc:
            sync_financial_conso_auto()
        cc.assert_not_called()
