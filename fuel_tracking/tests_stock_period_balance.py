"""Bilan stock par site et période exacte : formule, sources de stock, conversion cm → L, statuts, certification."""
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Site
from fuel_tracking.models import FuelEnocMovement, FuelStockBalance, FuelStockObservation as Obs, FuelTankMaster
from fuel_tracking.management.commands.sync_enoc_fuel_movements import clean_payload
from fuel_tracking.services import enoc_canonical as C
from fuel_tracking.services import stock_enoc as E
from fuel_tracking.services import stock_period_balance as SB
from fuel_tracking.tests_enoc_qualification import EnocBase


class BalanceBase(EnocBase):
    def setUp(self):
        super().setUp()
        Site.objects.filter(site_id="DKR_0001").update(zone="DKR", country="sen", installed_typology="A_Ax_GG GE")
        Site.objects.create(site_id="DKR_0002", name="SOURCE", zone="DKR", country="sen")
        day0 = timezone.localtime(timezone.now() - timedelta(days=40)).date()
        self.base = timezone.make_aware(datetime.combine(day0, time.min))
        self.start = self.base + timedelta(days=1)                                  # « le 2 » 00:00
        self.end = self.base + timedelta(days=19, hours=23, minutes=59, seconds=59)  # « le 20 » 23:59:59
        self.n = 0

    def obs(self, liters, at, site="DKR_0001", source="LOLIFORCE", **extra):
        self.n += 1
        values = dict(source_type=source, source_event_id=f"obs-{self.n}", site_id=site, tank_id="1", observation_datetime=at,
                      level_raw=f"{liters} L", level_value=liters, level_unit="L", level_liters=liters,
                      conversion_method="DIRECT_LITERS", conversion_status="VALID", validation_status="VALIDATED")
        values.update(extra)
        return Obs.objects.create(**values)

    def stocks(self, ini=500, fin=300, site="DKR_0001"):
        self.obs(ini, self.start + timedelta(hours=1), site=site)
        if fin is not None:
            self.obs(fin, self.end - timedelta(hours=1), site=site)

    def tc(self, code, qty, at, **kw):
        m = self.mk(code, qty=qty, op_extra={"operation_date": at.isoformat()}, **kw)
        C.sync_qualified([m])
        return m

    def compute(self, sites=("DKR_0001",)):
        rows = SB.compute_balances(self.start, self.end, site_ids=list(sites))
        return {r["site_id"]: r for r in rows}


