"""
Vues « BI » du Suivi carburant (graphiques, KPI, classements) et exports Excel — lecture seule.

  GET /api/fuel-tracking/bi/conso/?from_month=YYYY-MM&to_month=YYYY-MM&zone=&typology=
  GET /api/fuel-tracking/bi/stock/?zone=&month=YYYY-MM
  GET /api/fuel-tracking/bi/cph/?start=YYYY-MM-DD&end=YYYY-MM-DD&zone=
  GET /api/fuel-tracking/bi/export/<conso|stock|cph|commande|estimation>/?<mêmes filtres>   → .xlsx

Périmètre : Sénégal uniquement (demande métier 2026-11). Un site au pays non renseigné reste inclus
et est compté à part ; les sites d'un autre pays sont exclus.

Règles (CLAUDE.md) : périmètre « avec GE » identique aux autres écrans ; mesuré et estimé jamais
additionnés ; une valeur absente reste null (vide dans Excel, jamais 0) ; ratios = ratio des sommes.
Les écrans et les exports partagent les mêmes fonctions de calcul : un chiffre exporté est celui affiché.
Chaque indicateur est accompagné de son « explication » (formule, valeurs, sources, exclusions) :
le détail affiché au clic sur un KPI vient du même calcul, pas d'un texte figé.
"""
from __future__ import annotations

import calendar
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from io import BytesIO

from django.db.models import Count, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

TOP_N = 10
# En dessous, le ratio L/kWh d'un site est trop bruité pour un classement.
MIN_KWH_FOR_RATIO = 100
QUALITY_ORDER = ["OK", "LOW_QUALITY", "NO_VALID_LEVEL"]

SCOPE_LABEL = "Sénégal"
_SCOPE_RE = r"^\s*(s[eé]n[eé]gal|sn|sen)\s*$"
_SCOPE_PY = re.compile(_SCOPE_RE, re.I)

SRC_CAPTEUR = "Snowflake · VW_FUEL_REPORT : niveau de cuve, relevés QUALITY_STATUS = OK, au moins 2 points valides, chute de niveau détectée"
SRC_KWH = "Snowflake · GE_PROD_KWH : énergie produite par le groupe électrogène"
SRC_ENOC = "ENOC (MongoDB) : quantités ajoutées lors des ravitaillements, quantité > 0"
SRC_TARGET = "Targets carburant par site (Fuel Manager), targets actifs"
SRC_GE = "Périmètre GE : fichier Stan (facturation avec GE), à défaut Snowflake / ENOC / Base GE"
SRC_STOCK = ("Dernier relevé de stock de chaque site, le plus récent parmi : capteur Snowflake, relevés terrain / gardiennage / ENOC "
             "validés, stock final validé de la Synthèse (Closing)")
SRC_CPH = "Moteur CPH EnerTrack : CPH (L/h) de l'abaque du GE × heures de marche (Snowflake), selon la charge"


def scope_q(field: str = "country"):
    """Sénégal + pays non renseigné ; tout autre pays exclu."""
    return Q(**{f"{field}__iregex": _SCOPE_RE}) | Q(**{f"{field}__isnull": True}) | Q(**{field: ""})


def in_scope(country) -> bool:
    return not country or bool(_SCOPE_PY.match(str(country)))


def _f(v, places=1):
    return round(float(v), places) if v is not None else None


def _ratio(num, den, places=3):
    return round(float(num) / float(den), places) if num is not None and den not in (None, 0) else None


def _pct_change(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    return round((float(cur) - float(prev)) / float(prev) * 100, 1)


# Septembre 2026 est le début du suivi : aucune comparaison avec une période qui commence avant
# (pas d'écart « septembre vs août »). Les écarts commencent avec octobre comparé à septembre.
DEBUT_SUIVI = "2026-09"


def _comparable(prev_months: list[str]) -> list[str]:
    """Période précédente retenue seulement si elle est entièrement dans le suivi (sinon aucune comparaison)."""
    return prev_months if prev_months and min(prev_months) >= DEBUT_SUIVI else []


def _shift_month(month: str, delta: int) -> str:
    y, m = int(month[:4]), int(month[5:7])
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _days_in(month: str) -> int:
    return calendar.monthrange(int(month[:4]), int(month[5:7]))[1]


class _BadPeriod(ValueError):
    pass


def _v(label, valeur, unite=""):
    return {"label": label, "valeur": valeur, "unite": unite}


def _x(titre, formule, valeurs, sources, perimetre=None, exclusions=None, maj=None, note=None):
    """Explication d'un indicateur, affichée au clic : uniquement des valeurs issues du calcul."""
    return {"titre": titre, "formule": formule, "valeurs": valeurs, "sources": sources,
            "perimetre": perimetre or [], "exclusions": [e for e in (exclusions or []) if e["nb"]],
            "mise_a_jour": maj, "note": note}


def _last_sync(model):
    return model.objects.filter(status="SUCCESS").exclude(finished_at__isnull=True) \
        .order_by("-finished_at").values_list("finished_at", flat=True).first()


# ─── Consommation ────────────────────────────────────────────────────────────

def _conso_scope():
    from fuel_tracking.models import FuelConsommationMonthly

    return FuelConsommationMonthly.objects.filter(scope_q()).annotate(
        typo=Coalesce("typology_fichier", "typology", Value(None)))


def site_ids_for_typology(typology: str, from_month: str, to_month: str) -> set[str]:
    """Sites d'une typologie sur la période, avec le même vocabulaire que les graphiques de Suivi Conso
    (typologie du fichier, sinon Snowflake) : le filtre Typologie donne les mêmes sites partout sur la page."""
    if from_month > to_month:
        from_month, to_month = to_month, from_month
    qs = _conso_scope().filter(typo=typology, month_year__gte=from_month, month_year__lte=to_month)
    return {(s or "").upper() for s in qs.values_list("site_id", flat=True).distinct()}


def _totals(qs):
    both = Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)
    a = qs.aggregate(
        nb_sites=Count("site_id", distinct=True),
        nb_sites_mesure=Count("site_id", distinct=True, filter=Q(conso_snowflake_l__isnull=False)),
        nb_lignes_mesure=Count("id", filter=Q(conso_snowflake_l__isnull=False)),
        conso_mesuree_l=Sum("conso_snowflake_l"),
        # L/site/jour = conso ÷ jours-site réellement mesurés (pas jours calendaires) : n'invente pas de données.
        jours_site_mesures=Sum("nb_jours_data", filter=Q(conso_snowflake_l__isnull=False)),
        enoc_ajoutee_l=Sum("enoc_qte_ajoutee_l", filter=Q(enoc_qte_ajoutee_l__gt=0)),
        nb_sites_enoc=Count("site_id", distinct=True, filter=Q(enoc_qte_ajoutee_l__gt=0)),
        nb_ravitaillements=Sum("enoc_nb_demandes"),
        num=Sum("conso_snowflake_l", filter=both),
        den=Sum("ge_prod_kwh", filter=both),
        nb_sites_ratio=Count("site_id", distinct=True, filter=both),
    )
    return {
        "nb_sites": a["nb_sites"],
        "nb_sites_mesure": a["nb_sites_mesure"],
        "couverture_pct": round(a["nb_sites_mesure"] / a["nb_sites"] * 100, 1) if a["nb_sites"] else None,
        "conso_mesuree_l": _f(a["conso_mesuree_l"]),
        "l_site_jour": _ratio(a["conso_mesuree_l"], a["jours_site_mesures"], 1),
        "enoc_ajoutee_l": _f(a["enoc_ajoutee_l"]),
        "nb_ravitaillements": a["nb_ravitaillements"],
        "l_kwh": _ratio(a["num"], a["den"]),
        "_jours_site": a["jours_site_mesures"], "_lignes": a["nb_lignes_mesure"], "_num": _f(a["num"]),
        "_den": _f(a["den"]), "_nb_ratio": a["nb_sites_ratio"], "_nb_enoc": a["nb_sites_enoc"],
    }


def _public(t):
    return {k: v for k, v in t.items() if not k.startswith("_")}


def _breakdown(qs, field):
    both = Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)
    rows = (
        qs.values(key=F(field))
        .annotate(
            nb_sites=Count("site_id", distinct=True),
            conso_mesuree_l=Sum("conso_snowflake_l"),
            jours=Sum("nb_jours_data", filter=Q(conso_snowflake_l__isnull=False)),
            enoc_ajoutee_l=Sum("enoc_qte_ajoutee_l", filter=Q(enoc_qte_ajoutee_l__gt=0)),
            num=Sum("conso_snowflake_l", filter=both),
            den=Sum("ge_prod_kwh", filter=both),
        )
        .order_by(F("conso_mesuree_l").desc(nulls_last=True))
    )
    return [{
        "label": r["key"] or "Non renseigné", "nb_sites": r["nb_sites"],
        "conso_mesuree_l": _f(r["conso_mesuree_l"]), "l_site_jour": _ratio(r["conso_mesuree_l"], r["jours"], 1),
        "enoc_ajoutee_l": _f(r["enoc_ajoutee_l"]), "l_kwh": _ratio(r["num"], r["den"]),
    } for r in rows]


def _per_site(period, targets, tolerance):
    """Agrégat par site sur la période — base des classements ET de la feuille « Sites » de l'export."""
    both = Q(conso_snowflake_l__isnull=False, ge_prod_kwh__isnull=False)
    rows = period.values("site_id").annotate(
        conso=Sum("conso_snowflake_l"), enoc=Sum("enoc_qte_ajoutee_l", filter=Q(enoc_qte_ajoutee_l__gt=0)),
        nb_ravit=Sum("enoc_nb_demandes"), jours=Sum("nb_jours_data", filter=Q(conso_snowflake_l__isnull=False)),
        num=Sum("conso_snowflake_l", filter=both), den=Sum("ge_prod_kwh", filter=both),
        kwh=Sum("ge_prod_kwh"), nb_mois=Count("id", filter=Q(conso_snowflake_l__isnull=False)),
    )
    info = {r["site_id"]: r for r in period.order_by("month_year").values("site_id", "site_name", "zone_fichier", "typo")}
    out = []
    for r in rows:
        i = info.get(r["site_id"], {})
        t = targets.get((r["site_id"] or "").upper())
        # Target comparé seulement sur les mois où la conso est mesurée (target mensuel × mois mesurés).
        target_l = float(t.target_mensuel_l) * r["nb_mois"] if t is not None and r["nb_mois"] else None
        conso = float(r["conso"]) if r["conso"] is not None else None
        ecart_t = round((conso - target_l) / target_l * 100, 1) if conso is not None and target_l else None
        out.append({
            "site_id": r["site_id"], "site_name": i.get("site_name"), "zone": i.get("zone_fichier"), "typologie": i.get("typo"),
            "nb_mois_mesures": r["nb_mois"], "jours_mesures": r["jours"],
            "conso_mesuree_l": _f(conso), "l_site_jour": _ratio(r["conso"], r["jours"], 1),
            "enoc_ajoutee_l": _f(r["enoc"]), "nb_ravitaillements": r["nb_ravit"],
            "ecart_enoc_l": _f(conso - float(r["enoc"])) if conso is not None and r["enoc"] is not None else None,
            "ge_prod_kwh": _f(r["kwh"]), "l_kwh": _ratio(r["num"], r["den"]) if r["den"] and float(r["den"]) >= MIN_KWH_FOR_RATIO else None,
            "target_l": _f(target_l), "ecart_target_pct": ecart_t,
            "statut_target": None if ecart_t is None else ("Au-dessus" if ecart_t > tolerance else "Sous" if ecart_t < -tolerance else "Dans la tolérance"),
            "target_confirme": bool(t is not None and t.confirmed_year),
        })
    return out


