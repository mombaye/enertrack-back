"""
Tests de la priorité de source Running Time CPH (spec 2026-09) — couvrent :
  - les 8 règles de _resolve_business_runtime (fuel_cph_snowflake.py) ;
  - compute_daily_status (fuel_cph_service.py) sur les 2 chemins
    d'intégration énergie (Path A tracker actif / Path B GENSET_REPORT seul)
    et les statuts spéciaux (DSE=0 confirmé/conflit) ;
  - la reclassification NOT_APPLICABLE_NO_GE (compute_monthly_cph_estimates)
    pour un site sans GE (Postgres has_genset=False).

Aucun de ces tests ne touche Snowflake ni Postgres : fetch_daily_tracker_energy/
_load_active_parameters/fetch_site_ge_specs/fetch_site_rectifier_efficiency
sont mockés là où nécessaire, FuelCphGeParameter est instancié SANS .save().
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.test import SimpleTestCase

from fuel_tracking.models import FuelCphGeParameter
from fuel_tracking.services.fuel_cph_service import compute_daily_status, compute_monthly_cph_estimates
from fuel_tracking.services.fuel_cph_snowflake import (
    RUNTIME_DG_ON_CALCULATED,
    RUNTIME_DSE_CONTROLLER,
    RUNTIME_DSE_ZERO_CONFIRMED,
    RUNTIME_DSE_ZERO_CONFLICT,
    RUNTIME_NO_VALID,
    RUNTIME_RECTIFIER_5MIN,
    RUNTIME_TRACKER_5MIN,
    _resolve_business_runtime,
)


class ResolveBusinessRuntimeTests(SimpleTestCase):
    """Les 8 règles exactes de la spec 2026-09 (remplace DSE > DG-On > Redresseur > Tracker)."""

    def test_dse_positive_wins_regardless_of_other_sources(self):
        runtime_h, source, status, reason = _resolve_business_runtime(
            dse_h=Decimal("5"), dg_on_h=Decimal("2"), rectifier_h=Decimal("1"),
            is_hybrid_solar_ge=False, tracker_h=Decimal("3"),
        )
        self.assertEqual(runtime_h, Decimal("5"))
        self.assertEqual(source, RUNTIME_DSE_CONTROLLER)
        self.assertEqual(status, RUNTIME_DSE_CONTROLLER)
        self.assertIsNone(reason)

    def test_dse_zero_confirmed_when_no_other_source_positive(self):
        runtime_h, source, status, reason = _resolve_business_runtime(
            dse_h=Decimal("0"), dg_on_h=None, rectifier_h=None, is_hybrid_solar_ge=False, tracker_h=None,
        )
        self.assertEqual(runtime_h, Decimal("0"))
        self.assertIsNone(source)  # aucune source physique gagnante pour un 0h confirmé
        self.assertEqual(status, RUNTIME_DSE_ZERO_CONFIRMED)
        self.assertIsNone(reason)

    def test_dse_zero_conflicts_with_positive_tracker(self):
        runtime_h, source, status, reason = _resolve_business_runtime(
            dse_h=Decimal("0"), dg_on_h=None, rectifier_h=None, is_hybrid_solar_ge=False, tracker_h=Decimal("2.5"),
        )
        self.assertIsNone(runtime_h)  # aucun repli automatique
        self.assertIsNone(source)
        self.assertEqual(status, RUNTIME_DSE_ZERO_CONFLICT)
        self.assertIsNotNone(reason)

    def test_dse_zero_conflicts_with_positive_dg_on(self):
        _, _, status, _ = _resolve_business_runtime(
            dse_h=Decimal("0"), dg_on_h=Decimal("4"), rectifier_h=None, is_hybrid_solar_ge=False, tracker_h=None,
        )
        self.assertEqual(status, RUNTIME_DSE_ZERO_CONFLICT)

    def test_dse_absent_falls_back_to_tracker_before_dg_on(self):
        """Corrige l'audit 2026-09 : le tracker doit primer sur DG-On/Redresseur quand le DSE est absent."""
        runtime_h, source, status, reason = _resolve_business_runtime(
            dse_h=None, dg_on_h=Decimal("6"), rectifier_h=Decimal("6"), is_hybrid_solar_ge=False, tracker_h=Decimal("2.5"),
        )
        self.assertEqual(runtime_h, Decimal("2.5"))
        self.assertEqual(source, RUNTIME_TRACKER_5MIN)
        self.assertEqual(status, RUNTIME_TRACKER_5MIN)
        self.assertIsNone(reason)

    def test_dse_and_tracker_absent_non_hybrid_uses_dg_on(self):
        runtime_h, source, status, reason = _resolve_business_runtime(
            dse_h=None, dg_on_h=Decimal("4"), rectifier_h=Decimal("6"), is_hybrid_solar_ge=False, tracker_h=None,
        )
        self.assertEqual(runtime_h, Decimal("4"))
        self.assertEqual(source, RUNTIME_DG_ON_CALCULATED)
        self.assertEqual(status, RUNTIME_DG_ON_CALCULATED)

    def test_dse_and_tracker_absent_hybrid_uses_rectifier(self):
        runtime_h, source, status, reason = _resolve_business_runtime(
            dse_h=None, dg_on_h=Decimal("4"), rectifier_h=Decimal("6"), is_hybrid_solar_ge=True, tracker_h=None,
        )
        self.assertEqual(runtime_h, Decimal("6"))
        self.assertEqual(source, RUNTIME_RECTIFIER_5MIN)
        self.assertEqual(status, RUNTIME_RECTIFIER_5MIN)

    def test_no_source_at_all(self):
        runtime_h, source, status, reason = _resolve_business_runtime(
            dse_h=None, dg_on_h=None, rectifier_h=None, is_hybrid_solar_ge=False, tracker_h=None,
        )
        self.assertIsNone(runtime_h)
        self.assertIsNone(source)
        self.assertEqual(status, RUNTIME_NO_VALID)
        self.assertIsNotNone(reason)


