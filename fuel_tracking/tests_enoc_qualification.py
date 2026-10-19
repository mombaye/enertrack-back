"""
ENOC → contrat canonique eGrid → qualification (type brut ≠ impact stock) → rapprochement.
Tests obligatoires (instruction §8) + parité du connecteur (aucune valeur vide → 0, toutes les opérations conservées).
"""
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Site
from fuel_tracking.management.commands.sync_enoc_fuel_movements import clean_payload
from fuel_tracking.models import FuelEnocMovement, FuelEnocQualifiedMovement as Q, FuelMovement, FuelRegistryCorrection
from fuel_tracking.services import enoc_canonical as C
from fuel_tracking.services import stock_enoc as E


class EnocBase(TestCase):
    def setUp(self):
        Site.objects.create(site_id="DKR_0001", name="ALPHA")
        self.when = timezone.now() - timedelta(days=2)

    def mk(self, code, typ="TOTAL_CARD", status="done", operation=True, qty=200, photos=("https://enoc/p/1.jpg",),
           site="DKR_0001", unit=None, op_extra=None, req_extra=None):
        req = {"_id": f"oid-{code}", "request_code": code, "status": status, "site_id": site, "site_name": "ALPHA",
               "zone": "DAKAR", "operation_type": typ, "requested_quantity_liters": 300, "approved_quantity_liters": 250,
               "date_refueling": (self.when - timedelta(days=1)).isoformat(), "created_at": (self.when - timedelta(days=3)).isoformat(),
               "level_before": 40, "level_before_unit": "cm", "hour_meter_before": 1200,
               "technician_name": "Tech A", "action": {"validated_by": "Fuel Manager", "validated_at": (self.when - timedelta(days=2)).isoformat()},
               **(req_extra or {})}
        op = None
        if operation:
            op = {"request_code": code, "operation_type": typ, "operation_date": self.when.isoformat(), "level_after": 80,
                  "level_after_unit": "cm", "hour_meter_after": 1210, "created_by": "Closer B", "created_at": self.when.isoformat(),
                  **({"quantity_added_liters": qty} if qty is not None else {}), **({"photos": list(photos)} if photos else {}),
                  **({"quantity_unit": unit} if unit else {}), **(op_extra or {})}
        flat = {"source_id": code, "request_code": code, "status": status, "site_id": site, "operation_type": typ,
                "request": req, "operation": op, "operations": [op] if op else []}
        return FuelEnocMovement.objects.create(**clean_payload(flat))

    def q(self, m):
        C.sync_qualified([m])
        return Q.objects.get(enoc_movement=m)


