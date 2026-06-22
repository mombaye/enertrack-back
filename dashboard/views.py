# dashboard/views.py

import logging
from datetime import date
from decimal import Decimal

from django.db.models import Sum, Count, Avg, Q, Max
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError

from billing.models import (
    ImportBatch,
    SonatelInvoice,
    MonthlySynthesis,
    ContractMonth,
    ContractSiteLink,
)
from certification.models import CertificationBatch, CertificationResult

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_qp_date(request, name: str):
    from datetime import datetime
    v = request.query_params.get(name)
    if not v:
        return None
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except Exception:
        raise ValidationError({name: "format attendu YYYY-MM-DD"})


def _s(v) -> str:
    """Decimal → string JSON-safe, 0 si None."""
    if v is None:
        return "0"
    return str(v)


def _pct(part, total) -> float:
    """Pourcentage arrondi 1 décimale, 0 si total nul."""
    if not total:
        return 0.0
    return round(float((part or 0) / total * 100), 1)


def _filter_year_month_range(qs, start: date, end: date):
    """
    Filtre MonthlySynthesis / ContractMonth par (year, month)
    sur la plage [start, end] inclusive.
    """
    sy, sm = start.year, start.month
    ey, em = end.year, end.month
    return qs.filter(
        (Q(year__gt=sy) | Q(year=sy, month__gte=sm)),
        (Q(year__lt=ey) | Q(year=ey, month__lte=em)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# DashboardSummaryView
# ─────────────────────────────────────────────────────────────────────────────

class DashboardSummaryView(APIView):
    """
    GET /api/dashboard/summary/
        ?start=YYYY-MM-DD   (défaut : 1er jan de l'année en cours)
        ?end=YYYY-MM-DD     (défaut : aujourd'hui)

    Agrège billing + certification en un seul appel.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        today = timezone.localdate()
        start = _parse_qp_date(request, "start") or date(today.year, 1, 1)
        end   = _parse_qp_date(request, "end")   or today

        if end < start:
            raise ValidationError({"end": "end < start"})

        return Response({
            "range":         {"start": start.isoformat(), "end": end.isoformat()},
            "billing":       self._billing(start, end),
            "certification": self._certification(start, end),
        })

    # ──────────────────────────────────────────────────────────────────────────
    # BILLING
    # ──────────────────────────────────────────────────────────────────────────

    def _billing(self, start: date, end: date) -> dict:

        # ── Base MonthlySynthesis filtrée sur la plage + sites éligibles ─────────
        ms_base = _filter_year_month_range(
            MonthlySynthesis.objects.select_related("source__site").filter(
                source__site__isnull=False,
                source__site__invoice_payment__iexact="Aktivco",
                source__site__grid_fee=True,
            ),
            start, end,
        )

        # Agrégats globaux
        agg = ms_base.aggregate(
            total_ttc      = Sum("montant_ttc"),
            total_ht       = Sum("montant_hors_tva"),
            total_nrj      = Sum("energie_calculee"),
            total_conso    = Sum("conso"),
            total_abo      = Sum("abonnement_calcule"),
            total_pen      = Sum("penalite_abonnement_calculee"),
            total_cosphi   = Sum("montant_cosinus_phi"),
        )

        # Nb factures distinctes dans la plage (via overlap avec periode)
        inv_qs = SonatelInvoice.objects.filter(
            site__isnull=False,
            site__invoice_payment__iexact="Aktivco",
            site__grid_fee=True,
            date_debut_periode__lte=end,
            date_fin_periode__gte=start,
        )

        total_invoices = inv_qs.count()

        # Nb sites et contrats actifs (avec au moins 1 facture dans la plage)
        active_contracts = (
            inv_qs.values("numero_compte_contrat").distinct().count()
        )
        active_sites = (
            inv_qs.values("site_id").distinct().count()
        )

        # Répartition statuts
        status_counts = (
            inv_qs.values("status")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        status_distribution = [
            {
                "status":  r["status"] or "UNKNOWN",
                "count":   r["count"],
                "percent": _pct(r["count"], total_invoices),
            }
            for r in status_counts
        ]

        # Dernier import
        last_import_obj = (
            ImportBatch.objects
            .filter(kind=ImportBatch.Kind.SENELEC_INVOICE)
            .order_by("-imported_at")
            .first()
        )
        last_import = None
        if last_import_obj:
            last_import = {
                "id":              last_import_obj.id,
                "source_filename": last_import_obj.source_filename,
                "imported_at":     last_import_obj.imported_at.isoformat(),
            }

        # Évolution mensuelle
        evo_qs = (
            ms_base
            .values("year", "month")
            .annotate(
                invoices   = Count("source_id", distinct=True),
                montant_ht = Sum("montant_hors_tva"),
                montant_ttc= Sum("montant_ttc"),
                nrj        = Sum("energie_calculee"),
                conso      = Sum("conso"),
                abonnement = Sum("abonnement_calcule"),
                pen_prime  = Sum("penalite_abonnement_calculee"),
                cosphi     = Sum("montant_cosinus_phi"),
            )
            .order_by("year", "month")
        )
        evolution = [
            {
                "period":      f"{r['year']}-{str(r['month']).zfill(2)}",
                "invoices":    r["invoices"] or 0,
                "montant_ht":  _s(r["montant_ht"]),
                "montant_ttc": _s(r["montant_ttc"]),
                "nrj":         _s(r["nrj"]),
                "conso":       _s(r["conso"]),
                "abonnement":  _s(r["abonnement"]),
                "pen_prime":   _s(r["pen_prime"]),
                "cosphi":      _s(r["cosphi"]),
            }
            for r in evo_qs
        ]

        return {
            "total_invoices":  total_invoices,
            "total_ttc":       _s(agg["total_ttc"]),
            "total_ht":        _s(agg["total_ht"]),
            "total_nrj":       _s(agg["total_nrj"]),
            "total_conso_kwh": _s(agg["total_conso"]),
            "total_abo":       _s(agg["total_abo"]),
            "total_pen":       _s(agg["total_pen"]),
            "total_cosphi":    _s(agg["total_cosphi"]),
            "active_contracts": active_contracts,
            "active_sites":     active_sites,
            "last_import":      last_import,
            "status_distribution": status_distribution,
            "evolution":        evolution,
        }

    # ──────────────────────────────────────────────────────────────────────────
    # CERTIFICATION
    # ──────────────────────────────────────────────────────────────────────────

    def _certification(self, start: date, end: date) -> dict:

        # Batches dans la plage (launched_at)
        batches_qs = CertificationBatch.objects.filter(
            launched_at__date__gte=start,
            launched_at__date__lte=end,
        ).order_by("-launched_at")

        # Dernier batch (tous statuts, pas seulement DONE)
        last_batch_obj = CertificationBatch.objects.order_by("-launched_at").first()
        last_batch = None
        if last_batch_obj:
            last_batch = {
                "id":               last_batch_obj.id,
                "echeance":         last_batch_obj.echeance_label,
                "status":           last_batch_obj.status,
                "launched_at":      last_batch_obj.launched_at.isoformat() if last_batch_obj.launched_at else None,
                "finished_at":      last_batch_obj.finished_at.isoformat() if last_batch_obj.finished_at else None,
                "total":            last_batch_obj.total,
                "certified_fms":    last_batch_obj.certified_fms,
                "certified_senelec":last_batch_obj.certified_senelec,
                "needs_review":     last_batch_obj.needs_review,
                "unknown_contract": last_batch_obj.unknown_contract,
                "fms_unavailable":  last_batch_obj.fms_unavailable,
            }

        # Taux global (sur TOUS les batches DONE dans la plage)
        done_batches = batches_qs.filter(status=CertificationBatch.Status.DONE)
        global_agg = done_batches.aggregate(
            t_total   = Sum("total"),
            t_cfms    = Sum("certified_fms"),
            t_csen    = Sum("certified_senelec"),
            t_review  = Sum("needs_review"),
            t_unknown = Sum("unknown_contract"),
            t_fms_na  = Sum("fms_unavailable"),
        )
        gt = global_agg["t_total"] or 0
        global_rate = {
            "certified_fms":     _pct(global_agg["t_cfms"],    gt),
            "certified_senelec": _pct(global_agg["t_csen"],    gt),
            "needs_review":      _pct(global_agg["t_review"],  gt),
            "unknown_contract":  _pct(global_agg["t_unknown"], gt),
            "fms_unavailable":   _pct(global_agg["t_fms_na"],  gt),
        }

        # Historique des batches (10 derniers, tous statuts)
        history = [
            {
                "id":               b.id,
                "echeance":         b.echeance_label,
                "status":           b.status,
                "launched_at":      b.launched_at.isoformat() if b.launched_at else None,
                "total":            b.total,
                "certified_fms":    b.certified_fms,
                "certified_senelec":b.certified_senelec,
                "needs_review":     b.needs_review,
                "unknown_contract": b.unknown_contract,
                "fms_unavailable":  b.fms_unavailable,
            }
            for b in batches_qs[:10]
        ]

        return {
            "last_batch":  last_batch,
            "global_rate": global_rate,
            "history":     history,
            "total_batches_in_range": batches_qs.count(),
        }