# fuel_tracking/services/tank_conversion.py
"""
Conversion d'un relevé de niveau en litres — AUCUN coefficient générique, aucun « litres par cm ».

Un relevé en cm n'est converti que si le site a une cuve VALIDÉE (fuel_site_tank) avec une géométrie
reconnue par le calculateur (services/tank_geometry.py, version steve-v1) ou un abaque validé :

  1. abaque validé                         → volume retenu = abaque (TANK_CURVE)
  2. géométrie + capacité de plaque         → volume géométrique, k = plaque / géométrie pleine ;
     cohérente (écart ≤ 5 %)                   volume retenu = volume CALIBRÉ (CALIBRATED)
  3. géométrie sans plaque, ou plaque       → volume géométrique conservé comme valeur TECHNIQUE,
     incohérente (> 5 %)                       volume retenu = NULL (GEOMETRIC_ONLY) — jamais certifié
                                               automatiquement ; validation manuelle explicite possible
  4. sinon                                  → NULL, PENDING_TANK_MAPPING

Relevé déjà en litres : valeur native retenue (DIRECT_LITERS), contrôlée contre la capacité.
Contrôles : niveau négatif, au-dessus de la hauteur utile, volume > capacité + tolérance (5 %),
cuve non validée, plusieurs cuves sans tank_id (jamais d'attribution automatique à la cuve 1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from fuel_tracking.services import tank_geometry as G

VALID = "VALID"
GEOMETRIC_ONLY = "GEOMETRIC_ONLY"
PENDING_TANK_MAPPING = "PENDING_TANK_MAPPING"
INVALID = "INVALID"

DIRECT_LITERS = "DIRECT_LITERS"
TANK_CURVE = "TANK_CURVE"
GEOMETRIC_CALCULATION = "GEOMETRIC_CALCULATION"
CALIBRATED = "CALIBRATED"
NONE = "NONE"

HORIZONTAL_CYLINDER = "HORIZONTAL_CYLINDER"
VERTICAL_CYLINDER = "VERTICAL_CYLINDER"
RECTANGULAR = "RECTANGULAR"
HORIZONTAL_ELLIPTICAL = "HORIZONTAL_ELLIPTICAL"
CUSTOM_CURVE = "CUSTOM_CURVE"

SHAPE_TO_FAMILY = {HORIZONTAL_CYLINDER: "h-circ", VERTICAL_CYLINDER: "v-cyl", RECTANGULAR: "rect", HORIZONTAL_ELLIPTICAL: "h-elli"}
FAMILY_TO_SHAPE = {"h-circ": HORIZONTAL_CYLINDER, "h-elli": HORIZONTAL_ELLIPTICAL, "h-oblo": HORIZONTAL_ELLIPTICAL,
                   "v-cyl": VERTICAL_CYLINDER, "v-tronc": VERTICAL_CYLINDER, "rect": RECTANGULAR}
PLATE_TOLERANCE = 0.05


class MissingDimensions(Exception):
    pass


def _f(v):
    return float(v) if v is not None else None


@dataclass
class TankSpec:
    """Vue minimale d'une cuve site (indépendante de l'ORM pour être testable). Cotes en cm."""

    site_id: str
    tank_id: str
    family: str | None = None
    d1: float | None = None
    d2: float | None = None
    d3: float | None = None
    bottom: str | None = "plat"
    bz: float | None = None
    top: str | None = "plat"
    tz: float | None = None
    plate_capacity_l: float | None = None
    declared_capacity_l: float | None = None
    calibration_curve: list | None = None
    calibration_validated: bool = False
    validated: bool = False
    status: str | None = None
    model_ref: str | None = None

    @classmethod
    def from_model(cls, t) -> "TankSpec":
        fam = t.shape_family or SHAPE_TO_FAMILY.get(t.shape)
        d1, d2, d3 = _f(t.dimension_1_cm), _f(t.dimension_2_cm), _f(t.dimension_3_cm)
        if fam and d1 is None and d2 is None and d3 is None:  # cuve saisie en mètres (ancien référentiel)
            m = lambda v: _f(v) * 100 if v is not None else None  # noqa: E731
            if fam == "h-circ":
                d1, d3 = m(t.diameter_m), m(t.length_m)
            elif fam == "v-cyl":
                d1, d2 = m(t.diameter_m), m(t.height_m)
            else:
                d1, d2, d3 = m(t.width_m), m(t.height_m), m(t.length_m)
        model = getattr(t, "tank_model", None) if getattr(t, "tank_model_id", None) else None
        return cls(
            site_id=t.site_id, tank_id=t.tank_id, family=fam, d1=d1, d2=d2, d3=d3,
            bottom=t.bottom_profile or "plat", bz=_f(t.bottom_z_cm), top=t.top_profile or "plat", tz=_f(t.top_z_cm),
            plate_capacity_l=_f(t.capacity_plate_l), declared_capacity_l=_f(t.capacity_l),
            calibration_curve=t.calibration_curve, calibration_validated=bool(t.calibration_validated),
            validated=t.validation_status == "VALIDEE" and t.active, status=t.validation_status,
            model_ref=model.tank_model_id if model is not None else None,
        )

    def has_geometry(self) -> bool:
        try:
            self.geometric_full_l()
            return True
        except G.GeometryError:
            return False

    def useful_height_cm(self) -> float | None:
        if self.family:
            try:
                return G.useful_height_cm(self.family, self.d1, self.d2, self.d3, self.bottom, self.bz, self.top, self.tz)
            except G.GeometryError:
                pass
        if self.calibration_curve:
            try:
                return max(float(p[0]) for p in self.calibration_curve)
            except (TypeError, ValueError, IndexError):
                return None
        return None

    # Compatibilité : ancien nom
    physical_height_cm = useful_height_cm

    def geometric_full_l(self) -> float:
        if not self.family:
            raise G.GeometryError("Forme de cuve inconnue.")
        return G.full_volume_l(self.family, self.d1, self.d2, self.d3, self.bottom, self.bz, self.top, self.tz)

    def geometric_capacity_l(self) -> float | None:
        try:
            return self.geometric_full_l()
        except G.GeometryError:
            return None

    def calibration(self) -> dict:
        return G.calibration(self.plate_capacity_l, self.geometric_capacity_l())

    def check_capacity_l(self) -> float | None:
        """Capacité de contrôle : plaque si connue, sinon volume géométrique plein."""
        return self.plate_capacity_l or self.geometric_capacity_l()


@dataclass
class ConversionResult:
    level_liters: float | None
    method: str
    status: str
    messages: list[dict] = field(default_factory=list)
    capacity_l: float | None = None
    tank_id: str | None = None
    geometric_l: float | None = None
    calibrated_l: float | None = None
    k: float | None = None
    rule: str | None = None
    model_ref: str | None = None
    fill_pct: float | None = None

    def add(self, code: str, message: str):
        self.messages.append({"code": code, "message": message})
        return self


def geometric_volume_l(tank: TankSpec, level_cm: float) -> float:
    """Volume géométrique (L) ; MissingDimensions si la géométrie n'est pas reconnue."""
    try:
        return G.volume_l(tank.family, tank.d1, tank.d2, tank.d3, level_cm, tank.bottom, tank.bz, tank.top, tank.tz)
    except G.GeometryError as e:
        raise MissingDimensions(str(e))


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


