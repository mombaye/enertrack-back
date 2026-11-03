from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone

from fuel_tracking.models import FuelImportBatch, FuelSiteTarget
from fuel_tracking.services import conso_comparison as CC
from fuel_tracking.services import fuel_target as TG
from fuel_tracking.services import stock_import_pipeline as P
from fuel_tracking.tests_stock_period_balance import BalanceBase
from fuel_tracking.tests_stock_registry import TmpRootMixin, xlsx

HEAD = ["CODE SITE ORANGE", "NOM SITE", "REGION", "TYPOLOGIE", "Target mensuel", "Conso 2025 janv", "Ecart conso vs target"]


def target_file(rows=None):
    rows = rows or [["DKR_0001", "SITE A", "DKR", "GE", 3100, 2900, -200], ["DKR_0002", "SITE B", "DKR", "GE", 1000, 1500, 500]]
    return xlsx({TG.SHEET: [["Analyse variation conso fuel"], HEAD, *rows]})


class TargetImportTests(TmpRootMixin, BalanceBase):
    def test_import_stores_site_targets_with_required_metadata(self):
        batch = P.process_bytes(target_file(), "closing_2025.xlsx", "TARGET_FUEL", origin="UPLOAD")
        self.assertEqual(batch.status, FuelImportBatch.Status.ACCEPTED, batch.status_message)
        t = FuelSiteTarget.objects.get(site_id="DKR_0001")
        self.assertEqual((t.target_type, t.target_unit, t.source_target, t.niveau_target, t.statut_target),
                         ("TARGET_FUEL_SITE", "L/mois", "CLOSING_HISTORIQUE", "SITE", "A_CONFIRMER_PERIODE"))
        self.assertEqual(t.target_mensuel_l, Decimal("3100"))
        self.assertEqual(t.reference_year, 2025)
        self.assertEqual((t.source_sheet, t.source_row), (TG.SHEET, 3))
        self.assertIsNone(t.confirmed_year)

    def test_reimport_same_file_never_duplicates_and_bad_rows_are_rejected(self):
        P.process_bytes(target_file(), "a.xlsx", "TARGET_FUEL", origin="UPLOAD")
        again = P.process_bytes(target_file(), "a.xlsx", "TARGET_FUEL", origin="UPLOAD")
        self.assertEqual(again.status, FuelImportBatch.Status.REJECTED)
        self.assertEqual(FuelSiteTarget.objects.count(), 2)
        rows = [["DKR_0001", "A", "DKR", "GE", None, 0, 0], ["DKR_0002", "B", "DKR", "GE", -5, 0, 0], ["DKR_0001", "A bis", "DKR", "GE", 900, 0, 0], ["ZZZ_9", "X", "DKR", "GE", 100, 0, 0]]
        b = P.process_bytes(target_file(rows), "b.xlsx", "TARGET_FUEL", origin="UPLOAD")
        self.assertEqual((b.rows_total, b.rows_rejected), (4, 2))             # cible vide ou négative : jamais remplacée par 0
        active = {t.site_id: t for t in FuelSiteTarget.objects.filter(is_active=True)}
        self.assertEqual(set(active), {"DKR_0001", "ZZZ_9"})
        self.assertEqual(active["DKR_0001"].target_mensuel_l, Decimal("900"))
        self.assertEqual(FuelSiteTarget.objects.filter(site_id="DKR_0001").count(), 2)    # l'ancienne version est conservée (inactive)

    def test_unknown_sheet_is_refused(self):
        b = P.process_bytes(xlsx({"Autre": [["x"]]}), "c.xlsx", "TARGET_FUEL", origin="UPLOAD")
        self.assertEqual(b.status, FuelImportBatch.Status.FAILED)
        self.assertEqual(FuelSiteTarget.objects.count(), 0)


