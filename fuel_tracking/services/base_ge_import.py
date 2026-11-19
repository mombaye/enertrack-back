from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation

from django.db import transaction

from fuel_tracking.services.stock_import_files import _jsonable, _norm, iter_sheets
from fuel_tracking.services import stock_registry as R

BASE_GE_SOURCE = "BASE_GE"
BASE_GE_LABEL = "Base GE.xlsx"


def _text(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _header_index(row):
    headers = [_norm(value) for value in (row or [])]
    aliases = {
        "site_id": {"site id", "site_id", "siteid"},
        "type_ge": {"type de ge", "type ge", "type_de_ge", "genset type"},
    }
    found = {}
    for key, names in aliases.items():
        for index, header in enumerate(headers):
            if header in names:
                found[key] = index
                break
    return found


def process_base_ge_file(batch, file_bytes: bytes, file_name: str) -> dict:
    """Importe le dernier référentiel Base GE sans dépendre de Feuil32/positions fixes."""
    from fuel_tracking.models import FuelConsommationMonthly, FuelImportRow

    resolver = R.SiteResolver()
    rows = []
    seen_sites = Counter()
    header_found = False
    sheets_seen = []

    for sheet, sheet_rows in iter_sheets(file_bytes, file_name):
        sheets_seen.append(sheet)
        header = None
        for number, values in enumerate(sheet_rows, start=1):
            mapping = _header_index(values)
            if "site_id" in mapping and "type_ge" in mapping:
                header = mapping
                header_found = True
                continue
            if header is None or not values:
                continue
            site_raw = values[header["site_id"]] if header["site_id"] < len(values) else None
            type_raw = values[header["type_ge"]] if header["type_ge"] < len(values) else None
            site = _text(site_raw)
            ge_type = _text(type_raw)
            if not site and not ge_type:
                continue
            raw = {"site_id": _jsonable(site_raw), "type_ge": _jsonable(type_raw)}
            resolved = resolver.resolve(site)
            messages = []
            status = "ACCEPTED"
            if not site:
                status = "REJECTED"
                messages.append({"code": "MISSING_SITE_ID", "message": "Colonne Site ID vide."})
            elif not ge_type:
                status = "WARNING"
                messages.append({"code": "EMPTY_TYPE_GE", "message": "Type de GE vide : le repli ENOC/Snowflake sera utilisé."})
            if resolved is None and site:
                status = "WARNING"
                messages.append({"code": "UNKNOWN_SITE", "message": f"Site « {site} » absent de Gestion des sites ; conservé dans le rapport."})
            if site:
                seen_sites[site.upper()] += 1
            row = FuelImportRow(
                batch=batch, sheet_name=sheet[:128], row_number=number,
                raw_payload=raw, site_id_raw=site[:128] if site else None,
                site_id_resolved=resolved,
                validation_status=status, messages=messages,
            )
            row.save()
            rows.append((site, resolved, ge_type, messages, status))

    if not header_found:
        raise ValueError("Colonnes obligatoires introuvables : Site ID et Type de GE.")

    duplicate_sites = {site for site, count in seen_sites.items() if count > 1}
    for site, _resolved, _ge_type, messages, status in rows:
        if site and site.upper() in duplicate_sites:
            messages.append({"code": "DUPLICATE_SITE_ID", "message": "Site présent plusieurs fois : la dernière ligne du fichier fait foi."})
            if status == "ACCEPTED":
                status = "WARNING"

    # Le dernier fichier accepté remplace entièrement le type Base GE précédent.
    with transaction.atomic():
        FuelConsommationMonthly.objects.filter(fichier_source=BASE_GE_LABEL).update(
            type_ge_fichier=None, fichier_source=None
        )
        updated = created = 0
        for site, resolved, ge_type, messages, status in rows:
            if not resolved or not ge_type or status == "REJECTED":
                continue
            qs = FuelConsommationMonthly.objects.filter(site_id__iexact=resolved)
            count = qs.update(type_ge_fichier=ge_type, fichier_source=BASE_GE_LABEL)
            if count:
                updated += count
        batch.summary = {
            "source": BASE_GE_LABEL, "sheets": sheets_seen,
            "sites": len(seen_sites), "types": len({r[2] for r in rows if r[2]}),
            "sites_dupliques": len(duplicate_sites), "rows_processed": len(rows),
            "rows_updated": updated, "rows_created": created,
        }

    return batch.summary
