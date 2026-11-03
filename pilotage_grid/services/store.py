"""Stockage versionné des jeux de données Pilotage Grid (validation, import, restauration, export)."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile

from django.db import transaction
from django.utils import timezone

from pilotage_grid.models import PilotageDataset

# clé → libellé, blocs obligatoires (structure attendue par l'interface), parties de l'interface alimentées
SPECS: dict[str, dict] = {
    "paiements": {"label": "Paiements", "required": {"lists": dict, "cube": list, "det": list, "hors": list},
                  "sections": ["Synthèse", "Paiements", "Sites", "Non parvenues"]},
    "sites": {"label": "Sites et séries mensuelles",
              "required": {"months": list, "sites": list, "ser": (list, dict), "con": (list, dict), "reps": list, "gp": (list, dict), "avg": (list, dict), "pm": (list, dict),
                           "srcs": list, "corr": dict},
              "sections": ["Synthèse", "Sites", "Fiche site", "Non parvenues", "Config Nouvelle Baie", "Contrats et contrôles"]},
    "demandes": {"label": "Sites demandés", "required": {"months": list, "sites": list, "miss": list, "nokey": (list, int), "nrows": int, "nuniq": int, "ndup": (list, int)},
                 "sections": ["Sites demandés"]},
    "efficacite": {"label": "Efficacité énergétique", "required": {"months": list, "cats": list, "sites": list, "nanalyse": (int, list), "nbdd": (int, list)},
                   "sections": ["Efficacité énergétique"]},
    "tarifs": {"label": "Changements de tarif", "required": {"rows": list, "comp": (list, dict), "bands": (list, dict), "inv": (list, dict)},
               "sections": ["Changements de tarif"]},
    "cosphi": {"label": "cos φ par site", "required": {"mon": dict, "inv": (list, dict)}, "sections": ["Fiche site"]},
    "redevance": {"label": "Redevance", "required": {"sites": list, "det": (list, dict), "mon": (list, dict), "st": (list, dict), "ctl": (list, dict), "ch6": (list, dict)},
                  "sections": ["Redevance"]},
}


class DatasetError(ValueError):
    pass


def validate(key: str, data) -> None:
    spec = SPECS.get(key)
    if spec is None:
        raise DatasetError(f"Jeu de données inconnu : {key}. Attendus : {', '.join(SPECS)}.")
    if not isinstance(data, dict):
        raise DatasetError("Le contenu doit être un objet JSON (structure du jeu de données).")
    missing = [k for k in spec["required"] if k not in data]
    if missing:
        raise DatasetError(f"Blocs manquants pour « {key} » : {', '.join(missing)}.")
    bad = [k for k, t in spec["required"].items() if not isinstance(data[k], t)]
    if bad:
        raise DatasetError(f"Type inattendu pour : {', '.join(bad)}.")
    first = next(iter(spec["required"]))
    if isinstance(data[first], (list, dict)) and not data[first]:
        raise DatasetError(f"Le bloc « {first} » est vide : import refusé (un jeu vide écraserait la base).")


def _canonical(data) -> bytes:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _counts(data: dict) -> dict:
    return {k: len(v) for k, v in data.items() if isinstance(v, (list, dict))}


def active(key: str) -> PilotageDataset | None:
    return PilotageDataset.objects.filter(key=key, is_active=True).first()


@transaction.atomic
def save_version(key: str, data, *, user=None, source: str = "import", source_file: str = "", note: str = "") -> tuple[PilotageDataset, bool]:
    """Crée une nouvelle version active. Contenu identique à la version active → aucune nouvelle version (False)."""
    validate(key, data)
    raw = _canonical(data)
    checksum = hashlib.sha256(raw).hexdigest()
    current = PilotageDataset.objects.select_for_update().filter(key=key, is_active=True).first()
    if current is not None and current.checksum == checksum:
        return current, False
    last = PilotageDataset.objects.filter(key=key).order_by("-version").values_list("version", flat=True).first() or 0
    PilotageDataset.objects.filter(key=key, is_active=True).update(is_active=False)
    ds = PilotageDataset.objects.create(
        key=key, version=last + 1, is_active=True, data=data, checksum=checksum, size_bytes=len(raw), row_counts=_counts(data),
        source=source, source_file=source_file[:255], note=note[:500], imported_by=user if getattr(user, "pk", None) else None,
        imported_by_label=(getattr(user, "email", "") or getattr(user, "username", "") or "")[:255],
    )
    return ds, True


@transaction.atomic
def restore_version(key: str, version: int, *, user=None) -> PilotageDataset:
    """Restaurer une ancienne version = en créer une nouvelle copie active (l'historique n'est jamais réécrit)."""
    old = PilotageDataset.objects.filter(key=key, version=version).first()
    if old is None:
        raise DatasetError(f"Version {version} introuvable pour « {key} ».")
    ds, _created = save_version(key, old.data, user=user, source="restore", source_file=f"v{version}", note=f"Restauration de la version {version}")
    return ds


def meta(ds: PilotageDataset | None, key: str) -> dict:
    spec = SPECS[key]
    base = {"key": key, "label": spec["label"], "sections": spec["sections"]}
    if ds is None:
        return {**base, "version": None, "disponible": False}
    return {**base, "disponible": True, "version": ds.version, "checksum": ds.checksum, "size_bytes": ds.size_bytes, "row_counts": ds.row_counts,
            "source": ds.source, "source_file": ds.source_file, "note": ds.note, "imported_by": ds.imported_by_label,
            "created_at": ds.created_at.isoformat()}


def export_zip() -> bytes:
    """Export de la base utilisée : un JSON par jeu actif + manifeste (versions, sommes de contrôle)."""
    buf = io.BytesIO()
    manifest = {"exported_at": timezone.now().isoformat(), "datasets": []}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for key in SPECS:
            ds = active(key)
            if ds is None:
                continue
            z.writestr(f"{key}.json", json.dumps(ds.data, ensure_ascii=False, separators=(",", ":")))
            manifest["datasets"].append(meta(ds, key))
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue()
