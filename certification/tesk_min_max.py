# certification/tasks.py
# v3 — ACM = source de substitution Grid (fallback étape 5B)
#         _apply_certification_rules() sans appel réseau

import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from celery import shared_task, chord, group
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import CertificationBatch, CertificationResult, EfmsConnectionLog
#from .services.efms import EfmsService, EfmsConnectionError, EfmsQueryError
from .services.efms_max_min import EfmsService, EfmsConnectionError, EfmsQueryError
from .services.billing_check import check_montant_coherence

logger = logging.getLogger(__name__)

D0 = Decimal("0")

THRESHOLD_FMS   = Decimal("0.9")
THRESHOLD_HISTO = Decimal("0.85")


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


# ─────────────────────────────────────────────────────────────────────────────
# RÈGLES DE CERTIFICATION
# ─────────────────────────────────────────────────────────────────────────────

def _apply_certification_rules(result: CertificationResult) -> None:
    """
    Applique les règles de certification sur les données déjà calculées
    aux étapes 5 (Grid) et 5B (ACM fallback). Aucun appel réseau ici.

    Logique (confirmée client) :
      • Si Grid FMS disponible et ratio > 0.9 → CERTIFIED_FMS  (règle FMS)
      • Si Grid absent mais ACM disponible (fallback étape 5B) et ratio > 0.9
                                             → CERTIFIED_FMS  (règle ACM)
      • Si historique Senelec ratio > 0.85   → CERTIFIED_SENELEC
      • Sinon                                → NEEDS_REVIEW

    L'ACM est une SOURCE DE SUBSTITUTION au Grid, pas un double-check.
    L'étape 5B a déjà alimenté conso_fms_periode/30j avec les valeurs ACM
    si le Grid était absent — on se contente ici d'évaluer les ratios.
    """
    fact_p  = result.conso_facturee_periode
    fact_30 = result.conso_facturee_30j

    # ── Calcul ratios FMS (Grid ou ACM selon ce qui a été mis en 5/5B) ───────
    result.ratio_fms_periode = _safe_div(result.conso_fms_periode, fact_p)
    result.ratio_fms_30j     = _safe_div(result.conso_fms_30j,     fact_30)
    result.ratio_histo_3mois = _safe_div(result.histo_3mois_avg,   fact_30)

    # Ratios ACM dédiés pour traçabilité (déjà remplis en 5B si fallback)
    if result.acm_available and result.estim_conso_acm_periode is not None:
        result.ratio_acm_periode = _safe_div(result.estim_conso_acm_periode, fact_p)
    if result.acm_available and result.estim_conso_acm_30j is not None:
        result.ratio_acm_30j = _safe_div(result.estim_conso_acm_30j, fact_30)

    # ── Cohérence 1 : FMS (Grid ou ACM substitut) ────────────────────────────
    if result.fms_available:
        fms_ok = (
            (result.ratio_fms_periode is not None and result.ratio_fms_periode > THRESHOLD_FMS)
            or
            (result.ratio_fms_30j is not None and result.ratio_fms_30j > THRESHOLD_FMS)
        )

        if fms_ok:
            result.status = CertificationResult.Status.CERTIFIED_FMS

            # Déterminer la règle : ACM si fallback étape 5B, Grid sinon
            if result.acm_available:
                # ACM utilisé comme source (fallback)
                if result.ratio_acm_periode is not None and result.ratio_acm_periode > THRESHOLD_FMS:
                    result.certified_by_rule = CertificationResult.CertifiedByRule.ACM_PERIODE
                else:
                    result.certified_by_rule = CertificationResult.CertifiedByRule.ACM_30J
            else:
                # Grid direct
                if result.ratio_fms_periode is not None and result.ratio_fms_periode > THRESHOLD_FMS:
                    result.certified_by_rule = CertificationResult.CertifiedByRule.FMS_PERIODE
                else:
                    result.certified_by_rule = CertificationResult.CertifiedByRule.FMS_30J

            logger.info(
                "[rules] %s → CERTIFIED_FMS (rule=%s ratio_p=%s ratio_30j=%s acm=%s)",
                result.invoice.numero_facture,
                result.certified_by_rule,
                result.ratio_fms_periode,
                result.ratio_fms_30j,
                result.acm_available,
            )
            return

    # ── Cohérence 2 : Historique Senelec ─────────────────────────────────────
    if result.ratio_histo_3mois is not None and result.ratio_histo_3mois > THRESHOLD_HISTO:
        result.status            = CertificationResult.Status.CERTIFIED_SENELEC
        result.certified_by_rule = CertificationResult.CertifiedByRule.HISTO_3MOIS
        logger.info(
            "[rules] %s → CERTIFIED_SENELEC (ratio_histo=%s)",
            result.invoice.numero_facture,
            result.ratio_histo_3mois,
        )
        return

    result.status = CertificationResult.Status.NEEDS_REVIEW
    logger.info(
        "[rules] %s → NEEDS_REVIEW (fms=%s ratio_p=%s ratio_30j=%s histo=%s)",
        result.invoice.numero_facture,
        result.fms_available,
        result.ratio_fms_periode,
        result.ratio_fms_30j,
        result.ratio_histo_3mois,
    )


