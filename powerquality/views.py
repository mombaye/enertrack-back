# powerquality/views.py
import math
import re
import pandas as pd
from dateutil.parser import parse as parse_date

from django.db import transaction
from django.db.models import Q
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from energy.models import Country, Site
from powerquality.models import PQReport
from powerquality.serializers import PQReportSerializer


# -----------------------------
# Helpers
# -----------------------------
def _norm(s: str) -> str:
    """Normalise un libellé de colonne (retire unités [..], accents,
    ponctuation, espaces multiples)."""
    s = str(s or "")
    s = s.lower()
    s = re.sub(r"\[[^\]]+\]", "", s)         # retire [unités]
    s = s.replace("°", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def to_none(v):
    """Convertit codes 'N/A', 'No Last Value', vide, NaN en None."""
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in {
        "N/A", "NA", "N A", "N A.", "N A/", "N/A/", "",
        "NO LAST VALUE", "N A N", "NAN"
    }:
        return None
    return v


def num(v, decimals=6):
    """Parse nombre tolérant (virgules, N/A)."""
    v = to_none(v)
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        return round(float(str(v).replace(",", "")), decimals)
    except Exception:
        return None


def dt(v):
    """Parse date/jour-mois-année tolérant."""
    v = to_none(v)
    if v is None:
        return None
    try:
        return parse_date(str(v), dayfirst=True)
    except Exception:
        return None


# -----------------------------
# ViewSet
# -----------------------------
class PQReportViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET  /api/pq/?q=&site_id=&country=&date_from=&date_to=
    POST /api/pq/import/ (file=.xlsx/.csv)
    """
    queryset = PQReport.objects.select_related("site", "site__country", "country")
    serializer_class = PQReportSerializer
    parser_classes = [MultiPartParser]

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params

        user = getattr(self.request, "user", None)
        if user and getattr(user, "pays", None):
            qs = qs.filter(country__name=user.pays)

        if p.get("country"):
            qs = qs.filter(country__name__iexact=p["country"])
        if p.get("site_id"):
            qs = qs.filter(site__site_id__iexact=p["site_id"])
        if p.get("q"):
            q = p["q"]
            qs = qs.filter(Q(site__site_id__icontains=q) | Q(site__site_name__icontains=q))
        if p.get("date_from"):
            qs = qs.filter(begin_period__gte=p["date_from"])
        if p.get("date_to"):
            qs = qs.filter(end_period__lte=p["date_to"])

        return qs.order_by("-begin_period", "site__site_id")

    @action(detail=False, methods=["post"], url_path="import", parser_classes=[MultiPartParser])
    def import_file(self, request):
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "file is required"}, status=400)

        # --- Lecture brute
        try:
            if f.name.lower().endswith((".xlsx", ".xls")):
                df_raw = pd.read_excel(f, header=None, engine="openpyxl")
            else:
                df_raw = pd.read_csv(f, header=None, sep=None, engine="python")
        except Exception as e:
            return Response({"detail": f"Read error: {e}"}, status=400)

        # --- Trouver la ligne qui contient "Country / Site ID / Begin Period"
        header_idx = None
        scan_max = min(35, len(df_raw))
        for i in range(scan_max):
            row_norm = [_norm(x) for x in df_raw.iloc[i].astype(str).tolist()]
            if "country" in row_norm and "site id" in row_norm and (
                "begin period 00h00" in row_norm or "begin period" in row_norm
            ):
                header_idx = i
                break

        if header_idx is None:
            return Response(
                {"detail": "Entête introuvable (Country / Site ID / Begin Period)."},
                status=400,
            )

        # --- Construit colonnes à partir de 2 lignes d'en-tête
        dfb = df_raw.iloc[header_idx:].reset_index(drop=True)
        # Lignes 0 et 1 = en-têtes (groupe & libellés)
        h0 = dfb.iloc[0].astype(str).replace({"nan": "", "None": ""}).str.strip()
        h1 = dfb.iloc[1].astype(str).replace({"nan": "", "None": ""}).str.strip()

        # Propage 'MonoPhase' / 'TriPhase' / 'TriPhase 2' vers la droite
        group = h0.replace("", pd.NA).fillna(method="ffill")

        # Concat groupe + sous-libellé → "monophase vavg v", "triphase active energy consumed kwh", ...
        combined = (group + " " + h1).str.strip()
        combined = combined.where(combined != "", h1)
        combined = combined.fillna("")

        # Données
        df = dfb.iloc[2:].copy()  # saute les 2 lignes d’en-tête
        df.columns = [str(c) for c in combined]
        df = df.where(pd.notna(df), None)

        # Mapping robuste
        ncols = {c: _norm(c) for c in df.columns}

        def col_like(*cands):
            """Match exact normalisé; sinon fallback 'contains all tokens'."""
            # exact
            for real, normed in ncols.items():
                for cand in cands:
                    if normed == cand:
                        return real
            # contains all tokens
            for real, normed in ncols.items():
                for cand in cands:
                    toks = cand.split()
                    if all(t in normed for t in toks):
                        return real
            return None

        # Colonnes clefs
        C_COUNTRY = col_like("country")
        C_SITEID = col_like("site id")
        C_BEGIN = col_like("begin period 00h00", "begin period")
        C_END = col_like("end period 23h59", "end period")
        C_EXTRACT = col_like("extract date")

        # Mono
        M = {
            "mono_vmin_v":               col_like("monophase vmin v"),
            "mono_vavg_v":               col_like("monophase vavg v"),
            "mono_vmax_v":               col_like("monophase vmax v"),
            "mono_imin_a":               col_like("monophase imin a"),
            "mono_iavg_a":               col_like("monophase iavg a"),
            "mono_imax_a":               col_like("monophase imax a"),
            "mono_pmin_kw":              col_like("monophase pmin kw"),
            "mono_pavg_kw":              col_like("monophase pavg kw"),
            "mono_pmax_kw":              col_like("monophase pmax kw"),
            "mono_total_energy_kwh":     col_like("monophase total energy kwh"),
            "mono_energy_consumed_kwh":  col_like("monophase energy consumed kwh"),
        }

        # Tri (synonymes pour 'consumed/produced')
        T = dict(
            tri_vmin_u1_v = col_like("triphase vmin u1 v"),
            tri_vavg_u1_v = col_like("triphase vavg u1 v"),
            tri_vmax_u1_v = col_like("triphase vmax u1 v"),
            tri_vmin_u2_v = col_like("triphase vmin u2 v"),
            tri_vavg_u2_v = col_like("triphase vavg u2 v"),
            tri_vmax_u2_v = col_like("triphase vmax u2 v"),
            tri_vmin_u3_v = col_like("triphase vmin u3 v"),
            tri_vavg_u3_v = col_like("triphase vavg u3 v"),
            tri_vmax_u3_v = col_like("triphase vmax u3 v"),
            tri_imin_i1_a = col_like("triphase imin i1 a"),
            tri_iavg_i1_a = col_like("triphase iavg i1 a"),
            tri_imax_i1_a = col_like("triphase imax i1 a"),
            tri_imin_i2_a = col_like("triphase imin i2 a"),
            tri_iavg_i2_a = col_like("triphase iavg i2 a"),
            tri_imax_i2_a = col_like("triphase imax i2 a"),
            tri_imin_i3_a = col_like("triphase imin i3 a"),
            tri_iavg_i3_a = col_like("triphase iavg i3 a"),
            tri_imax_i3_a = col_like("triphase imax i3 a"),
            tri_pmin_kw   = col_like("triphase pmin kw"),
            tri_pavg_kw   = col_like("triphase pavg kw"),
            tri_pmax_kw   = col_like("triphase pmax kw"),
            tri_total_energy_kwh      = col_like("triphase total energy kwh"),
            tri_active_energy_kwh     = col_like(
                "triphase active energy consumed kwh",
                "triphase active energy kwh"),
            tri_reactive_energy_kvarh = col_like(
                "triphase reactive energy consumed kvarh",
                "triphase reactive energy kvarh"),
            tri_apparent_energy_kvah  = col_like(
                "triphase apparent energy produced kvah",
                "triphase apparent energy kvah"),
        )

        # Tri 2
        T2 = dict(
            tri2_vmin_u1_v = col_like("triphase 2 vmin u1 v"),
            tri2_vavg_u1_v = col_like("triphase 2 vavg u1 v"),
            tri2_vmax_u1_v = col_like("triphase 2 vmax u1 v"),
            tri2_vmin_u2_v = col_like("triphase 2 vmin u2 v"),
            tri2_vavg_u2_v = col_like("triphase 2 vavg u2 v"),
            tri2_vmax_u2_v = col_like("triphase 2 vmax u2 v"),
            tri2_vmin_u3_v = col_like("triphase 2 vmin u3 v"),
            tri2_vavg_u3_v = col_like("triphase 2 vavg u3 v"),
            tri2_vmax_u3_v = col_like("triphase 2 vmax u3 v"),
            tri2_imin_i1_a = col_like("triphase 2 imin i1 a"),
            tri2_iavg_i1_a = col_like("triphase 2 iavg i1 a"),
            tri2_imax_i1_a = col_like("triphase 2 imax i1 a"),
            tri2_imin_i2_a = col_like("triphase 2 imin i2 a"),
            tri2_iavg_i2_a = col_like("triphase 2 iavg i2 a"),
            tri2_imax_i2_a = col_like("triphase 2 imax i2 a"),
            tri2_imin_i3_a = col_like("triphase 2 imin i3 a"),
            tri2_iavg_i3_a = col_like("triphase 2 iavg i3 a"),
            tri2_imax_i3_a = col_like("triphase 2 imax i3 a"),
            tri2_pmin_kw   = col_like("triphase 2 pmin kw"),
            tri2_pavg_kw   = col_like("triphase 2 pavg kw"),
            tri2_pmax_kw   = col_like("triphase 2 pmax kw"),
            tri2_total_energy_kwh      = col_like("triphase 2 total energy kwh"),
            tri2_active_energy_kwh     = col_like(
                "triphase 2 active energy consumed kwh",
                "triphase 2 active energy kwh"),
            tri2_reactive_energy_kvarh = col_like(
                "triphase 2 reactive energy consumed kvarh",
                "triphase 2 reactive energy kvarh"),
            tri2_apparent_energy_kvah  = col_like(
                "triphase 2 apparent energy produced kvah",
                "triphase 2 apparent energy kvah"),
        )

        # Optionnel: log des colonnes non mappées (utile pour débogage)
        unmapped = [k for k, v in {**M, **T, **T2}.items() if v is None]
        # print("Unmapped fields:", unmapped)

        created = 0
        upserted = 0
        errors = []

        with transaction.atomic():
            for _, row in df.iterrows():
                sid = (row.get(C_SITEID) or "").strip()
                if not sid:
                    continue

                country_name = (
                    (row.get(C_COUNTRY) or "").strip()
                    or getattr(getattr(request, "user", None), "pays", None)
                    or "Unknown"
                )
                country, _ = Country.objects.get_or_create(name=country_name)

                site, _ = Site.objects.get_or_create(
                    site_id=sid, defaults={"country": country, "site_name": sid}
                )
                if site.country_id != country.id:
                    site.country = country
                    site.save()

                b = dt(row.get(C_BEGIN))
                e = dt(row.get(C_END))
                if not b or not e:
                    errors.append(
                        f"{sid}: période invalide -> {row.get(C_BEGIN)} / {row.get(C_END)}"
                    )
                    continue
                xdate = dt(row.get(C_EXTRACT))

                defaults = dict(
                    country=country,
                    extract_date=xdate,
                    source_filename=f.name,
                )

                # Mono
                for field, col in M.items():
                    defaults[field] = num(row.get(col)) if col else None
                # Tri
                for field, col in T.items():
                    defaults[field] = num(row.get(col)) if col else None
                # Tri 2
                for field, col in T2.items():
                    defaults[field] = num(row.get(col)) if col else None

                PQReport.objects.update_or_create(
                    site=site, begin_period=b, end_period=e, defaults=defaults
                )
                upserted += 1

        return Response(
            {
                "upserted": upserted,
                "created": created,
                "errors": errors,
                "unmapped_fields": unmapped,  # utile pour voir ce qui manque, peut être retiré
            },
            status=status.HTTP_201_CREATED if upserted else status.HTTP_200_OK,
        )
