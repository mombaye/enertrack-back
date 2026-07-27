# optimization/services.py

from datetime import date, datetime
from django.db.models import Max, Q
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, List
import re

from billing.models import SonatelInvoice, ContractSiteLink, TariffRate

from django.utils import timezone
from django.db import transaction
from decimal import Decimal

from .models import OptimizationBatch, OptimizationResult






D0 = Decimal("0")
Q3 = Decimal("0.001")


MT_TARIFFS = {"MTLU", "MTG", "MTCU"}
BT_TARIFFS = {"PGP", "PFP", "PMP", "DGP", "DPP", "PPP", "DMP"}


def _q3(value):
    if value is None:
        return None
    return Decimal(value).quantize(Q3, rounding=ROUND_HALF_UP)


def _d(value) -> Decimal:
    if value is None:
        return D0
    try:
        return Decimal(str(value))
    except Exception:
        return D0


def _is_positive(value) -> bool:
    return value is not None and _d(value) > D0


def _one_year_before(d: date) -> date:
    """
    Évite l'erreur du 29 février.
    Exemple : 2024-02-29 -> 2023-02-28
    """
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def _days_inclusive(start: date, end: date) -> int:
    return (end - start).days + 1


def normalize_tariff_category(raw: Optional[str]) -> Optional[str]:
    """
    Exemples :
    - 'Basse tension (DGP)' -> DGP
    - 'DGP' -> DGP
    - ' mtlu ' -> MTLU
    """
    if not raw:
        return None

    s = str(raw).strip().upper()

    match = re.search(r"\(([^)]+)\)", s)
    if match:
        return match.group(1).strip().upper()

    return s or None


def get_tariff_family(tariff: Optional[str]) -> str:
    tariff = normalize_tariff_category(tariff)

    if tariff in MT_TARIFFS:
        return "MT"

    if tariff in BT_TARIFFS:
        return "BT"

    return "UNKNOWN"


def get_ps_min_for_family(family: str) -> Optional[Decimal]:
    if family == "MT":
        return Decimal("34")

    if family == "BT":
        return Decimal("17")

    return None


def _get_site_link(numero_compte_contrat: str):
    return (
        ContractSiteLink.objects
        .select_related("site")
        .filter(numero_compte_contrat=numero_compte_contrat)
        .first()
    )


def _is_eligible_link(link) -> bool:
    if not link or not link.site:
        return False

    invoice_payment = (link.site.invoice_payment or "").strip().lower()

    return invoice_payment == "aktivco" and link.site.grid_fee is True



def _normalize_contract_number(value):
    if value is None:
        return None

    s = str(value).strip().replace("\u00a0", "").replace(" ", "")

    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]

    return s or None


def _is_valid_contract_number(value):
    """
    Exclut les valeurs type RAS, Prepaid, Pas de contrat, Fournisseurs Tiers.
    Un vrai contrat doit être numérique.
    """
    s = _normalize_contract_number(value)

    if not s:
        return False

    return s.isdigit() and len(s) >= 8


def _json_safe(value):
    """
    Convertit les objets non JSON-safe avant sauvegarde dans JSONField.
    """
    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, (date, datetime)):
        return value.isoformat()

    if isinstance(value, list):
        return [_json_safe(v) for v in value]

    if isinstance(value, tuple):
        return [_json_safe(v) for v in value]

    if isinstance(value, dict):
        cleaned = {}

        for k, v in value.items():
            if k == "site":
                site = value.get("site")
                cleaned["site_snapshot"] = {
                    "id": getattr(site, "id", None),
                    "site_id": getattr(site, "site_id", None),
                    "name": getattr(site, "name", None),
                } if site else None
                continue

            cleaned[k] = _json_safe(v)

        return cleaned

    if hasattr(value, "_meta") and hasattr(value, "pk"):
        return str(value.pk)

    return value


def _clean_base_for_simulation(base):
    return _json_safe(base or {})


def _clean_base_for_simulation(base):
    return _json_safe(base or {})

def _safe_decimal(value):
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _parse_iso_date(value):
    if not value:
        return None

    if isinstance(value, date):
        return value

    try:
        return date.fromisoformat(str(value))
    except Exception:
        return None


def _pick_tariff_rate(category, ref_date):
    """
    Récupère le tarif applicable à une date donnée.
    """
    category = normalize_tariff_category(category)

    if not category or not ref_date:
        return None

    return (
        TariffRate.objects
        .filter(
            category__iexact=category,
            date_debut__lte=ref_date,
            date_fin__gte=ref_date,
        )
        .order_by("-date_debut")
        .first()
    )


def _tariffs_for_family(family):
    if family == "MT":
        return sorted(MT_TARIFFS)

    if family == "BT":
        return sorted(BT_TARIFFS)

    return []


