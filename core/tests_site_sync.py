from datetime import date

from django.test import TestCase

from billing.models import ContractSiteLink, ImportBatch, SonatelInvoice
from core.models import Site


class SiteSyncTests(TestCase):
    """Mise à jour d'un site → scope recalculé, contrat lié, factures déjà importées rattachées."""

    def setUp(self):
        self.batch = ImportBatch.objects.create(source_filename="factures.xlsx")
        self.invoice = SonatelInvoice.objects.create(
            batch=self.batch, numero_compte_contrat="22005301879", numero_facture="F1",
            date_debut_periode=date(2026, 8, 1), date_fin_periode=date(2026, 8, 31))

    def test_contract_links_invoice_and_scope_follows_data(self):
        site = Site.objects.create(site_id="DKR_2834", name="RUF44", scope_status="OUT_OF_SCOPE")
        self.assertIsNone(SonatelInvoice.objects.get(pk=self.invoice.pk).site)
        site.contract_number = "22005301879.0"
        site.invoice_payment = "Aktivco"
        site.grid_fee = True
        site.save()
        self.assertEqual(Site.objects.get(pk=site.pk).scope_status, "IN_SCOPE")
        self.assertEqual(ContractSiteLink.objects.get(numero_compte_contrat="22005301879").site_id, site.pk)
        self.assertEqual(SonatelInvoice.objects.get(pk=self.invoice.pk).site_id, site.pk)
        site.grid_fee = False
        site.save()
        self.assertEqual(Site.objects.get(pk=site.pk).scope_status, "OUT_OF_SCOPE")

    def test_reference_reassigns_contract_and_unknown_keeps_scope(self):
        other = Site.objects.create(site_id="DKR_0001", name="A")
        ContractSiteLink.objects.create(numero_compte_contrat="22005301879", site=other)
        site = Site.objects.create(site_id="DKR_2834", name="RUF44", scope_status="IN_SCOPE",
                                   contract_number="22005301879 / 22000000001")
        self.assertEqual(ContractSiteLink.objects.get(numero_compte_contrat="22005301879").site_id, site.pk)
        self.assertTrue(ContractSiteLink.objects.filter(numero_compte_contrat="22000000001", site=site).exists())
        self.assertEqual(Site.objects.get(pk=site.pk).scope_status, "IN_SCOPE")  # données incomplètes : inchangé

    def test_contract_on_two_records_is_ambiguous_and_not_moved(self):
        from core.site_sync import sync_site_contracts

        a = Site.objects.create(site_id="DKR_0001", name="A", contract_number="22005301879")
        self.assertEqual(SonatelInvoice.objects.get(pk=self.invoice.pk).site_id, a.pk)
        b = Site.objects.create(site_id="DKR_0002", name="B", contract_number="22005301879")
        # Deuxième fiche avec le même contrat : rien ne bascule vers B, le cas est signalé.
        self.assertEqual(ContractSiteLink.objects.get(numero_compte_contrat="22005301879").site_id, a.pk)
        self.assertEqual(SonatelInvoice.objects.get(pk=self.invoice.pk).site_id, a.pk)
        stats = sync_site_contracts(b)
        self.assertEqual(stats["ambigus"], [("22005301879", ["DKR_0002", "DKR_0001"])])


class SiteSyncRelinkTests(TestCase):
    """Sites mis à jour mais factures « non liées » / certifications « Contrat inconnu » : rattrapage automatique."""

    def setUp(self):
        self.batch = ImportBatch.objects.create(source_filename="factures.xlsx")
        self.invoice = SonatelInvoice.objects.create(
            batch=self.batch, numero_compte_contrat="22005301879", numero_facture="F1", payment_status="PAID",
            date_debut_periode=date(2026, 8, 1), date_fin_periode=date(2026, 8, 31))

    def test_contract_with_leading_zero_still_links(self):
        site = Site.objects.create(site_id="DKR_2834", name="RUF44", contract_number="022005301879")
        self.assertEqual(SonatelInvoice.objects.get(pk=self.invoice.pk).site_id, site.pk)
        # le format de la facture est aussi lié, pour les prochains imports
        self.assertEqual(ContractSiteLink.objects.get(numero_compte_contrat="22005301879").site_id, site.pk)

    def test_variant_already_linked_elsewhere_is_never_moved(self):
        other = Site.objects.create(site_id="DKR_0001", name="A")
        ContractSiteLink.objects.create(numero_compte_contrat="22005301879", site=other)
        self.assertEqual(SonatelInvoice.objects.get(pk=self.invoice.pk).site_id, other.pk)
        Site.objects.create(site_id="DKR_2834", name="RUF44", contract_number="022005301879")
        self.assertEqual(SonatelInvoice.objects.get(pk=self.invoice.pk).site_id, other.pk)

    def test_contract_link_created_directly_links_invoices(self):
        site = Site.objects.create(site_id="DKR_2834", name="RUF44")
        ContractSiteLink.objects.create(numero_compte_contrat="22005301879", site=site)  # page Liens contrats
        self.assertEqual(SonatelInvoice.objects.get(pk=self.invoice.pk).site_id, site.pk)

    def test_unknown_contract_certification_is_relaunched(self):
        from unittest import mock

        from certification.models import CertificationBatch, CertificationResult

        cb = CertificationBatch.objects.create(import_batch=self.batch, unknown_contract=1, total=1)
        res = CertificationResult.objects.create(cert_batch=cb, invoice=self.invoice, site=None,
                                                 status=CertificationResult.Status.UNKNOWN_CONTRACT)
        with mock.patch("certification.tasks.certify_single_invoice.delay") as delay, \
                self.captureOnCommitCallbacks(execute=True):
            site = Site.objects.create(site_id="DKR_2834", name="RUF44", contract_number="22005301879")
        res.refresh_from_db()
        cb.refresh_from_db()
        self.assertEqual((res.status, res.site_id, cb.unknown_contract), ("PENDING_CERTIFICATION", site.pk, 0))
        delay.assert_called_once_with(res.pk)

    def test_backfill_command_reports_remaining_causes(self):
        from io import StringIO

        from django.core.management import call_command

        SonatelInvoice.objects.create(batch=self.batch, numero_compte_contrat="99999", numero_facture="F2", payment_status="PAID")
        Site.objects.create(site_id="DKR_0001", name="A", contract_number="22005301879", invoice_payment="Sonatel", grid_fee=True)
        out = StringIO()
        call_command("sync_sites_billing", stdout=out)
        text = out.getvalue()
        self.assertIn("Factures encore sans site : 1 (dont 1 payées)", text)
        self.assertIn("CONTRAT_ABSENT_GESTION_SITES", text)
        self.assertIn("DKR_0001 : paiement « Sonatel »", text)  # rattachée mais hors périmètre Suivi facturation
