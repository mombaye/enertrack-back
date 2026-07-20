# fuel_tracking/services/genset_curve_matching.py
"""
Matching (marque, modèle, kVA) -> courbe de consommation GensetFuelCurve.

ENOC remonte marque + modèle (`model`, ~97% renseigné) + kVA pour chaque GE.
Le modèle ENOC ne correspond pas toujours exactement au libellé du catalogue
(ex: "P33-3U" vs "P33-3 ", "GEP30-1" vs "GEP30") donc le matching se fait en
paliers, du plus précis au plus grossier :

1. Modèle exact (normalisé, même marque)      -> confiance MODEL_EXACT
2. Modèle flou (difflib, même marque)          -> confiance MODEL_FUZZY
3. kVA exact (même marque)                     -> confiance KVA_EXACT
4. kVA le plus proche, tolérance 15% (même marque) -> confiance KVA_NEAREST
5. Rien                                        -> NO_MATCH

Quand plusieurs modèles catalogués partagent la même clé de matching avec des
courbes différentes, on moyenne les coefficients et on ajoute le suffixe
_AMBIGUOUS_AVERAGED plutôt que de deviner un modèle au hasard.
"""
import difflib
from functools import lru_cache

from fuel_tracking.models import GensetFuelCurve


BRAND_ALIASES = {
    "AMAN": "AMMAN",
    "CUGBIR GENERATOR": "GUCBIR",
}

KVA_TOLERANCE_PCT = 0.15
MODEL_FUZZY_CUTOFF = 0.72


def _norm(s: str) -> str:
    return (s or "").strip().upper().replace(" ", "")


def normalize_brand(raw: str) -> str:
    b = (raw or "").strip().upper()
    for sep in ("|", "/"):
        if sep in b:
            b = b.split(sep)[0].strip()
    return BRAND_ALIASES.get(b, b)


@lru_cache(maxsize=1)
def _curves_by_brand():
    by_brand = {}
    for curve in GensetFuelCurve.objects.all():
        by_brand.setdefault(curve.manufacturer_normalized, []).append(curve)
    return by_brand


def _summarize(candidates: list[GensetFuelCurve], base_confidence: str) -> dict:
    # Des doublons catalogue (même modèle, variante de contrôleur DSE) ont des
    # courbes identiques — ne pas les compter comme une vraie ambiguïté.
    distinct_curves = {
        (round(float(c.coef_a), 3), round(float(c.coef_b), 3), round(float(c.coef_c), 3))
        for c in candidates
    }
    is_ambiguous = len(distinct_curves) > 1
    confidence = f"{base_confidence}_AMBIGUOUS_AVERAGED" if is_ambiguous else base_confidence

    n = len(candidates)

    return {
        "matched": True,
        "confidence": confidence,
        "brand_matched": candidates[0].manufacturer_normalized,
        "prp_kva_matched": float(candidates[0].prp_kva),
        "candidate_models": sorted({c.type_de_ge for c in candidates}),
        "coef_a": sum(float(c.coef_a) for c in candidates) / n,
        "coef_b": sum(float(c.coef_b) for c in candidates) / n,
        "coef_c": sum(float(c.coef_c) for c in candidates) / n,
        "conso_100_l_h": sum(float(c.conso_100_l_h) for c in candidates) / n,
        "conso_75_l_h": sum(float(c.conso_75_l_h) for c in candidates) / n,
        "conso_50_l_h": sum(float(c.conso_50_l_h) for c in candidates) / n,
    }


def match_genset_curve(brand: str, power_kva, model: str | None = None) -> dict:
    """
    Retourne un dict avec au minimum `matched` (bool) et `confidence`.
    Si matched=True : coef_a/b/c + conso_100/75/50_l_h (moyennés si ambigu).
    """
    if not brand:
        return {"matched": False, "confidence": "NO_MATCH", "reason": "marque manquante"}

    norm_brand = normalize_brand(brand)
    curves = _curves_by_brand().get(norm_brand)

    if not curves:
        return {"matched": False, "confidence": "NO_MATCH", "reason": f"marque inconnue au catalogue: {norm_brand}"}

    # Palier 1 : modèle exact
    if model:
        norm_model = _norm(model)
        exact_model = [c for c in curves if _norm(c.type_de_ge) == norm_model]
        if exact_model:
            return _summarize(exact_model, "MODEL_EXACT")

        # Palier 2 : modèle flou
        model_by_norm = {}
        for c in curves:
            model_by_norm.setdefault(_norm(c.type_de_ge), []).append(c)

        close = difflib.get_close_matches(norm_model, list(model_by_norm.keys()), n=1, cutoff=MODEL_FUZZY_CUTOFF)
        if close:
            return _summarize(model_by_norm[close[0]], "MODEL_FUZZY")

    # Palier 3/4 : repli kVA
    try:
        kva = float(power_kva) if power_kva else None
    except (TypeError, ValueError):
        kva = None

    if not kva or kva <= 0:
        return {"matched": False, "confidence": "NO_MATCH", "reason": "modèle non reconnu et kVA manquant/invalide"}

    exact_kva = [c for c in curves if float(c.prp_kva) == kva]
    if exact_kva:
        return _summarize(exact_kva, "KVA_EXACT")

    nearest = min(curves, key=lambda c: abs(float(c.prp_kva) - kva))
    delta_pct = abs(float(nearest.prp_kva) - kva) / kva

    if delta_pct > KVA_TOLERANCE_PCT:
        return {
            "matched": False,
            "confidence": "NO_MATCH",
            "reason": f"aucun modèle {norm_brand} proche de {kva} kVA (plus proche : {float(nearest.prp_kva)} kVA)",
        }

    nearest_kva_candidates = [c for c in curves if float(c.prp_kva) == float(nearest.prp_kva)]
    return _summarize(nearest_kva_candidates, "KVA_NEAREST")


def conso_theorique_l_h(match_result: dict, charge_pct) -> float | None:
    """conso(x) = a·x² + b·x + c, x = charge_pct en fraction (1.0 = 100%)."""
    if not match_result.get("matched") or charge_pct is None:
        return None

    x = float(charge_pct)
    return match_result["coef_a"] * x * x + match_result["coef_b"] * x + match_result["coef_c"]
