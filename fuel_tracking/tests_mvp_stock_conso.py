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
from django.test import SimpleTestCase, TestCase, override_settings
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

    def test_type_de_ge_1_et_2(self):
        """Closing de septembre : « Marque GE installé #1/#2 » et « Type de GE #1/#2 » → type de GE de la ligne (24/11/2026)."""
        head = ["Id du site", "Marque GE installé #1", "New GE/Reused #1", "Type de GE #1", "Puissance PRP (KVA) #1",
                "Marque GE installé #2", "New GE/Reused #2", "Type de GE #2", "Date de relevé initiale",
                "Qtité laissée à la date du relevé", "Date de relevé finale", "Qtité laissée à la date du relevé", "Test date"]
        d0, d1 = datetime(2026, 8, 31), datetime(2026, 9, 30)
        rows = [["Synthèse"], head, ["DKR_0001", "SDMO", "New", "K22", 22, "FG Wilson", "New", "P33-3", d0, 500, d1, 300, None],
                ["DKR_0002", "SDMO", "New", "J66K", 60, 0, None, 0, d0, 500, d1, 300, None],
                ["DKR_0003", None, None, None, None, None, None, None, d0, 500, d1, 300, None]]
        data, _diag = closing_stock_rows(rows)
        self.assertEqual([(r["fabricant_ge"], r["type_ge"]) for r in data],
                         [("SDMO / FG Wilson", "K22 / P33-3"), ("SDMO", "J66K"), (None, None)])


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
        """Ligne du fichier DKR_0001 + mouvements ENOC dedans / hors de sa plage de dates, et des mouvements à ne jamais compter."""
        from datetime import date

        from fuel_tracking.models import FuelEnocMovement as EM, FuelEnocQualifiedMovement as EQ

        rows = self._rows()
        d0, d1 = date.fromisoformat(rows[0]["stock"]["date_initial"]), date.fromisoformat(rows[0]["stock"]["date_final"])
        tz = timezone.get_current_timezone()
        inside, before = timezone.make_aware(datetime.combine(d0, datetime.min.time()) + timedelta(days=1, hours=10), tz), \
            timezone.make_aware(datetime.combine(d0, datetime.min.time()) - timedelta(days=2), tz)
        for n, (fam, qty, when, integ) in enumerate([("TRUCK", 500, inside, "INTEGRE"), ("TRUCK", 250, inside, "INTEGRE"), ("TOTAL_CARD", 300, inside, "INTEGRE"),
                                                      ("TRUCK", 900, before, "INTEGRE"), ("TRUCK", 70, inside, "NON_INTEGRABLE")]):
            EQ.objects.create(enoc_movement=EM.objects.create(source_id=f"E{n}"), type_family=fam, business_classification="LIVRAISON", execution_state="EXECUTEE",
                              integration_status=integ, site_id_resolved="DKR_0001", accounting_at=when, integrated_quantity_l=Decimal(qty))
        for n, (src, mtype, qty, impact, when, status) in enumerate([
                ("ENOC", "TRANSFERT_INTERSITE_ENTRANT", 120, 120, inside, "VALIDATED"), ("ENOC", "TRANSFERT_INTERSITE_SORTANT", 80, -80, inside, "VALIDATED"),
                ("ENOC", "THEFT", 40, None, inside, "VALIDATED"), ("ENOC", "DRAIN", 15, None, inside, "VALIDATED"),
                ("ENOC", "THEFT", 999, None, inside, "REJECTED"), ("CLOSING", "THEFT", 555, None, inside, "VALIDATED"), ("ENOC", "DRAIN", 33, None, before, "VALIDATED")]):
            FuelMovement.objects.create(source_type=src, source_event_id=f"M{n}", site_id="DKR_0001", movement_type=mtype, movement_datetime=when,
                                        quantity_l=Decimal(qty), impact_stock_l=None if impact is None else Decimal(impact), status=status,
                                        retained_in_stock=True, transfer_id=f"T{n}" if mtype.startswith("TRANSFERT") else None)
        return rows, d0, d1

    def test_ravitaillement_all_six_from_enoc_on_file_dates(self):
        from unittest import mock

        from fuel_tracking.services import stock_initial_sources as S

        rows, d0, d1 = self._rav_rows()
        with mock.patch("fuel_tracking.services.stock_enoc.check_transfer_balance", return_value=[]):
            S.ravitaillement(rows)
        x = rows[0]["ravitaillement"]
        self.assertEqual((x["debut"], x["fin"], x["source"]), (d0.isoformat(), d1.isoformat(), "ENOC"))
        self.assertEqual({k: x[k] for k in S.RAV_KEYS},
                         {"eydon_l": 750.0, "carte_total_l": 300.0, "ajout_l": 120.0, "retrait_l": 80.0, "vole_l": 40.0, "fuite_l": 15.0})
        self.assertEqual(x["nb_mouvements_enoc"], 7)

    def test_ravitaillement_unbalanced_ponction_not_counted(self):
        from unittest import mock

        from fuel_tracking.services import stock_initial_sources as S

        rows, _, _ = self._rav_rows()
        with mock.patch("fuel_tracking.services.stock_enoc.check_transfer_balance", return_value=[{"transfer_id": "T0"}]):
            S.ravitaillement(rows)
        self.assertEqual((rows[0]["ravitaillement"]["ajout_l"], rows[0]["ravitaillement"]["retrait_l"]), (0.0, 80.0))

    def test_ravitaillement_empty_without_balance(self):
        from fuel_tracking.services import stock_initial_sources as S

        rows = [{"site_id": "X_1", "periode": {"debut": "2026-10-01"}, "stock": {"bilan_id": None, "date_initial": None, "date_final": None}}]
        S.ravitaillement(rows)
        self.assertTrue(all(rows[0]["ravitaillement"][k] is None for k in S.RAV_KEYS))   # pas de dates de fichier : vide, jamais 0

    def _set_terrain_counter(self, rows, value, periode_start=None):
        """Compteur de terrain du fichier sur les relevés de clôture du bilan (comme après import / reprise)."""
        from fuel_tracking.models import FuelStockBalance as B

        b = B.objects.get(pk=rows[0]["stock"]["bilan_id"])
        for d in (b.stock_initial_detail, b.stock_final_detail):
            o = Obs.objects.get(pk=d["observation_id"])
            ctx = dict(o.source_context or {})
            if periode_start:
                ctx["periode"] = [periode_start, (ctx.get("periode") or [None, None])[1]]
            o.source_context = {**ctx, "compteur_terrain_initial_h": value}
            o.save(update_fields=["source_context"])

    def test_retained_counter_order_fms_then_guard_then_terrain(self):
        from datetime import date
        from unittest import mock

        from fuel_tracking.services import stock_initial_sources as S

        d = date(2026, 9, 30)
        cases = [({"index_h": 5000.0}, {"compteur": 1234.0}, (5000.0, S.SRC_INDEX_FMS)),
                 ({}, {"compteur": 1234.0}, (1234.0, S.SRC_GUARD)),
                 ({}, {}, (777.5, S.SRC_TERRAIN))]
        for fms, guard, expected in cases:
            rows = self._rows()
            self._set_terrain_counter(rows, 777.5)
            with mock.patch.object(S, "_snowflake_points", return_value=({("DKR_0001", d): fms}, None)),                     mock.patch.object(S, "_guard_readings", return_value=({("DKR_0001", d): guard}, None)):
                S.enrich(rows)
            x = rows[0]["stock_initial_sources"]
            self.assertEqual(x["compteur_terrain_h"], 777.5)
            self.assertEqual((x["compteur_retenu_h"], x["source_compteur"]), expected)

    def test_file_line_dates_rule_for_stock_initial(self):
        """Ligne du fichier de la période (même Date de Relevé Finale) : SA Date de Relevé Initiale fait foi (cas KLD_0530 :
        ligne 15/07 → 29/10 alors que le bilan démarre au 01/10) ; compteur et date affichés sont ceux de cette ligne."""
        from unittest import mock

        from fuel_tracking.services import stock_initial_sources as S

        rows = self._rows()
        self._set_terrain_counter(rows, 2137.41, periode_start="2026-07-15")
        with mock.patch.object(S, "_snowflake_points", return_value=({}, None)), mock.patch.object(S, "_guard_readings", return_value=({}, None)):
            S.enrich(rows)
        x = rows[0]["stock_initial_sources"]
        self.assertEqual((x["date"], x["compteur_terrain_h"], x["source_compteur"]), ("2026-07-15", 2137.41, S.SRC_TERRAIN))

    def test_terrain_counter_of_another_period_never_used(self):
        """Ligne d'une AUTRE période (autres dates initiale et finale) : jamais reprise."""
        from unittest import mock

        from fuel_tracking.models import FuelStockBalance as B
        from fuel_tracking.services import stock_initial_sources as S

        rows = self._rows()
        b = B.objects.get(pk=rows[0]["stock"]["bilan_id"])
        for d in (b.stock_initial_detail, b.stock_final_detail):
            o = Obs.objects.get(pk=d["observation_id"])
            o.source_context = {**o.source_context, "periode": ["2026-06-01", "2026-06-30"], "compteur_terrain_initial_h": 999.0}
            o.save(update_fields=["source_context"])
        with mock.patch.object(S, "_snowflake_points", return_value=({}, None)), mock.patch.object(S, "_guard_readings", return_value=({}, None)):
            S.enrich(rows)
        x = rows[0]["stock_initial_sources"]
        self.assertEqual((x["compteur_terrain_h"], x["compteur_retenu_h"]), (None, None))

    def test_final_block_order_fms_then_guard_then_terrain(self):
        from unittest import mock

        from fuel_tracking.models import FuelStockBalance as B
        from fuel_tracking.services import stock_initial_sources as S

        def rows_with_terrain_values():
            """Lignes du tableau, puis valeurs de terrain posées sur les relevés de la ligne (une réimportation les réécrit)."""
            rows = self._rows()
            b = B.objects.get(pk=rows[0]["stock"]["bilan_id"])
            for d in (b.stock_initial_detail, b.stock_final_detail):   # même ligne de fichier : mêmes valeurs sur ses deux relevés
                o = Obs.objects.get(pk=d["observation_id"])
                o.source_context = {**o.source_context, "stock_terrain_final_l": 410.0, "compteur_terrain_final_h": 1300.0}
                o.save(update_fields=["source_context"])
            return rows, date.fromisoformat(o.source_context["periode"][1])

        _, df = rows_with_terrain_values()
        cases = [({"niveau_l": 395.0, "index_h": 1310.0}, {"litres": 400.0, "compteur": 1305.0}, (395.0, S.SRC_FMS, 1310.0, S.SRC_INDEX_FMS)),
                 ({}, {"litres": 400.0, "compteur": 1305.0}, (400.0, S.SRC_GUARD, 1305.0, S.SRC_GUARD)),
                 ({}, {}, (410.0, S.SRC_TERRAIN, 1300.0, S.SRC_TERRAIN))]
        for fms, guard, expected in cases:
            rows, _ = rows_with_terrain_values()
            with mock.patch.object(S, "_snowflake_points", return_value=({("DKR_0001", df): fms}, None)),                     mock.patch.object(S, "_guard_readings", return_value=({("DKR_0001", df): guard}, None)):
                S.enrich(rows)
            x = rows[0]["stock_final_sources"]
            self.assertEqual((x["date"], x["terrain_l"], x["compteur_terrain_h"]), (df.isoformat(), 410.0, 1300.0))
            self.assertEqual((x["retenue_l"], x["source"], x["compteur_retenu_h"], x["source_compteur"]), expected)

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


class FmsFromDailyFactsTests(MvpBase):
    """Niveau FMS et compteur lus dans les faits journaliers en base (copiés de Snowflake chaque heure) : aucun appel Snowflake."""

    def _fact(self, data_id, site, d, lvl=None, hours=None):
        from fuel_tracking.models import FuelSiteDailyFacts as F

        return F.objects.create(country="Senegal", data_id=data_id, site_id=site, date=d,
                                fms_level_l=None if lvl is None else Decimal(str(lvl)), dg_total_running_h=None if hours is None else Decimal(str(hours)))

    def test_values_read_from_daily_facts_without_snowflake(self):
        from datetime import date
        from unittest import mock

        from fuel_tracking.services import stock_initial_sources as S

        d = date(2026, 9, 30)
        self._fact(1, "S_1", d, 634.6, 502.4833)
        self._fact(2, "S_2", d)                      # synchronisé, capteur sans mesure : n.d., jamais 0
        self._fact(3, "S_3", d, 100, 10)             # S_3 a deux DATA_ID contradictoires : aucune valeur
        self._fact(4, "S_3", d, 120, 10)
        with mock.patch("fuel_tracking.services.fuel_daily_facts_snowflake._connect", side_effect=AssertionError("Snowflake appelé")):
            vals, err = S._snowflake_points({("S_1", d), ("S_2", d), ("S_3", d)})
        self.assertIsNone(err)
        self.assertEqual(vals, {("S_1", d): {"niveau_l": 634.6, "index_h": 502.48}, ("S_3", d): {"index_h": 10.0}})

    def test_day_not_synced_yet_is_reported_and_sync_requested(self):
        from datetime import date
        from unittest import mock

        from fuel_tracking.services import stock_initial_sources as S

        with mock.patch.object(S, "request_fact_days") as req:
            vals, err = S._snowflake_points({("S_1", date(2026, 9, 30))})
        self.assertEqual(vals, {})
        self.assertIn("30/09/2026 en cours de synchronisation", err)
        req.assert_called_once_with([date(2026, 9, 30)])

    def test_day_synced_for_other_sites_is_not_reported(self):
        """Site sans faits ce jour-là (ex. sans GE dans l'inventaire Snowflake) alors que le jour est synchronisé : pas de message."""
        from datetime import date
        from unittest import mock

        from fuel_tracking.services import stock_initial_sources as S

        d = date(2026, 9, 30)
        self._fact(1, "AUTRE_SITE", d, 500, 10)
        with mock.patch.object(S, "request_fact_days") as req:
            vals, err = S._snowflake_points({("KLK_0715", d)})
        self.assertEqual((vals, err), ({}, None))
        req.assert_not_called()

    def test_snapshot_level_used_when_fact_has_no_level(self):
        from datetime import date

        from fuel_tracking.models import FuelStockSnapshot as SNAP
        from fuel_tracking.services import stock_initial_sources as S

        d = date(2026, 9, 30)
        self._fact(1, "S_1", d, None, 7)
        SNAP.objects.create(site_id="S_1", snapshot_year=2026, snapshot_month=9, stock_snowflake_l=Decimal("640.5"), stock_snowflake_date=d)
        vals, _ = S._snowflake_points({("S_1", d)})
        self.assertEqual(vals[("S_1", d)], {"niveau_l": 640.5, "index_h": 7.0})

    def test_sync_query_reads_level_and_counter(self):
        from fuel_tracking.services.fuel_daily_facts_snowflake import build_daily_facts_sql

        sql = build_daily_facts_sql([1], with_tracker=False)
        self.assertIn("MAX(LAST_VALID_LEVEL)", sql)
        self.assertIn("MAX(TOTAL_DG_RUNNING_HOUR)", sql)
        self.assertIn("g.total_h, f.fms_level", sql)
        # Load DC telco (AVERAGE_LOAD, joint sur ID = DATA_ID) et Ah batterie (BATTERY_REPORT)
        self.assertIn("DB_GFMS_PROD.GOLD.AVERAGE_LOAD", sql)
        self.assertIn("DB_GFMS_ANALYTICS_PROD.GOLD.BATTERY_REPORT", sql)
        self.assertIn("ld.load_w, b.charge_ah, b.discharge_ah, bd.batt_points, bd.batt_pos", sql)
        # Recharge batterie (équipe data) : médiane du jour lue dans GOLD.BATTERY_REPORT, plus de télémétrie 5 min
        self.assertIn("MAX(BATTERY_CHARGE_POWER_MEDIAN_KW)", sql)
        self.assertNotIn("GFMS_METRICS", sql)
        # Puissance redresseur (règle équipe data) : correction du rendement AVANT la médiane, filtres de qualité
        self.assertIn("P_DC / (RECTIFIER_EFFICIENCY / 100.0) / 1000.0", sql)
        self.assertIn("RECTIFIER_EFFICIENCY BETWEEN 70 AND 100", sql)
        self.assertIn("MEDIAN(rect_kw)", sql)
        self.assertNotIn("MEDIAN(P_DC)", sql)
        # Le pilote Snowflake interpole les paramètres avec « % » : aucun « % » littéral hors %(nom)s, même en commentaire
        import re

        for tr in (True, False):
            q = build_daily_facts_sql([1], with_tracker=tr)
            self.assertEqual(re.findall(r"%(?!\(\w+\)s|%)", q), [], f"« % » littéral (tracker={tr})")
        self.assertIn("rect_power_daily AS (", sql)   # la puissance redresseur reste calculée


