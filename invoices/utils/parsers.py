import math
import decimal
import datetime
import pandas as pd
from django.utils.dateparse import parse_date



def safe_decimal(val):
    # Gère aussi NaN et None
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    try:
        return decimal.Decimal(str(val).replace(',', '.'))
    except (TypeError, decimal.InvalidOperation, ValueError):
        return None

def safe_float(val):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None

def safe_int(val):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def safe_date(val):
    """
    Gère les dates sous forme string, datetime, float Excel, ou None.
    """
    # Cas 1 : datetime.datetime ou datetime.date natif
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val if isinstance(val, datetime.date) else val.date()
    # Cas 2 : Numérique Excel (nombre de jours depuis 1899-12-30)
    if isinstance(val, (float, int)) and not pd.isna(val):
        # Excel start = 1899-12-30
        excel_start = datetime.datetime(1899, 12, 30)
        try:
            return (excel_start + datetime.timedelta(days=int(val))).date()
        except Exception:
            return None
    # Cas 3 : String (y compris format français 01/02/2025 ou ISO 2025-02-01)
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        # D'abord tente parse_date (ISO)
        d = parse_date(val)
        if d:
            return d
        # Puis tente format FR
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                return datetime.datetime.strptime(val, fmt).date()
            except Exception:
                continue
        # Ajoute ici d'autres formats si besoin
    return None


def safe_str(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return str(val).strip()