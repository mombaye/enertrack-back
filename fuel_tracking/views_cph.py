# fuel_tracking/views_cph.py
"""
API Suivi Carburant / CPH (instruction globale §9) — calcul à la demande sur
la plage exacte demandée (services/cph_engine.py).

  GET  /api/fuel-tracking/cph/                         synthèse + tableau (pagination serveur)
  GET  /api/fuel-tracking/cph/sites/<site_id>/         détail jour par jour d'un site
  GET  /api/fuel-tracking/cph/health/                  santé du calcul (fraîcheur, couverture, plausibilité)
  GET  /api/fuel-tracking/cph/export/controle/         export « Contrôle complet » (CSV, site × jour)
  GET  /api/fuel-tracking/cph/export/anomalies/        export « Anomalies Fuel » (statut ≠ OK)
  POST /api/fuel-tracking/cph/observations/import/     fichier d'observation standard
  POST /api/fuel-tracking/cph/abaque/import/           abaque CPH PRP 50 Hz (admin, manager)
  GET  /api/fuel-tracking/cph/observations/imports/    historique des imports
  GET  /api/fuel-tracking/cph/referentiel/             courbes + mappages (état de validation)
  POST /api/fuel-tracking/cph/mappings/<id>/validate/  validation métier d'un mappage (admin, manager)
  POST /api/fuel-tracking/cph/mappings/<id>/unvalidate/  retrait manuel (l'automatique ne le réactive plus)
  POST /api/fuel-tracking/cph/mappings/auto-match/      relance de la correspondance automatique (admin, manager)
  POST /api/fuel-tracking/cph/curves/<curve_id>/approve/   vérification métier d'une courbe (information de qualité)
  POST /api/fuel-tracking/cph/curves/<curve_id>/revoke/
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, Max
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel_tracking.services import cph_engine as E
from fuel_tracking.services import cph_matching as M
from fuel_tracking.services.cph_health import cph_health, freshness
from fuel_tracking.services.cph_service import compute_period, compute_period_summary

VALIDATOR_ROLES = {"admin", "manager"}


class IsCphValidator(BasePermission):
    message = "Validation réservée aux rôles admin et manager."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and getattr(request.user, "role", None) in VALIDATOR_ROLES)


def _f(v, places: int = 3):
    if v is None:
        return None
    return round(float(v), places)


def _parse_period(params) -> tuple[date, date]:
    today = timezone.localdate()
    end = date.fromisoformat(params["end"]) if params.get("end") else today - timedelta(days=1)
    start = date.fromisoformat(params["start"]) if params.get("start") else end.replace(day=1)
    return start, end


def _curve_dict(c: E.Curve | None):
    if c is None:
        return None
    return {"curve_id": c.curve_id, "label": c.label, "status": c.status, "prp_kva": _f(c.prp_kva),
            "prp_kw": _f(c.prp_kw), "power_factor": _f(c.power_factor), "a": _f(c.a, 6), "b": _f(c.b, 6),
            "c": _f(c.c, 6), "source": c.source}


def _reconciliation_dict(r: dict | None):
    if r is None:
        return None
    return {
        "observation_start": r["observation_start"], "observation_end": r["observation_end"],
        "stock_initial_l": _f(r["stock_initial_l"]), "stock_final_l": _f(r["stock_final_l"]),
        "livraisons_l": _f(r["livraisons_l"]), "rajouts_l": _f(r["rajouts_l"]), "retraits_l": _f(r["retraits_l"]),
        "vols_l": _f(r["vols_l"]), "vidanges_l": _f(r["vidanges_l"]),
        "livraisons_statut": r["livraisons_statut"], "observation_status": r["observation_status"],
        "import_file": r["import_file"],
        "conso_theorique_l": _f(r["conso_theorique_l"]), "jours_conso_calculee": r["jours_conso_calculee"],
        "jours_observation": r["jours_observation"],
        "conso_stock_l": _f(r["conso_stock_l"]), "ecart_l": _f(r["ecart_l"]), "ecart_pct": _f(r["ecart_pct"], 2),
        "statut": r["statut"], "motifs": r["motifs"],
    }


# Premier point bloquant d'un site, en langage métier (présentation seulement :
# aucune valeur n'est recalculée). Ordre = chaîne de calcul : rattachement des
# données → courbe → runtime → puissance/CPH → observation stock → ENOC.
# Un site qui a un verdict (OK / À justifier / À investiguer) n'a pas de blocage.
BLOCAGE_LABELS = {
    "DONNEES_SITE": ("cph", "Rattachement des données Snowflake à vérifier"),
    "MULTI_GE": ("cph", "Site multi-GE : une courbe par GE non gérée"),
    "TYPE_GE_ABSENT": ("cph", "Type de GE absent de la Base GE"),
    "COURBE_CPH_MANQUANTE": ("cph", "Aucune courbe CPH pour ce type de GE"),
    "MAPPAGE_AMBIGU": ("cph", "Plusieurs courbes candidates (modèle ambigu)"),
    "MAPPAGE_NON_VALIDE": ("cph", "Correspondance plaque → courbe à valider"),
    "RUNTIME_ABSENT": ("cph", "Aucune heure de marche GE fiable"),
    "PUISSANCE_ABSENTE": ("cph", "Puissance GE non disponible"),
    "HORS_COURBE": ("cph", "Charge GE hors du domaine de la courbe"),
    "CPH_PARTIEL": ("cph", "CPH incomplet sur la période du relevé"),
    "OBSERVATION_ABSENTE": ("rapprochement", "Aucun relevé de stock sur la période"),
    "OBSERVATION_INCOMPLETE": ("rapprochement", "Relevé de stock incomplet ou non validé"),
    "LIVRAISONS_ENOC": ("rapprochement", "Livraisons ENOC à contrôler"),
    "HORS_PERIMETRE_GE": ("cph", "Site sans GE confirmé par Snowflake : hors calcul carburant"),
}

# Statut de correspondance (services/cph_matching.py) → point bloquant affiché.
_MATCH_BLOCAGES = {
    M.SITE_MULTI_GE: "MULTI_GE",
    M.TYPE_GE_ABSENT: "TYPE_GE_ABSENT",
    M.REJETE: "MAPPAGE_NON_VALIDE",
    M.COURBE_CPH_MANQUANTE: "COURBE_CPH_MANQUANTE",
    M.MODELE_AMBIGU: "MAPPAGE_AMBIGU",
    M.A_VALIDER: "MAPPAGE_NON_VALIDE",
}

_DAY_STATUS_CODES = {
    E.DAY_RUNTIME_ABSENT: "RUNTIME_ABSENT",
    E.DAY_PUISSANCE_ABSENTE: "PUISSANCE_ABSENTE",
    E.DAY_PUISSANCE_HORS_PLAFOND: "HORS_COURBE",
    E.DAY_CPH_HORS_DOMAINE: "HORS_COURBE",
}


def _ge(r: dict) -> bool:
    return r.get("perimetre", E.PERIMETRE_GE) == E.PERIMETRE_GE


def _blocage_code(r: dict) -> str | None:
    if not _ge(r):
        return "HORS_PERIMETRE_GE"
    if r["data_issue"]:
        return "DONNEES_SITE"
    if r["curve"] is None:
        statut = (r.get("correspondance") or {}).get("statut")
        return _MATCH_BLOCAGES.get(statut, "MAPPAGE_NON_VALIDE")
    if r["runtime_days"] == 0:
        return "RUNTIME_ABSENT"
    blocking = {k: v for k, v in r["day_status_counts"].items() if k in _DAY_STATUS_CODES}
    # Aucun CPH sur la période (seuls d'éventuels jours GE à l'arrêt valent 0 L) : le blocage est au calcul.
    if r["cph_status"] == E.CPH_NON_CALCULE_PERIODE or (r["cph_days"] == 0 and blocking):
        return _DAY_STATUS_CODES[max(blocking, key=blocking.get)] if blocking else "RUNTIME_ABSENT"
    # Un CPH partiel sur la période ne bloque que s'il manque sur la fenêtre du
    # relevé : c'est le rapprochement qui le dit (statut CPH_NON_CALCULE).
    rec = r["rapprochement"]
    if rec is None:
        return "OBSERVATION_ABSENTE"
    if rec["statut"] == E.R_DONNEES_INCOMPLETES:
        if rec.get("livraisons_statut") == E.LIVRAISONS_ENOC_A_CONTROLER:
            return "LIVRAISONS_ENOC"
        return "OBSERVATION_INCOMPLETE"
    if rec["statut"] == E.R_CPH_NON_CALCULE:
        return "CPH_PARTIEL"
    return None


def _blocage(r: dict) -> dict | None:
    code = _blocage_code(r)
    if code is None:
        return None
    etape, label = BLOCAGE_LABELS[code]
    rec = r["rapprochement"] or {}
    if code == "DONNEES_SITE":
        detail = r["data_issue"]
    elif code == "HORS_PERIMETRE_GE":
        detail = (r["motif_cph"] or {}).get("detail")
    elif r["curve"] is None:
        detail = r["curve_reason"]
    elif etape == "rapprochement" or code == "CPH_PARTIEL":
        detail = next((m for m in rec.get("motifs", []) if m), None)
    else:
        day_status = {v: k for k, v in _DAY_STATUS_CODES.items()}.get(code)
        detail = next((m for m in r["motifs"] if day_status and m.startswith(day_status)), None)
    return {"code": code, "etape": etape, "label": label, "detail": detail}


def _site_summary(r: dict) -> dict:
    return {
        "blocage": _blocage(r),
        "perimetre": r.get("perimetre", E.PERIMETRE_GE),
        "site_id": r["site_id"], "site_name": r["site_name"], "country": r["country"], "zone": r["zone"],
        "kind": r["kind"], "kind_source": r["kind_source"], "grid_supply": r["grid_supply"], "off_grid": r["off_grid"],
        "dg_count": r["dg_count"], "ge_label": r["ge_label"], "data_issue": r["data_issue"],
        "start": r["start"], "end": r["end"], "days": r["days"],
        "runtime_days": r["runtime_days"], "runtime_total_h": _f(r["runtime_total_h"]),
        "runtime_source_main": r["runtime_source_main"], "runtime_source_days": r["runtime_source_days"],
        "sources": {s: {"availability_pct": _f(ev["availability"] * 100, 1), "days_valid": ev["days_valid"],
                        "exploitable": ev["exploitable"], "rejection": ev["rejection"]}
                    for s, ev in r["sources"].items()},
        "runtime_source_availability_pct": _f(r["runtime_source_availability"] * 100 if r["runtime_source_availability"] is not None else None, 1),
        "power_source_main": r["power_source_main"], "power_source_days": r["power_source_days"],
        "p_ge_moy_kw": _f(r["p_ge_moy_kw"]),
        "charge_moy_pct": _f(r["charge_moy"] * 100 if r["charge_moy"] is not None else None, 1),
        "curve": _curve_dict(r["curve"]), "curve_reason": r["curve_reason"],
        "correspondance": r["correspondance"],
        "mapping_status": (r["correspondance"] or {}).get("statut"),
        "curve_source_status": (r["correspondance"] or {}).get("curve_source_status"),
        "ge_kva": _f(r["ge_kva"], 1),
        "motif_cph": r["motif_cph"],
        "comparaison": _comparison_dict(r["comparaison"]),
        "conso_specifique": _sfc_dict(r["conso_specifique"]),
        "cph_days": r["cph_days"], "cph_moy_l_h": _f(r["cph_moy_l_h"]),
        "conso_days": r["conso_days"], "conso_theorique_l": _f(r["conso_theorique_l"]),
        "conso_partielle_l": _f(r["conso_partielle_l"]), "cph_status": r["cph_status"],
        "extrapolated_days": r["extrapolated_days"], "day_status_counts": r["day_status_counts"],
        "motifs": r["motifs"],
        "rapprochement_statut": r["rapprochement_statut"],
        "rapprochement": _reconciliation_dict(r["rapprochement"]),
        "observations": len(r["reconciliations"]),
        # Statuts séparés : un CPH calculé sans relevé de stock reste « CPH_CALCULE ».
        "statut_cph": r["statut_cph"],
        "statut_rapprochement_calcul": r["statut_rapprochement_calcul"],
        "facture_avec_ge": r["facture_avec_ge"],
        "site_type": r["site_type"],
        "configuration": r["kind"],
        "configuration_fichier": r["configuration_fichier"],
        "running_days": r["running_days"],
        "power_method_main": _power_method_main(r),
        "power_method_days": r["power_method_days"],
        "blocked_days": {c: {"jours": b["jours"], "runtime_h": _f(b["runtime_h"]), "mesuree_l": _f(b["mesuree_l"])}
                         for c, b in r["blocked_days"].items()},
        "diag_codes": sorted(_diag_codes(r)),
        "ac_statut": (r["ac_reference"] or {}).get("statut"),
        "p_ac_aux_kw": _f((r["ac_reference"] or {}).get("p_ac_aux_kw")),
    }


def _power_method_main(r: dict) -> str | None:
    days = r["power_method_days"]
    return max(days.items(), key=lambda kv: kv[1])[0] if days else None


def _ac_reference_dict(ac: dict | None) -> dict | None:
    """Diagnostic complet du load AC historique indoor (valeurs brutes lues, unités, médianes)."""
    if ac is None:
        return None
    return {
        "statut": ac["statut"], "p_ac_aux_kw": _f(ac["p_ac_aux_kw"]), "reason": ac["reason"],
        "jours_reference": ac["jours_reference"], "mesures": ac["mesures"],
        "reference_dates": ac["reference_dates"],
        "mediane_ac_brut": _f(ac["mediane_ac_brut"], 1), "mediane_ac_kw": _f(ac["mediane_ac_kw"]),
        "mediane_p_dc_entree_kw": _f(ac["mediane_p_dc_entree_kw"]),
        "mediane_ecart_brut_kw": _f(ac["mediane_ecart_brut_kw"]),
        "jours_ecart_negatif": ac["jours_ecart_negatif"],
        "unite_brute": ac["unite_brute"], "unite_convertie": "kW", "diviseur": _f(ac["diviseur"], 0),
        "candidats": [{"date": c["date"], "ac_brut": _f(c["ac_brut"], 1), "ac_kw": _f(c["ac_kw"]),
                       "p_dc_entree_kw": _f(c["p_dc_entree_kw"]), "points": c["points"]} for c in ac["candidats"]],
    }


def _trace_dict(trace: dict | None) -> dict | None:
    if trace is None:
        return None
    return {m: {**t, "valeur_kw": _f(t["valeur_kw"])} for m, t in trace.items()}


def _flat_trace(d: dict) -> dict:
    """Trace à plat demandée pour l'audit (une ligne par site × jour)."""
    t = d["power_trace"] or {}
    get = lambda m, k: (t.get(m) or {}).get(k)  # noqa: E731
    return {
        "direct_dse_attempted": bool(get(E.PM_DIRECT, "tentee")), "direct_dse_status": get(E.PM_DIRECT, "statut"),
        "pdc_rectifier_attempted": bool(get(E.PM_PDC, "tentee")), "pdc_rectifier_status": get(E.PM_PDC, "statut"),
        "indoor_ac_attempted": bool(get(E.PM_INDOOR, "tentee")), "indoor_ac_status": get(E.PM_INDOOR, "statut"),
        "selected_power_method": d["power_method"], "selected_power_kw": _f(d["p_ge_kw"]),
        "rejection_reasons": [x["motif"] for x in t.values() if x.get("motif") and x.get("statut") != "RETENUE"],
    }


