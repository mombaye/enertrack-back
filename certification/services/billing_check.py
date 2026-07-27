# certification/services/billing_check.py
# Étape 8 — Recalcul du montant HTVA d'une facture Senelec
# et comparaison avec le montant facturé (tolérance 7 %)
#
# HISTORIQUE DES CORRECTIONS :
#   v2 [BUG 1] Prime Fixe : ajout du multiplicateur PS
#              prime_fixe = prime_fixe_mensuelle * ps * nb_j / 30
#
#   v2 [BUG 2] Cosphi guard : valeur 0.0 importée → pas de pénalité
#              if cp <= D0: return None
#
#   v3 [BUG 3] Pénalité Prime Fixe : suppression du doublon PS
#              AVANT (v2, bug) : 1.5 * taux * ps * delta_p * nb_j / 30
#              APRÈS (v3, fix) : 1.5 * taux * delta_p * nb_j / 30
#              prime_fixe_mensuelle est en FCFA/kVA/mois, delta_p en kVA.
#              ps ne doit PAS être remultiplié.

from __future__ import annotations

import logging
import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Optional

logger = logging.getLogger(__name__)

D0 = Decimal("0")
SEUIL_COHERENCE = Decimal("7")  # % max d'écart admis

# ── Polices BT (soumises à TCO 2,5 %) ──────────────────────────────────────
BT_POLICES = {"PGP", "PFP", "PMP", "DGP", "DPP", "PPP", "DMP"}

# ── Polices avec tranches LCTR ──────────────────────────────────────────────
LCTR_POLICES = {"DPP", "PMP", "PPP", "DMP"}

# ── Tranches LCTR (kWh) : (LCTR1, LCTR2) ───────────────────────────────────
_LCTR: dict[str, tuple[Decimal, Decimal]] = {
    "DPP": (Decimal("150"), Decimal("100")),
    "DMP": (Decimal("50"),  Decimal("250")),
    "PPP": (Decimal("50"),  Decimal("450")),
    "PMP": (Decimal("100"), Decimal("400")),
}

# ── Redevance mensuelle (FCFA / 30 j) ───────────────────────────────────────
_REDEVANCE: dict[str, Decimal] = {
    "DPP":  Decimal("944"),
    "DMP":  Decimal("3092"),
    "PPP":  Decimal("2631"),
    "PMP":  Decimal("2949"),
    "MTG":  Decimal("15850"),
    "MTLU": Decimal("15850"),
    "MTCU": Decimal("15850"),
    "PGP":  Decimal("15850"),
    "PFP":  Decimal("15850"),
    "DGP":  Decimal("15850"),
}

# ── Table Majoration (act1, act2) indexée par puissance transfo (kVA) ───────
_MAJORATION: dict[int, tuple[Decimal, Decimal]] = {
    25:  (Decimal("0.028"),   Decimal("0.115")),
    50:  (Decimal("0.022"),   Decimal("0.19")),
    75:  (Decimal("0.0198"),  Decimal("0.255")),
    100: (Decimal("0.0175"),  Decimal("0.32")),
    125: (Decimal("0.0163"),  Decimal("0.3783")),
    160: (Decimal("0.0147"),  Decimal("0.46")),
    200: (Decimal("0.0143"),  Decimal("0.55")),
    225: (Decimal("0.0137"),  Decimal("0.6")),
    250: (Decimal("0.013"),   Decimal("0.65")),
    275: (Decimal("0.0557"),  Decimal("0.6962")),
    300: (Decimal("0.0984"),  Decimal("0.7423")),
    315: (Decimal("0.124"),   Decimal("0.77")),
    350: (Decimal("0.0777"),  Decimal("0.8359")),
    375: (Decimal("0.0446"),  Decimal("0.8829")),
    400: (Decimal("0.0115"),  Decimal("0.93")),
    425: (Decimal("0.0114"),  Decimal("0.9725")),
    450: (Decimal("0.0113"),  Decimal("1.015")),
    475: (Decimal("0.0111"),  Decimal("1.0575")),
    500: (Decimal("0.0109"),  Decimal("1.1")),
    525: (Decimal("0.0109"),  Decimal("1.1385")),
    550: (Decimal("0.0107"),  Decimal("1.1769")),
    575: (Decimal("0.0106"),  Decimal("1.2154")),
    600: (Decimal("0.0105"),  Decimal("1.2538")),
    625: (Decimal("0.0103"),  Decimal("1.2923")),
    630: (Decimal("0.0103"),  Decimal("1.33")),
    650: (Decimal("0.0102"),  Decimal("1.3308")),
}

