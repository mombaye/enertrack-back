"""Vues BI du Suivi carburant : agrégats justes, absent ≠ 0, mesuré et estimé jamais additionnés."""
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from fuel_tracking.models import FuelConsommationMonthly, FuelStockSnapshot


def api():
    c = APIClient()
    U = get_user_model()
    u = U.objects.filter(username="bi").first() or U.objects.create_user("bi", "bi@x.sn", "p", role="analyst")
    c.force_authenticate(u)
    return c


def conso(month, site, **kw):
    y, m = int(month[:4]), int(month[5:7])
    kw.setdefault("has_genset", True)
    return FuelConsommationMonthly.objects.create(month_year=month, year=y, month=m, site_id=site, **kw)


class BiConsoTests(TestCase):
    def setUp(self):
        conso("2026-09", "A", country="SN", conso_snowflake_l=D("100"), ge_prod_kwh=D("400"), enoc_qte_ajoutee_l=D("80"))
        conso("2026-10", "A", country="SN", conso_snowflake_l=D("120"), ge_prod_kwh=D("400"), enoc_qte_ajoutee_l=D("300"))
        conso("2026-10", "B", country="Mali", conso_snowflake_l=None, ge_prod_kwh=D("500"))
        conso("2026-10", "NOGE", country="SN", has_genset=False, conso_snowflake_l=D("999"))

    def test_kpis_period_and_deltas(self):
        b = api().get("/api/fuel-tracking/bi/conso/", {"from_month": "2026-10", "to_month": "2026-10"}).json()
        k = b["kpis"]
        self.assertEqual(k["conso_mesuree_l"], 120.0)          # site sans GE exclu
        self.assertEqual(k["nb_sites"], 1)                     # B (Mali) hors périmètre Sénégal
        self.assertEqual(k["couverture_pct"], 100.0)
        self.assertEqual(k["l_kwh"], 0.3)                      # ratio des sommes, B (sans conso) exclu du dénominateur
        self.assertIsNone(b["deltas"]["conso_mesuree_l"])      # un seul mois choisi : pas de comparaison
        self.assertTrue(b["mois_unique"])
        self.assertEqual(b["top_conso"][0]["site_id"], "A")
        self.assertEqual(b["top_ecart_enoc"][0]["ecart_enoc_l"], -180.0)

    def test_senegal_scope_and_explain(self):
        conso("2026-10", "C", country="Senegal", conso_snowflake_l=D("50"), nb_jours_data=5)
        conso("2026-10", "D", country=None, conso_snowflake_l=D("10"), nb_jours_data=5)
        b = api().get("/api/fuel-tracking/bi/conso/", {"from_month": "2026-10", "to_month": "2026-10"}).json()
        k = b["kpis"]
        self.assertEqual(b["perimetre"], "Sénégal")
        self.assertEqual(k["conso_mesuree_l"], 180.0)          # A (SN) + C + D (pays vide) ; B (Mali) exclu
        self.assertEqual(k["nb_sites"], 3)
        x = b["explain"]["conso_mesuree_l"]
        self.assertEqual(x["valeurs"][0]["valeur"], 180.0)     # l'explication reprend le chiffre affiché
        self.assertTrue(any("non renseigné" in p for p in x["perimetre"]))
        self.assertTrue(any(e["nb"] == 1 for e in x["exclusions"]))  # NOGE (sans GE) exclu

    def test_zone_filter_and_pareto(self):
        FuelConsommationMonthly.objects.filter(site_id="A").update(zone_fichier="DKR")
        b = api().get("/api/fuel-tracking/bi/conso/", {"from_month": "2026-10", "to_month": "2026-10", "zone": "THS"}).json()
        self.assertIsNone(b["kpis"]["conso_mesuree_l"])        # absent, jamais 0
        b = api().get("/api/fuel-tracking/bi/conso/", {"from_month": "2026-10", "to_month": "2026-10"}).json()
        self.assertEqual(b["by_zone"][0]["label"], "DKR")
        self.assertEqual(b["pareto"]["pct_sites_80"], 100.0)

    def test_empty(self):
        FuelConsommationMonthly.objects.all().delete()
        self.assertEqual(api().get("/api/fuel-tracking/bi/conso/").json()["months"], [])