class QualificationRulesTests(EnocBase):
    def test_total_card_done_with_quantity_date_photo_is_to_reconcile_never_stock(self):
        m = self.mk("TC-1")
        q = self.q(m)
        self.assertEqual((q.type_family, q.business_classification, q.execution_state, q.reconciliation_status),
                         ("TOTAL_CARD", "RAVITAILLEMENT_DECLARE_AVEC_PREUVE", "A_RAPPROCHER", "A_RAPPROCHER"))
        self.assertEqual(q.reason, C.REASON_TC_OK)
        self.assertFalse(q.stock_impact or q.consumption_impact)
        E.sync_enoc_to_registry()
        mv = FuelMovement.objects.get(source_event_id="TC-1")
        self.assertFalse(mv.retained_in_stock)  # aucune écriture automatique en stock réel

    def test_total_card_without_actual_quantity_no_impact(self):
        q = self.q(self.mk("TC-2", qty=None))
        self.assertEqual((q.business_classification, q.execution_state, q.stock_impact), ("DEMANDE_CARTE_TOTAL", "QUANTITE_REELLE_MANQUANTE", False))
        q_open = self.q(self.mk("TC-3", status="validated", operation=False))
        self.assertEqual((q_open.business_classification, q_open.execution_state, q_open.stock_impact), ("DEMANDE_CARTE_TOTAL", "APPROUVEE", False))

    def test_ponction_is_nature_to_qualify(self):
        q = self.q(self.mk("PO-1", typ="PONCTION"))
        self.assertEqual((q.execution_state, q.reason, q.stock_impact, q.consumption_impact),
                         ("NATURE_A_QUALIFIER", "Signification métier de Ponction non validée", False, False))
        with self.assertRaises(C.ReconciliationError):
            C.reconcile(q, None, "rapprocher", "test", "relevé")

    def test_truck_incomplete_no_impact(self):
        q = self.q(self.mk("TR-1", typ="TRUCK", photos=()))
        self.assertEqual((q.business_classification, q.execution_state, q.stock_impact), ("LIVRAISON_A_CONFIRMER", "PREUVE_MANQUANTE", False))
        q2 = self.q(self.mk("TR-2", typ="TRUCK", status="pending", operation=False))
        self.assertEqual((q2.business_classification, q2.execution_state), ("LIVRAISON_A_CONFIRMER", "DEMANDE"))

    def test_truck_complete_with_proof_is_declared_to_reconcile_not_certified(self):
        q = self.q(self.mk("TR-3", typ="TRUCK", op_extra={"supplier": "EYDON", "delivery_note_number": "BL-77", "truck_number": "DK-1234-A"}))
        self.assertEqual((q.business_classification, q.execution_state, q.stock_impact, q.reconciliation_status),
                         ("LIVRAISON_DECLAREE_A_RAPPROCHER", "A_RAPPROCHER", False, "A_RAPPROCHER"))
        self.assertEqual((q.supplier_name, q.delivery_note_number, q.vehicle_or_truck_reference), ("EYDON", "BL-77", "DK-1234-A"))

    def test_missing_quantity_stays_null(self):
        m = self.mk("TC-4", qty=None, req_extra={"requested_quantity_liters": None, "approved_quantity_liters": ""})
        self.assertIsNone(m.quantity_added_liters)  # connecteur : jamais 0 par défaut
        q = self.q(m)
        self.assertEqual((q.actual_quantity_l, q.approved_quantity_l, q.quantity_unit), (None, None, None))
        self.assertEqual(Decimal(q.requested_quantity_l), Decimal("300")) if q.requested_quantity_l is not None else None

    def test_missing_proof_not_eligible(self):
        q = self.q(self.mk("TC-5", photos=()))
        self.assertEqual((q.execution_state, q.proof_count), ("PREUVE_MANQUANTE", 0))
        self.assertIn("preuve / photo", q.missing_conditions)
        with self.assertRaises(C.ReconciliationError):
            C.reconcile(q, None, "rapprocher", "motif", "relevé")

    def test_open_request_not_eligible(self):
        q = self.q(self.mk("TC-6", status="pending", operation=False))
        self.assertEqual((q.execution_state, q.stock_impact, q.actual_quantity_l), ("DEMANDE", False, None))

    def test_missing_site_not_eligible(self):
        q = self.q(self.mk("TC-7", site=None))
        self.assertEqual(q.execution_state, "TERMINEE")
        self.assertIn("site_id", q.missing_conditions)
        self.assertFalse(q.stock_impact)

    def test_rejected_and_blocking_incoherence(self):
        self.assertEqual(self.q(self.mk("TC-8", status="rejected")).execution_state, "REJETEE")
        q = self.q(self.mk("TC-9", req_extra={"level_before": 900, "level_before_unit": "L"},
                           op_extra={"level_after": 500, "level_after_unit": "L"}))
        self.assertTrue(q.blocking)
        self.assertEqual(q.execution_state, "TERMINEE")
        self.assertIn("NIVEAU_APRES_INFERIEUR", [a["code"] for a in q.alerts])

    def test_unknown_type_kept_without_guess(self):
        q = self.q(self.mk("XX-1", typ="LAVAGE"))
        self.assertEqual((q.type_family, q.raw_movement_type, q.execution_state), ("AUTRE", "LAVAGE", "NATURE_A_QUALIFIER"))


