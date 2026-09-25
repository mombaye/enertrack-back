# ─────────────────────────────────────────────────────────────────────────────
# estimation/views.py
# ─────────────────────────────────────────────────────────────────────────────

from rest_framework import viewsets, mixins, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.utils import timezone
from rest_framework.views import APIView
from .models import EstimationBatch, EstimationResult, ExternalEstimationUpload
from .serializers import EstimationBatchSerializer, EstimationResultSerializer
from .tasks import launch_estimation_batch
from rest_framework.parsers import MultiPartParser


def _reset_and_launch(year: int, month: int, user):
    """(Re)lance l'estimation programme d'un mois. Retourne (batch, created, already_running)."""
    user = user if user and user.is_authenticated else None
    batch, created = EstimationBatch.objects.get_or_create(
        year=year, month=month,
        defaults={"created_by": user, "status": EstimationBatch.Status.PENDING},
    )
    if not created:
        if batch.status == EstimationBatch.Status.RUNNING:
            return batch, False, True
        batch.results.all().delete()
        batch.status      = EstimationBatch.Status.PENDING
        batch.finished_at = None
        batch.celery_task_id = None
        batch.total = 0
        batch.count_acm = 0
        batch.count_grid = 0
        batch.count_senelec = 0
        batch.count_target = 0
        batch.count_theorique = 0
        batch.count_histo = 0
        batch.count_nc = 0
        batch.count_hors_scope = 0
        batch.created_by = user
        batch.save()

    task = launch_estimation_batch.delay(batch.id)
    batch.celery_task_id = task.id
    batch.save(update_fields=["celery_task_id"])
    return batch, created, False


def _parse_provisions_file(file_bytes: bytes):
    """
    Lit un fichier Provisions (Feuil1 : site_ID | Site_Name | Conso_Kwh | Montant | Source | Mois).
    Retourne ({(year, month): [row, ...]}, total_parsed, skipped_invalid_dates).
    Lève ValueError si le fichier est illisible.
    """
    import io
    import openpyxl
    from collections import defaultdict
    from datetime import datetime
    from decimal import Decimal, InvalidOperation

    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as e:
        raise ValueError(f"Impossible de lire le fichier : {e}") from e

    ws = wb["Feuil1"] if "Feuil1" in wb.sheetnames else wb.worksheets[0]
    by_month: dict[tuple, list] = defaultdict(list)
    skipped_date = 0
    total_parsed = 0

    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[0]:
            continue
        row = tuple(row) + (None,) * (6 - len(row))
        date_raw = row[5]
        if date_raw is None:
            skipped_date += 1
            continue
        if hasattr(date_raw, "year"):
            year, month = date_raw.year, date_raw.month
        else:
            try:
                dt = datetime.strptime(str(date_raw)[:7], "%Y-%m")
                year, month = dt.year, dt.month
            except ValueError:
                skipped_date += 1
                continue

        try:
            conso   = Decimal(str(row[2])) if row[2] is not None else None
            montant = Decimal(str(row[3])) if row[3] is not None else None
        except (InvalidOperation, ValueError):
            conso = montant = None

        by_month[(year, month)].append({
            "site_id_raw":   str(row[0]).strip(),
            "site_name_raw": str(row[1]).strip() if row[1] else "",
            "conso":         conso,
            "montant":       montant,
            "source_raw":    str(row[4]).strip() if row[4] else "",
        })
        total_parsed += 1

    wb.close()
    return by_month, total_parsed, skipped_date


def _store_external(by_month: dict, user) -> tuple[int, list[str], int]:
    """
    Remplace atomiquement les ExternalEstimationUpload de chaque (year, month) du fichier.
    Retourne (lignes importées, périodes "YYYY-MM", lignes dont le site est inconnu).
    """
    from django.db import transaction
    from core.models import Site

    site_map = {s.site_id: s for s in Site.objects.only("id", "site_id")}
    imported = 0
    unknown_sites = 0
    periods = []

    with transaction.atomic():
        for (year, month), rows in sorted(by_month.items()):
            ExternalEstimationUpload.objects.filter(year=year, month=month).delete()
            objs = []
            seen_site_pks = set()
            for item in rows:
                site = site_map.get(item["site_id_raw"])
                if site is None:
                    unknown_sites += 1
                elif site.pk in seen_site_pks:
                    continue
                else:
                    seen_site_pks.add(site.pk)
                objs.append(ExternalEstimationUpload(
                    year=year,
                    month=month,
                    site=site,
                    site_id_raw=item["site_id_raw"],
                    site_name_raw=item["site_name_raw"],
                    conso_kwh=item["conso"],
                    montant=item["montant"],
                    source_raw=item["source_raw"],
                    uploaded_by=user,
                ))
            ExternalEstimationUpload.objects.bulk_create(objs, batch_size=1000)
            imported += len(objs)
            periods.append(f"{year}-{month:02d}")

    return imported, periods, unknown_sites


class EstimationBatchViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    list   GET  /estimation/batches/
    detail GET  /estimation/batches/{id}/
    launch POST /estimation/batches/launch/
    status GET  /estimation/batches/{id}/status/
    """
    serializer_class   = EstimationBatchSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = EstimationBatch.objects.all().order_by("-year", "-month")
        year  = self.request.query_params.get("year")
        month = self.request.query_params.get("month")
        if year:  qs = qs.filter(year=int(year))
        if month: qs = qs.filter(month=int(month))
        return qs

    @action(detail=False, methods=["post"], url_path="launch")
    def launch(self, request):
        year  = request.data.get("year")
        month = request.data.get("month")

        if not year or not month:
            return Response(
                {"detail": "year et month sont requis."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            year, month = int(year), int(month)
        except (ValueError, TypeError):
            return Response(
                {"detail": "year et month doivent être des entiers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        batch, created, already_running = _reset_and_launch(year, month, request.user)
        if already_running:
            return Response(
                {"detail": "Une estimation est déjà en cours pour ce mois.", "batch_id": batch.id},
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            {
                "batch_id":       batch.id,
                "celery_task_id": batch.celery_task_id,
                "status":         batch.status,
                "label":          batch.label,
                "detail":         "Estimation lancée." if created else "Estimation re-lancée.",
            },
            status=status.HTTP_202_ACCEPTED,
        )

    @action(methods=["get"], detail=True, url_path="status")
    def batch_status(self, request, pk=None):
        batch = self.get_object()
        return Response({
            "batch_id":     batch.id,
            "label":        batch.label,
            "status":       batch.status,
            "created_at":   batch.created_at,
            "finished_at":  batch.finished_at,
           "counters": {
                "total": batch.total,
                "count_acm": batch.count_acm,
                "count_grid": batch.count_grid,
                "count_senelec": batch.count_senelec,
                "count_target": batch.count_target,
                "count_theorique": batch.count_theorique,
                "count_histo": batch.count_histo,
                "count_nc": batch.count_nc,
                "count_hors_scope": batch.count_hors_scope,
            },
        })


class EstimationResultViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """
    list   GET /estimation/results/
    detail GET /estimation/results/{id}/

    Filtres : ?batch= ?source= ?site= ?fiabilite_grid=
    """
    serializer_class   = EstimationResultSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = EstimationResult.objects.select_related("batch", "site").order_by("-computed_at")

        batch          = self.request.query_params.get("batch")
        source         = self.request.query_params.get("source")
        site           = self.request.query_params.get("site")
        fiabilite_grid = self.request.query_params.get("fiabilite_grid")
        year           = self.request.query_params.get("year")
        month          = self.request.query_params.get("month")

        if batch:          qs = qs.filter(batch_id=batch)
        if source:         qs = qs.filter(source_utilisee=source.upper())
        if site:           qs = qs.filter(site__site_id=site)
        if fiabilite_grid: qs = qs.filter(fiabilite_grid=fiabilite_grid.upper())
        if year:           qs = qs.filter(batch__year=int(year))
        if month:          qs = qs.filter(batch__month=int(month))

        return qs


 
class EstimationHistoryImportView(APIView):
    """
    POST /estimation/history/import/
    Fichier Provisions_GRID_Conso.xlsx (Feuil1 : site_ID | Site_Name | Conso_Kwh | Montant | Source | Mois).

    1. Les valeurs du fichier sont enregistrées comme estimation de référence
       (ExternalEstimationUpload) pour chaque mois trouvé — elles n'écrasent plus
       les résultats calculés par le programme.
    2. L'estimation programme est relancée pour le mois le plus récent du fichier.
    3. Le front affiche ensuite la comparaison programme ↔ fichier pour ce mois.
    """
    parser_classes     = [MultiPartParser]
    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Aucun fichier fourni."}, status=400)

        try:
            by_month, total_parsed, skipped_date = _parse_provisions_file(f.read())
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        if not by_month:
            return Response({"detail": "Aucune ligne valide trouvée."}, status=400)

        imported, periods, unknown_sites = _store_external(by_month, request.user)

        year, month = max(by_month)
        batch, _, already_running = _reset_and_launch(year, month, request.user)

        return Response(
            {
                "message":               "Import terminé — estimation relancée.",
                "total_parsed":          total_parsed,
                "imported":              imported,
                "periods":               periods,
                "skipped_unknown_sites": unknown_sites,
                "skipped_invalid_dates": skipped_date,
                "relaunched": {
                    "batch_id":        batch.id,
                    "year":            year,
                    "month":           month,
                    "label":           batch.label,
                    "status":          batch.status,
                    "already_running": already_running,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class ExternalEstimationImportView(APIView):
    """
    POST /estimation/external/import/
    Importe un fichier d'estimation externe (même format que l'historique :
    site_ID | Site_Name | Conso_Kwh | Montant | Source | Mois).
    Remplace atomiquement les données externes existantes pour chaque
    (year, month) trouvé dans le fichier.
    """
    permission_classes = [IsAuthenticated]
    parser_classes = [MultiPartParser]

    def post(self, request, *args, **kwargs):
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Aucun fichier fourni."}, status=400)

        try:
            by_month, _, skipped_date = _parse_provisions_file(f.read())
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        if not by_month:
            return Response({"detail": "Aucune ligne valide trouvée."}, status=400)

        imported, periods, _ = _store_external(by_month, request.user)

        return Response({
            "imported": imported,
            "periods": periods,
            "skipped_invalid_dates": skipped_date,
        }, status=status.HTTP_201_CREATED)


class EstimationCompareView(APIView):
    """
    GET /estimation/compare/?year=&month=
    Compare les EstimationResult du programme (batch EnerTrack) avec les
    données importées via ExternalEstimationImportView pour le même mois.
    """
    permission_classes = [IsAuthenticated]

    MATCH_THRESHOLD_PCT = 5.0

    def get(self, request, *args, **kwargs):
        year  = request.query_params.get("year")
        month = request.query_params.get("month")

        if not year or not month:
            return Response({"detail": "Paramètres year et month requis."}, status=400)
        try:
            year, month = int(year), int(month)
        except ValueError:
            return Response({"detail": "year et month doivent être des entiers."}, status=400)

        # EnerTrack batch results
        try:
            batch = EstimationBatch.objects.get(year=year, month=month)
        except EstimationBatch.DoesNotExist:
            return Response({"detail": "Aucun batch EnerTrack pour cette période."}, status=404)

        et_results = {
            r.site.site_id: r
            for r in EstimationResult.objects.filter(batch=batch).select_related("site")
            if r.site and r.site.site_id
        }

        # External results
        ext_rows = ExternalEstimationUpload.objects.filter(year=year, month=month)
        has_external = ext_rows.exists()
        ext_by_site_id = {e.site_id_raw: e for e in ext_rows}

        rows = []
        matched = 0
        total_ext = len(ext_by_site_id)

        # All site IDs from both sources
        all_site_ids = set(et_results.keys()) | set(ext_by_site_id.keys())

        for sid in sorted(all_site_ids):
            et = et_results.get(sid)
            ext = ext_by_site_id.get(sid)

            et_conso   = float(et.conso_estimee_kwh)   if et and et.conso_estimee_kwh   is not None else None
            et_montant = float(et.montant_estime)       if et and et.montant_estime       is not None else None
            et_source  = et.source_utilisee if et else None
            et_name    = et.site.name if et and et.site else sid

            ext_conso   = float(ext.conso_kwh) if ext and ext.conso_kwh   is not None else None
            ext_montant = float(ext.montant)    if ext and ext.montant     is not None else None
            ext_source  = ext.source_raw if ext else None
            ext_name    = ext.site_name_raw if ext else ""

            site_name = et_name or ext_name or sid

            ecart_conso     = (et_conso   - ext_conso)   if et_conso   is not None and ext_conso   is not None else None
            ecart_montant   = (et_montant - ext_montant) if et_montant is not None and ext_montant is not None else None
            ecart_conso_pct = (ecart_conso / ext_conso * 100)   if ecart_conso   is not None and ext_conso   != 0 else None
            ecart_montant_pct = (ecart_montant / ext_montant * 100) if ecart_montant is not None and ext_montant != 0 else None

            is_match = (
                ecart_conso_pct is not None
                and abs(ecart_conso_pct) <= self.MATCH_THRESHOLD_PCT
            )
            if is_match:
                matched += 1

            rows.append({
                "site_id":            sid,
                "site_name":          site_name,
                "enertrack_conso":    et_conso,
                "enertrack_montant":  et_montant,
                "enertrack_source":   et_source,
                "external_conso":     ext_conso,
                "external_montant":   ext_montant,
                "external_source":    ext_source,
                "ecart_conso":        ecart_conso,
                "ecart_conso_pct":    round(ecart_conso_pct, 2) if ecart_conso_pct is not None else None,
                "ecart_montant":      ecart_montant,
                "ecart_montant_pct":  round(ecart_montant_pct, 2) if ecart_montant_pct is not None else None,
                "match":              is_match,
            })

        comparable = sum(1 for r in rows if r["external_conso"] is not None and r["enertrack_conso"] is not None)

        return Response({
            "year":         year,
            "month":        month,
            "has_external": has_external,
            "score": {
                "total_enertrack":  len(et_results),
                "total_external":   total_ext,
                "comparable":       comparable,
                "matched":          matched,
                "match_pct":        round(matched / comparable * 100, 1) if comparable else 0,
                "threshold_pct":    self.MATCH_THRESHOLD_PCT,
            },
            "rows": rows,
        })
 