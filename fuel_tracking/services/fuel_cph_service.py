# fuel_tracking/services/fuel_cph_service.py
"""
Agrégation Python du calcul CPH — applique les paramètres GE (Postgres,
FuelCphGeParameter) aux énergies journalières brutes renvoyées par
fuel_cph_snowflake.fetch_daily_tracker_energy, détermine le statut de chaque
jour (spec section 8), calcule les litres estimés (jours OK/OVER_CAPACITY
uniquement — "sans litre inventé"), puis agrège par site sur le mois.

Ordre des gardes de qualité (une seule cause retenue par jour, la première
qui s'applique) :
  1. MISSING_LOAD_POWER  — aucun intervalle GE détecté ce jour, ou énergie
     de charge site indisponible.
  2. MISSING_PARAMETER   — aucune fiche FuelCphGeParameter active à cette
     date pour ce site (impossible de convertir l'énergie en litres).
  3. BATTERY_DATA_NOT_READY — moins de 95% des intervalles GE avec une
     mesure batterie valide.
  4. NO_VALID_RUNTIME / RUNTIME_NOT_VALIDATED_FOR_INTERVAL_CPH — pas de
     runtime DSE de contrôle, ou écart > 0.15h avec le runtime déduit du
     compteur tracker.
  5. OK (ou OVER_CAPACITY, informatif — charge GE calculée > 100% de la
     capacité déclarée, litres quand même produits).
"""
from collections import Counter
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from fuel_tracking.models import FuelCphGeParameter
from fuel_tracking.services.fuel_cph_snowflake import (
    fetch_daily_tracker_energy,
    fetch_monthly_runtime_fallback,
    fetch_site_ge_specs,
    fetch_site_rectifier_efficiency,
)

BATTERY_COVERAGE_MIN = Decimal("0.95")
RUNTIME_TOLERANCE_H = Decimal("0.15")
DEFAULT_POWER_FACTOR = Decimal("0.8")  # cos φ standard — informatif uniquement (n'affecte pas les litres), voir compute_daily_status.

_EMPTY_RESULT = {
    "battery_charge_ac_energy_kwh": None,
    "total_ge_energy_kwh": None,
    "average_ge_power_kw": None,
    "cph_estimated_lph": None,
    "estimated_consumption_l": None,
    "ge_load_percent": None,
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
    Applique les gardes de qualité et le calcul CPH pour un (site, date).

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
    site_load_energy_kwh = energies.get("site_load_energy_kwh")
    battery_dc_energy_kwh = energies.get("battery_dc_energy_kwh")
    valid_battery_intervals = energies.get("valid_battery_intervals") or 0

    if ge_intervals == 0 or site_load_energy_kwh is None or not dg_runtime_interval_h:
        return "MISSING_LOAD_POWER", dict(_EMPTY_RESULT)

    if params is None:
        return "MISSING_PARAMETER", dict(_EMPTY_RESULT)

    rectifier_efficiency_ratio = params.rectifier_efficiency_ratio if params.rectifier_efficiency_ratio is not None else rectifier_efficiency_fallback
    if rectifier_efficiency_ratio is None or params.spc_l_per_kwh is None:
        return "MISSING_PARAMETER", dict(_EMPTY_RESULT)

    if Decimal(valid_battery_intervals) / Decimal(ge_intervals) < BATTERY_COVERAGE_MIN:
        return "BATTERY_DATA_NOT_READY", dict(_EMPTY_RESULT)

    if dg_runtime_controller_h is None:
        return "NO_VALID_RUNTIME", dict(_EMPTY_RESULT)

    if abs(dg_runtime_interval_h - dg_runtime_controller_h) > RUNTIME_TOLERANCE_H:
        return "RUNTIME_NOT_VALIDATED_FOR_INTERVAL_CPH", dict(_EMPTY_RESULT)

    battery_ac = (battery_dc_energy_kwh or Decimal("0")) / rectifier_efficiency_ratio
    total_ge = site_load_energy_kwh + battery_ac
    avg_power = total_ge / dg_runtime_interval_h
    cph_lph = (avg_power * params.spc_l_per_kwh).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    consumption_l = (total_ge * params.spc_l_per_kwh).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    pge_kva = params.pge_kva if params.pge_kva is not None else (ge_specs or {}).get("pge_kva")
    power_factor = params.power_factor if params.power_factor is not None else DEFAULT_POWER_FACTOR

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
    }


