# fuel_tracking/views.py
#
# Module suivi-carburant : toutes les données viennent d'un import manuel du
# fichier Excel mensuel "Commande FUEL ESCO SENEGAL <mois>" (bouton
# "Importer" du frontend, ou commande de gestion import_commande_synthese).
# Aucune récupération automatique (eFMS SQL Server, ENOC Mongo/API,
# Snowflake) : ce pipeline live a été retiré pour repartir sur une base
# simple, un onglet à la fois, chacun alimenté par sa propre feuille source.

import logging

from django.conf import settings
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


class FuelCommandeSyntheseView(APIView):
    """
    GET /api/fuel-tracking/commande-synthese/?month=YYYY-MM

    Retourne l'import brut (sans recalcul) de la feuille "Synthèse Commande"
    du fichier Excel mensuel "Commande FUEL ESCO SENEGAL <mois>", groupé par
    bloc (CATEGORIE / TYPOLOGIE). Alimenté par la commande de gestion
    import_commande_synthese. Sans mois demandé (ou si absent), retourne le
    mois le plus récent disponible.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import FuelCommandeSynthese

        available_months = list(
            FuelCommandeSynthese.objects.order_by("-month_year")
            .values_list("month_year", flat=True)
            .distinct()
        )

        month = request.query_params.get("month")
        if not month:
            if not available_months:
                return Response({
                    "month_year": None,
                    "prev_month_year": None,
                    "categorie": [],
                    "typologie": [],
                    "available_months": [],
                })
            month = available_months[0]

        rows = FuelCommandeSynthese.objects.filter(month_year=month).order_by("group_type", "order_index")

        def serialize(row):
            return {
                "label": row.label,
                "is_total_row": row.is_total_row,
                "nb_sites": float(row.nb_sites),
                "commande_normale_l": float(row.commande_normale_l),
                "commande_hivernale_l": float(row.commande_hivernale_l),
                "total_l": float(row.total_l),
                "nb_sites_prev": float(row.nb_sites_prev),
                "commande_normale_prev_l": float(row.commande_normale_prev_l),
                "commande_hivernale_prev_l": float(row.commande_hivernale_prev_l),
                "total_prev_l": float(row.total_prev_l),
                "ecart_sites": float(row.ecart_sites),
                "ecart_qte_l": float(row.ecart_qte_l),
                "commentaires": row.commentaires,
            }

        categorie_rows = [serialize(r) for r in rows if r.group_type == FuelCommandeSynthese.GroupType.CATEGORIE]
        typologie_rows = [serialize(r) for r in rows if r.group_type == FuelCommandeSynthese.GroupType.TYPOLOGIE]
        prev_month = rows[0].prev_month_year if rows else None

        return Response({
            "month_year": month,
            "prev_month_year": prev_month,
            "categorie": categorie_rows,
            "typologie": typologie_rows,
            "available_months": available_months,
        })


class FuelCommandeSyntheseImportView(APIView):
    """
    POST /api/fuel-tracking/commande-synthese/import/
    (multipart, champs "file", "month_year", "prev_month_year")

    Upload du classeur Excel mensuel complet "Commande FUEL ESCO SENEGAL
    <mois>.xlsb" (ou .xlsx) — bouton "Importer" du frontend. Le mois courant
    et le mois précédent (ex: Août / Juillet) sont fournis explicitement par
    l'utilisateur au moment de l'upload — obligatoires, pas de détection
    automatique depuis le fichier ici (peu fiable d'un mois à l'autre, voir
    fuel_tracking/services/commande_synthese_import.py). On enregistre le
    fichier tel quel dans data_imports/ (traçabilité) puis on en extrait la
    feuille "Synthèse Commande" via le même parseur que la commande de
    gestion import_commande_synthese : import brut, sans recalcul.
    """
    parser_classes = [MultiPartParser]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import re
        from pathlib import Path

        from fuel_tracking.services.commande_synthese_import import (
            CommandeSyntheseImportError,
            import_commande_synthese_file,
            validate_month_year,
        )

        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Aucun fichier fourni."}, status=400)

        allowed_ext = (".xlsb", ".xlsx", ".xlsm")
        if not f.name.lower().endswith(allowed_ext):
            return Response({"detail": f"Format non supporté. Attendu : {', '.join(allowed_ext)}"}, status=400)

        month_year = request.data.get("month_year")
        prev_month_year = request.data.get("prev_month_year")
        try:
            validate_month_year(month_year, "Mois concerné")
            validate_month_year(prev_month_year, "Mois précédent")
        except CommandeSyntheseImportError as e:
            return Response({"detail": str(e)}, status=400)
        if month_year == prev_month_year:
            return Response({"detail": "Le mois concerné et le mois précédent doivent être différents."}, status=400)

        dest_dir = Path(settings.BASE_DIR) / "data_imports"
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w\.\- ]", "_", f.name)
        dest_path = dest_dir / safe_name

        with open(dest_path, "wb") as out:
            for chunk in f.chunks():
                out.write(chunk)

        try:
            rows_imported, resolved_month_year = import_commande_synthese_file(
                str(dest_path), month_year=month_year, prev_month_year=prev_month_year
            )
        except CommandeSyntheseImportError as e:
            return Response({"detail": str(e)}, status=400)
        except Exception as e:
            logger.exception("Échec import Synthèse Commande depuis %s", f.name)
            return Response({"detail": f"Erreur lors de la lecture du fichier : {e}"}, status=400)

        return Response({"month_year": resolved_month_year, "rows_imported": rows_imported, "filename": f.name})