def _line_decimal(detail, key):
    return _safe_decimal(detail.get(key))


def _apply_gain_to_reference(reference_amount, gain):
    """
    Applique un gain calculé par simulation sur la facture réelle de référence.

    Pourquoi :
    - la facture_reference vient des montants réels importés ;
    - la simulation sert surtout à calculer l'écart entre scénario actuel et scénario optimisé.
    """
    reference_amount = _safe_decimal(reference_amount)
    gain = _safe_decimal(gain)

    optimized = reference_amount - gain

    if optimized < D0:
        optimized = D0

    return _q3(optimized)


def compute_best_exclusive_optimization_from_base(base, power, tariff):
    """
    Choisit une seule optimisation applicable :
    - POWER si le gain puissance est meilleur ;
    - TARIFF si le gain tarif est meilleur ;
    - NONE si aucun gain.

    Important :
    On ne cumule jamais PS + Tarif.
    """

    facture_reference = _safe_decimal(base.get("facture_reference"))

    gain_power = _safe_decimal(power.get("gain_power"))
    gain_tariff = _safe_decimal(tariff.get("gain_tariff"))

    facture_power = _safe_decimal(power.get("facture_power_optimized"))
    facture_tariff = _safe_decimal(tariff.get("facture_tariff_optimized"))

    ps_current = _safe_decimal(base.get("ps_current"))
    tariff_current = normalize_tariff_category(base.get("tariff_current"))

    if gain_power <= 0 and gain_tariff <= 0:
        return {
            "facture_reference": _q3(facture_reference),
            "best_optimization_type": OptimizationResult.BestOptimizationType.NONE,
            "best_facture_optimized": _q3(facture_reference),
            "best_gain": Decimal("0"),
            "best_ps": _q3(ps_current),
            "best_tariff": tariff_current,
            "selection_reason": "Aucun gain positif détecté.",
        }

    if gain_power >= gain_tariff:
        return {
            "facture_reference": _q3(facture_reference),
            "best_optimization_type": OptimizationResult.BestOptimizationType.POWER,
            "best_facture_optimized": _q3(facture_power),
            "best_gain": _q3(gain_power),
            "best_ps": power.get("ps_optimized"),
            "best_tariff": tariff_current,
            "selection_reason": (
                "Optimisation puissance retenue : économie nette sur prime fixe "
                "supérieure ou égale au gain tarif."
            ),
        }

    return {
        "facture_reference": _q3(facture_reference),
        "best_optimization_type": OptimizationResult.BestOptimizationType.TARIFF,
        "best_facture_optimized": _q3(facture_tariff),
        "best_gain": _q3(gain_tariff),
        "best_ps": _q3(ps_current),
        "best_tariff": tariff.get("tariff_optimized"),
        "selection_reason": "Optimisation tarif retenue : gain tarif supérieur au gain puissance.",
    }

    
