from datetime import date, datetime, timedelta

from django.test import SimpleTestCase

from fuel_tracking.services import cph_power_v2 as P

S = "DKR_0001"
KVA = 33.0                       # plafond = 1,05 × 33 × 0,8 = 27,72 kW ; nominal actif = 26,4 kW


def dc(day, n, p_dc=20.0, eff=0.95, site=S, start_hour=0):
    return [P.Sample(site, datetime(day.year, day.month, day.day, start_hour) + timedelta(minutes=5 * i), p_dc_kw=p_dc, efficiency=eff) for i in range(n)]


def grid(day, n, p, site=S):
    return [P.Sample(site, datetime(day.year, day.month, day.day) + timedelta(minutes=5 * i), p_grid_kw=p) for i in range(n)]


class Power2Tests(SimpleTestCase):
    spec = P.SiteSpec(S, P.OUTDOOR, KVA)

    def test_outdoor_formula_and_day_median(self):
        d = date(2026, 10, 5)
        r = P.resolve_power(self.spec, dc(d, 6, 19.0, 0.95), d, d, d)
        self.assertEqual(r.source, P.SRC_JOUR)
        self.assertAlmostEqual(r.p_ge_kw, 19.0 / 0.95)
        self.assertAlmostEqual(r.taux_charge, (19.0 / 0.95) / (KVA * 0.8))
        self.assertEqual((r.n_mesures, r.jours_distincts), (6, 1))

    def test_month_median_when_day_insufficient(self):
        d = date(2026, 10, 12)
        samples = dc(date(2026, 10, 3), 12, 20.0) + dc(date(2026, 10, 4), 12, 20.0) + dc(date(2026, 10, 5), 12, 20.0) + dc(d, 2, 20.0)
        r = P.resolve_power(self.spec, samples, d, date(2026, 10, 1), d)
        self.assertEqual((r.source, r.n_mesures, r.jours_distincts), (P.SRC_MOIS, 38, 4))
        self.assertEqual([t["niveau"] for t in r.tentatives], [P.SRC_JOUR])

    def test_period_median_when_month_insufficient_spans_months(self):
        start, end = date(2026, 9, 25), date(2026, 10, 3)
        samples = dc(date(2026, 9, 26), 12, 20.0) + dc(date(2026, 9, 27), 12, 20.0) + dc(date(2026, 9, 28), 12, 20.0)
        r = P.resolve_power(self.spec, samples, date(2026, 10, 2), start, end)     # le mois d'octobre n'a aucune mesure
        self.assertEqual(r.source, P.SRC_PERIODE)
        self.assertEqual([t["niveau"] for t in r.tentatives], [P.SRC_JOUR, P.SRC_MOIS])

    def test_unavailable_is_null_never_zero(self):
        d = date(2026, 10, 5)
        r = P.resolve_power(self.spec, dc(d, 3), d, d, d)
        self.assertEqual((r.p_ge_kw, r.source, r.motif), (None, None, P.MOTIF_INDISPONIBLE))

    def test_never_uses_future_measures(self):
        start, end = date(2026, 10, 1), date(2026, 10, 12)
        future = dc(date(2026, 10, 13), 40, 20.0) + dc(date(2026, 10, 14), 40, 20.0) + dc(date(2026, 10, 15), 40, 20.0)
        r = P.resolve_power(self.spec, future, date(2026, 10, 12), start, end)
        self.assertIsNone(r.p_ge_kw)                                  # 120 mesures futures ignorées
        self.assertEqual(r.motif, P.MOTIF_INDISPONIBLE)

    def test_other_site_is_refused(self):
        d = date(2026, 10, 5)
        with self.assertRaises(ValueError):
            P.resolve_power(self.spec, dc(d, 6, site="DKR_0002"), d, d, d)

    def test_cap_105_percent_rejects_samples_and_result(self):
        d = date(2026, 10, 5)
        r = P.resolve_power(self.spec, dc(d, 10, 30.0, 0.95), d, d, d)       # 31,6 kW > 27,72 kW
        self.assertIsNone(r.p_ge_kw)
        self.assertEqual(r.rejets.get("PUISSANCE_AU_DELA_105_PCT"), 10)

    def test_negative_and_invalid_efficiency_rejected(self):
        d = date(2026, 10, 5)
        bad = dc(d, 3, -5.0) + dc(d, 3, 20.0, 1.5) + dc(d, 6, 20.0, 0.95)
        r = P.resolve_power(self.spec, bad, d, d, d)
        self.assertEqual((r.rejets["P_DC_NEGATIVE"], r.rejets["RENDEMENT_INVALIDE"], r.n_mesures), (3, 3, 6))

    def test_indoor_without_solar_uses_grid_only(self):
        spec = P.SiteSpec(S, P.INDOOR, KVA)
        d = date(2026, 10, 5)
        r = P.resolve_power(spec, grid(d, 6, 18.0) + dc(d, 6, 99.0), d, d, d)   # P_DC ignoré : jamais ajouté à la puissance Grid
        self.assertAlmostEqual(r.p_ge_kw, 18.0)

    def test_indoor_solar_formula(self):
        spec = P.SiteSpec(S, P.INDOOR_SOLAR, KVA)
        d = date(2026, 10, 5)
        # P_DC_AC = 10/0,9 = 11,11 ; P_GRID = 20 ; charge aux = 8,89 ; P_GE = 20
        r = P.resolve_power(spec, dc(d, 6, 10.0, 0.9) + grid(d, 6, 20.0), d, d, d)
        self.assertAlmostEqual(r.p_ge_kw, 20.0)
        # Grid inférieur au DC équivalent : charge auxiliaire bornée à 0 → P_GE = P_DC_AC
        r2 = P.resolve_power(spec, dc(d, 6, 18.0, 0.9) + grid(d, 6, 15.0), d, d, d)
        self.assertAlmostEqual(r2.p_ge_kw, 18.0 / 0.9)

    def test_indoor_solar_needs_both_series(self):
        spec = P.SiteSpec(S, P.INDOOR_SOLAR, KVA)
        d = date(2026, 10, 5)
        r = P.resolve_power(spec, dc(d, 40, 10.0, 0.9), d, d, d)
        self.assertIsNone(r.p_ge_kw)

    def test_nominal_power_unknown_blocks_and_is_never_deduced(self):
        d = date(2026, 10, 5)
        r = P.resolve_power(P.SiteSpec(S, P.OUTDOOR, None), dc(d, 6), d, d, d)
        self.assertEqual((r.p_ge_kw, r.motif), (None, "PUISSANCE_NOMINALE_GE_INCONNUE"))

    def test_percentiles_and_dispersion_flag(self):
        d = date(2026, 10, 5)
        samples = [P.Sample(S, datetime(2026, 10, 5, 0, 5 * i), p_dc_kw=v, efficiency=1.0) for i, v in enumerate([5, 6, 10, 11, 12, 25])]
        r = P.resolve_power(self.spec, samples, d, d, d)
        self.assertEqual(r.source, P.SRC_JOUR)
        self.assertTrue(r.p10_kw < r.p50_kw < r.p90_kw)
        self.assertTrue(r.dispersion_excessive)                        # P90/P10 > 2 : signalé, pas masqué