# Synthèse des blocages (diagnostic) : codes jour (chaque méthode échouée compte) + codes site.
DIAG_LABELS = {
    E.MC_PERIODE_INCOMPLETE: "Période incomplète (jours après la dernière donnée Snowflake)",
    E.MC_RUNTIME_INDISPONIBLE: "Runtime indisponible",
    E.MC_RUNTIME_NON_QUALIFIE: "Runtime non qualifié",
    E.MC_PUISSANCE_INDISPONIBLE: "Puissance GE indisponible (toutes méthodes échouées)",
    E.PR_P_DSE_INDISPONIBLE: "Puissance DSE / production GE indisponible",
    E.PR_P_DSE_INCOHERENTE: "Production GE incohérente avec le runtime",
    E.PR_PDC_GE_INDISPONIBLE: "P_DC pendant GE indisponible",
    E.PR_PDC_NEGATIF: "P_DC négative",
    E.MC_RENDEMENT_INVALIDE: "Rendement redresseur invalide",
    E.PR_LOAD_AC_INDISPONIBLE: "Load AC historique indisponible",
    E.PR_LOAD_AC_INCOHERENT: "Load AC incohérent (P_AC < P_DC entrée)",
    E.PR_LOAD_AC_UNITE_SUSPECTE: "Unité Active Power Avg suspecte",
    E.AC_MESURE_ZERO: "Load AC réellement égal à zéro (mesuré)",
    E.PR_CONFIGURATION_INCONNUE: "Configuration Indoor/Outdoor inconnue",
    E.MC_PUISSANCE_HORS_LIMITE: "Puissance au-delà de 1,05 × kVA × 0,8",
    E.MC_PUISSANCE_NOMINALE_ABSENTE: "kVA nominal de la courbe absent",
    E.MC_COURBE_CPH_MANQUANTE: "Courbe CPH manquante",
    E.MC_MAPPING_A_VALIDER: "Mapping à valider",
    E.MC_MODELE_AMBIGU: "Modèle ambigu",
    E.MC_TYPE_GE_ABSENT: "Type GE absent",
    E.MC_SITE_MULTI_GE: "Site multi-GE",
    E.SR_STOCK_ABSENT: "Stock absent",
    E.SR_MOUVEMENTS_ABSENTS: "Mouvements stock absents ou non validés (dont livraisons ENOC à contrôler)",
}
# Codes portés par le site (pas par des jours non calculés) : informatif ou rapprochement.
_SITE_DIAG = {E.AC_MESURE_ZERO, E.SR_STOCK_ABSENT, E.SR_MOUVEMENTS_ABSENTS}


