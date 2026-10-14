"""
Recette du registre unifié de stock carburant (Phase 1) : conversion cuves, fichiers Loliforce / SCSS,
contrôles, périmètre de gardiennage, comparaison, stock retenu, ENOC (réutilisation du connecteur),
historique refueling, pipeline du répertoire monté, piste d'audit et API.
"""
import io
import math
import shutil
import tempfile
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from pathlib import Path

import openpyxl
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Site
from fuel_tracking.models import (FuelEnocMovement, FuelGuardScope, FuelImportBatch, FuelImportRow, FuelMovement,
                                  FuelRegistryCorrection, FuelStockObservation, FuelTankMaster)
from fuel_tracking.services import stock_enoc as E
from fuel_tracking.services import stock_import_files as F
from fuel_tracking.services import stock_import_pipeline as P
from fuel_tracking.services import stock_registry as R
from fuel_tracking.services import tank_conversion as TC

HEAD = ["Site ID", "Site Name", "Catégorie", "Zone", "Ville", "MARQUE GE", "TYPE DE GE", "PUISSANCE GE", "Relevé en CM",
        "Relevé en litres", "Compteur horaire (h)", "Date de relevé", "Commentaires", "Site gardé par"]


def xlsx(sheets: dict) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def guard_row(site, cm=None, l=None, d=datetime(2026, 10, 3), guard="LOLIFORCE", name=None, hm=1200):
    return [site, name or f"NAME_{site}", "A_Ax_S6", "DAKAR", "DAKAR", "SDMO", "K22", 22, cm, l, hm, d, None, guard]


def tank(site, tank_id="1", validated=True, **kw):
    spec = dict(shape="HORIZONTAL_CYLINDER", length_m=Decimal("2.84"), diameter_m=Decimal("1.6"), capacity_l=Decimal("5710"))
    spec.update(kw)
    return FuelTankMaster.objects.create(site_id=site, tank_id=tank_id, validation_status="VALIDATED" if validated else "PENDING", **spec)


