# fuel_tracking/services/cph_engine.py
"""
Moteur Suivi Carburant / CPH — instruction globale validée + abaque CPH GE
PRP 50 Hz (décision métier du 28/09/2026).

Calcul au grain site/jour sur la plage EXACTE demandée (jamais ramenée au
mois), à partir des faits bruts Snowflake stockés dans FuelSiteDailyFacts.
Aucune valeur inventée : une donnée absente reste None avec un motif, aucun
COALESCE(…, 0), aucun repli silencieux — chaque jour garde la source retenue
et le motif de rejet des autres sources.

Chaîne de calcul (instruction §4 à §7) :
  runtime_ge_h      priorité DSE > redresseur (off-grid) > Day DG On > compteur
                    terrain, parmi les sources disponibles ≥ 50 % de la période
  p_ge_retenue_kw   outdoor : production GE / runtime DSE, sinon P_DC / rendement
                    indoor  : P_DC pendant GE / rendement + load AC historique
  charge_ge         p_ge_retenue_kw / p_ge_prp_kw
  cph_l_h           a·charge² + b·charge + c   (courbe PRP validée uniquement)
  conso_l_jour      runtime_ge_h × cph_l_h
  conso_stock_l     initial + livraisons + rajouts − retraits − vols − vidanges − final
  ecart_l           conso_stock_l − conso_theorique_l
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

RULE_VERSION = "CPH-PRP-50HZ-2026-09-28"

D0 = Decimal("0")
MIN_AVAILABILITY = Decimal("0.5")
RUNTIME_MAX_H = Decimal("24")
SLOT_H = Decimal("5") / Decimal("60")
TRACKER_MIN_COVERAGE_MIN = 1296          # 90 % de 1 440 min : intervalles continus
AC_REFERENCE_LOOKBACK_DAYS = 60          # historique indoor avant le début de période
AC_REFERENCE_MIN_DAYS = 5                # jours de référence minimum pour la médiane
POWER_CAP_RATIO = Decimal("1.05")        # refus au-delà de 105 % × kVA × cos φ
EXTRAPOLATION_BELOW = Decimal("0.5")     # charge < 50 % : estimation mathématique
MAX_PERIOD_DAYS = 92

# Sources runtime (priorité stricte, instruction §4)
RT_DSE = "DSE"
RT_REDRESSEUR = "REDRESSEUR"
RT_DAY_DG_ON = "DAY_DG_ON"
RT_COMPTEUR = "COMPTEUR_TERRAIN"
RUNTIME_PRIORITY = [RT_DSE, RT_REDRESSEUR, RT_DAY_DG_ON, RT_COMPTEUR]
# Champs bruts Snowflake d'heures de marche : présents mais rejetés → RUNTIME_NON_QUALIFIE.
RAW_RUNTIME_FIELDS = ("dse_runtime_h", "dg_on_runtime_h", "tracker_runtime_h", "rectifier_active_slots")
RUNTIME_LABELS = {
    RT_DSE: "DSE / contrôleur GE (GENSET_REPORT.DG_RUNTIME_CONTROLLER)",
    RT_REDRESSEUR: "Redresseur actif, créneaux 5 min (RECTIFIER_EFFICIENCY_STATUS)",
    RT_DAY_DG_ON: "Day DG On (GENSET_REPORT.DG_RUNTIME_CALCULATED)",
    RT_COMPTEUR: "Compteur horaire terrain (GFMS_DATA_TRACKER_NC)",
}

# Sources puissance (instruction §5)
PW_PRODUCTION = "PRODUCTION_GE"
PW_DC_REDRESSEUR = "DC_REDRESSEUR"
PW_INDOOR = "ESTIMATION_HISTORIQUE_LOAD_AC"   # P_DC pendant GE + load AC historique (jamais ACT_ACTIVE_POWER_AVG direct)

# Puissance active nominale estimée du GE = kVA de la courbe × facteur de puissance (0,8 par défaut).
NOMINAL_POWER_FACTOR = Decimal("0.8")

# Motifs précis d'absence de CPH (cph_l_h = NULL, conso_estimee_l = NULL)
MC_RUNTIME_INDISPONIBLE = "RUNTIME_INDISPONIBLE"
MC_RUNTIME_NON_QUALIFIE = "RUNTIME_NON_QUALIFIE"
MC_PUISSANCE_INDISPONIBLE = "PUISSANCE_INDISPONIBLE"
MC_PUISSANCE_HORS_LIMITE = "PUISSANCE_HORS_LIMITE"
MC_PUISSANCE_NOMINALE_ABSENTE = "PUISSANCE_NOMINALE_ABSENTE"
MC_RENDEMENT_INVALIDE = "RENDEMENT_REDRESSEUR_INVALIDE"
MC_COURBE_CPH_MANQUANTE = "COURBE_CPH_MANQUANTE"
MC_MAPPING_A_VALIDER = "MAPPING_GE_A_VALIDER"
MC_MODELE_AMBIGU = "MODELE_GE_AMBIGU"
MC_SITE_MULTI_GE = "SITE_MULTI_GE"
MC_TYPE_GE_ABSENT = "TYPE_GE_ABSENT"

# Statuts journaliers
DAY_CPH_CALCULE = "CPH_CALCULE"
DAY_GE_ARRET = "GE_A_L_ARRET"
DAY_RUNTIME_ABSENT = "RUNTIME_ABSENT"
DAY_PUISSANCE_ABSENTE = "PUISSANCE_ABSENTE"
DAY_COURBE_ABSENTE = "COURBE_ABSENTE"
DAY_PUISSANCE_HORS_PLAFOND = "PUISSANCE_HORS_PLAFOND"
DAY_CPH_HORS_DOMAINE = "CPH_HORS_DOMAINE"

# Statuts de période (CPH)
CPH_COMPLET = "COMPLET"
CPH_PARTIEL = "PARTIEL"
CPH_NON_CALCULE_PERIODE = "NON_CALCULE"

# Statuts rapprochement (instruction §7)
R_OK = "OK"
R_A_JUSTIFIER = "A_JUSTIFIER"
R_A_INVESTIGUER = "A_INVESTIGUER"
R_DONNEES_INCOMPLETES = "DONNEES_INCOMPLETES"
R_CPH_NON_CALCULE = "CPH_NON_CALCULE"
R_SEVERITY = {R_OK: 1, R_DONNEES_INCOMPLETES: 2, R_CPH_NON_CALCULE: 3, R_A_JUSTIFIER: 4, R_A_INVESTIGUER: 5}
LIVRAISONS_ENOC_A_CONTROLER = "LIVRAISONS_ENOC_A_CONTROLER"
LIVRAISONS_ENOC_RACCORDEES = "LIVRAISONS_ENOC_RACCORDEES"

OBSERVATION_VALIDATED_STATUSES = {"valide", "validee", "validated", "ok"}

DEFAULT_THRESHOLDS = {
    "seuil_ok_pct": Decimal("10"), "seuil_aj_pct": Decimal("20"),
    "seuil_ok_l": Decimal("100"), "seuil_aj_l": Decimal("200"),
}


@dataclass
class Curve:
    curve_id: str
    label: str
    status: str
    prp_kva: Decimal
    prp_kw: Decimal
    power_factor: Decimal
    a: Decimal
    b: Decimal
    c: Decimal
    source: str = ""


@dataclass
class SiteContext:
    site_id: str
    country: str | None = None
    data_id: int | None = None
    site_name: str | None = None
    zone: str | None = None
    kind: str | None = None                 # "INDOOR" / "OUTDOOR" / None
    kind_source: str | None = None
    grid_supply: str | None = None
    off_grid: bool | None = None
    dg_count: int | None = None
    ge_label: str | None = None
    curve: Curve | None = None
    curve_reason: str | None = None         # pourquoi aucune courbe n'est appliquée
    data_issue: str | None = None           # anomalie bloquante de rattachement des faits
    match: dict | None = None               # correspondance plaque → courbe (statut, score, méthode…)
    curve_code: str | None = None           # motif précis si curve est None (COURBE_CPH_MANQUANTE…)
    ge_kva: Decimal | None = None           # puissance nominale de l'inventaire (Base GE), information


@dataclass
class Observation:
    start: date
    end: date
    opening_fuel_l: Decimal | None
    closing_fuel_l: Decimal | None
    fuel_deliveries_l: Decimal | None
    fuel_transfer_in_l: Decimal | None
    fuel_transfer_out_l: Decimal | None
    fuel_theft_l: Decimal | None
    fuel_drain_l: Decimal | None
    observation_status: str | None = None
    comment: str | None = None
    justificatif: str | None = None
    import_file: str | None = None
    import_id: int | None = None


@dataclass
class EngineSettings:
    enoc_deliveries_connected: bool = False
    thresholds: dict = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    nominal_power_factor: Decimal = NOMINAL_POWER_FACTOR
    # Plage de plausibilité de la consommation spécifique (L/kWh) : ~0,25-0,30 à charge correcte,
    # plus élevée à faible charge. Hors plage = alerte (jamais bloquant, jamais corrigé).
    sfc_min_l_kwh: Decimal = Decimal("0.20")
    sfc_max_l_kwh: Decimal = Decimal("0.50")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _dec(v) -> Decimal | None:
    if v is None:
        return None
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _q(v: Decimal | None, places: str = "0.001") -> Decimal | None:
    return None if v is None else v.quantize(Decimal(places), rounding=ROUND_HALF_UP)


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def normalize_label(label: str | None) -> str:
    return " ".join(str(label or "").lower().split())


def is_off_grid(grid_supply: str | None) -> bool | None:
    """GRID_SUPPLY_MODIFIED brut → True (off-grid) / False (réseau) / None (inconnu)."""
    if grid_supply is None or not str(grid_supply).strip():
        return None
    g = str(grid_supply).lower().replace("_", "-").replace(" ", "")
    if "off" in g or g in ("no", "non", "none", "0", "false"):
        return True
    if "on" in g or "grid" in g or g in ("yes", "oui", "1", "true"):
        return False
    return None


def _efficiency_ok(eff: Decimal | None) -> bool:
    return eff is not None and D0 < eff <= Decimal("1")


# ─── Runtime (instruction §4) ────────────────────────────────────────────────

def source_value(source: str, fact: dict | None, ctx: SiteContext) -> tuple[Decimal | None, str | None]:
    """Valeur valide d'une source pour un jour, ou (None, motif). 0 est une valeur valide."""
    if fact is None:
        return None, "aucune donnée Snowflake ce jour"
    if source == RT_DSE:
        v = _dec(fact.get("dse_runtime_h"))
        if v is None:
            return None, "DSE absent ce jour"
        if not (D0 <= v <= RUNTIME_MAX_H):
            return None, f"DSE hors bornes 0-24 h ({v})"
        return v, None
    if source == RT_REDRESSEUR:
        if ctx.off_grid is not True:
            return None, "redresseur réservé aux sites off-grid éligibles"
        slots = fact.get("rectifier_slots")
        active = fact.get("rectifier_active_slots")
        if slots is None or active is None or slots == 0:
            return None, "aucun créneau redresseur ce jour"
        v = Decimal(active) * SLOT_H
        return (v, None) if v <= RUNTIME_MAX_H else (None, f"redresseur hors bornes ({v} h)")
    if source == RT_DAY_DG_ON:
        v = _dec(fact.get("dg_on_runtime_h"))
        if v is None:
            return None, "Day DG On absent ce jour"
        if not (D0 <= v <= RUNTIME_MAX_H):
            return None, f"Day DG On hors bornes 0-24 h ({v})"
        return v, None
    if source == RT_COMPTEUR:
        v = _dec(fact.get("tracker_runtime_h"))
        cov = fact.get("tracker_covered_min")
        if v is None or cov is None:
            return None, "compteur terrain absent ce jour"
        if cov < TRACKER_MIN_COVERAGE_MIN:
            return None, f"compteur terrain discontinu ({cov} min couvertes < {TRACKER_MIN_COVERAGE_MIN})"
        if not (D0 <= v <= RUNTIME_MAX_H):
            return None, f"compteur terrain hors bornes ({v} h)"
        return v, None
    raise ValueError(source)