class StockInitialPairCacheTests(MvpBase):
    """Relevés de gardien ENOC gardés par (site, jour) : changer de page ou de taille de page ne relit que ce qui manque."""

    def test_only_missing_pairs_are_fetched(self):
        from datetime import date
        from unittest import mock

        from django.core.cache import cache

        from fuel_tracking.services import stock_initial_sources as S

        cache.clear()
        calls = []

        def fake(pairs):
            calls.append(set(pairs))
            return {p: {"litres": 100.0, "compteur": None} for p in pairs if p[0] != "S_3"}, None

        d = date(2026, 9, 30)
        with mock.patch.object(S, "_fetch_guard", side_effect=fake):
            S._guard_readings({("S_1", d), ("S_2", d)})
            b, err = S._guard_readings({("S_1", d), ("S_2", d), ("S_3", d)})   # 200 par page : seul S_3 est lu
        self.assertIsNone(err)
        self.assertEqual(calls, [{("S_1", d), ("S_2", d)}, {("S_3", d)}])
        self.assertEqual((b[("S_1", d)]["litres"], b[("S_3", d)]), (100.0, {}))
        self.assertEqual(S.missing_pairs({("S_3", d), ("S_9", d)}), {("S_9", d)})

    def test_failure_is_kept_with_message_but_retried_later(self):
        from datetime import date
        from unittest import mock

        from django.core.cache import cache

        from fuel_tracking.services import stock_initial_sources as S

        cache.clear()
        d = date(2026, 9, 30)
        with mock.patch.object(S, "_fetch_guard", return_value=({}, "ENOC injoignable : relevés de gardien non lus.")) as f:
            v1, e1 = S._guard_readings({("S_1", d)})
            v2, e2 = S._guard_readings({("S_1", d)})
        self.assertEqual(f.call_count, 1)
        self.assertEqual((v1, e1, e2), ({("S_1", d): {}}, "ENOC injoignable : relevés de gardien non lus.", "ENOC injoignable : relevés de gardien non lus."))

    def test_fetch_false_never_reads_sources(self):
        from datetime import date
        from unittest import mock

        from django.core.cache import cache

        from fuel_tracking.services import stock_initial_sources as S

        cache.clear()
        with mock.patch.object(S, "_fetch_guard") as f:
            vals, err = S._guard_readings({("S_9", date(2026, 9, 30))}, fetch=False)
        f.assert_not_called()
        self.assertEqual((vals, err), ({}, None))


class FmsSnapshotLevelTests(MvpBase):
    """Niveau FMS archivé par la synchro de stock : utilisé seulement à la date EXACTE, sans appel Snowflake."""

    def test_snapshot_level_exact_day_only(self):
        from datetime import date

        from fuel_tracking.models import FuelStockSnapshot as SNAP
        from fuel_tracking.services import stock_initial_sources as S

        d = date(2026, 9, 30)
        SNAP.objects.create(site_id="S_1", snapshot_year=2026, snapshot_month=9, stock_snowflake_l=Decimal("640.5"), stock_snowflake_date=d)
        SNAP.objects.create(site_id="S_2", snapshot_year=2026, snapshot_month=9, stock_snowflake_l=Decimal("300"), stock_snowflake_date=date(2026, 9, 28))
        SNAP.objects.create(site_id="S_3", snapshot_year=2026, snapshot_month=9, stock_snowflake_l=Decimal("100"), stock_snowflake_date=d)
        SNAP.objects.create(site_id="S_3", stock_snowflake_l=Decimal("120"), stock_snowflake_date=d)   # contradictoire : aucune valeur
        self.assertEqual(S._snapshot_levels({("S_1", d), ("S_2", d), ("S_3", d)}), {("S_1", d): 640.5})


class BackfillFmsLevelsTests(MvpBase):
    """Reprise du niveau FMS : complète les faits existants, ne crée jamais de jour à moitié, Snowflake en lecture seule."""

    def test_updates_existing_rows_only(self):
        from datetime import date
        from io import StringIO
        from unittest import mock

        from django.core.management import call_command

        from fuel_tracking.models import FuelSiteDailyFacts as F, FuelSiteInventory as INV

        INV.objects.create(country="Senegal", data_id=7, site_id="DKR_0001", dg_count=1)
        F.objects.create(country="Senegal", data_id=7, site_id="DKR_0001", date=date(2026, 9, 30), dse_runtime_h=Decimal("5"))
        sqls = []

        class Cur:
            def execute(self, sql, params):
                sqls.append(sql)
                self.sql = sql

            def fetchall(self):
                if "VW_FUEL_REPORT" in self.sql:
                    return [(7, date(2026, 9, 30), 634.6), (7, date(2026, 9, 29), 650.0)]   # 29/09 : pas de ligne de faits
                return [(7, date(2026, 9, 30), 502.4833)]

        conn = mock.Mock()
        conn.cursor.return_value = Cur()
        out = StringIO()
        with mock.patch("fuel_tracking.services.fuel_daily_facts_snowflake._connect", return_value=conn):
            call_command("backfill_fms_levels", start=date(2026, 9, 29), end=date(2026, 9, 30), stdout=out)
        row = F.objects.get(data_id=7, date=date(2026, 9, 30))
        self.assertEqual((float(row.fms_level_l), float(row.dg_total_running_h), float(row.dse_runtime_h)), (634.6, 502.4833, 5.0))
        self.assertFalse(F.objects.filter(date=date(2026, 9, 29)).exists())
        self.assertIn("sync_fuel_daily_facts --start 2026-09-29", out.getvalue())
        self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql in sqls))   # lecture seule


class TerrainFinalParsingTests(MvpBase):
    """Bloc final des fichiers : quantité et compteur relevés sur le terrain à la Date de Relevé Finale."""

    def test_closing_and_output_final_terrain_values(self):
        from fuel_tracking.services.output_app_fuel import parse_rows
        from fuel_tracking.services.stock_file_analysis import closing_stock_rows

        head = ["ID du site", "Date de Relevé Initiale", "Qtité laissée à la date du relevé", "Relevé Visuel Compteur genset  à la date de relevé",
                "Date de Relevé Finale", "Qtité laissée à la date du relevé", "Relevé Visuel Compteur genset  à la date de relevé"]
        r = closing_stock_rows([head, ["DKR_0001", "30/09/2026", 500, 1200.5, "28/10/2026", 300, 1300]])[0][0]
        self.assertEqual((r["stock_terrain_final_l"], r["compteur_terrain_final_h"]), (300, 1300))
        head = ["ID du site", "Nom du site", "Date de Relevé Initiale", "Qtité laissée à la date initiale (relevé de terrain)",
                "Relevé Visuel Compteur genset (relevé de terrain)", "Date de Relevé Finale", "Qtité laissée à la date finale (relevé de terrain)",
                "Qtité laissée à la date finale (Société de gardiennage)", "Qtité laissée retenue", "Source de donnée de stock",
                "Relevé Visuel Compteur genset (relevé de terrain)"]
        r = parse_rows([head, ["DKR_0001", "X", "30/09/2026", 500, 324.42, "28/10/2026", 280, 290, 295, "Fuel Sensor", 400]])[0]
        self.assertEqual((r["stock_terrain_final_l"], r["compteur_terrain_final_h"], r["stock_final_l"]), (280, 400, 295))


class TerrainCounterParsingTests(MvpBase):
    """Compteur genset relevé sur le terrain à la date initiale : lu dans le Closing et dans l'Output App Fuel."""

    def test_closing_reads_counter_of_initial_block_only(self):
        from fuel_tracking.services.stock_file_analysis import closing_stock_rows

        head = ["ID du site", "Date de Relevé Initiale", "Qtité laissée à la date du relevé", "Relevé Visuel Compteur genset  à la date de relevé",
                "Date de Relevé Finale", "Qtité laissée à la date du relevé", "Relevé Visuel Compteur genset  à la date de relevé"]
        rows, _ = closing_stock_rows([head, ["DKR_0001", "30/09/2026", 500, 1200.5, "28/10/2026", 300, 1300]])
        self.assertEqual(rows[0]["compteur_terrain_initial_h"], 1200.5)

    def test_output_app_fuel_reads_terrain_counter(self):
        from fuel_tracking.services.output_app_fuel import parse_rows

        head = ["ID du site", "Nom du site", "Date de Relevé Initiale", "Qtité laissée à la date initiale (relevé de terrain)",
                "Relevé Visuel Compteur genset \n(relevé de terrain)", "Relevé Visuel Compteur genset \n(Société de gardiennage)",
                "Date de Relevé Finale", "Qtité laissée retenue", "Source de donnée de stock", "Relevé Visuel Compteur genset \n(relevé de terrain)"]
        r = parse_rows([head, ["DKR_0001", "X", "30/09/2026", 500, 324.42, 999, "28/10/2026", 300, "Relevé terrain", 400]])[0]
        self.assertEqual(r["compteur_terrain_initial_h"], 324.42)


class GeInfoTests(MvpBase):
    """Fabricant / type de GE, champ par champ : Snowflake SITE_DG, puis historique SITE_DG_DAILY, puis ENOC ; jamais déduit."""

    def test_order_per_field_and_source(self):
        from fuel_tracking.services import ge_info as G

        sf = {"S_1": [(2, ("FG Wilson", G.SRC_SF), ("P22-1", G.SRC_SF)), (1, ("CATERPILLAR", G.SRC_SF), ("P33-3U", G.SRC_SF))],
              "S_2": [(1, (None, None), ("K22", G.SRC_SF))],                        # fabricant absent de Snowflake
              "S_3": [(1, ("Olympian", G.SRC_SF_HIST), ("GEP22-2", G.SRC_SF_HIST))]}  # retrouvé dans l'historique
        en = {"S_1": [("SDMO", "K22")], "S_2": [("SDMO", "K22")], "S_4": [("AKSA", "AP33")], "S_5": [("None", None)]}
        out = G.build(sf, en)
        # type : ENOC d'abord (décision métier) ; fabricant : Snowflake d'abord
        self.assertEqual(out["S_1"], {"fabricant_ge": "CATERPILLAR / FG Wilson", "type_ge": "K22",
                                      "source_ge": f"Fabricant : {G.SRC_SF} · Type : {G.SRC_ENOC}"})
        self.assertEqual(out["S_2"], {"fabricant_ge": "SDMO", "type_ge": "K22", "source_ge": G.SRC_ENOC})
        self.assertEqual(out["S_3"]["source_ge"], G.SRC_SF_HIST)                       # pas d'ENOC : type Snowflake (historique)
        self.assertEqual(out["S_4"], {"fabricant_ge": "AKSA", "type_ge": "AP33", "source_ge": G.SRC_ENOC})
        self.assertNotIn("S_5", out)

    def test_snowflake_current_then_history_with_guard(self):
        from unittest import mock

        from fuel_tracking.services import ge_info as G

        cur = mock.Mock()
        conn = mock.Mock()
        conn.cursor.return_value = cur
        with mock.patch("fuel_tracking.services.fuel_daily_facts_snowflake._connect", return_value=conn):
            # (S_ID, NOM, VENDOR, GENSET_TYPE, VENDOR historique, TYPE historique, SITE_ID, nb de SITE_ID)
            cur.fetchall.return_value = [(10, 1, "SDMO", None, "Kohler", "J88K", "DKR_0001", 1),
                                         (11, 1, None, None, "Olympian", "GEP22-2", "DKR_0002", 1),
                                         (12, 1, "X", "Y", None, None, None, 2)]   # S_ID ambigu : jamais utilisé
            out, meta = G.fetch_snowflake()
            self.assertEqual(out["DKR_0001"], [(1, ("SDMO", G.SRC_SF), ("J88K", G.SRC_SF_HIST))])
            self.assertEqual(out["DKR_0002"], [(1, ("Olympian", G.SRC_SF_HIST), ("GEP22-2", G.SRC_SF_HIST))])
            self.assertEqual(len(out), 2)
            cur.fetchall.return_value = [(10, 1, "A", "B", None, None, None, 0), (11, 1, "A", "B", None, None, None, 0),
                                         (12, 1, "A", "B", None, None, "DKR_0003", 1)]
            out, meta = G.fetch_snowflake()   # 1 S_ID sur 3 rattaché : garde-fou
            self.assertEqual(out, {})
            self.assertIn("Snowflake non utilisé", meta["ignore"])
        sql = cur.execute.call_args[0][0]
        self.assertIn("e.ID = g.s_id", sql)
        self.assertIn("SITE_DG_DAILY", sql)
        self.assertTrue(sql.lstrip().upper().startswith("WITH"))
        self.assertNotRegex(sql.upper(), r"(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP)")   # lecture seule

    def test_enrich_reads_cache_only(self):
        from unittest import mock

        from django.core.cache import cache

        from fuel_tracking.services import ge_info as G
        from fuel_tracking.services import stock_initial_sources as S

        cache.set(G.CACHE_KEY, {"raw": {"sf": {"DKR_0001": [(1, ("SDMO", G.SRC_SF), ("K22", G.SRC_SF))]}, "enoc": {}, "gdb": {"K22": {20.0}},
                                        "gdb_meta": {"fichier": "Closing sept.xlsb"}}, "lu_le": "2026-11-19T10:00:00+00:00"})
        rows = [{"site_id": "DKR_0001", "stock": {"bilan_id": None}}, {"site_id": "DKR_0002", "stock": {"bilan_id": None}}]
        with mock.patch.object(S, "_guard_readings", return_value=({}, None)), mock.patch.object(G, "fetch_snowflake", side_effect=AssertionError("Snowflake lu")):
            S.enrich(rows)
        self.assertEqual(rows[0]["estimation_theorique"]["type_ge"], "K22")
        # site inconnu : aucune valeur chiffrée (tout n.d., jamais 0) ; seuls les motifs et indicateurs de réserve sont renseignés
        e1 = rows[1]["estimation_theorique"]
        self.assertEqual([k for k, v in e1.items() if v is not None and not isinstance(v, (str, bool))], [])
        self.assertFalse(any(v for v in e1.values() if isinstance(v, bool)))
        self.assertIn("aucune puissance", e1["p_retenue_motif"])
        self.assertEqual((rows[0]["estimation_theorique"]["prp_kva"], rows[0]["estimation_theorique"]["prp_kw"]), ([20.0], [16.0]))


