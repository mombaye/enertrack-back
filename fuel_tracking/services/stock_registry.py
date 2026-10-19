# fuel_tracking/services/stock_registry.py
"""
Registre unifié de stock carburant — règles communes à toutes les sources.

  - Sites : référentiel commun core.Site (Gestion des sites), rapprochement sans tenir compte de la casse.
  - Périmètre de gardiennage (fuel_guard_scope) : site dans le périmètre du prestataire → normal ;
    périmètre non configuré → À VALIDER ; autre prestataire configuré → À VALIDER. Jamais de rejet au
    seul motif que l'autre société relève aussi le site ; aucun site n'est supposé Loliforce par défaut.
  - Relevés (fuel_stock_observation) : conversion cm → L par services/tank_conversion.py (calculateur
    steve-v1, volume géométrique / calibré) ; AUTO_VALIDÉ uniquement si tous les contrôles objectifs passent ;
    sinon À TRAITER avec des codes d'anomalie (issue_codes → anomalie, règle échouée, action à faire).
    Rien n'est supprimé, toute modification est tracée (fuel_registry_correction).
  - Comparaison Loliforce / SCSS : même site, deux relevés en litres à moins de 48 h, capacité validée :
    écart ≤ max(100 L, 10 % capacité) → COHERENT, sinon STOCK_CONFLICT (priorité configurable
    FUEL_GUARD_CONFLICT_PRIORITY, sinon les deux relevés passent À TRAITER). Jamais de moyenne.
  - Stock retenu (fuel_stock_retained) : cuve site VALIDÉE, relevé validé en litres, capacité contrôlée ;
    jour le plus récent, priorité FUEL_STOCK_SOURCE_PRIORITY entre relevés du même jour (terrain avec preuve,
    gardiennage, ENOC). Snowflake reste une information technique, jamais retenue.
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
    qs = FuelTankMaster.objects.filter(active=True).exclude(validation_status="REJETEE").select_related("tank_model")
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


# ── Anomalies → action à faire ──────────────────────────────────────────────
# Chaque relevé « à traiter » porte des codes (issue_codes) : jamais un simple « à valider ».

ISSUES = {
    "NO_VALIDATED_TANK": ("aucune cuve validée pour ce site", "Conversion cm → L : cuve site VALIDÉE requise",
                          "Renseigner ou confirmer la cuve avant conversion", "OPEN_TANK"),
    "TANK_PROPOSED": ("cuve proposée, pas encore validée", "Conversion cm → L : cuve site VALIDÉE requise",
                      "Contrôler et valider la cuve proposée (forme, cotes, capacité de plaque)", "OPEN_TANK"),
    "TANK_TO_CONFIRM": ("capacité connue mais forme / cotes insuffisantes", "Géométrie reconnue par le calculateur requise",
                        "Renseigner la forme et les cotes de la cuve", "OPEN_TANK"),
    "TANK_INCOMPLETE": ("aucune information cuve exploitable", "Cuve site VALIDÉE requise",
                        "Renseigner la cuve du site (forme, cotes, capacité de plaque)", "OPEN_TANK"),
    "TANK_UNKNOWN": ("numéro de cuve absent du référentiel du site", "Le relevé doit viser une cuve connue",
                     "Créer la cuve sur le site ou corriger le numéro de cuve", "OPEN_TANK"),
    "MULTI_TANK_WITHOUT_ID": ("plusieurs cuves sur le site, relevé sans numéro de cuve", "Jamais d'attribution automatique à la cuve 1",
                              "Identifier les cuves du site et demander le numéro de cuve dans les relevés", "OPEN_TANK"),
    "MISSING_DIMENSIONS": ("cotes de cuve insuffisantes pour la forme", "Géométrie reconnue par le calculateur requise",
                           "Compléter les cotes de la cuve", "OPEN_TANK"),
    "PLATE_MISSING": ("capacité de plaque non confirmée : volume géométrique seulement", "Volume retenu = volume calibré (plaque cohérente à 5 %)",
                      "Confirmer la capacité de plaque de la cuve, ou valider manuellement le volume géométrique", "OPEN_TANK"),
    "PLATE_INCOHERENT": ("écart plaque / géométrie > 5 %", "Capacité de plaque cohérente à 5 % avec la géométrie",
                         "Reprendre les cotes intérieures ou la capacité de plaque", "OPEN_TANK"),
    "OVER_CAPACITY": ("volume au-dessus de la capacité", "Volume ≤ capacité + 5 %",
                      "Vérifier le relevé (unité, niveau) ou la capacité de la cuve", "OPEN_HISTORY"),
    "LEVEL_ABOVE_HEIGHT": ("niveau au-dessus de la hauteur utile de la cuve", "Niveau ≤ hauteur utile",
                           "Vérifier le relevé ou les cotes de la cuve", "OPEN_HISTORY"),
    "NEGATIVE_LEVEL": ("niveau négatif", "Niveau ≥ 0", "Corriger le relevé à la source", "OPEN_HISTORY"),
    "UNKNOWN_UNIT": ("unité de niveau inconnue", "Unité cm ou L", "Corriger le relevé à la source", "OPEN_HISTORY"),
    "NOT_CONFIGURED": ("périmètre de gardiennage non configuré", "Relevé gardiennage : site dans le périmètre du prestataire",
                       "Affecter le site à Loliforce ou SCSS (période)", "SET_GUARD"),
    "PROVIDER_MISMATCH": ("relevé transmis par un autre prestataire que celui du site", "Relevé gardiennage : prestataire du site",
                          "Confirmer le prestataire du site, puis valider ou rejeter le relevé", "VALIDATE_OBS"),
    "STOCK_CONFLICT": ("conflit Loliforce / SCSS (écart > max(100 L ; 10 % capacité))", "Aucune moyenne automatique",
                       "Choisir le relevé correct (valider l'un, rejeter l'autre)", "VALIDATE_OBS"),
    "SAME_SOURCE_CONFLICT": ("deux valeurs différentes de la même source à la même date", "Une valeur par source et par date",
                             "Valider la bonne valeur, rejeter l'autre", "VALIDATE_OBS"),
    "ENOC_NOT_CLOSED": ("opération ENOC non clôturée", "Relevé ENOC d'une opération clôturée (done)",
                        "Attendre la clôture ENOC ou valider manuellement", "VALIDATE_OBS"),
    "CM_L_GAP": ("écart entre litres déclarés et conversion du niveau cm", "Écart ≤ max(100 L ; 10 % capacité)",
                 "Vérifier le relevé (cm ou litres) avant validation", "VALIDATE_OBS"),
    "CAPACITY_NOT_VALIDATED": ("aucune cuve validée : capacité non contrôlable", "Cuve site VALIDÉE requise",
                               "Renseigner ou confirmer la cuve du site", "OPEN_TANK"),
}


def issue_detail(code: str) -> dict:
    a, rule, action, kind = ISSUES.get(code, (code, "—", "Ouvrir l'historique du site", "OPEN_HISTORY"))
    return {"code": code, "anomalie": a, "regle": rule, "action": action, "action_type": kind}


def flag(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _flags(raw) -> list[dict]:
    out = []
    for f in raw or []:
        out.append(f if isinstance(f, dict) else {"code": "REVIEW", "message": str(f)})
    return out


def display_status(o) -> str:
    """AUTO_VALIDE / VALIDE_MANUELLEMENT / A_TRAITER / REJETE / NON_EXPLOITABLE."""
    vs = o["validation_status"] if isinstance(o, dict) else o.validation_status
    by = o.get("validated_by_id") if isinstance(o, dict) else o.validated_by_id
    if vs == "VALIDATED":
        return "VALIDE_MANUELLEMENT" if by else "AUTO_VALIDE"
    return {"PENDING": "A_TRAITER", "REJECTED": "REJETE", "NON_EXPLOITABLE": "NON_EXPLOITABLE"}.get(vs, vs)


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
    extra_messages: list | None = None  # alertes de lecture non bloquantes
    review_flags: list | None = None  # [{code, message}] motifs bloquants indépendants de la conversion → À TRAITER
    cm_value: float | None = None  # niveau cm saisi EN PLUS de litres déclarés (les deux sont conservés)


def _gap_tolerance(cap) -> float:
    return max(100.0, 0.10 * float(cap)) if cap else 100.0


def build_observation(inp: ObservationInput, tanks: list[TC.TankSpec], guard: tuple[str, str | None] | None):
    """Construit (sans l'enregistrer) l'observation, ses volumes, ses codes d'anomalie et ses messages."""
    from fuel_tracking.models import FuelStockObservation as Obs

    conv = TC.convert(inp.level_value, inp.level_unit, tanks, inp.tank_id, tolerance_pct())
    messages = list(inp.extra_messages or []) + list(conv.messages)
    flags = _flags(inp.review_flags)
    codes: list[str] = []
    reasons = []
    status = Obs.ValidationStatus.VALIDATED
    geometric, calibrated, gap, level_cm, declared = conv.geometric_l, conv.calibrated_l, None, None, None
    rule = conv.rule

    if inp.level_unit == "CM":
        level_cm = inp.level_value
    elif inp.level_unit == "L":
        declared = inp.level_value
    if inp.cm_value is not None and inp.level_unit == "L":
        level_cm = inp.cm_value
        cm_conv = TC.convert(inp.cm_value, "CM", tanks, inp.tank_id, tolerance_pct())
        geometric, calibrated = cm_conv.geometric_l, cm_conv.calibrated_l
        ref = cm_conv.level_liters
        if ref is not None:
            gap = round(inp.level_value - ref, 1)
            tol = _gap_tolerance(cm_conv.capacity_l)
            if abs(gap) > tol:
                flags.append(flag("CM_L_GAP", f"Litres déclarés {inp.level_value:.0f} L ≠ conversion du niveau {inp.cm_value:g} cm "
                                              f"({ref:.0f} L) : écart {gap:+.0f} L > {tol:.0f} L."))
        elif geometric is not None:
            gap = round(inp.level_value - geometric, 1)

    if conv.status == TC.INVALID and {m["code"] for m in conv.messages} <= {"UNKNOWN_UNIT", "MISSING_LEVEL"}:
        status = Obs.ValidationStatus.NON_EXPLOITABLE
        codes += [m["code"] for m in conv.messages]
        reasons.append("Donnée conservée mais non exploitable : " + "; ".join(m["message"] for m in conv.messages))
    elif conv.status == TC.INVALID:
        status = Obs.ValidationStatus.REJECTED
        codes += [m["code"] for m in conv.messages]
        reasons.append("Rejet automatique : " + "; ".join(m["message"] for m in conv.messages))
    else:
        if conv.status in (TC.PENDING_TANK_MAPPING, TC.GEOMETRIC_ONLY) or any(m["code"] == "CAPACITY_NOT_VALIDATED" for m in conv.messages):
            status = Obs.ValidationStatus.PENDING
            codes += [m["code"] for m in conv.messages if m["code"] != "CURVE_OUT_OF_RANGE"]
            reasons.append("; ".join(m["message"] for m in conv.messages))
        if guard is not None and guard[0] != IN_SCOPE:
            status = Obs.ValidationStatus.PENDING
            codes.append(guard[0])
            reasons.append(guard[1])
            messages.append({"code": guard[0], "message": guard[1]})
        for f in flags:
            status = Obs.ValidationStatus.PENDING
            codes.append(f["code"])
            reasons.append(f["message"])

    codes = list(dict.fromkeys(codes))
    if status == Obs.ValidationStatus.VALIDATED:
        reason = "Contrôles automatiques réussis : conversion valide, capacité contrôlée" + (", périmètre de gardiennage conforme." if guard else ".")
    elif status == Obs.ValidationStatus.PENDING:
        reason = " | ".join(f"{issue_detail(c)['anomalie']} → {issue_detail(c)['action']}" for c in codes) or " | ".join(r for r in reasons if r)
    else:
        reason = " | ".join(r for r in reasons if r)

    obs = Obs(
        source_type=inp.source_type, source_event_id=inp.source_event_id[:128], site_id=inp.site_id,
        tank_id=conv.tank_id or inp.tank_id, observation_datetime=inp.observation_datetime,
        level_raw=None if inp.level_raw is None else str(inp.level_raw)[:128],
        level_value=TC.to_decimal(inp.level_value), level_unit=inp.level_unit if inp.level_unit in ("L", "CM") else None,
        level_liters=TC.to_decimal(conv.level_liters), conversion_method=conv.method, conversion_status=conv.status,
        conversion_message="; ".join(m["message"] for m in conv.messages) or None,
        capacity_l=TC.to_decimal(conv.capacity_l), hour_meter=TC.to_decimal(inp.hour_meter),
        volume_geometric_l=TC.to_decimal(geometric), volume_calibrated_l=TC.to_decimal(calibrated),
        declared_liters=TC.to_decimal(declared), level_cm=TC.to_decimal(level_cm), cm_liters_gap_l=TC.to_decimal(gap),
        fill_pct=TC.to_decimal(conv.fill_pct), conversion_rule=(rule or None) and rule[:255], tank_model_ref=conv.model_ref,
        source_user=(inp.source_user or None) and str(inp.source_user)[:255],
        source_reference=(inp.source_reference or None) and str(inp.source_reference)[:255],
        guard_scope_status=guard[0] if guard else None, comment=inp.comment,
        proof_reference=(inp.proof_reference or None) and str(inp.proof_reference)[:512],
        validation_status=status, review_flags=flags, issue_codes=codes, validation_reason=reason,
    )
    return obs, messages


def same_source_conflict(obs) -> dict | None:
    """Autre relevé déjà importé (même source, site, cuve, date) avec une valeur différente."""
    from fuel_tracking.models import FuelStockObservation as Obs

    others = Obs.objects.filter(source_type=obs.source_type, site_id=obs.site_id, tank_id=obs.tank_id,
                                observation_datetime=obs.observation_datetime).exclude(source_event_id=obs.source_event_id)
    for o in others:
        if (o.level_value, o.level_unit) != (obs.level_value, obs.level_unit):
            return flag("SAME_SOURCE_CONFLICT", f"Valeur différente d'un relevé {obs.source_type} déjà importé pour la même date "
                                                f"({o.level_raw} — relevé #{o.pk}).")
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
            "reason": None if status == COHERENT else f"Écart {diff:.0f} L > tolérance {tol:.0f} L : aucune moyenne automatique."}


