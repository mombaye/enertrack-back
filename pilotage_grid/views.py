"""
API Pilotage Grid.

  GET  /api/pilotage-grid/datasets/                          jeux actifs (version, volumes, source, date d'import)
  GET  /api/pilotage-grid/datasets/<clé>/                    contenu actif (ETag = somme de contrôle)
  GET  /api/pilotage-grid/datasets/<clé>/versions/           historique des versions
  POST /api/pilotage-grid/datasets/<clé>/import/             mise à jour (fichier .json) — nouvelle version, l'ancienne reste restaurable
  POST /api/pilotage-grid/datasets/<clé>/versions/<n>/restore/   restaure une version (copie active)
  GET  /api/pilotage-grid/export/                            export zip de toute la base utilisée
  GET  /api/pilotage-grid/datasets/<clé>/export/             export JSON d'un jeu
"""
from __future__ import annotations

import json

from django.http import HttpResponse
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from pilotage_grid.models import PilotageDataset
from pilotage_grid.services import store as S

READ_ROLES = {"admin", "manager", "analyst"}
WRITE_ROLES = {"admin", "manager"}
MAX_IMPORT_BYTES = 40 * 1024 * 1024


class CanRead(BasePermission):
    message = "Accès réservé aux rôles admin, manager et analyst."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and getattr(request.user, "role", None) in READ_ROLES)


class CanWrite(BasePermission):
    message = "Mise à jour réservée aux rôles admin et manager."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and getattr(request.user, "role", None) in WRITE_ROLES)


def _check_key(key: str):
    if key not in S.SPECS:
        return Response({"detail": f"Jeu de données inconnu : {key}.", "attendus": list(S.SPECS)}, status=404)
    return None


class DatasetListView(APIView):
    permission_classes = [CanRead]

    def get(self, request):
        role = getattr(request.user, "role", None)
        return Response({"datasets": [S.meta(S.active(k), k) for k in S.SPECS], "can_import": role in WRITE_ROLES})


class DatasetDetailView(APIView):
    permission_classes = [CanRead]

    def get(self, request, key):
        bad = _check_key(key)
        if bad:
            return bad
        ds = S.active(key)
        if ds is None:
            return Response({"detail": f"Aucune donnée « {key} » en base : importez le fichier initial."}, status=404)
        etag = f'"{ds.checksum}"'
        if request.META.get("HTTP_IF_NONE_MATCH") == etag:
            return HttpResponse(status=304, headers={"ETag": etag})
        resp = HttpResponse(json.dumps(ds.data, ensure_ascii=False, separators=(",", ":")), content_type="application/json; charset=utf-8")
        resp["ETag"] = etag
        resp["Cache-Control"] = "private, no-cache"
        resp["X-Dataset-Version"] = str(ds.version)
        return resp


class DatasetVersionsView(APIView):
    permission_classes = [CanRead]

    def get(self, request, key):
        bad = _check_key(key)
        if bad:
            return bad
        rows = PilotageDataset.objects.filter(key=key).defer("data").order_by("-version")[:100]
        return Response({"key": key, "versions": [{**S.meta(d, key), "is_active": d.is_active} for d in rows]})


def _parse_upload(f) -> tuple[str, object]:
    """(clé éventuelle, données) depuis un fichier .json : soit la structure du jeu, soit {"key": ..., "data": ...}."""
    name = (f.name or "").lower()
    if not name.endswith(".json"):
        raise S.DatasetError(
            "Format non pris en charge pour l'instant : seul le fichier .json (export de la base) est accepté. "
            "L'import des fichiers Excel sources sera ajouté avec les fichiers initiaux.")
    if f.size > MAX_IMPORT_BYTES:
        raise S.DatasetError("Fichier trop volumineux (40 Mo maximum).")
    try:
        obj = json.loads(f.read().decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise S.DatasetError(f"JSON illisible : {e}")
    if isinstance(obj, dict) and set(obj) >= {"key", "data"}:
        return obj["key"], obj["data"]
    return "", obj


class DatasetImportView(APIView):
    permission_classes = [CanWrite]
    parser_classes = [MultiPartParser]

    def post(self, request, key):
        bad = _check_key(key)
        if bad:
            return bad
        f = request.FILES.get("file")
        if f is None:
            return Response({"detail": "Aucun fichier fourni (champ « file »)."}, status=400)
        try:
            env_key, data = _parse_upload(f)
            if env_key and env_key != key:
                raise S.DatasetError(f"Ce fichier contient le jeu « {env_key} » mais l'import vise « {key} ».")
            ds, created = S.save_version(key, data, user=request.user, source="import", source_file=f.name, note=(request.data.get("note") or ""))
        except S.DatasetError as e:
            return Response({"detail": str(e)}, status=400)
        return Response({"created": created, "unchanged": not created, "dataset": S.meta(ds, key)}, status=201 if created else 200)


class DatasetRestoreView(APIView):
    permission_classes = [CanWrite]

    def post(self, request, key, version):
        bad = _check_key(key)
        if bad:
            return bad
        try:
            ds = S.restore_version(key, int(version), user=request.user)
        except S.DatasetError as e:
            return Response({"detail": str(e)}, status=404)
        return Response({"dataset": S.meta(ds, key)})


class DatasetExportView(APIView):
    permission_classes = [CanRead]

    def get(self, request, key):
        bad = _check_key(key)
        if bad:
            return bad
        ds = S.active(key)
        if ds is None:
            return Response({"detail": "Aucune donnée en base pour ce jeu."}, status=404)
        r = HttpResponse(json.dumps(ds.data, ensure_ascii=False, separators=(",", ":")), content_type="application/json; charset=utf-8")
        r["Content-Disposition"] = f'attachment; filename="pilotage_{key}_v{ds.version}.json"'
        return r


class ExportAllView(APIView):
    permission_classes = [CanRead]

    def get(self, request):
        from django.utils import timezone

        content = S.export_zip()
        r = HttpResponse(content, content_type="application/zip")
        r["Content-Disposition"] = f'attachment; filename="pilotage_grid_base_{timezone.now():%Y%m%d_%H%M}.zip"'
        return r
