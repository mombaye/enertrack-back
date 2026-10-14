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
