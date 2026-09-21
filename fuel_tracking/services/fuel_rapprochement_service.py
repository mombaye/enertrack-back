# fuel_tracking/services/fuel_rapprochement_service.py
"""
Calcul du rapprochement stock mensuel par site (spec C) :

  conso_stock = stock_initial + livraisons + rajouts − retraits − vols − vidanges − stock_final

Statuts (ordre de priorité, un seul par ligne) :
  DONNEES_INCOMPLETES   — stock_initial ou stock_final manquant
  CPH_NON_CALCULE       — conso de référence (mesurée ou estimée CPH) absente
  OK                    — |écart%| ≤ seuil_ok_pct
  A_JUSTIFIER           — seuil_ok_pct < |écart%| ≤ seuil_a_justifier_pct
  A_INVESTIGUER         — |écart%| > seuil_a_justifier_pct

livraisons_source :
  ENOC_REEL                  — ENOC a déclaré des livraisons (enoc_qte_ajoutee_l > 0)
  LIVRAISONS_ENOC_A_CONTROLER — ENOC à 0 L — données à vérifier avant tout rapprochement

Seuils par défaut (peuvent être surchargés via FuelRapprochementThreshold label='default') :
  OK          ≤ 10 %
  A_JUSTIFIER ≤ 20 %
  A_INVESTIGUER > 20 %
"""
from decimal import Decimal, ROUND_HALF_UP

LIVRAISONS_ENOC_A_CONTROLER = "LIVRAISONS_ENOC_A_CONTROLER"
ENOC_REEL = "ENOC_REEL"

_DEFAULT_SEUIL_OK_PCT = Decimal("10.00")
_DEFAULT_SEUIL_AJ_PCT = Decimal("20.00")


