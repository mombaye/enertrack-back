# fuel_tracking/services/stock_tanks.py
"""
Cuves : bibliothèque de MODÈLES (fuel_tank_model, calculateur de Steve) et AFFECTATION réelle site → cuve
(fuel_site_tank, alimentée par le Closing, les fichiers terrain, ENOC ou une saisie autorisée).

  - Le calculateur fournit les modèles et les formules ; il ne dit pas quelle cuve est sur quel site.
  - Un modèle n'est PROPOSÉ à une cuve site que si famille et cotes correspondent sans ambiguïté
    (un seul modèle à ± 3 cm sur le diamètre / la section et ± 5 cm sur la longueur) ; jamais sur la seule
    capacité (une 5 000 L n'est pas attribuée parce que c'est une capacité fréquente).
  - Une proposition n'est pas une validation : la capacité de plaque d'un modèle proposé n'est utilisée
    qu'après confirmation.
  - Cuve Closing VALIDÉE automatiquement si forme + cotes reconnues + capacité déclarée cohérente (≤ 5 %).
  - Capacité de plaque issue du suivi refueling (colonne CUVE, valeur ronde au litre près de la centaine) :
    appliquée seulement si elle est cohérente à 5 % avec la géométrie de la cuve du site.
"""
from __future__ import annotations

import re
from decimal import Decimal

from fuel_tracking.services import stock_registry as R
from fuel_tracking.services import tank_conversion as TC
from fuel_tracking.services import tank_geometry as G

DIAMETER_TOL_CM = 3.0
LENGTH_TOL_CM = 5.0
DERIVED_CAPACITY_TOL = 0.005  # capacité déclarée = géométrie à 0,5 % près → valeur recalculée, pas une plaque


def _d(v):
    return None if v is None else Decimal(str(round(float(v), 3)))


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


# ── Bibliothèque des modèles (fichier de Steve) ──────────────────────────────

def model_status(family, d1, d2, d3, bottom, bz, top, tz, plate) -> tuple[str, str, dict]:
    try:
        full = G.full_volume_l(family, d1, d2, d3, bottom, bz, top, tz)
    except G.GeometryError as e:
        return "INCOMPLET", f"Calcul non reconnu : {e}", {}
    cal = G.calibration(plate, full)
    info = {"full": full, **cal}
    if not plate:
        return "INCOMPLET", "Capacité de plaque absente : volume calibré impossible.", info
    if not cal["coherent"]:
        return "A_CONFIRMER", f"Écart géométrie / plaque {cal['gap_pct']:+.1f} % (> 5 %) : cotes à reprendre sur site.", info
    return "COMPLET", f"Écart géométrie / plaque {cal['gap_pct']:+.1f} % (≤ 5 %).", info