class PrpGensetDbTests(MvpBase):
    """Puissance PRP (kVA) : GENSET DB du fichier de clôture, type de GE exact ; PRP (kW) = kVA × 0,8 ; ambigu → n.d."""

    def test_parse_and_lookup(self):
        from fuel_tracking.services import ge_info as G

        rows = [["", "Manufacturer ", "Type", "Genset List", "Type de GE", "", "Voltage [V]", "No. of phases", "PRP [kVA]", "PRP [kW]"],
                ["", "", "", "", "Site avec cuve et sans GE"],
                ["", "SDMO", "", "", "D275", 275, 400, 3, 250, 200],
                ["", "FG Wilson", "P110-3 (DSE4520)", "x", "P110-3 ", 110, 400, 3, 100, 80],
                ["", "FG Wilson", "P110-3 (DSE7320)", "x", "P110-3 ", 110, 400, 3, 100, 80],   # même PRP : non ambigu
                ["", "SDMO", "", "", "J220K", 220, 400, 3, 200, 160],
                ["", "SDMO", "", "", "J220K", 220, 400, 3, 220, 176]]                          # 2 PRP : ambigu
        gdb = G.parse_genset_db(rows)
        self.assertEqual({k: gdb[k] for k in ("D275", "P1103", "J220K")}, {"D275": {250.0}, "P1103": {100.0}, "J220K": {200.0, 220.0}})
        self.assertEqual((gdb["SDMOD275"], gdb["FGWILSONP1103"]), ({250.0}, {100.0}))   # clé « fabricant + type » aussi
        sites = {"A": {"type_ge": "p110-3"}, "B": {"type_ge": "D275 / J220K"}, "C": {"type_ge": "P33-3U"}, "D": {"type_ge": None}}
        G.add_prp(sites, gdb, "Closing.xlsb")
        self.assertEqual((sites["A"]["prp_kva"], sites["A"]["prp_kw"], sites["A"]["prp_source"]), ([100.0], [80.0], "GENSET DB (Closing.xlsb)"))
        self.assertEqual((sites["B"]["prp_kva"], sites["B"]["prp_kw"]), ([250.0], [200.0]))
        self.assertIn("ambigu", sites["B"]["prp_motif"])
        self.assertEqual((sites["C"]["prp_kva"], sites["C"]["prp_motif"]), ([30.0], None))   # fiche constructeur FC-07 (25/11)
        self.assertEqual((sites["D"]["prp_kva"], sites["D"]["prp_motif"]), (None, None))

    def test_courbes_cph_genset_db(self):
        """Coefficients a, b, c de GENSET DB par type de GE ; colonnes absentes → ajustement exact des points 50/75/100 %."""
        from fuel_tracking.services import ge_info as G

        head = ["", "Manufacturer ", "Type", "Genset List", "Type de GE", "", "Voltage [V]", "No. of phases", "PRP [kVA]", "PRP [kW]",
                "cosφ", "Conso 100%", "Conso 75%", "Conso 50%", "a", "b", "c"]
        rows = [head,
                ["", "SDMO", "", "", "K22", 22, 400, 3, 20, 16, 0.8, 5.0, 4.0, 3.0, 0.5, 3.2, 1.3],
                ["", "SDMO", "", "", "J66", 66, 400, 3, 60, 48, 0.8, 12.0, 9.5, 7.0, None, None, None]]
        crv = {}
        gdb = G.parse_genset_db(rows, None, crv)
        self.assertEqual(gdb["K22"], {20.0})
        self.assertEqual(crv["K22"], {(0.5, 3.2, 1.3)})
        a, b, c = next(iter(crv["J66"]))
        for x, y in ((0.5, 7.0), (0.75, 9.5), (1.0, 12.0)):
            self.assertAlmostEqual(a * x * x + b * x + c, y, places=6)
        sites = {"A": {"type_ge": "SDMO - K22"}, "B": {"type_ge": "X99"}}
        G.add_prp(sites, gdb, "Closing.xlsb", crv)
        self.assertEqual((sites["A"]["prp_kw"], sites["A"]["courbes_cph"]), ([16.0], [(0.5, 3.2, 1.3)]))
        self.assertEqual(sites["B"]["courbes_cph"], [None])

    def test_archive_introuvable_signalee_et_derniere_lecture_conservee(self):
        """Worker sans le registre monté (celery-interactive) : archive Closing introuvable → erreur explicite, et la
        dernière GENSET DB lue reste en cache au lieu d'afficher « n.d. » partout."""
        from unittest import mock

        from django.core.cache import cache

        from fuel_tracking.services import ge_info as G

        FuelImportBatch.objects.create(source_type="CLOSING", file_name="Closing_sept.xlsb", file_hash="h" * 64,
                                       archive_path="/inexistant/registre/archive/Closing_sept.xlsb")
        gdb, meta = G.fetch_genset_db()
        self.assertEqual((gdb, meta["fichier"], meta["archives_introuvables"]), ({}, None, 1))

        cache.set(G.CACHE_KEY, {"raw": {"gdb": {"K22": {22.0}}, "gdb_meta": {"fichier": "Closing_sept.xlsb"}},
                                "lu_le": timezone.now().isoformat()})
        try:
            with mock.patch.object(G, "fetch_snowflake", return_value=({}, {})), mock.patch.object(G, "fetch_enoc", return_value={}), \
                    mock.patch("fuel_tracking.services.base_ge.latest_types", return_value=({"A": ("K22", None)}, "base.xlsx")):
                out = G.refresh()
            self.assertTrue(any("introuvable" in e for e in out["erreurs"]))
            data = G.cached()
            self.assertEqual((data["sites"]["A"]["prp_kva"], data["sites"]["A"]["prp_source"]), ([22.0], "GENSET DB (Closing_sept.xlsb)"))
            self.assertTrue(data["meta"]["genset_db"]["conservee"])
        finally:
            cache.delete(G.CACHE_KEY)


class AuditSuiviConsoTests(OutputAppFuelImportTests):
    """Audit global (lecture seule) : chaque valeur affichée comparée à la source ; sources externes simulées."""

    def test_status_rules(self):
        from fuel_tracking.management.commands.audit_suivi_conso import _status

        self.assertEqual([_status(None, None), _status(None, 5), _status(5, None), _status(5, 5.2), _status(5, 9), _status("K22", "k22")],
                         ["ABSENT", "MANQUANT", "SANS_SOURCE", "OK", "ECART", "OK"])

    def test_command_runs_and_compares_with_sources(self):
        import tempfile
        from io import StringIO
        from pathlib import Path
        from unittest import mock

        from django.core.management import call_command

        from fuel_tracking.management.commands import audit_suivi_conso as A
        from fuel_tracking.services import stock_import_pipeline as P
        from fuel_tracking.services import stock_initial_sources as S

        P.process_bytes(self.output_bytes(), "Output App Fuel.xlsx", "CLOSING", origin="UPLOAD")
        SB.compute_balances(timezone.make_aware(datetime(2026, 10, 1)), timezone.make_aware(datetime(2026, 10, 31, 23, 59, 59)), site_ids=["DKR_0001"])
        fichier = {"_nom": "OAF.xlsx", "DKR_0001": {"date_initiale": date(2026, 9, 30), "date_finale": date(2026, 10, 31),
                                                   "stock_initial_l": 1000, "stock_terrain_final_l": 800}}
        out = StringIO()
        with tempfile.TemporaryDirectory() as tmp,                 mock.patch.object(A.Command, "_file_lines", return_value=fichier),                 mock.patch.object(A.Command, "_snowflake", return_value=({("DKR_0001", date(2026, 9, 30)): {"niveau": 950.0}}, {}, "")),                 mock.patch.object(A.Command, "_enoc_ge", return_value={}),                 mock.patch.object(A.Command, "_mongo_completeness", return_value=None),                 mock.patch.object(S, "_fetch_guard", return_value=({}, None)),                 mock.patch.object(S, "_guard_readings", return_value=({}, None)):
            path = str(Path(tmp) / "audit.csv")
            call_command("audit_suivi_conso", mois="2026-10", csv=path, stdout=out)
            txt = Path(path).read_text(encoding="utf-8")
        self.assertIn("SYNTHÈSE PAR CHAMP", out.getvalue())
        self.assertIn("DKR_0001;STOCK INITIAL;Qtité FMS;;950.0;MANQUANT", txt)   # Snowflake a une valeur, le tableau non


class BaseGeImportTests(MvpBase):
    """Fichier « Base GE » (Configuration, BASE_GE) : type de GE par site, prioritaire ; PRP via GENSET DB (type ou fabricant + type)."""

    def _xlsx(self, rows):
        import io

        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Feuil32"
        for r in rows:
            ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_import_and_priority(self):
        from fuel_tracking.services import base_ge as BG
        from fuel_tracking.services import ge_info as G
        from fuel_tracking.services import stock_import_pipeline as P

        data = self._xlsx([[], [None, None, ";"], [None, None, 1],
                           [None, "Site ID", "NOM", "Typologie facturée", "Type de GE", "Puissance GE (KVA)"],
                           [None, "DKR_0001", "SITE 1", "A_Ax_S6", "SDMO - K22", 22],
                           [None, "DKR_0001", "SITE 1 bis", "A_Ax_S6", "SDMO - K33", 33],        # doublon : première ligne retenue
                           [None, "XYZ_9999", "INCONNU", "A_Ax_S6", "FG Wilson P33-3", 33],     # site inconnu : avertissement
                           [None, "DKR_0002", "SITE 2", "A_Ax_S6", None, None]])                # type vide : aucun type
        b = P.process_bytes(data, "Base GE.xlsx", "BASE_GE", origin="UPLOAD")
        self.assertNotIn(b.status, ("REJECTED", "FAILED"))
        base, f = BG.latest_types()
        self.assertEqual(base["DKR_0001"], ("SDMO - K22", 22))
        self.assertNotIn("DKR_0002", base)
        out = G.build({"DKR_0001": [(1, ("Kohler", G.SRC_SF), ("J88K", G.SRC_SF))]}, {"DKR_0001": [("SDMO", "22")]}, base, f)
        self.assertEqual(out["DKR_0001"]["type_ge"], "SDMO - K22")                       # fichier Base GE prioritaire
        self.assertIn("Fichier Base GE (Base GE.xlsx)", out["DKR_0001"]["source_ge"])

    def test_vf5_fabricant_column(self):
        """Base GE VF5 : onglet « Liste », « Fabricant GE » en colonne à part et « Type de GE » sans marque (« K22 »)."""
        from fuel_tracking.services import base_ge as BG
        from fuel_tracking.services import ge_info as G
        from fuel_tracking.services import stock_import_pipeline as P

        data = self._xlsx([[], [None, None, ";"], [None, None, 1],
                           [None, "Site ID", "NOM", "On-Grid / Off-Grid ", "Fabricant GE", "Type de GE", "Puissance\nGE\n(KVA)"],
                           [None, "DKR_0001", "SITE 1", "Off grid", "SDMO", "K22", 22],
                           [None, "DKR_0002", "SITE 2", "Off grid", None, "P33-3U", 33]])    # fabricant vide : jamais inventé
        b = P.process_bytes(data, "Base_GE_VF5.xlsx", "BASE_GE", origin="UPLOAD")
        self.assertNotIn(b.status, ("REJECTED", "FAILED"))
        base, f = BG.latest_types()
        self.assertEqual((base["DKR_0001"], base["DKR_0002"]), (("K22", 22), ("P33-3U", 33)))
        fab = BG.latest_fabricants()
        self.assertEqual(fab, {"DKR_0001": "SDMO"})
        sf = {"DKR_0001": [(1, ("Kohler", G.SRC_SF), ("J88K", G.SRC_SF))], "DKR_0002": [(1, ("CATERPILLAR", G.SRC_SF), (None, None))]}
        out = G.build(sf, {}, base, f, [], base_fab=fab)
        self.assertEqual((out["DKR_0001"]["fabricant_ge"], out["DKR_0001"]["type_ge"], out["DKR_0001"]["source_ge"]),
                         ("SDMO", "K22", "Fichier Base GE (Base_GE_VF5.xlsx)"))
        self.assertEqual((out["DKR_0002"]["fabricant_ge"], out["DKR_0002"]["type_ge"]), ("CATERPILLAR", "P33-3U"))   # source suivante

    def test_vf5_genset_db_tab_first(self):
        """Base GE VF5 : PRP et courbe du type lues dans l'onglet GENSET DB du fichier Base GE, avant la GENSET DB du Closing."""
        import io

        import openpyxl

        from fuel_tracking.services import base_ge as BG
        from fuel_tracking.services import ge_info as G
        from fuel_tracking.services import stock_import_pipeline as P

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Liste"
        for r in ([None, "Site ID", "NOM", "Fabricant GE", "Type de GE", "Puissance GE (KVA)"],
                  [None, "DKR_0001", "S1", "CATERPILLAR", "DE33E0", 33], [None, "DKR_0002", "S2", "SDMO", "K22", 22]):
            ws.append(r)
        g = wb.create_sheet("GENSET DB")
        for r in ([None, None, None, None, 1, 2, 3],
                  [None, "Manufacturer ", "Type", "Genset List", "Type de GE", None, "Voltage [V]", "No. of phases", "PRP [kVA]",
                   "PRP [kW]", "cosφ", "Conso 100%", "Conso 75%", "Conso 50%", "a", "b", "c"],
                  [None, None, None, None, None, None, None, None, None, None, None, 1, 0.75, 0.5],
                  [None, "Caterpillar", None, None, "DE33E0", 33, 400, 3, 30, 24, 0.8, 7.0, 5.2, 3.8, 0.8, 4.0, 2.2]):
            g.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        b = P.process_bytes(buf.getvalue(), "Base_GE_VF5.xlsx", "BASE_GE", origin="UPLOAD")
        self.assertNotIn(b.status, ("REJECTED", "FAILED"))
        bg, bc, bf, f = BG.latest_genset_db()
        self.assertEqual((bg["DE33E0"], bc["DE33E0"], f), ({30.0}, {(0.8, 4.0, 2.2)}, "Base_GE_VF5.xlsx"))
        closing_gdb = {"DE33E0": {33.0}, "K22": {20.0}}                       # GENSET DB du Closing de référence
        closing_crv = {"DE33E0": {(1.0, 4.0, 2.7)}, "K22": {(1.0, 2.0, 1.0)}}
        gdb, crv, src = G.with_base_genset_db(closing_gdb, closing_crv)
        sites = {"DKR_0001": {"type_ge": "DE33E0"}, "DKR_0002": {"type_ge": "K22"}}
        G.add_prp(sites, gdb, "Closing.xlsb", crv, src)
        s1, s2 = sites["DKR_0001"], sites["DKR_0002"]
        self.assertEqual((s1["prp_kva"], s1["courbes_cph"], s1["prp_source"]),
                         ([30.0], [(0.8, 4.0, 2.2)], "GENSET DB (Base_GE_VF5.xlsx)"))   # onglet du fichier Base GE prioritaire
        self.assertEqual((s2["prp_kva"], s2["courbes_cph"], s2["prp_source"]),
                         ([20.0], [(1.0, 2.0, 1.0)], "GENSET DB (Closing.xlsb)"))       # type absent de l'onglet : Closing

    def test_genset_db_lookup_by_type_or_brand_and_type(self):
        from fuel_tracking.services import ge_info as G

        gdb = G.parse_genset_db([["", "Manufacturer ", "Type", "Genset List", "Type de GE", "", "Voltage [V]", "No. of phases", "PRP [kVA]"],
                                 ["", "SDMO", "", "", "K22", 22, 400, 3, 20],
                                 ["", "FG Wilson", "", "", "P33-3", 33, 400, 3, 30]])
        sites = {"A": {"type_ge": "SDMO - K22"}, "B": {"type_ge": "SDMO--K22"}, "C": {"type_ge": "FG Wilson P33-3"},
                 "D": {"type_ge": "FG Wilson P33-3U"}, "E": {"type_ge": "K22"}}
        G.add_prp(sites, gdb, "Closing.xlsb")
        self.assertEqual([sites[k]["prp_kva"] for k in "ABCDE"], [[20.0], [20.0], [30.0], [30.0], [20.0]])   # P33-3U ≠ P33-3 : jamais approché ; PRP de la fiche constructeur FC-07 (25/11)