def site_capacity(tanks: list[TC.TankSpec], tank_id: str | None) -> float | None:
    validated = [t for t in tanks if t.validated]
    if tank_id:
        for t in validated:
            if str(t.tank_id).upper() == str(tank_id).upper():
                return t.check_capacity_l()
        return None
    if len(validated) == 1:
        return validated[0].check_capacity_l()
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


def guard_conflict_priority() -> str | None:
    """Prestataire prioritaire configuré en cas de conflit (FUEL_GUARD_CONFLICT_PRIORITY) ; vide → exception."""
    v = str(getattr(settings, "FUEL_GUARD_CONFLICT_PRIORITY", "") or "").strip().upper()
    return v if v in GUARD_SOURCES else None


def apply_guard_conflicts(site_ids) -> int:
    """
    STOCK_CONFLICT : sans priorité configurée, les deux relevés auto-validés passent À TRAITER ; avec une
    priorité, seul le relevé de l'autre prestataire passe À TRAITER. Jamais un relevé validé par un humain.
    """
    from fuel_tracking.models import FuelStockObservation as Obs

    priority = guard_conflict_priority()
    changed = 0
    for site_id in set(site_ids):
        for c in guard_comparisons(site_id):
            if c["status"] != STOCK_CONFLICT:
                continue
            ids = [c["loliforce"]["id"], c["scss"]["id"]]
            if priority:
                ids = [c["scss" if priority == "LOLIFORCE" else "loliforce"]["id"]]
            for oid in ids:
                o = Obs.objects.get(pk=oid)
                if o.validation_status == "VALIDATED" and o.validated_by_id is None:
                    reason = f"Conflit Loliforce / SCSS : {c['reason']}" + (f" Priorité configurée : {priority}." if priority else "")
                    log_correction("OBSERVATION", o.pk, "SYSTEM", reason, site_id=o.site_id, field="validation_status",
                                   old=o.validation_status, new="PENDING", author_label="système (comparaison gardiennage)")
                    o.validation_status = "PENDING"
                    o.issue_codes = list(dict.fromkeys(list(o.issue_codes or []) + ["STOCK_CONFLICT"]))
                    o.validation_reason = f"{issue_detail('STOCK_CONFLICT')['anomalie']} → {issue_detail('STOCK_CONFLICT')['action']}. {c['reason']}"
                    o.save(update_fields=["validation_status", "validation_reason", "issue_codes", "updated_at"])
                    changed += 1
    return changed


