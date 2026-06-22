# billing/tasks.py — PATCH import_status_update_task
#
# Modifications :
#   (1) Lecture de DEUX colonnes du fichier Excel :
#       - "Statut"          → certification status (SonatelInvoice.status)
#       - "Statut Paiement" → payment status (SonatelInvoice.payment_status)
#   (2) Mapping des valeurs métier Excel → choix Django
#   (3) Mise à jour des deux champs + propagation sur MonthlySynthesis
#
# Le reste de billing/tasks.py est identique.



# billing/tasks.py
import io
import traceback
from decimal import Decimal
from typing import Dict, Optional, Set, Tuple

import pandas as pd
from celery import shared_task
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q, Sum, Count, Min, Max, Avg
from django.utils import timezone

from .models import (
    ImportBatch, ImportIssue, SonatelInvoice,
    MonthlySynthesis, ContractMonth, ContractSiteLink, TariffRate,
)
from .views import (
    _to_date_fr, _to_contract_str, _is_blank, _norm_header,
    _row_snapshot, _to_int, _compute_target_fields, _build_monthly_payloads,
    COLUMN_MAP, DATE_COLS, INT_COLS, DEC_COLS,
)
from .utils import parse_decimal_fr


def _save_progress(batch, pct, message="", meta=None):
    batch.task_progress = min(pct, 99)
    batch.task_message = message
    batch.task_updated_at = timezone.now()
    fields = ["task_progress", "task_message", "task_updated_at"]
    if meta is not None:
        batch.task_meta = meta
        fields.append("task_meta")
    batch.save(update_fields=fields)


# ─── Mapping statut certification (colonne "Statut") ─────────────────────────
# Valeurs observées dans le fichier Excel de base billing 2025

CERT_STATUS_MAP: dict[str, str] = {
    # Certifiés → VALIDATED
    "sites certifies - ok paiement":             SonatelInvoice.Status.VALIDATED,
    "sites d2 - certifies":                      SonatelInvoice.Status.VALIDATED,
    "sites d2 avec conso hors telco":            SonatelInvoice.Status.VALIDATED,
    "sites e1_e2 certifies - hors scope":        SonatelInvoice.Status.VALIDATED,

    # Contestés / en attente explication → CONTESTED
    "pas ok avec la consommation facturee":      SonatelInvoice.Status.CONTESTED,
    "demande d explications sur la penalite ou tarif et son calcul": SonatelInvoice.Status.CONTESTED,

    # Rejet / inconnu → CREATED (statut initial)
    "numero de contrat inconu":                  SonatelInvoice.Status.CREATED,
    "sites e1_e2 a verifier - hors scope":       SonatelInvoice.Status.CREATED,
    "facture deja recue":                        SonatelInvoice.Status.CREATED,
}

# ─── Mapping statut paiement (colonne "Statut Paiement") ─────────────────────

PAYMENT_STATUS_MAP: dict[str, str] = {
    "paye":                           "PAID",
    "payé":                           "PAID",
    "paye ":                          "PAID",
    "impayee":                        "UNPAID",
    "impayée":                        "UNPAID",
    "hors scope / remplace / annul":  "OUT_OF_SCOPE",
    "hors scope / remplace / annul ": "OUT_OF_SCOPE",
    "hors scope":                     "OUT_OF_SCOPE",
    "annule":                         "OUT_OF_SCOPE",
    "annulé":                         "OUT_OF_SCOPE",
}


import unicodedata

