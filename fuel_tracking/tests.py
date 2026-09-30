from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

from django.test import SimpleTestCase

from fuel_tracking.services import cph_engine as E
from fuel_tracking.services.cph_service import resolve_curve

# Caterpillar DE22E3 (CPH-0014, VALIDÉ_CONSTRUCTEUR) : 20 kVA / 16 kW, a=3.2 b=0 c=2.1
DE22E3 = E.Curve(curve_id="CPH-0014", label="Caterpillar DE22E3", status="VALIDÉ_CONSTRUCTEUR",
                 prp_kva=D("20"), prp_kw=D("16"), power_factor=D("0.8"), a=D("3.2"), b=D("0"), c=D("2.1"))
# Rehlko R50C5 (CPH-0123) : c < 0, CPH négatif à faible charge
R50C5 = E.Curve(curve_id="CPH-0123", label="Rehlko R50C5", status="VALIDÉ_CONSTRUCTEUR",
                prp_kva=D("45"), prp_kw=D("36"), power_factor=D("0.8"), a=D("-6.4"), b=D("20"), c=D("-2.6"))

OCT1 = date(2026, 10, 1)


def outdoor(**kw):
    base = dict(site_id="S1", kind="OUTDOOR", off_grid=True, dg_count=1, curve=DE22E3)
    base.update(kw)
    return E.SiteContext(**base)


def fact(**kw):
    return dict(kw)


def days(start, n):
    return [start + timedelta(days=i) for i in range(n)]


def period(ctx, facts, start, end, observations=(), settings=None):
    return E.compute_site_period(ctx, facts, start, end, list(observations), settings or E.EngineSettings())


