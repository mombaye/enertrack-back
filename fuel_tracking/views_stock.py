# fuel_tracking/views_stock.py
"""
API du registre unifié de stock carburant (Suivis Stock — Phase 1). Ajouts uniquement : l'ancien
GET /api/fuel-tracking/stock/ (Snowflake + ENOC, information technique) est inchangé.

  GET   /api/fuel-tracking/stock/registre/                    une ligne par site (pagination serveur, filtres)
  GET   /api/fuel-tracking/stock/registre/<site_id>/          historique : relevés, mouvements, comparaisons, cuves, corrections
  POST  /api/fuel-tracking/stock/observations/<id>/valider/   {reason}            (admin, manager)
  POST  /api/fuel-tracking/stock/observations/<id>/rejeter/   {reason}            (admin, manager)
  POST  /api/fuel-tracking/stock/mouvements/<id>/valider/     {reason}            (admin, manager)
  POST  /api/fuel-tracking/stock/mouvements/<id>/rejeter/     {reason}            (admin, manager)
  GET   /api/fuel-tracking/stock/cuves/?site_id=&statut=
  POST  /api/fuel-tracking/stock/cuves/                       création d'une cuve  (admin, manager)
  PATCH /api/fuel-tracking/stock/cuves/<id>/                  correction / validation tracée (admin, manager)
  GET   /api/fuel-tracking/stock/imports/                     lots d'import
  POST  /api/fuel-tracking/stock/imports/                     upload {file, source_type} (admin, manager)
  GET   /api/fuel-tracking/stock/imports/<id>/lignes/         lignes brutes + contrôles
  POST  /api/fuel-tracking/stock/imports/<id>/relancer/       relance tracée {reason} (admin, manager)
  GET   /api/fuel-tracking/stock/gardiennage/?site_id=
  POST  /api/fuel-tracking/stock/gardiennage/                 {site_id, provider, active_from, active_to?, reason}
  POST  /api/fuel-tracking/stock/gardiennage/<id>/cloturer/   {active_to, reason} — jamais de suppression
"""
from __future__ import annotations

from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel_tracking.services import stock_registry as R
from fuel_tracking.views_cph import VALIDATOR_ROLES, IsCphValidator

STALE_DAYS = 7
FILL_CRITICAL = 15
FILL_WARNING = 40

ALERT_LABELS = {
    "NO_DATA": "Aucun relevé",
    "NOT_RETAINED": "Aucun stock retenu",
    "STALE": f"Relevé de plus de {STALE_DAYS} jours",
    "PENDING_VALIDATION": "Relevé(s) à traiter",
    "STOCK_CONFLICT": "Conflit Loliforce / SCSS",
    "TANK_NOT_REFERENCED": "Aucune cuve connue",
    "TANK_PENDING": "Cuve à confirmer",
    "TANK_INCOMPLETE": "Cuve incomplète",
    "MULTI_TANK_WITHOUT_ID": "Plusieurs cuves à identifier",
    "PLATE_MISSING": "Capacité de plaque à confirmer",
    "LOW_STOCK_CRITICAL": f"Stock < {FILL_CRITICAL} %",
    "LOW_STOCK": f"Stock < {FILL_WARNING} %",
    "GUARD_NOT_CONFIGURED": "Gardiennage non configuré",
}
STATUT_LABELS = {"RETENU": "Stock retenu", "INDISPONIBLE": "Indisponible (action requise)", "AUCUNE_DONNEE": "Aucune donnée"}
KPI_DENOMINATOR = "sites du référentiel Gestion des sites (périmètre du registre stock)"
COUNTRY_LABELS = {"sen": "Sénégal", "civ": "Côte d'Ivoire", "cam": "Cameroun", "td": "Tchad", "bfa": "Burkina Faso"}


def _f(v):
    return float(v) if v is not None else None


def _reason(request) -> str:
    reason = str(request.data.get("reason") or "").strip()
    if not reason:
        raise ValueError("Motif obligatoire.")
    return reason


def _page(request, default=50, maximum=200):
    try:
        page = max(1, int(request.query_params.get("page", 1)))
    except ValueError:
        page = 1
    try:
        limit = min(maximum, max(1, int(request.query_params.get("limit", default))))
    except ValueError:
        limit = default
    return page, limit