def _normalize_status_str(s: str) -> str:
    """Normalise une valeur de statut pour le lookup : minuscule, sans accents."""
    s = str(s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s


def _parse_cert_status(raw) -> str | None:
    """Mappe la valeur Excel 'Statut' vers SonatelInvoice.Status."""
    if _is_blank(raw):
        return None
    key = _normalize_status_str(str(raw))
    return CERT_STATUS_MAP.get(key)


def _parse_payment_status(raw) -> str | None:
    """Mappe la valeur Excel 'Statut Paiement' vers PaymentStatus."""
    if _is_blank(raw):
        return None
    key = _normalize_status_str(str(raw))
    return PAYMENT_STATUS_MAP.get(key)


@shared_task(bind=True, name="billing.import_status_update")
def import_status_update_task(
    self,
    batch_id: int,
    storage_key: str,
    default_status: str = "VALIDATED",
):
    """
    Import asynchrone de mise à jour des deux statuts.

    Lit un fichier facturation Sénélec (même format que l'import normal,
    ou fichier de base billing avec colonnes 'Statut' et 'Statut Paiement').

    Pour chaque ligne :
      - Identifie la facture via (contrat, facture, debut, fin)
      - Met à jour SonatelInvoice.status          si colonne 'Statut' présente
      - Met à jour SonatelInvoice.payment_status  si colonne 'Statut Paiement' présente
      - Propage les deux statuts sur MonthlySynthesis

    Colonnes reconnues pour les statuts (insensibles à la casse) :
      Certification : "Statut" → mapping CERT_STATUS_MAP
      Paiement      : "Statut Paiement" → mapping PAYMENT_STATUS_MAP

    Si aucune colonne 'Statut' n'est présente, utilise default_status
    comme statut de certification cible.
    """
    batch = ImportBatch.objects.get(pk=batch_id)
    batch.task_status   = ImportBatch.TaskStatus.RUNNING
    batch.task_progress = 0
    batch.task_message  = "Démarrage mise à jour statuts…"
    batch.task_updated_at = timezone.now()
    batch.save(update_fields=["task_status", "task_progress", "task_message", "task_updated_at"])

    try:
        valid_cert_statuses = {s.value for s in SonatelInvoice.Status}
        if default_status not in valid_cert_statuses:
            raise ValueError(f"Statut invalide: {default_status!r}.")

        # ── Lecture fichier ───────────────────────────────────────────────────
        _save_progress(batch, 2, "Lecture du fichier…")
        with default_storage.open(storage_key, "rb") as fh:
            file_bytes = fh.read()

        df = pd.read_excel(io.BytesIO(file_bytes), dtype=object)
        total_rows = len(df)
        del file_bytes
        _save_progress(batch, 5, f"Fichier chargé — {total_rows} lignes")

        # ── Renommage colonnes facture ────────────────────────────────────────
        normed_cols = {_norm_header(c): c for c in df.columns}
        normed_map  = {_norm_header(src): dst for src, dst in COLUMN_MAP.items()}
        rename_map  = {normed_cols[src]: dst for src, dst in normed_map.items() if src in normed_cols}
        df = df.rename(columns=rename_map)

        required_cols = ["numero_compte_contrat", "numero_facture", "date_debut_periode", "date_fin_periode"]
        missing_cols  = [c for c in required_cols if c not in df.columns]
        if missing_cols:
            raise ValueError(f"Colonnes obligatoires manquantes: {missing_cols}")

        # ── Détection colonnes statut ─────────────────────────────────────────
        # Cherche "Statut" (certification) et "Statut Paiement" dans les colonnes
        # originales ET dans les colonnes renommées (COLUMN_MAP peut avoir renommé)
        all_cols_lower = {c.lower().strip(): c for c in df.columns}

        # Colonne certification
        cert_col = None
        for candidate in ["statut", "statut ", "status"]:
            if candidate in all_cols_lower:
                cert_col = all_cols_lower[candidate]
                break

        # Colonne paiement
        payment_col = None
        for candidate in ["statut paiement", "statut paiement ", "payment_status", "statut_paiement"]:
            if candidate in all_cols_lower:
                payment_col = all_cols_lower[candidate]
                break

        has_cert_col    = cert_col is not None
        has_payment_col = payment_col is not None

        import logging
        logger = logging.getLogger(__name__)
        logger.info(
            "[status_update] Colonnes détectées — cert=%r payment=%r",
            cert_col, payment_col,
        )

        # ── Pré-charger les contrats ──────────────────────────────────────────
        file_contracts = set(
            df["numero_compte_contrat"].dropna().map(_to_contract_str).dropna().tolist()
        )
        linked_contracts = set(
            ContractSiteLink.objects.filter(
                numero_compte_contrat__in=file_contracts,
                site__invoice_payment__iexact="Aktivco",
                site__grid_fee=True,
            ).values_list("numero_compte_contrat", flat=True)
        )

        existing_map: dict[tuple, int] = {}
        for pk, acc, fac, d1, d2 in SonatelInvoice.objects.filter(
            numero_compte_contrat__in=linked_contracts,
        ).values_list("id", "numero_compte_contrat", "numero_facture",
                      "date_debut_periode", "date_fin_periode"):
            existing_map[(acc, str(fac), str(d1), str(d2))] = pk

        _save_progress(batch, 10, f"{len(existing_map)} factures pré-chargées")

        # ── Parcours des lignes ───────────────────────────────────────────────
        updated_count  = 0
        not_found_rows = []
        no_site_rows   = []
        error_rows     = []
        issues_buf     = []
        seen_keys: set = set()

        # Structure : {pk: {"cert": str|None, "payment": str|None}}
        pk_to_updates: dict[int, dict] = {}

        CHUNK = 100
        for i, row in df.iterrows():
            excel_row = int(i) + 2

            if i % CHUNK == 0:
                pct = 10 + int((i / max(total_rows, 1)) * 80)
                _save_progress(batch, pct, f"Traitement… {i}/{total_rows}")

            raw_row = _row_snapshot(row.to_dict())

            try:
                acc = _to_contract_str(row.get("numero_compte_contrat"))
                fac = None if _is_blank(row.get("numero_facture")) else str(row.get("numero_facture")).strip()
                d1  = _to_date_fr(row.get("date_debut_periode"))
                d2  = _to_date_fr(row.get("date_fin_periode"))
            except Exception as e:
                error_rows.append({"row": excel_row, "error": f"Parse error: {e}"})
                continue

            if not acc or not fac or not d1 or not d2:
                error_rows.append({"row": excel_row, "error": "Identifiants manquants"})
                continue

            dedup_key = (acc, fac, str(d1), str(d2))
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            if acc not in linked_contracts:
                no_site_rows.append({"row": excel_row, "contrat": acc, "facture": fac})
                continue

            if dedup_key not in existing_map:
                not_found_rows.append({"row": excel_row, "contrat": acc, "facture": fac,
                                       "date_debut": str(d1), "date_fin": str(d2)})
                issues_buf.append(ImportIssue(
                    batch=batch, row_number=excel_row,
                    severity=ImportIssue.Severity.WARN, field="numero_facture",
                    message=f"Facture {fac} / contrat {acc} introuvable.",
                    raw_data=raw_row,
                ))
                continue

            pk = existing_map[dedup_key]

            # ── Résolution statut certification ───────────────────────────────
            cert_status = None
            if has_cert_col:
                cert_status = _parse_cert_status(row.get(cert_col))
                if cert_status is None and not _is_blank(row.get(cert_col)):
                    # Valeur inconnue → log + fallback default_status
                    issues_buf.append(ImportIssue(
                        batch=batch, row_number=excel_row,
                        severity=ImportIssue.Severity.WARN,
                        field="statut",
                        message=f"Valeur 'Statut' non reconnue: {row.get(cert_col)!r}. Fallback: {default_status}",
                        raw_data=raw_row,
                    ))
                    cert_status = default_status
            else:
                cert_status = default_status

            # ── Résolution statut paiement ────────────────────────────────────
            payment_status = None
            if has_payment_col and not _is_blank(row.get(payment_col)):
                payment_status = _parse_payment_status(row.get(payment_col))
                if payment_status is None:
                    issues_buf.append(ImportIssue(
                        batch=batch, row_number=excel_row,
                        severity=ImportIssue.Severity.WARN,
                        field="statut_paiement",
                        message=f"Valeur 'Statut Paiement' non reconnue: {row.get(payment_col)!r}",
                        raw_data=raw_row,
                    ))

            pk_to_updates[pk] = {
                "cert":    cert_status,
                "payment": payment_status,
            }

        # ── Mise à jour en base (par lots de 500) ─────────────────────────────
        _save_progress(batch, 91, "Mise à jour des statuts en base…")

        ids_all = list(pk_to_updates.keys())
        BATCH_SIZE = 500
        now = timezone.now()

        for chunk_start in range(0, len(ids_all), BATCH_SIZE):
            chunk_ids = ids_all[chunk_start: chunk_start + BATCH_SIZE]

            # Groupe par (cert_status, payment_status) pour bulk_update rapide
            from itertools import groupby
            groups: dict[tuple, list[int]] = {}
            for pk in chunk_ids:
                upd = pk_to_updates[pk]
                key = (upd["cert"], upd["payment"])
                groups.setdefault(key, []).append(pk)

            for (cert_s, pay_s), pks in groups.items():
                update_fields: dict = {
                    "status_updated_at": now,
                    "status_last_batch": batch,
                }
                if cert_s is not None:
                    update_fields["status"] = cert_s
                if pay_s is not None:
                    update_fields["payment_status"] = pay_s
                    update_fields["payment_status_updated_at"] = now

                SonatelInvoice.objects.filter(pk__in=pks).update(**update_fields)

                # Propager sur MonthlySynthesis
                ms_fields: dict = {}
                if cert_s is not None:
                    ms_fields["status"] = cert_s
                if ms_fields:
                    MonthlySynthesis.objects.filter(source_id__in=pks).update(**ms_fields)

            updated_count += len(chunk_ids)

        if issues_buf:
            ImportIssue.objects.bulk_create(issues_buf)

        # ── Résultat ──────────────────────────────────────────────────────────
        cert_updated    = sum(1 for u in pk_to_updates.values() if u["cert"] is not None)
        payment_updated = sum(1 for u in pk_to_updates.values() if u["payment"] is not None)

        final_meta = {
            "total_rows":        total_rows,
            "updated":           updated_count,
            "cert_updated":      cert_updated,
            "payment_updated":   payment_updated,
            "not_found":         len(not_found_rows),
            "no_site":           len(no_site_rows),
            "errors":            len(error_rows),
            "cert_col_detected":    cert_col,
            "payment_col_detected": payment_col,
            "not_found_rows": not_found_rows[:500],
            "no_site_rows":   no_site_rows[:500],
            "error_rows":     error_rows[:200],
        }

        batch.task_status   = ImportBatch.TaskStatus.SUCCESS
        batch.task_progress = 100
        batch.task_message  = (
            f"Terminé — {updated_count} factures · "
            f"{cert_updated} statuts certif · "
            f"{payment_updated} statuts paiement · "
            f"{len(not_found_rows)} non trouvées"
        )
        batch.task_meta       = final_meta
        batch.task_updated_at = timezone.now()
        batch.save(update_fields=[
            "task_status", "task_progress", "task_message", "task_meta", "task_updated_at"
        ])
        return final_meta

    except Exception as exc:
        batch.task_status   = ImportBatch.TaskStatus.FAILURE
        batch.task_progress = 100
        batch.task_message  = f"Erreur: {str(exc)[:500]}"
        batch.task_meta     = {"traceback": traceback.format_exc()[-3000:]}
        batch.task_updated_at = timezone.now()
        batch.save(update_fields=[
            "task_status", "task_progress", "task_message", "task_meta", "task_updated_at"
        ])
        raise

    finally:
        try:
            default_storage.delete(storage_key)
        except Exception:
            pass







def _save_progress(batch: ImportBatch, pct: int, message: str = "", meta: dict = None):
    batch.task_progress = min(pct, 99)
    batch.task_message = message
    batch.task_updated_at = timezone.now()
    fields = ["task_progress", "task_message", "task_updated_at"]
    if meta is not None:
        batch.task_meta = meta
        fields.append("task_meta")
    batch.save(update_fields=fields)


@shared_task(bind=True, name="billing.import_invoices")
def import_invoices_task(self, batch_id: int, storage_key: str, echeance_str: str):
    """
    Import asynchrone des factures Sonatel.
    Lit le fichier depuis Django default_storage (volume partagé web/celery).
    """
    batch = ImportBatch.objects.get(pk=batch_id)
    batch.task_status = ImportBatch.TaskStatus.RUNNING
    batch.task_progress = 0
    batch.task_message = "Démarrage…"
    batch.task_updated_at = timezone.now()
    batch.save(update_fields=["task_status", "task_progress", "task_message", "task_updated_at"])

    try:
        echeance = _to_date_fr(echeance_str)
        if not echeance:
            raise ValueError(f"Date d'échéance invalide: {echeance_str!r}")

        # ── Lecture fichier depuis storage partagé ────────────────────────────
        # Le fichier est dans MEDIA_ROOT (volume Docker partagé web+celery).
        # On lit tout en mémoire pour ne pas laisser de handle ouvert.
        _save_progress(batch, 2, "Lecture du fichier…")
        with default_storage.open(storage_key, "rb") as fh:
            file_bytes = fh.read()

        df = pd.read_excel(io.BytesIO(file_bytes), dtype=object)
        total_rows = len(df)
        del file_bytes  # libérer la mémoire

        _save_progress(batch, 5, f"Fichier chargé — {total_rows} lignes")

        # ── Renommage colonnes ────────────────────────────────────────────────
        normed_cols = {_norm_header(c): c for c in df.columns}
        normed_map  = {_norm_header(src): dst for src, dst in COLUMN_MAP.items()}
        rename_map  = {normed_cols[src]: dst for src, dst in normed_map.items() if src in normed_cols}
        df = df.rename(columns=rename_map)

        required_cols = ["numero_compte_contrat", "numero_facture", "date_debut_periode", "date_fin_periode"]
        missing_cols  = [c for c in required_cols if c not in df.columns]
        if missing_cols:
            ImportIssue.objects.bulk_create([ImportIssue(
                batch=batch, row_number=None, severity=ImportIssue.Severity.ERROR,
                field=";".join(missing_cols),
                message=f"Colonnes obligatoires manquantes: {', '.join(missing_cols)}",
                raw_data={"columns": list(df.columns)},
            )])
            raise ValueError(f"Colonnes manquantes: {missing_cols}")

        # ── Pré-charger mapping contrat → site ────────────────────────────────
        contracts = set(
            df["numero_compte_contrat"].dropna().map(_to_contract_str).dropna().tolist()
        ) if "numero_compte_contrat" in df.columns else set()

        contract_to_site_id = dict(
            ContractSiteLink.objects.filter(
                numero_compte_contrat__in=contracts,
                site__invoice_payment__iexact="Aktivco",
                site__grid_fee=True,
            ).values_list("numero_compte_contrat", "site_id")
        )

        # ── Compteurs ─────────────────────────────────────────────────────────
        created_count = updated_count = monthly_total = 0
        skipped_missing_required = skipped_invalid_period = skipped_dup_in_file = 0
        issues_buf: list = []
        seen_in_file: set = set()
        missing_contracts: set = set()
        affected_keys: Set[Tuple[str, int, int]] = set()
        tariff_cache: Dict[tuple, Optional[TariffRate]] = {}

        CHUNK = 50

        with transaction.atomic():
            for i, row in df.iterrows():
                excel_row = int(i) + 2

                if i % CHUNK == 0:
                    pct = 5 + int((i / max(total_rows, 1)) * 85)
                    _save_progress(
                        batch, pct,
                        f"Traitement… {i}/{total_rows} lignes",
                        meta={
                            "rows_processed": i,
                            "total_rows": total_rows,
                            "created": created_count,
                            "updated": updated_count,
                            "issues": len(issues_buf),
                            "missing_site": len(missing_contracts),
                        },
                    )

                raw_row = _row_snapshot(row.to_dict())
                data = {}

                for k in COLUMN_MAP.values():
                    if k not in df.columns:
                        continue
                    val = row.get(k, None)
                    if k in DATE_COLS:
                        parsed = _to_date_fr(val)
                        if parsed is None and not _is_blank(val):
                            issues_buf.append(ImportIssue(
                                batch=batch, row_number=excel_row,
                                severity=ImportIssue.Severity.WARN, field=k,
                                message=f"Date non parseable: {val!r}", raw_data=raw_row,
                            ))
                        data[k] = parsed
                    elif k in INT_COLS:
                        parsed = _to_int(val)
                        if parsed is None and not _is_blank(val):
                            issues_buf.append(ImportIssue(
                                batch=batch, row_number=excel_row,
                                severity=ImportIssue.Severity.WARN, field=k,
                                message=f"Entier non parseable: {val!r}", raw_data=raw_row,
                            ))
                        data[k] = parsed
                    elif k in DEC_COLS:
                        try:
                            parsed = parse_decimal_fr(val)
                        except Exception as e:
                            parsed = None
                            issues_buf.append(ImportIssue(
                                batch=batch, row_number=excel_row,
                                severity=ImportIssue.Severity.WARN, field=k,
                                message=f"Decimal parse error: {val!r} ({e})", raw_data=raw_row,
                            ))
                        if parsed is None and not _is_blank(val):
                            issues_buf.append(ImportIssue(
                                batch=batch, row_number=excel_row,
                                severity=ImportIssue.Severity.WARN, field=k,
                                message=f"Decimal non parseable: {val!r}", raw_data=raw_row,
                            ))
                        data[k] = parsed
                    else:
                        data[k] = None if _is_blank(val) else str(val).strip()

                data["echeance"] = echeance

                # Mapping site
                data["numero_compte_contrat"] = _to_contract_str(data.get("numero_compte_contrat"))
                acc = data.get("numero_compte_contrat")
                if acc:
                    site_pk = contract_to_site_id.get(acc)
                    if site_pk:
                        data["site_id"] = site_pk
                    else:
                        missing_contracts.add(acc)
                        continue

                # Champs requis
                req_missing = [
                    fname for fname, fval in [
                        ("numero_compte_contrat", data.get("numero_compte_contrat")),
                        ("numero_facture",        data.get("numero_facture")),
                        ("date_debut_periode",    data.get("date_debut_periode")),
                        ("date_fin_periode",      data.get("date_fin_periode")),
                    ] if _is_blank(fval)
                ]
                if req_missing:
                    skipped_missing_required += 1
                    issues_buf.append(ImportIssue(
                        batch=batch, row_number=excel_row,
                        severity=ImportIssue.Severity.ERROR, field=";".join(req_missing),
                        message=f"Champs requis manquants: {', '.join(req_missing)}", raw_data=raw_row,
                    ))
                    continue

                if data["date_fin_periode"] < data["date_debut_periode"]:
                    skipped_invalid_period += 1
                    issues_buf.append(ImportIssue(
                        batch=batch, row_number=excel_row,
                        severity=ImportIssue.Severity.ERROR,
                        field="date_debut_periode;date_fin_periode",
                        message="date_fin < date_debut", raw_data=raw_row,
                    ))
                    continue

                dedup_key = (
                    data["numero_compte_contrat"], data["numero_facture"],
                    data["date_debut_periode"],    data["date_fin_periode"],
                )
                if dedup_key in seen_in_file:
                    skipped_dup_in_file += 1
                    continue
                seen_in_file.add(dedup_key)

                _compute_target_fields(
                    data=data, issues_buf=issues_buf, batch=batch,
                    excel_row=excel_row, raw_row=raw_row, tariff_cache=tariff_cache,
                )

                # Upsert facture
                existing = SonatelInvoice.objects.filter(
                    numero_compte_contrat=data["numero_compte_contrat"],
                    numero_facture=data["numero_facture"],
                    date_debut_periode=data["date_debut_periode"],
                    date_fin_periode=data["date_fin_periode"],
                ).first()

                if existing:
                    for k, v in data.items():
                        setattr(existing, k, v)
                    existing.batch = batch
                    if hasattr(existing, "last_seen_at"):
                        existing.last_seen_at = timezone.now()
                    existing.save()
                    existing.months.all().delete()
                    payloads = _build_monthly_payloads(existing)
                    MonthlySynthesis.objects.bulk_create(payloads)
                    updated_count += 1
                else:
                    create_kwargs = dict(batch=batch, **data)
                    field_names = {fld.name for fld in SonatelInvoice._meta.fields}
                    if "last_seen_at" in field_names:
                        create_kwargs["last_seen_at"] = timezone.now()
                    inv = SonatelInvoice.objects.create(**create_kwargs)
                    payloads = _build_monthly_payloads(inv)
                    MonthlySynthesis.objects.bulk_create(payloads)
                    created_count += 1

                monthly_total += len(payloads)
                for p in payloads:
                    affected_keys.add((p.numero_compte_contrat, p.year, p.month))

            if issues_buf:
                ImportIssue.objects.bulk_create(issues_buf)

        # ── ContractMonth (sans OR géant) ────────────────────────────────────
        _save_progress(batch, 92, "Recalcul ContractMonth…")
        count_upserted = _upsert_contract_months(affected_keys)
        count_deleted  = _delete_stale_contract_months(affected_keys)

        # ── Succès ────────────────────────────────────────────────────────────
        final_meta = {
            "rows_created":                 created_count,
            "rows_updated":                 updated_count,
            "monthly_rows_created":         monthly_total,
            "skipped_missing_required":     skipped_missing_required,
            "skipped_invalid_period":       skipped_invalid_period,
            "skipped_duplicate_in_file":    skipped_dup_in_file,
            "issues_logged":                len(issues_buf),
            "contract_months_upserted":     count_upserted,
            "contract_months_deleted":      count_deleted,
            "invoices_missing_site_count":  len(missing_contracts),
            "invoices_missing_site_sample": list(missing_contracts)[:20],
        }
        batch.task_status   = ImportBatch.TaskStatus.SUCCESS
        batch.task_progress = 100
        batch.task_message  = (
            f"Terminé — +{created_count} créées, ↺{updated_count} maj, "
            f"{len(issues_buf)} issues"
        )
        batch.task_meta       = final_meta
        batch.task_updated_at = timezone.now()
        batch.save(update_fields=[
            "task_status", "task_progress", "task_message", "task_meta", "task_updated_at"
        ])
        return final_meta

    except Exception as exc:
        batch.task_status   = ImportBatch.TaskStatus.FAILURE
        batch.task_progress = 100
        batch.task_message  = f"Erreur: {str(exc)[:500]}"
        batch.task_meta     = {"traceback": traceback.format_exc()[-3000:]}
        batch.task_updated_at = timezone.now()
        batch.save(update_fields=[
            "task_status", "task_progress", "task_message", "task_meta", "task_updated_at"
        ])
        raise

    finally:
        # Nettoyage du fichier temporaire dans le storage
        try:
            default_storage.delete(storage_key)
        except Exception:
            pass


# ─── ContractMonth helpers (sans OR géant) ───────────────────────────────────

def _upsert_contract_months(keys: Set[Tuple[str, int, int]]) -> int:
    if not keys:
        return 0

    contracts = {acc for acc, _, _ in keys}
    years     = {y   for _, y, _ in keys}
    months    = {m   for _, _, m in keys}

    qs = (
        MonthlySynthesis.objects
        .filter(
            numero_compte_contrat__in=contracts,
            year__in=years,
            month__in=months,
            source__site__isnull=False,
            source__site__invoice_payment__iexact="Aktivco",
            source__site__grid_fee=True,
        )
        .values("numero_compte_contrat", "year", "month")
        .annotate(
            conso=Sum("conso"),
            montant_energie=Sum("montant_energie"),
            montant_ttc=Sum("montant_ttc"),
            montant_hors_tva=Sum("montant_hors_tva"),
            montant_redevance=Sum("montant_redevance"),
            montant_tco=Sum("montant_tco"),
            montant_tva=Sum("montant_tva"),
            montant_energie_k1=Sum("montant_energie_k1"),
            montant_energie_k2=Sum("montant_energie_k2"),
            rappel_k1=Sum("rappel_k1"),
            rappel_k2=Sum("rappel_k2"),
            majoration_k1=Sum("majoration_k1"),
            majoration_k2=Sum("majoration_k2"),
            montant_prime_fixe=Sum("montant_prime_fixe"),
            montant_cosinus_phi=Sum("montant_cosinus_phi"),
            conso_reactive=Sum("conso_reactive"),
            majo_reactif=Sum("majo_reactif"),
            conso_h1=Sum("conso_h1"),
            abonnement_calcule=Sum("abonnement_calcule"),
            penalite_abonnement_calculee=Sum("penalite_abonnement_calculee"),
            energie_calculee=Sum("energie_calculee"),
            valeur_cosinus_phi=Avg("valeur_cosinus_phi"),
            invoices_count=Count("id"),
            first_period_start=Min("period_start"),
            last_period_end=Max("period_end"),
        )
    )

    objs = [
        ContractMonth(
            numero_compte_contrat=r["numero_compte_contrat"],
            year=r["year"], month=r["month"],
            conso=r["conso"], montant_energie=r["montant_energie"],
            montant_ttc=r["montant_ttc"], montant_hors_tva=r["montant_hors_tva"],
            montant_redevance=r["montant_redevance"], montant_tco=r["montant_tco"],
            montant_tva=r["montant_tva"],
            montant_energie_k1=r["montant_energie_k1"], montant_energie_k2=r["montant_energie_k2"],
            rappel_k1=r["rappel_k1"], rappel_k2=r["rappel_k2"],
            majoration_k1=r["majoration_k1"], majoration_k2=r["majoration_k2"],
            montant_prime_fixe=r["montant_prime_fixe"], montant_cosinus_phi=r["montant_cosinus_phi"],
            conso_reactive=r["conso_reactive"], majo_reactif=r["majo_reactif"], conso_h1=r["conso_h1"],
            abonnement_calcule=r["abonnement_calcule"],
            penalite_abonnement_calculee=r["penalite_abonnement_calculee"],
            energie_calculee=r["energie_calculee"],
            valeur_cosinus_phi=r["valeur_cosinus_phi"],
            invoices_count=r["invoices_count"],
            first_period_start=r["first_period_start"], last_period_end=r["last_period_end"],
        )
        for r in qs
        # Filtrage exact : évite les faux positifs du IN × IN × IN
        if (r["numero_compte_contrat"], r["year"], r["month"]) in keys
    ]

    if not objs:
        return 0

    ContractMonth.objects.bulk_create(
        objs,
        update_conflicts=True,
        unique_fields=["numero_compte_contrat", "year", "month"],
        update_fields=[
            "conso", "montant_energie", "montant_ttc", "montant_hors_tva",
            "montant_redevance", "montant_tco", "montant_tva",
            "montant_energie_k1", "montant_energie_k2",
            "rappel_k1", "rappel_k2", "majoration_k1", "majoration_k2",
            "montant_prime_fixe", "montant_cosinus_phi",
            "conso_reactive", "majo_reactif", "conso_h1",
            "abonnement_calcule", "penalite_abonnement_calculee", "energie_calculee",
            "valeur_cosinus_phi", "invoices_count", "first_period_start", "last_period_end",
        ],
    )
    return len(objs)


def _delete_stale_contract_months(keys: Set[Tuple[str, int, int]]) -> int:
    if not keys:
        return 0

    contracts = {acc for acc, _, _ in keys}
    years     = {y   for _, y, _ in keys}
    months    = {m   for _, _, m in keys}

    alive = set(
        MonthlySynthesis.objects
        .filter(numero_compte_contrat__in=contracts, year__in=years, month__in=months)
        .values_list("numero_compte_contrat", "year", "month")
        .distinct()
    )

    stale = set(keys) - alive
    if not stale:
        return 0

    stale_contracts = {acc for acc, _, _ in stale}
    stale_years     = {y   for _, y, _ in stale}
    stale_months    = {m   for _, _, m in stale}

    ids_to_delete = [
        pk for pk, acc, y, m in
        ContractMonth.objects.filter(
            numero_compte_contrat__in=stale_contracts,
            year__in=stale_years,
            month__in=stale_months,
        ).values_list("id", "numero_compte_contrat", "year", "month")
        if (acc, y, m) in stale
    ]
    if not ids_to_delete:
        return 0
    return ContractMonth.objects.filter(id__in=ids_to_delete).delete()[0]





# billing/tasks.py  — AJOUT à la fin du fichier existant
# ─────────────────────────────────────────────────────────────────────────────
# Coller ce bloc APRÈS le bloc _delete_stale_contract_months() existant.
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(bind=True, name="billing.import_status_update")
def import_status_update_task(
    self,
    batch_id: int,
    storage_key: str,
    default_status: str = "VALIDATED",
):
    """
    Import asynchrone de mise à jour des statuts.

    Lit un fichier facturation Sonatel standard (même colonnes que l'import normal).
    Pour chaque ligne :
      - Cherche la SonatelInvoice correspondante via (contrat, facture, debut, fin).
      - Si trouvée → met à jour status + status_updated_at + status_last_batch.
      - Sinon → enregistrée dans "non_found" pour visualisation.

    Statuts possibles : CREATED | VALIDATED | CONTESTED
    """
    from .models import (
        ImportBatch, ImportIssue, SonatelInvoice, ContractSiteLink,
    )
    from .views import (
        _to_date_fr, _to_contract_str, _is_blank, _norm_header,
        _row_snapshot, COLUMN_MAP, DATE_COLS,
    )

    batch = ImportBatch.objects.get(pk=batch_id)
    batch.task_status   = ImportBatch.TaskStatus.RUNNING
    batch.task_progress = 0
    batch.task_message  = "Démarrage mise à jour statuts…"
    batch.task_updated_at = timezone.now()
    batch.save(update_fields=["task_status", "task_progress", "task_message", "task_updated_at"])

    try:
        # ── Validation du statut cible ────────────────────────────────────────
        valid_statuses = {s.value for s in SonatelInvoice.Status}
        if default_status not in valid_statuses:
            raise ValueError(
                f"Statut invalide: {default_status!r}. "
                f"Valeurs acceptées : {sorted(valid_statuses)}"
            )

        # ── Lecture fichier ───────────────────────────────────────────────────
        _save_progress(batch, 2, "Lecture du fichier…")
        with default_storage.open(storage_key, "rb") as fh:
            file_bytes = fh.read()

        df = pd.read_excel(io.BytesIO(file_bytes), dtype=object)
        total_rows = len(df)
        del file_bytes
        _save_progress(batch, 5, f"Fichier chargé — {total_rows} lignes")

        # ── Renommage colonnes ────────────────────────────────────────────────
        normed_cols = {_norm_header(c): c for c in df.columns}
        normed_map  = {_norm_header(src): dst for src, dst in COLUMN_MAP.items()}
        rename_map  = {normed_cols[src]: dst for src, dst in normed_map.items() if src in normed_cols}
        df = df.rename(columns=rename_map)

        # Colonnes minimales requises pour identifier une facture
        required_cols = [
            "numero_compte_contrat", "numero_facture",
            "date_debut_periode", "date_fin_periode",
        ]
        missing_cols = [c for c in required_cols if c not in df.columns]
        if missing_cols:
            raise ValueError(f"Colonnes obligatoires manquantes: {missing_cols}")

        # Colonne statut optionnelle dans le fichier (override par ligne)
        has_status_col = "statut" in [c.lower() for c in df.columns]
        status_col_name = next(
            (c for c in df.columns if c.lower() == "statut"), None
        )

        # ── Pré-charger les contrats connus en base ───────────────────────────
        file_contracts = set(
            df["numero_compte_contrat"].dropna()
            .map(_to_contract_str).dropna().tolist()
        )

        # Tous les contrats liés à un site Aktivco
        linked_contracts = set(
            ContractSiteLink.objects.filter(
                numero_compte_contrat__in=file_contracts,
                site__invoice_payment__iexact="Aktivco",
                site__grid_fee=True,
            ).values_list("numero_compte_contrat", flat=True)
        )

        # Pré-charger les factures existantes pour les contrats du fichier
        # Structure : {(contrat, facture, debut_str, fin_str): invoice_pk}
        existing_map: dict[tuple, int] = {}
        qs_existing = SonatelInvoice.objects.filter(
            numero_compte_contrat__in=linked_contracts,
        ).values_list(
            "id", "numero_compte_contrat", "numero_facture",
            "date_debut_periode", "date_fin_periode",
        )
        for pk, acc, fac, d1, d2 in qs_existing:
            key = (acc, str(fac), str(d1), str(d2))
            existing_map[key] = pk

        _save_progress(batch, 10, f"{len(existing_map)} factures pré-chargées")

        # ── Parcours des lignes ───────────────────────────────────────────────
        updated_count    = 0
        not_found_rows   = []   # lignes du fichier sans correspondance en DB
        no_site_rows     = []   # contrats non liés à un site
        error_rows       = []   # erreurs de parsing
        issues_buf       = []
        seen_keys: set   = set()

        ids_to_update:   list[int] = []   # PKs à mettre à jour
        pk_to_status:    dict[int, str] = {}

        CHUNK = 100

        for i, row in df.iterrows():
            excel_row = int(i) + 2

            if i % CHUNK == 0:
                pct = 10 + int((i / max(total_rows, 1)) * 80)
                _save_progress(
                    batch, pct,
                    f"Traitement… {i}/{total_rows}",
                    meta={
                        "rows_processed": i,
                        "total_rows": total_rows,
                        "updated": updated_count,
                        "not_found": len(not_found_rows),
                        "no_site": len(no_site_rows),
                        "errors": len(error_rows),
                    },
                )

            raw_row = _row_snapshot(row.to_dict())

            # -- Parse identifiants --
            try:
                acc  = _to_contract_str(row.get("numero_compte_contrat"))
                fac  = None if _is_blank(row.get("numero_facture")) else str(row.get("numero_facture")).strip()
                d1   = _to_date_fr(row.get("date_debut_periode"))
                d2   = _to_date_fr(row.get("date_fin_periode"))
            except Exception as e:
                error_rows.append({
                    "row": excel_row,
                    "error": f"Parse error: {e}",
                    "raw": {
                        "contrat": str(row.get("numero_compte_contrat", "")),
                        "facture": str(row.get("numero_facture", "")),
                    },
                })
                issues_buf.append(ImportIssue(
                    batch=batch, row_number=excel_row,
                    severity=ImportIssue.Severity.ERROR,
                    field="identifiants",
                    message=f"Parse error: {e}",
                    raw_data=raw_row,
                ))
                continue

            if not acc or not fac or not d1 or not d2:
                error_rows.append({
                    "row": excel_row,
                    "error": "Champs identifiants manquants (contrat/facture/dates)",
                    "raw": {"contrat": acc, "facture": fac},
                })
                issues_buf.append(ImportIssue(
                    batch=batch, row_number=excel_row,
                    severity=ImportIssue.Severity.WARN,
                    field="identifiants",
                    message="Champs requis manquants",
                    raw_data=raw_row,
                ))
                continue

            # -- Statut cible pour cette ligne --
            if has_status_col and not _is_blank(row.get(status_col_name)):
                from .views import _parse_status
                row_status = _parse_status(row.get(status_col_name)) or default_status
            else:
                row_status = default_status

            dedup_key = (acc, fac, str(d1), str(d2))
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            # -- Contrat lié à un site ? --
            if acc not in linked_contracts:
                no_site_rows.append({
                    "row": excel_row,
                    "contrat": acc,
                    "facture": fac,
                    "date_debut": str(d1),
                    "date_fin": str(d2),
                    "reason": "Contrat non lié à un site Aktivco",
                })
                continue

            # -- Facture en base ? --
            lookup_key = (acc, fac, str(d1), str(d2))
            if lookup_key not in existing_map:
                not_found_rows.append({
                    "row": excel_row,
                    "contrat": acc,
                    "facture": fac,
                    "date_debut": str(d1),
                    "date_fin": str(d2),
                    "target_status": row_status,
                    "reason": "Facture absente de la base (pas encore importée ?)",
                })
                issues_buf.append(ImportIssue(
                    batch=batch, row_number=excel_row,
                    severity=ImportIssue.Severity.WARN,
                    field="numero_facture",
                    message=f"Facture {fac} / contrat {acc} introuvable en base.",
                    raw_data=raw_row,
                ))
                continue

            # -- Préparer la maj --
            pk = existing_map[lookup_key]
            ids_to_update.append(pk)
            pk_to_status[pk] = row_status

        # ── Mise à jour en base (par lots) ───────────────────────────────────
        _save_progress(batch, 91, "Mise à jour des statuts en base…")

        BATCH_SIZE = 500
        for chunk_start in range(0, len(ids_to_update), BATCH_SIZE):
            chunk_ids = ids_to_update[chunk_start : chunk_start + BATCH_SIZE]

            # Si toutes les factures du chunk ont le même statut → bulk_update rapide
            statuses_in_chunk = {pk_to_status[pk] for pk in chunk_ids}

            if len(statuses_in_chunk) == 1:
                SonatelInvoice.objects.filter(pk__in=chunk_ids).update(
                    status=statuses_in_chunk.pop(),
                    status_updated_at=timezone.now(),
                    status_last_batch=batch,
                )
            else:
                # Statuts mixtes → update individuelle (peu fréquent)
                for pk in chunk_ids:
                    SonatelInvoice.objects.filter(pk=pk).update(
                        status=pk_to_status[pk],
                        status_updated_at=timezone.now(),
                        status_last_batch=batch,
                    )

            updated_count += len(chunk_ids)

        # Propager le statut sur MonthlySynthesis (cohérence)
        if ids_to_update:
            from .models import MonthlySynthesis
            for chunk_start in range(0, len(ids_to_update), BATCH_SIZE):
                chunk_ids = ids_to_update[chunk_start : chunk_start + BATCH_SIZE]
                statuses_in_chunk = {pk_to_status[pk] for pk in chunk_ids}
                if len(statuses_in_chunk) == 1:
                    MonthlySynthesis.objects.filter(
                        source_id__in=chunk_ids,
                    ).update(status=statuses_in_chunk.pop())
                else:
                    for pk in chunk_ids:
                        MonthlySynthesis.objects.filter(source_id=pk).update(
                            status=pk_to_status[pk]
                        )

        # ── Issues bulk insert ────────────────────────────────────────────────
        if issues_buf:
            ImportIssue.objects.bulk_create(issues_buf)

        # ── Résultat final ────────────────────────────────────────────────────
        final_meta = {
            "total_rows":     total_rows,
            "updated":        updated_count,
            "not_found":      len(not_found_rows),
            "no_site":        len(no_site_rows),
            "errors":         len(error_rows),
            "default_status": default_status,
            # Données pour visualisation — on limite à 500 pour ne pas saturer JSON
            "not_found_rows": not_found_rows[:500],
            "no_site_rows":   no_site_rows[:500],
            "error_rows":     error_rows[:200],
        }
        batch.task_status   = ImportBatch.TaskStatus.SUCCESS
        batch.task_progress = 100
        batch.task_message  = (
            f"Terminé — {updated_count} mis à jour, "
            f"{len(not_found_rows)} non trouvés, "
            f"{len(no_site_rows)} hors site, "
            f"{len(error_rows)} erreurs"
        )
        batch.task_meta       = final_meta
        batch.task_updated_at = timezone.now()
        batch.save(update_fields=[
            "task_status", "task_progress", "task_message",
            "task_meta", "task_updated_at",
        ])
        return final_meta

    except Exception as exc:
        batch.task_status   = ImportBatch.TaskStatus.FAILURE
        batch.task_progress = 100
        batch.task_message  = f"Erreur: {str(exc)[:500]}"
        batch.task_meta     = {"traceback": traceback.format_exc()[-3000:]}
        batch.task_updated_at = timezone.now()
        batch.save(update_fields=[
            "task_status", "task_progress", "task_message",
            "task_meta", "task_updated_at",
        ])
        raise

    finally:
        try:
            default_storage.delete(storage_key)
        except Exception:
            pass