# fuel_tracking/views.py
#
# Module suivi-carburant : automatisé, plus d'import manuel de fichier.
# 4 sous-parties construites étape par étape (Dashboard résumé, Consommation,
# Stock, Commande) — chacune alimentée par sa propre synchronisation
# (Snowflake/ENOC pour l'instant, voir fuel_tracking/services/). Les anciens
# modèles liés à l'import manuel (FuelCommandeSynthese, FuelSuiviCommandeSite)
# restent en base pour l'historique mais ne sont plus exposés via l'API.

import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


def _file_site_ids():
    """
    Site_id des sites présents dans les fichiers de référence partagés
    (Base GE.xlsx / Base août 26 validée, superset/sous-ensemble l'un de
    l'autre — voir import_base_ge.py). fichier_source n'est écrit QUE par
    import_base_ge, sur le(s) mois où la commande a été lancée — interroger
    sur TOUT l'historique (pas juste le mois demandé) rend le périmètre de
    sites indépendant du mois filtré, sinon changer de mois dans l'UI
    renverrait 0 site pour tout mois où l'import fichier n'a pas tourné.
    Utilisé à la fois par FuelConsommationListView et
    FuelConsommationDashboardView pour qu'ils partagent EXACTEMENT le même
    périmètre de sites.
    """
    from fuel_tracking.models import FuelConsommationMonthly

    return list(
        FuelConsommationMonthly.objects.filter(fichier_source__isnull=False)
        .values_list("site_id", flat=True)
        .distinct()
    )


def _file_ge_site_ids():
    """
    Site_id des sites du fichier de référence (Base GE.xlsx) dont la colonne
    "Typo simple" mentionne GE (ex: "SOLAIRE+GE", "SECTEUR+SOLAIRE+GE",
    "SECTEUR+GE") — demande explicite (2026-08) : ne plus considérer TOUS
    les sites du fichier comme des sites GE par défaut, se fier au contenu
    réel de Typo simple. Un site du fichier dont Typo simple ne mentionne
    PAS GE compterait Sans GE malgré sa présence dans le fichier (aucun cas
    de ce type sur les 469 lignes actuelles — vérifié 2026-08 — mais la
    règle reste celle-ci, pas "présent dans le fichier = GE").
    Découpage sur "+" (pas un simple "GE" in typo) pour éviter un faux
    positif sur un futur libellé qui contiendrait la sous-chaîne "GE" sans
    que ce soit le token GE lui-même.
    """
    from fuel_tracking.models import FuelConsommationMonthly

    rows = (
        FuelConsommationMonthly.objects.filter(fichier_source__isnull=False, typo_simple_fichier__isnull=False)
        .values_list("site_id", "typo_simple_fichier")
        .distinct()
    )
    return {
        site_id for site_id, typo in rows
        if "GE" in [p.strip() for p in typo.split("+")]
    }


def _compute_ge_detection(month):
    """
    Recoupement Snowflake/ENOC/fichiers pour un mois donné — factorisé pour
    être partagé entre FuelConsommationListView (panneau interactif,
    cliquable) et FuelConsommationDashboardView (même panneau, lecture
    seule, voir DashboardSheet). Toujours calculé sur TOUT le mois, avant
    tout filtre search/country/has_genset.
    """
    from django.db.models import Count, Q

    from fuel_tracking.models import FuelConsommationMonthly

    file_site_ids = set(_file_site_ids())
    detection_base = FuelConsommationMonthly.objects.filter(month_year=month)
    detection_agg = detection_base.aggregate(
        total_sites=Count("id"),
        avec_ge=Count("id", filter=Q(has_genset=True)),
        sans_ge=Count("id", filter=Q(has_genset=False)),
        avec_ge_snowflake=Count("id", filter=Q(has_genset_snowflake=True)),
        avec_ge_enoc=Count("id", filter=Q(has_genset_enoc=True)),
        vus_seulement_enoc=Count("id", filter=Q(has_genset_enoc=True, has_genset_snowflake=False)),
        vus_seulement_snowflake=Count("id", filter=Q(has_genset_snowflake=True, has_genset_enoc=False)),
        vus_par_les_deux=Count("id", filter=Q(has_genset_snowflake=True, has_genset_enoc=True)),
    )
    ge_site_ids = set(detection_base.filter(has_genset=True).values_list("site_id", flat=True))
    return {
        **detection_agg,
        "sites_dans_fichier": len(file_site_ids),
        "dans_fichier_et_ge": len(file_site_ids & ge_site_ids),
        "dans_fichier_sans_ge": len(file_site_ids - ge_site_ids),
        "ge_hors_fichier": len(ge_site_ids - file_site_ids),
    }


def _effective_ge_q(file_ge_site_ids):
    """
    Condition "site avec GE" à utiliser pour tout ce qui touche à
    Suivis Consommation / Dashboard. Un site compte Avec GE si Snowflake OU
    ENOC le confirme (has_genset), OU si sa Typo simple (Base GE.xlsx)
    mentionne GE (voir _file_ge_site_ids) — même si Snowflake (DG_COUNT=0)
    et ENOC n'ont ni l'un ni l'autre de fiche GE pour lui (45 sites dans ce
    cas, vérifié 2026-08). has_genset (Snowflake OU ENOC) reste affiché tel
    quel ailleurs (ge_detection) pour la transparence, mais ne sert plus
    seul à décider Avec GE / Sans GE.
    """
    from django.db.models import Q

    return Q(has_genset=True) | Q(site_id__in=file_ge_site_ids)


