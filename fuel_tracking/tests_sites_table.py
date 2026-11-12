"""Stock retenu toutes sources, origine du stock de la Synthèse, tableau par site (mesuré et estimé séparés)."""
from datetime import date, datetime, timedelta
from decimal import Decimal as D
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from fuel_tracking.models import FuelStockObservation, FuelStockSnapshot
from fuel_tracking.services.current_stock import current_stock
from fuel_tracking.services.stock_file_analysis import closing_stock_rows
from fuel_tracking.tests_bi import api, conso

TODAY = timezone.localdate()


def closing_obs(site, litres, d, origine=None, status="VALIDATED", n=[0]):
    n[0] += 1
    return FuelStockObservation.objects.create(
        source_type="CLOSING", source_event_id=f"CL:test{n[0]}", site_id=site, level_liters=litres, level_unit="L",
        observation_datetime=timezone.make_aware(datetime.combine(d, datetime.min.time())), validation_status=status,
        conversion_status="VALID", source_context={"origine_stock": origine} if origine else {})


def snap(site, stock=None, d=None, cap=None):
    return {"site_id": site, "stock_snowflake_l": stock, "stock_snowflake_date": d, "capacity_snowflake_l": cap}


@mock.patch("fuel_tracking.services.stock_registry.fuel_stock_retained", return_value={})
class CurrentStockTests(TestCase):
    def test_most_recent_wins(self, _r):
        closing_obs("A", 400, TODAY - timedelta(days=2), "Relevés de Terrain Technicien")
        c = current_stock([snap("A", 900, TODAY - timedelta(days=10), 1000)])["A"]
        self.assertEqual((c["stock_l"], c["source"], c["estime"]), (400.0, "CLOSING", False))
        self.assertEqual((c["capacite_l"], c["capacite_source"]), (1000.0, "Capteur"))

    def test_same_day_sensor_beats_closing_and_estimation_flag(self, _r):
        closing_obs("B", 300, TODAY, "Estimation Energy Desk")
        self.assertEqual(current_stock([snap("B", 250, TODAY)])["B"]["source"], "SNOWFLAKE")
        c = current_stock([snap("C")]) ["C"]
        self.assertIsNone(c["stock_l"])                                   # aucune source : inconnu, jamais 0
        closing_obs("D", 0, TODAY, "Estimation Energy Desk")
        self.assertTrue(current_stock([snap("D")])["D"]["estime"])

    def test_pending_closing_ignored(self, _r):
        closing_obs("E", 500, TODAY, status="PENDING")
        self.assertIsNone(current_stock([snap("E")])["E"]["stock_l"])

    def test_field_reading_wins_tie(self, retained):
        retained.return_value = {"F": {"stock_l": 700, "source_type": "SCSS", "capacity_l": 2000, "statut_releve": "Validé",
                                       "observation_datetime": timezone.now()}}
        closing_obs("F", 100, TODAY)
        c = current_stock([snap("F", 50, TODAY)])["F"]
        self.assertEqual((c["stock_l"], c["source_label"], c["capacite_l"], c["nb_sources"]), (700.0, "SCSS", 2000.0, 3))


class ClosingOrigineTests(TestCase):
    def test_origine_columns_read(self):
        head = [None, "ID du site", "Date de relevé initiale", "Qtité laissée à la date du relevé", "Date de relevé finale",
                "Qtité laissée à la date du relevé", "Source Données de stock Initial", "Source Données de stock Final",
                "Etat de fiabilisation  de Stock Final"]
        rows = [head, [None, "THS_0001", date(2026, 8, 30), 100, date(2026, 9, 30), 80, "Estimation Energy Desk", "Relevés de Terrain Technicien", "n.a"]]
        data, _ = closing_stock_rows(rows)
        self.assertEqual(data[0]["origine_initial"], "Estimation Energy Desk")
        self.assertEqual(data[0]["origine_final"], "Relevés de Terrain Technicien")
        self.assertIsNone(data[0]["fiabilisation"])


CPH = [
    {"site_id": "S1", "perimetre": "GE", "country": "Senegal", "cph_status": "COMPLET", "runtime_total_h": D("300"), "cph_moy_l_h": D("2"),
     "charge_moy": D("0.2"), "conso_theorique_l": D("600")},
    {"site_id": "S2", "perimetre": "GE", "country": "Senegal", "cph_status": "COMPLET", "runtime_total_h": D("100"), "cph_moy_l_h": D("3"),
     "charge_moy": D("0.5"), "conso_theorique_l": D("300")},
    {"site_id": "S3", "perimetre": "GE", "country": "Senegal", "cph_status": "NON_CALCULE", "motif_cph": {"code": "RUNTIME_ABSENT"}},
]