def process_tank_models_file(batch, file_bytes: bytes, file_name: str) -> dict:
    """Onglet « Tank DB » → fuel_tank_model ; onglet « Profils » comparé aux tables embarquées (steve-v1)."""
    from fuel_tracking.models import FuelImportRow, FuelTankModel
    from fuel_tracking.services.stock_import_files import _jsonable, _norm, iter_sheets

    stats = {"modeles_crees": 0, "modeles_mis_a_jour": 0, "inchanges": 0, "statuts": {}, "profils": None, "codes": {}}
    found = False
    for sheet, rows in iter_sheets(file_bytes, file_name, only={"Tank DB", "Profils"}):
        if sheet.strip() == "Profils":
            stats["profils"] = _compare_profiles(list(rows))
            continue
        found = True
        header = None
        for n, row in enumerate(rows, start=1):
            normed = [_norm(v) for v in (row or [])]
            if header is None:
                if "#" in normed and "famille" in normed:
                    header = normed
                continue
            rec = {h: row[i] for i, h in enumerate(header) if h and i < len(row) and h not in ("",)}
            num = rec.get("#")
            fam_label = _norm(rec.get("famille"))
            if num is None or not fam_label:
                continue
            fam = G.FAMILY_BY_LABEL.get(fam_label)
            bottom = G.PROFILE_BY_LABEL.get(_norm(rec.get("profil du fond")), None)
            top = G.PROFILE_BY_LABEL.get(_norm(rec.get("profil du ciel")), None) or "plat"
            d1, d2, d3 = _num(rec.get("cote 1 [cm]")), _num(rec.get("cote 2 [cm]")), _num(rec.get("cote 3 [cm]"))
            bz, tz = _num(rec.get("z saisi [cm]")), _num(rec.get("zt saisi [cm]"))
            plate = _num(rec.get("capacity [liters]"))
            country = rec.get("country")
            raw = {k: _jsonable(v) for k, v in rec.items() if k and not k.startswith("moteur")}
            messages = []
            if fam is None:
                status, why, info = "INCOMPLET", f"Famille inconnue : « {rec.get('famille')} ».", {}
            elif bottom is None:
                status, why, info = "INCOMPLET", f"Profil de fond inconnu : « {rec.get('profil du fond')} ».", {}
            else:
                status, why, info = model_status(fam, d1, d2, d3, bottom, bz, top, tz, plate)
            messages.append({"code": f"MODEL_{status}", "message": why})
            model_id = f"{(str(country or 'XX')[:3]).upper()}-TDB-{int(num):02d}"
            values = dict(
                country=country, source_file=file_name[:255], source_sheet=sheet[:128], source_row=n,
                source_label=(str(rec.get("type / shape") or "").strip() or None), shape_code=fam,
                shape_label=G.FAMILIES.get(fam), tank_family=G.FAMILIES.get(fam), nominal_capacity_l=_d(plate),
                dimension_1_cm=_d(d1), dimension_2_cm=_d(d2), dimension_3_cm=_d(d3), bottom_profile=bottom, bottom_z_cm=_d(bz),
                top_profile=top, top_z_cm=_d(tz), geometric_full_l=_d(info.get("full")), calibration_k=_d(info.get("k")),
                plate_gap_pct=_d(info.get("gap_pct")), geometric_formula_version=G.GEOMETRIC_FORMULA_VERSION,
                calibration_formula_version=G.CALIBRATION_FORMULA_VERSION,
                calibration_method="Volume calibré = géométrique × (plaque / géométrique plein)",
                model_status=status, status_reason=why, import_batch=batch,
            )
            row_obj = FuelImportRow.objects.create(batch=batch, sheet_name=sheet[:128], row_number=n, raw_payload=raw,
                                                   validation_status="ACCEPTED" if status == "COMPLET" else "WARNING",
                                                   messages=messages, row_fingerprint=model_id)
            existing = FuelTankModel.objects.filter(tank_model_id=model_id).first()
            if existing is None:
                m = FuelTankModel.objects.create(tank_model_id=model_id, **values)
                R.log_correction("TANK_MODEL", m.pk, "CREATE", f"Import {file_name}", new=model_id, author_label=f"import #{batch.pk}")
                stats["modeles_crees"] += 1
            else:
                changed = False
                for k, v in values.items():
                    old = getattr(existing, k)
                    if k in ("import_batch", "source_file", "source_row"):
                        continue
                    if str(old if old is not None else "") != str(v if v is not None else "") and not (
                            isinstance(old, Decimal) and v is not None and abs(float(old) - float(v)) < 1e-3):
                        R.log_correction("TANK_MODEL", existing.pk, "CORRECT", f"Import {file_name}", field=k, old=old, new=v,
                                         author_label=f"import #{batch.pk}")
                        setattr(existing, k, v)
                        changed = True
                existing.import_batch = batch
                existing.save()
                stats["modeles_mis_a_jour" if changed else "inchanges"] += 1
            stats["statuts"][status] = stats["statuts"].get(status, 0) + 1
            del row_obj
    if not found:
        raise ValueError("Onglet « Tank DB » introuvable : fichier calculateur de cuves non reconnu.")
    return stats


