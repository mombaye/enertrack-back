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
_DEFAULT_SEUIL_OK_L = Decimal("100.00")
_DEFAULT_SEUIL_AJ_L = Decimal("200.00")


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
    seuil_ok_l: Decimal = _DEFAULT_SEUIL_OK_L,
    seuil_aj_l: Decimal = _DEFAULT_SEUIL_AJ_L,
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

    abs_l = abs(ecart_l)
    abs_pct = abs(ecart_pct) if ecart_pct is not None else None

    # Seuils effectifs = max(plancher absolu L, % de la conso de référence)
    ref = conso_reference_l if conso_reference_l > Decimal("0") else Decimal("0")
    effective_ok = max(seuil_ok_l, ref * seuil_ok_pct / Decimal("100"))
    effective_aj = max(seuil_aj_l, ref * seuil_aj_pct / Decimal("100"))

    if abs_l <= effective_ok:
        statut = "OK"
        if abs_pct is not None:
            motif = (f"Écart {float(ecart_pct):+.1f}% ({float(ecart_l):+.0f} L) "
                     f"≤ seuil OK (max {float(seuil_ok_l):.0f} L, {float(seuil_ok_pct):.0f}%).")
        else:
            motif = (f"Écart {float(ecart_l):+.0f} L ≤ seuil OK absolu ({float(seuil_ok_l):.0f} L) "
                     f"— conso de référence = 0 L (% non calculable).")
    elif abs_l <= effective_aj:
        statut = "A_JUSTIFIER"
        if abs_pct is not None:
            motif = (f"Écart {float(ecart_pct):+.1f}% ({float(ecart_l):+.0f} L) entre seuil OK "
                     f"(max {float(seuil_ok_l):.0f} L, {float(seuil_ok_pct):.0f}%) "
                     f"et seuil A_JUSTIFIER (max {float(seuil_aj_l):.0f} L, {float(seuil_aj_pct):.0f}%) — justification requise.")
        else:
            motif = (f"Écart {float(ecart_l):+.0f} L dépasse le seuil OK absolu ({float(seuil_ok_l):.0f} L) "
                     f"mais ≤ seuil A_JUSTIFIER ({float(seuil_aj_l):.0f} L) — justification requise.")
    else:
        statut = "A_INVESTIGUER"
        if abs_pct is not None:
            motif = (f"Écart {float(ecart_pct):+.1f}% ({float(ecart_l):+.0f} L) dépasse le seuil d'investigation "
                     f"(max {float(seuil_aj_l):.0f} L, {float(seuil_aj_pct):.0f}%) — analyse approfondie requise.")
        else:
            motif = (f"Écart {float(ecart_l):+.0f} L dépasse le seuil d'investigation absolu ({float(seuil_aj_l):.0f} L) "
                     f"— analyse approfondie requise.")

    if livraisons_source == LIVRAISONS_ENOC_A_CONTROLER:
        motif += " Livraisons ENOC à 0 L — données à contrôler avant de conclure."

    return {**base, "conso_stock_l": conso_stock, "ecart_l": ecart_l, "ecart_pct": ecart_pct,
            "statut": statut, "motif": motif}


def run_rapprochement_for_month(year: int, month: int, site_ids: list[str] | None = None) -> int:
    """
    Calcule le rapprochement stock pour tous les sites du mois `year-month`
    (ou seulement `site_ids`) et met à jour FuelConsommationMonthly en place.
    Retourne le nombre de lignes mises à jour.

    Priorité des snapshots :
      - stock_initial : snapshot mensuel fin-de-mois M-1 (snapshot_year=prev_year, snapshot_month=prev_month)
      - stock_final   : snapshot mensuel fin-de-mois M (snapshot_year=year, snapshot_month=month)
      - Fallback      : snapshot courant (snapshot_year IS NULL) si le mensuel est absent.
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
        seuil_ok_l = thr.seuil_ok_l
        seuil_aj_l = thr.seuil_aj_l
    except FuelRapprochementThreshold.DoesNotExist:
        seuil_ok = _DEFAULT_SEUIL_OK_PCT
        seuil_aj = _DEFAULT_SEUIL_AJ_PCT
        seuil_ok_l = _DEFAULT_SEUIL_OK_L
        seuil_aj_l = _DEFAULT_SEUIL_AJ_L

    prev_year = year if month > 1 else year - 1
    prev_month = month - 1 if month > 1 else 12

    month_year = f"{year:04d}-{month:02d}"
    qs = FuelConsommationMonthly.objects.filter(month_year=month_year)
    if site_ids:
        qs = qs.filter(site_id__in=site_ids)

    site_id_list = list(qs.values_list("site_id", flat=True))

    monthly_initial = {
        s.site_id: s
        for s in FuelStockSnapshot.objects.filter(
            site_id__in=site_id_list,
            snapshot_year=prev_year,
            snapshot_month=prev_month,
        )
    }
    monthly_final = {
        s.site_id: s
        for s in FuelStockSnapshot.objects.filter(
            site_id__in=site_id_list,
            snapshot_year=year,
            snapshot_month=month,
        )
    }
    current_by_site = {
        s.site_id: s
        for s in FuelStockSnapshot.objects.filter(
            site_id__in=site_id_list,
            snapshot_year__isnull=True,
        )
    }

    to_update = []
    for row in qs.select_related().iterator(chunk_size=500):
        initial_snap = monthly_initial.get(row.site_id) or current_by_site.get(row.site_id)
        final_snap = monthly_final.get(row.site_id) or current_by_site.get(row.site_id)
        stock_initial_l = initial_snap.stock_snowflake_l if (initial_snap and initial_snap.stock_snowflake_l is not None) else None
        stock_final_l = final_snap.stock_snowflake_l if (final_snap and final_snap.stock_snowflake_l is not None) else None

        livraisons = Decimal(str(row.enoc_qte_ajoutee_l or 0))
        conso_ref = (
            row.conso_snowflake_l
            if row.conso_snowflake_l is not None
            else row.conso_estimee_cph_l
        )
        if conso_ref is not None:
            conso_ref = Decimal(str(conso_ref))

        result = compute_rapprochement(
            stock_initial_l=stock_initial_l,
            stock_final_l=stock_final_l,
            livraisons_l=livraisons,
            conso_reference_l=conso_ref,
            seuil_ok_pct=seuil_ok,
            seuil_aj_pct=seuil_aj,
            seuil_ok_l=seuil_ok_l,
            seuil_aj_l=seuil_aj_l,
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
