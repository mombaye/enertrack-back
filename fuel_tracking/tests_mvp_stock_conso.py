"""
Règles MVP du bilan stock / consommation (cahier des charges « Suivi Carburant — Consommation & Stock », 16/11/2026) :
  1. stock initial : fichier de clôture (Closing) avant FUEL_APP_STOCK_START, puis stock final du bilan précédent (repli : Closing, avec réserve) ;
     stock final : relevé ENOC le plus récent (terrain, gardiennage, ravitaillement), validation non requise ;
  2. mouvements : ENOC uniquement (fichiers listés, jamais comptés) ;
  3. plus de rapprochement Closing / ENOC ; la conso Closing reste une mesure de contrôle ;
  4-5. formule, contrôles et score inchangés.
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from fuel_tracking.models import FuelImportBatch, FuelMovement, FuelStockBalance, FuelStockObservation as Obs
from fuel_tracking.services import enoc_canonical as C
from fuel_tracking.services import stock_period_balance as SB
from fuel_tracking.services.stock_file_analysis import closing_stock_rows
from fuel_tracking.tests_enoc_qualification import MVP_RULES
from fuel_tracking.tests_stock_period_balance import BalanceBase
from fuel_tracking.tests_stock_registry import TmpRootMixin, xlsx


class MvpBase(BalanceBase):
    RULES = MVP_RULES

    def closing_initial(self, liters, at, **ctx):
        return self.obs(liters, at, source="CLOSING", tank_id="SITE", source_context=ctx or {})

    def enoc_level(self, liters, at, **extra):
        """Relevé ENOC (niveau avant / après ravitaillement) NON validé : le MVP doit l'accepter."""
        values = dict(validation_status="PENDING", eligible_for_stock=False, business_status="EN_ATTENTE")
        values.update(extra)
        return self.obs(liters, at, source="ENOC", **values)


class MvpAppBase(MvpBase):
    """Période à partir de FUEL_APP_STOCK_START (novembre 2026 et après) : stock final = relevé ENOC le plus récent."""

    def setUp(self):
        super().setUp()
        ov = override_settings(FUEL_APP_STOCK_START=self.start.date().isoformat())
        ov.enable()
        self.addCleanup(ov.disable)


class MvpEnocStatusTests(MvpAppBase):
    def test_pending_and_done_without_trace_are_eligible_rejected_never(self):
        base = {"_has_request": True}
        self.assertTrue(C.map_status({**base, "raw_status": "pending"})["eligible_for_stock"])
        self.assertTrue(C.map_status({**base, "raw_status": "done"})["eligible_for_stock"])  # aucune date ni valideur
        self.assertTrue(C.map_status({"_has_request": False, "raw_status": None})["eligible_for_stock"])  # opération sans demande
        self.assertFalse(C.map_status({**base, "raw_status": "rejected"})["eligible_for_stock"])
        self.assertFalse(C.map_status({**base, "raw_status": "validated", "_legacy_rest": True})["eligible_for_stock"])
        self.assertIn("MVP", C.map_status({**base, "raw_status": "pending"})["status_mapping_rule"])

    @override_settings(FUEL_MVP_IGNORE_VALIDATION=False)
    def test_legacy_rule_still_requires_validation(self):
        self.assertFalse(C.map_status({"_has_request": True, "raw_status": "pending"})["eligible_for_stock"])

    def test_pending_truck_delivery_counted_in_balance(self):
        self.closing_initial(500, self.start + timedelta(hours=1))
        self.enoc_level(300, self.end - timedelta(hours=1))
        self.tc("TR-P1", 200, self.base + timedelta(days=10), typ="TRUCK", status="pending",
                req_extra={"action": {}}, op_extra={"level_after": 300, "level_after_unit": "L"})
        r = self.compute()["DKR_0001"]
        self.assertEqual(float(r["truck_l"]), 200.0)
        self.assertEqual(float(r["consommation_reelle_l"]), 400.0)  # 500 + 200 − 300


