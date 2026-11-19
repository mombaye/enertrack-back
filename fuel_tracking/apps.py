from django.apps import AppConfig


class FuelTrackingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fuel_tracking"

    def ready(self):
        # BASE_GE est ajouté ici pour rester compatible avec les déploiements
        # précédents dont la table utilise déjà le pipeline d'imports générique.
        from fuel_tracking.services import stock_import_files as files
        from fuel_tracking.services.base_ge_import import process_base_ge_file
        files.PROCESSORS["BASE_GE"] = process_base_ge_file

        from fuel_tracking.views_stock import ImportBatchListView
        from rest_framework.response import Response
        from rest_framework.parsers import FormParser, MultiPartParser
        from fuel_tracking.views_cph import IsCphValidator
        from fuel_tracking.services import stock_import_pipeline as pipeline

        def post(self, request):
            f = request.FILES.get("file")
            source_type = str(request.data.get("source_type") or "").upper()
            if f is None:
                return Response({"detail": "Fichier manquant."}, status=400)
            allowed = set(pipeline.INCOMING.values()) | {"HISTORICAL_REFUELING", "BASE_GE"}
            if source_type not in allowed:
                return Response({"detail": "Type de source invalide."}, status=400)
            if not f.name.lower().endswith(pipeline.ACCEPTED_EXT):
                return Response({"detail": "Format accepté : .xlsx, .xlsm, .xlsb."}, status=400)
            try:
                batch = pipeline.process_bytes(f.read(), f.name, source_type, origin="UPLOAD", user=request.user)
            except OSError as exc:
                return Response({"detail": f"Répertoire d'import indisponible ({exc.__class__.__name__})."}, status=503)
            from fuel_tracking.views_stock import _batch
            return Response(_batch(batch), status=201)

        # La méthode originale refusait BASE_GE avant d'appeler le processeur.
        ImportBatchListView.post = post
        ImportBatchListView.parser_classes = [MultiPartParser, FormParser]
        ImportBatchListView.permission_classes = [IsCphValidator]
