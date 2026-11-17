"""
Santé des données du module Carburant : fraîcheur de chaque source, couverture des relevés et sites en retard. LECTURE SEULE (aucune écriture ENOC / Snowflake).

Sources : Closing (fichier mensuel envoyé à la Finance), gardiennage (Loliforce, SCSS — relevés cumulés depuis janvier, reçus toutes les 2 semaines),
mouvements ENOC (lus), faits Snowflake (instrumentée / CPH).
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

GUARD_DELAY_DAYS = int(getattr(settings, "FUEL_GUARD_READING_MAX_AGE_DAYS", 16))     # relevés reçus toutes les 2 semaines : alerte au-delà


def _closing_batch_for(mois: str | None):
    """Fichier de clôture qui couvre le mois affiché (mois déclaré à l'import, sinon mois majoritaire des stocks finaux) ;
    à défaut, le plus récent. Ex. octobre 2026 → Output App Fuel, septembre 2026 → Closing de septembre."""
    from fuel_tracking.models import FuelImportBatch as B
    from fuel_tracking.services.closing_upload import CANCEL_KEY, MONTH_KEY, final_stock_month

    if not mois:
        return None
    for b in B.objects.filter(source_type="CLOSING", status__in=("ACCEPTED", "VALIDATION_REQUIRED")).order_by("-received_at"):
        s = b.summary or {}
        if CANCEL_KEY not in s and (s.get(MONTH_KEY) or final_stock_month(b)[0]) == mois:
            return b
    return None


def _batch(source_type, batch=None):
    from fuel_tracking.models import FuelImportBatch as B

    b = batch or B.objects.filter(source_type=source_type, status__in=("ACCEPTED", "VALIDATION_REQUIRED")).order_by("-received_at").first()
    if b is None:
        return None
    return {"fichier": b.file_name, "recu_le": b.received_at.isoformat(), "lignes": b.rows_total, "acceptees": b.rows_accepted + b.rows_warning, "rejetees": b.rows_rejected,
            "doublons": b.rows_duplicate, "statut": b.status, "periode": [str(b.declared_period_start or ""), str(b.declared_period_end or "")]}


def health(top: int = 60, mois: str | None = None) -> dict:
    from django.db.models import Max

    from fuel_tracking.models import FuelEnocMovement, FuelEnocSyncRun, FuelSiteDailyFacts, FuelStockObservation as Obs
    from fuel_tracking.services.conso_comparison import closing_base_sites

    now = timezone.now()
    base = closing_base_sites()

    # Gardiennage : dernier relevé par site (toutes sociétés confondues, jamais additionnés)
    last = {}
    for sid, d in Obs.objects.filter(source_type__in=("LOLIFORCE", "SCSS")).values_list("site_id", "observation_datetime").iterator():
        k = sid.upper()
        if k not in last or d > last[k]:
            last[k] = d
    limit = now - timedelta(days=GUARD_DELAY_DAYS)
    in_base = {s: last.get(s) for s in base}
    never = sorted(s for s, d in in_base.items() if d is None)
    late = sorted(((s, d) for s, d in in_base.items() if d is not None and d < limit), key=lambda x: x[1])
    ok = len(base) - len(never) - len(late)

    enoc_run = FuelEnocSyncRun.objects.exclude(status="RUNNING").order_by("-started_at").first()
    enoc_last_move = FuelEnocMovement.objects.aggregate(v=Max("operation_date"))["v"]
    enoc_sites = {s.upper() for s in FuelEnocMovement.objects.values_list("site_id", flat=True).distinct() if s} if hasattr(FuelEnocMovement, "site_id") else set()
    sf_last = FuelSiteDailyFacts.objects.aggregate(v=Max("date"))["v"]

    return {
        "generated_at": now.isoformat(),
        "sources": {
            "closing": _batch("CLOSING", _closing_batch_for(mois)),
            "gardiennage": {"loliforce": _batch("LOLIFORCE"), "scss": _batch("SCSS"),
                            "dernier_releve": max(last.values()).isoformat() if last else None, "sites_avec_releve": len(last)},
            "enoc": {"derniere_synchro": (enoc_run.finished_at or enoc_run.started_at).isoformat() if enoc_run else None,
                     "statut": enoc_run.status if enoc_run else None, "dernier_mouvement": str(enoc_last_move) if enoc_last_move else None},
            "snowflake": {"derniere_journee": str(sf_last) if sf_last else None, "age_jours": (timezone.localdate() - sf_last).days if sf_last else None},
            "target_fuel": _batch("TARGET_FUEL"),
        },
        "couverture": {
            "sites_base_closing": len(base), "sites_releve_gardien_recent": ok, "seuil_jours": GUARD_DELAY_DAYS,
            "sites_releve_en_retard": len(late), "sites_sans_releve_gardien": len(never),
            "sites_enoc_hors_base_closing": len(enoc_sites - base) if base else None,
        },
        "releves_en_retard": [{"site_id": s, "dernier_releve": d.isoformat(), "jours": (now - d).days} for s, d in late[:top]],
        "sites_sans_releve": never[:top],
        "stock_final": final_stock_coverage(base, top, mois),
    }


def final_stock_coverage(base: set, top: int = 60, mois: str | None = None) -> dict | None:
    """
    Couverture du STOCK FINAL du dernier mois complet dans la source convenue (règles MVP) — remplace, à l'écran, le contrôle
    des relevés de gardiennage, qui ne sont plus une source du bilan :
      - mois avant FUEL_APP_STOCK_START (septembre, octobre 2026) : relevé du fichier de clôture (Closing / Output App Fuel) ;
      - ensuite : relevé ENOC (terrain, gardiennage, niveaux de ravitaillement).
    Relevé exploitable = 0 à 3 j avant la fin du mois (ou jusqu'à N j après pour un relevé de clôture) ; avec réserve = 4 à 14 j avant.
    None si les règles MVP ne sont pas actives.
    """
    from datetime import datetime, time

    from fuel_tracking.models import FuelStockObservation as Obs
    from fuel_tracking.services import stock_period_balance as SB

    c = SB.cfg()
    if not c.get("final_sources") or not base:
        return None
    from datetime import date

    from fuel_tracking.services.output_app_fuel import sites_without_genset

    today = timezone.localdate()
    last_complete = today.replace(day=1) - timedelta(days=1)
    try:
        first = date(int(mois[:4]), int(mois[5:7]), 1) if mois else last_complete.replace(day=1)
    except (TypeError, ValueError):
        first = last_complete.replace(day=1)
    end_day = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    month = first.strftime("%Y-%m")
    sans_ge = sites_without_genset(month)               # liste de l'Output App Fuel DU MOIS seulement (jamais du mois suivant)
    sans_ge = {s for s in base if s in sans_ge}          # sites de la base déclarés sans GE ni cuve : hors périmètre
    base = base - sans_ge
    if month in (c.get("reference_periods") or []):
        # Mois de référence : le stock final est celui de la ligne du fichier Closing, quelle que soit sa date.
        ref = SB.reference_batches(month)
        from fuel_tracking.models import FuelImportRow

        have = {s.upper() for s in Obs.objects.filter(batch_id__in=ref, source_type="CLOSING").values_list("site_id", flat=True)}
        # ligne présente dans le fichier mais sans carburant (pas de GE, tout à 0) : pas un relevé manquant
        in_file = {s.upper() for s in FuelImportRow.objects.filter(batch_id__in=ref).exclude(site_id_resolved=None)
                   .values_list("site_id_resolved", flat=True)}
        sans_carburant = (in_file - have) & base
        base = base - sans_carburant
        absent = sorted(s for s in base if s not in have)
        return {"mois": month, "source": "Fichier Closing de référence (repris tel quel)", "fenetre": [first.isoformat(), end_day.isoformat()],
                "sites_base": len(base), "sites_exploitables": len(base) - len(absent), "sites_avec_reserve": 0, "sites_sans_releve": len(absent),
                "sans_releve": absent[:top], "sites_sans_ge_exclus": len(sans_ge), "lignes_sans_carburant_exclues": len(sans_carburant)}
    use_closing = not SB._uses_app_initial(datetime.combine(first, time.min), c)
    post = c.get("post_days", 0) if use_closing else 0
    tz = timezone.get_current_timezone()
    lo = datetime.combine(end_day - timedelta(days=c["reserve_days"]), time.min, tz)
    if use_closing:
        # mois où le fichier de clôture fait foi : tout stock final relevé dans le mois est retenu (avec réserve), décision du 17/11/2026
        lo = datetime.combine(first + timedelta(days=1), time.min, tz)
    hi = datetime.combine(end_day + timedelta(days=post), time(23, 59, 59), tz)
    qs = Obs.objects.filter(observation_datetime__range=(lo, hi)).exclude(validation_status="REJECTED")
    qs = qs.filter(source_type="CLOSING") if use_closing else qs.filter(source_type__in=c["final_sources"])
    best = {}
    for sid, dt in qs.values_list("site_id", "observation_datetime").iterator():
        k = sid.upper()
        age = (end_day - timezone.localtime(dt).date()).days  # négatif = après la fin du mois
        if k not in best or abs(age) < abs(best[k]):
            best[k] = age
    ok = sorted(s for s in base if s in best and best[s] <= c["fresh_days"])
    reserve = sorted(s for s in base if s in best and best[s] > c["fresh_days"])
    absent = sorted(s for s in base if s not in best)
    return {"mois": month, "source": "Fichier de clôture (Closing / Output App Fuel)" if use_closing else "ENOC",
            "fenetre": [lo.date().isoformat(), hi.date().isoformat()], "sites_base": len(base), "sites_exploitables": len(ok),
            "sites_avec_reserve": len(reserve), "sites_sans_releve": len(absent), "sans_releve": absent[:top],
            "sites_sans_ge_exclus": len(sans_ge)}