# ── Stock retenu ─────────────────────────────────────────────────────────────

def _f(v):
    return float(v) if v is not None else None


UNAVAILABLE_NO_OBS = "Aucun relevé de stock pour ce site."
UNAVAILABLE_NONE_VALIDATED = "Relevé(s) présent(s) mais aucun relevé validé exploitable en litres."
UNAVAILABLE_NO_TANK = "Aucune cuve validée pour ce site : aucun stock réel ne peut être retenu."
DEFAULT_PRIORITY = ("TERRAIN_PREUVE", "LOLIFORCE", "SCSS", "ENOC")


def source_priority() -> tuple:
    return tuple(getattr(settings, "FUEL_STOCK_SOURCE_PRIORITY", DEFAULT_PRIORITY) or DEFAULT_PRIORITY)


def _rank(o: dict, prio: tuple) -> int:
    keys = (["TERRAIN_PREUVE"] if o.get("proof_reference") else []) + [o["source_type"]]
    return min((prio.index(k) for k in keys if k in prio), default=len(prio))


def fuel_stock_retained(site_ids=None, now=None, as_of=None) -> dict[str, dict]:
    """
    Stock retenu par site : site identifié, cuve site VALIDÉE, litres natifs ou conversion validée, date connue,
    capacité contrôlée, statut AUTO_VALIDÉ ou VALIDÉ_MANUELLEMENT. Le jour le plus récent est retenu ; entre
    relevés du même jour, la priorité configurée (FUEL_STOCK_SOURCE_PRIORITY) départage. Snowflake / Closing exclus.
    """
    from fuel_tracking.models import FuelStockObservation as Obs

    now = now or timezone.now()
    prio = source_priority()
    qs = Obs.objects.exclude(source_type__in=("SNOWFLAKE", "CLOSING"))
    if site_ids is not None:
        qs = qs.filter(site_id__in=list(site_ids))
    if as_of is not None:  # stock retenu à une date passée (bilan stock)
        qs = qs.filter(observation_datetime__lte=as_of)
    fields = ("id", "site_id", "tank_id", "source_type", "observation_datetime", "level_raw", "level_value", "level_unit",
              "level_liters", "conversion_method", "conversion_status", "validation_status", "validation_reason",
              "capacity_l", "proof_reference", "validated_by_id", "volume_geometric_l", "volume_calibrated_l",
              "conversion_rule", "tank_model_ref", "issue_codes")
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
        if not validated_tanks:
            out[site_id] = _unavailable(site_id, latest_any, UNAVAILABLE_NO_TANK, now)
            continue
        candidates = []
        for o in obs:
            if o["validation_status"] != "VALIDATED" or o["level_liters"] is None or o["conversion_status"] != "VALID":
                continue
            cap = site_capacity(tanks, o["tank_id"])
            if cap is not None and float(o["level_liters"]) > cap * (1 + tol / 100):
                continue  # volume au-delà de la capacité : jamais retenu
            candidates.append({**o, "_cap": cap})
        if not candidates:
            out[site_id] = _unavailable(site_id, latest_any, UNAVAILABLE_NONE_VALIDATED, now)
            continue

        day = candidates[0]["observation_datetime"].date()
        same_day = sorted((c for c in candidates if c["observation_datetime"].date() == day),
                          key=lambda c: (_rank(c, prio), -c["observation_datetime"].timestamp()))
        best = same_day[0]
        unknown = [o for o in obs if o["observation_datetime"].date() == day and o["validation_status"] in ("PENDING", "VALIDATED")
                   and set(o.get("issue_codes") or []) & {"TANK_UNKNOWN", "MULTI_TANK_WITHOUT_ID"}]
        if unknown:
            out[site_id] = _unavailable(site_id, unknown[0], f"Relevé du {day:%d/%m/%Y} visant une cuve non référencée ou non "
                                                             "identifiée : stock du site incomplet (cuves à identifier).", now)
            continue
        if len(validated_tanks) > 1:
            per_tank = {}
            for c in same_day:
                if c["tank_id"]:
                    per_tank.setdefault(str(c["tank_id"]).upper(), c)
            missing = [t.tank_id for t in validated_tanks if str(t.tank_id).upper() not in per_tank]
            if missing:
                out[site_id] = _unavailable(site_id, best,
                                            f"Relevé incomplet le {day:%d/%m/%Y} : cuve(s) {', '.join(missing)} sans relevé validé ce jour-là.", now)
                continue
            parts = list(per_tank.values())
            stock = sum(float(p["level_liters"]) for p in parts)
            cap_total = sum((t.check_capacity_l() or 0) for t in validated_tanks) or None
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
            "conversion_rule": best["conversion_rule"],
            "tank_model_ref": best["tank_model_ref"],
            "volume_geometric_l": _f(best["volume_geometric_l"]),
            "volume_calibrated_l": _f(best["volume_calibrated_l"]),
            "validation_status": best["validation_status"],
            "statut_releve": display_status(best),
            "capacity_l": round(cap_total, 1) if cap_total else None,
            "fill_pct": round(stock / cap_total * 100, 1) if cap_total else None,
            "tanks_count": len(parts),
            "proof_reference": best["proof_reference"],
            "unavailable_reason": None,
            "capacity_checked": cap_total is not None,
            "priority_rank": _rank(best, prio),
        }
    return out