def _pareto(sites):
    """Courbe de concentration : part cumulée de la conso selon la part des sites (classés du plus gros au plus petit)."""
    vals = sorted((s["conso_mesuree_l"] for s in sites if s["conso_mesuree_l"] and s["conso_mesuree_l"] > 0), reverse=True)
    total, n = sum(vals), len(vals)
    if not n or total <= 0:
        return {"points": [], "nb_sites": 0, "pct_sites_50": None, "pct_sites_80": None}
    points, cum, p50, p80 = [{"pct_sites": 0.0, "pct_conso": 0.0}], 0.0, None, None
    step = max(1, n // 50)
    for i, v in enumerate(vals, 1):
        cum += v
        share = cum / total * 100
        if p50 is None and share >= 50:
            p50 = round(i / n * 100, 1)
        if p80 is None and share >= 80:
            p80 = round(i / n * 100, 1)
        if i % step == 0 or i == n:
            points.append({"pct_sites": round(i / n * 100, 1), "pct_conso": round(share, 1)})
    return {"points": points, "nb_sites": n, "pct_sites_50": p50, "pct_sites_80": p80}


def _daily(site_ids, months):
    """Conso mesurée journalière (Snowflake VW_FUEL_REPORT, faits journaliers) des sites de la période."""
    from fuel_tracking.models import FuelSiteDailyFacts

    if not months or not site_ids:
        return []
    d_from = date(int(months[0][:4]), int(months[0][5:7]), 1)
    d_to = date(int(months[-1][:4]), int(months[-1][5:7]), _days_in(months[-1]))
    rows = (FuelSiteDailyFacts.objects.filter(scope_q(), site_id__in=site_ids, date__gte=d_from, date__lte=d_to,
                                              measured_conso_l__isnull=False)
            .values("date").annotate(l=Sum("measured_conso_l"), n=Count("site_id", distinct=True)).order_by("date"))
    return [{"date": r["date"], "conso_mesuree_l": _f(r["l"]), "nb_sites": r["n"],
             "l_site": _ratio(r["l"], r["n"], 1)} for r in rows]


def compute_conso(params) -> dict:
    from fuel_tracking.models import FuelConsommationSyncRun, FuelEnocSyncRun
    from fuel_tracking.services.fuel_target import active_targets, tolerance_pct
    from fuel_tracking.views import _effective_ge_q, _file_ge_site_ids

    scoped = _conso_scope()
    ge_q = _effective_ge_q(_file_ge_site_ids())
    base = scoped.filter(ge_q)
    all_months = list(base.order_by("month_year").values_list("month_year", flat=True).distinct())
    zones = sorted({z for z in base.values_list("zone_fichier", flat=True).distinct() if z})
    typologies = sorted({t for t in base.values_list("typo", flat=True).distinct() if t})
    if not all_months:
        return {"perimetre": SCOPE_LABEL, "months": [], "available_months": [], "filters": {"zones": [], "typologies": []}, "sites": []}

    to_month = (params.get("to_month") or "").strip() or all_months[-1]
    from_month = (params.get("from_month") or "").strip() or _shift_month(to_month, -5)
    if from_month > to_month:
        from_month, to_month = to_month, from_month
    # Filtre « un seul mois » : on n'affiche que ce mois, sans comparaison à une période précédente.
    if (params.get("month") or "").strip():
        from_month = to_month = params["month"].strip()
    if params.get("zone"):
        base = base.filter(zone_fichier=params["zone"])
    if params.get("typology"):
        base = base.filter(typo=params["typology"])

    months = [m for m in all_months if from_month <= m <= to_month]
    span = len(months) or 1
    period = base.filter(month_year__in=months)
    single = from_month == to_month
    # Fenêtre précédente de même longueur, uniquement si elle commence au début du suivi ou après.
    prev_start = _shift_month(from_month, -span)
    prev_months = [] if single or prev_start < DEBUT_SUIVI else [m for m in all_months if prev_start <= m < from_month]
    prev = base.filter(month_year__in=prev_months)

    targets, tolerance = active_targets(), tolerance_pct()
    monthly = []
    for m in months:
        mq = period.filter(month_year=m)
        t = _totals(mq)
        # Target du mois : sites ayant à la fois une conso mesurée et un target actif.
        t_sum = c_sum = 0.0
        for sid, c in mq.filter(conso_snowflake_l__isnull=False).values_list("site_id", "conso_snowflake_l"):
            tg = targets.get((sid or "").upper())
            if tg is not None:
                t_sum += float(tg.target_mensuel_l)
                c_sum += float(c)
        q = dict(mq.values_list("quality_status").annotate(n=Count("id")))
        quality = {k: q.pop(k, 0) for k in QUALITY_ORDER}
        quality["Autre / absent"] = sum(q.values())
        monthly.append({"month": m, **_public(t), "target_l": _f(t_sum) if t_sum else None,
                        "conso_vs_target_l": _f(c_sum) if t_sum else None, "qualite": quality})

    cur = _totals(period)
    prev_t = _totals(prev) if prev_months else None
    deltas = {k: _pct_change(cur[k], prev_t[k]) if prev_t else None
              for k in ("conso_mesuree_l", "enoc_ajoutee_l", "l_kwh", "l_site_jour")}
    if prev_t and cur["couverture_pct"] is not None and prev_t["couverture_pct"] is not None:
        deltas["couverture_pts"] = round(cur["couverture_pct"] - prev_t["couverture_pct"], 1)

    sites = _per_site(period, targets, tolerance)
    with_target = [s for s in sites if s["ecart_target_pct"] is not None]
    t_tot = sum(s["target_l"] for s in with_target)
    c_tot = sum(s["conso_mesuree_l"] for s in with_target)
    kpis = _public(cur)
    kpis.update({
        "nb_sites_target": len(with_target),
        "ecart_target_pct": _pct_change(c_tot, t_tot) if with_target else None,
        "nb_sites_au_dessus_target": sum(1 for s in with_target if s["statut_target"] == "Au-dessus"),
        "nb_targets_a_confirmer": sum(1 for s in with_target if not s["target_confirme"]),
        "tolerance_target_pct": tolerance,
    })

    ratios = [s for s in sites if s["l_kwh"] is not None]
    buckets = [("< 0,20", None, 0.20), ("0,20–0,25", 0.20, 0.25), ("0,25–0,30", 0.25, 0.30),
               ("0,30–0,35", 0.30, 0.35), ("0,35–0,45", 0.35, 0.45), ("> 0,45", 0.45, None)]
    dist = [{"label": label, "nb_sites": sum(1 for s in ratios if (lo is None or s["l_kwh"] >= lo) and (hi is None or s["l_kwh"] < hi)),
             "reference": lo == 0.25} for label, lo, hi in buckets]

    # Matrice zone × mois (L / site / jour mesuré).
    by_zone = _breakdown(period, "zone_fichier")
    cells = [{"zone": r["zone_fichier"] or "Non renseigné", "month": r["month_year"], "nb_sites": r["n"],
              "l_site_jour": _ratio(r["c"], r["j"], 1)}
             for r in period.filter(conso_snowflake_l__isnull=False).values("zone_fichier", "month_year")
             .annotate(c=Sum("conso_snowflake_l"), j=Sum("nb_jours_data"), n=Count("site_id", distinct=True))]
    pareto = _pareto(sites)

    # Explications (clic sur un indicateur).
    no_ge = scoped.filter(month_year__in=months).exclude(ge_q).values("site_id").distinct().count()
    no_country = period.filter(Q(country__isnull=True) | Q(country="")).values("site_id").distinct().count()
    sans_mesure = cur["nb_sites"] - cur["nb_sites_mesure"]
    maj_conso, maj_enoc = _last_sync(FuelConsommationSyncRun), _last_sync(FuelEnocSyncRun)
    per = [f"{SCOPE_LABEL}, sites avec groupe électrogène",
           f"Mois : {from_month}" if single else f"Période : {months[0] if months else from_month} → {months[-1] if months else to_month} ({len(months)} mois)"]
    if params.get("zone"):
        per.append(f"Zone : {params['zone']}")
    if params.get("typology"):
        per.append(f"Typologie : {params['typology']}")
    if no_country:
        per.append(f"Dont {no_country} sites au pays non renseigné (inclus)")
    excl_ge = {"label": "Sites sans groupe électrogène (Solar / Grid), exclus : ils ne consomment pas de gasoil", "nb": no_ge}
    explain = {
        "conso_mesuree_l": _x("Conso mesurée", "Somme des consommations mesurées par capteur, site par site et mois par mois.", [
            _v("Total", kpis["conso_mesuree_l"], "L"), _v("Sites mesurés", cur["nb_sites_mesure"]),
            _v("Lignes site × mois additionnées", cur["_lignes"]),
            *([_v("Période précédente", prev_t["conso_mesuree_l"] if prev_t else None, "L")] if not single else []),
        ], [SRC_CAPTEUR, SRC_GE], per, [excl_ge, {"label": "Sites GE sans mesure capteur : non comptés (pas « 0 L »)", "nb": sans_mesure}], maj_conso),
        "l_site_jour": _x("Moyenne par site et par jour", "Conso mesurée ÷ nombre de jours-site réellement mesurés.", [
            _v("Conso mesurée", kpis["conso_mesuree_l"], "L"), _v("Jours-site mesurés", cur["_jours_site"]),
            _v("Résultat", kpis["l_site_jour"], "L / site / jour"),
        ], [SRC_CAPTEUR], per, [excl_ge, {"label": "Sites GE sans mesure : ni au numérateur ni au dénominateur", "nb": sans_mesure}], maj_conso,
            "Les jours sans relevé ne sont pas comptés comme des jours à 0 L : la moyenne n'est pas diluée par les pannes capteur."),
        "l_kwh": _x("Rendement GE", "Litres mesurés ÷ kWh produits par le GE, sur les sites ayant les deux valeurs (ratio des sommes).", [
            _v("Litres (sites avec kWh)", cur["_num"], "L"), _v("kWh produits", cur["_den"], "kWh"),
            _v("Sites pris en compte", cur["_nb_ratio"]), _v("Résultat", kpis["l_kwh"], "L/kWh"), _v("Norme d'un diesel bien chargé", "0,25 – 0,30", "L/kWh"),
        ], [SRC_CAPTEUR, SRC_KWH], per, [excl_ge, {"label": "Sites sans kWh ou sans litres mesurés", "nb": cur["nb_sites"] - (cur["_nb_ratio"] or 0)}], maj_conso,
            "Au-dessus de 0,30 L/kWh : GE sous-chargé, fuite, vol ou capteur à contrôler."),
        "ecart_target_pct": _x("Écart au target", "(Conso mesurée − target) ÷ target, sur les sites ayant un target actif ; target = target mensuel × mois mesurés.", [
            _v("Conso des sites avec target", _f(c_tot) if with_target else None, "L"), _v("Target cumulé", _f(t_tot) if with_target else None, "L"),
            _v("Sites comparés", len(with_target)), _v("Au-dessus de la tolérance", kpis["nb_sites_au_dessus_target"]),
            _v("Tolérance", tolerance, "%"), _v("Targets non confirmés par le Fuel Manager", kpis["nb_targets_a_confirmer"]),
        ], [SRC_CAPTEUR, SRC_TARGET], per, [{"label": "Sites mesurés sans target actif : non comparés", "nb": cur["nb_sites_mesure"] - len(with_target)}], maj_conso),
        "enoc_ajoutee_l": _x("Livré ENOC", "Somme des litres ajoutés lors des ravitaillements enregistrés dans ENOC.", [
            _v("Total livré", kpis["enoc_ajoutee_l"], "L"), _v("Demandes de ravitaillement", kpis["nb_ravitaillements"]),
            _v("Sites ravitaillés", cur["_nb_enoc"]),
        ], [SRC_ENOC], per, [excl_ge], maj_enoc, "Non additionné à la conso mesurée : ce sont deux sources distinctes, comparées site par site."),
        "couverture_pct": _x("Sites mesurés", "Sites GE ayant au moins un mois de conso mesurée ÷ sites GE de la période.", [
            _v("Sites GE", cur["nb_sites"]), _v("Sites mesurés", cur["nb_sites_mesure"]), _v("Résultat", kpis["couverture_pct"], "%"),
        ], [SRC_CAPTEUR, SRC_GE], per, [excl_ge], maj_conso,
            "Filtre strict : un site compte comme mesuré seulement si une chute de niveau a été détectée."),
    }

    def top(rows, key, reverse=True):
        return sorted(rows, key=key, reverse=reverse)[:TOP_N]

    return {
        "perimetre": SCOPE_LABEL, "mois_unique": single,
        "from_month": months[0] if months else from_month, "to_month": months[-1] if months else to_month,
        "months": months, "available_months": all_months, "previous_months": prev_months,
        "filters": {"zones": zones, "typologies": typologies},
        "kpis": kpis, "deltas": deltas, "explain": explain, "monthly": monthly,
        "daily": _daily({s["site_id"] for s in sites}, months),
        "by_zone": by_zone, "by_typology": _breakdown(period, "typo"),
        "zone_month": {"zones": [b["label"] for b in by_zone], "months": months, "cells": cells},
        "pareto": pareto,
        "top_conso": top([s for s in sites if s["conso_mesuree_l"] is not None], lambda s: s["conso_mesuree_l"]),
        "top_target": top([s for s in with_target if s["statut_target"] == "Au-dessus"], lambda s: s["ecart_target_pct"]),
        "top_ecart_enoc": top([s for s in sites if s["ecart_enoc_l"] is not None], lambda s: abs(s["ecart_enoc_l"])),
        "top_l_kwh": top(ratios, lambda s: s["l_kwh"]),
        "distribution_l_kwh": dist, "reference_l_kwh": [0.25, 0.30],
        "sites": sites,
    }


SOURCES = ("variation", "closing", "instrumentee", "cph_complete")
SRC_TITRES = {"variation": "Variation de stock", "closing": "Consommation Closing",
              "instrumentee": "Consommation Snowflake", "cph_complete": "CPH complète"}
SRC_ORIGINES = {
    "variation": ["Bilans de stock EnerTrack : relevés de stock (Closing, gardiennage, terrain) et mouvements ENOC"],
    "closing": ["Fichier Closing importé (consommation déclarée par site)"],
    "instrumentee": [SRC_CAPTEUR + " (faits journaliers)"],
    "cph_complete": [SRC_CPH],
}
MAX_SOURCES_DAYS = 93


def _month_bounds(from_month: str, to_month: str):
    d_from = date(int(from_month[:4]), int(from_month[5:7]), 1)
    d_to = date(int(to_month[:4]), int(to_month[5:7]), _days_in(to_month))
    yesterday = timezone.localdate() - timedelta(days=1)
    return d_from, min(d_to, yesterday)


def compute_conso_sources(params) -> dict:
    """
    Les 4 mesures de la consommation sur la période exacte, reprises du tableau « Consommations »
    (services.conso_comparison) — mêmes règles, mêmes chiffres — limitées au Sénégal et aux sites du fichier Closing.
    Jamais additionnées entre elles : chacune a son propre périmètre de sites.
    """
    from fuel_tracking.services import conso_comparison as C

    to_month = (params.get("month") or params.get("to_month") or "").strip()
    from_month = (params.get("month") or params.get("from_month") or "").strip() or to_month
    if not to_month:
        raise _BadPeriod("Mois manquant.")
    if from_month > to_month:
        from_month, to_month = to_month, from_month
    d_from, d_to = _month_bounds(from_month, to_month)
    out = {"perimetre": SCOPE_LABEL, "debut": d_from, "fin": d_to, "disponible": False, "motif": None, "kpis": {}, "explain": {}}
    if d_to < d_from:
        out["motif"] = "Période dans le futur : aucune donnée."
        return out
    if (d_to - d_from).days + 1 > MAX_SOURCES_DAYS:
        out["motif"] = "Choisissez au plus 3 mois (ou un seul mois) : le calcul CPH et les bilans de stock sont faits sur une période courte."
        return out
    zone = (params.get("zone") or "").strip()
    site = (params.get("site") or "").strip().upper()
    data = C.build(d_from, d_to, {"zone": zone} if zone else {}, with_cph_quality=False)
    typology = (params.get("typology") or "").strip()
    if typology:
        ids = site_ids_for_typology(typology, from_month, to_month)
        data["rows"] = [r for r in data["rows"] if (r["site_id"] or "").upper() in ids]
    if site:
        # Fiche site : le site seul, même hors fichier Closing (signalé), jamais un autre site.
        data["rows"] = [r for r in data["rows"] if (r["site_id"] or "").upper() == site]
        row = data["rows"][0] if data["rows"] else None
        out["site"] = None if row is None else {
            "stock": {k: row["stock"][k] for k in ("initial_l", "source_initial", "entrees_l", "final_l", "source_final", "variation_l", "statut", "motif", "action")},
            "closing": {"conso_l": row["closing"]["conso_l"]},
            "instrumentee": {k: row["instrumentee"][k] for k in ("conso_l", "jours_mesure", "jours", "couverture_pct", "disponible")},
            "cph": {k: row["cph"][k] for k in ("complete_l", "partielle_l", "statut", "runtime_h", "cph_l_h", "charge_pct", "motif_code")},
            "target": {k: row["target"][k] for k in ("periode_l", "mensuel_l", "statut")},
            "modele_ge": row.get("modele_ge"), "puissance_nominale_kva": row.get("puissance_nominale_kva"),
            "correspondance": row["correspondance"],
        }
        rows = [r for r in data["rows"] if in_scope(r.get("country"))]
    else:
        rows = [r for r in data["rows"] if r["correspondance"]["utilise"] and in_scope(r.get("country"))]
    k = C._kpis(rows)
    per = [f"Site {site}" if site else f"{SCOPE_LABEL}, sites du fichier Closing (base de référence)", f"Période exacte : {d_from:%d/%m/%Y} → {d_to:%d/%m/%Y}"]
    if zone:
        per.append(f"Zone : {zone}")
    if typology:
        per.append(f"Typologie : {typology}")
    hors_base = sum(1 for r in data["rows"] if not r["correspondance"]["utilise"] and in_scope(r.get("country")))
    autres = sum(1 for r in data["rows"] if not in_scope(r.get("country")))
    fr = data.get("fraicheur") or {}
    for key in SOURCES:
        v = k[key]
        excl = [] if site else [
            {"label": "Sites sans valeur pour cette mesure : non comptés (pas « 0 L »)", "nb": k["sites"] - v["sites"]},
            {"label": "Sites hors du fichier Closing : affichés dans le détail, exclus des totaux", "nb": hors_base},
            {"label": "Sites d'un autre pays, exclus", "nb": autres}]
        vals = [_v("Total", v["total_l"], "L"), _v("Sites pris en compte", v["sites"]), _v("Sites du périmètre", k["sites"])]
        note = None
        if key == "variation":
            vals.append(_v("Variations négatives (anomalies)", v.get("negatives")))
            if not data["periode"]["sites_avec_bilan"]:
                note = "Aucun bilan de stock n'est calculé sur cette période exacte : la variation de stock est non calculable."
        if key in ("instrumentee", "cph_complete") and fr.get("periode_incomplete"):
            note = f"{fr.get('jours_non_couverts')} jour(s) de la période ne sont pas encore couverts par Snowflake (données jusqu'au {fr.get('snowflake_derniere_date') or '—'})."
        if key == "cph_complete":
            vals.append(_v("Sites en CPH partielle (non additionnés)", k["cph_partielle"]["sites"]))
        if site and v["total_l"] is None:
            note = (note + " " if note else "") + "Pas de valeur pour ce site sur la période : non calculable (pas « 0 L »)."
        out["kpis"][key] = {"total_l": v["total_l"], "sites": v["sites"]}
        out["explain"][key] = _x(SRC_TITRES[key], C.HELP[key], vals, SRC_ORIGINES[key], per, excl, None, note)
    out.update({"disponible": True, "sites": k["sites"], "sites_avec_bilan": data["periode"]["sites_avec_bilan"], "fraicheur": fr})
    return out


class BiConsoSourcesView(APIView):
    """GET bi/conso/sources/?month=YYYY-MM | from_month&to_month, zone= — variation de stock, Closing, instrumentée, CPH complète."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            return Response(compute_conso_sources(request.query_params))
        except (_BadPeriod, ValueError) as e:
            return Response({"detail": str(e)}, status=400)
        except Exception as e:  # source indisponible : « indisponible », jamais 0
            return Response({"detail": f"Mesures indisponibles ({type(e).__name__})."}, status=503)


# ─── Fiche site ──────────────────────────────────────────────────────────────

def search_sites(q: str, limit: int = 20) -> list[dict]:
    """Sites du Sénégal dont l'identifiant ou le nom contient q ; identifiant exact puis début d'identifiant en premier."""
    q = (q or "").strip()
    if len(q) < 2:
        return []
    seen = {}
    for r in (_conso_scope().filter(Q(site_id__icontains=q) | Q(site_name__icontains=q)).order_by("-month_year")
              .values("site_id", "site_name", "zone_fichier", "typo", "has_genset")[:2000]):
        if r["site_id"] not in seen:
            seen[r["site_id"]] = {"site_id": r["site_id"], "site_name": r["site_name"], "zone": r["zone_fichier"],
                                  "typologie": r["typo"], "has_genset": r["has_genset"]}
    ql = q.lower()
    rank = lambda s: (s["site_id"].lower() != ql, not s["site_id"].lower().startswith(ql), s["site_id"])  # noqa: E731
    return sorted(seen.values(), key=rank)[:limit]


class BiSiteSearchView(APIView):
    """GET bi/sites/?q= — recherche d'un site (identifiant ou nom), Sénégal."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"results": search_sites(request.query_params.get("q", ""))})


class _SiteNotFound(Exception):
    pass


def compute_site(site_id: str, params) -> dict:
    """Toutes les données d'un site sur la période : identité, indicateurs, mois, jours, stock, ravitaillements ENOC.
    Mêmes règles que les vues globales (mesuré jamais additionné à l'estimé, null ≠ 0)."""
    from fuel_tracking.models import FuelConsommationSyncRun, FuelEnocMovement, FuelEnocSyncRun, FuelSiteDailyFacts, FuelStockSnapshot, FuelStockSyncRun
    from fuel_tracking.services.fuel_target import active_targets, tolerance_pct
    from fuel_tracking.views import _effective_ge_q, _file_ge_site_ids

    sid = (site_id or "").strip()
    rows = _conso_scope().filter(site_id__iexact=sid)
    last = rows.order_by("-month_year").values("site_id", "site_name", "zone_fichier", "typo", "country", "power_supply", "dg_count").first()
    if last is None:
        raise _SiteNotFound(sid)
    sid = last["site_id"]
    has_ge = rows.filter(_effective_ge_q(_file_ge_site_ids())).exists()

    all_months = list(rows.order_by("month_year").values_list("month_year", flat=True).distinct())
    to_month = (params.get("to_month") or "").strip() or all_months[-1]
    from_month = (params.get("from_month") or "").strip() or _shift_month(to_month, -5)
    if from_month > to_month:
        from_month, to_month = to_month, from_month
    if (params.get("month") or "").strip():
        from_month = to_month = params["month"].strip()
    single = from_month == to_month
    span = (int(to_month[:4]) * 12 + int(to_month[5:7])) - (int(from_month[:4]) * 12 + int(from_month[5:7])) + 1
    months = [_shift_month(from_month, i) for i in range(span)]
    prev_months = [] if single else _comparable([_shift_month(from_month, -span + i) for i in range(span)])

    targets, tolerance = active_targets(), tolerance_pct()
    tgt = targets.get(sid.upper())
    cur_rows = _per_site(rows.filter(month_year__in=months), targets, tolerance)
    prev_rows = _per_site(rows.filter(month_year__in=prev_months), targets, tolerance) if prev_months else []
    cur = cur_rows[0] if cur_rows else None
    prev = prev_rows[0] if prev_rows else None
    keys = ("conso_mesuree_l", "l_site_jour", "enoc_ajoutee_l", "l_kwh", "ge_prod_kwh", "nb_ravitaillements", "target_l",
            "ecart_target_pct", "statut_target", "ecart_enoc_l", "jours_mesures", "nb_mois_mesures")
    kpis = {k: (cur or {}).get(k) for k in keys}
    deltas = {k: _pct_change(kpis[k], prev.get(k)) if prev else None for k in ("conso_mesuree_l", "l_site_jour", "enoc_ajoutee_l", "l_kwh")}

    by_month = {r["month_year"]: r for r in rows.filter(month_year__in=months).values(
        "month_year", "conso_snowflake_l", "nb_jours_data", "enoc_qte_ajoutee_l", "enoc_nb_demandes", "ge_prod_kwh", "quality_status")}
    monthly = []
    for m in months:
        r = by_month.get(m)
        c = r and r["conso_snowflake_l"]
        monthly.append({
            "month": m, "present": r is not None,
            "conso_mesuree_l": _f(c), "jours_mesures": r["nb_jours_data"] if r and c is not None else None,
            "l_jour": _ratio(c, r["nb_jours_data"], 1) if r and c is not None and r["nb_jours_data"] else None,
            "enoc_ajoutee_l": _f(r["enoc_qte_ajoutee_l"]) if r and r["enoc_qte_ajoutee_l"] and r["enoc_qte_ajoutee_l"] > 0 else None,
            "nb_ravitaillements": r["enoc_nb_demandes"] if r else None,
            "ge_prod_kwh": _f(r["ge_prod_kwh"]) if r else None,
            "l_kwh": _ratio(c, r["ge_prod_kwh"]) if r and c is not None and r["ge_prod_kwh"] and float(r["ge_prod_kwh"]) >= MIN_KWH_FOR_RATIO else None,
            "qualite": r["quality_status"] if r else None,
            "target_l": _f(tgt.target_mensuel_l) if tgt is not None else None,
        })

    d_from = date(int(months[0][:4]), int(months[0][5:7]), 1)
    d_to = date(int(months[-1][:4]), int(months[-1][5:7]), _days_in(months[-1]))
    daily = [{"date": r["date"], "conso_mesuree_l": _f(r["measured_conso_l"]), "runtime_h": _f(r["dg_on_runtime_h"], 2),
              "ge_kwh": _f(r["dg_production_kwh"]), "points": r["fuel_raw_points"]}
             for r in FuelSiteDailyFacts.objects.filter(site_id__iexact=sid, date__gte=d_from, date__lte=d_to).order_by("date")
             .values("date", "measured_conso_l", "dg_on_runtime_h", "dg_production_kwh", "fuel_raw_points")]

    from fuel_tracking.services.current_stock import current_stock

    st = FuelStockSnapshot.objects.filter(site_id__iexact=sid, snapshot_year__isnull=True).values(
        "site_id", "stock_snowflake_l", "capacity_snowflake_l", "stock_snowflake_date", "stock_enoc_l", "stock_enoc_date").first()
    ref_row = next((m for m in reversed(monthly) if m["l_jour"] is not None), None)
    stock = None
    if st:
        today = timezone.localdate()
        c = current_stock([st]).get(st["site_id"]) or {}
        sl, cap = c.get("stock_l"), c.get("capacite_l")
        stock = {"stock_l": _f(sl), "capacite_l": _f(cap), "remplissage_pct": _f(sl / cap * 100) if sl is not None and cap else None,
                 "date": c.get("date"), "age_releve_j": (today - c["date"]).days if c.get("date") else None,
                 "source_stock": c.get("source"), "source_stock_label": c.get("source_label"), "stock_estime": bool(c.get("estime")),
                 "origine_stock": c.get("origine"),
                 "stock_enoc_l": _f(st["stock_enoc_l"]), "date_enoc": st["stock_enoc_date"],
                 "conso_jour_l": ref_row["l_jour"] if ref_row else None, "mois_reference": ref_row["month"] if ref_row else None,
                 "autonomie_j": round(float(sl) / ref_row["l_jour"], 1) if ref_row and ref_row["l_jour"] and sl is not None else None}

    enoc = [{"date": r["operation_date"], "type": r["operation_type"], "statut": r["status"], "demande_l": _f(r["requested_quantity_liters"]),
             "approuve_l": _f(r["approved_quantity_liters"]), "ajoute_l": _f(r["quantity_added_liters"]),
             "niveau_avant": _f(r["level_before"]), "niveau_apres": _f(r["level_after"]), "unite": r["level_after_unit"] or r["level_before_unit"]}
            for r in FuelEnocMovement.objects.filter(site_id__iexact=sid, operation_date__date__gte=d_from, operation_date__date__lte=d_to)
            .order_by("-operation_date").values("operation_date", "operation_type", "status", "requested_quantity_liters", "approved_quantity_liters",
                                                "quantity_added_liters", "level_before", "level_after", "level_before_unit", "level_after_unit")[:200]]

    maj_conso = _last_sync(FuelConsommationSyncRun)
    per = [f"Site {sid}", f"Mois : {from_month}" if single else f"Période : {from_month} → {to_month} ({span} mois)"]
    j = kpis["jours_mesures"]
    explain = {
        "conso_mesuree_l": _x("Conso mesurée du site", "Somme des consommations mensuelles mesurées par capteur sur la période.", [
            _v("Total", kpis["conso_mesuree_l"], "L"), _v("Mois mesurés", kpis["nb_mois_mesures"]), _v("Jours mesurés", j),
            *([_v("Période précédente", prev.get("conso_mesuree_l"), "L")] if prev else []),
        ], [SRC_CAPTEUR], per, [], maj_conso, None if kpis["conso_mesuree_l"] is not None else "Aucune mesure capteur sur la période : non calculable (pas « 0 L »)."),
        "l_site_jour": _x("Moyenne par jour", "Conso mesurée ÷ jours réellement mesurés.", [
            _v("Conso mesurée", kpis["conso_mesuree_l"], "L"), _v("Jours mesurés", j), _v("Résultat", kpis["l_site_jour"], "L / jour"),
        ], [SRC_CAPTEUR], per, [], maj_conso, "Les jours sans relevé ne comptent pas comme des jours à 0 L."),
        "l_kwh": _x("Rendement GE", "Litres mesurés ÷ kWh produits par le GE (au moins 100 kWh sur la période).", [
            _v("kWh produits", kpis["ge_prod_kwh"], "kWh"), _v("Résultat", kpis["l_kwh"], "L/kWh"), _v("Norme d'un diesel bien chargé", "0,25 – 0,30", "L/kWh"),
        ], [SRC_CAPTEUR, SRC_KWH], per, [], maj_conso, "Au-dessus de 0,30 L/kWh : GE sous-chargé, fuite, vol ou capteur à contrôler."),
        "enoc_ajoutee_l": _x("Livré ENOC", "Somme des litres ajoutés lors des ravitaillements ENOC du site.", [
            _v("Total livré", kpis["enoc_ajoutee_l"], "L"), _v("Demandes", kpis["nb_ravitaillements"]),
            _v("Écart conso mesurée − livré", kpis["ecart_enoc_l"], "L"),
        ], [SRC_ENOC], per, [], _last_sync(FuelEnocSyncRun),
            "Non additionné à la conso mesurée : un écart durable se vérifie dans la liste des ravitaillements."),
        "ecart_target_pct": _x("Écart au target", "(Conso mesurée − target) ÷ target ; target = target mensuel × mois mesurés.", [
            _v("Conso mesurée", kpis["conso_mesuree_l"], "L"), _v("Target mensuel", _f(tgt.target_mensuel_l) if tgt is not None else None, "L"),
            _v("Target de la période", kpis["target_l"], "L"), _v("Tolérance", tolerance, "%"),
            _v("Target confirmé par le Fuel Manager", "Oui" if tgt is not None and tgt.confirmed_year else "Non"),
        ], [SRC_CAPTEUR, SRC_TARGET], per, [], maj_conso, None if tgt is not None else "Pas de target actif pour ce site : écart non calculable."),
    }
    if stock:
        explain["stock_l"] = _x("Stock en cuve", "Relevé de stock le plus récent du site (capteur, terrain ou Synthèse).", [
            _v("Stock", stock["stock_l"], "L"), _v("Source", stock["source_stock_label"]), _v("Origine selon la Synthèse", stock["origine_stock"]), _v("Capacité", stock["capacite_l"], "L"), _v("Remplissage", stock["remplissage_pct"], "%"),
            _v("Date du relevé", stock["date"]), _v("Stock déclaré ENOC", stock["stock_enoc_l"], "L"),
        ], [SRC_STOCK], [f"Site {sid}", "Stock actuel (indépendant de la période)"], [], _last_sync(FuelStockSyncRun),
            "Relevé de plus de 7 jours : le stock peut ne plus être réel." if (stock["age_releve_j"] or 0) > 7 else None)
        explain["autonomie_j"] = _x("Autonomie", "Stock actuel ÷ conso moyenne par jour du dernier mois mesuré.", [
            _v("Stock", stock["stock_l"], "L"), _v("Conso par jour", stock["conso_jour_l"], "L"), _v("Mois de référence", stock["mois_reference"]),
            _v("Résultat", stock["autonomie_j"], "jours"),
        ], [SRC_STOCK, SRC_CAPTEUR], [f"Site {sid}"], [], _last_sync(FuelStockSyncRun),
            None if stock["autonomie_j"] is not None else "Stock ou conso journalière inconnus : autonomie non calculable.")

    return {
        "perimetre": SCOPE_LABEL,
        "site": {"site_id": sid, "site_name": last["site_name"], "zone": last["zone_fichier"], "typologie": last["typo"],
                 "country": last["country"], "alimentation": last["power_supply"], "nb_ge": last["dg_count"], "has_genset": has_ge},
        "from_month": from_month, "to_month": to_month, "months": months, "mois_unique": single,
        "available_months": all_months, "previous_months": prev_months,
        "target": {"mensuel_l": _f(tgt.target_mensuel_l), "confirme": bool(tgt.confirmed_year), "source": tgt.source_target} if tgt is not None else None,
        "tolerance_target_pct": tolerance,
        "kpis": kpis, "deltas": deltas, "explain": explain,
        "monthly": monthly, "daily": daily, "stock": stock, "enoc": enoc,
    }


class BiSiteView(APIView):
    """GET bi/sites/<site_id>/?from_month&to_month|month — fiche complète d'un site."""
    permission_classes = [IsAuthenticated]

    def get(self, request, site_id):
        try:
            return Response(compute_site(site_id, request.query_params))
        except _SiteNotFound:
            return Response({"detail": f"Site {site_id} introuvable dans le périmètre {SCOPE_LABEL}."}, status=404)


class BiConsoView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = compute_conso(request.query_params)
        data.pop("sites", None)  # liste complète réservée à l'export Excel
        return Response(data)


# ─── Stock ───────────────────────────────────────────────────────────────────

def _site_zones():
    """Zone du site (Base GE, dernier mois connu) — le snapshot stock n'en porte pas."""
    from fuel_tracking.models import FuelConsommationMonthly

    return dict(FuelConsommationMonthly.objects.exclude(zone_fichier__isnull=True).exclude(zone_fichier="")
                .order_by("month_year").values_list("site_id", "zone_fichier"))


def stock_sites(params):
    """Sites GE du périmètre avec stock retenu (relevé le plus récent, services/current_stock), capacité, conso mesurée par jour et autonomie.
    Partagé par Suivi Stock et le plan de ravitaillement : mêmes chiffres sur les deux écrans."""
    from fuel_tracking.models import FuelConsommationMonthly, FuelStockSnapshot

    all_ge = FuelStockSnapshot.objects.filter(snapshot_year__isnull=True, has_genset=True)
    qs = all_ge.filter(scope_q())
    zones_map = _site_zones()
    zones = sorted({zones_map[s] for s in qs.values_list("site_id", flat=True) if s in zones_map})
    if params.get("zone"):
        qs = qs.filter(site_id__in=[s for s, z in zones_map.items() if z == params["zone"]])

    # Autonomie = stock actuel ÷ conso mesurée moyenne par jour mesuré du mois de référence (dernier mois complet).
    months = list(FuelConsommationMonthly.objects.order_by("-month_year").values_list("month_year", flat=True).distinct()[:2])
    ref_month = (params.get("month") or "").strip() or (months[1] if len(months) > 1 else (months[0] if months else None))
    daily = {}
    if ref_month:
        for sid, c, j in FuelConsommationMonthly.objects.filter(month_year=ref_month, conso_snowflake_l__gt=0) \
                .values_list("site_id", "conso_snowflake_l", "nb_jours_data"):
            daily[sid] = float(c) / (j or _days_in(ref_month))

    from fuel_tracking.services.current_stock import current_stock

    today = timezone.localdate()
    snaps = list(qs.values("site_id", "site_name", "typology", "stock_snowflake_l", "capacity_snowflake_l",
                           "stock_snowflake_date", "stock_enoc_l", "stock_enoc_date"))
    cur = current_stock(snaps)
    sites = []
    for r in snaps:
        d = daily.get(r["site_id"])
        c = cur.get(r["site_id"]) or {}
        stock, cap = c.get("stock_l"), c.get("capacite_l")
        sites.append({
            "site_id": r["site_id"], "site_name": r["site_name"], "zone": zones_map.get(r["site_id"]), "typologie": r["typology"],
            "stock_l": _f(stock), "capacite_l": _f(cap), "capacite_source": c.get("capacite_source"),
            "remplissage_pct": _f(stock / cap * 100) if stock is not None and cap else None, "date": c.get("date"),
            "age_releve_j": (today - c["date"]).days if c.get("date") else None,
            "source_stock": c.get("source"), "source_stock_label": c.get("source_label"),
            "stock_estime": bool(c.get("estime")), "origine_stock": c.get("origine"),
            "stock_enoc_l": _f(r["stock_enoc_l"]), "date_enoc": r["stock_enoc_date"],
            "conso_jour_l": round(d, 1) if d else None,
            "autonomie_j": round(float(stock) / d, 1) if d and stock is not None else None,
        })

    return {"all_ge": all_ge, "qs": qs, "zones": zones, "zones_map": zones_map, "ref_month": ref_month, "today": today, "sites": sites}


def compute_stock(params) -> dict:
    from fuel_tracking.models import FuelStockSyncRun

    base = stock_sites(params)
    all_ge, qs, zones, zones_map = base["all_ge"], base["qs"], base["zones"], base["zones_map"]
    ref_month, today, sites = base["ref_month"], base["today"], base["sites"]

    # Agrégats calculés sur le stock retenu par site (relevé le plus récent, toutes sources) : mêmes chiffres que les lignes.
    known = [s for s in sites if s["stock_l"] is not None]
    pcts = [s["remplissage_pct"] for s in sites if s["remplissage_pct"] is not None]
    with_cap = [s for s in known if s["capacite_l"]]
    a = {
        "nb_sites": len(sites),
        "nb_stock_connu": sum(1 for s in sites if s["stock_l"] is not None or s["stock_enoc_l"] is not None),
        "nb_stock_capteur": len(known),
        "stock_l": sum(s["stock_l"] for s in known) if known else None,
        "capacite_l": sum(s["capacite_l"] for s in with_cap) if with_cap else None,
        "critique": sum(1 for p in pcts if p < 15), "alerte": sum(1 for p in pcts if 15 <= p < 40),
        "moyen": sum(1 for p in pcts if 40 <= p < 70), "bon": sum(1 for p in pcts if p >= 70),
        "sans_pct": len(sites) - len(pcts),
    }
    last_date = max((s["date"] for s in sites if s["date"]), default=None)
    stock_cap = sum(s["stock_l"] for s in with_cap) if with_cap else None

    with_pct = [s for s in sites if s["remplissage_pct"] is not None]
    with_auto = [s for s in sites if s["autonomie_j"] is not None]
    auto_buckets = [("< 3 j", 0, 3), ("3–7 j", 3, 7), ("7–15 j", 7, 15), ("15–30 j", 15, 30), ("> 30 j", 30, None)]
    age_buckets = [("≤ 1 j", 0, 2), ("2–3 j", 2, 4), ("4–7 j", 4, 8), ("8–15 j", 8, 16), ("> 15 j", 16, None)]
    by_zone = defaultdict(lambda: {"nb_sites": 0, "stock_l": 0.0, "capacite_l": 0.0, "nb_critique": 0, "nb_autonomie_moins_3j": 0, "has": False})
    for s in sites:
        b = by_zone[s["zone"] or "Non renseigné"]
        b["nb_sites"] += 1
        if s["stock_l"] is not None:
            b["has"] = True
            b["stock_l"] += s["stock_l"]
            b["capacite_l"] += s["capacite_l"] or 0
        if s["remplissage_pct"] is not None and s["remplissage_pct"] < 15:
            b["nb_critique"] += 1
        if s["autonomie_j"] is not None and s["autonomie_j"] < 3:
            b["nb_autonomie_moins_3j"] += 1

    nb_auto3 = sum(1 for s in with_auto if s["autonomie_j"] < 3)
    mediane = sorted(s["autonomie_j"] for s in with_auto)[len(with_auto) // 2] if with_auto else None
    anciens = sum(1 for s in sites if s["age_releve_j"] is not None and s["age_releve_j"] > 7)
    kpis = {
        "nb_sites": a["nb_sites"], "nb_stock_connu": a["nb_stock_connu"],
        "stock_connu_pct": round(a["nb_stock_connu"] / a["nb_sites"] * 100, 1) if a["nb_sites"] else None,
        "stock_total_l": _f(a["stock_l"]), "capacite_l": _f(a["capacite_l"]),
        "remplissage_pct": round(stock_cap / a["capacite_l"] * 100, 1) if stock_cap is not None and a["capacite_l"] else None,
        "nb_critique": a["critique"], "nb_alerte": a["alerte"], "nb_autonomie_moins_3j": nb_auto3,
        "autonomie_mediane_j": mediane, "nb_releves_anciens": anciens,
        "derniere_mesure": last_date, "mois_reference_autonomie": ref_month,
    }
    maj = _last_sync(FuelStockSyncRun)
    per = [f"{SCOPE_LABEL}, sites avec groupe électrogène", "Stock actuel : dernier relevé disponible de chaque site"]
    if params.get("zone"):
        per.append(f"Zone : {params['zone']}")
    excl_other = {"label": "Sites d'un autre pays, exclus", "nb": all_ge.count() - all_ge.filter(scope_q()).count()}
    excl_unknown = {"label": "Sites sans relevé de stock exploitable : non comptés (pas « 0 L »)", "nb": a["sans_pct"]}
    auto_src = f"Conso mesurée de {ref_month} (Snowflake VW_FUEL_REPORT) ÷ jours mesurés du mois"
    explain = {
        "stock_total_l": _x("Stock en cuve", "Somme du dernier relevé de stock de chaque site (capteur, terrain ou Synthèse, le plus récent).", [
            _v("Stock total", kpis["stock_total_l"], "L"), _v("Capacité des cuves relevées", kpis["capacite_l"], "L"),
            _v("Remplissage global", kpis["remplissage_pct"], "%"), _v("Sites au stock connu", a["nb_stock_capteur"]), _v("Dont stock estimé (Synthèse)", sum(1 for s in sites if s["stock_estime"])),
            _v("Relevé le plus récent", last_date),
        ], [SRC_STOCK], per, [excl_other, excl_unknown], maj),
        "nb_critique": _x("Cuves sous 15 %", "Sites dont le dernier relevé est inférieur à 15 % de la capacité de la cuve.", [
            _v("Sous 15 %", a["critique"]), _v("Entre 15 et 40 %", a["alerte"]), _v("Entre 40 et 70 %", a["moyen"]),
            _v("Au-dessus de 70 %", a["bon"]), _v("Sans relevé exploitable", a["sans_pct"]),
        ], [SRC_STOCK], per, [excl_other], maj),
        "nb_autonomie_moins_3j": _x("Autonomie < 3 jours", "Stock actuel ÷ conso moyenne par jour ; sites dont le résultat est sous 3 jours.", [
            _v("Sites sous 3 jours", nb_auto3), _v("Sites dont l'autonomie est calculable", len(with_auto)), _v("Mois de référence de la conso", ref_month),
        ], [SRC_STOCK, auto_src], per, [excl_other, {"label": "Sites sans conso mesurée au mois de référence : autonomie non calculable", "nb": len(sites) - len(with_auto)}], maj),
        "autonomie_mediane_j": _x("Autonomie médiane", "Valeur centrale des autonomies calculées : la moitié des sites a moins, l'autre moitié a plus.", [
            _v("Médiane", mediane, "jours"), _v("Sites pris en compte", len(with_auto)),
        ], [SRC_STOCK, auto_src], per, [{"label": "Sites sans autonomie calculable", "nb": len(sites) - len(with_auto)}], maj,
            "La médiane n'est pas tirée vers le haut par quelques très grosses cuves, contrairement à une moyenne."),
        "stock_connu_pct": _x("Stock connu", "Sites ayant un stock relevé (capteur ou ENOC) ÷ sites GE.", [
            _v("Sites GE", a["nb_sites"]), _v("Stock connu", a["nb_stock_connu"]), _v("Dont capteur", a["nb_stock_capteur"]),
        ], [SRC_STOCK, "ENOC : dernier stock déclaré"], per, [excl_other], maj),
        "nb_releves_anciens": _x("Relevés de plus de 7 jours", "Sites dont le dernier relevé de stock (toutes sources) date de plus de 7 jours.", [
            _v("Sites concernés", anciens), _v("Sites avec un relevé", sum(1 for s in sites if s["age_releve_j"] is not None)),
        ], [SRC_STOCK], per, [], maj, "Pour ces sites, le stock affiché peut ne plus être réel : à confirmer sur place."),
    }

    return {
        "perimetre": SCOPE_LABEL,
        "filters": {"zones": zones},
        "kpis": kpis, "explain": explain,
        "distribution_remplissage": [
            {"label": "< 15 %", "nb_sites": a["critique"], "niveau": "critique"},
            {"label": "15–40 %", "nb_sites": a["alerte"], "niveau": "alerte"},
            {"label": "40–70 %", "nb_sites": a["moyen"], "niveau": "moyen"},
            {"label": "> 70 %", "nb_sites": a["bon"], "niveau": "bon"},
            {"label": "Inconnu", "nb_sites": a["sans_pct"], "niveau": "inconnu"},
        ],
        "distribution_autonomie": [
            {"label": label, "nb_sites": sum(1 for s in with_auto if s["autonomie_j"] >= lo and (hi is None or s["autonomie_j"] < hi))}
            for label, lo, hi in auto_buckets
        ],
        # Origine du stock retenu par site (maquette « Origine du stock final ») : libellé de la source, « Estimation » si estimé.
        "distribution_source": sorted(
            ({"label": label, "nb_sites": n} for label, n in Counter(
                "Estimation" if s["stock_estime"] else (s["source_stock_label"] or "Aucun relevé") for s in sites
            ).items()), key=lambda x: -x["nb_sites"]),
        "distribution_age_releve": [
            {"label": label, "nb_sites": sum(1 for s in sites if s["age_releve_j"] is not None and s["age_releve_j"] >= lo and (hi is None or s["age_releve_j"] < hi))}
            for label, lo, hi in age_buckets
        ] + [{"label": "Aucun relevé", "nb_sites": sum(1 for s in sites if s["age_releve_j"] is None)}],
        "by_zone": sorted([{
            "label": k, "nb_sites": b["nb_sites"], "stock_l": round(b["stock_l"], 1) if b["has"] else None,
            "remplissage_pct": round(b["stock_l"] / b["capacite_l"] * 100, 1) if b["has"] and b["capacite_l"] else None,
            "nb_critique": b["nb_critique"], "nb_autonomie_moins_3j": b["nb_autonomie_moins_3j"]} for k, b in by_zone.items()], key=lambda x: -x["nb_sites"]),
        "top_critique": sorted(with_pct, key=lambda s: s["remplissage_pct"])[:TOP_N],
        "top_autonomie_faible": sorted(with_auto, key=lambda s: s["autonomie_j"])[:TOP_N],
        "top_stock": sorted((s for s in sites if s["stock_l"] is not None), key=lambda s: -s["stock_l"])[:TOP_N],
        "sites": sites,
    }


class BiStockEvolutionView(APIView):
    """GET /api/fuel-tracking/bi/stock/evolution/?zone=&mois=12 — stock de fin de mois sur les derniers mois."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fuel_tracking.models import FuelStockSyncRun
        from fuel_tracking.services.stock_history import stock_evolution

        try:
            n = int(request.query_params.get("mois") or 12)
        except ValueError:
            return Response({"detail": "Paramètre « mois » invalide."}, status=400)
        zone = request.query_params.get("zone") or None
        d = stock_evolution(n, zone)
        last = next((m for m in reversed(d["months"]) if m["stock_l"] is not None), None)
        per = [f"{SCOPE_LABEL}, sites avec groupe électrogène", f"Zone : {zone}" if zone else "Toutes zones", f"{len(d['months'])} derniers mois"]
        d["explain"] = {"evolution": _x(
            "Évolution du stock", "Pour chaque mois : somme, site par site, du dernier relevé de stock valide daté dans le mois "
            "(capteur, relevé terrain / gardiennage / ENOC, ou stock final de la Synthèse). Remplissage = stock ÷ capacité des mêmes sites.", [
                _v("Dernier mois avec relevés", last["month"] if last else None), _v("Stock", last["stock_l"] if last else None, "L"),
                _v("Sites relevés ce mois-là", last["nb_sites_releves"] if last else None), _v("Remplissage", last["remplissage_pct"] if last else None, "%"),
                _v("Sites GE du périmètre", last["nb_sites_ge"] if last else None)],
            [SRC_STOCK, "Capteur : snapshots de fin de mois archivés ; mois en cours : dernier relevé"], per,
            [{"label": "Sites sans relevé dans le mois : non comptés ce mois-là (ni 0 L, ni report du mois précédent)",
              "nb": (last["nb_sites_ge"] - last["nb_sites_releves"]) if last else 0}],
            _last_sync(FuelStockSyncRun),
            "Le total en litres dépend du nombre de sites relevés chaque mois : comparer les mois avec le remplissage, "
            "qui reste juste quand la couverture change.")}
        return Response(d)


class BiStockView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = compute_stock(request.query_params)
        data.pop("sites", None)
        return Response(data)


# ─── CPH ─────────────────────────────────────────────────────────────────────

def compute_cph(params) -> dict:
    from fuel_tracking.models import FuelDailyFactsSyncRun
    from fuel_tracking.services.cph_service import compute_period_summary
    from fuel_tracking.views_cph import _parse_period, _periode_info

    if (params.get("month") or "").strip():
        start, end = _month_bounds(params["month"].strip(), params["month"].strip())
    else:
        start, end = _parse_period(params)
    if end < start:
        start, end = end, start
    if (end - start).days > 92:
        raise _BadPeriod("Période CPH limitée à 93 jours.")
    all_rows = compute_period_summary(start, end)
    rows = [r for r in all_rows if in_scope(r.get("country"))]
    zones = sorted({r.get("zone") for r in rows if r.get("perimetre") == "GE" and r.get("zone")})
    if params.get("zone"):
        rows = [r for r in rows if r.get("zone") == params["zone"]]

    ge = [r for r in rows if r.get("perimetre") == "GE"]
    meas = lambda r: (r.get("comparaison") or {}).get("conso_mesuree_l")  # noqa: E731
    theo = [r for r in ge if r.get("conso_theorique_l") is not None]
    rt = [r for r in ge if r.get("runtime_total_h") is not None]
    both = [r for r in theo if meas(r) is not None]

    status_counts, motifs, by_zone = defaultdict(int), defaultdict(int), {}
    for r in ge:
        status_counts[r.get("cph_status") or "NON_CALCULE"] += 1
        if r.get("cph_status") != "COMPLET":
            motifs[((r.get("motif_cph") or {}).get("code")) or "AUTRE"] += 1
        k = r.get("zone") or "Non renseigné"
        b = by_zone.setdefault(k, {"label": k, "nb_sites": 0, "nb_cph_complet": 0, "conso_theorique_l": 0.0, "runtime_h": 0.0})
        b["nb_sites"] += 1
        if r.get("conso_theorique_l") is not None:
            b["nb_cph_complet"] += 1
            b["conso_theorique_l"] += float(r["conso_theorique_l"])
        if r.get("runtime_total_h") is not None:
            b["runtime_h"] += float(r["runtime_total_h"])
    for b in by_zone.values():
        b["conso_theorique_l"] = round(b["conso_theorique_l"], 1) if b["nb_cph_complet"] else None
        b["runtime_h"] = round(b["runtime_h"], 1)

    sites = []
    for r in ge:
        m, t = meas(r), r.get("conso_theorique_l")
        sites.append({
            "site_id": r["site_id"], "site_name": r.get("site_name"), "zone": r.get("zone"),
            "configuration": r.get("kind"), "ge_kva": _f(r.get("ge_kva")), "statut_cph": r.get("cph_status"),
            "motif": (r.get("motif_cph") or {}).get("code"), "runtime_h": _f(r.get("runtime_total_h")),
            "source_runtime": r.get("runtime_source_main"), "charge_pct": _f(float(r["charge_moy"]) * 100) if r.get("charge_moy") is not None else None,
            "p_ge_kw": _f(r.get("p_ge_moy_kw"), 2), "cph_l_h": _f(r.get("cph_moy_l_h"), 2),
            "conso_theorique_l": _f(t), "conso_partielle_l": _f(r.get("conso_partielle_l")), "conso_mesuree_l": _f(m),
            "ecart_l": _f(float(m) - float(t)) if m is not None and t is not None else None,
            "ecart_pct": _pct_change(m, t) if m is not None and t is not None else None,
        })
    by_id = {s["site_id"]: s for s in sites}
    sum_theo_all = sum(float(r["conso_theorique_l"]) for r in theo)
    sum_theo = sum(float(r["conso_theorique_l"]) for r in both)
    sum_meas = sum(float(meas(r)) for r in both)
    theo_rt = sum(float(r["runtime_total_h"]) for r in theo if r.get("runtime_total_h") is not None)
    rt_total = sum(float(r["runtime_total_h"]) for r in rt)
    charged = [r for r in rt if r.get("charge_moy") is not None and float(r["runtime_total_h"]) > 0]
    charge_h = sum(float(r["runtime_total_h"]) for r in charged)
    charge = _f(sum(float(r["charge_moy"]) * float(r["runtime_total_h"]) for r in charged) / charge_h * 100) if charge_h else None

    kpis = {
        "nb_sites_ge": len(ge), "nb_cph_complet": len(theo),
        "couverture_cph_pct": round(len(theo) / len(ge) * 100, 1) if ge else None,
        "conso_theorique_l": round(sum_theo_all, 1) if theo else None,
        "runtime_total_h": round(rt_total, 1) if rt else None,
        "cph_moy_l_h": _ratio(sum_theo_all, theo_rt or None, 2) if theo else None,
        "charge_moy_pct": charge,
        "nb_sites_comparables": len(both),
        "conso_theorique_comparable_l": round(sum_theo, 1) if both else None,
        "conso_mesuree_comparable_l": round(sum_meas, 1) if both else None,
        "ecart_global_pct": _pct_change(sum_meas, sum_theo) if both else None,
    }
    maj = _last_sync(FuelDailyFactsSyncRun)
    per = [f"{SCOPE_LABEL}, sites avec groupe électrogène", f"Période : {start:%d/%m/%Y} → {end:%d/%m/%Y}"]
    if params.get("zone"):
        per.append(f"Zone : {params['zone']}")
    ge_all = [r for r in all_rows if r.get("perimetre") == "GE"]
    excl_other = {"label": "Sites d'un autre pays, exclus", "nb": sum(1 for r in ge_all if not in_scope(r.get("country")))}
    excl_nc = {"label": "Sites au CPH partiel ou non calculé : non additionnés", "nb": len(ge) - len(theo)}
    explain = {
        "couverture_cph_pct": _x("Sites au CPH complet", "Sites dont la conso estimée est calculée sur tous les jours ÷ sites GE.", [
            _v("Sites GE", len(ge)), _v("CPH complet", len(theo)), _v("CPH partiel", status_counts.get("PARTIEL", 0)),
            _v("Non calculé", status_counts.get("NON_CALCULE", 0)),
        ], [SRC_CPH], per, [excl_other], maj, "Le motif de chaque site non complet est détaillé dans le graphique « Motifs de non-calcul »."),
        "conso_theorique_l": _x("Conso estimée CPH", "Somme, jour par jour, de CPH (L/h) × heures de marche, sur les sites au CPH complet.", [
            _v("Total", kpis["conso_theorique_l"], "L"), _v("Sites additionnés", len(theo)), _v("Heures de marche de ces sites", _f(theo_rt), "h"),
        ], [SRC_CPH], per, [excl_other, excl_nc], maj, "C'est une estimation : elle n'est jamais additionnée à la conso mesurée."),
        "runtime_total_h": _x("Heures de marche GE", "Somme des heures de fonctionnement des GE retenues par le moteur CPH.", [
            _v("Total", kpis["runtime_total_h"], "h"), _v("Sites avec des heures", len(rt)),
        ], ["Snowflake : compteur d'heures du contrôleur GE ou du tracker, selon la source la plus fiable par site"], per,
            [excl_other, {"label": "Sites GE sans heures de marche", "nb": len(ge) - len(rt)}], maj),
        "cph_moy_l_h": _x("CPH moyen", "Conso estimée ÷ heures de marche, sites au CPH complet (ratio des sommes).", [
            _v("Conso estimée", kpis["conso_theorique_l"], "L"), _v("Heures", _f(theo_rt), "h"), _v("Résultat", kpis["cph_moy_l_h"], "L/h"),
        ], [SRC_CPH], per, [excl_other, excl_nc], maj),
        "charge_moy_pct": _x("Charge moyenne GE", "Moyenne des charges (puissance demandée ÷ puissance nominale), pondérée par les heures de marche.", [
            _v("Résultat", charge, "%"), _v("Sites pris en compte", len(charged)), _v("Heures pondérées", _f(charge_h), "h"),
        ], [SRC_CPH, "Snowflake : puissance DC des redresseurs et rendement, pendant la marche du GE"], per, [excl_other], maj,
            "Sous 30 % de charge, un groupe diesel consomme nettement plus par kWh produit."),
        "ecart_global_pct": _x("Mesuré vs CPH", "(Conso mesurée − conso estimée CPH) ÷ conso estimée, sur les sites ayant les deux sur les mêmes jours.", [
            _v("Conso mesurée", kpis["conso_mesuree_comparable_l"], "L"), _v("Conso estimée CPH", kpis["conso_theorique_comparable_l"], "L"),
            _v("Sites comparés", len(both)),
        ], [SRC_CAPTEUR, SRC_CPH], per, [excl_other, {"label": "Sites au CPH complet sans conso mesurée : non comparés", "nb": len(theo) - len(both)}], maj,
            "Au-delà de ±15 %, l'écart est à justifier site par site (classement « Écarts mesuré / CPH »)."),
    }

    return {
        "perimetre": SCOPE_LABEL, "start": start, "end": end, "periode": _periode_info(start, end),
        "filters": {"zones": zones},
        "kpis": kpis, "explain": explain,
        "statuts_cph": [{"label": k, "nb_sites": v} for k, v in sorted(status_counts.items(), key=lambda kv: -kv[1])],
        "motifs_non_calcule": [{"label": k, "nb_sites": v} for k, v in sorted(motifs.items(), key=lambda kv: -kv[1])[:8]],
        "by_zone": sorted(by_zone.values(), key=lambda b: -b["nb_sites"]),
        "charge_vs_cph": [{"site_id": s["site_id"], "charge_pct": s["charge_pct"], "cph_l_h": s["cph_l_h"]}
                          for s in sites if s["charge_pct"] is not None and s["cph_l_h"] is not None][:800],
        "top_conso_theorique": [by_id[r["site_id"]] for r in sorted(theo, key=lambda r: -float(r["conso_theorique_l"]))[:TOP_N]],
        "top_ecart_mesure": [by_id[r["site_id"]] for r in sorted(both, key=lambda r: -abs(float(meas(r)) - float(r["conso_theorique_l"])))[:TOP_N]],
        "top_runtime": [by_id[r["site_id"]] for r in sorted(rt, key=lambda r: -float(r["runtime_total_h"]))[:TOP_N]],
        "sites": sites,
    }


class BiCphView(APIView):
    """Vue BI sur le calcul CPH de la période (même calcul mis en cache que l'onglet Contrôle CPH)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            data = compute_cph(request.query_params)
        except (_BadPeriod, ValueError) as e:
            return Response({"detail": str(e)}, status=400)
        except Exception as e:  # source indisponible : l'écran affiche « indisponible », jamais 0
            return Response({"detail": f"Calcul CPH indisponible ({type(e).__name__})."}, status=503)
        data.pop("sites", None)
        return Response(data)


# ─── Export Excel ────────────────────────────────────────────────────────────

NAVY = "0B1F4D"

FMT = {"L": "#,##0", "pct": "0.0", "ratio": "0.000", "dec1": "#,##0.0", "dec2": "0.00", "int": "#,##0", "date": "dd/mm/yyyy", "txt": "@"}


def _sheet(wb, title, columns, rows):
    """Feuille tableau : en-tête Camusat, filtres auto, volets figés, formats numériques, cellules vides si absent."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    ws = wb.create_sheet()
    ws.title = title[:31]
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=NAVY)
    thin = Side(style="thin", color="D9DEE7")
    for ci, (label, _key, _fmt, width) in enumerate(columns, 1):
        c = ws.cell(row=1, column=ci, value=label)
        c.font, c.fill = head_font, head_fill
        c.alignment = Alignment(vertical="center", wrap_text=True)
        c.border = Border(bottom=thin)
        ws.column_dimensions[get_column_letter(ci)].width = width
    ws.row_dimensions[1].height = 32
    for ri, row in enumerate(rows, 2):
        for ci, (_label, key, fmt, _w) in enumerate(columns, 1):
            v = key(row) if callable(key) else row.get(key)
            c = ws.cell(row=ri, column=ci, value=v)
            if v is not None and fmt in FMT:
                c.number_format = FMT[fmt]
    ws.freeze_panes = "B2" if len(columns) > 3 else "A2"
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
    return ws


def _summary_sheet(wb, title, filters, kpis, explain=None):
    """Feuille « Synthèse » : périmètre, filtres, indicateurs et, pour chacun, la règle de calcul."""
    from openpyxl.styles import Alignment, Font, PatternFill

    ws = wb.active
    ws.title = "Synthèse"
    ws["A1"] = title
    ws["A1"].font = Font(bold=True, size=14, color=NAVY)
    ws["A2"] = f"Export du {timezone.localtime():%d/%m/%Y à %H:%M} — EnerTrack, Suivi carburant"
    ws["A2"].font = Font(italic=True, color="64748B")
    r = 4
    for label, value in [("Périmètre", SCOPE_LABEL), *filters]:
        ws.cell(row=r, column=1, value=label).font = Font(bold=True)
        ws.cell(row=r, column=2, value=value)
        r += 1
    r += 1
    for col, h in enumerate(("Indicateur", "Valeur", "Calcul"), 1):
        c = ws.cell(row=r, column=col, value=h)
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor=NAVY)
    for item in kpis:
        label, value, fmt = item[:3]
        key = item[3] if len(item) > 3 else None
        r += 1
        ws.cell(row=r, column=1, value=label)
        c = ws.cell(row=r, column=2, value=value if value is not None else "Non disponible")
        if value is not None and fmt in FMT:
            c.number_format = FMT[fmt]
        x = (explain or {}).get(key) if key else None
        if x:
            ws.cell(row=r, column=3, value=x["formule"]).alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 46
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 80
    return ws


def _xlsx_response(wb, name):
    buf = BytesIO()
    wb.save(buf)
    r = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    r["Content-Disposition"] = f'attachment; filename="{name}"'
    return r


def _export_conso(p):
    from openpyxl import Workbook

    d = compute_conso(p)
    k, x = d.get("kpis") or {}, d.get("explain")
    wb = Workbook()
    _summary_sheet(wb, "Suivi carburant — Consommation", [
        ("Période", f"{d.get('from_month', '—')} → {d.get('to_month', '—')}"),
        ("Zone", p.get("zone") or "Toutes"), ("Typologie", p.get("typology") or "Toutes"),
        ("Sites", "Sites avec groupe électrogène"),
    ], [
        ("Conso mesurée (capteur), L", k.get("conso_mesuree_l"), "L", "conso_mesuree_l"),
        ("Conso moyenne, L / site / jour mesuré", k.get("l_site_jour"), "dec1", "l_site_jour"),
        ("Rendement GE, L/kWh", k.get("l_kwh"), "ratio", "l_kwh"),
        ("Écart conso vs target, % (sites avec target)", k.get("ecart_target_pct"), "pct", "ecart_target_pct"),
        ("Sites au-dessus du target", k.get("nb_sites_au_dessus_target"), "int"),
        ("Targets non confirmés", k.get("nb_targets_a_confirmer"), "int"),
        ("Livré ENOC, L", k.get("enoc_ajoutee_l"), "L", "enoc_ajoutee_l"),
        ("Demandes de ravitaillement ENOC", k.get("nb_ravitaillements"), "int"),
        ("Sites GE", k.get("nb_sites"), "int"), ("Sites avec conso mesurée", k.get("nb_sites_mesure"), "int"),
        ("Sites mesurés, %", k.get("couverture_pct"), "pct", "couverture_pct"),
        ("Part des sites faisant 80 % de la conso, %", (d.get("pareto") or {}).get("pct_sites_80"), "pct"),
    ], x)
    _sheet(wb, "Mensuel", [
        ("Mois", "month", "txt", 10), ("Sites GE", "nb_sites", "int", 10), ("Sites mesurés", "nb_sites_mesure", "int", 12),
        ("Sites mesurés %", "couverture_pct", "pct", 12), ("Conso mesurée (L)", "conso_mesuree_l", "L", 16),
        ("L/site/jour", "l_site_jour", "dec1", 11), ("L/kWh", "l_kwh", "ratio", 9), ("Livré ENOC (L)", "enoc_ajoutee_l", "L", 15),
        ("Target (L)", "target_l", "L", 13), ("Conso des sites avec target (L)", "conso_vs_target_l", "L", 18),
        ("Relevé valide", lambda r: r["qualite"]["OK"], "int", 11), ("Qualité faible", lambda r: r["qualite"]["LOW_QUALITY"], "int", 12),
        ("Niveau invalide", lambda r: r["qualite"]["NO_VALID_LEVEL"], "int", 13), ("Sans relevé", lambda r: r["qualite"]["Autre / absent"], "int", 13),
    ], d.get("monthly") or [])
    try:
        src = compute_conso_sources(p) if d.get("from_month") else None
    except Exception:
        src = None
    if src:
        _sheet(wb, "4 mesures", [
            ("Mesure", "titre", "txt", 26), ("Total (L)", "total_l", "L", 14), ("Sites", "sites", "int", 9), ("Calcul", "formule", "txt", 90),
        ], [{"titre": SRC_TITRES[key], **src["kpis"][key], "formule": src["explain"][key]["formule"]} for key in SOURCES]
           if src["disponible"] else [{"titre": "Non calculé", "total_l": None, "sites": None, "formule": src["motif"]}])
    _sheet(wb, "Journalier", [
        ("Date", "date", "date", 12), ("Conso mesurée (L)", "conso_mesuree_l", "L", 16), ("Sites mesurés", "nb_sites", "int", 12),
        ("L / site", "l_site", "dec1", 10),
    ], d.get("daily") or [])
    bd = [("Sites", "nb_sites", "int", 9), ("Conso mesurée (L)", "conso_mesuree_l", "L", 16), ("L/site/jour", "l_site_jour", "dec1", 11),
          ("L/kWh", "l_kwh", "ratio", 9), ("Livré ENOC (L)", "enoc_ajoutee_l", "L", 15)]
    _sheet(wb, "Par zone", [("Zone", "label", "txt", 18)] + bd, d.get("by_zone") or [])
    _sheet(wb, "Par typologie", [("Typologie", "label", "txt", 22)] + bd, d.get("by_typology") or [])
    _sheet(wb, "Sites", [
        ("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 26), ("Zone", "zone", "txt", 10), ("Typologie", "typologie", "txt", 14),
        ("Mois mesurés", "nb_mois_mesures", "int", 9), ("Jours mesurés", "jours_mesures", "int", 9),
        ("Conso mesurée (L)", "conso_mesuree_l", "L", 15), ("L/site/jour", "l_site_jour", "dec1", 10),
        ("kWh GE", "ge_prod_kwh", "L", 11), ("L/kWh", "l_kwh", "ratio", 8),
        ("Livré ENOC (L)", "enoc_ajoutee_l", "L", 14), ("Demandes ENOC", "nb_ravitaillements", "int", 10), ("Écart mesuré − livré (L)", "ecart_enoc_l", "L", 15),
        ("Target période (L)", "target_l", "L", 14), ("Écart vs target %", "ecart_target_pct", "pct", 11),
        ("Statut target", "statut_target", "txt", 15), ("Target confirmé", lambda r: "Oui" if r["target_confirme"] else ("Non" if r["target_l"] is not None else None), "txt", 10),
    ], sorted(d.get("sites") or [], key=lambda s: -(s["conso_mesuree_l"] or 0)))
    return _xlsx_response(wb, f"suivi_conso_{d.get('from_month', '')}_{d.get('to_month', '')}.xlsx")


def _export_stock(p):
    from openpyxl import Workbook

    d = compute_stock(p)
    k = d["kpis"]
    wb = Workbook()
    _summary_sheet(wb, "Suivi carburant — Stock actuel", [
        ("Zone", p.get("zone") or "Toutes"), ("Dernier relevé", str(k.get("derniere_mesure") or "—")),
        ("Mois de référence de l'autonomie", k.get("mois_reference_autonomie") or "—"),
    ], [
        ("Stock total, L", k.get("stock_total_l"), "L", "stock_total_l"), ("Remplissage global, %", k.get("remplissage_pct"), "pct"),
        ("Sites GE", k.get("nb_sites"), "int"), ("Sites au stock connu, %", k.get("stock_connu_pct"), "pct", "stock_connu_pct"),
        ("Cuves sous 15 %", k.get("nb_critique"), "int", "nb_critique"), ("Cuves entre 15 et 40 %", k.get("nb_alerte"), "int"),
        ("Autonomie médiane, jours", k.get("autonomie_mediane_j"), "dec1", "autonomie_mediane_j"),
        ("Sites à moins de 3 jours d'autonomie", k.get("nb_autonomie_moins_3j"), "int", "nb_autonomie_moins_3j"),
        ("Relevés de plus de 7 jours", k.get("nb_releves_anciens"), "int", "nb_releves_anciens"),
    ], d.get("explain"))
    _sheet(wb, "Sites", [
        ("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 26), ("Zone", "zone", "txt", 10), ("Typologie", "typologie", "txt", 14),
        ("Stock (L)", "stock_l", "L", 13), ("Source du stock", "source_stock_label", "txt", 12),
        ("Stock estimé", lambda r: "Oui" if r["stock_estime"] else "", "txt", 9), ("Origine (Synthèse)", "origine_stock", "txt", 22),
        ("Capacité (L)", "capacite_l", "L", 12), ("Remplissage %", "remplissage_pct", "pct", 12),
        ("Date relevé", "date", "date", 12), ("Âge du relevé (j)", "age_releve_j", "int", 11),
        ("Stock ENOC (L)", "stock_enoc_l", "L", 12), ("Date ENOC", "date_enoc", "date", 12),
        ("Conso / jour (L)", "conso_jour_l", "dec1", 11), ("Autonomie (j)", "autonomie_j", "dec1", 11),
    ], sorted(d["sites"], key=lambda s: (s["remplissage_pct"] is None, s["remplissage_pct"] or 0)))
    _sheet(wb, "Par zone", [("Zone", "label", "txt", 18), ("Sites", "nb_sites", "int", 9), ("Stock (L)", "stock_l", "L", 13),
                            ("Remplissage %", "remplissage_pct", "pct", 12), ("Cuves sous 15 %", "nb_critique", "int", 12),
                            ("Autonomie < 3 j", "nb_autonomie_moins_3j", "int", 12)], d["by_zone"])
    return _xlsx_response(wb, f"suivi_stock_{timezone.localdate():%Y-%m-%d}.xlsx")


def _export_cph(p):
    from openpyxl import Workbook

    d = compute_cph(p)
    k = d["kpis"]
    wb = Workbook()
    _summary_sheet(wb, "Suivi carburant — Contrôle CPH", [
        ("Période", f"{d['start']:%d/%m/%Y} → {d['end']:%d/%m/%Y}"), ("Zone", p.get("zone") or "Toutes"),
    ], [
        ("Sites GE", k["nb_sites_ge"], "int"), ("Sites au CPH complet", k["nb_cph_complet"], "int"),
        ("Sites au CPH complet, %", k["couverture_cph_pct"], "pct", "couverture_cph_pct"),
        ("Conso estimée CPH, L", k["conso_theorique_l"], "L", "conso_theorique_l"),
        ("Heures de marche GE", k["runtime_total_h"], "dec1", "runtime_total_h"), ("CPH moyen, L/h", k["cph_moy_l_h"], "dec2", "cph_moy_l_h"),
        ("Charge moyenne GE, %", k["charge_moy_pct"], "pct", "charge_moy_pct"),
        ("Sites comparables (CPH et mesuré)", k["nb_sites_comparables"], "int"),
        ("Écart mesuré vs CPH, % (sites comparables)", k["ecart_global_pct"], "pct", "ecart_global_pct"),
    ], d.get("explain"))
    _sheet(wb, "Sites", [
        ("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 26), ("Zone", "zone", "txt", 11),
        ("Configuration", "configuration", "txt", 11), ("GE (kVA)", "ge_kva", "dec1", 9), ("Statut CPH", "statut_cph", "txt", 12),
        ("Motif", "motif", "txt", 22), ("Heures de marche", "runtime_h", "dec1", 11), ("Source heures", "source_runtime", "txt", 12),
        ("Charge %", "charge_pct", "pct", 9), ("P GE (kW)", "p_ge_kw", "dec2", 9), ("CPH (L/h)", "cph_l_h", "dec2", 9),
        ("Conso CPH (L)", "conso_theorique_l", "L", 12), ("Conso CPH partielle (L)", "conso_partielle_l", "L", 13),
        ("Conso mesurée (L)", "conso_mesuree_l", "L", 13), ("Écart mesuré − CPH (L)", "ecart_l", "L", 13), ("Écart %", "ecart_pct", "pct", 9),
    ], d["sites"])
    _sheet(wb, "Motifs", [("Motif", "label", "txt", 30), ("Sites", "nb_sites", "int", 10)], d["motifs_non_calcule"])
    _sheet(wb, "Par zone", [("Zone", "label", "txt", 18), ("Sites GE", "nb_sites", "int", 10), ("CPH complet", "nb_cph_complet", "int", 11),
                            ("Conso CPH (L)", "conso_theorique_l", "L", 13), ("Heures", "runtime_h", "dec1", 11)], d["by_zone"])
    return _xlsx_response(wb, f"controle_cph_{d['start']:%Y-%m-%d}_{d['end']:%Y-%m-%d}.xlsx")


def _export_commande(p):
    from openpyxl import Workbook

    from fuel_tracking.models import FuelCommandeSynthese, FuelSuiviCommandeSite

    month = (p.get("month") or "").strip() or FuelSuiviCommandeSite.objects.order_by("-month_year").values_list("month_year", flat=True).first()
    rows = list(FuelSuiviCommandeSite.objects.filter(month_year=month).order_by("-commande_avec_marge_l").values()) if month else []
    wb = Workbook()
    tot = lambda f: _f(sum(float(r[f] or 0) for r in rows)) if rows else None  # noqa: E731
    _summary_sheet(wb, "Suivi carburant — Commande Ops", [("Mois", month or "—"), ("Source", "Fichier de commande Ops importé")], [
        ("Sites du fichier", len(rows), "int"), ("Commande avec marge, L", tot("commande_avec_marge_l"), "L"),
        ("Commande sans marge, L", tot("commande_sans_marge_l"), "L"),
        ("Sites avec commande > 0", sum(1 for r in rows if (r["commande_avec_marge_l"] or 0) > 0), "int"),
        ("Sites au stock final estimé négatif", sum(1 for r in rows if (r["estimation_stock_final_l"] or 0) < 0), "int"),
    ])
    _sheet(wb, "Sites", [
        ("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 26), ("Batch", "batch", "txt", 12),
        ("Typologie contractuelle", "typologie_contractuelle", "txt", 16), ("Typologie facturée", "typologie_facturee", "txt", 16),
        ("Indoor / Outdoor", "indoor_outdoor", "txt", 10), ("Load", "load_commande", "dec2", 8), ("Conso moy. / jour (L)", "conso_moy_jour_l", "dec1", 12),
        ("Commande sans marge (L)", "commande_sans_marge_l", "L", 14), ("Commande avec marge (L)", "commande_avec_marge_l", "L", 14),
        ("Stock final estimé (L)", "estimation_stock_final_l", "L", 14),
    ], [{k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in r.items()} for r in rows])
    try:
        syn = list(FuelCommandeSynthese.objects.filter(month_year=month).values())
    except Exception:
        syn = []
    if syn:
        cols = [k for k in syn[0].keys() if k not in ("id", "month_year", "imported_at", "source_filename")]
        _sheet(wb, "Synthèse Ops", [(c.replace("_", " ").capitalize(), c, "txt" if isinstance(syn[0][c], str) else "L", 16) for c in cols],
               [{k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in r.items()} for r in syn])
    return _xlsx_response(wb, f"commande_ops_{month or 'na'}.xlsx")


def _export_estimation(request):
    from openpyxl import Workbook

    from fuel_tracking.views import FuelCommandeEstimationView

    data = FuelCommandeEstimationView().get(request).data
    k = data.get("kpis") or {}
    wb = Workbook()
    _summary_sheet(wb, "Suivi carburant — Estimation commande", [
        ("Mois estimé", data.get("target_month") or "—"), ("Mois sources", ", ".join(data.get("source_months") or [])),
        ("Marge appliquée, %", str(data.get("marge_pct"))),
    ], [
        ("Sites estimés", k.get("nb_sites"), "int"), ("Commande estimée totale, L", k.get("total_commande_estimee_l"), "L"),
        ("Commande Ops de référence, L", k.get("total_commande_ops_reference_l"), "L"),
        ("Sites en rupture prévue", k.get("nb_sites_rupture_prevue"), "int"),
        ("Sites en confiance élevée", k.get("nb_sites_confiance_elevee"), "int"), ("Sites en confiance faible", k.get("nb_sites_confiance_faible"), "int"),
    ])
    _sheet(wb, "Sites", [
        ("Site", "site_id", "txt", 12), ("Nom", "site_name", "txt", 26), ("Mois d'historique", "nb_mois_historique", "int", 10),
        ("Sources", lambda r: ", ".join(r.get("sources_historique") or []), "txt", 14),
        ("Conso / jour pondérée (L)", "conso_jour_ponderee_l", "dec1", 13), ("Conso projetée (L)", "conso_projetee_l", "L", 13),
        ("Stock actuel (L)", "stock_actuel_l", "L", 12), ("Capacité cuve (L)", "capacite_cuve_l", "L", 12),
        ("Commande sans marge (L)", "commande_sans_marge_l", "L", 14), ("Commande avec marge (L)", "commande_avec_marge_l", "L", 14),
        ("Plafonnée par capacité", lambda r: "Oui" if r.get("plafonnee_par_capacite") else "Non", "txt", 11),
        ("Stock final estimé (L)", "stock_final_estime_l", "L", 13), ("Confiance", "confiance", "txt", 10),
        ("Commande Ops de référence (L)", "commande_ops_reference_l", "L", 15),
    ], data.get("sites") or [])
    return _xlsx_response(wb, f"estimation_commande_{data.get('target_month') or 'na'}.xlsx")


class BiExportView(APIView):
    """GET /api/fuel-tracking/bi/export/<kind>/ — classeur Excel complet (toutes les lignes, pas seulement le Top 10)."""
    permission_classes = [IsAuthenticated]
    KINDS = ("conso", "stock", "cph", "commande", "estimation", "ravitaillement", "sites")

    def get(self, request, kind):
        p = request.query_params
        try:
            if kind == "conso":
                return _export_conso(p)
            if kind == "stock":
                return _export_stock(p)
            if kind == "cph":
                return _export_cph(p)
            if kind == "commande":
                return _export_commande(p)
            if kind == "estimation":
                return _export_estimation(request)
            if kind == "ravitaillement":
                from fuel_tracking.views_ravitaillement import export_plan
                return export_plan(p)
            if kind == "sites":
                from fuel_tracking.views_sites_table import export_site_table
                return export_site_table(p)
        except (_BadPeriod, ValueError) as e:
            return Response({"detail": str(e)}, status=400)
        return Response({"detail": "Export inconnu.", "exports": self.KINDS}, status=404)
