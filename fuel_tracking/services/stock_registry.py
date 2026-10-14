# fuel_tracking/services/stock_registry.py
"""
Registre unifié de stock carburant — règles communes à toutes les sources.

  - Sites : référentiel commun core.Site (Gestion des sites), rapprochement sans tenir compte de la casse.
  - Périmètre de gardiennage (fuel_guard_scope) : site dans le périmètre du prestataire → normal ;
    périmètre non configuré → À VALIDER ; autre prestataire configuré → À VALIDER. Jamais de rejet au
    seul motif que l'autre société relève aussi le site ; aucun site n'est supposé Loliforce par défaut.
  - Relevés (fuel_stock_observation) : conversion cm → L par services/tank_conversion.py ; validation
    automatique UNIQUEMENT si conversion valide + capacité de cuve validée + périmètre conforme ; sinon
    À VALIDER avec le motif. Rien n'est supprimé, toute modification est tracée (fuel_registry_correction).
  - Comparaison Loliforce / SCSS : même site, deux relevés en litres à moins de 48 h, capacité validée :
    écart ≤ max(100 L, 10 % capacité) → COHERENT, sinon STOCK_CONFLICT (les deux relevés repassent
    À VALIDER). Pas de moyenne, pas de choix silencieux.
  - Stock retenu (fuel_stock_retained) : relevés VALIDÉS avec litres, contrôle capacité, le plus récent par
    site (somme des cuves validées relevées le même jour). Snowflake reste une information technique.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from fuel_tracking.services import tank_conversion as TC

IN_SCOPE = "IN_SCOPE"
NOT_CONFIGURED = "NOT_CONFIGURED"
PROVIDER_MISMATCH = "PROVIDER_MISMATCH"

COHERENT = "COHERENT"
STOCK_CONFLICT = "STOCK_CONFLICT"
NOT_COMPARABLE = "NOT_COMPARABLE"

COMPARISON_WINDOW = timedelta(hours=48)
GUARD_SOURCES = ("LOLIFORCE", "SCSS")


def tolerance_pct() -> float:
    return float(getattr(settings, "FUEL_CAPACITY_TOLERANCE_PCT", 5.0))


# ── Sites ────────────────────────────────────────────────────────────────────

class SiteResolver:
    """Rapproche un identifiant brut du référentiel core.Site (casse ignorée)."""

    def __init__(self):
        from core.models import Site

        pairs = list(Site.objects.values_list("site_id", "name"))
        self.by_upper = {s.upper(): s for s, _ in pairs if s}
        names: dict[str, set] = defaultdict(set)
        for s, n in pairs:
            if s and n:
                names[_name_key(n)].add(s)
        self.by_name = {k: next(iter(v)) for k, v in names.items() if len(v) == 1}
        self.empty = not self.by_upper

    def resolve(self, raw) -> str | None:
        if raw is None:
            return None
        s = str(raw).strip().replace(" ", "")
        if not s:
            return None
        if self.empty:
            return s.upper()
        # Séparateur saisi « THS-2080 » au lieu de « THS_2080 » : identifiant brut conservé dans fuel_import_row.
        return self.by_upper.get(s.upper()) or self.by_upper.get(re.sub(r"[-.]", "_", s.upper()))

    def resolve_by_name(self, name) -> str | None:
        """Site retrouvé par son nom exact (casse, espaces et tirets ignorés) — seulement si le nom est unique."""
        if not name or self.empty:
            return None
        return self.by_name.get(_name_key(name))


def _name_key(name) -> str:
    return re.sub(r"[\s_\-]+", "", str(name)).upper()


# ── Cuves ────────────────────────────────────────────────────────────────────

def site_tanks(site_ids) -> dict[str, list[TC.TankSpec]]:
    from fuel_tracking.models import FuelTankMaster

    out: dict[str, list[TC.TankSpec]] = defaultdict(list)
    qs = FuelTankMaster.objects.filter(active=True).exclude(validation_status="REJECTED")
    if site_ids is not None:
        qs = qs.filter(site_id__in=list(site_ids))
    for t in qs:
        out[t.site_id].append(TC.TankSpec.from_model(t))
    return out


# ── Périmètre de gardiennage ─────────────────────────────────────────────────

def guard_scope_status(site_id: str, provider: str, at, scopes_by_site: dict | None = None) -> tuple[str, str | None]:
    """(statut, message) du périmètre de gardiennage pour ce site, ce prestataire, à cette date."""
    from fuel_tracking.models import FuelGuardScope

    day = at.date() if isinstance(at, datetime) else at
    if scopes_by_site is not None:
        scopes = scopes_by_site.get(site_id, [])
    else:
        scopes = list(FuelGuardScope.objects.filter(site_id=site_id))
    active = [s for s in scopes if s.active_from <= day and (s.active_to is None or s.active_to >= day)]
    if not active:
        return NOT_CONFIGURED, "Périmètre de gardiennage non configuré pour ce site à cette date : relevé à valider."
    providers = {s.provider for s in active}
    if provider in providers:
        return IN_SCOPE, None
    return PROVIDER_MISMATCH, (f"Site attribué à {', '.join(sorted(providers))} à cette date, relevé transmis par {provider} : "
                               "à valider (jamais rejeté pour ce seul motif).")


def guard_scopes_by_site(site_ids=None) -> dict[str, list]:
    from fuel_tracking.models import FuelGuardScope

    qs = FuelGuardScope.objects.all()
    if site_ids is not None:
        qs = qs.filter(site_id__in=list(site_ids))
    out: dict[str, list] = defaultdict(list)
    for s in qs:
        out[s.site_id].append(s)
    return out


# ── Piste d'audit ────────────────────────────────────────────────────────────

def log_correction(entity: str, entity_id: int, action: str, reason: str, *, site_id=None, field=None,
                   old=None, new=None, user=None, author_label=None):
    from fuel_tracking.models import FuelRegistryCorrection

    return FuelRegistryCorrection.objects.create(
        entity=entity, entity_id=entity_id, site_id=site_id, action=action, field=field,
        old_value=None if old is None else str(old), new_value=None if new is None else str(new),
        reason=reason, author=user if (user and getattr(user, "is_authenticated", False)) else None,
        author_label=author_label or (None if user else "système"),
    )


# ── Relevés ──────────────────────────────────────────────────────────────────

@dataclass
class ObservationInput:
    source_type: str
    source_event_id: str
    site_id: str
    observation_datetime: datetime
    level_raw: str | None
    level_value: float | None
    level_unit: str | None
    tank_id: str | None = None
    hour_meter: float | None = None
    source_user: str | None = None
    source_reference: str | None = None
    comment: str | None = None
    proof_reference: str | None = None
    extra_messages: list | None = None  # alertes de lecture non bloquantes (ex. deux relevés cm et L)
    review_flags: list | None = None  # motifs bloquants indépendants de la conversion → À VALIDER


def build_observation(inp: ObservationInput, tanks: list[TC.TankSpec], guard: tuple[str, str | None] | None):
    """Construit (sans l'enregistrer) l'observation et la liste des messages de contrôle."""
    from fuel_tracking.models import FuelStockObservation as Obs

    conv = TC.convert(inp.level_value, inp.level_unit, tanks, inp.tank_id, tolerance_pct())
    messages = list(inp.extra_messages or []) + list(conv.messages)
    reasons = []
    status = Obs.ValidationStatus.VALIDATED

    if conv.status == TC.INVALID:
        status = Obs.ValidationStatus.REJECTED
        reasons.append("Rejet automatique : " + "; ".join(m["message"] for m in conv.messages))
    else:
        if conv.status == TC.PENDING_TANK_MAPPING:
            status = Obs.ValidationStatus.PENDING
            reasons.append("Conversion impossible : " + "; ".join(m["message"] for m in conv.messages if m["code"] != "CAPACITY_NOT_VALIDATED"))
        if any(m["code"] == "CAPACITY_NOT_VALIDATED" for m in conv.messages):
            status = Obs.ValidationStatus.PENDING
            reasons.append("Capacité de cuve non validée : contrôle de capacité impossible.")
        if guard is not None and guard[0] != IN_SCOPE:
            status = Obs.ValidationStatus.PENDING
            reasons.append(guard[1])
            messages.append({"code": guard[0], "message": guard[1]})
        for flag in inp.review_flags or []:
            status = Obs.ValidationStatus.PENDING
            reasons.append(flag)

    obs = Obs(
        source_type=inp.source_type, source_event_id=inp.source_event_id[:128], site_id=inp.site_id,
        tank_id=conv.tank_id or inp.tank_id, observation_datetime=inp.observation_datetime,
        level_raw=None if inp.level_raw is None else str(inp.level_raw)[:128],
        level_value=TC.to_decimal(inp.level_value), level_unit=inp.level_unit if inp.level_unit in ("L", "CM") else None,
        level_liters=TC.to_decimal(conv.level_liters), conversion_method=conv.method, conversion_status=conv.status,
        conversion_message="; ".join(m["message"] for m in conv.messages) or None,
        capacity_l=TC.to_decimal(conv.capacity_l), hour_meter=TC.to_decimal(inp.hour_meter),
        source_user=(inp.source_user or None) and str(inp.source_user)[:255],
        source_reference=(inp.source_reference or None) and str(inp.source_reference)[:255],
        guard_scope_status=guard[0] if guard else None, comment=inp.comment,
        proof_reference=(inp.proof_reference or None) and str(inp.proof_reference)[:512],
        validation_status=status, review_flags=list(inp.review_flags or []),
        validation_reason=" | ".join(r for r in reasons if r) or ("Validation automatique : conversion valide, capacité contrôlée"
                                                                   + (", périmètre de gardiennage conforme." if guard else ".")),
    )
    return obs, messages


def same_source_conflict(obs) -> str | None:
    """Autre relevé déjà importé (même source, site, cuve, date) avec une valeur différente."""
    from fuel_tracking.models import FuelStockObservation as Obs

    others = Obs.objects.filter(source_type=obs.source_type, site_id=obs.site_id, tank_id=obs.tank_id,
                                observation_datetime=obs.observation_datetime).exclude(source_event_id=obs.source_event_id)
    for o in others:
        if (o.level_value, o.level_unit) != (obs.level_value, obs.level_unit):
            return (f"Valeur différente d'un relevé {obs.source_type} déjà importé pour la même date "
                    f"({o.level_raw} — relevé #{o.pk}) : à valider.")
    return None


# ── Comparaison Loliforce / SCSS ─────────────────────────────────────────────

def compare_pair(a, b, capacity_l) -> dict:
    """Compare deux relevés (objets avec level_liters, observation_datetime, source_type)."""
    out = {"a": getattr(a, "pk", None), "b": getattr(b, "pk", None), "sources": [a.source_type, b.source_type]}
    if a.level_liters is None or b.level_liters is None:
        return {**out, "status": NOT_COMPARABLE, "reason": "Un des relevés n'est pas en litres (conversion impossible)."}
    delta_t = abs(a.observation_datetime - b.observation_datetime)
    if delta_t >= COMPARISON_WINDOW:
        return {**out, "status": NOT_COMPARABLE, "reason": f"Relevés espacés de {delta_t.total_seconds() / 3600:.0f} h (≥ 48 h)."}
    if not capacity_l:
        return {**out, "status": NOT_COMPARABLE, "reason": "Capacité de cuve non validée."}
    tol = max(100.0, 0.10 * float(capacity_l))
    diff = abs(float(a.level_liters) - float(b.level_liters))
    status = COHERENT if diff <= tol else STOCK_CONFLICT
    return {**out, "status": status, "ecart_l": round(diff, 1), "tolerance_l": round(tol, 1),
            "reason": None if status == COHERENT else f"Écart {diff:.0f} L > tolérance {tol:.0f} L : aucune moyenne, aucun choix automatique."}


def site_capacity(tanks: list[TC.TankSpec], tank_id: str | None) -> float | None:
    validated = [t for t in tanks if t.validated]
    if tank_id:
        for t in validated:
            if str(t.tank_id).upper() == str(tank_id).upper():
                return t.capacity_l or t.geometric_capacity_l()
        return None
    if len(validated) == 1:
        return validated[0].capacity_l or validated[0].geometric_capacity_l()
    return None


def guard_comparisons(site_id: str, observations=None, tanks=None) -> list[dict]:
    """Toutes les paires Loliforce/SCSS à moins de 48 h d'un site (même cuve)."""
    from fuel_tracking.models import FuelStockObservation as Obs

    if observations is None:
        observations = list(Obs.objects.filter(site_id=site_id, source_type__in=GUARD_SOURCES)
                            .exclude(validation_status="REJECTED").order_by("observation_datetime"))
    tanks = tanks if tanks is not None else site_tanks([site_id]).get(site_id, [])
    loli = [o for o in observations if o.source_type == "LOLIFORCE"]
    scss = [o for o in observations if o.source_type == "SCSS"]
    out = []
    single_tank = len(tanks) <= 1  # une seule cuve (ou aucune) : l'identifiant de cuve n'est pas discriminant
    for a in loli:
        for b in scss:
            if not single_tank and (a.tank_id or None) != (b.tank_id or None):
                continue
            if abs(a.observation_datetime - b.observation_datetime) >= COMPARISON_WINDOW:
                continue
            res = compare_pair(a, b, site_capacity(tanks, a.tank_id))
            res.update({"site_id": site_id, "tank_id": a.tank_id,
                        "loliforce": {"id": a.pk, "datetime": a.observation_datetime, "level_l": _f(a.level_liters), "raw": a.level_raw},
                        "scss": {"id": b.pk, "datetime": b.observation_datetime, "level_l": _f(b.level_liters), "raw": b.level_raw}})
            out.append(res)
    return out


def apply_guard_conflicts(site_ids) -> int:
    """Repasse À VALIDER les relevés auto-validés impliqués dans un STOCK_CONFLICT (jamais un relevé validé par un humain)."""
    from fuel_tracking.models import FuelStockObservation as Obs

    changed = 0
    for site_id in set(site_ids):
        for c in guard_comparisons(site_id):
            if c["status"] != STOCK_CONFLICT:
                continue
            for oid in (c["loliforce"]["id"], c["scss"]["id"]):
                o = Obs.objects.get(pk=oid)
                if o.validation_status == "VALIDATED" and o.validated_by_id is None:
                    reason = f"STOCK_CONFLICT Loliforce/SCSS : {c['reason']}"
                    log_correction("OBSERVATION", o.pk, "SYSTEM", reason, site_id=o.site_id, field="validation_status",
                                   old=o.validation_status, new="PENDING", author_label="système (comparaison gardiennage)")
                    o.validation_status = "PENDING"
                    o.validation_reason = reason
                    o.save(update_fields=["validation_status", "validation_reason", "updated_at"])
                    changed += 1
    return changed


# ── Stock retenu ─────────────────────────────────────────────────────────────

def _f(v):
    return float(v) if v is not None else None


UNAVAILABLE_NO_OBS = "Aucun relevé de stock pour ce site."
UNAVAILABLE_NONE_VALIDATED = "Relevé(s) présent(s) mais aucun relevé validé exploitable en litres."


def fuel_stock_retained(site_ids=None, now=None) -> dict[str, dict]:
    """
    Stock retenu par site. Retourne {site_id: {...}} ; un site sans stock retenu a stock_l = None et la
    raison (unavailable_reason). Seules les sources ENOC / LOLIFORCE / SCSS sont éligibles.
    """
    from fuel_tracking.models import FuelStockObservation as Obs

    now = now or timezone.now()
    qs = Obs.objects.exclude(source_type__in=("SNOWFLAKE", "CLOSING"))
    if site_ids is not None:
        qs = qs.filter(site_id__in=list(site_ids))
    fields = ("id", "site_id", "tank_id", "source_type", "observation_datetime", "level_raw", "level_value", "level_unit",
              "level_liters", "conversion_method", "conversion_status", "validation_status", "validation_reason",
              "capacity_l", "proof_reference")
    by_site: dict[str, list[dict]] = defaultdict(list)
    for o in qs.order_by("site_id", "-observation_datetime", "-id").values(*fields):
        by_site[o["site_id"]].append(o)
    tanks_by_site = site_tanks(by_site.keys())
    tol = tolerance_pct()

    out = {}
    for site_id, obs in by_site.items():
        tanks = tanks_by_site.get(site_id, [])
        validated_tanks = [t for t in tanks if t.validated]
        latest_any = obs[0]
        candidates = []
        for o in obs:
            if o["validation_status"] != "VALIDATED" or o["level_liters"] is None or o["conversion_status"] != "VALID":
                continue
            cap = site_capacity(tanks, o["tank_id"]) if validated_tanks else None
            if cap is not None and float(o["level_liters"]) > cap * (1 + tol / 100):
                continue  # volume au-delà de la capacité validée : jamais retenu
            candidates.append({**o, "_cap": cap})
        if not candidates:
            out[site_id] = _unavailable(site_id, latest_any, UNAVAILABLE_NONE_VALIDATED, now)
            continue

        best = candidates[0]
        day = best["observation_datetime"].date()
        if len(validated_tanks) > 1:
            # Somme des cuves validées relevées le même jour (relevé le plus récent par cuve).
            per_tank = {}
            for c in candidates:
                if c["observation_datetime"].date() != day or not c["tank_id"]:
                    continue
                per_tank.setdefault(str(c["tank_id"]).upper(), c)
            missing = [t.tank_id for t in validated_tanks if str(t.tank_id).upper() not in per_tank]
            if missing:
                out[site_id] = _unavailable(site_id, best,
                                            f"Relevé incomplet le {day:%d/%m/%Y} : cuve(s) {', '.join(missing)} sans relevé validé ce jour-là.", now)
                continue
            parts = list(per_tank.values())
            stock = sum(float(p["level_liters"]) for p in parts)
            cap_total = sum((t.capacity_l or t.geometric_capacity_l() or 0) for t in validated_tanks) or None
        else:
            parts = [best]
            stock = float(best["level_liters"])
            cap_total = best["_cap"]
        out[site_id] = {
            "site_id": site_id,
            "stock_l": round(stock, 1),
            "source_type": best["source_type"],
            "observation_id": best["id"],
            "observation_ids": [p["id"] for p in parts],
            "observation_datetime": best["observation_datetime"],
            "age_hours": round((now - best["observation_datetime"]).total_seconds() / 3600, 1),
            "level_raw": best["level_raw"] if len(parts) == 1 else " + ".join(f"C{p['tank_id']}: {p['level_raw']}" for p in parts),
            "level_unit": best["level_unit"],
            "conversion_method": best["conversion_method"],
            "conversion_status": best["conversion_status"],
            "validation_status": best["validation_status"],
            "capacity_l": round(cap_total, 1) if cap_total else None,
            "fill_pct": round(stock / cap_total * 100, 1) if cap_total else None,
            "tanks_count": len(parts),
            "proof_reference": best["proof_reference"],
            "unavailable_reason": None,
            "capacity_checked": cap_total is not None,
        }
    return out


def _unavailable(site_id, o, reason, now):
    return {
        "site_id": site_id, "stock_l": None, "source_type": o["source_type"] if o else None,
        "observation_id": o["id"] if o else None, "observation_ids": [],
        "observation_datetime": o["observation_datetime"] if o else None,
        "age_hours": round((now - o["observation_datetime"]).total_seconds() / 3600, 1) if o else None,
        "level_raw": o["level_raw"] if o else None, "level_unit": o["level_unit"] if o else None,
        "conversion_method": o["conversion_method"] if o else None, "conversion_status": o["conversion_status"] if o else None,
        "validation_status": o["validation_status"] if o else None, "capacity_l": _f(o.get("capacity_l")) if o else None,
        "fill_pct": None, "tanks_count": 0, "proof_reference": None,
        "unavailable_reason": reason + (f" Dernier relevé : {o['validation_reason']}" if o and o.get("validation_reason") and o["validation_status"] != "VALIDATED" else ""),
        "capacity_checked": False,
    }


# ── Actions humaines (valider / rejeter / corriger) ─────────────────────────

@transaction.atomic
def validate_observation(obs, user, reason: str, validate: bool = True):
    new = "VALIDATED" if validate else "REJECTED"
    if validate and (obs.level_liters is None or obs.conversion_status != "VALID"):
        raise ValueError("Relevé non convertible en litres (cuve à référencer ou valider d'abord) : validation impossible.")
    log_correction("OBSERVATION", obs.pk, "VALIDATE" if validate else "REJECT", reason, site_id=obs.site_id,
                   field="validation_status", old=obs.validation_status, new=new, user=user)
    obs.validation_status = new
    obs.validation_reason = reason
    obs.validated_by = user
    obs.validated_at = timezone.now()
    obs.save(update_fields=["validation_status", "validation_reason", "validated_by", "validated_at", "updated_at"])
    return obs


@transaction.atomic
def validate_movement(mv, user, reason: str, validate: bool = True):
    new = "VALIDATED" if validate else "REJECTED"
    if validate and (mv.quantity_l is None or mv.movement_type is None):
        raise ValueError("Mouvement sans quantité réalisée ou sans type : validation impossible.")
    log_correction("MOVEMENT", mv.pk, "VALIDATE" if validate else "REJECT", reason, site_id=mv.site_id,
                   field="status", old=mv.status, new=new, user=user)
    mv.status = new
    mv.validation_reason = reason
    mv.validated_by = user
    mv.validated_at = timezone.now()
    mv.save(update_fields=["status", "validation_reason", "validated_by", "validated_at", "updated_at"])
    return mv


TANK_EDITABLE = ("shape", "length_m", "width_m", "height_m", "diameter_m", "capacity_l", "calibration_curve",
                 "calibration_validated", "active", "comment")


@transaction.atomic
def correct_tank(tank, changes: dict, user, reason: str, validate: bool | None = None):
    """Correction d'une cuve (chaque champ modifié est tracé) ; validate=True/False valide ou rejette la cuve."""
    for field, value in changes.items():
        if field not in TANK_EDITABLE:
            raise ValueError(f"Champ non modifiable : {field}")
        old = getattr(tank, field)
        if field in ("length_m", "width_m", "height_m", "diameter_m", "capacity_l") and value not in (None, ""):
            value = Decimal(str(value))
            if value <= 0:
                raise ValueError(f"{field} doit être strictement positif.")
        if value == "":
            value = None
        if str(old) != str(value):
            log_correction("TANK", tank.pk, "CORRECT", reason, site_id=tank.site_id, field=field, old=old, new=value, user=user)
            setattr(tank, field, value)
    if validate is not None:
        new = "VALIDATED" if validate else "REJECTED"
        if validate:
            spec = TC.TankSpec.from_model(tank)
            if not spec.shape:
                raise ValueError("Forme de cuve manquante : validation impossible.")
            if not (tank.calibration_curve and tank.calibration_validated):
                try:
                    TC.geometric_volume_l(spec, 1.0)
                except TC.MissingDimensions:
                    raise ValueError("Dimensions incomplètes pour la forme de la cuve : validation impossible.")
        log_correction("TANK", tank.pk, "VALIDATE" if validate else "REJECT", reason, site_id=tank.site_id,
                       field="validation_status", old=tank.validation_status, new=new, user=user)
        tank.validation_status = new
        tank.validated_by = user
        tank.validated_at = timezone.now()
    tank.save()
    return tank


def reconvert_site_observations(site_id: str, user=None) -> int:
    """Après correction / validation d'une cuve : reconvertit les relevés cm du site non validés humainement."""
    from fuel_tracking.models import FuelStockObservation as Obs

    tanks = site_tanks([site_id]).get(site_id, [])
    scopes = guard_scopes_by_site([site_id])
    n = 0
    for o in Obs.objects.filter(site_id=site_id, validated_by__isnull=True).exclude(source_type__in=("SNOWFLAKE", "CLOSING")):
        guard = guard_scope_status(site_id, o.source_type, o.observation_datetime, scopes) if o.source_type in GUARD_SOURCES else None
        inp = ObservationInput(o.source_type, o.source_event_id, o.site_id, o.observation_datetime, o.level_raw,
                               _f(o.level_value), o.level_unit, o.tank_id, review_flags=o.review_flags)
        new, _ = build_observation(inp, tanks, guard)
        before = (o.tank_id, o.level_liters, o.conversion_status, o.validation_status)
        after = (new.tank_id, new.level_liters, new.conversion_status, new.validation_status)
        if before != after:
            log_correction("OBSERVATION", o.pk, "SYSTEM", "Reconversion après modification du référentiel cuve",
                           site_id=site_id, field="level_liters/conversion/validation", old=before, new=after, user=user,
                           author_label=None if user else "système")
            for f in ("tank_id", "level_liters", "conversion_method", "conversion_status", "conversion_message", "capacity_l",
                      "validation_status", "validation_reason", "guard_scope_status"):
                setattr(o, f, getattr(new, f))
            o.save()
            n += 1
    apply_guard_conflicts([site_id])
    return n