def _unavailable(site_id, o, reason, now):
    detail = None
    if o and o["validation_status"] != "VALIDATED":
        codes = o.get("issue_codes") or []
        if codes:
            detail = "; ".join(f"{issue_detail(c)['anomalie']} → {issue_detail(c)['action']}" for c in codes)
        elif o.get("validation_reason"):
            detail = o["validation_reason"]
    return {
        "site_id": site_id, "stock_l": None, "source_type": o["source_type"] if o else None,
        "observation_id": o["id"] if o else None, "observation_ids": [],
        "observation_datetime": o["observation_datetime"] if o else None,
        "age_hours": round((now - o["observation_datetime"]).total_seconds() / 3600, 1) if o else None,
        "level_raw": o["level_raw"] if o else None, "level_unit": o["level_unit"] if o else None,
        "conversion_method": o["conversion_method"] if o else None, "conversion_status": o["conversion_status"] if o else None,
        "conversion_rule": o.get("conversion_rule") if o else None, "tank_model_ref": o.get("tank_model_ref") if o else None,
        "volume_geometric_l": _f(o.get("volume_geometric_l")) if o else None,
        "volume_calibrated_l": _f(o.get("volume_calibrated_l")) if o else None,
        "validation_status": o["validation_status"] if o else None,
        "statut_releve": display_status(o) if o else None,
        "capacity_l": _f(o.get("capacity_l")) if o else None,
        "fill_pct": None, "tanks_count": 0, "proof_reference": None,
        "unavailable_reason": reason + (f" Dernier relevé : {detail}" if detail else ""),
        "capacity_checked": False, "issue_codes": (o.get("issue_codes") or []) if o else [],
    }


