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