def _diag_codes(r: dict) -> set[str]:
    if not _ge(r):
        return set()
    codes = set(r["blocked_days"])
    if (r["ac_reference"] or {}).get("statut") == E.AC_MESURE_ZERO:
        codes.add(E.AC_MESURE_ZERO)
    if r["statut_rapprochement_calcul"] in (E.SR_STOCK_ABSENT, E.SR_MOUVEMENTS_ABSENTS):
        codes.add(r["statut_rapprochement_calcul"])
    return codes


def _diagnostic_blocages(rows: list[dict]) -> list[dict]:
    """
    Par motif : sites, runtime concerné, conso mesurée disponible et volume potentiel non calculé.
    Volume potentiel = runtime des jours bloqués × CPH moyen du site sur ses jours calculés :
    indicatif, seulement si le site a un CPH ailleurs sur la période (jamais une valeur de calcul).
    """
    out = []
    for code, label in DIAG_LABELS.items():
        sites = [r for r in rows if code in _diag_codes(r)]
        runtime, mesuree, potentiel, sites_potentiel = E.D0, None, None, 0
        for r in sites:
            if code in _SITE_DIAG:
                rt = r["runtime_total_h"] or E.D0
                ms = r["comparaison"]["conso_mesuree_l"]
            else:
                b = r["blocked_days"][code]
                rt, ms = b["runtime_h"], b["mesuree_l"]
                if r["cph_moy_l_h"] is not None and rt > E.D0:
                    potentiel = (potentiel or E.D0) + rt * r["cph_moy_l_h"]
                    sites_potentiel += 1
            runtime += rt
            if ms is not None:
                mesuree = (mesuree or E.D0) + ms
        out.append({
            "code": code, "label": label, "type": "site" if code in _SITE_DIAG else "jour",
            "bloquant": code != E.AC_MESURE_ZERO,
            "sites": len(sites), "jours": sum(r["blocked_days"].get(code, {}).get("jours", 0) for r in sites),
            "runtime_h": _f(runtime, 1), "conso_mesuree_l": _f(mesuree, 1),
            "volume_potentiel_l": _f(potentiel, 1), "sites_volume_potentiel": sites_potentiel,
            "filtre": {"diag": code},
        })
    return out


def _pct(num: int, den: int):
    return round(100 * num / den, 1) if den else None


def _couvertures(rows: list[dict]) -> list[dict]:
    """Six couvertures distinctes (jamais un KPI « couverture » ambigu)."""
    with_cph = [r for r in rows if r["statut_cph"] in (E.SC_CPH_CALCULE, E.SC_CPH_PARTIEL)]
    rt_ok = [r for r in rows if r["runtime_days"] > 0]
    running = [r for r in rt_ok if r["running_days"] > 0]
    typed = [r for r in rows if r["ge_label"]]
    items = [
        ("conso_mesuree", "Couverture consommation mesurée",
         sum(1 for r in rows if r["comparaison"]["conso_mesuree_l"] is not None), len(rows),
         "Sites avec une conso mesurée capteur (VW_FUEL_REPORT, QUALITY_STATUS = OK, ≥ 2 points) sur la période "
         "÷ sites GE du périmètre. Ne dit rien du CPH."),
        ("runtime", "Couverture runtime qualifié", len(rt_ok), len(rows),
         "Sites avec au moins un jour de runtime qualifié (source disponible ≥ 50 %) ÷ sites GE."),
        ("mapping", "Couverture mapping GE → courbe",
         sum(1 for r in typed if r["curve"] is not None), len(typed),
         "Sites dont la plaque GE a une courbe utilisable (auto-validée ou validée) ÷ sites GE avec un type de GE connu."),
        ("puissance", "Couverture puissance GE qualifiée",
         sum(1 for r in running if r["power_method_days"]), len(running),
         "Sites avec au moins un jour de puissance GE qualifiée (production, P_DC/η ou P_DC + load AC) "
         "÷ sites avec runtime qualifié et GE en marche."),
        ("cph", "Couverture CPH calculé", len(with_cph), len(rows),
         "Sites avec une conso estimée CPH complète ou partielle ÷ sites GE "
         f"(complet : {sum(1 for r in with_cph if r['statut_cph'] == E.SC_CPH_CALCULE)}, "
         f"partiel : {sum(1 for r in with_cph if r['statut_cph'] == E.SC_CPH_PARTIEL)})."),
        ("rapprochement", "Couverture rapprochement stock",
         sum(1 for r in with_cph if r["statut_rapprochement_calcul"] == E.SR_CALCULE), len(with_cph),
         "Sites avec stock et mouvements complets et validés (écart calculé) ÷ sites avec CPH calculé."),
    ]
    return [{"code": c, "label": lbl, "numerateur": n, "denominateur": d, "pct": _pct(n, d), "definition": df}
            for c, lbl, n, d, df in items]


