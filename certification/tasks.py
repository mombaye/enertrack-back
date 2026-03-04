# certification/tasks.py

import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from celery import shared_task, chord, group
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import CertificationBatch, CertificationResult, EfmsConnectionLog
from .services.efms import EfmsService, EfmsConnectionError, EfmsQueryError

logger = logging.getLogger(__name__)

D0 = Decimal("0")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS MÉTIER
# ─────────────────────────────────────────────────────────────────────────────

def _safe_div(a, b) -> Decimal | None:
    try:
        a = Decimal(str(a))
        b = Decimal(str(b))
        if b == D0:
            return None
        return a / b
    except (InvalidOperation, TypeError):
        return None


def _fetch_internal_history(invoice) -> tuple[Decimal | None, Decimal | None]:
    from billing.models import MonthlySynthesis

    if not invoice.site_id:
        return None, None

    qs = (
        MonthlySynthesis.objects
        .filter(source__site_id=invoice.site_id)
        .exclude(source_id=invoice.id)
        .exclude(conso__isnull=True)
        .exclude(conso=D0)
        .order_by("-year", "-month")
        .values("conso", "period_total_days", "days_covered")
    )

    last_conso = None
    consos_30j = []

    for row in qs[:3]:
        days = row["days_covered"]
        if days and days > 0:
            conso_30j = Decimal(str(row["conso"])) * 30 / Decimal(str(days))
            consos_30j.append(conso_30j)
            if last_conso is None:
                last_conso = conso_30j

    if not consos_30j:
        return None, None

    avg_3mois = sum(consos_30j) / Decimal(str(len(consos_30j)))
    return last_conso, avg_3mois


def _apply_certification_rules(result: CertificationResult) -> None:
    fact_p  = result.conso_facturee_periode
    fact_30 = result.conso_facturee_30j

    result.ratio_fms_periode = _safe_div(result.conso_fms_periode, fact_p)
    result.ratio_fms_30j     = _safe_div(result.conso_fms_30j,     fact_30)
    result.ratio_histo_3mois = _safe_div(result.histo_3mois_avg,   fact_30)

    THRESHOLD_FMS   = Decimal("0.9")
    THRESHOLD_HISTO = Decimal("0.85")

    if result.fms_available:
        if result.ratio_fms_periode and result.ratio_fms_periode > THRESHOLD_FMS:
            result.status            = CertificationResult.Status.CERTIFIED_FMS
            result.certified_by_rule = CertificationResult.CertifiedByRule.FMS_PERIODE
            return
        if result.ratio_fms_30j and result.ratio_fms_30j > THRESHOLD_FMS:
            result.status            = CertificationResult.Status.CERTIFIED_FMS
            result.certified_by_rule = CertificationResult.CertifiedByRule.FMS_30J
            return

    if result.ratio_histo_3mois and result.ratio_histo_3mois > THRESHOLD_HISTO:
        result.status            = CertificationResult.Status.CERTIFIED_SENELEC
        result.certified_by_rule = CertificationResult.CertifiedByRule.HISTO_3MOIS
        return

    result.status = CertificationResult.Status.NEEDS_REVIEW


# Mapping status → champ compteur sur CertificationBatch
_STATUS_TO_COUNTER = {
    CertificationResult.Status.CERTIFIED_FMS:     "certified_fms",
    CertificationResult.Status.CERTIFIED_SENELEC: "certified_senelec",
    CertificationResult.Status.NEEDS_REVIEW:      "needs_review",
    CertificationResult.Status.UNKNOWN_CONTRACT:  "unknown_contract",
    CertificationResult.Status.FMS_UNAVAILABLE:   "fms_unavailable",
}


def _increment_batch_counter(cert_batch_id: int, status: str) -> None:
    """
    Incrément atomique du compteur du batch correspondant au statut.
    Utilise F() pour éviter les race conditions en parallèle.
    """
    counter_field = _STATUS_TO_COUNTER.get(status)
    if not counter_field:
        return
    CertificationBatch.objects.filter(pk=cert_batch_id).update(
        **{counter_field: F(counter_field) + 1}
    )


# ─────────────────────────────────────────────────────────────────────────────
# TÂCHE 1 — Traitement d'UNE facture
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    name="certification.certify_single_invoice",
)
def certify_single_invoice(self, result_id: int) -> dict:
    try:
        result = CertificationResult.objects.select_related(
            "invoice", "site", "cert_batch"
        ).get(id=result_id)
    except CertificationResult.DoesNotExist:
        logger.error(f"[certify] CertificationResult #{result_id} introuvable.")
        return {"result_id": result_id, "status": "ERROR", "error": "not_found"}

    invoice = result.invoice
    cert_batch_id = result.cert_batch_id

    try:
        # ── Étape 4 : Normalisation ───────────────────────────────────────────
        if invoice.date_debut_periode and invoice.date_fin_periode:
            nb_jours = (invoice.date_fin_periode - invoice.date_debut_periode).days + 1
        else:
            nb_jours = None

        conso_p = invoice.conso_facturee

        if conso_p and nb_jours and nb_jours > 0:
            conso_30j = Decimal(str(conso_p)) * 30 / Decimal(str(nb_jours))
        else:
            conso_30j = None

        result.conso_facturee_periode = conso_p
        result.nb_jours_facturation   = nb_jours
        result.conso_facturee_30j     = conso_30j

        # ── Étape 5 : Données FMS ─────────────────────────────────────────────
        efms = EfmsService(cert_batch=result.cert_batch)

        try:
            conso_fms_p = efms.get_conso_periode(
                site_id=result.site.site_id,
                date_debut=invoice.date_debut_periode,
                date_fin=invoice.date_fin_periode,
            )
            conso_fms_30j, fms_month = efms.get_conso_last_complete_month(
                site_id=result.site.site_id,
                before_date=invoice.date_debut_periode,
            )
            result.fms_available           = True
            result.conso_fms_periode       = conso_fms_p
            result.conso_fms_30j           = conso_fms_30j
            result.fms_last_complete_month = fms_month

            if conso_fms_p is None and conso_fms_30j is None:
                result.fms_error = "NA — aucune donnée FMS sur la période"

        except EfmsConnectionError as e:
            result.fms_available = False
            result.fms_error     = str(e)
            logger.warning(f"[certify] FMS indisponible result #{result_id}: {e}")

        except EfmsQueryError as e:
            result.fms_available = False
            result.fms_error     = f"SQL error: {e}"
            logger.error(f"[certify] Erreur SQL FMS result #{result_id}: {e}")

        # ── Étape 6 : Historique interne ──────────────────────────────────────
        result.histo_last_conso, result.histo_3mois_avg = _fetch_internal_history(invoice)

        # ── Étape 7 : Règles ──────────────────────────────────────────────────
        _apply_certification_rules(result)

        result.save()

        # ── Incrément atomique du compteur batch ──────────────────────────────
        # Fait ICI, après chaque facture → progress temps réel
        _increment_batch_counter(cert_batch_id, result.status)

        logger.info(f"[certify] result #{result_id} → {result.status}")
        return {"result_id": result_id, "status": result.status}

    except Exception as exc:
        logger.exception(f"[certify] Erreur inattendue result #{result_id}: {exc}")
        raise self.retry(exc=exc)