def _compare_profiles(rows: list) -> dict:
    """Compare l'onglet Profils du fichier aux tables embarquées (aucune modification automatique du moteur)."""
    from fuel_tracking.services.stock_import_files import _norm

    tb = G.tables()
    hdr = next((r for r in rows if r and _norm(r[0]) == "t / u"), None)
    if hdr is None:
        return {"statut": "ABSENT"}
    idx = {_norm(h): i for i, h in enumerate(hdr) if h}
    data = [r for r in rows[rows.index(hdr) + 1:] if r and isinstance(r[0], (int, float))][:201]
    worst = 0.0
    for name in ("ellip", "toris", "korb", "cone"):
        for kind in ("psi", "phi"):
            col = idx.get(f"{kind} {name}")
            if col is None:
                continue
            for i, r in enumerate(data):
                worst = max(worst, abs(float(r[col] or 0) - tb[kind][name][i]))
    same = worst < 1e-6
    return {"statut": "IDENTIQUES" if same else "DIFFERENTES", "ecart_max": worst, "version_moteur": tb["version"],
            "message": None if same else "Tables de profils différentes de la version embarquée : moteur inchangé, à revoir."}


# ── Affectation site → cuve ──────────────────────────────────────────────────

def library_models():
    from fuel_tracking.models import FuelTankModel

    return list(FuelTankModel.objects.all())


def propose_model(spec: TC.TankSpec, models) -> tuple[object | None, str]:
    """Modèle correspondant SANS ambiguïté (famille + cotes), sinon (None, motif)."""
    if not spec.family:
        return None, "Forme inconnue : aucun modèle proposé."
    cands = []
    for m in models:
        if m.shape_code != spec.family:
            continue
        a = [_num(m.dimension_1_cm), _num(m.dimension_2_cm), _num(m.dimension_3_cm)]
        b = [spec.d1, spec.d2, spec.d3]
        ok = True
        for i, (x, y) in enumerate(zip(a, b)):
            if not x and not y:
                continue
            if x is None or y is None:
                ok = False
                break
            tol = LENGTH_TOL_CM if i == 2 and spec.family != "v-tronc" else DIAMETER_TOL_CM
            if abs(x - y) > tol:
                ok = False
                break
        if ok:
            cands.append(m)
    if len(cands) == 1:
        return cands[0], f"Cotes compatibles avec le modèle {cands[0].tank_model_id} ({cands[0].source_label or ''} {cands[0].nominal_capacity_l or ''} L)."
    if len(cands) > 1:
        return None, f"Plusieurs modèles compatibles ({', '.join(m.tank_model_id for m in cands)}) : aucune proposition."
    return None, "Aucun modèle de la bibliothèque ne correspond aux cotes."


def assess_closing_tank(spec: TC.TankSpec, declared_capacity, multi: bool) -> tuple[str, str]:
    """Statut d'affectation d'une cuve issue du Closing (validation automatique si cohérente)."""
    if multi:
        return "PLUSIEURS_CUVES_A_IDENTIFIER", "Plusieurs cuves mentionnées pour ce site sans identifiant : identifier chaque cuve."
    if not spec.has_geometry():
        if declared_capacity:
            return "A_CONFIRMER", "Capacité connue mais forme / cotes insuffisantes."
        return "INCOMPLETE", "Aucune information cuve exploitable."
    full = spec.geometric_full_l()
    if not declared_capacity:
        return "PROPOSEE", f"Forme et cotes reconnues (géométrie {full:.0f} L) mais capacité absente : à confirmer."
    gap = (full - declared_capacity) / declared_capacity
    if abs(gap) <= 0.05:
        if is_derived_capacity(declared_capacity, full):
            # La capacité du Closing est recalculée depuis les cotes (fonds plats) : ce n'est pas une preuve de plaque.
            return "VALIDEE", (f"Closing : forme + cotes reconnues (géométrie {full:.0f} L). Capacité du fichier "
                               f"{declared_capacity:.0f} L = calcul depuis les cotes, pas une capacité de plaque : plaque à confirmer.")
        return "VALIDEE", (f"Closing : forme + cotes reconnues, capacité déclarée {declared_capacity:.0f} L cohérente avec la "
                           f"géométrie {full:.0f} L (écart {gap * 100:+.1f} %).")
    return "PROPOSEE", f"Capacité déclarée {declared_capacity:.0f} L ≠ géométrie {full:.0f} L ({gap * 100:+.1f} %) : à confirmer."


def is_derived_capacity(declared, geometric_full) -> bool:
    return bool(declared and geometric_full and abs(declared - geometric_full) / geometric_full <= DERIVED_CAPACITY_TOL)