class BiStockTests(TestCase):
    def test_current_only_and_autonomy(self):
        FuelStockSnapshot.objects.create(site_id="A", has_genset=True, stock_snowflake_l=100, capacity_snowflake_l=1000, stock_snowflake_pct=10)
        FuelStockSnapshot.objects.create(site_id="A", has_genset=True, snapshot_year=2026, snapshot_month=10, stock_snowflake_l=900)
        FuelStockSnapshot.objects.create(site_id="B", has_genset=True)
        FuelStockSnapshot.objects.create(site_id="M", country="Mali", has_genset=True, stock_snowflake_l=5, capacity_snowflake_l=1000, stock_snowflake_pct=1)
        conso("2026-10", "A", conso_snowflake_l=D("310"))
        b = api().get("/api/fuel-tracking/bi/stock/", {"month": "2026-10"}).json()
        self.assertEqual(b["kpis"]["nb_sites"], 2)
        self.assertEqual(b["kpis"]["nb_critique"], 1)
        self.assertEqual(b["top_critique"][0]["autonomie_j"], 10.0)   # 100 L / (310 L / 31 j)
        self.assertEqual(b["kpis"]["couverture_j"], 10.0)             # seul A a une conso par jour : B non compté
        self.assertEqual(b["distribution_remplissage"][-1]["nb_sites"], 1)  # B inconnu
        self.assertEqual(b["explain"]["stock_total_l"]["exclusions"][0]["nb"], 1)  # M (Mali) exclu et compté


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


class BiConsoTargetTests(TestCase):
    def test_target_compared_on_measured_months_only(self):
        from fuel_tracking.models import FuelSiteTarget
        conso("2026-10", "A", country="SN", conso_snowflake_l=D("130"), nb_jours_data=10)
        conso("2026-10", "B", country="SN", conso_snowflake_l=None)
        FuelSiteTarget.objects.create(site_id="A", target_mensuel_l=D("100"), is_active=True)
        FuelSiteTarget.objects.create(site_id="B", target_mensuel_l=D("100"), is_active=True)
        b = api().get("/api/fuel-tracking/bi/conso/", {"from_month": "2026-10", "to_month": "2026-10"}).json()
        k = b["kpis"]
        self.assertEqual(k["l_site_jour"], 13.0)               # 130 L / 10 jours mesurés
        self.assertEqual(k["nb_sites_target"], 1)              # B sans conso : pas comparé, pas « 0 L »
        self.assertEqual(k["ecart_target_pct"], 30.0)
        self.assertEqual(k["nb_sites_au_dessus_target"], 1)
        self.assertEqual(b["top_target"][0]["site_id"], "A")
        self.assertNotIn("sites", b)


