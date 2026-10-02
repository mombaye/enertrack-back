from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

from django.test import SimpleTestCase, TestCase

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
        self.assertIn("COALESCENCE_ZERO_SUSPECTE", r["sources"][E.RT_DAY_DG_ON]["rejection"])
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
        self.assertIn("LOAD_AC_INDISPONIBLE", day["motifs"][0])
        self.assertEqual(day["power_trace"][E.PM_INDOOR]["code"], E.PR_LOAD_AC_INDISPONIBLE)

    def test_unknown_site_kind(self):
        f = fact(dse_runtime_h=4, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=5, eff_ge_tracker=D("0.9"))
        day = period(outdoor(kind=None), {OCT1: f}, OCT1, OCT1)["daily"][0]
        self.assertIn("CONFIGURATION_INCONNUE", day["motifs"][0])
        self.assertEqual(day["motif_code"], E.PR_CONFIGURATION_INCONNUE)


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
    def curve_model(self, status="VALIDÉ_CONSTRUCTEUR", power_factor=D("0.8")):
        return SimpleNamespace(curve_id="CPH-0014", manufacturer="Caterpillar", model="DE22E3", variant="",
                               status=status, prp_kva=D("20"), prp_kw=D("16"), power_factor=power_factor,
                               coef_a=D("3.2"), coef_b=D("0"), coef_c=D("2.1"), source="fiche")

    def mapping(self, curve=None, status="A_VALIDER", kva=D("22"), reasons=("score insuffisant",)):
        return SimpleNamespace(validated_curve=curve, match_status=status, match_score=80, match_method="AUTO:X",
                               match_reasons=list(reasons), matched_at=None, validated_at=None, validated_by=None,
                               action_required="Valider plaque", inventory_kva=kva, pk=1)

    def test_mapping_to_validate_gives_no_curve(self):
        curve, reason, match = resolve_curve(1, "CATERPILLAR - DE22E3", D("22"), {"caterpillar - de22e3": [self.mapping()]})
        self.assertIsNone(curve)
        self.assertIn("à valider", reason)
        self.assertEqual(match["statut"], "A_VALIDER")

    def test_auto_validated_historical_curve_is_applied_and_keeps_its_quality_status(self):
        m = self.mapping(self.curve_model("HISTORIQUE_A_VALIDER"), status="AUTO_VALIDE_COMPATIBLE")
        curve, reason, match = resolve_curve(1, "X", None, {"x": [m]})
        self.assertEqual(curve.curve_id, "CPH-0014")
        self.assertIsNone(reason)
        self.assertEqual(match["statut"], "AUTO_VALIDE_COMPATIBLE")
        self.assertEqual(match["courbe_statut"], "HISTORIQUE_A_VALIDER")
        self.assertEqual(curve.status, "HISTORIQUE_A_VALIDER")

    def test_manual_validation_is_applied(self):
        m = self.mapping(self.curve_model(), status="VALIDE_MANUELLEMENT")
        curve, _, match = resolve_curve(1, "X", None, {"x": [m]})
        self.assertIsNotNone(curve)
        self.assertEqual(match["statut"], "VALIDE_MANUELLEMENT")

    def test_curve_without_power_factor_is_applied_with_nominal_08(self):
        # La puissance nominale est kVA × 0,8 : le cos φ propre à la courbe n'est plus requis.
        m = self.mapping(self.curve_model(power_factor=None), status="AUTO_VALIDE_COMPATIBLE")
        curve, reason, match = resolve_curve(1, "X", None, {"x": [m]})
        self.assertIsNotNone(curve)
        self.assertIsNone(reason)
        self.assertEqual(match["curve_source_status"], "VALIDE_CONSTRUCTEUR")

    def test_multi_ge_site_and_unknown_ge(self):
        curve, reason, match = resolve_curve(2, "X", None, {})
        self.assertIsNone(curve)
        self.assertIn("multi-GE", reason)
        self.assertEqual(match["statut"], "SITE_MULTI_GE")
        self.assertEqual(resolve_curve(1, None, None, {})[2]["statut"], "TYPE_GE_ABSENT")
        self.assertEqual(resolve_curve(1, "ZZ - 1", None, {})[2]["statut"], "COURBE_CPH_MANQUANTE")

    def test_same_label_several_kva_uses_site_kva(self):
        m30 = self.mapping(self.curve_model(), status="AUTO_VALIDE_COMPATIBLE", kva=D("30"))
        m33 = self.mapping(None, kva=D("33"))
        mappings = {"olympian - gep30-1": [m30, m33]}
        curve, _, _ = resolve_curve(1, "OLYMPIAN - GEP30-1", D("30"), mappings)
        self.assertIsNotNone(curve)
        curve, reason, match = resolve_curve(1, "OLYMPIAN - GEP30-1", None, mappings)
        self.assertIsNone(curve)
        self.assertIn("ambigu", reason)
        self.assertEqual(match["statut"], "MODELE_AMBIGU")

    def test_off_grid_parsing(self):
        self.assertTrue(E.is_off_grid("Off-Grid"))
        self.assertFalse(E.is_off_grid("On Grid"))
        self.assertIsNone(E.is_off_grid(None))