class CanonicalExtractionTests(EnocBase):
    def test_fields_and_sources_traced(self):
        q = self.q(self.mk("TC-10", op_extra={"attachments": [{"url": "https://enoc/f/a.pdf"}, {"url": "https://enoc/f/b.jpg"}]}))
        self.assertEqual((q.enoc_reference, q.enoc_request_id, q.site_id_resolved, q.raw_status), ("TC-10", "oid-TC-10", "DKR_0001", "done"))
        self.assertEqual((float(q.requested_quantity_l), float(q.approved_quantity_l), float(q.actual_quantity_l), q.quantity_unit),
                         (300.0, 250.0, 200.0, "L"))
        self.assertEqual((float(q.tank_level_before_cm), float(q.tank_level_after_cm), q.tank_level_before_l), (40.0, 80.0, None))
        self.assertEqual((float(q.generator_hourmeter_before_h), float(q.generator_hourmeter_after_h)), (1200.0, 1210.0))
        self.assertEqual((q.technician_name, q.validator_name, q.closer_name), ("Tech A", "Fuel Manager", "Closer B"))
        self.assertEqual(q.proof_count, 3)
        self.assertEqual(q.field_sources["actual"], "operation.quantity_added_liters")
        self.assertIn("implicite", q.field_sources["unit"])
        self.assertIsNotNone(q.planned_at)
        self.assertNotEqual(q.planned_at, q.actual_operation_at)  # date prévue ≠ date réelle
        m = FuelEnocMovement.objects.get(source_id="TC-10")
        self.assertEqual(m.raw_payload["operation"]["attachments"][0]["url"], "https://enoc/f/a.pdf")  # brut intact

    def test_actual_values_never_taken_from_request(self):
        m = self.mk("TC-11", operation=False, req_extra={"quantity_added_liters": 999, "operation_date": "2026-01-01"})
        q = self.q(m)
        self.assertEqual((q.actual_quantity_l, q.actual_operation_at), (None, None))

    def test_source_change_resets_reconciliation(self):
        user = get_user_model().objects.create_user("admin1", "admin1@x.sn", "p", role="admin")
        m = self.mk("TC-12")
        q = self.q(m)
        with self.assertRaises(C.ReconciliationError):
            C.reconcile(q, user, "rapprocher", "ok", "")  # élément de rapprochement obligatoire
        C.reconcile(q, user, "rapprocher", "Relevé Loliforce du lendemain cohérent", "Loliforce 02/10 : +200 L")
        q.refresh_from_db()
        self.assertEqual((q.execution_state, q.reconciliation_status, q.stock_impact, q.reconciled_by_id),
                         ("ELIGIBLE_STOCK_REEL", "RAPPROCHE", True, user.pk))
        E.sync_enoc_to_registry()
        self.assertTrue(FuelMovement.objects.get(source_event_id="TC-12").retained_in_stock)
        C.sync_qualified([m])  # source inchangée : rapprochement conservé
        q.refresh_from_db()
        self.assertEqual(q.execution_state, "ELIGIBLE_STOCK_REEL")
        raw = dict(m.raw_payload)
        raw["operation"] = {**raw["operation"], "quantity_added_liters": 260}
        m.raw_payload = raw
        m.save()
        C.sync_qualified([m])
        q.refresh_from_db()
        self.assertEqual((q.execution_state, q.stock_impact, q.reconciled_by_id), ("A_RAPPROCHER", False, None))
        self.assertTrue(FuelRegistryCorrection.objects.filter(entity="ENOC_MOVEMENT", field="reconciliation_status").exists())

    def test_certified_when_delivery_note(self):
        q = self.q(self.mk("TR-4", typ="TRUCK", op_extra={"delivery_note_number": "BL-1"}))
        C.reconcile(q, None, "rapprocher", "ok", "bon de livraison BL-1 + relevé")
        self.assertEqual(q.reconciliation_status, "CERTIFIE")

    def test_manual_validation_of_non_eligible_enoc_movement_refused(self):
        from fuel_tracking.services import stock_registry as R

        self.mk("TR-5", typ="TRUCK")
        E.sync_enoc_to_registry()
        mv = FuelMovement.objects.get(source_event_id="TR-5")
        with self.assertRaises(ValueError):
            R.validate_movement(mv, None, "forçage")