def apply_plate_evidence(batch, evidence: dict[str, list[tuple]], stats: dict) -> None:
    """
    Capacité de plaque déclarée site par site (fichier refueling, colonne CUVE) :
      - une seule valeur ronde par site ; site avec UNE cuve dont la géométrie est connue → plaque appliquée si
        cohérente à 5 %, sinon signalée ;
      - site sans cuve → cuve créée À_CONFIRMER (capacité connue, forme / cotes à relever).
    """
    from fuel_tracking.models import FuelTankMaster

    label = f"import #{batch.pk} ({batch.source_type})"
    for site_id, items in evidence.items():
        values = sorted({v for v, _ in items})
        if len(values) != 1:
            stats["plaques_ambigues"] = stats.get("plaques_ambigues", 0) + 1
            continue
        plate = values[0]
        ref = items[0][1]
        tanks = list(FuelTankMaster.objects.filter(site_id=site_id, active=True).exclude(validation_status="REJETEE"))
        if not tanks:
            t = FuelTankMaster.objects.create(
                site_id=site_id, tank_id="1", capacity_plate_l=_d(plate), capacity_plate_source=f"Suivi refueling (CUVE) — {ref}",
                source_type=batch.source_type, source_batch=batch, source_file=batch.file_name[:255], validation_status="A_CONFIRMER",
                validation_reason="Capacité de plaque connue (suivi refueling) mais forme / cotes à relever sur site.")
            R.log_correction("TANK", t.pk, "CREATE", f"Création depuis {label} (capacité de plaque seule)", site_id=site_id, author_label=label)
            stats["cuves_capacite_seule"] = stats.get("cuves_capacite_seule", 0) + 1
            continue
        if len(tanks) > 1:
            stats["plaques_sites_multicuves"] = stats.get("plaques_sites_multicuves", 0) + 1
            continue
        t = tanks[0]
        if t.capacity_plate_l is not None:
            continue
        spec = TC.TankSpec.from_model(t)
        full = spec.geometric_capacity_l()
        if full is None:
            t.capacity_plate_l, t.capacity_plate_source = _d(plate), f"Suivi refueling (CUVE) — {ref}"
            if t.validation_status == "INCOMPLETE":
                t.validation_status, t.validation_reason = "A_CONFIRMER", "Capacité de plaque connue mais forme / cotes insuffisantes."
            t.save()
            R.log_correction("TANK", t.pk, "CORRECT", f"Capacité de plaque depuis {label}", site_id=site_id,
                             field="capacity_plate_l", old=None, new=plate, author_label=label)
            stats["plaques_appliquees"] = stats.get("plaques_appliquees", 0) + 1
            continue
        cal = G.calibration(plate, full)
        if cal["coherent"]:
            t.capacity_plate_l, t.capacity_plate_source = _d(plate), f"Suivi refueling (CUVE) — {ref}"
            t.save()
            R.log_correction("TANK", t.pk, "CORRECT", f"Capacité de plaque depuis {label} (écart géométrie {cal['gap_pct']:+.1f} %)",
                             site_id=site_id, field="capacity_plate_l", old=None, new=plate, author_label=label)
            stats["plaques_appliquees"] = stats.get("plaques_appliquees", 0) + 1
        else:
            stats["plaques_incoherentes"] = stats.get("plaques_incoherentes", 0) + 1
            R.log_correction("TANK", t.pk, "SYSTEM", f"Capacité de plaque {plate:.0f} L ({label}) NON appliquée : écart géométrie "
                             f"{cal['gap_pct']:+.1f} % (> 5 %)", site_id=site_id, author_label=label)


def plate_from_cuve_cell(v) -> float | None:
    """Valeur CUVE du suivi refueling : capacité de plaque si nombre rond (multiple de 100), sinon None."""
    n = _num(v) if not isinstance(v, str) else _num(re.sub(r"[^\d.,]", "", v).replace(",", "."))
    if n is None or n <= 0:
        return None
    return n if abs(n - round(n / 100) * 100) < 1e-6 else None