def _pagination(page, limit, total):
    pages = max(1, (total + limit - 1) // limit)
    return {"page": page, "limit": limit, "total": total, "totalPages": pages, "hasNext": page < pages, "hasPrev": page > 1}


def _expected_guard(scopes, today):
    active = sorted({s.provider for s in scopes if s.active_from <= today and (s.active_to is None or s.active_to >= today)})
    return " + ".join(active) if active else None


def build_registry_rows(now=None) -> tuple[list[dict], dict]:
    """Une ligne par site du référentiel (et par site présent dans le registre), avec stock retenu et alertes."""
    from core.models import Site
    from fuel_tracking.models import FuelImportBatch, FuelMovement, FuelStockObservation, FuelTankMaster
    from fuel_tracking.services.site_energy_profile import energy_profiles

    now = now or timezone.now()
    today = now.date()
    sites = {s["site_id"]: s for s in Site.objects.values("site_id", "name", "installed_typology", "billing_typology", "zone", "country")}
    retained = R.fuel_stock_retained(now=now)
    for sid in retained:
        sites.setdefault(sid, {"site_id": sid, "name": None, "installed_typology": None, "billing_typology": None, "zone": None, "country": None})
    profiles = energy_profiles()

    pending = dict(FuelStockObservation.objects.filter(validation_status="PENDING").values_list("site_id").annotate(n=Count("id")))
    pending_codes: dict[str, set] = {}
    for sid, codes in FuelStockObservation.objects.filter(validation_status="PENDING").values_list("site_id", "issue_codes"):
        pending_codes.setdefault(sid, set()).update(codes or [])
    tanks: dict[str, list] = {}
    for t in FuelTankMaster.objects.filter(active=True).exclude(validation_status="REJETEE").values(
            "site_id", "tank_id", "validation_status", "capacity_plate_l", "tank_model__tank_model_id", "model_match_status"):
        tanks.setdefault(t["site_id"], []).append(t)
    scopes = R.guard_scopes_by_site()
    by_source = {}
    for sid, src in FuelStockObservation.objects.filter(source_type__in=R.GUARD_SOURCES).values_list("site_id", "source_type").distinct():
        by_source.setdefault(sid, set()).add(src)
    conflict_sites = set()
    for sid, srcs in by_source.items():
        if len(srcs) == 2:
            obs = list(FuelStockObservation.objects.filter(site_id=sid, source_type__in=R.GUARD_SOURCES)
                       .exclude(validation_status="REJECTED"))
            if any(c["status"] == R.STOCK_CONFLICT for c in R.guard_comparisons(sid, obs)):
                conflict_sites.add(sid)

    last_validated, last_any = {}, {}
    for mv in (FuelMovement.objects.exclude(status="REJECTED").order_by("site_id", "-movement_datetime")
               .values("id", "site_id", "movement_type", "movement_datetime", "quantity_l", "requested_quantity_l", "status",
                       "proof_reference", "ticket_reference", "source_type", "source_category", "retained_in_stock")):
        last_any.setdefault(mv["site_id"], mv)
        if mv["status"] == "VALIDATED":
            last_validated.setdefault(mv["site_id"], mv)

    rows = []
    for sid, s in sites.items():
        r = retained.get(sid)
        t = tanks.get(sid, [])
        codes = pending_codes.get(sid, set())
        alerts = []
        if r is None:
            alerts.append("NO_DATA")
        elif r["stock_l"] is None:
            alerts.append("NOT_RETAINED")
        if r and r["observation_datetime"] is not None and r["age_hours"] is not None and r["age_hours"] > STALE_DAYS * 24:
            alerts.append("STALE")
        if pending.get(sid):
            alerts.append("PENDING_VALIDATION")
        if sid in conflict_sites or "STOCK_CONFLICT" in codes:
            alerts.append("STOCK_CONFLICT")
        if not t:
            alerts.append("TANK_NOT_REFERENCED")
        else:
            st = {x["validation_status"] for x in t}
            if st & {"PROPOSEE", "A_CONFIRMER"}:
                alerts.append("TANK_PENDING")
            if "INCOMPLETE" in st:
                alerts.append("TANK_INCOMPLETE")
            if "PLUSIEURS_CUVES_A_IDENTIFIER" in st:
                alerts.append("MULTI_TANK_WITHOUT_ID")
        if "MULTI_TANK_WITHOUT_ID" in codes and "MULTI_TANK_WITHOUT_ID" not in alerts:
            alerts.append("MULTI_TANK_WITHOUT_ID")
        if codes & {"PLATE_MISSING", "PLATE_INCOHERENT"}:
            alerts.append("PLATE_MISSING")
        if r and r.get("fill_pct") is not None and r["stock_l"] is not None:
            if r["fill_pct"] < FILL_CRITICAL:
                alerts.append("LOW_STOCK_CRITICAL")
            elif r["fill_pct"] < FILL_WARNING:
                alerts.append("LOW_STOCK")
        guard = _expected_guard(scopes.get(sid, []), today)
        if guard is None and sid in by_source:
            alerts.append("GUARD_NOT_CONFIGURED")
        mv = last_validated.get(sid)
        mv_any = last_any.get(sid)
        validated_tanks = [x for x in t if x["validation_status"] == "VALIDEE"]
        model_refs = sorted({x["tank_model__tank_model_id"] for x in t if x["tank_model__tank_model_id"]})
        prof = profiles.get(sid) or {}
        rows.append({
            "site_id": sid,
            "site_name": s.get("name"),
            "pays": COUNTRY_LABELS.get(s.get("country") or "", s.get("country")),
            "zone": s.get("zone"),
            "typologie": s.get("installed_typology") or s.get("billing_typology"),
            "environnement": prof.get("environnement", "Inconnu"),
            "raccordement": prof.get("raccordement", "Inconnu"),
            "ge": prof.get("ge", "Inconnu"),
            "solaire": prof.get("solaire", "Inconnu"),
            "profil_energetique": prof.get("profil", "Incomplet / à confirmer"),
            "gardiennage_attendu": guard,
            "stock_l": r["stock_l"] if r else None,
            "source": r["source_type"] if r else None,
            "observation_id": r["observation_id"] if r else None,
            "observation_datetime": r["observation_datetime"] if r else None,
            "age_hours": r["age_hours"] if r else None,
            "level_raw": r["level_raw"] if r else None,
            "level_unit": r["level_unit"] if r else None,
            "conversion_method": r["conversion_method"] if r else None,
            "conversion_status": r["conversion_status"] if r else None,
            "conversion_rule": r.get("conversion_rule") if r else None,
            "volume_geometric_l": r.get("volume_geometric_l") if r else None,
            "volume_calibrated_l": r.get("volume_calibrated_l") if r else None,
            "validation_status": r["validation_status"] if r else None,
            "statut_releve": r.get("statut_releve") if r else None,
            "issue_codes": r.get("issue_codes", []) if r else [],
            "modele_cuve": (r.get("tank_model_ref") if r else None) or (", ".join(model_refs) if model_refs else None),
            "modele_statut": (t[0]["model_match_status"] if len(t) == 1 else None),
            "capacity_l": r["capacity_l"] if r else None,
            "fill_pct": r["fill_pct"] if r and r["stock_l"] is not None else None,
            "statut": "RETENU" if r and r["stock_l"] is not None else ("INDISPONIBLE" if r else "AUCUNE_DONNEE"),
            "unavailable_reason": (r["unavailable_reason"] if r else R.UNAVAILABLE_NO_OBS),
            "alerts": alerts,
            "pending_count": pending.get(sid, 0),
            "tanks_total": len(t),
            "tanks_validated": len(validated_tanks),
            "tank_statuses": sorted({x["validation_status"] for x in t}),
            "last_validated_movement": _mv(mv),
            "last_movement": _mv(mv_any),
        })
        rows[-1].update(_next_action(rows[-1]))
    rows.sort(key=lambda x: x["site_id"])
    tank_counts = dict(FuelTankMaster.objects.filter(active=True).values_list("validation_status").annotate(n=Count("id")))
    kpis = {
        "sites_total": len(rows),
        "denominateur": KPI_DENOMINATOR,
        "sites_stock_retenu": sum(1 for r in rows if r["statut"] == "RETENU"),
        "sites_indisponible": sum(1 for r in rows if r["statut"] == "INDISPONIBLE"),
        "sites_sans_donnee": sum(1 for r in rows if r["statut"] == "AUCUNE_DONNEE"),
        "releves_a_valider": sum(pending.values()),
        "sites_conflit": len(conflict_sites),
        "sites_cuve_validee": sum(1 for r in rows if r["tanks_validated"]),
        "cuves_validees": tank_counts.get("VALIDEE", 0),
        "cuves_a_valider": sum(v for k, v in tank_counts.items() if k not in ("VALIDEE", "REJETEE")),
        "cuves_par_statut": tank_counts,
        "imports_a_valider": FuelImportBatch.objects.filter(status="VALIDATION_REQUIRED").count(),
        "sites_action_requise": sum(1 for r in rows if r["prochaine_action"]),
    }
    return rows, kpis


def _next_action(row: dict) -> dict:
    """Prochaine action en langage simple pour la vue compacte (champ additif, aucune règle de calcul)."""
    for code in row["issue_codes"]:
        d = R.issue_detail(code)
        return {"prochaine_action": d["action"], "prochaine_action_type": d["action_type"], "anomalie": d["anomalie"]}
    if row["statut"] == "AUCUNE_DONNEE":
        return {"prochaine_action": None, "prochaine_action_type": None, "anomalie": "Aucun relevé reçu"}
    if row["tanks_validated"] == 0:
        return {"prochaine_action": "Renseigner puis valider la cuve du site", "prochaine_action_type": "OPEN_TANK",
                "anomalie": "Aucune cuve validée"}
    if row["statut"] == "INDISPONIBLE" or row["pending_count"]:
        return {"prochaine_action": "Contrôler puis valider ou rejeter le relevé", "prochaine_action_type": "VALIDATE_OBS",
                "anomalie": "Relevé à traiter"}
    if "GUARD_NOT_CONFIGURED" in row["alerts"]:
        d = R.issue_detail("NOT_CONFIGURED")
        return {"prochaine_action": d["action"], "prochaine_action_type": "SET_GUARD", "anomalie": d["anomalie"]}
    return {"prochaine_action": None, "prochaine_action_type": None, "anomalie": None}


def _mv(mv):
    if not mv:
        return None
    return {"id": mv["id"], "movement_type": mv["movement_type"], "movement_datetime": mv["movement_datetime"],
            "quantity_l": _f(mv["quantity_l"]), "requested_quantity_l": _f(mv["requested_quantity_l"]), "status": mv["status"],
            "proof_reference": mv["proof_reference"], "ticket_reference": mv["ticket_reference"], "source_type": mv["source_type"]}


class StockRegistryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows, kpis = build_registry_rows()
        q = request.query_params
        search = (q.get("search") or "").strip().lower()
        if search:
            rows = [r for r in rows if search in r["site_id"].lower() or search in (r["site_name"] or "").lower()]
        if q.get("statut"):
            rows = [r for r in rows if r["statut"] == q["statut"]]
        if q.get("alerte"):
            rows = [r for r in rows if q["alerte"] in r["alerts"]]
        if q.get("action_requise") in ("1", "true"):
            rows = [r for r in rows if r["prochaine_action"]]
        if q.get("tri") == "priorite":  # vue simple : stock connu, puis action requise, puis sans relevé
            rank = {"RETENU": 0, "INDISPONIBLE": 1, "AUCUNE_DONNEE": 2}
            rows = sorted(rows, key=lambda r: (rank.get(r["statut"], 3), r["site_id"]))
        if q.get("source"):
            rows = [r for r in rows if r["source"] == q["source"]]
        if q.get("gardiennage"):
            g = q["gardiennage"]
            rows = [r for r in rows if (r["gardiennage_attendu"] is None if g == "NON_CONFIGURE" else g in (r["gardiennage_attendu"] or ""))]
        page, limit = _page(request)
        total = len(rows)
        start = (page - 1) * limit
        return Response({
            "data": rows[start:start + limit],
            "pagination": _pagination(page, limit, total),
            "kpis": kpis,
            "alert_labels": ALERT_LABELS,
            "statut_labels": STATUT_LABELS,
            "can_validate": getattr(request.user, "role", None) in VALIDATOR_ROLES,
            "regles": {
                "tolerance_capacite_pct": R.tolerance_pct(),
                "comparaison": "Loliforce / SCSS : même site, relevés en litres à moins de 48 h, capacité validée ; "
                               "écart ≤ max(100 L, 10 % capacité) → cohérent, sinon conflit (aucune moyenne).",
                "snowflake": "Information technique uniquement : jamais retenu comme stock.",
                "stock_retenu": "Cuve site VALIDÉE + litres natifs ou conversion validée (volume calibré sur la plaque) + capacité "
                                "contrôlée + statut AUTO_VALIDÉ ou VALIDÉ_MANUELLEMENT. Priorité entre relevés du même jour : "
                                + " > ".join(R.source_priority()) + ".",
                "profil_energetique": "Environnement, raccordement, GE et solaire viennent de champs sources explicites ; jamais "
                                      "déduits de la typologie. Inconnu si la source est vide.",
            },
        })


def _obs(o):
    return {
        "id": o.id, "source_type": o.source_type, "source_event_id": o.source_event_id, "tank_id": o.tank_id,
        "observation_datetime": o.observation_datetime, "level_raw": o.level_raw, "level_value": _f(o.level_value),
        "level_unit": o.level_unit, "level_liters": _f(o.level_liters), "conversion_method": o.conversion_method,
        "conversion_status": o.conversion_status, "conversion_message": o.conversion_message, "capacity_l": _f(o.capacity_l),
        "hour_meter": _f(o.hour_meter), "source_user": o.source_user, "source_reference": o.source_reference,
        "guard_scope_status": o.guard_scope_status, "comment": o.comment, "proof_reference": o.proof_reference,
        "validation_status": o.validation_status, "validation_reason": o.validation_reason, "review_flags": R._flags(o.review_flags),
        "validated_by": getattr(o.validated_by, "username", None), "validated_at": o.validated_at, "batch_id": o.batch_id,
        "statut_releve": R.display_status(o), "issues": [R.issue_detail(c) for c in (o.issue_codes or [])],
        "volume_geometric_l": _f(o.volume_geometric_l), "volume_calibrated_l": _f(o.volume_calibrated_l),
        "declared_liters": _f(o.declared_liters), "level_cm": _f(o.level_cm), "cm_liters_gap_l": _f(o.cm_liters_gap_l),
        "fill_pct": _f(o.fill_pct), "conversion_rule": o.conversion_rule, "tank_model_ref": o.tank_model_ref,
    }


def _movement(m):
    return {
        "id": m.id, "source_type": m.source_type, "source_event_id": m.source_event_id, "movement_type": m.movement_type,
        "movement_datetime": m.movement_datetime, "requested_quantity_l": _f(m.requested_quantity_l), "quantity_l": _f(m.quantity_l),
        "counterpart_site_id": m.counterpart_site_id, "tank_id": m.tank_id, "level_before_raw": m.level_before_raw,
        "level_after_raw": m.level_after_raw, "hour_meter_before": _f(m.hour_meter_before), "hour_meter_after": _f(m.hour_meter_after),
        "supplier": m.supplier, "ticket_reference": m.ticket_reference, "delivery_note": m.delivery_note,
        "proof_reference": m.proof_reference, "source_user": m.source_user, "source_status": m.source_status, "status": m.status,
        "alerts": m.alerts, "comment": m.comment, "validation_reason": m.validation_reason,
        "validated_by": getattr(m.validated_by, "username", None), "validated_at": m.validated_at, "batch_id": m.batch_id,
        "raw_operation_type": m.raw_operation_type, "source_category": m.source_category, "mapping_rule": m.mapping_rule,
        "retained_in_stock": m.retained_in_stock, "movement_direction": m.movement_direction,
        "impact_stock_l": _f(m.impact_stock_l), "transfer_id": m.transfer_id,
        "integration_status": m.integration_status, "quality_status": m.quality_status,
        "quality_missing": m.quality_missing or [], "date_source_used": m.date_source_used,
    }


def _tank(t):
    spec = R.TC.TankSpec.from_model(t)
    full = spec.geometric_capacity_l()
    cal = spec.calibration()
    m = t.tank_model if t.tank_model_id else None
    return {
        "id": t.id, "site_id": t.site_id, "tank_id": t.tank_id, "shape": t.shape, "shape_family": spec.family,
        "dimension_1_cm": spec.d1, "dimension_2_cm": spec.d2, "dimension_3_cm": spec.d3,
        "bottom_profile": spec.bottom, "bottom_z_cm": spec.bz, "top_profile": spec.top, "top_z_cm": spec.tz,
        "length_m": _f(t.length_m), "width_m": _f(t.width_m), "height_m": _f(t.height_m), "diameter_m": _f(t.diameter_m),
        "capacity_l": _f(t.capacity_l), "capacity_plate_l": _f(t.capacity_plate_l), "capacity_plate_source": t.capacity_plate_source,
        "geometric_capacity_l": round(full, 1) if full else None, "calibration_k": round(cal["k"], 4) if cal["k"] else None,
        "plate_gap_pct": cal["gap_pct"], "plate_coherent": cal["coherent"],
        "physical_height_cm": spec.useful_height_cm(), "calibration_curve": t.calibration_curve,
        "calibration_validated": t.calibration_validated, "source_type": t.source_type, "source_batch_id": t.source_batch_id,
        "source_file": t.source_file, "source_sheet": t.source_sheet, "source_row": t.source_row, "proof_reference": t.proof_reference,
        "serial_number": t.serial_number, "number_of_tanks_on_site": t.number_of_tanks_on_site, "observed_at": t.observed_at,
        "tank_model_id": m.tank_model_id if m else None, "tank_model_label": f"{m.source_label or m.shape_label} · {_f(m.nominal_capacity_l) or '—'} L" if m else None,
        "model_match_status": t.model_match_status,
        "validation_status": t.validation_status, "validation_reason": t.validation_reason,
        "validated_by": getattr(t.validated_by, "username", None), "validated_at": t.validated_at, "active": t.active, "comment": t.comment,
    }


class StockRegistrySiteView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, site_id):
        from core.models import Site
        from fuel_tracking.models import (FuelMovement, FuelRegistryCorrection, FuelStockObservation, FuelStockSnapshot,
                                          FuelTankMaster)

        site = Site.objects.filter(site_id__iexact=site_id).values("site_id", "name", "installed_typology", "zone").first()
        sid = site["site_id"] if site else site_id
        obs = list(FuelStockObservation.objects.filter(site_id=sid).select_related("validated_by").order_by("-observation_datetime", "-id")[:300])
        mvs = list(FuelMovement.objects.filter(site_id=sid).select_related("validated_by").order_by("-movement_datetime", "-id")[:300])
        tanks = list(FuelTankMaster.objects.filter(site_id=sid).select_related("validated_by", "tank_model"))
        snap = FuelStockSnapshot.objects.filter(site_id__iexact=sid, snapshot_year__isnull=True).first()
        corrections = FuelRegistryCorrection.objects.filter(site_id=sid).select_related("author")[:200]
        scopes = R.guard_scopes_by_site([sid]).get(sid, [])
        return Response({
            "site": site or {"site_id": sid, "name": None},
            "retenu": R.fuel_stock_retained([sid]).get(sid),
            "observations": [_obs(o) for o in obs],
            "mouvements": [_movement(m) for m in mvs],
            "comparaisons": R.guard_comparisons(sid),
            "cuves": [_tank(t) for t in tanks],
            "gardiennage": [{"id": s.id, "provider": s.provider, "active_from": s.active_from, "active_to": s.active_to,
                             "comment": s.comment} for s in scopes],
            "snowflake_technique": None if snap is None else {
                "stock_l": _f(snap.stock_snowflake_l), "capacity_l": _f(snap.capacity_snowflake_l),
                "date": snap.stock_snowflake_date, "quality_status": snap.quality_status,
                "note": "Information technique (capteur Snowflake) : jamais retenue comme stock.",
            },
            "corrections": [{"id": c.id, "entity": c.entity, "entity_id": c.entity_id, "action": c.action, "field": c.field,
                             "old_value": c.old_value, "new_value": c.new_value, "reason": c.reason,
                             "author": getattr(c.author, "username", None) or c.author_label, "created_at": c.created_at}
                            for c in corrections],
            "can_validate": getattr(request.user, "role", None) in VALIDATOR_ROLES,
        })