class TankConversionTests(TestCase):
    """Formules géométriques, priorité abaque, contrôles."""

    def spec(self, **kw):
        base = dict(site_id="S", tank_id="1", validated=True)
        base.update(kw)
        return TC.TankSpec(**base)

    def test_rectangular(self):
        t = self.spec(shape="RECTANGULAR", length_m=2.2, width_m=0.88, height_m=0.8, capacity_l=1548.8)
        r = TC.convert(20, "CM", [t])
        self.assertEqual((r.status, r.method), ("VALID", "GEOMETRIC_CALCULATION"))
        self.assertAlmostEqual(r.level_liters, 2.2 * 0.88 * 0.2 * 1000, places=2)

    def test_vertical_cylinder(self):
        t = self.spec(shape="VERTICAL_CYLINDER", diameter_m=1.2, height_m=2.0)
        r = TC.convert(50, "CM", [t])
        self.assertAlmostEqual(r.level_liters, math.pi * 0.6 ** 2 * 0.5 * 1000, places=2)

    def test_horizontal_cylinder_circular_segment(self):
        # Site ACAD (closing) : L 2,84 m, D 1,6 m, niveau 0,18 m → 353,18 L dans le fichier.
        t = self.spec(shape="HORIZONTAL_CYLINDER", length_m=2.84, diameter_m=1.6, capacity_l=5710.16)
        r = TC.convert(18, "CM", [t])
        self.assertAlmostEqual(r.level_liters, 353.18, delta=0.5)
        full = TC.convert(160, "CM", [t])
        self.assertAlmostEqual(full.level_liters, math.pi * 0.8 ** 2 * 2.84 * 1000, delta=0.5)
        half = TC.convert(80, "CM", [t])
        self.assertAlmostEqual(half.level_liters, full.level_liters / 2, delta=0.5)

    def test_horizontal_elliptical(self):
        t = self.spec(shape="HORIZONTAL_ELLIPTICAL", length_m=2.0, width_m=1.6, height_m=1.0)
        full = TC.convert(100, "CM", [t])
        self.assertAlmostEqual(full.level_liters, math.pi * 0.8 * 0.5 * 2.0 * 1000, delta=0.5)

    def test_validated_curve_has_priority_over_geometry(self):
        t = self.spec(shape="HORIZONTAL_CYLINDER", length_m=2.84, diameter_m=1.6,
                      calibration_curve=[[0, 0], [10, 100], [20, 300]], calibration_validated=True)
        r = TC.convert(15, "CM", [t])
        self.assertEqual(r.method, "TANK_CURVE")
        self.assertAlmostEqual(r.level_liters, 200.0)

    def test_unvalidated_curve_falls_back_to_geometry(self):
        t = self.spec(shape="RECTANGULAR", length_m=1, width_m=1, height_m=1,
                      calibration_curve=[[0, 0], [100, 5]], calibration_validated=False)
        r = TC.convert(10, "CM", [t])
        self.assertEqual(r.method, "GEOMETRIC_CALCULATION")
        self.assertAlmostEqual(r.level_liters, 100.0)

    def test_no_generic_coefficient_when_tank_unknown_or_unvalidated(self):
        r = TC.convert(20, "CM", [])
        self.assertEqual((r.level_liters, r.status, r.method), (None, "PENDING_TANK_MAPPING", "NONE"))
        r = TC.convert(20, "CM", [self.spec(shape="RECTANGULAR", length_m=1, width_m=1, height_m=1, validated=False)])
        self.assertEqual((r.level_liters, r.status), (None, "PENDING_TANK_MAPPING"))
        self.assertEqual(r.messages[0]["code"], "TANK_NOT_VALIDATED")

    def test_missing_dimensions(self):
        r = TC.convert(20, "CM", [self.spec(shape="RECTANGULAR", length_m=2.0, height_m=0.77)])
        self.assertEqual((r.level_liters, r.status, r.messages[-1]["code"]), (None, "PENDING_TANK_MAPPING", "MISSING_DIMENSIONS"))

    def test_negative_and_above_height_and_over_capacity(self):
        t = self.spec(shape="RECTANGULAR", length_m=1, width_m=1, height_m=1, capacity_l=500)
        self.assertEqual(TC.convert(-3, "CM", [t]).messages[0]["code"], "NEGATIVE_LEVEL")
        self.assertEqual(TC.convert(120, "CM", [t]).messages[0]["code"], "LEVEL_ABOVE_HEIGHT")
        over = TC.convert(60, "CM", [t])  # 600 L > 500 L + 5 %
        self.assertEqual((over.status, over.messages[-1]["code"]), ("INVALID", "OVER_CAPACITY"))
        within = TC.convert(52, "CM", [t])  # 520 L ≤ 525 L (tolérance 5 %)
        self.assertEqual(within.status, "VALID")
        self.assertEqual(TC.convert(530, "L", [t], tolerance_pct=5).status, "INVALID")
        self.assertEqual(TC.convert(530, "L", [t], tolerance_pct=10).status, "VALID")

    def test_multi_tank_without_id_never_tank_1(self):
        tanks = [self.spec(tank_id="1", shape="RECTANGULAR", length_m=1, width_m=1, height_m=1),
                 self.spec(tank_id="2", shape="RECTANGULAR", length_m=1, width_m=1, height_m=1)]
        r = TC.convert(20, "CM", tanks)
        self.assertEqual((r.level_liters, r.status, r.tank_id), (None, "PENDING_TANK_MAPPING", None))
        self.assertEqual(r.messages[0]["code"], "MULTI_TANK_WITHOUT_ID")
        r2 = TC.convert(20, "CM", tanks, tank_id="2")
        self.assertEqual((r2.status, r2.tank_id), ("VALID", "2"))
        self.assertEqual(TC.convert(100, "L", tanks).status, "PENDING_TANK_MAPPING")

    def test_direct_liters(self):
        r = TC.convert(140, "L", [self.spec(shape="RECTANGULAR", length_m=1, width_m=1, height_m=1, capacity_l=1000)])
        self.assertEqual((r.method, r.status, r.level_liters, r.messages), ("DIRECT_LITERS", "VALID", 140.0, []))
        r = TC.convert(140, "L", [])
        self.assertEqual(r.messages[0]["code"], "CAPACITY_NOT_VALIDATED")

    def test_unknown_unit_and_missing_level(self):
        self.assertEqual(TC.convert(10, "BIDON", []).messages[0]["code"], "UNKNOWN_UNIT")
        self.assertEqual(TC.convert(None, "CM", []).messages[0]["code"], "MISSING_LEVEL")


class LevelCellTests(TestCase):
    def test_cells(self):
        p = F.parse_level_cell
        self.assertEqual([(r.value, r.unit, r.tank_id) for r in p(26, "CM").readings], [(26.0, "CM", None)])
        self.assertEqual([(r.value, r.unit) for r in p("16,5CM", "CM").readings], [(16.5, "CM")])
        self.assertEqual([(r.value, r.unit) for r in p("100 L", "L").readings], [(100.0, "L")])
        self.assertEqual([(r.value, r.unit) for r in p("20L", "CM").readings], [(20.0, "L")])
        self.assertEqual([(r.value, r.tank_id) for r in p("Cuve 1=7 Cuve 2= 11", "CM").readings], [(7.0, "1"), (11.0, "2")])
        self.assertEqual([(r.value, r.tank_id) for r in p("C1: 8cm / C2: 24cm", "CM").readings], [(8.0, "1"), (24.0, "2")])
        multi = p("7 et 11", "CM")
        self.assertEqual([(r.value, r.tank_id) for r in multi.readings], [(7.0, None), (11.0, None)])
        self.assertEqual(multi.messages[0]["code"], "MULTI_TANK_WITHOUT_ID")
        self.assertEqual(p("Pas de jauge", "CM").error["code"], "MISSING_LEVEL")
        self.assertEqual(p("illisible", "CM").error["code"], "MISSING_LEVEL")
        self.assertEqual(p("3 Bidons 20L", "CM").error["code"], "UNKNOWN_UNIT")
        self.assertEqual(p(None, "CM").readings, [])