# ── Actions humaines (valider / rejeter / corriger) ─────────────────────────

@transaction.atomic
def validate_observation(obs, user, reason: str, validate: bool = True):
    """
    Validation / rejet manuel (VALIDÉ_MANUELLEMENT : identité, date, motif). Un volume géométrique seul
    (plaque non confirmée) peut être retenu par une validation EXPLICITE, tracée comme telle.
    """
    new = "VALIDATED" if validate else "REJECTED"
    fields = ["validation_status", "validation_reason", "validated_by", "validated_at", "updated_at"]
    if validate:
        if obs.conversion_status == "GEOMETRIC_ONLY" and obs.volume_geometric_l is not None and obs.level_unit == "CM":
            log_correction("OBSERVATION", obs.pk, "VALIDATE", reason, site_id=obs.site_id, field="level_liters",
                           old=obs.level_liters, new=obs.volume_geometric_l, user=user)
            obs.level_liters = obs.volume_geometric_l
            obs.conversion_status = "VALID"
            obs.conversion_rule = ((obs.conversion_rule or "") + " — volume géométrique retenu par validation manuelle")[:255]
            fields += ["level_liters", "conversion_status", "conversion_rule"]
        elif obs.level_liters is None or obs.conversion_status != "VALID":
            raise ValueError("Relevé non convertible en litres (cuve à référencer ou valider d'abord) : validation impossible.")
    log_correction("OBSERVATION", obs.pk, "VALIDATE" if validate else "REJECT", reason, site_id=obs.site_id,
                   field="validation_status", old=obs.validation_status, new=new, user=user)
    obs.validation_status = new
    obs.validation_reason = reason
    obs.validated_by = user
    obs.validated_at = timezone.now()
    obs.save(update_fields=fields)
    return obs