class ComputeDailyStatusTests(SimpleTestCase):
    """Path A (tracker actif) vs Path B (GENSET_REPORT + LOAD_REPORT, sans intervalle tracker)."""

    def _params(self, **overrides):
        defaults = dict(spc_l_per_kwh=Decimal("0.3"), rectifier_efficiency_ratio=Decimal("0.9"), pge_kva=None, power_factor=None)
        defaults.update(overrides)
        return FuelCphGeParameter(**defaults)

    def _energies(self, **overrides):
        base = {
            "ge_intervals": 0, "valid_battery_intervals": 0,
            "dg_runtime_interval_h": None, "dg_runtime_controller_h": None,
            "dg_runtime_business_h": None, "dg_runtime_business_source": None,
            "dg_runtime_business_status": None, "dg_runtime_business_rejection_reason": None,
            "site_load_energy_kwh": None, "battery_dc_energy_kwh": None, "load_kw": None,
        }
        base.update(overrides)
        return base

    def test_dse_zero_confirmed_is_ok_zero_litres_without_params(self):
        energies = self._energies(dg_runtime_business_h=Decimal("0"), dg_runtime_business_status=RUNTIME_DSE_ZERO_CONFIRMED)
        status, computed = compute_daily_status(energies, params=None)
        self.assertEqual(status, "OK")
        self.assertEqual(computed["estimated_consumption_l"], Decimal("0"))
        self.assertEqual(computed["cph_estimated_lph"], Decimal("0"))

    def test_dse_zero_conflict_short_circuits_without_computing_litres(self):
        energies = self._energies(
            ge_intervals=3, dg_runtime_interval_h=Decimal("2"), dg_runtime_controller_h=Decimal("0"),
            dg_runtime_business_status=RUNTIME_DSE_ZERO_CONFLICT, valid_battery_intervals=3,
            site_load_energy_kwh=Decimal("10"),
        )
        status, computed = compute_daily_status(energies, self._params())
        self.assertEqual(status, RUNTIME_DSE_ZERO_CONFLICT)
        self.assertIsNone(computed["estimated_consumption_l"])

    def test_no_valid_runtime_short_circuits(self):
        energies = self._energies(dg_runtime_business_status=RUNTIME_NO_VALID)
        status, computed = compute_daily_status(energies, self._params())
        self.assertEqual(status, RUNTIME_NO_VALID)
        self.assertIsNone(computed["estimated_consumption_l"])

    def test_path_a_tracker_active_ok(self):
        energies = self._energies(
            ge_intervals=5, valid_battery_intervals=5,
            dg_runtime_interval_h=Decimal("3"), dg_runtime_controller_h=Decimal("3"),
            dg_runtime_business_h=Decimal("3"), dg_runtime_business_source=RUNTIME_DSE_CONTROLLER,
            dg_runtime_business_status=RUNTIME_DSE_CONTROLLER,
            site_load_energy_kwh=Decimal("9"), battery_dc_energy_kwh=Decimal("0"),
        )
        status, computed = compute_daily_status(energies, self._params())
        self.assertEqual(status, "OK")
        self.assertEqual(computed["conso_estimee_source"], "CPH_TRACKER_5MIN")
        self.assertEqual(computed["estimated_consumption_l"], Decimal("2.7000"))  # 9 kWh * 0.3 L/kWh

    def test_path_a_missing_load_power_when_tracker_inactive_and_no_business_runtime(self):
        energies = self._energies()  # ge_intervals=0, aucune source résolue
        status, computed = compute_daily_status(energies, self._params())
        self.assertEqual(status, RUNTIME_NO_VALID)  # dg_runtime_business_status=None -> traité comme NO_VALID_RUNTIME

    def test_path_b_dg_on_without_tracker_uses_load_report(self):
        """Corrige le point 3 de l'audit : DG-On avec runtime mais MISSING_LOAD_POWER alors
        que la charge existe dans LOAD_REPORT — désormais utilisée via load_kw."""
        energies = self._energies(
            dg_runtime_business_h=Decimal("4"), dg_runtime_business_source=RUNTIME_DG_ON_CALCULATED,
            dg_runtime_business_status=RUNTIME_DG_ON_CALCULATED, load_kw=Decimal("2.5"),
        )
        status, computed = compute_daily_status(energies, self._params())
        self.assertEqual(status, "OK")
        self.assertEqual(computed["conso_estimee_source"], "CPH_GENSET_DAILY_AVG")
        self.assertEqual(computed["estimated_consumption_l"], Decimal("3.0000"))  # (2.5*4) kWh * 0.3 L/kWh

    def test_path_b_rectifier_without_tracker_uses_load_report(self):
        energies = self._energies(
            dg_runtime_business_h=Decimal("2"), dg_runtime_business_source=RUNTIME_RECTIFIER_5MIN,
            dg_runtime_business_status=RUNTIME_RECTIFIER_5MIN, load_kw=Decimal("5"),
        )
        status, computed = compute_daily_status(energies, self._params())
        self.assertEqual(status, "OK")
        self.assertEqual(computed["conso_estimee_source"], "CPH_GENSET_DAILY_AVG")

    def test_charge_absente_conserves_source_but_status_becomes_missing_load_power(self):
        """Spec 2026-09, point 3 : "si le runtime existe mais que load_kw est absent,
        conserver la source et retourner MISSING_LOAD_POWER" — la source déjà résolue
        (dg_runtime_business_source) n'est PAS effacée par compute_daily_status."""
        energies = self._energies(
            dg_runtime_business_h=Decimal("5"), dg_runtime_business_source=RUNTIME_DSE_CONTROLLER,
            dg_runtime_business_status=RUNTIME_DSE_CONTROLLER, load_kw=None,
        )
        status, computed = compute_daily_status(energies, self._params())
        self.assertEqual(status, "MISSING_LOAD_POWER")
        self.assertEqual(energies["dg_runtime_business_source"], RUNTIME_DSE_CONTROLLER)

    def test_missing_parameter_when_no_reference_sheet(self):
        energies = self._energies(
            dg_runtime_business_h=Decimal("4"), dg_runtime_business_source=RUNTIME_DG_ON_CALCULATED,
            dg_runtime_business_status=RUNTIME_DG_ON_CALCULATED, load_kw=Decimal("2.5"),
        )
        status, computed = compute_daily_status(energies, params=None)
        self.assertEqual(status, "MISSING_PARAMETER")