def evaluate_runtime_sources(ctx: SiteContext, facts: dict, days: list[date]) -> dict:
    """
    Disponibilité de chaque source sur la période = jours avec une valeur
    valide (0 compris) / jours de la période — jamais de COALESCE à 0.
    Retourne {source: {availability, days_valid, exploitable, rejection}}.
    """
    n = len(days)
    out = {}
    for src in RUNTIME_PRIORITY:
        vals = {d: source_value(src, facts.get(d), ctx)[0] for d in days}
        valid = sum(1 for v in vals.values() if v is not None)
        availability = Decimal(valid) / Decimal(n) if n else D0
        rejection = None
        if src == RT_REDRESSEUR and ctx.off_grid is not True:
            rejection = (
                "site non off-grid : redresseur non éligible" if ctx.off_grid is False
                else "statut réseau inconnu (GRID_SUPPLY_MODIFIED) : redresseur non éligible"
            )
        elif availability < MIN_AVAILABILITY:
            rejection = f"disponibilité {availability * 100:.0f} % < 50 %"
        elif src == RT_DAY_DG_ON:
            # Contrôle de coalescence artificielle à zéro : Day DG On = 0 un
            # jour où une autre source mesure une marche du GE.
            suspect = []
            for d in days:
                v = vals[d]
                if v is not None and v == D0:
                    others = [source_value(o, facts.get(d), ctx)[0] for o in (RT_DSE, RT_REDRESSEUR, RT_COMPTEUR)]
                    if any(o is not None and o > D0 for o in others):
                        suspect.append(d)
            if suspect:
                rejection = (
                    f"coalescence à zéro suspecte : Day DG On = 0 alors qu'une autre source "
                    f"mesure une marche ({len(suspect)} jour(s), ex. {suspect[0].isoformat()})"
                )
        out[src] = {
            "availability": availability,
            "days_valid": valid,
            "exploitable": rejection is None,
            "rejection": rejection,
            "values": vals,
        }
    return out


