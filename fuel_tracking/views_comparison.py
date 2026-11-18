# fuel_tracking/views_comparison.py
"""
« Suivis Consommations » — tableau unique, période unique.

  GET /api/fuel-tracking/consommations/                 KPI + tableau (pagination serveur) + facettes + période par défaut
  GET /api/fuel-tracking/consommations/periodes/        périodes disposant d'un bilan (+ défaut)
  GET /api/fuel-tracking/consommations/export/<kind>/   controle_complet | anomalies_fuel | bilan_certification (CSV)
Paramètres : date_debut, date_fin (AAAA-MM-JJ, jours entiers 00:00:00 → 23:59:59), filtres pays, zone, site, typologie, configuration,
typologie_energie, statut_bilan, statut_certification, source_stock, source_runtime, source_puissance, source_target, qualite (reserve|sans_reserve|anomalie), tri, page, page_size.
"""
from __future__ import annotations

from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fuel_tracking.services import conso_comparison as C
from fuel_tracking.services.period_utils import available_periods, default_period, to_day


def _period(p):
    d_from, d_to = to_day(p.get("date_debut") or p.get("start")), to_day(p.get("date_fin") or p.get("end"))
    if d_from and d_to:
        if d_to < d_from:
            raise ValueError("date_fin est antérieure à date_debut.")
        return d_from, d_to, False
    dflt = default_period()
    if dflt is None:
        return None, None, True
    return dflt[0], dflt[1], True


class ConsommationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        p = request.query_params
        try:
            d_from, d_to, is_default = _period(p)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        if d_from is None:
            return Response({"detail": "Aucun bilan de stock calculé : lancez le calcul des bilans.", "rows": [], "total": 0, "periode": None}, status=200)
        data = _cached_build(d_from, d_to, p)
        rows = data.pop("rows")
        try:
            size = max(1, min(int(p.get("page_size", 50)), 1000))   # « Tous les sites » côté interface
            page = max(1, int(p.get("page", 1)))
        except ValueError:
            size, page = 50, 1
        data["total"] = len(rows)
        data["page"], data["page_size"] = page, size
        data["rows"] = rows[(page - 1) * size: page * size]
        # Blocs « INVENTAIRE », « DONNÉES DE STOCK INITIAL » et « RAVITAILLEMENT » pour la page affichée seulement.
        # Les valeurs Snowflake / ENOC déjà en cache s'affichent tout de suite ; celles qui manquent sont lues EN TÂCHE DE FOND
        # (au-delà de SYNC_MAX paires) : la page ne reste jamais bloquée sur une source externe lente.
        data["avertissement_sources"], data["sources_en_attente"] = _enrich_page(data["rows"])
        data["periode"]["par_defaut"] = is_default
        return Response(data)


class ConsommationsPeriodesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        dflt = default_period()
        return Response({"periodes": available_periods(50), "defaut": {"debut": dflt[0].isoformat(), "fin": dflt[1].isoformat()} if dflt else None})


class ConsommationsExportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, kind):
        if kind not in C.EXPORTS:
            return Response({"detail": "Export inconnu.", "exports": C.EXPORTS}, status=404)
        try:
            d_from, d_to, _ = _period(request.query_params)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        if d_from is None:
            return Response({"detail": "Aucun bilan de stock calculé."}, status=404)
        name, content, n = C.export_csv(kind, d_from, d_to, request.query_params)
        r = HttpResponse(content, content_type="text/csv; charset=utf-8")
        r["Content-Disposition"] = f'attachment; filename="{name}"'
        r["X-Row-Count"] = str(n)
        return r


def _data_version() -> str:
    """Empreinte des données de l'écran : tout recalcul de bilan, import de fichier, synchro Snowflake / CPH change la clé."""
    from django.db.models import Count, Max

    from fuel_tracking.models import FuelImportBatch, FuelStockBalance
    from fuel_tracking.services.cph_service import data_version

    b = FuelStockBalance.objects.aggregate(n=Count("id"), m=Max("calculated_at"))
    i = FuelImportBatch.objects.aggregate(n=Count("id"), m=Max("received_at"))
    return f"{b['n']}|{b['m']}|{i['n']}|{i['m']}|{data_version()}"


