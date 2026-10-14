import io
from datetime import date

from django.core.management import call_command
from django.test import TestCase

from billing.models import ImportBatch, MonthlySynthesis, SonatelInvoice
from core.models import Site


class RecupererFacturesTests(TestCase):
    def test_attaches_invoices_and_reports_months(self):
        batch = ImportBatch.objects.create(source_filename="facturation_aout.xlsx")
        inv = SonatelInvoice.objects.create(batch=batch, numero_compte_contrat="22005301879", numero_facture="F1",
                                            date_debut_periode=date(2026, 8, 1), date_fin_periode=date(2026, 8, 31))
        MonthlySynthesis.objects.create(source=inv, numero_compte_contrat="22005301879", year=2026, month=8,
                                        period_start=date(2026, 8, 1), period_end=date(2026, 8, 31),
                                        period_total_days=31, days_covered=31)
        Site.objects.bulk_create([Site(site_id="DKR_2834", name="RUF44", contract_number="22005301879",
                                       invoice_payment="Aktivco", grid_fee=True)])  # sans signal : cas « existant »
        out = io.StringIO()
        call_command("recuperer_factures_sites", "--from", "2026-07", "--to", "2026-09", stdout=out)
        text = out.getvalue()
        self.assertEqual(SonatelInvoice.objects.get(pk=inv.pk).site.site_id, "DKR_2834")
        self.assertIn("1 visibles (1 sites)", text)
        self.assertIn("juil. 2026 : aucune facture importée", text)
        self.assertEqual(Site.objects.get(site_id="DKR_2834").scope_status, "IN_SCOPE")