class StockPeriodBalanceTests(BalanceBase):
    def test_complete_balance_with_stocks_and_movements(self):
        self.stocks(500, 300)
        self.tc("TC-B1", 400, self.base + timedelta(days=10))
        r = self.compute()["DKR_0001"]
        self.assertEqual((float(r["stock_initial_l"]), float(r["carte_total_l"]), float(r["stock_final_l"])), (500.0, 400.0, 300.0))
        self.assertEqual(float(r["consommation_reelle_l"]), 600.0)  # 500 + 400 − 300
        self.assertEqual((r["statut_bilan"], r["source_stock_initial"], r["source_stock_final"]), ("PRET_A_CERTIFIER", "LOLIFORCE", "LOLIFORCE"))
        b = FuelStockBalance.objects.get(site_id="DKR_0001")
        self.assertEqual((b.niveau_certification, b.rule_version, b.typologie, b.country), ("NON_CERTIFIE", "bilan-v1", "A_Ax_GG GE", "sen"))
        self.assertEqual(b.mouvements[0]["composante"], "carte_total_l")

    def test_ponction_impacts_source_and_destination_with_zero_country_net(self):
        self.stocks(500, 300)
        self.stocks(800, 600, site="DKR_0002")
        action = {"validated_at": (self.base + timedelta(days=5)).isoformat(), "validated_by": "FM"}
        m = self.mk("PO-B1", typ="PONCTION", status="validated", operation=False,
                    req_extra={"approved_quantity_liters": 100, "action": action,
                               "ponction": {"source_site_id": "DKR_0002", "level_before": 800, "level_before_unit": "L",
                                            "level_after": 700, "level_after_unit": "L"}})
        E.sync_enoc_to_registry(only_ids=[m.pk])
        rows = self.compute(("DKR_0001", "DKR_0002"))
        dst, src = rows["DKR_0001"], rows["DKR_0002"]
        self.assertEqual((float(dst["transferts_entrants_l"]), float(src["transferts_sortants_l"])), (100.0, 100.0))
        self.assertEqual((float(dst["consommation_reelle_l"]), float(src["consommation_reelle_l"])), (300.0, 100.0))
        net = sum(float(r["transferts_entrants_l"]) - float(r["transferts_sortants_l"]) for r in rows.values())
        self.assertEqual(net, 0.0)  # impact pays nul

    def test_missing_final_stock_gives_null_consumption(self):
        self.stocks(500, None)
        self.tc("TC-B2", 400, self.base + timedelta(days=10))
        r = self.compute()["DKR_0001"]
        self.assertIsNone(r["consommation_reelle_l"])  # jamais 0
        self.assertEqual(r["statut_bilan"], "DONNEES_STOCK_INCOMPLETES")
        self.assertIsNone(r["stock_final_l"])
        self.assertTrue(any("Stock final absent" in m for m in r["motifs"]))

    def test_cm_level_with_validated_tank_conversion_traced(self):
        from fuel_tracking.services import stock_registry as R
        from fuel_tracking.services import tank_conversion as TC

        FuelTankMaster.objects.create(site_id="DKR_0001", tank_id="1", validation_status="VALIDEE", shape_family="rect",
                                      dimension_1_cm=100, dimension_2_cm=100, dimension_3_cm=100, capacity_plate_l=1000)
        res = TC.convert(50.0, "CM", R.site_tanks(["DKR_0001"])["DKR_0001"])
        self.assertEqual(res.status, "VALID")
        self.obs(res.level_liters, self.start + timedelta(hours=2), level_raw="50 cm", level_value=50, level_unit="CM", level_cm=50,
                 conversion_method=res.method, conversion_rule=res.rule, tank_model_ref=res.model_ref)
        self.obs(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        d = r["stock_initial_detail"]
        self.assertEqual((d["unite_origine"], d["niveau_cm"], d["valeur_brute"]), ("CM", 50.0, "50 cm"))  # mesure brute conservée
        self.assertEqual((d["litres"], d["methode_conversion"]), (res.level_liters, res.method))
        self.assertTrue(d["regle_conversion"])
        self.assertEqual(float(r["consommation_reelle_l"]), res.level_liters - 300)

    def test_cm_level_without_validated_tank_not_converted(self):
        self.obs(None, self.start + timedelta(hours=2), level_raw="50 cm", level_value=50, level_unit="CM", level_cm=50,
                 level_liters=None, conversion_method="NONE", conversion_status="PENDING_TANK_MAPPING")
        self.obs(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        self.assertIsNone(r["stock_initial_l"])  # aucun litre inventé
        self.assertEqual(r["statut_bilan"], "DONNEES_STOCK_INCOMPLETES")
        cand = r["candidats"]["initial"][0]
        self.assertEqual((cand["utilisable"], cand["niveau_cm"]), (False, 50.0))
        self.assertIn("CONVERSION_CUVE_MANQUANTE", cand["motif_rejet"])
        self.assertTrue(any("CONVERSION_CUVE_MANQUANTE" in a for a in r["alertes"]))

    def test_several_candidates_priority_documented(self):
        t = self.start + timedelta(hours=1)
        self.obs(520, t, source="ENOC", eligible_for_stock=True, business_status="VALIDEE_EXECUTEE")
        self.obs(500, t + timedelta(hours=1), source="LOLIFORCE")
        self.obs(480, t + timedelta(hours=3), source="CLOSING")
        self.obs(900, t, source="SCSS", validation_status="PENDING")
        self.obs(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        d = r["stock_initial_detail"]
        self.assertEqual((r["source_stock_initial"], float(r["stock_initial_l"])), ("CLOSING", 480.0))  # priorité 1
        self.assertIn("Closing mensuel validé (priorité 1)", d["motif_selection"])
        self.assertIn("2 autre(s) candidat(s) utilisable(s) écarté(s)", d["motif_selection"])
        self.assertEqual(len(r["candidats"]["initial"]), 4)
        self.assertIn("non validé", next(c for c in r["candidats"]["initial"] if c["source"] == "SCSS")["motif_rejet"])
        Obs.objects.filter(source_type="CLOSING").delete()
        Obs.objects.filter(source_type="ENOC").update(level_liters=900)
        r = self.compute()["DKR_0001"]
        self.assertEqual(r["source_stock_initial"], "LOLIFORCE")  # gardiennage avant ENOC
        self.assertTrue(any("ECART_STOCK_CANDIDATS" in a for a in r["alertes"]))  # 500 vs 900 : contradiction signalée
        self.assertEqual(r["statut_bilan"], "PRET_A_CERTIFIER_AVEC_RESERVE")

    def test_historical_non_verifiable_movement_excluded(self):
        self.stocks(500, 300)
        item = {"source_id": "REST-B1", "request_code": "REST-B1", "status": "done", "site_id": "DKR_0001",
                "operation_type": "TOTAL_CARD", "operation_date": (self.base + timedelta(days=8)).isoformat(),
                "quantity_added_liters": 999}
        C.sync_qualified([FuelEnocMovement.objects.create(**clean_payload(item))])
        r = self.compute()["DKR_0001"]
        self.assertEqual(float(r["consommation_reelle_l"]), 200.0)  # les 999 L historiques ne comptent pas
        self.assertEqual(r["statut_bilan"], "HISTORIQUE_NON_VERIFIABLE")
        self.assertIn("Historique non vérifiable", r["mouvements_exclus"][0]["motif"])

    def test_period_2_to_20_only_counts_movements_inside(self):
        self.stocks(500, 300)
        self.tc("TC-B3", 111, self.base + timedelta(hours=12))            # « le 1er » : hors période
        self.tc("TC-B4", 400, self.base + timedelta(days=10))             # dans la période
        self.tc("TC-B5", 222, self.base + timedelta(days=20, hours=12))   # « le 21 » : hors période
        r = self.compute()["DKR_0001"]
        self.assertEqual(float(r["carte_total_l"]), 400.0)
        self.assertEqual([m["reference"] for m in r["mouvements"]], ["TC-B4"])
        self.assertEqual((r["observation_start"], r["observation_end"]), (self.start, self.end))

    def test_reconciliation_only_when_real_consumption_computed(self):
        self.stocks(500, 300)
        self.stocks(800, None, site="DKR_0002")
        self.compute(("DKR_0001", "DKR_0002"))
        cph = [{"site_id": "DKR_0001", "conso_mesuree_l": Decimal("150"), "conso_theorique_l": Decimal("250")},
               {"site_id": "DKR_0002", "conso_mesuree_l": Decimal("90"), "conso_theorique_l": None}]
        with mock.patch("fuel_tracking.services.cph_service.compute_period_summary", return_value=cph):
            rows = {r["site_id"]: r for r in SB.reconciliation(self.start, self.end)}
        a, b = rows["DKR_0001"], rows["DKR_0002"]
        self.assertEqual((a["conso_reelle_l"], a["ecart_reel_instrumentee_l"], a["ecart_reel_theorique_l"]), (200.0, 50.0, -50.0))
        self.assertEqual((b["conso_reelle_l"], b["conso_instrumentee_l"], b["ecart_reel_instrumentee_l"]), (None, 90.0, None))
        self.assertIn("aucun écart", b["motif"])

    def test_certification_history_user_date_comment(self):
        U = get_user_model()
        fm = U.objects.create_user("fm", "fm@x.sn", "p", role="manager")
        self.stocks(500, 300)
        self.compute()
        b = FuelStockBalance.objects.get(site_id="DKR_0001")
        with self.assertRaises(SB.BalanceError):
            SB.certify(b, fm, "certifier_avec_reserve", "")  # commentaire obligatoire si réserve
        c = APIClient()
        c.force_authenticate(U.objects.create_user("an", "an@x.sn", "p", role="analyst"))
        self.assertEqual(c.post(f"/api/fuel-tracking/stock/bilans/{b.pk}/certifier/", {"decision": "certifier"}, format="json").status_code, 403)
        c.force_authenticate(fm)
        r = c.post(f"/api/fuel-tracking/stock/bilans/{b.pk}/certifier/", {"decision": "certifier"}, format="json")
        self.assertEqual((r.status_code, r.json()["niveau_certification"]), (200, "CERTIFIE"))
        cert = b.certifications.get()
        self.assertEqual((cert.user_id, cert.action, cert.snapshot["consommation_reelle_l"]), (fm.pk, "CERTIFIE", "200.000"))
        self.assertIsNotNone(cert.created_at)
        cert.comment = "modifié"
        with self.assertRaises(ValueError):
            cert.save()  # historique non modifiable
        d = c.get(f"/api/fuel-tracking/stock/bilans/{b.pk}/").json()
        self.assertEqual((d["certifications"][0]["utilisateur"], d["certifications"][0]["toujours_valide"]), ("fm", True))
        Obs.objects.filter(site_id="DKR_0001", level_liters=300).update(level_liters=250)  # donnée modifiée après certification
        self.compute()
        b.refresh_from_db()
        self.assertEqual((b.niveau_certification, b.certifications.count()), ("A_RECERTIFIER", 1))

    def test_api_list_filters_compute_and_detail(self):
        U = get_user_model()
        c = APIClient()
        c.force_authenticate(U.objects.create_user("adm", "adm@x.sn", "p", role="admin"))
        self.stocks(500, 300)
        self.stocks(800, None, site="DKR_0002")
        debut, fin = self.start.strftime("%Y-%m-%d"), self.end.strftime("%Y-%m-%d")
        r = c.post("/api/fuel-tracking/stock/bilans/calculer/", {"debut": debut, "fin": fin, "site": "DKR_0001"}, format="json")
        self.assertEqual((r.status_code, r.json()["calculables"]), (200, 1), r.content)
        SB.compute_balances(self.start, self.end, site_ids=["DKR_0002"])
        url = f"/api/fuel-tracking/stock/bilans/?debut={debut}&fin={fin}"
        j = c.get(url).json()
        self.assertEqual((j["pagination"]["total"], j["totaux"]["calculables"], j["totaux"]["incomplets"]), (2, 1, 1))
        self.assertEqual(c.get(url + "&stock_final=absent").json()["data"][0]["site_id"], "DKR_0002")
        self.assertEqual(c.get(url + "&statut=PRET_A_CERTIFIER").json()["pagination"]["total"], 1)
        self.assertEqual(j["totaux"]["sources_stock"]["LOLIFORCE"], {"initial": 2, "final": 1})
        d = c.get(f"/api/fuel-tracking/stock/bilans/{j['data'][0]['id']}/").json()
        self.assertIn("candidats", d)
        self.assertEqual(len(d["priorite_sources"]), 4)


class TankPlateDiagnosticTests(BalanceBase):
    def test_diagnostic_finds_likely_input_error_read_only(self):
        from io import StringIO

        from django.core.management import call_command

        a = FuelTankMaster.objects.create(site_id="DKR_0001", tank_id="1", validation_status="VALIDEE", shape_family="h-circ",
                                          dimension_1_cm=157, dimension_2_cm=80, dimension_3_cm=268, capacity_plate_l=1350)
        b = FuelTankMaster.objects.create(site_id="DKR_0002", tank_id="1", validation_status="VALIDEE", shape_family="h-circ",
                                          dimension_1_cm=157, dimension_3_cm=268, capacity_plate_l=1500, capacity_l=5188)
        for t in (a, b):
            self.obs(None, self.start, site=t.site_id, level_unit="CM", level_value=50, level_liters=None,
                     conversion_status="GEOMETRIC_ONLY", validation_status="PENDING")
        out = StringIO()
        call_command("diagnostic_cuves_plaque", stdout=out)
        text = out.getvalue()
        self.assertIn("Cuves validées à plaque incohérente (relevés bloqués) : 2", text)
        self.assertIn("DKR_0001 cuve 1 h-circ 157.0/80.0/268.0 cm, plaque 1350.0 L", text)
        self.assertIn("→ DIAMETRE_EN_COTE_2", text)  # D = 80 cm : ≈ 1 347 L
        self.assertIn("→ PLAQUE_ERRONEE", text)       # cotes cohérentes avec la capacité déclarée
        a.refresh_from_db()
        self.assertEqual(float(a.dimension_1_cm), 157.0)  # lecture seule