class ObservationFileTests(SimpleTestCase):
    def _xlsx(self, sheets):
        import io

        import openpyxl

        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for name, rows in sheets.items():
            ws = wb.create_sheet(name)
            for r in rows:
                ws.append(r)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    def test_abaque_file_is_redirected_to_abaque_import(self):
        from fuel_tracking.services.fuel_observation_import import ABAQUE_FILE_ERROR, read_rows

        content = self._xlsx({"Abaque CPH": [["CURVE_ID"]], "Mappage inventaire": [["GE"]]})
        with self.assertRaisesMessage(ValueError, ABAQUE_FILE_ERROR):
            read_rows("ABAQUE_CPH_GE_PRP_50HZ.xlsx", content)

    def test_unrelated_file_gets_short_message(self):
        from fuel_tracking.services.fuel_observation_import import read_rows

        with self.assertRaisesMessage(ValueError, "n'est pas un fichier d'observation stock"):
            read_rows("autre.xlsx", self._xlsx({"Feuil1": [["a", "b"], [1, 2]]}))

    def test_partial_header_lists_only_missing_columns(self):
        from fuel_tracking.services.fuel_observation_import import COLUMNS, read_rows

        header = [c for c in COLUMNS if c != "justificatif"]
        with self.assertRaises(ValueError) as ctx:
            read_rows("obs.xlsx", self._xlsx({"Obs": [header]}))
        self.assertEqual(str(ctx.exception), "Colonnes manquantes : justificatif.")

    def test_template_header_with_semicolons_is_read(self):
        from fuel_tracking.services.fuel_observation_import import COLUMNS, read_rows

        content = ("﻿" + ";".join(COLUMNS) + "\r\n").encode("utf-8")
        self.assertEqual(read_rows("modele.csv", content), [])


