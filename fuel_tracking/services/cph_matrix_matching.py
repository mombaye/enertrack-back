# fuel_tracking/services/cph_matrix_matching.py
"""
Matching (marque, kVA) -> matrice CPH (CphMatrixPoint), pour la colonne
"CPH Target Aktivco" du module fuel.

Contrairement à genset_curve_matching (courbe quadratique ajustée sur 3
points, par marque+modèle précis), cette matrice donne des points de mesure
réels tous les 5-10% de charge — l'interpolation entre les deux points
encadrants est donc linéaire, pas une régression.

Seule la famille "Perkins" est actuellement renseignée dans le fichier
source (voir import_cph_matrix) — le matching ne retournera donc un résultat
que pour les sites dont le GE est de marque Perkins. C'est honnête : mieux
vaut "pas de cible CPH" que de comparer un SDMO à une courbe Perkins.
"""
from functools import lru_cache

from fuel_tracking.models import CphMatrixPoint


KVA_TOLERANCE_PCT = 0.15


def _normalize_brand(raw: str) -> str:
    b = (raw or "").strip().upper()
    for sep in ("|", "/"):
        if sep in b:
            b = b.split(sep)[0].strip()
    return b


@lru_cache(maxsize=1)
def _points_by_engine_kva():
    by_key = {}
    for p in CphMatrixPoint.objects.all():
        by_key.setdefault((p.engine_family_normalized, float(p.dg_capacity_kva)), []).append(p)
    for pts in by_key.values():
        pts.sort(key=lambda p: float(p.charge_pct))
    return by_key


def match_cph_target(brand: str, power_kva) -> dict:
    """
    Retourne `matched` + (si trouvé) la liste de points [pct, L/h] triés par
    % de charge croissant, pour interpolation linéaire côté appelant.
    """
    if not brand or not power_kva:
        return {"matched": False, "reason": "marque ou kVA manquant"}

    norm_brand = _normalize_brand(brand)

    try:
        kva = float(power_kva)
    except (TypeError, ValueError):
        return {"matched": False, "reason": "kVA invalide"}

    if kva <= 0:
        return {"matched": False, "reason": "kVA invalide"}

    by_key = _points_by_engine_kva()
    candidates_kva = sorted({k[1] for k in by_key if k[0] == norm_brand})

    if not candidates_kva:
        return {"matched": False, "reason": f"pas de matrice CPH pour la marque {norm_brand}"}

    nearest_kva = min(candidates_kva, key=lambda k: abs(k - kva))
    delta_pct = abs(nearest_kva - kva) / kva

    if delta_pct > KVA_TOLERANCE_PCT:
        return {
            "matched": False,
            "reason": f"aucune taille {norm_brand} proche de {kva} kVA (plus proche : {nearest_kva} kVA)",
        }

    points = by_key[(norm_brand, nearest_kva)]

    return {
        "matched": True,
        "engine_family": norm_brand,
        "dg_capacity_kva_matched": nearest_kva,
        "points": [[float(p.charge_pct), float(p.cph_l_h)] for p in points],
    }


def cph_target_l_h(match_result: dict, charge_pct) -> float | None:
    """Interpolation linéaire entre les deux points encadrants charge_pct (fraction, 1.0 = 100%)."""
    if not match_result.get("matched") or charge_pct is None:
        return None

    points = match_result.get("points") or []
    if not points:
        return None

    x = float(charge_pct)

    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]

    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y0
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)

    return None