def compute_monthly_cph_estimates(year: int, month: int, site_ids: list[str] | None = None) -> dict:
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

        for d, energies in sorted(days.items()):
            params = _params_for_date(entries, d)
            status, computed = compute_daily_status(energies, params, ge_specs, rectifier_fallback)
            status_counts[status] += 1

            # Le runtime GE (compteur tracker) est compté sur TOUS les jours
            # détectés, OK ou non — il reflète le fonctionnement réel du GE
            # (télémétrie), indépendamment de la disponibilité des paramètres
            # nécessaires au calcul des litres.
            if energies.get("dg_runtime_interval_h") is not None:
                runtime_total += energies["dg_runtime_interval_h"]

            if status in ("OK", "OVER_CAPACITY") and computed["estimated_consumption_l"] is not None:
                ok_consumptions.append(computed["estimated_consumption_l"])
                ok_cphs.append(computed["cph_estimated_lph"])

            daily_rows.append({
                "site_id": site_id,
                "date": d,
                **energies,
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

        monthly[site_id] = {
            "conso_estimee_cph_l": sum(ok_consumptions) if ok_consumptions else None,
            "cph_l_per_h_moy": (sum(ok_cphs) / len(ok_cphs)).quantize(Decimal("0.001")) if ok_cphs else None,
            "cph_nb_jours_ok": nb_jours_ok,
            "cph_nb_jours_calcules": nb_jours_calcules,
            "cph_calculation_status": dominant_status,
            "cph_status_breakdown": dict(status_counts),
            "cph_runtime_h_total": runtime_total.quantize(Decimal("0.01")) if runtime_total > 0 else None,
            "cph_runtime_source": "TRACKER_5MIN" if runtime_total > 0 else None,
            "cph_ge_type": (ge_specs or {}).get("ge_type"),
        }

    # Repli Running Time (GENSET_REPORT, priorité DSE > DG-On calculé) — pour
    # les sites du périmètre demandé qui n'ont AUCUNE donnée tracker 5 min ce
    # mois-ci (compteur DG_TOTAL_RUNNING_TIME_MINUTES jamais remonté, vérifié
    # 2026-08 sur des sites qui ont pourtant d'autres télémétries). Donne un
    # Running Time exploitable mais PAS une estimation de litres : l'énergie
    # (charge site + batterie) n'est intégrable qu'à partir du compteur 5 min.
    if site_ids:
        missing_or_no_runtime = [
            sid for sid in site_ids
            if sid not in monthly or monthly[sid]["cph_runtime_h_total"] is None
        ]
        if missing_or_no_runtime:
            fallback_by_site = fetch_monthly_runtime_fallback(year, month, site_ids=missing_or_no_runtime)
            # ge_specs déjà connus pour les sites présents dans `raw` — pour les
            # autres (jamais vus dans la télémétrie 5 min), un seul appel groupé
            # plutôt qu'un par site.
            extra_specs = fetch_site_ge_specs([sid for sid in fallback_by_site if sid not in ge_specs_by_site])
            for site_id, fb in fallback_by_site.items():
                ge_specs = ge_specs_by_site.get(site_id) or extra_specs.get(site_id)
                if site_id in monthly:
                    monthly[site_id]["cph_runtime_h_total"] = fb["runtime_h"]
                    monthly[site_id]["cph_runtime_source"] = fb["source"]
                else:
                    monthly[site_id] = {
                        "conso_estimee_cph_l": None,
                        "cph_l_per_h_moy": None,
                        "cph_nb_jours_ok": 0,
                        "cph_nb_jours_calcules": 0,
                        "cph_calculation_status": "MISSING_LOAD_POWER",
                        "cph_status_breakdown": {},
                        "cph_runtime_h_total": fb["runtime_h"],
                        "cph_runtime_source": fb["source"],
                        "cph_ge_type": (ge_specs or {}).get("ge_type"),
                    }

    return {"daily": daily_rows, "monthly": monthly}
