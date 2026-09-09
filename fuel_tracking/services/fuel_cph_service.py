# fuel_tracking/services/fuel_cph_service.py
"""
Agrégation Python du calcul CPH — applique les paramètres GE (Postgres,
FuelCphGeParameter) aux énergies journalières brutes renvoyées par
fuel_cph_snowflake.fetch_daily_tracker_energy, détermine le statut de chaque
jour, calcule les litres estimés (jours OK/OVER_CAPACITY uniquement — "sans
litre inventé"), puis agrège par site sur le mois.

Deux chemins d'intégration énergie, selon l'ancrage du jour (spec 2026-09,
point 3 : "les 35 cas DG-On ont un runtime mais MISSING_LOAD_POWER alors que
la charge existe" — corrigé en distinguant ces 2 chemins plutôt qu'un seul
qui supposait toujours un intervalle tracker) :
  - Path A (tracker actif ce jour, ge_intervals > 0) : intégration fine 5 min
    (site_load_energy_kwh/battery_dc_energy_kwh déjà sommés côté Snowflake),
    validée contre le DSE (tolérance 0.15h) quand le DSE est disponible.
  - Path B (DSE/DG-On/redresseur présents mais AUCUN intervalle tracker ce
    jour) : énergie de repli = load_kw (LOAD_REPORT) × runtime_h métier
    résolu — pas de comparaison possible avec un compteur tracker absent,
    donc pas de garde BATTERY_DATA_NOT_READY/RUNTIME_NOT_VALIDATED sur ce
    chemin (rien à valider), battery_dc_energy_kwh traité comme 0 (aucune
    télémétrie batterie sans tracker).

Ordre des gardes de qualité (une seule cause retenue par jour, la première
qui s'applique) :
  1. DSE_ZERO_SOURCE_CONFLICT — DSE=0 alors qu'une autre source (tracker/
     DG-On/redresseur) est positive ce jour : conflit signalé tel quel,
     AUCUN repli automatique (spec 2026-09, point 5).
  2. NO_VALID_RUNTIME — aucune source de runtime métier valide DU TOUT (ni
     DSE, ni tracker, ni DG-On calculé, ni redresseur 5 min).
  3. DSE=0 confirmé (aucune autre source positive) — 0h métier réel, renvoyé
     directement en OK/0 L : mathématiquement forcé par un runtime nul, pas
     un litre inventé.
  4. MISSING_LOAD_POWER — Path A : énergie de charge tracker indisponible.
     Path B : runtime résolu mais load_kw (LOAD_REPORT) absent — la source
     déjà résolue est CONSERVÉE (dg_runtime_business_source inchangé), seul
     le statut du jour devient MISSING_LOAD_POWER (spec 2026-09, point 3).
  5. MISSING_PARAMETER — aucune fiche FuelCphGeParameter active à cette
     date pour ce site (impossible de convertir l'énergie en litres).
  6. BATTERY_DATA_NOT_READY — Path A uniquement : moins de 95% des
     intervalles GE avec une mesure batterie valide.
  7. RUNTIME_NOT_VALIDATED_FOR_INTERVAL_CPH — Path A uniquement : le DSE est
     absent (rien pour valider le compteur tracker) ou s'écarte de plus de
     0.15h du runtime déduit de ce compteur.
  8. OK (ou OVER_CAPACITY, informatif — charge GE calculée > 100% de la
     capacité déclarée, litres quand même produits).

NOT_APPLICABLE_NO_GE (site sans GE confirmé par Postgres has_genset — pas un
signal Snowflake) est appliqué en aval par compute_monthly_cph_estimates
quand `site_has_genset` est fourni, pas par cette fonction : elle ne reçoit
jamais l'information has_genset.
"""
from collections import Counter
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fuel_tracking.models import FuelCphGeParameter
from fuel_tracking.services.fuel_cph_snowflake import (
    RUNTIME_DSE_ZERO_CONFIRMED,
    RUNTIME_DSE_ZERO_CONFLICT,
    RUNTIME_NO_VALID,
    fetch_daily_tracker_energy,
    fetch_site_ge_specs,
    fetch_site_rectifier_efficiency,
)

BATTERY_COVERAGE_MIN = Decimal("0.95")
RUNTIME_TOLERANCE_H = Decimal("0.15")
DEFAULT_POWER_FACTOR = Decimal("0.8")  # cos φ standard — informatif uniquement (n'affecte pas les litres), voir compute_daily_status.
NOT_APPLICABLE_NO_GE = "NOT_APPLICABLE_NO_GE"

