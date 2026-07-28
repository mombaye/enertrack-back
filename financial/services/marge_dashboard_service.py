# financial/services/marge_dashboard_service.py
"""
Assemble les données du Dashboard Marge Grid (module Évaluation Financière) —
une ligne par site, avec les deux bases de marge (estimée / réelle), pour
consommation par MargeDashboardDataView. Toute l'agrégation/filtrage (§3 à §6
du CDC) se fait côté client — cette fonction ne renvoie que les données brutes.

Sources :
- bo_analysis.BOMarginSnapshot — import figé du fichier "Analyse Marge.xlsx"
  (redevance/estimation Mai+Juin, statut marge estimée, catégorie BO, owner).
- financial.FinancialEvaluation — marge réelle (redevance - facture Sénélec
  réelle) pour le mois cible, jointe par site.
- core.Site — région, batch opérationnel, indoor/outdoor, modernisation.
"""
from calendar import monthrange
from decimal import Decimal

from bo_analysis.models import ActionOwner, BOMarginSnapshot, CategorieBO
from billing.models import ContractMonth, ContractSiteLink
from core.models import Site
from financial.models import FinancialEvaluation


def _num(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        v = float(v)
    if v != v:  # NaN
        return None
    return v


def _clean(v, fallback="Non renseigné"):
    v = (v or "").strip()
    return v if v else fallback


def _load_senelec_by_site_pk(year: int, month: int) -> dict:
    """
    "Load Senelec" (charge moyenne déduite de la conso Sénélec du mois, W) —
    formule client : conso_kwh / 24 / nb_jours_du_mois * 1000. Basée sur
    ContractMonth.conso (facturation réelle), sommée par site (un site peut
    avoir plusieurs comptes contrat via ContractSiteLink).
    """
    nb_jours = monthrange(year, month)[1]

    site_to_contracts: dict[int, list[str]] = {}
    for site_pk, numero in ContractSiteLink.objects.values_list("site_id", "numero_compte_contrat"):
        site_to_contracts.setdefault(site_pk, []).append(numero)

    all_contracts = [n for nums in site_to_contracts.values() for n in nums]
    conso_by_contract = dict(
        ContractMonth.objects.filter(numero_compte_contrat__in=all_contracts, year=year, month=month)
        .values_list("numero_compte_contrat", "conso")
    )

    result = {}
    for site_pk, contracts in site_to_contracts.items():
        conso_kwh = sum((conso_by_contract.get(n) or 0) for n in contracts)
        if conso_kwh:
            result[site_pk] = float(conso_kwh) / 24 / nb_jours * 1000
    return result


def build_marge_dashboard_rows(year: int, month: int) -> dict:
    """
    Retourne {"rows": [...], "meta": {...}} — une ligne par site de
    BOMarginSnapshot, enrichie de la marge réelle (FinancialEvaluation) et
    du référentiel Site (région, batch, indoor/outdoor, modernisation).
    """
    categorie_bo_labels = dict(CategorieBO.choices)
    action_owner_labels = dict(ActionOwner.choices)
    zone_labels = dict(Site.ZONE_CHOICES)
    site_kind_labels = dict(Site.SITE_KIND)

    fe_by_site_pk = {}
    for fe in FinancialEvaluation.objects.filter(year=year, month=month).only(
        "site_id", "montant_htva_reel", "marge", "marge_statut",
        "calculation_source", "has_invoice_for_month",
    ):
        fe_by_site_pk[fe.site_id] = fe

    load_senelec_by_site_pk = _load_senelec_by_site_pk(year, month)

    rows = []
    month_a_label = month_b_label = ""

    for r in BOMarginSnapshot.objects.select_related("site").all():
        if not month_a_label and r.month_a_label:
            month_a_label, month_b_label = r.month_a_label, r.month_b_label

        site = r.site
        fe = fe_by_site_pk.get(r.site_id) if site else None

        region = zone_labels.get(site.zone, "Non renseigné") if site and site.zone else "Non renseigné"
        batch = _clean(site.batch_operational) if site else "Non renseigné"
        io = site_kind_labels.get(site.site_type, "Non renseigné") if site and site.site_type else "Non renseigné"
        if site:
            modernise = "Site modernisé" if site.modernized else "Site non modernisé"
        else:
            modernise = "Non renseigné"

        if r.categorie_bo == "autre" and r.categorie_bo_autre:
            cat_bo = r.categorie_bo_autre.strip()
        else:
            cat_bo = categorie_bo_labels.get(r.categorie_bo, "Non renseigné")

        if r.action_owner == "autre" and r.action_owner_autre:
            owner = r.action_owner_autre.strip()
        else:
            owner = action_owner_labels.get(r.action_owner, "Non renseigné")

        # "Réelle" au sens du CDC = adossée à une vraie facture Sénélec (payée ou
        # brute non payée), pas au repli sur l'estimation : calculation_source
        # ESTIMATION_ONLY signifie qu'aucune facture n'est encore rapprochée pour
        # ce site/mois — auquel cas marge/marge_statut existent quand même (calculés
        # par repli) mais ne représentent pas une donnée "réelle" au sens du CDC §4.2.
        has_real_invoice = fe is not None and fe.calculation_source in (
            FinancialEvaluation.CalculationSource.PAID_INVOICE,
            FinancialEvaluation.CalculationSource.RAW_UNPAID_INVOICE,
        )
        if has_real_invoice and fe.marge_statut:
            factures_reelles = _num(fe.montant_htva_reel)
            marge_reelle = _num(fe.marge)
            statut_reelle = fe.marge_statut
        else:
            factures_reelles = None
            marge_reelle = None
            statut_reelle = "RAS"

        rows.append({
            "site_id": r.site_id_raw,
            "site_name": r.site_name_raw or r.site_id_raw,
            "region": region,
            "batch": batch,
            "typo_facturee": _clean(r.typologie_reelle),
            "indoor_outdoor": io,
            "modernise": modernise,
            "redevance_mai": _num(r.redevance_grid_a),
            "redevance_juin": _num(r.redevance_grid_b),
            "conso_mai_xof": _num(r.estimation_conso_xof_a),
            "conso_juin_xof": _num(r.estimation_conso_xof_b),
            "marge_mai_est": _num(r.redevance_vs_estimation_a),
            "marge_juin_est": _num(r.redevance_vs_estimation_b),
            "statut_est": (r.statut_marge or "RAS").strip().upper() or "RAS",
            "factures_reelles": factures_reelles,
            "marge_reelle": marge_reelle,
            "statut_reelle": statut_reelle,
            "categorie_bo": cat_bo,
            "comment_bo": (r.commentaire_bo or "").strip(),
            "owner": owner,
            "commentaire": (r.commentaire or "").strip(),
            "load_senelec_w": load_senelec_by_site_pk.get(site.id) if site else None,
        })

    return {
        "rows": rows,
        "meta": {
            "total_sites": len(rows),
            "month_a_label": month_a_label or "Mai",
            "month_b_label": month_b_label or "Juin",
            "year": year,
        },
    }
