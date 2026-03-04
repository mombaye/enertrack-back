# billing/utils_import.py
import math
import pandas as pd
from datetime import date,datetime
from decimal import Decimal, InvalidOperation
from calendar import monthrange

def _is_blank(v):
    return v is None or (isinstance(v, float) and math.isnan(v)) or str(v).strip() == ""

def parse_period(v):
    """
    Retourne (granularity, start, end)
    - DAY: 2026-01-15 -> (DAY, 2026-01-15, 2026-01-15)
    - MONTH: '2026-01' / '01/2026' -> (MONTH, 2026-01-01, 2026-01-31)
    """
    if _is_blank(v):
        return None, None, None

    # Excel Timestamp / datetime
    if isinstance(v, (pd.Timestamp, datetime)):
        d = v.date()
        return "DAY", d, d

    if isinstance(v, date):
        return "DAY", v, v

    s = str(v).strip()

    # YYYY-MM (mensuel)
    if len(s) == 7 and s[4] == "-":
        y = int(s[:4]); m = int(s[5:])
        last = monthrange(y, m)[1]
        return "MONTH", date(y, m, 1), date(y, m, last)

    # MM/YYYY (mensuel)
    if "/" in s and len(s.split("/")) == 2:
        a, b = s.split("/")
        if len(b) == 4:
            m = int(a); y = int(b)
            last = monthrange(y, m)[1]
            return "MONTH", date(y, m, 1), date(y, m, last)

    # fallback: date complète (journalier)
    try:
        d = pd.to_datetime(s, dayfirst=True, errors="raise").date()
        return "DAY", d, d
    except Exception:
        return None, None, None
        
def parse_decimal_fr(v) -> Decimal:
    if _is_blank(v): return Decimal("0")
    s = str(v).strip().replace(" ", "").replace("\u00A0", "").replace(",", ".")
    return Decimal(s)

def _to_contract_str(v):
    if _is_blank(v): return None
    if isinstance(v, int): return str(v)
    if isinstance(v, float):
        if math.isnan(v): return None
        return str(int(v))
    s = str(v).strip()
    if s.endswith(".0"): s = s[:-2]
    return s.strip() or None

def parse_period_date(v) -> date | None:
    """
    Accepte: date Excel, '2026-01', '01/2026', '2026-01-15'
    Retour: date (si mois → 1er jour)
    """
    if _is_blank(v): return None
    if isinstance(v, (pd.Timestamp,)):
        return v.date()

    s = str(v).strip()
    # YYYY-MM
    if len(s) == 7 and s[4] == "-":
        y = int(s[:4]); m = int(s[5:])
        return date(y, m, 1)
    # MM/YYYY
    if "/" in s and len(s.split("/")) == 2:
        a, b = s.split("/")
        if len(b) == 4:  # MM/YYYY
            m = int(a); y = int(b)
            return date(y, m, 1)

    try:
        dt = pd.to_datetime(s, dayfirst=True, errors="raise").date()
        # si l’utilisateur met une date, on peut garder la date
        return dt
    except Exception:
        return None