class BaseGeUploadApiTests(BaseGeImportTests):
    """API : la Base GE s'importe par /stock/imports/ (type BASE_GE) ; envoyée par erreur comme Closing → refus clair."""

    def setUp(self):
        super().setUp()
        from django.contrib.auth import get_user_model
        from rest_framework.test import APIClient

        u = get_user_model().objects.create_superuser("admin_bge", "a@example.com", "x", role="admin")             if "role" in [f.name for f in get_user_model()._meta.fields] else get_user_model().objects.create_superuser("admin_bge", "a@example.com", "x")
        self.api = APIClient()
        self.api.force_authenticate(u)
        self.data = self._xlsx([[None, "Site ID", "NOM", "Type de GE", "Puissance GE (KVA)"], [None, "DKR_0001", "S1", "SDMO - K22", 22]])

    def test_upload_as_base_ge(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        r = self.api.post("/api/fuel-tracking/stock/imports/", {"file": SimpleUploadedFile("Base GE.xlsx", self.data), "source_type": "BASE_GE"}, format="multipart")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual((r.json()["source_type"], r.json()["rows_total"]), ("BASE_GE", 1))

    def test_base_ge_sent_as_closing_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        r = self.api.post("/api/fuel-tracking/configuration/closing/", {"file": SimpleUploadedFile("Base GE.xlsx", self.data), "mois": "2026-10"}, format="multipart")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Base GE", r.json()["detail"])

    def test_import_and_priority(self):
        pass

    def test_genset_db_lookup_by_type_or_brand_and_type(self):
        pass


class BaseGeBrandTests(MvpBase):
    """Fabricant GE lu dans le libellé « Type de GE » du fichier Base GE (pas de colonne fabricant) ; jamais déduit."""

    def test_split_label(self):
        from fuel_tracking.services.base_ge import known_brands, split_label

        labels = ["SDMO - K22", "CATERPILLAR - DE33E0", "FG Wilson P33-3U", "PRAMAC | GDW50", "SDMO--J66", "PA50", "OLYMPIAN--GEP33-3"]
        brands = known_brands(labels, ["FG Wilson", "Perkins"])
        self.assertEqual([split_label(x, brands) for x in labels],
                         [("SDMO", "K22"), ("CATERPILLAR", "DE33E0"), ("FG Wilson", "P33-3U"), ("PRAMAC", "GDW50"), ("SDMO", "J66"),
                          (None, "PA50"), ("OLYMPIAN", "GEP33-3")])
        self.assertEqual(split_label("SDMOX22", brands), (None, "SDMOX22"))   # « SDMO » collé au modèle : pas une marque séparée

    def test_brand_from_base_ge_first(self):
        from fuel_tracking.services import ge_info as G

        base = {"S_1": ("FG Wilson P33-3U", 33), "S_2": ("PA50", 50)}
        sf = {"S_1": [(1, ("CATERPILLAR", G.SRC_SF), ("P33-3", G.SRC_SF))], "S_2": [(1, ("Kohler", G.SRC_SF), (None, None))]}
        out = G.build(sf, {}, base, "Base GE.xlsx", ["FG Wilson"])
        self.assertEqual((out["S_1"]["fabricant_ge"], out["S_1"]["type_ge"], out["S_1"]["source_ge"]),
                         ("FG Wilson", "FG Wilson P33-3U", "Fichier Base GE (Base GE.xlsx)"))
        self.assertEqual((out["S_2"]["fabricant_ge"], out["S_2"]["source_ge"]),                       # pas de marque dans « PA50 »
                         ("Kohler", f"Fabricant : {G.SRC_SF} · Type : Fichier Base GE (Base GE.xlsx)"))


class DuplicateAcrossTypesTests(BaseGeImportTests):
    """Même fichier : un lot en échec, rejeté ou d'un autre type ne bloque jamais un nouvel import ; un lot réussi du même type, si."""

    def test_failed_closing_does_not_block_base_ge(self):
        from fuel_tracking.models import FuelImportBatch as B
        from fuel_tracking.services import stock_import_pipeline as P

        data = self._xlsx([[None, "Site ID", "NOM", "Type de GE"], [None, "DKR_0001", "S1", "SDMO - K22"]])
        first = P.process_bytes(data, "Base GE.xlsx", "CLOSING", origin="UPLOAD")      # envoyé par erreur comme Closing
        B.objects.filter(pk=first.pk).update(status=B.Status.FAILED)                   # « Échec technique » (cas prod lot #268)
        b = P.process_bytes(data, "Base GE.xlsx", "BASE_GE", origin="UPLOAD")
        self.assertIsNone(b.duplicate_of_id)
        self.assertEqual(b.rows_total, 1)
        again = P.process_bytes(data, "Base GE.xlsx", "BASE_GE", origin="UPLOAD")      # même fichier, même type, déjà importé
        self.assertEqual((again.status, again.duplicate_of_id), ("REJECTED", b.pk))

    def test_import_and_priority(self):
        pass

    def test_genset_db_lookup_by_type_or_brand_and_type(self):
        pass


class BaseGeLiveTests(BaseGeImportTests):
    """Dès l'import du fichier Base GE, fabricant et type apparaissent dans le Suivi Conso (sans attendre une relecture)."""

    def test_new_import_visible_immediately(self):
        from django.core.cache import cache

        from fuel_tracking.services import ge_info as G
        from fuel_tracking.services import stock_import_pipeline as P

        cache.set(G.CACHE_KEY, {"raw": {"sf": {}, "enoc": {}, "gdb": {"K22": {20.0}, "K33": {30.0}}, "gdb_meta": {"fichier": "Closing.xlsb"}},
                                "lu_le": "2026-11-19T10:00:00+00:00"})
        P.process_bytes(self._xlsx([[None, "Site ID", "NOM", "Type de GE"], [None, "DKR_0001", "S1", "SDMO - K22"]]), "Base GE v1.xlsx", "BASE_GE", origin="UPLOAD")
        self.assertEqual(G.sites_info(["DKR_0001"])["DKR_0001"]["type_ge"], "SDMO - K22")
        P.process_bytes(self._xlsx([[None, "Site ID", "NOM", "Type de GE"], [None, "DKR_0001", "S1", "SDMO - K33"]]), "Base GE v2.xlsx", "BASE_GE", origin="UPLOAD")
        x = G.sites_info(["DKR_0001"])["DKR_0001"]
        self.assertEqual((x["fabricant_ge"], x["type_ge"], x["prp_kva"], x["prp_kw"]), ("SDMO", "SDMO - K33", [30.0], [24.0]))   # dernier fichier

    def test_import_and_priority(self):
        pass

    def test_genset_db_lookup_by_type_or_brand_and_type(self):
        pass


class RuntimeFmsTests(MvpBase):
    """Temps de marche GE sur la plage du fichier (lendemain de la date initiale → date finale) depuis les faits Snowflake."""

    def test_dg_on_and_rectifier_sums(self):
        from datetime import date, timedelta

        from fuel_tracking.models import FuelSiteDailyFacts as F, FuelSiteInventory as INV
        from fuel_tracking.services import stock_initial_sources as S

        INV.objects.create(country="Senegal", data_id=1, site_id="OFF_1", grid_supply="OFF GRID", dg_count=1)
        INV.objects.create(country="Senegal", data_id=2, site_id="ON_1", grid_supply="ON GRID", dg_count=1)
        d0 = date(2026, 9, 30)
        for sid, did in (("OFF_1", 1), ("ON_1", 2)):
            for k, (dg, act) in enumerate([(5, 60), (30, 12), (2.5, 24), (None, None)]):   # 30 h : hors bornes, écarté
                F.objects.create(country="Senegal", data_id=did, site_id=sid, date=d0 + timedelta(days=k), dg_on_runtime_h=dg,
                                 rectifier_slots=288 if act is not None else None, rectifier_active_slots=act)
        rows = [{"site_id": sid, "stock": {"date_initial": "2026-09-30", "date_final": "2026-10-03"}} for sid in ("OFF_1", "ON_1")]
        lines = {sid: {"ini": {"periode": ["2026-09-30", "2026-10-03"]}, "fin": {"periode": ["2026-09-30", "2026-10-03"]}} for sid in ("OFF_1", "ON_1")}
        out = S._fms_runtimes(rows, lines)
        # jour initial (30/09, 5 h) exclu ; 01/10 : 30 h écarté ; 02/10 : 2,5 h ; 03/10 : absent
        self.assertEqual((out["OFF_1"]["dg_on"], out["OFF_1"]["dg_on_jours"], out["OFF_1"]["jours"]), (2.5, 1, 3))
        self.assertEqual((out["OFF_1"]["rect"], out["OFF_1"]["rect_jours"]), (3.0, 2))              # (12 + 24) × 5 min
        # règle équipe data : tous les sites (réseau compris) ; disponibilité = créneaux reçus ÷ (288 × 3 jours)
        self.assertEqual((out["ON_1"]["rect"], out["ON_1"]["rect_off_grid"]), (3.0, False))
        self.assertEqual((out["OFF_1"]["rect_off_grid"], out["OFF_1"]["rect_dispo_pct"]), (True, round(100 * 576 / 864, 1)))
        self.assertIsNone(out["OFF_1"]["rect_motif"])
        # temps retenu : heures redresseur seulement hors réseau
        for sid, attendu in (("OFF_1", (3.0, "Alarme day_rectifier_on FMS")), ("ON_1", (None, None))):
            e = {"tm_rectifier_h": out[sid]["rect"], "tm_rectifier_off_grid": out[sid]["rect_off_grid"]}
            self.assertEqual(S._first_of((e["tm_rectifier_h"] if e["tm_rectifier_off_grid"] is True else None,
                                          "Alarme day_rectifier_on FMS")), attendu)



class RuntimeTableTests(OutputAppFuelImportTests):
    """Temps de marche (relevé de terrain du fichier, Index Compteur FMS) dans le tableau."""

    _rows = StockInitialSourcesTests._rows

    def test_import_and_october_balance(self):
        pass

    def test_strict_rule_rejects_readings_after_boundary(self):
        pass

    def test_sites_without_genset_excluded_from_health(self):
        pass

    def test_files_give_stocks_only_movements_from_enoc(self):
        pass

    def test_terrain_and_index_runtimes_in_table(self):
        from unittest import mock

        from fuel_tracking.models import FuelStockBalance as B
        from fuel_tracking.services import stock_initial_sources as S

        rows = self._rows()
        b = B.objects.get(pk=rows[0]["stock"]["bilan_id"])
        for dd in (b.stock_initial_detail, b.stock_final_detail):
            o = Obs.objects.get(pk=dd["observation_id"])
            o.source_context = {**o.source_context, "temps_marche_terrain_h": 999.0,   # colonne de résultat du fichier : ignorée
                                "compteur_terrain_initial_h": 1000.0, "compteur_terrain_final_h": 1120.5}
            o.save(update_fields=["source_context"])
        di, df = date(2026, 9, 30), date.fromisoformat(o.source_context["periode"][1])
        with mock.patch.object(S, "_guard_readings", return_value=({}, None)),                 mock.patch.object(S, "_snowflake_points", return_value=({("DKR_0001", di): {"index_h": 1000.0}, ("DKR_0001", df): {"index_h": 1130.25}}, None)):
            S.enrich(rows)
        e = rows[0]["estimation_theorique"]
        self.assertEqual((e["tm_terrain_h"], e["tm_index_fms_h"]), (120.5, 130.25))


class TotalsRowTests(MvpBase):
    """Ligne de totaux : somme sur tous les sites du filtre ; « n.d. » non compté (jamais 0) ; puissances de plusieurs GE additionnées."""

    def test_totals(self):
        from unittest import mock

        from fuel_tracking.services import stock_initial_sources as S

        def fake_enrich(rows, fetch=True):
            vals = [(100.0, [20.0, 30.0], 5.5), (None, None, None), (250.5, [30.0], 2.0)]
            for r, (t, kva, h) in zip(rows, vals):
                r["stock_initial_sources"] = {"terrain_l": t}
                r["estimation_theorique"] = {"prp_kva": kva, "tm_terrain_h": h, "type_ge": "K22" if t else None,
                                             "p_ge_kw": Decimal("2.5") if t else None, "tm_source": "FMS" if h else None}
            return None

        rows = [{"site_id": f"S_{i}", "stock": {}} for i in range(3)]
        with mock.patch.object(S, "enrich", side_effect=fake_enrich) as en:
            t = S.totals(rows)
        en.assert_called_once()
        self.assertFalse(en.call_args.kwargs.get("fetch", True))           # jamais d'appel Snowflake / ENOC pour les totaux
        self.assertEqual(t["stock_initial_sources"]["terrain_l"], {"total": 350.5, "sites": 2})
        self.assertEqual(t["estimation_theorique"]["prp_kva"], {"total": 80.0, "sites": 2})
        self.assertEqual(t["estimation_theorique"]["tm_terrain_h"], {"total": 7.5, "sites": 2})
        self.assertEqual(t["ravitaillement"]["eydon_l"], {"total": None, "sites": 0})
        self.assertEqual(t["estimation_theorique"]["type_ge"], {"total": 2, "sites": 2, "comptage": True})   # sites avec un type
        self.assertEqual(t["estimation_theorique"]["p_ge_kw"], {"total": 5.0, "sites": 2})          # médianes Decimal comptées
        self.assertEqual(t["estimation_theorique"]["tm_source"], {"total": 2, "sites": 2, "comptage": True})
        self.assertEqual(t["estimation_theorique"]["fabricant_ge"], {"total": 0, "sites": 0, "comptage": True})
        self.assertNotIn("stock_initial_sources", rows[0])                      # lignes affichées non modifiées


class RuntimeSourceTests(MvpBase):
    """Source de donnée Temps de marche GE : Index Compteur FMS → relevé de terrain → day_dg_on → day_rectifier_on (ordre Ops)."""

    def test_order(self):
        from fuel_tracking.services.stock_initial_sources import _first_of

        def src(index=None, terrain=None, dg_on=None, rect=None):
            return _first_of((index, "FMS"), (terrain, "relevé de terrain"), (dg_on, "Alarme day_dg_on FMS"), (rect, "Alarme day_rectifier_on FMS"))

        self.assertEqual(src(index=326.1, terrain=300, dg_on=4.3, rect=8.5), (326.1, "FMS"))
        self.assertEqual(src(terrain=0.0, dg_on=4.3, rect=8.5), (0.0, "relevé de terrain"))          # 0 h est une vraie valeur
        self.assertEqual(src(dg_on=4.3, rect=8.5), (4.3, "Alarme day_dg_on FMS"))
        self.assertEqual(src(rect=8.5), (8.5, "Alarme day_rectifier_on FMS"))
        self.assertEqual(src(), (None, None))


class RuntimeCounterDiffTests(MvpBase):
    """Temps de marche = relevé final − relevé initial ; impossible (négatif, > 24 h / jour, relevé manquant) → n.d. + raison."""

    def _run(self, ci, cf, jours=30):
        from datetime import date, timedelta
        from unittest import mock

        from fuel_tracking.services import stock_initial_sources as S

        d0 = date(2026, 9, 30)
        r = {"site_id": "S_1", "stock": {"bilan_id": None, "date_initial": d0.isoformat(), "date_final": (d0 + timedelta(days=jours)).isoformat()}}
        line = {"periode": [d0.isoformat(), (d0 + timedelta(days=jours)).isoformat()], "compteur_terrain_initial_h": ci, "compteur_terrain_final_h": cf}
        with mock.patch.object(S, "_file_line_values", return_value={"S_1": {"ini": line, "fin": line}}),                 mock.patch.object(S, "_guard_readings", return_value=({}, None)), mock.patch.object(S, "_snowflake_points", return_value=({}, None)),                 mock.patch.object(S, "ravitaillement"):
            S.enrich([r])
        e = r["estimation_theorique"]
        return e["tm_terrain_h"], e["tm_terrain_motif"]

    def test_rules(self):
        self.assertEqual(self._run(744.44, 1448.52, 35), (704.08, None))              # KLD_0203 : 704,08 h (fichier : 740,08)
        self.assertIsNone(self._run(500, 400)[0])
        self.assertIn("inférieur", self._run(500, 400)[1])
        self.assertIn("incohérent", self._run(0, 14494, 35)[1])
        self.assertIn("absent", self._run(None, 400)[1])
        self.assertEqual(self._run(None, None), (None, None))


class PowerMediansTests(MvpBase):
    """Puissances médianes mesurées (redresseur, générateur, Grid) sur la plage du fichier, depuis les faits journaliers."""

    def test_medians(self):
        from datetime import date, timedelta

        from fuel_tracking.models import FuelSiteDailyFacts as F
        from fuel_tracking.services import stock_initial_sources as S

        d0 = date(2026, 9, 30)
        # (P_DC kW, AC kW, Day DG On h, production GE kWh)
        days = [(4.0, 24.0, 10, 47.5), (5.0, 26.0, 8, 30), (6.0, None, 0, 0), (None, 0, 2, 50)]
        for k, (pdc, ac, h, kwh) in enumerate(days):
            F.objects.create(country="Senegal", data_id=1, site_id="OUT_1", date=d0 + timedelta(days=k + 1), p_dc_day_kw=pdc,
                             ac_active_power_avg_w=ac, dg_on_runtime_h=h, dg_production_kwh=kwh)
        F.objects.create(country="Senegal", data_id=1, site_id="OUT_1", date=d0, p_dc_day_kw=99, ac_active_power_avg_w=99,
                         dg_on_runtime_h=1, dg_production_kwh=99)   # jour initial exclu
        rows = [{"site_id": "OUT_1", "stock": {"date_initial": "2026-09-30", "date_final": "2026-10-04"}}]
        lines = {"OUT_1": {"ini": {"periode": ["2026-09-30", "2026-10-04"]}, "fin": {"periode": ["2026-09-30", "2026-10-04"]}}}
        out = S._power_medians(rows, lines, {}, {"OUT_1": 22.0})["OUT_1"]
        # Redresseur : plus de faits rect_power_* → indisponible (l'ancienne médiane des P_DC journalières n'est plus utilisée)
        self.assertEqual((out["p_rect_kw"], out["p_rect_source"]), (None, "PUISSANCE_REDRESSEUR_INDISPONIBLE"))
        self.assertEqual((out["p_grid_kw"], out["p_grid_jours"]), (25.0, 2))   # AC_METER déjà en kW ; 0 non compté
        # GE : kWh ÷ h des jours de marche ; 25 kW > 105 % × 22 kVA × 0,8 écarté ; jour à 0 h ignoré
        self.assertEqual((out["p_ge_kw"], out["p_ge_jours"]), (4.25, 2))
        # min / max : mêmes valeurs que la médiane (jour écarté au-delà du plafond non compté)
        self.assertEqual((out["p_ge_min_kw"], out["p_ge_max_kw"]), (3.75, 4.75))
        self.assertEqual((out["p_grid_min_kw"], out["p_grid_max_kw"]), (24.0, 26.0))
        self.assertEqual((out["p_rect_min_kw"], out["p_rect_max_kw"]), (None, None))
        sans_kva = S._power_medians(rows, lines, {}, {})["OUT_1"]
        self.assertEqual((sans_kva["p_ge_kw"], sans_kva["p_ge_jours"]), (4.75, 3))
        self.assertEqual((sans_kva["p_ge_min_kw"], sans_kva["p_ge_max_kw"]), (3.75, 25.0))
        F.objects.create(country="Senegal", data_id=2, site_id="ARR_1", date=date(2026, 10, 2), dg_on_runtime_h=0, dg_production_kwh=0)
        arr = S._power_medians([{**rows[0], "site_id": "ARR_1"}], {"ARR_1": lines["OUT_1"]}, {}, {})["ARR_1"]
        self.assertIsNone(arr["p_ge_kw"])
        self.assertEqual((arr["p_ge_min_kw"], arr["p_ge_max_kw"], arr["p_grid_min_kw"]), (None, None, None))   # jamais 0
        self.assertIn("arrêt", arr["p_ge_motif"])
        vide = S._power_medians([{**rows[0], "site_id": "VID_1"}], {"VID_1": lines["OUT_1"]}, {}, {})["VID_1"]
        self.assertIn("aucune donnée GENSET_REPORT", vide["p_ge_motif"])
        self.assertEqual((vide["p_load_dc_kw"], vide["p_batt_kw"]), (None, None))
        self.assertIn("AVERAGE_LOAD", vide["p_load_dc_motif"])
        self.assertEqual(vide["p_batt_source"], "BATTERY_CHARGE_TELEMETRY_UNAVAILABLE")

    def test_puissance_redresseur_regle_data(self):
        """Règle équipe data : médiane des jours qualifiés (≥ 50 % de 288) → sinon médiane période → sinon indisponible."""
        from fuel_tracking.services import stock_initial_sources as S

        self.assertAlmostEqual(4435.8 / (94.26 / 100) / 1000, 4.706, places=3)   # exemple du calcul validé
        jour = lambda pts, med, vals=None: {"rect_power_points": pts, "rect_power_median_kw": med, "rect_power_values_kw": vals}
        # 2 jours qualifiés (204 et 160 pts ≥ 144) + 1 non qualifié (ignoré quand des jours qualifiés existent)
        r = S._rect_power([jour(204, 4.985), jour(160, 5.2), jour(20, 9.0, [9.0] * 20)], 3)
        self.assertEqual((r["p_rect_kw"], r["p_rect_jours"], r["p_rect_source"], r["p_rect_motif"]), (5.09, 2, "RECTIFIER_MEDIAN_DAY", None))
        self.assertEqual((r["p_rect_median_day_kw"], r["p_rect_median_period_kw"], r["p_rect_dispo_pct"]), (5.093, None, 44.4))
        self.assertEqual((r["p_rect_min_kw"], r["p_rect_max_kw"]), (round(4.985, 2), 5.2))   # jour non qualifié (9 kW) exclu
        # Aucun jour qualifié : médiane de toutes les mesures valides de la période
        r = S._rect_power([jour(100, 3.0, [2.0, 3.0, 4.0]), jour(50, 6.0, [5.0, 6.0])], 2)
        self.assertEqual((r["p_rect_kw"], r["p_rect_source"], r["p_rect_median_period_kw"]), (4.0, "RECTIFIER_MEDIAN_PERIOD", 4.0))
        self.assertIn("aucun jour ≥ 50 %", r["p_rect_motif"])
        self.assertEqual((r["p_rect_min_kw"], r["p_rect_max_kw"]), (2.0, 6.0))   # toutes les mesures valides de la période
        # Aucune mesure valide : null, jamais 0
        r = S._rect_power([jour(None, None), {}], 2)
        self.assertEqual((r["p_rect_kw"], r["p_rect_source"]), (None, "PUISSANCE_REDRESSEUR_INDISPONIBLE"))
        self.assertEqual((r["p_rect_min_kw"], r["p_rect_max_kw"]), (None, None))
        self.assertIn("aucune mesure redresseur valide", r["p_rect_motif"])

    def test_load_dc(self):
        """Load DC = médiane LOAD_W journaliers (W → kW), 0 non compté."""
        from datetime import date

        from fuel_tracking.models import FuelSiteDailyFacts as F
        from fuel_tracking.services import stock_initial_sources as S

        for d, lw in [(1, 2000), (2, 3000), (3, 0), (4, 2500), (5, None)]:
            F.objects.create(country="Senegal", data_id=3, site_id="BAT_1", date=date(2026, 10, d), load_dc_w=lw)
        F.objects.create(country="Senegal", data_id=3, site_id="BAT_1", date=date(2026, 9, 30), load_dc_w=99999)   # jour initial exclu
        rows = [{"site_id": "BAT_1", "stock": {"date_initial": "2026-09-30", "date_final": "2026-10-05"}}]
        lines = {"BAT_1": {"ini": {"periode": ["2026-09-30", "2026-10-05"]}, "fin": {"periode": ["2026-09-30", "2026-10-05"]}}}
        out = S._power_medians(rows, lines, {}, {})["BAT_1"]
        self.assertEqual((out["p_load_dc_kw"], out["p_load_dc_jours"], out["p_load_dc_motif"]), (2.5, 3, None))

    def test_recharge_batterie_regle_data(self):
        """Règle équipe data : I × V ÷ 1000 par pas 5 min ; jour qualifié ≥ 50 % de 288 pas reçus ; médiane jour → période ;
        aucune télémétrie / aucune charge → null avec la source, jamais 0 ; réserve si peu de pas en charge."""
        from fuel_tracking.services import stock_initial_sources as S

        self.assertAlmostEqual(13.00 * 54.01 / 1000, 0.702, places=3)   # exemple BKL_0188 de l'équipe data
        jour = lambda pts, pos, kw=None, i=None, v=None, vals=None: {
            "battery_telemetry_points": pts, "battery_charge_slots": pos, "battery_charge_median_kw": kw,
            "battery_charge_median_current_a": i, "battery_charge_median_voltage_v": v, "battery_charge_values_kw": vals}
        # 2 jours qualifiés avec charge (+ 1 qualifié sans charge, ignoré pour la médiane)
        r = S._batt_power([jour(281, 280, 0.702, 13.0, 54.01), jour(280, 200, 0.9, 16.0, 54.2), jour(288, 0)], 3)
        self.assertEqual((r["p_batt_kw"], r["p_batt_source"], r["p_batt_jours"], r["p_batt_pos"]), (0.801, "BATTERY_CHARGE_MEDIAN_DAY", 2, 480))
        self.assertEqual((r["p_batt_current_a"], r["p_batt_voltage_v"], r["p_batt_dispo_pct"], r["p_batt_reserve"]), (14.5, 54.105, 98.3, None))
        # Peu de pas positifs (BKL_0004 : 3 pas) : valeur affichée avec réserve
        r = S._batt_power([jour(284, 3, 0.483, 9.03, 53.14)], 1)
        self.assertEqual(r["p_batt_kw"], 0.483)
        self.assertIn("seulement 3 pas", r["p_batt_reserve"])
        # Télémétrie présente sans charge (BKL_0088) : null, jamais 0
        r = S._batt_power([jour(288, 0)], 1)
        self.assertEqual((r["p_batt_kw"], r["p_batt_source"]), (None, "NO_BATTERY_CHARGING_MEASURED"))
        # Aucune télémétrie (site sans mesure dans BATTERY_REPORT)
        r = S._batt_power([jour(None, None), {}], 2)
        self.assertEqual((r["p_batt_kw"], r["p_batt_source"]), (None, "BATTERY_CHARGE_TELEMETRY_UNAVAILABLE"))
        # Aucun jour qualifié avec charge : n.d. (règle prudente du 24/11/2026), jamais une médiane de jours peu couverts
        r = S._batt_power([jour(100, 3, 0.5), jour(60, 2, 2.0)], 2)
        self.assertEqual((r["p_batt_kw"], r["p_batt_source"], r["p_batt_median_period_kw"]), (None, "BATTERY_CHARGE_POWER_UNAVAILABLE", None))
        self.assertIn("2 jour(s) en charge sous ce seuil", r["p_batt_motif"])
        # Jour qualifié en charge mais puissance NULL dans BATTERY_REPORT (597 cas en octobre) : n.d., jamais 0
        r = S._batt_power([jour(288, 40, None)], 1)
        self.assertEqual((r["p_batt_kw"], r["p_batt_source"]), (None, "BATTERY_CHARGE_POWER_UNAVAILABLE"))


class TheoreticalConsoTests(SimpleTestCase):
    """Bloc ESTIMATION : puissance retenue, taux de charge, CPH = a·x² + b·x + c, conso théorique, conso fuel sensor."""

    def base(self, **kw):
        e = {"p_ge_kw": None, "p_rect_kw": None, "p_load_dc_kw": None, "p_batt_kw": None, "prp_kw": [16.0],
             "courbes_cph": [(0.5, 3.2, 1.3)], "tm_retenu_h": 100.0, "type_ge": "K22"}
        e.update(kw)
        return e

    def test_max_ge_redresseur(self):
        from fuel_tracking.services.stock_initial_sources import theoretical_conso

        r = theoretical_conso(self.base(p_ge_kw=6.0, p_rect_kw=8.0))
        self.assertEqual((r["p_retenue_kw"], r["p_retenue_source"]), (8.0, "PUISSANCE_MEDIANE_REDRESSEUR"))
        self.assertEqual(r["taux_charge"], 0.5)
        self.assertAlmostEqual(r["cph_l_h"], 0.5 * 0.25 + 3.2 * 0.5 + 1.3, places=3)   # 3,025 L/h
        self.assertAlmostEqual(r["conso_theorique_l"], 302.5, places=2)
        self.assertFalse(r["cph_extrapolation"])
        r = theoretical_conso(self.base(p_ge_kw=9.0, p_rect_kw=None))
        self.assertEqual(r["p_retenue_source"], "PUISSANCE_MEDIANE_GE")

    def test_repli_load_dc_batterie(self):
        from fuel_tracking.services.stock_initial_sources import theoretical_conso

        r = theoretical_conso(self.base(p_load_dc_kw=3.0, p_batt_kw=1.0))
        self.assertEqual((r["p_retenue_kw"], r["p_retenue_source"], r["p_retenue_reserve"]), (4.0, "LOAD_DC_PLUS_RECHARGE_BATTERIE", False))
        self.assertTrue(r["cph_extrapolation"])   # 25 % de charge
        r = theoretical_conso(self.base(p_load_dc_kw=3.0))
        self.assertEqual((r["p_retenue_kw"], r["p_retenue_source"], r["p_retenue_reserve"]), (3.0, "LOAD_DC_SEUL", True))
        r = theoretical_conso(self.base())
        self.assertEqual((r["p_retenue_kw"], r["conso_theorique_l"]), (None, None))
        self.assertIn("aucune puissance", r["p_retenue_motif"])

    def test_cas_non_calculables(self):
        from fuel_tracking.services.stock_initial_sources import theoretical_conso

        r = theoretical_conso(self.base(p_ge_kw=20.0))                      # 125 % de la PRP
        self.assertEqual((r["taux_charge"], r["conso_theorique_l"]), (None, None))
        r = theoretical_conso(self.base(p_ge_kw=8.0, prp_kw=None, courbes_cph=None))
        self.assertEqual(r["taux_charge"], None)
        r = theoretical_conso(self.base(p_ge_kw=8.0, prp_kw=[16.0, 20.0], courbes_cph=[None, None]))
        self.assertIn("plusieurs GE", r["taux_charge_motif"])
        r = theoretical_conso(self.base(p_ge_kw=8.0, courbes_cph=[None]))
        self.assertEqual((r["taux_charge"], r["cph_l_h"], r["conso_theorique_l"]), (0.5, None, None))
        r = theoretical_conso(self.base(p_ge_kw=8.0, tm_retenu_h=None))
        self.assertEqual((r["cph_l_h"] is not None, r["conso_theorique_l"]), (True, None))

    def test_estimation_retenue_cph_seule(self):
        """Règle Ops : conso retenue = conso théorique CPH, jamais la conso fuel sensor à sa place ; 0 reste 0."""
        from fuel_tracking.services.stock_initial_sources import RETENUE_SRC_CPH, retained_estimation, theoretical_conso

        e = self.base(p_ge_kw=8.0, p_rect_kw=None, conso_fuel_sensor_l=250.0)
        e.update(theoretical_conso(e))
        r = retained_estimation(e)
        self.assertEqual((r["conso_retenue_l"], r["conso_retenue_source"], r["conso_retenue_motif"]),
                         (e["conso_theorique_l"], RETENUE_SRC_CPH, None))
        e = self.base(p_ge_kw=8.0, tm_retenu_h=None, conso_fuel_sensor_l=250.0)   # CPH n.d., capteur présent
        e.update(theoretical_conso(e))
        r = retained_estimation(e)
        self.assertEqual((r["conso_retenue_l"], r["conso_retenue_source"]), (None, None))
        self.assertEqual(r["conso_retenue_motif"], "Temps de marche GE retenu n.d.")
        e = self.base(p_ge_kw=8.0, tm_retenu_h=0.0)                               # GE à l'arrêt : 0 L, pas n.d.
        e.update(theoretical_conso(e))
        self.assertEqual(retained_estimation(e)["conso_retenue_l"], 0.0)

    def test_diagnostic_maillon_bloquant(self):
        """diagnose_conso_theorique : premier maillon bloquant, dans l'ordre du calcul."""
        from fuel_tracking.management.commands.diagnose_conso_theorique import STEPS, blocking_step
        from fuel_tracking.services.stock_initial_sources import theoretical_conso

        def step(**kw):
            e = self.base(**kw)
            e.update(theoretical_conso(e))
            return blocking_step(e)

        self.assertIsNone(step(p_ge_kw=8.0))
        self.assertEqual(step(), STEPS[0])
        self.assertEqual(step(p_ge_kw=8.0, prp_kw=None, courbes_cph=None), STEPS[1])
        self.assertEqual(step(p_ge_kw=8.0, prp_kw=[16.0, 20.0], courbes_cph=[None, None]), STEPS[2])
        self.assertEqual(step(p_ge_kw=20.0), STEPS[3])
        self.assertEqual(step(p_ge_kw=8.0, courbes_cph=[None]), STEPS[4])
        self.assertEqual(step(p_ge_kw=8.0, tm_retenu_h=None), STEPS[6])

    def test_conso_fuel_sensor(self):
        from decimal import Decimal

        from fuel_tracking.services.stock_initial_sources import _fuel_sensor_conso

        r = _fuel_sensor_conso([{"measured_conso_l": Decimal("10.5")}, {"measured_conso_l": Decimal("0")},
                                {"measured_conso_l": None}], 3)
        self.assertEqual((r["conso_fuel_sensor_l"], r["conso_fuel_sensor_jours"], r["conso_fuel_sensor_reserve"]), (10.5, 2, True))
        r = _fuel_sensor_conso([{"measured_conso_l": None}], 1)
        self.assertEqual((r["conso_fuel_sensor_l"], r["conso_fuel_sensor_jours"]), (None, 0))


class SyncDailyFactsPerimeterTests(TestCase):
    """Synchro des faits journaliers : même périmètre que le tableau Suivi Conso (décision du 24/11/2026) — sites avec GE
    dans l'inventaire + sites avec stock dans les fichiers de clôture (DG_COUNT à 0 dans SITE_ESCO_CURRENT)."""

    def test_sites_avec_stock_fichier_synchronises_meme_sans_dg_count(self):
        from io import StringIO
        from unittest import mock

        from django.core.management import call_command

        inv = [{"data_id": 1, "site_id": "GE_1", "site_name": None, "grid_supply": None, "dg_count": 1},
               {"data_id": 2, "site_id": "Stock_2", "site_name": None, "grid_supply": None, "dg_count": 0},
               {"data_id": 3, "site_id": "RIEN_3", "site_name": None, "grid_supply": None, "dg_count": 0}]
        seen = []
        with mock.patch("fuel_tracking.services.fuel_daily_facts_snowflake.fetch_inventory", return_value=inv), \
                mock.patch("fuel_tracking.services.fuel_daily_facts_snowflake.fetch_daily_facts",
                           side_effect=lambda ids, *a, **k: seen.append(sorted(ids)) or []), \
                mock.patch("fuel_tracking.services.conso_comparison.fuel_stock_sites", return_value={"STOCK_2"}) as fs:
            out = StringIO()
            call_command("sync_fuel_daily_facts", start=date(2026, 9, 28), end=date(2026, 10, 2), country="Senegal", stdout=out)
        self.assertEqual(seen[0], [1, 2])   # GE_1 (DG_COUNT > 0) + Stock_2 (stock dans les fichiers) ; RIEN_3 exclu
        self.assertEqual(sorted(c.args[0] for c in fs.call_args_list), ["2026-09", "2026-10"])
        self.assertIn("1 avec GE dans l'inventaire + 1 avec stock", out.getvalue())


class DailyRateTests(SimpleTestCase):
    """Conso journalière des bilans : jamais une division par presque 0 qui fait échouer le recalcul de TOUS les bilans
    (« numeric field overflow » au déploiement du 24/11/2026)."""

    def test_releves_trop_proches_ou_valeur_hors_colonne(self):
        from fuel_tracking.services.stock_period_balance import daily_rate

        from datetime import timezone as dt_tz

        t0 = datetime(2026, 10, 1, 8, 0, tzinfo=dt_tz.utc)
        self.assertEqual(daily_rate(Decimal("300"), t0, t0 + timedelta(days=30)), Decimal("10.000"))
        self.assertIsNone(daily_rate(Decimal("300"), t0, t0 + timedelta(microseconds=5)))   # ancien calcul : ≈ 5,2 × 10^12 L/jour
        self.assertIsNone(daily_rate(Decimal("300"), t0, t0 + timedelta(minutes=59)))
        self.assertIsNone(daily_rate(Decimal("300"), t0, t0))
        self.assertIsNone(daily_rate(Decimal("300"), t0 + timedelta(days=1), t0))           # relevé final avant l'initial
        self.assertIsNone(daily_rate(None, t0, t0 + timedelta(days=30)))
        self.assertEqual(daily_rate(Decimal("0"), t0, t0 + timedelta(days=2)), Decimal("0.000"))   # 0 reste 0
        self.assertIsNone(daily_rate(Decimal("1E13"), t0, t0 + timedelta(hours=2)))          # hors colonne : n.d.


class BackfillBatteryChargeTests(MvpBase):
    """Reprise de la recharge batterie (GOLD.BATTERY_REPORT, instructions équipe data) : lignes existantes seulement,
    NULL jamais transformé en 0, doublon contradictoire → NULL, Snowflake en lecture seule."""

    def test_complete_les_faits_existants(self):
        from io import StringIO
        from unittest import mock

        from django.core.management import call_command

        from fuel_tracking.models import FuelSiteDailyFacts as F, FuelSiteInventory as INV

        INV.objects.create(country="Senegal", data_id=7, site_id="DKR_0001", dg_count=1)
        for d in (1, 2, 3):
            F.objects.create(country="Senegal", data_id=7, site_id="DKR_0001", date=date(2026, 10, d), dse_runtime_h=Decimal("5"))
        sqls = []

        class Cur:
            def execute(self, sql, params):
                sqls.append(sql)

            def fetchall(self):
                return [(7, date(2026, 10, 1), 288, 288, 120, 120, 0.702, 0.702),      # en charge
                        (7, date(2026, 10, 2), 288, 288, 0, 0, None, None),            # télémétrie sans charge : NULL
                        (7, date(2026, 10, 3), 288, 288, 50, 50, 0.4, 0.9),            # doublon contradictoire : NULL
                        (7, date(2026, 10, 4), 280, 280, 90, 90, 0.5, 0.5)]            # pas de ligne de faits : signalé

        conn = mock.Mock()
        conn.cursor.return_value = Cur()
        out = StringIO()
        with mock.patch("fuel_tracking.services.fuel_daily_facts_snowflake._connect", return_value=conn):
            call_command("backfill_battery_charge", start=date(2026, 10, 1), end=date(2026, 10, 4), stdout=out)
        rows = {f.date.day: f for f in F.objects.filter(data_id=7)}
        self.assertEqual((rows[1].battery_telemetry_points, rows[1].battery_charge_slots, float(rows[1].battery_charge_median_kw)), (288, 120, 0.702))
        self.assertEqual((rows[2].battery_telemetry_points, rows[2].battery_charge_slots, rows[2].battery_charge_median_kw), (288, 0, None))
        self.assertIsNone(rows[3].battery_charge_median_kw)
        self.assertEqual(float(rows[1].dse_runtime_h), 5.0)   # aucune autre colonne touchée
        self.assertFalse(F.objects.filter(date=date(2026, 10, 4)).exists())
        self.assertIn("sync_fuel_daily_facts --start 2026-10-04", out.getvalue())
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") and "BATTERY_REPORT" in s for s in sqls))
        self.assertFalse(any("GFMS_METRICS" in s for s in sqls))
        # La période retient la médiane des jours qualifiés en charge
        from fuel_tracking.services import stock_initial_sources as S

        r = S._batt_power(list(F.objects.filter(data_id=7).values(*["battery_telemetry_points", "battery_charge_slots",
                          "battery_charge_median_kw", "battery_charge_median_current_a", "battery_charge_median_voltage_v"])), 3)
        self.assertEqual((r["p_batt_kw"], r["p_batt_source"]), (0.702, "BATTERY_CHARGE_MEDIAN_DAY"))


class GensetAliasTests(SimpleTestCase):
    """Correspondance explicite des types absents de GENSET DB (liste A validée le 24/11/2026) : PRP et courbe du type cible,
    correspondance affichée dans la source ; jamais de rapprochement hors table (P33-3U reste n.d.)."""

    def test_liste_a_et_tracabilite(self):
        from fuel_tracking.services import ge_info as G

        gdb = {"J66K": {60.0}, "SDMOJ66K": {60.0}, "J88": {88.0}, "P333": {30.0}, "FGWILSONP333": {30.0}}
        crv = {"J66K": {(0.1, 2.0, 1.0)}}
        sites = {"A": {"type_ge": "SDMO - J66"}, "B": {"type_ge": "SDMO--J88K"}, "C": {"type_ge": "FG Wilson P33-3U"},
                 "D": {"type_ge": "J66K"}}
        G.add_prp(sites, gdb, "Closing.xlsb", crv)
        self.assertEqual((sites["A"]["prp_kva"], sites["A"]["prp_kw"], sites["A"]["courbes_cph"]), ([60.0], [48.0], [(0.1, 2.0, 1.0)]))
        self.assertIn("correspondance de type validée : J66 → J66K", sites["A"]["prp_source"])
        self.assertEqual(sites["B"]["prp_kva"], [88.0])
        self.assertEqual(sites["C"]["prp_kva"], [30.0])                       # P33-3U : fiche constructeur FC-07 (25/11)
        self.assertNotIn("GENSET DB", sites["C"]["prp_source"])
        self.assertEqual(sites["D"]["prp_source"], "GENSET DB (Closing.xlsb)")  # trouvé tel quel : aucune correspondance


class DiagnosticOpsComparisonTests(SimpleTestCase):
    """diagnose_conso_theorique : écart avec Ops décomposé en litres par cause (lecture seule)."""

    def test_xlsx_tabs(self):
        """--xlsx : deux onglets, « Plus bas qu'Ops » trié par écart et « Sans puissance » (puissance retenue n.d.) ; n.d. reste vide."""
        import os
        import tempfile
        from decimal import Decimal

        import openpyxl

        from fuel_tracking.management.commands.diagnose_conso_theorique import STEPS, ops_cause, write_xlsx

        base = {"mois": "2026-10", "site_name": "S", "type_ge": "K22", "prp_kw": [16.0], "p_ge_kw": None}
        rows = [{**base, "site_id": "A", "conso_theorique_l": Decimal("100"), "ops_conso_theorique_l": 300.0},
                {**base, "site_id": "B", "conso_theorique_l": 50.0, "ops_conso_theorique_l": 500.0},
                {**base, "site_id": "C", "conso_theorique_l": None, "ops_conso_theorique_l": 80.0, "p_retenue_kw": None},
                {**base, "site_id": "D", "conso_theorique_l": 90.0, "ops_conso_theorique_l": None}]
        for d in rows:
            e = {"conso_theorique_l": d["conso_theorique_l"], "p_retenue_kw": d.get("p_retenue_kw")}
            o = d["ops_conso_theorique_l"]
            d.update(ecart_ops_l=(o - float(d["conso_theorique_l"] or 0)) if o is not None else None, cause_ecart_ops=ops_cause(e, o))
        self.assertEqual(rows[2]["cause_ecart_ops"], f"n.d. chez nous — {STEPS[0]}")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "diag.xlsx")
            self.assertEqual(write_xlsx(path, rows), (2, 1))
            wb = openpyxl.load_workbook(path)
            self.assertEqual(wb.sheetnames, ["Plus bas qu'Ops", "Sans puissance"])
            bas = list(wb["Plus bas qu'Ops"].iter_rows(values_only=True))
            h = bas[0]
            self.assertEqual([r[h.index("Site ID")] for r in bas[1:3]], ["B", "A"])          # écart 450 puis 200
            self.assertEqual(bas[3][h.index("Écart Ops − EnerTrack (L)")], 650)               # ligne Total
            self.assertEqual(bas[1][h.index("PRP (kW)")], "16")
            pw = list(wb["Sans puissance"].iter_rows(values_only=True))
            self.assertEqual(pw[1][pw[0].index("Site ID")], "C")
            self.assertIsNone(pw[1][pw[0].index("Puissance médiane GE (kW)")])                # n.d. : cellule vide, jamais 0

    def test_decomposition_ecart(self):
        from fuel_tracking.management.commands.diagnose_conso_theorique import STEPS, Command

        es = [({"site_id": "A"}, {"conso_theorique_l": 100.0, "cph_extrapolation": True}),     # plus bas : 300 − 100
              ({"site_id": "B"}, {"conso_theorique_l": None, "p_retenue_kw": 5.0, "taux_charge": None, "taux_charge_motif": "Puissance PRP (kW) n.d."}),
              ({"site_id": "C"}, {"conso_theorique_l": 0.0}),                                    # 0 L chez nous
              ({"site_id": "D"}, {"conso_theorique_l": 50.0})]                                   # pas de valeur Ops
        ops = {"A": {"l": 300.0}, "B": {"l": 200.0}, "C": {"l": 80.0}}
        out = []
        Command()._compare_ops(out.append, es, ops)
        txt = "\n".join(out)
        self.assertIn("Total EnerTrack :        150 L", txt)
        self.assertIn("Total Ops       :        580 L", txt)
        self.assertIn(f"200 L     1 site(s)  n.d. chez nous — {STEPS[1]}", txt)
        self.assertIn("200 L     1 site(s)  valeur plus basse que Ops", txt)
        self.assertIn("80 L     1 site(s)  0 L chez nous", txt)
        self.assertIn("-50 L        sites calculés chez nous sans valeur Ops", txt)
        self.assertIn("ratio médian EnerTrack ÷ Ops = 0.33", txt)



class ClosingGensetTypeTests(OutputAppFuelImportTests):
    """Décision du 24/11/2026 : type de GE = 1. Base GE ; 2. fichier de clôture du mois (ligne du site) ; 3. ENOC / Snowflake."""

    def output_bytes(self):
        from openpyxl import load_workbook
        import io

        wb = load_workbook(io.BytesIO(super().output_bytes()))
        ws = wb["Output"]
        ws.cell(row=2, column=ws.max_column + 1, value="Fabriquant GE")
        ws.cell(row=2, column=ws.max_column + 1, value="Type de GE")
        n = ws.max_column
        for r, (fab, typ) in zip(range(3, 7), (("SDMO", " K22 "), ("FG Wilson", "0"), ("SDMO", "J66K"), ("SDMO", "K33"))):
            ws.cell(row=r, column=n - 1, value=fab)
            ws.cell(row=r, column=n, value=typ)
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    def test_type_lu_sur_la_ligne_du_mois(self):
        from fuel_tracking.models import FuelStockObservation as Obs
        from fuel_tracking.services import stock_import_pipeline as P
        from fuel_tracking.services import stock_initial_sources as S

        P.process_bytes(self.output_bytes(), "Output App Fuel.xlsx", "CLOSING", origin="UPLOAD")
        ctx = {o.site_id: o.source_context for o in Obs.objects.filter(source_type="CLOSING")}
        self.assertEqual((ctx["DKR_0001"]["fabricant_ge"], ctx["DKR_0001"]["type_ge"]), ("SDMO", "K22"))
        self.assertIsNone(ctx["DKR_0002"]["type_ge"])                        # « 0 » n'est pas un type
        self.assertIsNone(ctx["DKR_0002"]["fabricant_ge"])                   # fabricant sans type : ignoré
        rows = [{"site_id": "DKR_0001", "stock": {"date_initial": "2026-09-30", "date_final": "2026-10-31"}},
                {"site_id": "DKR_0002", "stock": {"date_initial": "2026-10-02", "date_final": "2026-11-05"}}]
        got = S.closing_genset_types(S._file_line_values(rows))
        self.assertEqual(got, {"DKR_0001": ("SDMO", "K22", "Fichier de clôture (Output App Fuel, 2026-09-30 → 2026-10-31)")})
        other = [{"site_id": "DKR_0001", "stock": {"date_initial": "2026-08-31", "date_final": "2026-09-30"}}]
        self.assertEqual(S.closing_genset_types(S._file_line_values(other)), {})   # jamais la ligne d'un autre mois

    def test_ordre_des_sources(self):
        from fuel_tracking.services import ge_info as G
        from fuel_tracking.services.stock_file_analysis import _ge_labels

        self.assertEqual(_ge_labels([("SDMO", "J66K"), ("SDMO", "K22")]), {"fabricant_ge": "SDMO", "type_ge": "J66K / K22"})
        self.assertEqual(_ge_labels([("SDMO", "N/A"), (None, None)]), {"fabricant_ge": None, "type_ge": None})
        cl = {k: ("SDMO", "K22", "Fichier de clôture (Closing, 2026-08-31 → 2026-09-30)") for k in "ABCD"}
        out = G.build({"C": [(1, ("FG Wilson", G.SRC_SF), ("P33-3", G.SRC_SF))], "E": [(1, ("FG Wilson", G.SRC_SF), ("P33-3", G.SRC_SF))]},
                      {"B": [("PRAMAC", "GBW15")]}, {"A": ("SDMO - J66K", 60)}, "Base GE.xlsx", ["SDMO"], cl)
        self.assertEqual(out["A"]["type_ge"], "SDMO - J66K")                 # 1. Base GE
        self.assertEqual((out["B"]["type_ge"], out["C"]["type_ge"], out["D"]["type_ge"]), ("K22", "K22", "K22"))   # 2. clôture
        self.assertEqual(out["C"], {"fabricant_ge": "SDMO", "type_ge": "K22",
                                    "source_ge": "Fichier de clôture (Closing, 2026-08-31 → 2026-09-30)"})
        self.assertEqual(out["E"]["type_ge"], "P33-3")                       # 3. Snowflake si aucun fichier
        G.add_prp(out, {"K22": {22.0}}, "Closing.xlsb")
        self.assertEqual(out["D"]["prp_kw"], [17.6])
        got = G.sites_info(["dkr_0009"], {"DKR_0009": ("SDMO", "K22", "Fichier de clôture (Closing, 2026-08-31 → 2026-09-30)")})
        self.assertEqual(got["DKR_0009"]["type_ge"], "K22")                  # transmis jusqu'à l'affichage


