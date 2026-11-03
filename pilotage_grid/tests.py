import io
import json
import zipfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from pilotage_grid.models import PilotageDataset
from pilotage_grid.services import html_extract as X
from pilotage_grid.services import store as S

U = get_user_model()

PAIEMENTS = {"lists": {"st": ["Payée"]}, "cube": [[1, 2]], "det": [[1]], "hors": []}
TARIFS = {"rows": [["A"]], "comp": [], "bands": [], "inv": []}


def tiny_html():
    sites = {"months": ["2026-01"], "sites": [["S1"]], "ser": [], "con": [], "reps": [], "gp": [], "avg": [], "pm": [], "srcs": [], "corr": {"today": "2026-01-02"}}
    dem = {"months": ["2026-01"], "sites": [["S1"]], "miss": [], "nokey": 0, "nrows": 1, "nuniq": 1, "ndup": 0}
    eff = {"months": ["2026-01"], "cats": ["a"], "sites": [["S1"]], "nanalyse": 1, "nbdd": 1}
    cos = {"mon": {"S1": {"1": [0.9, 0]}}, "inv": []}
    red = {"sites": [["S1"]], "det": [], "mon": [], "st": [], "ctl": [], "ch6": []}
    j = lambda o: json.dumps(o, ensure_ascii=False)  # noqa: E731
    script = "\n".join([
        f"const P={j(PAIEMENTS)};const Q={j(sites)};",
        "const x=P.lists;",
        f"const DEM=(()=>{{const D={j(dem)};const M=D.months;return {{}}}})();",
        f"const EFF=(()=>{{const D={j(eff)};return {{}}}})();",
        f"const TAR=(()=>{{const D={j(TARIFS)};return {{}}}})();",
        f"const COS={j(cos)};",
        f"const D={j(red)};",
    ])
    return ('<!doctype html><html><body><!DOCTYPE html><html lang="fr"><head><title>t</title>'
            '<link rel="preconnect" href="https://fonts.googleapis.com">'
            '<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script></head><body>'
            f'<header class="top"></header><script>{script}</script></body></html></body></html>')


class HtmlExtractTests(TestCase):
    def test_extract_all_seven_datasets(self):
        ds = X.extract_datasets(tiny_html())
        self.assertEqual(set(ds), set(S.SPECS))
        self.assertEqual(ds["paiements"], PAIEMENTS)
        for k, v in ds.items():
            S.validate(k, v)

    def test_template_has_no_embedded_data_and_no_external_resources(self):
        t = X.build_template(tiny_html())
        self.assertIn("__PG.paiements", t)
        self.assertIn("__PG.redevance", t)
        self.assertNotIn('"cube"', t)
        self.assertNotIn("googleapis", t)
        self.assertNotIn("cdnjs", t)
        self.assertIn('src="chart.umd.min.js"', t)
        self.assertIn("window.__pgStart", t)

    def test_missing_dataset_is_reported(self):
        with self.assertRaises(X.ExtractError):
            X.extract_datasets(tiny_html().replace("const COS=", "const XXX="))


class StoreTests(TestCase):
    def test_versioning_and_unchanged(self):
        d1, c1 = S.save_version("paiements", PAIEMENTS)
        d2, c2 = S.save_version("paiements", PAIEMENTS)
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(d1.pk, d2.pk)
        changed = {**PAIEMENTS, "cube": [[9, 9]]}
        d3, c3 = S.save_version("paiements", changed)
        self.assertTrue(c3)
        self.assertEqual(d3.version, 2)
        self.assertEqual(PilotageDataset.objects.filter(key="paiements", is_active=True).count(), 1)
        self.assertFalse(PilotageDataset.objects.get(pk=d1.pk).is_active)

    def test_validation_rejects_bad_and_empty(self):
        with self.assertRaises(S.DatasetError):
            S.save_version("paiements", {"cube": []})
        with self.assertRaises(S.DatasetError):
            S.save_version("paiements", {**PAIEMENTS, "lists": {}})   # bloc vide : écraserait la base
        with self.assertRaises(S.DatasetError):
            S.save_version("inconnu", PAIEMENTS)

    def test_restore_creates_new_active_copy(self):
        S.save_version("paiements", PAIEMENTS)
        S.save_version("paiements", {**PAIEMENTS, "cube": [[9]]})
        r = S.restore_version("paiements", 1)
        self.assertEqual(r.version, 3)
        self.assertEqual(r.data, PAIEMENTS)
        self.assertEqual(PilotageDataset.objects.filter(key="paiements").count(), 3)   # historique conservé


