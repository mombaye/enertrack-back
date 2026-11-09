"""Calcul d'une facture : pas de requête Snowflake de repli quand le préchargement a déjà répondu pour sa période."""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from billing.models import ImportBatch, SonatelInvoice
from certification.models import CertificationBatch, CertificationResult
from certification.tasks import certify_single_invoice
from core.models import Site

EMPTY_GRID = {"periode": None, "30j": None, "last_month_date": None}
EMPTY_ACM = {"periode": None, "30j": None}


class FakeEfms:
    """Préchargement simulé ; chaque requête de repli est comptée."""

    entries = (None, None)

    def __init__(self, cert_batch=None):
        self.calls = []
        FakeEfms.last = self

    def get_prefetched_entries(self, *a):
        return FakeEfms.entries

    def get_conso_periode(self, *a):
        self.calls.append("grid_periode")
        return None, "none"

    def get_conso_last_complete_month(self, *a):
        self.calls.append("grid_last_month")
        return None, None

    def get_conso_acm(self, *a):
        self.calls.append("acm")
        return None, None


class PrefetchFallbackTests(TestCase):
    def setUp(self):
        site = Site.objects.create(site_id="DKR_0101", name="S")
        batch = ImportBatch.objects.create(source_filename="octobre.xlsx")
        inv = SonatelInvoice.objects.create(
            batch=batch, numero_compte_contrat="C1", numero_facture="F1", site=site,
            date_debut_periode=date(2026, 9, 1), date_fin_periode=date(2026, 9, 30), conso_facturee=Decimal("900"),
        )
        cert = CertificationBatch.objects.create(import_batch=batch, status=CertificationBatch.Status.RUNNING)
        self.result = CertificationResult.objects.create(
            cert_batch=cert, invoice=inv, site=site, status=CertificationResult.Status.PENDING_CERTIFICATION)

    def _run(self, entries):
        FakeEfms.entries = entries
        with mock.patch("certification.tasks.EfmsService", FakeEfms):
            certify_single_invoice(self.result.id)
        self.result.refresh_from_db()
        return FakeEfms.last.calls

    def test_prefetched_but_empty_does_not_requery(self):
        calls = self._run((EMPTY_GRID, EMPTY_ACM))
        self.assertEqual(calls, [])
        self.assertFalse(self.result.fms_available)
        self.assertNotEqual(self.result.status, CertificationResult.Status.PENDING_CERTIFICATION)

    def test_missing_from_cache_still_falls_back(self):
        calls = self._run((None, None))
        self.assertEqual(calls, ["grid_periode", "grid_last_month", "acm"])

    def test_prefetched_values_are_used(self):
        calls = self._run(({"periode": Decimal("880"), "30j": Decimal("880"), "last_month_date": None},
                           {"periode": Decimal("870"), "30j": Decimal("870")}))
        self.assertEqual(calls, [])
        self.assertEqual(self.result.conso_fms_periode, Decimal("880"))
        self.assertEqual(self.result.estim_conso_acm_periode, Decimal("870"))


class ResumeCampaignTests(TestCase):
    """Reprise d'une campagne arrêtée : rien n'est supprimé, seules les factures en attente sont relancées."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user(username="r", password="x"))
        site = Site.objects.create(site_id="DKR_0202", name="S")
        batch = ImportBatch.objects.create(source_filename="octobre.xlsx")
        self.cert = CertificationBatch.objects.create(import_batch=batch, status=CertificationBatch.Status.FAILED)
        S = CertificationResult.Status
        self.ids = {}
        for n, st, s in [("F1", S.CERTIFIED_FMS, site), ("F2", S.PENDING_CERTIFICATION, site),
                         ("F3", S.PENDING_CERTIFICATION, site), ("F4", S.UNKNOWN_CONTRACT, None)]:
            inv = SonatelInvoice.objects.create(batch=batch, numero_compte_contrat=f"C{n}", numero_facture=n, site=s,
                                                date_debut_periode=date(2026, 9, 1), date_fin_periode=date(2026, 9, 30))
            self.ids[n] = CertificationResult.objects.create(cert_batch=self.cert, invoice=inv, site=s, status=st).id

    def _resume(self):
        with mock.patch("enertrack_backend.task_dispatch.consumed_queues", return_value={"celery", "interactive"}), \
             mock.patch("certification.tasks.resume_certification_batch.apply_async") as aa:
            aa.return_value = mock.Mock(id="t1")
            return self.client.post(f"/api/certification/batches/{self.cert.id}/resume/"), aa

    def test_endpoint_queues_resume_and_keeps_everything(self):
        resp, aa = self._resume()
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(resp.json()["pending"], 2)
        aa.assert_called_once()
        self.assertEqual(CertificationResult.objects.filter(cert_batch=self.cert).count(), 4)

    def test_refused_while_really_running(self):
        CertificationBatch.objects.filter(pk=self.cert.id).update(status=CertificationBatch.Status.RUNNING)
        resp, aa = self._resume()
        self.assertEqual(resp.status_code, 409)
        aa.assert_not_called()

    def test_task_dispatches_only_pending(self):
        from certification.tasks import resume_certification_batch

        with mock.patch("certification.tasks._prefetch_and_dispatch") as disp:
            out = resume_certification_batch(self.cert.id)
        self.assertEqual(out["resumed"], 2)
        self.assertEqual(sorted(disp.call_args[0][1]), sorted([self.ids["F2"], self.ids["F3"]]))
        self.cert.refresh_from_db()
        self.assertEqual(self.cert.status, CertificationBatch.Status.RUNNING)
        self.assertEqual(self.cert.certified_fms, 1)
        self.assertEqual(self.cert.unknown_contract, 1)
        self.assertEqual(CertificationResult.objects.filter(cert_batch=self.cert).count(), 4)

    def test_nothing_pending_marks_done(self):
        from certification.tasks import resume_certification_batch

        CertificationResult.objects.filter(cert_batch=self.cert, status=CertificationResult.Status.PENDING_CERTIFICATION) \
            .update(status=CertificationResult.Status.FMS_UNAVAILABLE)
        with mock.patch("certification.tasks._prefetch_and_dispatch") as disp:
            resume_certification_batch(self.cert.id)
        disp.assert_not_called()
        self.cert.refresh_from_db()
        self.assertEqual(self.cert.status, CertificationBatch.Status.DONE)
        self.assertEqual(self.cert.fms_unavailable, 2)