class RuntimeTests(SimpleTestCase):
    def test_dse_zero_is_zero_not_missing(self):
        facts = {d: fact(dse_runtime_h=0) for d in days(OCT1, 10)}
        r = period(outdoor(), facts, OCT1, OCT1 + timedelta(days=9))
        day = r["daily"][0]
        self.assertEqual(day["runtime_h"], D("0"))
        self.assertEqual(day["runtime_source"], E.RT_DSE)
        self.assertEqual(day["status"], E.DAY_GE_ARRET)
        self.assertEqual(day["conso_l"], D("0"))
        self.assertIsNone(day["cph_l_h"])
        self.assertEqual(r["runtime_total_h"], D("0"))

    def test_dse_priority_over_other_sources(self):
        facts = {d: fact(dse_runtime_h=5, dg_on_runtime_h=7, rectifier_slots=288, rectifier_active_slots=96,
                         tracker_runtime_h=6, tracker_covered_min=1440) for d in days(OCT1, 4)}
        r = period(outdoor(), facts, OCT1, OCT1 + timedelta(days=3))
        self.assertTrue(all(d["runtime_source"] == E.RT_DSE and d["runtime_h"] == D("5") for d in r["daily"]))

    def test_source_below_50pct_availability_is_not_used(self):
        # DSE présent 1 jour sur 4 (25 %) → rejeté pour toute la période, même ce jour-là.
        facts = {d: fact(tracker_runtime_h=3, tracker_covered_min=1440) for d in days(OCT1, 4)}
        facts[OCT1]["dse_runtime_h"] = 8
        r = period(outdoor(off_grid=False), facts, OCT1, OCT1 + timedelta(days=3))
        self.assertFalse(r["sources"][E.RT_DSE]["exploitable"])
        self.assertIn("25 %", r["sources"][E.RT_DSE]["rejection"])
        self.assertEqual(r["daily"][0]["runtime_source"], E.RT_COMPTEUR)
        self.assertEqual(r["daily"][0]["runtime_h"], D("3"))

    def test_availability_counts_non_null_days_without_coalesce(self):
        facts = {d: fact(dse_runtime_h=0) for d in days(OCT1, 2)}  # 2 jours sur 4 = 50 %
        r = period(outdoor(), facts, OCT1, OCT1 + timedelta(days=3))
        self.assertEqual(r["sources"][E.RT_DSE]["availability"], D("0.5"))
        self.assertTrue(r["sources"][E.RT_DSE]["exploitable"])
        self.assertEqual(r["daily"][2]["status"], E.DAY_RUNTIME_ABSENT)
        self.assertIsNone(r["daily"][2]["runtime_h"])

    def test_dse_out_of_bounds_rejected(self):
        facts = {d: fact(dse_runtime_h=1192095) for d in days(OCT1, 2)}
        r = period(outdoor(), facts, OCT1, OCT1 + timedelta(days=1))
        self.assertIsNone(r["daily"][0]["runtime_h"])
        self.assertEqual(r["cph_status"], E.CPH_NON_CALCULE_PERIODE)

    def test_rectifier_only_for_off_grid(self):
        facts = {d: fact(rectifier_slots=288, rectifier_active_slots=60) for d in days(OCT1, 2)}
        on_grid = period(outdoor(off_grid=False), facts, OCT1, OCT1 + timedelta(days=1))
        self.assertIsNone(on_grid["daily"][0]["runtime_h"])
        self.assertIn("non off-grid", on_grid["sources"][E.RT_REDRESSEUR]["rejection"])
        off_grid = period(outdoor(off_grid=True), facts, OCT1, OCT1 + timedelta(days=1))
        self.assertEqual(off_grid["daily"][0]["runtime_h"], D("60") * E.SLOT_H)
        self.assertEqual(off_grid["daily"][0]["runtime_source"], E.RT_REDRESSEUR)

    def test_day_dg_on_rejected_when_zero_coalesced(self):
        facts = {d: fact(dg_on_runtime_h=0, tracker_runtime_h=4, tracker_covered_min=1440) for d in days(OCT1, 3)}
        r = period(outdoor(off_grid=False), facts, OCT1, OCT1 + timedelta(days=2))
        self.assertFalse(r["sources"][E.RT_DAY_DG_ON]["exploitable"])
        self.assertIn("coalescence", r["sources"][E.RT_DAY_DG_ON]["rejection"])
        self.assertEqual(r["daily"][0]["runtime_source"], E.RT_COMPTEUR)

    def test_tracker_requires_continuous_coverage(self):
        facts = {d: fact(tracker_runtime_h=5, tracker_covered_min=600) for d in days(OCT1, 2)}
        r = period(outdoor(off_grid=False), facts, OCT1, OCT1 + timedelta(days=1))
        self.assertIsNone(r["daily"][0]["runtime_h"])

    def test_no_source_gives_null_runtime_and_no_cph(self):
        r = period(outdoor(), {}, OCT1, OCT1)
        day = r["daily"][0]
        self.assertIsNone(day["runtime_h"])
        self.assertIsNone(day["cph_l_h"])
        self.assertIsNone(day["conso_l"])
        self.assertEqual(day["status"], E.DAY_RUNTIME_ABSENT)


