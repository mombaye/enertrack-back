"""MVP Suivi Carburant : bilan par site et période exacte, fraîcheur, estimations avec réserve, score, imports, qualité."""
import json
from datetime import datetime, time, timedelta
from datetime import timezone as dt_tz
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Site
from fuel_tracking.models import (FuelEnocMovement, FuelEnocQualifiedMovement as Q, FuelImportBatch, FuelMovement, FuelStockBalance,
                                  FuelStockObservation as Obs, FuelTankMaster)
from fuel_tracking.management.commands.sync_enoc_fuel_movements import clean_payload
from fuel_tracking.services import enoc_canonical as C
from fuel_tracking.services import stock_enoc as E
from fuel_tracking.services import stock_import_pipeline as P
from fuel_tracking.services import stock_period_balance as SB
from fuel_tracking.services import stock_registry as R
from fuel_tracking.services import tank_conversion as TC
from fuel_tracking.tests_enoc_qualification import EnocBase
from fuel_tracking.tests_stock_registry import HEAD, TmpRootMixin, guard_row, tank, xlsx


class BalanceBase(EnocBase):
    def setUp(self):
        super().setUp()
        ov = override_settings(ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE=True)  # désactivé par défaut en production (décision Steve)
        ov.enable()
        self.addCleanup(ov.disable)
        Site.objects.filter(site_id="DKR_0001").update(zone="DKR", country="sen", installed_typology="A_Ax_GG GE")
        Site.objects.create(site_id="DKR_0002", name="SOURCE", zone="DKR", country="sen")
        day0 = timezone.localtime(timezone.now() - timedelta(days=60)).date()
        self.base = timezone.make_aware(datetime.combine(day0, time.min))
        self.start = self.base + timedelta(days=1)                                   # « le 2 » 00:00
        self.end = self.base + timedelta(days=19, hours=23, minutes=59, seconds=59)   # « le 20 » 23:59:59
        self.n = 0

    # ── relevés ───────────────────────────────────────────────────────────────────────────────────────────
    def obs(self, liters, at, site="DKR_0001", source="LOLIFORCE", **extra):
        """Relevé en litres directs, validé."""
        self.n += 1
        values = dict(source_type=source, source_event_id=f"obs-{self.n}", site_id=site, tank_id="1", observation_datetime=at,
                      level_raw=f"{liters} L", level_value=liters, level_unit="L", level_liters=liters, declared_liters=liters,
                      conversion_method="DIRECT_LITERS", conversion_status="VALID", validation_status="VALIDATED",
                      reading_quality_status="LITRE_DIRECT", confidence="HAUTE")
        values.update(extra)
        return Obs.objects.create(**values)

    def obs_cm(self, cm, at, site="DKR_0001", source="LOLIFORCE"):
        """Relevé en cm converti PAR LE MOTEUR (conversion certifiable, estimation ou aucun litre selon la cuve)."""
        self.n += 1
        tanks = R.site_tanks([site]).get(site, [])
        inp = R.ObservationInput(source, f"obs-cm-{self.n}", site, at, f"{cm} cm", float(cm), "CM", None)
        o, _ = R.build_observation(inp, tanks, None)
        o.save()
        return o

    def stocks(self, ini=500, fin=300, site="DKR_0001"):
        self.obs(ini, self.start + timedelta(hours=1), site=site)
        if fin is not None:
            self.obs(fin, self.end - timedelta(hours=1), site=site)

    # ── mouvements ────────────────────────────────────────────────────────────────────────────────────────
    def tc(self, code, qty, at, **kw):
        m = self.mk(code, qty=qty, op_extra={"operation_date": at.isoformat(), **(kw.pop("op_extra", {}))}, **kw)
        C.sync_qualified([m])
        return m

    def compute(self, sites=("DKR_0001",), start=None, end=None):
        rows = SB.compute_balances(start or self.start, end or self.end, site_ids=list(sites))
        return {r["site_id"]: r for r in rows}


