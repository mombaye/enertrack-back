# sonatel_billing/utils.py
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

FR_WS = {" ", "\u00A0"}  # espaces & espaces insécables

def parse_decimal_fr(x):
    """'458 543' -> Decimal('458543'); '48 763' -> 48763; ''/None -> None."""
    if x is None:
        return None
    s = str(x).strip()
    if not s:
        return None
    # remplacer virgule décimale FR par point
    s = s.replace(",", ".")
    # enlever les séparateurs de milliers (espaces)
    for ws in FR_WS:
        s = s.replace(ws, "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None

def month_start(d: date) -> date:
    return d.replace(day=1)

def month_end(d: date) -> date:
    # dernier jour du mois: aller au 1er du mois suivant puis -1 jour
    if d.month == 12:
        nxt = d.replace(year=d.year+1, month=1, day=1)
    else:
        nxt = d.replace(month=d.month+1, day=1)
    return nxt - timedelta(days=1)

def iter_month_slices(start: date, end: date):
    """Génère (year, month, slice_start, slice_end, days_in_month, days_covered)."""
    cur = start
    while cur <= end:
        ms = month_start(cur)
        me = month_end(cur)
        seg_start = cur
        seg_end = min(me, end)
        days_covered = (seg_end - seg_start).days + 1
        days_in_this_month = (me - ms).days + 1
        yield (
            seg_start.year,
            seg_start.month,
            seg_start,
            seg_end,
            days_in_this_month,
            days_covered,
        )
        cur = seg_end + timedelta(days=1)


