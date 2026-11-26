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

from fuel_tracking.services import conso_column_filters as CF
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
        cf = CF.parse(p.get(CF.PARAM))
        if cf:   # filtres de colonnes « comme Excel » : sur les valeurs affichées, avant pagination
            idx = _cached_index(d_from, d_to, p, rows)
            rows = [r for r in rows if CF.keep(idx, cf, r["site_id"])]
        try:
            size = max(1, min(int(p.get("page_size", 50)), 1000))   # « Tous les sites » côté interface
            page = max(1, int(p.get("page", 1)))
        except ValueError:
            size, page = 50, 1
        data["total"] = len(rows)
        data["page"], data["page_size"] = page, size
        data["totaux"], synth = _cached_totals(d_from, d_to, p, rows)   # ligne de totaux : tous les sites du filtre
        if synth:   # synthèse du mois = totaux du tableau (décision du 25/11/2026)
            prev = data.get("kpis_closing") or {"mois": d_from.strftime("%Y-%m"), "debut": d_from.isoformat(), "fin": d_to.isoformat()}
            data["kpis_closing"] = {**prev, "source": "TABLEAU_SUIVI_CONSO", "fichier": None, "kpis": {**(prev.get("kpis") or {}), **synth}}
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


SYNC_MAX = 0           # aucune lecture Snowflake / ENOC pendant l'affichage : toujours en tâche de fond (la page ne bloque jamais)
BUILD_TTL = 6 * 3600   # la clé contient la version des données : un recalcul ou un import la change (jamais servi périmé)


def _enrich_page(rows) -> tuple[str | None, int]:
    """(message sources, nombre de sites dont les valeurs Snowflake / ENOC sont en cours de lecture)."""
    from django.core.cache import cache

    from fuel_tracking.services import stock_initial_sources as S

    from fuel_tracking.services import ge_info as G

    ge_pending = 0
    if G.is_stale(G.cached()):
        G.request_refresh()                      # fabricant / type de GE : relus en tâche de fond (toutes les 6 h au plus)
        ge_pending = 0 if G.cached() else len(rows)
    miss = S.missing_pairs(S.row_pairs(rows))
    if len(miss) <= SYNC_MAX:
        return S.enrich(rows), ge_pending
    try:
        from fuel_tracking.tasks import warm_stock_initial_sources

        lock = "fuel-si:warm-lock:" + ",".join(sorted(f"{s}@{d}" for s, d in miss))[:200]
        if cache.add(lock, 1, 120):   # une seule lecture en cours pour les mêmes sites
            warm_stock_initial_sources.delay([[s, d.isoformat()] for s, d in sorted(miss)])
    except Exception:  # noqa: BLE001 — pas de file Celery : lecture directe (comportement d'avant)
        return S.enrich(rows), 0
    # valeurs déjà en cache affichées tout de suite ; le reste arrive au prochain rafraîchissement (l'interface relance seule)
    return S.enrich(rows, fetch=False), max(ge_pending, len(S.missing_pairs(S.row_pairs(rows))))


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
    from fuel_tracking.services import ge_info as G

    done = [{"ge": G.refresh()}] if G.is_stale(G.cached()) else []
    for a, b in dict.fromkeys(periods):
        rows = _cached_build(a, b, {"date_debut": a.isoformat(), "date_fin": b.isoformat()})["rows"]
        done.append({"periode": f"{a}→{b}", "sites": len(rows), "lus": S.warm_pairs(S.row_pairs(rows))})
        missing = S.missing_fact_days({d for _, d in S.row_pairs(rows)})
        if missing:   # jours de relevé sans faits Snowflake (hors fenêtre de la resynchro) : synchronisés en tâche de fond
            S.request_fact_days(missing)
            done[-1]["jours_snowflake_a_synchroniser"] = [d.isoformat() for d in missing]
    return done