def simulate_annual_invoice(base, ps=None, tariff=None):
    """
    Simule une facture annuelle à partir de la base annuelle glissante.

    On recalcule :
    - énergie selon le tarif testé
    - prime fixe selon la PS testée
    - pénalité prime selon Pmax et PS testée
    - on conserve redevance, TCO et cos phi de la facture réelle proratisée
    """

    ps = _safe_decimal(ps if ps is not None else base.get("ps_current"))
    tariff = normalize_tariff_category(tariff or base.get("tariff_current"))

    facture_reference = _safe_decimal(base.get("facture_reference"))
    details = base.get("invoice_details") or []

    if ps <= 0:
        return {
            "status": "ERROR",
            "total_ht": facture_reference,
            "warning": "PS non exploitable pour simulation.",
            "breakdown": {},
            "lines": [],
        }

    if not tariff:
        return {
            "status": "ERROR",
            "total_ht": facture_reference,
            "warning": "Tarif non exploitable pour simulation.",
            "breakdown": {},
            "lines": [],
        }

    total_energy = Decimal("0")
    total_prime_fixe = Decimal("0")
    total_penalty_prime = Decimal("0")
    total_redevance = Decimal("0")
    total_tco = Decimal("0")
    total_cosphi = Decimal("0")

    simulated_lines = []
    warnings = []

    for detail in details:
        ref_date = (
            _parse_iso_date(detail.get("retained_start"))
            or _parse_iso_date(detail.get("date_debut_periode"))
            or base.get("period_start")
        )

        tariff_rate = _pick_tariff_rate(tariff, ref_date)

        retained_days = _line_decimal(detail, "retained_days")
        pmax = _line_decimal(detail, "pmax")

        k1 = _line_decimal(detail, "k1_prorated")
        k2 = _line_decimal(detail, "k2_prorated")
        h1 = _line_decimal(detail, "h1_prorated")
        conso = _line_decimal(detail, "conso_prorated")

        redevance = _line_decimal(detail, "redevance_prorated")
        tco = _line_decimal(detail, "tco_prorated")
        cosphi = _line_decimal(detail, "cosphi_amount_prorated")

        current_energy = _line_decimal(detail, "energie_calculee_prorated")
        current_prime = _line_decimal(detail, "prime_fixe_source_prorated")
        current_penalty = _line_decimal(detail, "penalite_prime_prorated")

        if retained_days <= 0:
            continue

        if not tariff_rate:
            warnings.append(
                f"Tarif introuvable pour {tariff} à la date {ref_date}. "
                f"Fallback sur les montants actuels pour la facture {detail.get('numero_facture')}."
            )

            energy_amount = current_energy
            prime_fixe_amount = current_prime
            penalty_prime_amount = current_penalty
        else:
            energy_amount = Decimal("0")

            # Calcul énergie avec K1/K2/H1 quand disponibles
            if k1 > 0 or k2 > 0 or h1 > 0:
                energy_amount += k1 * _safe_decimal(tariff_rate.energie_k1)
                energy_amount += k2 * _safe_decimal(tariff_rate.energie_k2)

                if hasattr(tariff_rate, "energie_k3"):
                    energy_amount += h1 * _safe_decimal(tariff_rate.energie_k3)
            else:
                # Fallback si les index ne sont pas exploitables
                energy_amount = conso * _safe_decimal(tariff_rate.energie_k1)

            # Prime fixe annuelle proratisée par ligne
            prime_fixe_amount = (
                _safe_decimal(tariff_rate.prime_fixe)
                * ps
                * retained_days
                / Decimal("30")
            )

            # Pénalité prime recalculée
            delta = pmax - ps
            if delta < 0:
                delta = Decimal("0")

            penalty_prime_amount = (
                Decimal("1.5")
                * _safe_decimal(tariff_rate.prime_fixe)
                * delta
                * retained_days
                / Decimal("30")
            )

        line_total = (
            energy_amount
            + prime_fixe_amount
            + penalty_prime_amount
            + redevance
            + tco
            + cosphi
        )

        total_energy += energy_amount
        total_prime_fixe += prime_fixe_amount
        total_penalty_prime += penalty_prime_amount
        total_redevance += redevance
        total_tco += tco
        total_cosphi += cosphi

        simulated_lines.append({
            "numero_facture": detail.get("numero_facture"),
            "period": f"{detail.get('retained_start')} -> {detail.get('retained_end')}",
            "tariff": tariff,
            "ps": str(_q3(ps)),
            "pmax": str(_q3(pmax)),
            "energy": str(_q3(energy_amount)),
            "prime_fixe": str(_q3(prime_fixe_amount)),
            "penalty_prime": str(_q3(penalty_prime_amount)),
            "redevance": str(_q3(redevance)),
            "tco": str(_q3(tco)),
            "cosphi": str(_q3(cosphi)),
            "total_ht": str(_q3(line_total)),
        })

    total_ht = (
        total_energy
        + total_prime_fixe
        + total_penalty_prime
        + total_redevance
        + total_tco
        + total_cosphi
    )

    # Sécurité : si la simulation ne produit rien, on garde la facture réelle
    if total_ht <= 0:
        total_ht = facture_reference
        warnings.append("Simulation vide ou non exploitable : fallback sur facture référence.")

    return {
        "status": "OK",
        "total_ht": _q3(total_ht),
        "warning": "; ".join(warnings) if warnings else None,
        "breakdown": {
            "energy": str(_q3(total_energy)),
            "prime_fixe": str(_q3(total_prime_fixe)),
            "penalty_prime": str(_q3(total_penalty_prime)),
            "redevance": str(_q3(total_redevance)),
            "tco": str(_q3(total_tco)),
            "cosphi": str(_q3(total_cosphi)),
        },
        "lines": simulated_lines,
    }


def _get_candidate_ps(base):
    ps_current = _safe_decimal(base.get("ps_current"))
    pmax_avg = _safe_decimal(base.get("pmax_avg"))
    ps_min = _safe_decimal(base.get("ps_min_applicable"))

    if ps_current <= 0 or pmax_avg <= 0 or ps_min <= 0:
        return None

    if pmax_avg >= ps_current:
        return None

    # Important : même si le cahier écrit min(), métier parlant on ne peut pas descendre
    # sous la PS minimale. Donc la bonne PS testée est max(PS_min, Moy_Pmax).
    ps_candidate = max(ps_min, pmax_avg)

    if ps_candidate >= ps_current:
        return None

    return _q3(ps_candidate)