class BiExportTests(TestCase):
    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def _wb(self, resp):
        from io import BytesIO
        from openpyxl import load_workbook
        self.assertEqual(resp.status_code, 200, getattr(resp, "content", b"")[:300])
        self.assertEqual(resp["Content-Type"], self.XLSX)
        return load_workbook(BytesIO(resp.content))

    def test_conso_export_has_all_sites_and_blank_for_absent(self):
        for i in range(15):
            conso("2026-10", f"S{i:02d}", country="SN", conso_snowflake_l=D(100 + i))
        conso("2026-10", "VIDE", country="SN", conso_snowflake_l=None)
        wb = self._wb(api().get("/api/fuel-tracking/bi/export/conso/", {"from_month": "2026-10", "to_month": "2026-10"}))
        self.assertEqual(wb.sheetnames, ["Synthèse", "Mensuel", "4 mesures", "Journalier", "Par zone", "Par typologie", "Sites"])
        ws = wb["Sites"]
        self.assertEqual(ws.max_row, 17)                       # en-tête + 16 sites (pas seulement le Top 10)
        last = [c.value for c in ws[17]]
        self.assertEqual(last[0], "VIDE")
        self.assertIsNone(last[6])                             # conso absente = cellule vide, jamais 0

    def test_stock_cph_commande_estimation_exports(self):
        from fuel_tracking.models import FuelSuiviCommandeSite
        FuelStockSnapshot.objects.create(site_id="A", has_genset=True, stock_snowflake_l=100, capacity_snowflake_l=1000, stock_snowflake_pct=10)
        self.assertEqual(self._wb(api().get("/api/fuel-tracking/bi/export/stock/"))["Sites"].max_row, 2)
        dist = api().get("/api/fuel-tracking/bi/stock/").json()["distribution_source"]
        self.assertEqual(sum(b["nb_sites"] for b in dist), 1)   # chaque site GE compté une fois, sous l'origine de son stock retenu
        self.assertIsNone(api().get("/api/fuel-tracking/bi/stock/").json()["kpis"]["couverture_j"])  # aucune conso par jour : non calculable, pas 0
        FuelSuiviCommandeSite.objects.create(month_year="2026-10", site_id="A", commande_avec_marge_l=D("500"))
        self.assertEqual(self._wb(api().get("/api/fuel-tracking/bi/export/commande/"))["Sites"]["J2"].value, 500.0)
        self._wb(api().get("/api/fuel-tracking/bi/export/estimation/"))
        rows = [{"site_id": "A", "perimetre": "GE", "cph_status": "COMPLET", "conso_theorique_l": D("100"), "runtime_total_h": D("10"),
                 "charge_moy": D("0.5"), "cph_moy_l_h": D("10"), "comparaison": {"conso_mesuree_l": D("130")}}]
        with mock.patch("fuel_tracking.services.cph_service.compute_period_summary", return_value=rows):
            ws = self._wb(api().get("/api/fuel-tracking/bi/export/cph/", {"start": "2026-10-01", "end": "2026-10-31"}))["Sites"]
        self.assertEqual(ws["P2"].value, 30.0)                 # écart mesuré − CPH en litres

    def test_unknown_and_bad_period(self):
        self.assertEqual(api().get("/api/fuel-tracking/bi/export/xyz/").status_code, 404)
        self.assertEqual(api().get("/api/fuel-tracking/bi/export/cph/", {"start": "2026-01-01", "end": "2026-12-31"}).status_code, 400)


class BiMonthAndSourcesTests(TestCase):
    def setUp(self):
        conso("2026-09", "A", country="Senegal", conso_snowflake_l=D("100"), nb_jours_data=30)
        conso("2026-10", "A", country="Senegal", conso_snowflake_l=D("120"), nb_jours_data=31)

    def test_single_month_has_no_comparison(self):
        b = api().get("/api/fuel-tracking/bi/conso/", {"from_month": "2026-09", "to_month": "2026-10", "month": "2026-10"}).json()
        self.assertTrue(b["mois_unique"])
        self.assertEqual(b["months"], ["2026-10"])
        self.assertEqual(b["kpis"]["conso_mesuree_l"], 120.0)
        self.assertEqual(b["previous_months"], [])
        self.assertTrue(all(v is None for v in b["deltas"].values()))

    def test_sources_senegal_only_and_period_limit(self):
        rows = [{"country": "Senegal", "correspondance": {"utilise": True}}, {"country": "Mali", "correspondance": {"utilise": True}},
                {"country": None, "correspondance": {"utilise": False}}]
        seen = {}

        def fake_kpis(rs):
            seen["n"] = len(rs)
            v = {"total_l": 500.0, "sites": 1, "negatives": 0}
            return {"sites": len(rs), "variation": v, "closing": v, "instrumentee": v, "cph_complete": v, "cph_partielle": {"total_l": None, "sites": 0}}
        build = {"rows": rows, "periode": {"sites_avec_bilan": 0}, "fraicheur": {}}
        with mock.patch("fuel_tracking.services.conso_comparison.build", return_value=build), \
                mock.patch("fuel_tracking.services.conso_comparison._kpis", side_effect=fake_kpis):
            b = api().get("/api/fuel-tracking/bi/conso/sources/", {"month": "2026-10"}).json()
        self.assertTrue(b["disponible"])
        self.assertEqual(seen["n"], 1)                                   # Mali exclu, site hors Closing exclu
        self.assertEqual(b["kpis"]["variation"]["total_l"], 500.0)
        self.assertIn("non calculable", b["explain"]["variation"]["note"])  # aucun bilan sur la période
        b = api().get("/api/fuel-tracking/bi/conso/sources/", {"from_month": "2026-01", "to_month": "2026-10"}).json()
        self.assertFalse(b["disponible"])                                # > 3 mois : non calculé, jamais 0
        self.assertIsNotNone(b["motif"])


    def test_sources_follow_typology(self):
        # Typologie au sens des graphiques (fichier, sinon Snowflake) : seuls les sites de cette typologie entrent dans les KPIs.
        conso("2026-10", "T1", country="SN", typology_fichier="Rural")
        conso("2026-10", "T2", country="SN", typology_fichier="Urbain")
        rows = [{"site_id": "T1", "country": "SN", "correspondance": {"utilise": True}},
                {"site_id": "T2", "country": "SN", "correspondance": {"utilise": True}}]
        seen = {}

        def fake_kpis(rs):
            seen["ids"] = [r["site_id"] for r in rs]
            v = {"total_l": 1.0, "sites": len(rs), "negatives": 0}
            return {"sites": len(rs), "variation": v, "closing": v, "instrumentee": v, "cph_complete": v, "cph_partielle": {"total_l": None, "sites": 0}}
        build = {"rows": rows, "periode": {"sites_avec_bilan": 1}, "fraicheur": {}}
        with mock.patch("fuel_tracking.services.conso_comparison.build", return_value=build), \
                mock.patch("fuel_tracking.services.conso_comparison._kpis", side_effect=fake_kpis):
            b = api().get("/api/fuel-tracking/bi/conso/sources/", {"month": "2026-10", "typology": "Rural"}).json()
        self.assertEqual(seen["ids"], ["T1"])
        self.assertIn("Typologie : Rural", b["explain"]["variation"]["perimetre"])