def select_daily_runtime(day: date, sources_eval: dict) -> tuple[Decimal | None, str | None, list[str]]:
    """Première source exploitable (priorité stricte) ayant une valeur ce jour."""
    motifs = []
    for src in RUNTIME_PRIORITY:
        ev = sources_eval[src]
        if not ev["exploitable"]:
            motifs.append(f"{src} : {ev['rejection']}")
            continue
        v = ev["values"][day]
        if v is None:
            motifs.append(f"{src} : absent ce jour")
            continue
        return v, src, motifs
    return None, None, motifs


# ─── Puissance GE (instruction §5) ───────────────────────────────────────────

def dc_input_during_ge(ctx: SiteContext, fact: dict) -> tuple[Decimal | None, str | None, str | None]:
    """P_DC pendant GE / rendement redresseur (0 < η ≤ 1), sans courant batterie. Retourne (kW, méthode, motif)."""
    if (fact.get("tracker_ge_on_slots") or 0) > 0 and fact.get("p_dc_ge_tracker_kw") is not None:
        p, eff, method = _dec(fact["p_dc_ge_tracker_kw"]), _dec(fact.get("eff_ge_tracker")), "créneaux GE du compteur terrain"
    elif ctx.off_grid is True and fact.get("p_dc_rect_active_kw") is not None:
        p, eff, method = _dec(fact["p_dc_rect_active_kw"]), _dec(fact.get("eff_rect_active")), "créneaux redresseur actif (site off-grid)"
    else:
        return None, None, "P_DC pendant GE indisponible (aucun créneau GE identifié)"
    if p < D0:
        return None, method, f"P_DC négative ({p} kW)"
    if not _efficiency_ok(eff):
        return None, method, f"{MC_RENDEMENT_INVALIDE} : rendement redresseur hors (0 ; 1] ({eff})"
    return p / eff, method, None