def compute_power_optimization_from_base(base):
    """
    Optimisation puissance seule.

    Logique métier :
    - on garde le même tarif ;
    - on teste une PS optimisée ;
    - on recalcule la prime fixe ;
    - on recalcule la pénalité de dépassement éventuelle ;
    - on compare le scénario actuel et le scénario optimisé.

    Gain PS =
        coût puissance actuel
        -
        coût puissance optimisé

    Le coût puissance inclut :
    - prime fixe liée à la PS ;
    - pénalité de dépassement si Pmax > PS.
    """

    ps_current = _safe_decimal(base.get("ps_current"))
    tariff_current = normalize_tariff_category(base.get("tariff_current"))
    facture_reference = _safe_decimal(base.get("facture_reference"))

    ps_candidate = _get_candidate_ps(base)

    current_sim = simulate_annual_invoice(
        base,
        ps=ps_current,
        tariff=tariff_current,
    )

    if not ps_candidate:
        return {
            "ps_optimized": ps_current,
            "facture_power_optimized": _q3(facture_reference),
            "gain_power": Decimal("0"),
            "warning": "Aucune baisse de puissance exploitable.",
            "simulation": current_sim,
        }

    optimized_sim = simulate_annual_invoice(
        base,
        ps=ps_candidate,
        tariff=tariff_current,
    )

    current_breakdown = current_sim.get("breakdown") or {}
    optimized_breakdown = optimized_sim.get("breakdown") or {}

    current_power_cost = (
        _safe_decimal(current_breakdown.get("prime_fixe"))
        + _safe_decimal(current_breakdown.get("penalty_prime"))
    )

    optimized_power_cost = (
        _safe_decimal(optimized_breakdown.get("prime_fixe"))
        + _safe_decimal(optimized_breakdown.get("penalty_prime"))
    )

    gain = current_power_cost - optimized_power_cost

    if gain <= 0:
        return {
            "ps_optimized": ps_candidate,
            "facture_power_optimized": _q3(facture_reference),
            "gain_power": Decimal("0"),
            "warning": (
                "La baisse de prime fixe ne couvre pas la nouvelle pénalité "
                "de dépassement éventuelle."
            ),
            "simulation": {
                "current": current_sim,
                "optimized": optimized_sim,
                "current_power_cost": str(_q3(current_power_cost)),
                "optimized_power_cost": str(_q3(optimized_power_cost)),
            },
        }

    facture_power_optimized = _apply_gain_to_reference(
        facture_reference,
        gain,
    )

    return {
        "ps_optimized": ps_candidate,
        "facture_power_optimized": facture_power_optimized,
        "gain_power": _q3(gain),
        "warning": optimized_sim.get("warning"),
        "simulation": {
            "current": current_sim,
            "optimized": optimized_sim,
            "current_power_cost": str(_q3(current_power_cost)),
            "optimized_power_cost": str(_q3(optimized_power_cost)),
            "gain_power_cost": str(_q3(gain)),
        },
    }


def compute_tariff_optimization_from_base(base, ps=None):
    """
    Optimisation tarif seule.

    Logique métier :
    - on garde la même puissance ;
    - on teste les tarifs de la même famille ;
    - on retient le tarif qui donne le meilleur gain ;
    - on n'applique pas de baisse de PS dans ce scénario.
    """

    family = base.get("tariff_family")
    tariff_current = normalize_tariff_category(base.get("tariff_current"))
    ps_current = _safe_decimal(ps if ps is not None else base.get("ps_current"))
    facture_reference = _safe_decimal(base.get("facture_reference"))

    current_sim = simulate_annual_invoice(
        base,
        ps=ps_current,
        tariff=tariff_current,
    )

    current_total = _safe_decimal(current_sim.get("total_ht"))

    tariffs = _tariffs_for_family(family)

    if not tariffs:
        return {
            "tariff_optimized": tariff_current,
            "facture_tariff_optimized": _q3(facture_reference),
            "gain_tariff": Decimal("0"),
            "warning": "Famille tarifaire inconnue ou aucun tarif disponible.",
            "simulation": current_sim,
        }

    best_tariff = tariff_current
    best_simulated_total = current_total
    best_simulation = current_sim

    for candidate_tariff in tariffs:
        sim = simulate_annual_invoice(
            base,
            ps=ps_current,
            tariff=candidate_tariff,
        )

        simulated_total = _safe_decimal(sim.get("total_ht"))

        if simulated_total > 0 and simulated_total < best_simulated_total:
            best_simulated_total = simulated_total
            best_tariff = candidate_tariff
            best_simulation = sim

    gain = current_total - best_simulated_total

    if gain <= 0 or best_tariff == tariff_current:
        return {
            "tariff_optimized": tariff_current,
            "facture_tariff_optimized": _q3(facture_reference),
            "gain_tariff": Decimal("0"),
            "warning": "Aucun tarif plus avantageux détecté.",
            "simulation": best_simulation,
        }

    facture_tariff_optimized = _apply_gain_to_reference(
        facture_reference,
        gain,
    )

    return {
        "tariff_optimized": best_tariff,
        "facture_tariff_optimized": facture_tariff_optimized,
        "gain_tariff": _q3(gain),
        "warning": best_simulation.get("warning"),
        "simulation": {
            "current": current_sim,
            "optimized": best_simulation,
            "current_simulated_total": str(_q3(current_total)),
            "optimized_simulated_total": str(_q3(best_simulated_total)),
            "gain_tariff": str(_q3(gain)),
        },
    }