class ComputeMonthlyEstimatesNoGeTests(SimpleTestCase):
    """Site sans GE (Postgres has_genset=False) — reclassé NOT_APPLICABLE_NO_GE (spec 2026-09, règle 1)."""

    @patch("fuel_tracking.services.fuel_cph_service.fetch_site_rectifier_efficiency", return_value={})
    @patch("fuel_tracking.services.fuel_cph_service.fetch_site_ge_specs", return_value={})
    @patch("fuel_tracking.services.fuel_cph_service._load_active_parameters", return_value={})
    @patch("fuel_tracking.services.fuel_cph_service.fetch_daily_tracker_energy")
    def test_site_without_genset_marked_not_applicable(self, mock_fetch, *_mocks):
        mock_fetch.return_value = {
            "NOGE_0001": {
                date(2026, 9, 1): {
                    "country": "Senegal", "data_id": 1,
                    "ge_intervals": 5, "valid_battery_intervals": 5,
                    "dg_runtime_interval_h": Decimal("3"), "dg_runtime_controller_h": Decimal("3"),
                    "dg_runtime_business_h": Decimal("3"), "dg_runtime_business_source": RUNTIME_DSE_CONTROLLER,
                    "dg_runtime_business_status": RUNTIME_DSE_CONTROLLER, "dg_runtime_business_rejection_reason": None,
                    "site_load_energy_kwh": Decimal("10"), "battery_dc_energy_kwh": Decimal("1"), "load_kw": None,
                }
            }
        }
        result = compute_monthly_cph_estimates(2026, 9, site_ids=["NOGE_0001"], site_has_genset={"NOGE_0001": False})

        self.assertEqual(result["monthly"]["NOGE_0001"]["cph_calculation_status"], "NOT_APPLICABLE_NO_GE")
        self.assertIsNone(result["monthly"]["NOGE_0001"]["cph_runtime_h_total"])
        self.assertIsNone(result["monthly"]["NOGE_0001"]["cph_runtime_source"])
        self.assertEqual(len(result["daily"]), 1)
        self.assertEqual(result["daily"][0]["calculation_status"], "NOT_APPLICABLE_NO_GE")

    @patch("fuel_tracking.services.fuel_cph_service.fetch_site_rectifier_efficiency", return_value={})
    @patch("fuel_tracking.services.fuel_cph_service.fetch_site_ge_specs", return_value={})
    @patch("fuel_tracking.services.fuel_cph_service._load_active_parameters", return_value={})
    @patch("fuel_tracking.services.fuel_cph_service.fetch_daily_tracker_energy")
    def test_site_with_genset_untouched_by_override(self, mock_fetch, *_mocks):
        mock_fetch.return_value = {
            "GE_0001": {
                date(2026, 9, 1): {
                    "country": "Senegal", "data_id": 2,
                    "ge_intervals": 5, "valid_battery_intervals": 5,
                    "dg_runtime_interval_h": Decimal("3"), "dg_runtime_controller_h": Decimal("3"),
                    "dg_runtime_business_h": Decimal("3"), "dg_runtime_business_source": RUNTIME_DSE_CONTROLLER,
                    "dg_runtime_business_status": RUNTIME_DSE_CONTROLLER, "dg_runtime_business_rejection_reason": None,
                    "site_load_energy_kwh": Decimal("10"), "battery_dc_energy_kwh": Decimal("1"), "load_kw": None,
                }
            }
        }
        result = compute_monthly_cph_estimates(2026, 9, site_ids=["GE_0001"], site_has_genset={"GE_0001": True})
        self.assertNotEqual(result["monthly"]["GE_0001"]["cph_calculation_status"], "NOT_APPLICABLE_NO_GE")
        self.assertEqual(result["monthly"]["GE_0001"]["cph_runtime_source"], RUNTIME_DSE_CONTROLLER)