def _sfc_dict(c: dict) -> dict:
    return {**c, "energie_ge_kwh": _f(c["energie_ge_kwh"], 1), "estimee_l_kwh": _f(c["estimee_l_kwh"], 3),
            "mesuree_l_kwh": _f(c["mesuree_l_kwh"], 3), "plage_l_kwh": [_f(v, 2) for v in c["plage_l_kwh"]]}


def _comparison_dict(c: dict) -> dict:
    return {**c, **{k: _f(c[k], 1 if k == "ecart_pct" else 3) for k in (
        "conso_mesuree_l", "conso_estimee_communs_l", "conso_mesuree_communs_l", "ecart_l", "ecart_pct")}}


def _day_dict(d: dict) -> dict:
    return {
        "date": d["date"], "runtime_h": _f(d["runtime_h"]), "runtime_source": d["runtime_source"],
        "runtime_motifs": d["runtime_motifs"], "raw": {k: _f(v) for k, v in d["raw"].items()},
        "p_ge_kw": _f(d["p_ge_kw"]), "power_source": d["power_source"], "power_detail": d["power_detail"],
        "p_dc_input_kw": _f(d["p_dc_input_kw"]), "p_ac_aux_kw": _f(d["p_ac_aux_kw"]),
        "curve_id": d["curve_id"], "charge_pct": _f(d["charge"] * 100 if d["charge"] is not None else None, 1),
        "cph_l_h": _f(d["cph_l_h"]), "conso_l": _f(d["conso_l"]), "measured_l": _f(d["measured_l"]),
        "extrapolated": d["extrapolated"],
        "status": d["status"], "motif_code": d["motif_code"], "motifs": d["motifs"],
        "power_method": d["power_method"], "power_trace": _trace_dict(d["power_trace"]),
        "power_cap_kw": _f(d["power_cap_kw"]),
        "power_raw": {k: _f(v) for k, v in (d["power_raw"] or {}).items()} or None,
        "power_rejection_codes": d["power_rejection_codes"],
        **_flat_trace(d),
    }


def _apply_table_filters(rows: list[dict], params) -> list[dict]:
    pe = (params.get("perimetre") or "").strip()
    if pe:
        rows = [r for r in rows if r.get("perimetre", E.PERIMETRE_GE) == pe]
    site = (params.get("site") or "").strip().lower()
    if site:
        rows = [r for r in rows if site in r["site_id"].lower() or site in (r["site_name"] or "").lower()]
    rt = (params.get("runtime_source") or "").strip()
    if rt:
        rows = [r for r in rows if (r["runtime_source_main"] or "AUCUNE") == rt]
    pw = (params.get("power_source") or "").strip()
    if pw:
        rows = [r for r in rows if (r["power_source_main"] or "AUCUNE") == pw]
    st = (params.get("statut") or "").strip()
    if st:
        rows = [r for r in rows if r["rapprochement_statut"] == st]
    cst = (params.get("cph_status") or "").strip()
    if cst:
        rows = [r for r in rows if r["cph_status"] == cst]
    bl = (params.get("blocage") or "").strip()
    if bl:
        rows = [r for r in rows if _blocage_code(r) == bl]
    sc = (params.get("statut_conso") or "").strip()
    if sc:
        rows = [r for r in rows if r["comparaison"]["statut"] == sc]
    co = (params.get("correspondance") or params.get("mapping_status") or "").strip()
    if co:
        rows = [r for r in rows if (r["correspondance"] or {}).get("statut") == co]
    cs = (params.get("curve_source_status") or "").strip()
    if cs:
        rows = [r for r in rows if (r["correspondance"] or {}).get("curve_source_status") == cs]
    mc = (params.get("motif_cph") or "").strip()
    if mc:
        rows = [r for r in rows if (r["motif_cph"] or {}).get("code") == mc]
    if (params.get("alerte_sfc") or "").strip() in ("1", "true"):
        rows = [r for r in rows if r["conso_specifique"]["alerte_estimee"] or r["conso_specifique"]["alerte_mesuree"]]
    for key, getter in (("statut_cph", lambda r: r["statut_cph"]),
                        ("statut_rapprochement_calcul", lambda r: r["statut_rapprochement_calcul"]),
                        ("power_method", lambda r: _power_method_main(r) or "AUCUNE"),
                        ("configuration", lambda r: r["kind"] or "INCONNUE")):
        v = (params.get(key) or "").strip()
        if v:
            rows = [r for r in rows if getter(r) == v]
    dg = (params.get("diag") or "").strip()
    if dg:
        rows = [r for r in rows if dg in _diag_codes(r)]
    dr = (params.get("dispo_runtime") or "").strip()
    if dr in _DISPO_FILTERS:
        rows = [r for r in rows if _DISPO_FILTERS[dr](r["runtime_source_availability"])]
    return rows


# Disponibilité de la source runtime retenue (fraction 0-1 ; None = aucune source retenue).
_DISPO_FILTERS = {
    "90+": lambda a: a is not None and a >= Decimal("0.9"),
    "50+": lambda a: a is not None and a >= Decimal("0.5"),
    "lt50": lambda a: a is not None and a < Decimal("0.5"),
    "aucune": lambda a: a is None,
}