def get_contracts_to_optimize(only_eligible_sites: bool = True) -> List[str]:
    """
    Retourne uniquement les vrais numéros de contrat présents dans les factures.
    On exclut les valeurs texte : RAS, Prepaid, Pas de contrat, etc.
    """

    qs = (
        SonatelInvoice.objects
        .exclude(numero_compte_contrat__isnull=True)
        .exclude(numero_compte_contrat="")
        .exclude(payment_status=SonatelInvoice.PaymentStatus.OUT_OF_SCOPE)
    )

    if only_eligible_sites:
        eligible_contracts = (
            ContractSiteLink.objects
            .filter(site__invoice_payment__iexact="Aktivco", site__grid_fee=True)
            .values_list("numero_compte_contrat", flat=True)
        )

        qs = qs.filter(
            Q(site__invoice_payment__iexact="Aktivco", site__grid_fee=True)
            | Q(numero_compte_contrat__in=eligible_contracts)
        )

    raw_contracts = qs.values_list("numero_compte_contrat", flat=True).distinct()

    contracts = []
    seen = set()

    for c in raw_contracts:
        normalized = _normalize_contract_number(c)

        if not _is_valid_contract_number(normalized):
            continue

        if normalized in seen:
            continue

        seen.add(normalized)
        contracts.append(normalized)

    return contracts

def _invoice_base_qs(numero_compte_contrat: str):
    """
    Base factures utilisables pour l'optimisation.
    On exclut OUT_OF_SCOPE.
    """
    return (
        SonatelInvoice.objects
        .select_related("site")
        .filter(numero_compte_contrat=numero_compte_contrat)
        .exclude(payment_status=SonatelInvoice.PaymentStatus.OUT_OF_SCOPE)
        .exclude(date_debut_periode__isnull=True)
        .exclude(date_fin_periode__isnull=True)
    )


def _compute_index_delta(new_value, old_value) -> Optional[Decimal]:
    if new_value is None or old_value is None:
        return None

    delta = _d(new_value) - _d(old_value)

    if delta < D0:
        return None

    return delta


