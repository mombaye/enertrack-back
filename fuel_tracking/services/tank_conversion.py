# fuel_tracking/services/tank_conversion.py
"""
Conversion d'un relevé de niveau de cuve en litres — AUCUN coefficient générique.

Ordre de priorité (cuve VALIDÉE uniquement, fuel_tank_master.validation_status = VALIDATED) :
  1. abaque (courbe de jaugeage) validé  → TANK_CURVE (interpolation linéaire entre deux points) ;
  2. géométrie avec dimensions validées   → GEOMETRIC_CALCULATION ;
  3. sinon                                → level_liters = NULL, PENDING_TANK_MAPPING.

Un relevé déjà en litres est repris tel quel (DIRECT_LITERS) ; il reste contrôlé contre la capacité
de la cuve validée.

Contrôles : niveau négatif, niveau au-dessus de la hauteur physique, volume au-dessus de la capacité
(tolérance configurable FUEL_CAPACITY_TOLERANCE_PCT), dimensions manquantes, cuve non validée,
plusieurs cuves sur le site sans tank_id (jamais d'attribution automatique à la cuve 1).

Formules (dimensions en mètres, niveau h en mètres, volume en litres = m³ × 1000) :
  - parallélépipède        : V = L × l × h
  - cylindre vertical      : V = π × (D/2)² × h
  - cylindre horizontal    : V = L × [r² · acos((r−h)/r) − (r−h) · √(2rh − h²)]   (segment circulaire)
  - cylindre elliptique horizontal (largeur l = grand axe horizontal, hauteur H = axe vertical) :
                             V = (l/H) × volume du cylindre horizontal de diamètre H au même niveau
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

VALID = "VALID"
PENDING_TANK_MAPPING = "PENDING_TANK_MAPPING"
INVALID = "INVALID"

DIRECT_LITERS = "DIRECT_LITERS"
TANK_CURVE = "TANK_CURVE"
GEOMETRIC_CALCULATION = "GEOMETRIC_CALCULATION"
NONE = "NONE"

HORIZONTAL_CYLINDER = "HORIZONTAL_CYLINDER"
VERTICAL_CYLINDER = "VERTICAL_CYLINDER"
RECTANGULAR = "RECTANGULAR"
HORIZONTAL_ELLIPTICAL = "HORIZONTAL_ELLIPTICAL"
CUSTOM_CURVE = "CUSTOM_CURVE"


@dataclass
class TankSpec:
    """Vue minimale d'une cuve (indépendante de l'ORM pour être testable)."""

    site_id: str
    tank_id: str
    shape: str | None = None
    length_m: float | None = None
    width_m: float | None = None
    height_m: float | None = None
    diameter_m: float | None = None
    capacity_l: float | None = None
    calibration_curve: list | None = None
    calibration_validated: bool = False
    validated: bool = False

    @classmethod
    def from_model(cls, t) -> "TankSpec":
        f = lambda v: float(v) if v is not None else None  # noqa: E731
        return cls(
            site_id=t.site_id, tank_id=t.tank_id, shape=t.shape, length_m=f(t.length_m), width_m=f(t.width_m),
            height_m=f(t.height_m), diameter_m=f(t.diameter_m), capacity_l=f(t.capacity_l),
            calibration_curve=t.calibration_curve, calibration_validated=bool(t.calibration_validated),
            validated=t.validation_status == "VALIDATED" and t.active,
        )

    def physical_height_cm(self) -> float | None:
        if self.shape in (HORIZONTAL_CYLINDER,):
            return self.diameter_m * 100 if self.diameter_m else None
        if self.shape in (VERTICAL_CYLINDER, RECTANGULAR, HORIZONTAL_ELLIPTICAL):
            return self.height_m * 100 if self.height_m else None
        if self.calibration_curve:
            try:
                return max(float(p[0]) for p in self.calibration_curve)
            except (TypeError, ValueError, IndexError):
                return None
        return None

    def geometric_capacity_l(self) -> float | None:
        h = self.physical_height_cm()
        if h is None:
            return None
        try:
            return geometric_volume_l(self, h)
        except MissingDimensions:
            return None


@dataclass
class ConversionResult:
    level_liters: float | None
    method: str
    status: str
    messages: list[dict] = field(default_factory=list)
    capacity_l: float | None = None
    tank_id: str | None = None

    def add(self, code: str, message: str):
        self.messages.append({"code": code, "message": message})
        return self


class MissingDimensions(Exception):
    pass


def _need(*values):
    if any(v is None or v <= 0 for v in values):
        raise MissingDimensions()


def _segment_area(r: float, h: float) -> float:
    """Aire du segment circulaire de rayon r rempli sur une hauteur h (m²)."""
    h = min(max(h, 0.0), 2 * r)
    return r * r * math.acos((r - h) / r) - (r - h) * math.sqrt(max(2 * r * h - h * h, 0.0))


def geometric_volume_l(tank: TankSpec, level_cm: float) -> float:
    h = level_cm / 100.0
    s = tank.shape
    if s == RECTANGULAR:
        _need(tank.length_m, tank.width_m, tank.height_m)
        return tank.length_m * tank.width_m * h * 1000.0
    if s == VERTICAL_CYLINDER:
        _need(tank.diameter_m, tank.height_m)
        return math.pi * (tank.diameter_m / 2) ** 2 * h * 1000.0
    if s == HORIZONTAL_CYLINDER:
        _need(tank.length_m, tank.diameter_m)
        return tank.length_m * _segment_area(tank.diameter_m / 2, h) * 1000.0
    if s == HORIZONTAL_ELLIPTICAL:
        _need(tank.length_m, tank.width_m, tank.height_m)
        b = tank.height_m / 2
        return (tank.width_m / tank.height_m) * tank.length_m * _segment_area(b, h) * 1000.0
    raise MissingDimensions()


def curve_volume_l(curve: list, level_cm: float) -> float | None:
    """Interpolation linéaire sur l'abaque ; None si hors plage ou abaque incohérent (non croissant)."""
    try:
        pts = sorted((float(c), float(v)) for c, v in curve)
    except (TypeError, ValueError):
        return None
    if len(pts) < 2 or any(b[0] <= a[0] or b[1] < a[1] for a, b in zip(pts, pts[1:])):
        return None
    if level_cm < pts[0][0] or level_cm > pts[-1][0]:
        return None
    for (c0, v0), (c1, v1) in zip(pts, pts[1:]):
        if c0 <= level_cm <= c1:
            return v0 + (v1 - v0) * (level_cm - c0) / (c1 - c0)
    return None


