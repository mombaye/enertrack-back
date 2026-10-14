# fuel_tracking/services/stock_import_pipeline.py
"""
Pipeline des fichiers du registre stock — répertoire partagé monté FUEL_IMPORT_ROOT
(prod : /mnt/Suivi_carburant_prod) :

    incoming/loliforce/   incoming/scss/   incoming/closing/     ← dépôt des fichiers
    processing/                                                 ← en cours de traitement
    accepted/  rejected/                                        ← fichier original + rapport CSV
    archive/AAAA-MM/                                            ← copie d'archive immuable (empreinte en préfixe)

Étapes : empreinte SHA-256 → doublon technique (même empreinte déjà traitée) refusé sans réimport →
copie d'archive → lignes brutes chargées → contrôles ligne à ligne → statut du lot → déplacement vers
accepted/ ou rejected/ avec le rapport d'erreurs. Le fichier original n'est jamais modifié.
Relance explicite et tracée : relaunch(batch, motif) retraite le fichier archivé (nouveau lot lié).
L'historique refueling (one-shot) passe par la commande import_fuel_stock_file ou l'upload.
"""
from __future__ import annotations

import csv
import hashlib
import logging
import shutil
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from fuel_tracking.services import stock_import_files as F

logger = logging.getLogger(__name__)

INCOMING = {"loliforce": "LOLIFORCE", "scss": "SCSS", "closing": "CLOSING"}
ACCEPTED_EXT = (".xlsx", ".xlsm", ".xlsb")


def root() -> Path:
    return Path(getattr(settings, "FUEL_IMPORT_ROOT", "/mnt/Suivi_carburant_prod"))


def ensure_dirs() -> Path:
    r = root()
    for sub in [f"incoming/{d}" for d in INCOMING] + ["processing", "accepted", "rejected", "archive"]:
        (r / sub).mkdir(parents=True, exist_ok=True)
    return r


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    i = 2
    while True:
        cand = dest.with_name(f"{dest.stem}__{i}{dest.suffix}")
        if not cand.exists():
            return cand
        i += 1


def find_duplicate(file_hash: str):
    from fuel_tracking.models import FuelImportBatch as B

    return B.objects.filter(file_hash=file_hash, duplicate_of__isnull=True).order_by("received_at").first()


def write_report(batch, folder: Path) -> Path | None:
    rows = batch.rows.exclude(validation_status="ACCEPTED").order_by("sheet_name", "row_number")
    path = folder / f"{batch.file_name}.rapport_lot_{batch.pk}.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["lot", "feuille", "ligne", "site_brut", "site", "statut", "codes", "messages"])
        for r in rows.iterator():
            w.writerow([batch.pk, r.sheet_name, r.row_number, r.site_id_raw, r.site_id_resolved, r.validation_status,
                        ",".join(m.get("code", "") for m in r.messages or []),
                        " | ".join(m.get("message", "") for m in r.messages or [])])
    return path


def _finalize_counts(batch):
    from django.db.models import Count

    counts = dict(batch.rows.values_list("validation_status").annotate(n=Count("id")))
    batch.rows_total = sum(counts.values())
    batch.rows_accepted = counts.get("ACCEPTED", 0)
    batch.rows_warning = counts.get("WARNING", 0)
    batch.rows_rejected = counts.get("REJECTED", 0)
    batch.rows_pending = counts.get("PENDING", 0)
    batch.rows_duplicate = sum(
        1 for msgs in batch.rows.filter(validation_status="WARNING").values_list("messages", flat=True).iterator()
        if any(m.get("code") == "DUPLICATE_ALREADY_IMPORTED" for m in msgs or [])
    )


def _decide_status(batch) -> tuple[str, str]:
    from fuel_tracking.models import FuelImportBatch as B

    useful = batch.rows_total - batch.rows_duplicate
    if batch.rows_total == 0:
        return B.Status.REJECTED, "Aucune ligne exploitable trouvée (format non reconnu ?)."
    if useful == 0:
        return B.Status.ACCEPTED, f"Toutes les lignes ({batch.rows_duplicate}) étaient déjà importées : aucune nouvelle donnée."
    if batch.rows_pending:
        return B.Status.VALIDATION_REQUIRED, (f"{batch.rows_pending} ligne(s) à valider, {batch.rows_rejected} rejetée(s), "
                                              f"{batch.rows_accepted + batch.rows_warning - batch.rows_duplicate} acceptée(s).")
    if batch.rows_rejected == useful:
        return B.Status.REJECTED, f"Toutes les lignes ({batch.rows_rejected}) sont rejetées."
    return B.Status.ACCEPTED, f"{batch.rows_accepted + batch.rows_warning - batch.rows_duplicate} ligne(s) acceptée(s), {batch.rows_rejected} rejetée(s)."


