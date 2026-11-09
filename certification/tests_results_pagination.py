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


class LaunchScopePilotageGridTests(TestCase):
    """Contrats retrouvés via la fiche site de la plateforme et via les sites Pilotage Grid."""

    def test_resolution_sources(self):
        from datetime import date
        from unittest import mock

        from certification.tasks import launch_certification_batch
        from core.models import Site
        from pilotage_grid.models import PgSite

        by_contract, by_pg, pg_only = Site.objects.bulk_create([
            Site(site_id="DKR_1001", name="Fiche", contract_number="22000000401"),
            Site(site_id="KLK_1002", name="Pilotage"),
            Site(site_id="ZIG_1003", name="Autre"),
        ])
        PgSite.objects.create(site_key="KLK_1002", contrat=22000000502, region="KAOLACK", tarif="DGP", tension="BT", porteur="AKTIVCO")
        # Site Pilotage Grid sans site correspondant sur la plateforme : on ne l'invente pas.
        PgSite.objects.create(site_key="SANS_ID 1", contrat=22000000603, region="DAKAR", tarif="DGP", tension="BT", porteur="AKTIVCO")

        batch = ImportBatch.objects.create(source_filename="octobre.xlsx")
        period = dict(date_debut_periode=date(2026, 9, 1), date_fin_periode=date(2026, 9, 30))
        for n, c in [("F4", "22000000401"), ("F5", "22000000502.0"), ("F6", "22000000603")]:
            SonatelInvoice.objects.create(batch=batch, numero_compte_contrat=c, numero_facture=n, **period)
        cert = CertificationBatch.objects.create(import_batch=batch)

        with mock.patch("certification.tasks.EfmsService"), mock.patch("certification.tasks.chord"), \
             mock.patch("enertrack_backend.task_dispatch.consumed_queues", return_value={"celery"}):
            launch_certification_batch(cert.id)

        r = {x.invoice.numero_facture: x for x in CertificationResult.objects.filter(cert_batch=cert)}
        self.assertEqual(r["F4"].site_id, by_contract.id)
        self.assertEqual(r["F5"].site_id, by_pg.id)
        self.assertEqual(r["F5"].status, CertificationResult.Status.PENDING_CERTIFICATION)
        self.assertIsNone(r["F6"].site_id)
        self.assertEqual(r["F6"].status, CertificationResult.Status.UNKNOWN_CONTRACT)


class CampaignDeleteAndSummaryTests(TestCase):
    def setUp(self):
        from decimal import Decimal
        from core.models import Site

        self.client = APIClient()
        self.client.force_authenticate(get_user_model().objects.create_user(username="s", password="x"))
        batch = ImportBatch.objects.create(source_filename="octobre.xlsx")
        self.batch = batch
        self.cert = CertificationBatch.objects.create(import_batch=batch, status=CertificationBatch.Status.DONE,
                                                      echeance_year=2026, echeance_month=11)
        site = Site.objects.create(site_id="DKR_9001", name="S1")
        S = CertificationResult.Status
        for n, st, ttc, s in [("F1", S.CERTIFIED_FMS, "1000", site), ("F2", S.NEEDS_REVIEW, "5000", site),
                              ("F3", S.MESURE_A_VERIFIER, None, site), ("F4", S.UNKNOWN_CONTRACT, "200", None)]:
            inv = SonatelInvoice.objects.create(batch=batch, numero_compte_contrat=f"C{n}", numero_facture=n,
                                                montant_ttc=Decimal(ttc) if ttc else None, site=s)
            CertificationResult.objects.create(cert_batch=self.cert, invoice=inv, site=s, status=st)

    def test_summary(self):
        d = self.client.get(f"/api/certification/batches/{self.cert.id}/summary/").json()
        self.assertEqual(d["total"], 4)
        self.assertEqual(d["groups"]["certified"]["count"], 1)
        self.assertEqual(d["groups"]["certified"]["pct"], 25.0)
        self.assertEqual(d["groups"]["to_review"]["count"], 2)
        self.assertEqual(d["groups"]["to_review"]["montant_ttc"], 5000.0)
        self.assertEqual(d["montant_ttc_total"], 6200.0)
        self.assertEqual(d["factures_sans_montant"], 1)  # absent ≠ 0
        self.assertEqual(d["coverage"]["sites_rattaches"], 1)
        self.assertEqual(d["unknown_contracts_sample"], ["CF4"])
        self.assertEqual([r["numero_facture"] for r in d["top_to_review"]][0], "F2")

    def test_delete_keeps_imported_invoices(self):
        resp = self.client.delete(f"/api/certification/batches/{self.cert.id}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(CertificationBatch.objects.filter(pk=self.cert.id).exists())
        self.assertFalse(CertificationResult.objects.filter(cert_batch_id=self.cert.id).exists())
        self.assertEqual(SonatelInvoice.objects.filter(batch=self.batch).count(), 4)

    def test_delete_refused_while_running_unless_stuck(self):
        from datetime import timedelta
        from django.utils import timezone

        CertificationBatch.objects.filter(pk=self.cert.id).update(status=CertificationBatch.Status.RUNNING)
        self.assertEqual(self.client.delete(f"/api/certification/batches/{self.cert.id}/").status_code, 409)
        CertificationBatch.objects.filter(pk=self.cert.id).update(launched_at=timezone.now() - timedelta(hours=3))
        self.assertEqual(self.client.delete(f"/api/certification/batches/{self.cert.id}/").status_code, 204)