class FuelConsommationListView(APIView):
    """
    GET /api/fuel-tracking/consommation/?month=YYYY-MM&search=&country=&has_genset=&page=&limit=

    Liste paginée de la Consommation carburant mensuelle par site
    (FuelConsommationMonthly) — automatisée (Snowflake + ENOC), voir
    sync_fuel_consommation. Sans mois demandé, retourne le mois le plus
    récent disponible. `search` filtre sur site_id/site_name (icontains).
    `has_genset=true|false` filtre les sites avec/sans groupe électrogène
    (seuls ceux avec GE peuvent avoir une conso fuel) ; omis, retourne tous
    les sites du pays.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count, Q, Sum

        from django.utils import timezone

        from fuel_tracking.models import (
            FuelCphGeParameter,
            FuelConsommationMonthly,
            FuelConsommationSyncRun,
            FuelEnocSyncRun,
        )

        # Statut du fichier de référence CPH (FuelCphGeParameter) — pas une
        # connectivité live comme Snowflake/ENOC, mais l'UI a besoin de savoir
        # si le fichier a été chargé pour expliquer pourquoi conso_estimee_cph_l
        # est vide partout (MISSING_PARAMETER) tant qu'il ne l'est pas.
        today = timezone.now().date()
        cph_params_qs = FuelCphGeParameter.objects.all()
        cph_parameters = {
            "sites_configures": cph_params_qs.filter(
                valid_from__lte=today
            ).exclude(valid_to__lt=today).values("site_id").distinct().count(),
            "dernier_import": cph_params_qs.order_by("-updated_at").values_list("updated_at", flat=True).first(),
        }

        available_months = list(
            FuelConsommationMonthly.objects.order_by("-month_year")
            .values_list("month_year", flat=True)
            .distinct()
        )

        last_sf_run = FuelConsommationSyncRun.objects.order_by("-started_at").first()
        last_enoc_run = FuelEnocSyncRun.objects.order_by("-started_at").first()
        sources = {
            "snowflake": {
                "connected": bool(last_sf_run and last_sf_run.status == FuelConsommationSyncRun.Status.SUCCESS),
                "last_status": last_sf_run.status if last_sf_run else None,
                "last_run_at": (last_sf_run.finished_at or last_sf_run.started_at) if last_sf_run else None,
                "error": last_sf_run.error_message if last_sf_run else None,
            },
            "enoc": {
                "connected": bool(last_enoc_run and last_enoc_run.status == FuelEnocSyncRun.Status.SUCCESS and last_enoc_run.rows_fetched > 0),
                "last_status": last_enoc_run.status if last_enoc_run else None,
                "last_run_at": (last_enoc_run.finished_at or last_enoc_run.started_at) if last_enoc_run else None,
                "error": last_enoc_run.error_message if last_enoc_run else None,
            },
        }

        month = request.query_params.get("month")
        if not month:
            if not available_months:
                return Response({"month_year": None, "data": [], "pagination": None, "available_months": [], "kpis": None, "sources": sources, "cph_parameters": cph_parameters, "ge_detection": None})
            month = available_months[0]

        # Suivis Consommation (2026-08) — périmètre à nouveau TOUT le réseau
        # (retour arrière explicite sur la restriction "sites du fichier
        # uniquement" : 59 sites reconnus GE par Snowflake/ENOC étaient
        # exclus car absents des fichiers, demande "on veut toutes les
        # données selon le filtre"). Les filtres Avec GE/Sans GE/Avec GE mais
        # pas de données portent donc à nouveau sur le plein effectif — voir
        # `ge_detection` plus bas pour le détail Snowflake/ENOC/fichier.
        qs = FuelConsommationMonthly.objects.filter(month_year=month).order_by("site_id")

        # Recoupement Snowflake/ENOC/fichiers — voir _compute_ge_detection,
        # partagé avec FuelConsommationDashboardView pour que les 2 panneaux
        # (interactif ici, lecture seule sur le Dashboard) affichent
        # exactement les mêmes chiffres.
        file_site_ids = set(_file_site_ids())
        file_ge_site_ids = _file_ge_site_ids()
        ge_detection = _compute_ge_detection(month)

        # Filtre "détection" — clic sur une case du panneau GeDetectionPanel
        # (2026-08) : affiche directement dans le tableau les sites qui
        # composent ce chiffre, plutôt que de laisser deviner. Mêmes clés
        # que ge_detection ci-dessus (sauf total_sites = pas de filtre).
        # Exclusif du filtre has_genset (Tous/Avec GE/Sans GE/Avec GE mais
        # aucune donnée) — les 2 filtres ne sont jamais combinés.
        detection_filters = {
            "avec_ge": Q(has_genset=True),
            "sans_ge": Q(has_genset=False),
            "avec_ge_snowflake": Q(has_genset_snowflake=True),
            "avec_ge_enoc": Q(has_genset_enoc=True),
            "vus_seulement_enoc": Q(has_genset_enoc=True, has_genset_snowflake=False),
            "vus_seulement_snowflake": Q(has_genset_snowflake=True, has_genset_enoc=False),
            "vus_par_les_deux": Q(has_genset_snowflake=True, has_genset_enoc=True),
            "sites_dans_fichier": Q(site_id__in=file_site_ids),
            "dans_fichier_et_ge": Q(site_id__in=file_site_ids, has_genset=True),
            "dans_fichier_sans_ge": Q(site_id__in=file_site_ids, has_genset=False),
            "ge_hors_fichier": Q(has_genset=True) & ~Q(site_id__in=file_site_ids),
        }
        detection_param = (request.query_params.get("detection") or "").strip()
        if detection_param in detection_filters:
            qs = qs.filter(detection_filters[detection_param])

        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(site_id__icontains=search) | Q(site_name__icontains=search))

        country = (request.query_params.get("country") or "").strip()
        if country:
            qs = qs.filter(country=country)

        # Répartition avec/sans GE calculée AVANT le filtre has_genset lui-même,
        # pour que le sélecteur du frontend puisse toujours afficher les 2
        # effectifs (ex: "Avec GE (373)" / "Sans GE (2949)"), qu'un filtre soit
        # actif ou non. "Avec GE" utilise effective_ge_q (Snowflake/ENOC OU
        # Typo simple du fichier Base GE.xlsx mentionne GE — voir
        # _effective_ge_q/_file_ge_site_ids). "Avec GE mais aucune donnée" =
        # avec GE ET Conso estimée ET Conso mesurée vue (Snowflake OU
        # gardiennage, voir conso_mesuree_source de serialize()) sont TOUTES
        # LES DEUX manquantes — demande explicite (2026-08 : "cela doit
        # prendre parmi les GE ceux qui n'ont pas de données de Conso
        # estimée (L) / Conso mesurée vue (L)"). Running Time n'entre plus
        # dans ce critère. Running Time/Conso estimée viennent
        # EXCLUSIVEMENT du pipeline CPH Snowflake — plus de repli sur Base
        # août 26 validée, qui n'est plus utilisée par ce tableau.
        effective_ge_q = _effective_ge_q(file_ge_site_ids)
        incomplete_q = effective_ge_q & (
            Q(conso_estimee_cph_l__isnull=True) & Q(conso_snowflake_l__isnull=True) & Q(conso_gardien_l__isnull=True)
        )
        ge_counts = qs.aggregate(
            sites_avec_ge=Count("id", filter=effective_ge_q),
            sites_sans_ge=Count("id", filter=~effective_ge_q),
            sites_ge_enoc_only=Count("id", filter=Q(has_genset_enoc=True, has_genset_snowflake=False)),
            sites_avec_ge_incomplet=Count("id", filter=incomplete_q),
        )

        has_genset_param = (request.query_params.get("has_genset") or "").strip().lower()
        if has_genset_param in ("true", "1"):
            qs = qs.filter(effective_ge_q)
        elif has_genset_param in ("false", "0"):
            qs = qs.filter(~effective_ge_q)
        elif has_genset_param == "incomplete":
            qs = qs.filter(incomplete_q)

        agg = qs.aggregate(
            total_sites=Count("id"),
            sites_avec_conso=Count("id", filter=Q(conso_snowflake_l__isnull=False)),
            sites_avec_estimation=Count("id", filter=Q(conso_estimee_snowflake_l__isnull=False) | Q(conso_estimee_enoc_l__isnull=False)),
            total_conso_snowflake_l=Sum("conso_snowflake_l"),
            total_enoc_qte_ajoutee_l=Sum("enoc_qte_ajoutee_l"),
            total_enoc_nb_demandes=Sum("enoc_nb_demandes"),
        )
        kpis = {
            "total_sites": agg["total_sites"],
            "sites_avec_ge": ge_counts["sites_avec_ge"],
            "sites_sans_ge": ge_counts["sites_sans_ge"],
            "sites_ge_enoc_only": ge_counts["sites_ge_enoc_only"],
            "sites_avec_ge_incomplet": ge_counts["sites_avec_ge_incomplet"],
            "sites_avec_conso": agg["sites_avec_conso"],
            "sites_avec_estimation": agg["sites_avec_estimation"],
            "total_conso_snowflake_l": float(agg["total_conso_snowflake_l"] or 0),
            "total_enoc_qte_ajoutee_l": float(agg["total_enoc_qte_ajoutee_l"] or 0),
            "total_enoc_nb_demandes": agg["total_enoc_nb_demandes"] or 0,
        }

        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except ValueError:
            page = 1
        try:
            limit = min(200, max(1, int(request.query_params.get("limit", 50))))
        except ValueError:
            limit = 50

        total = agg["total_sites"]
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        rows = qs[start:start + limit]

        def serialize(row):
            # Suivis Consommation (2026-08) — demande explicite : "toute la
            # base fixe est prise du fichier Base GE" (identité/typologie
            # ci-dessous) ; "pour les autres [colonnes calculées], respecter
            # les informations de Snowflake" — Running Time et Conso estimée
            # viennent EXCLUSIVEMENT du pipeline CPH Snowflake (télémétrie
            # GFMS_DATA_TRACKER_NC), plus de repli sur Base août 26 validée.
            # Base GE.xlsx colonnes X/Y (Running Time/Conso estimée propres
            # au fichier) restent ignorées ici : renseignées pour 5 des 469
            # lignes seulement, motif manifestement factice (2,3,4,5,6h).
            runtime_h = row.cph_runtime_h_total
            runtime_source = f"snowflake_{(row.cph_runtime_source or '').lower()}" if runtime_h is not None else None

            conso_estimee = row.conso_estimee_cph_l
            estimee_source = "cph_snowflake" if conso_estimee is not None else None

            # Conso mesurée vue : Snowflake (capteur automatisé, priorité 1)
            # > relevé de gardiennage (jauge physique relevée manuellement,
            # priorité 2) — pour les sites sans capteur Snowflake fiable
            # (demande explicite 2026-08 : "pouvons-nous prendre les valeurs
            # de ce fichier pour les sites avec aucune supervision"). Fichier
            # importé mois par mois (import_gardien_conso) : vide sur les
            # mois où aucun fichier de gardiennage n'a encore été fourni.
            if row.conso_snowflake_l is not None:
                conso_mesuree, mesuree_source = row.conso_snowflake_l, "snowflake"
            elif row.conso_gardien_l is not None:
                conso_mesuree, mesuree_source = row.conso_gardien_l, "gardiennage"
            else:
                conso_mesuree, mesuree_source = None, None

            ecart_l = None
            ecart_pct = None
            if conso_estimee is not None and conso_mesuree is not None:
                ecart_l = float(conso_mesuree - conso_estimee)
                if conso_mesuree:
                    ecart_pct = round(ecart_l / float(conso_mesuree) * 100, 2)

            # Commentaire — explique en clair, pour les valeurs manquantes de
            # CETTE ligne, pourquoi (aucune des sources disponibles ne l'a
            # fournie), plutôt que de laisser deviner depuis une cellule
            # vide. Rien à dire quand tout est renseigné.
            comment_parts = []
            if runtime_h is None:
                comment_parts.append("Running Time : non déduit par le pipeline CPH Snowflake ce mois-ci (ni compteur télémétrie 5 min, ni contrôleur DSE, ni DG-On calculé).")
            if conso_estimee is None:
                comment_parts.append("Conso estimée : pipeline CPH Snowflake sans résultat ce mois-ci (voir cph_calculation_status).")
            if conso_mesuree is None:
                comment_parts.append("Conso mesurée vue : aucune baisse de niveau de cuve fiable détectée par Snowflake (VW_FUEL_REPORT), et aucun relevé de gardiennage disponible pour ce site ce mois-ci.")
            commentaire = " ".join(comment_parts) or None

            # Demande explicite (2026-08) : pour les sites du fichier, se
            # fier à Typo simple ("SOLAIRE+GE", etc.) plutôt qu'à la seule
            # présence dans le fichier. has_genset_snowflake/_enoc restent
            # les valeurs brutes (transparence, voir ge_detection), mais le
            # has_genset AFFICHÉ inclut aussi les sites dont Typo simple
            # mentionne GE — 45 sites du fichier n'ont ni DG_COUNT>0 ni fiche
            # ENOC, et comptent quand même Avec GE ici (leur Typo simple
            # mentionne bien GE).
            has_genset_effective = bool(row.has_genset) or row.site_id in file_ge_site_ids

            return {
                "site_id": row.site_id,
                "site_name": row.site_name,
                "typology": row.typology_fichier or row.typology,
                "typologie_simple": row.typo_simple_fichier,
                "site_type": row.site_type_fichier or row.site_type,
                "type_ge": row.type_ge_fichier or row.cph_ge_type,
                "dg_count": row.dg_count,
                "power_supply": row.power_supply,
                "has_genset": has_genset_effective,
                "has_genset_snowflake": row.has_genset_snowflake,
                "has_genset_enoc": row.has_genset_enoc,
                "nb_ge_enoc": row.nb_ge_enoc,
                "conso_snowflake_l": float(row.conso_snowflake_l) if row.conso_snowflake_l is not None else None,
                "nb_jours_data": row.nb_jours_data,
                "conso_estimee_snowflake_l": float(row.conso_estimee_snowflake_l) if row.conso_estimee_snowflake_l is not None else None,
                "conso_estimee_snowflake_nb_releves": row.conso_estimee_snowflake_nb_releves,
                "conso_estimee_enoc_l": float(row.conso_estimee_enoc_l) if row.conso_estimee_enoc_l is not None else None,
                "conso_estimee_nb_releves": row.conso_estimee_nb_releves,
                "conso_specifique_moy_l_kwh": float(row.conso_specifique_moy_l_kwh) if row.conso_specifique_moy_l_kwh is not None else None,
                "ge_prod_kwh": float(row.ge_prod_kwh) if row.ge_prod_kwh is not None else None,
                "sensor_status": row.sensor_status,
                # Colonnes qualité VW_FUEL_REPORT — audit (spec 2026-08).
                "quality_status": row.quality_status,
                "raw_point_count": row.raw_point_count,
                "valid_point_count": row.valid_point_count,
                "isolated_spike_count": row.isolated_spike_count,
                "over_capacity_point_count": row.over_capacity_point_count,
                "refill_detected": row.refill_detected,
                "estimated_refill_volume_l": float(row.estimated_refill_volume_l) if row.estimated_refill_volume_l is not None else None,
                "enoc_qte_demandee_l": float(row.enoc_qte_demandee_l),
                "enoc_qte_validee_l": float(row.enoc_qte_validee_l),
                "enoc_qte_ajoutee_l": float(row.enoc_qte_ajoutee_l),
                "enoc_nb_demandes": row.enoc_nb_demandes,
                "ecart_conso_vs_enoc_l": float(row.ecart_conso_vs_enoc_l) if row.ecart_conso_vs_enoc_l is not None else None,
                # Estimation CPH (télémétrie GFMS_DATA_TRACKER_NC) — troisième
                # source, indépendante des 2 ci-dessus, pour les GE sans
                # capteur de cuve fiable. Voir sync_fuel_cph/FuelCphGeDaily.
                "conso_estimee_cph_l": float(row.conso_estimee_cph_l) if row.conso_estimee_cph_l is not None else None,
                "cph_l_per_h_moy": float(row.cph_l_per_h_moy) if row.cph_l_per_h_moy is not None else None,
                "cph_nb_jours_ok": row.cph_nb_jours_ok,
                "cph_nb_jours_calcules": row.cph_nb_jours_calcules,
                "cph_calculation_status": row.cph_calculation_status,
                "cph_status_breakdown": row.cph_status_breakdown,
                "cph_runtime_h_total": float(row.cph_runtime_h_total) if row.cph_runtime_h_total is not None else None,
                "cph_runtime_source": row.cph_runtime_source,
                "cph_ge_type": row.cph_ge_type,
                "cph_pge_kva": float(row.cph_pge_kva) if row.cph_pge_kva is not None else None,
                "cph_power_factor": float(row.cph_power_factor) if row.cph_power_factor is not None else None,
                "cph_spc_l_per_kwh": float(row.cph_spc_l_per_kwh) if row.cph_spc_l_per_kwh is not None else None,
                # 4e source — fichier métier validé (Base GE.xlsx), jamais
                # fusionnée avec conso_snowflake_l/conso_estimee_cph_l.
                "conso_fichier_l": float(row.conso_fichier_l) if row.conso_fichier_l is not None else None,
                "fichier_source": row.fichier_source,
                # Colonnes Suivis Consommation affichées (2026-08) — sourcées
                # de Base GE.xlsx pour tout ce que le fichier fournit ;
                # Énergie site/Batterie DC/Batterie AC/Énergie GE viennent du
                # pipeline CPH Snowflake (FuelCphGeDaily agrégé), seule source
                # pour ces 4 métriques, absentes du fichier.
                "pge_kva_fichier": float(row.pge_kva_fichier) if row.pge_kva_fichier is not None else None,
                "ge_load_pct_fichier": float(row.ge_load_pct_fichier) if row.ge_load_pct_fichier is not None else None,
                "cph_lph_fichier": float(row.cph_lph_fichier) if row.cph_lph_fichier is not None else None,
                # Valeurs résolues (priorité fichier(s) > Snowflake, voir
                # commentaire en tête de serialize()) — jamais les colonnes
                # brutes Base GE.xlsx X/Y (quasi vides, voir help_text du
                # modèle).
                "ge_runtime_fichier_h": float(runtime_h) if runtime_h is not None else None,
                "ge_runtime_source": runtime_source,
                "conso_estimee_fichier_l": float(conso_estimee) if conso_estimee is not None else None,
                "conso_estimee_source": estimee_source,
                "conso_mesuree_fichier_l": float(conso_mesuree) if conso_mesuree is not None else None,
                "conso_mesuree_source": mesuree_source,
                "gardien_statut": row.gardien_statut,
                "ecart_fichier_l": ecart_l,
                "ecart_fichier_pct": ecart_pct,
                "cph_site_load_energy_kwh": float(row.cph_site_load_energy_kwh) if row.cph_site_load_energy_kwh is not None else None,
                "cph_battery_dc_energy_kwh": float(row.cph_battery_dc_energy_kwh) if row.cph_battery_dc_energy_kwh is not None else None,
                "cph_battery_ac_energy_kwh": float(row.cph_battery_ac_energy_kwh) if row.cph_battery_ac_energy_kwh is not None else None,
                "cph_total_ge_energy_kwh": float(row.cph_total_ge_energy_kwh) if row.cph_total_ge_energy_kwh is not None else None,
                "commentaire": commentaire,
            }

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
            "sources": sources,
            "kpis": kpis,
            "cph_parameters": cph_parameters,
            "ge_detection": ge_detection,
        })


class FuelConsommationDashboardView(APIView):
    """
    GET /api/fuel-tracking/consommation/dashboard/?month=YYYY-MM&from_month=YYYY-MM&to_month=YYYY-MM

    Alimente l'onglet Dashboard. Périmètre IDENTIQUE à Suivis Consommation
    (FuelConsommationListView) : _effective_ge_q (Snowflake/ENOC OU site du
    fichier Base GE.xlsx) — sinon les 2 onglets afficheraient des totaux
    décalés (demande explicite : "les données de la partie Dashboard
    doivent être les mêmes que sur Suivis Consommations"). Le
    total_conso_estimee_cph_l ci-dessous vient exclusivement du pipeline CPH
    Snowflake, comme la colonne "Conso estimée" de la liste, pour que les 2
    totaux concordent.

    Portée de `months`/`monthly`/`top_sites` (priorité dans cet ordre,
    spec 2026-08) :
      1. `from_month` + `to_month` fournis ensemble → cette plage exacte.
      2. sinon `month` seul (même sélecteur que l'onglet Suivis
         Consommations) fourni et explicitement choisi par l'utilisateur
         → ce seul mois.
      3. sinon (aucun paramètre) → les 3 derniers mois disponibles par
         défaut (comparatif), jamais tout l'historique.
    """
    permission_classes = [IsAuthenticated]

    @staticmethod
    def _month_stats(qs, month_year, file_ge_site_ids):
        from django.db.models import Count, Q, Sum

        # Conso estimée / Running Time viennent EXCLUSIVEMENT du pipeline CPH
        # Snowflake (demande explicite 2026-08 : "pour les autres [colonnes
        # calculées], respecter les informations de Snowflake") — même
        # champs que FuelConsommationListView.serialize(), plus de repli sur
        # Base août 26 validée, pour que le total ici corresponde
        # exactement à la somme de la colonne "Conso estimée" de Suivis
        # Consommation sur le même mois. nb_sites_incomplet utilise
        # EXACTEMENT la même définition que le filtre "Avec GE mais aucune
        # donnée" de FuelConsommationListView (incomplete_q) : Conso estimée
        # ET Conso mesurée vue (Snowflake OU gardiennage) toutes les deux
        # manquantes — Running Time n'entre plus dans ce critère.
        incomplete_q = _effective_ge_q(file_ge_site_ids) & (
            Q(conso_estimee_cph_l__isnull=True) & Q(conso_snowflake_l__isnull=True) & Q(conso_gardien_l__isnull=True)
        )

        agg = qs.aggregate(
            nb_sites_ge=Count("id"),
            nb_sites_avec_conso=Count("id", filter=Q(conso_snowflake_l__isnull=False)),
            nb_sites_monitored=Count("id", filter=Q(sensor_status="MONITORED")),
            total_conso_snowflake_l=Sum("conso_snowflake_l"),
            total_enoc_qte_ajoutee_l=Sum("enoc_qte_ajoutee_l"),
            total_enoc_nb_demandes=Sum("enoc_nb_demandes"),
            nb_sites_enoc_ajoutee=Count("id", filter=Q(enoc_qte_ajoutee_l__gt=0)),
            # Ratio pondéré (pas une moyenne de ratios) — seulement les lignes
            # où les 2 valeurs existent, même principe que le calcul par site
            # (cf. fuel_consommation_snowflake.py).
            specif_num=Sum("conso_snowflake_l", filter=Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)),
            specif_den=Sum("ge_prod_kwh", filter=Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)),
            total_conso_estimee_cph_l=Sum("conso_estimee_cph_l"),
            nb_sites_avec_cph=Count("id", filter=Q(conso_estimee_cph_l__isnull=False)),
            nb_sites_incomplet=Count("id", filter=incomplete_q),
        )
        specif_num = agg["specif_num"]
        specif_den = agg["specif_den"]
        conso_specifique = (
            float(specif_num) / float(specif_den)
            if specif_num is not None and specif_den not in (None, 0)
            else None
        )
        return {
            "month_year": month_year,
            "nb_sites_ge": agg["nb_sites_ge"],
            "nb_sites_avec_conso": agg["nb_sites_avec_conso"],
            "nb_sites_monitored": agg["nb_sites_monitored"],
            "total_conso_snowflake_l": float(agg["total_conso_snowflake_l"] or 0),
            "total_enoc_qte_ajoutee_l": float(agg["total_enoc_qte_ajoutee_l"] or 0),
            "total_enoc_nb_demandes": agg["total_enoc_nb_demandes"] or 0,
            "nb_sites_enoc_ajoutee": agg["nb_sites_enoc_ajoutee"],
            "conso_specifique_moy_l_kwh": conso_specifique,
            "total_conso_estimee_cph_l": float(agg["total_conso_estimee_cph_l"] or 0),
            "nb_sites_avec_cph": agg["nb_sites_avec_cph"],
            "nb_sites_incomplet": agg["nb_sites_incomplet"],
        }

    @staticmethod
    def _top_sites(qs, limit=10):
        from django.db.models import Count, Sum

        rows = (
            qs.exclude(conso_snowflake_l__isnull=True)
            .values("site_id", "site_name")
            .annotate(
                total_conso_l=Sum("conso_snowflake_l"),
                nb_mois_avec_conso=Count("id"),
            )
            .order_by("-total_conso_l")[:limit]
        )
        return [
            {
                "site_id": r["site_id"],
                "site_name": r["site_name"],
                "total_conso_l": float(r["total_conso_l"] or 0),
                "nb_mois_avec_conso": r["nb_mois_avec_conso"],
            }
            for r in rows
        ]

    def get(self, request):
        from django.utils import timezone

        from fuel_tracking.models import FuelCphGeParameter, FuelConsommationMonthly

        # effective_ge_q (Snowflake/ENOC OU Typo simple du fichier Base
        # GE.xlsx mentionne GE, voir _effective_ge_q/_file_ge_site_ids) —
        # même périmètre que FuelConsommationListView pour que les 2 onglets
        # affichent des totaux identiques. Pas le réseau entier (3 356
        # sites) : les sites sans GE n'ont structurellement aucune conso
        # fuel possible, ça diluerait le sens de cette page.
        file_ge_site_ids = _file_ge_site_ids()
        base_qs = FuelConsommationMonthly.objects.filter(_effective_ge_q(file_ge_site_ids))
        all_months = list(
            base_qs.order_by("month_year").values_list("month_year", flat=True).distinct()
        )
        total_ge_sites = base_qs.filter(month_year=all_months[-1]).count() if all_months else 0

        # Statut du fichier de référence CPH — même logique que
        # FuelConsommationListView, pour que le dashboard explique
        # immédiatement pourquoi la conso CPH est vide (ou pas).
        today = timezone.now().date()
        cph_params_qs = FuelCphGeParameter.objects.all()
        cph_parameters = {
            "sites_configures": cph_params_qs.filter(
                valid_from__lte=today
            ).exclude(valid_to__lt=today).values("site_id").distinct().count(),
            "dernier_import": cph_params_qs.order_by("-updated_at").values_list("updated_at", flat=True).first(),
        }

        requested_month = (request.query_params.get("month") or "").strip()
        from_month = (request.query_params.get("from_month") or "").strip()
        to_month = (request.query_params.get("to_month") or "").strip()

        if from_month and to_month:
            trend_months = [m for m in all_months if from_month <= m <= to_month]
        elif requested_month:
            trend_months = [m for m in all_months if m == requested_month]
        else:
            trend_months = all_months[-3:]

        trend_qs = base_qs.filter(month_year__in=trend_months)

        monthly = [
            self._month_stats(trend_qs.filter(month_year=my), my, file_ge_site_ids)
            for my in trend_months
        ]
        top_sites = self._top_sites(trend_qs)

        # Même panneau "Détection GE" que Suivis Consommation (voir
        # _compute_ge_detection), en lecture seule ici — scope sur le
        # DERNIER mois de la plage affichée, comme le camembert "Couverture
        # des sites" (lastStats côté frontend).
        ge_detection = _compute_ge_detection(trend_months[-1]) if trend_months else None

        return Response({
            "months": trend_months,
            "monthly": monthly,
            "top_sites": top_sites,
            "total_ge_sites": total_ge_sites,
            "available_months": all_months,
            "cph_parameters": cph_parameters,
            "ge_detection": ge_detection,
        })


class FuelCommandeView(APIView):
    """
    GET /api/fuel-tracking/commandes/?month=YYYY-MM&search=&page=&limit=

    Commande carburant mensuelle — import mensuel brut (pas de synchro
    automatisée : commande décidée par l'équipe Ops dans un fichier Excel,
    voir import_commande_fuel), lecture seule ici, aucun upload sur cette
    page. Combine :
      - FuelCommandeSynthese : 2 blocs de synthèse (par catégorie/batch, par
        typologie facturée), mois courant vs précédent + écart déjà
        calculés dans le fichier source.
      - FuelSuiviCommandeSite : détail par site (conso moyenne, commande
        sans/avec marge, stock final estimé).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count, Q, Sum

        from fuel_tracking.models import FuelCommandeSynthese, FuelSuiviCommandeSite

        available_months = list(
            FuelSuiviCommandeSite.objects.order_by("-month_year").values_list("month_year", flat=True).distinct()
        )

        month = (request.query_params.get("month") or "").strip()
        if not month:
            if not available_months:
                return Response({
                    "month_year": None, "available_months": [], "synthese": {"categorie": [], "typologie": []},
                    "sites": {"data": [], "pagination": None, "kpis": None},
                })
            month = available_months[0]

        def serialize_synthese(row):
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

        synth_qs = FuelCommandeSynthese.objects.filter(month_year=month).order_by("group_type", "order_index")
        prev_month_year = synth_qs.values_list("prev_month_year", flat=True).first()
        synthese = {
            "categorie": [serialize_synthese(r) for r in synth_qs.filter(group_type=FuelCommandeSynthese.GroupType.CATEGORIE)],
            "typologie": [serialize_synthese(r) for r in synth_qs.filter(group_type=FuelCommandeSynthese.GroupType.TYPOLOGIE)],
        }

        sites_qs = FuelSuiviCommandeSite.objects.filter(month_year=month).order_by("site_id")

        search = (request.query_params.get("search") or "").strip()
        if search:
            sites_qs = sites_qs.filter(Q(site_id__icontains=search) | Q(site_name__icontains=search))

        # "Rupture de stock prévue" — estimation_stock_final_l < 0, calculée
        # AVANT le filtre search pour un KPI stable qu'une recherche soit
        # active ou non (même principe que ge_counts ailleurs dans ce fichier).
        kpis_qs = FuelSuiviCommandeSite.objects.filter(month_year=month)
        kpis = kpis_qs.aggregate(
            total_sites=Count("id"),
            total_commande_avec_marge_l=Sum("commande_avec_marge_l"),
            total_commande_sans_marge_l=Sum("commande_sans_marge_l"),
            nb_sites_commande_positive=Count("id", filter=Q(commande_avec_marge_l__gt=0)),
            nb_sites_stock_negatif=Count("id", filter=Q(estimation_stock_final_l__lt=0)),
        )
        kpis = {
            "total_sites": kpis["total_sites"],
            "total_commande_avec_marge_l": float(kpis["total_commande_avec_marge_l"] or 0),
            "total_commande_sans_marge_l": float(kpis["total_commande_sans_marge_l"] or 0),
            "nb_sites_commande_positive": kpis["nb_sites_commande_positive"],
            "nb_sites_stock_negatif": kpis["nb_sites_stock_negatif"],
        }

        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except ValueError:
            page = 1
        try:
            limit = min(200, max(1, int(request.query_params.get("limit", 50))))
        except ValueError:
            limit = 50

        total = sites_qs.count()
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        rows = sites_qs[start:start + limit]

        def serialize_site(row):
            return {
                "site_id": row.site_id,
                "site_name": row.site_name,
                "typologie_contractuelle": row.typologie_contractuelle,
                "load_commande": float(row.load_commande),
                "indoor_outdoor": row.indoor_outdoor,
                "batch": row.batch,
                "typologie_facturee": row.typologie_facturee,
                "typo_operations": row.typo_operations,
                "conso_moy_jour_l": float(row.conso_moy_jour_l),
                "commande_sans_marge_l": float(row.commande_sans_marge_l),
                "commande_avec_marge_l": float(row.commande_avec_marge_l),
                "estimation_stock_final_l": float(row.estimation_stock_final_l),
            }

        return Response({
            "month_year": month,
            "prev_month_year": prev_month_year,
            "available_months": available_months,
            "synthese": synthese,
            "sites": {
                "data": [serialize_site(r) for r in rows],
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total": total,
                    "totalPages": total_pages,
                    "hasNext": page < total_pages,
                    "hasPrev": page > 1,
                },
                "kpis": kpis,
            },
        })