def _cached_totals(d_from, d_to, p, rows):
    """Totaux de colonnes (ligne du haut du tableau) sur tous les sites du filtre, en cache 10 min : clé = période + filtres +
    version des données + dernier fichier Base GE + dernière relecture GE (tout changement de source change la clé)."""
    import hashlib
    import json

    from django.core.cache import cache

    from fuel_tracking.models import FuelImportBatch
    from fuel_tracking.services import ge_info as G
    from fuel_tracking.services import stock_initial_sources as S

    params = {k: v for k, v in (p.dict() if hasattr(p, "dict") else dict(p)).items() if k not in ("page", "page_size")}
    base = FuelImportBatch.objects.filter(source_type="BASE_GE").order_by("-received_at").values_list("pk", flat=True).first()
    raw = json.dumps([str(d_from), str(d_to), sorted(params.items()), _data_version(), base, (G.cached() or {}).get("lu_le")], default=str)
    key = "fuel-conso-totaux-v4:" + hashlib.sha1(raw.encode()).hexdigest()
    try:
        hit = cache.get(key)
    except Exception:  # noqa: BLE001
        hit = None
    if hit is not None:
        return hit
    try:
        from fuel_tracking.services.closing_kpis import table_kpis

        full = S.enriched_copy(rows)   # une seule lecture pour la ligne de totaux et la synthèse du mois
        out = (S.totals(full, enriched=True), table_kpis(full, d_from, d_to))
    except Exception:  # noqa: BLE001 — jamais bloquant : le tableau s'affiche sans la ligne de totaux
        import logging

        logging.getLogger(__name__).exception("Totaux du Suivi Conso impossibles")
        return None, None
    try:
        cache.set(key, out, 600)
    except Exception:  # noqa: BLE001
        pass
    return out


def _cached_index(d_from, d_to, p, rows):
    """site → {colonne: valeur affichée} pour les filtres de colonnes, en cache 10 min (clé = période + filtres hors filtres de
    colonnes + version des données + dernier Base GE + dernière relecture GE)."""
    import hashlib
    import json

    from django.core.cache import cache

    from fuel_tracking.models import FuelImportBatch
    from fuel_tracking.services import ge_info as G
    from fuel_tracking.services import stock_initial_sources as S

    params = {k: v for k, v in (p.dict() if hasattr(p, "dict") else dict(p)).items() if k not in ("page", "page_size", CF.PARAM, "colonne")}
    base = FuelImportBatch.objects.filter(source_type="BASE_GE").order_by("-received_at").values_list("pk", flat=True).first()
    raw = json.dumps([str(d_from), str(d_to), sorted(params.items()), _data_version(), base, (G.cached() or {}).get("lu_le")], default=str)
    key = "fuel-conso-colonnes-v3:" + hashlib.sha1(raw.encode()).hexdigest()
    try:
        hit = cache.get(key)
    except Exception:  # noqa: BLE001
        hit = None
    if hit is not None:
        return hit
    idx = CF.index(S.enriched_copy(rows))
    try:
        cache.set(key, idx, 600)
    except Exception:  # noqa: BLE001
        pass
    return idx


class ConsommationsValeursView(APIView):
    """GET /api/fuel-tracking/consommations/valeurs/?colonne=bloc.champ&<filtres> — valeurs d'une colonne (bouton ▼ de l'en-tête),
    avec le nombre de sites, parmi les sites retenus par les autres filtres (comme Excel)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        p = request.query_params
        col = p.get("colonne") or ""
        if col not in CF.FILTER_KEYS:
            return Response({"detail": "Colonne non filtrable."}, status=400)
        try:
            d_from, d_to, _dflt = _period(p)
        except ValueError as e:
            return Response({"detail": str(e)}, status=400)
        if d_from is None:
            return Response({"colonne": col, "valeurs": []})
        rows = _cached_build(d_from, d_to, p)["rows"]
        idx = _cached_index(d_from, d_to, p, rows)
        return Response({"colonne": col, "valeurs": CF.distinct(idx, CF.parse(p.get(CF.PARAM)), [r["site_id"] for r in rows], col)})


def _cached_build(d_from, d_to, p):
    """Résultat du calcul de l'écran mis en cache (6 h, clé = période + filtres + version des données) : changer de page ou
    revenir sur l'écran ne recalcule plus tout. Jamais servi périmé : tout nouveau calcul ou import change la version."""
    import copy
    import hashlib
    import json

    from django.core.cache import cache

    params = {k: v for k, v in (p.dict() if hasattr(p, "dict") else dict(p)).items() if k not in ("page", "page_size", CF.PARAM, "colonne")}
    raw = json.dumps([str(d_from), str(d_to), sorted(params.items()), _data_version()], default=str)
    key = "fuel-conso-build-v3:" + hashlib.sha1(raw.encode()).hexdigest()
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