class ClosingFallbackUnknownBaseTypeTests(SimpleTestCase):
    """Décision du 24/11/2026 : type Base GE inconnu de GENSET DB → type du fichier de clôture du mois s'il donne une PRP."""

    def test_repli_et_tracabilite(self):
        from fuel_tracking.services import ge_info as G
        from fuel_tracking.services.stock_file_analysis import ge_label

        self.assertIsNone(ge_label("Site avec cuve et sans GE"))             # mention, pas un type de GE
        gdb = {"P333": {30.0}, "K22": {22.0}, "J66K": {60.0}}
        base = {"A": ("FG Wilson P33-3X", 30), "B": ("J66K", 60), "C": ("SDMO - K33", 33), "D": ("SDMO - K33", 33)}
        src = "Fichier de clôture (Output App Fuel, 2026-09-30 → 2026-10-31)"
        closing = {"A": ("FG Wilson", "P33-3", src), "B": ("SDMO", "K22", src), "C": ("SDMO", "K44", src)}
        sites = G.build({}, {}, base, "Base GE.xlsx", ["SDMO", "FG Wilson"], closing)
        G.add_prp(sites, gdb, "Closing.xlsb")
        self.assertEqual(G.closing_if_base_unknown(sites, base, closing, gdb, "Closing.xlsb", None), 1)
        a = sites["A"]
        self.assertEqual((a["type_ge"], a["fabricant_ge"], a["prp_kw"], a["type_ge_base"]), ("P33-3", "FG Wilson", [24.0], "FG Wilson P33-3X"))
        self.assertEqual(a["source_ge"], src + " — type Base GE « FG Wilson P33-3X » : type « FG Wilson P33-3X » absent de GENSET DB")
        self.assertEqual((sites["B"]["type_ge"], sites["B"]["prp_kva"]), ("J66K", [60.0]))   # Base GE reconnu : conservé
        self.assertEqual((sites["C"]["type_ge"], sites["C"]["prp_kva"]), ("SDMO - K33", None))  # clôture pas mieux : Base GE
        self.assertEqual(sites["D"]["type_ge"], "SDMO - K33")                # pas de ligne de clôture : Base GE


