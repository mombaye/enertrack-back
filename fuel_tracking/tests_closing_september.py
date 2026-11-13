"""Closing mensuel format 2026 : toutes les lignes du fichier, mois comptable du fichier, contrôle des totaux, target FI, vols, statuts."""
import os
from datetime import date, datetime
from unittest import mock

from django.test import TestCase

from core.models import Site
from fuel_tracking.models import FuelClosingLine, FuelSiteTarget
from fuel_tracking.services import closing_lines as CL
from fuel_tracking.services import conso_comparison as CC
from fuel_tracking.services import fuel_target as TG
from fuel_tracking.services import stock_import_pipeline as P
from fuel_tracking.services.stock_file_analysis import closing_stock_rows
from fuel_tracking.tests_stock_registry import TmpRootMixin, xlsx

HEAD = {"C": "ID du site", "E": "Région O&M", "J": "On GRID /\n Offgrid", "W": "Nombre de GE installé\n[Ex: 1,2,3 …]", "AS": "Date de Relevé Initiale",
        "AT": "Qtité laissée à la date du relevé  ", "BF": "Total Livraison", "BG": "Qté Rajoutée ou Retirée de la cuve par les techniciens d' un site à l' autre",
        "CA": "Qté Volée", "CB": "Qté vidangée (Fuel de mauvaise qualité) / \nRetrait carburant client / \nFuite / \nEcart Conso Théorique Vs Conso Variation de Stock",
        "CD": "Date de Relevé Finale", "CJ": "Qtité laissée à la date du relevé  ", "EI": "Estimation Conso Fuel Sensor sur la période",
        "EJ": "Qtité consommée sur la période de relevé niveau Fuel (L)", "EK": "Qtité cons considéréé  (L)", "EQ": "Nbre Jour cons",
        "EZ": "Estimation Consommation Mois de SEPTEMBRE 2026", "FH": "Fuel target  [L/jr] ", "FI": "Fuel target  [L/mois] ",
        "DI": "Analysis #1: temps de fonctionnement GE supérieur à l' indisponibilité GRID  ?", "EH": "Analysis #2: Test conformité donées de stock  Fuel  ?",
        "EL": "Analysis #3: Grande différence entre la Conso observée et à la  Conso Théorique  ?"}


def col(n):
    v = 0
    for ch in n:
        v = v * 26 + ord(ch) - 64
    return v


def serial(d):
    return (datetime(d.year, d.month, d.day) - datetime(1899, 12, 30)).days


def line(site, region="DKR", grid="Off-Grid", ge=1, ini=1000, liv=500, vol=0, vid=0, fin=800, d_fin=date(2026, 9, 26), fi=300, an=("Non", 0, "Oui"), conso=None):
    """Ligne cohérente avec la formule du fichier : EK = AT + BF − CA − CB − CJ (BF contient déjà le rajout BG)."""
    ek = ini + liv - vol - vid - fin if conso is None else conso
    return {"C": site, "E": region, "J": grid, "W": ge, "AS": serial(date(2026, 8, 31)), "AT": ini, "BF": liv, "BG": 0, "CA": vol, "CB": vid, "CD": serial(d_fin),
            "CJ": fin, "EI": 0, "EJ": ek, "EK": ek, "EQ": 26, "EZ": ek, "FH": None if fi is None else fi / 30, "FI": fi, "DI": an[0], "EH": an[1], "EL": an[2]}


