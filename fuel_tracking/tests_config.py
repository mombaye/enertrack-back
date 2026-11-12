import io
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from fuel_tracking.models import FuelImportBatch

CODE = "135790"  # code de test, distinct du code réel
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "fuel-config-tests"}}


def scss_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "SEMAINE 44"
    ws.append(["Site ID", "Site Name", "Relevé en cm", "Relevé en litres", "Compteur horaire (h)", "Date de relevé", "Commentaires", "Site gardé par"])
    ws.append(["DKR_9999", "TEST", None, 120, None, "30/10/2026", None, "SCSS"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@override_settings(FUEL_CONFIG_CODE_HASH=make_password(CODE), CACHES=LOCMEM, FUEL_CONFIG_MAX_ATTEMPTS=3)
class ConfigAccessTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.user = get_user_model().objects.create_user(username="cfg", email="cfg@test.local", password="x-pass-123")
        self.c = APIClient()
        self.c.force_authenticate(self.user)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def token(self):
        r = self.c.post("/api/fuel-tracking/configuration/acces/", {"code": CODE}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()["token"]

    def test_requires_login(self):
        r = APIClient().post("/api/fuel-tracking/configuration/acces/", {"code": CODE}, format="json")
        self.assertIn(r.status_code, (401, 403))

    def test_bad_code_then_lock(self):
        for left in (2, 1, 0):
            r = self.c.post("/api/fuel-tracking/configuration/acces/", {"code": "000000"}, format="json")
            self.assertEqual(r.status_code, 403)
            self.assertEqual(r.json()["essais_restants"], left)
        r = self.c.post("/api/fuel-tracking/configuration/acces/", {"code": CODE}, format="json")
        self.assertEqual(r.status_code, 429)

    def test_non_numeric_code_is_refused(self):
        r = self.c.post("/api/fuel-tracking/configuration/acces/", {"code": "12a456"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_files_need_a_valid_token_bound_to_the_user(self):
        self.assertEqual(self.c.get("/api/fuel-tracking/configuration/fichiers/").status_code, 403)
        t = self.token()
        r = self.c.get("/api/fuel-tracking/configuration/fichiers/", HTTP_X_FUEL_CONFIG_TOKEN=t)
        self.assertEqual(r.status_code, 200)
        self.assertIn("CLOSING", r.json()["types"])
        other = get_user_model().objects.create_user(username="autre", email="autre@test.local", password="x-pass-456")
        c2 = APIClient()
        c2.force_authenticate(other)
        self.assertEqual(c2.get("/api/fuel-tracking/configuration/fichiers/", HTTP_X_FUEL_CONFIG_TOKEN=t).status_code, 403)
        self.assertEqual(self.c.get("/api/fuel-tracking/configuration/fichiers/", HTTP_X_FUEL_CONFIG_TOKEN=t + "x").status_code, 403)

    def _post(self, t, name="REMONTEES TEST.xlsx"):
        from fuel_tracking.views_config import run_pending_upload

        with mock.patch("fuel_tracking.tasks.process_config_upload.apply_async",
                        side_effect=lambda args, queue: run_pending_upload(args[0])) as m:
            r = self.c.post("/api/fuel-tracking/configuration/fichiers/", {"file": SimpleUploadedFile(name, scss_xlsx()), "source_type": "SCSS"},
                            HTTP_X_FUEL_CONFIG_TOKEN=t)
        self.assertEqual(r.status_code, 202, r.content)
        self.assertEqual(m.call_args.kwargs["queue"], "interactive")
        return r.json()["ticket"]

    def _state(self, t, ticket, client=None):
        return (client or self.c).get(f"/api/fuel-tracking/configuration/fichiers/?ticket={ticket}", HTTP_X_FUEL_CONFIG_TOKEN=t)

    def test_upload_without_mounted_folder_goes_to_persistent_media_volume(self):
        t = self.token()
        media = Path(self.tmp) / "media"
        with override_settings(FUEL_IMPORT_ROOT=str(Path(self.tmp) / "absent"), MEDIA_ROOT=str(media)):
            ticket = self._post(t)
            st = self._state(t, ticket).json()
            self.assertEqual(st["etat"], "TERMINE")
            b = FuelImportBatch.objects.get(pk=st["batch"]["id"])
            self.assertEqual(b.origin, "UPLOAD")
            self.assertEqual(b.received_by, self.user)
            self.assertTrue(b.archive_path.startswith(str(media / "fuel_imports")))
            self.assertTrue(Path(b.archive_path).is_file())
            self.assertFalse((Path(self.tmp) / "absent").exists())
            # la copie temporaire est supprimée, l'archive reste
            self.assertFalse((media / "fuel_imports" / "_config_pending" / ticket / "file.bin").exists())
            # même fichier une 2e fois : doublon technique, rien de réimporté
            st2 = self._state(t, self._post(t)).json()
            self.assertEqual(st2["batch"]["duplicate_of"], b.pk)

    def test_ticket_is_private_and_validated(self):
        t = self.token()
        with override_settings(FUEL_IMPORT_ROOT=str(Path(self.tmp) / "absent"), MEDIA_ROOT=str(Path(self.tmp) / "media")):
            ticket = self._post(t)
            self.assertEqual(self._state(t, "../../etc").status_code, 404)
            other = get_user_model().objects.create_user(username="autre2", email="autre2@test.local", password="x-pass-789")
            c2 = APIClient()
            c2.force_authenticate(other)
            t2 = c2.post("/api/fuel-tracking/configuration/acces/", {"code": CODE}, format="json").json()["token"]
            self.assertEqual(self._state(t2, ticket, c2).status_code, 404)

    def test_pending_without_result_becomes_failed_after_delay(self):
        t = self.token()
        with override_settings(FUEL_IMPORT_ROOT=str(Path(self.tmp) / "absent"), MEDIA_ROOT=str(Path(self.tmp) / "media")):
            with mock.patch("fuel_tracking.tasks.process_config_upload.apply_async"):
                r = self.c.post("/api/fuel-tracking/configuration/fichiers/", {"file": SimpleUploadedFile("a.xlsx", scss_xlsx()), "source_type": "SCSS"},
                                HTTP_X_FUEL_CONFIG_TOKEN=t)
            ticket = r.json()["ticket"]
            self.assertEqual(self._state(t, ticket).json()["etat"], "EN_COURS")
            with mock.patch("fuel_tracking.views_config.STALE_S", -1):
                self.assertEqual(self._state(t, ticket).json()["etat"], "ECHEC")

    def test_broker_down_returns_503_and_cleans_up(self):
        t = self.token()
        media = Path(self.tmp) / "media"
        with override_settings(FUEL_IMPORT_ROOT=str(Path(self.tmp) / "absent"), MEDIA_ROOT=str(media)):
            with mock.patch("fuel_tracking.tasks.process_config_upload.apply_async", side_effect=ConnectionError("broker")):
                r = self.c.post("/api/fuel-tracking/configuration/fichiers/", {"file": SimpleUploadedFile("a.xlsx", scss_xlsx()), "source_type": "SCSS"},
                                HTTP_X_FUEL_CONFIG_TOKEN=t)
            self.assertEqual(r.status_code, 503)
            self.assertEqual(list((media / "fuel_imports" / "_config_pending").iterdir()), [])

    def test_upload_rejects_wrong_type_and_extension(self):
        t = self.token()
        r = self.c.post("/api/fuel-tracking/configuration/fichiers/", {"file": SimpleUploadedFile("a.xlsx", b"x"), "source_type": "ENOC"}, HTTP_X_FUEL_CONFIG_TOKEN=t)
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/fuel-tracking/configuration/fichiers/", {"file": SimpleUploadedFile("a.csv", b"x"), "source_type": "SCSS"}, HTTP_X_FUEL_CONFIG_TOKEN=t)
        self.assertEqual(r.status_code, 400)