class _ActionView(APIView):
    permission_classes = [IsCphValidator]
    parser_classes = [JSONParser, FormParser]
    model = None
    validate = True

    def post(self, request, pk):
        obj = self.model.objects.filter(pk=pk).first()
        if obj is None:
            return Response({"detail": "Introuvable."}, status=404)
        try:
            reason = _reason(request)
            obj = self.act(obj, request.user, reason)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        return Response(self.serialize(obj))


class ObservationValidateView(_ActionView):
    validate = True

    @property
    def model(self):
        from fuel_tracking.models import FuelStockObservation

        return FuelStockObservation

    def act(self, obj, user, reason):
        R.validate_observation(obj, user, reason, validate=self.validate)
        R.apply_guard_conflicts([obj.site_id])
        return obj

    def serialize(self, obj):
        obj.refresh_from_db()
        return _obs(obj)


class ObservationRejectView(ObservationValidateView):
    validate = False


class MovementValidateView(_ActionView):
    validate = True

    @property
    def model(self):
        from fuel_tracking.models import FuelMovement

        return FuelMovement

    def act(self, obj, user, reason):
        return R.validate_movement(obj, user, reason, validate=self.validate)

    def serialize(self, obj):
        return _movement(obj)


class MovementRejectView(MovementValidateView):
    validate = False