SYNC_MAX = 20          # paires (site, jour) manquantes lues pendant la requête ; au-delà : tâche de fond
BUILD_TTL = 6 * 3600   # la clé contient la version des données : un recalcul ou un import la change (jamais servi périmé)


def _enrich_page(rows) -> tuple[str | None, int]:
    """(message sources, nombre de sites dont les valeurs Snowflake / ENOC sont en cours de lecture)."""
    from django.core.cache import cache

    from fuel_tracking.services import stock_initial_sources as S

    miss = S.missing_pairs(S.row_pairs(rows))
    if len(miss) <= SYNC_MAX:
        return S.enrich(rows), 0
    try:
        from fuel_tracking.tasks import warm_stock_initial_sources

        lock = "fuel-si:warm-lock:" + ",".join(sorted(f"{s}@{d}" for s, d in miss))[:200]
        if cache.add(lock, 1, 120):   # une seule lecture en cours pour les mêmes sites
            warm_stock_initial_sources.delay([[s, d.isoformat()] for s, d in sorted(miss)])
    except Exception:  # noqa: BLE001 — pas de file Celery : lecture directe (comportement d'avant)
        return S.enrich(rows), 0
    # valeurs déjà en cache affichées tout de suite ; le reste arrive au prochain rafraîchissement (l'interface relance seule)
    return S.enrich(rows, fetch=False), len(S.missing_pairs(S.row_pairs(rows)))


def warm_cache(months: int = 2) -> list[dict]:
    """Préchauffe l'écran Suivi Conso : calcul de la période par défaut et des `months` derniers mois complets, puis valeurs
    Snowflake / ENOC de tous leurs sites. Appelé après chaque calcul des bilans et périodiquement (Celery)."""
    from datetime import timedelta

    from django.utils import timezone

    from fuel_tracking.services import stock_initial_sources as S

    periods = []
    dflt = default_period()
    if dflt:
        periods.append((dflt[0], dflt[1]))
    first = timezone.localdate().replace(day=1)
    for _ in range(months):
        last = first - timedelta(days=1)
        first = last.replace(day=1)
        periods.append((first, last))
    done = []
    for a, b in dict.fromkeys(periods):
        rows = _cached_build(a, b, {"date_debut": a.isoformat(), "date_fin": b.isoformat()})["rows"]
        done.append({"periode": f"{a}→{b}", "sites": len(rows), "lus": S.warm_pairs(S.row_pairs(rows))})
    return done


def _cached_build(d_from, d_to, p):
    """Résultat du calcul de l'écran mis en cache (6 h, clé = période + filtres + version des données) : changer de page ou
    revenir sur l'écran ne recalcule plus tout. Jamais servi périmé : tout nouveau calcul ou import change la version."""
    import copy
    import hashlib
    import json

    from django.core.cache import cache

    params = {k: v for k, v in (p.dict() if hasattr(p, "dict") else dict(p)).items() if k not in ("page", "page_size")}
    raw = json.dumps([str(d_from), str(d_to), sorted(params.items()), _data_version()], default=str)
    key = "fuel-conso-build:" + hashlib.sha1(raw.encode()).hexdigest()
    data = cache.get(key)
    if data is None:
        data = C.build(d_from, d_to, params)
        cache.set(key, data, BUILD_TTL)
    return copy.deepcopy(data)


class ConsommationsSanteView(APIView):
    """GET /api/fuel-tracking/consommations/sante/ — fraîcheur des sources, couverture des relevés gardiens, sites en retard."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.services.data_health import health

        return Response(health(mois=request.query_params.get("mois") or None))


class ConsommationsEvolutionView(APIView):
    """GET /api/fuel-tracking/consommations/evolution/ — totaux par période (variation de stock, Closing) pour les graphiques."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(C.evolution(12))

