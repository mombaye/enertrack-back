# certification/views.py

import logging
from django.utils import timezone
from rest_framework import viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import CertificationBatch, CertificationResult, EfmsConnectionLog
from .serializers import (
    CertificationBatchSerializer,
    CertificationBatchDetailSerializer,
    CertificationResultSerializer,
    EfmsConnectionLogSerializer,
)
from .tasks import launch_certification_batch
from .services.efms import EfmsService

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CertificationBatchViewSet
# ─────────────────────────────────────────────────────────────────────────────

class CertificationBatchViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    list   GET  /certification/batches/
    detail GET  /certification/batches/{id}/
    launch POST /certification/batches/launch/
    status GET  /certification/batches/{id}/status/
    """
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = CertificationBatch.objects.select_related(
            "import_batch", "launched_by"
        ).order_by("-launched_at")

        batch_status = self.request.query_params.get("status")
        year  = self.request.query_params.get("year")
        month = self.request.query_params.get("month")

        if batch_status:
            qs = qs.filter(status=batch_status.upper())
        if year:
            qs = qs.filter(echeance_year=int(year))
        if month:
            qs = qs.filter(echeance_month=int(month))

        return qs

    def get_serializer_class(self):
        if self.action == "retrieve":
            return CertificationBatchDetailSerializer
        return CertificationBatchSerializer

    # ── POST /certification/batches/launch/ ──────────────────────────────────
    @action(methods=["post"], detail=False, url_path="launch")
    def launch(self, request):
        """
        Lance une campagne de certification sur un ImportBatch existant.

        Body: { "import_batch_id": 42 }

        Cree le CertificationBatch, declenche la tache Celery,
        retourne immediatement avec le cert_batch_id pour polling.
        """
        from billing.models import ImportBatch

        import_batch_id = request.data.get("import_batch_id")
        if not import_batch_id:
            return Response(
                {"detail": "import_batch_id requis."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            import_batch = ImportBatch.objects.get(id=import_batch_id)
        except ImportBatch.DoesNotExist:
            return Response(
                {"detail": f"ImportBatch #{import_batch_id} introuvable."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Verifie qu'une certification n'est pas deja en cours
        existing = CertificationBatch.objects.filter(
            import_batch=import_batch,
            status__in=[
                CertificationBatch.Status.PENDING,
                CertificationBatch.Status.RUNNING,
            ],
        ).first()
        if existing:
            return Response(
                {
                    "detail": "Une certification est deja en cours pour ce batch.",
                    "cert_batch_id": existing.id,
                    "status": existing.status,
                },
                status=status.HTTP_409_CONFLICT,
            )

        # Recupere l'echeance depuis l'ImportBatch si disponible
        echeance_year  = None
        echeance_month = None
        if hasattr(import_batch, "rows"):
            first_inv = import_batch.rows.filter(echeance__isnull=False).first()
            if first_inv and first_inv.echeance:
                echeance_year  = first_inv.echeance.year
                echeance_month = first_inv.echeance.month

        # Cree le CertificationBatch
        cert_batch = CertificationBatch.objects.create(
            import_batch   = import_batch,
            echeance_year  = echeance_year,
            echeance_month = echeance_month,
            launched_by    = request.user,
            status         = CertificationBatch.Status.PENDING,
        )

        # Lance la tache Celery
        task = launch_certification_batch.delay(cert_batch.id)

        cert_batch.celery_task_id = task.id
        cert_batch.save(update_fields=["celery_task_id"])

        logger.info(
            f"[API] CertificationBatch #{cert_batch.id} lance "
            f"(task={task.id}) par {request.user}"
        )

        return Response(
            {
                "cert_batch_id":  cert_batch.id,
                "celery_task_id": task.id,
                "status":         cert_batch.status,
                "echeance":       cert_batch.echeance_label,
                "detail": "Certification lancee. Utilisez /status/ pour suivre la progression.",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    # ── GET /certification/batches/{id}/status/ ───────────────────────────────
    @action(methods=["get"], detail=True, url_path="status")
    def batch_status(self, request, pk=None):
        """Polling de progression d'un batch."""
        batch = self.get_object()
        return Response(
            {
                "cert_batch_id":    batch.id,
                "status":           batch.status,
                "echeance":         batch.echeance_label,
                "launched_at":      batch.launched_at,
                "finished_at":      batch.finished_at,
                "celery_task_id":   batch.celery_task_id,
                "counters": {
                    "total":             batch.total,
                    "certified_fms":     batch.certified_fms,
                    "certified_senelec": batch.certified_senelec,
                    "needs_review":      batch.needs_review,
                    "unknown_contract":  batch.unknown_contract,
                    "fms_unavailable":   batch.fms_unavailable,
                },
            }
        )


