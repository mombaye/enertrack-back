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
    fichier tel quel dans data_imports/ (traçabilité) puis on en extrait :
      - la feuille "Synthèse Commande" (import brut, voir
        commande_synthese_import.py) ;
      - la feuille "Suivis commande" (import brut des colonnes mises en
        bleu dans le fichier source uniquement, voir
        suivi_commande_import.py), pour le même mois.
    Si la 2e feuille échoue (absente/renommée un mois donné), on ne bloque
    pas l'import de la Synthèse Commande — l'échec est juste signalé dans
    la réponse.
    """
    parser_classes = [MultiPartParser]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import re
        from pathlib import Path

        from fuel_tracking.services.commande_synthese_import import (
            SHEET_NAME as COMMANDE_SYNTHESE_SHEET_NAME,
            CommandeSyntheseImportError,
            import_commande_synthese_file,
            validate_month_year,
        )
        from fuel_tracking.services.suivi_commande_import import (
            SHEET_NAME as SUIVI_COMMANDE_SHEET_NAME,
            SuiviCommandeImportError,
            import_suivi_commande_file,
        )
        from fuel_tracking.services.xlsb_utils import read_workbook_grids

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

        # Le classeur est lu une seule fois pour les deux feuilles (au lieu de le
        # rouvrir/redécompresser une fois par import) — un .xlsb de plusieurs Mo
        # coûte déjà cher en upload, inutile de doubler le temps de lecture serveur.
        try:
            grids = read_workbook_grids(
                str(dest_path), [COMMANDE_SYNTHESE_SHEET_NAME, SUIVI_COMMANDE_SHEET_NAME]
            )
        except Exception as e:
            logger.exception("Échec lecture du classeur depuis %s", f.name)
            return Response({"detail": f"Erreur lors de la lecture du fichier : {e}"}, status=400)

        try:
            rows_imported, resolved_month_year = import_commande_synthese_file(
                str(dest_path), month_year=month_year, prev_month_year=prev_month_year,
                grid=grids.get(COMMANDE_SYNTHESE_SHEET_NAME),
            )
        except CommandeSyntheseImportError as e:
            return Response({"detail": str(e)}, status=400)
        except Exception as e:
            logger.exception("Échec import Synthèse Commande depuis %s", f.name)
            return Response({"detail": f"Erreur lors de la lecture du fichier : {e}"}, status=400)

        suivi_commande_rows_imported = None
        suivi_commande_error = None
        try:
            suivi_commande_rows_imported = import_suivi_commande_file(
                str(dest_path), month_year=resolved_month_year,
                grid=grids.get(SUIVI_COMMANDE_SHEET_NAME),
            )
        except SuiviCommandeImportError as e:
            suivi_commande_error = str(e)
        except Exception as e:
            logger.exception("Échec import Suivis commande depuis %s", f.name)
            suivi_commande_error = f"Erreur lors de la lecture de la feuille Suivis commande : {e}"

        return Response({
            "month_year": resolved_month_year,
            "rows_imported": rows_imported,
            "filename": f.name,
            "suivi_commande_rows_imported": suivi_commande_rows_imported,
            "suivi_commande_error": suivi_commande_error,
        })


class FuelCommandeSyntheseHistoryView(APIView):
    """
    GET /api/fuel-tracking/commande-synthese/history/

    Historique des imports — un import correspond à un mois (month_year), qui
    regroupe les lignes écrites dans FuelCommandeSynthese et/ou
    FuelSuiviCommandeSite lors du même upload (voir
    FuelCommandeSyntheseImportView). Un mois peut n'apparaître que dans l'une
    des deux tables si l'autre feuille a échoué à l'import (voir
    suivi_commande_error) — on liste quand même le mois dans ce cas.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count, Max

        from fuel_tracking.models import FuelCommandeSynthese, FuelSuiviCommandeSite

        cs_map = {
            g["month_year"]: g
            for g in FuelCommandeSynthese.objects.values("month_year").annotate(
                prev_month_year=Max("prev_month_year"),
                filename=Max("source_filename"),
                imported_at=Max("imported_at"),
                n=Count("id"),
            )
        }
        sv_map = {
            g["month_year"]: g
            for g in FuelSuiviCommandeSite.objects.values("month_year").annotate(
                filename=Max("source_filename"),
                imported_at=Max("imported_at"),
                n=Count("id"),
            )
        }

        results = []
        for month_year in sorted(set(cs_map) | set(sv_map), reverse=True):
            cs = cs_map.get(month_year)
            sv = sv_map.get(month_year)
            timestamps = [g["imported_at"] for g in (cs, sv) if g and g["imported_at"]]
            results.append({
                "month_year": month_year,
                "prev_month_year": cs["prev_month_year"] if cs else None,
                "filename": (cs["filename"] if cs else None) or (sv["filename"] if sv else None),
                "imported_at": max(timestamps) if timestamps else None,
                "rows_commande_synthese": cs["n"] if cs else 0,
                "rows_suivi_commande": sv["n"] if sv else 0,
            })

        return Response({"results": results})

    def delete(self, request):
        """
        DELETE /api/fuel-tracking/commande-synthese/history/?month=YYYY-MM

        Supprime toutes les données importées (Synthèse Commande + Suivis
        commande) pour le mois donné. Le fichier source archivé dans
        data_imports/ n'est pas supprimé (traçabilité).
        """
        from fuel_tracking.models import FuelCommandeSynthese, FuelSuiviCommandeSite
        from fuel_tracking.services.commande_synthese_import import (
            CommandeSyntheseImportError,
            validate_month_year,
        )

        month_year = request.query_params.get("month")
        try:
            validate_month_year(month_year, "Mois")
        except CommandeSyntheseImportError as e:
            return Response({"detail": str(e)}, status=400)

        cs_qs = FuelCommandeSynthese.objects.filter(month_year=month_year)
        sv_qs = FuelSuiviCommandeSite.objects.filter(month_year=month_year)
        cs_count = cs_qs.count()
        sv_count = sv_qs.count()

        if cs_count == 0 and sv_count == 0:
            return Response({"detail": f"Aucune donnée importée trouvée pour {month_year}."}, status=404)

        cs_qs.delete()
        sv_qs.delete()

        return Response({
            "month_year": month_year,
            "deleted_commande_synthese": cs_count,
            "deleted_suivi_commande": sv_count,
        })