def _power_code(reason: str | None) -> str:
    return MC_RENDEMENT_INVALIDE if reason and MC_RENDEMENT_INVALIDE in reason else MC_PUISSANCE_INDISPONIBLE


def indoor_ac_reference(ctx: SiteContext, facts: dict, start: date, end: date) -> dict:
    """
    Load AC historique indoor : médiane de MAX(0, P_AC instrumentée − P_DC
    entrée redresseur) sur les jours du même site où le réseau est présent et
    le GE absent (DSE ou compteur = 0 et aucune source > 0). AC_METER est
    journalier : la médiane est journalière, pas par créneau horaire.
    """
    if ctx.off_grid is not False:
        return {"p_ac_aux_kw": None, "reference_dates": [], "reason": (
            "site off-grid : aucun jour réseau présent pour isoler le load AC" if ctx.off_grid
            else "statut réseau inconnu : jours de référence réseau non identifiables"
        )}
    samples = []
    ref_dates = []
    for d in daterange(start - timedelta(days=AC_REFERENCE_LOOKBACK_DAYS), end):
        f = facts.get(d)
        if not f:
            continue
        measured = [source_value(s, f, ctx)[0] for s in (RT_DSE, RT_COMPTEUR, RT_DAY_DG_ON)]
        direct_zero = any(v is not None and v == D0 for v in measured[:2])
        any_running = any(v is not None and v > D0 for v in measured)
        if not direct_zero or any_running:
            continue
        ac_w = _dec(f.get("ac_active_power_avg_w"))
        p_dc = _dec(f.get("p_dc_day_kw"))
        eff = _dec(f.get("eff_day"))
        if ac_w is None or ac_w < D0 or p_dc is None or p_dc < D0 or not _efficiency_ok(eff):
            continue
        samples.append(max(D0, ac_w / Decimal("1000") - p_dc / eff))
        ref_dates.append(d)
    if len(samples) < AC_REFERENCE_MIN_DAYS:
        return {"p_ac_aux_kw": None, "reference_dates": ref_dates, "reason": (
            f"profil AC historique insuffisant ({len(samples)} jour(s) réseau sans GE instrumenté(s) "
            f"< {AC_REFERENCE_MIN_DAYS})"
        )}
    return {"p_ac_aux_kw": Decimal(str(statistics.median(samples))), "reference_dates": ref_dates, "reason": None}


def ge_power(ctx: SiteContext, fact: dict | None, runtime_h: Decimal, runtime_source: str, ac_ref: dict | None) -> dict:
    if fact is None:
        return {"p_ge_kw": None, "source": None, "detail": None, "reason": "aucune donnée de puissance ce jour"}
    if ctx.kind == "OUTDOOR":
        prod = _dec(fact.get("dg_production_kwh"))
        if runtime_source == RT_DSE and prod is not None and prod > D0 and runtime_h > D0:
            return {"p_ge_kw": prod / runtime_h, "source": PW_PRODUCTION,
                    "detail": f"DG_PRODUCTION_KWH {_q(prod)} kWh / runtime DSE {_q(runtime_h)} h", "reason": None}
        p_in, method, reason = dc_input_during_ge(ctx, fact)
        if p_in is None:
            return {"p_ge_kw": None, "source": None, "detail": None, "reason": f"outdoor : {reason}"}
        return {"p_ge_kw": p_in, "source": PW_DC_REDRESSEUR,
                "detail": f"P_DC / rendement sur {method} (charge batterie non ajoutée)", "reason": None}
    if ctx.kind == "INDOOR":
        p_in, method, reason = dc_input_during_ge(ctx, fact)
        if p_in is None:
            return {"p_ge_kw": None, "source": None, "detail": None, "reason": f"indoor : {reason}"}
        if ac_ref is None or ac_ref["p_ac_aux_kw"] is None:
            return {"p_ge_kw": None, "source": None, "detail": None,
                    "reason": f"indoor : {ac_ref['reason'] if ac_ref else 'profil AC non calculé'}"}
        return {"p_ge_kw": p_in + ac_ref["p_ac_aux_kw"], "source": PW_INDOOR,
                "detail": (f"P_DC pendant GE {_q(p_in)} kW ({method}) + load AC historique "
                           f"{_q(ac_ref['p_ac_aux_kw'])} kW (médiane de {len(ac_ref['reference_dates'])} jours)"),
                "p_dc_input_kw": p_in, "p_ac_aux_kw": ac_ref["p_ac_aux_kw"], "reason": None}
    return {"p_ge_kw": None, "source": None, "detail": None, "reason": "type de site indoor/outdoor inconnu"}


# ─── CPH (instruction §6) ────────────────────────────────────────────────────