@transaction.atomic
def validate_movement(mv, user, reason: str, validate: bool = True):
    new = "VALIDATED" if validate else "REJECTED"
    if validate and (mv.quantity_l is None or mv.movement_type is None):
        raise ValueError("Mouvement sans quantité réalisée ou sans type : validation impossible.")
    if validate and mv.source_category in ("DEMANDE_LIVRAISON", "CARTE_TOTAL", "INCONNU", "LIVRAISON_DECLAREE"):
        raise ValueError("Une demande, une carte Total, une livraison déclarée ou un type inconnu n'est jamais un mouvement réel "
                         "sans rapprochement : utiliser le rapprochement ENOC.")
    if validate and mv.source_type == "ENOC" and not mv.retained_in_stock:
        raise ValueError("Mouvement ENOC non éligible au stock réel : le rapprochement ENOC est la seule voie de validation.")
    log_correction("MOVEMENT", mv.pk, "VALIDATE" if validate else "REJECT", reason, site_id=mv.site_id,
                   field="status", old=mv.status, new=new, user=user)
    mv.status = new
    mv.validation_reason = reason
    mv.validated_by = user
    mv.validated_at = timezone.now()
    mv.save(update_fields=["status", "validation_reason", "validated_by", "validated_at", "updated_at"])
    return mv


