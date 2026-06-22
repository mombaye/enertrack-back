# estimation/tasks.py
# Pipeline d'estimation mensuelle
#
# SOURCES D'ESTIMATION (ordre de priorité — aligné fichier client Janvier 2026) :
#   1. gFMS (ACM)         — act_energy_p, si données présentes
#   2. gFMS (Grid)        — Grid_Energy_Conso_per_day_kWh, si |FMS - Sénélec| < 500 kWh
#   3. Estimation SENELEC — conso facturée mois courant ramenée à nb_j
#   4. Estimation Target  — GridTargetRule.target_kwh_per_day × nb_j
#   5. Historique M-1     — MonthlySynthesis mois précédent
#   6. Estimation Théorique — Site.analysis_load × 24 × nb_j / 1000
#   7. NC                 — aucune source disponible
#
# CORRECTIONS :
#   [FIX A] get_conso_grid_cached retourne 3 valeurs (pas 6) — déballage corrigé
#   [FIX B] target_conso_kwh calculé depuis GridTargetRule (source TARGET)
#   [FIX C] Source SENELEC ajoutée (conso facturée mois courant)
#   [FIX D] Source THEORIQUE ajoutée (analysis_load × 24h × nb_j / 1000)
#   [FIX E] Seuil fiabilité 0.95 → 0.90 (conforme CDC)
#   [FIX F] Sites hors scope créent un EstimationResult (count_hors_scope correct)

import calendar
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from celery import shared_task, chord, group
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import EstimationBatch, EstimationResult
from certification.services.efms import EfmsService, EfmsConnectionError, EfmsQueryError

logger = logging.getLogger(__name__)

D0    = Decimal("0")
# [FIX E] Seuil fiabilité aligné sur CDC : 0.90 (pas 0.95)
THRESHOLD_FMS   = Decimal("0.90")
MIN_POINTS_FIABLE = 10

# Seuil ±500 kWh entre FMS et Sénélec pour valider la source Grid
SEUIL_ECART_FMS_SENELEC = Decimal("500")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _safe_div(a, b) -> Decimal | None:
    try:
        a, b = Decimal(str(a)), Decimal(str(b))
        return None if b == D0 else a / b
    except (InvalidOperation, TypeError):
        return None


