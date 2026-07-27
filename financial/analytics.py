# financial/analytics.py
"""
Service d'analyse approfondie des marges NOK.
Fournit des insights sur les causes racines et les corrélations.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from datetime import date

from django.db.models import Q, Sum, Avg, Count, F, Case, When, Value, DecimalField
from django.db.models.functions import Coalesce, Abs

from .models import FinancialEvaluation, SiteMonthlyLoad
from billing.models import ContractMonth, ContractSiteLink, MonthlySynthesis
from certification.models import CertificationResult, CertificationBatch

D0 = Decimal("0")
Q3 = Decimal("0.001")


def _d(v) -> Decimal:
    if v is None:
        return D0
    return Decimal(str(v))


def _q(v: Decimal) -> Decimal:
    return v.quantize(Q3, rounding=ROUND_HALF_UP)


def _pct(part: Decimal, total: Decimal) -> float:
    if total == D0:
        return 0.0
    return float((part / total * 100).quantize(Decimal("0.01")))


class MargeAnalytics:
    """
    Analyses approfondies des marges financières.
    """

    @staticmethod
    def get_period_range(year: int, month_start: int, month_end: int) -> list[tuple[int, int]]:
        """Génère la liste des (year, month) pour une plage."""
        periods = []
        y, m = year, month_start
        while (y, m) <= (year if month_end >= month_start else year + 1, month_end):
            periods.append((y, m))
            m += 1
            if m > 12:
                m = 1
                y += 1
            if len(periods) > 24:  # Sécurité
                break
        return periods

    @staticmethod
    def summary(
        year: int,
        month_start: int = 1,
        month_end: int = 12,
    ) -> dict:
        """
        Résumé global sur une période.
        """
        qs = FinancialEvaluation.objects.filter(year=year)
        if month_start == month_end:
            qs = qs.filter(month=month_start)
        else:
            qs = qs.filter(month__gte=month_start, month__lte=month_end)

        agg = qs.aggregate(
            total_redevance=Coalesce(Sum("redevance"), D0),
            total_facture=Coalesce(Sum("montant_htva_reel"), D0),
            total_marge=Coalesce(Sum("marge"), D0),
            count_total=Count("id"),
            count_ok=Count("id", filter=Q(marge_statut="OK")),
            count_nok=Count("id", filter=Q(marge_statut="NOK")),
            count_hc=Count("id", filter=Q(hors_catalogue=True)),
            count_pc=Count("id", filter=Q(periode_courte=True)),
            count_no_load=Count("id", filter=Q(load_w__isnull=True)),
            count_no_rule=Count("id", filter=Q(fee_rule__isnull=True)),
            avg_marge=Coalesce(Avg("marge"), D0),
            # Récurrence
            count_light=Count("id", filter=Q(recurrence_type="light")),
            count_critique=Count("id", filter=Q(recurrence_type="critique")),
        )

        # Marge négative totale (pour comprendre l'ampleur)
        marge_negative = qs.filter(marge__lt=0).aggregate(
            total=Coalesce(Sum("marge"), D0)
        )["total"]

        # Marge positive totale
        marge_positive = qs.filter(marge__gt=0).aggregate(
            total=Coalesce(Sum("marge"), D0)
        )["total"]

        return {
            "periode": f"{year}-{month_start:02d}" if month_start == month_end else f"{year}-{month_start:02d} → {year}-{month_end:02d}",
            "total_redevance": str(_q(_d(agg["total_redevance"]))),
            "total_facture": str(_q(_d(agg["total_facture"]))),
            "total_marge": str(_q(_d(agg["total_marge"]))),
            "marge_negative_totale": str(_q(_d(marge_negative))),
            "marge_positive_totale": str(_q(_d(marge_positive))),
            "avg_marge": str(_q(_d(agg["avg_marge"]))),
            "count_total": agg["count_total"] or 0,
            "count_ok": agg["count_ok"] or 0,
            "count_nok": agg["count_nok"] or 0,
            "taux_ok_pct": _pct(Decimal(agg["count_ok"] or 0), Decimal(agg["count_total"] or 1)),
            "taux_nok_pct": _pct(Decimal(agg["count_nok"] or 0), Decimal(agg["count_total"] or 1)),
            "count_hc": agg["count_hc"] or 0,
            "count_pc": agg["count_pc"] or 0,
            "count_no_load": agg["count_no_load"] or 0,
            "count_no_rule": agg["count_no_rule"] or 0,
            "count_light": agg["count_light"] or 0,
            "count_critique": agg["count_critique"] or 0,
        }

    @staticmethod
    def decomposition_causes(
        year: int,
        month_start: int = 1,
        month_end: int = 12,
    ) -> dict:
        """
        Décompose les marges NOK par facteur explicatif.
        
        Croise FinancialEvaluation avec les données de facturation (ContractMonth)
        pour identifier les causes : cosphi, dépassement puissance, hors catalogue, etc.
        """
        # Base : évaluations NOK sur la période
        qs_nok = FinancialEvaluation.objects.filter(
            year=year,
            marge_statut="NOK",
        )
        if month_start == month_end:
            qs_nok = qs_nok.filter(month=month_start)
        else:
            qs_nok = qs_nok.filter(month__gte=month_start, month__lte=month_end)

        # Récupérer les site_ids et leurs contrats
        site_ids = list(qs_nok.values_list("site_id", flat=True).distinct())
        
        # Mapping site_id → numero_compte_contrat
        site_to_contract = dict(
            ContractSiteLink.objects.filter(site_id__in=site_ids)
            .values_list("site_id", "numero_compte_contrat")
        )

        contract_ids = list(site_to_contract.values())

        # Données de facturation sur la période
        billing_qs = ContractMonth.objects.filter(
            numero_compte_contrat__in=contract_ids,
            year=year,
        )
        if month_start == month_end:
            billing_qs = billing_qs.filter(month=month_start)
        else:
            billing_qs = billing_qs.filter(month__gte=month_start, month__lte=month_end)

        # Agrégats par contrat
        billing_data = {}
        for cm in billing_qs:
            billing_data[cm.numero_compte_contrat] = {
                "montant_cosinus_phi": _d(cm.montant_cosinus_phi),
                "penalite_abonnement": _d(cm.penalite_abonnement_calculee),
                "montant_hors_tva": _d(cm.montant_hors_tva),
                "abonnement": _d(cm.abonnement_calcule),
                "energie": _d(cm.energie_calculee),
            }

        # Compteurs par cause
        causes = {
            "cosphi": {"sites": set(), "montant": D0, "ecart_marge": D0},
            "depassement_puissance": {"sites": set(), "montant": D0, "ecart_marge": D0},
            "hors_catalogue": {"sites": set(), "montant": D0, "ecart_marge": D0},
            "load_manquant": {"sites": set(), "montant": D0, "ecart_marge": D0},
            "regle_manquante": {"sites": set(), "montant": D0, "ecart_marge": D0},
            "autres": {"sites": set(), "montant": D0, "ecart_marge": D0},
        }

        total_ecart = D0

        for ev in qs_nok.select_related("site"):
            ecart = abs(_d(ev.marge)) if ev.marge and _d(ev.marge) < 0 else D0
            total_ecart += ecart

            site_id = ev.site_id
            contract = site_to_contract.get(site_id)
            billing = billing_data.get(contract, {})

            categorized = False

            # 1. Cosphi
            cosphi_montant = billing.get("montant_cosinus_phi", D0)
            if cosphi_montant > D0:
                causes["cosphi"]["sites"].add(site_id)
                causes["cosphi"]["montant"] += cosphi_montant
                causes["cosphi"]["ecart_marge"] += min(ecart, cosphi_montant)
                categorized = True

            # 2. Dépassement puissance
            pen_abo = billing.get("penalite_abonnement", D0)
            if pen_abo > D0:
                causes["depassement_puissance"]["sites"].add(site_id)
                causes["depassement_puissance"]["montant"] += pen_abo
                causes["depassement_puissance"]["ecart_marge"] += min(ecart, pen_abo)
                categorized = True

            # 3. Hors catalogue
            if ev.hors_catalogue:
                causes["hors_catalogue"]["sites"].add(site_id)
                causes["hors_catalogue"]["ecart_marge"] += ecart * Decimal("0.3")  # Estimation 30%
                categorized = True

            # 4. Load manquant
            if ev.load_w is None:
                causes["load_manquant"]["sites"].add(site_id)
                causes["load_manquant"]["ecart_marge"] += ecart
                categorized = True

            # 5. Règle manquante
            if ev.fee_rule is None and ev.load_w is not None:
                causes["regle_manquante"]["sites"].add(site_id)
                causes["regle_manquante"]["ecart_marge"] += ecart
                categorized = True

            # 6. Autres (non catégorisé)
            if not categorized:
                causes["autres"]["sites"].add(site_id)
                causes["autres"]["ecart_marge"] += ecart

        # Formater le résultat
        result = {
            "total_ecart_negatif": str(_q(total_ecart)),
            "causes": {},
        }

        for cause_key, cause_data in causes.items():
            result["causes"][cause_key] = {
                "sites_count": len(cause_data["sites"]),
                "montant_facteur": str(_q(cause_data["montant"])),
                "contribution_ecart": str(_q(cause_data["ecart_marge"])),
                "pct_ecart": _pct(cause_data["ecart_marge"], total_ecart) if total_ecart > 0 else 0,
            }

        return result

    @staticmethod
    def evolution_mensuelle(year: int) -> list[dict]:
        """
        Évolution mois par mois sur une année.
        """
        rows = (
            FinancialEvaluation.objects
            .filter(year=year)
            .values("month")
            .annotate(
                total_redevance=Coalesce(Sum("redevance"), D0),
                total_facture=Coalesce(Sum("montant_htva_reel"), D0),
                total_marge=Coalesce(Sum("marge"), D0),
                count_ok=Count("id", filter=Q(marge_statut="OK")),
                count_nok=Count("id", filter=Q(marge_statut="NOK")),
                count_hc=Count("id", filter=Q(hors_catalogue=True)),
                avg_marge=Coalesce(Avg("marge"), D0),
            )
            .order_by("month")
        )

        return [
            {
                "period": f"{year}-{r['month']:02d}",
                "month": r["month"],
                "total_redevance": str(_q(_d(r["total_redevance"]))),
                "total_facture": str(_q(_d(r["total_facture"]))),
                "total_marge": str(_q(_d(r["total_marge"]))),
                "avg_marge": str(_q(_d(r["avg_marge"]))),
                "count_ok": r["count_ok"],
                "count_nok": r["count_nok"],
                "count_hc": r["count_hc"],
                "taux_nok_pct": _pct(Decimal(r["count_nok"]), Decimal(r["count_ok"] + r["count_nok"])) if (r["count_ok"] + r["count_nok"]) > 0 else 0,
            }
            for r in rows
        ]

    @staticmethod
    def top_sites_nok(
        year: int,
        month_start: int = 1,
        month_end: int = 12,
        limit: int = 20,
    ) -> list[dict]:
        """
        Top sites avec les marges les plus négatives.
        """
        qs = FinancialEvaluation.objects.filter(year=year, marge_statut="NOK")
        if month_start == month_end:
            qs = qs.filter(month=month_start)
        else:
            qs = qs.filter(month__gte=month_start, month__lte=month_end)

        rows = (
            qs.values("site__site_id", "site__name", "site__zone")
            .annotate(
                marge_totale=Coalesce(Sum("marge"), D0),
                marge_moyenne=Coalesce(Avg("marge"), D0),
                nb_mois_nok=Count("id"),
                nb_hors_catalogue=Count("id", filter=Q(hors_catalogue=True)),
            )
            .order_by("marge_totale")[:limit]
        )

        # Enrichir avec données de facturation
        site_ids = [r["site__site_id"] for r in rows]
        site_to_contract = dict(
            ContractSiteLink.objects.filter(site__site_id__in=site_ids)
            .values_list("site__site_id", "numero_compte_contrat")
        )

        # Données cosphi et pénalité
        billing_agg = {}
        contracts = list(site_to_contract.values())
        if contracts:
            billing_qs = ContractMonth.objects.filter(
                numero_compte_contrat__in=contracts,
                year=year,
            )
            if month_start == month_end:
                billing_qs = billing_qs.filter(month=month_start)
            else:
                billing_qs = billing_qs.filter(month__gte=month_start, month__lte=month_end)

            for cm in billing_qs:
                if cm.numero_compte_contrat not in billing_agg:
                    billing_agg[cm.numero_compte_contrat] = {
                        "cosphi": D0, "penalite": D0
                    }
                billing_agg[cm.numero_compte_contrat]["cosphi"] += _d(cm.montant_cosinus_phi)
                billing_agg[cm.numero_compte_contrat]["penalite"] += _d(cm.penalite_abonnement_calculee)

        result = []
        for r in rows:
            site_id = r["site__site_id"]
            contract = site_to_contract.get(site_id)
            billing = billing_agg.get(contract, {})

            result.append({
                "site_id": site_id,
                "site_name": r["site__name"],
                "zone": r["site__zone"],
                "marge_totale": str(_q(_d(r["marge_totale"]))),
                "marge_moyenne": str(_q(_d(r["marge_moyenne"]))),
                "nb_mois_nok": r["nb_mois_nok"],
                "nb_hors_catalogue": r["nb_hors_catalogue"],
                "montant_cosphi": str(_q(billing.get("cosphi", D0))),
                "montant_penalite": str(_q(billing.get("penalite", D0))),
            })

        return result

    @staticmethod
    def impact_facteurs(
        year: int,
        month_start: int = 1,
        month_end: int = 12,
    ) -> dict:
        """
        Impact financier des différents facteurs sur les marges.
        """
        # Récupérer les contrats liés aux évaluations
        evals = FinancialEvaluation.objects.filter(year=year)
        if month_start == month_end:
            evals = evals.filter(month=month_start)
        else:
            evals = evals.filter(month__gte=month_start, month__lte=month_end)

        site_ids = list(evals.values_list("site_id", flat=True).distinct())
        contracts = list(
            ContractSiteLink.objects.filter(site_id__in=site_ids)
            .values_list("numero_compte_contrat", flat=True)
        )

        # Agrégats facturation
        billing_qs = ContractMonth.objects.filter(
            numero_compte_contrat__in=contracts,
            year=year,
        )
        if month_start == month_end:
            billing_qs = billing_qs.filter(month=month_start)
        else:
            billing_qs = billing_qs.filter(month__gte=month_start, month__lte=month_end)

        agg = billing_qs.aggregate(
            total_ht=Coalesce(Sum("montant_hors_tva"), D0),
            total_energie=Coalesce(Sum("energie_calculee"), D0),
            total_abonnement=Coalesce(Sum("abonnement_calcule"), D0),
            total_cosphi=Coalesce(Sum("montant_cosinus_phi"), D0),
            total_penalite=Coalesce(Sum("penalite_abonnement_calculee"), D0),
        )

        total_ht = _d(agg["total_ht"])

        return {
            "total_ht": str(_q(total_ht)),
            "facteurs": [
                {
                    "key": "energie",
                    "label": "Énergie (NRJ)",
                    "montant": str(_q(_d(agg["total_energie"]))),
                    "pct": _pct(_d(agg["total_energie"]), total_ht),
                    "color": "#1e3a8a",
                },
                {
                    "key": "abonnement",
                    "label": "Abonnement",
                    "montant": str(_q(_d(agg["total_abonnement"]))),
                    "pct": _pct(_d(agg["total_abonnement"]), total_ht),
                    "color": "#0891b2",
                },
                {
                    "key": "cosphi",
                    "label": "Pénalité Cosphi",
                    "montant": str(_q(_d(agg["total_cosphi"]))),
                    "pct": _pct(_d(agg["total_cosphi"]), total_ht),
                    "color": "#dc2626",
                },
                {
                    "key": "penalite_puissance",
                    "label": "Pénalité Puissance",
                    "montant": str(_q(_d(agg["total_penalite"]))),
                    "pct": _pct(_d(agg["total_penalite"]), total_ht),
                    "color": "#f59e0b",
                },
            ],
        }

    @staticmethod
    def correlation_certification(
        year: int,
        month: int,
    ) -> dict:
        """
        Croise les résultats de certification avec les marges.
        """
        # Trouver le batch de certification pour ce mois
        cert_batch = CertificationBatch.objects.filter(
            echeance_year=year,
            echeance_month=month,
            status="DONE",
        ).order_by("-finished_at").first()

        if not cert_batch:
            return {
                "available": False,
                "message": f"Aucune certification terminée pour {year}-{month:02d}",
            }

        # Récupérer les résultats de certification
        cert_results = CertificationResult.objects.filter(
            cert_batch=cert_batch
        ).select_related("site")

        # Évaluations financières du mois
        evals = FinancialEvaluation.objects.filter(
            year=year, month=month
        ).select_related("site")

        # Créer un mapping site_id → eval
        eval_map = {ev.site_id: ev for ev in evals}

        # Croiser
        matrix = {
            "CERTIFIED_FMS": {"OK": 0, "NOK": 0, "NONE": 0},
            "CERTIFIED_SENELEC": {"OK": 0, "NOK": 0, "NONE": 0},
            "NEEDS_REVIEW": {"OK": 0, "NOK": 0, "NONE": 0},
            "UNKNOWN_CONTRACT": {"OK": 0, "NOK": 0, "NONE": 0},
            "FMS_UNAVAILABLE": {"OK": 0, "NOK": 0, "NONE": 0},
        }

        double_alerte = []  # NOK + NEEDS_REVIEW

        for cr in cert_results:
            cert_status = cr.status or "UNKNOWN_CONTRACT"
            if cert_status not in matrix:
                continue

            ev = eval_map.get(cr.site_id)
            marge_status = ev.marge_statut if ev else None

            if marge_status == "OK":
                matrix[cert_status]["OK"] += 1
            elif marge_status == "NOK":
                matrix[cert_status]["NOK"] += 1
                if cert_status == "NEEDS_REVIEW":
                    double_alerte.append({
                        "site_id": cr.site.site_id if cr.site else "?",
                        "site_name": cr.site.name if cr.site else "",
                        "marge": str(ev.marge) if ev and ev.marge else "?",
                        "ratio_fms": str(cr.ratio_fms_periode) if cr.ratio_fms_periode else "?",
                    })
            else:
                matrix[cert_status]["NONE"] += 1

        return {
            "available": True,
            "cert_batch_id": cert_batch.id,
            "matrix": matrix,
            "insights": {
                "nok_et_needs_review": len(double_alerte),
                "nok_mais_certified_fms": matrix["CERTIFIED_FMS"]["NOK"],
                "nok_et_fms_unavailable": matrix["FMS_UNAVAILABLE"]["NOK"],
            },
            "double_alerte_sample": double_alerte[:10],
        }

    @staticmethod
    def recommandations(
        year: int,
        month_start: int = 1,
        month_end: int = 12,
    ) -> list[dict]:
        """
        Génère des recommandations basées sur l'analyse.
        """
        decomp = MargeAnalytics.decomposition_causes(year, month_start, month_end)
        summary = MargeAnalytics.summary(year, month_start, month_end)

        recs = []

        causes = decomp.get("causes", {})

        # Cosphi
        cosphi = causes.get("cosphi", {})
        if cosphi.get("sites_count", 0) > 0:
            recs.append({
                "priorite": "HAUTE" if cosphi.get("pct_ecart", 0) > 25 else "MOYENNE",
                "categorie": "COSPHI",
                "titre": f"Optimiser le cos φ sur {cosphi['sites_count']} sites",
                "description": f"Les pénalités cosphi représentent {cosphi['pct_ecart']:.1f}% de l'écart négatif total.",
                "action": "Installer des batteries de condensateurs ou réviser les équipements réactifs.",
                "impact_potentiel": cosphi["contribution_ecart"],
            })

        # Dépassement puissance
        dep = causes.get("depassement_puissance", {})
        if dep.get("sites_count", 0) > 0:
            recs.append({
                "priorite": "HAUTE" if dep.get("pct_ecart", 0) > 20 else "MOYENNE",
                "categorie": "PUISSANCE",
                "titre": f"Réviser la puissance souscrite de {dep['sites_count']} sites",
                "description": f"Les pénalités de dépassement représentent {dep['pct_ecart']:.1f}% de l'écart.",
                "action": "Augmenter la puissance souscrite ou réduire les pics de consommation.",
                "impact_potentiel": dep["contribution_ecart"],
            })

        # Hors catalogue
        hc = causes.get("hors_catalogue", {})
        if hc.get("sites_count", 0) > 10:
            recs.append({
                "priorite": "MOYENNE",
                "categorie": "CATALOGUE",
                "titre": f"Mettre à jour le catalogue pour {hc['sites_count']} sites",
                "description": "Ces sites utilisent un load interpolé car absent du catalogue.",
                "action": "Ajouter les combinaisons typologie/config/load manquantes au catalogue.",
                "impact_potentiel": hc["contribution_ecart"],
            })

        # Load manquant
        load = causes.get("load_manquant", {})
        if load.get("sites_count", 0) > 0:
            recs.append({
                "priorite": "HAUTE",
                "categorie": "DONNEES",
                "titre": f"Compléter les loads de {load['sites_count']} sites",
                "description": "Ces sites n'ont pas de load mensuel → pas de redevance calculable.",
                "action": "Importer les loads depuis le fichier Sénélec ou saisir manuellement.",
                "impact_potentiel": load["contribution_ecart"],
            })

        # Récurrence critique
        critique_count = int(summary.get("count_critique", 0))
        if critique_count > 0:
            recs.append({
                "priorite": "CRITIQUE",
                "categorie": "RECURRENCE",
                "titre": f"{critique_count} sites en récurrence critique (≥6 mois NOK)",
                "description": "Ces sites présentent des marges négatives depuis plus de 6 mois consécutifs.",
                "action": "Audit terrain urgent + renégociation contrat ou résiliation.",
                "impact_potentiel": "—",
            })

        return sorted(recs, key=lambda x: {"CRITIQUE": 0, "HAUTE": 1, "MOYENNE": 2, "BASSE": 3}.get(x["priorite"], 4))