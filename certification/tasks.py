# certification/tasks.py — v5
#
# CHANGEMENTS v5 (par rapport à v4.1) :
#   1. DÉCORRÉLATION alerte ↔ certification dans _apply_certification_rules :
#      - flag_mesure_alert est désormais calculé INDÉPENDAMMENT du statut
#      - une facture peut être CERTIFIED_FMS + flag_mesure_alert=True
#      - MESURE_A_VERIFIER est réservé aux non-certifiées avec alerte
#
#   2. GARDE-FOU cohérence interne (nouveau) :
#      - si |ratio_fms_periode - ratio_fms_30j| > 0,5 → flag_mesure_alert=True
#      - attrape les cas où une fenêtre SQL est vérolée par un point aberrant
#        que l'autre ne capte pas (ex KLD_2007 : 6,27 vs 0,93)
#
#   3. LOGS enrichis pour distinguer "certifié sain" de "certifié + alerte"
#
# Pas de changement modèle / migration. Les compteurs continuent de fonctionner :
# `mesure_alert` reste mappé sur le statut MESURE_A_VERIFIER (non-certifié + alerte),
# ce qui garde la barre de progression cohérente (segments mutuellement exclusifs).

import logging
from decimal import Decimal, InvalidOperation

from celery import shared_task, chord, group
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import CertificationBatch, CertificationResult
from .services.efms import EfmsConnectionError, EfmsQueryError
from .services.snowflake_efms import SnowflakeEfmsService as EfmsService
from .services.billing_check import check_montant_coherence

logger = logging.getLogger(__name__)

D0 = Decimal("0")

# ─── Seuils certification ────────────────────────────────────────────────────
THRESHOLD_FMS    = Decimal("0.95")
THRESHOLD_HISTO  = Decimal("0.95")

# ─── Seuils alerte mesure ────────────────────────────────────────────────────
SEUIL_ALERTE_BAS  = Decimal("0.50")   # ratio < 50% → alerte
SEUIL_ALERTE_HAUT = Decimal("1.50")   # ratio > 150% → alerte
SEUIL_INCOHERENCE = Decimal("0.50")   # ✅ v5 — écart max toléré entre ratio_p et ratio_30j


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
    return last_conso, sum(consos_30j) / Decimal(str(len(consos_30j)))


# ═════════════════════════════════════════════════════════════════════════════
# RÈGLES DE CERTIFICATION — v5
# ═════════════════════════════════════════════════════════════════════════════