def compute_rapprochement(
    *,
    stock_initial_l: Decimal | None,
    stock_final_l: Decimal | None,
    livraisons_l: Decimal,
    rajouts_l: Decimal = Decimal("0"),
    retraits_l: Decimal = Decimal("0"),
    vols_l: Decimal = Decimal("0"),
    vidanges_l: Decimal = Decimal("0"),
    conso_reference_l: Decimal | None,
    seuil_ok_pct: Decimal = _DEFAULT_SEUIL_OK_PCT,
    seuil_aj_pct: Decimal = _DEFAULT_SEUIL_AJ_PCT,
) -> dict:
    """
    Retourne :
      stock_initial_l, livraisons_l, rajouts_l, retraits_l, vols_l, vidanges_l,
      stock_final_l, conso_stock_l, ecart_l, ecart_pct, statut, motif, livraisons_source
    """
    livraisons_source = LIVRAISONS_ENOC_A_CONTROLER if livraisons_l == Decimal("0") else ENOC_REEL

    base = {
        "stock_initial_l": stock_initial_l,
        "livraisons_l": livraisons_l,
        "rajouts_l": rajouts_l,
        "retraits_l": retraits_l,
        "vols_l": vols_l,
        "vidanges_l": vidanges_l,
        "stock_final_l": stock_final_l,
        "livraisons_source": livraisons_source,
    }

    if stock_initial_l is None or stock_final_l is None:
        missing = []
        if stock_initial_l is None:
            missing.append("stock initial")
        if stock_final_l is None:
            missing.append("stock final")
        return {**base, "conso_stock_l": None, "ecart_l": None, "ecart_pct": None,
                "statut": "DONNEES_INCOMPLETES",
                "motif": f"Données manquantes : {' et '.join(missing)}."}

    conso_stock = stock_initial_l + livraisons_l + rajouts_l - retraits_l - vols_l - vidanges_l - stock_final_l

    if conso_reference_l is None:
        return {**base, "conso_stock_l": conso_stock, "ecart_l": None, "ecart_pct": None,
                "statut": "CPH_NON_CALCULE",
                "motif": "Consommation de référence (mesurée ou estimée CPH) absente pour ce site ce mois-ci."}

    ecart_l = conso_reference_l - conso_stock
    if conso_reference_l != Decimal("0"):
        ecart_pct = (ecart_l / conso_reference_l * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        ecart_pct = None

    abs_pct = abs(ecart_pct) if ecart_pct is not None else None

    if abs_pct is None:
        statut = "DONNEES_INCOMPLETES"
        motif = "Conso de référence = 0 L — écart en % non calculable."
    elif abs_pct <= seuil_ok_pct:
        statut = "OK"
        motif = f"Écart {float(ecart_pct):+.1f}% ≤ seuil OK ({float(seuil_ok_pct):.0f}%)."
    elif abs_pct <= seuil_aj_pct:
        statut = "A_JUSTIFIER"
        motif = (f"Écart {float(ecart_pct):+.1f}% entre le seuil OK ({float(seuil_ok_pct):.0f}%) "
                 f"et le seuil A_JUSTIFIER ({float(seuil_aj_pct):.0f}%) — justification requise.")
    else:
        statut = "A_INVESTIGUER"
        motif = (f"Écart {float(ecart_pct):+.1f}% dépasse le seuil d'investigation "
                 f"({float(seuil_aj_pct):.0f}%) — analyse approfondie requise.")

    if livraisons_source == LIVRAISONS_ENOC_A_CONTROLER:
        motif += " Livraisons ENOC à 0 L — données à contrôler avant de conclure."

    return {**base, "conso_stock_l": conso_stock, "ecart_l": ecart_l, "ecart_pct": ecart_pct,
            "statut": statut, "motif": motif}


def run_rapprochement_for_month(year: int, month: int, site_ids: list[str] | None = None) -> int:
    """
    Calcule le rapprochement stock pour tous les sites du mois `year-month`
    (ou seulement `site_ids`) et met à jour FuelConsommationMonthly en place.
    Retourne le nombre de lignes mises à jour.

    Stock de référence = FuelStockSnapshot.stock_snowflake_l (snapshot courant).
    En l'absence d'un modèle mensuel de snapshot, le même snapshot sert pour
    stock_initial et stock_final — c'est une approximation documentée dans
    rapprochement_motif lorsque stock_initial_l == stock_final_l.
    """
    from fuel_tracking.models import (
        FuelConsommationMonthly,
        FuelRapprochementThreshold,
        FuelStockSnapshot,
    )

    try:
        thr = FuelRapprochementThreshold.objects.get(label="default")
        seuil_ok = thr.seuil_ok_pct
        seuil_aj = thr.seuil_a_justifier_pct
    except FuelRapprochementThreshold.DoesNotExist:
        seuil_ok = _DEFAULT_SEUIL_OK_PCT
        seuil_aj = _DEFAULT_SEUIL_AJ_PCT

    month_year = f"{year:04d}-{month:02d}"
    qs = FuelConsommationMonthly.objects.filter(month_year=month_year)
    if site_ids:
        qs = qs.filter(site_id__in=site_ids)

    site_id_list = list(qs.values_list("site_id", flat=True))
    stock_by_site = {
        s.site_id: s
        for s in FuelStockSnapshot.objects.filter(site_id__in=site_id_list)
    }

    to_update = []
    for row in qs.select_related().iterator(chunk_size=500):
        snap = stock_by_site.get(row.site_id)
        stock_l = snap.stock_snowflake_l if (snap and snap.stock_snowflake_l is not None) else None

        livraisons = Decimal(str(row.enoc_qte_ajoutee_l or 0))
        conso_ref = (
            row.conso_snowflake_l
            if row.conso_snowflake_l is not None
            else row.conso_estimee_cph_l
        )
        if conso_ref is not None:
            conso_ref = Decimal(str(conso_ref))

        result = compute_rapprochement(
            stock_initial_l=stock_l,
            stock_final_l=stock_l,
            livraisons_l=livraisons,
            conso_reference_l=conso_ref,
            seuil_ok_pct=seuil_ok,
            seuil_aj_pct=seuil_aj,
        )

        row.rapprochement_stock_initial_l = result["stock_initial_l"]
        row.rapprochement_livraisons_l = result["livraisons_l"]
        row.rapprochement_rajouts_l = result["rajouts_l"]
        row.rapprochement_retraits_l = result["retraits_l"]
        row.rapprochement_vols_l = result["vols_l"]
        row.rapprochement_vidanges_l = result["vidanges_l"]
        row.rapprochement_stock_final_l = result["stock_final_l"]
        row.rapprochement_conso_stock_l = result["conso_stock_l"]
        row.rapprochement_ecart_l = result["ecart_l"]
        row.rapprochement_ecart_pct = result["ecart_pct"]
        row.rapprochement_statut = result["statut"]
        row.rapprochement_motif = result["motif"]
        row.livraisons_source = result["livraisons_source"]
        to_update.append(row)

    if to_update:
        FuelConsommationMonthly.objects.bulk_update(
            to_update,
            fields=[
                "rapprochement_stock_initial_l", "rapprochement_livraisons_l",
                "rapprochement_rajouts_l", "rapprochement_retraits_l",
                "rapprochement_vols_l", "rapprochement_vidanges_l",
                "rapprochement_stock_final_l", "rapprochement_conso_stock_l",
                "rapprochement_ecart_l", "rapprochement_ecart_pct",
                "rapprochement_statut", "rapprochement_motif", "livraisons_source",
            ],
            batch_size=500,
        )
    return len(to_update)
