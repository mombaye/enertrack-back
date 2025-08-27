from django.shortcuts import render

# Create your views here.
# pwm/views.py
import math, re, pandas as pd
from dateutil.parser import parse as parse_date
from django.db import transaction
from django.db.models import Q
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from energy.models import Country, Site, InstallStatus
from .models import PwmReport
from .serializers import PwmReportSerializer


# --------- helpers ---------
def _norm(s: str) -> str:
    s = str(s or "").lower()
    s = re.sub(r"\[[^\]]+\]", "", s)   # enlève [unités]
    s = s.replace("°", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def to_none(v):
    if v is None: return None
    s = str(v).strip().upper()
    if s in {"NI", "NM", "NC", "N/A", "NA", "N A", "NO LAST VALUE", "", "NAN"}:
        return None
    return v

def num(v, decimals=6):
    v = to_none(v)
    if v is None or (isinstance(v, float) and math.isnan(v)): return None
    try:
        return round(float(str(v).replace(",", "")), decimals)
    except Exception:
        return None

def iint(v):
    v = to_none(v)
    if v is None: return None
    try:
        return int(float(str(v).replace(",", "")))
    except Exception:
        return None

def dmy(v):
    v = to_none(v)
    if v is None: return None
    try:
        # dans vos fichiers: 01-09-2024 etc.
        return parse_date(str(v), dayfirst=True)
    except Exception:
        return None

def hhmm_to_minutes(v):
    v = to_none(v)
    if v is None: return None
    s = str(v).strip()
    if s.count(":") == 1:
        h, m = s.split(":")
        try:
            return int(h) * 60 + int(m)
        except Exception:
            return None
    # 0;00 ou 0.00 ?
    try:
        return int(float(s))
    except Exception:
        return None

def status_from_cell(v) -> str:
    if v is None: return InstallStatus.NC
    s = str(v).strip().upper()
    if s in {"YES", "Y"}: return InstallStatus.YES
    if s in {"NO", "N"}:  return InstallStatus.NO
    if s == "NI":         return InstallStatus.NI
    if s == "NM":         return InstallStatus.NM
    if s in {"ODG", "0DG"}: return InstallStatus.ODG
    return InstallStatus.NC


# --------- ViewSet ----------
class PwmReportViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET  /api/pwm/?q=&site_id=&country=&date_from=&date_to=
    POST /api/pwm/import/  (multipart: file=.xlsx/.csv)
    """
    queryset = PwmReport.objects.select_related("site", "site__country", "country")
    serializer_class = PwmReportSerializer
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
            qs = qs.filter(period_start__gte=p["date_from"])
        if p.get("date_to"):
            qs = qs.filter(period_end__lte=p["date_to"])
        return qs.order_by("-period_start", "site__site_id")

    @action(detail=False, methods=["post"], url_path="import", parser_classes=[MultiPartParser])
    def import_file(self, request):
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "file is required"}, status=400)

        # lecture brute
        try:
            if f.name.lower().endswith((".xlsx", ".xls")):
                df_raw = pd.read_excel(f, header=None, engine="openpyxl")
            else:
                df_raw = pd.read_csv(f, header=None, sep=None, engine="python")
        except Exception as e:
            return Response({"detail": f"Read error: {e}"}, status=400)

        # -------- entête (report/start/end/country) --------
        head_text = df_raw.head(15).fillna("").astype(str).agg(" ".join, axis=1).str.cat(sep=" ")
        # ex: "Report Date: 23-07-2025 19:58   Start Date: 01-09-2024 End Date: 30-09-2024  Country Senegal"
        report_date = None
        start_date  = None
        end_date    = None
        country_name = None
        try:
            # Report Date:
            m = re.search(r"report\s*date[: ]+([0-9/\- :apmAPM]+)", head_text, re.I)
            if m: report_date = dmy(m.group(1))
        except Exception:
            pass
        try:
            m = re.search(r"start\s*date[: ]+([0-9/\- :]+)", head_text, re.I)
            if m: start_date = dmy(m.group(1))
            m = re.search(r"end\s*date[: ]+([0-9/\- :]+)", head_text, re.I)
            if m: end_date = dmy(m.group(1))
        except Exception:
            pass
        m = re.search(r"\bcountry\b[\s:]+([A-Za-z ]+)", head_text, re.I)
        if m:
            country_name = m.group(1).strip()
        # fallback pays utilisateur
        if not country_name:
            country_name = getattr(getattr(request, "user", None), "pays", None) or "Unknown"

        # -------- localiser la ligne d’en-tête du tableau --------
        header_idx = None
        for i in range(min(40, len(df_raw))):
            rown = [_norm(x) for x in df_raw.iloc[i].astype(str).tolist()]
            if "site id" in rown and "grid act pwm average power" in rown:
                header_idx = i
                break
        if header_idx is None:
            return Response({"detail": "En-tête du tableau introuvable (colonne 'Site ID' / 'GRID ACT PWM Average Power')."}, status=400)

        dfb = df_raw.iloc[header_idx:].reset_index(drop=True)
        cols = [str(c).strip() for c in dfb.iloc[0].tolist()]
        df = dfb.iloc[1:].copy()
        df.columns = cols
        df = df.where(pd.notna(df), None)  # remplace NaN par None

        # mapping des colonnes
        ncols = {c: _norm(c) for c in df.columns}

        def col_like(*cands):
            for real, nn in ncols.items():
                if nn in cands:
                    return real
            return None

        C_COUNTRY   = col_like("country")
        C_SITEID    = col_like("site id")
        C_SITENAME  = col_like("site name")
        C_SITECLASS = col_like("site class")
        C_GRID      = col_like("grid")
        C_DG        = col_like("dg")
        C_SOLAR     = col_like("solar")
        C_TYPOW     = col_like("typology power w")
        C_GRID_ACT  = col_like("grid act pwm average power w")

        # DC1..DC12
        dc_cols = {}
        for k in range(1, 13):
            target = f"dc{k} pwm average power"
            for real, nn in ncols.items():
                if nn == target:
                    dc_cols[k] = real
                    break

        C_TPWM_MIN  = col_like("total pwm minimum power")
        C_TPWM_AVG  = col_like("total pwm average power")
        C_TPWM_MAX  = col_like("total pwm maximum power")
        C_PWC_AVG   = col_like("total pwc average load power")

        C_UP_DC     = col_like("dc pwm average up time", "dc pwm average up time pct")
        C_UP_PWC    = col_like("pwc up time", "pwc up time pct")
        C_UP_ROUTER = col_like("router up time", "router up time pct")

        C_TYPO_VS   = col_like("typology load power vs pwm real load power")
        C_GRID_AV   = col_like("grid availability")
        C_NB_CUTS   = col_like("number of grid cuts cuts", "number of grid cuts")
        C_CUTS_DUR  = col_like("total grid cuts duration hh mm", "total grid cuts duration")

        created, upserted = 0, 0
        errors, unmapped = [], []

        with transaction.atomic():
            # objets pays & période
            country = Country.objects.get_or_create(name=country_name)[0]

            for _, row in df.iterrows():
                sid = (row.get(C_SITEID) or "").strip()
                if not sid or sid.lower() in {"#", "nan"}:
                    continue

                # surligne la période : si la table n’en fournit pas, on prend l’en-tête
                b = dmy(start_date) if not isinstance(start_date, str) else dmy(start_date)
                e = dmy(end_date)   if not isinstance(end_date, str) else dmy(end_date)
                # si des colonnes "Begin/End" existe dans ce type, on pourrait les prendre ici.
                if not b or not e:
                    errors.append(f"{sid}: période introuvable depuis l’en-tête")
                    continue

                site_name = (row.get(C_SITENAME) or "").strip() or sid
                site, _ = Site.objects.get_or_create(site_id=sid, defaults={"country": country, "site_name": site_name})
                if site.country_id != country.id:
                    site.country = country
                    site.save()

                defaults = dict(
                    country=country,
                    report_date=dmy(report_date) if isinstance(report_date, str) else report_date,
                    source_filename=f.name,
                    site_name=site_name,
                    site_class=(row.get(C_SITECLASS) or None),
                    grid_status=status_from_cell(row.get(C_GRID)),
                    dg_status=status_from_cell(row.get(C_DG)),
                    solar_status=status_from_cell(row.get(C_SOLAR)),
                    typology_power_w=iint(row.get(C_TYPOW)),
                    grid_act_pwm_avg_w=num(row.get(C_GRID_ACT)),
                    total_pwm_min_w=num(row.get(C_TPWM_MIN)),
                    total_pwm_avg_w=num(row.get(C_TPWM_AVG)),
                    total_pwm_max_w=num(row.get(C_TPWM_MAX)),
                    total_pwc_avg_load_w=num(row.get(C_PWC_AVG)),
                    dc_pwm_avg_uptime_pct=num(row.get(C_UP_DC)),
                    pwc_uptime_pct=num(row.get(C_UP_PWC)),
                    router_uptime_pct=num(row.get(C_UP_ROUTER)),
                    typology_load_vs_pwm_real_load_pct=num(row.get(C_TYPO_VS)),
                    grid_availability_pct=num(row.get(C_GRID_AV)),
                    number_grid_cuts=iint(row.get(C_NB_CUTS)),
                    total_grid_cuts_minutes=hhmm_to_minutes(row.get(C_CUTS_DUR)),
                )

                # DC1..DC12 dynamiquement
                for k in range(1, 13):
                    field = f"dc{k}_pwm_avg_w"
                    col = dc_cols.get(k)
                    defaults[field] = num(row.get(col)) if col else None
                    if not col:
                        unmapped.append(field)

                PwmReport.objects.update_or_create(
                    site=site, period_start=b, period_end=e, defaults=defaults
                )
                upserted += 1

        return Response({
            "upserted": upserted,
            "created": created,
            "errors": errors,
            "unmapped_fields": list(sorted(set(unmapped))),  # juste pour debug
            "header": {
                "country": country_name,
                "report_date": report_date.isoformat() if hasattr(report_date, "isoformat") else str(report_date) if report_date else None,
                "start": start_date.isoformat() if hasattr(start_date, "isoformat") else str(start_date) if start_date else None,
                "end": end_date.isoformat() if hasattr(end_date, "isoformat") else str(end_date) if end_date else None,
            }
        }, status=status.HTTP_201_CREATED if upserted else status.HTTP_200_OK)
