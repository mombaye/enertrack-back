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

RULE_VERSION = "CPH-PRP-50HZ-2026-10-02"  # chaîne de repli puissance tracée

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
RUNTIME_LABEL_SHORT = {RT_DSE: "DSE", RT_REDRESSEUR: "redresseur", RT_DAY_DG_ON: "Day DG On", RT_COMPTEUR: "compteur"}
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

# Méthodes de puissance (chaîne de repli, dans cet ordre) et codes de rejet par méthode
PM_DIRECT = "DIRECT_DSE_PRODUCTION"
PM_PDC = "PDC_REDRESSEUR"
PM_INDOOR = "INDOOR_PDC_LOAD_AC"
POWER_METHODS = (PM_DIRECT, PM_PDC, PM_INDOOR)
PR_P_DSE_INDISPONIBLE = "P_DSE_INDISPONIBLE"
PR_P_DSE_INCOHERENTE = "P_DSE_INCOHERENTE"
PR_PDC_GE_INDISPONIBLE = "PDC_GE_INDISPONIBLE"
PR_PDC_NEGATIF = "PDC_NEGATIF"
PR_LOAD_AC_INDISPONIBLE = "LOAD_AC_INDISPONIBLE"
PR_LOAD_AC_INCOHERENT = "LOAD_AC_INCOHERENT"
PR_LOAD_AC_UNITE_SUSPECTE = "LOAD_AC_UNITE_SUSPECTE"
PR_CONFIGURATION_INCONNUE = "CONFIGURATION_INCONNUE"
BATTERIE_NON_INTEGREE = "BATTERIE_NON_INTEGREE — validation du sens énergétique requise"

# Statuts du load AC historique indoor : une médiane à 0 n'est valable que si elle est mesurée.
AC_QUALIFIE = "LOAD_AC_QUALIFIE"
AC_MESURE_ZERO = "LOAD_AC_MESURE_ZERO"
AC_INDISPONIBLE = PR_LOAD_AC_INDISPONIBLE
AC_INCOHERENT = PR_LOAD_AC_INCOHERENT
AC_UNITE_SUSPECTE = PR_LOAD_AC_UNITE_SUSPECTE

# Statuts séparés CPH / rapprochement stock
SC_CPH_CALCULE = "CPH_CALCULE"
SC_CPH_PARTIEL = "CPH_PARTIEL"
SC_CPH_NON_CALCULE = "CPH_NON_CALCULE"
SR_CALCULE = "RAPPROCHEMENT_CALCULE"
SR_STOCK_ABSENT = "RAPPROCHEMENT_NON_CALCULE_STOCK_ABSENT"
SR_MOUVEMENTS_ABSENTS = "RAPPROCHEMENT_NON_CALCULE_MOUVEMENTS_ABSENTS"
SR_CPH_INCOMPLET = "RAPPROCHEMENT_NON_CALCULE_CPH_INCOMPLET"

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
MC_PERIODE_INCOMPLETE = "PERIODE_INCOMPLETE"   # jour postérieur à la dernière synchronisation Snowflake

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
    facture_avec_ge: bool | None = None     # fichier ESCO « Facturation avec GE » (information, non bloquant)
    site_type: str | None = None            # On-Grid / Off-Grid (Base GE, sinon Snowflake)
    configuration_fichier: str | None = None  # Indoor / Outdoor déclaré dans le fichier de facturation


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
    ac_w_to_kw_divisor: Decimal = Decimal("1000")
    # Dernier jour synchronisé depuis Snowflake : au-delà, un jour sans fait = PERIODE_INCOMPLETE.
    data_until: date | None = None
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
                    f"COALESCENCE_ZERO_SUSPECTE : Day DG On = 0 alors qu'une autre source "
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
        return None, None, f"{PR_PDC_GE_INDISPONIBLE} : P_DC absent pendant les créneaux GE (aucun créneau GE identifié)"
    if p < D0:
        return None, method, f"{PR_PDC_NEGATIF} : P_DC négative ({p} kW)"
    if not _efficiency_ok(eff):
        return None, method, f"{MC_RENDEMENT_INVALIDE} : rendement redresseur hors (0 ; 1] ({eff})"
    return p / eff, method, None


def _reason_code(reason: str | None, default: str) -> str:
    for code in (MC_RENDEMENT_INVALIDE, PR_PDC_GE_INDISPONIBLE, PR_PDC_NEGATIF, PR_LOAD_AC_INDISPONIBLE,
                 PR_LOAD_AC_INCOHERENT, PR_LOAD_AC_UNITE_SUSPECTE):
        if reason and reason.startswith(code):
            return code
    return default


