# fuel_tracking/services/base_ge.py
"""
Fichier « Base GE » (rubrique Configuration, type BASE_GE) : type de GE par site, source du champ « Type de GE » du
Suivi Conso (bloc ESTIMATION CONSOMMATION FUEL THÉORIQUE) — décision métier.

Feuille lue : la première dont une ligne d'en-tête contient « Site ID » et « Type de GE » (ex. « Feuil32 »).
Colonnes : Site ID, Type de GE (libellé « MARQUE - MODÈLE », ex. « SDMO - K22 », ou modèle seul « K22 » dans Base GE VF5),
Fabricant GE (facultatif, Base GE VF5 : quand la colonne existe, c'est elle qui donne le fabricant), Puissance GE (KVA).
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
            fab = h.index("fabricant ge") if "fabricant ge" in h else None   # Base GE VF5 : colonne fabricant à part
            return n, {"site": h.index("site id"), "type": h.index("type de ge"), "kva": kva, "nom": nom, "fab": fab}
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
        fab = " ".join(str(get(row, "fab") or "").split()) or None
        raw = {"site_id": site_raw, "nom": F._jsonable(get(row, "nom")), "type_ge": typ, "puissance_ge_kva": kva}
        if cols.get("fab") is not None:
            raw["fabricant_ge"] = fab
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
    # Pas de vidage de cache : le Suivi Conso lit le dernier fichier Base GE en base à chaque affichage (ge_info.sites_info) ;
    # les puissances GENSET DB et les données Snowflake / ENOC en cache restent disponibles.
    return stats


def _latest_rows() -> tuple[dict, str | None]:
    """site → ligne brute (type renseigné, première occurrence) du dernier fichier Base GE importé ; (lignes, nom du fichier)."""
    from fuel_tracking.models import FuelImportBatch as B, FuelImportRow as Row

    b = B.objects.filter(source_type="BASE_GE").exclude(status__in=("REJECTED", "FAILED")).order_by("-received_at").first()
    if b is None:
        return {}, None
    out = {}
    for sid_res, sid_raw, raw in (Row.objects.filter(batch=b).exclude(validation_status="REJECTED").order_by("row_number")
                                  .values_list("site_id_resolved", "site_id_raw", "raw_payload")):
        if not (raw or {}).get("type_ge"):
            continue
        key = (sid_res or sid_raw or "").upper()
        if key and key not in out:
            out[key] = raw
    return out, b.file_name


def latest_types() -> tuple[dict, str | None]:
    """site → (type de GE, puissance GE kVA du fichier) du dernier fichier Base GE importé ; (valeurs, nom du fichier)."""
    rows, name = _latest_rows()
    return {k: (r["type_ge"], r.get("puissance_ge_kva")) for k, r in rows.items()}, name


def latest_fabricants() -> dict:
    """site → fabricant de la colonne « Fabricant GE » du dernier fichier Base GE (Base GE VF5) ; vide pour un ancien fichier
    sans cette colonne (le fabricant est alors lu dans le libellé du type)."""
    rows, _name = _latest_rows()
    return {k: " ".join(str(r["fabricant_ge"]).split()) for k, r in rows.items() if str(r.get("fabricant_ge") or "").strip()}


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


# ── Fabricant lu dans le libellé « Type de GE » du fichier (le fichier n'a pas de colonne fabricant) ──
SEP = re.compile(r"\s+-+\s*|\s*-{2,}\s*|\s*\|\s*")   # « SDMO - K22 », « SDMO--J66 », « PRAMAC | GDW50 »


def known_brands(labels, extra=()) -> list[str]:
    """Marques connues : celles écrites avec un séparateur dans le fichier lui-même + fabricants de GENSET DB."""
    out = {}
    for lab in list(labels) + list(extra):
        parts = SEP.split(str(lab or "").strip(), maxsplit=1)
        b = (parts[0] if len(parts) == 2 else (lab if lab in extra else "")).strip()
        if b and not b.isdigit() and len(b) >= 2:
            out.setdefault(b.upper(), b)
    return sorted(out.values(), key=len, reverse=True)


def split_label(label, brands) -> tuple[str | None, str | None]:
    """(fabricant, modèle) lus dans le libellé : séparateur « - », « -- » ou « | » ; sinon marque connue en début de libellé
    (« FG Wilson P33-3U ») ; sinon pas de fabricant (« PA50 »). Jamais déduit du modèle."""
    lab = " ".join(str(label or "").split())
    if not lab:
        return None, None
    parts = SEP.split(lab, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    up = lab.upper()
    for b in brands:
        n = len(b)
        if up.startswith(b.upper()) and (len(lab) == n or not lab[n].isalnum()):
            return lab[:n].strip(), lab[n:].strip() or None
    return None, lab
