# fuel_tracking/services/cph_matching.py
"""
Correspondance automatique « libellé GE de l'inventaire (Base GE) → courbe CPH »
de l'abaque PRP 50 Hz.

Deux statuts DISTINCTS, jamais confondus :
  - statut de CORRESPONDANCE plaque → courbe (ce module, CphInventoryMapping.match_status) :
      AUTO_VALIDE_COMPATIBLE  correspondance unique et compatible (score ≥ 70 %, aucune contradiction)
      VALIDE_MANUELLEMENT     choix d'un validateur (prioritaire, jamais écrasé par l'automatique)
      A_VALIDER               candidat unique mais incompatible ou score insuffisant (motif conservé)
      COURBE_CPH_MANQUANTE    aucune courbe candidate dans l'abaque
      MODELE_AMBIGU           plusieurs courbes candidates comparables
    et, au niveau du site : SITE_MULTI_GE (une courbe par GE non gérée), GE_INCONNU (Base GE muette) ;
  - statut de QUALITÉ / origine de la courbe (CphCurve.status : VALIDÉ_CONSTRUCTEUR,
    HISTORIQUE_A_VALIDER, FICHE_ARCHIVEE_A_VALIDER, FICHE_DISTRIBUTEUR_A_VALIDER) — jamais modifié ici :
    une correspondance automatique ne transforme pas une courbe historique en courbe constructeur.

Score de compatibilité (0-100) d'un candidat unique :
  modèle  60 (identique après normalisation) ou 40 (même modèle à un suffixe de variante
          alphabétique ≤ 2 caractères près, ex. P33-3U / P33-3, J66 / J66K) ; sinon contradiction ;
  marque  20 si la marque du libellé correspond à celle de la courbe (groupes d'alias),
          0 si le libellé ne porte pas de marque lisible ; marque différente → contradiction ;
  puissance 20 si le kVA inventaire est à ± 3 % du kVA PRP ou du kVA secours (≈ 1,1 × PRP,
          valeur souvent portée par la référence commerciale), 10 si à ± 10 %, sinon contradiction ;
  fréquence courbe ou libellé en 60 Hz → contradiction.
Une simple proximité de kVA n'associe jamais deux modèles différents : le modèle est obligatoire.
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

AUTO_VALIDE_COMPATIBLE = "AUTO_VALIDE_COMPATIBLE"
VALIDE_MANUELLEMENT = "VALIDE_MANUELLEMENT"
A_VALIDER = "A_VALIDER"
COURBE_CPH_MANQUANTE = "COURBE_CPH_MANQUANTE"
MODELE_AMBIGU = "MODELE_AMBIGU"
SITE_MULTI_GE = "SITE_MULTI_GE"
GE_INCONNU = "GE_INCONNU"

USABLE_MATCH_STATUSES = (AUTO_VALIDE_COMPATIBLE, VALIDE_MANUELLEMENT)
MATCH_STATUSES = (AUTO_VALIDE_COMPATIBLE, VALIDE_MANUELLEMENT, A_VALIDER, COURBE_CPH_MANQUANTE, MODELE_AMBIGU)

METHOD_MANUAL = "MANUEL"
METHOD_MANUAL_REMOVAL = "RETRAIT_MANUEL"
MANUAL_METHODS = (METHOD_MANUAL, METHOD_MANUAL_REMOVAL)

AUTO_THRESHOLD = 70
STANDBY_RATIO = Decimal("1.1")
POWER_TIGHT = Decimal("0.03")
POWER_LOOSE = Decimal("0.10")

# Marques d'un même groupe industriel, écrites sous plusieurs formes dans l'inventaire et l'abaque.
BRAND_ALIASES = (
    {"CATERPILLAR", "CAT", "OLYMPIAN"},
    {"SDMO", "KOHLER", "REHLKO"},
    {"FGWILSON", "WILSON"},
    {"GUCBIR", "CUGBIR"},
    {"AMMAN", "AMAN"},
)


def norm(text) -> str:
    """Majuscules, sans accents, sans espaces, tirets ni ponctuation."""
    if text is None:
        return ""
    t = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", t.upper())


def _brand_group(token: str) -> frozenset:
    for group in BRAND_ALIASES:
        if token in group:
            return frozenset(group)
    return frozenset({token})


def curve_brands(manufacturer: str) -> set[frozenset]:
    return {_brand_group(norm(p)) for p in re.split(r"[/,&]", manufacturer or "") if norm(p)}


def known_brands(curves: list[dict]) -> set[str]:
    brands = {norm(p) for c in curves for p in re.split(r"[/,&]", c.get("manufacturer") or "") if norm(p)}
    for group in BRAND_ALIASES:
        brands |= group
    return {b for b in brands if len(b) >= 3}


def split_label(label: str, brands: set[str]) -> tuple[str, set[frozenset], str]:
    """(modèle normalisé, groupes de marques lues, modèle brut) d'un libellé « MARQUE - MODÈLE »."""
    parts = [p for p in re.split(r"\s*(?:\s-\s|--|\|)\s*", (label or "").strip()) if p.strip()]
    if not parts:
        return "", set(), ""
    model_raw = parts[-1].strip()
    brand_text = norm(" ".join(parts[:-1]))
    model = norm(model_raw)
    found = {b for b in brands if b in brand_text}
    if len(parts) == 1:
        # « FG Wilson P33-3U » : la marque est en préfixe du libellé sans séparateur.
        prefix = max((b for b in brands if model.startswith(b) and len(model) > len(b)), key=len, default=None)
        if prefix:
            found = {prefix}
            model = model[len(prefix):]
            model_raw = model
    return model, {_brand_group(b) for b in found}, model_raw


