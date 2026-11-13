"""
Target fuel mensuel par SITE (L/mois) — import depuis l'onglet « NR analyse variation conso fuel » du Closing historique, prorata de période, statuts.

Règles : le target est par site (jamais par GE) ; jamais créé à partir d'une puissance, d'un modèle de GE, d'une courbe CPH, d'un target load/énergie ;
valeur historique → TARGET_HISTORIQUE_A_CONFIRMER tant que le Fuel Manager ne l'a pas confirmée pour l'année de la période ; absent → TARGET_INDISPONIBLE.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from fuel_tracking.services import stock_import_files as F

SHEET = "NR analyse variation conso fuel"
RULE_VERSION = "target-fuel-v1"

SOUS, DANS, AU_DESSUS = "SOUS_TARGET", "DANS_TARGET", "AU_DESSUS_TARGET"
A_CONFIRMER, INDISPONIBLE = "TARGET_HISTORIQUE_A_CONFIRMER", "TARGET_INDISPONIBLE"


def _find_header(rows: list) -> tuple[int, dict] | None:
    for i, row in enumerate(rows[:40]):
        keys = {F._norm(c): j for j, c in enumerate(row) if c is not None and str(c).strip()}
        code = next((j for k, j in keys.items() if k.startswith("code site orange")), None)
        target = next((j for k, j in keys.items() if k.startswith("target mensuel")), None)
        if code is not None and target is not None:
            cols = {"code": code, "target": target}
            for name, prefix in (("nom", "nom site"), ("region", "region"), ("typologie", "typologie")):
                cols[name] = next((j for k, j in keys.items() if k.startswith(prefix)), None)
            cols["_names"] = {j: str(row[j]).strip() for j in range(len(row)) if row[j] is not None and str(row[j]).strip()}
            return i, cols
    return None


def process_target_file(batch, file_bytes: bytes, file_name: str) -> dict:
    from fuel_tracking.models import FuelImportRow as Row
    from fuel_tracking.models import FuelSiteTarget as T
    from fuel_tracking.services.stock_registry import SiteResolver

    sheet_rows = None
    for name, rows in F.iter_sheets(file_bytes, file_name):
        if F._norm(name) == F._norm(SHEET):
            sheet_rows = (name, list(rows))
            break
    if sheet_rows is None:
        raise ValueError(f"Onglet « {SHEET} » introuvable : fichier de target fuel non reconnu.")
    name, rows = sheet_rows
    head = _find_header(rows)
    if head is None:
        raise ValueError("Colonnes « CODE SITE ORANGE » et « Target mensuel » introuvables dans l'onglet.")
    hi, cols = head
    resolver = SiteResolver()
    names = cols["_names"]
    year_cols = [j for j, h in names.items() if re.search(r"\b20\d\d\b", h)]
    years = sorted({int(m) for j in year_cols for m in re.findall(r"\b(20\d\d)\b", names[j])})
    reference_year = years[0] if len(years) == 1 else (years[-1] if years else None)

    stats = {"onglet": name, "lignes_lues": 0, "importes": 0, "rejetes": 0, "doublons": 0, "sites_inconnus": 0, "annee_reference": reference_year}
    seen: set[str] = set()
    targets, out_rows = [], []
    for k, row in enumerate(rows[hi + 1:], start=hi + 2):
        code = row[cols["code"]] if cols["code"] < len(row) else None
        if code is None or not str(code).strip():
            continue
        stats["lignes_lues"] += 1
        raw = {names.get(j, f"col{j}"): F._jsonable(v) for j, v in enumerate(row) if v is not None}
        site_raw = str(code).strip()
        resolved = resolver.resolve(site_raw)
        value = F.parse_number(row[cols["target"]]) if cols["target"] < len(row) else None
        msgs, status = [], Row.ValidationStatus.ACCEPTED
        if value is None or value < 0:
            msgs.append({"code": "TARGET_INVALIDE", "message": "Target mensuel absent ou invalide (nombre positif attendu) : ligne rejetée, aucun target inventé."})
            status = Row.ValidationStatus.REJECTED
        key = (resolved or site_raw).upper()
        if status != Row.ValidationStatus.REJECTED and key in seen:
            msgs.append({"code": "DUPLICATE_ALREADY_IMPORTED", "message": "Site déjà présent plus haut dans l'onglet : première occurrence conservée."})
            status = Row.ValidationStatus.WARNING
            stats["doublons"] += 1
        elif status != Row.ValidationStatus.REJECTED:
            seen.add(key)
            if resolved is None:
                msgs.append({"code": "SITE_INCONNU", "message": "Site absent de Gestion des sites : target conservé sous l'identifiant brut."})
                status = Row.ValidationStatus.WARNING
                stats["sites_inconnus"] += 1
            targets.append(T(
                site_id=(resolved or site_raw).upper(), site_name=(str(row[cols["nom"]]).strip() if cols["nom"] is not None and cols["nom"] < len(row) and row[cols["nom"]] else None),
                region=(str(row[cols["region"]]).strip() if cols["region"] is not None and cols["region"] < len(row) and row[cols["region"]] else None),
                typologie=(str(row[cols["typologie"]]).strip() if cols["typologie"] is not None and cols["typologie"] < len(row) and row[cols["typologie"]] else None),
                target_mensuel_l=Decimal(str(value)), reference_year=reference_year, historique={h: raw.get(h) for j, h in names.items() if j in year_cols or "ecart" in F._norm(h) or "écart" in h.lower()},
                batch=batch, source_sheet=name, source_row=k))
            stats["importes"] += 1
        if status == Row.ValidationStatus.REJECTED:
            stats["rejetes"] += 1
        out_rows.append(Row(batch=batch, sheet_name=name, row_number=k, raw_payload=raw, site_id_raw=site_raw, site_id_resolved=resolved, validation_status=status,
                            messages=msgs, row_fingerprint=F.fingerprint("TARGET", key, value)))
    Row.objects.bulk_create(out_rows, batch_size=1000)
    if targets:
        T.objects.filter(is_active=True).update(is_active=False)      # l'historique reste consultable : une nouvelle version remplace la précédente
        T.objects.bulk_create(targets, batch_size=1000)
    return stats


# ── Prorata et statuts ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
def prorated_target(monthly: Decimal | float | None, d_from: date, d_to: date) -> Decimal | None:
    """target_période = Σ_mois target_mensuel × jours sélectionnés du mois / jours du mois (proratisé mois par mois)."""
    if monthly is None:
        return None
    total, d = Decimal("0"), d_from
    while d <= d_to:
        dim = calendar.monthrange(d.year, d.month)[1]
        end = min(d_to, date(d.year, d.month, dim))
        days = (end - d).days + 1
        total += Decimal(str(monthly)) * days / dim
        d = end + timedelta(days=1)
    return total.quantize(Decimal("0.001"))


def tolerance_pct() -> float:
    return float(getattr(settings, "FUEL_TARGET_TOLERANCE_PCT", 10))


def classify(variation: float | None, target: float | None) -> str | None:
    """Position de la variation par rapport au target (dans la tolérance configurable) ; None si non comparable."""
    if variation is None or target is None or target == 0:
        return None
    pct = (variation - target) / target * 100
    return DANS if abs(pct) <= tolerance_pct() else AU_DESSUS if pct > 0 else SOUS


def norm_site(v) -> str:
    """Identifiant de site normalisé pour la jointure conso / target : sans espaces (dont insécables), en majuscules."""
    return "".join(str(v or "").split()).upper()


def active_targets() -> dict:
    from fuel_tracking.models import FuelSiteTarget as T

    return {norm_site(t.site_id): t for t in T.objects.filter(is_active=True)}


def confirmed_for(t, d_from: date, d_to: date) -> bool:
    """Confirmé par le Fuel Manager pour l'année de la période (les deux extrémités)."""
    if t is not None and getattr(t, "source_target", None) == "CLOSING_AKT_INV":
        return t.reference_year == d_from.year == d_to.year        # target du fichier mensuel de l'année en cours : pas un historique à confirmer
    return bool(t is not None and t.confirmed_year and t.confirmed_year == d_from.year == d_to.year)