TANK_EDITABLE = ("shape", "shape_family", "dimension_1_cm", "dimension_2_cm", "dimension_3_cm", "bottom_profile", "bottom_z_cm",
                 "top_profile", "top_z_cm", "length_m", "width_m", "height_m", "diameter_m", "capacity_l", "capacity_plate_l",
                 "capacity_plate_source", "serial_number", "number_of_tanks_on_site", "proof_reference", "calibration_curve",
                 "calibration_validated", "active", "comment", "tank_model", "model_match_status")
NUMERIC_TANK = ("length_m", "width_m", "height_m", "diameter_m", "capacity_l", "capacity_plate_l", "dimension_1_cm",
                "dimension_2_cm", "dimension_3_cm", "bottom_z_cm", "top_z_cm")


def tank_status_for(spec: TC.TankSpec, number_of_tanks: int | None = None) -> tuple[str, str]:
    """Statut d'affectation déduit des seules données (hors validation) : PROPOSEE / A_CONFIRMER / INCOMPLETE / PLUSIEURS…"""
    if number_of_tanks and number_of_tanks > 1 and not spec.tank_id:
        return "PLUSIEURS_CUVES_A_IDENTIFIER", "Plusieurs cuves mentionnées sans identifiant."
    if spec.has_geometry():
        return "PROPOSEE", "Forme et cotes reconnues par le calculateur : à confirmer."
    if spec.plate_capacity_l or spec.declared_capacity_l:
        return "A_CONFIRMER", "Capacité connue mais forme / cotes insuffisantes."
    return "INCOMPLETE", "Aucune information cuve exploitable."


@transaction.atomic
def correct_tank(tank, changes: dict, user, reason: str, validate: bool | None = None):
    """Correction d'une cuve site (chaque champ modifié est tracé) ; validate=True/False valide ou rejette la cuve."""
    from fuel_tracking.models import FuelTankModel

    for field, value in changes.items():
        if field not in TANK_EDITABLE:
            raise ValueError(f"Champ non modifiable : {field}")
        if field == "tank_model":
            value = FuelTankModel.objects.filter(tank_model_id=value).first() if value else None
            old = tank.tank_model.tank_model_id if tank.tank_model_id else None
            new = value.tank_model_id if value else None
            if old != new:
                log_correction("TANK", tank.pk, "CORRECT", reason, site_id=tank.site_id, field="tank_model", old=old, new=new, user=user)
                tank.tank_model = value
                if value is not None and "model_match_status" not in changes:
                    tank.model_match_status = "CONFIRMED"
            continue
        old = getattr(tank, field)
        if field in NUMERIC_TANK and value not in (None, ""):
            value = Decimal(str(value))
            if value <= 0:
                raise ValueError(f"{field} doit être strictement positif.")
        if field == "number_of_tanks_on_site" and value not in (None, ""):
            value = int(value)
        if value == "":
            value = None
        if str(old) != str(value):
            log_correction("TANK", tank.pk, "CORRECT", reason, site_id=tank.site_id, field=field, old=old, new=value, user=user)
            setattr(tank, field, value)
    if any(f in changes for f in ("shape", "shape_family")) and tank.shape_family:
        tank.shape = TC.FAMILY_TO_SHAPE.get(tank.shape_family, tank.shape)
    if tank.tank_model_id and tank.model_match_status == "CONFIRMED" and ("tank_model" in changes or "model_match_status" in changes):
        _apply_confirmed_model(tank, user, reason)
    if validate is not None:
        spec = TC.TankSpec.from_model(tank)
        if validate:
            if not spec.family and not (tank.calibration_curve and tank.calibration_validated):
                raise ValueError("Forme de cuve manquante : validation impossible.")
            if not (tank.calibration_curve and tank.calibration_validated) and not spec.has_geometry():
                raise ValueError("Cotes incomplètes pour la forme de la cuve : validation impossible.")
            new = "VALIDEE"
        else:
            new = "REJETEE"
        log_correction("TANK", tank.pk, "VALIDATE" if validate else "REJECT", reason, site_id=tank.site_id,
                       field="validation_status", old=tank.validation_status, new=new, user=user)
        tank.validation_status = new
        tank.validation_reason = reason
        tank.validated_by = user
        tank.validated_at = timezone.now()
    elif tank.validation_status not in ("VALIDEE", "REJETEE") and changes:
        st, why = tank_status_for(TC.TankSpec.from_model(tank), tank.number_of_tanks_on_site)
        tank.validation_status, tank.validation_reason = st, why
    tank.save()
    return tank