# Mapping status → champ compteur sur CertificationBatch
_STATUS_TO_COUNTER = {
    CertificationResult.Status.CERTIFIED_FMS:     "certified_fms",
    CertificationResult.Status.CERTIFIED_SENELEC: "certified_senelec",
    CertificationResult.Status.NEEDS_REVIEW:      "needs_review",
    CertificationResult.Status.UNKNOWN_CONTRACT:  "unknown_contract",
    CertificationResult.Status.FMS_UNAVAILABLE:   "fms_unavailable",
}


def _increment_batch_counter(cert_batch_id: int, status: str) -> None:
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

    invoice       = result.invoice
    cert_batch_id = result.cert_batch_id

    save_fields = [
        "status", "certified_by_rule", "computed_at",
        # étape 4
        "conso_facturee_periode", "nb_jours_facturation", "conso_facturee_30j",
        # étape 5
        "fms_available", "conso_fms_periode", "conso_fms_30j",
        "fms_last_complete_month", "fms_error",
        # étape 6
        "histo_last_conso", "histo_3mois_avg",
        # étape 7A ratios
        "ratio_fms_periode", "ratio_fms_30j", "ratio_histo_3mois",
        # étape 7B ACM
        "acm_available", "estim_conso_acm_periode", "estim_conso_acm_30j",
        "ratio_acm_periode", "ratio_acm_30j", "acm_error",
        # étape 8
        "montant_htva_calcule", "variation_montant_pct",
        "montant_coherent", "montant_check_error",
    ]

    try:
        efms = EfmsService(cert_batch=result.cert_batch)

        # ── Étape 4 : Normalisation conso facturée ────────────────────────────
        if invoice.date_debut_periode and invoice.date_fin_periode:
            nb_jours = (invoice.date_fin_periode - invoice.date_debut_periode).days + 1
        else:
            nb_jours = None

        conso_p   = invoice.conso_facturee
        conso_30j = (
            Decimal(str(conso_p)) * 30 / Decimal(str(nb_jours))
            if conso_p and nb_jours and nb_jours > 0
            else None
        )

        result.conso_facturee_periode = conso_p
        result.nb_jours_facturation   = nb_jours
        result.conso_facturee_30j     = conso_30j

        # ── Étape 5 : Données FMS Grid ────────────────────────────────────────
        try:
            conso_fms_p, _fms_method = efms.get_conso_periode(
                site_id   = result.site.site_id,
                date_debut= invoice.date_debut_periode,
                date_fin  = invoice.date_fin_periode,
            )
            conso_fms_30j, fms_month = efms.get_conso_last_complete_month(
                site_id    = result.site.site_id,
                before_date= invoice.date_debut_periode,
            )

            if conso_fms_p is None and conso_fms_30j is None:
                # Grid présent dans la BDD mais données insuffisantes pour ce site
                result.fms_available = False
                result.fms_error     = "NA — aucune donnée Grid suffisante (site épars)"
                logger.info("[certify] %s : Grid insuffisant → tentative ACM", invoice.numero_facture)
            else:
                result.fms_available           = True
                result.conso_fms_periode       = conso_fms_p
                result.conso_fms_30j           = conso_fms_30j
                result.fms_last_complete_month = fms_month

        except EfmsConnectionError as e:
            result.fms_available = False
            result.fms_error     = str(e)

        except EfmsQueryError as e:
            result.fms_available = False
            result.fms_error     = f"SQL error: {e}"

        # ── Étape 5B : Fallback ACM si Grid indisponible ──────────────────────
        # Si Grid n'a pas de données pour ce site, on utilise act_energy_p
        # depuis la table AC_Meter_Report_day comme source de substitution.
        # Les données ACM alimentent les mêmes champs conso_fms_* pour que
        # les règles de certification (étape 7) fonctionnent de façon uniforme.
        if not result.fms_available:
            try:
                acm_p, acm_30j = efms.get_conso_acm(
                    site_id   = result.site.site_id,
                    date_debut= invoice.date_debut_periode,
                    date_fin  = invoice.date_fin_periode,
                )

                if acm_p is not None or acm_30j is not None:
                    # ACM disponible → alimente les champs FMS pour les règles
                    result.fms_available     = True
                    result.conso_fms_periode = acm_p
                    result.conso_fms_30j     = acm_30j
                    result.fms_error         = None   # effacer l'erreur Grid

                    # Champs ACM dédiés pour la traçabilité
                    result.acm_available           = True
                    result.estim_conso_acm_periode = acm_p
                    result.estim_conso_acm_30j     = acm_30j
                    # Les ratios ACM seront calculés dans _apply_certification_rules

                    logger.info(
                        "[certify] %s : Grid indispo → ACM fallback "
                        "(période=%.1f kWh, 30j=%.1f kWh)",
                        invoice.numero_facture,
                        float(acm_p   or 0),
                        float(acm_30j or 0),
                    )
                else:
                    # Grid ET ACM indisponibles → FMS_UNAVAILABLE
                    result.acm_available = False
                    result.acm_error     = "ACM sans données suffisantes"
                    logger.info(
                        "[certify] %s : Grid ET ACM indisponibles → FMS_UNAVAILABLE",
                        invoice.numero_facture,
                    )

            except (EfmsConnectionError, EfmsQueryError) as e:
                result.acm_available = False
                result.acm_error     = f"ACM échoué: {e}"
                logger.warning(
                    "[certify] %s : ACM fallback échoué (%s)",
                    invoice.numero_facture, e,
                )

        # ── Étape 6 : Historique interne Senelec ─────────────────────────────
        result.histo_last_conso, result.histo_3mois_avg = _fetch_internal_history(invoice)

        # ── Étape 7 : Règles de certification ────────────────────────────────
        # Pas d'appel réseau ici — on évalue uniquement les données déjà collectées
        _apply_certification_rules(result)

        # ── Étape 8 : Cohérence montant ───────────────────────────────────────
        try:
            mhtva, variation, coherent, err8 = check_montant_coherence(invoice)
            result.montant_htva_calcule  = mhtva
            result.variation_montant_pct = variation
            result.montant_coherent      = coherent
            result.montant_check_error   = err8
        except Exception as exc8:
            result.montant_check_error = f"Erreur étape 8: {exc8}"
            logger.warning("[certify] Étape 8 échouée result #%d : %s", result_id, exc8)

        # ── Sauvegarde ────────────────────────────────────────────────────────
        result.save()
        _increment_batch_counter(cert_batch_id, result.status)

        logger.info(
            "[certify] #%d → %s | rule=%s | acm=%s | coherent=%s variation=%.1f%%",
            result_id,
            result.status,
            result.certified_by_rule,
            result.acm_available,
            result.montant_coherent,
            float(result.variation_montant_pct or 0),
        )
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

    batch.refresh_counters()
    batch.status      = CertificationBatch.Status.DONE
    batch.finished_at = timezone.now()
    batch.save(update_fields=[
        "status", "finished_at",
        "certified_fms", "certified_senelec",
        "needs_review", "unknown_contract", "fms_unavailable",
    ])

    logger.info(
        "[finalize] Batch #%d DONE — total=%d fms=%d senelec=%d review=%d acm_fallback=?",
        cert_batch_id, batch.total,
        batch.certified_fms, batch.certified_senelec, batch.needs_review,
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
        invoices = cert_batch.import_batch.rows.select_related("site").all()

        result_ids    = []
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

        CertificationBatch.objects.filter(pk=cert_batch_id).update(
            total=len(result_ids) + unknown_count
        )

        all_tasks = [certify_single_invoice.s(rid) for rid in result_ids]
        chord(
            group(all_tasks),
            finalize_certification_batch.s(cert_batch_id),
        ).apply_async()

        logger.info(
            "[launch] Batch #%d : %d factures lancées + %d unknown.",
            cert_batch_id, len(result_ids), unknown_count,
        )

    except Exception as exc:
        logger.exception(f"[launch] Erreur critique batch #{cert_batch_id}: {exc}")
        cert_batch.status = CertificationBatch.Status.FAILED
        cert_batch.save(update_fields=["status"])
        raise