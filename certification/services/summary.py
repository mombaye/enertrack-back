"""Résumé d'une campagne de certification, affiché à la fin du calcul (et à la demande).

Montants : somme des TTC facturés par Senelec (invoice.montant_ttc). Une facture sans montant n'est pas comptée
comme 0 : elle est dénombrée à part (« montant absent »).
"""
from django.db.models import Count, F, Q, Sum

from certification.models import CertificationBatch, CertificationResult

S = CertificationResult.Status
CERTIFIED = (S.CERTIFIED_FMS, S.CERTIFIED_SENELEC)
TO_REVIEW = (S.NEEDS_REVIEW, S.MESURE_A_VERIFIER)
TOP_N = 10


def _num(v):
    return float(v) if v is not None else None


def _pct(part, total):
    return round(100.0 * part / total, 1) if total else None


def _row(r: CertificationResult) -> dict:
    inv = r.invoice
    return {
        "result_id": r.id,
        "numero_facture": inv.numero_facture,
        "numero_compte_contrat": inv.numero_compte_contrat,
        "site_id": r.site.site_id if r.site_id else None,
        "site_name": r.site.name if r.site_id else None,
        "status": r.status,
        "montant_ttc": _num(inv.montant_ttc),
        "variation_montant_pct": _num(r.variation_montant_pct),
        "ratio_fms_periode": _num(r.ratio_fms_periode),
        "flag_mesure_alert": r.flag_mesure_alert,
    }


def build_summary(cert_batch: CertificationBatch) -> dict:
    qs = CertificationResult.objects.filter(cert_batch=cert_batch)
    total = qs.count()

    by_status = {s.value: {"count": 0, "montant_ttc": 0.0} for s in S}
    for r in qs.values("status").annotate(n=Count("id"), ttc=Sum("invoice__montant_ttc")):
        by_status[r["status"]] = {"count": r["n"], "montant_ttc": _num(r["ttc"]) or 0.0}

    def group(statuses):
        n = sum(by_status[s]["count"] for s in statuses)
        return {"count": n, "pct": _pct(n, total), "montant_ttc": sum(by_status[s]["montant_ttc"] for s in statuses)}

    agg = qs.aggregate(
        ttc=Sum("invoice__montant_ttc"),
        sans_montant=Count("id", filter=Q(invoice__montant_ttc__isnull=True)),
        sites=Count("site", distinct=True),
        fms=Count("id", filter=Q(fms_available=True)),
        acm=Count("id", filter=Q(acm_available=True)),
        montant_incoherent=Count("id", filter=Q(montant_coherent=False)),
        montant_non_verifie=Count("id", filter=Q(montant_coherent__isnull=True)),
    )
    rules = {
        r["certified_by_rule"]: r["n"]
        for r in qs.filter(status__in=CERTIFIED).exclude(certified_by_rule__isnull=True)
                  .values("certified_by_rule").annotate(n=Count("id"))
    }

    to_review = (qs.filter(status__in=TO_REVIEW).select_related("invoice", "site")
                 .order_by(F("invoice__montant_ttc").desc(nulls_last=True), "id")[:TOP_N])
    unknown = list(qs.filter(status=S.UNKNOWN_CONTRACT).values_list("invoice__numero_compte_contrat", flat=True)
                   .distinct().order_by("invoice__numero_compte_contrat")[:TOP_N])

    duration_s = None
    if cert_batch.finished_at and cert_batch.launched_at:
        duration_s = int((cert_batch.finished_at - cert_batch.launched_at).total_seconds())

    return {
        "cert_batch_id": cert_batch.id,
        "echeance": cert_batch.echeance_label,
        "status": cert_batch.status,
        "launched_at": cert_batch.launched_at,
        "finished_at": cert_batch.finished_at,
        "duration_s": duration_s,
        "total": total,
        "montant_ttc_total": _num(agg["ttc"]) or 0.0,
        "factures_sans_montant": agg["sans_montant"],
        "groups": {
            "certified": group(CERTIFIED),
            "to_review": group(TO_REVIEW),
            "unknown_contract": group((S.UNKNOWN_CONTRACT,)),
            "fms_unavailable": group((S.FMS_UNAVAILABLE,)),
            "pending": group((S.PENDING_CERTIFICATION,)),
        },
        "by_status": by_status,
        "certified_by_rule": rules,
        "coverage": {
            "sites_rattaches": agg["sites"],
            "factures_avec_site": total - by_status[S.UNKNOWN_CONTRACT]["count"],
            "factures_avec_fms": agg["fms"],
            "factures_avec_acm": agg["acm"],
        },
        "montant_check": {
            "incoherent": agg["montant_incoherent"],
            "non_verifie": agg["montant_non_verifie"],
        },
        "top_to_review": [_row(r) for r in to_review],
        "unknown_contracts_sample": unknown,
        "unknown_contracts_total": qs.filter(status=S.UNKNOWN_CONTRACT)
                                     .values("invoice__numero_compte_contrat").distinct().count(),
    }