class PowerAndCphTests(SimpleTestCase):
    def test_outdoor_dc_power_does_not_add_battery(self):
        f = fact(dse_runtime_h=10, tracker_ge_on_slots=120, p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"),
                 )
        r = period(outdoor(), {OCT1: f}, OCT1, OCT1)
        day = r["daily"][0]
        self.assertEqual(day["power_source"], E.PW_DC_REDRESSEUR)
        self.assertEqual(day["p_ge_kw"], D("10"))  # 8 / 0.8, sans ajout batterie
        x = D("10") / D("16")
        self.assertEqual(day["cph_l_h"], D("3.2") * x * x + D("2.1"))
        self.assertEqual(day["conso_l"], D("10") * day["cph_l_h"])
        self.assertEqual(day["status"], E.DAY_CPH_CALCULE)

    def test_outdoor_production_ge_first_when_dse_runtime(self):
        f = fact(dse_runtime_h=4, dg_production_kwh=40, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=5, eff_ge_tracker=D("0.9"))
        day = period(outdoor(), {OCT1: f}, OCT1, OCT1)["daily"][0]
        self.assertEqual(day["power_source"], E.PW_PRODUCTION)
        self.assertEqual(day["p_ge_kw"], D("10"))

    def test_efficiency_must_be_in_0_1(self):
        f = fact(dse_runtime_h=4, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=5, eff_ge_tracker=D("85"))
        day = period(outdoor(), {OCT1: f}, OCT1, OCT1)["daily"][0]
        self.assertIsNone(day["p_ge_kw"])
        self.assertEqual(day["status"], E.DAY_PUISSANCE_ABSENTE)
        self.assertIsNone(day["cph_l_h"])

    def test_no_cph_without_validated_curve(self):
        f = fact(dse_runtime_h=4, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=5, eff_ge_tracker=D("0.9"))
        ctx = outdoor(curve=None, curve_reason="mappage non validé")
        day = period(ctx, {OCT1: f}, OCT1, OCT1)["daily"][0]
        self.assertEqual(day["status"], E.DAY_COURBE_ABSENTE)
        self.assertIsNone(day["cph_l_h"])
        self.assertIsNone(day["conso_l"])
        self.assertIn("mappage non validé", day["motifs"][0])

    def test_power_above_105pct_refused(self):
        f = fact(dse_runtime_h=4, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=D("17"), eff_ge_tracker=D("1"))
        day = period(outdoor(), {OCT1: f}, OCT1, OCT1)["daily"][0]  # 17 kW > 1.05 × 20 × 0.8 = 16.8
        self.assertEqual(day["status"], E.DAY_PUISSANCE_HORS_PLAFOND)
        self.assertIsNone(day["cph_l_h"])

    def test_low_load_extrapolation_is_flagged(self):
        f = fact(dse_runtime_h=4, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=D("4"), eff_ge_tracker=D("1"))
        day = period(outdoor(), {OCT1: f}, OCT1, OCT1)["daily"][0]
        self.assertTrue(day["extrapolated"])
        self.assertIn("estimation mathématique", day["motifs"][0])

    def test_negative_cph_extrapolation_refused(self):
        f = fact(dse_runtime_h=4, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=D("3"), eff_ge_tracker=D("1"))
        day = period(outdoor(curve=R50C5), {OCT1: f}, OCT1, OCT1)["daily"][0]  # x = 0.083 → CPH < 0
        self.assertEqual(day["status"], E.DAY_CPH_HORS_DOMAINE)
        self.assertIsNone(day["conso_l"])

    def test_indoor_power_is_dc_during_ge_plus_ac_history(self):
        ctx = E.SiteContext(site_id="S2", kind="INDOOR", off_grid=False, dg_count=1, curve=DE22E3)
        facts = {}
        # 6 jours de référence (réseau, GE absent) : AC 5 kW, DC 2.4 kW / 0.8 = 3 kW → aux 2 kW
        for i, d in enumerate(days(OCT1 - timedelta(days=10), 6)):
            facts[d] = fact(dse_runtime_h=0, ac_active_power_avg_w=D("5000") + i, p_dc_day_kw=D("2.4"), eff_day=D("0.8"))
        facts[OCT1] = fact(dse_runtime_h=3, tracker_ge_on_slots=30, p_dc_ge_tracker_kw=D("4"), eff_ge_tracker=D("0.8"),
                           ac_active_power_avg_w=D("9000"))
        r = period(ctx, facts, OCT1, OCT1)
        day = r["daily"][0]
        self.assertEqual(day["power_source"], E.PW_INDOOR)
        self.assertEqual(day["p_dc_input_kw"], D("5"))
        aux = day["p_ac_aux_kw"]
        self.assertEqual(aux.quantize(D("0.0001")), D("2.0025"))
        self.assertEqual(day["p_ge_kw"], D("5") + aux)
        self.assertEqual(len(r["ac_reference"]["reference_dates"]), 6)
        self.assertIn("P_DC pendant GE", day["power_detail"])

    def test_indoor_ac_instrument_never_used_as_ge_power(self):
        ctx = E.SiteContext(site_id="S2", kind="INDOOR", off_grid=False, dg_count=1, curve=DE22E3)
        facts = {OCT1: fact(dse_runtime_h=3, tracker_ge_on_slots=30, p_dc_ge_tracker_kw=D("4"), eff_ge_tracker=D("0.8"),
                            ac_active_power_avg_w=D("9000"))}
        day = period(ctx, facts, OCT1, OCT1)["daily"][0]
        self.assertIsNone(day["p_ge_kw"])
        self.assertIn("profil AC historique insuffisant", day["motifs"][0])

    def test_unknown_site_kind(self):
        f = fact(dse_runtime_h=4, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=5, eff_ge_tracker=D("0.9"))
        day = period(outdoor(kind=None), {OCT1: f}, OCT1, OCT1)["daily"][0]
        self.assertIn("indoor/outdoor inconnu", day["motifs"][0])


