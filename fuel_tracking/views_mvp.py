# fuel_tracking/views_mvp.py
"""
Vues MVP Suivi Carburant (Stock) :

  POST stock/imports/analyser/            analyse préalable d'un fichier (feuilles, colonnes, mapping) — LECTURE SEULE
  GET  stock/imports/<id>/rapport/        rapport complet du lot (CSV téléchargeable)
  GET  stock/releves/                     relevés de stock (mesures brutes, litres directs / calculés / estimés, cuve, confiance)
  GET  stock/cuves-a-traiter/             cuves validées / proposées / sans plaque / incohérentes, score, impact
  GET  stock/priorites/                   priorités de fiabilisation classées par impact
"""
from __future__ import annotations

from datetime import datetime

from django.db.models import Count, Q
from django.http import HttpResponse
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel_tracking.views_balance import parse_bound
from fuel_tracking.views_cph import IsCphValidator


def alert_codes(alertes) -> set:
    from fuel_tracking.services.stock_period_balance import alert_codes_of

    return alert_codes_of(alertes)


def _f(v):
    return float(v) if v is not None else None


def _page_of(request, default=50, maximum=200):
    from fuel_tracking.views_stock import _page

    return _page(request, default=default, maximum=maximum)


def _paginate(rows, request, default=50):
    from fuel_tracking.views_stock import _pagination

    page, limit = _page_of(request, default)
    lo = (page - 1) * limit
    return rows[lo:lo + limit], _pagination(page, limit, len(rows))


class ImportAnalyzeView(APIView):
    """Analyse sans import : feuilles, en-têtes, mapping documenté, couverture de sites propre au fichier."""

    permission_classes = [IsCphValidator]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        from fuel_tracking.services import stock_import_pipeline as P
        from fuel_tracking.services.stock_file_analysis import analyze

        f = request.FILES.get("file")
        if f is None:
            return Response({"detail": "Fichier manquant."}, status=400)
        if not f.name.lower().endswith(P.ACCEPTED_EXT):
            return Response({"detail": "Format accepté : .xlsx, .xlsm, .xlsb."}, status=400)
        try:
            return Response(analyze(f.read(), f.name, str(request.data.get("source_type") or "").upper() or None))
        except Exception as e:  # fichier illisible : message clair, aucune écriture
            return Response({"detail": f"Fichier illisible ({e.__class__.__name__}) : {e}"}, status=400)


class ImportBatchReportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk: int):
        from fuel_tracking.models import FuelImportBatch
        from fuel_tracking.services.stock_import_pipeline import report_csv

        b = FuelImportBatch.objects.select_related("received_by").filter(pk=pk).first()
        if b is None:
            return Response({"detail": "Lot introuvable."}, status=404)
        resp = HttpResponse("﻿" + report_csv(b), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="rapport_lot_{b.pk}.csv"'
        return resp


QUALITY_LABEL = {
    "LITRE_DIRECT": "Litre direct", "CM_CONVERTI_CUVE_VALIDEE": "cm converti (cuve validée)",
    "CM_CONVERTI_CUVE_GEOMETRIQUE_RESERVE": "cm estimé (cuve géométrique sans plaque)",
    "CM_CONVERTI_CUVE_PROPOSEE_RESERVE": "cm estimé (cuve proposée)", "CM_SANS_CUVE": "cm sans cuve (aucun litre)",
    "SITE_NON_RECONNU": "Site non reconnu", "UNITE_INCONNUE": "Unité inconnue", "VALEUR_INVALIDE": "Valeur invalide",
    "DOUBLON_POTENTIEL": "Doublon potentiel",
}
ROW_CODE_STATUS = {"UNKNOWN_SITE": "SITE_NON_RECONNU", "UNKNOWN_UNIT": "UNITE_INCONNUE", "INVALID_VALUE": "VALEUR_INVALIDE",
                   "NEGATIVE_LEVEL": "VALEUR_INVALIDE", "MISSING_LEVEL": "VALEUR_INVALIDE"}


class StockReadingsView(APIView):
    """Toutes les mesures brutes : valeur brute conservée, litres directs / calculés / estimés séparés."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from core.models import Site
        from fuel_tracking.models import FuelImportRow, FuelStockObservation as O

        p = request.query_params
        if p.get("vue") == "rejetes":
            return self.rejected(request)
        qs = O.objects.exclude(source_type="SNOWFLAKE").select_related("import_row")
        if p.get("source"):
            qs = qs.filter(source_type=p["source"])
        if p.get("site"):
            qs = qs.filter(site_id__icontains=p["site"].strip())
        if p.get("qualite") == "DOUBLON_POTENTIEL":
            qs = qs.filter(potential_duplicate_of__isnull=False)
        elif p.get("qualite"):
            qs = qs.filter(reading_quality_status=p["qualite"])
        if p.get("confiance"):
            qs = qs.filter(confidence=p["confiance"])
        if p.get("unite") in ("L", "CM"):
            qs = qs.filter(level_unit=p["unite"])
        if p.get("statut"):
            qs = qs.filter(validation_status=p["statut"])
        if p.get("lot"):
            qs = qs.filter(batch_id=p["lot"])
        if p.get("anomalie"):
            qs = qs.filter(issue_codes__contains=p["anomalie"])
        if p.get("estimes") in ("1", "true"):
            qs = qs.filter(estimated_liters__isnull=False)
        d1, d2 = parse_bound(p.get("debut")), parse_bound(p.get("fin"), end=True)
        if d1:
            qs = qs.filter(observation_datetime__gte=d1)
        if d2:
            qs = qs.filter(observation_datetime__lte=d2)
        total = qs.count()
        page, limit = _page_of(request, 50)
        rows = list(qs.order_by("-observation_datetime", "-id")[(page - 1) * limit:page * limit])
        sites = {s["site_id"]: s for s in Site.objects.filter(site_id__in={o.site_id for o in rows}).values("site_id", "name", "zone", "country")}
        data = []
        for o in rows:
            s = sites.get(o.site_id, {})
            data.append({
                "id": o.pk, "source": o.source_type, "site_id": o.site_id, "site_name": s.get("name"), "zone": s.get("zone"), "country": s.get("country"),
                "site_brut": getattr(o.import_row, "site_id_raw", None), "date": o.observation_datetime, "niveau_brut": o.level_raw,
                "unite": o.level_unit, "niveau_cm": _f(o.level_cm), "litres_directs": _f(o.declared_liters if o.level_unit == "L" else None),
                "litres_calcules": _f(o.level_liters if o.level_unit == "CM" else None), "litres_estimes": _f(o.estimated_liters),
                "mode_estimation": o.estimation_mode, "detail_estimation": o.estimation_detail or None, "cuve": o.tank_id,
                "modele_cuve": o.tank_model_ref, "methode_conversion": o.conversion_method, "regle": o.conversion_rule,
                "confiance": o.confidence, "qualite": "DOUBLON_POTENTIEL" if o.potential_duplicate_of else o.reading_quality_status,
                "qualite_releve": o.reading_quality_status, "doublon_de": o.potential_duplicate_of, "statut_validation": o.validation_status,
                "anomalies": o.issue_codes, "motif": o.validation_reason, "lot": o.batch_id, "auteur": o.source_user, "commentaire": o.comment,
            })
        from fuel_tracking.views_stock import _pagination

        base = O.objects.exclude(source_type="SNOWFLAKE")
        counts = {r["reading_quality_status"] or "NON_QUALIFIE": r["n"] for r in base.values("reading_quality_status").annotate(n=Count("id"))}
        counts["DOUBLON_POTENTIEL"] = base.filter(potential_duplicate_of__isnull=False).count()
        return Response({
            "data": data, "pagination": _pagination(page, limit, total), "totaux_qualite": counts,
            "totaux_source": {r["source_type"]: r["n"] for r in base.values("source_type").annotate(n=Count("id"))},
            "libelles": QUALITY_LABEL, "facettes": {"sources": sorted(counts and {r for r in base.values_list("source_type", flat=True).distinct()}),
                                                    "qualites": list(QUALITY_LABEL)},
        })

    def rejected(self, request):
        """Lignes de fichier non converties en relevé : site non reconnu, unité inconnue, valeur invalide…"""
        from fuel_tracking.models import FuelImportRow
        from fuel_tracking.views_stock import _pagination

        p = request.query_params
        qs = FuelImportRow.objects.filter(validation_status="REJECTED").exclude(batch__source_type__in=("TANK_MODELS", "CLOSING"))
        if p.get("lot"):
            qs = qs.filter(batch_id=p["lot"])
        if p.get("source"):
            qs = qs.filter(batch__source_type=p["source"])
        page, limit = _page_of(request, 50)
        total = qs.count()
        data = []
        for r in qs.order_by("-id")[(page - 1) * limit:page * limit]:
            codes = [m.get("code") for m in r.messages or []]
            status = next((ROW_CODE_STATUS[c] for c in codes if c in ROW_CODE_STATUS), "VALEUR_INVALIDE")
            data.append({"id": r.pk, "lot": r.batch_id, "feuille": r.sheet_name, "ligne": r.row_number, "site_brut": r.site_id_raw,
                         "qualite": status, "codes": codes, "motif": " | ".join(m.get("message", "") for m in r.messages or []),
                         "date": r.event_datetime})
        return Response({"data": data, "pagination": _pagination(page, limit, total), "libelles": QUALITY_LABEL})


def tank_class(spec, min_score: int) -> dict:
    """Catégories NON exclusives d'une cuve + possibilité d'estimation."""
    full = spec.geometric_capacity_l()
    cal = spec.calibration() if spec.plate_capacity_l else {"gap_pct": None, "coherent": None}
    cats = []
    if spec.validated:
        cats.append("VALIDEE")
    if spec.status in ("PROPOSEE", "A_CONFIRMER"):
        cats.append("PROPOSEE")
    if full and not spec.plate_capacity_l:
        cats.append("SANS_PLAQUE")
    if spec.plate_capacity_l and cal["coherent"] is False:
        cats.append("INCOHERENTE")
    if not full:
        cats.append("GEOMETRIE_ABSENTE")
    if spec.validated and not spec.plate_capacity_l and full:
        from fuel_tracking.services.stock_registry import geometric_estimation_allowed

        usage = "ESTIMATION_GEOMETRIQUE" if geometric_estimation_allowed() else "ESTIMATION_GEOMETRIQUE_DESACTIVEE"
    elif spec.validated and cal["coherent"]:
        usage = "CONVERSION_CERTIFIABLE"
    elif spec.status in ("PROPOSEE", "A_CONFIRMER") and full and (spec.compat_score or 0) >= min_score and cal["coherent"] is not False:
        usage = "ESTIMATION_CUVE_PROPOSEE"
    else:
        usage = "AUCUN_LITRE"
    return {"categories": cats, "usage": usage, "geometrie_l": round(full) if full else None, "ecart_plaque_pct": cal["gap_pct"]}


TANK_ACTIONS = {
    "INCOHERENTE": "Relever cotes et plaque sur site, corriger la fiche cuve.",
    "SANS_PLAQUE": "Relever la capacité de plaque (sinon estimation avec réserve seulement).",
    "PROPOSEE": "Vérifier le modèle proposé puis valider la cuve (Cuves & gardiennage).",
    "GEOMETRIE_ABSENTE": "Relever forme et cotes de la cuve.",
    "VALIDEE": "Aucune action.",
}


def tank_rows(site=None, categorie=None, usage=None):
    """Cuves à traiter classées par impact (sites prioritaires décidés par le métier en tête). Retourne (rows, counts, usages, last, min_score)."""
    from django.conf import settings

    from fuel_tracking.models import FuelStockBalance, FuelStockObservation as O, FuelTankMaster as T
    from fuel_tracking.services import tank_conversion as TC
    from fuel_tracking.services.stock_registry import proposed_tank_min_score

    min_score = proposed_tank_min_score()
    pinned = [x.upper() for x in (getattr(settings, "FUEL_PRIORITY_TANK_SITES", []) or [])]
    blocked = dict(O.objects.filter(level_unit="CM", level_liters__isnull=True, estimated_liters__isnull=True)
                   .exclude(source_type="SNOWFLAKE").values_list("site_id").annotate(n=Count("id")))
    estimated = dict(O.objects.filter(estimated_liters__isnull=False).values_list("site_id").annotate(n=Count("id")))
    last = FuelStockBalance.objects.order_by("-calculated_at").values("observation_start", "observation_end").first()
    impact = {}
    if last:
        for b in FuelStockBalance.objects.filter(**last).filter(Q(estimation=True) | Q(cause_principale__in=("CM_SANS_CUVE", "DONNEES_STOCK_INCOMPLETES"))):
            impact[b.site_id] = impact.get(b.site_id, 0) + 1
    rows, counts = [], {}
    qs = T.objects.filter(active=True).exclude(validation_status="REJETEE").select_related("tank_model")
    if site:
        qs = qs.filter(site_id__icontains=site.strip())
    for t in qs:
        spec = TC.TankSpec.from_model(t)
        info = tank_class(spec, min_score)
        for c in info["categories"]:
            counts[c] = counts.get(c, 0) + 1
        if categorie and categorie not in info["categories"]:
            continue
        main = next((c for c in ("INCOHERENTE", "SANS_PLAQUE", "PROPOSEE", "GEOMETRIE_ABSENTE", "VALIDEE") if c in info["categories"]), "VALIDEE")
        rows.append({
            "id": t.pk, "site_id": t.site_id, "cuve": t.tank_id, "statut": t.validation_status, "forme": spec.family,
            "cotes_cm": [spec.d1, spec.d2, spec.d3], "plaque_l": spec.plate_capacity_l, "capacite_source_l": _f(t.capacity_l),
            "modele": spec.model_ref, "score_compatibilite": t.compat_score, "score_min_estimation": min_score, **info,
            "releves_bloques": blocked.get(t.site_id, 0), "releves_estimes": estimated.get(t.site_id, 0),
            "bilans_impactes": impact.get(t.site_id, 0), "action": TANK_ACTIONS[main], "prioritaire_demande": t.site_id.upper() in pinned,
        })
    rows.sort(key=lambda r: (0 if r["prioritaire_demande"] else 1, -(r["bilans_impactes"]), -(r["releves_bloques"] + r["releves_estimes"]), r["site_id"]))
    if usage:
        rows = [r for r in rows if r["usage"] == usage]
    usages = {}
    for r in rows:
        usages[r["usage"]] = usages.get(r["usage"], 0) + 1
    return rows, counts, usages, last, min_score


class TanksToProcessView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        p = request.query_params
        rows, counts, usages, last, min_score = tank_rows(p.get("site"), p.get("categorie"), p.get("usage"))
        page_rows, pagination = _paginate(rows, request, 50)
        return Response({"data": page_rows, "pagination": pagination, "totaux_categories": counts, "totaux_usage": usages,
                         "periode_impact": last, "seuil_score_proposee": min_score,
                         "note": "Une cuve proposée n'est jamais validée automatiquement : seule l'estimation (confiance faible) est autorisée "
                                 "au-dessus du seuil de compatibilité."})


PRIORITY_LABEL = {
    "forte_conso_estimee_sans_cuve_validee": "Forte consommation estimée sans cuve validée",
    "stock_trop_ancien": "Stock trop ancien", "stock_initial_final_manquant": "Stock initial / final manquant",
    "cuve_incoherente": "Cuve incohérente", "livraison_closing_absente_enoc": "Livraison du Closing absente d'ENOC",
    "mouvements_enoc_closing_en_conflit": "Conflit ENOC / Closing", "sortie_closing_absente_enoc": "Sortie du Closing absente d'eGrid", "mouvement_enoc_incomplet": "Mouvement ENOC incomplet",
    "ecart_reel_cph_instrumente": "Écart élevé réel / CPH / instrumenté",
}


class PrioritiesView(APIView):
    """Actions classées par impact (litres) pour fiabiliser progressivement les données."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import FuelEnocQualifiedMovement as EQ, FuelStockBalance as B, FuelStockObservation as O, FuelTankMaster as T
        from fuel_tracking.services import tank_conversion as TC
        from fuel_tracking.services.stock_period_balance import reconciliation

        p = request.query_params
        start, end = parse_bound(p.get("debut")), parse_bound(p.get("fin"), end=True)
        if not (start and end):
            last = B.objects.order_by("-calculated_at").values("observation_start", "observation_end").first()
            start, end = (last["observation_start"], last["observation_end"]) if last else (None, None)
        items = []
        if start and end:
            for b in B.objects.filter(observation_start=start, observation_end=end):
                base = {"site_id": b.site_id, "site_name": b.site_name, "zone": b.zone, "bilan_id": b.pk}
                if b.estimation and b.consommation_reelle_l is not None:
                    items.append({**base, "categorie": "forte_conso_estimee_sans_cuve_validee", "impact_l": _f(b.consommation_reelle_l),
                                  "motif": "; ".join(b.reserves[:2]) or "Stock estimé", "action": "Valider la cuve ou relever la plaque : la consommation devient certifiable."})
                ref = (b.stock_initial_detail or {}).get("reference_closing") or {}
                codes = alert_codes(b.alertes)
                if "LIVRAISON_CLOSING_ABSENTE_ENOC" in codes:
                    items.append({**base, "categorie": "livraison_closing_absente_enoc", "impact_l": abs(ref.get("ecart_entrees_l") or 0) or None,
                                  "motif": next((a for a in b.alertes if a.startswith("LIVRAISON_CLOSING_ABSENTE_ENOC")), "Livraison du Closing absente d'ENOC"),
                                  "action": "Compléter la/les livraison(s) dans ENOC (Fuel Manager) : le bilan pourra alors être certifié."})
                if "SORTIE_CLOSING_ABSENTE_ENOC" in codes:
                    items.append({**base, "categorie": "sortie_closing_absente_enoc", "impact_l": abs(ref.get("ecart_sorties_l") or 0) or None,
                                  "motif": next((a for a in b.alertes if a.startswith("SORTIE_CLOSING_ABSENTE_ENOC")), "Sortie du Closing absente d'eGrid"),
                                  "action": "Saisir le retrait / vol / vidange dans ENOC (Fuel Manager)."})
                if "CONFLIT_ENOC_CLOSING" in codes:
                    items.append({**base, "categorie": "mouvements_enoc_closing_en_conflit",
                                  "impact_l": max(abs(ref.get("ecart_entrees_l") or 0), abs(ref.get("ecart_sorties_l") or 0)) or None,
                                  "motif": next((a for a in b.alertes if a.startswith("CONFLIT_ENOC_CLOSING")), "Conflit ENOC / Closing"),
                                  "action": "Arbitrer avec le Fuel Manager : entrée ENOC non confirmée ou sortie du Closing absente d'eGrid."})
                if b.cause_principale == "STOCK_TROP_ANCIEN":
                    items.append({**base, "categorie": "stock_trop_ancien", "impact_l": _f(b.volume_brut_integre_l),
                                  "motif": next(iter(b.motifs), "Relevé trop ancien"), "action": "Obtenir un relevé terrain proche de la borne (gardiennage / ENOC)."})
                elif b.statut_bilan == "NON_CALCULABLE" and b.cause_principale in ("DONNEES_STOCK_INCOMPLETES", "CM_SANS_CUVE"):
                    items.append({**base, "categorie": "stock_initial_final_manquant", "impact_l": _f(b.volume_brut_integre_l),
                                  "motif": next(iter(b.motifs), "Stock manquant"),
                                  "action": "Valider une cuve (relevés en cm) ou importer les relevés de la période." if b.cause_principale == "CM_SANS_CUVE"
                                  else "Importer / obtenir le relevé initial ou final."})
        blocked = dict(O.objects.filter(level_unit="CM", level_liters__isnull=True).exclude(source_type="SNOWFLAKE").values_list("site_id").annotate(n=Count("id")))
        for t in T.objects.filter(active=True, validation_status="VALIDEE"):
            spec = TC.TankSpec.from_model(t)
            if spec.plate_capacity_l and spec.calibration()["coherent"] is False:
                items.append({"site_id": t.site_id, "cuve": t.tank_id, "tank_id": t.pk, "categorie": "cuve_incoherente",
                              "impact_l": float(blocked.get(t.site_id, 0)), "impact_unite": "relevés bloqués",
                              "motif": f"Plaque {spec.plate_capacity_l:.0f} L, géométrie {round(spec.geometric_capacity_l() or 0)} L ({spec.calibration()['gap_pct']:+.0f} %)",
                              "action": "Relever cotes et plaque sur site, corriger la fiche cuve."})
        for q in EQ.objects.filter(Q(quality_status_global="DEFAUTS_MAJEURS") | Q(non_integration_cause__in=("SITE_SOURCE_ABSENT", "INCOHERENCE_BLOQUANTE",
                                                                                                         "SITE_NON_IDENTIFIE", "QUANTITE_ABSENTE", "DATE_ABSENTE"))) \
                .exclude(business_status__in=("HISTORIQUE_NON_VERIFIABLE", "REJETEE")).order_by("-approved_at")[:300]:
            qty = q.integrated_quantity_l or q.approved_quantity_l or q.actual_quantity_l
            items.append({"site_id": q.site_id_resolved or q.site_id, "categorie": "mouvement_enoc_incomplet", "enoc_id": q.pk, "reference": q.enoc_reference,
                          "impact_l": _f(qty) or 0.0, "motif": q.non_integration_cause or "Écart de niveau", "action": "Compléter le mouvement dans ENOC (lecture seule dans eGrid)."})
        if start and end and p.get("avec_ecarts", "1") in ("1", "true"):
            threshold = float(p.get("seuil_ecart_pct", 30))
            try:
                for r in reconciliation(start, end):
                    for key, label in (("instrumentee", "instrumentée Snowflake"), ("theorique", "théorique CPH")):
                        pct, gap = r.get(f"ecart_reel_{key}_pct"), r.get(f"ecart_reel_{key}_l")
                        ref = r.get("conso_instrumentee_l" if key == "instrumentee" else "conso_theorique_l")
                        if pct is not None and abs(pct) >= threshold:
                            items.append({"site_id": r["site_id"], "site_name": r["site_name"], "bilan_id": r["bilan_id"], "categorie": "ecart_reel_cph_instrumente",
                                          "impact_l": abs(gap), "motif": f"Réel vs {label} : {gap:+.0f} L ({pct:+.0f} %)",
                                          "action": "Vérifier stocks, mouvements et mesure (sonde / CPH)."})
                        elif pct is None and gap is not None and ref is not None and abs(gap) >= 200:  # référence quasi nulle : litres seulement
                            items.append({"site_id": r["site_id"], "site_name": r["site_name"], "bilan_id": r["bilan_id"], "categorie": "ecart_reel_cph_instrumente",
                                          "impact_l": abs(gap), "motif": f"Réel vs {label} : {gap:+.0f} L (référence quasi nulle : {ref:.1f} L, pourcentage non calculé)",
                                          "action": "Vérifier le temps de marche / la sonde, puis les stocks et mouvements."})
            except Exception:  # Snowflake / CPH indisponibles : le reste de la file reste affiché
                pass
        counts = {}
        for it in items:
            counts[it["categorie"]] = counts.get(it["categorie"], 0) + 1
        if p.get("categorie"):
            items = [i for i in items if i["categorie"] == p["categorie"]]
        items.sort(key=lambda i: -(i.get("impact_l") or 0))
        for rank, it in enumerate(items, start=1):
            it["rang"] = rank
            it["libelle_categorie"] = PRIORITY_LABEL[it["categorie"]]
        data, pagination = _paginate(items, request, 50)
        return Response({"data": data, "pagination": pagination, "periode": {"debut": start, "fin": end}, "totaux": counts, "libelles": PRIORITY_LABEL})


class StockReportListView(APIView):
    """Rapports téléchargeables (CSV) : libellé, description, paramètres demandés."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.services.stock_reports import REPORTS

        return Response({"data": [{"kind": k, **v} for k, v in REPORTS.items()]})


class StockReportView(APIView):
    """Télécharge un rapport en CSV (UTF-8 avec BOM, séparateur « ; »). Lecture seule."""

    permission_classes = [IsAuthenticated]

    def get(self, request, kind: str):
        from fuel_tracking.services.stock_reports import ReportError, build_report, to_csv

        try:
            name, cols, rows = build_report(kind, request.query_params.dict())
        except ReportError as e:
            return Response({"detail": str(e)}, status=400)
        resp = HttpResponse(to_csv(cols, rows), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{name}.csv"'
        resp["X-Report-Rows"] = str(len(rows))
        return resp
