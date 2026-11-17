# fuel_tracking/services/genset_db_import.py
"""
Abaque CPH depuis l'onglet « GENSET DB » du fichier de clôture (Closing) — importé automatiquement avec le Closing
(rubrique Configuration ou dépôt dans incoming/closing/).

Colonnes lues (ligne d'en-tête « Manufacturer / Type / Genset List / Type de GE / … ») : fabricant, type de GE,
kVA nominal (colonne sans titre qui suit « Type de GE »), tension, phases, PRP kVA, PRP kW, cos φ, conso à
100 / 75 / 50 % de charge (L/h) et coefficients a, b, c de la courbe CPH (L/h) = a·x² + b·x + c, x = charge.

  - chaque modèle devient une courbe CphCurve (identifiant stable « GDB-… » : fabricant + type + PRP), statut
    « Historique à valider », source « Closing — GENSET DB » ; les points 50 / 75 / 100 % font foi, un écart des
    coefficients a, b, c du fichier par rapport à l'ajustement des 3 points est signalé, jamais corrigé en silence ;
  - pour chaque type de GE de l'inventaire des sites (Base GE : type_ge_fichier + pge_kva_fichier), les courbes dont
    « fabricant + type » correspond EXACTEMENT au libellé (casse, espaces et ponctuation ignorés) deviennent candidates ;
    aucune variante approchée n'est rapprochée (« P33-3U » ≠ « P33-3 ») ;
  - la correspondance automatique existante (cph_matching.apply_auto_matching : modèle, marque, puissance ± 15 %)
    décide ; une décision humaine (validation ou rejet manuel) n'est jamais écrasée.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation

from django.utils import timezone

SHEET = "GENSET DB"
SOURCE_LABEL = "Closing — GENSET DB"


def _alnum(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _dec(v) -> Decimal | None:
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        return Decimal(str(v).replace(",", ".").strip())
    except (InvalidOperation, ValueError):
        return None


def _txt(v) -> str:
    return " ".join(str(v).split()) if v is not None else ""


def curve_id_for(manufacturer: str, model: str, prp_kva) -> str:
    h = hashlib.sha1(f"{_alnum(manufacturer)}|{_alnum(model)}|{prp_kva}".encode()).hexdigest()[:10].upper()
    return f"GDB-{h}"  # 14 caractères (CphCurve.curve_id : 16 max)


def parse_rows(rows) -> tuple[list[dict], list[str]]:
    """Lignes brutes de la feuille → courbes (dédoublonnées sur fabricant + type + PRP) et avertissements."""
    from fuel_tracking.services.cph_abaque_import import fitted_coefficients

    header_idx, idx, curves, warnings, seen = None, {}, [], [], {}
    for n, row in enumerate(rows):
        vals = list(row or [])
        norm = [str(v).strip().lower() if v is not None else "" for v in vals]
        if header_idx is None:
            if "type de ge" in norm and any(h.startswith("manufacturer") for h in norm):
                header_idx = n
                col = lambda name: next((i for i, h in enumerate(norm) if h.startswith(name)), None)  # noqa: E731
                tge = col("type de ge")
                idx = {"manufacturer": col("manufacturer"), "type": norm.index("type") if "type" in norm else None,
                       "list": col("genset list"), "model": tge, "kva_nominal": tge + 1, "voltage": col("voltage"),
                       "phases": col("no. of phases"), "prp_kva": col("prp [kva]"), "prp_kw": col("prp [kw]"), "cos": col("cos"),
                       "c100": col("conso 100"), "c75": col("conso 75"), "c50": col("conso 50"),
                       "a": norm.index("a") if "a" in norm else None, "b": norm.index("b") if "b" in norm else None,
                       "c": norm.index("c") if "c" in norm else None}
            continue
        get = lambda k: vals[idx[k]] if idx.get(k) is not None and idx[k] < len(vals) else None  # noqa: E731
        model = _txt(get("model"))
        prp, c50, c75, c100 = _dec(get("prp_kva")), _dec(get("c50")), _dec(get("c75")), _dec(get("c100"))
        if not model or prp is None or None in (c50, c75, c100):
            continue  # ligne de titre (« Site avec cuve et sans GE »…) ou incomplète : pas une courbe
        manufacturer = _txt(get("manufacturer"))
        cid = curve_id_for(manufacturer, model, prp)
        fa, fb, fc = fitted_coefficients(c50, c75, c100)
        a, b, c = (_dec(get(k)) for k in ("a", "b", "c"))
        if None not in (a, b, c) and max(abs(fa - a), abs(fb - b), abs(fc - c)) > Decimal("0.05"):
            warnings.append(f"{manufacturer} {model} ({prp} kVA) : a, b, c du fichier ({a:.3f}, {b:.3f}, {c:.3f}) ≠ ajustement des points "
                            f"50/75/100 % ({fa:.3f}, {fb:.3f}, {fc:.3f}) — les points font foi.")
        rec = {
            "curve_id": cid, "manufacturer": manufacturer[:64], "model": model[:64], "model_key": _alnum(model)[:64],
            "variant": _txt(get("list") or get("type"))[:128], "prp_kva": prp, "prp_kw": _dec(get("prp_kw")) or prp * Decimal("0.8"),
            "power_factor": _dec(get("cos")), "voltage_v": int(_dec(get("voltage"))) if _dec(get("voltage")) else None,
            "phases": int(_dec(get("phases"))) if _dec(get("phases")) else None, "frequency": "50 Hz",
            "conso_50_l_h": c50, "conso_75_l_h": c75, "conso_100_l_h": c100,
            "coef_a": fa.quantize(Decimal("0.000001")), "coef_b": fb.quantize(Decimal("0.000001")), "coef_c": fc.quantize(Decimal("0.000001")),
            "kva_nominal": _dec(get("kva_nominal")), "_key": _alnum(manufacturer) + _alnum(model), "_model_alnum": _alnum(model),
        }
        if cid in seen:  # même fabricant + type + PRP (variantes de contrôleur) : une seule courbe si les points sont identiques
            if (seen[cid]["conso_50_l_h"], seen[cid]["conso_75_l_h"], seen[cid]["conso_100_l_h"]) != (c50, c75, c100):
                warnings.append(f"{manufacturer} {model} ({prp} kVA) présent plusieurs fois avec des consommations différentes : première ligne retenue.")
            continue
        seen[cid] = rec
        curves.append(rec)
    if header_idx is None:
        raise ValueError(f"Feuille « {SHEET} » : ligne d'en-tête (Manufacturer / Type de GE) introuvable.")
    return curves, warnings


def candidates_for(label: str, curves: list[dict]) -> list[str]:
    """Courbes dont « fabricant + type » est exactement le libellé d'inventaire (sans casse ni ponctuation).
    Courbe sans fabricant : le libellé doit se terminer par son type."""
    key = _alnum(label)
    out = [c["curve_id"] for c in curves if c["manufacturer"] and c["_key"] == key]
    if not out:
        out = [c["curve_id"] for c in curves if not c["manufacturer"] and len(c["_model_alnum"]) >= 3 and key.endswith(c["_model_alnum"])]
    return out


def inventory_labels() -> list[tuple[str, Decimal | None]]:
    """(type de GE, kVA) distincts de l'inventaire des sites (Base GE), comme lus par le moteur CPH."""
    from fuel_tracking.models import FuelConsommationMonthly

    rows = (FuelConsommationMonthly.objects.filter(type_ge_fichier__isnull=False)
            .values_list("type_ge_fichier", "pge_kva_fichier").distinct())
    return sorted({(str(l).strip(), k) for l, k in rows if str(l or "").strip()}, key=lambda x: (x[0], x[1] or 0))


