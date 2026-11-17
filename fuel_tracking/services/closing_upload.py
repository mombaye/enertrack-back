# fuel_tracking/services/closing_upload.py
"""
Rubrique « Configuration » du Suivi Carburant : envoi du fichier de clôture (Closing) depuis l'interface.

Le fichier envoyé est déposé dans FUEL_IMPORT_ROOT/incoming/closing/ — exactement comme un dépôt manuel dans le
partage — puis traité en arrière-plan (tâche Celery « fuel_tracking.process_closing_upload », file interactive) par
le pipeline d'import existant (stock_import_pipeline.process_bytes : empreinte anti-doublon, archive, rapport,
dossiers accepted / rejected). Les bilans des mois couverts par le fichier sont ensuite recalculés, du plus ancien au
plus récent, pour que le stock initial historique soit visible tout de suite. Si la tâche ne peut pas être lancée,
le fichier reste dans incoming/closing/ et le passage automatique (toutes les 15 min) le traite.
"""
from __future__ import annotations

import calendar
import logging
import re
from datetime import date, timedelta
from pathlib import Path

from django.utils import timezone

logger = logging.getLogger(__name__)

SOURCE_TYPE = "CLOSING"
INCOMING_SUBDIR = "closing"


def incoming_dir() -> Path:
    from fuel_tracking.services.stock_import_pipeline import ensure_dirs

    return ensure_dirs() / "incoming" / INCOMING_SUBDIR


def save_upload(data: bytes, file_name: str) -> Path:
    """Dépose le fichier dans incoming/closing/ (nom horodaté, jamais d'écrasement). Retourne son chemin."""
    from fuel_tracking.services.stock_import_pipeline import _unique

    safe = re.sub(r"[^\w.\- ()]+", "_", Path(file_name).name).strip() or "closing.xlsb"
    dest = _unique(incoming_dir() / f"{timezone.now():%Y%m%d_%H%M%S}_{safe}")
    dest.write_bytes(data)
    return dest


def pending_files() -> list[dict]:
    """Fichiers déposés dans incoming/closing/ pas encore traités."""
    try:
        d = incoming_dir()
    except OSError:
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file() and not p.name.startswith((".", "~$")):
            st = p.stat()
            out.append({"file_name": p.name, "file_size": st.st_size,
                        "deposited_at": timezone.datetime.fromtimestamp(st.st_mtime, tz=timezone.get_current_timezone()).isoformat()})
    return out


MONTH_KEY = "mois_declare"          # mois couvert par le fichier, déclaré à l'import (AAAA-MM)
CANCEL_KEY = "annulation"           # retrait du fichier (qui, quand, ce qui a été supprimé)
POST_TOLERANCE_DAYS = 6             # relevé de fin de mois fait jusqu'à 6 jours après (campagne de clôture)


def parse_month(value) -> date:
    """« AAAA-MM » → 1er du mois. ValueError si le format est invalide."""
    s = str(value or "").strip()
    try:
        y, m = (int(x) for x in s.split("-")[:2])
        return date(y, m, 1)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Mois invalide : « {s} » (format attendu AAAA-MM).") from exc


def _month_range(first: date) -> tuple[date, date]:
    return first, first.replace(day=calendar.monthrange(first.year, first.month)[1])


def _next_month(first: date) -> date:
    return (first + timedelta(days=32)).replace(day=1)


def months_covered(batch, today: date | None = None) -> list[tuple[date, date]]:
    """
    Mois dont le bilan dépend de ce fichier de clôture, du plus ancien au plus récent, sans dépasser le dernier mois complet :
      - mois déclaré à l'import (ex. 2026-10) + mois suivant (son stock final sert de stock initial au mois suivant) ;
      - sans mois déclaré (dépôt direct dans le partage) : du mois du premier relevé au mois qui suit le dernier relevé.
    """
    from django.db.models import Max, Min

    from fuel_tracking.models import FuelStockObservation as Obs

    today = today or timezone.localdate()
    last_complete = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    declared = (getattr(batch, "summary", None) or {}).get(MONTH_KEY)
    if declared:
        first = parse_month(declared)
        return [_month_range(m) for m in (first, _next_month(first)) if m <= last_complete]
    pk = getattr(batch, "pk", None)
    agg = (Obs.objects.filter(batch_id=pk, source_type=SOURCE_TYPE).aggregate(a=Min("observation_datetime"), b=Max("observation_datetime"))
           if pk else {"a": None, "b": None})
    lo = timezone.localtime(agg["a"]).date() if agg["a"] else batch.declared_period_start
    hi = timezone.localtime(agg["b"]).date() if agg["b"] else batch.declared_period_end
    if not lo or not hi:
        return []
    end_month = min(_next_month(hi.replace(day=1)), last_complete)
    months, cur = [], lo.replace(day=1)
    while cur <= end_month:
        months.append(_month_range(cur))
        cur = _next_month(cur)
    return months