class FichesConstructeurTests(SimpleTestCase):
    """Décision du 25/11/2026 : fiches constructeur validées = PRP et courbe des types absents de GENSET DB (jamais le T66K)."""

    def test_fiche_apres_genset_db(self):
        from fuel_tracking.services import ge_info as G

        gdb, crv = {"P333": {30.0}, "K22": {22.0}}, {"K22": {(1.0, 2.0, 3.0)}}
        sites = {"A": {"type_ge": "FG Wilson P33-3U"}, "B": {"type_ge": "OLYMPIAN - GEP18-4"}, "C": {"type_ge": "SDMO - T66K"},
                 "D": {"type_ge": "K22 / P33-3U"}, "E": {"type_ge": "SDMO - K33"}}
        G.add_prp(sites, gdb, "Closing.xlsb", crv)
        a = sites["A"]
        self.assertEqual((a["prp_kva"], a["prp_kw"], a["courbes_cph"]), ([30.0], [24.0], [(2.4, 2.6, 1.9)]))
        self.assertEqual(a["prp_source"], "fiche constructeur FC-07 (FG Wilson P33-3U, fiche FG Wilson du P33-3U)")
        self.assertEqual((sites["B"]["prp_kva"], sites["B"]["courbes_cph"]), ([16.5], [(1.6, 1.2, 1.6)]))
        self.assertIsNone(sites["C"]["prp_kva"])                             # T66K : équivalence provisoire, non reprise
        d = sites["D"]                                                       # deux GE : GENSET DB + fiche, les deux sources
        self.assertEqual(d["prp_kva"], [22.0, 30.0])
        self.assertEqual(d["prp_source"], "GENSET DB (Closing.xlsb) ; fiche constructeur FC-07 (FG Wilson P33-3U, fiche FG Wilson du P33-3U)")
        self.assertIsNone(sites["E"]["prp_kva"])
        self.assertIn("absent de GENSET DB", sites["E"]["prp_motif"])
        g = {"P333U": {33.0}}                                               # type présent dans GENSET DB : GENSET DB prime
        s2 = {"X": {"type_ge": "P33-3U"}}
        G.add_prp(s2, g, "Closing.xlsb")
        self.assertEqual((s2["X"]["prp_kva"], s2["X"]["prp_source"]), ([33.0], "GENSET DB (Closing.xlsb)"))


class TableKpisTests(SimpleTestCase):
    """Décision du 25/11/2026 : synthèse du mois = totaux du tableau Suivi Conso (aucune valeur lue dans un fichier)."""

    def test_synthese_depuis_le_tableau(self):
        from fuel_tracking.services.closing_kpis import table_kpis

        rav = lambda **k: {"source": "ENOC", **dict.fromkeys(("eydon_l", "carte_total_l", "ajout_l", "retrait_l", "vole_l", "fuite_l"), 0.0), **k}  # noqa: E731
        rows = [
            {"stock_initial_sources": {"retenue_l": 1000.0, "date": "2026-09-30"}, "stock_final_sources": {"retenue_l": 400.0, "date": "2026-10-30"},
             "ravitaillement": rav(eydon_l=600.0, vole_l=50.0), "estimation_theorique": {"type_ge": "K22"}},          # conso 1 150 L / 30 j
            {"stock_initial_sources": {"retenue_l": 500.0, "date": "2026-10-01"}, "stock_final_sources": {"retenue_l": None},
             "ravitaillement": rav(ajout_l=100.0), "estimation_theorique": {"type_ge": None}},                         # incomplet
            {"stock_initial_sources": {"retenue_l": 200.0, "date": "2026-10-01"}, "stock_final_sources": {"retenue_l": 150.0, "date": "2026-10-11"},
             "ravitaillement": {"source": None}, "estimation_theorique": {"type_ge": "J66K"}},                      # ENOC non lu : incomplet
        ]
        k = table_kpis(rows, date(2026, 10, 1), date(2026, 10, 31))
        val = {x: k[x]["valeur"] for x in k}
        self.assertEqual((val["nb_sites"], val["nb_sites_ge"]), (3, 2))
        self.assertEqual((val["stock_initial_l"], val["stock_final_l"]), (1700.0, 550.0))   # = totaux des colonnes « retenue »
        self.assertEqual((val["livree_l"], val["autre_l"], val["perdue_l"]), (600.0, 100.0, 50.0))
        self.assertEqual(val["conso_releves_l"], 1150.0)                                    # seul le site complet
        self.assertAlmostEqual(val["conso_moyenne_jour_l"], 1150 / 30, places=3)
        self.assertEqual(val["check_l"], 650.0)                                             # 1700+600+100−550−1150−50 : sites incomplets
        self.assertIsNone(val["commande_mois_suivant_l"])                                   # aucune source : n.d., jamais 0
        self.assertIn("Tableau Suivi Conso", k["stock_initial_l"]["detail"]["source"])


