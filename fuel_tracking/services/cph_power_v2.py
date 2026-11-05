"""
Puissance GE v2 et cascade de médianes (cahier des charges §8.2 et §8.3) — module PUR (aucune écriture, aucun accès Snowflake/ENOC).

Principes :
  - la puissance GE ne vient JAMAIS de DSE, ni de la production GE, ni d'un libellé de modèle ; la puissance nominale (kVA) vient du
    référentiel validé et est fournie par l'appelant ;
  - outdoor            : P_GE = P_DC / rendement
  - indoor sans solaire: P_GE = puissance Grid mesurée (sans ajouter P_DC ni charges auxiliaires)
  - indoor Grid solaire: P_DC_AC = P_DC / rendement ; charge_aux = MAX(P_GRID − P_DC_AC, 0) ; P_GE = P_DC_AC + charge_aux
  - contrôle : P_GE ≤ 105 % × kVA × 0,8, sinon rejet avec motif ;
  - cascade : médiane du JOUR → du MOIS → de la PÉRIODE sélectionnée → INDISPONIBLE (NULL, jamais 0) ;
  - uniquement les mesures du MÊME site et jamais postérieures à la fin de la période sélectionnée.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from statistics import median

NOMINAL_PF = 0.8
CAP_RATIO = 1.05

OUTDOOR, INDOOR, INDOOR_SOLAR = "OUTDOOR", "INDOOR", "INDOOR_SOLAIRE"
SRC_JOUR, SRC_MOIS, SRC_PERIODE = "MEDIANE_JOUR", "MEDIANE_MOIS", "MEDIANE_PERIODE_SELECTIONNEE"
MOTIF_INDISPONIBLE = "PUISSANCE_GE_INDISPONIBLE"


@dataclass(frozen=True)
class Thresholds:
    """Seuils configurables (valeurs proposées par le cahier des charges)."""
    min_day: int = 6
    min_month: int = 30
    min_month_days: int = 3
    min_period: int = 30
    min_period_days: int = 3
    dispersion_ratio: float = 2.0       # P90 / P10 au-delà : dispersion excessive signalée (jamais un rejet silencieux)


@dataclass(frozen=True)
class Sample:
    site_id: str
    at: datetime | date
    p_dc_kw: float | None = None
    efficiency: float | None = None      # rendement redresseur en ratio (0, 1]
    p_grid_kw: float | None = None       # puissance Grid mesurée, kW

    @property
    def day(self) -> date:
        return self.at.date() if isinstance(self.at, datetime) else self.at


@dataclass
class SiteSpec:
    site_id: str
    typology: str                         # OUTDOOR | INDOOR | INDOOR_SOLAIRE
    nominal_kva: float | None             # référentiel validé uniquement


@dataclass
class PowerResult:
    p_ge_kw: float | None
    source: str | None
    motif: str | None
    n_mesures: int = 0
    jours_distincts: int = 0
    p10_kw: float | None = None
    p50_kw: float | None = None
    p90_kw: float | None = None
    taux_charge: float | None = None
    dispersion_excessive: bool = False
    rejets: dict = field(default_factory=dict)
    tentatives: list = field(default_factory=list)   # motifs des niveaux essayés avant le niveau retenu


def pct(values: list[float], q: float) -> float | None:
    """Percentile q (0-100), interpolation linéaire. None si aucune valeur."""
    if not values:
        return None
    v = sorted(values)
    if len(v) == 1:
        return v[0]
    k = (len(v) - 1) * q / 100.0
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def cap_kw(kva: float | None) -> float | None:
    return None if kva is None or kva <= 0 else CAP_RATIO * kva * NOMINAL_PF


def _clean(samples: list[Sample], spec: SiteSpec, rejets: dict):
    """Rejette les mesures impossibles ; renvoie (séries_dc_ac, séries_grid) listes de (Sample, valeur kW). Aucun 0 inventé."""
    dc_ac, grid = [], []
    cap = cap_kw(spec.nominal_kva)
    for s in samples:
        if s.site_id != spec.site_id:
            raise ValueError(f"mesure d'un autre site ({s.site_id} ≠ {spec.site_id}) : interdit")
        if s.p_dc_kw is not None:
            if s.p_dc_kw < 0:
                rejets["P_DC_NEGATIVE"] = rejets.get("P_DC_NEGATIVE", 0) + 1
            elif s.efficiency is None or not (0 < s.efficiency <= 1):
                rejets["RENDEMENT_INVALIDE"] = rejets.get("RENDEMENT_INVALIDE", 0) + 1
            else:
                v = s.p_dc_kw / s.efficiency
                if cap is not None and v > cap:
                    rejets["PUISSANCE_AU_DELA_105_PCT"] = rejets.get("PUISSANCE_AU_DELA_105_PCT", 0) + 1
                else:
                    dc_ac.append((s, v))
        if s.p_grid_kw is not None:
            if s.p_grid_kw < 0:
                rejets["P_GRID_NEGATIVE"] = rejets.get("P_GRID_NEGATIVE", 0) + 1
            elif cap is not None and s.p_grid_kw > cap:
                rejets["PUISSANCE_AU_DELA_105_PCT"] = rejets.get("PUISSANCE_AU_DELA_105_PCT", 0) + 1
            else:
                grid.append((s, s.p_grid_kw))
    return dc_ac, grid


def _aggregate(samples: list[Sample], spec: SiteSpec):
    """(p_ge_kw | None, n_mesures, jours, valeurs_pour_percentiles, rejets, motif). Formules du §8.3 niveau 1, appliquées à n'importe quel lot de mesures."""
    rejets: dict = {}
    dc_ac, grid = _clean(samples, spec, rejets)
    if spec.typology == OUTDOOR:
        vals = [v for _s, v in dc_ac]
        used = [s for s, _v in dc_ac]
        p = median(vals) if vals else None
        return p, len(vals), len({s.day for s in used}), vals, rejets, None if vals else "AUCUNE_MESURE_P_DC_QUALIFIEE"
    if spec.typology == INDOOR:
        vals = [v for _s, v in grid]
        used = [s for s, _v in grid]
        p = median(vals) if vals else None
        return p, len(vals), len({s.day for s in used}), vals, rejets, None if vals else "AUCUNE_MESURE_GRID_QUALIFIEE"
    if spec.typology == INDOOR_SOLAR_KEY:
        if not dc_ac or not grid:
            return None, min(len(dc_ac), len(grid)), 0, [], rejets, "MESURES_P_DC_ET_GRID_REQUISES"
        p_dc_ac = median(v for _s, v in dc_ac)
        p_grid = median(v for _s, v in grid)
        p = p_dc_ac + max(p_grid - p_dc_ac, 0.0)
        n = min(len(dc_ac), len(grid))
        days = len({s.day for s, _v in dc_ac} & {s.day for s, _v in grid})
        return p, n, days, [v for _s, v in grid], rejets, None
    return None, 0, 0, [], rejets, "TYPOLOGIE_NON_SUPPORTEE"