class FuelCommandeEstimationView(APIView):
    """
    GET /api/fuel-tracking/commandes/estimation/?marge=0.15

    Estimation carburant du mois suivant — indépendante de l'import manuel
    Ops (FuelSuiviCommandeSite) : calculée à partir des données automatisées
    déjà en base (FuelConsommationMonthly, FuelStockSnapshot), pas d'un
    fichier Excel décidé à la main.

    Logique d'estimateur (pas juste une moyenne brute) :
      1. Conso/jour "meilleure valeur" par mois — même priorité que
         FuelConsommationListView.serialize() : mesurée (Snowflake, puis
         gardiennage) > estimée CPH (télémétrie) > estimée fichier "Base
         août 26". Jamais deux sources mélangées dans un même mois.
      2. Moyenne PONDÉRÉE sur les mois disponibles (le plus récent pèse le
         plus), calculée en L/JOUR (pas en L/mois) pour ne pas fausser la
         projection entre un mois de 28 et un mois de 31 jours.
      3. Niveau de confiance par site — un estimateur communique une
         incertitude, pas juste un chiffre : "Élevée" (≥2 mois de conso
         MESURÉE, variation raisonnable entre mois), "Moyenne" (1 mois
         mesuré, ou ≥2 mois mais volatils, ou repli CPH/fichier), "Faible"
         (un seul mois et non mesuré, ou aucun stock actuel connu).
      4. Projection = conso/jour pondérée × nb jours du mois cible.
      5. Commande = besoin (projection − stock actuel), plafonnée par la
         place réellement disponible dans la cuve (jamais plus que
         capacité − stock).
      6. Repère de validation : comparaison à la dernière commande décidée
         par Ops (FuelSuiviCommandeSite) quand elle existe, pour juger si le
         modèle est dans le bon ordre de grandeur.
    """
    permission_classes = [IsAuthenticated]

    MARGIN_DEFAULT = 0.15
    # Poids décroissants du mois le plus récent au plus ancien — normalisés
    # au nombre de mois réellement disponibles pour ce site (voir _weighted_rate).
    WEIGHTS_BY_HISTORY_LEN = {1: [1.0], 2: [0.6, 0.4], 3: [0.5, 0.3, 0.2]}
    MAX_MONTHS_HISTORY = 3
    HIGH_VARIANCE_THRESHOLD = 0.35  # coefficient de variation au-delà duquel on rétrograde la confiance

    def get(self, request):
        import calendar
        from datetime import date

        from django.db.models import Q
        from django.utils import timezone

        from fuel_tracking.models import FuelConsommationMonthly, FuelStockSnapshot, FuelSuiviCommandeSite

        try:
            marge = float(request.query_params.get("marge", self.MARGIN_DEFAULT))
        except ValueError:
            marge = self.MARGIN_DEFAULT
        marge = max(0.0, marge)

        # Exclut le mois calendaire EN COURS, même s'il a déjà quelques
        # lignes (alimentées en continu par la synchro Celery du mois
        # courant) : tant qu'il n'est pas terminé, ce n'est pas un mois de
        # référence fiable pour la moyenne pondérée, et il fausserait la
        # cible (mois suivant le dernier mois COMPLET, pas le mois suivant
        # "aujourd'hui") — ex. le 2026-09-02, la cible doit être 2026-09
        # (juste après août, dernier mois complet), pas 2026-10.
        current_month = timezone.now().strftime("%Y-%m")
        available_months = sorted(
            FuelConsommationMonthly.objects.filter(month_year__lt=current_month)
            .order_by("-month_year")
            .values_list("month_year", flat=True).distinct()
        )[-self.MAX_MONTHS_HISTORY:]

        if not available_months:
            return Response({
                "target_month": None, "source_months": [], "marge_pct": marge,
                "kpis": None, "sites": [],
            })

        latest_year, latest_month = (int(x) for x in available_months[-1].split("-"))
        target_year, target_month_num = (latest_year + 1, 1) if latest_month == 12 else (latest_year, latest_month + 1)
        target_month = f"{target_year:04d}-{target_month_num:02d}"
        nb_jours_cible = calendar.monthrange(target_year, target_month_num)[1]

        rows = FuelConsommationMonthly.objects.filter(
            month_year__in=available_months, has_genset=True,
        ).values(
            "site_id", "site_name", "month_year", "year", "month",
            "conso_snowflake_l", "conso_gardien_l",
            "conso_estimee_cph_l", "conso_estimee_aout26_l",
        )

        by_site: dict[str, dict] = {}
        for r in rows:
            entry = by_site.setdefault(r["site_id"], {"site_name": r["site_name"], "months": {}})
            if r["conso_snowflake_l"] is not None:
                conso, source, measured = r["conso_snowflake_l"], "snowflake", True
            elif r["conso_gardien_l"] is not None:
                conso, source, measured = r["conso_gardien_l"], "gardiennage", True
            elif r["conso_estimee_cph_l"] is not None:
                conso, source, measured = r["conso_estimee_cph_l"], "cph", False
            elif r["conso_estimee_aout26_l"] is not None:
                conso, source, measured = r["conso_estimee_aout26_l"], "fichier", False
            else:
                continue
            nb_j = calendar.monthrange(r["year"], r["month"])[1]
            entry["months"][r["month_year"]] = {
                "conso_l": float(conso), "conso_jour_l": float(conso) / nb_j,
                "source": source, "measured": measured,
            }

        stock_by_site = {
            s.site_id: s for s in FuelStockSnapshot.objects.filter(site_id__in=by_site.keys(), has_genset=True)
        }

        last_ops_month = FuelSuiviCommandeSite.objects.order_by("-month_year").values_list("month_year", flat=True).first()
        ops_ref = {}
        if last_ops_month:
            ops_ref = {
                s.site_id: float(s.commande_avec_marge_l)
                for s in FuelSuiviCommandeSite.objects.filter(month_year=last_ops_month)
            }

        results = []
        for site_id, entry in by_site.items():
            months_sorted = sorted(entry["months"].items())  # chronologique, du plus ancien au plus récent
            history = [m for _, m in months_sorted][-self.MAX_MONTHS_HISTORY:]
            if not history:
                continue

            weights = self.WEIGHTS_BY_HISTORY_LEN.get(len(history), self.WEIGHTS_BY_HISTORY_LEN[1])
            # weights[0] = poids du plus récent → on parcourt l'historique à l'envers
            rates = [h["conso_jour_l"] for h in reversed(history)]
            conso_jour_ponderee = sum(w * r for w, r in zip(weights, rates))

            nb_measured = sum(1 for h in history if h["measured"])
            moyenne = sum(rates) / len(rates)
            variance_cv = (
                (sum((r - moyenne) ** 2 for r in rates) / len(rates)) ** 0.5 / moyenne
                if moyenne > 0 and len(rates) > 1 else 0.0
            )

            stock_row = stock_by_site.get(site_id)
            stock_l = float(stock_row.stock_snowflake_l) if stock_row and stock_row.stock_snowflake_l is not None else (
                float(stock_row.stock_enoc_l) if stock_row and stock_row.stock_enoc_l is not None else None
            )
            capacity_l = float(stock_row.capacity_snowflake_l) if stock_row and stock_row.capacity_snowflake_l is not None else None

            if len(history) >= 2 and nb_measured >= 2 and variance_cv <= self.HIGH_VARIANCE_THRESHOLD and stock_l is not None:
                confiance = "Élevée"
            elif stock_l is None or (len(history) == 1 and not history[0]["measured"]):
                confiance = "Faible"
            else:
                confiance = "Moyenne"

            conso_proj_l = conso_jour_ponderee * nb_jours_cible
            stock_connu = stock_l if stock_l is not None else 0.0
            commande_sans_marge_l = max(0.0, conso_proj_l - stock_connu)
            commande_avec_marge_l = commande_sans_marge_l * (1 + marge)
            place_disponible = max(0.0, capacity_l - stock_connu) if capacity_l is not None else None
            commande_finale_l = min(commande_avec_marge_l, place_disponible) if place_disponible is not None else commande_avec_marge_l
            stock_final_estime_l = stock_connu + commande_finale_l - conso_proj_l

            results.append({
                "site_id": site_id,
                "site_name": entry["site_name"],
                "nb_mois_historique": len(history),
                "sources_historique": [h["source"] for h in history],
                "conso_jour_ponderee_l": round(conso_jour_ponderee, 2),
                "conso_projetee_l": round(conso_proj_l, 1),
                "stock_actuel_l": round(stock_l, 1) if stock_l is not None else None,
                "stock_connu": stock_l is not None,
                "capacite_cuve_l": round(capacity_l, 1) if capacity_l is not None else None,
                "commande_sans_marge_l": round(commande_sans_marge_l, 1),
                "commande_avec_marge_l": round(commande_finale_l, 1),
                "plafonnee_par_capacite": place_disponible is not None and commande_avec_marge_l > place_disponible,
                "stock_final_estime_l": round(stock_final_estime_l, 1),
                "confiance": confiance,
                "commande_ops_reference_l": ops_ref.get(site_id),
            })

        results.sort(key=lambda r: r["commande_avec_marge_l"], reverse=True)

        search = (request.query_params.get("search") or "").strip().lower()
        if search:
            results = [r for r in results if search in r["site_id"].lower() or search in (r["site_name"] or "").lower()]

        total_commande_l = sum(r["commande_avec_marge_l"] for r in results)
        total_ops_ref_l = sum(v for v in ops_ref.values()) if ops_ref else None
        kpis = {
            "nb_sites": len(results),
            "total_commande_estimee_l": round(total_commande_l, 0),
            "nb_sites_rupture_prevue": sum(1 for r in results if r["stock_final_estime_l"] < 0),
            "nb_sites_confiance_faible": sum(1 for r in results if r["confiance"] == "Faible"),
            "nb_sites_confiance_elevee": sum(1 for r in results if r["confiance"] == "Élevée"),
            "total_commande_ops_reference_l": round(total_ops_ref_l, 0) if total_ops_ref_l else None,
            "ops_reference_month": last_ops_month,
        }

        return Response({
            "target_month": target_month,
            "source_months": available_months,
            "marge_pct": marge,
            "kpis": kpis,
            "sites": results,
        })