def _apply_certification_rules(result: CertificationResult) -> None:
    """
    v5 — Décorrélation alerte / certification + garde-fou cohérence interne.

    Source PRIMAIRE : Grid (conso_fms_*)
    Source FALLBACK : ACM (acm_available=True)

    DÉCISIONS DÉSORMAIS ORTHOGONALES :

    • flag_mesure_alert (booléen) : alerte sur la mesure FMS / historique
        - ratio < 50% ou > 150% (alerte bidirectionnelle v4.1)
        - |ratio_periode - ratio_30j| > 50pts (incohérence interne v5)

    • status : calculé indépendamment via les seuils 95% sur les ratios
        - CERTIFIED_FMS    : ratio FMS période OU 30j > 95%
        - CERTIFIED_SENELEC: ratio histo 3M > 95%
        - MESURE_A_VERIFIER: aucune certif + flag_mesure_alert=True
        - NEEDS_REVIEW     : aucune certif + pas d'alerte

    Conséquence : 4 cas possibles
        (A) CERTIFIED + flag=False        → certifié sain
        (B) CERTIFIED + flag=True         → certifié mais mesure suspecte (à investiguer)
        (C) MESURE_A_VERIFIER + flag=True → non certifié + mesure suspecte (combo perdant)
        (D) NEEDS_REVIEW + flag=False     → non certifié, mesure correcte, simple écart 50-95%
    """
    fact_p  = result.conso_facturee_periode
    fact_30 = result.conso_facturee_30j

    # ── Calcul des ratios ────────────────────────────────────────────────────
    result.ratio_fms_periode = _safe_div(result.conso_fms_periode, fact_p)
    result.ratio_fms_30j     = _safe_div(result.conso_fms_30j,     fact_30)
    result.ratio_histo_3mois = _safe_div(result.histo_3mois_avg,   fact_30)

    # Ratios ACM informatifs (toujours calculés si donnée ACM dispo)
    if result.estim_conso_acm_periode is not None or result.estim_conso_acm_30j is not None:
        result.ratio_acm_periode = _safe_div(result.estim_conso_acm_periode, fact_p)
        result.ratio_acm_30j     = _safe_div(result.estim_conso_acm_30j,     fact_30)

    # ── 1. CALCUL DU FLAG_MESURE_ALERT (indépendant du statut) ───────────────
    flag_mesure = False
    triggers    = []

    def _check(ratio, label):
        nonlocal flag_mesure
        if ratio is None:
            return
        if ratio < SEUIL_ALERTE_BAS:
            flag_mesure = True
            triggers.append(f"{label}<50% ({ratio:.3f})")
        elif ratio > SEUIL_ALERTE_HAUT:
            flag_mesure = True
            triggers.append(f"{label}>150% ({ratio:.3f})")

    # Alertes bidirectionnelles classiques (v4.1)
    if result.fms_available:
        _check(result.ratio_fms_periode, "FMS_p")
        _check(result.ratio_fms_30j,     "FMS_30j")
    _check(result.ratio_histo_3mois, "Histo")

    # ✅ v5 — Garde-fou cohérence interne période ↔ 30j
    # Sur des factures de ~30 jours, les deux ratios doivent être proches.
    # Un écart > 0,5 (50pts) signe une fenêtre vérolée par un point aberrant
    # capté par une fenêtre SQL et pas par l'autre.
    # Cas typique : KLD_2007 facture 7541624499 → r_p=5,52 vs r_30j=0,92
    # (aujourd'hui certifié à tort, demain flaggé).
    if (result.fms_available
        and result.ratio_fms_periode is not None
        and result.ratio_fms_30j is not None):
        ecart_fms = abs(result.ratio_fms_periode - result.ratio_fms_30j)
        if ecart_fms > SEUIL_INCOHERENCE:
            flag_mesure = True
            triggers.append(f"incoherence_FMS_p_vs_30j ({ecart_fms:.2f})")

    result.flag_mesure_alert = flag_mesure

    # ── 2. DÉTERMINATION DU STATUT (indépendant de flag_mesure_alert) ────────
    # Cohérence 1 : FMS (Grid primaire ou ACM fallback)
    if result.fms_available:
        fms_ok = (
            (result.ratio_fms_periode is not None and result.ratio_fms_periode > THRESHOLD_FMS)
            or
            (result.ratio_fms_30j is not None and result.ratio_fms_30j > THRESHOLD_FMS)
        )
        if fms_ok:
            result.status = CertificationResult.Status.CERTIFIED_FMS
            if result.acm_available:
                result.certified_by_rule = (
                    CertificationResult.CertifiedByRule.ACM_PERIODE
                    if result.ratio_acm_periode is not None and result.ratio_acm_periode > THRESHOLD_FMS
                    else CertificationResult.CertifiedByRule.ACM_30J
                )
            else:
                result.certified_by_rule = (
                    CertificationResult.CertifiedByRule.FMS_PERIODE
                    if result.ratio_fms_periode is not None and result.ratio_fms_periode > THRESHOLD_FMS
                    else CertificationResult.CertifiedByRule.FMS_30J
                )
            log_suffix = "  ⚠ AVEC ALERTE MESURE" if flag_mesure else ""
            logger.info(
                "[rules] %s → CERTIFIED_FMS rule=%s acm_fallback=%s%s | triggers=%s",
                result.invoice.numero_facture, result.certified_by_rule,
                result.acm_available, log_suffix,
                " | ".join(triggers) if triggers else "—",
            )
            return

    # Cohérence 2 : Historique
    if result.ratio_histo_3mois is not None and result.ratio_histo_3mois > THRESHOLD_HISTO:
        result.status            = CertificationResult.Status.CERTIFIED_SENELEC
        result.certified_by_rule = CertificationResult.CertifiedByRule.HISTO_3MOIS
        log_suffix = "  ⚠ AVEC ALERTE MESURE" if flag_mesure else ""
        logger.info(
            "[rules] %s → CERTIFIED_SENELEC%s | triggers=%s",
            result.invoice.numero_facture, log_suffix,
            " | ".join(triggers) if triggers else "—",
        )
        return

    # Aucune certif possible → bascule selon présence d'alerte
    if flag_mesure:
        result.status = CertificationResult.Status.MESURE_A_VERIFIER
        logger.info(
            "[rules] %s → MESURE_A_VERIFIER (non certifié + alerte) | %s",
            result.invoice.numero_facture, " | ".join(triggers),
        )
    else:
        result.status = CertificationResult.Status.NEEDS_REVIEW
        logger.info(
            "[rules] %s → NEEDS_REVIEW (non certifié, sans alerte)",
            result.invoice.numero_facture,
        )