class LegacyRestPayloadTests(EnocBase):
    """Lignes de l'ancien connecteur API REST : raw_payload = élément REST (pas de request / operation)."""

    def rest(self, code, status="done", typ="TOTAL_CARD", qty=150, **extra):
        item = {"source_id": code, "request_code": code, "status": status, "site_id": "DKR_0001", "operation_type": typ,
                "operation_date": self.when.isoformat(), "requested_quantity_liters": 200, "approved_quantity_liters": 180,
                **({"quantity_added_liters": qty} if qty is not None else {}), **extra}
        return FuelEnocMovement.objects.create(**clean_payload(item))

    def test_legacy_done_with_quantity_and_photo_is_to_reconcile(self):
        q = self.q(self.rest("REST-1", photos=["https://enoc/p/9.jpg"]))
        self.assertEqual((q.raw_status, float(q.actual_quantity_l), q.quantity_unit, q.execution_state),
                         ("done", 150.0, "L", "A_RAPPROCHER"))
        self.assertIn("SOURCE_API_REST_HISTORIQUE", [a["code"] for a in q.alerts])
        self.assertEqual(q.field_sources["raw_status"], "rest.status")

    def test_legacy_without_proof_and_open_status(self):
        self.assertEqual(self.q(self.rest("REST-2")).execution_state, "PREUVE_MANQUANTE")
        self.assertEqual(self.q(self.rest("REST-3", status="pending", qty=None, operation_date=None)).execution_state, "DEMANDE")


class ConnectorParityTests(TestCase):
    def test_all_operations_kept_and_empty_quantities_null(self):
        from fuel_tracking.services import enoc_mongo_service as S

        class Coll:
            def __init__(self, docs):
                self.docs = docs

            def find(self, *a, **k):
                return list(self.docs)

        db = {"fuel_requests": Coll([{"_id": "r1", "request_code": "R1", "status": "done", "site_id": "DKR_0001", "operation_type": "TRUCK"}]),
              "fuel_operations": Coll([{"request_code": "R1", "operation_date": "2026-10-01T08:00:00", "quantity_added_liters": 100},
                                       {"request_code": "R1", "operation_date": "2026-10-02T08:00:00", "quantity_added_liters": 50}])}

        class DB:
            def __getattr__(self, name):
                return db[name]

        client = mock.MagicMock()
        client.__getitem__.return_value = DB()
        with mock.patch.object(S, "_connect", return_value=client):
            items = S.fetch_fuel_movements()
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0]["operations"]), 2)  # plus aucune opération écrasée
        self.assertEqual(items[0]["operation"]["quantity_added_liters"], 50)  # la plus récente
        clean = clean_payload(items[0])
        self.assertIsNone(clean["requested_quantity_liters"])  # absent → NULL, jamais 0
        self.assertIsNone(clean["approved_quantity_liters"])


class EnocApiAndAuditTests(EnocBase):
    def setUp(self):
        super().setUp()
        U = get_user_model()
        self.admin = U.objects.create_user("adm", "adm@x.sn", "p", role="admin")
        self.analyst = U.objects.create_user("ana", "ana@x.sn", "p", role="analyst")
        self.client = APIClient()

    def test_list_filters_totals_and_reconcile_api(self):
        self.mk("TC-20")
        self.mk("TC-21", status="pending", operation=False)
        self.mk("PO-20", typ="PONCTION")
        E.sync_enoc_to_registry()
        self.client.force_authenticate(self.analyst)
        r = self.client.get("/api/fuel-tracking/stock/enoc/mouvements/").json()
        self.assertEqual(r["pagination"]["total"], 3)
        t = r["totaux"]
        self.assertEqual((t["demandes"]["count"], t["declares"]["count"], t["eligibles"]["count"], t["certifies"]["count"]), (1, 1, 0, 0))
        self.assertEqual(self.client.get("/api/fuel-tracking/stock/enoc/mouvements/?type_brut=PONCTION").json()["pagination"]["total"], 1)
        self.assertEqual(self.client.get("/api/fuel-tracking/stock/enoc/mouvements/?impact=OUI").json()["pagination"]["total"], 0)
        self.assertEqual(self.client.get("/api/fuel-tracking/stock/enoc/mouvements/?preuve=AVEC").json()["pagination"]["total"], 2)
        q = Q.objects.get(enoc_reference="TC-20")
        self.assertEqual(self.client.post(f"/api/fuel-tracking/stock/enoc/mouvements/{q.pk}/rapprocher/",
                                          {"decision": "rapprocher", "reason": "ok", "evidence": "relevé"}, format="json").status_code, 403)
        self.client.force_authenticate(self.admin)
        r = self.client.post(f"/api/fuel-tracking/stock/enoc/mouvements/{q.pk}/rapprocher/",
                             {"decision": "rapprocher", "reason": "Relevé terrain cohérent", "evidence": "Loliforce +200 L"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["etat"], r.json()["impact_stock"]), ("ELIGIBLE_STOCK_REEL", True))
        d = self.client.get(f"/api/fuel-tracking/stock/enoc/mouvements/{q.pk}/").json()
        self.assertIn("contexte_rapprochement", d)
        self.assertIn("brut", d)

    def test_parity_audit_command(self):
        for i in range(3):
            self.mk(f"TC-3{i}")
        self.mk("PO-30", typ="PONCTION")
        out = StringIO()
        call_command("enoc_parity_audit", stdout=out)
        text = out.getvalue()
        self.assertIn("| Champ eGrid | Règle de transformation | Taux de présence | Écart constaté |", text)
        self.assertIn("operation.quantity_added_liters", text)
        self.assertNotIn("Section en erreur", text)


