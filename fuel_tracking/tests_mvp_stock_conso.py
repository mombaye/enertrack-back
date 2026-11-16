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


class MvpEnocStatusTests(MvpBase):
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
    # Étape 1 — période historique (avant FUEL_APP_STOCK_START) : stock initial = Closing uniquement
    def test_historical_initial_from_closing_only_final_from_latest_enoc(self):
        self.obs(900, self.start + timedelta(hours=2))                       # gardiennage fichier (Loliforce) : ignoré
        self.closing_initial(500, self.start + timedelta(hours=1))
        self.obs(250, self.end - timedelta(hours=1))                         # gardiennage fichier en fin de période : ignoré
        self.enoc_level(350, self.end - timedelta(days=2))                   # plus ancien
        self.enoc_level(320, self.end - timedelta(hours=3))                  # plus récent → retenu
        r = self.compute()["DKR_0001"]
        self.assertEqual((r["source_stock_initial"], float(r["stock_initial_l"])), ("CLOSING", 500.0))
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

    def test_no_closing_initial_means_not_computable(self):
        self.obs(500, self.start + timedelta(hours=1))  # seul un relevé de gardiennage fichier : pas une source du stock initial historique
        self.enoc_level(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        self.assertIsNone(r["consommation_reelle_l"])
        self.assertEqual((r["statut_bilan"], r["cause_principale"]), ("NON_CALCULABLE", "DONNEES_STOCK_INCOMPLETES"))

    # Étape 1 — à partir de FUEL_APP_STOCK_START : stock initial = stock final du bilan précédent
    def test_app_period_initial_is_previous_balance_final(self):
        prev_start, prev_end = self.start - timedelta(days=19), self.start - timedelta(seconds=1)
        self.closing_initial(800, prev_start + timedelta(hours=1))
        self.enoc_level(500, prev_end - timedelta(hours=2))
        self.compute(start=prev_start, end=prev_end)                          # bilan précédent enregistré (stock final ENOC 500 L)
        self.closing_initial(999, self.start + timedelta(hours=1))           # Closing de la période : ignoré, l'application prend le relais
        self.enoc_level(300, self.end - timedelta(hours=1))
        with override_settings(FUEL_APP_STOCK_START=self.start.date().isoformat()):
            r = self.compute()["DKR_0001"]
        self.assertEqual((r["source_stock_initial"], float(r["stock_initial_l"])), ("APP", 500.0))
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


class MvpMovementsAndClosingTests(MvpBase):
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

    def upload(self, user, data, name="Esco Senegal - Synthese Conso Fuel Septembre 2026.xlsx"):
        self.client.force_authenticate(user)
        return self.client.post("/api/fuel-tracking/configuration/closing/", {"file": SimpleUploadedFile(name, data)}, format="multipart")

    def test_upload_imports_closing_and_recomputes_covered_months(self):
        first = (timezone.localdate().replace(day=1) - timedelta(days=1)).replace(day=1)   # 1er jour du dernier mois complet
        d1 = datetime.combine(first - timedelta(days=1), datetime.min.time())               # relevé initial : veille du mois
        d2 = datetime.combine(first + timedelta(days=20), datetime.min.time())
        res = self.upload(self.manager, self.closing_bytes(d1, d2))
        self.assertEqual(res.status_code, 202, res.content)
        b = FuelImportBatch.objects.get(source_type="CLOSING")
        self.assertEqual(b.summary["stocks_closing"]["stocks_importes"], 2, (b.status_message, b.summary))
        self.assertEqual(Obs.objects.filter(batch=b, site_id="DKR_0001").count(), 2)
        bal = FuelStockBalance.objects.filter(site_id="DKR_0001", observation_start__date=first)
        self.assertTrue(bal.exists())  # bilan du mois couvert recalculé sans attendre 3 h 11
        self.assertEqual((bal.get().source_stock_initial, float(bal.get().stock_initial_l)), ("CLOSING", 500.0))
        status = self.client.get("/api/fuel-tracking/configuration/closing/").json()
        self.assertEqual((status["lots"][0]["stocks_importes"], status["en_attente"], status["peut_envoyer"]), (2, [], True))

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