_EMPTY_RESULT = {
    "battery_charge_ac_energy_kwh": None,
    "total_ge_energy_kwh": None,
    "average_ge_power_kw": None,
    "cph_estimated_lph": None,
    "estimated_consumption_l": None,
    "ge_load_percent": None,
    "pge_kva": None,
    "power_factor": None,
    "spc_l_per_kwh": None,
    "conso_estimee_source": None,
}


def _load_active_parameters(site_ids: list[str]) -> dict[str, list[tuple]]:
    """site_id -> [(valid_from, valid_to, FuelCphGeParameter), ...] trié par
    valid_from — peu de fiches par site en pratique, recherche linéaire ok."""
    by_site: dict[str, list[tuple]] = {}
    qs = FuelCphGeParameter.objects.filter(site_id__in=site_ids).order_by("site_id", "valid_from")
    for p in qs:
        by_site.setdefault(p.site_id, []).append((p.valid_from, p.valid_to, p))
    return by_site


def _params_for_date(entries: list[tuple], d: date) -> FuelCphGeParameter | None:
    for valid_from, valid_to, p in entries:
        if valid_from <= d and (valid_to is None or valid_to >= d):
            return p
    return None


def compute_daily_status(
    energies: dict,
    params: FuelCphGeParameter | None,
    ge_specs: dict | None = None,
    rectifier_efficiency_fallback: Decimal | None = None,
) -> tuple[str, dict]:
    """
    Applique les gardes de qualité et le calcul CPH pour un (site, date) —
    voir le docstring du module pour l'ordre des 8 gardes et les 2 chemins
    d'intégration énergie (Path A tracker actif / Path B GENSET_REPORT seul).

    `ge_specs` (optionnel, {"pge_kva":..., "ge_type":...}) vient de Snowflake
    SITE_DG (fetch_site_ge_specs) — utilisé pour PGE_KVA si `params.pge_kva`
    n'est pas renseigné dans le fichier de référence. power_factor retombe
    sur DEFAULT_POWER_FACTOR (0.8) si absent. Les deux ne servent QU'à
    l'indicateur informatif ge_load_percent/OVER_CAPACITY — jamais au calcul
    des litres, donc leur absence ne bloque jamais un résultat OK.

    `rectifier_efficiency_fallback` (optionnel) vient de Snowflake
    GFMS_DATA_TRACKER_NC (fetch_site_rectifier_efficiency, moyenne du mois) —
    utilisé si `params.rectifier_efficiency_ratio` n'est pas renseigné dans
    le fichier. Contrairement à pge_kva/power_factor, ce champ ENTRE dans le
    calcul des litres : si ni le fichier ni Snowflake ne le fournissent,
    aucun litre ne peut être produit (MISSING_PARAMETER), même avec un SPC
    valide.
    """
    ge_intervals = energies.get("ge_intervals") or 0
    dg_runtime_interval_h = energies.get("dg_runtime_interval_h")
    dg_runtime_controller_h = energies.get("dg_runtime_controller_h")
    load_kw = energies.get("load_kw")
    runtime_h = energies.get("dg_runtime_business_h")
    runtime_status = energies.get("dg_runtime_business_status")
    battery_dc_energy_kwh = energies.get("battery_dc_energy_kwh")
    valid_battery_intervals = energies.get("valid_battery_intervals") or 0

    # 1-2 : conflit de sources ou aucun runtime résolu — rien à calculer, la
    # source/le motif de rejet sont déjà tracés en amont (dg_runtime_business_*).
    if runtime_status in (RUNTIME_DSE_ZERO_CONFLICT, RUNTIME_NO_VALID):
        return runtime_status, dict(_EMPTY_RESULT)

    # 3 : DSE=0 confirmé (aucune autre source positive) — 0h métier réel,
    # 0 L mathématiquement forcé, sans avoir besoin de params/SPC : pas un
    # litre inventé, c'est l'absence totale d'activité qui l'impose.
    if runtime_status == RUNTIME_DSE_ZERO_CONFIRMED:
        return "OK", dict(_EMPTY_RESULT, cph_estimated_lph=Decimal("0"), estimated_consumption_l=Decimal("0"))

    tracker_active = ge_intervals > 0 and bool(dg_runtime_interval_h)

    if tracker_active:
        # Path A — intégration 5 min déjà sommée côté Snowflake.
        site_load_energy_kwh = energies.get("site_load_energy_kwh")
        if site_load_energy_kwh is None:
            return "MISSING_LOAD_POWER", dict(_EMPTY_RESULT)
        conso_estimee_source = "CPH_TRACKER_5MIN"
    else:
        # Path B — DSE/DG-On/redresseur présents mais aucun intervalle
        # tracker ce jour : énergie de repli = load_kw (LOAD_REPORT) ×
        # runtime_h métier résolu (spec 2026-09, point 3).
        if runtime_h is None or runtime_h <= 0:
            return (runtime_status or "NO_VALID_RUNTIME"), dict(_EMPTY_RESULT)
        if load_kw is None:
            # La source déjà résolue (dg_runtime_business_source) est
            # CONSERVÉE — seul le statut du jour devient MISSING_LOAD_POWER.
            return "MISSING_LOAD_POWER", dict(_EMPTY_RESULT)
        site_load_energy_kwh = load_kw * runtime_h
        dg_runtime_interval_h = runtime_h  # dénominateur d'intégration ; aucune validation DSE possible sans compteur tracker.
        conso_estimee_source = "CPH_GENSET_DAILY_AVG"

    if params is None:
        return "MISSING_PARAMETER", dict(_EMPTY_RESULT)

    # PGE_KVA/POWER_FACTOR résolus dès ici (fichier > Snowflake > défaut) pour
    # être tracés dans TOUTES les issues à partir de ce point, y compris les
    # jours rejetés, même si ces 2 champs ne conditionnent jamais le calcul
    # des litres lui-même.
    pge_kva = params.pge_kva if params.pge_kva is not None else (ge_specs or {}).get("pge_kva")
    power_factor = params.power_factor if params.power_factor is not None else DEFAULT_POWER_FACTOR
    traced = dict(_EMPTY_RESULT, pge_kva=pge_kva, power_factor=power_factor, spc_l_per_kwh=params.spc_l_per_kwh)

    rectifier_efficiency_ratio = params.rectifier_efficiency_ratio if params.rectifier_efficiency_ratio is not None else rectifier_efficiency_fallback
    if rectifier_efficiency_ratio is None or params.spc_l_per_kwh is None:
        return "MISSING_PARAMETER", traced

    if tracker_active:
        # Gardes propres au chemin tracker — sans objet en Path B (pas de
        # compteur tracker à comparer/valider).
        if Decimal(valid_battery_intervals) / Decimal(ge_intervals) < BATTERY_COVERAGE_MIN:
            return "BATTERY_DATA_NOT_READY", traced
        if dg_runtime_controller_h is None:
            return "RUNTIME_NOT_VALIDATED_FOR_INTERVAL_CPH", traced
        if abs(dg_runtime_interval_h - dg_runtime_controller_h) > RUNTIME_TOLERANCE_H:
            return "RUNTIME_NOT_VALIDATED_FOR_INTERVAL_CPH", traced

    battery_ac = (battery_dc_energy_kwh or Decimal("0")) / rectifier_efficiency_ratio
    total_ge = site_load_energy_kwh + battery_ac
    avg_power = total_ge / dg_runtime_interval_h
    cph_lph = (avg_power * params.spc_l_per_kwh).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    consumption_l = (total_ge * params.spc_l_per_kwh).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    ge_load_pct = None
    if pge_kva and power_factor:
        ge_load_pct = (Decimal("100") * avg_power / (pge_kva * power_factor)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    status = "OVER_CAPACITY" if (ge_load_pct is not None and ge_load_pct > 100) else "OK"

    return status, {
        "battery_charge_ac_energy_kwh": battery_ac,
        "total_ge_energy_kwh": total_ge,
        "average_ge_power_kw": avg_power,
        "cph_estimated_lph": cph_lph,
        "estimated_consumption_l": consumption_l,
        "ge_load_percent": ge_load_pct,
        "pge_kva": pge_kva,
        "power_factor": power_factor,
        "spc_l_per_kwh": params.spc_l_per_kwh,
        "conso_estimee_source": conso_estimee_source,
    }


def compute_monthly_cph_estimates(
    year: int, month: int, site_ids: list[str] | None = None,
    site_has_genset: dict[str, bool] | None = None,
) -> dict:
    """
    Retourne {
      "daily": [{"site_id", "date", <énergies brutes>, "calculation_status", <résultats calculés>}, ...],
      "monthly": {site_id: {conso_estimee_cph_l, cph_l_per_h_moy, cph_nb_jours_ok,
                             cph_nb_jours_calcules, cph_calculation_status, cph_status_breakdown,
                             cph_runtime_h_total, cph_runtime_source, cph_ge_type}},
    }
    "daily" reproduit une ligne par (site, date) avec au moins un intervalle
    GE — destiné à FuelCphGeDaily. "monthly" est l'agrégat destiné aux
    colonnes CPH de FuelConsommationMonthly.

    `site_has_genset` (optionnel, {site_id: bool}) — vient de Postgres
    (FuelConsommationMonthly.has_genset, PAS de Snowflake : "sans GE" est un
    statut métier déjà résolu ailleurs dans l'app, pas recalculé ici). Un
    site avec has_genset=False voit TOUS ses jours et son agrégat mensuel
    forcés à NOT_APPLICABLE_NO_GE (spec 2026-09, règle 1 : "runtime non
    applicable" plutôt qu'un statut de rejet type NO_VALID_RUNTIME, qui
    laisserait croire à un problème de télémétrie sur un site qui n'a
    simplement pas de GE). Omis (None) par défaut : aucun site n'est
    reclassé (comportement historique, utile aux tests qui n'ont pas besoin
    de charger Postgres).
    """
    raw = fetch_daily_tracker_energy(year, month, site_ids=site_ids)
    params_by_site = _load_active_parameters(list(raw.keys()))
    ge_specs_by_site = fetch_site_ge_specs(list(raw.keys()))
    rectifier_fallback_by_site = fetch_site_rectifier_efficiency(year, month, list(raw.keys()))

    daily_rows: list[dict] = []
    monthly: dict[str, dict] = {}

    for site_id, days in raw.items():
        entries = params_by_site.get(site_id, [])
        ge_specs = ge_specs_by_site.get(site_id)
        rectifier_fallback = rectifier_fallback_by_site.get(site_id)
        status_counts: Counter = Counter()
        ok_consumptions: list[Decimal] = []
        ok_cphs: list[Decimal] = []
        runtime_total = Decimal("0")
        runtime_source_hours: dict[str, Decimal] = {}
        last_pge_kva = last_power_factor = last_spc = None
        site_load_total = battery_dc_total = battery_ac_total = total_ge_total = Decimal("0")
        has_energy_detail = False

        for d, energies in sorted(days.items()):
            params = _params_for_date(entries, d)
            status, computed = compute_daily_status(energies, params, ge_specs, rectifier_fallback)
            status_counts[status] += 1

            # Dernière valeur connue de PGE_KVA/POWER_FACTOR/SPC_L_PER_KWH sur
            # le mois — stable en pratique (une fiche FuelCphGeParameter ne
            # change pas d'un jour à l'autre), affichée telle quelle pour
            # audit visuel sur la page (spec section 2.2/7).
            if computed.get("spc_l_per_kwh") is not None:
                last_spc = computed["spc_l_per_kwh"]
            if computed.get("pge_kva") is not None:
                last_pge_kva = computed["pge_kva"]
            if computed.get("power_factor") is not None:
                last_power_factor = computed["power_factor"]

            # Runtime GE compté sur TOUS les jours détectés, OK ou non — il
            # reflète le fonctionnement réel du GE, indépendamment de la
            # disponibilité des paramètres nécessaires au calcul des litres.
            # Utilise dg_runtime_business_h/source résolu par
            # _resolve_business_runtime (fuel_cph_snowflake.py, 8 règles :
            # DSE > tracker > DG-On [non-hybride]/redresseur [hybride], DSE=0
            # distingué en confirmé/conflit) — PAS le compteur tracker seul.
            # business_source n'est renseigné QUE pour les 4 sources
            # physiques (DSE_CONTROLLER/TRACKER_5MIN/DG_ON_CALCULATED/
            # RECTIFIER_STATUS_5MIN) ; None pour ZERO_CONFIRMED/CONFLICT/
            # NO_VALID (rien à additionner, 0h ou runtime non résolu).
            business_h = energies.get("dg_runtime_business_h")
            business_source = energies.get("dg_runtime_business_source")
            if business_source is not None and business_h is not None:
                runtime_total += business_h
                runtime_source_hours[business_source] = runtime_source_hours.get(business_source, Decimal("0")) + business_h

            if status in ("OK", "OVER_CAPACITY") and computed["estimated_consumption_l"] is not None:
                ok_consumptions.append(computed["estimated_consumption_l"])
                ok_cphs.append(computed["cph_estimated_lph"])
                has_energy_detail = True
                site_load_total += energies.get("site_load_energy_kwh") or Decimal("0")
                battery_dc_total += energies.get("battery_dc_energy_kwh") or Decimal("0")
                battery_ac_total += computed["battery_charge_ac_energy_kwh"] or Decimal("0")
                total_ge_total += computed["total_ge_energy_kwh"] or Decimal("0")

            daily_rows.append({
                "site_id": site_id,
                "date": d,
                **energies,
                "dg_runtime_interval_status": "VALID" if energies.get("dg_runtime_interval_h") else "NO_INTERVALS",
                "calculation_status": status,
                **computed,
            })

        nb_jours_ok = status_counts.get("OK", 0) + status_counts.get("OVER_CAPACITY", 0)
        nb_jours_calcules = sum(status_counts.values())

        # Statut mensuel affiché : si au moins un jour a produit des litres,
        # on affiche "OK" (le détail des jours rejetés reste disponible via
        # cph_status_breakdown) — un statut "dominant" purement au vote
        # afficherait MISSING_PARAMETER même quand la moitié du mois a un
        # vrai résultat partiel, ce qui masquerait un chiffre valide.
        if nb_jours_ok > 0:
            dominant_status = "OK"
        elif status_counts:
            dominant_status = status_counts.most_common(1)[0][0]
        else:
            dominant_status = None

        # Source "dominante" du mois = celle qui a fourni le plus d'heures
        # cumulées, pas la plus fréquente en nombre de jours — un site peut
        # avoir 2 jours DSE (10h) et 20 jours tracker (2h chacun, 40h) :
        # dominante = tracker par heures, ce qui reflète mieux "d'où vient le
        # chiffre affiché" que de compter les jours.
        if runtime_source_hours:
            dominant_runtime_source = max(runtime_source_hours.items(), key=lambda kv: kv[1])[0]
        else:
            dominant_runtime_source = None

        monthly[site_id] = {
            "conso_estimee_cph_l": sum(ok_consumptions) if ok_consumptions else None,
            "cph_l_per_h_moy": (sum(ok_cphs) / len(ok_cphs)).quantize(Decimal("0.001")) if ok_cphs else None,
            "cph_nb_jours_ok": nb_jours_ok,
            "cph_nb_jours_calcules": nb_jours_calcules,
            "cph_calculation_status": dominant_status,
            "cph_status_breakdown": dict(status_counts),
            "cph_runtime_h_total": runtime_total.quantize(Decimal("0.01")) if runtime_total > 0 else None,
            "cph_runtime_source": dominant_runtime_source,
            "cph_runtime_source_breakdown": (
                {k: float(v.quantize(Decimal("0.01"))) for k, v in runtime_source_hours.items()}
                if runtime_source_hours else None
            ),
            "cph_ge_type": (ge_specs or {}).get("ge_type"),
            "cph_pge_kva": last_pge_kva,
            "cph_power_factor": last_power_factor,
            "cph_spc_l_per_kwh": last_spc,
            "cph_site_load_energy_kwh": site_load_total.quantize(Decimal("0.001")) if has_energy_detail else None,
            "cph_battery_dc_energy_kwh": battery_dc_total.quantize(Decimal("0.001")) if has_energy_detail else None,
            "cph_battery_ac_energy_kwh": battery_ac_total.quantize(Decimal("0.001")) if has_energy_detail else None,
            "cph_total_ge_energy_kwh": total_ge_total.quantize(Decimal("0.001")) if has_energy_detail else None,
        }

    # Sites sans GE (Postgres, pas Snowflake) : reclassés NOT_APPLICABLE_NO_GE
    # plutôt que de laisser un statut de rejet (NO_VALID_RUNTIME, etc.) qui
    # laisserait croire à un problème de télémétrie sur un site qui n'a
    # simplement pas de groupe électrogène. N'affecte que les sites déjà
    # présents dans `monthly`/`daily_rows` (ceux que Snowflake a renvoyés) —
    # un site sans GE ET sans aucune télémétrie n'a de toute façon aucune
    # ligne à reclasser ici.
    if site_has_genset:
        no_ge_sites = {sid for sid in monthly if site_has_genset.get(sid) is False}
        for row in daily_rows:
            if row["site_id"] in no_ge_sites:
                row["dg_runtime_business_source"] = None
                row["dg_runtime_business_status"] = NOT_APPLICABLE_NO_GE
                row["dg_runtime_business_rejection_reason"] = None
                row["calculation_status"] = NOT_APPLICABLE_NO_GE
                for key in _EMPTY_RESULT:
                    row[key] = None
        for sid in no_ge_sites:
            monthly[sid] = {
                **monthly[sid],
                "conso_estimee_cph_l": None,
                "cph_l_per_h_moy": None,
                "cph_calculation_status": NOT_APPLICABLE_NO_GE,
                "cph_runtime_h_total": None,
                "cph_runtime_source": None,
                "cph_runtime_source_breakdown": None,
            }

    return {"daily": daily_rows, "monthly": monthly}
