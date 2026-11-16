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


def months_covered(batch, today: date | None = None) -> list[tuple[date, date]]:
    """
    Mois dont le bilan dépend de ce Closing : du mois de son premier relevé au mois qui suit son dernier relevé
    (le stock final du Closing sert de stock initial au mois suivant), sans dépasser le dernier mois complet.
    Du plus ancien au plus récent.
    """
    from django.db.models import Max, Min

    from fuel_tracking.models import FuelStockObservation as Obs

    today = today or timezone.localdate()
    pk = getattr(batch, "pk", None)
    agg = (Obs.objects.filter(batch_id=pk, source_type=SOURCE_TYPE).aggregate(a=Min("observation_datetime"), b=Max("observation_datetime"))
           if pk else {"a": None, "b": None})
    lo = timezone.localtime(agg["a"]).date() if agg["a"] else batch.declared_period_start
    hi = timezone.localtime(agg["b"]).date() if agg["b"] else batch.declared_period_end
    if not lo or not hi:
        return []
    last_complete = today.replace(day=1) - timedelta(days=1)
    nxt = (hi.replace(day=1) + timedelta(days=32)).replace(day=1)
    end_month = min(nxt, last_complete.replace(day=1))
    months, cur = [], lo.replace(day=1)
    while cur <= end_month:
        months.append((cur, cur.replace(day=calendar.monthrange(cur.year, cur.month)[1])))
        cur = (cur + timedelta(days=32)).replace(day=1)
    return months


def recompute_balances(batch) -> list[dict]:
    from fuel_tracking.services import stock_period_balance as SB
    from fuel_tracking.services.period_utils import day_bounds

    res = []
    for start, end in months_covered(batch):
        s, e = day_bounds(start, end)
        rows = SB.compute_balances(s, e)
        res.append({"debut": start.isoformat(), "fin": end.isoformat(), "sites": len(rows),
                    "calculables": sum(1 for r in rows if r.get("consommation_reelle_l") is not None)})
    return res


def process_saved(path: str, user_id: int | None = None) -> dict:
    """Traite un fichier déposé par l'interface puis recalcule les bilans des mois couverts."""
    from django.contrib.auth import get_user_model

    from fuel_tracking.models import FuelImportBatch as B
    from fuel_tracking.services.stock_import_pipeline import process_bytes, sha256

    p = Path(path)
    user = get_user_model().objects.filter(pk=user_id).first() if user_id else None
    if p.exists():
        batch = process_bytes(p.read_bytes(), p.name, SOURCE_TYPE, origin="UPLOAD", user=user, src_path=p)
    else:  # déjà pris en charge par le passage automatique : on retrouve son lot par le nom horodaté
        batch = B.objects.filter(source_type=SOURCE_TYPE, file_name=p.name[:255]).order_by("-received_at").first()
        if batch is None:
            return {"statut": "INTROUVABLE", "fichier": p.name}
    if batch.duplicate_of_id:
        batch = batch.duplicate_of
    from fuel_tracking.models import FuelStockObservation as Obs

    has_stocks = Obs.objects.filter(batch_id=batch.pk, source_type=SOURCE_TYPE).exists()
    if batch.status not in (B.Status.ACCEPTED, B.Status.VALIDATION_REQUIRED) and not has_stocks:
        return {"statut": batch.status, "lot": batch.pk, "bilans": []}
    bilans = recompute_balances(batch)
    logger.info("[fuel_tracking] Closing %s (lot %s) importé, bilans recalculés : %s", batch.file_name, batch.pk, bilans)
    return {"statut": batch.status, "lot": batch.pk, "bilans": bilans}