def model_relation(label_model: str, curve_model: str) -> str | None:
    if not label_model or not curve_model:
        return None
    if label_model == curve_model:
        return "EXACT"
    longer, shorter = (label_model, curve_model) if len(label_model) > len(curve_model) else (curve_model, label_model)
    suffix = longer[len(shorter):]
    if longer.startswith(shorter) and 1 <= len(suffix) <= 2 and suffix.isalpha():
        return "VARIANTE"
    return None


def _model_is_readable(model: str) -> bool:
    return len(model) >= 2 and bool(re.search(r"[A-Z]", model)) and bool(re.search(r"\d", model))


def score_candidate(label: str, kva, curve: dict, all_curves: list[dict]) -> dict:
    """Score de compatibilité d'UNE courbe candidate ; contradictions = motifs bloquants."""
    brands = known_brands(all_curves)
    lm, label_brands, lm_raw = split_label(label, brands)
    cm = norm(curve.get("model"))
    score, reasons, contradictions, method = 0, [], [], []

    relation = model_relation(lm, cm)
    if not _model_is_readable(lm):
        contradictions.append(f"modèle absent ou illisible dans « {label} »")
    elif relation is None:
        contradictions.append(f"modèle « {lm_raw} » incompatible avec « {curve.get('model')} »")
    else:
        score += 60 if relation == "EXACT" else 40
        method.append("MODELE_IDENTIQUE" if relation == "EXACT" else "MODELE_VARIANTE")
        reasons.append("modèle identique" if relation == "EXACT" else f"modèle à une variante près ({lm_raw} / {curve.get('model')})")

    c_brands = curve_brands(curve.get("manufacturer") or "")
    if not label_brands:
        reasons.append("marque non lue dans le libellé")
    elif label_brands & c_brands:
        score += 20
        method.append("MARQUE")
        reasons.append("marque identique")
    else:
        contradictions.append(f"marque du libellé différente de « {curve.get('manufacturer')} »")

    prp = curve.get("prp_kva")
    if kva is None or prp in (None, 0):
        reasons.append("puissance inventaire absente : non contrôlée")
    else:
        kva, prp = Decimal(str(kva)), Decimal(str(prp))
        gap_prp = abs(kva / prp - 1)
        gap_standby = abs(kva / (prp * STANDBY_RATIO) - 1)
        gap = min(gap_prp, gap_standby)
        ref = "PRP" if gap_prp <= gap_standby else "secours ≈ 1,1 × PRP"
        if gap <= POWER_TIGHT:
            score += 20
            method.append("PUISSANCE")
            reasons.append(f"puissance compatible ({kva} kVA ↔ {prp} kVA PRP, réf. {ref})")
        elif gap <= POWER_LOOSE:
            score += 10
            method.append("PUISSANCE_PROCHE")
            reasons.append(f"puissance proche ({kva} kVA ↔ {prp} kVA PRP, écart {gap * 100:.0f} %)")
        else:
            contradictions.append(f"écart de puissance important ({kva} kVA inventaire ↔ {prp} kVA PRP)")

    freq = norm(curve.get("frequency"))
    if ("60" in freq and "50" not in freq) or "60HZ" in norm(label):
        contradictions.append("fréquence 60 Hz incompatible avec l'abaque PRP 50 Hz")

    if relation is not None:
        rank = {"EXACT": 2, "VARIANTE": 1}
        rivals = [
            c for c in all_curves
            if c["curve_id"] != curve["curve_id"]
            and (r := model_relation(lm, norm(c.get("model")))) is not None and rank[r] >= rank[relation]
            and (not label_brands or label_brands & curve_brands(c.get("manufacturer") or ""))
        ]
        # Doublons de l'abaque (même modèle, mêmes points de consommation — ex. seul le
        # contrôleur DSE7320 / DSE4520 diffère) : même CPH, donc pas d'ambiguïté réelle.
        twins = [c["curve_id"] for c in rivals if curve_points(c) == curve_points(curve)]
        different = [c["curve_id"] for c in rivals if curve_points(c) != curve_points(curve)]
        if different:
            contradictions.append(
                f"plusieurs courbes comparables aux consommations différentes ({', '.join([curve['curve_id'], *different])})")
        if twins:
            reasons.append(f"courbe(s) équivalente(s) ignorée(s), mêmes points de consommation : {', '.join(twins)}")

    return {"score": score, "reasons": reasons, "contradictions": contradictions, "method": "+".join(method)}