class ApiTests(TestCase):
    def client_for(self, role):
        c = APIClient()
        c.force_authenticate(U.objects.create_user(f"u_{role}", f"{role}@x.sn", "p", role=role))
        return c

    def test_read_list_dataset_etag_and_export(self):
        S.save_version("paiements", PAIEMENTS)
        c = self.client_for("analyst")
        lst = c.get("/api/pilotage-grid/datasets/").json()
        self.assertFalse(lst["can_import"])
        self.assertTrue(next(d for d in lst["datasets"] if d["key"] == "paiements")["disponible"])
        r = c.get("/api/pilotage-grid/datasets/paiements/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content), PAIEMENTS)
        self.assertEqual(c.get("/api/pilotage-grid/datasets/paiements/", HTTP_IF_NONE_MATCH=r["ETag"]).status_code, 304)
        self.assertEqual(c.get("/api/pilotage-grid/datasets/tarifs/").status_code, 404)        # pas encore importé
        self.assertEqual(c.get("/api/pilotage-grid/datasets/nimporte/").status_code, 404)
        z = zipfile.ZipFile(io.BytesIO(c.get("/api/pilotage-grid/export/").content))
        self.assertEqual(sorted(z.namelist()), ["manifest.json", "paiements.json"])

    def test_import_roles_formats_and_idempotence(self):
        up = lambda name, payload: SimpleUploadedFile(name, payload if isinstance(payload, bytes) else json.dumps(payload).encode(), content_type="application/json")  # noqa: E731
        analyst, manager = self.client_for("analyst"), self.client_for("manager")
        self.assertEqual(analyst.post("/api/pilotage-grid/datasets/tarifs/import/", {"file": up("t.json", TARIFS)}, format="multipart").status_code, 403)
        r = manager.post("/api/pilotage-grid/datasets/tarifs/import/", {"file": up("t.json", TARIFS)}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["dataset"]["version"], 1)
        again = manager.post("/api/pilotage-grid/datasets/tarifs/import/", {"file": up("t.json", TARIFS)}, format="multipart")
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()["unchanged"])
        xl = manager.post("/api/pilotage-grid/datasets/tarifs/import/", {"file": up("t.xlsx", b"PK")}, format="multipart")
        self.assertEqual(xl.status_code, 400)
        self.assertIn("Excel", xl.json()["detail"])
        wrong = manager.post("/api/pilotage-grid/datasets/tarifs/import/", {"file": up("t.json", {"key": "paiements", "data": PAIEMENTS})}, format="multipart")
        self.assertEqual(wrong.status_code, 400)
        env = manager.post("/api/pilotage-grid/datasets/paiements/import/", {"file": up("p.json", {"key": "paiements", "data": PAIEMENTS})}, format="multipart")
        self.assertEqual(env.status_code, 201)
        vers = manager.get("/api/pilotage-grid/datasets/tarifs/versions/").json()["versions"]
        self.assertEqual(len(vers), 1)
        self.assertEqual(vers[0]["imported_by"], "manager@x.sn")

    def test_bo_role_has_no_access(self):
        self.assertEqual(self.client_for("bo").get("/api/pilotage-grid/datasets/").status_code, 403)


def calc_fixtures():
    paiements = {
        "lists": {"st": ["Payée", "Annulée", "Impayée", "Hors", "Sans"], "por": ["AKTIVCO", "SONATEL"], "ech": ["", "2026-02"], "cpt": ["2026-01", "2026-02", "2026-03"],
                  "reg": ["DKR", "KLK"], "tar": ["PMP", "MTLU"]},
        # [statut, porteur, échéance, mois comptable, région, tarif, nb, ttc, ht]
        "cube": [[0, 0, 1, 1, 0, 0, 10, 1000, 800], [2, 1, 0, 1, 0, 0, 2, 300, 250], [0, 1, 1, 2, 1, 1, 5, 500, 400], [0, 0, 1, 0, 0, 0, 99, 9999, 9]],
        # [n° facture, contrat, site, nom, date, ttc, statut, porteur, localisation, région, tarif]
        "det": [[111, 222, "S1", "Site un", "2026-02-10", 300, 2, "SONATEL", "Dakar", "DKR", "PMP"], [112, 223, "S2", "Site deux", "2026-03-05", 50, 2, "AKTIVCO", "Kaolack", "KLK", "MTLU"]],
        "hors": [],
    }
    months = [f"2026-{m:02d}" for m in range(1, 7)] + ["2026-07"]
    jours = [31, 28, 31, 30, 31, 30, 10]
    ser = lambda base: [0, [base * j for j in jours], [base * j * 2 for j in jours], [base * j for j in jours], [0] * 7, jours, [0] * 7]  # noqa: E731
    sites = {
        "months": months,
        "sites": [["S1", "Site un", "DKR", "PMP", "BT", 10.0, 0, "x", "x", "c1", "AKTIVCO", 222, 1, "ok", 0.0, False, "2026-06-30", 6, 0],
                  ["S2", "Site deux", "KLK", "MTLU", "MT", 20.0, 0, "x", "x", "c2", "SONATEL", 223, 1, "ok", 0.0, False, "2026-06-30", 6, 0]],
        "ser": [ser(100), ser(30)], "con": [[], []], "reps": [["S2"]], "gp": [None, None], "avg": [None, None], "pm": [], "srcs": [], "corr": {"today": "2026-07-05"},
    }
    return paiements, sites


