# fuel_tracking/services/fuel_observation_import.py
"""
Import du fichier d'observation standard (instruction §8). Une cellule vide
reste NULL — jamais 0. Une valeur illisible rejette la ligne avec un motif
plutôt que d'être convertie. Traçabilité : fichier, utilisateur, date, version
de règle (FuelObservationImport).
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.db import transaction

from fuel_tracking.services.cph_engine import RULE_VERSION

COLUMNS = [
    "country", "site_id", "site_name", "observation_start", "observation_end",
    "opening_fuel_l", "closing_fuel_l", "fuel_deliveries_l", "fuel_transfer_in_l",
    "fuel_transfer_out_l", "fuel_theft_l", "fuel_drain_l", "observation_status",
    "comment", "justificatif",
]
DECIMAL_COLUMNS = ["opening_fuel_l", "closing_fuel_l", "fuel_deliveries_l", "fuel_transfer_in_l",
                   "fuel_transfer_out_l", "fuel_theft_l", "fuel_drain_l"]


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def parse_decimal(v) -> Decimal | None:
    if _blank(v):
        return None
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v))
    s = str(v).strip().replace(" ", "").replace(" ", "")
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except InvalidOperation:
        raise ValueError(f"nombre illisible {v!r}")


def parse_date(v) -> date | None:
    if _blank(v):
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"date illisible {v!r}")


def read_rows(file_name: str, content: bytes) -> list[dict]:
    name = file_name.lower()
    if name.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        raw = list(wb.worksheets[0].iter_rows(values_only=True))
    elif name.endswith(".csv"):
        text = content.decode("utf-8-sig")
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,\t")
        raw = list(csv.reader(io.StringIO(text), dialect))
    else:
        raise ValueError("Format non supporté : .xlsx ou .csv attendu.")
    if not raw:
        raise ValueError("Fichier vide.")
    header = [str(h).strip().lower() if h is not None else "" for h in raw[0]]
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        raise ValueError(f"Colonnes manquantes : {', '.join(missing)}. Attendu : {', '.join(COLUMNS)}.")
    idx = {c: header.index(c) for c in COLUMNS}
    out = []
    for n, r in enumerate(raw[1:], start=2):
        if not r or all(_blank(x) for x in r):
            continue
        out.append({"_line": n, **{c: (r[i] if i < len(r) else None) for c, i in idx.items()}})
    return out


def import_observations(file_name: str, content: bytes, user=None):
    from fuel_tracking.models import FuelObservation, FuelObservationImport

    rows = read_rows(file_name, content)
    objs, errors = [], []
    for r in rows:
        try:
            site_id = str(r["site_id"]).strip() if not _blank(r["site_id"]) else None
            start, end = parse_date(r["observation_start"]), parse_date(r["observation_end"])
            if not site_id or start is None or end is None:
                raise ValueError("site_id, observation_start et observation_end sont obligatoires")
            if end < start:
                raise ValueError("observation_end antérieure à observation_start")
            values = {c: parse_decimal(r[c]) for c in DECIMAL_COLUMNS}
            negative = [c for c, v in values.items() if v is not None and v < 0]
            if negative:
                raise ValueError(f"valeur(s) négative(s) : {', '.join(negative)}")
        except ValueError as e:
            errors.append({"line": r["_line"], "site_id": r.get("site_id"), "error": str(e)})
            continue
        text = {c: (str(r[c]).strip() if not _blank(r[c]) else None)
                for c in ("country", "site_name", "observation_status", "comment", "justificatif")}
        objs.append(dict(site_id=site_id, observation_start=start, observation_end=end, **values, **text))

    with transaction.atomic():
        imp = FuelObservationImport.objects.create(
            file_name=file_name, uploaded_by=user if getattr(user, "is_authenticated", False) else None,
            rule_version=RULE_VERSION, rows_total=len(rows), rows_imported=len(objs),
            rows_rejected=len(errors), errors=errors[:500],
        )
        FuelObservation.objects.bulk_create([FuelObservation(obs_import=imp, **o) for o in objs], batch_size=1000)
    return imp