# ─────────────────────────────────────────────────────────────────────────────
# CertificationResultViewSet
# ─────────────────────────────────────────────────────────────────────────────

class CertificationResultViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    list   GET /certification/results/
    detail GET /certification/results/{id}/

    Filtres : ?cert_batch= ?status= ?site= ?invoice= ?fms_available=
    """
    serializer_class   = CertificationResultSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = CertificationResult.objects.select_related(
            "cert_batch", "invoice", "site"
        ).order_by("-computed_at")

        cert_batch    = self.request.query_params.get("cert_batch")
        result_status = self.request.query_params.get("status")
        site          = self.request.query_params.get("site")
        invoice       = self.request.query_params.get("invoice")
        fms_available = self.request.query_params.get("fms_available")

        if cert_batch:
            qs = qs.filter(cert_batch_id=cert_batch)
        if result_status:
            qs = qs.filter(status=result_status.upper())
        if site:
            qs = qs.filter(site__site_id=site)
        if invoice:
            qs = qs.filter(invoice__numero_facture__icontains=invoice)
        if fms_available is not None:
            qs = qs.filter(fms_available=(fms_available.lower() == "true"))

        return qs


# ─────────────────────────────────────────────────────────────────────────────
# EfmsConnectionLogViewSet
# ─────────────────────────────────────────────────────────────────────────────

class EfmsConnectionLogViewSet(
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    """
    GET /certification/efms-logs/
    Filtres : ?status=  ?cert_batch=
    """
    serializer_class   = EfmsConnectionLogSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = EfmsConnectionLog.objects.select_related(
            "cert_batch"
        ).order_by("-attempted_at")

        log_status = self.request.query_params.get("status")
        cert_batch = self.request.query_params.get("cert_batch")

        if log_status:
            qs = qs.filter(status=log_status.upper())
        if cert_batch:
            qs = qs.filter(cert_batch_id=cert_batch)

        return qs


# ─────────────────────────────────────────────────────────────────────────────
# EfmsHealthCheckView
# ─────────────────────────────────────────────────────────────────────────────

# Remplacer la classe EfmsHealthCheckView dans certification/views.py

class EfmsHealthCheckView(APIView):
    """
    GET /certification/efms-health/
    GET /certification/efms-health/?verbose=1   ← diagnostic complet
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        efms = EfmsService()
        verbose = request.query_params.get("verbose") in ("1", "true", "yes")

        if verbose:
            result = efms.diagnose()
            return Response(
                {
                    "efms_reachable": result["query_ok"],
                    "tcp_reachable":  result["tcp_reachable"],
                    "odbc_connected": result["odbc_connected"],
                    "query_ok":       result["query_ok"],
                    "row_count_test": result["row_count_test"],
                    "host":           result["host"],
                    "port":           result["port"],
                    "db":             result["db"],
                    "user":           result["user"],
                    "driver":         result["driver"],
                    "error":          result["error"],
                    "checked_at":     timezone.now().isoformat(),
                },
                status=status.HTTP_200_OK if result["query_ok"] else status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        ok = efms.check_connection()
        return Response(
            {
                "efms_reachable": ok,
                "host":           efms.host,
                "port":           efms.port,
                "checked_at":     timezone.now().isoformat(),
            },
            status=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )