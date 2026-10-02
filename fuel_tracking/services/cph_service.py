# fuel_tracking/services/cph_service.py
"""Chargement PostgreSQL → entrées du moteur cph_engine (aucun accès Snowflake ici)."""
from __future__ import annotations

import hashlib
import logging

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings as dj_settings

from fuel_tracking.services import cph_engine as E
from fuel_tracking.services import cph_matching as M

logger = logging.getLogger(__name__)


def _thresholds() -> dict:
    from fuel_tracking.models import FuelRapprochementThreshold

    thr = FuelRapprochementThreshold.objects.filter(label="default").first()
    if thr is None:
        return dict(E.DEFAULT_THRESHOLDS)
    return {
        "seuil_ok_pct": thr.seuil_ok_pct, "seuil_aj_pct": thr.seuil_a_justifier_pct,
        "seuil_ok_l": thr.seuil_ok_l, "seuil_aj_l": thr.seuil_aj_l,
    }


def engine_settings() -> E.EngineSettings:
    from django.db.models import Max
    from fuel_tracking.models import FuelSiteDailyFacts

    return E.EngineSettings(
        data_until=FuelSiteDailyFacts.objects.aggregate(v=Max("date"))["v"],
        enoc_deliveries_connected=bool(getattr(dj_settings, "FUEL_ENOC_DELIVERIES_CONNECTED", False)),
        thresholds=_thresholds(),
        nominal_power_factor=Decimal(str(getattr(dj_settings, "FUEL_CPH_NOMINAL_POWER_FACTOR", E.NOMINAL_POWER_FACTOR))),
        ac_w_to_kw_divisor=Decimal(str(getattr(dj_settings, "FUEL_CPH_AC_POWER_TO_KW_DIVISOR", "1000"))),
        sfc_min_l_kwh=Decimal(str(getattr(dj_settings, "FUEL_CPH_SFC_MIN_L_KWH", "0.20"))),
        sfc_max_l_kwh=Decimal(str(getattr(dj_settings, "FUEL_CPH_SFC_MAX_L_KWH", "0.50"))),
    )


# Statut de correspondance → motif précis d'absence de CPH.
CURVE_CODES = {
    M.COURBE_CPH_MANQUANTE: E.MC_COURBE_CPH_MANQUANTE,
    M.A_VALIDER: E.MC_MAPPING_A_VALIDER,
    M.REJETE: E.MC_MAPPING_A_VALIDER,
    M.MODELE_AMBIGU: E.MC_MODELE_AMBIGU,
    M.SITE_MULTI_GE: E.MC_SITE_MULTI_GE,
    M.TYPE_GE_ABSENT: E.MC_TYPE_GE_ABSENT,
}


def _curve_from_model(c) -> E.Curve:
    return E.Curve(
        curve_id=c.curve_id, label=f"{c.manufacturer} {c.model}" + (f" — {c.variant}" if c.variant else ""),
        status=c.status, prp_kva=c.prp_kva, prp_kw=c.prp_kw, power_factor=c.power_factor,
        a=c.coef_a, b=c.coef_b, c=c.coef_c, source=c.source,
    )


def _match_info(statut, m=None, curve=None, motif=None) -> dict:
    """Correspondance plaque → courbe exposée par site (statut distinct de la qualité de courbe)."""
    return {
        "statut": statut,
        "mapping_status": statut,
        "curve_source_status": M.curve_source_status(curve.status) if curve is not None else None,
        "score": getattr(m, "match_score", None),
        "methode": getattr(m, "match_method", None) or None,
        "date": getattr(m, "matched_at", None) or getattr(m, "validated_at", None),
        "valide_par": getattr(getattr(m, "validated_by", None), "username", None),
        "courbe_id": curve.curve_id if curve is not None else None,
        "courbe_statut": curve.status if curve is not None else None,
        "mapping_id": getattr(m, "pk", None),
        "motif": motif,
    }


def resolve_curve(dg_count, ge_label, ge_kva, mappings: dict) -> tuple[E.Curve | None, str | None, dict]:
    """
    mappings : libellé normalisé → [CphInventoryMapping, …] (un par kVA inventaire).
    Une courbe est appliquée si la correspondance est AUTO_VALIDE_COMPATIBLE ou
    VALIDE_MANUELLEMENT, quel que soit le statut d'origine de la courbe (affiché à part).
    La puissance nominale utilisée pour la charge est le kVA PRP de la courbe × 0,8.
    """
    if dg_count is not None and dg_count > 1:
        reason = f"site multi-GE (DG_COUNT = {dg_count}) : affectation d'une courbe par GE non disponible"
        return None, reason, _match_info(M.SITE_MULTI_GE, motif=reason)
    if not ge_label:
        reason = "type de GE absent de l'inventaire Base GE"
        return None, reason, _match_info(M.TYPE_GE_ABSENT, motif=reason)
    candidates = mappings.get(E.normalize_label(ge_label)) or []
    if not candidates:
        reason = f"libellé GE « {ge_label} » absent du mappage de l'abaque : aucune courbe CPH"
        return None, reason, _match_info(M.COURBE_CPH_MANQUANTE, motif=reason)
    if len(candidates) == 1:
        m = candidates[0]
    else:
        exact = [c for c in candidates if ge_kva is not None and c.inventory_kva == ge_kva]
        if len(exact) != 1:
            kvas = ", ".join(str(c.inventory_kva) for c in candidates)
            reason = (f"libellé « {ge_label} » présent pour plusieurs kVA ({kvas}) et kVA du site "
                      f"{'inconnu' if ge_kva is None else f'{ge_kva} non trouvé'} : mappage ambigu")
            return None, reason, _match_info(M.MODELE_AMBIGU, motif=reason)
        m = exact[0]
    vc = m.validated_curve
    if m.match_status not in M.USABLE_MATCH_STATUSES or vc is None:
        detail = " ; ".join(m.match_reasons or []) or m.action_required or "validation requise"
        labels = {
            M.COURBE_CPH_MANQUANTE: "aucune courbe CPH dans l'abaque",
            M.MODELE_AMBIGU: "plusieurs courbes candidates — choix manuel requis",
            M.REJETE: "rejetée par un validateur",
        }
        reason = f"correspondance « {ge_label} » {labels.get(m.match_status, 'à valider')} ({m.match_status}) — {detail}"
        return None, reason, _match_info(m.match_status, m, motif=reason)
    return _curve_from_model(vc), None, _match_info(m.match_status, m, vc)


def _normalize_kind(value: str | None) -> str | None:
    v = (value or "").strip().upper()
    return v if v in ("INDOOR", "OUTDOOR") else None


def load_contexts(country: str | None = None, site_ids: list[str] | None = None, zone: str | None = None) -> list[E.SiteContext]:
    """Sites avec GE (SITE_ESCO_CURRENT.DG_COUNT > 0) — les sites sans GE sont exclus, pas comptés à 0 L."""
    from core.models import Site
    from fuel_tracking.models import CphInventoryMapping, FuelConsommationMonthly, FuelSiteInventory

    inv = FuelSiteInventory.objects.filter(dg_count__gt=0)
    if country:
        inv = inv.filter(country=country)
    if site_ids:
        inv = inv.filter(site_id__in=site_ids)
    by_site: dict[str, list] = defaultdict(list)
    for row in inv.order_by("site_id", "data_id"):
        by_site[row.site_id].append(row)
    if not by_site:
        return []

    core_sites = {
        s["site_id"]: s for s in Site.objects.filter(site_id__in=by_site.keys()).values(
            "site_id", "zone", "installed_site_type", "site_type")
    }
    if zone:
        by_site = {sid: rows for sid, rows in by_site.items() if (core_sites.get(sid) or {}).get("zone") == zone}

    # Dernière valeur renseignée par site (lignes mensuelles les plus récentes d'abord).
    ge_labels: dict[str, tuple] = {}
    extras: dict[str, dict] = defaultdict(dict)
    for row in (
        FuelConsommationMonthly.objects.filter(site_id__in=by_site.keys())
        .order_by("site_id", "-month_year")
        .values("site_id", "type_ge_fichier", "pge_kva_fichier", "facturation_avec_ge_fichier",
                "configuration_fichier", "site_type_fichier", "site_type")
    ):
        sid = row["site_id"]
        if row["type_ge_fichier"] is not None:
            ge_labels.setdefault(sid, (row["type_ge_fichier"], row["pge_kva_fichier"]))
        ex = extras[sid]
        for key, value in (("facture_avec_ge", row["facturation_avec_ge_fichier"]),
                           ("configuration", row["configuration_fichier"]),
                           ("site_type", row["site_type_fichier"] or row["site_type"])):
            if value is not None and value != "" and key not in ex:
                ex[key] = value

    mappings: dict[str, list] = defaultdict(list)
    for m in CphInventoryMapping.objects.select_related("validated_curve", "validated_by"):
        mappings[m.inventory_label_normalized].append(m)

    contexts = []
    for sid, rows in by_site.items():
        inv_row = rows[0]
        cs = core_sites.get(sid) or {}
        if cs.get("installed_site_type"):
            kind, kind_source = cs["installed_site_type"], "core.Site.installed_site_type"
        elif cs.get("site_type"):
            kind, kind_source = cs["site_type"], "core.Site.site_type"
        else:
            kind, kind_source = None, None
        ex = extras.get(sid, {})
        conf_file = _normalize_kind(ex.get("configuration"))
        if kind is None and conf_file:
            # Configuration déclarée par Ops (fichier ESCO facturation) quand l'inventaire est vide.
            kind, kind_source = conf_file, "FuelConsommationMonthly.configuration_fichier"
        ge_label, ge_kva = ge_labels.get(sid, (None, None))
        curve, curve_reason, match = resolve_curve(inv_row.dg_count, ge_label, ge_kva, mappings)
        ctx = E.SiteContext(
            site_id=sid, country=inv_row.country, data_id=inv_row.data_id, site_name=inv_row.site_name,
            zone=cs.get("zone"), kind=kind, kind_source=kind_source, grid_supply=inv_row.grid_supply,
            off_grid=E.is_off_grid(inv_row.grid_supply), dg_count=inv_row.dg_count,
            ge_label=ge_label, curve=curve, curve_reason=curve_reason, match=match,
            curve_code=None if curve is not None else CURVE_CODES.get(match["statut"], E.MC_MAPPING_A_VALIDER),
            ge_kva=ge_kva, facture_avec_ge=ex.get("facture_avec_ge"), site_type=ex.get("site_type"),
            configuration_fichier=ex.get("configuration"),
        )
        if len(rows) > 1:
            ctx.data_id = None
            ctx.data_issue = (
                f"plusieurs DATA_ID pour ce site ({', '.join(str(r.data_id) for r in rows)}) : "
                "rattachement des faits ambigu, calcul non effectué"
            )
        contexts.append(ctx)
    return contexts


def load_facts(contexts: list[E.SiteContext], start: date, end: date) -> dict[str, dict[date, dict]]:
    from fuel_tracking.models import FuelSiteDailyFacts

    keys = {(c.country, c.data_id): c.site_id for c in contexts if c.data_id is not None}
    if not keys:
        return {}
    fields = [f.name for f in FuelSiteDailyFacts._meta.get_fields() if f.name not in ("id", "synced_at")]
    out: dict[str, dict[date, dict]] = defaultdict(dict)
    qs = FuelSiteDailyFacts.objects.filter(
        data_id__in={k[1] for k in keys},
        date__gte=start - timedelta(days=E.AC_REFERENCE_LOOKBACK_DAYS), date__lte=end,
    ).values(*fields)
    for f in qs.iterator(chunk_size=5000):
        sid = keys.get((f["country"], f["data_id"]))
        if sid is not None:
            out[sid][f["date"]] = f
    return out