def workbook(lines, kpi_conso=None, kpi_vole=None):
    width = col("FI")
    rows = [[None] * width for _ in range(15)]
    # bloc du haut : « Dernier Jour du mois de SEPTEMBRE » (étiquette puis date) et KPI du fichier (valeur puis libellé)
    rows[7][7], rows[7][9] = "Dernier Jour du mois de SEPTEMBRE", serial(date(2026, 9, 30))
    rows[8][7], rows[8][9] = "Dernier Jour du mois de AOUT", serial(date(2026, 8, 31))
    total = sum(x["EK"] for x in lines)
    rows[8][4], rows[8][5] = kpi_conso if kpi_conso is not None else total, "Quantité consommée entre les dates de relevés"
    rows[6][2], rows[6][3] = sum(x["AT"] for x in lines), "Quantité laissée dans les cuves aux dates de relevés de AOUT 2026"
    rows[6][4], rows[6][5] = sum(x["CJ"] for x in lines), "Quantité laissée dans les cuves aux dates de relevés de SEPTEMBRE 2026"
    rows[9][4], rows[9][5] = kpi_vole if kpi_vole is not None else sum(x["CA"] for x in lines), "Probable quantité volée"
    head = [None] * width
    for k, v in HEAD.items():
        head[col(k) - 1] = v
    rows.append(head)
    for x in lines:
        r = [None] * width
        for k, v in x.items():
            r[col(k) - 1] = (serial(v) if isinstance(v, date) else v)
        rows.append(r)
    return xlsx({"Synthese conso fuel": rows})


LINES = [
    line("SIT_0001", ini=1000, liv=500, vol=50, vid=20, fin=800, fi=300, d_fin=date(2026, 9, 26)),
    line("SIT_0002", ini=2000, liv=0, fin=1500, fi=400, d_fin=date(2026, 9, 27)),
    line("SIT_0003", ini=900, liv=700, fin=400, fi=200, d_fin=date(2026, 10, 3)),                  # relevé final début octobre : appartient quand même au Closing de septembre
    line("SIT_0004", ini=3000, liv=1000, fin=1000, fi=500, d_fin=date(2026, 10, 12)),              # idem
    line("ZZZ_9999", ini=100, liv=50, fin=60, fi=100),                                              # site inconnu de Gestion des sites : conservé, compté
    line("SIT_0001", ini=10, liv=0, fin=5, fi=999),                                                 # identifiant en double : conservé comme dans le fichier
    line("SIT_0005", region=None, ini=400, liv=100, fin=300, fi=None),                              # sans région ni target
]