# ═════════════════════════════════════════════════════════════════════════════
# COMPTEURS BATCH
# ═════════════════════════════════════════════════════════════════════════════

# Note : `mesure_alert` reste mappé au statut MESURE_A_VERIFIER (non-certifié+alerte)
# pour garder la barre de progression cohérente (segments mutuellement exclusifs).
# Le total des certifiées-avec-alerte se voit au niveau des lignes (icône + bg peach)
# et via le filtre "Alertes seulement" qui filtre sur flag_mesure_alert=True.
_STATUS_TO_COUNTER = {
    CertificationResult.Status.CERTIFIED_FMS:     "certified_fms",
    CertificationResult.Status.CERTIFIED_SENELEC: "certified_senelec",
    CertificationResult.Status.NEEDS_REVIEW:      "needs_review",
    CertificationResult.Status.UNKNOWN_CONTRACT:  "unknown_contract",
    CertificationResult.Status.FMS_UNAVAILABLE:   "fms_unavailable",
    CertificationResult.Status.MESURE_A_VERIFIER: "mesure_alert",
}


def _increment_batch_counter(cert_batch_id: int, status: str) -> None:
    counter_field = _STATUS_TO_COUNTER.get(status)
    if counter_field:
        CertificationBatch.objects.filter(pk=cert_batch_id).update(
            **{counter_field: F(counter_field) + 1}
        )


# ═════════════════════════════════════════════════════════════════════════════
# TÂCHES CELERY (inchangées par rapport à v4.1)
# ═════════════════════════════════════════════════════════════════════════════

