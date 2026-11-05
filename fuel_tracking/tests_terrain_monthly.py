from datetime import datetime

from django.test import TestCase

from core.models import Site
from fuel_tracking.models import FuelImportBatch, FuelMovement, FuelStockObservation as Obs
from fuel_tracking.services import stock_import_pipeline as P
from fuel_tracking.tests_stock_registry import TmpRootMixin, tank, xlsx

HEAD = ["country", "site_id", "site_name", "observation_start", "observation_end", "observation_date", "tank_identifier",
        "stock_opening_value", "stock_opening_unit", "stock_closing_value", "stock_closing_unit", "delivery_l", "transfer_in_l",
        "transfer_out_l", "theft_l", "drain_l", "withdrawal_l", "generator_hour_meter", "comment", "justificatif",
        "observation_status", "source_operator", "source_file_reference"]
D1, D2, DM = datetime(2026, 10, 1), datetime(2026, 10, 31), datetime(2026, 10, 15)


def row(site="DKR_0001", o=(500, "L"), c=(300, "L"), delivery=None, theft=None, **kw):
    r = dict(country="SN", site_id=site, site_name="N", observation_start=D1, observation_end=D2, observation_date=DM, tank_identifier=None,
             stock_opening_value=o[0], stock_opening_unit=o[1], stock_closing_value=c[0], stock_closing_unit=c[1], delivery_l=delivery,
             transfer_in_l=None, transfer_out_l=None, theft_l=theft, drain_l=None, withdrawal_l=None, generator_hour_meter=None, comment=None,
             justificatif=None, observation_status=None, source_operator="Op", source_file_reference=None)
    r.update(kw)
    return [r[h] for h in HEAD]


class TerrainMonthlyTests(TmpRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        for sid in ("DKR_0001", "DKR_0002", "DKR_0003"):
            Site.objects.create(site_id=sid, name=sid)

    def run_file(self, rows, name="terrain_oct.xlsx"):
        return P.process_bytes(xlsx({"Octobre": [HEAD] + rows}), name, "TERRAIN_MENSUEL", origin="UPLOAD")

    def test_levels_and_movements_imported_with_traceability_and_not_retained(self):
        b = self.run_file([row(delivery=200, theft=30)])
        self.assertEqual((b.source_type, b.rows_total), ("TERRAIN_MENSUEL", 1))
        obs = Obs.objects.filter(source_type="TERRAIN_MENSUEL").order_by("observation_datetime")
        self.assertEqual([float(o.level_liters) for o in obs], [500.0, 300.0])
        self.assertIn("terrain_oct.xlsx · Octobre · ligne 2", obs[0].source_reference or obs[0].source_context.get("source_reference", "terrain_oct.xlsx · Octobre · ligne 2"))
        mv = {m.movement_type: m for m in FuelMovement.objects.filter(source_type="TERRAIN_MENSUEL")}
        self.assertEqual({k: float(v.quantity_l) for k, v in mv.items()}, {"DELIVERY": 200.0, "THEFT": 30.0})
        self.assertTrue(all(m.retained_in_stock is False and m.status == "DECLARED" for m in mv.values()))   # complémentaire : jamais retenu seul

    def test_empty_cells_stay_null_and_zero_stock_is_valid(self):
        self.run_file([row(site="DKR_0002", o=(0, "L"), c=(None, None))])
        obs = list(Obs.objects.filter(source_type="TERRAIN_MENSUEL", site_id="DKR_0002"))
        self.assertEqual(len(obs), 1)                             # le stock de clôture vide n'est pas devenu 0
        self.assertEqual(float(obs[0].level_liters), 0.0)         # un stock à 0 L est une donnée valide
        self.assertEqual(FuelMovement.objects.count(), 0)         # mouvements vides : rien créé

    def test_unknown_site_unit_and_impossible_numbers_are_reported(self):
        b = self.run_file([row(site="XXX_9999"), row(site="DKR_0003", o=(50, "gallons")), row(site="DKR_0001", delivery=-5)])
        codes = b.summary["codes"]
        self.assertGreaterEqual(codes.get("UNKNOWN_SITE", 0), 1)
        self.assertGreaterEqual(codes.get("UNKNOWN_UNIT", 0), 1)
        self.assertGreaterEqual(codes.get("INVALID_NUMBER", 0), 1)
        self.assertFalse(Obs.objects.filter(site_id="DKR_0003", level_raw__contains="gallons").exists())
        self.assertFalse(FuelMovement.objects.filter(quantity_l__lt=0).exists())

    def test_reimport_is_idempotent_and_never_overwrites(self):
        b1 = self.run_file([row(delivery=200)], name="a.xlsx")
        n_obs, n_mv = Obs.objects.count(), FuelMovement.objects.count()
        b2 = self.run_file([row(delivery=200)], name="a.xlsx")            # même fichier : refusé par empreinte, rien réimporté
        self.assertEqual((b2.status, b2.duplicate_of_id), ("REJECTED", b1.pk))
        self.assertEqual((Obs.objects.count(), FuelMovement.objects.count()), (n_obs, n_mv))
        b3 = self.run_file([row(delivery=200), row(site="DKR_0002", o=(80, "L"), c=(60, "L"))], name="b.xlsx")   # fichier cumulé : ancienne ligne = doublon
        self.assertEqual(b3.summary["doublons"], 1)
        self.assertEqual(Obs.objects.filter(site_id="DKR_0001").count(), 2)             # pas de double comptage
        self.assertEqual(FuelMovement.objects.filter(site_id="DKR_0001").count(), 1)
        self.assertEqual(Obs.objects.filter(site_id="DKR_0002").count(), 2)

    def test_cm_level_converted_by_engine_with_validated_tank(self):
        tank("DKR_0001")                                              # 100×100×100 cm, plaque 1000 L → 1 cm = 10 L
        self.run_file([row(o=(40, "cm"), c=(20, "cm"))])
        vals = sorted(float(o.level_liters) for o in Obs.objects.filter(source_type="TERRAIN_MENSUEL") if o.level_liters is not None)
        self.assertEqual(vals, [200.0, 400.0])

    def test_source_is_not_prioritary_and_label_detected(self):
        from fuel_tracking.services.stock_period_balance import SOURCE_RANK
        self.assertNotIn("TERRAIN_MENSUEL", SOURCE_RANK)               # rang par défaut « autre source » (4) : jamais prioritaire
        from fuel_tracking.services.stock_file_analysis import analyze
        res = analyze(xlsx({"Octobre": [HEAD, row()]}), "t.xlsx", None)
        self.assertEqual(res["source_detectee"], "TERRAIN_MENSUEL")
        self.assertEqual(FuelImportBatch.objects.count(), 0)           # analyse = aucune écriture