def select_tank(site_tanks: list[TankSpec], tank_id: str | None) -> tuple[TankSpec | None, dict | None]:
    """Cuve visée par le relevé ; jamais d'attribution automatique quand le site a plusieurs cuves."""
    active = [t for t in site_tanks if t.status != "REJETEE"]
    if tank_id:
        for t in active:
            if str(t.tank_id).strip().upper() == str(tank_id).strip().upper():
                return t, None
        return None, {"code": "TANK_UNKNOWN", "message": f"Cuve « {tank_id} » absente du référentiel cuves du site."}
    if len(active) > 1 or any(t.status == "PLUSIEURS_CUVES_A_IDENTIFIER" for t in active):
        return None, {"code": "MULTI_TANK_WITHOUT_ID",
                      "message": "Le site a plusieurs cuves et le relevé ne précise pas laquelle : aucune attribution automatique."}
    if len(active) == 1:
        return active[0], None
    return None, {"code": "NO_VALIDATED_TANK", "message": "Aucune cuve validée pour ce site."}


def _fill(res: ConversionResult):
    if res.level_liters is not None and res.capacity_l:
        res.fill_pct = round(res.level_liters / res.capacity_l * 100, 1)


def convert(level_value: float | None, unit: str | None, site_tanks: list[TankSpec], tank_id: str | None = None,
            tolerance_pct: float = 5.0) -> ConversionResult:
    if level_value is None:
        return ConversionResult(None, NONE, INVALID).add("MISSING_LEVEL", "Niveau absent.")
    if unit not in ("L", "CM"):
        return ConversionResult(None, NONE, INVALID).add("UNKNOWN_UNIT", f"Unité inconnue : {unit!r}.")
    if level_value < 0:
        return ConversionResult(None, NONE, INVALID).add("NEGATIVE_LEVEL", f"Niveau négatif ({level_value}).")

    tank, problem = select_tank(site_tanks, tank_id)
    tol = 1 + tolerance_pct / 100

    if unit == "L":
        res = ConversionResult(float(level_value), DIRECT_LITERS, VALID, tank_id=tank.tank_id if tank else tank_id,
                               rule="Litres natifs de la source")
        if problem and problem["code"] == "MULTI_TANK_WITHOUT_ID":
            res.status = PENDING_TANK_MAPPING
            res.messages.append(problem)
            return res
        if tank is None or not tank.validated:
            return res.add("CAPACITY_NOT_VALIDATED", "Aucune cuve validée : contrôle de capacité impossible.")
        cap = tank.check_capacity_l()
        res.capacity_l, res.model_ref = cap, tank.model_ref
        if cap is None:
            res.add("CAPACITY_NOT_VALIDATED", "Capacité de cuve inconnue : contrôle de capacité impossible.")
        elif level_value > cap * tol:
            res.status = INVALID
            res.add("OVER_CAPACITY", f"{level_value:.0f} L > capacité {cap:.0f} L (+{tolerance_pct:g} %).")
        _fill(res)
        return res

    # Relevé en cm : cuve VALIDÉE obligatoire.
    res = ConversionResult(None, NONE, PENDING_TANK_MAPPING, tank_id=tank.tank_id if tank else tank_id)
    if problem:
        res.messages.append(problem)
        return res
    res.model_ref = tank.model_ref
    if not tank.validated:
        code = {"PROPOSEE": "TANK_PROPOSED", "A_CONFIRMER": "TANK_TO_CONFIRM", "INCOMPLETE": "TANK_INCOMPLETE",
                "PLUSIEURS_CUVES_A_IDENTIFIER": "MULTI_TANK_WITHOUT_ID"}.get(tank.status or "", "NO_VALIDATED_TANK")
        return res.add(code, f"Cuve {tank.tank_id} non validée ({tank.status}) : conversion cm → L interdite.")
    height = tank.useful_height_cm()
    if height is not None and level_value > height:
        res.status = INVALID
        return res.add("LEVEL_ABOVE_HEIGHT", f"Niveau {level_value:g} cm > hauteur utile {height:g} cm.")

    if tank.calibration_curve and tank.calibration_validated:
        v = curve_volume_l(tank.calibration_curve, float(level_value))
        if v is not None:
            res.level_liters, res.method, res.status, res.rule = round(v, 3), TANK_CURVE, VALID, "Abaque de jaugeage validé"
            res.capacity_l = tank.check_capacity_l() or max(float(p[1]) for p in tank.calibration_curve)
            if v > res.capacity_l * tol:
                res.status = INVALID
                res.add("OVER_CAPACITY", f"{v:.0f} L > capacité {res.capacity_l:.0f} L.")
            _fill(res)
            return res
        res.add("CURVE_OUT_OF_RANGE", "Niveau hors de l'abaque (ou abaque incohérent) : calcul géométrique tenté.")

    try:
        geo = geometric_volume_l(tank, float(level_value))
        full = tank.geometric_full_l()
    except (MissingDimensions, G.GeometryError) as e:
        return res.add("MISSING_DIMENSIONS", f"Géométrie de la cuve {tank.tank_id} non reconnue : {e}")
    res.geometric_l = round(geo, 3)
    res.method = GEOMETRIC_CALCULATION
    cal = G.calibration(tank.plate_capacity_l, full)
    res.k = cal["k"]
    res.capacity_l = tank.plate_capacity_l or full
    if cal["coherent"]:
        res.calibrated_l = round(geo * cal["k"], 3)
        res.level_liters = res.calibrated_l
        res.method, res.status = CALIBRATED, VALID
        res.rule = (f"Volume calibré = géométrique × k ({cal['k']:.4f}), plaque {tank.plate_capacity_l:.0f} L, "
                    f"écart {cal['gap_pct']:+.1f} % — {G.GEOMETRIC_FORMULA_VERSION}")
    else:
        res.status = GEOMETRIC_ONLY
        if tank.plate_capacity_l:
            res.calibrated_l = round(geo * cal["k"], 3)
            res.add("PLATE_INCOHERENT", f"Écart plaque / géométrie {cal['gap_pct']:+.1f} % (> 5 %) : cotes ou capacité de plaque à reprendre.")
            res.rule = "Volume géométrique (technique) — plaque incohérente"
        else:
            res.add("PLATE_MISSING", "Capacité de plaque non confirmée : volume géométrique conservé comme valeur technique.")
            res.rule = "Volume géométrique (technique) — plaque inconnue"
    vol = res.level_liters if res.level_liters is not None else geo
    if vol > res.capacity_l * tol:
        res.status = INVALID
        res.level_liters = None
        res.add("OVER_CAPACITY", f"{vol:.0f} L > capacité {res.capacity_l:.0f} L (+{tolerance_pct:g} %).")
    _fill(res)
    if res.fill_pct is None and res.geometric_l is not None and res.capacity_l:
        res.fill_pct = round(res.geometric_l / res.capacity_l * 100, 1)
    return res


def to_decimal(v) -> Decimal | None:
    return None if v is None else Decimal(str(round(float(v), 3)))