class GeFiltersTests(SimpleTestCase):
    """Filtres de l'écran Suivi Conso : Fabriquant GE, Type de GE, Source de donnée GE (n.d. = « AUCUN »)."""

    def test_famille_de_source_et_filtres(self):
        from fuel_tracking.services import conso_comparison as C
        from fuel_tracking.services import ge_info as G

        self.assertEqual(G.source_family("Fichier Base GE (Base GE.xlsx)"), "Fichier Base GE")
        self.assertEqual(G.source_family("Fichier de clôture (Output App Fuel, 2026-09-30 → 2026-10-31)"), "Fichier de clôture")
        self.assertEqual(G.source_family("Fichier de clôture (Closing, a → b) — type Base GE « X » : absent"),
                         "Fichier de clôture (type Base GE non reconnu)")
        self.assertEqual(G.source_family("Fabricant : Snowflake (SITE_DG) · Type : ENOC (registre des GE)"), "ENOC (registre des GE)")
        self.assertIsNone(G.source_family(None))
        rows = [{"ge": {"fabricant": "SDMO", "type": "K22", "source": "Fichier Base GE"}},
                {"ge": {"fabricant": None, "type": "GEP 22-6", "source": "Fichier de clôture"}},
                {"ge": {"fabricant": None, "type": None, "source": None}}]
        f = lambda **p: C._filter(rows, {**{k: None for k in ("pays", "zone", "typologie", "typologie_energie", "statut_bilan",  # noqa: E731
                                                              "source_runtime", "source_puissance", "source_target", "site",
                                                              "correspondance", "statut_certification", "qualite")}, **p})
        self.assertEqual(len(f(fabricant_ge="AUCUN")), 2)
        self.assertEqual(f(type_ge="gep 22-6"), [rows[1]])
        self.assertEqual(f(source_ge="Fichier Base GE"), [rows[0]])


class ColumnFiltersTests(SimpleTestCase):
    """Filtres de colonnes « comme Excel » : valeur affichée → clé ; vides distincts de 0 ; valeurs selon les autres filtres."""

    def test_cles_filtres_et_valeurs(self):
        from fuel_tracking.services import conso_column_filters as CF

        self.assertEqual((CF.vkey(None), CF.vkey(0), CF.vkey(Decimal("1234.500")), CF.vkey([22.0, 30.0]), CF.vkey("  SDMO  K22 ")),
                         ("", "0", "1234.5", "22 / 30", "SDMO K22"))
        rows = [{"site_id": "A", "site_name": "a", "estimation_theorique": {"type_ge": "K22", "conso_theorique_l": 10.0}},
                {"site_id": "B", "site_name": "b", "estimation_theorique": {"type_ge": "K22", "conso_theorique_l": None}},
                {"site_id": "C", "site_name": "c", "estimation_theorique": {"type_ge": None, "conso_theorique_l": 0.0}}]
        idx = CF.index(rows)
        f = CF.parse('{"estimation_theorique.type_ge": ["K22"], "inconnue.x": ["1"]}')
        self.assertEqual(list(f), ["estimation_theorique.type_ge"])                      # colonne inconnue ignorée
        self.assertEqual([s for s in "ABC" if CF.keep(idx, f, s)], ["A", "B"])
        vals = CF.distinct(idx, f, "ABC", "estimation_theorique.conso_theorique_l")      # parmi les sites K22 seulement
        self.assertEqual(vals, [{"valeur": "10", "nb": 1}, {"valeur": "", "nb": 1}])     # (Vides) en dernier, 0 ≠ vide
        self.assertEqual(CF.distinct(idx, f, "ABC", "estimation_theorique.type_ge"),     # sa propre colonne : tous les choix
                         [{"valeur": "K22", "nb": 2}, {"valeur": "", "nb": 1}])
        self.assertEqual(CF.parse("pas du json"), {})

    def test_avec_ou_sans_ge(self):
        from fuel_tracking.services import conso_comparison as C

        rows = [{"ge": {"type": "K22"}}, {"ge": {"type": None}}]
        self.assertEqual(C._filter(rows, {"avec_ge": "oui"}), [rows[0]])
        self.assertEqual(C._filter(rows, {"avec_ge": "non"}), [rows[1]])


class StockVsTheoreticalTests(MvpBase):
    """Bloc « ANALYSE VARIATION DE STOCK Vs ESTIMATION THÉORIQUE » : règles du fichier Output App Fuel d'Ops (octobre 2026)."""

    @staticmethod
    def row(theo=1848.47, ini=1000.0, fin=400.0, src="ENOC", **rav):
        rv = {"source": src, **dict.fromkeys(("eydon_l", "carte_total_l", "ajout_l", "retrait_l", "vole_l", "fuite_l"), 0.0), **rav}
        return {"site_id": "S_1", "stock_initial_sources": {"retenue_l": ini}, "stock_final_sources": {"retenue_l": fin},
                "ravitaillement": rv, "estimation_theorique": {"conso_retenue_l": theo, "conso_retenue_motif": None if theo is not None else "Puissance retenue n.d."}}

    def test_variation_delta_and_flag(self):
        from fuel_tracking.services.stock_initial_sources import stock_vs_theoretical as A

        a = A(self.row(theo=1000.0, eydon_l=600.0, carte_total_l=100.0, ajout_l=50.0, retrait_l=20.0, vole_l=10.0, fuite_l=5.0))
        self.assertEqual(a["variation_l"], 1000 + 600 + 100 + 50 - 20 - 10 - 5 - 400)   # 1 315 L
        self.assertEqual(a["delta_l"], 1000 - 1315)
        self.assertEqual(a["grande_difference"], "Oui")                                    # |−315| > 100

    def test_threshold_strictly_above_100(self):
        from fuel_tracking.services.stock_initial_sources import stock_vs_theoretical as A

        self.assertEqual(A(self.row(theo=700.0))["delta_l"], 100.0)                 # variation 600 L → DELTA 100 L
        self.assertEqual(A(self.row(theo=700.0))["grande_difference"], "Non")      # 100 L pile : pas au-dessus du seuil
        self.assertEqual(A(self.row(theo=700.5))["grande_difference"], "Oui")
        self.assertEqual(A(self.row(theo=499.0))["grande_difference"], "Oui")      # DELTA −101 : écart dans l'autre sens
        self.assertEqual(A(self.row(theo=550.0))["grande_difference"], "Non")

    def test_missing_values_stay_null(self):
        from fuel_tracking.services.stock_initial_sources import stock_vs_theoretical as A

        a = A(self.row(theo=None))
        self.assertEqual((a["conso_theorique_retenue_l"], a["delta_l"], a["grande_difference"]), (None, None, None))
        self.assertEqual(a["variation_l"], 600.0)                                          # la variation reste calculée
        self.assertIn("Puissance retenue n.d.", a["motif"])
        a = A(self.row(fin=None))
        self.assertEqual((a["variation_l"], a["delta_l"], a["grande_difference"]), (None, None, None))
        self.assertIn("stock final retenu n.d.", a["variation_motif"])
        a = A(self.row(src=None))
        self.assertIsNone(a["variation_l"])
        self.assertIn("ENOC", a["variation_motif"])
        a = A(self.row(theo=0.0, ini=0.0, fin=0.0))                                        # 0 reste 0
        self.assertEqual((a["variation_l"], a["delta_l"], a["grande_difference"]), (0.0, 0.0, "Non"))

    def test_negative_variation_kept(self):
        from fuel_tracking.services.stock_initial_sources import stock_vs_theoretical as A

        a = A(self.row(theo=50.0, ini=100.0, fin=300.0))
        self.assertEqual((a["variation_l"], a["delta_l"], a["grande_difference"]), (-200.0, 250.0, "Oui"))

    def test_totals_count_oui(self):
        from unittest import mock

        from fuel_tracking.services import stock_initial_sources as S

        def fake_enrich(rows, fetch=True):
            for r, (v, d, g) in zip(rows, [(600.0, 150.0, "Oui"), (300.0, -20.0, "Non"), (None, None, None)]):
                r["analyse_variation"] = {"variation_l": v, "delta_l": d, "grande_difference": g, "conso_theorique_retenue_l": None}

        rows = [{"site_id": f"S_{i}", "stock": {}} for i in range(3)]
        with mock.patch.object(S, "enrich", side_effect=fake_enrich):
            t = S.totals(rows)
        self.assertEqual(t["analyse_variation"]["variation_l"], {"total": 900.0, "sites": 2})
        self.assertEqual(t["analyse_variation"]["delta_l"], {"total": 130.0, "sites": 2})
        self.assertEqual(t["analyse_variation"]["conso_theorique_retenue_l"], {"total": None, "sites": 0})
        self.assertEqual(t["analyse_variation"]["grande_difference"], {"total": 1, "sites": 2, "oui": True, "valeur": "Oui"})

    def test_filter_keys(self):
        from fuel_tracking.services import conso_column_filters as CF

        r = {"site_id": "S_1", "analyse_variation": {"grande_difference": "Oui", "delta_l": 150.25, "variation_l": None}}
        idx = CF.index([r])["S_1"]
        self.assertEqual((idx["analyse_variation.grande_difference"], idx["analyse_variation.delta_l"], idx["analyse_variation.variation_l"]),
                         ("Oui", "150.25", ""))


class AlerteAppFuelTests(MvpBase):
    """Alerte App Fuel : conforme si |variation − théorique| ≤ max(50 L ; 5 % × théorique) (process validation conso fuel)."""

    row = staticmethod(StockVsTheoreticalTests.row)

    def test_exemples_du_process(self):
        from fuel_tracking.services.stock_initial_sources import stock_vs_theoretical as A

        a = A(self.row(theo=620.0, ini=1000.0, fin=400.0))           # variation 600 L : écart 20 L, tolérance max(50 ; 31) = 50 L
        self.assertEqual((a["tolerance_l"], a["alerte"]), (50.0, "Conforme"))
        a = A(self.row(theo=620.0, ini=1300.0, fin=400.0))           # variation 900 L : écart 280 L pour 50 L tolérés
        self.assertEqual(a["alerte"], "En écart")
        self.assertIn("280 L > tolérance 50 L", a["alerte_motif"])

    def test_tolerance_5_pct_and_limits(self):
        from fuel_tracking.services.stock_initial_sources import stock_vs_theoretical as A

        a = A(self.row(theo=2000.0, ini=1500.0, fin=400.0))          # variation 1 100 L, écart 900 L, tolérance 100 L
        self.assertEqual((a["tolerance_l"], a["alerte"]), (100.0, "En écart"))
        self.assertEqual(A(self.row(theo=2000.0, ini=2500.0, fin=400.0))["alerte"], "Conforme")   # écart 100 L = tolérance
        self.assertEqual(A(self.row(theo=2000.0, ini=2501.0, fin=400.0))["alerte"], "En écart")   # écart 101 L
        self.assertEqual(A(self.row(theo=0.0, ini=50.0, fin=0.0))["alerte"], "Conforme")          # 0 L théorique : 50 L tolérés

    def test_nd_never_conforme(self):
        from fuel_tracking.services.stock_initial_sources import stock_vs_theoretical as A

        a = A(self.row(theo=None))
        self.assertEqual((a["alerte"], a["tolerance_l"]), (None, None))
        self.assertIn("Puissance retenue n.d.", a["alerte_motif"])
        a = A(self.row(fin=None))
        self.assertIsNone(a["alerte"])
        self.assertIn("stock final retenu n.d.", a["alerte_motif"])

    def test_totals_count_ecart(self):
        from unittest import mock

        from fuel_tracking.services import stock_initial_sources as S

        def fake_enrich(rows, fetch=True):
            for r, v in zip(rows, ["En écart", "Conforme", None, "En écart"]):
                r["analyse_variation"] = {"alerte": v}

        rows = [{"site_id": f"S_{i}", "stock": {}} for i in range(4)]
        with mock.patch.object(S, "enrich", side_effect=fake_enrich):
            t = S.totals(rows)
        self.assertEqual(t["analyse_variation"]["alerte"], {"total": 2, "sites": 3, "oui": True, "valeur": "En écart"})


class RuntimeVisibilityTests(MvpBase):
    """Temps de marche GE : visibilité (%) de chaque source ; seules les sources vues sur plus de 50 % de la période sont retenues."""

    @staticmethod
    def e(**kw):
        base = {"tm_jours_periode": 30, "tm_index_fms_h": None, "tm_terrain_h": None, "tm_dg_on_h": None, "tm_dg_on_jours": 0,
                "tm_rectifier_h": None, "tm_rectifier_dispo_pct": None, "tm_rectifier_off_grid": True}
        return {**base, **kw}

    def test_visibility_per_source(self):
        from fuel_tracking.services.stock_initial_sources import retained_runtime as R

        o = R(self.e(tm_index_fms_h=7.3, tm_terrain_h=7.0, tm_dg_on_h=0.0, tm_dg_on_jours=12, tm_rectifier_h=480.3, tm_rectifier_dispo_pct=83.4))
        self.assertEqual((o["tm_index_fms_visibilite_pct"], o["tm_terrain_visibilite_pct"], o["tm_dg_on_visibilite_pct"],
                          o["tm_rectifier_visibilite_pct"]), (100.0, 100.0, 40.0, 83.4))
        self.assertEqual((o["tm_retenu_h"], o["tm_source"]), (7.3, "FMS"))
        self.assertIn("40 % ≤ 50 %", o["tm_dg_on_ecart_motif"])

    def test_dg_on_below_50_pct_no_longer_retained(self):
        from fuel_tracking.services.stock_initial_sources import retained_runtime as R

        # avant : 0,00 h day_dg_on retenu ; 15 jours sur 30 = 50 % → pas « plus de 50 % » → écarté, redresseur hors réseau retenu
        o = R(self.e(tm_dg_on_h=0.0, tm_dg_on_jours=15, tm_rectifier_h=412.83, tm_rectifier_dispo_pct=90.0))
        self.assertEqual((o["tm_retenu_h"], o["tm_source"]), (412.83, "Alarme day_rectifier_on FMS"))
        self.assertEqual(o["tm_sources_ecartees"], ["Alarme day_dg_on FMS (50 %)"])
        o = R(self.e(tm_dg_on_h=6.08, tm_dg_on_jours=16))                               # 53,3 % → retenu
        self.assertEqual((o["tm_retenu_h"], o["tm_source"], o["tm_dg_on_visibilite_pct"]), (6.08, "Alarme day_dg_on FMS", 53.3))

    def test_nothing_visible_enough_is_nd(self):
        from fuel_tracking.services.stock_initial_sources import retained_runtime as R

        o = R(self.e(tm_dg_on_h=0.0, tm_dg_on_jours=3, tm_rectifier_h=10.0, tm_rectifier_dispo_pct=20.0))
        self.assertEqual((o["tm_retenu_h"], o["tm_source"]), (None, None))            # jamais 0 par défaut
        self.assertEqual(len(o["tm_sources_ecartees"]), 2)
        o = R(self.e())
        self.assertEqual((o["tm_retenu_h"], o["tm_dg_on_visibilite_pct"], o["tm_sources_ecartees"]), (None, None, None))

    def test_rectifier_on_grid_still_excluded(self):
        from fuel_tracking.services.stock_initial_sources import retained_runtime as R

        o = R(self.e(tm_rectifier_h=400.0, tm_rectifier_dispo_pct=95.0, tm_rectifier_off_grid=False))
        self.assertEqual((o["tm_retenu_h"], o["tm_rectifier_visibilite_pct"]), (None, 95.0))
