# fuel_tracking/services/stock_import_files.py
"""
Lecture et traitement des fichiers du registre stock (les lignes brutes sont chargées AVANT toute
interprétation — fuel_import_row.raw_payload — puis contrôlées une à une).

  LOLIFORCE / SCSS  : relevés des sociétés de gardiennage (une feuille par zone, blocs « SEMAINE n »
                      répétant l'en-tête). Colonnes : Site ID, Site Name, Catégorie, Zone, Ville, MARQUE GE,
                      TYPE DE GE, PUISSANCE GE, [CONTACT AGENT], Relevé en CM, Relevé en litres,
                      Compteur horaire (h), Date de relevé, Commentaires, Site gardé par.
                      → fuel_stock_observation (source LOLIFORCE / SCSS).
  CLOSING (.xlsb)   : feuille « Synthese conso fuel » — initialisation du référentiel cuves uniquement
                      (forme, dimensions, capacité ; GE et capteurs conservés en brut). Les quantités
                      calculées du closing ne sont PAS des relevés certifiés : aucune observation créée.
  HISTORICAL_REFUELING : feuille « Suivi refueling … » → fuel_movement (historique) ; feuille « Coef cuve »
                      → propositions de cuves (À VALIDER) pour les sites sans cuve référencée.

Cellules de niveau : « 11 cm », « 16,5CM », « 100 L », « Cuve 1=7 Cuve 2=11 », « C1: 8cm / C2: 24cm »
(cuves explicitement numérotées) ; « 7 et 11 », « 50/19 » (plusieurs valeurs SANS numéro de cuve →
jamais attribuées automatiquement) ; « Pas de jauge », « illisible », « cadran »… → niveau absent ;
« 3 Bidons 20L » → unité non gérée. Une donnée absente reste NULL, jamais 0.
"""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone

from django.utils import timezone

from fuel_tracking.services import stock_registry as R
from fuel_tracking.services import tank_conversion as TC

# ── Lecture générique xlsx / xlsb ─────────────────────────────────────────────


def iter_sheets(file_bytes: bytes, file_name: str, only: set[str] | None = None):
    """Itère (nom_feuille, lignes) — lignes = listes de valeurs ; xlsb via pyxlsb, sinon openpyxl."""
    if file_name.lower().endswith(".xlsb"):
        from pyxlsb import open_workbook

        with open_workbook(io.BytesIO(file_bytes)) as wb:
            for name in wb.sheets:
                if only and name.strip() not in only:
                    continue
                with wb.get_sheet(name) as sh:
                    yield name, ([c.v for c in row] for row in sh.rows())
        return
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    try:
        for ws in wb.worksheets:
            if only and ws.title.strip() not in only:
                continue
            yield ws.title, (list(r) for r in ws.iter_rows(values_only=True))
    finally:
        wb.close()


def _norm(h) -> str:
    return re.sub(r"\s+", " ", str(h or "")).strip().lower()


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, float) and v != v:
        return None
    return v


def excel_serial_to_date(v) -> date | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not 30000 < f < 60000:
        return None
    return (datetime(1899, 12, 30) + timedelta(days=f)).date()


def parse_date(v) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):
        return excel_serial_to_date(v)
    s = str(v).strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def as_utc_datetime(d: date) -> datetime:
    """Date métier (Africa/Dakar = UTC) → datetime aware à 00:00."""
    return datetime(d.year, d.month, d.day, tzinfo=dt_timezone.utc)


NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_number(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return None if v != v else float(v)
    s = str(v).strip().replace(" ", "").replace(" ", "")
    if not s:
        return None
    if re.fullmatch(r"-?\d+(?:[.,]\d+)?", s):
        return float(s.replace(",", "."))
    return None


def fingerprint(*parts) -> str:
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()


# ── Cellules de niveau ────────────────────────────────────────────────────────

MISSING_WORDS = ("illisible", "cadran", "jauge", "panne", "inconnu", "vide", "enlev", "solaire", "pas de ge",
                 "non affich", "parvenu", "reservoir", "réservoir", "manom", "il ne peut", "affichage", "panel",
                 "ge hs", "hs")
TANK_LABEL_RE = re.compile(r"c(?:uve)?\s*\(?\s*(\d)\s*\)?\s*[:=]?\s*(-?\d+(?:[.,]\d+)?)\s*(cm|l)?", re.I)


@dataclass
class Reading:
    value: float | None
    unit: str | None
    tank_id: str | None
    raw: str
    position: int = 1


@dataclass
class CellParse:
    readings: list[Reading] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    error: dict | None = None


def parse_level_cell(v, default_unit: str) -> CellParse:
    """Interprète une cellule « Relevé en CM » (default_unit=CM) ou « Relevé en litres » (default_unit=L)."""
    out = CellParse()
    if v is None or (isinstance(v, str) and not v.strip()):
        return out
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        out.readings.append(Reading(float(v), default_unit, None, str(v)))
        return out
    if isinstance(v, (datetime, date)):
        out.error = {"code": "COLUMN_SHIFT", "message": f"Date trouvée dans la colonne de niveau ({v}) : colonnes décalées."}
        return out
    raw = str(v).strip()
    low = raw.lower()
    if "bidon" in low:
        out.error = {"code": "UNKNOWN_UNIT", "message": f"Relevé en bidons ({raw}) : unité non gérée pour un niveau de cuve."}
        return out
    labelled = TANK_LABEL_RE.findall(raw)
    if labelled:
        for tank, num, unit in labelled:
            u = {"cm": "CM", "l": "L"}.get(unit.lower(), default_unit) if unit else default_unit
            out.readings.append(Reading(float(num.replace(",", ".")), u, tank, raw))
        return out
    nums = NUM_RE.findall(raw)
    unit = default_unit
    if re.search(r"\d\s*(cm)\b", low):
        unit = "CM"
    elif re.search(r"\d\s*l(itres?)?\b", low):
        unit = "L"
    if not nums:
        if any(w in low for w in MISSING_WORDS) or re.fullmatch(r"[\W_]*", raw):
            out.error = {"code": "MISSING_LEVEL", "message": f"Niveau non relevé : « {raw} »."}
        else:
            out.error = {"code": "UNKNOWN_UNIT", "message": f"Valeur de niveau non interprétable : « {raw} »."}
        return out
    rest = NUM_RE.sub("", low)
    rest = re.sub(r"\b(cm|l|litres?)\b|[\s.…/:=et]+", "", rest)
    if rest:
        out.error = {"code": "UNKNOWN_UNIT", "message": f"Valeur de niveau non interprétable : « {raw} »."}
        return out
    if len(nums) > 1:
        for i, n in enumerate(nums, start=1):
            out.readings.append(Reading(float(n.replace(",", ".")), unit, None, raw, position=i))
        out.messages.append({"code": "MULTI_TANK_WITHOUT_ID",
                             "message": f"{len(nums)} valeurs sans numéro de cuve (« {raw} ») : aucune attribution automatique."})
        return out
    out.readings.append(Reading(float(nums[0].replace(",", ".")), unit, None, raw))
    return out


# ── Lots : résultat de ligne ──────────────────────────────────────────────────

@dataclass
class RowOutcome:
    status: str  # ACCEPTED / WARNING / REJECTED / PENDING
    messages: list[dict]
    duplicate: bool = False


def _row_status(messages, created_statuses, duplicate_only: bool) -> str:
    if duplicate_only:
        return "WARNING"
    if not created_statuses:
        return "REJECTED"
    if any(s == "PENDING" for s in created_statuses):
        return "PENDING"
    if all(s == "REJECTED" for s in created_statuses):
        return "REJECTED"
    if messages or any(s == "REJECTED" for s in created_statuses):
        return "WARNING"
    return "ACCEPTED"


GUARD_ALIASES = {"LOLIFORCE": "LOLIFORCE", "LOLI FORCE": "LOLIFORCE", "SCSS": "SCSS"}


# ── Loliforce / SCSS ─────────────────────────────────────────────────────────

GUARD_COLS = {
    "site id": "site_id", "site name": "site_name", "relevé en cm": "cm", "relevé en litres": "l",
    "compteur horaire (h)": "hour_meter", "date de relevé": "date", "commentaires": "comment",
    "site gardé par": "guard", "contact agent": "contact",
}


SKIPPED_GUARD_SHEETS = {
    "compile": "Feuille de compilation (valeurs recopiées des feuilles de zone, litres calculés hors référentiel cuves) : "
               "non importée pour éviter doublons et conversions non tracées.",
}


def iter_guard_rows(file_bytes: bytes, file_name: str, skipped: dict | None = None):
    """
    (feuille, n° ligne, dict brut, dict normalisé) pour chaque ligne de données. Chaque bloc « SEMAINE n »
    répète son en-tête, parfois SANS la colonne Site ID (seul Site Name est présent) : l'en-tête est relu
    à chaque bloc et les colonnes sont toujours lues par leur nom, jamais par leur position.
    """
    for sheet, rows in iter_sheets(file_bytes, file_name):
        key = sheet.strip().lower()
        if key in SKIPPED_GUARD_SHEETS:
            if skipped is not None:
                skipped[sheet] = SKIPPED_GUARD_SHEETS[key]
            continue
        header = None
        for n, row in enumerate(rows, start=1):
            normed = [_norm(v) for v in (row or [])]
            if "relevé en cm" in normed or "site id" in normed:
                header = normed
                continue
            if header is None or not row:
                continue
            first = row[0] if row else None
            if first is None or (isinstance(first, str) and (not first.strip() or first.strip().upper().startswith("SEMAINE"))):
                continue
            raw = {}
            for i, h in enumerate(header):
                if h and i < len(row) and h not in raw:
                    raw[h] = row[i]
            rec = {GUARD_COLS[h]: v for h, v in raw.items() if h in GUARD_COLS}
            if not rec.get("site_id") and not rec.get("site_name"):
                continue
            yield sheet, n, {k: _jsonable(v) for k, v in raw.items()}, rec


def process_guard_file(batch, file_bytes: bytes, file_name: str) -> dict:
    from fuel_tracking.models import FuelImportRow, FuelStockObservation

    provider = batch.source_type
    resolver = R.SiteResolver()
    today = timezone.now().date()
    skipped: dict = {}
    rows = list(iter_guard_rows(file_bytes, file_name, skipped))
    site_ids = {resolver.resolve(r[3].get("site_id")) or resolver.resolve_by_name(r[3].get("site_name")) for r in rows} - {None}
    tanks_by_site = R.site_tanks(site_ids)
    scopes = R.guard_scopes_by_site(site_ids)
    existing_ids = set(FuelStockObservation.objects.filter(source_type=provider, site_id__in=site_ids)
                       .values_list("source_event_id", flat=True))
    seen_in_file: set[str] = set()
    stats = {"observations_creees": 0, "doublons": 0, "codes": {}, "feuilles_ignorees": skipped}
    touched_sites = set()

    for sheet, n, raw, rec in rows:
        messages: list[dict] = []
        if rec.get("site_id"):
            site_raw = str(rec.get("site_id")).strip()
            site_id = resolver.resolve(site_raw)
        else:
            site_raw = str(rec.get("site_name")).strip()
            site_id = resolver.resolve_by_name(site_raw)
            if site_id:
                messages.append({"code": "SITE_RESOLVED_BY_NAME",
                                 "message": f"Colonne Site ID absente de ce bloc : site retrouvé par son nom « {site_raw} » → {site_id}."})
        d = parse_date(rec.get("date"))
        row = FuelImportRow(batch=batch, sheet_name=sheet[:128], row_number=n, raw_payload=raw, site_id_raw=site_raw[:128],
                            site_id_resolved=site_id, event_datetime=as_utc_datetime(d) if d else None)

        def reject(code, message):
            messages.append({"code": code, "message": message})
            row.validation_status = "REJECTED"

        if site_id is None:
            reject("UNKNOWN_SITE", f"Site « {site_raw} » absent de Gestion des sites"
                   + ("." if rec.get("site_id") else " (Site ID absent, nom introuvable ou ambigu)."))
        elif resolver.empty:
            messages.append({"code": "REFERENTIAL_EMPTY", "message": "Référentiel des sites vide : site non contrôlé."})
        if d is None:
            reject("MISSING_DATE", f"Date de relevé absente ou illisible ({rec.get('date')!r}).")
        elif d > today + timedelta(days=1):
            reject("FUTURE_DATE", f"Date de relevé dans le futur ({d:%d/%m/%Y}).")
        elif d.year < 2020:
            reject("IMPLAUSIBLE_DATE", f"Date de relevé invraisemblable ({d:%d/%m/%Y}).")

        cm = parse_level_cell(rec.get("cm"), "CM")
        lt = parse_level_cell(rec.get("l"), "L")
        hour_meter = parse_number(rec.get("hour_meter"))
        if isinstance(rec.get("hour_meter"), (datetime, date)):
            reject("COLUMN_SHIFT", "Date trouvée dans la colonne compteur horaire : colonnes décalées.")
        readings = lt.readings or cm.readings
        if lt.readings and cm.readings:
            messages.append({"code": "CM_AND_L", "message": f"Deux relevés saisis (cm : {rec.get('cm')} ; L : {rec.get('l')}) : "
                             "le relevé en litres est utilisé, le relevé en cm est conservé en commentaire."})
        for p in (cm, lt):
            messages.extend(p.messages)
        if not readings:
            errors = [p.error for p in (cm, lt) if p.error]
            if errors:
                for e in errors:
                    reject(e["code"], e["message"])
            else:
                reject("MISSING_LEVEL", "Aucun niveau relevé (cm et litres vides).")

        guard_decl = GUARD_ALIASES.get(re.sub(r"\s+", " ", str(rec.get("guard") or "")).strip().upper())
        if rec.get("guard") and guard_decl and guard_decl != provider:
            messages.append({"code": "GUARD_DECLARED_MISMATCH", "message": f"Ligne déclarée « gardée par {rec.get('guard')} » dans un fichier {provider}."})

        created_statuses = []
        duplicate_only = False
        if row.validation_status != "REJECTED":
            row.validation_status = "PENDING"
            row.messages = messages
            row.row_fingerprint = fingerprint(provider, site_id, d, rec.get("cm"), rec.get("l"))
            row.save()
            dup_count = 0
            guard = R.guard_scope_status(site_id, provider, d, scopes)
            for rd in readings:
                event_id = "F:" + fingerprint(provider, site_id, d, rd.tank_id or f"#{rd.position}", rd.unit, rd.value)
                if event_id in existing_ids or event_id in seen_in_file:
                    dup_count += 1
                    continue
                seen_in_file.add(event_id)
                comment_parts = [str(rec.get("comment")).strip()] if rec.get("comment") and str(rec.get("comment")).strip() else []
                if lt.readings and cm.readings:
                    comment_parts.append(f"Relevé cm aussi saisi : {rec.get('cm')}")
                inp = R.ObservationInput(
                    source_type=provider, source_event_id=event_id, site_id=site_id, observation_datetime=as_utc_datetime(d),
                    level_raw=rd.raw, level_value=rd.value, level_unit=rd.unit, tank_id=rd.tank_id, hour_meter=hour_meter,
                    source_user=rec.get("contact"), source_reference=f"{file_name} · {sheet} · ligne {n}",
                    comment=" — ".join(comment_parts) or None,
                )
                obs, obs_msgs = R.build_observation(inp, tanks_by_site.get(site_id, []), guard)
                conflict = R.same_source_conflict(obs)
                if conflict:
                    obs.review_flags = [conflict]
                    if obs.validation_status == "VALIDATED":
                        obs.validation_status = "PENDING"
                        obs.validation_reason = conflict
                    obs_msgs.append({"code": "SAME_SOURCE_CONFLICT", "message": conflict})
                obs.batch = batch
                obs.import_row = row
                obs.save()
                touched_sites.add(site_id)
                stats["observations_creees"] += 1
                created_statuses.append(obs.validation_status)
                for m in obs_msgs:
                    if m not in messages:
                        messages.append(m)
            if dup_count and not created_statuses:
                duplicate_only = True
                messages.append({"code": "DUPLICATE_ALREADY_IMPORTED", "message": "Relevé déjà importé (doublon technique) : ignoré."})
                stats["doublons"] += 1
            row.validation_status = _row_status([m for m in messages if m["code"] not in ("REFERENTIAL_EMPTY",)],
                                                created_statuses, duplicate_only)
            row.messages = messages
            row.save(update_fields=["validation_status", "messages"])
        else:
            row.messages = messages
            row.row_fingerprint = fingerprint(provider, site_id or site_raw, d, rec.get("cm"), rec.get("l"))
            row.save()
        _count(stats, messages)

    stats["conflits_gardiennage_repasses_a_valider"] = R.apply_guard_conflicts(touched_sites)
    return stats


# ── Historique refueling ─────────────────────────────────────────────────────

REFUEL_COLS = {
    "n° ticket fms": "ticket", "site id": "site_id", "retrait": "direction", "source": "source",
    "date de la demande": "date", "responsable": "responsable", "source site id/dépôt- id destination": "counterpart",
    "qtt initiale mesurée sur site (l)": "init_l", "qtt initiale mesurée sur site (cm)": "init_cm",
    "quantité demandée (l)": "requested", "qtt finale mesurée sur sites (l)": "final_l",
    "qtt finale mesurée sur sites (cm)": "final_cm", "compteur horaire derniére refueling (h)": "hm_before",
    "compteur horaire actuel (h)": "hm_after", "cuve": "cuve", "motif transfert": "motif", "validé par": "validator",
    "statut": "status", "commentaire": "comment",
}


def movement_type_for(source: str | None, direction: str | None) -> str | None:
    src = (source or "").strip().upper()
    add = (direction or "").strip().lower().startswith("ajout")
    withdraw = (direction or "").strip().lower().startswith("retrait")
    if src == "EYDON" and add:
        return "DELIVERY_EYDON"
    if src == "TOTAL" and add:
        return "DELIVERY_TOTAL_CARD"
    if src == "PONCTION":
        return "TRANSFER_IN" if add else "TRANSFER_OUT" if withdraw else None
    if withdraw:
        return "WITHDRAWAL"
    return None


def _level_raw(l_val, cm_val) -> str | None:
    parts = []
    if l_val not in (None, ""):
        parts.append(f"{l_val} L")
    if cm_val not in (None, ""):
        parts.append(f"{cm_val} cm")
    return " / ".join(parts) or None


def process_refueling_file(batch, file_bytes: bytes, file_name: str) -> dict:
    from fuel_tracking.models import FuelMovement

    resolver = R.SiteResolver()
    existing = set(FuelMovement.objects.filter(source_type="HISTORICAL_REFUELING").values_list("source_event_id", flat=True))
    stats = {"mouvements_crees": 0, "doublons": 0, "cuves_proposees": 0, "codes": {}}
    for sheet, rows in iter_sheets(file_bytes, file_name):
        title = sheet.strip().lower()
        if title.startswith("suivi refueling"):
            _process_refuel_sheet(batch, sheet, rows, resolver, existing, stats, file_name)
        elif title == "coef cuve":
            stats["cuves_proposees"] += _process_tank_sheet(batch, sheet, rows, resolver, stats, only_new_sites=True)
    return stats


def _process_refuel_sheet(batch, sheet, rows, resolver, existing, stats, file_name):
    from fuel_tracking.models import FuelImportRow, FuelMovement

    header = None
    for n, row in enumerate(rows, start=1):
        if header is None:
            if row and any(_norm(v) == "n° ticket fms" for v in row):
                header = [_norm(v) for v in row]
            continue
        if not row or len(row) < 2 or not row[1]:
            continue
        raw = {}
        for i, h in enumerate(header):
            if h and i < len(row) and h not in raw:
                raw[h] = row[i]
        rec = {REFUEL_COLS[h]: v for h, v in raw.items() if h in REFUEL_COLS}
        messages = []
        site_raw = str(rec.get("site_id")).strip()
        site_id = resolver.resolve(site_raw)
        d = parse_date(rec.get("date"))
        ir = FuelImportRow(batch=batch, sheet_name=sheet[:128], row_number=n, raw_payload={k: _jsonable(v) for k, v in raw.items()},
                           site_id_raw=site_raw[:128], site_id_resolved=site_id, event_datetime=as_utc_datetime(d) if d else None)
        rejected = False
        if site_id is None:
            messages.append({"code": "UNKNOWN_SITE", "message": f"Site « {site_raw} » absent de Gestion des sites."})
            rejected = True
        if d is None:
            messages.append({"code": "MISSING_DATE", "message": "Date de la demande absente."})
            rejected = True
        mtype = movement_type_for(rec.get("source"), rec.get("direction"))
        if mtype is None:
            messages.append({"code": "MISSING_MOVEMENT_TYPE", "message": f"Type de mouvement indéterminé (source {rec.get('source')!r}, sens {rec.get('direction')!r})."})
        requested = parse_number(rec.get("requested"))
        ticket = rec.get("ticket")
        ticket = str(int(ticket)) if isinstance(ticket, float) and ticket.is_integer() else (str(ticket).strip() if ticket else None)
        if rejected:
            ir.validation_status = "REJECTED"
            ir.messages = messages
            ir.save()
            _count(stats, messages)
            continue
        event_id = (f"T:{ticket}:{site_id}:{mtype}" if ticket
                    else "F:" + fingerprint(site_id, d, rec.get("source"), rec.get("direction"), requested, rec.get("responsable")))
        ir.row_fingerprint = fingerprint(event_id)
        if event_id in existing:
            messages.append({"code": "DUPLICATE_ALREADY_IMPORTED", "message": "Mouvement déjà importé (doublon technique) : ignoré."})
            ir.validation_status = "WARNING"
            ir.messages = messages
            ir.save()
            stats["doublons"] += 1
            _count(stats, messages)
            continue
        existing.add(event_id)
        alerts = list(messages)
        src_status = (str(rec.get("status")).strip() if rec.get("status") else None)
        if mtype in ("DELIVERY_EYDON", "DELIVERY_TOTAL_CARD", "ADD"):
            if not ticket:
                alerts.append({"code": "DELIVERY_WITHOUT_TICKET", "message": "Livraison sans ticket FMS."})
            alerts.append({"code": "DELIVERY_WITHOUT_PROOF", "message": "Aucune preuve (photo / bon) dans le fichier historique."})
        if (src_status or "").lower() != "soldé":
            alerts.append({"code": "DELIVERY_REQUESTED_ONLY", "message": f"Demande non soldée (statut {src_status or 'vide'}) : quantité demandée seulement."})
        alerts.append({"code": "QUANTITY_NOT_DECLARED", "message": "Seule la quantité demandée est connue : quantité réalisée non déclarée (laissée vide)."})
        if requested is None:
            alerts.append({"code": "MOVEMENT_WITHOUT_QUANTITY", "message": "Mouvement sans quantité."})
        comment = " — ".join(str(x).strip() for x in (rec.get("motif"), rec.get("comment"),
                                                      f"Validé par {rec.get('validator')}" if rec.get("validator") else None,
                                                      f"Cuve : {rec.get('cuve')}" if rec.get("cuve") not in (None, "") else None) if x)
        ir.validation_status = "WARNING"
        ir.messages = alerts
        ir.save()
        FuelMovement.objects.create(
            source_type="HISTORICAL_REFUELING", source_event_id=event_id[:128], batch=batch, import_row=ir, site_id=site_id,
            counterpart_site_id=(str(rec.get("counterpart")).strip()[:64] if rec.get("counterpart") else None),
            movement_type=mtype, movement_datetime=as_utc_datetime(d), requested_quantity_l=TC.to_decimal(requested),
            quantity_l=None, level_before_raw=_level_raw(rec.get("init_l"), rec.get("init_cm")),
            level_after_raw=_level_raw(rec.get("final_l"), rec.get("final_cm")),
            hour_meter_before=TC.to_decimal(parse_number(rec.get("hm_before"))),
            hour_meter_after=TC.to_decimal(parse_number(rec.get("hm_after"))),
            supplier=("EYDON" if mtype == "DELIVERY_EYDON" else "TOTAL" if mtype == "DELIVERY_TOTAL_CARD" else None),
            ticket_reference=ticket, source_user=(str(rec.get("responsable"))[:255] if rec.get("responsable") else None),
            source_status=src_status, status="CLOSED" if (src_status or "").lower() == "soldé" else "DECLARED",
            alerts=alerts, comment=comment or None,
        )
        stats["mouvements_crees"] += 1
        _count(stats, alerts)


def _count(stats, messages):
    for m in messages:
        stats["codes"][m["code"]] = stats["codes"].get(m["code"], 0) + 1


# ── Référentiel cuves (Closing, Coef cuve) ───────────────────────────────────

SHAPES = {
    "cuve cylindrique horizontale": TC.HORIZONTAL_CYLINDER,
    "cuve cylindrique verticale": TC.VERTICAL_CYLINDER,
    "cuve parallélépipédique": TC.RECTANGULAR,
    "cuve cylindrique ellyptique horizontale": TC.HORIZONTAL_ELLIPTICAL,
    "cuve cylindrique elliptique horizontale": TC.HORIZONTAL_ELLIPTICAL,
}
TANK_COLS = {
    "id du site": "site_id", "forme géométrique cuve": "shape", "longueur [m]": "length", "largeur [m]": "width",
    "hauteur [m]": "height", "diamètre [m]": "diameter", "capacité cuve (l)": "capacity",
}
CLOSING_EXTRA = ("nombre de ge installé [ex: 1,2,3 …]", "marque ge installé #1", "type de ge #1", "puissance prp (kva) #1",
                 "fuel sensor fonctionnel et visible sur gfms [oui/non]")


def process_closing_file(batch, file_bytes: bytes, file_name: str) -> dict:
    resolver = R.SiteResolver()
    stats = {"cuves_creees": 0, "cuves_mises_a_jour": 0, "codes": {}}
    found = False
    for sheet, rows in iter_sheets(file_bytes, file_name, only={"Synthese conso fuel"}):
        found = True
        _process_tank_sheet(batch, sheet, rows, resolver, stats, only_new_sites=False)
    if not found:
        raise ValueError("Feuille « Synthese conso fuel » introuvable : fichier closing non reconnu.")
    return stats


def _dim(v):
    n = parse_number(v)
    return n if n is not None and n > 0 else None


def _process_tank_sheet(batch, sheet, rows, resolver, stats, only_new_sites: bool) -> int:
    from fuel_tracking.models import FuelImportRow, FuelTankMaster

    header = None
    per_site_count: dict[str, int] = {}
    pending_rows = []
    for n, row in enumerate(rows, start=1):
        if header is None:
            if n > 40:
                break
            normed = [_norm(v) for v in (row or [])]
            if "id du site" in normed and "forme géométrique cuve" in normed:
                header = normed
            continue
        if not row:
            continue
        raw = {}
        for i, h in enumerate(header):
            if h and i < len(row) and h not in raw:
                raw[h] = row[i]
        rec = {TANK_COLS[h]: v for h, v in raw.items() if h in TANK_COLS}
        if not rec.get("site_id") or not isinstance(rec.get("site_id"), str) or "_" not in rec["site_id"]:
            continue
        pending_rows.append((n, raw, rec))
        sid = rec["site_id"].strip().upper()
        if rec.get("shape"):
            per_site_count[sid] = per_site_count.get(sid, 0) + 1

    created = 0
    order: dict[str, int] = {}
    existing_sites = set(FuelTankMaster.objects.values_list("site_id", flat=True))
    for n, raw, rec in pending_rows:
        keep = {k: _jsonable(v) for k, v in raw.items() if k in TANK_COLS or k in CLOSING_EXTRA}
        site_raw = rec["site_id"].strip()
        site_id = resolver.resolve(site_raw)
        messages = []
        ir = FuelImportRow(batch=batch, sheet_name=sheet[:128], row_number=n, raw_payload=keep, site_id_raw=site_raw[:128],
                           site_id_resolved=site_id)
        shape_label = _norm(rec.get("shape"))
        if site_id is None:
            messages.append({"code": "UNKNOWN_SITE", "message": f"Site « {site_raw} » absent de Gestion des sites."})
            ir.validation_status = "REJECTED"
        elif not shape_label:
            messages.append({"code": "NO_TANK_INFO", "message": "Aucune forme / dimension de cuve sur cette ligne."})
            ir.validation_status = "ACCEPTED"
        elif shape_label not in SHAPES:
            messages.append({"code": "UNKNOWN_SHAPE", "message": f"Forme de cuve inconnue : « {rec.get('shape')} »."})
            ir.validation_status = "REJECTED"
        else:
            order[site_id] = order.get(site_id, 0) + 1
            tank_id = str(order[site_id])
            multi = per_site_count.get(site_raw.upper(), 0) > 1
            spec = dict(shape=SHAPES[shape_label], length_m=_dim(rec.get("length")), width_m=_dim(rec.get("width")),
                        height_m=_dim(rec.get("height")), diameter_m=_dim(rec.get("diameter")), capacity_l=_dim(rec.get("capacity")))
            t = TC.TankSpec(site_id, tank_id, **spec)
            try:
                geo = TC.geometric_volume_l(t, t.physical_height_cm() or 0)
            except TC.MissingDimensions:
                geo = None
                messages.append({"code": "MISSING_DIMENSIONS", "message": "Dimensions incomplètes pour la forme : conversion géométrique impossible."})
            if spec["capacity_l"] is None:
                messages.append({"code": "MISSING_CAPACITY", "message": "Capacité absente du fichier" + (f" (géométrie : {geo:.0f} L)." if geo else ".")})
            elif geo and abs(geo - spec["capacity_l"]) > 0.05 * spec["capacity_l"]:
                messages.append({"code": "CAPACITY_GEOMETRY_MISMATCH", "message": f"Capacité {spec['capacity_l']:.0f} L ≠ géométrie {geo:.0f} L."})
            if multi:
                messages.append({"code": "TANK_ORDER_TO_CONFIRM", "message": f"{per_site_count[site_raw.upper()]} cuves pour ce site : numérotation {tank_id} selon l'ordre du fichier, à confirmer."})
            if only_new_sites and site_id in existing_sites:
                messages.append({"code": "TANK_ALREADY_REFERENCED", "message": "Cuve(s) déjà référencée(s) pour ce site : proposition non appliquée."})
                ir.validation_status = "WARNING"
            else:
                ir.validation_status = "PENDING"
                ir.save()
                outcome = _upsert_tank(batch, site_id, tank_id, spec, ir, messages)
                if outcome == "created":
                    created += 1
                    stats["cuves_creees"] = stats.get("cuves_creees", 0) + 1
                elif outcome == "updated":
                    stats["cuves_mises_a_jour"] = stats.get("cuves_mises_a_jour", 0) + 1
                if outcome == "differs_validated":
                    ir.validation_status = "WARNING"
        ir.messages = messages
        ir.save()
        _count(stats, messages)
    return created


def _upsert_tank(batch, site_id, tank_id, spec, ir, messages) -> str:
    from fuel_tracking.models import FuelTankMaster

    tank = FuelTankMaster.objects.filter(site_id=site_id, tank_id=tank_id).first()
    label = f"import #{batch.pk} ({batch.source_type})"
    if tank is None:
        tank = FuelTankMaster.objects.create(site_id=site_id, tank_id=tank_id, source_type=batch.source_type, source_batch=batch,
                                             comment=f"Proposition {batch.source_type} — à valider.", **spec)
        R.log_correction("TANK", tank.pk, "CREATE", f"Création depuis {label}", site_id=site_id, author_label=label)
        return "created"
    changes = {k: v for k, v in spec.items() if v is not None and (getattr(tank, k) is None or float(getattr(tank, k)) != v)
               if k != "shape"}
    if spec["shape"] != tank.shape:
        changes["shape"] = spec["shape"]
    if not changes:
        return "unchanged"
    if tank.validation_status == "VALIDATED":
        messages.append({"code": "TANK_DIFFERS_FROM_VALIDATED",
                         "message": "Valeurs différentes de la cuve validée : cuve validée inchangée ("
                                    + ", ".join(f"{k} {getattr(tank, k)} → {v}" for k, v in changes.items()) + ")."})
        return "differs_validated"
    for k, v in changes.items():
        R.log_correction("TANK", tank.pk, "CORRECT", f"Mise à jour depuis {label}", site_id=site_id, field=k,
                         old=getattr(tank, k), new=v, author_label=label)
        setattr(tank, k, v)
    tank.source_batch = batch
    tank.save()
    return "updated"


PROCESSORS = {
    "LOLIFORCE": process_guard_file,
    "SCSS": process_guard_file,
    "CLOSING": process_closing_file,
    "HISTORICAL_REFUELING": process_refueling_file,
}
