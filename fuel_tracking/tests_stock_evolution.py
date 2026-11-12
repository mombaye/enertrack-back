"""Évolution mensuelle du stock : relevé daté dans le mois, toutes sources, site sans relevé non compté."""
from datetime import datetime
from unittest import mock

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from fuel_tracking.models import FuelStockObservation, FuelStockSnapshot
from fuel_tracking.services.stock_history import last_months
from fuel_tracking.tests_bi import api

URL = "/api/fuel-tracking/bi/stock/evolution/"


def closing(site, litres, d, origine=None, n=[0]):
    n[0] += 1
    FuelStockObservation.objects.create(source_type="CLOSING", source_event_id=f"CL:ev{n[0]}", site_id=site, level_liters=litres,
                                        level_unit="L", observation_datetime=timezone.make_aware(datetime.combine(d, datetime.min.time())),
                                        validation_status="VALIDATED", conversion_status="VALID",
                                        source_context={"origine_stock": origine} if origine else {})


@mock.patch("fuel_tracking.services.stock_registry.fuel_stock_retained", return_value={})
class StockEvolutionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.months = last_months(3)
        m0, m1 = self.months[0], self.months[1]
        self.d0 = datetime.strptime(m0 + "-28", "%Y-%m-%d").date()
        self.d1 = datetime.strptime(m1 + "-15", "%Y-%m-%d").date()
        FuelStockSnapshot.objects.create(site_id="A", has_genset=True, capacity_snowflake_l=1000)
        FuelStockSnapshot.objects.create(site_id="B", has_genset=True, capacity_snowflake_l=2000)
        closing("A", 400, self.d0, "Relevés de Terrain Technicien")
        closing("B", 1000, self.d0, "Estimation Energy Desk")
        closing("A", 300, self.d1)
        # archive capteur de fin de mois m1 pour B, plus récente qu'aucun relevé de B ce mois-là
        FuelStockSnapshot.objects.create(site_id="B", has_genset=True, snapshot_year=self.d1.year, snapshot_month=self.d1.month,
                                         stock_snowflake_l=500, stock_snowflake_date=self.d1, capacity_snowflake_l=2000)

    def test_months(self, _r):
        b = api().get(URL, {"mois": "3"}).json()
        m = {x["month"]: x for x in b["months"]}
        a, c = m[self.months[0]], m[self.months[1]]
        self.assertEqual((a["stock_l"], a["nb_sites_releves"], a["remplissage_pct"], a["nb_estimes"]), (1400.0, 2, 46.7, 1))
        self.assertEqual((c["stock_l"], c["nb_sites_releves"]), (800.0, 2))
        self.assertEqual(c["sources"], {"Synthèse": 1, "Capteur": 1})
        last = m[self.months[2]]
        self.assertIsNone(last["stock_l"])                       # aucun relevé daté ce mois-ci : absent, pas 0 ni report
        self.assertTrue(last["en_cours"])
        self.assertIn("evolution", b["explain"])

    def test_bad_param(self, _r):
        self.assertEqual(api().get(URL, {"mois": "x"}).status_code, 400)
