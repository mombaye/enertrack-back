"""Plan de ravitaillement : dates, quantités, exclusions comptées, jamais de quantité inventée."""
from datetime import timedelta
from decimal import Decimal as D

from django.test import TestCase
from django.utils import timezone

from fuel_tracking.models import FuelStockSnapshot
from fuel_tracking.tests_bi import api, conso

URL = "/api/fuel-tracking/bi/ravitaillement/"


def snap(site, stock, cap, age=0, **kw):
    d = timezone.localdate() - timedelta(days=age) if age is not None else None
    return FuelStockSnapshot.objects.create(site_id=site, has_genset=True, stock_snowflake_l=stock, capacity_snowflake_l=cap,
                                            stock_snowflake_date=d, **kw)


class RavitaillementTests(TestCase):
    def setUp(self):
        # Mois de référence 2026-10 (31 j) : 310 L → 10 L/jour.
        conso("2026-11", "Z", conso_snowflake_l=D("1"))
        for s in ("URG", "SEM", "LOIN", "VIEUX", "SANSCAP"):
            conso("2026-10", s, conso_snowflake_l=D("310"))
        snap("URG", 25, 1000)          # 2,5 j avant rupture → à livrer aujourd'hui
        snap("SEM", 60, 1000)          # 6 j → à livrer dans 4 j
        snap("LOIN", 200, 1000)        # 20 j → hors horizon de 7 j
        snap("VIEUX", 100, 1000, age=9)  # relevé de 9 j : 100 − 90 = 10 L estimés
        snap("SANSCAP", 30, None)      # capacité inconnue
        snap("NOCONSO", 30, 1000)      # pas de conso mesurée
        FuelStockSnapshot.objects.create(site_id="NOSTOCK", has_genset=True)

    def test_plan(self):
        b = api().get(URL, {"month": "2026-10"}).json()
        today = timezone.localdate()
        rows = {r["site_id"]: r for r in b["plan"]}
        self.assertEqual(set(rows), {"URG", "SEM", "VIEUX", "SANSCAP"})
        self.assertEqual(rows["URG"]["livrer_avant"], str(today))
        self.assertEqual(rows["URG"]["priorite"], "URGENT")
        self.assertEqual(rows["URG"]["quantite_l"], 900)          # 900 − 25 = 875 → 900
        self.assertEqual(rows["SEM"]["livrer_avant"], str(today + timedelta(days=4)))
        self.assertEqual(rows["SEM"]["priorite"], "PLANIFIE")
        self.assertEqual(rows["SEM"]["quantite_l"], 900)          # 900 − (60 − 40) = 880 → 900
        self.assertEqual(rows["VIEUX"]["stock_estime_l"], 10)
        self.assertTrue(rows["VIEUX"]["releve_ancien"])
        self.assertIsNone(rows["SANSCAP"]["quantite_l"])          # jamais inventée
        self.assertEqual(rows["SANSCAP"]["quantite_motif"], "Capacité de cuve inconnue")
        k = b["kpis"]
        self.assertEqual((k["nb_sites"], k["nb_urgent"], k["nb_hors_plan"], k["nb_au_dela"], k["nb_sans_quantite"]), (4, 3, 2, 1, 1))
        self.assertEqual({h["site_id"] for h in b["hors_plan"]}, {"NOCONSO", "NOSTOCK"})
        self.assertEqual(b["plan"][0]["livrer_avant"], str(today))  # trié par date
        self.assertEqual(sum(d["nb_sites"] for d in b["by_day"]), 4)

    def test_parametres(self):
        b = api().get(URL, {"month": "2026-10", "horizon": "30", "securite": "0", "cible": "100"}).json()
        self.assertIn("LOIN", {r["site_id"] for r in b["plan"]})
        self.assertEqual(api().get(URL, {"horizon": "99"}).status_code, 400)
        self.assertEqual(api().get(URL, {"cible": "abc"}).status_code, 400)

    def test_export(self):
        from io import BytesIO
        from openpyxl import load_workbook
        r = api().get("/api/fuel-tracking/bi/export/ravitaillement/", {"month": "2026-10"})
        self.assertEqual(r.status_code, 200)
        wb = load_workbook(BytesIO(r.content))
        self.assertEqual(wb.sheetnames, ["Synthèse", "Plan", "Par zone", "Hors plan"])
        self.assertEqual(wb["Plan"].max_row, 5)
        self.assertEqual(wb["Hors plan"].max_row, 3)

    def test_auth(self):
        from rest_framework.test import APIClient
        self.assertIn(APIClient().get(URL).status_code, (401, 403))
