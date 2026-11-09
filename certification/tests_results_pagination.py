"""Résultats de certification : pagination serveur complète (avant, l'écran n'affichait que 20 factures)."""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from billing.models import ImportBatch, SonatelInvoice
from certification.models import CertificationBatch, CertificationResult

URL = "/api/certification/results/"


class ResultsPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        batch = ImportBatch.objects.create(source_filename="octobre.xlsx")
        cls.cert = CertificationBatch.objects.create(import_batch=batch)
        invoices = SonatelInvoice.objects.bulk_create([
            SonatelInvoice(batch=batch, numero_compte_contrat=f"C{i:04d}", numero_facture=f"F{i:04d}")
            for i in range(55)
        ])
        CertificationResult.objects.bulk_create([
            CertificationResult(cert_batch=cls.cert, invoice=inv, status=CertificationResult.Status.UNKNOWN_CONTRACT)
            for inv in invoices
        ])

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user(username="u", password="x"))

    def test_default_page_size_unchanged(self):
        data = self.client.get(URL, {"cert_batch": self.cert.id}).json()
        self.assertEqual(data["count"], 55)
        self.assertEqual(len(data["results"]), 20)

    def test_page_size_param_and_every_invoice_reachable_once(self):
        seen = []
        for page in (1, 2):
            data = self.client.get(URL, {"cert_batch": self.cert.id, "page": page, "page_size": 50}).json()
            seen += [r["id"] for r in data["results"]]
        self.assertEqual(len(seen), 55)
        self.assertEqual(len(set(seen)), 55)

    def test_page_size_capped(self):
        data = self.client.get(URL, {"cert_batch": self.cert.id, "page_size": 10_000}).json()
        self.assertEqual(len(data["results"]), 55)  # sous le plafond de 500


class LaunchScopeTests(TestCase):
    """Tous les sites du fichier liés à un contrat sont certifiés, pas seulement Aktivco/grid_fee."""

    def test_non_aktivco_linked_site_is_certified_unknown_contract_stays_unknown(self):
        from datetime import date
        from unittest import mock

        from billing.models import ContractSiteLink
        from certification.tasks import launch_certification_batch
        from core.models import Site

        aktivco, other = Site.objects.bulk_create([
            Site(site_id="DKR_0001", name="A", invoice_payment="Aktivco", grid_fee=True),
            Site(site_id="THS_0002", name="B", invoice_payment="Autre", grid_fee=False),
        ])
        ContractSiteLink.objects.create(numero_compte_contrat="C1", site=aktivco)
        ContractSiteLink.objects.create(numero_compte_contrat="C2", site=other)
        batch = ImportBatch.objects.create(source_filename="octobre.xlsx")
        period = dict(date_debut_periode=date(2026, 9, 1), date_fin_periode=date(2026, 9, 30))
        SonatelInvoice.objects.create(batch=batch, numero_compte_contrat="C1", numero_facture="F1", site=aktivco, **period)
        SonatelInvoice.objects.create(batch=batch, numero_compte_contrat="C2", numero_facture="F2", site=None, **period)
        SonatelInvoice.objects.create(batch=batch, numero_compte_contrat="C3", numero_facture="F3", site=None, **period)
        cert = CertificationBatch.objects.create(import_batch=batch)

        with mock.patch("certification.tasks.EfmsService"), \
             mock.patch("certification.tasks.chord") as chord, \
             mock.patch("enertrack_backend.task_dispatch.consumed_queues", return_value={"celery"}):
            launch_certification_batch(cert.id)

        chord.assert_called_once()
        by_invoice = {r.invoice.numero_facture: r for r in CertificationResult.objects.filter(cert_batch=cert)}
        self.assertEqual(by_invoice["F1"].site_id, aktivco.id)
        self.assertEqual(by_invoice["F1"].status, CertificationResult.Status.PENDING_CERTIFICATION)
        self.assertEqual(by_invoice["F2"].site_id, other.id)
        self.assertEqual(by_invoice["F2"].status, CertificationResult.Status.PENDING_CERTIFICATION)
        self.assertIsNone(by_invoice["F3"].site_id)
        self.assertEqual(by_invoice["F3"].status, CertificationResult.Status.UNKNOWN_CONTRACT)
        # La facture reste hors périmètre facturation : seul le résultat de certification est rattaché.
        self.assertIsNone(SonatelInvoice.objects.get(numero_facture="F2").site_id)