class BlocageTests(SimpleTestCase):
    """Premier point bloquant affiché à l'utilisateur (views_cph._blocage_code)."""

    def facts(self, n=2):
        return {d: fact(dse_runtime_h=10, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"))
                for d in days(OCT1, n)}

    def code(self, ctx, facts, observations=(), settings=None):
        from fuel_tracking.views_cph import _blocage_code
        return _blocage_code(period(ctx, facts, OCT1, OCT1 + timedelta(days=1), observations, settings))

    def obs(self):
        return E.Observation(start=OCT1, end=OCT1 + timedelta(days=1), opening_fuel_l=D("500"), closing_fuel_l=D("300"),
                             fuel_deliveries_l=D("0"), fuel_transfer_in_l=D("0"), fuel_transfer_out_l=D("0"),
                             fuel_theft_l=D("0"), fuel_drain_l=D("0"), observation_status="VALIDÉ")

    def test_match_statuses_are_classified(self):
        cases = {
            "A_VALIDER": "MAPPAGE_NON_VALIDE",
            "SITE_MULTI_GE": "MULTI_GE",
            "TYPE_GE_ABSENT": "TYPE_GE_ABSENT",
            "COURBE_CPH_MANQUANTE": "COURBE_CPH_MANQUANTE",
            "MODELE_AMBIGU": "MAPPAGE_AMBIGU",
            "REJETE": "MAPPAGE_NON_VALIDE",
        }
        for statut, expected in cases.items():
            ctx = outdoor(curve=None, curve_reason="x", match={"statut": statut})
            self.assertEqual(self.code(ctx, self.facts()), expected, statut)

    def test_runtime_then_observation_then_enoc(self):
        connected = E.EngineSettings(enoc_deliveries_connected=True)
        self.assertEqual(self.code(outdoor(), {}), "RUNTIME_ABSENT")
        self.assertEqual(self.code(outdoor(), self.facts(1)), "OBSERVATION_ABSENTE")
        self.assertEqual(self.code(outdoor(), self.facts(1), [self.obs()], connected), "CPH_PARTIEL")
        self.assertEqual(self.code(outdoor(), self.facts()), "OBSERVATION_ABSENTE")
        self.assertEqual(self.code(outdoor(), self.facts(), [self.obs()]), "LIVRAISONS_ENOC")
        self.assertIsNone(self.code(outdoor(), self.facts(), [self.obs()], connected))

    def test_site_with_verdict_has_no_blocage_even_if_period_cph_is_partial(self):
        from fuel_tracking.views_cph import _blocage_code
        obs = self.obs()  # relevé du 1er au 2, période du 1er au 3 dont le 3 sans données
        r = period(outdoor(), self.facts(), OCT1, OCT1 + timedelta(days=2), [obs], E.EngineSettings(enoc_deliveries_connected=True))
        self.assertEqual(r["cph_status"], E.CPH_PARTIEL)
        self.assertIn(r["rapprochement_statut"], (E.R_OK, E.R_A_JUSTIFIER, E.R_A_INVESTIGUER))
        self.assertIsNone(_blocage_code(r))

    def test_data_issue_comes_first(self):
        self.assertEqual(self.code(outdoor(data_issue="plusieurs DATA_ID", curve=None, curve_reason="x"), self.facts()), "DONNEES_SITE")


class AutoMatchingTests(SimpleTestCase):
    """Correspondance automatique plaque → courbe (services/cph_matching.py)."""

    def curve(self, cid, manufacturer, model, kva, points=(D("3.8"), D("5.2"), D("6.9")), frequency="50 Hz"):
        return {"curve_id": cid, "manufacturer": manufacturer, "model": model, "prp_kva": D(kva), "frequency": frequency,
                "conso_50_l_h": points[0], "conso_75_l_h": points[1], "conso_100_l_h": points[2]}

    def match(self, label, kva, candidates, curves):
        from fuel_tracking.services.cph_matching import auto_match
        by = {c["curve_id"]: c for c in curves}
        return auto_match(label, None if kva is None else D(kva), candidates, by, curves)

    def test_unique_identical_model_is_auto_validated(self):
        curves = [self.curve("CPH-0001", "AKSA", "AP33", "33")]
        d = self.match("AKSA - AP33", "33", ["CPH-0001"], curves)
        self.assertEqual(d["status"], "AUTO_VALIDE_COMPATIBLE")
        self.assertEqual(d["curve_id"], "CPH-0001")
        self.assertEqual(d["score"], 100)
        self.assertTrue(d["method"].startswith("AUTO:"))

    def test_variant_suffix_and_standby_rating_are_compatible(self):
        curves = [self.curve("CPH-0048", "FG Wilson", "P33-3", "30")]
        d = self.match("FG Wilson P33-3U", "33", ["CPH-0048"], curves)  # 33 kVA secours ≈ 1,1 × 30 kVA PRP
        self.assertEqual(d["status"], "AUTO_VALIDE_COMPATIBLE")
        self.assertEqual(d["score"], 80)

    def test_same_kva_never_links_two_different_models(self):
        curves = [self.curve("CPH-0121", "Rehlko / SDMO", "J200_220", "182")]
        d = self.match("SDMO - J200", "200", ["CPH-0121"], curves)
        self.assertEqual(d["status"], "A_VALIDER")
        self.assertIsNone(d["curve_id"])
        self.assertTrue(any("incompatible" in r for r in d["reasons"]))

    def test_large_power_gap_is_blocking(self):
        curves = [self.curve("CPH-0001", "AKSA", "AP33", "33")]
        d = self.match("AKSA - AP33", "60", ["CPH-0001"], curves)
        self.assertEqual(d["status"], "A_VALIDER")
        self.assertTrue(any("puissance incompatible" in r for r in d["reasons"]))

    def test_brand_contradiction_and_60hz_are_blocking(self):
        curves = [self.curve("CPH-0125", "SDMO", "GEP 33-3", "33")]
        self.assertEqual(self.match("OLYMPIAN - GEP33-3", "33", ["CPH-0125"], curves)["status"], "A_VALIDER")
        curves = [self.curve("CPH-0001", "AKSA", "AP33", "33", frequency="60 Hz")]
        self.assertEqual(self.match("AKSA - AP33", "33", ["CPH-0001"], curves)["status"], "A_VALIDER")

    def test_missing_or_unreadable_model(self):
        curves = [self.curve("CPH-0004", "AMMAN", "M180", "180")]
        self.assertEqual(self.match("GENERAC - 0", "15", ["CPH-0004"], curves)["status"], "A_VALIDER")
        self.assertEqual(self.match("PA50", "50", [], curves)["status"], "COURBE_CPH_MANQUANTE")

    def test_several_candidates_stay_ambiguous(self):
        curves = [self.curve("CPH-0042", "FG Wilson", "P22-1", "20"), self.curve("CPH-0112", "Perkins", "P22-1", "20")]
        d = self.match("FG Wilson P22-1", "22", ["CPH-0042", "CPH-0112"], curves)
        self.assertEqual(d["status"], "MODELE_AMBIGU")
        self.assertIsNone(d["curve_id"])

    def test_duplicate_curves_with_same_points_are_not_ambiguous_but_different_ones_are(self):
        twins = [self.curve("CPH-0048", "FG Wilson", "P33-3", "30"), self.curve("CPH-0047", "FG Wilson", "P33-3", "30")]
        d = self.match("FG Wilson P33-3", "33", ["CPH-0048"], twins)
        self.assertEqual(d["status"], "AUTO_VALIDE_COMPATIBLE")
        self.assertTrue(any("CPH-0047" in r for r in d["reasons"]))
        rivals = [self.curve("CPH-0130", "SDMO", "J220K", "220", (D("24"), D("36"), D("48"))),
                  self.curve("CPH-0129", "SDMO", "J220K", "200", (D("21.5"), D("32"), D("43")))]
        self.assertEqual(self.match("SDMO |J220K", "220", ["CPH-0130"], rivals)["status"], "A_VALIDER")

    def test_human_decisions_are_never_overwritten(self):
        from fuel_tracking.services.cph_matching import apply_auto_matching

        class Mapping(SimpleNamespace):
            def save(self, update_fields=None):
                self.saved = True

        curve_obj = SimpleNamespace(pk=7, curve_id="CPH-0001", manufacturer="AKSA", model="AP33", prp_kva=D("33"),
                                    frequency="50 Hz", conso_50_l_h=D("4"), conso_75_l_h=D("5"), conso_100_l_h=D("6"))
        base = dict(inventory_label="AKSA - AP33", inventory_kva=D("33"), candidate_curve_ids=["CPH-0001"],
                    match_score=None, match_reasons=[], matched_at=None, validated_by_id=None, validated_at=None,
                    validation_comment="", saved=False)
        auto = Mapping(**base, match_status="A_VALIDER", match_method="", validated_curve_id=None)
        removed = Mapping(**base, match_status="A_VALIDER", match_method="RETRAIT_MANUEL", validated_curve_id=None)
        manual = Mapping(**base, match_status="VALIDE_MANUELLEMENT", match_method="MANUEL", validated_curve_id=99)
        counts = apply_auto_matching([auto, removed, manual], [curve_obj], None)
        self.assertEqual(auto.match_status, "AUTO_VALIDE_COMPATIBLE")
        self.assertEqual(auto.validated_curve_id, 7)
        self.assertFalse(removed.saved)
        self.assertEqual(removed.match_status, "A_VALIDER")
        self.assertFalse(manual.saved)
        self.assertEqual(manual.validated_curve_id, 99)
        self.assertEqual(counts, {"AUTO_VALIDE_COMPATIBLE": 1, "A_VALIDER": 1, "VALIDE_MANUELLEMENT": 1})


class MeasuredComparisonTests(SimpleTestCase):
    """Conso estimée (runtime × CPH) vs conso mesurée (VW_FUEL_REPORT)."""

    def facts(self, measured):
        # 10 h × CPH 3,35 L/h = 33,5 L estimés par jour
        return {d: fact(dse_runtime_h=10, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"),
                        measured_conso_l=m) for d, m in zip(days(OCT1, len(measured)), measured)}

    def test_estimate_is_kept_when_measure_is_absent(self):
        r = period(outdoor(), self.facts([None, None]), OCT1, OCT1 + timedelta(days=1))
        self.assertEqual(r["conso_theorique_l"], D("67.0"))
        c = r["comparaison"]
        self.assertEqual(c["statut"], E.C_MESURE_ABSENTE)
        self.assertIsNone(c["conso_mesuree_l"])
        self.assertIsNone(c["ecart_l"])

    def test_gap_is_measured_minus_estimated_on_common_days_only(self):
        r = period(outdoor(), self.facts([D("40"), None, D("0")]), OCT1, OCT1 + timedelta(days=2))
        c = r["comparaison"]
        self.assertEqual(c["conso_mesuree_l"], D("40"))  # 0 mesuré reste 0, NULL reste absent
        self.assertEqual(c["jours_mesure"], 2)
        self.assertEqual(c["jours_communs"], 2)
        self.assertEqual(c["conso_estimee_communs_l"], D("67.0"))
        self.assertEqual(c["ecart_l"], D("40") - D("67.0"))
        self.assertEqual(c["statut"], E.C_COHERENT)  # |−27 L| ≤ max(100 L, 10 %)
        self.assertIn("2/3", c["motif"])

    def test_no_cph_keeps_the_measure_and_says_why(self):
        r = period(outdoor(curve=None, curve_reason="x"), self.facts([D("30"), D("20")]), OCT1, OCT1 + timedelta(days=1))
        self.assertIsNone(r["cph_moy_l_h"])
        c = r["comparaison"]
        self.assertEqual(c["statut"], E.C_NON_CALCULEE)
        self.assertEqual(c["conso_mesuree_l"], D("50"))
        self.assertIsNone(c["ecart_l"])

    def test_charge_and_runtime_source_availability(self):
        r = period(outdoor(), self.facts([None, None]), OCT1, OCT1 + timedelta(days=1))
        self.assertEqual(r["runtime_source_main"], E.RT_DSE)
        self.assertEqual(r["runtime_source_availability"], D("1"))
        self.assertEqual(r["charge_moy"], D("0.625"))

    def test_custom_exact_dates_are_not_extended_to_the_month(self):
        f = self.facts([D("10")] * 12)
        r = period(outdoor(), f, date(2026, 10, 1), date(2026, 10, 12))
        self.assertEqual(r["days"], 12)
        self.assertEqual(r["comparaison"]["jours_communs"], 12)
        self.assertEqual(r["daily"][-1]["date"], date(2026, 10, 12))


class DailyFactsSqlTests(SimpleTestCase):
    def test_measured_conso_uses_the_strict_fuel_report_filter(self):
        from fuel_tracking.services.fuel_daily_facts_snowflake import build_daily_facts_sql
        sql = build_daily_facts_sql([1, 2])
        self.assertIn("DB_GFMS_ANALYTICS_DEV.GOLD.VW_FUEL_REPORT", sql)
        self.assertIn("QUALITY_STATUS = 'OK' AND VALID_POINT_COUNT >= 2 AND DROP_DETECTED = TRUE", sql)
        self.assertNotIn("INSERT", sql.upper().replace("INSERTED", ""))


class BlocageNoCphTests(SimpleTestCase):
    def test_ge_off_days_do_not_hide_a_missing_cph(self):
        from fuel_tracking.views_cph import _blocage_code
        f = {OCT1: fact(dse_runtime_h=0),  # GE à l'arrêt : 0 L valide
             OCT1 + timedelta(days=1): fact(dse_runtime_h=10)}  # marche sans puissance qualifiée
        r = period(outdoor(), f, OCT1, OCT1 + timedelta(days=1))
        self.assertEqual(r["cph_days"], 0)
        self.assertEqual(r["cph_status"], E.CPH_PARTIEL)
        self.assertEqual(_blocage_code(r), "PUISSANCE_ABSENTE")


class RulesV2Tests(SimpleTestCase):
    """Règles finalisées : charge = P / (kVA × 0,8), motifs précis, tolérance puissance 15 %."""

    def test_charge_uses_nominal_kva_times_08(self):
        # GEP18-6 : 18 kVA, PRP 13,2 kW dans l'abaque → la charge utilise 18 × 0,8 = 14,4 kW
        curve = E.Curve(curve_id="CPH-0103", label="Olympian GEP18-6", status="HISTORIQUE_A_VALIDER",
                        prp_kva=D("18"), prp_kw=D("13.2"), power_factor=None, a=D("1"), b=D("2"), c=D("1"))
        res = E.apply_curve(curve, D("7.2"))
        self.assertEqual(res["charge"], D("0.5"))
        self.assertEqual(res["cph_l_h"], D("1") * D("0.25") + D("2") * D("0.5") + D("1"))
        over = E.apply_curve(curve, D("15.2"))  # > 1,05 × 14,4 = 15,12 kW
        self.assertIsNone(over["cph_l_h"])
        self.assertEqual(over["code"], E.MC_PUISSANCE_HORS_LIMITE)

    def test_missing_nominal_power(self):
        curve = E.Curve(curve_id="X", label="X", status="HISTORIQUE_A_VALIDER", prp_kva=None, prp_kw=None,
                        power_factor=None, a=D("1"), b=D("1"), c=D("1"))
        self.assertEqual(E.apply_curve(curve, D("5"))["code"], E.MC_PUISSANCE_NOMINALE_ABSENTE)

    def test_precise_day_codes(self):
        f = {
            OCT1: fact(),                                                       # aucune donnée
            OCT1 + timedelta(days=1): fact(dse_runtime_h=30),                   # valeur brute hors bornes
            OCT1 + timedelta(days=2): fact(dse_runtime_h=5, tracker_ge_on_slots=10,
                                           p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("1.4")),  # rendement invalide
            OCT1 + timedelta(days=3): fact(dse_runtime_h=5),                    # pas de P_DC
        }
        r = period(outdoor(), f, OCT1, OCT1 + timedelta(days=3))
        codes = [d["motif_code"] for d in r["daily"]]
        self.assertEqual(codes, [E.MC_RUNTIME_INDISPONIBLE, E.MC_RUNTIME_NON_QUALIFIE,
                                 E.MC_RENDEMENT_INVALIDE, E.MC_PUISSANCE_INDISPONIBLE])
        self.assertTrue(all(d["cph_l_h"] is None and d["conso_l"] is None for d in r["daily"]))
        self.assertIsNotNone(r["motif_cph"])

    def test_site_motif_comes_from_the_mapping_when_no_curve(self):
        ctx = outdoor(curve=None, curve_reason="aucune courbe", curve_code=E.MC_COURBE_CPH_MANQUANTE)
        r = period(ctx, {OCT1: fact(dse_runtime_h=5)}, OCT1, OCT1)
        self.assertEqual(r["motif_cph"]["code"], E.MC_COURBE_CPH_MANQUANTE)
        self.assertIsNone(r["cph_moy_l_h"])
        self.assertIsNone(r["conso_theorique_l"])
        self.assertIsNone(period(outdoor(), {OCT1: fact(dse_runtime_h=10, tracker_ge_on_slots=10,
                                                       p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"))},
                                 OCT1, OCT1)["motif_cph"])

    def test_indoor_power_source_label(self):
        self.assertEqual(E.PW_INDOOR, "ESTIMATION_HISTORIQUE_LOAD_AC")

    def test_power_tolerance_is_15_percent_against_curve_kva(self):
        from fuel_tracking.services.cph_matching import auto_match
        c = {"curve_id": "CPH-0104", "manufacturer": "OLYMPIAN", "model": "GEP30-1", "prp_kva": D("27.3"),
             "frequency": "50 Hz", "conso_50_l_h": D("1"), "conso_75_l_h": D("2"), "conso_100_l_h": D("3")}
        ok = auto_match("OLYMPIAN - GEP30-1", D("30"), ["CPH-0104"], {"CPH-0104": c}, [c])      # 9,9 %
        ko = auto_match("OLYMPIAN - GEP30-1", D("33"), ["CPH-0104"], {"CPH-0104": c}, [c])      # 20,9 %
        self.assertEqual(ok["status"], "AUTO_VALIDE_COMPATIBLE")
        self.assertEqual(ko["status"], "A_VALIDER")
        self.assertEqual(auto_match("OLYMPIAN - GEP30-1", D("33"), ["CPH-0104"], {"CPH-0104": c}, [c],
                                    D("0.25"))["status"], "AUTO_VALIDE_COMPATIBLE")

    def test_normalisation_examples(self):
        from fuel_tracking.services.cph_matching import norm
        self.assertEqual(norm("AKSA - AP33"), norm("AKSA AP33"))
        self.assertEqual(norm("SDMO - J66"), norm("SDMO J66"))
        self.assertEqual(norm("FG Wilson P33-3U"), norm("FG WILSON P33 3U"))
        self.assertEqual(norm("OLYMPIAN - GEP30-1"), norm("OLYMPIAN GEP30 1"))

    def test_history_and_rejection(self):
        from fuel_tracking.services.cph_matching import apply_auto_matching

        class Mapping(SimpleNamespace):
            def save(self, update_fields=None):
                pass

        created = []

        class History:
            class objects:
                @staticmethod
                def create(**kw):
                    created.append(kw)

        curve_obj = SimpleNamespace(pk=7, curve_id="CPH-0001", manufacturer="AKSA", model="AP33", prp_kva=D("33"),
                                    frequency="50 Hz", conso_50_l_h=D("4"), conso_75_l_h=D("5"), conso_100_l_h=D("6"))
        base = dict(pk=1, inventory_label="AKSA - AP33", inventory_kva=D("33"), candidate_curve_ids=["CPH-0001"],
                    match_score=None, match_reasons=[], matched_at=None, validated_by_id=None, validated_at=None,
                    validation_comment="", validated_curve_id=None)
        m = Mapping(**base, match_status="A_VALIDER", match_method="")
        rejected = Mapping(**{**base, "pk": 2}, match_status="REJETE", match_method="RETRAIT_MANUEL")
        apply_auto_matching([m, rejected], [curve_obj], "now", history_model=History)
        self.assertEqual(m.match_status, "AUTO_VALIDE_COMPATIBLE")
        self.assertEqual(rejected.match_status, "REJETE")
        self.assertEqual(len(created), 1)
        h = created[0]
        self.assertEqual((h["old_status"], h["new_status"], h["new_curve_id"], h["score"]),
                         ("A_VALIDER", "AUTO_VALIDE_COMPATIBLE", "CPH-0001", 100))
        self.assertIn("AUTO:", h["rule"])

    def test_export_rows_match_header(self):
        from fuel_tracking.views_cph import EXPORT_HEADER, _csv_rows
        r = period(outdoor(match={"statut": "AUTO_VALIDE_COMPATIBLE", "curve_source_status": "VALIDE_CONSTRUCTEUR"}),
                   {OCT1: fact(dse_runtime_h=10, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8")),
                    OCT1 + timedelta(days=1): fact()}, OCT1, OCT1 + timedelta(days=1))
        lines = list(_csv_rows([r], only_anomalies=False))
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(line) == len(EXPORT_HEADER) for line in lines))
        anomalies = list(_csv_rows([r], only_anomalies=True))
        self.assertEqual(len(anomalies), 2)  # site sans verdict OK : toutes ses lignes sont des anomalies


class NoCphComparisonTests(SimpleTestCase):
    def test_no_gap_when_only_ge_off_days_are_estimated(self):
        f = {OCT1: fact(dse_runtime_h=0, measured_conso_l=D("12")),
             OCT1 + timedelta(days=1): fact(dse_runtime_h=6, measured_conso_l=D("20"))}  # marche sans puissance
        r = period(outdoor(), f, OCT1, OCT1 + timedelta(days=1))
        self.assertEqual(r["comparaison"]["statut"], E.C_NON_CALCULEE)
        self.assertIsNone(r["comparaison"]["ecart_l"])
        self.assertEqual(r["comparaison"]["conso_mesuree_l"], D("32"))

    def test_ge_off_whole_period_is_a_real_zero(self):
        f = {d: fact(dse_runtime_h=0, measured_conso_l=D("0")) for d in days(OCT1, 2)}
        r = period(outdoor(), f, OCT1, OCT1 + timedelta(days=1))
        self.assertEqual(r["conso_theorique_l"], D("0"))
        self.assertEqual(r["comparaison"]["ecart_l"], D("0"))


class SpecificConsumptionTests(SimpleTestCase):
    """Consommation spécifique L/kWh (CLAUDE.md §5 : ~0,25-0,30 L/kWh, hors plage = alerte)."""

    def facts(self, measured):
        # 10 h × P = 8 / 0,8 = 10 kW → 100 kWh / jour ; CPH(0,625) = 3,35 L/h → 33,5 L / jour
        return {d: fact(dse_runtime_h=10, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"),
                        measured_conso_l=m) for d, m in zip(days(OCT1, len(measured)), measured)}

    def test_estimated_and_measured_sfc(self):
        r = period(outdoor(), self.facts([D("30"), None]), OCT1, OCT1 + timedelta(days=1))
        sfc = r["conso_specifique"]
        self.assertEqual(sfc["energie_ge_kwh"], D("200"))
        self.assertEqual(sfc["estimee_l_kwh"], D("67.0") / D("200"))
        self.assertEqual(sfc["mesuree_l_kwh"], D("30") / D("100"))  # seuls les jours mesurés
        self.assertFalse(sfc["alerte_estimee"])
        self.assertFalse(sfc["alerte_mesuree"])

    def test_out_of_range_raises_an_alert_without_changing_values(self):
        r = period(outdoor(), self.facts([D("90")]), OCT1, OCT1)
        sfc = r["conso_specifique"]
        self.assertTrue(sfc["alerte_mesuree"])
        self.assertTrue(any("perte, vol ou capteur" in a for a in sfc["alertes"]))
        self.assertEqual(r["conso_theorique_l"], D("33.5"))

    def test_no_energy_means_no_ratio(self):
        r = period(outdoor(curve=None, curve_reason="x"), self.facts([D("10")]), OCT1, OCT1)
        self.assertIsNone(r["conso_specifique"]["estimee_l_kwh"])
        self.assertIsNone(r["conso_specifique"]["alerte_estimee"])


class PeriodCacheTests(SimpleTestCase):
    def test_cache_is_versioned_and_falls_back_when_unavailable(self):
        from unittest import mock

        from fuel_tracking.services import cph_service as svc

        rows = [{"site_id": "S1", "daily": [1, 2]}]
        with mock.patch.object(svc, "compute_period", return_value=rows) as cp, \
                mock.patch.object(svc, "data_version", return_value="v1"):
            store = {}
            fake = mock.Mock(get=lambda k: store.get(k), set=lambda k, v, t: store.__setitem__(k, v))
            with mock.patch("django.core.cache.cache", fake):
                self.assertEqual(svc.compute_period_summary(OCT1, OCT1), [{"site_id": "S1"}])  # sans détail jour
                svc.compute_period_summary(OCT1, OCT1)
                self.assertEqual(cp.call_count, 1)  # 2e appel servi par le cache
            broken = mock.Mock(get=mock.Mock(side_effect=ConnectionError), set=mock.Mock(side_effect=ConnectionError))
            with mock.patch("django.core.cache.cache", broken):
                self.assertEqual(svc.compute_period_summary(OCT1, OCT1), [{"site_id": "S1"}])
        with mock.patch.object(svc, "compute_period", return_value=rows) as cp, \
                mock.patch.object(svc, "data_version", return_value="v2"), \
                mock.patch("django.core.cache.cache", fake):
            svc.compute_period_summary(OCT1, OCT1)
            self.assertEqual(cp.call_count, 1)  # nouvelle version des données → recalcul


class HealthTests(TestCase):
    """Surveillance : données absentes / périmées, synchro en échec, référentiel absent."""

    def test_empty_database_is_critical(self):
        from fuel_tracking.services.cph_health import cph_health
        h = cph_health()
        codes = {i["code"] for i in h["issues"]}
        self.assertTrue(h["critique"])
        self.assertTrue({"FAITS_ABSENTS", "ABAQUE_ABSENT"} <= codes)

    def test_stale_facts_and_failed_sync(self):
        from fuel_tracking.models import FuelDailyFactsSyncRun, FuelSiteDailyFacts
        from fuel_tracking.services.cph_health import cph_health, freshness
        FuelSiteDailyFacts.objects.create(country="Senegal", data_id=1, site_id="S1", date=date.today() - timedelta(days=5))
        FuelDailyFactsSyncRun.objects.create(date_from=date.today(), date_to=date.today(),
                                             status=FuelDailyFactsSyncRun.Status.FAILED, error_message="timeout Snowflake")
        f = freshness()
        self.assertEqual(f["facts_age_days"], 5)
        self.assertTrue(f["facts_stale"])
        self.assertTrue(f["last_sync_failed"])
        codes = {i["code"]: i["niveau"] for i in cph_health()["issues"]}
        self.assertEqual(codes["FAITS_PERIMES"], "ALERTE")
        self.assertIn("SYNC_ECHEC", codes)



class PowerFallbackTests(SimpleTestCase):
    """Chaîne de repli puissance : DSE/production → P_DC/η (outdoor) → P_DC + load AC (indoor) → NULL."""

    def indoor(self, **kw):
        base = dict(site_id="S2", kind="INDOOR", off_grid=False, dg_count=1, curve=DE22E3)
        base.update(kw)
        return E.SiteContext(**base)

    def ref_days(self, ac_w, p_dc=D("2.4"), eff=D("0.8"), n=6):
        return {d: fact(dse_runtime_h=0, ac_active_power_avg_w=ac_w, p_dc_day_kw=p_dc, eff_day=eff, ac_point_count=288)
                for d in days(OCT1 - timedelta(days=10), n)}

    def test_production_is_tried_first_even_indoor(self):
        f = {OCT1: fact(dse_runtime_h=4, dg_production_kwh=D("40"))}  # aucun P_DC, aucun historique AC
        day = period(self.indoor(), f, OCT1, OCT1)["daily"][0]
        self.assertEqual(day["p_ge_kw"], D("10"))
        self.assertEqual(day["power_method"], E.PM_DIRECT)
        self.assertEqual(day["power_trace"][E.PM_DIRECT]["statut"], "RETENUE")
        self.assertEqual(day["power_trace"][E.PM_INDOOR]["statut"], "NON_TENTEE")
        self.assertIsNotNone(day["cph_l_h"])

    def test_fallback_to_rectifier_when_production_absent_and_trace_kept(self):
        f = {OCT1: fact(dse_runtime_h=4, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"))}
        day = period(outdoor(), f, OCT1, OCT1)["daily"][0]
        self.assertEqual(day["power_method"], E.PM_PDC)
        t = day["power_trace"]
        self.assertEqual((t[E.PM_DIRECT]["statut"], t[E.PM_DIRECT]["code"]), ("ABSENTE", E.PR_P_DSE_INDISPONIBLE))
        self.assertEqual(t[E.PM_PDC]["statut"], "RETENUE")
        self.assertEqual(t[E.PM_INDOOR]["statut"], "NON_APPLICABLE")
        self.assertIn("BATTERIE_NON_INTEGREE", day["power_detail"])

    def test_method_over_cap_is_rejected_and_next_one_used(self):
        # DE22E3 : 20 kVA × 0,8 × 1,05 = 16,8 kW ; production 100 kWh / 4 h = 25 kW → rejetée
        f = {OCT1: fact(dse_runtime_h=4, dg_production_kwh=D("100"), tracker_ge_on_slots=10,
                        p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"))}
        day = period(outdoor(), f, OCT1, OCT1)["daily"][0]
        self.assertEqual(day["power_trace"][E.PM_DIRECT]["code"], E.MC_PUISSANCE_HORS_LIMITE)
        self.assertEqual(day["power_method"], E.PM_PDC)
        self.assertEqual(day["p_ge_kw"], D("10"))

    def test_all_methods_failed_gives_null_with_every_reason(self):
        f = {OCT1: fact(dse_runtime_h=4)}
        day = period(outdoor(), f, OCT1, OCT1)["daily"][0]
        self.assertIsNone(day["p_ge_kw"])
        self.assertEqual(day["motif_code"], E.MC_PUISSANCE_INDISPONIBLE)
        self.assertIn("P_DSE_INDISPONIBLE", day["motifs"][0])
        self.assertIn("PDC_GE_INDISPONIBLE", day["motifs"][0])
        self.assertEqual(set(day["power_rejection_codes"]), {E.PR_P_DSE_INDISPONIBLE, E.PR_PDC_GE_INDISPONIBLE})

    def test_clamped_ac_is_incoherent_not_zero(self):
        # AC 2 000 W = 2 kW < entrée DC 3 kW chaque jour : l'ancien calcul donnait 0,000 kW par écrêtage
        ref = E.indoor_ac_reference(self.indoor(), self.ref_days(D("2000")), OCT1, OCT1)
        self.assertEqual(ref["statut"], E.AC_INCOHERENT)
        self.assertIsNone(ref["p_ac_aux_kw"])
        self.assertEqual(ref["jours_ecart_negatif"], 6)

    def test_true_measured_zero(self):
        ref = E.indoor_ac_reference(self.indoor(), self.ref_days(D("3020")), OCT1, OCT1)  # 3,02 kW ≈ 3 kW entrée
        self.assertEqual(ref["statut"], E.AC_QUALIFIE)
        ref0 = E.indoor_ac_reference(self.indoor(), self.ref_days(D("2950")), OCT1, OCT1)  # écart −0,05 kW ≤ tolérance
        self.assertEqual(ref0["statut"], E.AC_MESURE_ZERO)
        self.assertEqual(ref0["p_ac_aux_kw"], D("0"))
        self.assertEqual(ref0["mesures"], 6 * 288)

    def test_ac_unit_suspected_when_value_looks_like_kw(self):
        ref = E.indoor_ac_reference(self.indoor(), self.ref_days(D("4.5")), OCT1, OCT1)
        self.assertEqual(ref["statut"], E.AC_UNITE_SUSPECTE)
        self.assertIsNone(ref["p_ac_aux_kw"])

    def test_separate_cph_and_stock_statuses(self):
        f = {d: fact(dse_runtime_h=4, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"))
             for d in days(OCT1, 2)}
        r = period(outdoor(), f, OCT1, OCT1 + timedelta(days=1))
        self.assertEqual(r["statut_cph"], E.SC_CPH_CALCULE)
        self.assertEqual(r["statut_rapprochement_calcul"], E.SR_STOCK_ABSENT)
        obs = E.Observation(start=OCT1, end=OCT1 + timedelta(days=1), opening_fuel_l=D("500"), closing_fuel_l=D("400"),
                            fuel_deliveries_l=None, fuel_transfer_in_l=D("0"), fuel_transfer_out_l=D("0"),
                            fuel_theft_l=D("0"), fuel_drain_l=D("0"), observation_status="VALIDÉ")
        r2 = period(outdoor(), f, OCT1, OCT1 + timedelta(days=1), [obs])
        self.assertEqual(r2["statut_rapprochement_calcul"], E.SR_MOUVEMENTS_ABSENTS)
        self.assertEqual(r2["statut_cph"], E.SC_CPH_CALCULE)


class PeriodAndSynthesisTests(SimpleTestCase):
    """Période incomplète, conso partielle honnête, couvertures et synthèse des blocages."""

    def test_days_after_last_sync_are_periode_incomplete(self):
        f = {OCT1: fact(dse_runtime_h=4, tracker_ge_on_slots=10, p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"))}
        r = period(outdoor(), f, OCT1, OCT1 + timedelta(days=1), settings=E.EngineSettings(data_until=OCT1))
        self.assertEqual(r["statut_cph"], E.SC_CPH_PARTIEL)
        self.assertEqual(r["daily"][1]["motif_code"], E.MC_PERIODE_INCOMPLETE)
        self.assertIn(E.MC_PERIODE_INCOMPLETE, r["blocked_days"])

    def test_ge_off_days_only_is_not_a_partial_estimate(self):
        f = {OCT1: fact(dse_runtime_h=0), OCT1 + timedelta(days=1): fact(dse_runtime_h=5)}  # puissance absente le 2e jour
        r = period(outdoor(), f, OCT1, OCT1 + timedelta(days=1))
        self.assertEqual(r["statut_cph"], E.SC_CPH_NON_CALCULE)
        self.assertIsNone(r["conso_partielle_l"])

    def test_curve_missing_is_counted_with_measured_volume(self):
        f = {OCT1: fact(dse_runtime_h=4, measured_conso_l=D("12"), tracker_ge_on_slots=10,
                        p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"))}
        r = period(outdoor(curve=None, curve_code=E.MC_COURBE_CPH_MANQUANTE), f, OCT1, OCT1)
        b = r["blocked_days"][E.MC_COURBE_CPH_MANQUANTE]
        self.assertEqual((b["jours"], b["runtime_h"], b["mesuree_l"]), (1, D("4"), D("12")))

    def test_couvertures_and_diagnostic(self):
        from fuel_tracking import views_cph as V

        ok = {OCT1: fact(dse_runtime_h=4, measured_conso_l=D("10"), tracker_ge_on_slots=10,
                         p_dc_ge_tracker_kw=D("8"), eff_ge_tracker=D("0.8"))}
        no_power = {OCT1: fact(dse_runtime_h=4)}
        rows = [period(outdoor(site_id="A", ge_label="CAT DE22E3"), ok, OCT1, OCT1),
                period(outdoor(site_id="B", ge_label="CAT DE22E3"), no_power, OCT1, OCT1)]
        cov = {c["code"]: (c["numerateur"], c["denominateur"]) for c in V._couvertures(rows)}
        self.assertEqual(cov["conso_mesuree"], (1, 2))
        self.assertEqual(cov["runtime"], (2, 2))
        self.assertEqual(cov["puissance"], (1, 2))
        self.assertEqual(cov["cph"], (1, 2))
        self.assertEqual(cov["rapprochement"], (0, 1))
        diag = {d["code"]: d for d in V._diagnostic_blocages(rows)}
        self.assertEqual(diag[E.PR_PDC_GE_INDISPONIBLE]["sites"], 1)
        self.assertEqual(diag[E.PR_P_DSE_INDISPONIBLE]["runtime_h"], 4.0)
        self.assertEqual(diag[E.SR_STOCK_ABSENT]["sites"], 2)
        self.assertEqual([r["site_id"] for r in V._apply_table_filters(rows, {"diag": E.PR_PDC_GE_INDISPONIBLE})], ["B"])
        # La trace à plat expose chaque méthode tentée et les motifs de rejet.
        flat = V._flat_trace(rows[1]["daily"][0])
        self.assertTrue(flat["direct_dse_attempted"] and flat["pdc_rectifier_attempted"])
        self.assertIsNone(flat["selected_power_method"])
        self.assertEqual(len(flat["rejection_reasons"]), 2)