@shared_task(bind=True, max_retries=2, default_retry_delay=60,
             name="certification.certify_single_invoice")
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
    site_id       = result.site.site_id

    try:
        efms = EfmsService(cert_batch=result.cert_batch)

        # ── Étape 4 : Normalisation conso facturée ────────────────────────────
        nb_jours = None
        if invoice.date_debut_periode and invoice.date_fin_periode:
            nb_jours = (invoice.date_fin_periode - invoice.date_debut_periode).days + 1

        conso_p   = invoice.conso_facturee
        conso_30j = (
            Decimal(str(conso_p)) * 30 / Decimal(str(nb_jours))
            if conso_p and nb_jours and nb_jours > 0 else None
        )

        result.conso_facturee_periode = conso_p
        result.nb_jours_facturation   = nb_jours
        result.conso_facturee_30j     = conso_30j

        date_debut = invoice.date_debut_periode
        date_fin   = invoice.date_fin_periode

        # ── Étape 5 : Source PRIMAIRE — Grid ─────────────────────────────────
        grid_p, grid_30j, last_month = (
            efms.get_conso_grid_cached(cert_batch_id, site_id, date_debut, date_fin)
            if date_debut and date_fin else (None, None, None)
        )

        if grid_p is None and grid_30j is None and date_debut and date_fin:
            try:
                grid_p, method = efms.get_conso_periode(site_id, date_debut, date_fin)
                if grid_p is None:
                    grid_30j_val, last_month = efms.get_conso_last_complete_month(site_id, date_debut)
                    grid_30j = grid_30j_val
                else:
                    grid_30j = grid_p * 30 / Decimal(str(nb_jours)) if nb_jours else None
            except EfmsConnectionError as e:
                result.fms_error = f"eFMS inaccessible: {e}"
            except EfmsQueryError as e:
                result.fms_error = str(e)

        if grid_p is not None or grid_30j is not None:
            # Grid disponible → source principale
            result.fms_available           = True
            result.conso_fms_periode       = grid_p
            result.conso_fms_30j           = grid_30j
            result.fms_last_complete_month = last_month

            # Charger ACM informatif (Grid primaire mais ACM affiché)
            acm_p_info, acm_30j_info = (
                efms.get_conso_acm_cached(cert_batch_id, site_id, date_debut, date_fin)
                if date_debut and date_fin else (None, None)
            )
            if acm_p_info is not None or acm_30j_info is not None:
                result.estim_conso_acm_periode = acm_p_info
                result.estim_conso_acm_30j     = acm_30j_info

        else:
            # ── Étape 5B : Fallback — ACM ─────────────────────────────────────
            acm_p, acm_30j = (
                efms.get_conso_acm_cached(cert_batch_id, site_id, date_debut, date_fin)
                if date_debut and date_fin else (None, None)
            )

            if acm_p is None and acm_30j is None and date_debut and date_fin:
                try:
                    acm_p, acm_30j = efms.get_conso_acm(site_id, date_debut, date_fin)
                except EfmsConnectionError as e:
                    result.acm_error = f"eFMS inaccessible: {e}"
                except EfmsQueryError as e:
                    result.acm_error = str(e)

            if acm_p is not None or acm_30j is not None:
                result.fms_available           = True
                result.conso_fms_periode       = acm_p
                result.conso_fms_30j           = acm_30j
                result.acm_available           = True
                result.estim_conso_acm_periode = acm_p
                result.estim_conso_acm_30j     = acm_30j
            else:
                result.fms_available = False
                if not result.fms_error and not result.acm_error:
                    result.fms_error = "Grid et ACM indisponibles pour ce site"

        # ── Étape 6 : Historique interne Senelec ─────────────────────────────
        result.histo_last_conso, result.histo_3mois_avg = _fetch_internal_history(invoice)

        # ── Étape 7 : Règles de certification (v5) ────────────────────────────
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

        result.save()
        _increment_batch_counter(cert_batch_id, result.status)

        logger.info(
            "[certify] #%d → %s | rule=%s | grid=%s | acm_fallback=%s | acm_info=%s | alert=%s",
            result_id, result.status, result.certified_by_rule,
            result.fms_available and not result.acm_available,
            result.acm_available,
            result.estim_conso_acm_periode is not None,
            result.flag_mesure_alert,
        )
        return {"result_id": result_id, "status": result.status}

    except Exception as exc:
        logger.exception(f"[certify] Erreur inattendue result #{result_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task(name="certification.finalize_certification_batch")
def finalize_certification_batch(results: list, cert_batch_id: int) -> dict:
    try:
        batch = CertificationBatch.objects.get(id=cert_batch_id)
    except CertificationBatch.DoesNotExist:
        return {"error": "batch_not_found"}

    batch.refresh_counters()
    batch.status      = CertificationBatch.Status.DONE
    batch.finished_at = timezone.now()
    batch.save(update_fields=[
        "status", "finished_at",
        "certified_fms", "certified_senelec",
        "needs_review", "unknown_contract", "fms_unavailable",
        "mesure_alert",
    ])
    return {
        "cert_batch_id":     cert_batch_id,
        "status":            "DONE",
        "total":             batch.total,
        "certified_fms":     batch.certified_fms,
        "certified_senelec": batch.certified_senelec,
        "needs_review":      batch.needs_review,
        "unknown_contract":  batch.unknown_contract,
        "fms_unavailable":   batch.fms_unavailable,
        "mesure_alert":      batch.mesure_alert,
    }


@shared_task(name="certification.launch_certification_batch")
def launch_certification_batch(cert_batch_id: int) -> None:
    try:
        cert_batch = CertificationBatch.objects.select_related("import_batch").get(id=cert_batch_id)
    except CertificationBatch.DoesNotExist:
        return

    cert_batch.status = CertificationBatch.Status.RUNNING
    cert_batch.save(update_fields=["status"])

    try:
        invoices = cert_batch.import_batch.rows.select_related("site").all()
        if not invoices.exists():
            cert_batch.status      = CertificationBatch.Status.FAILED
            cert_batch.finished_at = timezone.now()
            cert_batch.save(update_fields=["status", "finished_at"])
            return

        result_ids    = []
        unknown_count = 0

        with transaction.atomic():
            for invoice in invoices:
                if invoice.site_id is None:
                    CertificationResult.objects.create(
                        cert_batch=cert_batch, invoice=invoice,
                        site=None, status=CertificationResult.Status.UNKNOWN_CONTRACT,
                    )
                    unknown_count += 1
                    continue

                result, created = CertificationResult.objects.get_or_create(
                    cert_batch=cert_batch, invoice=invoice,
                    defaults={"site": invoice.site, "status": CertificationResult.Status.PENDING_CERTIFICATION},
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
            return

        CertificationBatch.objects.filter(pk=cert_batch_id).update(total=len(result_ids) + unknown_count)

        site_ids_with_dates = (
            CertificationResult.objects.filter(id__in=result_ids)
            .select_related("invoice", "site")
            .values_list("site__site_id", "invoice__date_debut_periode", "invoice__date_fin_periode")
        )

        # Groupe par période EXACTE (pas une seule enveloppe min/max pour tout
        # le batch) : un batch mélange en pratique des factures de périodes très
        # différentes (constaté : jusqu'à ~9 mois d'écart entre factures d'un
        # même batch), donc une enveloppe globale ferait calculer la "conso
        # période" ACM/Grid de chaque site sur une fenêtre bien plus large que
        # sa propre facture — écarts constatés jusqu'à ×6 sur la conso ACM.
        # Les sites qui partagent la même période exacte restent regroupés en
        # une seule requête bulk Snowflake (pas de perte de l'optimisation).
        period_groups: dict[tuple, set] = {}
        for site_id, d_debut, d_fin in site_ids_with_dates:
            if not site_id or not d_debut or not d_fin:
                continue
            period_groups.setdefault((d_debut, d_fin), set()).add(site_id)

        if period_groups:
            try:
                efms = EfmsService(cert_batch=cert_batch)
                efms.prefetch_batch(
                    cert_batch_id=cert_batch_id,
                    groups=[
                        (list(site_ids), d_debut, d_fin)
                        for (d_debut, d_fin), site_ids in period_groups.items()
                    ],
                )
            except Exception as e:
                logger.warning("[launch] Prefetch échoué (%s)", e)

        chord(
            group([certify_single_invoice.s(rid) for rid in result_ids]),
            finalize_certification_batch.s(cert_batch_id),
        ).apply_async()

    except Exception as exc:
        logger.exception(f"[launch] Erreur critique batch #{cert_batch_id}: {exc}")
        cert_batch.status = CertificationBatch.Status.FAILED
        cert_batch.save(update_fields=["status"])
        raise