class MvpBalanceTests(BalanceBase):
    # 1. bilan prêt à certifier
    def test_ready_to_certify_complete_balance(self):
        self.stocks(500, 300)
        self.tc("TR-1", 200, self.base + timedelta(days=10), typ="TRUCK",
                req_extra={"level_before": 100, "level_before_unit": "L"}, op_extra={"level_after": 300, "level_after_unit": "L"})
        r = self.compute()["DKR_0001"]
        self.assertEqual(float(r["consommation_reelle_l"]), 400.0)  # 500 + 200 − 300
        self.assertEqual((r["statut_bilan"], r["score_confiance"], r["cause_principale"]), ("PRET_A_CERTIFIER", 100, None))
        self.assertEqual({d["critere"]: d["points"] for d in r["score_detail"]},
                         {"stock_initial": 25, "stock_final": 25, "litre_direct_ou_cuve_validee": 20, "mouvements_complets": 15,
                          "stock_recent": 10, "sans_alerte_majeure": 5})
        self.assertEqual((float(r["livraisons_externes_l"]), float(r["entrees_externes_nettes_l"]), float(r["volume_brut_integre_l"])), (200.0, 200.0, 200.0))
        self.assertEqual(r["proposition"]["commande_automatique"], False)
        self.assertIsNone(r["proposition"]["avertissement"])

    # 2. bilan avec réserve (mouvement avec défauts de contrôle)
    def test_reserve_when_movement_has_missing_controls(self):
        self.stocks(500, 300)
        self.tc("TC-R1", 400, self.base + timedelta(days=10), photos=())  # niveaux en cm, sans preuve
        r = self.compute()["DKR_0001"]
        self.assertEqual((float(r["consommation_reelle_l"]), r["statut_bilan"], r["cause_principale"]), (600.0, "ESTIMATION_AVEC_RESERVE", "RESERVES"))
        self.assertTrue(any("contrôles manquants" in x for x in r["reserves"]))
        self.assertEqual(r["proposition"]["avertissement"], "ESTIMATION — VALIDATION FUEL MANAGER REQUISE")
        self.assertLess(r["score_confiance"], 100)

    # 3. bilan non calculable
    def test_not_computable_without_stocks(self):
        self.tc("TC-N1", 400, self.base + timedelta(days=10))
        r = self.compute()["DKR_0001"]
        self.assertIsNone(r["consommation_reelle_l"])  # jamais 0
        self.assertEqual((r["statut_bilan"], r["cause_principale"]), ("NON_CALCULABLE", "DONNEES_STOCK_INCOMPLETES"))
        self.assertTrue(any("Stock initial absent" in m for m in r["motifs"]))
        self.assertIsNone(r["proposition"]["quantite_indicative_l"])

    # 4. litre direct prioritaire sur une estimation
    def test_direct_liter_beats_estimate(self):
        tank("DKR_0001", plate=None)  # cuve validée sans plaque : estimation possible
        self.obs_cm(48, self.start + timedelta(hours=1), source="SCSS")
        self.obs(480, self.start + timedelta(hours=2), source="LOLIFORCE")
        self.obs(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        d = r["stock_initial_detail"]
        self.assertEqual((float(r["stock_initial_l"]), d["mode_valeur"], d["estimation"]), (480.0, "LITRE_DIRECT", False))
        self.assertEqual(r["statut_bilan"], "PRET_A_CERTIFIER")
        self.assertTrue(any(c["estimation"] for c in r["candidats"]["initial"]))  # l'estimation reste visible comme candidat

    # 5. cm avec cuve validée : conversion tracée
    def test_cm_with_validated_tank_traced(self):
        tank("DKR_0001")  # 100×100×100, plaque 1 000 L : 1 cm = 10 L
        o = self.obs_cm(50, self.start + timedelta(hours=1))
        self.assertEqual((o.reading_quality_status, o.confidence, float(o.level_liters), o.validation_status),
                         ("CM_CONVERTI_CUVE_VALIDEE", "HAUTE", 500.0, "VALIDATED"))
        self.obs(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        d = r["stock_initial_detail"]
        self.assertEqual((d["unite_origine"], d["niveau_cm"], d["valeur_brute"], d["litres"], d["certifiable"]), ("CM", 50.0, "50 cm", 500.0, True))
        self.assertTrue(d["regle_conversion"] and d["methode_conversion"] == "CALIBRATED")
        self.assertEqual(r["statut_bilan"], "PRET_A_CERTIFIER")

    # 6. cm avec cuve géométrique sans plaque : estimation avec réserve, jamais certifiable
    def test_cm_geometric_tank_without_plate_is_estimate_with_reserve(self):
        tank("DKR_0001", plate=None)
        o = self.obs_cm(60, self.start + timedelta(hours=1))
        self.assertEqual((o.reading_quality_status, o.confidence, o.estimation_mode, float(o.estimated_liters)),
                         ("CM_CONVERTI_CUVE_GEOMETRIQUE_RESERVE", "FAIBLE", "CM_CONVERTI_CUVE_GEOMETRIQUE_RESERVE", 600.0))
        self.assertIsNone(o.level_liters)  # jamais présenté comme valeur certifiée
        self.assertEqual((o.estimation_detail["forme"], o.estimation_detail["dimensions_cm"], o.estimation_detail["statut_cuve"]),
                         ("rect", [100.0, 100.0, 100.0], "VALIDEE"))
        self.assertTrue(o.estimation_detail["version"] and o.estimation_detail["formule"])
        self.obs(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        self.assertEqual((float(r["consommation_reelle_l"]), r["statut_bilan"], r["cause_principale"], r["estimation"]),
                         (300.0, "ESTIMATION_AVEC_RESERVE", "ESTIMATION", True))
        self.assertLess(r["score_confiance"], 100)
        self.assertTrue(any("ESTIMÉ" in x for x in r["reserves"]))
        # un niveau physiquement impossible n'est jamais estimé
        imp = self.obs_cm(500, self.start + timedelta(hours=3), source="SCSS")
        self.assertIsNone(imp.estimated_liters)
        # certification : une estimation ne peut qu'être « certifiée avec réserve », commentaire obligatoire
        b = FuelStockBalance.objects.get(site_id="DKR_0001")
        with self.assertRaises(SB.BalanceError):
            SB.certify(b, None, "certifier", "")
        with self.assertRaises(SB.BalanceError):
            SB.certify(b, None, "certifier_avec_reserve", "")
        SB.certify(b, None, "certifier_avec_reserve", "Cuve sans plaque : estimation validée par le Fuel Manager")

    # 6 bis. paramètre ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE désactivé : aucun litre, aucune estimation
    def test_geometric_estimation_disabled_by_setting(self):
        tank("DKR_0001", plate=None)
        with self.settings(ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE=False):
            o = self.obs_cm(60, self.start + timedelta(hours=1))
        self.assertEqual((o.reading_quality_status, o.estimated_liters, o.level_liters), ("CM_SANS_CUVE", None, None))
        self.assertIn("ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE", o.estimation_detail["refus"])

    def test_geometric_reserve_text_and_never_certifiable(self):
        tank("DKR_0001", plate=None)
        self.obs_cm(60, self.start + timedelta(hours=1))
        self.obs_cm(30, self.end - timedelta(hours=1), source="SCSS")
        r = self.compute()["DKR_0001"]
        self.assertEqual(r["statut_bilan"], "ESTIMATION_AVEC_RESERVE")
        self.assertTrue(any("Volume calculé à partir des dimensions de cuve, plaque non confirmée".lower() in x.lower() for x in r["reserves"]))
        b = FuelStockBalance.objects.get(site_id="DKR_0001")
        with self.assertRaises(SB.BalanceError):
            SB.certify(b, None, "certifier", "")

    # 7. cuve proposée : score de compatibilité configurable
    def test_proposed_tank_estimate_only_above_score_threshold(self):
        t = tank("DKR_0001", status="PROPOSEE", plate=None, compat_score=60)
        low = self.obs_cm(40, self.start + timedelta(hours=1))
        self.assertEqual((low.reading_quality_status, low.estimated_liters), ("CM_SANS_CUVE", None))
        self.assertIn("score de compatibilité 60 < seuil 70", low.estimation_detail["refus"])
        FuelTankMaster.objects.filter(pk=t.pk).update(compat_score=85)
        high = self.obs_cm(40, self.start + timedelta(hours=2), source="SCSS")
        self.assertEqual((high.reading_quality_status, float(high.estimated_liters)), ("CM_CONVERTI_CUVE_PROPOSEE_RESERVE", 400.0))
        self.assertEqual((high.estimation_detail["statut_cuve"], high.estimation_detail["score_compatibilite"]), ("PROPOSEE", 85))
        t.refresh_from_db()
        self.assertEqual(t.validation_status, "PROPOSEE")  # jamais validée automatiquement
        with self.settings(FUEL_PROPOSED_TANK_MIN_SCORE=90):
            self.assertIsNone(self.obs_cm(40, self.start + timedelta(hours=3), source="ENOC").estimated_liters)

    # 8. absence de cuve : aucun litre inventé
    def test_no_tank_no_invented_liters(self):
        o = self.obs_cm(50, self.start + timedelta(hours=1))
        self.assertEqual((o.reading_quality_status, o.level_liters, o.estimated_liters, o.confidence), ("CM_SANS_CUVE", None, None, None))
        self.obs(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        self.assertIsNone(r["stock_initial_l"])
        self.assertEqual((r["statut_bilan"], r["cause_principale"]), ("NON_CALCULABLE", "CM_SANS_CUVE"))
        cand = r["candidats"]["initial"][0]
        self.assertEqual((cand["utilisable"], cand["niveau_cm"], cand["litres"]), (False, 50.0, None))
        self.assertIn("CONVERSION_CUVE_MANQUANTE", cand["motif_rejet"])
        self.assertTrue(any("CONVERSION_CUVE_MANQUANTE" in a for a in r["alertes"]))

    # 9. fraîcheur : 0-3 j normal, 4-14 j réserve, > 14 j non calculable, jamais de relevé postérieur silencieux
    def test_stock_freshness_thresholds(self):
        self.obs(500, self.start - timedelta(days=2))   # initial antérieur de 2 j : frais
        self.obs(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        self.assertEqual((r["age_stock_initial_j"], r["stock_initial_detail"]["tier"], r["statut_bilan"]), (2, "FRAIS", "PRET_A_CERTIFIER"))
        self.assertEqual(r["reserves"], [])  # 0-3 j : information, pas une réserve
        self.assertTrue(any("exploitable normalement" in m for m in r["motifs"]))
        Obs.objects.all().delete()
        self.obs(500, self.start - timedelta(days=10))  # 4-14 j : estimation avec réserve
        self.obs(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        self.assertEqual((r["age_stock_initial_j"], r["stock_initial_detail"]["tier"], r["statut_bilan"]), (10, "RESERVE", "ESTIMATION_AVEC_RESERVE"))
        self.assertTrue(any("ancien de 10 j" in x for x in r["reserves"]))
        self.assertEqual(float(r["consommation_reelle_l"]), 200.0)
        Obs.objects.all().delete()
        self.obs(500, self.start - timedelta(days=20))  # > 14 j : non calculable
        self.obs(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        self.assertEqual((r["statut_bilan"], r["cause_principale"], r["consommation_reelle_l"]), ("NON_CALCULABLE", "STOCK_TROP_ANCIEN", None))
        self.assertIn("TROP_ANCIEN", [x["tier"] for x in r["candidats"]["initial"]])
        Obs.objects.all().delete()
        self.obs(500, self.start + timedelta(days=3))  # lecture postérieure au début : jamais utilisée silencieusement
        self.obs(300, self.end - timedelta(hours=1))
        r = self.compute()["DKR_0001"]
        post = [x for x in r["candidats"]["initial"] if x["tier"] == "POSTERIEUR"]
        self.assertEqual(r["statut_bilan"], "NON_CALCULABLE")
        self.assertTrue(post and "postérieur" in post[0]["motif_rejet"])

    def test_final_stock_prior_reading_and_movements_after_it_are_flagged(self):
        self.obs(500, self.start + timedelta(hours=1))
        self.obs(300, self.end - timedelta(days=2))  # dernier relevé antérieur dans la tolérance
        self.tc("TC-A1", 150, self.end - timedelta(hours=10))  # entre le relevé final et la fin de période
        r = self.compute()["DKR_0001"]
        self.assertEqual((r["age_stock_final_j"], float(r["carte_total_l"]), float(r["consommation_reelle_l"])), (2, 0.0, 200.0))
        self.assertTrue(any("entre le relevé final" in a for a in r["alertes"]))
        self.assertEqual(r["statut_bilan"], "ESTIMATION_AVEC_RESERVE")

    # 10. double comptage Closing / ENOC évité
    def test_closing_enoc_double_counting_avoided(self):
        self.stocks(500, 300)
        at = self.base + timedelta(days=10)
        self.tc("TC-D1", 400, at)
        FuelMovement.objects.create(source_type="CLOSING", source_event_id="CLOSING-1", site_id="DKR_0001", movement_type="DELIVERY_TOTAL_CARD",
                                    movement_datetime=at + timedelta(hours=3), quantity_l=Decimal("401"), status="VALIDATED", retained_in_stock=True)
        FuelMovement.objects.create(source_type="CLOSING", source_event_id="CLOSING-2", site_id="DKR_0001", movement_type="DELIVERY_TOTAL_CARD",
                                    movement_datetime=at + timedelta(days=3), quantity_l=Decimal("100"), status="VALIDATED", retained_in_stock=True)
        r = self.compute()["DKR_0001"]
        self.assertEqual(float(r["carte_total_l"]), 500.0)  # 400 (ENOC) + 100 (autre livraison) ; le doublon de 401 L est écarté
        dup = [m for m in r["mouvements_exclus"] if "DOUBLON_POTENTIEL" in m["motif"]]
        self.assertEqual((len(dup), dup[0]["source"]), (1, "CLOSING"))

    # 11. Ponction équilibrée : impact site réel, impact pays nul, déséquilibre bloquant
    def test_ponction_balanced_site_impact_country_zero_and_unbalanced_blocks(self):
        self.stocks(500, 300)
        self.stocks(800, 600, site="DKR_0002")
        action = {"validated_at": (self.base + timedelta(days=5)).isoformat(), "validated_by": "FM"}
        m = self.mk("PO-B1", typ="PONCTION", status="validated", operation=False,
                    req_extra={"approved_quantity_liters": 100, "action": action, "ponction": {"source_site_id": "DKR_0002"}})
        E.sync_enoc_to_registry(only_ids=[m.pk])
        types = {mv.movement_direction: mv.movement_type for mv in FuelMovement.objects.filter(transfer_id="ENOC-PONCTION-PO-B1")}
        self.assertEqual(types, {"OUT": "TRANSFERT_INTERSITE_SORTANT", "IN": "TRANSFERT_INTERSITE_ENTRANT"})
        self.assertEqual(E.check_transfer_balance(), [])
        rows = self.compute(("DKR_0001", "DKR_0002"))
        dst, src = rows["DKR_0001"], rows["DKR_0002"]
        self.assertEqual((float(dst["transferts_entrants_l"]), float(src["transferts_sortants_l"])), (100.0, 100.0))
        self.assertEqual((float(dst["consommation_reelle_l"]), float(src["consommation_reelle_l"])), (300.0, 100.0))
        self.assertEqual((float(dst["impact_ponctions_l"]), float(src["impact_ponctions_l"])), (100.0, -100.0))
        self.assertEqual(sum(float(r["impact_ponctions_l"]) for r in rows.values()), 0.0)  # impact pays nul
        g = SB.ponctions_globales(self.start, self.end)
        self.assertEqual((g["entrantes_l"], g["sortantes_l"], g["impact_global_l"]), (100.0, 100.0, 0.0))  # indépendant des fenêtres de stock
        self.assertEqual((float(dst["entrees_externes_nettes_l"]), float(dst["volume_transfere_l"])), (0.0, 0.0))  # pas un achat
        # écriture déséquilibrée : bilan non calculable
        FuelMovement.objects.filter(transfer_id="ENOC-PONCTION-PO-B1", movement_direction="IN").update(impact_stock_l=Decimal("90"))
        r = self.compute(("DKR_0001",))["DKR_0001"]
        self.assertEqual((r["statut_bilan"], r["cause_principale"], r["consommation_reelle_l"]), ("NON_CALCULABLE", "TRANSFERT_DESEQUILIBRE", None))

    # 12. plusieurs défauts de qualité visibles simultanément
    def test_quality_indicators_are_not_exclusive(self):
        m = self.mk("TC-Q1", photos=(), req_extra={"level_before": 900, "level_before_unit": "L", "hour_meter_before": None},
                    op_extra={"level_after": 500, "level_after_unit": "L", "hour_meter_after": None})
        q = Q.objects.get(enoc_movement=self.q(m).enoc_movement)
        self.assertEqual((q.has_levels_before_after, q.has_proof, q.has_hourmeter, q.has_date, q.has_actual_quantity), (True, False, False, True, True))
        self.assertEqual((q.level_coherence_status, q.quality_status_global, q.stock_eligibility), ("ECART", "DEFAUTS_MAJEURS", "ELIGIBLE"))
        self.assertEqual((q.source_record_id, q.has_site_source, q.has_site_destination), ("TC-Q1", None, True))
        ok = self.q(self.mk("TC-Q2", req_extra={"level_before": 100, "level_before_unit": "L"}, op_extra={"level_after": 300, "level_after_unit": "L"}))
        self.assertEqual((ok.quality_status_global, ok.level_coherence_status, ok.has_hourmeter), ("COMPLET", "COHERENT", True))
        bad = self.q(self.mk("TC-Q3", qty=None))
        self.assertEqual((bad.stock_eligibility, bad.quality_status_global, bad.has_actual_quantity), ("NON_ELIGIBLE", "NON_INTEGRABLE", False))

    # 13. mouvement historique non vérifiable exclu
    def test_historical_movement_excluded_with_reserve(self):
        self.stocks(500, 300)
        item = {"source_id": "REST-B1", "request_code": "REST-B1", "status": "done", "site_id": "DKR_0001", "operation_type": "TOTAL_CARD",
                "operation_date": (self.base + timedelta(days=8)).isoformat(), "quantity_added_liters": 999}
        C.sync_qualified([FuelEnocMovement.objects.create(**clean_payload(item))])
        r = self.compute()["DKR_0001"]
        self.assertEqual(float(r["consommation_reelle_l"]), 200.0)  # les 999 L historiques ne comptent pas
        self.assertEqual(r["statut_bilan"], "ESTIMATION_AVEC_RESERVE")
        self.assertTrue(any("historique" in x for x in r["reserves"]))
        self.assertIn("Historique non vérifiable", r["mouvements_exclus"][0]["motif"])

    # 14. période personnalisée
    def test_custom_period_only_counts_movements_inside(self):
        self.stocks(500, 300)
        self.tc("TC-P1", 111, self.base + timedelta(hours=12))
        self.tc("TC-P2", 400, self.base + timedelta(days=12))
        self.tc("TC-P3", 222, self.base + timedelta(days=20, hours=12))
        r = self.compute()["DKR_0001"]
        self.assertEqual((float(r["carte_total_l"]), [m["reference"] for m in r["mouvements"]]), (400.0, ["TC-P2"]))
        # semaine libre : 10 → 17, relevés exacts aux deux bornes
        s2, e2 = self.base + timedelta(days=10), self.base + timedelta(days=16, hours=23, minutes=59)
        self.obs(900, s2 + timedelta(hours=1), source="SCSS")
        self.obs(850, e2 - timedelta(hours=1), source="SCSS")
        w = self.compute(start=s2, end=e2)["DKR_0001"]
        self.assertEqual((w["observation_start"], w["observation_end"], float(w["consommation_reelle_l"]), w["source_stock_initial"]), (s2, e2, 450.0, "SCSS"))
        self.assertEqual(FuelStockBalance.objects.filter(site_id="DKR_0001").count(), 2)  # une ligne par période exacte

    # 15. recalcul après modification d'une donnée
    def test_recompute_after_data_change_requires_recertification(self):
        fm = get_user_model().objects.create_user("fm", "fm@x.sn", "p", role="manager")
        self.stocks(500, 300)
        self.compute()
        b = FuelStockBalance.objects.get(site_id="DKR_0001")
        self.assertEqual(b.statut_bilan, "PRET_A_CERTIFIER")
        with self.assertRaises(SB.BalanceError):
            SB.certify(b, fm, "certifier_avec_reserve", "")
        cert = SB.certify(b, fm, "certifier", "")
        self.assertEqual((cert.user_id, cert.action, cert.snapshot["consommation_reelle_l"]), (fm.pk, "CERTIFIE", "200.000"))
        cert.comment = "x"
        with self.assertRaises(ValueError):
            cert.save()  # historique non modifiable
        Obs.objects.filter(level_liters=300).update(level_liters=250, declared_liters=250)
        r = self.compute()["DKR_0001"]
        b.refresh_from_db()
        self.assertEqual((float(r["consommation_reelle_l"]), b.niveau_certification, b.certifications.count()), (250.0, "A_RECERTIFIER", 1))

    def test_identical_stocks_without_movement_flagged(self):
        self.stocks(400, 400)
        r = self.compute()["DKR_0001"]
        self.assertEqual(float(r["consommation_reelle_l"]), 0.0)
        self.assertEqual(r["statut_bilan"], "ESTIMATION_AVEC_RESERVE")
        self.assertTrue(any("identiques" in a for a in r["alertes"]))
        self.assertLess(r["score_confiance"], 100)

    def test_site_not_recognised(self):
        self.stocks(500, 300, site="GHOST_1")
        r = self.compute(("GHOST_1",))["GHOST_1"]
        self.assertEqual((r["statut_bilan"], r["cause_principale"], r["consommation_reelle_l"]), ("NON_CALCULABLE", "SITE_NON_RECONNU", None))

    def test_reconciliation_only_when_stock_consumption_computed(self):
        self.stocks(500, 300)
        self.stocks(800, None, site="DKR_0002")
        self.compute(("DKR_0001", "DKR_0002"))
        cph = [{"site_id": "DKR_0001", "conso_mesuree_l": Decimal("150"), "conso_theorique_l": Decimal("250")},
               {"site_id": "DKR_0002", "conso_mesuree_l": Decimal("90"), "conso_theorique_l": None}]
        with mock.patch("fuel_tracking.services.cph_service.compute_period_summary", return_value=cph):
            rows = {r["site_id"]: r for r in SB.reconciliation(self.start, self.end)}
        a, b = rows["DKR_0001"], rows["DKR_0002"]
        with mock.patch("fuel_tracking.services.cph_service.compute_period_summary",
                        return_value=[{"site_id": "DKR_0001", "conso_mesuree_l": Decimal("0.5"), "conso_theorique_l": Decimal("0.8")}]):
            tiny = {r["site_id"]: r for r in SB.reconciliation(self.start, self.end)}["DKR_0001"]
        self.assertEqual((tiny["ecart_reel_theorique_l"], tiny["ecart_reel_theorique_pct"], tiny["ecart_reel_instrumentee_pct"]), (199.2, None, None))
        self.assertIn("quasi nulle", tiny["motif"])
        self.assertEqual((a["conso_reelle_l"], a["ecart_reel_instrumentee_l"], a["ecart_reel_theorique_l"], a["score_confiance"]), (200.0, 50.0, -50.0, 100))
        self.assertEqual((b["conso_reelle_l"], b["conso_instrumentee_l"], b["ecart_reel_instrumentee_l"]), (None, 90.0, None))
        self.assertIn("aucun écart", b["motif"])

    def test_api_list_volumes_priorities_readings_tanks(self):
        U = get_user_model()
        c = APIClient()
        c.force_authenticate(U.objects.create_user("adm", "adm@x.sn", "p", role="admin"))
        tank("DKR_0001", plate=None)
        self.obs_cm(60, self.start + timedelta(hours=1))
        self.obs(300, self.end - timedelta(hours=1))
        self.stocks(800, None, site="DKR_0002")
        debut, fin = self.start.strftime("%Y-%m-%d"), self.end.strftime("%Y-%m-%d")
        r = c.post("/api/fuel-tracking/stock/bilans/calculer/", {"debut": debut, "fin": fin}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        j = c.get(f"/api/fuel-tracking/stock/bilans/?debut={debut}&fin={fin}").json()
        self.assertEqual(j["totaux"]["par_statut"], {"ESTIMATION_AVEC_RESERVE": 1, "NON_CALCULABLE": 1})
        self.assertEqual(j["volumes"]["impact_global_ponctions_l"], 0.0)
        self.assertIn("transferts internes", j["volumes"]["note"])
        self.assertEqual(c.get(f"/api/fuel-tracking/stock/bilans/?debut={debut}&fin={fin}&estimation=1").json()["pagination"]["total"], 1)
        d = c.get(f"/api/fuel-tracking/stock/bilans/{j['data'][0]['id']}/").json()
        self.assertEqual(len(d["score_detail"]), 6)
        self.assertIn("quantite_indicative_l", d["proposition"])
        pr = c.get(f"/api/fuel-tracking/stock/priorites/?debut={debut}&fin={fin}&avec_ecarts=0").json()
        cats = {i["categorie"] for i in pr["data"]}
        self.assertEqual(cats, {"forte_conso_estimee_sans_cuve_validee", "stock_trop_ancien"})
        self.assertEqual(pr["data"][0]["rang"], 1)
        rd = c.get("/api/fuel-tracking/stock/releves/?estimes=1").json()
        self.assertEqual((rd["pagination"]["total"], rd["data"][0]["litres_estimes"], rd["data"][0]["niveau_cm"], rd["data"][0]["niveau_brut"]),
                         (1, 600.0, 60.0, "60 cm"))
        tk = c.get("/api/fuel-tracking/stock/cuves-a-traiter/").json()
        self.assertEqual((tk["data"][0]["usage"], tk["data"][0]["categories"], tk["data"][0]["releves_estimes"]),
                         ("ESTIMATION_GEOMETRIQUE", ["VALIDEE", "SANS_PLAQUE"], 1))


class MvpImportTests(TmpRootMixin, BalanceBase):
    def setUp(self):
        super().setUp()
        Site.objects.create(site_id="DKR_0003", name="GAMMA")

    def file(self, rows, name="g.xlsx", source="LOLIFORCE"):
        return P.process_bytes(xlsx({"KOLDA": [["SEMAINE 40"], [], HEAD] + rows}), name, source, origin="UPLOAD")

    # import idempotent + lot tracé + rapport
    def test_import_idempotent_batch_traced_and_report(self):
        U = get_user_model()
        admin = U.objects.create_user("imp", "imp@x.sn", "p", role="admin")
        rows = [guard_row("DKR_0001", cm=10, d=datetime(2026, 10, 3)), guard_row("DKR_0001", cm=12, d=datetime(2026, 10, 9)),
                guard_row("XXX_9999", cm=5)]
        data = xlsx({"KOLDA": [["SEMAINE 40"], [], HEAD] + rows})
        b1 = P.process_bytes(data, "g.xlsx", "LOLIFORCE", origin="UPLOAD", user=admin)
        self.assertEqual((b1.rows_total, b1.rows_rejected, b1.received_by_id, b1.mapping_version), (3, 1, admin.pk, "gardiennage-v1"))
        self.assertEqual((str(b1.declared_period_start), str(b1.declared_period_end)), ("2026-10-03", "2026-10-09"))
        self.assertEqual(len(b1.file_hash), 64)
        n = Obs.objects.count()
        again = P.process_bytes(data, "g.xlsx", "LOLIFORCE", origin="UPLOAD", user=admin)
        self.assertEqual((again.status, again.duplicate_of_id, Obs.objects.count()), ("REJECTED", b1.pk, n))  # aucun doublon créé
        c = APIClient()
        c.force_authenticate(admin)
        rep = c.get(f"/api/fuel-tracking/stock/imports/{b1.pk}/rapport/")
        self.assertEqual(rep.status_code, 200)
        text = rep.content.decode("utf-8")
        self.assertIn(b1.file_hash, text)
        self.assertIn("XXX_9999", text)
        self.assertIn("gardiennage-v1", text)

    # site non reconnu dans un fichier : ligne rejetée, visible dans les relevés rejetés
    def test_unknown_site_row_rejected_and_listed(self):
        U = get_user_model()
        c = APIClient()
        c.force_authenticate(U.objects.create_user("ana", "ana@x.sn", "p", role="analyst"))
        b = self.file([guard_row("XXX_9999", cm=5)])
        self.assertEqual((b.rows_rejected, Obs.objects.count()), (1, 0))
        r = c.get("/api/fuel-tracking/stock/releves/?vue=rejetes").json()
        self.assertEqual((r["pagination"]["total"], r["data"][0]["qualite"], r["data"][0]["site_brut"]), (1, "SITE_NON_RECONNU", "XXX_9999"))

    # analyse préalable : feuilles / colonnes / mapping, aucune écriture
    def test_file_analysis_is_read_only_and_documents_mapping(self):
        from fuel_tracking.services.stock_file_analysis import analyze

        data = xlsx({"KOLDA": [["SEMAINE 40"], [], HEAD, guard_row("DKR_0001", cm=10), guard_row("XXX_9999", cm=5)], "Compile": [HEAD]})
        before = (FuelImportBatch.objects.count(), Obs.objects.count())
        res = analyze(data, "loliforce.xlsx", "LOLIFORCE")
        self.assertEqual((FuelImportBatch.objects.count(), Obs.objects.count()), before)
        self.assertTrue(res["lecture_seule"])
        kolda = next(s for s in res["feuilles"] if s["feuille"] == "KOLDA")
        self.assertEqual((kolda["lignes_de_donnees"], kolda["colonnes_mappees"]["relevé en cm"], kolda["plage_dates"]),
                         (2, "cm", ["2026-10-03", "2026-10-03"]))
        self.assertIn("marque ge", kolda["colonnes_non_reconnues"])
        self.assertTrue(next(s for s in res["feuilles"] if s["feuille"] == "Compile")["ignoree"])
        self.assertEqual((res["source_detectee"], res["version_mapping"], res["couverture_sites"]["inconnus"]), ("LOLIFORCE", "gardiennage-v1", 1))

    def test_analysis_reports_units_sites_rejections_and_period(self):
        from fuel_tracking.services.stock_file_analysis import analyze

        rows = [guard_row("DKR_0001", cm=10, d=datetime(2026, 10, 3)), guard_row("DKR_0001", cm=None, l=300, d=datetime(2026, 10, 9)),
                guard_row("XXX_9999", cm=5, d=datetime(2026, 10, 4)), guard_row("DKR_0003", cm=None, d=datetime(2026, 10, 5))]
        res = analyze(xlsx({"KOLDA": [["SEMAINE 40"], [], HEAD] + rows}), "loliforce_oct.xlsx")  # source déduite du nom et des colonnes
        c = res["controle_lignes"]
        self.assertEqual((res["source_detectee"], res["libelle_source"]), ("LOLIFORCE", "Loliforce — relevés de stock terrain"))
        self.assertEqual((c["lignes_lues"], c["lignes_rejetees"], c["releves_en_cm"], c["releves_en_litres"]), (4, 2, 2, 1))
        self.assertEqual((c["sites_non_reconnus"], c["motifs_rejet"].get("UNKNOWN_SITE"), c["motifs_rejet"].get("MISSING_LEVEL")), (1, 1, 1))
        self.assertEqual((res["feuilles_retenues"], res["periode_detectee"]), (["KOLDA"], ["2026-10-03", "2026-10-09"]))
        self.assertEqual(c["unite_detectee"], "cm + litres")
        self.assertEqual(FuelImportBatch.objects.count(), 0)  # rien n'est importé

    def test_closing_stock_preview_reads_blocks_and_checks_coherence(self):
        from fuel_tracking.services.stock_file_analysis import analyze

        head = ["Id du site", "Date de relevé initiale", "Qtité laissée à la date du relevé", "Qtité totale livrée",
                "Qté rajoutée ou retirée de la cuve par les techniciens d' un site à l' autre", "Qté volée",
                "Qté vidangée (fuel de mauvaise qualité)", "Date de relevé finale", "Qtité laissée à la date du relevé",
                "Qtité cons considéréé (l)", "Test date", "Date de relevé", "Qtité laissée à la date du relevé"]
        d1, d2 = datetime(2026, 8, 1), datetime(2026, 8, 31)
        rows = [["Synthèse"], head,
                ["DKR_0001", d1, 500, 200, 0, 0, 0, d2, 300, 400, None, d2, 999],      # 500 + 200 − 300 = 400 : cohérent
                ["DKR_0002", d1, 800, 0, 0, 0, 0, d2, 600, 500, None, d2, 999],        # 200 calculés vs 500 : incohérent
                ["DKR_0003", d1, None, 0, 0, 0, 0, d2, 100, None, None, d2, 999]]      # stock initial absent : jamais 0
        res = analyze(xlsx({"Synthese conso fuel": rows}), "closing_aout.xlsx", "CLOSING")
        cs = res["closing_stocks"]
        self.assertEqual((cs["sites_lus"], cs["stock_initial_present"], cs["stock_final_present"], cs["sites_avec_les_deux_stocks_et_dates"]), (3, 2, 3, 2))
        self.assertEqual((cs["controle_coherence"]["sites_verifies"], cs["controle_coherence"]["formule_coherente"]), (2, 1))
        self.assertEqual((cs["dates_initiales"], cs["dates_finales"]), (["2026-08-01", "2026-08-01"], ["2026-08-31", "2026-08-31"]))
        self.assertEqual(cs["stock_final_l"]["max"], 600.0)  # le stock final est lu dans le bloc FINAL, pas la valeur de la colonne répétée (999)
        self.assertEqual(FuelImportBatch.objects.count(), 0)

    def closing_file(self, extra=()):
        head = ["Id du site", "Nombre de GE installé [ex: 1,2,3 …]", "Forme géométrique cuve", "Date de relevé initiale", "Qtité laissée à la date du relevé",
                "Qtité totale livrée", "Qté rajoutée ou retirée de la cuve par les techniciens d' un site à l' autre", "Qté volée",
                "Qté vidangée (fuel de mauvaise qualité)", "Date de relevé finale", "Qtité laissée à la date du relevé", "Qtité cons considéréé (l)", "Test date"]
        d1, d2 = datetime(2026, 8, 1), datetime(2026, 8, 31)
        rows = [["Synthèse"], head,
                ["DKR_0001", 1, None, d1, 500, 200, 0, 0, 0, d2, 300, 400, None],    # cohérent
                ["DKR_0002", 1, None, d1, 800, 0, 0, 0, 0, d2, 600, 500, None],      # formule incohérente → à valider
                ["DKR_0003", 0, None, d1, 0, 0, 0, 0, 0, d2, 0, 0, None],            # site sans GE, stock 0 : ni mesuré ni importé
                ["XXX_9999", 1, None, d1, 100, 0, 0, 0, 0, d2, 50, 50, None]]         # site inconnu
        return xlsx({"Synthese conso fuel": rows + [list(x) for x in extra]})

    def test_closing_stocks_import_idempotent_and_rules(self):
        b = P.process_bytes(self.closing_file(), "closing_aout.xlsx", "CLOSING", origin="UPLOAD")
        st = b.summary["stocks_closing"]
        self.assertEqual((st["stocks_importes"], st["sans_carburant_ignores"], st["sites_inconnus"], st["formule_incoherente_a_valider"]), (4, 2, 1, 2))
        obs = {(o.site_id, o.observation_datetime.date().isoformat()): o for o in Obs.objects.filter(source_type="CLOSING")}
        ok = obs[("DKR_0001", "2026-08-01")]
        self.assertEqual((float(ok.level_liters), ok.tank_id, ok.validation_status, ok.reading_quality_status), (500.0, "SITE", "VALIDATED", "LITRE_DIRECT"))
        bad = obs[("DKR_0002", "2026-08-31")]
        self.assertEqual(bad.validation_status, "PENDING")
        self.assertIn("CLOSING_FORMULE_INCOHERENTE", [f["code"] for f in bad.review_flags])
        self.assertFalse(Obs.objects.filter(site_id="DKR_0003", source_type="CLOSING").exists())  # pas de 0 inventé pour un site sans GE
        n = Obs.objects.filter(source_type="CLOSING").count()
        again = P.relaunch(b, "test idempotence")
        self.assertEqual(Obs.objects.filter(source_type="CLOSING").count(), n)
        self.assertEqual(again.summary["stocks_closing"]["doublons"], 4)

    def test_closing_zero_final_stock_imported_when_site_had_fuel(self):
        d1, d2 = datetime(2026, 8, 1), datetime(2026, 8, 31)
        # DKR_0003 : pas de GE déclaré mais 300 L au départ : le stock final de 0 L (cuve vide) est une vraie mesure et doit être importé
        P.process_bytes(self.closing_file(extra=[["DKR_0003", 0, None, d1, 300, 0, 0, 0, 0, d2, 0, 300, None]]), "closing_zero.xlsx", "CLOSING", origin="UPLOAD")
        got = {o.observation_datetime.date().isoformat(): float(o.level_liters) for o in Obs.objects.filter(source_type="CLOSING", site_id="DKR_0003")}
        self.assertEqual(got, {"2026-08-01": 300.0, "2026-08-31": 0.0})

    def test_priorities_list_missing_closing_deliveries_and_table_actions(self):
        P.process_bytes(self.closing_file(), "closing_aout.xlsx", "CLOSING", origin="UPLOAD")
        start, end = datetime(2026, 8, 1, tzinfo=dt_tz.utc), datetime(2026, 8, 31, 23, 59, 59, tzinfo=dt_tz.utc)
        SB.compute_balances(start, end, site_ids=["DKR_0001"])  # persiste : livraison du Closing absente d'ENOC (complétée avec réserve)
        U = get_user_model()
        c = APIClient()
        c.force_authenticate(U.objects.create_user("pr", "pr@x.sn", "p", role="admin"))
        pr = c.get("/api/fuel-tracking/stock/priorites/?debut=2026-08-01&fin=2026-08-31&avec_ecarts=0").json()
        item = next(i for i in pr["data"] if i["categorie"] == "livraison_closing_absente_enoc")
        self.assertEqual((item["site_id"], item["impact_l"]), ("DKR_0001", 200.0))
        out = StringIO()
        call_command("fuel_validation_sites", debut="2026-08-01", fin="2026-08-31", stdout=out)
        self.assertIn("Compléter dans ENOC", out.getvalue())
        out2 = StringIO()
        call_command("fuel_priorites", debut="2026-08-01", fin="2026-08-31", stdout=out2)
        self.assertIn("Livraison du Closing absente d'ENOC", out2.getvalue())
        self.assertIn("Cuves à traiter", out2.getvalue())
        self.assertNotIn("Certification manuelle", out.getvalue().split("DKR_0001")[1].split("\n")[0])

    # ── Closing / ENOC : complément avec réserve, conflit tracé, période validée ─────────────────────────────────
    def aug(self):
        return datetime(2026, 8, 1, tzinfo=dt_tz.utc), datetime(2026, 8, 31, 23, 59, 59, tzinfo=dt_tz.utc)

    def test_closing_delivery_missing_from_enoc_completes_with_reserve(self):
        b = P.process_bytes(self.closing_file(), "closing_aout.xlsx", "CLOSING", origin="UPLOAD")
        start, end = self.aug()
        r = SB.compute_balances(start, end, site_ids=["DKR_0001"])[0]  # le Closing compte 200 L livrés, ENOC aucun
        self.assertEqual((float(r["consommation_reelle_l"]), r["statut_bilan"]), (400.0, "ESTIMATION_AVEC_RESERVE"))
        self.assertTrue(any(a.startswith("LIVRAISON_CLOSING_ABSENTE_ENOC") and f"lot #{b.pk}" in a and "closing_aout.xlsx" in a for a in r["alertes"]))
        add = next(m for m in r["mouvements"] if m["source"] == "CLOSING")
        self.assertEqual((add["quantite_l"], add["lot"], add["fichier"], add["composante"]), (200.0, b.pk, "closing_aout.xlsx", "livraisons_externes_l"))
        self.assertEqual(r["stock_initial_detail"]["reference_closing"]["decision"], "COMPLEMENT_CLOSING")
        bal = FuelStockBalance.objects.get(site_id="DKR_0001", observation_start=start)
        with self.assertRaises(SB.BalanceError):
            SB.certify(bal, None, "certifier", "")  # jamais certifiable automatiquement sur cette base
        SB.certify(bal, None, "certifier_avec_reserve", "Livraison confirmée par téléphone")

    def test_closing_and_enoc_agree_is_ready(self):
        P.process_bytes(self.closing_file(), "closing_aout.xlsx", "CLOSING", origin="UPLOAD")
        self.tc("TC-CL1", 200, datetime(2026, 8, 15, tzinfo=dt_tz.utc), typ="TRUCK",
                req_extra={"level_before": 100, "level_before_unit": "L"}, op_extra={"level_after": 300, "level_after_unit": "L"})
        r = SB.compute_balances(*self.aug(), site_ids=["DKR_0001"], persist=False)[0]
        self.assertEqual((float(r["consommation_reelle_l"]), r["statut_bilan"]), (400.0, "PRET_A_CERTIFIER"))
        self.assertEqual(r["stock_initial_detail"]["reference_closing"]["decision"], "CONCORDANT")

    def test_closing_enoc_conflict_keeps_both_values(self):
        P.process_bytes(self.closing_file(), "closing_aout.xlsx", "CLOSING", origin="UPLOAD")
        for ref, qty, day in (("TC-CL1", 200, 15), ("TC-CL2", 100, 20)):  # ENOC : 300 L ; Closing : 200 L → conflit de 100 L (< 300 L)
            self.tc(ref, qty, datetime(2026, 8, day, tzinfo=dt_tz.utc), typ="TRUCK",
                    req_extra={"level_before": 100, "level_before_unit": "L"}, op_extra={"level_after": 300, "level_after_unit": "L"})
        r = SB.compute_balances(*self.aug(), site_ids=["DKR_0001"], persist=False)[0]
        self.assertEqual((float(r["consommation_reelle_l"]), r["statut_bilan"]), (500.0, "ESTIMATION_AVEC_RESERVE"))  # valeurs ENOC conservées
        ref = r["stock_initial_detail"]["reference_closing"]
        self.assertEqual((ref["decision"], ref["entrees_egrid_l"], ref["entrees_closing_l"]), ("CONFLIT", 300.0, 200.0))
        self.assertTrue(any(a.startswith("CONFLIT_ENOC_CLOSING") for a in r["alertes"]))
        # grand conflit (> 300 L) : à contrôler, aucune consommation
        self.tc("TC-CL3", 500, datetime(2026, 8, 25, tzinfo=dt_tz.utc), typ="TRUCK",
                req_extra={"level_before": 100, "level_before_unit": "L"}, op_extra={"level_after": 600, "level_after_unit": "L"})
        r = SB.compute_balances(*self.aug(), site_ids=["DKR_0001"], persist=False)[0]
        self.assertEqual((r["statut_bilan"], r["cause_principale"], r["consommation_reelle_l"]), ("NON_CALCULABLE", "CONFLIT_ENOC_CLOSING", None))

    def test_closing_authoritative_only_for_listed_validated_periods(self):
        P.process_bytes(self.closing_file(), "closing_aout.xlsx", "CLOSING", origin="UPLOAD")
        self.tc("TC-CL3", 900, datetime(2026, 8, 25, tzinfo=dt_tz.utc), typ="TRUCK",
                req_extra={"level_before": 100, "level_before_unit": "L"}, op_extra={"level_after": 600, "level_after_unit": "L"})
        with override_settings(FUEL_CLOSING_AUTHORITATIVE_PERIODS=["2026-07"]):  # autre mois : aucune autorité
            r = SB.compute_balances(*self.aug(), site_ids=["DKR_0001"], persist=False)[0]
        self.assertEqual(r["cause_principale"], "CONFLIT_ENOC_CLOSING")
        with override_settings(FUEL_CLOSING_AUTHORITATIVE_PERIODS=["2026-08"]):  # période au Closing validé
            r = SB.compute_balances(*self.aug(), site_ids=["DKR_0001"], persist=False)[0]
        self.assertEqual((float(r["consommation_reelle_l"]), r["statut_bilan"]), (400.0, "ESTIMATION_AVEC_RESERVE"))
        self.assertEqual(r["stock_initial_detail"]["reference_closing"]["decision"], "CLOSING_FAIT_FOI")
        self.assertTrue(any(m["reference"] == "TC-CL3" and "conservée pour audit" in m["motif"] for m in r["mouvements_exclus"]))  # ENOC conservé
        self.assertTrue(any(a.startswith("CONFLIT_ENOC_CLOSING") for a in r["alertes"]))

    def test_effective_parameters_and_tank_usage_follow_the_flag(self):
        from fuel_tracking.views_mvp import tank_class

        tank("DKR_0001", plate=None)
        spec = TC.TankSpec.from_model(FuelTankMaster.objects.get(site_id="DKR_0001"))
        self.assertEqual(tank_class(spec, 70)["usage"], "ESTIMATION_GEOMETRIQUE")  # BalanceBase active le paramètre
        with override_settings(ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE=False):
            self.assertEqual(tank_class(spec, 70)["usage"], "ESTIMATION_GEOMETRIQUE_DESACTIVEE")
            self.assertFalse(SB.effective_parameters()["ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE"])
        out = StringIO()
        call_command("fuel_parametres", stdout=out)
        self.assertIn('"ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE": true', out.getvalue())

    def test_downloadable_reports(self):
        b = P.process_bytes(self.closing_file(extra=[["XXX_8888", 1, None, datetime(2026, 8, 1), 10, 0, 0, 0, 0, datetime(2026, 8, 31), 5, 5, None]]),
                            "closing_aout.xlsx", "CLOSING", origin="UPLOAD")
        start, end = self.aug()
        SB.compute_balances(start, end, site_ids=["DKR_0001", "DKR_0002"])
        self.tc("TC-CL9", 900, datetime(2026, 8, 25, tzinfo=dt_tz.utc), typ="TRUCK",
                req_extra={"level_before": 100, "level_before_unit": "L"}, op_extra={"level_after": 600, "level_after_unit": "L"})
        SB.compute_balances(start, end, site_ids=["DKR_0001"])  # conflit de 900 L
        tank("DKR_0002", plate=None)
        U = get_user_model()
        c = APIClient()
        c.force_authenticate(U.objects.create_user("rep", "rep@x.sn", "p", role="analyst"))
        listing = c.get("/api/fuel-tracking/stock/rapports/").json()["data"]
        self.assertEqual({r["kind"] for r in listing}, {"lignes_rejetees", "validation_10_sites", "livraisons_closing_absentes", "ponctions_sans_source",
                                                        "conflits_enoc_closing", "cuves_prioritaires", "closing_formule_incoherente"})
        qs = "?debut=2026-08-01&fin=2026-08-31"

        def get(kind, extra=""):
            r = c.get(f"/api/fuel-tracking/stock/rapports/{kind}/{qs}{extra}")
            self.assertEqual(r.status_code, 200, (kind, r.content))
            self.assertIn("attachment", r["Content-Disposition"])
            text = r.content.decode("utf-8-sig")
            return text.splitlines()[0].split(";"), [l.split(";") for l in text.splitlines()[1:]]

        j = c.get("/api/fuel-tracking/stock/bilans/?debut=2026-08-01&fin=2026-08-31&alerte=CONFLIT_ENOC_CLOSING").json()
        self.assertEqual([r["site_id"] for r in j["data"]], ["DKR_0001"])
        self.assertEqual(j["par_alerte"].get("CONFLIT_ENOC_CLOSING"), 1)
        self.assertIn("ALLOW_GEOMETRIC_TANK_ESTIMATION_WITH_RESERVE", j["parametres"])
        head, rows = get("lignes_rejetees")
        self.assertTrue(any("XXX_8888" in r for r in rows) and "motifs" in head)
        head, rows = get("conflits_enoc_closing")
        self.assertTrue(any(r[0] == "DKR_0001" for r in rows))
        head, rows = get("closing_formule_incoherente")
        row = next(r for r in rows if r[0] == "DKR_0002")
        self.assertEqual((row[head.index("conso_closing_l")], row[head.index("conso_calculee_l")], row[head.index("lot")]), ("500.0", "200.0", str(b.pk)))
        head, rows = get("cuves_prioritaires")
        self.assertTrue(any(r[0] == "" or r[0] == "oui" for r in rows))
        head, rows = get("validation_10_sites")
        self.assertIn("action_attendue", head)
        head, rows = get("livraisons_closing_absentes")
        self.assertEqual(head[0], "site_id")
        get("ponctions_sans_source")
        r = c.get("/api/fuel-tracking/stock/rapports/validation_10_sites/")  # sans période
        self.assertEqual(r.status_code, 400)
        self.assertEqual(c.get("/api/fuel-tracking/stock/rapports/inconnu/").status_code, 400)

    def test_compare_closing_balances_end_to_end(self):
        from tempfile import NamedTemporaryFile

        b = P.process_bytes(self.closing_file(), "closing_aout.xlsx", "CLOSING", origin="UPLOAD")
        self.assertEqual(b.source_type, "CLOSING")
        with NamedTemporaryFile(suffix=".xlsx") as f:
            f.write(self.closing_file())
            f.flush()
            out = StringIO()
            call_command("compare_closing_balances", fichier=f.name, stdout=out)
        res = json.loads(out.getvalue().split("Lecture seule")[0])
        self.assertEqual(res["sites_compares"], 2)  # DKR_0001 et DKR_0002 (le site sans GE et le site inconnu sont écartés)
        self.assertIn("couverture_des_entrees", res)

    def test_guard_analysis_ignores_empty_template_rows_and_flags_odd_dates(self):
        from fuel_tracking.services.stock_file_analysis import analyze

        rows = [guard_row("DKR_0001", cm=10, d=datetime(2026, 10, 3)), guard_row("DKR_0001", cm=None, l=None, d=None),
                guard_row("DKR_0001", cm=12, d=datetime(2016, 10, 1)), guard_row("DKR_0001", cm=14, d=datetime(2027, 6, 14))]
        res = analyze(xlsx({"KOLDA": [["SEMAINE 40"], [], HEAD] + rows}), "loli.xlsx", "LOLIFORCE")
        c = res["controle_lignes"]
        self.assertEqual((c["lignes_modele_vides_ignorees"], c["lignes_lues"], c["lignes_rejetees"]), (1, 3, 2))
        self.assertEqual(c["periode_detectee"], ["2026-10-03", "2026-10-03"])
        self.assertEqual(c["dates_invraisemblables"], ["2016-10-01", "2027-06-14"])
        self.assertEqual(c["gardiennage_declare"], {"LOLIFORCE": 3})
        self.assertEqual(res["periode_detectee"], ["2026-10-03", "2026-10-03"])

    def test_import_isolates_impossible_dates_and_flags_misnamed_sheet(self):
        rows = [guard_row("DKR_0001", cm=10, d=datetime(2026, 10, 3)), guard_row("DKR_0001", cm=12, d=datetime(2026, 10, 4)),
                guard_row("DKR_0001", cm=14, d=datetime(2016, 10, 1)), guard_row("DKR_0001", cm=15, d=datetime(2027, 6, 14))]
        data = xlsx({"Remonte carburant 20 septembre ": [["SEMAINE 40"], [], HEAD] + rows})
        b = P.process_bytes(data, "REMONTE CARBURANT OCTOBRE 2026.xlsx", "SCSS", origin="UPLOAD")
        st = b.summary
        self.assertEqual(sorted(x["date"] for x in st["dates_impossibles"]), ["01/10/2016", "14/06/2027"])
        self.assertEqual(len(st["feuilles_suspectes"]), 1)
        self.assertIn("mois 09", st["feuilles_suspectes"][0]["message"])
        self.assertEqual(Obs.objects.filter(batch=b).count(), 2)  # les deux dates impossibles ne deviennent jamais des relevés
        from fuel_tracking.services.stock_file_analysis import analyze
        res = analyze(data, "REMONTE CARBURANT OCTOBRE 2026.xlsx", "SCSS")
        self.assertEqual(len(res["controle_lignes"]["feuilles_suspectes"]), 1)

    def test_validation_sites_table_covers_profiles_read_only(self):
        self.stocks(500, 300)
        self.tc("TR-V1", 200, self.base + timedelta(days=10), typ="TRUCK")
        self.compute(("DKR_0001",))
        out = StringIO()
        call_command("fuel_validation_sites", debut=self.start.strftime("%Y-%m-%d"), fin=self.end.strftime("%Y-%m-%d"), anonymiser=True, stdout=out)
        text = out.getvalue()
        self.assertIn("SITE_001", text)
        self.assertIn("stock_litre_direct", text)
        self.assertIn("lecture seule", text)

    # doublon potentiel entre sources
    def test_potential_duplicate_between_sources(self):
        self.obs(500, self.start + timedelta(hours=1), source="LOLIFORCE")
        dup = self.obs(500, self.start + timedelta(hours=2), source="SCSS")
        self.assertEqual(R.mark_potential_duplicates(["DKR_0001"]), 1)
        dup.refresh_from_db()
        self.assertIsNotNone(dup.potential_duplicate_of)
        U = get_user_model()
        c = APIClient()
        c.force_authenticate(U.objects.create_user("ana3", "ana3@x.sn", "p", role="analyst"))
        r = c.get("/api/fuel-tracking/stock/releves/?qualite=DOUBLON_POTENTIEL").json()
        self.assertEqual((r["pagination"]["total"], r["data"][0]["qualite"], r["data"][0]["qualite_releve"]), (1, "DOUBLON_POTENTIEL", "LITRE_DIRECT"))


class TankPlateDiagnosticTests(BalanceBase):
    def test_diagnostic_finds_likely_input_error_read_only(self):
        a = FuelTankMaster.objects.create(site_id="DKR_0001", tank_id="1", validation_status="VALIDEE", shape_family="h-circ",
                                          dimension_1_cm=157, dimension_2_cm=80, dimension_3_cm=268, capacity_plate_l=1350)
        b = FuelTankMaster.objects.create(site_id="DKR_0002", tank_id="1", validation_status="VALIDEE", shape_family="h-circ",
                                          dimension_1_cm=157, dimension_3_cm=268, capacity_plate_l=None, capacity_l=5188)
        for t in (a, b):
            self.obs(None, self.start, site=t.site_id, level_unit="CM", level_value=50, level_liters=None,
                     conversion_status="GEOMETRIC_ONLY", validation_status="PENDING")
        out = StringIO()
        call_command("diagnostic_cuves_plaque", stdout=out)
        text = out.getvalue()
        self.assertIn("Cuves validées à plaque incohérente (relevés bloqués) : 2", text)
        self.assertIn("→ DIAMETRE_EN_COTE_2", text)
        self.assertIn("→ PLAQUE_ABSENTE", text)
        a.refresh_from_db()
        self.assertEqual(float(a.dimension_1_cm), 157.0)  # lecture seule