def _apply_confirmed_model(tank, user, reason: str):
    """Modèle confirmé : la cuve site adopte la géométrie et la capacité de plaque du modèle (chaque champ tracé)."""
    m = tank.tank_model
    copy = {"shape_family": m.shape_code, "dimension_1_cm": m.dimension_1_cm, "dimension_2_cm": m.dimension_2_cm,
            "dimension_3_cm": m.dimension_3_cm, "bottom_profile": m.bottom_profile, "bottom_z_cm": m.bottom_z_cm,
            "top_profile": m.top_profile, "top_z_cm": m.top_z_cm}
    if m.nominal_capacity_l and not tank.capacity_plate_l:
        copy["capacity_plate_l"] = m.nominal_capacity_l
        copy["capacity_plate_source"] = f"Plaque du modèle {m.tank_model_id} (confirmé)"
    for f, v in copy.items():
        old = getattr(tank, f)
        if str(old) != str(v):
            log_correction("TANK", tank.pk, "CORRECT", f"Modèle {m.tank_model_id} confirmé — {reason}", site_id=tank.site_id,
                           field=f, old=old, new=v, user=user)
            setattr(tank, f, v)
    tank.shape = TC.FAMILY_TO_SHAPE.get(m.shape_code, tank.shape)


def reconvert_site_observations(site_id: str, user=None) -> int:
    """Après correction / validation d'une cuve ou du gardiennage : reconvertit les relevés du site non validés humainement."""
    from fuel_tracking.models import FuelStockObservation as Obs

    tanks = site_tanks([site_id]).get(site_id, [])
    scopes = guard_scopes_by_site([site_id])
    n = 0
    for o in Obs.objects.filter(site_id=site_id, validated_by__isnull=True).exclude(source_type__in=("SNOWFLAKE", "CLOSING")) \
            .exclude(validation_status="NON_EXPLOITABLE"):
        guard = guard_scope_status(site_id, o.source_type, o.observation_datetime, scopes) if o.source_type in GUARD_SOURCES else None
        cm_extra = _f(o.level_cm) if o.level_unit == "L" else None
        inp = ObservationInput(o.source_type, o.source_event_id, o.site_id, o.observation_datetime, o.level_raw,
                               _f(o.level_value), o.level_unit, o.tank_id,
                               review_flags=[f for f in _flags(o.review_flags) if f["code"] not in ("CM_L_GAP",)], cm_value=cm_extra)
        new, _ = build_observation(inp, tanks, guard)
        # Le statut de gardiennage et les anomalies comptent aussi : un site qui entre dans un périmètre perd son
        # anomalie « gardiennage non configuré » même si le relevé reste à valider (cuve seulement proposée).
        before = (o.tank_id, _f(o.level_liters), o.conversion_status, o.validation_status, _f(o.volume_geometric_l),
                  o.guard_scope_status, sorted(set(o.issue_codes or []) - {"STOCK_CONFLICT"}))
        after = (new.tank_id, _f(new.level_liters), new.conversion_status, new.validation_status, _f(new.volume_geometric_l),
                 new.guard_scope_status, sorted(set(new.issue_codes or []) - {"STOCK_CONFLICT"}))
        if before != after:
            log_correction("OBSERVATION", o.pk, "SYSTEM", "Reconversion après modification du référentiel cuve / gardiennage",
                           site_id=site_id, field="cuve/litres/conversion/statut/géométrique", old=before, new=after, user=user,
                           author_label=None if user else "système")
            for f in ("tank_id", "level_liters", "conversion_method", "conversion_status", "conversion_message", "capacity_l",
                      "validation_status", "validation_reason", "guard_scope_status", "volume_geometric_l", "volume_calibrated_l",
                      "cm_liters_gap_l", "fill_pct", "conversion_rule", "tank_model_ref", "issue_codes", "review_flags"):
                setattr(o, f, getattr(new, f))
            o.save()
            n += 1
    apply_guard_conflicts([site_id])
    return n
