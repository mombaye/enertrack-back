"""Vues BI du Suivi carburant : agrégats justes, absent ≠ 0, mesuré et estimé jamais additionnés."""
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from fuel_tracking.models import FuelConsommationMonthly, FuelStockSnapshot


def api():
    c = APIClient()
    c.force_authenticate(get_user_model().objects.create_user("bi", "bi@x.sn", "p", role="analyst"))
    return c


def conso(month, site, **kw):
    y, m = int(month[:4]), int(month[5:7])
    kw.setdefault("has_genset", True)
    return FuelConsommationMonthly.objects.create(month_year=month, year=y, month=m, site_id=site, **kw)


class BiConsoTests(TestCase):
    def setUp(self):
        conso("2026-09", "A", country="SN", conso_snowflake_l=D("100"), ge_prod_kwh=D("400"), enoc_qte_ajoutee_l=D("80"))
        conso("2026-10", "A", country="SN", conso_snowflake_l=D("120"), ge_prod_kwh=D("400"), enoc_qte_ajoutee_l=D("300"))
        conso("2026-10", "B", country="ML", conso_snowflake_l=None, ge_prod_kwh=D("500"))
        conso("2026-10", "NOGE", country="SN", has_genset=False, conso_snowflake_l=D("999"))

    def test_kpis_period_and_deltas(self):
        b = api().get("/api/fuel-tracking/bi/conso/", {"from_month": "2026-10", "to_month": "2026-10"}).json()
        k = b["kpis"]
        self.assertEqual(k["conso_mesuree_l"], 120.0)          # site sans GE exclu
        self.assertEqual(k["nb_sites"], 2)
        self.assertEqual(k["couverture_pct"], 50.0)             # B : conso absente, pas 0
        self.assertEqual(k["l_kwh"], 0.3)                      # ratio des sommes, B (sans conso) exclu du dénominateur
        self.assertEqual(b["deltas"]["conso_mesuree_l"], 20.0)  # vs 2026-09
        self.assertEqual(b["top_conso"][0]["site_id"], "A")
        self.assertEqual(b["top_ecart_enoc"][0]["ecart_l"], -180.0)

    def test_country_filter(self):
        b = api().get("/api/fuel-tracking/bi/conso/", {"from_month": "2026-10", "to_month": "2026-10", "country": "ML"}).json()
        self.assertIsNone(b["kpis"]["conso_mesuree_l"])        # absent, jamais 0
        self.assertEqual(b["kpis"]["nb_sites"], 1)

    def test_empty(self):
        FuelConsommationMonthly.objects.all().delete()
        self.assertEqual(api().get("/api/fuel-tracking/bi/conso/").json()["months"], [])


class BiStockTests(TestCase):
    def test_current_only_and_autonomy(self):
        FuelStockSnapshot.objects.create(site_id="A", has_genset=True, stock_snowflake_l=100, capacity_snowflake_l=1000, stock_snowflake_pct=10)
        FuelStockSnapshot.objects.create(site_id="A", has_genset=True, snapshot_year=2026, snapshot_month=10, stock_snowflake_l=900)
        FuelStockSnapshot.objects.create(site_id="B", has_genset=True)
        conso("2026-10", "A", conso_snowflake_l=D("310"))
        b = api().get("/api/fuel-tracking/bi/stock/", {"month": "2026-10"}).json()
        self.assertEqual(b["kpis"]["nb_sites"], 2)
        self.assertEqual(b["kpis"]["nb_critique"], 1)
        self.assertEqual(b["top_critique"][0]["autonomie_j"], 10.0)   # 100 L / (310 L / 31 j)
        self.assertEqual(b["distribution_remplissage"][-1]["nb_sites"], 1)  # B inconnu


class BiCphTests(TestCase):
    def test_only_sites_with_both_values_are_compared(self):
        rows = [
            {"site_id": "A", "perimetre": "GE", "cph_status": "COMPLET", "conso_theorique_l": D("100"), "runtime_total_h": D("10"),
             "charge_moy": D("0.5"), "cph_moy_l_h": D("10"), "comparaison": {"conso_mesuree_l": D("130")}, "country": "SN"},
            {"site_id": "B", "perimetre": "GE", "cph_status": "COMPLET", "conso_theorique_l": D("50"), "runtime_total_h": D("5"),
             "comparaison": {"conso_mesuree_l": None}, "country": "SN"},
            {"site_id": "C", "perimetre": "GE", "cph_status": "NON_CALCULE", "conso_theorique_l": None,
             "motif_cph": {"code": "RUNTIME_ABSENT"}, "comparaison": {}},
            {"site_id": "S", "perimetre": "SANS_GE"},
        ]
        with mock.patch("fuel_tracking.services.cph_service.compute_period_summary", return_value=rows):
            b = api().get("/api/fuel-tracking/bi/cph/", {"start": "2026-10-01", "end": "2026-10-31"}).json()
        k = b["kpis"]
        self.assertEqual((k["nb_sites_ge"], k["nb_cph_complet"], k["conso_theorique_l"]), (3, 2, 150.0))
        self.assertEqual((k["conso_theorique_comparable_l"], k["conso_mesuree_comparable_l"], k["ecart_global_pct"]), (100.0, 130.0, 30.0))
        self.assertEqual(b["motifs_non_calcule"], [{"label": "RUNTIME_ABSENT", "nb_sites": 1}])

    def test_period_too_long_is_rejected(self):
        self.assertEqual(api().get("/api/fuel-tracking/bi/cph/", {"start": "2026-01-01", "end": "2026-10-31"}).status_code, 400)