def build_contract_annual_base(
    numero_compte_contrat: str,
    only_eligible_sites: bool = True,
    reference_date=None,
) -> Dict[str, Any]:
    """
    Construit la base annuelle glissante pour un contrat.

    Résultat :
    - date_ref = MAX(date_fin_periode)
    - period_start = date_ref - 1 an
    - factures qui chevauchent cette période
    - prorata sur la facture qui commence avant period_start
    - sommes annuelles : conso, montant HT, K1, K2, H1, abonnement, pénalités...
    - snapshots : PS actuelle, Pmax moyenne, cosphi moyen, tarif courant
    """

    warnings = []

    numero_compte_contrat = _normalize_contract_number(numero_compte_contrat)

    if not _is_valid_contract_number(numero_compte_contrat):
        return {
            "status": "SKIPPED",
            "numero_compte_contrat": numero_compte_contrat or "",
            "warning": "Numéro de contrat invalide ou non exploitable.",
        }

    link = _get_site_link(numero_compte_contrat)
    base_qs = _invoice_base_qs(numero_compte_contrat)

    if only_eligible_sites:
        has_eligible_link = _is_eligible_link(link)

        has_eligible_invoice_site = base_qs.filter(
            site__invoice_payment__iexact="Aktivco",
            site__grid_fee=True,
        ).exists()

        if not has_eligible_link and not has_eligible_invoice_site:
            return {
                "status": "SKIPPED",
                "numero_compte_contrat": numero_compte_contrat,
                "warning": "Contrat non éligible : site non Aktivco ou grid_fee différent de True.",
            }

    if reference_date:
        date_ref = reference_date
    else:
        date_ref = base_qs.aggregate(max_date=Max("date_fin_periode"))["max_date"]

    if not date_ref:
        return {
            "status": "SKIPPED",
            "numero_compte_contrat": numero_compte_contrat,
            "warning": "Aucune facture valide avec date de fin.",
        }

    period_start = _one_year_before(date_ref)

    invoices = list(
        base_qs
        .filter(
            date_fin_periode__gte=period_start,
            date_debut_periode__lte=date_ref,
        )
        .order_by("date_debut_periode", "date_fin_periode")
    )

    if not invoices:
        return {
            "status": "SKIPPED",
            "numero_compte_contrat": numero_compte_contrat,
            "date_ref": date_ref,
            "period_start": period_start,
            "period_end": date_ref,
            "warning": "Aucune facture dans la fenêtre annuelle.",
        }

    latest_invoice = sorted(invoices, key=lambda x: x.date_fin_periode or date.min)[-1]

    # Site : priorité au ContractSiteLink, sinon site porté par la facture
    site = link.site if link and link.site else latest_invoice.site

    total_conso = D0
    total_ht = D0
    total_ttc = D0

    total_k1 = D0
    total_k2 = D0
    total_h1 = D0

    total_abonnement = D0
    total_penalite_prime = D0
    total_energie_calculee = D0
    total_cosphi_amount = D0
    total_redevance = D0
    total_tco = D0

    pmax_values = []
    cosphi_values = []
    prorated_invoice_count = 0

    invoice_details = []

    for inv in invoices:
        inv_start = inv.date_debut_periode
        inv_end = inv.date_fin_periode

        if not inv_start or not inv_end or inv_end < inv_start:
            warnings.append(
                "Facture ignorée car période invalide : %s" % (inv.numero_facture or "N/A")
            )
            continue

        retained_start = max(inv_start, period_start)
        retained_end = min(inv_end, date_ref)

        if retained_end < retained_start:
            continue

        total_days = _days_inclusive(inv_start, inv_end)
        retained_days = _days_inclusive(retained_start, retained_end)

        if total_days <= 0 or retained_days <= 0:
            continue

        ratio = Decimal(retained_days) / Decimal(total_days)

        is_prorated = retained_days != total_days
        if is_prorated:
            prorated_invoice_count += 1

        conso_prorated = _d(inv.conso_facturee) * ratio
        ht_prorated = _d(inv.montant_hors_tva) * ratio
        ttc_prorated = _d(inv.montant_ttc) * ratio

        k1_delta = _compute_index_delta(inv.nouvel_index_k1, inv.ancien_index_k1)
        k2_delta = _compute_index_delta(inv.nouvel_index_k2, inv.ancien_index_k2)

        k1_prorated = _d(k1_delta) * ratio
        k2_prorated = _d(k2_delta) * ratio
        h1_prorated = _d(inv.conso_h1) * ratio

        abonnement_prorated = _d(inv.abonnement_calcule) * ratio
        penalite_prime_prorated = _d(inv.penalite_abonnement_calculee) * ratio
        energie_calculee_prorated = _d(inv.energie_calculee) * ratio
        cosphi_amount_prorated = _d(inv.montant_cosinus_phi) * ratio
        redevance_prorated = _d(inv.montant_redevance) * ratio
        tco_prorated = _d(inv.montant_tco) * ratio

        total_conso += conso_prorated
        total_ht += ht_prorated
        total_ttc += ttc_prorated

        total_k1 += k1_prorated
        total_k2 += k2_prorated
        total_h1 += h1_prorated

        total_abonnement += abonnement_prorated
        total_penalite_prime += penalite_prime_prorated
        total_energie_calculee += energie_calculee_prorated
        total_cosphi_amount += cosphi_amount_prorated
        total_redevance += redevance_prorated
        total_tco += tco_prorated

        if _is_positive(inv.puissance_max_relevee):
            pmax_values.append(_d(inv.puissance_max_relevee))

        if inv.valeur_cosinus_phi is not None:
            cosphi_values.append(_d(inv.valeur_cosinus_phi))

        invoice_details.append({
            "invoice_id": inv.id,
            "numero_facture": inv.numero_facture,

            "date_debut_periode": inv_start.isoformat(),
            "date_fin_periode": inv_end.isoformat(),
            "retained_start": retained_start.isoformat(),
            "retained_end": retained_end.isoformat(),

            "total_days": total_days,
            "retained_days": retained_days,
            "ratio": str(_q3(ratio)),
            "is_prorated": is_prorated,

            "conso_prorated": str(_q3(conso_prorated)),
            "montant_ht_prorated": str(_q3(ht_prorated)),
            "montant_ttc_prorated": str(_q3(ttc_prorated)),

            "k1_prorated": str(_q3(k1_prorated)),
            "k2_prorated": str(_q3(k2_prorated)),
            "h1_prorated": str(_q3(h1_prorated)),

            "prime_fixe_source_prorated": str(_q3(_d(inv.montant_prime_fixe) * ratio)),
            "abonnement_prorated": str(_q3(abonnement_prorated)),
            "penalite_prime_prorated": str(_q3(penalite_prime_prorated)),
            "energie_calculee_prorated": str(_q3(energie_calculee_prorated)),
            "cosphi_amount_prorated": str(_q3(cosphi_amount_prorated)),
            "redevance_prorated": str(_q3(redevance_prorated)),
            "tco_prorated": str(_q3(tco_prorated)),

            "pmax": str(inv.puissance_max_relevee) if inv.puissance_max_relevee is not None else None,
            "ps": str(inv.puissance_souscrite) if inv.puissance_souscrite is not None else None,
            "tariff": normalize_tariff_category(inv.type_de_tarif),
        })

    if not invoice_details:
        return {
            "status": "SKIPPED",
            "numero_compte_contrat": numero_compte_contrat,
            "date_ref": date_ref,
            "period_start": period_start,
            "period_end": date_ref,
            "warning": "Aucune facture exploitable après contrôle des périodes.",
        }

    pmax_avg = None
    pmax_max = None

    if pmax_values:
        pmax_avg = sum(pmax_values) / Decimal(len(pmax_values))
        pmax_max = max(pmax_values)
    else:
        warnings.append("Aucune puissance max relevée exploitable hors zéro.")

    cosphi_avg = None
    if cosphi_values:
        cosphi_avg = sum(cosphi_values) / Decimal(len(cosphi_values))

    tariff_current = normalize_tariff_category(latest_invoice.type_de_tarif)
    tariff_family = get_tariff_family(tariff_current)
    ps_min_applicable = get_ps_min_for_family(tariff_family)

    if tariff_family == "UNKNOWN":
        warnings.append("Famille tarifaire inconnue pour le tarif actuel : %s" % (tariff_current or "N/A"))

    ps_current = latest_invoice.puissance_souscrite
    puissance_transfo = latest_invoice.puissance_transfo

    return {
        "status": "OK",

        "numero_compte_contrat": numero_compte_contrat,

        "site": site,
        "site_id": site.site_id if site else None,
        "site_name": site.name if site else None,

        "date_ref": date_ref,
        "period_start": period_start,
        "period_end": date_ref,

        "invoices_count": len(invoice_details),
        "prorated_invoice_count": prorated_invoice_count,

        "conso_annuelle": _q3(total_conso),
        "montant_ht_annuel": _q3(total_ht),
        "montant_ttc_annuel": _q3(total_ttc),

        "conso_k1_annuelle": _q3(total_k1),
        "conso_k2_annuelle": _q3(total_k2),
        "conso_h1_annuelle": _q3(total_h1),

        "abonnement_annuel": _q3(total_abonnement),
        "penalite_prime_annuelle": _q3(total_penalite_prime),
        "energie_calculee_annuelle": _q3(total_energie_calculee),
        "montant_cosphi_annuel": _q3(total_cosphi_amount),
        "montant_redevance_annuel": _q3(total_redevance),
        "montant_tco_annuel": _q3(total_tco),

        "ps_current": _q3(ps_current),
        "pmax_avg": _q3(pmax_avg),
        "pmax_max": _q3(pmax_max),
        "puissance_transfo": _q3(puissance_transfo),
        "cosphi_avg": _q3(cosphi_avg),

        "tariff_current": tariff_current,
        "tariff_family": tariff_family,
        "ps_min_applicable": _q3(ps_min_applicable),

        # Dans le cahier, la facture référence correspond au montant annuel actuel
        "facture_reference": _q3(total_ht),

        "invoice_details": invoice_details,
        "warnings": warnings,
    }