class CalculsTests(TestCase):
    def setUp(self):
        p, s = calc_fixtures()
        S.save_version("paiements", p)
        S.save_version("sites", s)

    def test_tables_follow_active_version(self):
        from pilotage_grid.models import PgInvoice, PgInvoiceFact, PgSite, PgSiteMonth
        self.assertEqual((PgInvoiceFact.objects.count(), PgInvoice.objects.count(), PgSite.objects.count(), PgSiteMonth.objects.count()), (4, 2, 2, 14))
        p, s = calc_fixtures()
        p["cube"].append([0, 0, 1, 1, 0, 0, 1, 1, 1])
        S.save_version("paiements", p)                      # un import met les tables à jour
        self.assertEqual(PgInvoiceFact.objects.count(), 5)

    def test_synthese_totals_filters_and_rule_of_accounting_month(self):
        from pilotage_grid.services import calculs as C
        m = C.months_list()
        # période 2026-01 → 2026-03 : le cube est filtré sur le mois COMPTABLE 2026-02 → 2026-04 (règle d'origine)
        s = C.synthese(m, {"from": "2026-01", "to": "2026-03"})
        self.assertEqual(s["kpis"]["ttc"], 1000 + 300 + 500)                 # la ligne du mois comptable 2026-01 est exclue
        self.assertEqual(s["statuts"][0]["ttc"], 1500)
        self.assertEqual(s["kpis"]["nb_factures"], 17)
        self.assertEqual(round(s["pct_paye"], 1), round(1500 / 1800 * 100, 1))
        mt = C.synthese(m, {"from": "2026-01", "to": "2026-03", "tension": "MT"})
        self.assertEqual(mt["kpis"]["ttc"], 500)
        self.assertEqual(mt["kpis"]["avec_changement_contrat"], 1)
        self.assertEqual(C.synthese(m, {"from": "2026-01", "to": "2026-03", "porteur": "AKTIVCO"})["kpis"]["ttc"], 1000)

    def test_synthese_consumption_series_variations_and_costs(self):
        from pilotage_grid.services import calculs as C
        m = C.months_list()
        s = C.synthese(m, {"from": "2026-01", "to": "2026-06"})
        self.assertEqual([x["month"] for x in s["mensuel"]], m[:6])
        self.assertEqual(s["mensuel"][0]["k1"], (100 + 30) * 31)
        self.assertEqual(s["kpis"]["sites_factures"], 2)
        self.assertEqual(round(s["kpis"]["cout_kwh"], 3), round(sum(x["ht"] for x in s["mensuel"]) / sum(x["k1"] + x["k2"] for x in s["mensuel"]), 3))
        self.assertEqual([c["site"] for c in s["couts"]], ["S1"])                  # S2 : 30 kWh/j < 1 000 kWh/mois ? 30×30,4 = 913 → exclu
        self.assertEqual({v["site"]: v["d"] for v in s["variations"]}, {"S1": 0.0, "S2": 0.0})   # consommation stable : écart nul

    def test_paiements_sections_and_invoices(self):
        from pilotage_grid.services import calculs as C
        m = C.months_list()
        p = C.paiements(m, {"from": "2026-01", "to": "2026-03"})
        self.assertEqual(p["kpis"]["nb"], 17)
        self.assertEqual(p["echeance"]["labels"], ["2026-02"])
        self.assertEqual({x["porteur"] for x in p["echeance"]["series"]}, {"AKTIVCO", "SONATEL"})
        self.assertEqual(sorted(p["regions"]["labels"]), ["DKR", "KLK"])
        n = C.paiements(m, {"from": "2026-01", "to": "2026-03", "metric": "nb"})
        self.assertEqual(n["kpis"]["nb"], 17)
        f = C.factures(m, {"from": "2026-01", "to": "2026-03", "statuts": "2"})
        self.assertEqual((f["total"], f["ttc"]), (2, 350))
        self.assertEqual(C.factures(m, {"from": "2026-01", "to": "2026-03", "statuts": "2", "q": "kaolack"})["total"], 1)

    def test_api_endpoints_and_empty_state(self):
        c = APIClient()
        c.force_authenticate(U.objects.create_user("calc", "calc@x.sn", "p", role="analyst"))
        self.assertEqual(c.get("/api/pilotage-grid/facets/").json()["default_from"], "2026-01")
        self.assertEqual(c.get("/api/pilotage-grid/synthese/", {"from": "2026-01", "to": "2026-03"}).status_code, 200)
        self.assertEqual(c.get("/api/pilotage-grid/paiements/").status_code, 200)
        self.assertEqual(c.get("/api/pilotage-grid/paiements/factures/", {"statuts": "2"}).json()["total"], 2)
        from pilotage_grid.models import PgSiteMonth
        PgSiteMonth.objects.all().delete()
        self.assertEqual(c.get("/api/pilotage-grid/synthese/").status_code, 404)