def auto_match(label: str, kva, candidate_ids: list[str], curves_by_id: dict[str, dict], all_curves: list[dict]) -> dict:
    """Décision automatique pour un mappage (libellé, kVA) — aucune validation aveugle."""
    candidates = [curves_by_id[c] for c in candidate_ids if c in curves_by_id]
    if not candidates:
        return {"status": COURBE_CPH_MANQUANTE, "curve_id": None, "score": None, "method": "AUTO",
                "reasons": ["aucune courbe candidate pour ce libellé dans l'abaque"]}
    if len(candidates) > 1:
        return {"status": MODELE_AMBIGU, "curve_id": None, "score": None, "method": "AUTO",
                "reasons": [f"{len(candidates)} courbes candidates comparables : {', '.join(c['curve_id'] for c in candidates)} — choix manuel requis"]}
    c = candidates[0]
    s = score_candidate(label, kva, c, all_curves)
    if s["contradictions"]:
        return {"status": A_VALIDER, "curve_id": None, "score": s["score"], "method": f"AUTO:{s['method']}",
                "reasons": s["contradictions"] + s["reasons"]}
    if s["score"] < AUTO_THRESHOLD:
        return {"status": A_VALIDER, "curve_id": None, "score": s["score"], "method": f"AUTO:{s['method']}",
                "reasons": [f"score de compatibilité {s['score']} % < {AUTO_THRESHOLD} %"] + s["reasons"]}
    return {"status": AUTO_VALIDE_COMPATIBLE, "curve_id": c["curve_id"], "score": s["score"],
            "method": f"AUTO:{s['method']}", "reasons": s["reasons"]}


def curve_points(c: dict) -> tuple:
    """Identité « consommation » d'une courbe : PRP et points 50 / 75 / 100 %."""
    return tuple(None if c.get(k) is None else Decimal(str(c[k])).normalize()
                 for k in ("prp_kva", "conso_50_l_h", "conso_75_l_h", "conso_100_l_h"))


def curve_dict(c) -> dict:
    """CphCurve (modèle ou modèle historique de migration) → dict attendu par ce module."""
    return {"curve_id": c.curve_id, "manufacturer": c.manufacturer, "model": c.model,
            "prp_kva": c.prp_kva, "frequency": c.frequency, "conso_50_l_h": c.conso_50_l_h,
            "conso_75_l_h": c.conso_75_l_h, "conso_100_l_h": c.conso_100_l_h}


def apply_auto_matching(mapping_qs, curve_qs, now) -> dict[str, int]:
    """
    (Ré)évalue les mappages. Une décision humaine (validation ou retrait manuel)
    n'est jamais écrasée. Retourne le nombre de mappages par statut.
    """
    curves = [curve_dict(c) for c in curve_qs]
    by_id = {c["curve_id"]: c for c in curves}
    pk_by_curve_id = {c.curve_id: c.pk for c in curve_qs}
    counts: dict[str, int] = {}
    for m in mapping_qs:
        manual_ok = m.match_method in MANUAL_METHODS and (
            m.match_method == METHOD_MANUAL_REMOVAL
            or (m.validated_curve_id is not None and m.match_status == VALIDE_MANUELLEMENT)
        )
        if not manual_ok:
            d = auto_match(m.inventory_label, m.inventory_kva, list(m.candidate_curve_ids or []), by_id, curves)
            m.match_status = d["status"]
            m.match_score = d["score"]
            m.match_method = d["method"]
            m.match_reasons = d["reasons"]
            m.matched_at = now
            m.validated_curve_id = pk_by_curve_id.get(d["curve_id"]) if d["curve_id"] else None
            m.validated_by_id = None
            m.validated_at = now if d["curve_id"] else None
            m.validation_comment = ""
            m.save(update_fields=["match_status", "match_score", "match_method", "match_reasons", "matched_at",
                                  "validated_curve", "validated_by", "validated_at", "validation_comment"])
        counts[m.match_status] = counts.get(m.match_status, 0) + 1
    return counts
