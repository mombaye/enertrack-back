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


def _batch(source_type):
    from fuel_tracking.models import FuelImportBatch as B

    b = B.objects.filter(source_type=source_type, status__in=("ACCEPTED", "VALIDATION_REQUIRED")).order_by("-received_at").first()
    if b is None:
        return None
    return {"fichier": b.file_name, "recu_le": b.received_at.isoformat(), "lignes": b.rows_total, "acceptees": b.rows_accepted + b.rows_warning, "rejetees": b.rows_rejected,
            "doublons": b.rows_duplicate, "statut": b.status, "periode": [str(b.declared_period_start or ""), str(b.declared_period_end or "")]}


def health(top: int = 60) -> dict:
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
            "closing": _batch("CLOSING"),
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
    }