class StockBalanceTests(EnocBase):
    """§6 : seul ÉLIGIBLE_STOCK_RÉEL entre dans le bilan ; composante inconnue → NULL (jamais 0)."""

    def setUp(self):
        from fuel_tracking.models import FuelStockObservation, FuelTankMaster

        super().setUp()
        FuelTankMaster.objects.create(site_id="DKR_0001", tank_id="1", validation_status="VALIDEE", shape_family="rect",
                                      dimension_1_cm=100, dimension_2_cm=100, dimension_3_cm=100, capacity_plate_l=1000)
        now = timezone.now()
        for i, (days, liters) in enumerate(((10, 500), (1, 300))):
            FuelStockObservation.objects.create(
                source_type="LOLIFORCE", source_event_id=f"obs-{i}", site_id="DKR_0001", tank_id="1",
                observation_datetime=now - timedelta(days=days), level_raw=f"{liters} L", level_value=liters, level_unit="L",
                level_liters=liters, conversion_method="DIRECT_LITERS", conversion_status="VALID", validation_status="VALIDATED")
        self.start = (now - timedelta(days=10)).date()
        self.end = timezone.localdate()

    def balance(self):
        from fuel_tracking.services.stock_balance import site_balance

        return site_balance("DKR_0001", self.start, self.end, with_reference=False)

    def test_pure_stock_delta_when_no_movement(self):
        b = self.balance()
        self.assertEqual((b["conso_reelle_l"], b["certification"]), (200.0, "CERTIFIE"))

    def test_movement_to_reconcile_makes_balance_null_then_eligible_counts(self):
        m = self.mk("TC-40", qty=400)
        q = self.q(m)
        b = self.balance()
        self.assertIsNone(b["conso_reelle_l"])  # composante inconnue → NULL, jamais 0
        self.assertEqual((b["certification"], len(b["mouvements_inconnus"])), ("NON_CALCULABLE", 1))
        C.reconcile(q, None, "rapprocher", "Relevé Loliforce cohérent", "Loliforce J-1")
        b = self.balance()
        self.assertEqual((b["conso_reelle_l"], b["composantes_l"]["livraisons"], b["certification"]), (600.0, 400.0, "CALCULE_NON_CERTIFIE"))

    def test_demand_never_counted_and_missing_initial_stock_null(self):
        self.mk("TC-41", status="pending", operation=False)
        self.assertEqual(self.balance()["conso_reelle_l"], 200.0)  # une demande n'est pas un mouvement
        self.start = self.start - timedelta(days=30)
        from fuel_tracking.services.stock_balance import site_balance

        b = site_balance("DKR_0001", self.start - timedelta(days=60), self.start, with_reference=False)
        self.assertIsNone(b["conso_reelle_l"])
        self.assertTrue(any("Stock initial inconnu" in r for r in b["motifs"]))

    def test_balance_api(self):
        U = get_user_model()
        c = APIClient()
        c.force_authenticate(U.objects.create_user("ana2", "ana2@x.sn", "p", role="analyst"))
        r = c.get(f"/api/fuel-tracking/stock/registre/dkr_0001/bilan/?debut={self.start}&fin={self.end}")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn("references", r.json())
        self.assertEqual(r.json()["conso_reelle_l"], 200.0)