INDOOR_SOLAR_KEY = INDOOR_SOLAR


def _result(p, source, n, days, vals, spec, th, rejets, tentatives) -> PowerResult:
    cap = cap_kw(spec.nominal_kva)
    if p is not None and cap is not None and p > cap:
        return PowerResult(None, None, "PUISSANCE_AU_DELA_105_PCT", n, days, rejets=rejets, tentatives=tentatives)
    p10, p50, p90 = pct(vals, 10), pct(vals, 50), pct(vals, 90)
    nominal_active = spec.nominal_kva * NOMINAL_PF if spec.nominal_kva else None
    return PowerResult(
        p_ge_kw=p, source=source, motif=None, n_mesures=n, jours_distincts=days, p10_kw=p10, p50_kw=p50, p90_kw=p90,
        taux_charge=(p / nominal_active) if (p is not None and nominal_active) else None,
        dispersion_excessive=bool(p10 and p90 and p10 > 0 and p90 / p10 > th.dispersion_ratio), rejets=rejets, tentatives=tentatives)


def resolve_power(spec: SiteSpec, samples: list[Sample], day: date, period_start: date, period_end: date, th: Thresholds | None = None) -> PowerResult:
    """Puissance GE retenue pour `day` : médiane du jour, sinon du mois, sinon de la période, sinon INDISPONIBLE (NULL).
    Les mesures postérieures à `period_end` ou antérieures au début du mois / de la période sont ignorées ; celles d'un autre site lèvent une erreur."""
    th = th or Thresholds()
    if spec.nominal_kva is None or spec.nominal_kva <= 0:
        return PowerResult(None, None, "PUISSANCE_NOMINALE_GE_INCONNUE")
    pool = [s for s in samples if s.day <= period_end]          # jamais de donnée future
    tent: list = []

    # Niveau 1 — médiane du jour
    day_s = [s for s in pool if s.day == day]
    p, n, days, vals, rej, why = _aggregate(day_s, spec)
    if p is not None and n >= th.min_day:
        return _result(p, SRC_JOUR, n, days, vals, spec, th, rej, tent)
    tent.append({"niveau": SRC_JOUR, "n_mesures": n, "motif": why or f"{n} mesure(s) < {th.min_day}"})

    # Niveau 2 — médiane du mois civil (même site, jusqu'à la fin de période)
    month_s = [s for s in pool if (s.day.year, s.day.month) == (day.year, day.month)]
    p, n, days, vals, rej, why = _aggregate(month_s, spec)
    if p is not None and n >= th.min_month and days >= th.min_month_days:
        return _result(p, SRC_MOIS, n, days, vals, spec, th, rej, tent)
    tent.append({"niveau": SRC_MOIS, "n_mesures": n, "jours_distincts": days, "motif": why or f"{n} mesure(s) / {days} jour(s) < {th.min_month} / {th.min_month_days}"})

    # Niveau 3 — médiane de la période exacte sélectionnée
    per_s = [s for s in pool if period_start <= s.day <= period_end]
    p, n, days, vals, rej, why = _aggregate(per_s, spec)
    if p is not None and n >= th.min_period and days >= th.min_period_days:
        return _result(p, SRC_PERIODE, n, days, vals, spec, th, rej, tent)
    tent.append({"niveau": SRC_PERIODE, "n_mesures": n, "jours_distincts": days, "motif": why or f"{n} mesure(s) / {days} jour(s) < {th.min_period} / {th.min_period_days}"})

    # Niveau 4 — indisponible : NULL, jamais 0
    return PowerResult(None, None, MOTIF_INDISPONIBLE, 0, 0, rejets=rej, tentatives=tent)