def indoor_ac_reference(ctx: SiteContext, facts: dict, start: date, end: date,
                        settings: "EngineSettings | None" = None) -> dict:
    """
    Load AC historique indoor = médiane de (P_AC instrumentée − P_DC entrée redresseur) sur les
    jours du même site où le réseau est présent et le GE absent (60 j d'historique, ≥ 5 jours).
    P_AC = ACT_ACTIVE_POWER_AVG ÷ diviseur (W → kW). Statuts :
      LOAD_AC_QUALIFIE      médiane > 0 ;
      LOAD_AC_MESURE_ZERO   médiane brute ≈ 0 (|écart| ≤ tolérance) : vrai zéro mesuré ;
      LOAD_AC_INCOHERENT    P_AC < P_DC entrée sur la majorité des jours : périmètre du compteur ou unité
                            → NULL (jamais 0 par écrêtage) ;
      LOAD_AC_UNITE_SUSPECTE P_AC brute < 50 alors que P_DC entrée ≥ 0,3 kW : valeur probablement en kW → NULL ;
      LOAD_AC_INDISPONIBLE  pas de jour réseau, jours instrumentés insuffisants → NULL.
    """
    div = (settings.ac_w_to_kw_divisor if settings else Decimal("1000"))
    base = {"p_ac_aux_kw": None, "statut": AC_INDISPONIBLE, "reference_dates": [], "jours_reference": 0,
            "mesures": None, "mediane_ac_brut": None, "mediane_ac_kw": None, "mediane_p_dc_entree_kw": None,
            "mediane_ecart_brut_kw": None, "jours_ecart_negatif": 0, "unite_brute": "W", "diviseur": div,
            "candidats": [], "reason": None}
    if ctx.off_grid is not False:
        base["reason"] = (f"{AC_INDISPONIBLE} : site off-grid, aucun jour réseau présent pour isoler le load AC"
                          if ctx.off_grid else f"{AC_INDISPONIBLE} : statut réseau inconnu, jours de référence non identifiables")
        return base
    rows = []
    for d in daterange(start - timedelta(days=AC_REFERENCE_LOOKBACK_DAYS), end):
        f = facts.get(d)
        if not f:
            continue
        measured = [source_value(s_, f, ctx)[0] for s_ in (RT_DSE, RT_COMPTEUR, RT_DAY_DG_ON)]
        direct_zero = any(v is not None and v == D0 for v in measured[:2])
        any_running = any(v is not None and v > D0 for v in measured)
        if not direct_zero or any_running:
            continue
        ac_raw = _dec(f.get("ac_active_power_avg_w"))
        p_dc = _dec(f.get("p_dc_day_kw"))
        eff = _dec(f.get("eff_day"))
        if ac_raw is None or ac_raw < D0 or p_dc is None or p_dc < D0 or not _efficiency_ok(eff):
            continue
        rows.append({"date": d, "ac_brut": ac_raw, "ac_kw": ac_raw / div, "p_dc_entree_kw": p_dc / eff,
                     "points": f.get("ac_point_count")})
    base["candidats"] = rows
    base["reference_dates"] = [r["date"] for r in rows]
    base["jours_reference"] = len(rows)
    pts = [r["points"] for r in rows if r["points"] is not None]
    base["mesures"] = sum(pts) if pts else None
    if len(rows) < AC_REFERENCE_MIN_DAYS:
        base["reason"] = (f"{AC_INDISPONIBLE} : {len(rows)} jour(s) réseau sans GE instrumenté(s) "
                          f"< {AC_REFERENCE_MIN_DAYS} requis")
        return base
    med = lambda xs: Decimal(str(statistics.median(xs)))  # noqa: E731
    ac_raw_med = med([r["ac_brut"] for r in rows])
    ac_kw_med = med([r["ac_kw"] for r in rows])
    pdc_med = med([r["p_dc_entree_kw"] for r in rows])
    raw_gaps = [r["ac_kw"] - r["p_dc_entree_kw"] for r in rows]
    gap_med = med(raw_gaps)
    base.update(mediane_ac_brut=ac_raw_med, mediane_ac_kw=ac_kw_med, mediane_p_dc_entree_kw=pdc_med,
                mediane_ecart_brut_kw=gap_med, jours_ecart_negatif=sum(1 for g in raw_gaps if g < D0))
    if ac_raw_med < Decimal("50") and pdc_med >= Decimal("0.3"):
        base["statut"] = AC_UNITE_SUSPECTE
        base["reason"] = (f"{AC_UNITE_SUSPECTE} : ACT_ACTIVE_POWER_AVG médian = {_q(ac_raw_med)} (brut) pour "
                          f"{_q(pdc_med)} kW d'entrée DC : la valeur semble déjà en kW (diviseur {div}) — load AC non qualifié")
        return base
    tolerance = max(Decimal("0.1"), pdc_med * Decimal("0.1"))
    if gap_med < -tolerance:
        base["statut"] = AC_INCOHERENT
        base["reason"] = (f"{AC_INCOHERENT} : P_AC instrumentée ({_q(ac_kw_med)} kW médian) < entrée DC "
                          f"({_q(pdc_med)} kW) sur {base['jours_ecart_negatif']}/{len(rows)} jours : "
                          "le compteur AC ne couvre pas tout le site ou l'unité est erronée — pas de 0 par écrêtage")
        return base
    value = med([max(D0, g) for g in raw_gaps])
    base["p_ac_aux_kw"] = value
    base["statut"] = AC_QUALIFIE if value > D0 else AC_MESURE_ZERO
    return base