class FuelStockListView(APIView):
    """
    GET /api/fuel-tracking/stock/?search=&has_genset=&page=&limit=

    Stock carburant ACTUEL par site (FuelStockSnapshot) — jointure Snowflake
    (VW_FUEL_REPORT) + ENOC (fuel_level_readings), voir sync_fuel_stock. Pas
    de notion de mois : une seule ligne par site, remplacée à chaque sync.
    `has_genset=true|false` filtre les sites avec/sans GE (seuls les sites
    avec GE consomment/stockent du fuel) ; omis, retourne tous les sites.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from django.db.models import Count, Q

        from fuel_tracking.models import FuelStockSnapshot, FuelStockSyncRun, FuelEnocSyncRun

        last_sf_run = FuelStockSyncRun.objects.order_by("-started_at").first()
        last_enoc_run = FuelEnocSyncRun.objects.order_by("-started_at").first()
        sources = {
            "snowflake": {
                "connected": bool(last_sf_run and last_sf_run.status == FuelStockSyncRun.Status.SUCCESS),
                "last_status": last_sf_run.status if last_sf_run else None,
                "last_run_at": (last_sf_run.finished_at or last_sf_run.started_at) if last_sf_run else None,
                "error": last_sf_run.error_message if last_sf_run else None,
            },
            "enoc": {
                "connected": bool(last_enoc_run and last_enoc_run.status == FuelEnocSyncRun.Status.SUCCESS and last_enoc_run.rows_fetched > 0),
                "last_status": last_enoc_run.status if last_enoc_run else None,
                "last_run_at": (last_enoc_run.finished_at or last_enoc_run.started_at) if last_enoc_run else None,
                "error": last_enoc_run.error_message if last_enoc_run else None,
            },
        }

        qs = FuelStockSnapshot.objects.all().order_by("site_id")

        search = (request.query_params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(site_id__icontains=search) | Q(site_name__icontains=search))

        # Calculés AVANT le filtre has_genset (comme ge_counts) : ces 3
        # compteurs décrivent toujours le périmètre "avec GE" réel, qu'un
        # filtre has_genset=false soit demandé ou non — évite qu'ils
        # retombent à 0 par contradiction avec le filtre actif. Seuils
        # identiques à FillBar (StockSheet.tsx) : critique <15%, alerte <40%.
        ge_counts = qs.aggregate(
            sites_avec_ge=Count("id", filter=Q(has_genset=True)),
            sites_sans_ge=Count("id", filter=Q(has_genset=False)),
            sites_stock_critique=Count("id", filter=Q(has_genset=True, stock_snowflake_pct__lt=15)),
            sites_stock_alerte=Count("id", filter=Q(has_genset=True, stock_snowflake_pct__gte=15, stock_snowflake_pct__lt=40)),
            sites_sans_aucun_stock=Count("id", filter=Q(has_genset=True, stock_snowflake_l__isnull=True, stock_enoc_l__isnull=True)),
        )

        has_genset_param = (request.query_params.get("has_genset") or "").strip().lower()
        if has_genset_param in ("true", "1"):
            qs = qs.filter(has_genset=True)
        elif has_genset_param in ("false", "0"):
            qs = qs.filter(has_genset=False)

        agg = qs.aggregate(
            total_sites=Count("id"),
            sites_avec_stock_snowflake=Count("id", filter=Q(stock_snowflake_l__isnull=False)),
            sites_avec_stock_enoc=Count("id", filter=Q(stock_enoc_l__isnull=False)),
        )
        kpis = {
            "total_sites": agg["total_sites"],
            "sites_avec_ge": ge_counts["sites_avec_ge"],
            "sites_sans_ge": ge_counts["sites_sans_ge"],
            "sites_avec_stock_snowflake": agg["sites_avec_stock_snowflake"],
            "sites_avec_stock_enoc": agg["sites_avec_stock_enoc"],
            "sites_stock_critique": ge_counts["sites_stock_critique"],
            "sites_stock_alerte": ge_counts["sites_stock_alerte"],
            "sites_sans_aucun_stock": ge_counts["sites_sans_aucun_stock"],
        }

        try:
            page = max(1, int(request.query_params.get("page", 1)))
        except ValueError:
            page = 1
        try:
            limit = min(200, max(1, int(request.query_params.get("limit", 50))))
        except ValueError:
            limit = 50

        total = agg["total_sites"]
        total_pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        rows = qs[start:start + limit]

        def serialize(row):
            # Commentaire — explique en clair pourquoi le stock manque pour
            # ce site, plutôt que de laisser deviner depuis une cellule
            # vide (même principe que Suivis Consommation, 2026-08).
            comment_parts = []
            if row.stock_snowflake_l is None:
                comment_parts.append(
                    "Stock Snowflake : aucun relevé de niveau de cuve physiquement valide sur la fenêtre glissante de 30 jours "
                    "(VW_FUEL_REPORT) — capteur absent, en panne, ou site jamais couvert par ce flux."
                )
            if row.stock_enoc_l is None:
                comment_parts.append(
                    "Stock ENOC : aucun relevé disponible pour ce site (collecte historique ponctuelle, pas un flux continu — "
                    "seuls certains sites ont été relevés au moins une fois)."
                )
            commentaire = " ".join(comment_parts) or None

            return {
                "site_id": row.site_id,
                "site_name": row.site_name,
                "typology": row.typology,
                "site_type": row.site_type,
                "dg_count": row.dg_count,
                "power_supply": row.power_supply,
                "has_genset": row.has_genset,
                "has_genset_snowflake": row.has_genset_snowflake,
                "has_genset_enoc": row.has_genset_enoc,
                "nb_ge_enoc": row.nb_ge_enoc,
                "stock_snowflake_l": float(row.stock_snowflake_l) if row.stock_snowflake_l is not None else None,
                "capacity_snowflake_l": float(row.capacity_snowflake_l) if row.capacity_snowflake_l is not None else None,
                "stock_snowflake_pct": float(row.stock_snowflake_pct) if row.stock_snowflake_pct is not None else None,
                "stock_snowflake_date": row.stock_snowflake_date,
                "quality_status": row.quality_status,
                "stock_enoc_l": float(row.stock_enoc_l) if row.stock_enoc_l is not None else None,
                "stock_enoc_date": row.stock_enoc_date,
                "commentaire": commentaire,
            }

        return Response({
            "data": [serialize(r) for r in rows],
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "totalPages": total_pages,
                "hasNext": page < total_pages,
                "hasPrev": page > 1,
            },
            "sources": sources,
            "kpis": kpis,
        })