def _capacity(tank: TankSpec) -> float | None:
    return tank.capacity_l if tank.capacity_l and tank.capacity_l > 0 else tank.geometric_capacity_l()


def select_tank(site_tanks: list[TankSpec], tank_id: str | None) -> tuple[TankSpec | None, dict | None]:
    """Cuve visée par le relevé ; jamais d'attribution automatique quand le site a plusieurs cuves."""
    active = [t for t in site_tanks]
    if tank_id:
        for t in active:
            if str(t.tank_id).strip().upper() == str(tank_id).strip().upper():
                return t, None
        return None, {"code": "TANK_UNKNOWN", "message": f"Cuve « {tank_id} » absente du référentiel cuves du site."}
    if len(active) > 1:
        return None, {"code": "MULTI_TANK_WITHOUT_ID",
                      "message": f"Le site a {len(active)} cuves et le relevé ne précise pas laquelle : aucune attribution automatique."}
    if len(active) == 1:
        return active[0], None
    return None, {"code": "TANK_NOT_REFERENCED", "message": "Aucune cuve référencée pour ce site."}


def convert(level_value: float | None, unit: str | None, site_tanks: list[TankSpec], tank_id: str | None = None,
            tolerance_pct: float = 5.0) -> ConversionResult:
    if level_value is None:
        return ConversionResult(None, NONE, INVALID).add("MISSING_LEVEL", "Niveau absent.")
    if unit not in ("L", "CM"):
        return ConversionResult(None, NONE, INVALID).add("UNKNOWN_UNIT", f"Unité inconnue : {unit!r}.")
    if level_value < 0:
        return ConversionResult(None, NONE, INVALID).add("NEGATIVE_LEVEL", f"Niveau négatif ({level_value}).")

    tank, problem = select_tank(site_tanks, tank_id)

    if unit == "L":
        res = ConversionResult(float(level_value), DIRECT_LITERS, VALID, tank_id=tank.tank_id if tank else tank_id)
        if problem and problem["code"] == "MULTI_TANK_WITHOUT_ID":
            res.status = PENDING_TANK_MAPPING
            res.messages.append(problem)
            return res
        if tank is None or not tank.validated:
            res.add("CAPACITY_NOT_VALIDATED", "Capacité de cuve non validée : contrôle de dépassement impossible.")
            return res
        cap = _capacity(tank)
        res.capacity_l = cap
        if cap is None:
            res.add("CAPACITY_NOT_VALIDATED", "Capacité de cuve inconnue : contrôle de dépassement impossible.")
        elif level_value > cap * (1 + tolerance_pct / 100):
            res.status = INVALID
            res.add("OVER_CAPACITY", f"{level_value:.0f} L > capacité {cap:.0f} L (+{tolerance_pct:g} %).")
        return res

    # Relevé en cm : il faut une cuve validée.
    res = ConversionResult(None, NONE, PENDING_TANK_MAPPING, tank_id=tank.tank_id if tank else tank_id)
    if problem:
        res.messages.append(problem)
        return res
    if not tank.validated:
        return res.add("TANK_NOT_VALIDATED", f"Cuve {tank.tank_id} non validée : conversion cm → L interdite.")
    height = tank.physical_height_cm()
    if height is not None and level_value > height:
        res.status = INVALID
        return res.add("LEVEL_ABOVE_HEIGHT", f"Niveau {level_value:g} cm > hauteur physique {height:g} cm.")

    liters = None
    if tank.calibration_curve and tank.calibration_validated:
        liters = curve_volume_l(tank.calibration_curve, float(level_value))
        if liters is not None:
            res.method = TANK_CURVE
        else:
            res.add("CURVE_OUT_OF_RANGE", "Niveau hors de l'abaque (ou abaque incohérent) : calcul géométrique tenté.")
    if liters is None:
        try:
            liters = geometric_volume_l(tank, float(level_value))
            res.method = GEOMETRIC_CALCULATION
        except MissingDimensions:
            return res.add("MISSING_DIMENSIONS", f"Dimensions de la cuve {tank.tank_id} incomplètes pour sa forme ({tank.shape or 'forme inconnue'}).")

    cap = _capacity(tank)
    res.capacity_l = cap
    res.level_liters = round(liters, 3)
    res.status = VALID
    if cap is not None and liters > cap * (1 + tolerance_pct / 100):
        res.status = INVALID
        res.add("OVER_CAPACITY", f"{liters:.0f} L > capacité {cap:.0f} L (+{tolerance_pct:g} %).")
    return res


def to_decimal(v) -> Decimal | None:
    return None if v is None else Decimal(str(round(float(v), 3)))