def _nb_jours_mois(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _periode_mois(year: int, month: int) -> tuple[date, date]:
    nb = _nb_jours_mois(year, month)
    return date(year, month, 1), date(year, month, nb)


def _check_fiabilite_grid(
    grid_kwh,
    grid_kvah,
    grid_estimated_kwh,
) -> tuple[str, Decimal | None]:
    """
    Applique les règles de fiabilité Grid.
    Si kVAh absent → CORRECT par défaut (pas de preuve du contraire).
    """
    if not grid_kvah or grid_kvah == D0:
        if grid_kwh and grid_kwh > D0:
            return EstimationResult.FiabiliteGrid.CORRECT, None
        return EstimationResult.FiabiliteGrid.MISSING, None

    if not grid_estimated_kwh or grid_estimated_kwh == D0:
        return EstimationResult.FiabiliteGrid.MISSING, None

    ratio = _safe_div(grid_kwh, grid_kvah)
    if ratio is None or ratio < Decimal("0.8") or ratio > Decimal("1.1"):
        return EstimationResult.FiabiliteGrid.NOT_CORRECT, ratio

    ecart = abs(grid_kvah - Decimal(str(grid_estimated_kwh))) / grid_kvah
    if ecart > Decimal("0.1"):
        return EstimationResult.FiabiliteGrid.NOT_CORRECT, ratio

    return EstimationResult.FiabiliteGrid.CORRECT, ratio


def _fetch_histo(site, year: int, month: int) -> tuple[Decimal | None, int]:
    """
    M-1 exact en priorité, moyenne 3 mois en fallback.
    Exclut le mois courant.
    """
    from billing.models import MonthlySynthesis

    qs = (
        MonthlySynthesis.objects
        .filter(source__site=site)
        .exclude(conso__isnull=True)
        .exclude(conso=D0)
        .exclude(year=year, month=month)
        .order_by("-year", "-month")
        .values("conso", "days_covered")
    )

    m1 = qs[:1]
    if m1:
        row = m1[0]
        days = row["days_covered"]
        if days and days > 0:
            conso_30j = Decimal(str(row["conso"])) * 30 / Decimal(str(days))
            if conso_30j > D0:
                return conso_30j, 1

    consos_30j = []
    for row in qs[:3]:
        days = row["days_covered"]
        if days and days > 0:
            consos_30j.append(Decimal(str(row["conso"])) * 30 / Decimal(str(days)))

    if not consos_30j:
        return None, 0

    avg = sum(consos_30j) / Decimal(str(len(consos_30j)))
    return avg, len(consos_30j)


def _fetch_senelec_conso(site, year: int, month: int) -> Decimal | None:
    """
    [FIX C] Source SENELEC : conso facturée du mois courant ramenée à nb_j.
    = Consommation facturée / days_covered × nb_j_mois
    Conforme au calcul client : col T "Consommation facturée ramenée à 30 jours".
    """
    from billing.models import MonthlySynthesis
    nb_j = _nb_jours_mois(year, month)

    row = (
        MonthlySynthesis.objects
        .filter(source__site=site, year=year, month=month)
        .exclude(conso__isnull=True)
        .exclude(conso=D0)
        .values("conso", "days_covered")
        .first()
    )
    if not row:
        return None
    days = row["days_covered"]
    if not days or days <= 0:
        return None
    conso_mois = Decimal(str(row["conso"])) * nb_j / Decimal(str(days))
    return conso_mois if conso_mois > D0 else None


def _fetch_target_conso(site, nb_j: int) -> Decimal | None:
    """
    [FIX B] Source TARGET : GridTargetRule.target_kwh_per_day × nb_j.
    Lookup par (configuration, site_type, load_band) comme dans le fichier client.
    """
    from core.models import GridTargetRule

    rule = GridTargetRule.objects.filter(
        configuration=site.configuration or "",
        site_type=site.site_type or "",
        load_band=site.load_band or "",
        active=True,
    ).first()

    if rule and rule.target_kwh_per_day:
        val = Decimal(str(rule.target_kwh_per_day)) * Decimal(str(nb_j))
        return val if val > D0 else None

    if rule and rule.target_kwh:
        return Decimal(str(rule.target_kwh)) if Decimal(str(rule.target_kwh)) > D0 else None

    return None


def _fetch_theorique_conso(site, nb_j: int) -> Decimal | None:
    """
    [FIX D] Source THEORIQUE : Site.analysis_load [W] × 24h × nb_j / 1000 → kWh.
    Dernier recours quand aucune autre source n'est disponible.
    """
    if not site.analysis_load or site.analysis_load <= 0:
        return None
    val = Decimal(str(site.analysis_load)) * 24 * Decimal(str(nb_j)) / Decimal("1000")
    return val if val > D0 else None


def _compute_montant_from_conso(site, conso_kwh: Decimal, nb_jours: int, ref_date: date) -> dict:
    """Calcule le montant HTVA estimé à partir d'une consommation kWh."""
    from billing.models import ContractSiteLink, TariffRate, SonatelInvoice
    from certification.services.billing_check import (
        _normalize_police, _d, _REDEVANCE, _LCTR, _q
    )

    link = ContractSiteLink.objects.filter(site=site).first()
    if not link:
        return {"error": "Aucun lien contrat-site trouvé"}

    inv = (
        SonatelInvoice.objects
        .filter(site=site)
        .exclude(type_de_tarif__isnull=True)
        .order_by("-date_debut_periode")
        .first()
    )
    if not inv:
        return {"error": "Aucune facture de référence"}

    police = _normalize_police(inv.type_de_tarif)
    if not police:
        return {"error": f"Police non reconnue : {inv.type_de_tarif}"}

    tr = (
        TariffRate.objects
        .filter(category__iexact=police, date_debut__lte=ref_date, date_fin__gte=ref_date)
        .order_by("-date_debut")
        .first()
    )
    if not tr:
        return {"error": f"Tarif introuvable pour {police} à {ref_date}"}

    nb_j                 = Decimal(str(nb_jours))
    tarif_k1             = _d(tr.energie_k1)
    tarif_k2             = _d(tr.energie_k2)
    tarif_k3             = _d(getattr(tr, "energie_k3", None) or tr.energie_k2)
    prime_fixe_mensuelle = _d(tr.prime_fixe)
    ps                   = _d(inv.puissance_souscrite)
    pmax                 = _d(inv.puissance_max_relevee)

    LCTR_POLICES = {"DPP", "PMP", "PPP", "DMP"}
    BT_POLICES   = {"PGP", "PFP", "PMP", "DGP", "DPP", "PPP", "DMP"}

    if police in LCTR_POLICES and police in _LCTR:
        t1, t2 = _LCTR[police]
        conso_lctr1 = min(conso_kwh, t1)
        conso_lctr2 = min(max(D0, conso_kwh - conso_lctr1), t2)
        conso_lctr3 = max(D0, conso_kwh - conso_lctr1 - conso_lctr2)
        montant_k1 = conso_lctr1 * tarif_k1
        montant_k2 = conso_lctr2 * tarif_k2
        montant_k3 = conso_lctr3 * tarif_k3
    else:
        montant_k1 = conso_kwh * tarif_k1
        montant_k2 = D0
        montant_k3 = D0

    prime_fixe  = prime_fixe_mensuelle * ps * nb_j / Decimal("30")
    delta_p     = max(D0, pmax - ps)
    penalite_pf = Decimal("1.5") * prime_fixe_mensuelle * delta_p * nb_j / Decimal("30")
    montant_nrj = montant_k1 + montant_k2 + montant_k3 + prime_fixe + penalite_pf

    montant_tco = D0
    if police in BT_POLICES:
        montant_tco = Decimal("0.025") * montant_nrj

    montant_redevance = D0
    if police in _REDEVANCE:
        montant_redevance = _REDEVANCE[police] / Decimal("30") * nb_j

    montant_abonnement = prime_fixe + montant_redevance + montant_tco
    montant_htva       = montant_nrj + montant_tco + montant_redevance

    return {
        "montant_estime":     _q(montant_htva),
        "montant_nrj":        _q(montant_nrj),
        "montant_abonnement": _q(montant_abonnement),
        "montant_redevance":  _q(montant_redevance),
        "montant_tco":        _q(montant_tco),
        "error":              None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TÂCHE 1 — Estimation d'UN site
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(
    bind=True,
    max_retries=2,
    default_retry_delay=60,
    name="estimation.estimate_single_site",
)
def estimate_single_site(self, result_id: int) -> dict:
    try:
        result = EstimationResult.objects.select_related("batch", "site").get(id=result_id)
    except EstimationResult.DoesNotExist:
        logger.error(f"[estimation] EstimationResult #{result_id} introuvable.")
        return {"result_id": result_id, "status": "ERROR"}

    batch    = result.batch
    site     = result.site
    year     = batch.year
    month    = batch.month
    nb_j     = _nb_jours_mois(year, month)
    debut, fin = _periode_mois(year, month)
    ref_date = debut

    result.nb_jours_mois = nb_j

    try:
        efms = EfmsService()

        # ── Source 1 : ACM ────────────────────────────────────────────────────
        acm_p, acm_30j = efms.get_conso_acm_cached(batch.id, site.site_id)

        if acm_p is None and acm_30j is None:
            try:
                acm_p, acm_30j = efms.get_conso_acm(site.site_id, debut, fin)
            except (EfmsConnectionError, EfmsQueryError) as e:
                result.error_message = f"ACM erreur: {e}"
                logger.warning("[estimation] %s ACM: %s", site.site_id, e)

        if acm_p is not None and acm_p > D0:
            result.acm_disponible    = True
            result.acm_conso_kwh     = acm_p
            result.source_utilisee   = EstimationResult.Source.ACM
            result.conso_estimee_kwh = acm_p
            result.fiabilite_grid    = EstimationResult.FiabiliteGrid.NA
            logger.info("[estimation] %s → ACM %.1f kWh", site.site_id, float(acm_p))

        else:
            # ── Source 2 : Grid FMS ───────────────────────────────────────────
            # [FIX A] get_conso_grid_cached retourne 3 valeurs
            grid_p, grid_30j, _ = efms.get_conso_grid_cached(batch.id, site.site_id)
            kvah = kvarh = est_kwh = None

            if grid_p is None:
                try:
                    grid_p, _ = efms.get_conso_periode(site.site_id, debut, fin)
                except (EfmsConnectionError, EfmsQueryError) as e:
                    result.error_message = (result.error_message or "") + f" Grid erreur: {e}"
                    logger.warning("[estimation] %s Grid: %s", site.site_id, e)

            if grid_p is not None and grid_p > D0:
                result.grid_disponible = True
                result.grid_conso_kwh  = grid_p

                # Fiabilité Grid
                fiabilite, ratio = _check_fiabilite_grid(
                    grid_kwh=grid_p,
                    grid_kvah=kvah,
                    grid_estimated_kwh=est_kwh,
                )
                result.fiabilite_grid  = fiabilite
                result.fiabilite_ratio = ratio

                # [FIX E] Vérification ±500 kWh Grid vs Sénélec facturé
                # Si l'écart est > 500 kWh → FMS non fiable pour ce mois
                senelec_mois = _fetch_senelec_conso(site, year, month)
                if (
                    fiabilite == EstimationResult.FiabiliteGrid.CORRECT
                    and senelec_mois is not None
                    and abs(grid_p - senelec_mois) > SEUIL_ECART_FMS_SENELEC
                ):
                    fiabilite = EstimationResult.FiabiliteGrid.NOT_CORRECT
                    result.fiabilite_grid = fiabilite
                    logger.info(
                        "[estimation] %s Grid écart FMS/Sénélec %.0f kWh > 500 → NOT_CORRECT",
                        site.site_id, float(abs(grid_p - senelec_mois)),
                    )

                if fiabilite == EstimationResult.FiabiliteGrid.CORRECT:
                    result.source_utilisee   = EstimationResult.Source.GRID
                    result.conso_estimee_kwh = grid_p
                    logger.info("[estimation] %s → Grid %.1f kWh", site.site_id, float(grid_p))

            # ── Source 3 : Estimation SENELEC (conso facturée mois courant) ───
            if result.conso_estimee_kwh is None:
                senelec_conso = senelec_mois if grid_p else _fetch_senelec_conso(site, year, month)
                if senelec_conso is not None and senelec_conso > D0:
                    result.source_utilisee   = EstimationResult.Source.SENELEC
                    result.conso_estimee_kwh = senelec_conso
                    logger.info("[estimation] %s → SENELEC %.1f kWh", site.site_id, float(senelec_conso))

            # ── Source 4 : Estimation Target (GridTargetRule) ─────────────────
            # [FIX B] target_conso_kwh calculé et utilisé comme source de fallback
            if result.conso_estimee_kwh is None:
                target_conso = _fetch_target_conso(site, nb_j)
                if target_conso is not None and target_conso > D0:
                    result.target_conso_kwh  = target_conso
                    result.source_utilisee   = EstimationResult.Source.TARGET
                    result.conso_estimee_kwh = target_conso
                    logger.info("[estimation] %s → TARGET %.1f kWh", site.site_id, float(target_conso))
            else:
                # Calculer quand même target_conso_kwh pour info même si non utilisé
                result.target_conso_kwh = _fetch_target_conso(site, nb_j)

            # ── Source 5 : Historique Sénélec M-1 ─────────────────────────────
            if result.conso_estimee_kwh is None:
                histo_30j, histo_nb = _fetch_histo(site, year, month)
                if histo_30j is not None and histo_30j > D0:
                    conso_mois = histo_30j * Decimal(str(nb_j)) / Decimal("30")
                    result.histo_disponible  = True
                    result.histo_conso_30j   = histo_30j
                    result.histo_nb_mois     = histo_nb
                    result.source_utilisee   = EstimationResult.Source.HISTO
                    result.conso_estimee_kwh = conso_mois
                    logger.info("[estimation] %s → HISTO %.1f kWh (%dm)", site.site_id, float(conso_mois), histo_nb)

            # ── Source 6 : Estimation Théorique ──────────────────────────────
            # [FIX D] analysis_load × 24h × nb_j / 1000
            if result.conso_estimee_kwh is None:
                theorique = _fetch_theorique_conso(site, nb_j)
                if theorique is not None and theorique > D0:
                    result.source_utilisee   = EstimationResult.Source.THEORIQUE
                    result.conso_estimee_kwh = theorique
                    logger.info("[estimation] %s → THEORIQUE %.1f kWh", site.site_id, float(theorique))
                else:
                    result.source_utilisee = EstimationResult.Source.NC
                    if not result.error_message:
                        result.error_message = "Aucune source disponible (ACM, Grid, SENELEC, Target, Histo, Théorique)"
                    logger.info("[estimation] %s → NC", site.site_id)

        # ── Calcul montant HTVA ───────────────────────────────────────────────
        if result.conso_estimee_kwh is not None and result.conso_estimee_kwh > D0:
            montant_data = _compute_montant_from_conso(
                site=site,
                conso_kwh=result.conso_estimee_kwh,
                nb_jours=nb_j,
                ref_date=ref_date,
            )
            if montant_data.get("error"):
                result.error_message = (result.error_message or "") + f" Montant: {montant_data['error']}"
            else:
                result.montant_estime     = montant_data["montant_estime"]
                result.montant_nrj        = montant_data["montant_nrj"]
                result.montant_abonnement = montant_data["montant_abonnement"]
                result.montant_redevance  = montant_data["montant_redevance"]
                result.montant_tco        = montant_data["montant_tco"]

        result.save()

        counter_map = {
            EstimationResult.Source.ACM:        "count_acm",
            EstimationResult.Source.GRID:       "count_grid",
            EstimationResult.Source.SENELEC:    "count_senelec",
            EstimationResult.Source.TARGET:     "count_target",
            EstimationResult.Source.HISTO:      "count_histo",
            EstimationResult.Source.THEORIQUE:  "count_theorique",
            EstimationResult.Source.NC:         "count_nc",
            EstimationResult.Source.HORS_SCOPE: "count_hors_scope",
        }
        counter = counter_map.get(result.source_utilisee)
        if counter:
            EstimationBatch.objects.filter(pk=batch.id).update(**{counter: F(counter) + 1})

        return {"result_id": result_id, "source": result.source_utilisee}

    except Exception as exc:
        logger.exception(f"[estimation] Erreur inattendue result #{result_id}: {exc}")
        raise self.retry(exc=exc)


# ─────────────────────────────────────────────────────────────────────────────
# TÂCHE 2 — Finalisation
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name="estimation.finalize_estimation_batch")
def finalize_estimation_batch(results: list, batch_id: int) -> dict:
    try:
        batch = EstimationBatch.objects.get(id=batch_id)
    except EstimationBatch.DoesNotExist:
        return {"error": "batch_not_found"}

    batch.refresh_counters()
    batch.status      = EstimationBatch.Status.DONE
    batch.finished_at = timezone.now()
    batch.save(update_fields=[
        "status",
        "finished_at",
        "total",
        "count_acm",
        "count_grid",
        "count_senelec",
        "count_target",
        "count_theorique",
        "count_histo",
        "count_nc",
        "count_hors_scope",
    ])
    return {"batch_id": batch_id, "status": "DONE", "total": batch.total}


# ─────────────────────────────────────────────────────────────────────────────
# TÂCHE 3 — Orchestrateur
# ─────────────────────────────────────────────────────────────────────────────

@shared_task(name="estimation.launch_estimation_batch")
def launch_estimation_batch(batch_id: int) -> None:
    try:
        batch = EstimationBatch.objects.get(id=batch_id)
    except EstimationBatch.DoesNotExist:
        return

    batch.status = EstimationBatch.Status.RUNNING
    batch.save(update_fields=["status"])

    year, month = batch.year, batch.month
    debut, fin  = _periode_mois(year, month)

    try:
        from billing.models import ContractSiteLink

        active_links = ContractSiteLink.objects.filter(
            site__invoice_payment__iexact="Aktivco",
            site__grid_fee=True,
        ).select_related("site")

        # [FIX F] Sites hors scope → EstimationResult HORS_SCOPE
        hors_scope_links = ContractSiteLink.objects.filter(
            site__invoice_payment__iexact="Aktivco",
            site__grid_fee=False,
        ).select_related("site")

        result_ids = []
        hors_scope = 0

        with transaction.atomic():
            # Sites IN SCOPE
            for link in active_links:
                site = link.site
                result, created = EstimationResult.objects.get_or_create(
                    batch=batch,
                    site=site,
                    defaults={
                        "numero_compte_contrat": link.numero_compte_contrat,
                        "source_utilisee":       EstimationResult.Source.NC,
                    },
                )
                if not created:
                    result.source_utilisee   = EstimationResult.Source.NC
                    result.conso_estimee_kwh = None
                    result.montant_estime    = None
                    result.error_message     = None
                    result.acm_disponible    = False
                    result.grid_disponible   = False
                    result.histo_disponible  = False
                    result.save()
                result_ids.append(result.id)

            # [FIX F] Sites HORS SCOPE
            for link in hors_scope_links:
                site = link.site
                EstimationResult.objects.update_or_create(
                    batch=batch,
                    site=site,
                    defaults={
                        "numero_compte_contrat": link.numero_compte_contrat,
                        "source_utilisee":       EstimationResult.Source.HORS_SCOPE,
                        "conso_estimee_kwh":     Decimal("0"),
                        "montant_estime":        Decimal("0"),
                        "error_message":         "Site hors scope (grid_fee=False)",
                    },
                )
                hors_scope += 1

        EstimationBatch.objects.filter(pk=batch_id).update(
            total=len(result_ids) + hors_scope,
            count_acm=0,
            count_grid=0,
            count_senelec=0,
            count_target=0,
            count_theorique=0,
            count_histo=0,
            count_nc=0,
            count_hors_scope=hors_scope,
        )

        if not result_ids:
            batch.status      = EstimationBatch.Status.DONE
            batch.finished_at = timezone.now()
            batch.save(update_fields=["status", "finished_at"])
            return

        site_ids = list(
            EstimationResult.objects.filter(id__in=result_ids)
            .values_list("site__site_id", flat=True)
        )

        try:
            efms = EfmsService()
            efms.prefetch_batch(
                cert_batch_id=batch_id,
                site_ids=site_ids,
                date_debut=debut,
                date_fin=fin,
            )
        except Exception as e:
            logger.warning("[estimation] Prefetch FMS échoué (%s) — fallback ponctuel", e)

        all_tasks = [estimate_single_site.s(rid) for rid in result_ids]
        chord(
            group(all_tasks),
            finalize_estimation_batch.s(batch_id),
        ).apply_async()

        logger.info("[estimation] Batch #%d lancé — %d sites IN SCOPE + %d HORS SCOPE.", batch_id, len(result_ids), hors_scope)

    except Exception as exc:
        logger.exception(f"[estimation] Erreur critique batch #{batch_id}: {exc}")
        batch.status = EstimationBatch.Status.FAILED
        batch.save(update_fields=["status"])
        raise