def _attempt(applicable: bool = True) -> dict:
    return {"tentee": False, "statut": "NON_APPLICABLE" if not applicable else "NON_TENTEE",
            "valeur_kw": None, "code": None, "motif": None, "detail": None}


def ge_power(ctx: SiteContext, fact: dict | None, runtime_h: Decimal, runtime_source: str, ac_ref: dict | None,
             cap_kw: Decimal | None = None) -> dict:
    """
    Chaîne de repli de la puissance GE pour un jour (runtime > 0) :
      1. DIRECT_DSE_PRODUCTION  DG_PRODUCTION_KWH ÷ runtime (énergie du jour ÷ heures de marche) ;
      2. PDC_REDRESSEUR         outdoor : P_DC pendant GE ÷ rendement (sans batterie) ;
      3. INDOOR_PDC_LOAD_AC     indoor : P_DC pendant GE ÷ rendement + load AC historique qualifié.
    Chaque méthode est tentée, retenue ou rejetée avec un code ; une méthode au-delà du plafond
    1,05 × kVA × 0,8 est rejetée et la suivante est tentée. NULL seulement si toutes échouent.
    """
    trace = {PM_DIRECT: _attempt(), PM_PDC: _attempt(ctx.kind == "OUTDOOR"), PM_INDOOR: _attempt(ctx.kind == "INDOOR")}
    raw = {"production_kwh": None, "p_dse_kw": None, "p_dc_kw": None, "rendement": None, "ac_avg_brut": None}
    fact = fact or {}
    raw["production_kwh"] = _dec(fact.get("dg_production_kwh"))
    raw["ac_avg_brut"] = _dec(fact.get("ac_active_power_avg_w"))

    def capped(method: str, p: Decimal) -> bool:
        if cap_kw is not None and p > cap_kw:
            t = trace[method]
            t.update(statut="REJETEE", code=MC_PUISSANCE_HORS_LIMITE, valeur_kw=p,
                     motif=f"{MC_PUISSANCE_HORS_LIMITE} : {_q(p)} kW > 1,05 × kVA × 0,8 = {_q(cap_kw)} kW")
            return True
        return False

    selected = None
    # 1. Production GE / runtime (toute configuration).
    t = trace[PM_DIRECT]
    t["tentee"] = True
    prod = raw["production_kwh"]
    if prod is None:
        t.update(statut="ABSENTE", code=PR_P_DSE_INDISPONIBLE, motif=f"{PR_P_DSE_INDISPONIBLE} : DG_PRODUCTION_KWH absente ce jour")
    elif prod <= D0:
        t.update(statut="REJETEE", code=PR_P_DSE_INCOHERENTE,
                 motif=f"{PR_P_DSE_INCOHERENTE} : production {prod} kWh alors que runtime {_q(runtime_h)} h > 0")
    else:
        p = prod / runtime_h
        raw["p_dse_kw"] = p
        if not capped(PM_DIRECT, p):
            t.update(statut="RETENUE", valeur_kw=p,
                     detail=f"DG_PRODUCTION_KWH {_q(prod)} kWh ÷ runtime {RUNTIME_LABEL_SHORT.get(runtime_source, runtime_source)} {_q(runtime_h)} h")
            selected = (PM_DIRECT, PW_PRODUCTION, p, t["detail"], {})

    # 2 / 3. Méthodes redresseur (configuration connue uniquement).
    if selected is None and ctx.kind in ("OUTDOOR", "INDOOR"):
        method = PM_PDC if ctx.kind == "OUTDOOR" else PM_INDOOR
        t = trace[method]
        t["tentee"] = True
        p_in, how, reason = dc_input_during_ge(ctx, fact) if fact else (None, None, f"{PR_PDC_GE_INDISPONIBLE} : aucune donnée ce jour")
        if fact:
            if how and "compteur" in how:
                raw["p_dc_kw"], raw["rendement"] = _dec(fact.get("p_dc_ge_tracker_kw")), _dec(fact.get("eff_ge_tracker"))
            elif how:
                raw["p_dc_kw"], raw["rendement"] = _dec(fact.get("p_dc_rect_active_kw")), _dec(fact.get("eff_rect_active"))
        if p_in is None:
            t.update(statut="ABSENTE" if _reason_code(reason, PR_PDC_GE_INDISPONIBLE) == PR_PDC_GE_INDISPONIBLE else "REJETEE",
                     code=_reason_code(reason, PR_PDC_GE_INDISPONIBLE), motif=reason)
        elif method == PM_PDC:
            if not capped(method, p_in):
                t.update(statut="RETENUE", valeur_kw=p_in,
                         detail=f"P_DC ÷ rendement sur {how} ; {BATTERIE_NON_INTEGREE}")
                selected = (method, PW_DC_REDRESSEUR, p_in, t["detail"], {"p_dc_input_kw": p_in})
        else:
            if ac_ref is None or ac_ref["p_ac_aux_kw"] is None:
                reason = (ac_ref or {}).get("reason") or f"{AC_INDISPONIBLE} : profil AC non calculé"
                t.update(statut="REJETEE", code=_reason_code(reason, PR_LOAD_AC_INDISPONIBLE), motif=reason)
            else:
                p = p_in + ac_ref["p_ac_aux_kw"]
                if not capped(method, p):
                    detail = (f"P_DC pendant GE {_q(p_in)} kW ({how}) + load AC historique {_q(ac_ref['p_ac_aux_kw'])} kW "
                              f"[{ac_ref['statut']}, médiane de {ac_ref['jours_reference']} jours] ; {BATTERIE_NON_INTEGREE}")
                    t.update(statut="RETENUE", valeur_kw=p, detail=detail)
                    selected = (method, PW_INDOOR, p, detail, {"p_dc_input_kw": p_in, "p_ac_aux_kw": ac_ref["p_ac_aux_kw"]})
    elif selected is None:
        for m in (PM_PDC, PM_INDOOR):
            trace[m].update(code=PR_CONFIGURATION_INCONNUE,
                            motif=f"{PR_CONFIGURATION_INCONNUE} : configuration Indoor/Outdoor inconnue, méthode redresseur non applicable")

    rejections = [t["motif"] for t in trace.values() if t["motif"] and t["statut"] != "RETENUE"]
    codes = [t["code"] for t in trace.values() if t["code"]]
    if selected:
        method, source, p, detail, extra = selected
        return {"p_ge_kw": p, "source": source, "method": method, "detail": detail, "reason": None, "code": None,
                "trace": trace, "raw": raw, "rejection_codes": codes, "rejections": rejections, **extra}
    if MC_PUISSANCE_HORS_LIMITE in codes:
        code = MC_PUISSANCE_HORS_LIMITE
    elif MC_RENDEMENT_INVALIDE in codes:
        code = MC_RENDEMENT_INVALIDE
    elif ctx.kind not in ("OUTDOOR", "INDOOR"):
        code = PR_CONFIGURATION_INCONNUE
    else:
        code = MC_PUISSANCE_INDISPONIBLE
    return {"p_ge_kw": None, "source": None, "method": None, "detail": None, "code": code,
            "reason": f"{code} : " + " ; ".join(rejections) + " ; aucune méthode de puissance applicable",
            "trace": trace, "raw": raw, "rejection_codes": codes, "rejections": rejections}


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
        "power_method": None, "power_trace": None, "power_raw": None, "power_rejection_codes": [], "power_cap_kw": None,
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

    cap = (POWER_CAP_RATIO * ctx.curve.prp_kva * power_factor
           if ctx.curve is not None and ctx.curve.prp_kva is not None and ctx.curve.prp_kva > D0 else None)
    pw = ge_power(ctx, fact, runtime_h, runtime_source, ac_ref, cap)
    row.update(p_ge_kw=pw["p_ge_kw"], power_source=pw["source"], power_detail=pw["detail"],
               p_dc_input_kw=pw.get("p_dc_input_kw"), p_ac_aux_kw=pw.get("p_ac_aux_kw"),
               power_method=pw["method"], power_trace=pw["trace"], power_raw=pw["raw"],
               power_rejection_codes=pw["rejection_codes"], power_cap_kw=cap)
    missing = []
    if pw["p_ge_kw"] is None:
        status = DAY_PUISSANCE_HORS_PLAFOND if pw["code"] == MC_PUISSANCE_HORS_LIMITE else DAY_PUISSANCE_ABSENTE
        missing.append((status, pw["code"], pw["reason"]))
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