def evaluate_site_tanks(user=None, dry_run=False) -> dict:
    """
    Réévalue les cuves non validées manuellement : statut d'affectation (règles Closing), proposition de modèle.
    Les cuves VALIDÉES ou REJETÉES par un humain ne sont jamais modifiées.
    """
    from fuel_tracking.models import FuelTankMaster

    models = library_models()
    stats = {"evaluees": 0, "statuts": {}, "modeles_proposes": 0, "changements": 0}
    counts = {}
    for t in FuelTankMaster.objects.filter(active=True):
        counts[t.site_id] = counts.get(t.site_id, 0) + 1
    for t in FuelTankMaster.objects.filter(active=True).select_related("tank_model", "source_batch"):
        stats["evaluees"] += 1
        # Score de compatibilité (autorise l'estimation d'une cuve proposée au-dessus du seuil) : recalculé pour toutes les cuves.
        spec0 = TC.TankSpec.from_model(t)
        m0, _ = propose_model(spec0, models)
        score = TC.compat_score(spec0, m0 is not None or t.tank_model_id is not None, _num(t.capacity_l))
        if t.compat_score != score:
            stats["scores_mis_a_jour"] = stats.get("scores_mis_a_jour", 0) + 1
            if not dry_run:
                FuelTankMaster.objects.filter(pk=t.pk).update(compat_score=score)
                t.compat_score = score
        if t.validated_by_id is not None:
            stats["statuts"][t.validation_status] = stats["statuts"].get(t.validation_status, 0) + 1
            continue
        if t.source_type != "CLOSING" and t.source_batch_id and t.source_batch.source_type == "CLOSING":
            # Réparation : cuve mise à jour par le Closing mais restée marquée de sa source d'origine (refueling).
            R.log_correction("TANK", t.pk, "SYSTEM", "Cuve reprise par le référentiel Closing (source corrigée)", site_id=t.site_id,
                             field="source_type", old=t.source_type, new="CLOSING", user=user,
                             author_label=None if user else "système (règles cuves)")
            t.source_type = "CLOSING"
            if not dry_run:
                FuelTankMaster.objects.filter(pk=t.pk).update(source_type="CLOSING")
            stats["sources_corrigees"] = stats.get("sources_corrigees", 0) + 1
        spec = TC.TankSpec.from_model(t)
        multi = counts.get(t.site_id, 0) > 1 and t.source_type == "HISTORICAL_REFUELING"
        if t.source_type == "CLOSING":
            status, why = assess_closing_tank(spec, _num(t.capacity_l), multi)
        else:
            status, why = R.tank_status_for(spec, t.number_of_tanks_on_site)
            if multi:
                status, why = "PLUSIEURS_CUVES_A_IDENTIFIER", "Plusieurs cuves mentionnées pour ce site sans identifiant."
        m, mwhy = propose_model(spec, models)
        changes = {}
        if status != t.validation_status:
            changes["validation_status"] = (t.validation_status, status)
        if m is not None and t.tank_model_id is None and t.model_match_status in ("NONE", None):
            changes["tank_model"] = (None, m.tank_model_id)
        if not changes and t.source_type == "CLOSING" and status == t.validation_status and why and why != t.validation_reason:
            # Même statut, motif reformulé (règle inchangée) : on met à jour le libellé sans nouvelle entrée de statut.
            stats["motifs_mis_a_jour"] = stats.get("motifs_mis_a_jour", 0) + 1
            if not dry_run:
                FuelTankMaster.objects.filter(pk=t.pk).update(validation_reason=why)
        if changes:
            stats["changements"] += 1
            if not dry_run:
                for f, (old, new) in changes.items():
                    R.log_correction("TANK", t.pk, "SYSTEM", why if f == "validation_status" else mwhy, site_id=t.site_id,
                                     field=f, old=old, new=new, user=user, author_label=None if user else "système (règles cuves)")
                t.validation_status, t.validation_reason = status, why
                if "tank_model" in changes:
                    t.tank_model, t.model_match_status = m, "PROPOSED"
                    t.comment = ((t.comment or "") + f" Modèle proposé : {mwhy}").strip()[:2000]
                t.save()
        if m is not None:
            stats["modeles_proposes"] += 1
        stats["statuts"][status] = stats["statuts"].get(status, 0) + 1
    return stats