def load_observations(site_ids: list[str], start: date, end: date) -> dict[str, list[E.Observation]]:
    """Observations entièrement incluses dans la période ; pour une même fenêtre, le dernier import prime."""
    from fuel_tracking.models import FuelObservation

    latest: dict[tuple, object] = {}
    qs = (FuelObservation.objects.select_related("obs_import")
          .filter(site_id__in=site_ids, observation_start__gte=start, observation_end__lte=end)
          .order_by("obs_import__uploaded_at", "id"))
    for o in qs:
        latest[(o.site_id, o.observation_start, o.observation_end)] = o
    out: dict[str, list[E.Observation]] = defaultdict(list)
    for (sid, _, _), o in sorted(latest.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        out[sid].append(E.Observation(
            start=o.observation_start, end=o.observation_end,
            opening_fuel_l=o.opening_fuel_l, closing_fuel_l=o.closing_fuel_l,
            fuel_deliveries_l=o.fuel_deliveries_l, fuel_transfer_in_l=o.fuel_transfer_in_l,
            fuel_transfer_out_l=o.fuel_transfer_out_l, fuel_theft_l=o.fuel_theft_l, fuel_drain_l=o.fuel_drain_l,
            observation_status=o.observation_status, comment=o.comment, justificatif=o.justificatif,
            import_file=o.obs_import.file_name, import_id=o.obs_import_id,
        ))
    return out


def compute_period(start: date, end: date, country: str | None = None, site_ids: list[str] | None = None,
                   zone: str | None = None) -> list[dict]:
    if end < start:
        raise ValueError("La date de fin précède la date de début.")
    if (end - start).days + 1 > E.MAX_PERIOD_DAYS:
        raise ValueError(f"Période limitée à {E.MAX_PERIOD_DAYS} jours.")
    contexts = load_contexts(country=country, site_ids=site_ids, zone=zone)
    facts = load_facts(contexts, start, end)
    observations = load_observations([c.site_id for c in contexts], start, end)
    es = engine_settings()
    return [
        E.compute_site_period(ctx, facts.get(ctx.site_id, {}), start, end, observations.get(ctx.site_id, []), es)
        for ctx in contexts
    ]


# ─── Cache du calcul période (versionné par les données) ─────────────────────

CACHE_TTL_SECONDS = 600


def data_version() -> str:
    """
    Empreinte des données qui influencent le calcul : toute synchronisation, import
    d'abaque, changement de correspondance, import d'observations ou réglage change
    la clé, donc le cache n'est jamais servi périmé (TTL de sécurité : 10 min).
    """
    from django.db.models import Count, Max
    from fuel_tracking.models import (
        CphCurve, CphInventoryMapping, CphMappingHistory, FuelConsommationMonthly, FuelDailyFactsSyncRun,
        FuelObservationImport, FuelSiteInventory,
    )

    parts = [
        E.RULE_VERSION,
        FuelDailyFactsSyncRun.objects.aggregate(i=Max("id"), f=Max("finished_at")),
        FuelSiteInventory.objects.aggregate(s=Max("synced_at")),
        CphCurve.objects.aggregate(i=Max("imported_at"), n=Count("id")),
        CphInventoryMapping.objects.aggregate(m=Max("matched_at"), v=Max("validated_at"), n=Count("id")),
        CphMappingHistory.objects.aggregate(i=Max("id")),
        FuelObservationImport.objects.aggregate(i=Max("id")),
        FuelConsommationMonthly.objects.aggregate(u=Max("updated_at")),
        [str(getattr(dj_settings, k, "")) for k in (
            "FUEL_ENOC_DELIVERIES_CONNECTED", "FUEL_CPH_NOMINAL_POWER_FACTOR", "FUEL_CPH_SFC_MIN_L_KWH",
            "FUEL_CPH_SFC_MAX_L_KWH", "FUEL_CPH_MATCH_POWER_TOLERANCE", "FUEL_CPH_AC_POWER_TO_KW_DIVISOR")],
    ]
    return hashlib.sha1(repr(parts).encode()).hexdigest()


def compute_period_summary(start: date, end: date, country: str | None = None, zone: str | None = None) -> list[dict]:
    """
    compute_period sans le détail jour par jour (inutile au tableau), mis en cache.
    Cache indisponible (Redis en panne) → calcul direct : l'écran ne tombe jamais pour ça.
    """
    from django.core.cache import cache

    key = "fuel_cph:" + hashlib.sha1(repr((start, end, country, zone, data_version())).encode()).hexdigest()
    try:
        rows = cache.get(key)
    except Exception:
        logger.warning("[fuel_cph] cache indisponible, calcul direct", exc_info=True)
        rows = None
    if rows is not None:
        return rows
    rows = [{k: v for k, v in r.items() if k != "daily"} for r in compute_period(start, end, country=country, zone=zone)]
    try:
        cache.set(key, rows, CACHE_TTL_SECONDS)
    except Exception:
        logger.warning("[fuel_cph] écriture cache impossible", exc_info=True)
    return rows
