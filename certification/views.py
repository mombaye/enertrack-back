# certification/views.py
# PATCH v4.1 — 3 corrections :
#   (1) batch_status() : mesure_alert ajouté dans counters
#   (2) launch() reset block : mesure_alert = 0 + update_fields
#   (3) CertificationResultViewSet : filtre flag_mesure_alert

import logging
from django.utils import timezone
from rest_framework import viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from billing.models import ImportBatch
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


class CertificationBatchViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
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

    @action(detail=False, methods=["post"], url_path="launch")
    def launch(self, request):
        import_batch_id = request.data.get("import_batch_id")
        if not import_batch_id:
            return Response(
                {"detail": "import_batch_id est requis."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            import_batch = ImportBatch.objects.get(pk=import_batch_id)
        except ImportBatch.DoesNotExist:
            return Response(
                {"detail": f"ImportBatch #{import_batch_id} introuvable."},
                status=status.HTTP_404_NOT_FOUND,
            )

        task_status   = getattr(import_batch, "task_status", None)
        task_progress = getattr(import_batch, "task_progress", None)
        task_message  = getattr(import_batch, "task_message", None)

        try:
            success_value = ImportBatch.TaskStatus.SUCCESS
        except Exception:
            success_value = "SUCCESS"

        if task_status != success_value:
            return Response(
                {
                    "detail": "L'import des factures n'est pas encore terminé pour ce batch.",
                    "import_batch_id": import_batch.id,
                    "task_status": task_status,
                    "task_progress": task_progress,
                    "task_message": task_message,
                },
                status=status.HTTP_409_CONFLICT,
            )

        if not import_batch.rows.exists():
            return Response(
                {
                    "detail": "Ce batch ne contient aucune facture importée.",
                    "import_batch_id": import_batch.id,
                },
                status=status.HTTP_409_CONFLICT,
            )

        echeance_year  = request.data.get("echeance_year")
        echeance_month = request.data.get("echeance_month")

        if not echeance_year or not echeance_month:
            first_invoice = (
                import_batch.rows
                .filter(echeance__isnull=False)
                .values("echeance")
                .first()
            )
            if first_invoice and first_invoice["echeance"]:
                d = first_invoice["echeance"]
                echeance_year  = d.year
                echeance_month = d.month

        if not echeance_year or not echeance_month:
            first_invoice = (
                import_batch.rows
                .filter(date_debut_periode__isnull=False)
                .values("date_debut_periode")
                .first()
            )
            if first_invoice and first_invoice["date_debut_periode"]:
                d = first_invoice["date_debut_periode"]
                echeance_year  = d.year
                echeance_month = d.month

        try:
            echeance_year  = int(echeance_year)  if echeance_year  else None
            echeance_month = int(echeance_month) if echeance_month else None
        except (ValueError, TypeError):
            echeance_year  = None
            echeance_month = None

        if not echeance_year or not echeance_month:
            return Response(
                {
                    "detail": "Impossible de déterminer l'échéance du batch.",
                    "import_batch_id": import_batch.id,
                },
                status=status.HTTP_409_CONFLICT,
            )

        cert_batch, created = CertificationBatch.objects.get_or_create(
            import_batch=import_batch,
            defaults={
                "launched_by":   request.user if request.user.is_authenticated else None,
                "echeance_year": echeance_year,
                "echeance_month": echeance_month,
                "status": CertificationBatch.Status.PENDING,
            },
        )

        if not created:
            if cert_batch.status == CertificationBatch.Status.RUNNING:
                return Response(
                    {
                        "detail": "Une certification est déjà en cours pour ce batch.",
                        "cert_batch_id": cert_batch.id,
                        "status": cert_batch.status,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            cert_batch.results.all().delete()
            cert_batch.status          = CertificationBatch.Status.PENDING
            cert_batch.finished_at     = None
            cert_batch.celery_task_id  = None
            cert_batch.total           = 0
            cert_batch.certified_fms   = 0
            cert_batch.certified_senelec = 0
            cert_batch.needs_review    = 0
            cert_batch.unknown_contract = 0
            cert_batch.fms_unavailable = 0
            cert_batch.mesure_alert    = 0   # ✅ v4 — reset compteur alerte mesure
            cert_batch.launched_by     = request.user if request.user.is_authenticated else None
            cert_batch.echeance_year   = echeance_year
            cert_batch.echeance_month  = echeance_month

            cert_batch.save(
                update_fields=[
                    "status", "finished_at", "celery_task_id",
                    "total", "certified_fms", "certified_senelec",
                    "needs_review", "unknown_contract", "fms_unavailable",
                    "mesure_alert",   # ✅ v4
                    "launched_by", "echeance_year", "echeance_month",
                ]
            )

        task = launch_certification_batch.delay(cert_batch.id)
        cert_batch.celery_task_id = task.id
        cert_batch.save(update_fields=["celery_task_id"])

        return Response(
            {
                "cert_batch_id":  cert_batch.id,
                "celery_task_id": task.id,
                "status":         cert_batch.status,
                "echeance":       cert_batch.echeance_label,
                "detail": "Certification lancée." if created else "Certification re-lancée.",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @action(methods=["get"], detail=True, url_path="status")
    def batch_status(self, request, pk=None):
        """Polling de progression d'un batch."""
        batch = self.get_object()
        return Response(
            {
                "cert_batch_id":  batch.id,
                "status":         batch.status,
                "echeance":       batch.echeance_label,
                "launched_at":    batch.launched_at,
                "finished_at":    batch.finished_at,
                "celery_task_id": batch.celery_task_id,
                "counters": {
                    "total":             batch.total,
                    "certified_fms":     batch.certified_fms,
                    "certified_senelec": batch.certified_senelec,
                    "needs_review":      batch.needs_review,
                    "unknown_contract":  batch.unknown_contract,
                    "fms_unavailable":   batch.fms_unavailable,
                    "mesure_alert":      batch.mesure_alert or 0,  # ✅ v4
                },
            }
        )


class CertificationResultViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
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

        # ✅ v4.1 — filtre alerte mesure bidirectionnel
        flag_mesure   = self.request.query_params.get("flag_mesure_alert")

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

        # ✅ v4.1 — ?flag_mesure_alert=true → seulement les lignes alertées
        if flag_mesure is not None:
            qs = qs.filter(flag_mesure_alert=(flag_mesure.lower() == "true"))

        return qs


class EfmsConnectionLogViewSet(
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class   = EfmsConnectionLogSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = EfmsConnectionLog.objects.select_related("cert_batch").order_by("-attempted_at")

        log_status = self.request.query_params.get("status")
        cert_batch = self.request.query_params.get("cert_batch")

        if log_status:
            qs = qs.filter(status=log_status.upper())
        if cert_batch:
            qs = qs.filter(cert_batch_id=cert_batch)

        return qs


class EfmsHealthCheckView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        efms    = EfmsService()
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