def apply_curve(curve: Curve, p_ge_kw: Decimal, power_factor: Decimal = NOMINAL_POWER_FACTOR) -> dict:
    """
    charge_ge = p_ge_kw / (kVA nominal × 0,8) ; refus si p_ge_kw > 1,05 × kVA × 0,8 ;
    CPH = a·charge² + b·charge + c (charge en fraction, comme les points 50/75/100 % de l'abaque).
    """
    if curve.prp_kva is None or curve.prp_kva <= D0:
        return {"status": DAY_PUISSANCE_ABSENTE, "code": MC_PUISSANCE_NOMINALE_ABSENTE, "charge": None, "cph_l_h": None,
                "extrapolated": False, "reason": f"{MC_PUISSANCE_NOMINALE_ABSENTE} : kVA nominal de la courbe {curve.curve_id} inconnu"}
    p_nom = curve.prp_kva * power_factor
    cap = POWER_CAP_RATIO * p_nom
    if p_ge_kw > cap:
        return {"status": DAY_PUISSANCE_HORS_PLAFOND, "code": MC_PUISSANCE_HORS_LIMITE, "charge": None, "cph_l_h": None,
                "extrapolated": False,
                "reason": f"{MC_PUISSANCE_HORS_LIMITE} : {_q(p_ge_kw)} kW > 1,05 × {curve.prp_kva} kVA × {power_factor} = {_q(cap)} kW"}
    x = p_ge_kw / p_nom
    if x <= D0:
        return {"status": DAY_CPH_HORS_DOMAINE, "code": MC_PUISSANCE_HORS_LIMITE, "charge": x, "cph_l_h": None,
                "extrapolated": False, "reason": f"{MC_PUISSANCE_HORS_LIMITE} : charge nulle ou négative"}
    cph = curve.a * x * x + curve.b * x + curve.c
    if cph <= D0:
        return {"status": DAY_CPH_HORS_DOMAINE, "code": MC_PUISSANCE_HORS_LIMITE, "charge": x, "cph_l_h": None,
                "extrapolated": x < EXTRAPOLATION_BELOW,
                "reason": f"{MC_PUISSANCE_HORS_LIMITE} : CPH ≤ 0 à charge {x * 100:.1f} % (hors domaine de la courbe {curve.curve_id})"}
    return {"status": DAY_CPH_CALCULE, "code": None, "charge": x, "cph_l_h": cph, "extrapolated": x < EXTRAPOLATION_BELOW, "reason": None}


def compute_day(ctx: SiteContext, d: date, fact: dict | None, sources_eval: dict, ac_ref: dict | None,
                power_factor: Decimal = NOMINAL_POWER_FACTOR) -> dict:
    runtime_h, runtime_source, runtime_motifs = select_daily_runtime(d, sources_eval)
    raw = {src: sources_eval[src]["values"][d] for src in RUNTIME_PRIORITY}
    row = {
        "date": d, "runtime_h": runtime_h, "runtime_source": runtime_source, "runtime_motifs": runtime_motifs,
        "raw": raw,
        "p_ge_kw": None, "power_source": None, "power_detail": None, "p_dc_input_kw": None, "p_ac_aux_kw": None,
        "curve_id": ctx.curve.curve_id if ctx.curve else None,
        "charge": None, "cph_l_h": None, "conso_l": None, "extrapolated": False,
        "status": None, "motif_code": None, "motifs": [],
        # Conso MESURÉE du jour (VW_FUEL_REPORT) — indicateur distinct, jamais mélangé à l'estimation.
        "measured_l": (fact or {}).get("measured_conso_l"),
    }
    if runtime_h is None:
        row["status"] = DAY_RUNTIME_ABSENT
        # Valeur brute présente mais rejetée (bornes, disponibilité < 50 %…) ≠ aucune donnée.
        has_raw = any(v is not None for v in raw.values()) or any(
            (fact or {}).get(k) is not None for k in RAW_RUNTIME_FIELDS)
        row["motif_code"] = MC_RUNTIME_NON_QUALIFIE if has_raw else MC_RUNTIME_INDISPONIBLE
        row["motifs"] = [f"{row['motif_code']} : runtime_ge_h = NULL, aucune source ne passe les contrôles"] + runtime_motifs
        return row
    if runtime_h == D0:
        # GE à l'arrêt mesuré : 0 h × CPH = 0 L, sans puissance ni courbe.
        row["status"] = DAY_GE_ARRET
        row["conso_l"] = D0
        return row

    pw = ge_power(ctx, fact, runtime_h, runtime_source, ac_ref)
    row.update(p_ge_kw=pw["p_ge_kw"], power_source=pw["source"], power_detail=pw["detail"],
               p_dc_input_kw=pw.get("p_dc_input_kw"), p_ac_aux_kw=pw.get("p_ac_aux_kw"))
    missing = []
    if pw["p_ge_kw"] is None:
        code = _power_code(pw["reason"])
        missing.append((DAY_PUISSANCE_ABSENTE, code, pw["reason"] if code in (pw["reason"] or "") else f"{code} : {pw['reason']}"))
    if ctx.curve is None:
        code = ctx.curve_code or MC_MAPPING_A_VALIDER
        missing.append((DAY_COURBE_ABSENTE, code, f"{code} : {ctx.curve_reason or 'aucune courbe CPH applicable'}"))
    if missing:
        row["status"], row["motif_code"] = missing[0][0], missing[0][1]
        row["motifs"] = [m for _, _, m in missing]
        return row

    res = apply_curve(ctx.curve, pw["p_ge_kw"], power_factor)
    row.update(charge=res["charge"], extrapolated=res["extrapolated"], status=res["status"], motif_code=res["code"])
    if res["cph_l_h"] is None:
        row["motifs"] = [res["reason"]]
        return row
    row["cph_l_h"] = res["cph_l_h"]
    row["conso_l"] = runtime_h * res["cph_l_h"]
    if res["extrapolated"]:
        row["motifs"] = ["charge < 50 % : estimation mathématique (extrapolation de la parabole)"]
    return row


def site_cph_motif(ctx: SiteContext, day_rows: list[dict], cph_status: str) -> dict | None:
    """Motif précis au niveau site/période quand la conso estimée n'est pas complète."""
    if cph_status == CPH_COMPLET:
        return None
    if ctx.data_issue:
        return {"code": MC_RUNTIME_INDISPONIBLE, "detail": ctx.data_issue, "jours": len(day_rows)}
    if ctx.curve is None:
        return {"code": ctx.curve_code or MC_MAPPING_A_VALIDER, "detail": ctx.curve_reason, "jours": len(day_rows)}
    counts: dict[str, int] = {}
    for r in day_rows:
        if r["motif_code"]:
            counts[r["motif_code"]] = counts.get(r["motif_code"], 0) + 1
    if not counts:
        return None
    code = max(counts, key=counts.get)
    detail = next((m for r in day_rows if r["motif_code"] == code for m in r["motifs"][:1]), None)
    return {"code": code, "detail": detail, "jours": counts[code]}


# ─── Conso estimée (CPH) vs conso mesurée (capteur) ─────────────────────────

C_NON_CALCULEE = "CONSO_ESTIMEE_NON_CALCULEE"
C_MESURE_ABSENTE = "MESURE_ABSENTE"
C_COHERENT = "COHERENT"
C_ECART_A_JUSTIFIER = "ECART_A_JUSTIFIER"
C_ECART_A_INVESTIGUER = "ECART_A_INVESTIGUER"
_R_TO_C = {R_OK: C_COHERENT, R_A_JUSTIFIER: C_ECART_A_JUSTIFIER, R_A_INVESTIGUER: C_ECART_A_INVESTIGUER}


def compare_measured(day_rows: list[dict], settings: EngineSettings) -> dict:
    """
    Écart = conso mesurée − conso estimée, calculé sur les jours COMMUNS (estimation
    calculée ET mesure capteur exploitable) : on ne compare jamais une période
    estimée à une mesure partielle. Seuils identiques au rapprochement stock.
    """
    n = len(day_rows)
    measured = [r["measured_l"] for r in day_rows if r["measured_l"] is not None]
    common = [r for r in day_rows if r["conso_l"] is not None and r["measured_l"] is not None]
    out = {
        "conso_mesuree_l": sum(measured, D0) if measured else None,
        "jours_mesure": len(measured),
        "jours_communs": len(common),
        "conso_estimee_communs_l": None, "conso_mesuree_communs_l": None,
        "ecart_l": None, "ecart_pct": None, "statut": None, "motif": None,
    }
    computed = [r for r in day_rows if r["conso_l"] is not None]
    # Aucun jour avec CPH alors que le GE a tourné (seuls des jours GE à l'arrêt valent 0 L) :
    # la conso estimée n'existe pas, aucun écart n'est calculé. Un GE à l'arrêt sur TOUTE la
    # période (0 L sur chaque jour) reste comparable : c'est un vrai 0.
    no_cph_while_running = all(r["status"] == DAY_GE_ARRET for r in computed) and len(computed) < n
    if not computed or no_cph_while_running:
        out["statut"] = C_NON_CALCULEE
        out["motif"] = "conso estimée non calculée (voir le point bloquant)"
        return out
    if not common:
        out["statut"] = C_MESURE_ABSENTE
        out["motif"] = ("aucune mesure capteur exploitable sur la période (VW_FUEL_REPORT)" if not measured
                        else "aucun jour avec à la fois une conso estimée et une mesure capteur")
        return out
    est = sum((r["conso_l"] for r in common), D0)
    mes = sum((r["measured_l"] for r in common), D0)
    ecart = mes - est
    out.update(conso_estimee_communs_l=est, conso_mesuree_communs_l=mes, ecart_l=ecart,
               ecart_pct=(ecart / est * 100) if est > D0 else None,
               statut=_R_TO_C[_thresholds_status(ecart, est, settings.thresholds)])
    out["motif"] = (f"comparaison sur {len(common)}/{n} jour(s) communs" if len(common) < n
                    else "comparaison sur tous les jours de la période")
    return out


def specific_consumption(cph_days: list[dict], settings: EngineSettings) -> dict:
    """
    Consommation spécifique (L/kWh) sur les jours avec CPH : énergie GE = Σ P_GE × runtime.
      estimée = Σ conso estimée ÷ énergie  → cohérence courbe / puissance retenue ;
      mesurée = Σ conso mesurée ÷ énergie (jours mesurés) → indicateur terrain (perte, vol, capteur).
    Hors plage [min ; max] = alerte, sans effet sur les valeurs calculées.
    """
    energy = sum((r["p_ge_kw"] * r["runtime_h"] for r in cph_days), D0)
    measured_days = [r for r in cph_days if r["measured_l"] is not None]
    energy_m = sum((r["p_ge_kw"] * r["runtime_h"] for r in measured_days), D0)
    est = sum((r["conso_l"] for r in cph_days), D0) / energy if energy > D0 else None
    mes = sum((r["measured_l"] for r in measured_days), D0) / energy_m if energy_m > D0 else None
    lo, hi = settings.sfc_min_l_kwh, settings.sfc_max_l_kwh
    out_of = (lambda v: None if v is None else not (lo <= v <= hi))
    alerts = []
    if out_of(est):
        alerts.append(f"conso spécifique estimée {est:.2f} L/kWh hors plage [{lo} ; {hi}] : vérifier la courbe ou la puissance retenue")
    if out_of(mes):
        alerts.append(f"conso spécifique mesurée {mes:.2f} L/kWh hors plage [{lo} ; {hi}] : perte, vol ou capteur à contrôler")
    return {
        "energie_ge_kwh": energy if energy > D0 else None,
        "estimee_l_kwh": est, "mesuree_l_kwh": mes,
        "alerte_estimee": out_of(est), "alerte_mesuree": out_of(mes),
        "plage_l_kwh": [lo, hi], "alertes": alerts,
    }


# ─── Rapprochement (instruction §7) ──────────────────────────────────────────

def _thresholds_status(ecart_l: Decimal, conso_th: Decimal, t: dict) -> str:
    ok = max(t["seuil_ok_l"], conso_th * t["seuil_ok_pct"] / Decimal("100"))
    aj = max(t["seuil_aj_l"], conso_th * t["seuil_aj_pct"] / Decimal("100"))
    a = abs(ecart_l)
    if a <= ok:
        return R_OK
    if a <= aj:
        return R_A_JUSTIFIER
    return R_A_INVESTIGUER


def reconcile(obs: Observation, day_rows: dict, settings: EngineSettings) -> dict:
    days = list(daterange(obs.start, obs.end))
    computed = [day_rows[d]["conso_l"] for d in days if d in day_rows and day_rows[d]["conso_l"] is not None]
    conso_th = sum(computed, D0) if len(computed) == len(days) else None
    livraisons_statut = LIVRAISONS_ENOC_RACCORDEES if settings.enoc_deliveries_connected else LIVRAISONS_ENOC_A_CONTROLER
    base = {
        "observation_start": obs.start, "observation_end": obs.end,
        "stock_initial_l": obs.opening_fuel_l, "stock_final_l": obs.closing_fuel_l,
        "livraisons_l": obs.fuel_deliveries_l, "rajouts_l": obs.fuel_transfer_in_l,
        "retraits_l": obs.fuel_transfer_out_l, "vols_l": obs.fuel_theft_l, "vidanges_l": obs.fuel_drain_l,
        "observation_status": obs.observation_status, "import_file": obs.import_file,
        "livraisons_statut": livraisons_statut,
        "conso_theorique_l": conso_th, "jours_conso_calculee": len(computed), "jours_observation": len(days),
        "conso_stock_l": None, "ecart_l": None, "ecart_pct": None,
    }
    motifs = []
    labels = {
        "stock_initial_l": "stock initial", "stock_final_l": "stock final", "livraisons_l": "livraisons",
        "rajouts_l": "rajouts", "retraits_l": "retraits", "vols_l": "vols", "vidanges_l": "vidanges",
    }
    missing = [lbl for k, lbl in labels.items() if base[k] is None]
    if missing:
        motifs.append(f"donnée(s) absente(s) : {', '.join(missing)}")
    if normalize_label(obs.observation_status).replace("é", "e") not in OBSERVATION_VALIDATED_STATUSES:
        motifs.append(f"observation non validée (statut : {obs.observation_status or 'vide'})")
    if not settings.enoc_deliveries_connected:
        motifs.append("LIVRAISONS_ENOC_A_CONTROLER : livraisons ENOC réelles non raccordées")
    if motifs:
        return {**base, "statut": R_DONNEES_INCOMPLETES, "motifs": motifs}
    if conso_th is None:
        return {**base, "statut": R_CPH_NON_CALCULE, "motifs": [
            f"consommation théorique non qualifiée : {len(computed)}/{len(days)} jour(s) calculé(s)"]}

    conso_stock = (obs.opening_fuel_l + obs.fuel_deliveries_l + obs.fuel_transfer_in_l
                   - obs.fuel_transfer_out_l - obs.fuel_theft_l - obs.fuel_drain_l - obs.closing_fuel_l)
    ecart = conso_stock - conso_th
    ecart_pct = (Decimal("100") * ecart / conso_th) if conso_th != D0 else None
    statut = _thresholds_status(ecart, conso_th, settings.thresholds)
    return {**base, "conso_stock_l": conso_stock, "ecart_l": ecart, "ecart_pct": ecart_pct, "statut": statut,
            "motifs": [] if ecart_pct is not None else ["conso théorique = 0 L : écart % non calculable"]}


# ─── Période complète pour un site ───────────────────────────────────────────