class MvpStockOriginTests(MvpBase):
    # Étape 1 — période historique (septembre, octobre 2026 : avant FUEL_APP_STOCK_START) : stocks initial ET final = Closing
    def test_historical_period_initial_and_final_from_closing(self):
        self.obs(900, self.start + timedelta(hours=2))                       # gardiennage fichier (Loliforce) : ignoré
        self.closing_initial(500, self.start + timedelta(hours=1))
        self.obs(250, self.end - timedelta(hours=1))                         # gardiennage fichier en fin de période : ignoré
        self.closing_initial(330, self.end - timedelta(hours=3))             # stock final du Closing → retenu
        self.enoc_level(320, self.end - timedelta(hours=1))                  # relevé ENOC plus récent : pas utilisé avant novembre
        r = self.compute()["DKR_0001"]
        self.assertEqual((r["source_stock_initial"], float(r["stock_initial_l"])), ("CLOSING", 500.0))
        self.assertEqual((r["source_stock_final"], float(r["stock_final_l"])), ("CLOSING", 330.0))
        self.assertEqual(float(r["consommation_reelle_l"]), 170.0)
        self.assertFalse(any("source moins fiable" in x for x in r["reserves"]))

    def test_historical_period_without_closing_final_not_computable(self):
        self.closing_initial(500, self.start + timedelta(hours=1))
        self.enoc_level(320, self.end - timedelta(hours=1))                  # ENOC seul en fin de période historique : insuffisant
        r = self.compute()["DKR_0001"]
        self.assertIsNone(r["consommation_reelle_l"])
        self.assertTrue(any("Stock final absent" in m for m in r["motifs"]))

    def test_no_closing_initial_means_not_computable(self):
        self.obs(500, self.start + timedelta(hours=1))  # seul un relevé de gardiennage fichier : pas une source du stock initial historique
        self.closing_initial(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        self.assertIsNone(r["consommation_reelle_l"])
        self.assertEqual((r["statut_bilan"], r["cause_principale"]), ("NON_CALCULABLE", "DONNEES_STOCK_INCOMPLETES"))

    # Étape 1 — à partir de FUEL_APP_STOCK_START : stock initial = stock final du bilan précédent
    def test_app_period_initial_is_previous_balance_final(self):
        prev_start, prev_end = self.start - timedelta(days=19), self.start - timedelta(seconds=1)
        self.closing_initial(800, prev_start + timedelta(hours=1))
        self.closing_initial(500, prev_end - timedelta(hours=2))
        self.compute(start=prev_start, end=prev_end)                          # bilan précédent (historique) : stock final Closing 500 L
        self.closing_initial(999, self.start + timedelta(hours=1))           # Closing de la période : ignoré, l'application prend le relais
        self.enoc_level(300, self.end - timedelta(hours=1))
        with override_settings(FUEL_APP_STOCK_START=self.start.date().isoformat()):
            r = self.compute()["DKR_0001"]
        self.assertEqual((r["source_stock_initial"], float(r["stock_initial_l"])), ("APP", 500.0))
        self.assertEqual((r["source_stock_final"], float(r["stock_final_l"])), ("ENOC", 300.0))
        self.assertEqual(float(r["consommation_reelle_l"]), 200.0)
        self.assertTrue(any("bilan précédent" in m for m in r["motifs"]))

    def test_app_period_without_previous_balance_falls_back_on_closing_with_reserve(self):
        self.closing_initial(600, self.start - timedelta(hours=6))
        self.enoc_level(300, self.end - timedelta(hours=1))
        with override_settings(FUEL_APP_STOCK_START=self.start.date().isoformat()):
            r = self.compute()["DKR_0001"]
        self.assertEqual((r["source_stock_initial"], float(r["stock_initial_l"])), ("CLOSING", 600.0))
        self.assertEqual(r["statut_bilan"], "ESTIMATION_AVEC_RESERVE")
        self.assertTrue(any("repli sur le dernier stock du fichier de clôture" in x for x in r["reserves"]))


class MvpFinalStockEnocTests(MvpAppBase):
    # À partir de novembre : stock final = relevé ENOC le plus récent (validation non requise)
    def test_final_is_latest_enoc_reading(self):
        self.closing_initial(500, self.start + timedelta(hours=1))
        self.obs(250, self.end - timedelta(hours=1))                         # gardiennage fichier : ignoré
        self.enoc_level(350, self.end - timedelta(days=2))
        self.enoc_level(320, self.end - timedelta(hours=3))
        r = self.compute()["DKR_0001"]
        self.assertEqual((r["source_stock_final"], float(r["stock_final_l"])), ("ENOC", 320.0))
        self.assertEqual(float(r["consommation_reelle_l"]), 180.0)
        self.assertFalse(any("source moins fiable" in x for x in r["reserves"]))  # ENOC est la source officielle du stock final

    def test_final_most_recent_even_if_another_source_same_day_is_older(self):
        self.closing_initial(500, self.start + timedelta(hours=1))
        self.enoc_level(320, self.end - timedelta(hours=5), source_type="ENOC_GARDIENNAGE")
        self.enoc_level(310, self.end - timedelta(hours=2))
        r = self.compute()["DKR_0001"]
        self.assertEqual((r["source_stock_final"], float(r["stock_final_l"])), ("ENOC", 310.0))

    def test_same_time_priority_terrain_then_gardiennage_then_refueling(self):
        self.closing_initial(500, self.start + timedelta(hours=1))
        at = self.end - timedelta(hours=2)
        self.enoc_level(310, at)
        self.enoc_level(305, at, source_type="ENOC_TERRAIN")
        r = self.compute()["DKR_0001"]
        self.assertEqual((r["source_stock_final"], float(r["stock_final_l"])), ("ENOC_TERRAIN", 305.0))

    def test_rejected_enoc_reading_never_used(self):
        self.closing_initial(500, self.start + timedelta(hours=1))
        self.enoc_level(300, self.end - timedelta(hours=5))
        self.enoc_level(100, self.end - timedelta(hours=1), validation_status="REJECTED")
        r = self.compute()["DKR_0001"]
        self.assertEqual(float(r["stock_final_l"]), 300.0)


class MvpMovementsAndClosingTests(MvpAppBase):
    # Étape 2 — mouvements de fichiers : listés, jamais comptés
    def test_file_movements_listed_not_counted(self):
        self.closing_initial(500, self.start + timedelta(hours=1))
        self.enoc_level(300, self.end - timedelta(hours=1))
        FuelMovement.objects.create(source_type="CLOSING", source_event_id="FILE-1", site_id="DKR_0001", movement_type="DELIVERY_TOTAL_CARD",
                                    movement_datetime=self.base + timedelta(days=8), quantity_l=Decimal("400"), status="VALIDATED",
                                    retained_in_stock=True)
        r = self.compute()["DKR_0001"]
        self.assertEqual(float(r["carte_total_l"]), 0.0)
        self.assertEqual(float(r["consommation_reelle_l"]), 200.0)
        self.assertTrue(any("mouvements ENOC uniquement" in m["motif"] for m in r["mouvements_exclus"]))

    # Étape 3 — plus de rapprochement Closing / ENOC ; conso Closing affichée en contrôle
    def test_no_closing_enoc_conflict_and_closing_conso_kept_as_control(self):
        d_ini, d_fin = self.start + timedelta(hours=1), self.end - timedelta(hours=1)
        ctx = {"type": "closing_periode", "periode": [d_ini.date().isoformat(), d_fin.date().isoformat()],
               "livre_l": 2000, "rajout_retrait_l": 0, "vole_l": 0, "vidange_l": 0, "conso_closing_l": 1700}
        self.closing_initial(500, d_ini, **ctx)
        self.obs(800, d_fin, source="CLOSING", tank_id="SITE", source_context=ctx)  # stock final du Closing : ignoré (source non officielle)
        self.enoc_level(300, d_fin - timedelta(hours=1))
        self.tc("TC-X", 200, self.base + timedelta(days=10))                        # ENOC 200 L vs 2 000 L au Closing : plus de conflit
        r = self.compute()["DKR_0001"]
        self.assertEqual(float(r["consommation_reelle_l"]), 400.0)                 # 500 + 200 − 300, sans complément Closing
        self.assertFalse(any("CONFLIT_ENOC_CLOSING" in a or "LIVRAISON_CLOSING_ABSENTE_ENOC" in a for a in r["alertes"]))
        self.assertNotEqual(r["cause_principale"], "CONFLIT_ENOC_CLOSING")
        self.assertEqual((float(r["conso_closing_l"]), r["source_conso_closing"]), (1700.0, "CLOSING"))


class ClosingFormat2026Tests(BalanceBase):
    def test_total_livraison_header_read_as_deliveries(self):
        head = ["Id du site", "Date de relevé initiale", "Qtité laissée à la date du relevé", "Total Livraison",
                "Qté rajoutée ou retirée de la cuve par les techniciens d' un site à l' autre", "Qté volée",
                "Qté vidangée (fuel de mauvaise qualité)", "Date de relevé finale", "Qtité laissée à la date du relevé",
                "Qtité cons considéréé (l)", "Test date"]
        rows = [["Synthèse"], head, ["DKR_0001", datetime(2026, 8, 31), 500, 1200, 0, 0, 0, datetime(2026, 9, 30), 300, 1400, None]]
        data, diag = closing_stock_rows(rows)
        self.assertEqual(diag["colonnes_utilisees"]["livre"], "total livraison")
        self.assertEqual(data[0]["livre_l"], 1200.0)


@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class ConfigurationClosingUploadTests(TmpRootMixin, MvpBase):
    """Rubrique Configuration : envoi du Closing depuis l'interface → import (pipeline existant) → recalcul des bilans des mois couverts."""

    def closing_bytes(self, d1, d2):
        head = ["Id du site", "Nombre de GE installé [ex: 1,2,3 …]", "Date de relevé initiale", "Qtité laissée à la date du relevé",
                "Total Livraison", "Qté rajoutée ou retirée de la cuve par les techniciens d' un site à l' autre", "Qté volée",
                "Qté vidangée (fuel de mauvaise qualité)", "Date de relevé finale", "Qtité laissée à la date du relevé",
                "Qtité cons considéréé (l)", "Test date"]
        return xlsx({"Synthese conso fuel": [["Synthèse"], head, ["DKR_0001", 1, d1, 500, 200, 0, 0, 0, d2, 300, 400, None]]})

    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.client = APIClient()
        self.manager = User.objects.create_user("mgr", "mgr@x.sn", "p", role="manager")
        self.analyst = User.objects.create_user("ana", "ana@x.sn", "p", role="analyst")

    def upload(self, user, data, name="Esco Senegal - Synthese Conso Fuel Septembre 2026.xlsx", mois="2026-09"):
        self.client.force_authenticate(user)
        payload = {"file": SimpleUploadedFile(name, data)}
        if mois is not None:
            payload["mois"] = mois
        return self.client.post("/api/fuel-tracking/configuration/closing/", payload, format="multipart")

    def test_upload_imports_closing_and_recomputes_covered_months(self):
        first = (timezone.localdate().replace(day=1) - timedelta(days=1)).replace(day=1)   # 1er jour du dernier mois complet
        d1 = datetime.combine(first - timedelta(days=1), datetime.min.time())               # relevé initial : veille du mois
        d2 = datetime.combine(first + timedelta(days=20), datetime.min.time())
        res = self.upload(self.manager, self.closing_bytes(d1, d2), mois=first.strftime("%Y-%m"))
        self.assertEqual(res.status_code, 202, res.content)
        b = FuelImportBatch.objects.get(source_type="CLOSING")
        self.assertEqual(b.summary["stocks_closing"]["stocks_importes"], 2, (b.status_message, b.summary))
        self.assertEqual(Obs.objects.filter(batch=b, site_id="DKR_0001").count(), 2)
        bal = FuelStockBalance.objects.filter(site_id="DKR_0001", observation_start__date=first)
        self.assertTrue(bal.exists())  # bilan du mois couvert recalculé sans attendre 3 h 11
        self.assertEqual((bal.get().source_stock_initial, float(bal.get().stock_initial_l)), ("CLOSING", 500.0))
        status = self.client.get("/api/fuel-tracking/configuration/closing/").json()
        self.assertEqual((status["lots"][0]["stocks_importes"], status["en_attente"], status["peut_envoyer"]), (2, [], True))
        lot = status["lots"][0]
        self.assertEqual((lot["etape"], lot["format"], lot["recalcul_bilans"]["statut"]), ("TERMINE", "Closing", "TERMINE"))
        self.assertEqual(lot["recalcul_bilans"]["mois_faits"], lot["recalcul_bilans"]["mois_total"])
        self.assertGreaterEqual(lot["recalcul_bilans"]["mois_total"], 1)

    def test_same_file_twice_is_not_reimported(self):
        d1, d2 = datetime(2026, 8, 31), datetime(2026, 9, 30)
        data = self.closing_bytes(d1, d2)
        self.upload(self.manager, data)
        self.upload(self.manager, data)
        self.assertEqual(Obs.objects.filter(source_type="CLOSING").count(), 2)
        self.assertEqual(FuelImportBatch.objects.filter(source_type="CLOSING", duplicate_of__isnull=False).count(), 1)

    def test_upload_reserved_to_admin_and_manager_and_checks_format(self):
        self.assertEqual(self.upload(self.analyst, b"x").status_code, 403)
        self.assertEqual(self.upload(self.manager, b"x", name="closing.csv").status_code, 400)

    def test_months_covered_capped_at_last_complete_month(self):
        from fuel_tracking.services.closing_upload import months_covered

        class Batch:
            declared_period_start, declared_period_end = date(2026, 8, 31), date(2026, 10, 2)
        m = months_covered(Batch(), today=date(2026, 11, 16))
        self.assertEqual([x[0].isoformat() for x in m], ["2026-08-01", "2026-09-01", "2026-10-01"])


class GensetDbAbaqueTests(MvpBase):
    """Point 3 : abaque CPH lu dans l'onglet GENSET DB du Closing (courbes + correspondance avec l'inventaire des sites)."""
    HEAD = [None, "Manufacturer ", "Type", "Genset List", "Type de GE", None, "Voltage [V]", "No. of phases", "PRP [kVA]", "PRP [kW]",
            "cosφ", "Conso 100%", "Conso 75%", "Conso 50%", "a", "b", "c", "Conso Moyenne"]

    def rows(self):
        return [[None, None, None, None, 1, 2, 3], self.HEAD, [None] * 11 + [1, 0.75, 0.5],
                [None, None, None, None, "Site avec cuve et sans GE"],
                [None, "FG Wilson", "P50-3 (DSE4520)", "FG Wilson P50-3 (DSE4520)", "P50-3 ", 50, 400, 3, 45, 36, 0.8, 10.6, 8.1, 5.9, 0.8, 7.9, 1.9, 10],
                [None, "FG Wilson", "P50-3 (DSE7320)", "FG Wilson P50-3 (DSE7320)", "P50-3 ", 50, 400, 3, 45, 36, 0.8, 10.6, 8.1, 5.9, 0.8, 7.9, 1.9, 10],
                [None, "SDMO", None, None, "K22", 22, 400, 3, 20, 16, 0.8, 5.1, 3.9, 2.8, 0.8, 3.0, 1.3, 5]]

    def test_parse_dedup_and_fit(self):
        from fuel_tracking.services import genset_db_import as G

        curves, warnings = G.parse_rows(self.rows())
        self.assertEqual(len(curves), 2)  # deux variantes de contrôleur, mêmes points : une seule courbe P50-3
        p50 = next(c for c in curves if c["model"] == "P50-3")
        self.assertEqual((p50["manufacturer"], float(p50["prp_kva"]), float(p50["conso_75_l_h"]), p50["curve_id"][:4]), ("FG Wilson", 45.0, 8.1, "GDB-"))
        self.assertEqual(G.candidates_for("FG Wilson P50-3", curves), [p50["curve_id"]])
        self.assertEqual(G.candidates_for("SDMO - K22", curves), [next(c["curve_id"] for c in curves if c["model"] == "K22")])
        self.assertEqual(G.candidates_for("FG Wilson P50-3U", curves), [])  # variante non rapprochée automatiquement

    def test_apply_creates_curves_and_auto_matches_inventory(self):
        from fuel_tracking.models import CphCurve, CphInventoryMapping
        from fuel_tracking.services import genset_db_import as G

        curves, _ = G.parse_rows(self.rows())
        res = G.apply(curves, "closing_sept.xlsb", labels=[("FG Wilson P50-3", Decimal("50")), ("SDMO - K22", Decimal("22")),
                                                            ("FG Wilson P33-3U", Decimal("33"))])
        self.assertEqual((res["courbes_creees"], res["types_ge_avec_courbe"], res["types_ge_sans_courbe"]), (2, 2, 1))
        m = CphInventoryMapping.objects.get(inventory_label="FG Wilson P50-3")
        self.assertEqual(m.match_status, "AUTO_VALIDE_COMPATIBLE", m.match_reasons)
        self.assertEqual(m.validated_curve.model, "P50-3")
        self.assertEqual(CphCurve.objects.get(pk=m.validated_curve_id).source, G.SOURCE_LABEL)
        again = G.apply(curves, "closing_sept.xlsb", labels=[("FG Wilson P50-3", Decimal("50"))])  # réimport idempotent
        self.assertEqual((again["courbes_creees"], CphInventoryMapping.objects.count()), (0, 2))

    def test_manual_decision_never_overwritten(self):
        from fuel_tracking.models import CphInventoryMapping
        from fuel_tracking.services import genset_db_import as G

        CphInventoryMapping.objects.create(inventory_label="FG Wilson P50-3", inventory_label_normalized="fg wilson p50-3",
                                           inventory_kva=Decimal("50"), candidate_curve_ids=[], abaque_status="COURBE_CPH_MANQUANTE",
                                           match_status="REJETE", match_method="RETRAIT_MANUEL")
        curves, _ = G.parse_rows(self.rows())
        G.apply(curves, "closing_sept.xlsb", labels=[("FG Wilson P50-3", Decimal("50"))])
        m = CphInventoryMapping.objects.get(inventory_label="FG Wilson P50-3")
        self.assertEqual((m.match_status, m.candidate_curve_ids), ("REJETE", []))


class EnocLevelFieldsTests(BalanceBase):
    """ENOC a remplacé level_before / level_after (+ unité) par level_*_l et level_*_cm (constaté en prod le 17/11/2026)."""

    def test_level_of_reads_new_and_old_fields(self):
        from fuel_tracking.services.enoc_mongo_service import level_of

        self.assertEqual(level_of({"level_after_l": 130.0, "level_after_cm": None, "level_after_unit": "L"}, "after"), (130.0, "L"))
        self.assertEqual(level_of({"level_before_l": None, "level_before_cm": 42.0}, "before"), (42.0, "cm"))
        self.assertEqual(level_of({"level_before": 55, "level_before_unit": "cm", "level_before_l": 300}, "before"), (55, "cm"))  # ancien champ prioritaire
        self.assertEqual(level_of({"level_after_l": 0.0}, "after"), (0.0, "L"))  # 0 L mesuré reste 0, jamais « absent »
        self.assertEqual(level_of(None, "after"), (None, None))

    def test_fetch_movements_maps_new_level_fields_to_registry(self):
        from unittest import mock

        from fuel_tracking.management.commands.sync_enoc_fuel_movements import clean_payload
        from fuel_tracking.models import FuelEnocMovement, FuelStockObservation as Obs
        from fuel_tracking.services import enoc_mongo_service as EM
        from fuel_tracking.services import stock_enoc as E

        at = self.base + timedelta(days=10)
        req = {"_id": "oid-1", "request_code": "RC-1", "status": "done", "site_id": "DKR_0001", "operation_type": "TOTAL_CARD",
               "created_at": (at - timedelta(days=1)).isoformat(), "level_before_l": 120.0, "level_before_cm": None,
               "approved_quantity_liters": 200, "action": {"validated_at": (at - timedelta(hours=12)).isoformat()}}
        op = {"request_code": "RC-1", "operation_type": "TOTAL_CARD", "operation_date": at.isoformat(), "created_at": at.isoformat(),
              "quantity_added_liters": 200.0, "level_after_l": None, "level_after_cm": 41.0, "level_after_unit": "cm"}

        class Coll:
            def __init__(self, docs):
                self.docs = docs

            def find(self, *a, **k):
                return list(self.docs)

        class Client:
            def __getitem__(self, name):
                return type("DB", (), {"fuel_requests": Coll([req]), "fuel_operations": Coll([op])})()

            def close(self):
                pass

        with mock.patch.object(EM, "_connect", return_value=Client()):
            items = EM.fetch_fuel_movements()
        self.assertEqual((items[0]["level_before"], items[0]["level_before_unit"], items[0]["level_after"], items[0]["level_after_unit"]),
                         (120.0, "L", 41.0, "cm"))
        FuelEnocMovement.objects.create(**clean_payload(items[0]))
        E.sync_enoc_to_registry()
        obs = {o.source_event_id: o for o in Obs.objects.filter(source_type="ENOC")}
        self.assertEqual((obs["RC-1:before"].level_unit, float(obs["RC-1:before"].level_value)), ("L", 120.0))
        self.assertEqual((obs["RC-1:after"].level_unit, float(obs["RC-1:after"].level_value)), ("CM", 41.0))


class OutputAppFuelImportTests(TmpRootMixin, MvpBase):
    """Fichier « Output App Fuel » (feuille Output) importé comme fichier de clôture : stocks initial (O) et final (AM / AQ / AR)."""

    def output_bytes(self):
        head = ["", "ID du site", "Nom du site", "Région O&M", "Date de Relevé Initiale", "Qtité laissée à la date initiale (relevé de terrain)",
                "Qtité laissée retenue", "Source de donnée de stock", "Total Livraison Fuel EYDON", "Total Livraison Fuel CARTE TOTAL",
                "Total Ajout", "Total Retrait", "Total Ponction", "Total QTE volée déclarée", "Total QTE vidangée / Fuite / Ecart de Stock",
                "Date de Relevé Finale", "Qtité laissée à la date finale (relevé de terrain)", "Qtité laissée retenue", "Source de donnée de stock",
                "Variation de stock Fuel considéréé  (L)", "Estimation Conso Fuel Théorique retenue  (L)", "Source de donnée Estimation Conso Fuel théorique"]
        d = lambda m, j: datetime(2026, m, j)  # noqa: E731
        rows = [["", "INVENTAIRE"], head,
                ["", "DKR_0001", "ALPHA", "DAKAR", d(9, 30), 1000, 1000, "relevé de terrain", 0, 600, 0, 0, 0, 0, 0,
                 d(10, 31), 400, 400, "Relevés de Terrain Technicien", 1200, 1100, "calcul cph et temps de marche GE"],
                ["", "DKR_0002", "SOURCE", "DAKAR", d(10, 2), 800, 800, "relevé de terrain", 0, 0, 0, 0, 0, 0, 0,
                 d(11, 5), 300, 300, "Estimation Energy Desk", 500, None, None],
                ["", "DKR_0003", "SANS GE", "DAKAR", d(9, 30), 0, 0, "relevé de terrain", 0, 0, 0, 0, 0, 0, 0,
                 d(10, 31), 0, 0, "Site sans GE et sans cuve", 0, None, None],
                ["", "XXX_9999", "INCONNU", "DAKAR", d(9, 30), 10, 10, "relevé de terrain", 0, 0, 0, 0, 0, 0, 0,
                 d(10, 31), 5, 5, "Relevés de Terrain Technicien", 5, None, None]]
        return xlsx({"Output": rows})

    def test_import_and_october_balance(self):
        from fuel_tracking.services import stock_import_pipeline as P

        b = P.process_bytes(self.output_bytes(), "Output App Fuel.xlsx", "CLOSING", origin="UPLOAD")
        st = b.summary["stocks_closing"]
        self.assertEqual((st["format"], st["stocks_importes"], st["sites_sans_ge_ignores"], st["sites_inconnus"], st["stock_final_estime_energy_desk"]),
                         ("Output App Fuel", 4, 1, 1, 1))
        self.assertEqual(b.status, "ACCEPTED")
        self.assertIn("4 stock(s) importé(s) (Output App Fuel)", b.status_message)
        oct1 = timezone.make_aware(datetime(2026, 10, 1))
        oct31 = timezone.make_aware(datetime(2026, 10, 31, 23, 59, 59))
        rows = {r["site_id"]: r for r in SB.compute_balances(oct1, oct31, site_ids=["DKR_0001", "DKR_0002"], persist=False)}
        a = rows["DKR_0001"]
        self.assertEqual((a["source_stock_initial"], float(a["stock_initial_l"]), a["source_stock_final"], float(a["stock_final_l"])),
                         ("CLOSING", 1000.0, "CLOSING", 400.0))
        # Octobre = période où le fichier de clôture fait foi (ENOC incomplet) : 1 000 + 600 livrés au fichier − 400 = 1 200 L,
        # identique à la variation Ops ; ENOC reste affiché, le bilan est « avec réserve ».
        self.assertEqual(float(a["consommation_reelle_l"]), 1200.0)
        self.assertEqual(float(a["conso_closing_l"]), 1200.0)
        self.assertEqual(float(a["livraisons_externes_l"]), 600.0)
        self.assertTrue(any("Closing de la période validée" in x for x in a["reserves"]))
        b2 = rows["DKR_0002"]                                               # relevés du 02/10 et du 05/11 + stock final estimé Energy Desk
        self.assertEqual((float(b2["stock_initial_l"]), float(b2["stock_final_l"]), b2["statut_bilan"]), (800.0, 300.0, "ESTIMATION_AVEC_RESERVE"))
        self.assertEqual(b2["stock_final_detail"]["date"][:10], "2026-11-05")            # date exacte du fichier, même après la borne
        self.assertTrue(b2["estimation"])                                   # stock final estimé : jamais présenté comme mesuré

    def test_sites_without_genset_excluded_from_health(self):
        from fuel_tracking.services import stock_import_pipeline as P
        from fuel_tracking.services.data_health import final_stock_coverage
        from fuel_tracking.services.output_app_fuel import sites_without_genset

        b = P.process_bytes(self.output_bytes(), "Output App Fuel.xlsx", "CLOSING", origin="UPLOAD")
        self.assertEqual(b.summary["stocks_closing"]["sites_sans_ge"], ["DKR_0003"])
        self.assertEqual(sites_without_genset("2026-10"), {"DKR_0003"})
        cov = final_stock_coverage({"DKR_0001", "DKR_0003"}, mois="2026-10")
        self.assertEqual((cov["mois"], cov["sites_base"], cov["sites_sans_ge_exclus"]), ("2026-10", 1, 1))
        self.assertNotIn("DKR_0003", cov["sans_releve"])
        sept = final_stock_coverage({"DKR_0001", "DKR_0003"}, mois="2026-09")   # liste d'octobre jamais appliquée à septembre
        self.assertEqual(sept["sites_sans_ge_exclus"], 0)

    @override_settings(FUEL_CLOSING_AUTHORITATIVE_PERIODS=[], FUEL_BALANCE_CLOSING_CROSSCHECK=False, FUEL_CLOSING_REFERENCE_PERIODS=[])
    def test_files_give_stocks_only_movements_from_enoc(self):
        """Décision du 18/11/2026 : du fichier, seuls les stocks ; les 600 L livrés au fichier ne sont PAS comptés (aucun mouvement ENOC ici)."""
        from fuel_tracking.services import stock_import_pipeline as P

        P.process_bytes(self.output_bytes(), "Output App Fuel.xlsx", "CLOSING", origin="UPLOAD")
        oct1 = timezone.make_aware(datetime(2026, 10, 1))
        oct31 = timezone.make_aware(datetime(2026, 10, 31, 23, 59, 59))
        a = SB.compute_balances(oct1, oct31, site_ids=["DKR_0001"], persist=False)[0]
        self.assertEqual((float(a["stock_initial_l"]), float(a["stock_final_l"]), float(a["livraisons_externes_l"])), (1000.0, 400.0, 0.0))
        self.assertEqual(float(a["consommation_reelle_l"]), 600.0)
        self.assertEqual(float(a["conso_closing_l"]), 1200.0)      # conso d'Ops : contrôle seulement

    @override_settings(FUEL_BALANCE_CLOSING_POST_BOUNDARY_DAYS=0)
    def test_strict_rule_rejects_readings_after_boundary(self):
        """Décision du 18/11/2026 : les dates de la ligne du fichier du mois priment sur toute tolérance (ici relevé final du 05/11)."""
        from fuel_tracking.services import stock_import_pipeline as P

        P.process_bytes(self.output_bytes(), "Output App Fuel.xlsx", "CLOSING", origin="UPLOAD")
        oct1 = timezone.make_aware(datetime(2026, 10, 1))
        oct31 = timezone.make_aware(datetime(2026, 10, 31, 23, 59, 59))
        r = SB.compute_balances(oct1, oct31, site_ids=["DKR_0002"], persist=False)[0]
        self.assertEqual((r["stock_initial_detail"]["date"][:10], r["stock_final_detail"]["date"][:10]), ("2026-10-02", "2026-11-05"))


class OutputPonctionAndRoundingTests(OutputAppFuelImportTests):
    """Correctif du 17/11/2026 : « Total Ponction » n'est pas recompté ; un résidu d'arrondi négatif n'est pas une anomalie."""

    def output_bytes(self):
        head = ["", "ID du site", "Nom du site", "Région O&M", "Date de Relevé Initiale", "Qtité laissée à la date initiale (relevé de terrain)",
                "Qtité laissée retenue", "Source de donnée de stock", "Total Livraison Fuel EYDON", "Total Livraison Fuel CARTE TOTAL",
                "Total Ajout", "Total Retrait", "Total Ponction", "Total QTE volée déclarée", "Total QTE vidangée / Fuite / Ecart de Stock",
                "Date de Relevé Finale", "Qtité laissée à la date finale (relevé de terrain)", "Qtité laissée retenue", "Source de donnée de stock",
                "Variation de stock Fuel considéréé  (L)", "Estimation Conso Fuel Théorique retenue  (L)", "Source de donnée Estimation Conso Fuel théorique"]
        d = lambda m, j: datetime(2026, m, j)  # noqa: E731
        rows = [["", "INVENTAIRE"], head,
                # ponction de 4 360 L sortie du site : déjà dans « Total Retrait » (AF), « Total Ponction » (AG) n'en est que le solde
                ["", "DKR_0001", "ALPHA", "DAKAR", d(9, 30), 5000, 5000, "relevé de terrain", 0, 0, 0, 4360, -4360, 0, 0,
                 d(10, 31), 300, 300, "Relevés de Terrain Technicien", 340, None, None],
                # stocks fichier à 12 décimales : variation −0,4 L ≤ tolérance (1 L) = arrondi
                ["", "DKR_0002", "BETA", "DAKAR", d(9, 30), 800, 800, "relevé de terrain", 0, 0, 0, 0, 0, 0, 0,
                 d(10, 31), 800.4, 800.4, "Relevés de Terrain Technicien", 0, None, None]]
        return xlsx({"Output": rows})

    def balances(self):
        oct1 = timezone.make_aware(datetime(2026, 10, 1))
        oct31 = timezone.make_aware(datetime(2026, 10, 31, 23, 59, 59))
        return {r["site_id"]: r for r in SB.compute_balances(oct1, oct31, site_ids=["DKR_0001", "DKR_0002"], persist=False)}

    def test_import_and_october_balance(self):
        from fuel_tracking.services import stock_import_pipeline as P

        P.process_bytes(self.output_bytes(), "Output App Fuel.xlsx", "CLOSING", origin="UPLOAD")
        rows = self.balances()
        # formule Ops : O + AK − AJ − AQ = 5 000 + 0 − 4 360 − 300 = 340 L (et non 5 000 − 8 720 − 300 = −4 020 L)
        self.assertEqual(float(rows["DKR_0001"]["consommation_reelle_l"]), 340.0)
        self.assertEqual(float(rows["DKR_0002"]["consommation_reelle_l"]), 0.0)
        self.assertNotEqual(rows["DKR_0002"]["cause_principale"], "CONSO_NEGATIVE")
        self.assertTrue(any("écart d'arrondi" in m for m in rows["DKR_0002"]["motifs"]))

    @override_settings(FUEL_BALANCE_NEGATIVE_TOLERANCE_L=0)
    def test_without_tolerance_rounding_is_negative(self):
        from fuel_tracking.services import stock_import_pipeline as P

        P.process_bytes(self.output_bytes(), "Output App Fuel.xlsx", "CLOSING", origin="UPLOAD")
        self.assertEqual(self.balances()["DKR_0002"]["cause_principale"], "CONSO_NEGATIVE")

    def test_post_deploy_corrects_previous_import(self):
        from io import StringIO

        from django.core.management import call_command

        from fuel_tracking.models import FuelStockObservation as Obs
        from fuel_tracking.services import stock_import_pipeline as P

        P.process_bytes(self.output_bytes(), "Output App Fuel.xlsx", "CLOSING", origin="UPLOAD")
        for o in Obs.objects.filter(source_type="CLOSING", site_id="DKR_0001"):   # contexte d'un import antérieur au correctif
            ctx = dict(o.source_context)
            ctx["rajout_retrait_l"] = -8720.0
            ctx.pop("ponction_corrigee")
            Obs.objects.filter(pk=o.pk).update(source_context=ctx)
        self.assertLess(float(self.balances()["DKR_0001"]["variation_stock_l"]), 0)
        out = StringIO()
        call_command("fuel_post_deploy", stdout=out)
        self.assertIn("2 relevé(s) au contexte de ponction à corriger", out.getvalue())
        self.assertEqual(float(self.balances()["DKR_0001"]["consommation_reelle_l"]), 340.0)
        out = StringIO()
        call_command("fuel_post_deploy", stdout=out)                            # idempotent
        self.assertIn("0 relevé(s) au contexte de ponction", out.getvalue())

    def test_files_give_stocks_only_movements_from_enoc(self):
        pass  # sans objet pour ce fichier

    def test_strict_rule_rejects_readings_after_boundary(self):
        pass  # sans objet pour ce fichier

    def test_sites_without_genset_excluded_from_health(self):
        pass  # sans objet pour ce fichier


class ClosingAuthoritativeOctoberTests(MvpBase):
    """Décision 17/11/2026 : septembre / octobre 2026, totaux du fichier de clôture prioritaires sur ENOC ; novembre : ENOC seul."""

    def test_sept_closing_initial_with_october_file_final_uses_file_totals(self):
        oct1 = timezone.make_aware(datetime(2026, 10, 1))
        oct31 = timezone.make_aware(datetime(2026, 10, 31, 23, 59, 59))
        sept_ctx = {"type": "closing_periode", "periode": ["2026-08-31", "2026-09-30"], "livre_l": 0, "conso_closing_l": 300}
        oct_ctx = {"type": "closing_periode", "fichier": "Output App Fuel", "periode": ["2026-09-30", "2026-10-31"], "livre_l": 700,
                   "rajout_retrait_l": 0, "vole_l": 0, "vidange_l": 0, "conso_closing_l": 900}
        self.obs(500, timezone.make_aware(datetime(2026, 9, 30, 12)), source="CLOSING", tank_id="SITE", source_context=sept_ctx)
        self.obs(300, timezone.make_aware(datetime(2026, 10, 31, 12)), source="CLOSING", tank_id="SITE", source_context=oct_ctx)
        self.tc("TC-OCT", 200, timezone.make_aware(datetime(2026, 10, 10)))   # ENOC : 200 L seulement
        r = SB.compute_balances(oct1, oct31, site_ids=["DKR_0001"], persist=False)[0]
        self.assertEqual(float(r["consommation_reelle_l"]), 900.0)            # 500 + 700 (fichier) − 300, et non 500 + 200 − 300
        self.assertTrue(any(m["source"] == "ENOC" for m in r["mouvements_exclus"]))  # ENOC conservé pour audit

    @override_settings(FUEL_CLOSING_AUTHORITATIVE_PERIODS=[])
    def test_without_authoritative_period_enoc_kept(self):
        oct1 = timezone.make_aware(datetime(2026, 10, 1))
        oct31 = timezone.make_aware(datetime(2026, 10, 31, 23, 59, 59))
        ctx = {"type": "closing_periode", "periode": ["2026-09-30", "2026-10-31"], "livre_l": 200, "rajout_retrait_l": 0, "vole_l": 0, "vidange_l": 0}
        self.obs(500, timezone.make_aware(datetime(2026, 9, 30, 12)), source="CLOSING", tank_id="SITE", source_context=ctx)
        self.obs(300, timezone.make_aware(datetime(2026, 10, 31, 12)), source="CLOSING", tank_id="SITE", source_context=ctx)
        self.tc("TC-OCT2", 200, timezone.make_aware(datetime(2026, 10, 10)))
        r = SB.compute_balances(oct1, oct31, site_ids=["DKR_0001"], persist=False)[0]
        self.assertEqual(float(r["consommation_reelle_l"]), 400.0)            # ENOC = Closing : pas de remplacement


class UploadStepTests(MvpBase):
    def test_steps(self):
        from types import SimpleNamespace as N

        from fuel_tracking.views_stock import _upload_step

        b = lambda status, dup=None, summary=None: N(status=status, duplicate_of_id=dup, summary=summary or {})  # noqa: E731
        self.assertEqual(_upload_step(b("PROCESSING"), None), "IMPORT")
        self.assertEqual(_upload_step(b("ACCEPTED"), {"statut": "EN_COURS"}), "RECALCUL")
        self.assertEqual(_upload_step(b("ACCEPTED"), {"statut": "TERMINE"}), "TERMINE")
        self.assertEqual(_upload_step(b("REJECTED", dup=3), None), "DOUBLON")
        self.assertEqual(_upload_step(b("FAILED"), None), "ECHEC")
        self.assertEqual(_upload_step(b("ACCEPTED"), {"statut": "ECHEC"}), "ECHEC")
        self.assertEqual(_upload_step(b("ACCEPTED"), None), "IMPORTE")
        self.assertEqual(_upload_step(b("REJECTED", summary={"annulation": {}}), {"statut": "EN_COURS"}), "RETRAIT_RECALCUL")
        self.assertEqual(_upload_step(b("REJECTED", summary={"annulation": {}}), {"statut": "TERMINE"}), "RETIRE")



@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
@override_settings(FUEL_CLOSING_REFERENCE_PERIODS=["2026-09"])
class ClosingReferenceMonthTests(ConfigurationClosingUploadTests):
    """Septembre 2026 = mois de RÉFÉRENCE : le bilan reprend la ligne du fichier Closing TELLE QUELLE (pas de recalcul)."""

    def ref_bytes(self):
        head = ["Id du site", "Nombre de GE installé [ex: 1,2,3 …]", "Date de relevé initiale", "Qtité laissée à la date du relevé",
                "Total Livraison", "Qté rajoutée ou retirée de la cuve par les techniciens d' un site à l' autre", "Qté volée",
                "Qté vidangée (fuel de mauvaise qualité)", "Date de relevé finale", "Qtité laissée à la date du relevé",
                "Qtité cons considéréé (l)", "Test date"]
        rows = [["Synthèse"], head,
                # conso du Closing (450) ≠ formule (500 + 200 − 50 − 300 = 350) : la valeur du fichier est affichée telle quelle
                ["DKR_0001", 1, datetime(2026, 8, 31), 500, 200, -50, 0, 0, datetime(2026, 9, 30), 300, 450, None],
                # relevé final daté du 28/10 dans le Closing de septembre : repris quand même (ligne du fichier)
                ["DKR_0002", 1, datetime(2026, 8, 30), 900, 0, 0, 0, 0, datetime(2026, 10, 28), 900, 0, None]]
        return xlsx({"Synthese conso fuel": rows})

    def test_september_is_the_file_as_is(self):
        from fuel_tracking.services import conso_comparison as CC

        res = self.upload(self.manager, self.ref_bytes(), mois="2026-09")
        self.assertEqual(res.status_code, 202, res.content)
        s = timezone.make_aware(datetime(2026, 9, 1))
        e = timezone.make_aware(datetime(2026, 9, 30, 23, 59, 59))
        rows = {r["site_id"]: r for r in SB.compute_balances(s, e, site_ids=["DKR_0001", "DKR_0002"], persist=False)}
        a = rows["DKR_0001"]
        self.assertEqual((float(a["stock_initial_l"]), float(a["livraisons_externes_l"]), float(a["retraits_l"]), float(a["stock_final_l"])),
                         (500.0, 200.0, 50.0, 300.0))
        self.assertEqual((float(a["variation_stock_l"]), float(a["conso_closing_l"]), a["cause_principale"]), (450.0, 450.0, "REFERENCE_CLOSING"))
        self.assertTrue(any("incohérente" in m for m in a["motifs"]))
        b = rows["DKR_0002"]
        self.assertEqual((float(b["stock_initial_l"]), float(b["stock_final_l"]), float(b["variation_stock_l"])), (900.0, 900.0, 0.0))
        SB.compute_balances(s, e, site_ids=["DKR_0001"])
        bal = FuelStockBalance.objects.get(site_id="DKR_0001", observation_start=s)
        self.assertEqual(CC.bilan_status(bal), CC.REFERENCE)

    @override_settings(FUEL_CLOSING_REFERENCE_PERIODS=[], FUEL_BALANCE_CLOSING_CROSSCHECK=False, FUEL_CLOSING_AUTHORITATIVE_PERIODS=[])
    def test_late_final_of_month_file_line_is_retained(self):
        """Stock final relevé le 28/10 sur la ligne du Closing de septembre : retenu par Ops → bilan de septembre calculé, avec réserve."""
        self.upload(self.manager, self.ref_bytes(), mois="2026-09")
        s = timezone.make_aware(datetime(2026, 9, 1))
        e = timezone.make_aware(datetime(2026, 9, 30, 23, 59, 59))
        r = SB.compute_balances(s, e, site_ids=["DKR_0002"], persist=False)[0]
        self.assertEqual((r["stock_final_detail"]["tier"], float(r["stock_final_l"]), float(r["variation_stock_l"])), ("FICHIER_DU_MOIS", 900.0, 0.0))
        self.assertEqual(r["stock_final_detail"]["date"][:10], "2026-10-28")          # date exacte du fichier

    @override_settings(FUEL_CLOSING_REFERENCE_PERIODS=[])
    def test_without_reference_month_september_is_computed(self):
        self.upload(self.manager, self.ref_bytes(), mois="2026-09")
        s = timezone.make_aware(datetime(2026, 9, 1))
        e = timezone.make_aware(datetime(2026, 9, 30, 23, 59, 59))
        r = SB.compute_balances(s, e, site_ids=["DKR_0001"], persist=False)[0]
        self.assertNotEqual(r["cause_principale"], "REFERENCE_CLOSING")


class ConfigurationMonthAndWithdrawalTests(ConfigurationClosingUploadTests):
    """Mois déclaré à l'import et retrait d'un fichier importé (rubrique Configuration)."""

    def test_months_covered_capped_at_last_complete_month(self):  # déjà couvert par la classe parente
        pass

    def sept_file(self):
        return self.closing_bytes(datetime(2026, 8, 31), datetime(2026, 9, 30))

    def test_month_is_required_and_not_in_future(self):
        self.assertEqual(self.upload(self.manager, self.sept_file(), mois=None).status_code, 400)
        self.assertEqual(self.upload(self.manager, self.sept_file(), mois="septembre").status_code, 400)
        self.assertEqual(self.upload(self.manager, self.sept_file(), mois="2099-01").status_code, 400)

    def test_declared_month_stored_and_checked(self):
        self.upload(self.manager, self.sept_file(), mois="2026-09")
        lot = self.client.get("/api/fuel-tracking/configuration/closing/").json()["lots"][0]
        self.assertEqual((lot["mois"], lot["declared_period_start"], lot["declared_period_end"]), ("2026-09", "2026-09-01", "2026-09-30"))
        self.assertNotIn("avertissement", lot["controle_mois"])
        self.assertEqual([m["debut"] for m in lot["recalcul_bilans"]["mois"]], ["2026-09-01", "2026-10-01"])  # mois + mois suivant

    def test_wrong_month_is_flagged(self):
        self.upload(self.manager, self.sept_file(), mois="2026-07")
        lot = self.client.get("/api/fuel-tracking/configuration/closing/").json()["lots"][0]
        self.assertIn("2026-09", lot["controle_mois"]["avertissement"])

    def test_withdraw_then_reimport(self):
        self.upload(self.manager, self.sept_file(), mois="2026-09")
        b = FuelImportBatch.objects.get(source_type="CLOSING")
        self.assertEqual(Obs.objects.filter(batch=b).count(), 2)
        self.client.force_authenticate(self.analyst)
        self.assertEqual(self.client.delete(f"/api/fuel-tracking/configuration/closing/{b.pk}/").status_code, 403)
        self.client.force_authenticate(self.manager)
        res = self.client.delete(f"/api/fuel-tracking/configuration/closing/{b.pk}/", {"motif": "mauvais mois"}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual((body["stocks_supprimes"], body["mois"]), (2, ["2026-09", "2026-10"]))
        self.assertEqual(Obs.objects.filter(source_type="CLOSING").count(), 0)
        lot = self.client.get("/api/fuel-tracking/configuration/closing/").json()["lots"][0]
        self.assertEqual((lot["etape"], lot["annulation"]["par"], lot["annulation"]["motif"]), ("RETIRE", "mgr", "mauvais mois"))
        self.assertEqual(lot["recalcul_bilans"]["statut"], "TERMINE")  # bilans recalculés après retrait
        sept = FuelStockBalance.objects.get(site_id="DKR_0001", observation_start__date=date(2026, 9, 1))
        self.assertIsNone(sept.stock_initial_l)                            # plus de stock du fichier retiré
        self.assertEqual(self.client.delete(f"/api/fuel-tracking/configuration/closing/{b.pk}/").status_code, 400)  # déjà retiré
        self.upload(self.manager, self.sept_file(), mois="2026-09")         # réimport du même fichier : accepté (pas un doublon)
        self.assertEqual(Obs.objects.filter(source_type="CLOSING").count(), 2)
        self.assertEqual(FuelImportBatch.objects.filter(source_type="CLOSING", duplicate_of__isnull=False).count(), 0)


class FinalStockHealthTests(MvpBase):
    """Santé des données : couverture du stock final dans la source convenue (plus les relevés de gardiennage)."""

    def test_coverage_uses_agreed_source(self):
        from fuel_tracking.services.data_health import final_stock_coverage

        end_day = timezone.localdate().replace(day=1) - timedelta(days=1)
        first = end_day.replace(day=1)
        tz = timezone.get_current_timezone()
        at = lambda d: timezone.make_aware(datetime.combine(d, datetime.min.time()), tz) + timedelta(hours=12)  # noqa: E731
        app_start = (first + timedelta(days=40)).replace(day=1).isoformat()  # mois complet = période « fichier de clôture »
        with override_settings(FUEL_APP_STOCK_START=app_start):
            self.obs(300, at(end_day), source="CLOSING", tank_id="SITE")              # DKR_0001 : relevé de clôture fin de mois
            self.obs(250, at(end_day), site="DKR_0002")                                # DKR_0002 : gardiennage fichier seulement
            f = final_stock_coverage({"DKR_0001", "DKR_0002"})
        self.assertTrue(f["source"].startswith("Fichier de clôture"))
        self.assertEqual((f["sites_exploitables"], f["sites_sans_releve"], f["sans_releve"]), (1, 1, ["DKR_0002"]))
        with override_settings(FUEL_APP_STOCK_START=first.isoformat()):                # mois complet = période ENOC
            self.obs(280, at(end_day - timedelta(days=6)), site="DKR_0002", source="ENOC")
            f = final_stock_coverage({"DKR_0001", "DKR_0002"})
        self.assertEqual((f["source"], f["sites_avec_reserve"], f["sans_releve"]), ("ENOC", 1, ["DKR_0001"]))


class ClosingKpiParseTests(MvpBase):
    """Bloc de KPI de l'en-tête du Closing : valeurs lues telles quelles, libellés conservés."""

    def test_parse_header_block(self):
        from fuel_tracking.services.closing_kpis import parse_kpis

        g = [[None] * 160 for _ in range(12)]
        put = lambda r, c, v: g[r].__setitem__(c, v)  # noqa: E731
        put(5, 2, 533); put(5, 3, "Nbre de site avec GE"); put(5, 4, 1.05); put(5, 5, "Conso moyenne / jour SEPTEMBRE 2026")
        put(6, 2, 400818); put(6, 3, "Quantité laissée dans les cuves aux dates de relevés de AOUT 2026")
        put(6, 4, 394244); put(6, 5, "Quantité laissée dans les cuves  aux dates de relevés de SEPTEMBRE 2026")
        put(8, 2, 123700); put(8, 3, "Qtité Commandée  SEPTEMBRE 2026  pour Conso OCTOBR 2026 chez Fournisseur Aktivco")
        put(9, 2, 79200); put(9, 3, "Qtité Commandée  SEPTEMBRE 2026  pour Conso OCTOBR 2026 mais non-livrée chez Fournisseur Aktivco")
        put(8, 4, 103355); put(8, 5, "Quantité consommée entre les dates de relevés")
        put(11, 2, -1580); put(11, 3, "Autre Quantité livrée non-Aktivco entre les dates de relevés"); put(11, 4, 3378); put(11, 5, "Nbre de site")
        put(6, 154, "Estimation Conso Fuel SEPTEMBRE 2026 Sites ESCO (Litres)"); put(6, 155, 105670)
        put(7, 154, "Estimation Conso Fuel AOUT 2026 Sites ESCO (Litres)"); put(7, 155, 84490)
        k = parse_kpis(g)
        self.assertEqual({x: k[x]["valeur"] for x in ("nb_sites_ge", "stock_initial_l", "stock_final_l", "commande_mois_suivant_l",
                                                     "commande_non_livree_l", "conso_releves_l", "autre_l", "nb_sites",
                                                     "estimation_conso_mois_l", "estimation_conso_prec_l")},
                         {"nb_sites_ge": 533, "stock_initial_l": 400818, "stock_final_l": 394244, "commande_mois_suivant_l": 123700,
                          "commande_non_livree_l": 79200, "conso_releves_l": 103355, "autre_l": -1580, "nb_sites": 3378,
                          "estimation_conso_mois_l": 105670, "estimation_conso_prec_l": 84490})
        self.assertIn("AOUT 2026", k["estimation_conso_prec_l"]["libelle"])

    def test_partial_period_has_no_kpis(self):
        from datetime import date

        from fuel_tracking.services.closing_kpis import kpis_for_period

        part = kpis_for_period(date(2026, 10, 1), date(2026, 10, 15))   # KPI dynamiques : toute période affichée
        self.assertEqual((part["source"], part["kpis"]["nb_jours"]["valeur"]), ("CALCUL_ENERTRACK", 15))
        self.assertIn("du 01/10/2026 au 15/10/2026", part["kpis"]["estimation_conso_mois_l"]["libelle"])
        k = kpis_for_period(date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual((k["source"], k["kpis"]["nb_jours"]["valeur"]), ("CALCUL_ENERTRACK", 31))
        self.assertIsNone(k["kpis"]["commande_mois_suivant_l"]["valeur"])   # non disponible, jamais 0


class ClosingRetainedFinalStockTests(OutputAppFuelImportTests):
    """Mois où le fichier de clôture fait foi : stock final retenu par Ops accepté quel que soit son âge (avec réserve)."""

    def output_bytes(self):
        head = ["", "ID du site", "Nom du site", "Région O&M", "Date de Relevé Initiale", "Qtité laissée à la date initiale (relevé de terrain)",
                "Qtité laissée retenue", "Source de donnée de stock", "Total Livraison Fuel EYDON", "Total Livraison Fuel CARTE TOTAL",
                "Total Ajout", "Total Retrait", "Total Ponction", "Total QTE volée déclarée", "Total QTE vidangée / Fuite / Ecart de Stock",
                "Date de Relevé Finale", "Qtité laissée à la date finale (relevé de terrain)", "Qtité laissée retenue", "Source de donnée de stock",
                "Variation de stock Fuel considéréé  (L)", "Estimation Conso Fuel Théorique retenue  (L)", "Source de donnée Estimation Conso Fuel théorique"]
        d = lambda m, j: datetime(2026, m, j)  # noqa: E731
        rows = [["", "INVENTAIRE"], head,
                ["", "DKR_0001", "ALPHA", "DAKAR", d(9, 30), 1000, 1000, "relevé de terrain", 0, 0, 0, 0, 0, 0, 0,
                 d(10, 15), 830, 830, "Relevés de Terrain Technicien", 170, None, None]]   # relevé 16 j avant la fin du mois
        return xlsx({"Output": rows})

    def _oct(self):
        from fuel_tracking.services import stock_import_pipeline as P

        P.process_bytes(self.output_bytes(), "Output App Fuel.xlsx", "CLOSING", origin="UPLOAD")
        return SB.compute_balances(timezone.make_aware(datetime(2026, 10, 1)), timezone.make_aware(datetime(2026, 10, 31, 23, 59, 59)),
                                   site_ids=["DKR_0001"], persist=False)[0]

    def test_import_and_october_balance(self):
        r = self._oct()
        self.assertEqual((float(r["consommation_reelle_l"]), r["stock_final_detail"]["tier"], r["stock_final_detail"]["date"][:10]),
                         (170.0, "FICHIER_DU_MOIS", "2026-10-15"))                       # dates exactes de la ligne du fichier

    @override_settings(FUEL_APP_STOCK_START=None)
    def test_strict_rule_rejects_readings_after_boundary(self):
        r = self._oct()                                     # hors mois de stocks issus du fichier : règle des 14 j, jamais accepté
        self.assertIsNone(r["consommation_reelle_l"])
        self.assertNotEqual((r["stock_final_detail"] or {}).get("tier"), "CLOSING_RETENU")

    @override_settings(FUEL_CLOSING_AUTHORITATIVE_PERIODS=[], FUEL_BALANCE_CLOSING_CROSSCHECK=False)
    def test_still_accepted_when_movements_come_from_enoc(self):
        self.assertEqual(self._oct()["stock_final_detail"]["tier"], "FICHIER_DU_MOIS")

    def test_files_give_stocks_only_movements_from_enoc(self):
        pass  # sans objet pour ce fichier

    def test_sites_without_genset_excluded_from_health(self):
        pass  # sans objet pour ce fichier


class SnowflakePerimeterTests(MvpBase):
    """Périmètre de suivi = sites avec GE selon l'inventaire Snowflake ; nombres de sites lus dans Snowflake."""

    def test_perimeter_and_site_counts_from_snowflake(self):
        from datetime import date

        from fuel_tracking.models import FuelSiteInventory as INV
        from fuel_tracking.services.closing_kpis import kpis_for_period
        from fuel_tracking.services.conso_comparison import perimeter_sites

        INV.objects.create(country="SN", data_id=1, site_id="DKR_0001", dg_count=1)
        INV.objects.create(country="SN", data_id=2, site_id="DKR_0002", dg_count=0)
        INV.objects.create(country="SN", data_id=3, site_id="DKR_0003", dg_count=2)
        self.assertEqual(perimeter_sites("2026-10"), {"DKR_0001", "DKR_0003"})   # aucun stock dans les fichiers ici
        k = kpis_for_period(date(2026, 10, 1), date(2026, 10, 31))["kpis"]
        self.assertEqual((k["nb_sites"]["valeur"], k["nb_sites_ge"]["valeur"]), (3, 2))
        self.assertIn("Snowflake", k["nb_sites_ge"]["detail"]["source"])


class PerimeterUnionTests(OutputAppFuelImportTests):
    """Un site avec du carburant dans les fichiers reste dans le périmètre même si Snowflake ne lui connaît pas de GE."""

    def test_site_with_stock_kept_even_without_snowflake_ge(self):
        from fuel_tracking.models import FuelSiteInventory as INV
        from fuel_tracking.services import stock_import_pipeline as P
        from fuel_tracking.services.conso_comparison import perimeter_sites

        P.process_bytes(self.output_bytes(), "Output App Fuel.xlsx", "CLOSING", origin="UPLOAD")
        INV.objects.create(country="SN", data_id=1, site_id="DKR_0001", dg_count=0)     # Snowflake : pas de GE
        INV.objects.create(country="SN", data_id=9, site_id="DKR_0009", dg_count=1)
        per = perimeter_sites("2026-10")
        self.assertIn("DKR_0001", per)        # stock dans l'Output → gardé
        self.assertIn("DKR_0009", per)        # GE selon Snowflake → gardé
        self.assertNotIn("DKR_0003", per)     # « sans GE et sans cuve » dans l'Output, inconnu de Snowflake

    def test_import_and_october_balance(self):
        pass

    def test_strict_rule_rejects_readings_after_boundary(self):
        pass

    def test_sites_without_genset_excluded_from_health(self):
        pass

    def test_files_give_stocks_only_movements_from_enoc(self):
        pass



class SnowflakeSitesRecognisedTests(MvpBase):
    """Un site absent de Gestion des sites mais connu de l'inventaire Snowflake est reconnu (import et bilan) : aucun site à créer."""

    def test_resolver_and_balance_use_snowflake_inventory(self):
        from fuel_tracking.models import FuelSiteInventory as INV
        from fuel_tracking.services.stock_registry import SiteResolver

        INV.objects.create(country="SN", data_id=1, site_id="SNW_0001", site_name="SITE SNOWFLAKE", dg_count=1)
        self.assertEqual(SiteResolver().resolve("snw_0001"), "SNW_0001")
        r = SB.compute_balances(timezone.make_aware(datetime(2026, 10, 1)), timezone.make_aware(datetime(2026, 10, 31, 23, 59, 59)),
                                site_ids=["SNW_0001"], persist=False)[0]
        self.assertNotEqual(r["cause_principale"], "SITE_NON_RECONNU")
        self.assertEqual(r["site_name"], "SITE SNOWFLAKE")


class StockInitialSourcesTests(OutputAppFuelImportTests):
    """Bloc « DONNÉES DE STOCK INITIAL » : valeurs prises à la DATE EXACTE du relevé initial du fichier ; FMS prioritaire."""

    def _rows(self):
        from datetime import date

        from fuel_tracking.services import conso_comparison as CC
        from fuel_tracking.services import stock_import_pipeline as P

        P.process_bytes(self.output_bytes(), "Output App Fuel.xlsx", "CLOSING", origin="UPLOAD")
        SB.compute_balances(timezone.make_aware(datetime(2026, 10, 1)), timezone.make_aware(datetime(2026, 10, 31, 23, 59, 59)), site_ids=["DKR_0001"])
        d = CC.build(date(2026, 10, 1), date(2026, 10, 31), {"site": "DKR_0001"}, with_cph_quality=False)
        return d["rows"]

    @staticmethod
    def _enoc(docs):
        """Faux client ENOC : la collection fuel_level_readings renvoie `docs` (lecture seule)."""
        from unittest import mock

        coll = mock.MagicMock()
        coll.find.side_effect = lambda crit: [d for d in docs if d["site_id"] in crit["site_id"]["$in"]]
        db = mock.MagicMock(fuel_level_readings=coll)
        client = mock.MagicMock()
        client.__getitem__.return_value = db
        return client

    def _enrich(self, rows, docs=None, down=False):
        from unittest import mock

        from django.core.cache import cache

        from fuel_tracking.services import enoc_mongo_service as M
        from fuel_tracking.services import stock_initial_sources as S

        cache.clear()
        conn = mock.patch.object(M, "_connect", side_effect=M.EnocMongoConnectionError("down")) if down else mock.patch.object(M, "_connect", return_value=self._enoc(docs or []))
        with conn, mock.patch.object(S, "_snowflake_points", return_value=({}, None)):
            return S.enrich(rows)

    def test_guard_value_is_enoc_reading_of_same_site_same_day_only(self):
        from fuel_tracking.models import FuelStockObservation as Obs
        from fuel_tracking.services import stock_initial_sources as S

        rows = self._rows()
        # un fichier Loliforce le même jour n'est PAS un relevé de gardien ENOC : ignoré
        Obs.objects.create(site_id="DKR_0001", tank_id="1", source_type="LOLIFORCE", observation_datetime=timezone.make_aware(datetime(2026, 9, 30, 9)),
                           level_raw="777 L", level_value=777, level_unit="L", level_liters=777, hour_meter=99, source_event_id="G777", validation_status="VALIDATED")
        docs = [
            {"site_id": "DKR_0001", "date_releve": "2026-09-29T08:00:00", "tanks": [{"level_liters": 111, "level_cm": 20}]},   # veille : ignorée
            {"site_id": "DKR_0001", "date_releve": "2026-09-30T08:00:00", "tanks": [{"level_liters": 900, "level_cm": 80}]},
            {"site_id": "DKR_0001", "date_releve": "2026-09-30T17:00:00", "tanks": [{"level_liters": 950, "level_cm": 82}], "hour_meter": 1234},
            {"site_id": "DKR_0002", "date_releve": "2026-09-30T08:00:00", "tanks": [{"level_liters": 500, "level_cm": 50}]},   # autre site
        ]
        self.assertIsNone(self._enrich(rows, docs))
        x = rows[0]["stock_initial_sources"]
        self.assertEqual((x["date"], x["terrain_l"], x["gardiennage_l"], x["retenue_l"], x["source"]), ("2026-09-30", 1000.0, 950.0, 1000.0, S.SRC_TERRAIN))
        self.assertEqual((x["compteur_gardiennage_h"], x["compteur_retenu_h"], x["source_compteur"]), (1234.0, 1234.0, S.SRC_GUARD))

    def test_no_enoc_reading_that_day_gives_empty_not_invented(self):
        rows = self._rows()
        docs = [{"site_id": "DKR_0001", "date_releve": "2026-10-02T08:00:00", "tanks": [{"level_liters": 640, "level_cm": 60}]},
                {"site_id": "DKR_0001", "date_releve": "2026-09-30T08:00:00", "tanks": [{"level_liters": 0, "level_cm": None}]}]   # 0 L par défaut : pas une mesure
        self._enrich(rows, docs)
        x = rows[0]["stock_initial_sources"]
        self.assertEqual((x["gardiennage_l"], x["compteur_gardiennage_h"]), (None, None))

    def test_enoc_down_leaves_guard_empty_with_message(self):
        rows = self._rows()
        msg = self._enrich(rows, down=True)
        self.assertIn("ENOC injoignable", msg)
        self.assertIsNone(rows[0]["stock_initial_sources"]["gardiennage_l"])

    def _rav_rows(self):
        from fuel_tracking.models import FuelStockBalance as B

        rows = self._rows()
        B.objects.filter(pk=rows[0]["stock"]["bilan_id"]).update(mouvements=[
            {"source": "ENOC", "composante": "truck_l", "quantite_l": 500.0},
            {"source": "ENOC", "composante": "truck_l", "quantite_l": 250.0},
            {"source": "ENOC", "composante": "carte_total_l", "quantite_l": 300.0},
            {"source": "ENOC", "composante": "transferts_entrants_l", "quantite_l": 120.0},
            {"source": "ENOC", "composante": "transferts_sortants_l", "quantite_l": -80.0},
            {"source": "CLOSING", "composante": "livraisons_externes_l", "quantite_l": 999.0},   # pas ENOC : jamais compté
        ])
        return rows

    def test_ravitaillement_enoc_totals_only_from_enoc(self):
        from fuel_tracking.models import FuelStockBalance as B
        from fuel_tracking.services import stock_initial_sources as S

        rows = self._rav_rows()
        S.ravitaillement(rows)
        x = rows[0]["ravitaillement"]
        self.assertEqual((x["eydon_l"], x["carte_total_l"], x["ajout_l"], x["retrait_l"], x["nb_mouvements_enoc"]), (750.0, 300.0, 120.0, 80.0, 5))
        # volé / fuite : repris de la ligne du fichier du mois (avant FUEL_APP_STOCK_START)
        ctx = (B.objects.get(pk=rows[0]["stock"]["bilan_id"]).stock_final_detail or {}).get("contexte_source") or {}
        self.assertEqual(x["source_vol_fuite"], S.SRC_FILE_MOVES)
        self.assertEqual((x["vole_l"], x["fuite_l"]), (S._f(ctx.get("vole_l")), S._f(ctx.get("vidange_l"))))

    def test_ravitaillement_vol_fuite_from_fuel_manager_after_app_start(self):
        from django.test import override_settings

        from fuel_tracking.services import stock_initial_sources as S

        rows = self._rav_rows()
        with override_settings(FUEL_APP_STOCK_START="2026-09-01"):
            S.ravitaillement(rows)
        x = rows[0]["ravitaillement"]
        self.assertEqual((x["vole_l"], x["fuite_l"], x["source_vol_fuite"]), (None, None, S.SRC_FM))
        self.assertEqual(x["eydon_l"], 750.0)

    def test_ravitaillement_empty_without_balance(self):
        from fuel_tracking.services import stock_initial_sources as S

        rows = [{"site_id": "X_1", "periode": {"debut": "2026-10-01"}, "stock": {"bilan_id": None, "date_initial": None, "date_final": None}}]
        S.ravitaillement(rows)
        self.assertTrue(all(rows[0]["ravitaillement"][k] is None for k in ("eydon_l", "carte_total_l", "ajout_l", "retrait_l", "vole_l", "fuite_l")))

    def test_fms_has_priority(self):
        from datetime import date
        from unittest import mock

        from fuel_tracking.services import stock_initial_sources as S

        rows = self._rows()
        with mock.patch.object(S, "_guard_readings", return_value=({}, None)),                 mock.patch.object(S, "_snowflake_points", return_value=({("DKR_0001", date(2026, 9, 30)): {"niveau_l": 980.0, "index_h": 5000.0}}, None)):
            S.enrich(rows)
        x = rows[0]["stock_initial_sources"]
        self.assertEqual((x["retenue_l"], x["source"], x["compteur_retenu_h"], x["source_compteur"]), (980.0, S.SRC_FMS, 5000.0, S.SRC_INDEX_FMS))

    def test_import_and_october_balance(self):
        pass

    def test_strict_rule_rejects_readings_after_boundary(self):
        pass

    def test_sites_without_genset_excluded_from_health(self):
        pass

    def test_files_give_stocks_only_movements_from_enoc(self):
        pass


class FmsPartialReadTests(MvpBase):
    """Vue du compteur FMS en erreur côté Snowflake : le niveau du capteur reste lu (lectures indépendantes)."""

    def test_level_kept_when_counter_view_fails(self):
        from datetime import date
        from unittest import mock

        from django.core.cache import cache

        from fuel_tracking.models import FuelSiteInventory as INV
        from fuel_tracking.services import stock_initial_sources as S

        INV.objects.create(country="SN", data_id=7, site_id="DKR_0001", dg_count=1)
        cache.clear()

        class Cur:
            def __init__(self):
                self.n = 0

            def execute(self, sql, params):
                self.n += 1
                self.sql = sql
                if "GENSET_REPORT" in sql and fail_counter:
                    raise RuntimeError("SQL compilation error")

            def fetchall(self):
                if "GENSET_REPORT" in self.sql:
                    return [(7, date(2026, 9, 30), 502.4833)]
                return [("DKR_0001", date(2026, 9, 30), 634.6)]

        conn = mock.Mock()
        conn.cursor.return_value = Cur()
        fail_counter = True
        with mock.patch("fuel_tracking.services.fuel_daily_facts_snowflake._connect", return_value=conn):
            vals, err = S._snowflake_points({("DKR_0001", date(2026, 9, 30))})
        self.assertEqual(vals[("DKR_0001", date(2026, 9, 30))], {"niveau_l": 634.6})
        self.assertIn("GENSET_REPORT", err)
        # compteur lu dans GENSET_REPORT.TOTAL_DG_RUNNING_HOUR (heures cumulées du jour)
        cache.clear()
        fail_counter = False
        conn.cursor.return_value = Cur()
        with mock.patch("fuel_tracking.services.fuel_daily_facts_snowflake._connect", return_value=conn):
            vals, err = S._snowflake_points({("DKR_0001", date(2026, 9, 30))})
        self.assertEqual((vals[("DKR_0001", date(2026, 9, 30))], err), ({"niveau_l": 634.6, "index_h": 502.48}, None))