class TargetRulesTests(BalanceBase):
    def test_prorata_partial_and_multi_month(self):
        self.assertEqual(TG.prorated_target(3100, date(2026, 1, 1), date(2026, 1, 10)), Decimal("1000.000"))      # 10 j sur 31
        self.assertEqual(TG.prorated_target(3100, date(2026, 1, 1), date(2026, 1, 31)), Decimal("3100.000"))
        # 16 j de janvier (/31) + 14 j de février (/28), mois par mois
        expected = Decimal("3100") * 16 / 31 + Decimal("2800") * 14 / 28
        self.assertEqual(TG.prorated_target(Decimal("3100"), date(2026, 1, 16), date(2026, 1, 31)) + TG.prorated_target(Decimal("2800"), date(2026, 2, 1), date(2026, 2, 14)),
                         expected.quantize(Decimal("0.001")))
        self.assertIsNone(TG.prorated_target(None, date(2026, 1, 1), date(2026, 1, 10)))

    def test_classification_and_guards(self):
        self.assertEqual(TG.classify(1000, 1000), TG.DANS)
        self.assertEqual(TG.classify(800, 1000), TG.SOUS)
        self.assertEqual(TG.classify(1300, 1000), TG.AU_DESSUS)
        self.assertIsNone(TG.classify(None, 1000))
        self.assertIsNone(TG.classify(500, 0))          # target nul : pas de pourcentage ni de position

    def _site_row(self, site, ini, fin, target_monthly, confirmed_year=None):
        self.stocks(ini, fin, site=site)
        FuelSiteTarget.objects.create(site_id=site, target_mensuel_l=Decimal(target_monthly), reference_year=2025, confirmed_year=confirmed_year)

    def test_comparison_target_statuses_and_global_gap_from_sums(self):
        from fuel_tracking.services import stock_period_balance as SB
        self._site_row("DKR_0001", 1000, 400, 19000, None)     # variation 600 ; historique à confirmer
        self._site_row("DKR_0002", 3000, 1000, 1000, None)
        SB.compute_balances(self.start, self.end, site_ids=["DKR_0001", "DKR_0002"])
        no_target = None
        with mock.patch("fuel_tracking.services.cph_service.compute_period_summary", return_value=[]):
            out = CC.build(self.start.date(), self.end.date(), {}, with_cph_quality=False)
        rows = {r["site_id"]: r for r in out["rows"]}
        self.assertEqual(rows["DKR_0001"]["target"]["statut"], "TARGET_HISTORIQUE_A_CONFIRMER")
        t1, t2 = rows["DKR_0001"]["target"]["periode_l"], rows["DKR_0002"]["target"]["periode_l"]
        v1, v2 = rows["DKR_0001"]["stock"]["variation_l"], rows["DKR_0002"]["stock"]["variation_l"]
        k = out["kpis"]["target"]
        self.assertEqual(k["sites_avec_target"], 2)
        self.assertEqual(k["total_periode_l"], round(t1 + t2, 1))
        # écart global = (Σ variations − Σ targets) / Σ targets : PAS la moyenne des pourcentages
        self.assertEqual(k["ecart_global_pct"], round((v1 + v2 - t1 - t2) / (t1 + t2) * 100, 1))
        mean_pct = ((v1 - t1) / t1 * 100 + (v2 - t2) / t2 * 100) / 2
        self.assertNotEqual(k["ecart_global_pct"], round(mean_pct, 1))
        self.assertIs(no_target, None)

    def test_confirmed_target_gets_position_status_and_missing_target_is_unavailable(self):
        from fuel_tracking.services import stock_period_balance as SB
        year = self.start.year
        self._site_row("DKR_0001", 1000, 400, 19000, confirmed_year=year)
        self.stocks(900, 500, site="DKR_0002")                 # pas de target pour ce site
        SB.compute_balances(self.start, self.end, site_ids=["DKR_0001", "DKR_0002"])
        with mock.patch("fuel_tracking.services.cph_service.compute_period_summary", return_value=[]):
            out = CC.build(self.start.date(), self.end.date(), {}, with_cph_quality=False)
        rows = {r["site_id"]: r for r in out["rows"]}
        self.assertIn(rows["DKR_0001"]["target"]["statut"], (TG.SOUS, TG.DANS, TG.AU_DESSUS))
        self.assertEqual(rows["DKR_0002"]["target"]["statut"], "TARGET_INDISPONIBLE")
        self.assertIsNone(rows["DKR_0002"]["target"]["periode_l"])         # jamais de target fabriqué
        self.assertIsNone(rows["DKR_0002"]["target"]["ecart_l"])
        self.assertEqual(out["kpis"]["target"]["sites_sans_target"] >= 1, True)