TANK_FIELDS = R.TANK_EDITABLE


class TankListView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def get_permissions(self):
        return [IsCphValidator()] if self.request.method == "POST" else [IsAuthenticated()]

    def get(self, request):
        from fuel_tracking.models import FuelTankMaster

        qs = FuelTankMaster.objects.select_related("validated_by", "tank_model").order_by("site_id", "tank_id")
        if request.query_params.get("site_id"):
            qs = qs.filter(site_id__iexact=request.query_params["site_id"])
        if request.query_params.get("statut"):
            qs = qs.filter(validation_status=request.query_params["statut"])
        page, limit = _page(request)
        total = qs.count()
        return Response({"data": [_tank(t) for t in qs[(page - 1) * limit:page * limit]], "pagination": _pagination(page, limit, total)})

    def post(self, request):
        from core.models import Site
        from fuel_tracking.models import FuelTankMaster

        try:
            reason = _reason(request)
            site = Site.objects.filter(site_id__iexact=str(request.data.get("site_id") or "").strip()).first()
            if site is None:
                raise ValueError("Site absent de Gestion des sites.")
            tank_id = str(request.data.get("tank_id") or "").strip()
            if not tank_id:
                raise ValueError("Identifiant de cuve obligatoire (jamais attribué automatiquement).")
            if FuelTankMaster.objects.filter(site_id=site.site_id, tank_id=tank_id).exists():
                raise ValueError("Cette cuve existe déjà : utiliser la correction.")
            tank = FuelTankMaster.objects.create(site_id=site.site_id, tank_id=tank_id, source_type="MANUAL")
            R.log_correction("TANK", tank.pk, "CREATE", reason, site_id=site.site_id, user=request.user)
            changes = {k: request.data[k] for k in TANK_FIELDS if k in request.data}
            validate = request.data.get("validate")
            R.correct_tank(tank, changes, request.user, reason, validate=True if validate in (True, "true", "1") else None)
            R.reconvert_site_observations(site.site_id, request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        return Response(_tank(tank), status=201)


class TankDetailView(APIView):
    permission_classes = [IsCphValidator]
    parser_classes = [JSONParser]

    def patch(self, request, pk):
        from fuel_tracking.models import FuelTankMaster

        tank = FuelTankMaster.objects.filter(pk=pk).first()
        if tank is None:
            return Response({"detail": "Introuvable."}, status=404)
        try:
            reason = _reason(request)
            changes = {k: request.data[k] for k in TANK_FIELDS if k in request.data}
            v = request.data.get("validate")
            validate = True if v in (True, "true", "1") else False if v in (False, "false", "0") else None
            R.correct_tank(tank, changes, request.user, reason, validate=validate)
            n = R.reconvert_site_observations(tank.site_id, request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        return Response({**_tank(tank), "releves_reconvertis": n})


def _batch(b):
    return {
        "id": b.id, "source_type": b.source_type, "source_libelle": b.get_source_type_display(), "file_name": b.file_name, "file_hash": b.file_hash, "file_size": b.file_size,
        "origin": b.origin, "status": b.status, "status_message": b.status_message, "duplicate_of": b.duplicate_of_id,
        "relaunch_of": b.relaunch_of_id, "relaunch_reason": b.relaunch_reason, "rows_total": b.rows_total,
        "rows_accepted": b.rows_accepted, "rows_warning": b.rows_warning, "rows_rejected": b.rows_rejected,
        "rows_pending": b.rows_pending, "rows_duplicate": b.rows_duplicate, "summary": b.summary,
        "received_at": b.received_at, "processed_at": b.processed_at,
        "received_by": getattr(b.received_by, "username", None),
        "declared_period_start": b.declared_period_start, "declared_period_end": b.declared_period_end, "mapping_version": b.mapping_version,
    }


class ImportBatchListView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self):
        return [IsCphValidator()] if self.request.method == "POST" else [IsAuthenticated()]

    def get(self, request):
        from fuel_tracking.models import FuelImportBatch

        qs = FuelImportBatch.objects.select_related("received_by").order_by("-received_at")
        if request.query_params.get("source_type"):
            qs = qs.filter(source_type=request.query_params["source_type"])
        if request.query_params.get("statut"):
            qs = qs.filter(status=request.query_params["statut"])
        page, limit = _page(request, default=20)
        total = qs.count()
        from fuel_tracking.services.stock_import_pipeline import INCOMING, root

        return Response({
            "data": [_batch(b) for b in qs[(page - 1) * limit:page * limit]],
            "pagination": _pagination(page, limit, total),
            "repertoire": {"monte": root().exists(), "sous_dossiers": [f"incoming/{d}" for d in INCOMING]},
        })

    def post(self, request):
        from fuel_tracking.services import stock_import_pipeline as P

        f = request.FILES.get("file")
        source_type = str(request.data.get("source_type") or "").upper()
        if f is None:
            return Response({"detail": "Fichier manquant."}, status=400)
        if source_type not in ("LOLIFORCE", "SCSS", "CLOSING", "HISTORICAL_REFUELING", "TANK_MODELS"):
            return Response({"detail": "Type de source invalide (LOLIFORCE, SCSS, CLOSING, HISTORICAL_REFUELING, TANK_MODELS)."}, status=400)
        if not f.name.lower().endswith(P.ACCEPTED_EXT):
            return Response({"detail": "Format accepté : .xlsx, .xlsm, .xlsb."}, status=400)
        try:
            batch = P.process_bytes(f.read(), f.name, source_type, origin="UPLOAD", user=request.user)
        except OSError as e:
            return Response({"detail": f"Répertoire d'import indisponible ({e.__class__.__name__}) : vérifier le montage FUEL_IMPORT_ROOT."}, status=503)
        return Response(_batch(batch), status=201)


class ImportBatchRowsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from fuel_tracking.models import FuelImportBatch, FuelImportRow

        b = FuelImportBatch.objects.filter(pk=pk).first()
        if b is None:
            return Response({"detail": "Introuvable."}, status=404)
        qs = FuelImportRow.objects.filter(batch=b).order_by("sheet_name", "row_number")
        if request.query_params.get("statut"):
            qs = qs.filter(validation_status=request.query_params["statut"])
        if request.query_params.get("site_id"):
            qs = qs.filter(Q(site_id_resolved__iexact=request.query_params["site_id"]) | Q(site_id_raw__iexact=request.query_params["site_id"]))
        page, limit = _page(request)
        total = qs.count()
        return Response({
            "batch": _batch(b),
            "data": [{"id": r.id, "sheet_name": r.sheet_name, "row_number": r.row_number, "site_id_raw": r.site_id_raw,
                      "site_id_resolved": r.site_id_resolved, "event_datetime": r.event_datetime,
                      "validation_status": r.validation_status, "messages": r.messages, "raw_payload": r.raw_payload}
                     for r in qs[(page - 1) * limit:page * limit]],
            "pagination": _pagination(page, limit, total),
        })


class ImportBatchRelaunchView(APIView):
    permission_classes = [IsCphValidator]
    parser_classes = [JSONParser, FormParser]

    def post(self, request, pk):
        from fuel_tracking.models import FuelImportBatch
        from fuel_tracking.services import stock_import_pipeline as P

        b = FuelImportBatch.objects.filter(pk=pk).first()
        if b is None:
            return Response({"detail": "Introuvable."}, status=404)
        if b.source_type == "ENOC":
            return Response({"detail": "Les lots ENOC se resynchronisent automatiquement (connecteur existant)."}, status=400)
        try:
            nb = P.relaunch(b, _reason(request), request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        return Response(_batch(nb), status=201)


class GuardScopeView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def get_permissions(self):
        return [IsCphValidator()] if self.request.method == "POST" else [IsAuthenticated()]

    def get(self, request):
        from fuel_tracking.models import FuelGuardScope

        qs = FuelGuardScope.objects.all().order_by("site_id", "-active_from")
        if request.query_params.get("site_id"):
            qs = qs.filter(site_id__iexact=request.query_params["site_id"])
        page, limit = _page(request)
        total = qs.count()
        return Response({"data": [{"id": s.id, "site_id": s.site_id, "provider": s.provider, "active_from": s.active_from,
                                   "active_to": s.active_to, "comment": s.comment} for s in qs[(page - 1) * limit:page * limit]],
                         "pagination": _pagination(page, limit, total)})

    def post(self, request):
        from fuel_tracking.models import FuelGuardScope
        from fuel_tracking.services.stock_import_files import parse_date

        try:
            reason = _reason(request)
            site_id = R.SiteResolver().resolve(request.data.get("site_id"))
            provider = str(request.data.get("provider") or "").upper()
            start = parse_date(request.data.get("active_from"))
            end = parse_date(request.data.get("active_to")) if request.data.get("active_to") else None
            if not site_id:
                raise ValueError("Site absent de Gestion des sites.")
            if provider not in ("LOLIFORCE", "SCSS"):
                raise ValueError("Prestataire : LOLIFORCE ou SCSS.")
            if start is None or (end and end < start):
                raise ValueError("Période invalide.")
            s = FuelGuardScope.objects.create(site_id=site_id, provider=provider, active_from=start, active_to=end,
                                              comment=request.data.get("comment") or None, created_by=request.user)
            R.log_correction("GUARD_SCOPE", s.pk, "CREATE", reason, site_id=site_id, new=f"{provider} {start}→{end or '…'}", user=request.user)
            n = R.reconvert_site_observations(site_id, request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        return Response({"id": s.id, "site_id": s.site_id, "provider": s.provider, "active_from": s.active_from,
                         "active_to": s.active_to, "releves_reevalues": n}, status=201)


class GuardScopeCloseView(APIView):
    permission_classes = [IsCphValidator]
    parser_classes = [JSONParser]

    def post(self, request, pk):
        from fuel_tracking.models import FuelGuardScope
        from fuel_tracking.services.stock_import_files import parse_date

        s = FuelGuardScope.objects.filter(pk=pk).first()
        if s is None:
            return Response({"detail": "Introuvable."}, status=404)
        try:
            reason = _reason(request)
            end = parse_date(request.data.get("active_to"))
            if end is None or end < s.active_from:
                raise ValueError("Date de fin invalide.")
            R.log_correction("GUARD_SCOPE", s.pk, "CORRECT", reason, site_id=s.site_id, field="active_to", old=s.active_to, new=end, user=request.user)
            s.active_to = end
            s.save(update_fields=["active_to"])
            R.reconvert_site_observations(s.site_id, request.user)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        return Response({"id": s.id, "active_to": s.active_to})


class StockWorkQueueView(APIView):
    """GET stock/a-traiter/ → compteurs ; ?categorie=…&page=&search= → liste filtrée avec action à faire."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.services import stock_work_queue as W

        cat = request.query_params.get("categorie")
        payload = {"can_validate": getattr(request.user, "role", None) in VALIDATOR_ROLES}
        if not cat:
            return Response({**payload, "counters": W.counters()})
        try:
            items = W.build(cat)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        search = (request.query_params.get("search") or "").strip().lower()
        if search:
            items = [i for i in items if search in (i["site_id"] or "").lower()]
        page, limit = _page(request, default=25, maximum=200)
        label = next((label for key, label, _ in W.CATEGORIES if key == cat), cat)
        return Response({**payload, "categorie": cat, "label": label, "data": items[(page - 1) * limit:page * limit],
                         "pagination": _pagination(page, limit, len(items))})


class EnocTypesView(APIView):
    """GET : dictionnaire réel des types ENOC + classification ; POST : nouvelle version (admin, manager)."""

    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def get_permissions(self):
        return [IsCphValidator()] if self.request.method == "POST" else [IsAuthenticated()]

    def get(self, request):
        from fuel_tracking.models import FuelEnocTypeMapping
        from fuel_tracking.services.stock_enoc import enoc_type_dictionary

        return Response({"data": enoc_type_dictionary(),
                         "categories": [{"key": k, "label": v} for k, v in FuelEnocTypeMapping.Category.choices],
                         "can_validate": getattr(request.user, "role", None) in VALIDATOR_ROLES,
                         "regles": ["Une demande de livraison n'est jamais une livraison réelle.",
                                    "Carte Total : information de contrôle / preuve, jamais une livraison automatique.",
                                    "Type inconnu : file d'exceptions, jamais classé automatiquement comme mouvement réel.",
                                    "Livraison confirmée retenue seulement avec site, date, quantité et preuve / référence."]})

    def post(self, request):
        from fuel_tracking.services.stock_enoc import set_enoc_mapping, sync_enoc_to_registry

        try:
            m = set_enoc_mapping(request.data.get("raw_value"), str(request.data.get("category") or ""), request.user,
                                 str(request.data.get("reason") or "").strip())
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        stats = sync_enoc_to_registry()  # reclassement immédiat (tracé)
        return Response({"raw_value": m.raw_value, "category": m.category, "version": m.version,
                         "mouvements_mis_a_jour": stats.get("mouvements_mis_a_jour", 0)}, status=201)


class TankModelListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import FuelTankModel
        from fuel_tracking.services import tank_geometry as G

        rows = []
        for m in FuelTankModel.objects.all():
            rows.append({
                "tank_model_id": m.tank_model_id, "country": m.country, "source_file": m.source_file, "source_sheet": m.source_sheet,
                "source_row": m.source_row, "source_label": m.source_label, "shape_code": m.shape_code, "shape_label": m.shape_label,
                "nominal_capacity_l": _f(m.nominal_capacity_l), "dimension_1_cm": _f(m.dimension_1_cm), "dimension_2_cm": _f(m.dimension_2_cm),
                "dimension_3_cm": _f(m.dimension_3_cm), "bottom_profile": m.bottom_profile, "bottom_z_cm": _f(m.bottom_z_cm),
                "top_profile": m.top_profile, "geometric_full_l": _f(m.geometric_full_l), "calibration_k": _f(m.calibration_k),
                "plate_gap_pct": _f(m.plate_gap_pct), "model_status": m.model_status, "status_reason": m.status_reason,
                "geometric_formula_version": m.geometric_formula_version, "sites": m.site_tanks.count(),
            })
        return Response({"data": rows, "familles": G.FAMILIES, "profils": G.PROFILES,
                         "version_moteur": G.GEOMETRIC_FORMULA_VERSION})


class EnergyAuditView(APIView):
    """Audit des champs sources du profil énergétique (couverture, valeurs, règles)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.services.site_energy_profile import audit_energy_fields

        return Response({"data": audit_energy_fields()})