def final_stock_month(batch) -> tuple[str | None, int]:
    """Mois majoritaire des stocks FINAUX importés par le lot (un relevé fait jusqu'à 6 j après la fin du mois compte pour
    ce mois) et nombre de stocks finaux : sert à contrôler le mois déclaré à l'import."""
    from collections import Counter

    from fuel_tracking.models import FuelStockObservation as Obs

    months = Counter()
    qs = Obs.objects.filter(batch_id=batch.pk, source_type=SOURCE_TYPE, level_raw__contains="stock final")
    for dt in qs.values_list("observation_datetime", flat=True):
        d = timezone.localtime(dt).date() - timedelta(days=POST_TOLERANCE_DAYS)
        months[f"{d.year:04d}-{d.month:02d}"] += 1
    if not months:
        return None, 0
    return months.most_common(1)[0][0], sum(months.values())


def declare_month(batch, mois: str) -> dict:
    """Enregistre le mois couvert par le fichier (période déclarée du lot) et contrôle sa cohérence avec les dates du fichier."""
    first = parse_month(mois)
    start, end = _month_range(first)
    dominant, n = final_stock_month(batch)
    info = {"mois": first.strftime("%Y-%m"), "stocks_finaux": n, "mois_majoritaire_des_dates": dominant}
    if dominant and dominant != info["mois"]:
        info["avertissement"] = (f"Mois déclaré {info['mois']} mais la majorité des stocks finaux du fichier sont datés de {dominant} : "
                                 f"vérifier le mois choisi.")
    batch.refresh_from_db(fields=["summary"])
    batch.declared_period_start, batch.declared_period_end = start, end
    batch.summary = {**(batch.summary or {}), MONTH_KEY: info["mois"], "controle_mois": info}
    batch.save(update_fields=["declared_period_start", "declared_period_end", "summary"])
    return info


RECALC_KEY = "recalcul_bilans"  # avancement du recalcul, enregistré dans FuelImportBatch.summary (suivi à l'écran)


def _set_progress(batches, data: dict) -> None:
    """Enregistre l'avancement du recalcul sur le(s) lot(s) suivis par l'interface (aucune migration : champ JSON summary)."""
    for b in {x.pk: x for x in batches if x is not None}.values():
        b.refresh_from_db(fields=["summary"])
        b.summary = {**(b.summary or {}), RECALC_KEY: data}
        b.save(update_fields=["summary"])


def recompute_months(months: list[tuple[date, date]], track) -> list[dict]:
    """Recalcule les bilans des mois donnés, du plus ancien au plus récent ; avancement enregistré mois par mois sur `track`."""
    from fuel_tracking.services import stock_period_balance as SB
    from fuel_tracking.services.period_utils import day_bounds

    months = sorted(months)
    started = timezone.now().isoformat()
    res = []
    _set_progress(track, {"statut": "EN_COURS", "debut": started, "mois_total": len(months), "mois_faits": 0, "mois": []})
    for start, end in months:
        s, e = day_bounds(start, end)
        rows = SB.compute_balances(s, e)
        res.append({"debut": start.isoformat(), "fin": end.isoformat(), "sites": len(rows),
                    "calculables": sum(1 for r in rows if r.get("consommation_reelle_l") is not None)})
        _set_progress(track, {"statut": "EN_COURS", "debut": started, "mois_total": len(months), "mois_faits": len(res), "mois": res})
    _set_progress(track, {"statut": "TERMINE", "debut": started, "fin": timezone.now().isoformat(), "mois_total": len(months),
                          "mois_faits": len(res), "mois": res})
    return res


def recompute_balances(batch, track=None) -> list[dict]:
    """Recalcule les bilans des mois couverts par le lot (voir months_covered)."""
    return recompute_months(months_covered(batch), track or [batch])


