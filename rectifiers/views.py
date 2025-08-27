# rectifiers/views.py
import math
import re
import pandas as pd
from dateutil.parser import parse as parse_date

from django.db import transaction, DataError
from django.db.models import Q
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from energy.models import Country, Site
from .models import RectifierReading
from .serializers import RectifierReadingSerializer


def _norm_col(s: str) -> str:
    s = str(s or "").lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def to_none_if_code(v):
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in {"NI", "NM", "NC", "N/A", ""}:
        return None
    return v

def safe_decimal(v, decimals=6):
    v = to_none_if_code(v)
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return round(float(str(v).replace(",", "")), decimals)
    except Exception:
        return None

def safe_dt(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        # pandas to_datetime gère bien beaucoup de formats, mais ici on reste simple
        return parse_date(str(v))
    except Exception:
        return None


class RectifierReadingViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/rectifiers/?q=&site_id=&country=&param=&date_from=&date_to=
    POST /api/rectifiers/import/  (multipart: file=... [, country=...])
    """
    queryset = RectifierReading.objects.select_related("site", "site__country", "country")
    serializer_class = RectifierReadingSerializer
    parser_classes = [MultiPartParser]

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params

        # Filtre par pays de l'utilisateur si défini (comme energy)
        user = getattr(self.request, "user", None)
        if user and getattr(user, "pays", None):
            qs = qs.filter(country__name=user.pays)

        if p.get("country"):
            qs = qs.filter(country__name__iexact=p["country"])
        if p.get("site_id"):
            qs = qs.filter(site__site_id__iexact=p["site_id"])
        if p.get("param"):
            qs = qs.filter(param_name__iexact=p["param"])
        if p.get("q"):
            q = p["q"]
            qs = qs.filter(
                Q(site__site_id__icontains=q) |
                Q(site__site_name__icontains=q) |
                Q(param_name__icontains=q)
            )
        if p.get("date_from"):
            qs = qs.filter(measured_at__gte=p["date_from"])
        if p.get("date_to"):
            qs = qs.filter(measured_at__lte=p["date_to"])

        return qs.order_by("-measured_at", "site__site_id")

    @action(detail=False, methods=["post"], url_path="import", parser_classes=[MultiPartParser])
    def import_file(self, request):
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "file is required"}, status=400)

        override_country = request.data.get("country")

        # Lecture Excel/CSV
        try:
            if f.name.lower().endswith((".xlsx", ".xls")):
                df_raw = pd.read_excel(f, header=None, engine="openpyxl")
            else:
                df_raw = pd.read_csv(f, header=None, sep=None, engine="python")
        except Exception as e:
            return Response({"detail": f"Read error: {e}"}, status=400)

        # trouver la ligne d'en-tête contenant 'Country' et 'Site ID'
        header_idx = None
        for i in range(min(len(df_raw), 30)):
            row_norm = [_norm_col(x) for x in df_raw.iloc[i].astype(str).tolist()]
            if "country" in row_norm and "site id" in row_norm and "param name" in row_norm:
                header_idx = i
                break
        if header_idx is None:
            return Response({"detail": "En-tête introuvable (attendu: Country, Site ID, Param Name, Param Value, Measure, Date)."}, status=400)

        df_body = df_raw.iloc[header_idx:].reset_index(drop=True)
        columns = [str(c).strip() for c in df_body.iloc[0].tolist()]
        df = df_body.iloc[1:].copy()
        df.columns = columns
        df = df.where(pd.notna(df), None)

        # map colonnes
        norm_cols = {c: _norm_col(c) for c in df.columns}
        def col_like(*cands):
            for real, normed in norm_cols.items():
                if normed in cands:
                    return real
            return None

        C_COUNTRY = col_like("country")
        C_SITE_ID = col_like("site id")
        C_PARAM   = col_like("param name")
        C_VALUE   = col_like("param value", "value")
        C_MEASURE = col_like("measure", "unit")
        C_DATE    = col_like("date", "timestamp", "time")

        required = [C_SITE_ID, C_PARAM, C_VALUE, C_DATE]
        if any(c is None for c in required):
            return Response({"detail": "Colonnes essentielles manquantes (Site ID, Param Name, Param Value, Date)."}, status=400)

        created = 0
        upserted = 0
        errors = []

        with transaction.atomic():
            for _, row in df.iterrows():
                sid = (row.get(C_SITE_ID) or "").strip()
                if not sid:
                    continue

                raw_country = (row.get(C_COUNTRY) or "").strip()
                country_name = override_country or raw_country or getattr(getattr(request, "user", None), "pays", None) or "Unknown"
                country, _ = Country.objects.get_or_create(name=country_name)

                # Référentiel site (si non présent, on le crée avec le pays)
                site, _ = Site.objects.get_or_create(
                    site_id=sid,
                    defaults={"country": country, "site_name": sid},
                )
                if site.country_id != country.id:
                    site.country = country
                    site.save()

                param_name  = (row.get(C_PARAM) or "").strip()
                param_value = safe_decimal(row.get(C_VALUE), decimals=6)
                measure     = (row.get(C_MEASURE) or "").strip()
                measured_at = safe_dt(row.get(C_DATE))
                if not measured_at:
                    errors.append(f"{sid}: date illisible -> {row.get(C_DATE)}")
                    continue

                try:
                    obj, created_flag = RectifierReading.objects.update_or_create(
                        site=site, param_name=param_name, measured_at=measured_at,
                        defaults=dict(
                            country=country,
                            param_value=param_value,
                            measure=measure,
                            source_filename=f.name,
                        )
                    )
                    upserted += 1
                    if created_flag:
                        created += 1
                except DataError as e:
                    errors.append(f"{sid} {measured_at}: overflow/invalid value -> {row.get(C_VALUE)}")
                    continue

        return Response({
            "upserted": upserted,
            "created": created,
            "errors": errors,
        }, status=status.HTTP_201_CREATED if upserted else status.HTTP_200_OK)