def rapprochement_calc_status(rec: dict | None) -> str:
    """Statut du calcul de rapprochement, séparé du statut CPH."""
    if rec is None or rec.get("stock_initial_l") is None or rec.get("stock_final_l") is None:
        return SR_STOCK_ABSENT
    if rec["statut"] in (R_OK, R_A_JUSTIFIER, R_A_INVESTIGUER):
        return SR_CALCULE
    if rec["statut"] == R_CPH_NON_CALCULE:
        return SR_CPH_INCOMPLET
    return SR_MOUVEMENTS_ABSENTS


# ─── Période complète pour un site ───────────────────────────────────────────

def compute_site_period(ctx: SiteContext, facts: dict, start: date, end: date,
                        observations: list[Observation], settings: EngineSettings) -> dict:
    days = list(daterange(start, end))
    sources_eval = evaluate_runtime_sources(ctx, facts, days)
    ac_ref = indoor_ac_reference(ctx, facts, start, end, settings) if ctx.kind == "INDOOR" else None
    day_rows = {d: compute_day(ctx, d, facts.get(d), sources_eval, ac_ref, settings.nominal_power_factor) for d in days}
    if settings.data_until is not None:
        for d, r in day_rows.items():
            if d > settings.data_until and d not in facts and r["status"] == DAY_RUNTIME_ABSENT:
                r["motif_code"] = MC_PERIODE_INCOMPLETE
                r["motifs"] = [f"{MC_PERIODE_INCOMPLETE} : jour postérieur à la dernière donnée Snowflake "
                               f"({settings.data_until.isoformat()}), runtime non encore disponible"]

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
    # Méthode de puissance retenue sur tous les jours de marche où une puissance est qualifiée.
    power_by_method: dict[str, int] = {}
    running_days = [r for r in day_rows.values() if r["runtime_h"] is not None and r["runtime_h"] > D0]
    for r in running_days:
        if r["power_method"]:
            power_by_method[r["power_method"]] = power_by_method.get(r["power_method"], 0) + 1
    # Jours non calculés : codes de blocage (chaque méthode échouée compte) et runtime concerné.
    blocked: dict[str, dict] = {}
    for r in day_rows.values():
        if r["conso_l"] is not None:
            continue
        codes = set(r["power_rejection_codes"] or []) if r["status"] in (DAY_PUISSANCE_ABSENTE, DAY_PUISSANCE_HORS_PLAFOND) else set()
        if r["motif_code"]:
            codes.add(r["motif_code"])
        if ctx.curve is None and r["runtime_h"] is not None and r["runtime_h"] > D0:
            # Courbe absente : motif de site, compté même si la puissance manque aussi ce jour-là.
            codes.add(ctx.curve_code or MC_MAPPING_A_VALIDER)
        for c in codes:
            b = blocked.setdefault(c, {"jours": 0, "runtime_h": D0, "mesuree_l": None})
            b["jours"] += 1
            if r["runtime_h"] is not None:
                b["runtime_h"] += r["runtime_h"]
            if r["measured_l"] is not None:
                b["mesuree_l"] = (b["mesuree_l"] or D0) + r["measured_l"]
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

    statut_cph = (SC_CPH_CALCULE if cph_status == CPH_COMPLET
                  else SC_CPH_PARTIEL if cph_status == CPH_PARTIEL and cph_days else SC_CPH_NON_CALCULE)
    statut_rappro = rapprochement_calc_status(rapprochement_ref)
    return {
        "statut_cph": statut_cph,
        "statut_rapprochement_calcul": statut_rappro,
        "power_method_days": power_by_method,
        "running_days": len(running_days),
        "blocked_days": blocked,
        "site_id": ctx.site_id, "site_name": ctx.site_name, "country": ctx.country, "zone": ctx.zone,
        "data_id": ctx.data_id, "kind": ctx.kind, "kind_source": ctx.kind_source,
        "grid_supply": ctx.grid_supply, "off_grid": ctx.off_grid, "dg_count": ctx.dg_count,
        "ge_label": ctx.ge_label, "data_issue": ctx.data_issue,
        "facture_avec_ge": ctx.facture_avec_ge, "site_type": ctx.site_type,
        "configuration_fichier": ctx.configuration_fichier,
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
        # Une somme de seuls jours GE à l'arrêt (0 L) n'est pas une conso estimée partielle : NULL.
        "conso_partielle_l": conso_sum if cph_status == CPH_PARTIEL and cph_days else None,
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
