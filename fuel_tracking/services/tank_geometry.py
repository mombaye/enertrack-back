# fuel_tracking/services/tank_geometry.py
"""
Moteur de volume de cuve — reprise exacte du « Calculateur universel de volume de carburant »
(Senegal_Tank_DB_Calculateur.xlsx, Camusat Regional Energy Desk, onglet Notice §4). Version : steve-v1.

Familles (cotes en cm) :
  h-circ   horizontale circulaire     cote1 = D, cote3 = L (virole, hors fonds)
  h-elli   horizontale elliptique     cote1 = W, cote2 = H, cote3 = L
  h-oblo   horizontale oblongue       cote1 = W, cote2 = H, cote3 = L
  v-cyl    verticale cylindrique      cote1 = D, cote2 = Hv (virole)
  v-tronc  verticale tronconique      cote1 = D1 (fond), cote2 = D2 (sommet), cote3 = H
  rect     rectangulaire              cote1 = A (largeur), cote2 = B (hauteur), cote3 = L

Profils de fond : plat, ellip, toris, cap (calotte, Z/R), hemi (Z = R), cone, klop (Z = 0,1938·D),
korb (Z = 0,2545·D). Tables Ψ (horizontales) et Φ (verticales) : data/tank_profiles_steve_v1.json, extraites
de l'onglet « Profils », interpolées linéairement (pas 0,005).

  Horizontale : V(h) = A(h)·L + 2·Z·(A_pleine/π)·Ψ(h/Hc)
  Verticale   : fond   h ≤ Zb      V = π R² Zb [Φ(1) − Φ(1 − h/Zb)]
                virole             V = π R² [Zb Φ(1) + (h − Zb)]
                ciel               V = π R² [Zb Φb(1) + Hv + Zt Φt'(…)]   (même méthode, depuis la base du ciel)
  Tronconique : V = (π h / 3)(R1² + R1 r + r²), r = R1 + (R2 − R1) h / H
  Volume calibré = volume géométrique × k, k = capacité de plaque / volume géométrique plein.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

GEOMETRIC_FORMULA_VERSION = "steve-v1"
CALIBRATION_FORMULA_VERSION = "steve-v1 (k = plaque / géométrie pleine)"

FAMILIES = {
    "h-circ": "Horizontale — section circulaire",
    "h-elli": "Horizontale — section elliptique",
    "h-oblo": "Horizontale — section oblongue",
    "v-cyl": "Verticale cylindrique",
    "v-tronc": "Verticale tronconique",
    "rect": "Rectangulaire / parallélépipédique",
}
PROFILES = {
    "plat": "Plat (sans fond bombé)",
    "ellip": "Bombé ellipsoïdal (flèche Z)",
    "toris": "Bombé torisphérique (flèche Z)",
    "cap": "Calotte sphérique (flèche Z)",
    "hemi": "Hémisphérique (Z = R imposé)",
    "cone": "Conique (flèche Z)",
    "klop": "Klöpper DIN 28011 (Z auto)",
    "korb": "Korbbogen DIN 28013 (Z auto)",
}
FAMILY_BY_LABEL = {v.lower(): k for k, v in FAMILIES.items()}
PROFILE_BY_LABEL = {v.lower(): k for k, v in PROFILES.items()}
TABLE_OF = {"ellip": "ellip", "hemi": "ellip", "toris": "toris", "klop": "toris", "korb": "korb", "cone": "cone"}


class GeometryError(ValueError):
    """Forme / cotes / profil insuffisants pour un calcul reconnu."""


@lru_cache(maxsize=1)
def tables() -> dict:
    path = Path(__file__).resolve().parent.parent / "data" / "tank_profiles_steve_v1.json"
    return json.loads(path.read_text())


def _interp(values: list[float], t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    pos = t / 0.005
    i = min(int(pos), len(values) - 2)
    frac = pos - i
    return values[i] + (values[i + 1] - values[i]) * frac


def _profile_fn(kind: str, profile: str, z_over_r: float | None) -> callable:
    """Ψ (kind=psi) ou Φ (kind=phi) du profil, fonction de t ∈ [0, 1]."""
    tb = tables()
    if profile == "plat":
        return lambda t: 0.0
    if profile == "cap":
        caps = tb["cap_z_over_r"]
        cols = tb[f"{kind}_cap"]
        zr = min(max(z_over_r or caps[0], caps[0]), caps[-1])
        j = 0
        while j < len(caps) - 2 and zr > caps[j + 1]:
            j += 1
        w = (zr - caps[j]) / (caps[j + 1] - caps[j])
        return lambda t: _interp(cols[j], t) * (1 - w) + _interp(cols[j + 1], t) * w
    col = tb[kind][TABLE_OF[profile]]
    return lambda t: _interp(col, t)


def end_rise_cm(profile: str, z: float | None, d_cm: float) -> float:
    """Flèche effective du fond : imposée pour hémisphérique / Klöpper / Korbbogen, sinon Z saisi."""
    if profile == "plat":
        return 0.0
    if profile == "hemi":
        return d_cm / 2
    if profile == "klop":
        return 0.1938 * d_cm
    if profile == "korb":
        return 0.2545 * d_cm
    if not z or z <= 0:
        raise GeometryError(f"Flèche Z du fond manquante pour le profil « {PROFILES.get(profile, profile)} ».")
    return z


def _seg(r: float, h: float) -> float:
    h = min(max(h, 0.0), 2 * r)
    return r * r * math.acos((r - h) / r) - (r - h) * math.sqrt(max(2 * r * h - h * h, 0.0))


def useful_height_cm(family, d1, d2, d3, bottom="plat", bz=None, top="plat", tz=None) -> float:
    needed = {"h-circ": (d1, d3), "h-elli": (d1, d2, d3), "h-oblo": (d1, d2, d3), "rect": (d1, d2, d3),
              "v-cyl": (d1, d2), "v-tronc": (d1, d2, d3)}.get(family)
    if needed is None:
        raise GeometryError(f"Famille de cuve inconnue : {family!r}.")
    _need(*needed)
    if family == "h-circ":
        return d1
    if family in ("h-elli", "h-oblo", "rect"):
        return d2
    if family == "v-cyl":
        return end_rise_cm(bottom, bz, d1) + d2 + end_rise_cm(top, tz, d1)
    if family == "v-tronc":
        return d3
    raise GeometryError(f"Famille de cuve inconnue : {family!r}.")


def _need(*vals):
    if any(v is None or v <= 0 for v in vals):
        raise GeometryError("Cotes incomplètes pour la famille de cuve.")


def volume_l(family: str, d1, d2, d3, level_cm: float, bottom: str = "plat", bz=None, top: str = "plat", tz=None) -> float:
    """Volume géométrique (L) au niveau h (cm), mesuré depuis le point le plus bas intérieur."""
    bottom = bottom or "plat"
    top = top or "plat"
    if bottom not in PROFILES or top not in PROFILES:
        raise GeometryError(f"Profil de fond inconnu : {bottom!r} / {top!r}.")
    h = max(level_cm, 0.0)
    if family in ("h-circ", "h-elli", "h-oblo"):
        if family == "h-circ":
            _need(d1, d3)
            w, hc, length = d1, d1, d3
        else:
            _need(d1, d2, d3)
            w, hc, length = d1, d2, d3
        h = min(h, hc)
        r = hc / 2
        if family == "h-oblo":
            rr = w / 2
            if hc < w:
                raise GeometryError("Section oblongue : la hauteur doit être ≥ la largeur.")
            full = math.pi * rr * rr + w * (hc - w)
            if h <= rr:
                area = _seg(rr, h)
            elif h <= hc - rr:
                area = math.pi * rr * rr / 2 + w * (h - rr)
            else:
                area = full - _seg(rr, hc - h)
        else:
            area = (w / hc) * _seg(r, h)
            full = math.pi * (w / 2) * (hc / 2)
        z = end_rise_cm(bottom, bz, hc)
        psi = _profile_fn("psi", bottom, z / r if r else None)
        v = area * length + 2 * z * (full / math.pi) * psi(h / hc)
        return v / 1000.0
    if family == "rect":
        _need(d1, d2, d3)
        return d1 * min(h, d2) * d3 / 1000.0
    if family == "v-cyl":
        _need(d1, d2)
        r = d1 / 2
        zb = end_rise_cm(bottom, bz, d1)
        zt = end_rise_cm(top, tz, d1)
        phib = _profile_fn("phi", bottom, zb / r if r else None)
        phit = _profile_fn("phi", top, zt / r if r else None)
        a = math.pi * r * r
        if zb and h <= zb:
            return a * zb * (phib(1.0) - phib(1 - h / zb)) / 1000.0
        bottom_v = a * zb * phib(1.0) if zb else 0.0
        if h <= zb + d2:
            return (bottom_v + a * (h - zb)) / 1000.0
        y = min(h - zb - d2, zt)
        top_v = a * zt * phit(y / zt) if zt else 0.0
        return (bottom_v + a * d2 + top_v) / 1000.0
    if family == "v-tronc":
        _need(d1, d2, d3)
        r1, r2 = d1 / 2, d2 / 2
        h = min(h, d3)
        r = r1 + (r2 - r1) * h / d3
        return math.pi * h / 3 * (r1 * r1 + r1 * r + r * r) / 1000.0
    raise GeometryError(f"Famille de cuve inconnue : {family!r}.")


def full_volume_l(family, d1, d2, d3, bottom="plat", bz=None, top="plat", tz=None) -> float:
    return volume_l(family, d1, d2, d3, useful_height_cm(family, d1, d2, d3, bottom, bz, top, tz), bottom, bz, top, tz)


def calibration(plate_l: float | None, geometric_full_l: float | None) -> dict:
    """k = plaque / géométrie pleine ; écart = (géométrie − plaque) / plaque (même convention que le calculateur)."""
    if not plate_l or not geometric_full_l:
        return {"k": None, "gap_pct": None, "coherent": None}
    gap = (geometric_full_l - plate_l) / plate_l
    return {"k": plate_l / geometric_full_l, "gap_pct": round(gap * 100, 2), "coherent": abs(gap) <= 0.05}