def process_saved(path: str, user_id: int | None = None, mois: str | None = None) -> dict:
    """Traite un fichier déposé par l'interface, enregistre le mois déclaré, puis recalcule les bilans des mois couverts."""
    from django.contrib.auth import get_user_model

    from fuel_tracking.models import FuelImportBatch as B
    from fuel_tracking.models import FuelStockObservation as Obs
    from fuel_tracking.services.stock_import_pipeline import process_bytes

    p = Path(path)
    user = get_user_model().objects.filter(pk=user_id).first() if user_id else None
    if p.exists():
        uploaded = process_bytes(p.read_bytes(), p.name, SOURCE_TYPE, origin="UPLOAD", user=user, src_path=p)
    else:  # déjà pris en charge par le passage automatique : on retrouve son lot par le nom horodaté
        uploaded = B.objects.filter(source_type=SOURCE_TYPE, file_name=p.name[:255]).order_by("-received_at").first()
        if uploaded is None:
            return {"statut": "INTROUVABLE", "fichier": p.name}
    batch = uploaded.duplicate_of if uploaded.duplicate_of_id else uploaded  # doublon : on recalcule à partir du lot d'origine
    if mois:
        declare_month(uploaded, mois)
        if batch.pk != uploaded.pk and not (batch.summary or {}).get(MONTH_KEY):
            declare_month(batch, mois)
    has_stocks = Obs.objects.filter(batch_id=batch.pk, source_type=SOURCE_TYPE).exists()
    if batch.status not in (B.Status.ACCEPTED, B.Status.VALIDATION_REQUIRED) and not has_stocks:
        _set_progress([uploaded], {"statut": "NON_LANCE", "motif": "Aucun stock importé : pas de bilan à recalculer."})
        return {"statut": batch.status, "lot": batch.pk, "bilans": []}
    uploaded.refresh_from_db()
    try:
        bilans = recompute_months(months_covered(uploaded if mois else batch), track=[uploaded, batch])
    except Exception as exc:
        _set_progress([uploaded, batch], {"statut": "ECHEC", "fin": timezone.now().isoformat(), "motif": f"{type(exc).__name__} : {exc}"[:500]})
        raise
    logger.info("[fuel_tracking] Closing %s (lot %s) importé, bilans recalculés : %s", batch.file_name, batch.pk, bilans)
    return {"statut": batch.status, "lot": batch.pk, "bilans": bilans}


def withdraw_batch(batch, user=None, motif: str = "") -> dict:
    """
    Retire un fichier de clôture importé : supprime les données qu'il a créées (stocks, mouvements, targets, lignes d'import),
    conserve le lot (marqué « retiré », avec qui / quand / quoi) et le fichier archivé, puis renvoie les mois à recalculer.
    Les cuves et modèles de cuves créés par le fichier restent (référentiel) ; les courbes CPH GENSET DB aussi.
    Le même fichier peut ensuite être réimporté (l'empreinte d'un lot retiré n'est plus considérée comme un doublon).
    """
    from django.db import transaction

    from fuel_tracking.models import FuelImportBatch as B
    from fuel_tracking.models import FuelImportRow, FuelMovement, FuelSiteTarget, FuelStockObservation as Obs

    if batch.source_type != SOURCE_TYPE:
        raise ValueError("Seuls les fichiers de clôture (Closing / Output App Fuel) se retirent depuis la rubrique Configuration.")
    if CANCEL_KEY in (batch.summary or {}):
        raise ValueError("Ce fichier a déjà été retiré.")
    if batch.status in (B.Status.RECEIVED, B.Status.PROCESSING):
        raise ValueError("Import en cours : attendre la fin du traitement avant de retirer le fichier.")
    months = months_covered(batch)  # avant suppression : dépend des relevés du lot
    with transaction.atomic():
        counts = {
            "stocks_supprimes": Obs.objects.filter(batch=batch).delete()[0],
            "mouvements_supprimes": FuelMovement.objects.filter(batch=batch).delete()[0],
            "targets_supprimes": FuelSiteTarget.objects.filter(batch=batch).delete()[0],
        }
        counts["lignes_supprimees"] = FuelImportRow.objects.filter(batch=batch).delete()[0]
        who = getattr(user, "username", None) or "système"
        now = timezone.now()
        batch.summary = {**(batch.summary or {}), CANCEL_KEY: {"par": who, "le": now.isoformat(), "motif": motif[:500], **counts,
                                                               "mois_a_recalculer": [m[0].isoformat()[:7] for m in months]}}
        batch.status = B.Status.REJECTED
        batch.status_message = (f"Fichier retiré par {who} le {timezone.localtime(now):%d/%m/%Y %H:%M} : "
                                f"{counts['stocks_supprimes']} stock(s) supprimé(s)" + (f" — {motif[:200]}" if motif else "") + ".")
        batch.save(update_fields=["summary", "status", "status_message"])
    return {**counts, "mois": [m[0].isoformat()[:7] for m in months]}