class ClosingSeptemberTests(TmpRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        for sid in ("SIT_0001", "SIT_0002", "SIT_0003", "SIT_0004", "SIT_0005"):
            Site.objects.create(site_id=sid, name=sid)

    def imp(self, lines=LINES, name="Esco Senegal - Synthèse Conso Fuel Septembre 2026.xlsx", **kw):
        return P.process_bytes(workbook(lines, **kw), name, "CLOSING", origin="UPLOAD")

    def test_all_lines_kept_and_attached_to_file_month_not_to_each_site_date(self):
        b = self.imp()
        total = round(sum(x["EK"] for x in LINES), 1)
        rows = CC.closing_file_lines(date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(len(rows), len(LINES))                                             # aucune ligne perdue (inconnue, doublon, sans région…)
        self.assertEqual(round(sum(x["conso_l"] for x in rows), 1), total)                 # = Σ EK exact du fichier
        self.assertEqual(FuelClosingLine.objects.filter(batch=b, mois="2026-09").count(), len(LINES))
        self.assertEqual(CC.closing_file_lines(date(2026, 10, 1), date(2026, 10, 31)), None)   # rien n'est reporté sur octobre
        fin = {x["site"]: x["fin"] for x in rows if x["site"] == "SIT_0003"}
        self.assertEqual(fin["SIT_0003"], "2026-10-03")                                     # la date de relevé est conservée, le mois est celui du fichier

    def test_report_counters_and_nothing_silently_dropped(self):
        b = self.imp()
        s = b.summary["lignes_fichier"]
        self.assertEqual((s["mois"], s["lignes_lues"], s["importees"]), ("2026-09", 7, 7))
        self.assertEqual((s["id_non_reconnus"], s["id_en_double"], s["sans_region"]), (1, 1, 1))
        self.assertIn("ZZZ_9999", s["non_reconnus_exemples"])

    def test_control_totals_ok_and_warning_when_file_kpi_differs(self):
        ok = self.imp().summary["lignes_fichier"]["controle"]
        self.assertTrue(ok["ok"])
        self.assertTrue(all(c["ok"] for c in ok["totaux"]))
        bad = self.imp(lines=LINES + [line("SIT_0002", ini=5, liv=5, fin=0)], kpi_conso=1.0, name="autre.xlsx")
        ctrl = bad.summary["lignes_fichier"]["controle"]
        self.assertFalse(ctrl["ok"])
        self.assertTrue(ctrl["avertissement"])
        self.assertTrue(bad.summary.get("avertissements"))

    def test_target_fi_imported_first_occurrence_and_join_is_normalized(self):
        self.imp()
        t = {x.site_id: float(x.target_mensuel_l) for x in FuelSiteTarget.objects.filter(is_active=True, source_target="CLOSING_AKT_INV")}
        self.assertEqual(t["SIT_0001"], 300.0)                                              # première occurrence, pas le doublon (999)
        self.assertNotIn("SIT_0005", t)                                                     # pas de target dans le fichier : aucun target inventé
        self.assertIn("ZZZ_9999", t)                                                        # site inconnu conservé sous son identifiant brut
        self.assertIs(TG.active_targets()[TG.norm_site(" sit _0001 ")].site_id == "SIT_0001", True)
        tg = FuelSiteTarget.objects.get(site_id="SIT_0002", is_active=True)
        self.assertTrue(TG.confirmed_for(tg, date(2026, 9, 1), date(2026, 9, 30)))          # target du fichier du mois : pas « historique à confirmer »
        self.assertFalse(TG.confirmed_for(tg, date(2025, 9, 1), date(2025, 9, 30)))

    def test_synthese_vols_vidange_variation_target_and_statuses(self):
        self.imp()
        s = CL.synthese("2026-09")
        self.assertTrue(s["disponible"])
        self.assertEqual(s["conso_closing"]["total_l"], round(sum(x["EK"] for x in LINES), 1))
        self.assertEqual((s["vols"]["total_l"], s["vols"]["sites_concernes"]), (50.0, 1))
        self.assertEqual((s["vidange_fuite_ecart"]["total_l"], s["vidange_fuite_ecart"]["sites_concernes"]), (20.0, 1))
        st = s["stock"]
        self.assertEqual(st["variation_stock_l"], round(st["initial_l"] + st["livraisons_l"] - st["final_l"], 1))     # AT + BF − CJ
        self.assertEqual(st["delta_stock_l"], round(st["initial_l"] - st["final_l"], 1))
        self.assertEqual(s["target"]["sites"], 6)
        self.assertEqual(s["target"]["tolerance_pct"], 10)
        self.assertEqual(s["statuts"][2]["code"], "3")
        self.assertEqual(s["statuts"][2]["valeurs"][0]["valeur"], "Oui")
        self.assertEqual(s["statuts"][2]["a_verifier"], 7)                                  # « Oui » = à vérifier ; « Non » / « ok » / « n.a » ne signalent rien

    def test_missing_month_gives_reason_and_action_not_a_zero(self):
        s = CL.synthese("2026-09")
        self.assertFalse(s["disponible"])
        self.assertIn("Aucun fichier Closing importé", s["motif"])
        self.assertEqual(s["action"]["cible"], "suivi-stock/imports")

    def test_api_endpoint(self):
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        self.imp()
        u = get_user_model().objects.create_user(username="m", password="x", role="manager") if "role" in [f.name for f in get_user_model()._meta.fields] else get_user_model().objects.create_user(username="m", password="x")
        c = APIClient()
        c.force_authenticate(u)
        r = c.get("/api/fuel-tracking/bi/closing/synthese/?month=2026-09")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["lignes"]["lues"], 7)
        self.assertEqual(c.get("/api/fuel-tracking/bi/closing/synthese/?month=bad").status_code, 400)

    def test_formula_total_livraison_does_not_double_count_rajout(self):
        data, diag = parse(workbook([line("SIT_0001", ini=1000, liv=500, vol=50, vid=20, fin=800)]))
        r = data[0]
        self.assertEqual(r["total_livraison_l"], 500.0)
        from fuel_tracking.services.stock_file_analysis import closing_formula
        calc, ref, coherent = closing_formula(r)
        self.assertEqual((calc, ref, coherent), (630.0, 630.0, True))
        self.assertEqual(diag["mois_comptable"], "2026-09")


def parse(xlsx_bytes):
    from fuel_tracking.services import stock_import_files as F

    for _n, rows in F.iter_sheets(xlsx_bytes, "x.xlsx", only={"Synthese conso fuel"}):
        return closing_stock_rows(rows)


class RealSeptemberFileTest(TestCase):
    """Valeurs de référence du fichier réel (non-régression). Actif seulement si FUEL_TEST_CLOSING_FILE pointe vers le .xlsb de septembre 2026."""

    @mock.patch.dict(os.environ, {}, clear=False)
    def test_reference_totals(self):
        path = os.environ.get("FUEL_TEST_CLOSING_FILE")
        if not path or not os.path.exists(path):
            self.skipTest("FUEL_TEST_CLOSING_FILE non défini")
        from fuel_tracking.services import stock_import_files as F

        data = diag = None
        for _n, rows in F.iter_sheets(open(path, "rb").read(), path, only={"Synthese conso fuel"}):
            data, diag = closing_stock_rows(rows)
        s = lambda k: round(sum(float(r[k]) for r in data if r.get(k) is not None), 1)  # noqa: E731
        self.assertEqual(diag["mois_comptable"], "2026-09")
        self.assertEqual((len(data), len({r["site"] for r in data})), (3378, 3370))   # 8 identifiants en double (3 ne diffèrent que par des espaces)
        self.assertAlmostEqual(s("conso_closing_l"), 103354.9, delta=0.2)      # = bloc KPI du fichier (103 354,85), toutes lignes
        self.assertAlmostEqual(s("stock_initial_l"), 400817.7, delta=0.2)
        self.assertAlmostEqual(s("stock_final_l"), 394244.2, delta=0.2)
        self.assertAlmostEqual(s("total_livraison_l"), 130695.9, delta=0.2)
        self.assertAlmostEqual(s("vole_l"), 11822.2, delta=0.2)
        self.assertAlmostEqual(s("vidange_l"), 22092.3, delta=0.2)
        self.assertAlmostEqual(s("target_mois_l"), 117381.2, delta=0.2)
        inside = sum(float(r["conso_closing_l"]) for r in data if r["date_finale"] and r["date_finale"].month == 9 and r["conso_closing_l"] is not None)
        self.assertAlmostEqual(inside, 59146.1, delta=0.2)                     # l'ancien filtre par date de relevé donnait 59 146 L


class ClosingVariationConsistencyTests(TmpRootMixin, TestCase):
    """La variation de stock EnerTrack d'un site doit retrouver la consommation du fichier (EK) quand les stocks viennent du même Closing."""

    def test_variation_equals_file_consumption_with_closing_deliveries(self):
        from fuel_tracking.services import stock_period_balance as SB
        from fuel_tracking.services.period_utils import day_bounds

        Site.objects.create(site_id="SIT_0001", name="SIT_0001")
        P.process_bytes(workbook([line("SIT_0001", ini=1000, liv=500, vol=50, vid=20, fin=800, d_fin=date(2026, 9, 28))]), "Closing Septembre 2026.xlsx", "CLOSING", origin="UPLOAD")
        s, e = day_bounds(date(2026, 9, 1), date(2026, 9, 30))
        rows = {r["site_id"]: r for r in SB.compute_balances(s, e, site_ids=["SIT_0001"])}
        r = rows["SIT_0001"]
        self.assertEqual(float(r["stock_initial_l"]), 1000.0)
        self.assertEqual(float(r["stock_final_l"]), 800.0)
        self.assertEqual(float(r["variation_stock_l"]), 630.0)                      # 1000 + 500 − 50 − 20 − 800 = EK du fichier
        self.assertIn("LIVRAISON_CLOSING_ABSENTE_ENOC", " ".join(r.get("alertes") or []))   # livraisons issues du Closing, signalées « avec réserve »