def process_bytes(data: bytes, file_name: str, source_type: str, *, origin: str = "FOLDER", user=None,
                  relaunch_of=None, relaunch_reason: str | None = None, src_path: Path | None = None,
                  move_files: bool = True):
    """Traite un fichier. Retourne le lot (FuelImportBatch)."""
    from fuel_tracking.models import FuelImportBatch as B

    if source_type not in F.PROCESSORS:
        raise ValueError(f"Type de source inconnu : {source_type}")
    file_hash = sha256(data)
    batch = B.objects.create(source_type=source_type, file_name=Path(file_name).name[:255], file_hash=file_hash,
                             file_size=len(data), origin=origin, received_by=user if (user and user.is_authenticated) else None,
                             relaunch_of=relaunch_of, relaunch_reason=relaunch_reason)
    r = ensure_dirs() if move_files else None

    dup = None if relaunch_of else find_duplicate(file_hash)
    if dup is not None and dup.pk != batch.pk:
        batch.duplicate_of = dup
        batch.status = B.Status.REJECTED
        batch.status_message = (f"Doublon technique : fichier identique (empreinte) déjà reçu le "
                                f"{timezone.localtime(dup.received_at):%d/%m/%Y %H:%M} (lot #{dup.pk}, {dup.get_status_display()}). "
                                "Aucune donnée réimportée — utiliser la relance tracée si un retraitement est nécessaire.")
        batch.processed_at = timezone.now()
        if r is not None and src_path is not None and src_path.exists():
            dest = _unique(r / "rejected" / f"{timezone.now():%Y%m%d_%H%M%S}_{file_hash[:8]}_{src_path.name}")
            shutil.move(str(src_path), dest)
            batch.stored_path = str(dest)
        batch.save()
        return batch

    processing_path = None
    if r is not None:
        archive_dir = r / "archive" / f"{timezone.now():%Y-%m}"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive = archive_dir / f"{file_hash[:16]}_{Path(file_name).name}"
        if not archive.exists():
            archive.write_bytes(data)
        batch.archive_path = str(archive)
        if src_path is not None and src_path.exists():
            processing_path = _unique(r / "processing" / src_path.name)
            shutil.move(str(src_path), processing_path)
    batch.status = B.Status.PROCESSING
    batch.save()

    try:
        with transaction.atomic():
            stats = F.PROCESSORS[source_type](batch, data, file_name)
            _finalize_counts(batch)
            batch.status, batch.status_message = _decide_status(batch)
            batch.summary = stats
    except Exception as exc:  # échec technique : le lot reste tracé, le fichier va dans rejected/
        logger.exception("[stock_registry] échec du traitement de %s", file_name)
        batch.status = B.Status.FAILED
        batch.status_message = f"Échec technique : {exc}"
    batch.processed_at = timezone.now()

    if r is not None:
        folder = r / ("accepted" if batch.status in (B.Status.ACCEPTED, B.Status.VALIDATION_REQUIRED) else "rejected")
        if processing_path is not None and processing_path.exists():
            dest = _unique(folder / f"{timezone.now():%Y%m%d_%H%M%S}_{file_hash[:8]}_{processing_path.name}")
            shutil.move(str(processing_path), dest)
            batch.stored_path = str(dest)
        else:
            batch.stored_path = batch.archive_path
        try:
            batch.report_path = str(write_report(batch, folder))
        except OSError:
            logger.exception("[stock_registry] rapport non écrit pour le lot %s", batch.pk)
    batch.save()
    return batch


def scan_incoming(user=None) -> list:
    """Traite tous les fichiers déposés dans incoming/* (ordre alphabétique)."""
    r = ensure_dirs()
    batches = []
    for folder, source_type in INCOMING.items():
        for path in sorted((r / "incoming" / folder).iterdir()):
            if not path.is_file() or path.name.startswith((".", "~$")):
                continue
            if path.suffix.lower() not in ACCEPTED_EXT:
                logger.warning("[stock_registry] fichier ignoré (extension) : %s", path)
                continue
            batches.append(process_bytes(path.read_bytes(), path.name, source_type, origin="FOLDER", user=user, src_path=path))
    return batches


def relaunch(batch, reason: str, user=None):
    """Relance explicite et tracée d'un lot à partir de sa copie d'archive."""
    from fuel_tracking.services.stock_registry import log_correction

    if not reason or not reason.strip():
        raise ValueError("Motif de relance obligatoire.")
    src = Path(batch.archive_path or batch.stored_path or "")
    if not src.is_file():
        raise ValueError("Fichier archivé introuvable : relance impossible.")
    data = src.read_bytes()
    log_correction("BATCH", batch.pk, "RELAUNCH", reason, user=user, author_label=None if user else "système")
    return process_bytes(data, batch.file_name, batch.source_type, origin=batch.origin, user=user, relaunch_of=batch,
                         relaunch_reason=reason, src_path=None)
