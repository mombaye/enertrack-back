# fuel_tracking/services/base_ge.py
"""
Fichier « Base GE » (rubrique Configuration, type BASE_GE) : type de GE par site, source du champ « Type de GE » du
Suivi Conso (bloc ESTIMATION CONSOMMATION FUEL THÉORIQUE) — décision métier.

Feuille lue : la première dont une ligne d'en-tête contient « Site ID » et « Type de GE » (ex. « Feuil32 »).
Colonnes : Site ID, Type de GE (libellé « MARQUE - MODÈLE », ex. « SDMO - K22 »), Puissance GE (KVA) (information).
Chaque ligne devient une ligne d'import (FuelImportRow) : ACCEPTED ; WARNING si site inconnu de Gestion des sites, doublon
(première occurrence conservée) ou type vide ; jamais de type inventé. Le dernier fichier importé fait foi (relu par ge_info).
"""
from __future__ import annotations

import re

from fuel_tracking.services import stock_import_files as F

SOURCE_LABEL = "Fichier Base GE"


def _norm(v) -> str:
    return " ".join(str(v or "").replace("\n", " ").split()).strip().lower()


def find_header(rows) -> tuple[int, dict] | None:
    for n, row in enumerate(rows):
        if n > 30:
            break
        h = [_norm(v) for v in (row or [])]
        if "site id" in h and "type de ge" in h:
            kva = next((i for i, x in enumerate(h) if x.startswith("puissance ge")), None)
            nom = h.index("nom") if "nom" in h else None
            return n, {"site": h.index("site id"), "type": h.index("type de ge"), "kva": kva, "nom": nom}
    return None


def process_base_ge_file(batch, file_bytes: bytes, file_name: str) -> dict:
    from fuel_tracking.models import FuelImportRow as Row
    from fuel_tracking.services.stock_registry import SiteResolver

    found = None
    for name, rows in F.iter_sheets(file_bytes, file_name):
        rows = list(rows)
        head = find_header(rows)
        if head:
            found = (name, rows, head)
            break
    if found is None:
        raise ValueError("Colonnes « Site ID » et « Type de GE » introuvables : fichier Base GE non reconnu.")
    name, rows, (hi, cols) = found
    resolver = SiteResolver()
    stats = {"onglet": name, "lignes_lues": 0, "types": 0, "sans_type": 0, "doublons": 0, "sites_inconnus": 0}
    seen, out = set(), []
    get = lambda row, k: row[cols[k]] if cols.get(k) is not None and cols[k] < len(row) else None  # noqa: E731
    for k, row in enumerate(rows[hi + 1:], start=hi + 2):
        row = list(row or [])
        site_raw = str(get(row, "site") or "").strip()
        if not re.match(r"^[A-Za-z0-9]+_[A-Za-z0-9]+", site_raw):
            continue
        stats["lignes_lues"] += 1
        resolved = resolver.resolve(site_raw)
        key = (resolved or site_raw).upper()
        typ = " ".join(str(get(row, "type") or "").split()) or None
        kva = F.parse_number(get(row, "kva"))
        raw = {"site_id": site_raw, "nom": F._jsonable(get(row, "nom")), "type_ge": typ, "puissance_ge_kva": kva}
        msgs, status = [], Row.ValidationStatus.ACCEPTED
        if key in seen:
            msgs.append({"code": "DUPLICATE_ALREADY_IMPORTED", "message": "Site déjà présent plus haut : première occurrence conservée."})
            status = Row.ValidationStatus.WARNING
            stats["doublons"] += 1
        else:
            seen.add(key)
            if not typ:
                msgs.append({"code": "TYPE_GE_VIDE", "message": "Type de GE vide dans le fichier : aucun type retenu pour ce site."})
                status = Row.ValidationStatus.WARNING
                stats["sans_type"] += 1
            else:
                stats["types"] += 1
            if resolved is None:
                msgs.append({"code": "SITE_INCONNU", "message": "Site absent de Gestion des sites : conservé sous l'identifiant brut."})
                status = Row.ValidationStatus.WARNING
                stats["sites_inconnus"] += 1
        out.append(Row(batch=batch, sheet_name=name, row_number=k, raw_payload=raw, site_id_raw=site_raw, site_id_resolved=resolved,
                       validation_status=status, messages=msgs, row_fingerprint=F.fingerprint("BASE_GE", key, typ)))
    Row.objects.bulk_create(out, batch_size=1000)
    try:   # le Suivi Conso relit le type de GE au prochain affichage
        from django.core.cache import cache

        from fuel_tracking.services import ge_info as G

        cache.delete(G.CACHE_KEY)
        cache.delete(G.CACHE_KEY + ":lock")
    except Exception:  # noqa: BLE001
        pass
    return stats


def latest_types() -> tuple[dict, str | None]:
    """site → (type de GE, puissance GE kVA du fichier) du dernier fichier Base GE importé ; (valeurs, nom du fichier)."""
    from fuel_tracking.models import FuelImportBatch as B, FuelImportRow as Row

    b = B.objects.filter(source_type="BASE_GE").exclude(status__in=("REJECTED", "FAILED")).order_by("-received_at").first()
    if b is None:
        return {}, None
    out = {}
    for sid_res, sid_raw, raw, st in (Row.objects.filter(batch=b).exclude(validation_status="REJECTED").order_by("row_number")
                                      .values_list("site_id_resolved", "site_id_raw", "raw_payload", "validation_status")):
        if not (raw or {}).get("type_ge"):
            continue
        key = (sid_res or sid_raw or "").upper()
        if key and key not in out:
            out[key] = (raw["type_ge"], raw.get("puissance_ge_kva"))
    return out, b.file_name


def looks_like_base_ge(file_bytes: bytes, file_name: str) -> bool:
    """Vrai si le classeur est un fichier Base GE (« Site ID » + « Type de GE ») et non un fichier de clôture. Lecture .xlsx
    seulement (un Closing est un .xlsb volumineux : jamais relu ici)."""
    if not str(file_name).lower().endswith((".xlsx", ".xlsm")):
        return False
    try:
        import io

        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        try:
            names = [_norm(n) for n in wb.sheetnames]
            if any(n in ("synthese conso fuel", "output") for n in names):
                return False
            return any(find_header(list(ws.iter_rows(max_row=31, values_only=True))) for ws in wb.worksheets)
        finally:
            wb.close()
    except Exception:  # noqa: BLE001 — classeur illisible : laissé au traitement habituel
        return False
