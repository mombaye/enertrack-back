# fuel_tracking/views_enoc.py
"""
Mouvements Fuel & GE ENOC qualifiés (contrat canonique + qualification + rapprochement) — onglet Stock.

  GET  stock/enoc/mouvements/                   liste paginée, filtres, totaux SÉPARÉS (jamais mélangés)
  GET  stock/enoc/mouvements/<id>/              détail : canonique, sources des champs, brut ENOC, contexte de rapprochement
  POST stock/enoc/mouvements/<id>/rapprocher/   {decision: rapprocher | rejeter, reason, evidence} (admin, manager)
"""
from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.db.models import Q, Sum
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel_tracking.views_cph import VALIDATOR_ROLES, IsCphValidator

DEMAND_STATES = ("DEMANDE", "APPROUVEE")
DECLARED_STATES = ("A_RAPPROCHER", "TERMINEE", "PREUVE_MANQUANTE")
TOTALS = {
    "demandes": ("Demandes (non exécutées ou sans quantité réelle)",
                 Q(execution_state__in=DEMAND_STATES) | Q(business_classification="DEMANDE_CARTE_TOTAL")),
    "declares": ("Ravitaillements / livraisons déclarés (non rapprochés)",
                 Q(execution_state__in=DECLARED_STATES) & ~Q(business_classification="DEMANDE_CARTE_TOTAL")),
    "eligibles": ("Éligibles stock réel (rapprochés)", Q(execution_state="ELIGIBLE_STOCK_REEL", reconciliation_status="RAPPROCHE")),
    "certifies": ("Certifiés (rapprochés + preuve fournisseur)", Q(execution_state="ELIGIBLE_STOCK_REEL", reconciliation_status="CERTIFIE")),
    "transferts": ("Ponctions comptabilisées (transferts inter-sites, volume approuvé)", Q(transfer_status="COMPTABILISE")),
    "transferts_a_completer": ("Ponctions à compléter (aucune écriture)", Q(transfer_status="TRANSFERT_A_COMPLETER")),
    "a_qualifier": ("Nature à qualifier (type inconnu)", Q(execution_state="NATURE_A_QUALIFIER")),
    "rejetes": ("Rejetés", Q(execution_state="REJETEE")),
}


def _f(v):
    return float(v) if v is not None else None


def enoc_link(reference: str | None) -> str | None:
    tpl = getattr(settings, "ENOC_REQUEST_URL_TEMPLATE", "") or ""
    return tpl.replace("{reference}", reference) if (tpl and reference) else None


def row(q) -> dict:
    return {
        "id": q.pk, "enoc_reference": q.enoc_reference, "enoc_request_id": q.enoc_request_id, "lien_enoc": enoc_link(q.enoc_reference),
        "site_id": q.site_id_resolved or q.site_id, "site_id_brut": q.site_id, "site_connu": q.site_id_resolved is not None,
        "site_name": q.site_name, "country": q.country, "zone": q.zone,
        "type_brut": q.raw_movement_type, "type_famille": q.type_family, "statut_brut": q.raw_status,
        "qualification": q.business_classification, "etat": q.execution_state, "rapprochement": q.reconciliation_status,
        "impact_stock": q.stock_impact, "impact_conso": q.consumption_impact, "motif": q.reason,
        "conditions_manquantes": q.missing_conditions, "alertes": q.alerts, "bloquant": q.blocking,
        "date_reelle": q.actual_operation_at, "date_prevue": q.planned_at, "date_demande": q.requested_at,
        "date_approbation": q.approved_at, "date_cloture": q.closed_at,
        "quantite_demandee_l": _f(q.requested_quantity_l), "quantite_approuvee_l": _f(q.approved_quantity_l),
        "quantite_reelle_l": _f(q.actual_quantity_l), "unite": q.quantity_unit,
        "niveau_avant_l": _f(q.tank_level_before_l), "niveau_apres_l": _f(q.tank_level_after_l),
        "niveau_avant_cm": _f(q.tank_level_before_cm), "niveau_apres_cm": _f(q.tank_level_after_cm),
        "compteur_avant_h": _f(q.generator_hourmeter_before_h), "compteur_apres_h": _f(q.generator_hourmeter_after_h),
        "technicien": q.technician_name, "valideur": q.validator_name, "cloture_par": q.closer_name, "commentaire": q.comment,
        "nb_preuves": q.proof_count, "preuves": q.proof_urls_or_ids[:10], "fournisseur": q.supplier_name,
        "bon_livraison": q.delivery_note_number, "camion": q.vehicle_or_truck_reference, "nb_operations": q.operations_count,
        "rapproche_par": getattr(q.reconciled_by, "username", None), "rapproche_le": q.reconciled_at,
        "motif_rapprochement": q.reconciliation_reason, "element_rapprochement": q.reconciliation_evidence,
        "synchronise_le": q.synced_at,
        # Ponction : site_id principal = destinataire, « Site source » = source ; volume = quantité approuvée
        "transfer_id": q.transfer_id, "statut_transfert": q.transfer_status,
        "site_source": q.ponction_source_site_resolved or q.ponction_source_site_id,
        "site_source_connu": q.ponction_source_site_resolved is not None,
        "site_destinataire": (q.site_id_resolved or q.site_id) if q.type_family == "PONCTION" else None,
        "niveau_source_avant_l": _f(q.source_level_before_l), "niveau_source_apres_l": _f(q.source_level_after_l),
        "niveau_source_avant_cm": _f(q.source_level_before_cm), "niveau_source_apres_cm": _f(q.source_level_after_cm),
        "variation_source_l": _f(q.source_variation_l), "ecart_controle_l": _f(q.control_gap_l),
        "impact_source_l": -_f(q.approved_quantity_l) if q.transfer_status == "COMPTABILISE" and q.approved_quantity_l else None,
        "impact_destinataire_l": _f(q.approved_quantity_l) if q.transfer_status == "COMPTABILISE" else None,
        "bon_transfert": q.transfer_note, "alerte_fuel_manager": q.fuel_manager_alert,
        "fm_traite_par": getattr(q.fuel_manager_reviewed_by, "username", None), "fm_traite_le": q.fuel_manager_reviewed_at,
        "fm_commentaire": q.fuel_manager_comment,
    }