def _synthesis(all_rows: list[dict]) -> dict:
    # Indicateurs CPH sur les seuls sites avec GE confirmé ; les autres sont comptés à part.
    rows = [r for r in all_rows if _ge(r)]
    others = [r for r in all_rows if not _ge(r)]
    count = lambda pred: sum(1 for r in rows if pred(r))  # noqa: E731
    return {
        "sites": len(rows),
        "sites_total": len(all_rows),
        "sites_sans_ge": len(others),
        "sites_ge_a_confirmer": sum(1 for r in others if (r["motif_cph"] or {}).get("code") == E.MC_GE_A_CONFIRMER),
        "cph_calcules": count(lambda r: r["cph_days"] > 0),
        "conso_theorique_complete": count(lambda r: r["cph_status"] == E.CPH_COMPLET),
        "cph_non_calcule": count(lambda r: r["cph_status"] == E.CPH_NON_CALCULE_PERIODE),
        "rapprochements_calcules": count(lambda r: r["rapprochement_statut"] in (E.R_OK, E.R_A_JUSTIFIER, E.R_A_INVESTIGUER)),
        "ok": count(lambda r: r["rapprochement_statut"] == E.R_OK),
        "a_justifier": count(lambda r: r["rapprochement_statut"] == E.R_A_JUSTIFIER),
        "a_investiguer": count(lambda r: r["rapprochement_statut"] == E.R_A_INVESTIGUER),
        "donnees_incompletes": count(lambda r: r["rapprochement_statut"] == E.R_DONNEES_INCOMPLETES),
        "rapprochement_cph_non_calcule": count(lambda r: r["rapprochement_statut"] == E.R_CPH_NON_CALCULE),
        "blocages": _blocages_summary(rows),
        "couvertures": _couvertures(rows),
        "diagnostic_blocages": _diagnostic_blocages(rows),
        "statuts_cph": _count_by(rows, lambda r: r["statut_cph"]),
        "statuts_rapprochement_calcul": _count_by(rows, lambda r: r["statut_rapprochement_calcul"]),
        "methodes_puissance": _count_by(rows, _power_method_main),
        "load_ac_statuts": _count_by(rows, lambda r: (r["ac_reference"] or {}).get("statut")),
        "conso": _conso_summary(rows),
        "correspondances": _count_by(rows, lambda r: (r["correspondance"] or {}).get("statut")),
        "courbes_appliquees": _count_by(rows, lambda r: M.curve_source_status(r["curve"].status) if r["curve"] else None),
        "motifs_cph": _count_by(rows, lambda r: (r["motif_cph"] or {}).get("code")),
        "alertes_sfc": {
            "estimee": sum(1 for r in rows if r["conso_specifique"]["alerte_estimee"]),
            "mesuree": sum(1 for r in rows if r["conso_specifique"]["alerte_mesuree"]),
        },
    }


def _count_by(rows: list[dict], key) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        k = key(r)
        if k:
            out[k] = out.get(k, 0) + 1
    return out


def _conso_summary(rows: list[dict]) -> dict:
    """Totaux conso estimée / mesurée : seules les valeurs calculées sont sommées, jamais un manque à 0."""
    complete = [r for r in rows if r["conso_theorique_l"] is not None]
    measured = [r for r in rows if r["comparaison"]["conso_mesuree_l"] is not None]
    compared = [r for r in rows if r["comparaison"]["ecart_l"] is not None]
    return {
        "sites_estimee_complete": len(complete),
        "sites_estimee_partielle": sum(1 for r in rows if r["cph_status"] == E.CPH_PARTIEL),
        "sites_estimee_non_calculee": sum(1 for r in rows if r["cph_status"] == E.CPH_NON_CALCULE_PERIODE),
        "total_estimee_complete_l": _f(sum((r["conso_theorique_l"] for r in complete), E.D0)) if complete else None,
        "sites_mesure": len(measured),
        "total_mesuree_l": _f(sum((r["comparaison"]["conso_mesuree_l"] for r in measured), E.D0)) if measured else None,
        "sites_compares": len(compared),
        "statuts": _count_by(rows, lambda r: r["comparaison"]["statut"]),
    }