class BiSiteTests(TestCase):
    def setUp(self):
        conso("2026-08", "DKR_0012", site_name="Médina", country="Senegal", zone_fichier="DKR", conso_snowflake_l=D("90"), nb_jours_data=30)
        conso("2026-09", "DKR_0012", site_name="Médina", country="Senegal", zone_fichier="DKR", conso_snowflake_l=D("100"), nb_jours_data=25,
              ge_prod_kwh=D("400"), enoc_qte_ajoutee_l=D("200"), enoc_nb_demandes=1)
        conso("2026-10", "DKR_0012", site_name="Médina", country="Senegal", zone_fichier="DKR", conso_snowflake_l=None)
        conso("2026-10", "DKR_0120", site_name="Pikine", country="SN")
        conso("2026-10", "BKO_0012", site_name="Bamako", country="Mali", conso_snowflake_l=D("50"))
        FuelStockSnapshot.objects.create(site_id="DKR_0012", has_genset=True, stock_snowflake_l=80, capacity_snowflake_l=1000, stock_snowflake_pct=8)

    def test_search_senegal_only_exact_first(self):
        r = api().get("/api/fuel-tracking/bi/sites/", {"q": "0012"}).json()["results"]
        self.assertEqual([s["site_id"] for s in r], ["DKR_0012"])          # site malien exclu
        r = api().get("/api/fuel-tracking/bi/sites/", {"q": "dkr_0012"}).json()["results"]
        self.assertEqual(r[0]["site_id"], "DKR_0012")
        r = api().get("/api/fuel-tracking/bi/sites/", {"q": "pikine"}).json()["results"]
        self.assertEqual(r[0]["site_id"], "DKR_0120")
        self.assertEqual(api().get("/api/fuel-tracking/bi/sites/", {"q": "d"}).json()["results"], [])

    def test_site_sheet(self):
        b = api().get("/api/fuel-tracking/bi/sites/dkr_0012/", {"from_month": "2026-09", "to_month": "2026-10"}).json()
        self.assertEqual(b["site"]["site_id"], "DKR_0012")
        self.assertEqual(b["kpis"]["conso_mesuree_l"], 100.0)               # octobre sans mesure : pas compté comme 0
        self.assertEqual(b["kpis"]["l_site_jour"], 4.0)                     # 100 L ÷ 25 jours mesurés
        self.assertEqual(b["kpis"]["l_kwh"], 0.25)
        oct_ = b["monthly"][1]
        self.assertIsNone(oct_["conso_mesuree_l"])
        # Juillet-août sont avant le début du suivi (septembre 2026) : aucune comparaison.
        self.assertIsNone(b["deltas"]["conso_mesuree_l"])
        self.assertEqual(b["previous_months"], [])
        self.assertEqual(b["stock"]["autonomie_j"], 20.0)                   # 80 L ÷ 4 L/jour (septembre)
        self.assertEqual(b["explain"]["conso_mesuree_l"]["valeurs"][0]["valeur"], 100.0)

    def test_comparaison_seulement_dans_le_suivi(self):
        from fuel_tracking.views_bi import _comparable
        self.assertEqual(_comparable(["2026-08", "2026-09"]), [])           # touche août : pas d'écart
        self.assertEqual(_comparable(["2026-09", "2026-10"]), ["2026-09", "2026-10"])
        self.assertEqual(_comparable([]), [])

    def test_site_single_month_and_scope(self):
        b = api().get("/api/fuel-tracking/bi/sites/DKR_0012/", {"month": "2026-09"}).json()
        self.assertTrue(b["mois_unique"])
        self.assertEqual(b["months"], ["2026-09"])
        self.assertIsNone(b["deltas"]["conso_mesuree_l"])
        self.assertEqual(api().get("/api/fuel-tracking/bi/sites/BKO_0012/").status_code, 404)
        self.assertEqual(api().get("/api/fuel-tracking/bi/sites/INCONNU/").status_code, 404)

    def test_sources_for_one_site(self):
        rows = [{"site_id": "DKR_0012", "country": "Senegal", "correspondance": {"utilise": False}, "modele_ge": "X", "puissance_nominale_kva": 20,
                 "stock": {"initial_l": 500, "source_initial": "Closing", "entrees_l": 200, "final_l": 400, "source_final": "Capteur",
                           "variation_l": 300, "statut": "VARIATION_CALCULEE", "motif": None, "action": None},
                 "closing": {"conso_l": 310}, "target": {"periode_l": 250, "mensuel_l": 250, "statut": "OK"},
                 "instrumentee": {"conso_l": None, "jours_mesure": 3, "jours": 31, "couverture_pct": 9.7, "disponible": False},
                 "cph": {"complete_l": None, "partielle_l": 120, "statut": "PARTIEL", "runtime_h": 40, "cph_l_h": 3, "charge_pct": 30, "motif_code": "AUTRE"},
                 "certification": {"valeur_l": None}},
                {"site_id": "DKR_0120", "country": "Senegal", "correspondance": {"utilise": True}}]
        build = {"rows": rows, "periode": {"sites_avec_bilan": 1}, "fraicheur": {}}

        def fake_kpis(rs):  # mêmes règles que conso_comparison._kpis, réduites aux 4 mesures
            tot = lambda vals: {"total_l": sum(vals) if vals else None, "sites": len(vals)}  # noqa: E731
            return {"sites": len(rs), "variation": {**tot([r["stock"]["variation_l"] for r in rs if r["stock"]["variation_l"] is not None]), "negatives": 0},
                    "closing": tot([r["closing"]["conso_l"] for r in rs if r["closing"]["conso_l"] is not None]),
                    "instrumentee": tot([r["instrumentee"]["conso_l"] for r in rs if r["instrumentee"]["disponible"]]),
                    "cph_complete": tot([r["cph"]["complete_l"] for r in rs if r["cph"]["complete_l"] is not None]),
                    "cph_partielle": tot([r["cph"]["partielle_l"] for r in rs if r["cph"]["partielle_l"] is not None])}
        with mock.patch("fuel_tracking.services.conso_comparison.build", return_value=build), \
                mock.patch("fuel_tracking.services.conso_comparison._kpis", side_effect=fake_kpis):
            b = api().get("/api/fuel-tracking/bi/conso/sources/", {"month": "2026-10", "site": "dkr_0012"}).json()
        self.assertEqual(b["sites"], 1)                                     # le site seul, même hors fichier Closing
        self.assertEqual(b["kpis"]["variation"]["total_l"], 300)
        self.assertIsNone(b["kpis"]["instrumentee"]["total_l"])             # couverture insuffisante : non calculable, pas 0
        self.assertEqual(b["site"]["cph"]["statut"], "PARTIEL")
        self.assertIn("non calculable", b["explain"]["cph_complete"]["note"])
