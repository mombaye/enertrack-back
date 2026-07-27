import pandas as pd
from datetime import datetime

from .models import GridOutageDaily, GridOutageAlarm


# gridoutages/utils.py
import pandas as pd
from datetime import datetime

from .models import GridOutageDaily, GridOutageAlarm


def _read_tabular_file(file_obj):
    """
    Lit un fichier tabulaire uploadé (CSV ou Excel) et renvoie un DataFrame.
    - .csv  -> read_csv
    - .xls/.xlsx -> read_excel
    """
    name = getattr(file_obj, "name", "").lower()

    if name.endswith((".xls", ".xlsx")):
        # Fichier Excel
        return pd.read_excel(file_obj)
    else:
        # Fichier CSV (on reste souple sur l'encodage)
        return pd.read_csv(
            file_obj,
            sep=None,             # auto-détection
            engine="python",
            encoding="latin-1",   # évite beaucoup de UnicodeDecodeError
        )


def import_grid_outage_daily(file_obj):
    """
    Import file 1 (daily outage).
    Si la clé (site_id, param_name, date) existe => update.
    Sinon => create.
    """
    df = _read_tabular_file(file_obj)

    # Normalisation noms colonnes
    df.columns = [c.strip() for c in df.columns]

    created = 0
    updated = 0

    for _, row in df.iterrows():
        date_value = pd.to_datetime(row["Date"])

        obj, is_created = GridOutageDaily.objects.update_or_create(
            site_id=row["Site ID"],
            param_name=row["Param Name"],
            date=date_value,
            defaults={
                "country": row["Country"],
                "param_value": row["Param Value"],
                "measure": row["Measure"],
            },
        )
        if is_created:
            created += 1
        else:
            updated += 1

    return created, updated


def import_grid_outage_alarms(file_obj):
    """
    Import file 2 (alarm FMS).
    Clé de déduplication = ID.
    """
    df = _read_tabular_file(file_obj)
    df.columns = [c.strip() for c in df.columns]

    created = 0
    updated = 0

    for _, row in df.iterrows():
        date_start = pd.to_datetime(row["Date Start"])
        date_end = pd.to_datetime(row["Date End"]) if not pd.isna(row["Date End"]) else None

        obj, is_created = GridOutageAlarm.objects.update_or_create(
            id=row["ID"],
            defaults={
                "client": row["Client"],
                "site_id": row["Site ID"],
                "alarm_name": row["Alarm Name"],
                "alarm_code": row["Alarm ID"],
                "alarm_details": row.get("Alarm Details") or "",
                "equip_ip": row.get("Equip IP") or "",
                "equip_name": row.get("Equip Name") or "",
                "alarm_severity": row["Alarm Severity"],
                "status": row["Status"],
                "username": row.get("Username") or "",
                "date_start": date_start,
                "date_end": date_end,
                "result": row.get("Result") or "",
                "ticket_id": row.get("Ticket ID") or "",
            },
        )
        if is_created:
            created += 1
        else:
            updated += 1

    return created, updated
