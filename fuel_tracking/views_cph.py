# fuel_tracking/views_cph.py
"""
API Suivi Carburant / CPH (instruction globale §9) — calcul à la demande sur
la plage exacte demandée (services/cph_engine.py).

  GET  /api/fuel-tracking/cph/                         synthèse + tableau (pagination serveur)
  GET  /api/fuel-tracking/cph/sites/<site_id>/         détail jour par jour d'un site
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
from fuel_tracking.services.cph_service import compute_period

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


def _blocage_code(r: dict) -> str | None:
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
        "cph_days": r["cph_days"], "cph_moy_l_h": _f(r["cph_moy_l_h"]),
        "conso_days": r["conso_days"], "conso_theorique_l": _f(r["conso_theorique_l"]),
        "conso_partielle_l": _f(r["conso_partielle_l"]), "cph_status": r["cph_status"],
        "extrapolated_days": r["extrapolated_days"], "day_status_counts": r["day_status_counts"],
        "motifs": r["motifs"],
        "rapprochement_statut": r["rapprochement_statut"],
        "rapprochement": _reconciliation_dict(r["rapprochement"]),
        "observations": len(r["reconciliations"]),
    }


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
    }


def _apply_table_filters(rows: list[dict], params) -> list[dict]:
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


def _synthesis(rows: list[dict]) -> dict:
    count = lambda pred: sum(1 for r in rows if pred(r))  # noqa: E731
    return {
        "sites": len(rows),
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
        "conso": _conso_summary(rows),
        "correspondances": _count_by(rows, lambda r: (r["correspondance"] or {}).get("statut")),
        "courbes_appliquees": _count_by(rows, lambda r: M.curve_source_status(r["curve"].status) if r["curve"] else None),
        "motifs_cph": _count_by(rows, lambda r: (r["motif_cph"] or {}).get("code")),
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
    }


def _compute(request):
    start, end = _parse_period(request.query_params)
    rows = compute_period(start, end, country=request.query_params.get("country") or None,
                          zone=request.query_params.get("zone") or None)
    return start, end, rows


class CphPeriodView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            start, end, rows = _compute(request)
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
            "filters": {
                "runtime_sources": sorted({r["runtime_source_main"] or "AUCUNE" for r in rows}),
                "power_sources": sorted({r["power_source_main"] or "AUCUNE" for r in rows}),
                "zones": sorted({r["zone"] for r in rows if r["zone"]}),
                "countries": sorted({r["country"] for r in rows if r["country"]}),
            },
            "meta": _meta(request),
        })


class CphSiteDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, site_id):
        try:
            start, end = _parse_period(request.query_params)
            rows = compute_period(start, end, site_ids=[site_id])
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        if not rows:
            return Response({"detail": "Site absent de l'inventaire des sites avec GE."}, status=404)
        r = rows[0]
        return Response({
            **_site_summary(r),
            "ac_reference": None if r["ac_reference"] is None else {
                "p_ac_aux_kw": _f(r["ac_reference"]["p_ac_aux_kw"]),
                "reference_dates": r["ac_reference"]["reference_dates"],
                "reason": r["ac_reference"]["reason"],
            },
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
    "stock_initial_l", "livraisons_l", "rajouts_l", "retraits_l", "vols_l", "vidanges_l", "stock_final_l",
    "statut_livraisons", "conso_stock_l", "ecart_l", "ecart_pct", "statut", "motifs_rapprochement",
    "formule", "version_regle",
]
FORMULA = ("charge = P_GE / (kVA × 0,8) ; CPH = a·charge² + b·charge + c ; conso estimée = runtime × CPH ; "
           "conso_stock = initial + livraisons + rajouts − retraits − vols − vidanges − final ; écart = conso_stock − conso_théorique")


def _csv_rows(rows: list[dict], only_anomalies: bool):
    for r in rows:
        c, rec, ac = r["curve"], r["rapprochement"] or {}, r["ac_reference"] or {}
        avail = " | ".join(f"{s}={ev['availability'] * 100:.0f}%" for s, ev in r["sources"].items())
        rejections = " | ".join(f"{s}: {ev['rejection']}" for s, ev in r["sources"].items() if ev["rejection"])
        ref_dates = ", ".join(d.isoformat() for d in ac.get("reference_dates", []))
        match, cmp_ = r["correspondance"] or {}, r["comparaison"]
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
                _f(rec.get("stock_initial_l")), _f(rec.get("livraisons_l")), _f(rec.get("rajouts_l")),
                _f(rec.get("retraits_l")), _f(rec.get("vols_l")), _f(rec.get("vidanges_l")), _f(rec.get("stock_final_l")),
                rec.get("livraisons_statut", ""), _f(rec.get("conso_stock_l")), _f(rec.get("ecart_l")),
                _f(rec.get("ecart_pct"), 2), r["rapprochement_statut"],
                " ; ".join(rec.get("motifs", [])) or ("aucune observation de stock sur la période" if not rec else ""),
                FORMULA, E.RULE_VERSION,
            ]


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