# ─────────────────────────────────────────────────────────────────────────────
# TÂCHE 2 — Finalisation (chord callback)
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name="certification.finalize_certification_batch")
def finalize_certification_batch(results: list, cert_batch_id: int) -> dict:
    try:
        batch = CertificationBatch.objects.get(id=cert_batch_id)
    except CertificationBatch.DoesNotExist:
        logger.error(f"[finalize] CertificationBatch #{cert_batch_id} introuvable.")
        return {"error": "batch_not_found"}

    # refresh_counters() en fallback de sécurité
    # (au cas où un incrément aurait été perdu)
    batch.refresh_counters()
    batch.status      = CertificationBatch.Status.DONE
    batch.finished_at = timezone.now()
    batch.save(update_fields=["status", "finished_at",
                               "certified_fms", "certified_senelec",
                               "needs_review", "unknown_contract", "fms_unavailable"])

    logger.info(
        f"[finalize] Batch #{cert_batch_id} DONE — "
        f"total={batch.total} fms={batch.certified_fms} "
        f"senelec={batch.certified_senelec} review={batch.needs_review}"
    )
    return {
        "cert_batch_id":     cert_batch_id,
        "status":            "DONE",
        "total":             batch.total,
        "certified_fms":     batch.certified_fms,
        "certified_senelec": batch.certified_senelec,
        "needs_review":      batch.needs_review,
        "unknown_contract":  batch.unknown_contract,
        "fms_unavailable":   batch.fms_unavailable,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TÂCHE 3 — Orchestrateur (fan-out)
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name="certification.launch_certification_batch")
def launch_certification_batch(cert_batch_id: int) -> None:
    try:
        cert_batch = CertificationBatch.objects.select_related(
            "import_batch"
        ).get(id=cert_batch_id)
    except CertificationBatch.DoesNotExist:
        logger.error(f"[launch] CertificationBatch #{cert_batch_id} introuvable.")
        return

    cert_batch.status = CertificationBatch.Status.RUNNING
    cert_batch.save(update_fields=["status"])

    try:
        invoices = (
            cert_batch.import_batch.rows
            .select_related("site")
            .all()
        )

        result_ids  = []
        unknown_count = 0

        with transaction.atomic():
            for invoice in invoices:
                if invoice.site_id is None:
                    CertificationResult.objects.create(
                        cert_batch=cert_batch,
                        invoice=invoice,
                        site=None,
                        status=CertificationResult.Status.UNKNOWN_CONTRACT,
                    )
                    unknown_count += 1
                    continue

                result, created = CertificationResult.objects.get_or_create(
                    cert_batch=cert_batch,
                    invoice=invoice,
                    defaults={
                        "site":   invoice.site,
                        "status": CertificationResult.Status.PENDING_CERTIFICATION,
                    },
                )

                if not created and result.is_terminal:
                    continue

                result_ids.append(result.id)

        # Incrémenter les UNKNOWN_CONTRACT créés directement ici
        if unknown_count:
            CertificationBatch.objects.filter(pk=cert_batch_id).update(
                unknown_contract=F("unknown_contract") + unknown_count
            )

        if not result_ids:
            cert_batch.refresh_counters()
            cert_batch.status      = CertificationBatch.Status.DONE
            cert_batch.finished_at = timezone.now()
            cert_batch.save(update_fields=["status", "finished_at"])
            logger.info(f"[launch] Batch #{cert_batch_id} : aucune facture à traiter.")
            return

        all_tasks = [certify_single_invoice.s(rid) for rid in result_ids]

        # Initialise total AVANT le fan-out → progress % correct dès le 1er poll
        CertificationBatch.objects.filter(pk=cert_batch_id).update(
            total=len(result_ids) + unknown_count
        )

        chord(
            group(all_tasks),
            finalize_certification_batch.s(cert_batch_id),
        ).apply_async()

        logger.info(
            f"[launch] Batch #{cert_batch_id} : "
            f"{len(result_ids)} factures lancées + {unknown_count} unknown."
        )

    except Exception as exc:
        logger.exception(f"[launch] Erreur critique batch #{cert_batch_id}: {exc}")
        cert_batch.status = CertificationBatch.Status.FAILED
        cert_batch.save(update_fields=["status"])
        raise