def _blocages_summary(rows: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for r in rows:
        code = _blocage_code(r)
        if code:
            counts[code] = counts.get(code, 0) + 1
    return [{"code": c, "etape": BLOCAGE_LABELS[c][0], "label": BLOCAGE_LABELS[c][1], "sites": n}
            for c, n in sorted(counts.items(), key=lambda kv: -kv[1])]


def _meta(request=None) -> dict:
    from django.conf import settings
    from fuel_tracking.models import (
        CphCurve, CphInventoryMapping, FuelDailyFactsSyncRun, FuelObservationImport, FuelSiteDailyFacts,
    )

    last_run = FuelDailyFactsSyncRun.objects.order_by("-started_at").first()
    last_obs = FuelObservationImport.objects.order_by("-uploaded_at").first()
    curves = list(CphCurve.objects.all())
    last_curve = max(curves, key=lambda c: c.imported_at, default=None)
    return {
        "abaque_file": last_curve.abaque_file if last_curve else None,
        "abaque_imported_at": last_curve.imported_at if last_curve else None,
        "rule_version": E.RULE_VERSION,
        "enoc_deliveries_connected": bool(getattr(settings, "FUEL_ENOC_DELIVERIES_CONNECTED", False)),
        "facts_last_date": FuelSiteDailyFacts.objects.aggregate(v=Max("date"))["v"],
        "facts_last_sync": {"status": last_run.status, "at": last_run.finished_at or last_run.started_at,
                            "error": last_run.error_message} if last_run else None,
        "curves_total": len(curves),
        "curves_usable": sum(1 for c in curves if c.is_usable),
        "mappings_total": CphInventoryMapping.objects.count(),
        "mappings_validated": CphInventoryMapping.objects.filter(
            match_status__in=M.USABLE_MATCH_STATUSES).exclude(validated_curve=None).count(),
        "mappings_by_status": dict(CphInventoryMapping.objects.order_by().values_list("match_status").annotate(n=Count("id"))),
        "max_period_days": E.MAX_PERIOD_DAYS,
        "observations_last_import": {
            "file_name": last_obs.file_name, "at": last_obs.uploaded_at,
            "rows_imported": last_obs.rows_imported, "rows_rejected": last_obs.rows_rejected,
        } if last_obs else None,
        "can_validate": bool(request and getattr(request.user, "role", None) in VALIDATOR_ROLES),
        **{k: v for k, v in freshness().items() if k != "facts_last_date"},
    }


def _compute(request, with_daily: bool = True):
    """with_daily=False : tableau et synthèse (calcul mis en cache) ; True : exports jour par jour."""
    start, end = _parse_period(request.query_params)
    fn = compute_period if with_daily else compute_period_summary
    rows = fn(start, end, country=request.query_params.get("country") or None,
              zone=request.query_params.get("zone") or None)
    return start, end, rows


def _periode_info(start: date, end: date) -> dict:
    """PÉRIODE INCOMPLÈTE : la période demandée dépasse les faits Snowflake disponibles."""
    last = freshness()["facts_last_date"]
    missing = (end - last).days if last and end > last else ((end - start).days + 1 if last is None else 0)
    return {"incomplete": missing > 0, "donnees_jusqu_au": last,
            "jours_sans_donnees": min(missing, (end - start).days + 1)}


class CphPeriodView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            start, end, rows = _compute(request, with_daily=False)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        synthesis = _synthesis(rows)
        filtered = _apply_table_filters(rows, request.query_params)
        try:
            page = max(1, int(request.query_params.get("page", 1)))
            limit = min(200, max(1, int(request.query_params.get("limit", 50))))
        except ValueError:
            page, limit = 1, 50
        total = len(filtered)
        total_pages = max(1, (total + limit - 1) // limit)
        page_rows = filtered[(page - 1) * limit: page * limit]
        return Response({
            "start": start, "end": end, "days": (end - start).days + 1,
            "synthesis": synthesis,
            "data": [_site_summary(r) for r in page_rows],
            "pagination": {"page": page, "limit": limit, "total": total, "totalPages": total_pages,
                           "hasNext": page < total_pages, "hasPrev": page > 1},
            "periode": _periode_info(start, end),
            "filters": {
                "perimetres": {E.PERIMETRE_GE: synthesis["sites"], E.PERIMETRE_SANS_GE: synthesis["sites_sans_ge"]},
                "power_methods": sorted({_power_method_main(r) or "AUCUNE" for r in rows}),
                "runtime_sources": sorted({r["runtime_source_main"] or "AUCUNE" for r in rows}),
                "power_sources": sorted({r["power_source_main"] or "AUCUNE" for r in rows}),
                "zones": sorted({r["zone"] for r in rows if r["zone"]}),
                "countries": sorted({r["country"] for r in rows if r["country"]}),
            },
            "meta": _meta(request),
        })


class CphHealthView(APIView):
    """Santé du calcul : fraîcheur, synchro, référentiel, couverture, plausibilité L/kWh."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(cph_health())


class CphSiteDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, site_id):
        try:
            start, end = _parse_period(request.query_params)
            rows = compute_period(start, end, site_ids=[site_id])
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        if not rows:
            return Response({"detail": "Site absent du référentiel (inventaire Snowflake et fichiers Ops)."}, status=404)
        r = rows[0]
        return Response({
            **_site_summary(r),
            "ac_reference": _ac_reference_dict(r["ac_reference"]),
            "batterie": E.BATTERIE_NON_INTEGREE,
            "periode": _periode_info(start, end),
            "reconciliations": [_reconciliation_dict(x) for x in r["reconciliations"]],
            "daily": [_day_dict(d) for d in r["daily"]],
        })


EXPORT_HEADER = [
    "site_id", "site_name", "pays", "zone", "type_site", "reseau", "ge_inventaire", "puissance_nominale_ge_kva",
    "periode_debut", "periode_fin", "date",
    "runtime_h", "source_runtime", "disponibilite_runtime_retenue_pct", "valeurs_brutes_runtime", "disponibilite_sources_pct", "motif_rejet_sources",
    "p_ge_kw", "source_puissance", "detail_puissance", "p_dc_entree_kw", "load_ac_historique_kw", "dates_reference_ac",
    "mapping_status", "correspondance_score", "correspondance_methode", "correspondance_date",
    "courbe_id", "curve_source_status", "prp_kva", "prp_kw", "cos_phi", "a", "b", "c",
    "charge_pct", "cph_l_h", "conso_estimee_l", "conso_mesuree_jour_l", "extrapolation_sous_50pct", "statut_jour",
    "motif_cph_jour", "motifs_jour",
    "conso_theorique_periode_l", "statut_cph_periode", "motif_cph_periode",
    "conso_mesuree_periode_l", "jours_communs", "ecart_mesuree_estimee_l", "ecart_mesuree_estimee_pct",
    "statut_conso", "motif_conso",
    "energie_ge_periode_kwh", "conso_specifique_estimee_l_kwh", "conso_specifique_mesuree_l_kwh", "alertes_conso_specifique",
    "stock_initial_l", "livraisons_l", "rajouts_l", "retraits_l", "vols_l", "vidanges_l", "stock_final_l",
    "statut_livraisons", "conso_stock_l", "ecart_l", "ecart_pct", "statut", "motifs_rapprochement",
    # Chaîne puissance complète (méthodes tentées, valeurs brutes) et statuts séparés.
    "facture_avec_ge", "type_site_on_off", "configuration", "configuration_fichier",
    "statut_cph", "statut_rapprochement_calcul", "jours_calcules", "jours_periode", "jours_ge_en_marche",
    "methode_puissance_retenue", "puissance_retenue_kw",
    "m1_direct_dse_tentee", "m1_direct_dse_statut", "m1_direct_dse_kw", "m1_direct_dse_motif",
    "m2_pdc_redresseur_tentee", "m2_pdc_redresseur_statut", "m2_pdc_redresseur_kw", "m2_pdc_redresseur_motif",
    "m3_indoor_load_ac_tentee", "m3_indoor_load_ac_statut", "m3_indoor_load_ac_kw", "m3_indoor_load_ac_motif",
    "production_ge_kwh", "p_dse_kw", "p_dc_brut_kw", "rendement_brut", "active_power_avg_brut_w",
    "plafond_puissance_kw", "codes_rejet_puissance",
    "load_ac_statut", "load_ac_jours_reference", "load_ac_mesures", "load_ac_mediane_brut_w",
    "load_ac_mediane_kw", "load_ac_mediane_p_dc_entree_kw", "load_ac_diviseur", "load_ac_motif",
    "batterie",
    "formule", "version_regle",
]
FORMULA = ("charge = P_GE / (kVA × 0,8) ; CPH = a·charge² + b·charge + c ; conso estimée = runtime × CPH ; "
           "conso_stock = initial + livraisons + rajouts − retraits − vols − vidanges − final ; écart = conso_stock − conso_théorique")


def _csv_rows(rows: list[dict], only_anomalies: bool):
    for r in rows:
        if not _ge(r):
            if not only_anomalies:
                yield _out_of_scope_line(r)
            continue
        c, rec, ac = r["curve"], r["rapprochement"] or {}, r["ac_reference"] or {}
        avail = " | ".join(f"{s}={ev['availability'] * 100:.0f}%" for s, ev in r["sources"].items())
        rejections = " | ".join(f"{s}: {ev['rejection']}" for s, ev in r["sources"].items() if ev["rejection"])
        ref_dates = ", ".join(d.isoformat() for d in ac.get("reference_dates", []))
        match, cmp_, sfc = r["correspondance"] or {}, r["comparaison"], r["conso_specifique"]
        site_ok = r["rapprochement_statut"] == E.R_OK
        avail_main = r["runtime_source_availability"]
        motif_site = r["motif_cph"] or {}
        for d in r["daily"]:
            # Anomalies : lignes non OK (site sans verdict OK) ou jour sans conso estimée calculée.
            if only_anomalies and site_ok and d["status"] in (E.DAY_CPH_CALCULE, E.DAY_GE_ARRET):
                continue
            yield [
                r["site_id"], r["site_name"], r["country"], r["zone"], r["kind"], r["grid_supply"], r["ge_label"],
                _f(r["ge_kva"], 1),
                r["start"], r["end"], d["date"],
                _f(d["runtime_h"]), d["runtime_source"], _f(avail_main * 100 if avail_main is not None else None, 1),
                " | ".join(f"{k}={_f(v)}" for k, v in d["raw"].items()),
                avail, rejections,
                _f(d["p_ge_kw"]), d["power_source"], d["power_detail"], _f(d["p_dc_input_kw"]), _f(d["p_ac_aux_kw"]),
                ref_dates if d["power_source"] == E.PW_INDOOR else "",
                match.get("statut"), match.get("score"), match.get("methode"),
                match["date"].isoformat() if match.get("date") else "",
                c.curve_id if c else match.get("courbe_id") or "", match.get("curve_source_status") or "",
                _f(c.prp_kva) if c else "", _f(c.prp_kw) if c else "", _f(c.power_factor) if c else "",
                _f(c.a, 6) if c else "", _f(c.b, 6) if c else "", _f(c.c, 6) if c else "",
                _f(d["charge"] * 100 if d["charge"] is not None else None, 1), _f(d["cph_l_h"]), _f(d["conso_l"]),
                _f(d["measured_l"]),
                "oui" if d["extrapolated"] else "non", d["status"], d["motif_code"] or "", " ; ".join(d["motifs"]),
                _f(r["conso_theorique_l"]), r["cph_status"], motif_site.get("code") or "",
                _f(cmp_["conso_mesuree_l"]), cmp_["jours_communs"], _f(cmp_["ecart_l"]), _f(cmp_["ecart_pct"], 2),
                cmp_["statut"], cmp_["motif"],
                _f(sfc["energie_ge_kwh"], 1), _f(sfc["estimee_l_kwh"]), _f(sfc["mesuree_l_kwh"]), " ; ".join(sfc["alertes"]),
                _f(rec.get("stock_initial_l")), _f(rec.get("livraisons_l")), _f(rec.get("rajouts_l")),
                _f(rec.get("retraits_l")), _f(rec.get("vols_l")), _f(rec.get("vidanges_l")), _f(rec.get("stock_final_l")),
                rec.get("livraisons_statut", ""), _f(rec.get("conso_stock_l")), _f(rec.get("ecart_l")),
                _f(rec.get("ecart_pct"), 2), r["rapprochement_statut"],
                " ; ".join(rec.get("motifs", [])) or ("aucune observation de stock sur la période" if not rec else ""),
                *_power_columns(r, d, ac),
                FORMULA, E.RULE_VERSION,
            ]


def _out_of_scope_line(r: dict) -> list:
    """Site sans GE confirmé : une ligne période, sans valeur calculée (jamais 0 L)."""
    line = dict.fromkeys(EXPORT_HEADER, "")
    line.update({
        "site_id": r["site_id"], "site_name": r["site_name"], "pays": r["country"], "zone": r["zone"],
        "type_site": r["kind"], "reseau": r["grid_supply"], "ge_inventaire": r["ge_label"],
        "puissance_nominale_ge_kva": _f(r["ge_kva"], 1), "periode_debut": r["start"], "periode_fin": r["end"],
        "statut_cph_periode": r["statut_cph"], "motif_cph_periode": (r["motif_cph"] or {}).get("code"),
        "statut_conso": r["comparaison"]["statut"], "motif_conso": r["comparaison"]["motif"],
        "facture_avec_ge": {True: "oui", False: "non"}.get(r["facture_avec_ge"], ""), "type_site_on_off": r["site_type"],
        "configuration": r["kind"] or "INCONNUE", "configuration_fichier": r["configuration_fichier"],
        "statut_cph": r["statut_cph"], "jours_periode": r["days"], "statut": r["rapprochement_statut"],
        "formule": FORMULA, "version_regle": E.RULE_VERSION,
    })
    return [line[h] for h in EXPORT_HEADER]


def _power_columns(r: dict, d: dict, ac: dict) -> list:
    t, raw = d["power_trace"] or {}, d["power_raw"] or {}
    cols = [
        {True: "oui", False: "non"}.get(r["facture_avec_ge"], ""), r["site_type"], r["kind"] or "INCONNUE",
        r["configuration_fichier"], r["statut_cph"], r["statut_rapprochement_calcul"],
        r["conso_days"], r["days"], r["running_days"],
        d["power_method"], _f(d["p_ge_kw"]),
    ]
    for m in E.POWER_METHODS:
        x = t.get(m) or {}
        cols += ["oui" if x.get("tentee") else "non", x.get("statut") or "", _f(x.get("valeur_kw")), x.get("motif") or ""]
    cols += [
        _f(raw.get("production_kwh")), _f(raw.get("p_dse_kw")), _f(raw.get("p_dc_kw")), _f(raw.get("rendement"), 4),
        _f(raw.get("ac_avg_brut"), 1), _f(d["power_cap_kw"]), " | ".join(d["power_rejection_codes"] or []),
        ac.get("statut") or "", ac.get("jours_reference") if ac else "", ac.get("mesures") if ac else "",
        _f(ac.get("mediane_ac_brut"), 1), _f(ac.get("mediane_ac_kw")), _f(ac.get("mediane_p_dc_entree_kw")),
        _f(ac.get("diviseur"), 0), ac.get("reason") or "",
        E.BATTERIE_NON_INTEGREE if d["power_method"] in (E.PM_PDC, E.PM_INDOOR) else "",
    ]
    return cols


class _CphExportView(APIView):
    permission_classes = [IsAuthenticated]
    only_anomalies = False
    prefix = ""

    def get(self, request):
        try:
            start, end, rows = _compute(request)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        rows = _apply_table_filters(rows, request.query_params)
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = f'attachment; filename="{self.prefix}_{start}_{end}.csv"'
        response.write("﻿")
        writer = csv.writer(response, delimiter=";")
        writer.writerow(EXPORT_HEADER)
        for line in _csv_rows(rows, self.only_anomalies):
            writer.writerow(["" if v is None else v for v in line])
        return response


class CphExportControleView(_CphExportView):
    prefix = "controle_complet_cph"


class CphExportAnomaliesView(_CphExportView):
    only_anomalies = True
    prefix = "anomalies_fuel"


class CphObservationImportView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request):
        from fuel_tracking.services.fuel_observation_import import import_observations

        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Aucun fichier fourni."}, status=400)
        try:
            imp = import_observations(f.name, f.read(), request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        return Response({"id": imp.id, "file_name": imp.file_name, "rule_version": imp.rule_version,
                         "rows_total": imp.rows_total, "rows_imported": imp.rows_imported,
                         "rows_rejected": imp.rows_rejected, "errors": imp.errors[:50]}, status=201)


class CphAbaqueImportView(APIView):
    permission_classes = [IsCphValidator]
    parser_classes = [MultiPartParser]

    def post(self, request):
        from fuel_tracking.services.cph_abaque_import import apply_abaque, parse_abaque

        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Aucun fichier fourni."}, status=400)
        if not f.name.lower().endswith((".xlsx", ".xlsm")):
            return Response({"detail": "Format attendu : ABAQUE_CPH_GE_PRP_50HZ.xlsx"}, status=400)
        try:
            parsed = parse_abaque(f)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        reset = apply_abaque(parsed, f.name)
        return Response({
            "file_name": f.name,
            "curves": len(parsed["curves"]),
            "status_counts": parsed["status_counts"],
            "mappings": len(parsed["mappings"]),
            "warnings": parsed["warnings"],
            "validations_reset": reset,
        }, status=201)


class CphObservationImportListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import FuelObservationImport

        return Response([{
            "id": i.id, "file_name": i.file_name, "uploaded_by": getattr(i.uploaded_by, "username", None),
            "uploaded_at": i.uploaded_at, "rule_version": i.rule_version, "rows_total": i.rows_total,
            "rows_imported": i.rows_imported, "rows_rejected": i.rows_rejected,
        } for i in FuelObservationImport.objects.select_related("uploaded_by")[:20]])


class CphReferentielView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import CphCurve, CphInventoryMapping, CphMappingHistory

        curves = {c.curve_id: c for c in CphCurve.objects.select_related("business_approved_by")}

        def curve_payload(c):
            return {"curve_id": c.curve_id, "manufacturer": c.manufacturer, "model": c.model, "variant": c.variant,
                    "prp_kva": _f(c.prp_kva), "prp_kw": _f(c.prp_kw), "power_factor": _f(c.power_factor),
                    "conso_50_l_h": _f(c.conso_50_l_h), "conso_75_l_h": _f(c.conso_75_l_h), "conso_100_l_h": _f(c.conso_100_l_h),
                    "a": _f(c.coef_a, 6), "b": _f(c.coef_b, 6), "c": _f(c.coef_c, 6), "status": c.status,
                    "curve_source_status": M.curve_source_status(c.status),
                    "is_usable": c.is_usable, "business_approved": c.business_approved,
                    "business_approved_by": getattr(c.business_approved_by, "username", None),
                    "business_approved_at": c.business_approved_at, "source": c.source, "source_url": c.source_url,
                    "note": c.note}

        history: dict[int, list] = {}
        for h in CphMappingHistory.objects.select_related("changed_by").order_by("-changed_at", "-id"):
            history.setdefault(h.mapping_id, []).append(h)
        mappings = []
        for m in CphInventoryMapping.objects.select_related("validated_curve", "validated_by"):
            mappings.append({
                "id": m.id, "inventory_label": m.inventory_label, "inventory_kva": _f(m.inventory_kva),
                "site_count": m.site_count, "abaque_status": m.abaque_status, "action_required": m.action_required,
                "candidates": [curve_payload(curves[cid]) for cid in m.candidate_curve_ids if cid in curves],
                "validated_curve_id": m.validated_curve.curve_id if m.validated_curve else None,
                "validated_by": getattr(m.validated_by, "username", None), "validated_at": m.validated_at,
                "validation_comment": m.validation_comment,
                "match_status": m.match_status, "mapping_status": m.match_status, "match_score": m.match_score,
                "match_method": m.match_method, "match_reasons": m.match_reasons, "matched_at": m.matched_at,
                "history": [{
                    "old_status": h.old_status, "new_status": h.new_status, "old_curve_id": h.old_curve_id,
                    "new_curve_id": h.new_curve_id, "score": h.score, "rule": h.rule,
                    "changed_by": getattr(h.changed_by, "username", None), "changed_at": h.changed_at, "comment": h.comment,
                } for h in history.get(m.id, [])[:10]],
            })
        return Response({
            "can_validate": getattr(request.user, "role", None) in VALIDATOR_ROLES,
            "curves": [curve_payload(c) for c in curves.values()],
            "mappings": mappings,
        })


class CphMappingValidateView(APIView):
    permission_classes = [IsCphValidator]

    def post(self, request, pk):
        from fuel_tracking.models import CphCurve, CphInventoryMapping, CphMappingHistory

        m = CphInventoryMapping.objects.filter(pk=pk).first()
        if m is None:
            return Response({"detail": "Mappage introuvable."}, status=404)
        curve_id = request.data.get("curve_id")
        comment = (request.data.get("comment") or "").strip()
        if curve_id not in m.candidate_curve_ids:
            return Response({"detail": f"La courbe {curve_id} n'est pas candidate pour « {m.inventory_label} »."}, status=400)
        if not comment:
            return Response({"detail": "Commentaire de validation requis (ex. référence de plaque signalétique)."}, status=400)
        curve = CphCurve.objects.get(curve_id=curve_id)
        old_status, old_curve = m.match_status, m.validated_curve.curve_id if m.validated_curve else None
        # Correction manuelle : prioritaire sur l'automatique, le statut d'origine de la courbe est inchangé.
        m.validated_curve = curve
        m.validated_by = request.user
        m.validated_at = timezone.now()
        m.validation_comment = comment
        m.match_status = CphInventoryMapping.MatchStatus.VALIDE_MANUELLEMENT
        m.match_method = M.METHOD_MANUAL
        m.matched_at = m.validated_at
        m.match_reasons = [f"validé manuellement par {request.user.username} : {comment}"]
        m.save(update_fields=["validated_curve", "validated_by", "validated_at", "validation_comment",
                              "match_status", "match_method", "matched_at", "match_reasons"])
        M.record_history(CphMappingHistory, m, old_status, old_curve, curve_id, "validation manuelle", m.validated_at,
                         user=request.user, comment=comment)
        return Response({"id": m.id, "validated_curve_id": curve_id, "validated_at": m.validated_at,
                         "match_status": m.match_status})


class CphMappingUnvalidateView(APIView):
    """Rejet manuel : statut REJETE, l'automatique ne le réactive plus (une validation manuelle reste possible)."""
    permission_classes = [IsCphValidator]

    def post(self, request, pk):
        from fuel_tracking.models import CphInventoryMapping, CphMappingHistory

        m = CphInventoryMapping.objects.select_related("validated_curve").filter(pk=pk).first()
        if m is None:
            return Response({"detail": "Mappage introuvable."}, status=404)
        comment = (request.data.get("comment") or "").strip()
        old_status, old_curve = m.match_status, m.validated_curve.curve_id if m.validated_curve else None
        now = timezone.now()
        m.validated_curve, m.validated_by, m.validated_at, m.validation_comment = None, None, None, ""
        m.match_status = CphInventoryMapping.MatchStatus.REJETE
        m.match_method = M.METHOD_MANUAL_REMOVAL
        m.matched_at = now
        m.match_reasons = [f"correspondance rejetée par {request.user.username}" + (f" : {comment}" if comment else "")]
        m.save(update_fields=["validated_curve", "validated_by", "validated_at", "validation_comment",
                              "match_status", "match_method", "matched_at", "match_reasons"])
        M.record_history(CphMappingHistory, m, old_status, old_curve, None, "rejet manuel", now,
                         user=request.user, comment=comment)
        return Response({"id": pk, "validated_curve_id": None, "match_status": m.match_status})


class CphMappingAutoMatchView(APIView):
    """Relance la correspondance automatique ; reset=true réévalue aussi les retraits manuels."""
    permission_classes = [IsCphValidator]

    def post(self, request):
        from fuel_tracking.models import CphCurve, CphInventoryMapping, CphMappingHistory

        qs = CphInventoryMapping.objects.all()
        if str(request.data.get("reset_removals", "")).lower() in ("1", "true"):
            qs.filter(match_method=M.METHOD_MANUAL_REMOVAL).update(match_method="")
        counts = M.apply_auto_matching(CphInventoryMapping.objects.all(), list(CphCurve.objects.all()), timezone.now(),
                                       history_model=CphMappingHistory)
        return Response({"counts": counts})


class CphCurveApproveView(APIView):
    permission_classes = [IsCphValidator]
    approve = True

    def post(self, request, curve_id):
        from fuel_tracking.models import CphCurve

        c = CphCurve.objects.filter(curve_id=curve_id).first()
        if c is None:
            return Response({"detail": "Courbe introuvable."}, status=404)
        comment = (request.data.get("comment") or "").strip()
        if self.approve:
            if c.status == CphCurve.Status.VALIDE_CONSTRUCTEUR:
                return Response({"detail": "Courbe déjà validée constructeur."}, status=400)
            if not comment:
                return Response({"detail": "Commentaire d'activation requis (justification métier)."}, status=400)
            c.business_approved, c.business_approved_by, c.business_approved_at = True, request.user, timezone.now()
            c.business_approval_comment = comment
            c.save()
            return Response({"curve_id": c.curve_id, "is_usable": c.is_usable})
        # Vérification métier de la courbe : information de qualité, sans effet sur les correspondances.
        c.business_approved, c.business_approved_by, c.business_approved_at = False, None, None
        c.business_approval_comment = ""
        c.save()
        return Response({"curve_id": c.curve_id, "is_usable": c.is_usable})


class CphCurveRevokeView(CphCurveApproveView):
    approve = False