# ── Table Cosphi ─────────────────────────────────────────────────────────────
_COSPHI_TAUX: list[tuple[Decimal, Decimal, Decimal]] = [
    (Decimal("1.00"),  Decimal("1.00"),  Decimal("-4")),
    (Decimal("0.98"),  Decimal("0.98"),  Decimal("-2")),
    (Decimal("0.97"),  Decimal("0.97"),  Decimal("-2")),
    (Decimal("0.96"),  Decimal("0.96"),  Decimal("-1")),
    # 0.95 → 0 (zone neutre)
    (Decimal("0.75"),  Decimal("0.79"),  Decimal("5")),
    (Decimal("0.65"),  Decimal("0.69"),  Decimal("15")),
    (Decimal("0.60"),  Decimal("0.64"),  Decimal("20")),
    (Decimal("0.55"),  Decimal("0.59"),  Decimal("30")),
    (Decimal("0.50"),  Decimal("0.54"),  Decimal("40")),
    (Decimal("0.45"),  Decimal("0.49"),  Decimal("50")),
    (Decimal("0.40"),  Decimal("0.44"),  Decimal("65")),
    (Decimal("0.00"),  Decimal("0.39"),  Decimal("80")),
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _d(x) -> Decimal:
    if x is None:
        return D0
    try:
        return Decimal(str(x))
    except InvalidOperation:
        return D0


def _q(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _normalize_police(raw: str | None) -> str | None:
    """Extrait le code police (ex: 'MT (DGP)' → 'DGP')."""
    if not raw:
        return None
    s = str(raw).strip().upper()
    m = re.search(r"\(([^)]+)\)", s)
    if m:
        return m.group(1).strip().upper()
    return s


def _get_majoration(puissance_kva) -> Optional[tuple[Decimal, Decimal]]:
    """Retourne (ACT1, ACT2) pour le palier ≤ puissance_kva."""
    if puissance_kva is None:
        return None
    try:
        kva = float(puissance_kva)
    except (TypeError, ValueError):
        return None
    best_key = None
    for k in sorted(_MAJORATION.keys()):
        if k <= kva:
            best_key = k
    if best_key is None:
        best_key = min(_MAJORATION.keys())
    return _MAJORATION[best_key]


def _get_cosphi_taux(cosphi_val: Decimal) -> Optional[Decimal]:
    """
    Retourne le taux cosphi en %.
    Retourne None si :
      - cosphi_val <= 0  [BUG 2 FIX] : absent ou non renseigné → 0 FCFA
      - 0.80 ≤ φ ≤ 0.95 : zone neutre
    """
    cp = cosphi_val
    if cp <= D0:                              # [BUG 2 FIX]
        return None
    if Decimal("0.80") <= cp <= Decimal("0.95"):
        return None
    for lo, hi, taux in _COSPHI_TAUX:
        if lo <= cp <= hi:
            return taux
    return None


# ── Calcul principal ─────────────────────────────────────────────────────────

def compute_montant_htva(invoice) -> tuple[Optional[Decimal], Optional[str]]:
    """
    Recalcule le montant HTVA d'une facture Senelec selon la logique Aktivko.

    Returns
    -------
    (montant_htva_calcule, error_msg)
    """
    try:
        # ── 0. Police ────────────────────────────────────────────────────────
        police = _normalize_police(invoice.type_de_tarif)
        if not police:
            return None, "type_de_tarif manquant ou non reconnu"

        # ── 1. Date de référence & nb jours ─────────────────────────────────
        ref_date = invoice.date_debut_periode or invoice.date_comptable_facture
        if not ref_date:
            return None, "Date de référence manquante (date_debut ou date_comptable)"

        nb_jours = invoice.nb_jour_facturation
        if not nb_jours or nb_jours <= 0:
            if invoice.date_debut_periode and invoice.date_fin_periode:
                nb_jours = (invoice.date_fin_periode - invoice.date_debut_periode).days + 1
            else:
                return None, "nb_jour_facturation manquant et période incomplète"

        nb_j = Decimal(str(nb_jours))

        # ── 2. Récupération des tarifs ───────────────────────────────────────
        from billing.models import TariffRate
        tr = (
            TariffRate.objects
            .filter(category__iexact=police, date_debut__lte=ref_date, date_fin__gte=ref_date)
            .order_by("-date_debut")
            .first()
        )
        if not tr:
            logger.warning(
                "[billing_check] TariffRate introuvable police=%r à %s → tarifs à 0",
                police, ref_date,
            )
            tarif_k1             = D0
            tarif_k2             = D0
            prime_fixe_mensuelle = D0
        else:
            tarif_k1             = _d(tr.energie_k1)
            tarif_k2             = _d(tr.energie_k2)
            prime_fixe_mensuelle = _d(tr.prime_fixe)

        # ── 3. Puissance souscrite & consommée ───────────────────────────────
        ps   = _d(invoice.puissance_souscrite)
        pmax = _d(invoice.puissance_max_relevee)

        # ── 4. Consommations K1 / K2 ─────────────────────────────────────────
        conso_k1       = max(D0, _d(invoice.nouvel_index_k1) - _d(invoice.ancien_index_k1))
        conso_k2       = max(D0, _d(invoice.nouvel_index_k2) - _d(invoice.ancien_index_k2))
        rappel_k1      = _d(invoice.rappel_k1)
        rappel_k2      = _d(invoice.rappel_k2)
        conso_facturee = _d(invoice.conso_facturee)

        # ── 5. Tranches LCTR ─────────────────────────────────────────────────
        conso_lctr1 = D0
        conso_lctr2 = D0
        conso_lctr3 = D0
        if police in LCTR_POLICES and police in _LCTR:
            t1, t2 = _LCTR[police]
            conso_lctr1 = min(conso_facturee, t1)
            conso_lctr2 = min(max(D0, conso_facturee - conso_lctr1), t2)
            conso_lctr3 = max(D0, conso_facturee - conso_lctr1 - conso_lctr2)

        # ── 6. Majoration réactive ────────────────────────────────────────────
        ma_k1 = D0
        ma_k2 = D0
        majo_params = _get_majoration(invoice.puissance_transfo)
        if majo_params is not None:
            act1, act2 = majo_params
            wa = conso_k1 + conso_k2
            h1 = _d(invoice.majo_reactif)
            if wa > D0:
                ma    = act1 * wa + act2 * h1
                ma_k1 = ma * conso_k1 / wa
                ma_k2 = ma - ma_k1

        # ── 7. Montants énergie ───────────────────────────────────────────────
        if tr and getattr(tr, 'energie_k3', None):
            tarif_k3 = _d(tr.energie_k3)
        else:
            tarif_k3 = tarif_k2

        montant_k1 = (conso_k1 + ma_k1 + conso_lctr1 + rappel_k1) * tarif_k1
        montant_k2 = (conso_k2 + ma_k2 + conso_lctr2 + rappel_k2) * tarif_k2
        montant_k3 = conso_lctr3 * tarif_k3

        # ── 8. Prime Fixe ─────────────────────────────────────────────────────
        # [BUG 1 FIX] Taux en FCFA/kVA/mois → multiplier par PS (kVA)
        prime_fixe = prime_fixe_mensuelle * ps * nb_j / Decimal("30")

        # ── 9. Pénalité Prime Fixe (dépassement puissance souscrite) ─────────
        # [BUG 3 FIX] Formule CDC : 1.5 × Taux_PF × delta_p × NbJ / 30
        # delta_p = dépassement en kVA = pmax - ps (si positif)
        # NE PAS multiplier par ps : ce serait compter la puissance deux fois.
        delta_p     = max(D0, pmax - ps)
        penalite_pf = Decimal("1.5") * prime_fixe_mensuelle * delta_p * nb_j / Decimal("30")

        # ── 10. Montant NRJ ───────────────────────────────────────────────────
        montant_nrj = montant_k1 + montant_k2 + montant_k3 + prime_fixe + penalite_pf

        # ── 11. Montant Cosphi ────────────────────────────────────────────────
        # [BUG 2 FIX] Guard cosphi <= 0 dans _get_cosphi_taux
        montant_cosphi = D0
        cosphi_val = invoice.valeur_cosinus_phi
        if cosphi_val is not None:
            taux = _get_cosphi_taux(_d(cosphi_val))
            if taux is not None:
                montant_cosphi = montant_nrj * taux / Decimal("100")

        # ── 12. TCO (polices BT — 2,5 %) ─────────────────────────────────────
        montant_tco = D0
        if police in BT_POLICES:
            montant_tco = Decimal("0.025") * (montant_nrj + montant_cosphi)

        # ── 13. Redevance ─────────────────────────────────────────────────────
        montant_redevance = D0
        if police in _REDEVANCE:
            montant_redevance = _REDEVANCE[police] / Decimal("30") * nb_j

        # ── 14. Montant HTVA Aktivko ──────────────────────────────────────────
        montant_htva_akt = montant_nrj + montant_cosphi + montant_tco + montant_redevance

        logger.debug(
            "[billing_check] %s | police=%s | ps=%.1f | nrj=%.2f "
            "cosphi=%.2f tco=%.2f redev=%.2f → htva=%.2f",
            getattr(invoice, "numero_facture", "?"), police, float(ps),
            montant_nrj, montant_cosphi, montant_tco,
            montant_redevance, montant_htva_akt,
        )

        return _q(montant_htva_akt), None

    except Exception as exc:
        logger.warning(
            "[billing_check] Erreur calcul facture %s : %s",
            getattr(invoice, "numero_facture", "?"), exc,
        )
        return None, f"Erreur calcul: {exc}"


def check_montant_coherence(
    invoice,
) -> tuple[Optional[Decimal], Optional[Decimal], Optional[bool], Optional[str]]:
    """
    Calcule et compare le montant HTVA recalculé vs facturé.

    Returns
    -------
    (montant_htva_calcule, variation_pct, coherent, error_msg)
      • variation_pct = (HTVA_facture / HTVA_akt − 1) × 100
      • coherent      = True si |variation_pct| ≤ 7 %, False sinon
    """
    montant_akt, err = compute_montant_htva(invoice)

    if montant_akt is None:
        return None, None, None, err

    htva_facture = _d(invoice.montant_hors_tva)
    if htva_facture == D0:
        return montant_akt, None, None, "montant_hors_tva = 0 ou manquant dans la facture"

    variation = (htva_facture / montant_akt - Decimal("1")) * Decimal("100")
    variation = _q(variation)
    coherent  = abs(variation) <= SEUIL_COHERENCE

    return montant_akt, variation, coherent, None