def apply(curves: list[dict], file_name: str, labels: list[tuple[str, Decimal | None]] | None = None) -> dict:
    """Écrit courbes et correspondances (sans écraser une décision humaine) puis lance la correspondance automatique."""
    from fuel_tracking.models import CphCurve, CphInventoryMapping, CphMappingHistory
    from fuel_tracking.services.cph_engine import normalize_label
    from fuel_tracking.services.cph_matching import MANUAL_METHODS, apply_auto_matching

    now = timezone.now()
    created = updated = 0
    for c in curves:
        fields = {k: v for k, v in c.items() if not k.startswith("_") and k not in ("curve_id", "kva_nominal")}
        fields.update(status=CphCurve.Status.HISTORIQUE_A_VALIDER, source=SOURCE_LABEL, abaque_file=file_name[:255],
                      domain="Fichier de clôture Ops", note="Courbe issue de l'onglet GENSET DB du Closing (points 50 / 75 / 100 %).")
        obj = CphCurve.objects.filter(curve_id=c["curve_id"]).first()
        if obj is None:
            CphCurve.objects.create(curve_id=c["curve_id"], **fields)
            created += 1
        else:
            if obj.power_factor is not None:
                fields.pop("power_factor")  # cos φ déjà ajusté : conservé
            for k, v in fields.items():
                setattr(obj, k, v)
            obj.imported_at = now
            obj.save()
            updated += 1

    labels = inventory_labels() if labels is None else labels
    touched, mapped, without = [], 0, 0
    for label, kva in labels:
        cands = candidates_for(label, curves)
        if not cands:
            without += 1
            continue
        mapped += 1
        m = CphInventoryMapping.objects.filter(inventory_label=label, inventory_kva=kva).first()
        if m is None:
            m = CphInventoryMapping.objects.create(
                inventory_label=label, inventory_label_normalized=normalize_label(label), inventory_kva=kva,
                candidate_curve_ids=cands,
                abaque_status=(CphInventoryMapping.AbaqueStatus.CANDIDAT_UNIQUE_A_VALIDER if len(cands) == 1
                               else CphInventoryMapping.AbaqueStatus.MODELE_AMBIGU),
                action_required=f"Courbe(s) {SOURCE_LABEL} : {', '.join(cands)}", site_count=None,
                normalized_key=_alnum(label), candidate_models=", ".join(sorted({c["model"] for c in curves if c["curve_id"] in cands})),
                abaque_file=file_name[:255], imported_at=now)
        elif m.match_method not in MANUAL_METHODS:
            ids = list(dict.fromkeys(list(m.candidate_curve_ids or []) + cands))
            if ids != list(m.candidate_curve_ids or []):
                m.candidate_curve_ids = ids
                m.imported_at = now
                m.save(update_fields=["candidate_curve_ids", "imported_at"])
        touched.append(m.pk)
    counts = apply_auto_matching(CphInventoryMapping.objects.filter(pk__in=touched), list(CphCurve.objects.all()), now,
                                 history_model=CphMappingHistory) if touched else {}
    return {"courbes_creees": created, "courbes_mises_a_jour": updated, "types_ge_inventaire": len(labels),
            "types_ge_avec_courbe": mapped, "types_ge_sans_courbe": without, "correspondances": counts}


def import_from_closing(file_bytes: bytes, file_name: str) -> dict:
    """Lit l'onglet GENSET DB d'un Closing et l'applique. ValueError si l'onglet est absent ou illisible."""
    from fuel_tracking.services.stock_import_files import iter_sheets

    for _name, rows in iter_sheets(file_bytes, file_name, only={SHEET}):
        curves, warnings = parse_rows(rows)
        res = apply(curves, file_name)
        res["avertissements"] = warnings[:50]
        return res
    raise ValueError(f"Onglet « {SHEET} » absent du fichier.")