def compute_site_period(ctx: SiteContext, facts: dict, start: date, end: date,
                        observations: list[Observation], settings: EngineSettings) -> dict:
    days = list(daterange(start, end))
    sources_eval = evaluate_runtime_sources(ctx, facts, days)
    ac_ref = indoor_ac_reference(ctx, facts, start, end) if ctx.kind == "INDOOR" else None
    day_rows = {d: compute_day(ctx, d, facts.get(d), sources_eval, ac_ref, settings.nominal_power_factor) for d in days}

    n = len(days)
    rt_days = [r for r in day_rows.values() if r["runtime_h"] is not None]
    conso_days = [r for r in day_rows.values() if r["conso_l"] is not None]
    cph_days = [r for r in day_rows.values() if r["status"] == DAY_CPH_CALCULE]
    runtime_by_src: dict[str, int] = {}
    for r in rt_days:
        runtime_by_src[r["runtime_source"]] = runtime_by_src.get(r["runtime_source"], 0) + 1
    power_by_src: dict[str, int] = {}
    for r in cph_days:
        power_by_src[r["power_source"]] = power_by_src.get(r["power_source"], 0) + 1
    status_counts: dict[str, int] = {}
    for r in day_rows.values():
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    cph_runtime = sum((r["runtime_h"] for r in cph_days), D0)
    cph_conso = sum((r["conso_l"] for r in cph_days), D0)
    conso_sum = sum((r["conso_l"] for r in conso_days), D0)
    if len(conso_days) == n:
        cph_status = CPH_COMPLET
    elif conso_days:
        cph_status = CPH_PARTIEL
    else:
        cph_status = CPH_NON_CALCULE_PERIODE

    motifs: list[str] = [ctx.data_issue] if ctx.data_issue else []
    for st, cnt in sorted(status_counts.items(), key=lambda kv: -kv[1]):
        if st in (DAY_CPH_CALCULE, DAY_GE_ARRET):
            continue
        example = next(r for r in day_rows.values() if r["status"] == st)
        motifs.append(f"{st} ({cnt} j) : {'; '.join(example['motifs'][:2]) or '—'}")

    comparison = compare_measured([day_rows[d] for d in days], settings)
    specific = specific_consumption(cph_days, settings)
    charge_days = [r for r in cph_days if r["charge"] is not None]
    charge_rt = sum((r["runtime_h"] for r in charge_days), D0)
    main_src = max(runtime_by_src.items(), key=lambda kv: kv[1])[0] if runtime_by_src else None

    in_period = [o for o in observations if start <= o.start and o.end <= end]
    reconciliations = [reconcile(o, day_rows, settings) for o in in_period]
    if reconciliations:
        worst = max(reconciliations, key=lambda r: R_SEVERITY[r["statut"]])
        rapprochement_statut, rapprochement_ref = worst["statut"], worst
    else:
        rapprochement_statut, rapprochement_ref = R_DONNEES_INCOMPLETES, None

    return {
        "site_id": ctx.site_id, "site_name": ctx.site_name, "country": ctx.country, "zone": ctx.zone,
        "data_id": ctx.data_id, "kind": ctx.kind, "kind_source": ctx.kind_source,
        "grid_supply": ctx.grid_supply, "off_grid": ctx.off_grid, "dg_count": ctx.dg_count,
        "ge_label": ctx.ge_label, "data_issue": ctx.data_issue,
        "curve": ctx.curve, "curve_reason": ctx.curve_reason,
        "start": start, "end": end, "days": n,
        "runtime_days": len(rt_days),
        "runtime_total_h": sum((r["runtime_h"] for r in rt_days), D0) if rt_days else None,
        "runtime_source_days": runtime_by_src,
        "runtime_source_main": main_src,
        "runtime_source_availability": sources_eval[main_src]["availability"] if main_src else None,
        "sources": {s: {k: v for k, v in ev.items() if k != "values"} for s, ev in sources_eval.items()},
        "power_source_days": power_by_src,
        "power_source_main": max(power_by_src.items(), key=lambda kv: kv[1])[0] if power_by_src else None,
        "p_ge_moy_kw": (sum((r["p_ge_kw"] * r["runtime_h"] for r in cph_days), D0) / cph_runtime) if cph_runtime > D0 else None,
        "ac_reference": ac_ref,
        "cph_days": len(cph_days),
        "cph_moy_l_h": (cph_conso / cph_runtime) if cph_runtime > D0 else None,
        "charge_moy": (sum((r["charge"] * r["runtime_h"] for r in charge_days), D0) / charge_rt) if charge_rt > D0 else None,
        "comparaison": comparison,
        "conso_specifique": specific,
        "correspondance": ctx.match,
        "conso_days": len(conso_days),
        "conso_theorique_l": conso_sum if cph_status == CPH_COMPLET else None,
        "conso_partielle_l": conso_sum if cph_status == CPH_PARTIEL else None,
        "cph_status": cph_status,
        "motif_cph": site_cph_motif(ctx, [day_rows[d] for d in days], cph_status),
        "ge_kva": ctx.ge_kva,
        "day_status_counts": status_counts,
        "extrapolated_days": sum(1 for r in cph_days if r["extrapolated"]),
        "motifs": motifs,
        "reconciliations": reconciliations,
        "rapprochement_statut": rapprochement_statut,
        "rapprochement": rapprochement_ref,
        "daily": [day_rows[d] for d in days],
    }
