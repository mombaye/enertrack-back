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