def run_power_optimization_batch(user=None, only_eligible_sites=True, reference_date=None):
    """
    Lance l'optimisation Puissance ou Tarif sur tout le parc.

    Important :
    - on calcule les deux scénarios pour comparaison ;
    - on retient uniquement le meilleur ;
    - un contrat ne peut pas être classé en POWER et TARIFF à la fois.
    """

    batch = OptimizationBatch.objects.create(
        launched_by=user if getattr(user, "is_authenticated", False) else None,
        status=OptimizationBatch.Status.RUNNING,
        started_at=timezone.now(),
        only_eligible_sites=only_eligible_sites,
    )

    contracts = get_contracts_to_optimize(only_eligible_sites=only_eligible_sites)

    batch.contracts_count = len(contracts)
    batch.save(update_fields=["contracts_count"])

    analyzed = 0
    skipped = 0

    optimizable_power_count = 0
    total_power_gain = Decimal("0")

    optimizable_tariff_count = 0
    total_tariff_gain = Decimal("0")

    optimizable_total_count = 0
    total_best_gain = Decimal("0")

    for contract in contracts:
        try:
            base = build_contract_annual_base(
                numero_compte_contrat=contract,
                only_eligible_sites=only_eligible_sites,
                reference_date=reference_date,
            )

            if base.get("status") != "OK":
                skipped += 1

                OptimizationResult.objects.create(
                    batch=batch,
                    status=OptimizationResult.Status.SKIPPED,
                    numero_compte_contrat=contract,
                    warning_message=base.get("warning") or "Contrat ignoré.",
                    simulation_details=_clean_base_for_simulation(base),
                )
                continue

            power = compute_power_optimization_from_base(base)
            tariff = compute_tariff_optimization_from_base(base)
            best = compute_best_exclusive_optimization_from_base(base, power, tariff)

            facture_reference = _safe_decimal(base.get("facture_reference"))

            gain_power = _safe_decimal(power.get("gain_power"))
            gain_tariff = _safe_decimal(tariff.get("gain_tariff"))

            best_gain = _safe_decimal(best.get("best_gain"))
            best_type = best.get("best_optimization_type")

            # Compteurs finaux : uniquement le scénario retenu
            if best_gain > 0:
                optimizable_total_count += 1
                total_best_gain += best_gain

                if best_type == OptimizationResult.BestOptimizationType.POWER:
                    optimizable_power_count += 1
                    total_power_gain += best_gain

                elif best_type == OptimizationResult.BestOptimizationType.TARIFF:
                    optimizable_tariff_count += 1
                    total_tariff_gain += best_gain

            site_obj = base.get("site")

            OptimizationResult.objects.create(
                batch=batch,
                status=OptimizationResult.Status.OK,

                numero_compte_contrat=contract,

                site=site_obj,
                site_code=base.get("site_id"),
                site_name=base.get("site_name"),

                date_ref=base.get("date_ref"),
                period_start=base.get("period_start"),
                period_end=base.get("period_end"),

                invoices_count=base.get("invoices_count") or 0,
                prorated_invoice_count=base.get("prorated_invoice_count") or 0,

                conso_annuelle=base.get("conso_annuelle"),
                montant_ht_annuel=base.get("montant_ht_annuel"),

                ps_current=base.get("ps_current"),
                pmax_avg=base.get("pmax_avg"),
                pmax_max=base.get("pmax_max"),
                puissance_transfo=base.get("puissance_transfo"),
                cosphi_avg=base.get("cosphi_avg"),

                tariff_current=base.get("tariff_current"),
                tariff_family=base.get("tariff_family"),

                facture_reference=facture_reference,

                ps_min_applicable=base.get("ps_min_applicable"),

                # Scénario puissance calculé
                ps_optimized=power.get("ps_optimized"),
                facture_power_optimized=power.get("facture_power_optimized"),
                gain_power=gain_power,

                # Scénario tarif calculé
                tariff_optimized=tariff.get("tariff_optimized"),
                facture_tariff_optimized=tariff.get("facture_tariff_optimized"),
                gain_tariff=gain_tariff,

                # Scénario retenu : POWER ou TARIFF ou NONE
                best_optimization_type=best_type,
                best_facture_optimized=best.get("best_facture_optimized"),
                best_gain=best_gain,

                warning_message="; ".join(
                    [
                        w for w in [
                            power.get("warning"),
                            tariff.get("warning"),
                            best.get("selection_reason"),
                            "; ".join(base.get("warnings") or []),
                        ]
                        if w
                    ]
                ) or None,

                simulation_details=_json_safe({
                    "base": _clean_base_for_simulation(base),
                    "power_optimization": power,
                    "tariff_optimization": tariff,
                    "best_exclusive_optimization": best,
                }),
            )

            analyzed += 1

        except Exception as e:
            skipped += 1

            OptimizationResult.objects.create(
                batch=batch,
                status=OptimizationResult.Status.ERROR,
                numero_compte_contrat=contract,
                error_message=str(e),
            )

    batch.contracts_analyzed = analyzed
    batch.contracts_skipped = skipped

    batch.optimizable_power_count = optimizable_power_count
    batch.total_power_gain = total_power_gain

    batch.optimizable_tariff_count = optimizable_tariff_count
    batch.total_tariff_gain = total_tariff_gain

    batch.optimizable_total_count = optimizable_total_count
    batch.total_best_gain = total_best_gain

    batch.status = OptimizationBatch.Status.DONE
    batch.finished_at = timezone.now()
    batch.save()

    return batch