def _date(v):
    try:
        return datetime.strptime(v, "%Y-%m-%d").date() if v else None
    except ValueError:
        return None


def filtered(params):
    from fuel_tracking.models import FuelEnocQualifiedMovement as M

    qs = M.objects.select_related("reconciled_by", "fuel_manager_reviewed_by")
    p = params
    if p.get("type_brut"):
        v = p["type_brut"]
        qs = qs.filter(Q(type_family=v) | Q(raw_movement_type__iexact=v))
    if p.get("statut_enoc"):
        v = p["statut_enoc"]
        qs = qs.filter(raw_status__isnull=True) if v == "(aucun)" else qs.filter(raw_status__iexact=v)
    if p.get("qualification"):
        v = p["qualification"]
        qs = qs.filter(Q(business_classification=v) | Q(execution_state=v))
    if p.get("impact") in ("OUI", "NON"):
        qs = qs.filter(stock_impact=p["impact"] == "OUI")
    if p.get("preuve") == "AVEC":
        qs = qs.filter(proof_count__gt=0)
    elif p.get("preuve") == "SANS":
        qs = qs.filter(proof_count=0)
    ponction = p.get("ponction")
    if ponction:
        qs = qs.filter(type_family="PONCTION")
        if ponction == "avec_ecart":
            qs = qs.filter(fuel_manager_alert__in=["EN_ATTENTE", "TRAITEE"])
        elif ponction == "a_completer":
            qs = qs.filter(transfer_status="TRANSFERT_A_COMPLETER")
        elif ponction == "comptabilisees":
            qs = qs.filter(transfer_status="COMPTABILISE")
        elif ponction == "attente_fm":
            qs = qs.filter(fuel_manager_alert="EN_ATTENTE")
    if p.get("a_rapprocher") in ("1", "true"):
        qs = qs.filter(execution_state="A_RAPPROCHER")
    if p.get("anomalie"):
        code = p["anomalie"]
        ids = [x.pk for x in qs.only("id", "alerts") if (any(a.get("code") == code for a in (x.alerts or [])) if code != "1" else bool(x.alerts))]
        qs = qs.filter(pk__in=ids)
    d1, d2 = _date(p.get("date_debut")), _date(p.get("date_fin"))
    if d1:
        qs = qs.filter(Q(actual_operation_at__date__gte=d1) | Q(actual_operation_at__isnull=True, requested_at__date__gte=d1))
    if d2:
        qs = qs.filter(Q(actual_operation_at__date__lte=d2) | Q(actual_operation_at__isnull=True, requested_at__date__lte=d2))
    if p.get("pays"):
        qs = qs.filter(country__iexact=p["pays"])
    if p.get("zone"):
        qs = qs.filter(zone__iexact=p["zone"])
    if p.get("site"):
        s = p["site"].strip()
        qs = qs.filter(Q(site_id_resolved__icontains=s) | Q(site_id__icontains=s) | Q(site_name__icontains=s) | Q(enoc_reference__icontains=s))
    return qs


class EnocMovementListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import FuelEnocQualifiedMovement as M
        from fuel_tracking.views_stock import _page, _pagination

        qs = filtered(request.query_params)
        totals = {}
        for key, (label, cond) in TOTALS.items():
            sub = qs.filter(cond)
            field = "approved_quantity_l" if key.startswith("transferts") else "actual_quantity_l"  # Ponction : volume approuvé
            agg = sub.aggregate(s=Sum(field))
            totals[key] = {"label": label, "count": sub.count(), "quantite_reelle_l": _f(agg["s"]),
                           "sans_quantite": sub.filter(**{f"{field}__isnull": True}).count(),
                           "base": "quantité approuvée" if field == "approved_quantity_l" else "quantité réelle"}
        page, limit = _page(request, default=25, maximum=100)
        total = qs.count()
        start = (page - 1) * limit
        data = [row(q) for q in qs.order_by("-actual_operation_at", "-requested_at", "-id")[start:start + limit]]
        facets = {
            "types_bruts": sorted({v for v in M.objects.values_list("raw_movement_type", flat=True) if v}),
            "statuts_enoc": sorted({v for v in M.objects.values_list("raw_status", flat=True) if v}),
            "qualifications": [{"key": k, "label": lbl} for k, lbl in M.ExecutionState.choices],
            "classifications": sorted(set(M.objects.values_list("business_classification", flat=True))),
            "anomalies": sorted({a.get("code") for al in M.objects.values_list("alerts", flat=True) for a in (al or []) if a.get("code")}),
            "zones": sorted({v for v in M.objects.values_list("zone", flat=True) if v}),
            "pays": sorted({v for v in M.objects.values_list("country", flat=True) if v}),
        }
        return Response({"data": data, "pagination": _pagination(page, limit, total), "totaux": totals, "facettes": facets,
                         "can_validate": getattr(request.user, "role", None) in VALIDATOR_ROLES,
                         "regles": [
                             "Type brut ENOC ≠ impact stock : aucun mouvement n'entre automatiquement dans le stock réel.",
                             "Carte Total / Truck terminés, avec site, quantité réelle > 0 en L, date réelle et preuve → À RAPPROCHER.",
                             "Ponction validée et complète : −quantité approuvée au « Site source », +quantité approuvée au site de la "
                             "demande, deux écritures liées (transfer_id, somme = 0), datées à la validation ENOC ; sinon à compléter, aucune écriture.",
                             "Écart de niveau source > max(20 L ; 10 %) : alerte Fuel Manager, le volume approuvé reste comptabilisé.",
                             "ÉLIGIBLE STOCK RÉEL uniquement après rapprochement tracé (relevé terrain / stock cohérent, preuve fournisseur si disponible).",
                             "Une valeur absente reste vide (jamais 0).",
                         ]})


class EnocMovementDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int):
        from fuel_tracking.models import FuelEnocQualifiedMovement as M, FuelRegistryCorrection
        from fuel_tracking.services.enoc_canonical import reconciliation_context

        q = M.objects.select_related("enoc_movement", "reconciled_by", "fuel_manager_reviewed_by").filter(pk=pk).first()
        if q is None:
            return Response({"detail": "Mouvement introuvable."}, status=404)
        raw = q.enoc_movement.raw_payload or {}
        hist = [{"date": c.created_at, "action": c.action, "champ": c.field, "ancien": c.old_value, "nouveau": c.new_value,
                 "motif": c.reason, "auteur": c.author.username if c.author_id else c.author_label}
                for c in FuelRegistryCorrection.objects.filter(entity="ENOC_MOVEMENT", entity_id=q.pk).select_related("author")[:50]]
        return Response({**row(q), "sources_champs": q.field_sources, "contexte_rapprochement": reconciliation_context(q),
                         "brut": {"demande": raw.get("request"), "operation": raw.get("operation"),
                                  "operations": raw.get("operations")}, "historique": hist,
                         "can_validate": getattr(request.user, "role", None) in VALIDATOR_ROLES})


class EnocMovementReconcileView(APIView):
    permission_classes = [IsCphValidator]
    parser_classes = [JSONParser]

    def post(self, request, pk: int):
        from fuel_tracking.models import FuelEnocQualifiedMovement as M
        from fuel_tracking.services.enoc_canonical import ReconciliationError, reconcile
        from fuel_tracking.services.stock_enoc import sync_enoc_to_registry

        q = M.objects.select_related("enoc_movement").filter(pk=pk).first()
        if q is None:
            return Response({"detail": "Mouvement introuvable."}, status=404)
        try:
            reconcile(q, request.user, str(request.data.get("decision") or ""), str(request.data.get("reason") or ""),
                      request.data.get("evidence"))
        except ReconciliationError as e:
            return Response({"detail": str(e)}, status=400)
        sync_enoc_to_registry(only_ids=[q.enoc_movement_id])  # le registre reflète immédiatement l'éligibilité (tracé)
        q.refresh_from_db()
        return Response(row(q))


class StockSiteBalanceView(APIView):
    """GET stock/registre/<site>/bilan/?debut=YYYY-MM-DD&fin=YYYY-MM-DD — bilan stock réel (NULL si composante inconnue)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, site_id: str):
        from datetime import timedelta

        from django.utils import timezone

        from fuel_tracking.services.stock_balance import site_balance
        from fuel_tracking.services.stock_registry import SiteResolver

        sid = SiteResolver().resolve(site_id)
        if sid is None:
            return Response({"detail": "Site inconnu."}, status=404)
        end = _date(request.query_params.get("fin")) or timezone.localdate()
        start = _date(request.query_params.get("debut")) or (end - timedelta(days=30))
        if start > end:
            return Response({"detail": "La date de début est postérieure à la date de fin."}, status=400)
        return Response(site_balance(sid, start, end))