class PeriodTests(SimpleTestCase):
    def facts(self):
        # Runtime DSE = numéro du jour (1er → 1 h, 2 → 2 h…), puissance constante.
        return {d: fact(dse_runtime_h=d.day, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"))
                for d in days(OCT1, 25)}

    def test_exact_dates_are_kept(self):
        a = period(outdoor(), self.facts(), date(2026, 10, 1), date(2026, 10, 12))
        b = period(outdoor(), self.facts(), date(2026, 10, 2), date(2026, 10, 20))
        self.assertEqual(a["days"], 12)
        self.assertEqual(b["days"], 19)
        self.assertEqual(a["runtime_total_h"], D(sum(range(1, 13))))
        self.assertEqual(b["runtime_total_h"], D(sum(range(2, 21))))
        cph = a["daily"][0]["cph_l_h"]
        self.assertEqual(a["conso_theorique_l"], D(sum(range(1, 13))) * cph)
        self.assertEqual(b["conso_theorique_l"], D(sum(range(2, 21))) * cph)
        self.assertEqual(a["daily"][0]["date"], date(2026, 10, 1))
        self.assertEqual(b["daily"][-1]["date"], date(2026, 10, 20))

    def test_partial_period_has_no_theoretical_total(self):
        f = self.facts()
        del f[date(2026, 10, 3)]
        r = period(outdoor(), f, date(2026, 10, 1), date(2026, 10, 5))
        self.assertEqual(r["cph_status"], E.CPH_PARTIEL)
        self.assertIsNone(r["conso_theorique_l"])
        self.assertIsNotNone(r["conso_partielle_l"])


class ReconciliationTests(SimpleTestCase):
    def obs(self, **kw):
        base = dict(start=date(2026, 10, 1), end=date(2026, 10, 2), opening_fuel_l=D("500"), closing_fuel_l=D("300"),
                    fuel_deliveries_l=D("0"), fuel_transfer_in_l=D("0"), fuel_transfer_out_l=D("0"),
                    fuel_theft_l=D("0"), fuel_drain_l=D("0"), observation_status="VALIDÉ")
        base.update(kw)
        return E.Observation(**base)

    def facts(self):
        # 10 h × CPH(0.625) = 10 × 3.35 = 33.5 L par jour → 67 L sur 2 jours
        return {d: fact(dse_runtime_h=10, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"))
                for d in days(OCT1, 2)}

    def test_enoc_not_connected_blocks_reconciliation(self):
        r = period(outdoor(), self.facts(), OCT1, OCT1 + timedelta(days=1), [self.obs()])
        rec = r["rapprochement"]
        self.assertEqual(rec["statut"], E.R_DONNEES_INCOMPLETES)
        self.assertEqual(rec["livraisons_statut"], E.LIVRAISONS_ENOC_A_CONTROLER)
        self.assertIsNone(rec["ecart_l"])
        self.assertTrue(any("LIVRAISONS_ENOC_A_CONTROLER" in m for m in rec["motifs"]))

    def test_empty_cell_is_never_zero(self):
        s = E.EngineSettings(enoc_deliveries_connected=True)
        r = period(outdoor(), self.facts(), OCT1, OCT1 + timedelta(days=1), [self.obs(fuel_theft_l=None)], s)
        self.assertEqual(r["rapprochement"]["statut"], E.R_DONNEES_INCOMPLETES)
        self.assertIn("vols", r["rapprochement"]["motifs"][0])
        self.assertIsNone(r["rapprochement"]["conso_stock_l"])

    def test_thresholds(self):
        s = E.EngineSettings(enoc_deliveries_connected=True)
        conso_th = D("2") * D("10") * (D("3.2") * (D("10") / D("16")) ** 2 + D("2.1"))
        cases = [(conso_th + D("90"), E.R_OK), (conso_th + D("150"), E.R_A_JUSTIFIER), (conso_th + D("250"), E.R_A_INVESTIGUER)]
        for conso_stock, expected in cases:
            o = self.obs(opening_fuel_l=conso_stock + D("300"), closing_fuel_l=D("300"))
            rec = period(outdoor(), self.facts(), OCT1, OCT1 + timedelta(days=1), [o], s)["rapprochement"]
            self.assertEqual(rec["statut"], expected, conso_stock)
            self.assertEqual(rec["ecart_l"], conso_stock - conso_th)

    def test_cph_non_calcule_when_theoretical_incomplete(self):
        s = E.EngineSettings(enoc_deliveries_connected=True)
        f = self.facts()
        del f[OCT1]
        rec = period(outdoor(), f, OCT1, OCT1 + timedelta(days=1), [self.obs()], s)["rapprochement"]
        self.assertEqual(rec["statut"], E.R_CPH_NON_CALCULE)

    def test_unvalidated_observation(self):
        s = E.EngineSettings(enoc_deliveries_connected=True)
        rec = period(outdoor(), self.facts(), OCT1, OCT1 + timedelta(days=1), [self.obs(observation_status="brouillon")], s)["rapprochement"]
        self.assertEqual(rec["statut"], E.R_DONNEES_INCOMPLETES)

    def test_no_observation(self):
        r = period(outdoor(), self.facts(), OCT1, OCT1 + timedelta(days=1))
        self.assertEqual(r["rapprochement_statut"], E.R_DONNEES_INCOMPLETES)
        self.assertIsNone(r["rapprochement"])


class CurveResolutionTests(SimpleTestCase):
    def curve_model(self, status="VALIDÉ_CONSTRUCTEUR", approved=False):
        return SimpleNamespace(curve_id="CPH-0014", manufacturer="Caterpillar", model="DE22E3", variant="",
                               status=status, prp_kva=D("20"), prp_kw=D("16"), power_factor=D("0.8"),
                               coef_a=D("3.2"), coef_b=D("0"), coef_c=D("2.1"), source="fiche",
                               is_usable=(status == "VALIDÉ_CONSTRUCTEUR" or approved))

    def test_unvalidated_mapping_gives_no_curve(self):
        m = SimpleNamespace(validated_curve=None, abaque_status="CANDIDAT_UNIQUE_A_VALIDER", action_required="Valider plaque", inventory_kva=D("22"))
        curve, reason = resolve_curve(1, "CATERPILLAR - DE22E3", D("22"), {"caterpillar - de22e3": [m]})
        self.assertIsNone(curve)
        self.assertIn("non validé", reason)

    def test_historical_curve_needs_business_approval(self):
        m = SimpleNamespace(validated_curve=self.curve_model("HISTORIQUE_A_VALIDER"), abaque_status="", action_required="", inventory_kva=D("20"))
        curve, reason = resolve_curve(1, "X", None, {"x": [m]})
        self.assertIsNone(curve)
        m.validated_curve = self.curve_model("HISTORIQUE_A_VALIDER", approved=True)
        curve, _ = resolve_curve(1, "X", None, {"x": [m]})
        self.assertEqual(curve.curve_id, "CPH-0014")

    def test_multi_ge_site(self):
        curve, reason = resolve_curve(2, "X", None, {})
        self.assertIsNone(curve)
        self.assertIn("multi-GE", reason)

    def test_same_label_several_kva_uses_site_kva(self):
        m30 = SimpleNamespace(validated_curve=self.curve_model(), abaque_status="", action_required="", inventory_kva=D("30"))
        m33 = SimpleNamespace(validated_curve=None, abaque_status="CANDIDAT_UNIQUE_A_VALIDER", action_required="", inventory_kva=D("33"))
        mappings = {"olympian - gep30-1": [m30, m33]}
        curve, _ = resolve_curve(1, "OLYMPIAN - GEP30-1", D("30"), mappings)
        self.assertIsNotNone(curve)
        curve, reason = resolve_curve(1, "OLYMPIAN - GEP30-1", None, mappings)
        self.assertIsNone(curve)
        self.assertIn("ambigu", reason)

    def test_off_grid_parsing(self):
        self.assertTrue(E.is_off_grid("Off-Grid"))
        self.assertFalse(E.is_off_grid("On Grid"))
        self.assertIsNone(E.is_off_grid(None))