@mock.patch("fuel_tracking.services.stock_registry.fuel_stock_retained", return_value={})
@mock.patch("fuel_tracking.services.cph_service.compute_period_summary", return_value=CPH)
class SiteTableTests(TestCase):
    def setUp(self):
        conso("2026-10", "S1", conso_snowflake_l=D("900"), nb_jours_data=30, ge_prod_kwh=D("2000"), enoc_qte_ajoutee_l=D("500"), enoc_nb_demandes=1)
        conso("2026-10", "S2", conso_snowflake_l=D("310"), nb_jours_data=31)
        conso("2026-10", "S3", conso_snowflake_l=None)
        conso("2026-09", "S1", conso_snowflake_l=D("600"), nb_jours_data=30)
        FuelStockSnapshot.objects.create(site_id="S1", has_genset=True, stock_snowflake_l=40, capacity_snowflake_l=1000, stock_snowflake_date=TODAY)

    def test_table_and_kpis(self, _cph, _r):
        b = api().get("/api/fuel-tracking/bi/sites-table/", {"month": "2026-10", "sort": "site_id", "desc": "0"}).json()
        k = b["kpis"]
        self.assertEqual((k["conso_mesuree_l"], k["nb_sites_mesure"]), (1210.0, 2))
        self.assertEqual((k["conso_estimee_l"], k["nb_sites_estime"]), (900.0, 2))
        self.assertEqual((k["ecart_l"], k["ecart_pct"], k["nb_sites_comparables"]), (310.0, 34.4, 2))
        self.assertEqual((k["runtime_total_h"], k["cph_moyen_l_h"]), (400.0, 2.25))
        r = {x["site_id"]: x for x in b["rows"]}
        s1 = r["S1"]
        self.assertEqual((s1["ecart_pct"], s1["l_jour"], s1["l_kwh"], s1["evolution_pct"]), (50.0, 30.0, 0.45, 50.0))
        self.assertEqual(set(s1["alertes"]), {"ECART_CPH", "L_KWH_HAUT", "SOUS_CHARGE", "AUTONOMIE_3J"})
        self.assertEqual(s1["source_stock"], "Capteur")
        self.assertIsNone(r["S3"]["conso_mesuree_l"])                    # absent, pas 0
        self.assertIsNone(r["S3"]["conso_estimee_l"])
        self.assertEqual([x["site_id"] for x in b["rows"]], ["S1", "S2", "S3"])

    def test_filters_pagination_sort(self, _cph, _r):
        c = api()
        b = c.get("/api/fuel-tracking/bi/sites-table/", {"month": "2026-10", "alerte": "ECART_CPH"}).json()
        self.assertEqual(b["total"], 1)
        b = c.get("/api/fuel-tracking/bi/sites-table/", {"month": "2026-10", "sort": "conso_estimee_l", "page_size": "10"}).json()
        self.assertEqual([x["site_id"] for x in b["rows"]], ["S1", "S2", "S3"])  # inconnu en fin
        self.assertEqual(c.get("/api/fuel-tracking/bi/sites-table/", {"sort": "xx"}).status_code, 400)
        self.assertEqual(c.get("/api/fuel-tracking/bi/sites-table/", {"month": "2026-13"}).status_code, 400)

    def test_cph_down_still_renders(self, cph, _r):
        cph.side_effect = RuntimeError("down")
        b = api().get("/api/fuel-tracking/bi/sites-table/", {"month": "2026-10"}).json()
        self.assertFalse(b["cph_disponible"])
        self.assertIsNone(b["kpis"]["conso_estimee_l"])
        self.assertEqual(b["kpis"]["conso_mesuree_l"], 1210.0)

    def test_export(self, _cph, _r):
        from io import BytesIO
        from openpyxl import load_workbook
        resp = api().get("/api/fuel-tracking/bi/export/sites/", {"month": "2026-10"})
        self.assertEqual(resp.status_code, 200)
        wb = load_workbook(BytesIO(resp.content))
        self.assertEqual(wb["Sites"].max_row, 4)