@override_settings(FUEL_CAPACITY_TOLERANCE_PCT=5)
class GuardFileImportTests(TestCase):
    """Fichiers Loliforce / SCSS : contrôles ligne à ligne et validation."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(FUEL_IMPORT_ROOT=self.tmp)
        self.override.enable()
        for sid, name in (("DKR_0001", "ALPHA"), ("DKR_0002", "BETA"), ("DKR_0003", "GAMMA"), ("DKR_0004", "DELTA")):
            Site.objects.create(site_id=sid, name=name)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_file(self, rows, source="LOLIFORCE", sheet="KOLDA", extra=None, name="f.xlsx"):
        sheets = {sheet: [["SEMAINE 40"], [], HEAD] + rows}
        sheets.update(extra or {})
        return P.process_bytes(xlsx(sheets), name, source, origin="UPLOAD")

    def row_codes(self, batch):
        return {r.row_number: [m["code"] for m in r.messages] for r in batch.rows.all()}

    def test_controls_unknown_site_missing_date_missing_level_unknown_unit(self):
        b = self.run_file([
            guard_row("XXX_9999", cm=10),
            guard_row("DKR_0001", cm=10, d=None),
            guard_row("DKR_0002", cm="Pas de jauge"),
            guard_row("DKR_0003", cm="3 Bidons 20L"),
        ])
        codes = self.row_codes(b)
        self.assertIn("UNKNOWN_SITE", codes[4])
        self.assertIn("MISSING_DATE", codes[5])
        self.assertIn("MISSING_LEVEL", codes[6])
        self.assertIn("UNKNOWN_UNIT", codes[7])
        self.assertEqual(b.rows_rejected, 4)
        self.assertEqual(FuelStockObservation.objects.count(), 0)
        self.assertEqual(b.status, "REJECTED")
        self.assertEqual(FuelImportRow.objects.filter(batch=b).count(), 4)  # lignes brutes conservées

    def test_cm_without_validated_tank_is_pending_null_liters(self):
        tank("DKR_0001", validated=False)
        self.run_file([guard_row("DKR_0001", cm=18)])
        o = FuelStockObservation.objects.get()
        self.assertIsNone(o.level_liters)
        self.assertEqual((o.conversion_status, o.validation_status), ("PENDING_TANK_MAPPING", "PENDING"))

    def test_negative_and_over_capacity_are_rejected_but_kept(self):
        tank("DKR_0001", shape="RECTANGULAR", length_m=1, width_m=1, height_m=1, capacity_l=500, diameter_m=None)
        FuelGuardScope.objects.create(site_id="DKR_0001", provider="LOLIFORCE", active_from="2026-01-01")
        self.run_file([guard_row("DKR_0001", cm=-5), guard_row("DKR_0001", l=900, d=datetime(2026, 10, 4))])
        obs = {o.level_raw: o for o in FuelStockObservation.objects.all()}
        self.assertEqual(obs["-5"].validation_status, "REJECTED")
        self.assertIn("négatif", obs["-5"].conversion_message)
        self.assertEqual((obs["900"].conversion_status, obs["900"].validation_status), ("INVALID", "REJECTED"))

    def test_in_scope_validated_tank_is_auto_validated(self):
        tank("DKR_0001")
        FuelGuardScope.objects.create(site_id="DKR_0001", provider="LOLIFORCE", active_from="2026-01-01")
        b = self.run_file([guard_row("DKR_0001", cm=18)])
        o = FuelStockObservation.objects.get()
        self.assertEqual((o.validation_status, o.conversion_method, o.guard_scope_status), ("VALIDATED", "GEOMETRIC_CALCULATION", "IN_SCOPE"))
        self.assertAlmostEqual(float(o.level_liters), 353.18, delta=0.5)
        self.assertEqual(b.status, "ACCEPTED")

    def test_scope_not_configured_and_provider_mismatch_are_pending_never_rejected(self):
        tank("DKR_0001")
        tank("DKR_0002")
        FuelGuardScope.objects.create(site_id="DKR_0002", provider="SCSS", active_from="2026-01-01")
        self.run_file([guard_row("DKR_0001", cm=18), guard_row("DKR_0002", cm=18)])
        o1 = FuelStockObservation.objects.get(site_id="DKR_0001")
        o2 = FuelStockObservation.objects.get(site_id="DKR_0002")
        self.assertEqual((o1.guard_scope_status, o1.validation_status), ("NOT_CONFIGURED", "PENDING"))
        self.assertEqual((o2.guard_scope_status, o2.validation_status), ("PROVIDER_MISMATCH", "PENDING"))
        self.assertIsNotNone(o2.level_liters)  # le relevé est conservé et converti, seulement à valider

    def test_multi_tank_cell_without_id_is_not_attributed(self):
        tank("DKR_0001", tank_id="1")
        tank("DKR_0001", tank_id="2")
        self.run_file([guard_row("DKR_0001", cm="7 et 11"), guard_row("DKR_0001", cm="C1=7 / C2=11", d=datetime(2026, 10, 4))])
        without = FuelStockObservation.objects.filter(observation_datetime__day=3)
        self.assertEqual(without.count(), 2)
        self.assertTrue(all(o.tank_id is None and o.level_liters is None for o in without))
        labelled = FuelStockObservation.objects.filter(observation_datetime__day=4).order_by("tank_id")
        self.assertEqual([o.tank_id for o in labelled], ["1", "2"])
        self.assertTrue(all(o.level_liters is not None for o in labelled))

    def test_duplicates_row_and_file(self):
        rows = [guard_row("DKR_0001", cm=18), guard_row("DKR_0001", cm=18)]
        b1 = self.run_file(rows)
        self.assertEqual(FuelStockObservation.objects.count(), 1)
        self.assertEqual(b1.rows_duplicate, 1)
        same = self.run_file(rows)  # même fichier → doublon technique
        self.assertEqual((same.status, same.duplicate_of_id), ("REJECTED", b1.pk))
        self.assertEqual(FuelStockObservation.objects.count(), 1)
        # fichier cumulatif (nouvelle semaine ajoutée) : seules les nouvelles lignes sont importées
        b3 = self.run_file(rows + [guard_row("DKR_0001", cm=15, d=datetime(2026, 10, 10))], name="f2.xlsx")
        self.assertEqual(FuelStockObservation.objects.count(), 2)
        self.assertEqual(b3.rows_duplicate, 2)

    def test_same_source_conflicting_value_goes_to_review(self):
        tank("DKR_0001")
        FuelGuardScope.objects.create(site_id="DKR_0001", provider="LOLIFORCE", active_from="2026-01-01")
        self.run_file([guard_row("DKR_0001", cm=18), guard_row("DKR_0001", cm=40)])
        statuses = sorted(FuelStockObservation.objects.values_list("validation_status", flat=True))
        self.assertEqual(statuses, ["PENDING", "VALIDATED"])

    def test_block_without_site_id_resolved_by_name_and_compile_sheet_skipped(self):
        no_id_head = HEAD[1:]
        rows = [["SEMAINE 41"], [], no_id_head, guard_row("x", cm=12, name="BETA")[1:]]
        b = P.process_bytes(xlsx({"KAOLACK": rows, "Compile": [HEAD, guard_row("DKR_0003", cm=5)]}), "g.xlsx", "LOLIFORCE", origin="UPLOAD")
        o = FuelStockObservation.objects.get()
        self.assertEqual(o.site_id, "DKR_0002")
        self.assertIn("Compile", b.summary["feuilles_ignorees"])

    def test_scss_template_and_cm_and_l(self):
        tank("DKR_0001", shape="RECTANGULAR", length_m=2, width_m=1, height_m=1, capacity_l=2000, diameter_m=None)
        FuelGuardScope.objects.create(site_id="DKR_0001", provider="SCSS", active_from="2026-01-01")
        head = HEAD[:8] + ["CONTACT AGENT"] + HEAD[8:]
        row = guard_row("DKR_0001", cm=10, l=200, guard="SCSS")
        row = row[:8] + ["77 000 00 00"] + row[8:]
        P.process_bytes(xlsx({"Remonte": [["TEMPLATE REMONTE CARURANT SCSS"], [], [], head, row]}), "scss.xlsx", "SCSS", origin="UPLOAD")
        o = FuelStockObservation.objects.get()
        self.assertEqual((o.source_type, o.level_unit, float(o.level_liters), o.validation_status), ("SCSS", "L", 200.0, "VALIDATED"))
        self.assertIn("10", o.comment)
        self.assertEqual(o.source_user, "77 000 00 00")


class ComparisonAndRetainedTests(TestCase):
    def setUp(self):
        Site.objects.create(site_id="DKR_0001", name="ALPHA")
        self.t = tank("DKR_0001", capacity_l=Decimal("2000"), shape="RECTANGULAR", length_m=2, width_m=1, height_m=1, diameter_m=None)
        self.now = datetime(2026, 10, 14, 12, tzinfo=dt_timezone.utc)

    def obs(self, source, liters, hours_ago, status="VALIDATED", tank_id="1", eid=None, site="DKR_0001"):
        return FuelStockObservation.objects.create(
            source_type=source, source_event_id=eid or f"{source}-{liters}-{hours_ago}-{tank_id}", site_id=site, tank_id=tank_id,
            observation_datetime=self.now - timedelta(hours=hours_ago), level_raw=str(liters), level_value=liters, level_unit="L",
            level_liters=liters, conversion_method="DIRECT_LITERS", conversion_status="VALID", validation_status=status)

    def test_coherent_within_tolerance(self):
        self.obs("LOLIFORCE", 500, 10)
        self.obs("SCSS", 680, 30)  # écart 180 ≤ max(100, 200)
        c = R.guard_comparisons("DKR_0001")
        self.assertEqual(c[0]["status"], R.COHERENT)
        self.assertEqual(c[0]["tolerance_l"], 200.0)

    def test_conflict_sets_both_pending_no_average(self):
        a = self.obs("LOLIFORCE", 500, 10)
        b = self.obs("SCSS", 900, 30)
        self.assertEqual(R.guard_comparisons("DKR_0001")[0]["status"], R.STOCK_CONFLICT)
        self.assertEqual(R.apply_guard_conflicts(["DKR_0001"]), 2)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.validation_status, b.validation_status), ("PENDING", "PENDING"))
        self.assertEqual(FuelRegistryCorrection.objects.filter(action="SYSTEM").count(), 2)
        ret = R.fuel_stock_retained(now=self.now)["DKR_0001"]
        self.assertIsNone(ret["stock_l"])  # ni moyenne (700) ni choix silencieux

    def test_single_tank_site_compares_even_if_one_reading_has_no_tank_id(self):
        self.obs("LOLIFORCE", 500, 10, tank_id=None)
        self.obs("SCSS", 900, 30, tank_id="1")
        self.assertEqual(R.guard_comparisons("DKR_0001")[0]["status"], R.STOCK_CONFLICT)

    def test_reconversion_after_tank_validation_sets_tank_id(self):
        Site.objects.create(site_id="DKR_0009", name="NINE")
        t = tank("DKR_0009", validated=False, shape="RECTANGULAR", length_m=1, width_m=1, height_m=1, capacity_l=1000, diameter_m=None)
        o = FuelStockObservation.objects.create(source_type="LOLIFORCE", source_event_id="x", site_id="DKR_0009",
                                                observation_datetime=self.now, level_raw="10", level_value=10, level_unit="CM",
                                                conversion_status="PENDING_TANK_MAPPING", validation_status="PENDING")
        R.correct_tank(t, {}, None, "ok", validate=True)
        R.reconvert_site_observations("DKR_0009")
        o.refresh_from_db()
        self.assertEqual((o.tank_id, float(o.level_liters)), ("1", 100.0))

    def test_not_comparable_beyond_48h_or_without_capacity(self):
        self.obs("LOLIFORCE", 500, 10)
        self.obs("SCSS", 900, 70)
        self.assertEqual(R.guard_comparisons("DKR_0001"), [])
        self.assertEqual(R.compare_pair(*FuelStockObservation.objects.all()[:2], None)["status"], R.NOT_COMPARABLE)

    def test_human_validated_observation_not_downgraded(self):
        a = self.obs("LOLIFORCE", 500, 10)
        a.validated_by = get_user_model().objects.create_user("m", "m@x.sn", "p", role="manager")
        a.save()
        self.obs("SCSS", 900, 30)
        R.apply_guard_conflicts(["DKR_0001"])
        a.refresh_from_db()
        self.assertEqual(a.validation_status, "VALIDATED")

    def test_retained_latest_validated_with_capacity_check(self):
        self.obs("ENOC", 700, 5, status="PENDING")
        self.obs("LOLIFORCE", 2500, 8)  # au-delà de la capacité validée → jamais retenu
        self.obs("LOLIFORCE", 600, 20)
        self.obs("SNOWFLAKE", 999, 1)
        r = R.fuel_stock_retained(now=self.now)["DKR_0001"]
        self.assertEqual((r["stock_l"], r["source_type"], r["age_hours"], r["fill_pct"]), (600.0, "LOLIFORCE", 20.0, 30.0))

    def test_retained_multi_tank_sum_and_incomplete(self):
        tank("DKR_0001", tank_id="2", capacity_l=Decimal("1000"), shape="RECTANGULAR", length_m=1, width_m=1, height_m=1, diameter_m=None)
        self.obs("LOLIFORCE", 600, 2, tank_id="1")
        r = R.fuel_stock_retained(now=self.now)["DKR_0001"]
        self.assertIsNone(r["stock_l"])
        self.assertIn("cuve(s) 2", r["unavailable_reason"])
        self.obs("LOLIFORCE", 300, 3, tank_id="2")
        r = R.fuel_stock_retained(now=self.now)["DKR_0001"]
        self.assertEqual((r["stock_l"], r["capacity_l"], r["tanks_count"]), (900.0, 3000.0, 2))

    def test_no_observation_site(self):
        self.assertNotIn("DKR_0001", R.fuel_stock_retained(now=self.now))


class EnocRegistryTests(TestCase):
    """ENOC : réutilisation de FuelEnocMovement (aucune nouvelle connexion), null ≠ 0, idempotence."""

    def setUp(self):
        Site.objects.create(site_id="DKR_0001", name="ALPHA")
        tank("DKR_0001", shape="RECTANGULAR", length_m=1, width_m=1, height_m=1, capacity_l=1000, diameter_m=None)
        self.when = timezone.now() - timedelta(days=1)

    def mk(self, code, status="done", operation=True, **raw_extra):
        op = {"request_code": code, "quantity_added_liters": 150, "operation_type": "AJOUT", **raw_extra} if operation else None
        raw = {"source_id": code, "status": status, "site_id": "dkr_0001", "quantity_added_liters": op and op["quantity_added_liters"],
               "request": {"request_code": code}, "operation": op}
        return FuelEnocMovement.objects.create(
            source_id=code, request_code=code, status=status, site_id="dkr_0001", operation_type="AJOUT", operation_date=self.when,
            created_at_source=self.when - timedelta(hours=2), level_before=Decimal("20"), level_before_unit="cm",
            level_after=Decimal("35"), level_after_unit="cm" if operation else None, quantity_added_liters=Decimal("0"),
            technician_name="Tech A", raw_payload=raw)

    def test_mapping_and_idempotence(self):
        self.mk("REQ-1", supplier="EYDON", photos=["https://x/p1.jpg"], ticket="T42")
        s1 = E.sync_enoc_to_registry()
        mv = FuelMovement.objects.get(source_event_id="REQ-1")
        self.assertEqual((mv.site_id, mv.movement_type, mv.supplier, float(mv.quantity_l), mv.status, mv.source_user),
                         ("DKR_0001", "DELIVERY_EYDON", "EYDON", 150.0, "CLOSED", "Tech A"))
        self.assertEqual((mv.ticket_reference, mv.proof_reference), ("T42", "https://x/p1.jpg"))
        obs = {o.source_event_id: o for o in FuelStockObservation.objects.filter(source_type="ENOC")}
        self.assertEqual(set(obs), {"REQ-1:before", "REQ-1:after"})
        self.assertEqual(float(obs["REQ-1:before"].level_liters), 200.0)
        self.assertEqual(obs["REQ-1:after"].validation_status, "VALIDATED")
        self.assertEqual(s1["mouvements_crees"], 1)
        s2 = E.sync_enoc_to_registry()
        self.assertEqual((s2["mouvements_crees"], s2["releves_crees"], s2["inchanges"], s2["lot"]), (0, 0, 1, None))
        self.assertEqual(FuelMovement.objects.count(), 1)

    def test_null_quantity_stays_null_and_requested_only(self):
        self.mk("REQ-2", status="pending", operation=False)
        E.sync_enoc_to_registry()
        mv = FuelMovement.objects.get()
        self.assertIsNone(mv.quantity_l)  # FuelEnocMovement porte 0 par défaut : le registre garde NULL
        self.assertIn("DELIVERY_REQUESTED_ONLY", [a["code"] for a in mv.alerts])
        before = FuelStockObservation.objects.get(source_event_id="REQ-2:before")
        self.assertEqual(before.validation_status, "PENDING")

    def test_delivery_without_ticket_or_proof_flagged(self):
        self.mk("REQ-3")
        E.sync_enoc_to_registry()
        codes = [a["code"] for a in FuelMovement.objects.get().alerts]
        self.assertIn("DELIVERY_WITHOUT_TICKET", codes)
        self.assertIn("DELIVERY_WITHOUT_PROOF", codes)

    def test_source_update_is_traced(self):
        m = self.mk("REQ-4")
        E.sync_enoc_to_registry()
        m.raw_payload["operation"]["quantity_added_liters"] = 180
        m.save()
        E.sync_enoc_to_registry()
        mv = FuelMovement.objects.get()
        self.assertEqual(float(mv.quantity_l), 180.0)
        c = FuelRegistryCorrection.objects.get(entity="MOVEMENT", field="quantity_l")
        self.assertEqual((c.old_value, c.new_value, c.author_label), ("150.000", "180.0", "ENOC"))

    def test_audit_eydon_fields(self):
        self.mk("REQ-5", supplier="EYDON")
        a = E.audit_enoc_delivery_fields()
        self.assertEqual((a["total"], a["champs_renseignes"]["fournisseur"], a["champs_renseignes"]["preuve (photo)"]), (1, 1, 0))


class RefuelingHistoryTests(TestCase):
    def setUp(self):
        Site.objects.create(site_id="DKR_0061", name="PEAGE10")
        Site.objects.create(site_id="THS_0084", name="BAITY")

    def test_history_movements(self):
        head = ["N° Ticket FMS", "Site ID", "SITE NAME", "ZONE", "VILLE", "Typologie", "retrait", "Source", "Date derniére refueling",
                "Date de la demande", "Responsable", "Source Site ID/Dépôt- ID Destination", "QTT initiale mesurée sur site (L)",
                "QTT initiale mesurée sur site (cm)", "Quantité demandée (L)", "Qtt finale mesurée sur sites (L)",
                "Qtt finale mesurée sur sites (cm)", "Compteur Horaire derniére refueling (H)", "Compteur Horaire Actuel (H)",
                "Delta CH (H)", "CUVE ", "Qtt initiale RMS", "Qtt finale RMS", "RMS DG RH ", "Motif Transfert", "Validé par", "Statut", "Commentaire"]
        d = datetime(2026, 9, 1)
        rows = [
            [5760795, "DKR_0061", "P", "DAKAR", "R", "A", "Ajout", "TOTAL", None, d, "Waly", None, 217, 13, 100, 354, 18, 6090, 6105, 15, 5000, None, None, None, "Appoint", None, "Soldé", None],
            [5760796, "THS_0084", "B", "THIES", "T", "A", "Ajout", "EYDON", None, d, "Malick", None, 0, None, 1000, None, None, None, None, 0, "Bidon", None, None, None, "Dépotage", None, "En cours", None],
            [5760797, "THS_0084", "B", "THIES", "T", "A", "retrait", "Ponction", None, d, "Malick", "DKR_0061", 300, None, 100, 200, None, None, None, 0, 1000, None, None, None, "Ponction", None, "Soldé", None],
        ]
        tmp = tempfile.mkdtemp()
        with override_settings(FUEL_IMPORT_ROOT=tmp):
            b = P.process_bytes(xlsx({"Suivi refueling septembre": [[None], head] + rows}), "r.xlsx", "HISTORICAL_REFUELING", origin="UPLOAD")
        shutil.rmtree(tmp, ignore_errors=True)
        mvs = {m.ticket_reference: m for m in FuelMovement.objects.all()}
        self.assertEqual(mvs["5760795"].movement_type, "DELIVERY_TOTAL_CARD")
        self.assertEqual(mvs["5760796"].movement_type, "DELIVERY_EYDON")
        self.assertEqual((mvs["5760797"].movement_type, mvs["5760797"].counterpart_site_id), ("TRANSFER_OUT", "DKR_0061"))
        self.assertTrue(all(m.quantity_l is None for m in mvs.values()))  # seule la quantité demandée est connue
        self.assertEqual(float(mvs["5760795"].requested_quantity_l), 100.0)
        self.assertEqual(mvs["5760795"].level_before_raw, "217 L / 13 cm")
        self.assertIn("DELIVERY_REQUESTED_ONLY", [a["code"] for a in mvs["5760796"].alerts])
        self.assertEqual(FuelStockObservation.objects.count(), 0)  # historique : aucun relevé certifié créé
        self.assertEqual(b.summary["mouvements_crees"], 3)


class PipelineTests(TestCase):
    def setUp(self):
        Site.objects.create(site_id="DKR_0001", name="ALPHA")
        self.tmp = Path(tempfile.mkdtemp())
        self.override = override_settings(FUEL_IMPORT_ROOT=str(self.tmp))
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mounted_directory_flow(self):
        P.ensure_dirs()
        for sub in ("incoming/loliforce", "incoming/scss", "incoming/closing", "processing", "accepted", "rejected", "archive"):
            self.assertTrue((self.tmp / sub).is_dir())
        self.assertFalse((self.tmp / "incoming/eydon").exists())
        data = xlsx({"KOLDA": [HEAD, guard_row("DKR_0001", cm=12), guard_row("XXX_1", cm=12)]})
        src = self.tmp / "incoming/loliforce/semaine41.xlsx"
        src.write_bytes(data)
        (b,) = P.scan_incoming()
        self.assertFalse(src.exists())
        self.assertEqual(b.status, "VALIDATION_REQUIRED")
        self.assertTrue(Path(b.stored_path).is_file() and "/accepted/" in b.stored_path)
        self.assertEqual(Path(b.stored_path).read_bytes(), data)  # original intact
        self.assertEqual(Path(b.archive_path).read_bytes(), data)
        self.assertTrue(Path(b.report_path).is_file())
        self.assertIn("UNKNOWN_SITE", Path(b.report_path).read_text(encoding="utf-8-sig"))
        # même fichier redéposé : doublon technique, rien réimporté
        src.write_bytes(data)
        (dup,) = P.scan_incoming()
        self.assertEqual((dup.status, dup.duplicate_of_id), ("REJECTED", b.pk))
        self.assertIn("/rejected/", dup.stored_path)
        self.assertEqual(FuelStockObservation.objects.count(), 1)
        # site ajouté ensuite → relance explicite et tracée
        Site.objects.create(site_id="XXX_1", name="NEW")
        rl = P.relaunch(b, "Site XXX_1 créé dans Gestion des sites")
        self.assertEqual((rl.relaunch_of_id, rl.rows_duplicate), (b.pk, 1))
        self.assertEqual(FuelStockObservation.objects.count(), 2)
        self.assertTrue(FuelRegistryCorrection.objects.filter(entity="BATCH", action="RELAUNCH").exists())

    def test_failed_file_goes_to_rejected(self):
        src = self.tmp / "incoming/closing"
        P.ensure_dirs()
        (src / "bad.xlsx").write_bytes(xlsx({"Autre": [["x"]]}))
        (b,) = P.scan_incoming()
        self.assertEqual(b.status, "FAILED")
        self.assertIn("/rejected/", b.stored_path)


class AuditAndApiTests(TestCase):
    def setUp(self):
        Site.objects.create(site_id="DKR_0001", name="ALPHA")
        User = get_user_model()
        self.manager = User.objects.create_user("mgr", "mgr@x.sn", "p", role="manager")
        self.analyst = User.objects.create_user("ana", "ana@x.sn", "p", role="analyst")
        self.client = APIClient()
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(FUEL_IMPORT_ROOT=self.tmp)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tank_correction_validation_reconverts_and_is_traced(self):
        t = tank("DKR_0001", validated=False, shape="RECTANGULAR", length_m=2, width_m=None, height_m=1, capacity_l=2000, diameter_m=None)
        P.process_bytes(xlsx({"K": [HEAD, guard_row("DKR_0001", cm=10)]}), "a.xlsx", "LOLIFORCE", origin="UPLOAD")
        o = FuelStockObservation.objects.get()
        self.assertIsNone(o.level_liters)
        self.client.force_authenticate(self.analyst)
        self.assertEqual(self.client.patch(f"/api/fuel-tracking/stock/cuves/{t.pk}/", {"width_m": 1, "reason": "x"}, format="json").status_code, 403)
        self.client.force_authenticate(self.manager)
        r = self.client.patch(f"/api/fuel-tracking/stock/cuves/{t.pk}/", {"validate": True, "reason": "Contrôle terrain"}, format="json")
        self.assertEqual(r.status_code, 400)  # largeur manquante : validation impossible
        r = self.client.patch(f"/api/fuel-tracking/stock/cuves/{t.pk}/", {"width_m": 1, "validate": True, "reason": "Mesure terrain 14/10"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        o.refresh_from_db()
        self.assertEqual((float(o.level_liters), o.conversion_status), (200.0, "VALID"))
        self.assertEqual(o.validation_status, "PENDING")  # périmètre de gardiennage non configuré
        fields = set(FuelRegistryCorrection.objects.filter(entity="TANK").values_list("field", flat=True))
        self.assertTrue({"width_m", "validation_status"} <= fields)
        c = FuelRegistryCorrection.objects.get(entity="TANK", field="width_m")
        self.assertEqual((c.old_value, c.new_value, c.reason, c.author), (None, "1", "Mesure terrain 14/10", self.manager))

        # validation humaine du relevé, motif obligatoire
        self.assertEqual(self.client.post(f"/api/fuel-tracking/stock/observations/{o.pk}/valider/", {}, format="json").status_code, 400)
        r = self.client.post(f"/api/fuel-tracking/stock/observations/{o.pk}/valider/", {"reason": "Gardien confirmé"}, format="json")
        self.assertEqual(r.json()["validation_status"], "VALIDATED")

        reg = self.client.get("/api/fuel-tracking/stock/registre/?search=dkr_0001").json()
        row = reg["data"][0]
        self.assertEqual((row["stock_l"], row["source"], row["conversion_method"], row["statut"]), (200.0, "LOLIFORCE", "GEOMETRIC_CALCULATION", "RETENU"))
        self.assertEqual(row["gardiennage_attendu"], None)
        self.assertIn("GUARD_NOT_CONFIGURED", row["alerts"])
        hist = self.client.get("/api/fuel-tracking/stock/registre/DKR_0001/").json()
        self.assertEqual(len(hist["observations"]), 1)
        self.assertTrue(hist["corrections"])
        self.assertEqual(FuelStockObservation.objects.count(), 1)  # jamais de suppression

    def test_upload_and_rows_and_relaunch_api(self):
        self.client.force_authenticate(self.analyst)
        f = SimpleUploadedFile("s.xlsx", xlsx({"K": [HEAD, guard_row("DKR_0001", cm=10, guard="SCSS")]}))
        self.assertEqual(self.client.post("/api/fuel-tracking/stock/imports/", {"file": f, "source_type": "SCSS"}).status_code, 403)
        self.client.force_authenticate(self.manager)
        f = SimpleUploadedFile("s.xlsx", xlsx({"K": [HEAD, guard_row("DKR_0001", cm=10, guard="SCSS")]}))
        r = self.client.post("/api/fuel-tracking/stock/imports/", {"file": f, "source_type": "SCSS"})
        self.assertEqual(r.status_code, 201, r.content)
        bid = r.json()["id"]
        rows = self.client.get(f"/api/fuel-tracking/stock/imports/{bid}/lignes/").json()
        self.assertEqual(rows["data"][0]["site_id_resolved"], "DKR_0001")
        lst = self.client.get("/api/fuel-tracking/stock/imports/").json()
        self.assertTrue(lst["repertoire"]["monte"])
        self.assertEqual(self.client.post(f"/api/fuel-tracking/stock/imports/{bid}/relancer/", {}, format="json").status_code, 400)
        r = self.client.post(f"/api/fuel-tracking/stock/imports/{bid}/relancer/", {"reason": "test"}, format="json")
        self.assertEqual(r.json()["relaunch_of"], bid)

    def test_guard_scope_api_reevaluates(self):
        tank("DKR_0001", shape="RECTANGULAR", length_m=1, width_m=1, height_m=1, capacity_l=1000, diameter_m=None)
        P.process_bytes(xlsx({"K": [HEAD, guard_row("DKR_0001", cm=10)]}), "a.xlsx", "LOLIFORCE", origin="UPLOAD")
        self.client.force_authenticate(self.manager)
        r = self.client.post("/api/fuel-tracking/stock/gardiennage/",
                             {"site_id": "dkr_0001", "provider": "LOLIFORCE", "active_from": "01/01/2026", "reason": "Contrat 2026"}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        o = FuelStockObservation.objects.get()
        self.assertEqual((o.guard_scope_status, o.validation_status), ("IN_SCOPE", "VALIDATED"))