class FuelSuiviCommandeListView(APIView):
    """
    GET /api/fuel-tracking/suivi-commande/?month=YYYY-MM&search=&page=&limit=

    Liste paginée du snapshot par site (FuelSuiviCommandeSite) — import brut
    des colonnes mises en bleu de la feuille "Suivis commande", sans
    recalcul. Sans mois demandé (ou si absent), retourne le mois le plus
    récent disponible. `search` filtre sur site_id/site_name (icontains).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import FuelSuiviCommandeSite

        available_months = list(
            FuelSuiviCommandeSite.objects.order_by("-month_year")
            .values_list("month_year", flat=True)
            .distinct()
        )

        month = request.query_params.get("month")
        if not month:
            if not available_months:
                return Response({"month_year": None, "data": [], "pagination": None, "available_months": [], "kpis": None})
            month = available_months[0]

        qs = FuelSuiviCommandeSite.objects.filter(month_year=month).order_by("site_id")

        search = (request.query_params.get("search") or "").strip()
        if search:
            from django.db.models import Q
            qs = qs.filter(Q(site_id__icontains=search) | Q(site_name__icontains=search))

        from django.db.models import Count, Sum
        agg = qs.aggregate(
            total_sites=Count("id"),
            total_conso_moy_jour_l=Sum("conso_moy_jour_l"),
            total_commande_sans_marge_l=Sum("commande_sans_marge_l"),
            total_commande_avec_marge_l=Sum("commande_avec_marge_l"),
            total_estimation_stock_final_l=Sum("estimation_stock_final_l"),
        )
        total = agg["total_sites"]
        kpis = {
            "total_sites": total,
            "total_conso_moy_jour_l": float(agg["total_conso_moy_jour_l"] or 0),
            "total_commande_sans_marge_l": float(agg["total_commande_sans_marge_l"] or 0),
            "total_commande_avec_marge_l": float(agg["total_commande_avec_marge_l"] or 0),
            "total_estimation_stock_final_l": float(agg["total_estimation_stock_final_l"] or 0),
        }

        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except ValueError:
            page = 1
        try:
            limit = min(200, max(1, int(request.query_params.get("limit", 50))))
        except ValueError:
            limit = 50

        start = (page - 1) * limit
        rows = qs[start:start + limit]

        def serialize(row):
            return {
                "site_id": row.site_id,
                "site_name": row.site_name,
                "typologie_contractuelle": row.typologie_contractuelle,
                "load_commande": float(row.load_commande),
                "indoor_outdoor": row.indoor_outdoor,
                "longitude": row.longitude,
                "batch": row.batch,
                "typologie_facturee": row.typologie_facturee,
                "conso_moy_jour_l": float(row.conso_moy_jour_l),
                "commande_sans_marge_l": float(row.commande_sans_marge_l),
                "commande_avec_marge_l": float(row.commande_avec_marge_l),
                "estimation_stock_final_l": float(row.estimation_stock_final_l),
                "typo_operations": row.typo_operations,
            }

        total_pages = max(1, (total + limit - 1) // limit)

        return Response({
            "month_year": month,
            "data": [serialize(r) for r in rows],
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "totalPages": total_pages,
                "hasNext": page < total_pages,
                "hasPrev": page > 1,
            },
            "available_months": available_months,
            "kpis": kpis,
        })
