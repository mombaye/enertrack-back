
# energy/views.py

import io
import math
import re
import pandas as pd
from calendar import month_name
from dateutil.parser import parse as parse_date

from django.db import transaction
from django.db.models import Q
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from .models import (
    Country, Site, SiteEnergyMonthlyStat, InstallStatus, EnergyMonthlyStat
)
from .serializers import (
    SiteSerializer, SiteEnergyMonthlyStatSerializer, EnergyMonthlyStatSerializer
)



MONTHS_MAP = {m.lower(): i for i, m in enumerate(month_name) if m}
MONTHS_MAP.update({
    'januray': 1,  # tolère la faute du fichier exemple
    'january': 1
})

def safe_decimal(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        s = str(v).strip().replace(',', '')
        return round(float(s), 2)
    except Exception:
        return None

def safe_int(v):
    try:
        return int(float(v))
    except Exception:
        return None

class EnergyStatViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = EnergyMonthlyStat.objects.select_related('country').all()
    serializer_class = EnergyMonthlyStatSerializer
    parser_classes = [MultiPartParser]

    def get_queryset(self):
        qs = super().get_queryset()
        user = getattr(self.request, 'user', None)
        # Filtre par pays de l'utilisateur si disponible
        if user and getattr(user, 'pays', None):
            qs = qs.filter(country__name=user.pays)
        # Filtres optionnels: ?year=2025&month=7
        year = self.request.query_params.get('year')
        month = self.request.query_params.get('month')
        if year:
            qs = qs.filter(year=year)
        if month:
            qs = qs.filter(month=month)
        return qs

    @action(detail=False, methods=['post'], url_path='import', parser_classes=[MultiPartParser])
    def import_file(self, request):
    
        f = request.FILES.get('file')
        if not f:
            return Response({"detail": "file is required"}, status=400)

        override_country = request.data.get('country')
        override_year = request.data.get('year')
        override_report_date = request.data.get('report_date')

        name = f.name.lower()
        # --- Lecture unique (pas de f.read() ensuite)
        try:
            if name.endswith(('.xlsx', '.xls')):
                df_raw = pd.read_excel(f, header=None, engine='openpyxl')
            else:
                # autodétection séparateur CSV
                df_raw = pd.read_csv(f, header=None, sep=None, engine="python")
        except Exception as e:
            return Response({"detail": f"Read error: {e}"}, status=400)

        # --- Pré‑en‑tête pour pays/année/date
        head_text = df_raw.head(10).fillna('').astype(str).agg(' '.join, axis=1).str.cat(sep=' ')
      
        user_country = getattr(getattr(request, 'user', None), 'pays', None)
        if override_country:
            detected_country = override_country.strip()
        elif user_country:
            detected_country = user_country
        else:
            tokens = [t for t in head_text.split() if t.istitle() and len(t) >= 3]
            detected_country = tokens[0] if tokens else 'Unknown'

        if override_year:
            detected_year = int(override_year)
        else:
            yrs = re.findall(r'\b(20\d{2})\b', head_text)
            detected_year = int(yrs[0]) if yrs else None

        if override_report_date:
            try:
                detected_report_date = parse_date(override_report_date)
            except Exception:
                detected_report_date = None
        else:
            try:
                detected_report_date = parse_date(head_text, fuzzy=True)
            except Exception:
                detected_report_date = None

        # --- Localiser la ligne d'en‑tête
        header_row_idx = None
        for i in range(len(df_raw)):
            row_lower = df_raw.iloc[i].astype(str).str.lower().tolist()
            if 'month' in row_lower and any('grid energy' in c for c in row_lower):
                header_row_idx = i
                break
        if header_row_idx is None:
            return Response({"detail": "Impossible de localiser l’en-tête (colonne 'Month')."}, status=400)

        # --- Reconstruire df sans relire le fichier
        df_body = df_raw.iloc[header_row_idx:].reset_index(drop=True)
        new_columns = df_body.iloc[0].astype(str).tolist()
        df = df_body.iloc[1:].copy()
        df.columns = [str(c).strip() for c in new_columns]

        # --- Normaliser les colonnes (tolérant aux variantes/espaces/majuscules)
        def norm(s: str) -> str:
            s = s.lower().replace('°', '')  # cas exotique
            s = re.sub(r'[^a-z0-9]+', ' ', s)
            return re.sub(r'\s+', ' ', s).strip()

        norm_cols = {c: norm(str(c)) for c in df.columns}

        # Cibles attendues -> clés normalisées possibles
        wanted = {
            'month': ['month'],
            'sites_integrated': ['of sites integrated sites', '# of sites integrated sites'],
            'sites_monitored': ['no of sites monitored', 'number of sites monitored'],
            'grid_mwh': ['grid energy mwh'],
            'solar_mwh': ['solar energy mwh'],
            'generators_mwh': ['generators energy mwh'],
            'telecom_mwh': ['telecom load energy mwh'],
            'grid_pct': ['grid energy'],
            'rer_pct': ['rer renewable energy ratio'],
            'generators_pct': ['generators energy'],
            'avg_telecom_load_mw': ['avg monthly telecom load power mw'],
        }

        # Résoudre les noms réels des colonnes
        colmap = {}
        for key, candidates in wanted.items():
            found = None
            for real, nreal in norm_cols.items():
                if nreal in candidates:
                    found = real
                    break
            colmap[key] = found

        # Sanity minimal
        if not colmap['month']:
            return Response({"detail": "Colonne 'Month' introuvable."}, status=400)

        # Nettoyage valeurs
        # remplace tous NaN/NaT/pd.NA par None (compatible Django)
        df = df.where(pd.notna(df), None)
        df = df.replace(r'^\s*$', None, regex=True)

        print(df)

        # --- Pays
        country_obj, _ = Country.objects.get_or_create(name=detected_country)

        created, updated = 0, 0
        errors = []

        with transaction.atomic():
            for _, row in df.iterrows():
                m = str(row.get(colmap['month']) or '').strip()
                if not m or m.lower().startswith('total'):
                    continue

                # Tolérer la faute "Januray"
                m_idx = MONTHS_MAP.get(m.lower())
                if not m_idx:
                    try:
                        m_idx = parse_date(m).month
                    except Exception:
                        errors.append(f"Month non reconnu: {m}")
                        continue

                year_val = detected_year or (detected_report_date.year if detected_report_date else None)
                if not year_val:
                    errors.append("Année introuvable (pré‑en‑tête manquant).")
                    break

                def GD(col):  # Get decimal/int tolérant
                    val = row.get(col) if col else None
                    return safe_decimal(val)

                def GI(col):
                    val = row.get(col) if col else None
                    return safe_int(val)

                defaults = dict(
                    sites_integrated=GI(colmap['sites_integrated']),
                    sites_monitored=GI(colmap['sites_monitored']),
                    grid_mwh=GD(colmap['grid_mwh']),
                    solar_mwh=GD(colmap['solar_mwh']),
                    generators_mwh=GD(colmap['generators_mwh']),
                    telecom_mwh=GD(colmap['telecom_mwh']),
                    grid_pct=GD(colmap['grid_pct']),
                    rer_pct=GD(colmap['rer_pct']),
                    generators_pct=GD(colmap['generators_pct']),
                    avg_telecom_load_mw=GD(colmap['avg_telecom_load_mw']),
                    source_filename=f.name,
                )

                EnergyMonthlyStat.objects.update_or_create(
                    country=country_obj, year=year_val, month=m_idx, defaults=defaults
                )
                # Compteurs (créé/MAJ) — on peut tester via get() si tu veux des stats exactes
                updated += 1

        return Response({
            "country": country_obj.name,
            "year": detected_year,
            "report_date": detected_report_date.isoformat() if detected_report_date else None,
            "created": created,
            "updated": updated,
            "errors": errors
        }, status=status.HTTP_201_CREATED if (created or updated) else status.HTTP_200_OK)







# ---------- Helpers communs ----------

MONTHS_MAP = {m.lower(): i for i, m in enumerate(month_name) if m}
MONTHS_MAP.update({"januray": 1, "january": 1})  # tolère faute

def _norm_col(s: str) -> str:
    s = s.lower().replace("°", "")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def to_none_if_code(v):
    """Retourne None pour NI / NM / NC / '' ; sinon la valeur brute."""
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in {"NI", "NM", "NC", "N I", "N/A", ""}:
        return None
    return v

def safe_decimal(v, decimals=1):
    v = to_none_if_code(v)
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    try:
        s = str(v).replace(",", "").strip()
        return round(float(s), decimals)
    except Exception:
        return None

def safe_int(v):
    v = to_none_if_code(v)
    if v is None:
        return None
    try:
        return int(float(str(v).replace(",", "")))
    except Exception:
        return None

def status_from_cell(v) -> str:
    if v is None:
        return InstallStatus.NC
    s = str(v).strip().upper()
    # mapping tolérant
    if s in {"YES", "Y"}:
        return InstallStatus.YES
    if s in {"NO", "N"}:
        return InstallStatus.NO
    if s in {"NI"}:
        return InstallStatus.NI
    if s in {"NM"}:
        return InstallStatus.NM
    if s in {"0DG", "ODG"}:
        return InstallStatus.ODG
    if s in {"NC"}:
        return InstallStatus.NC
    # fallback
    return InstallStatus.NC


# ---------- ViewSet : SiteEnergyMonthlyStat ----------
def pct_guard(v, decimals=1, hard_cap=100000):
    """
    Convertit en float arrondi. Si la valeur est absurde (>= hard_cap pour Decimal(6,1)),
    on renvoie None pour éviter l'overflow DB.
    """
    val = safe_decimal(v, decimals)
    if val is None:
        return None
    if abs(val) >= hard_cap:   # 10^5 pour Decimal(6,1)
        return None
    return val


class SiteEnergyViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GET /api/site-energy/?year=&month=&country=&q=
    POST /api/site-energy/import/ (multipart file=...)
    """
    queryset = SiteEnergyMonthlyStat.objects.select_related("site", "site__country")
    serializer_class = SiteEnergyMonthlyStatSerializer
    parser_classes = [MultiPartParser]

    def get_queryset(self):
        qs = super().get_queryset()
        p = self.request.query_params

        # Filtre par pays utilisateur si dispo
        user = getattr(self.request, "user", None)
        if user and getattr(user, "pays", None):
            qs = qs.filter(site__country__name=user.pays)

        if p.get("country"):
            qs = qs.filter(site__country__name__iexact=p["country"])
        if p.get("year"):
            qs = qs.filter(year=p["year"])
        if p.get("month"):
            qs = qs.filter(month=p["month"])
        if p.get("q"):
            q = p["q"]
            qs = qs.filter(Q(site__site_id__icontains=q) | Q(site__site_name__icontains=q))
        return qs.order_by("site__site_id", "-year", "-month")

    @action(detail=False, methods=["post"], url_path="import", parser_classes=[MultiPartParser])
    def import_file(self, request):
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "file is required"}, status=400)

        override_country = request.data.get("country")
        override_year = request.data.get("year")
        override_month = request.data.get("month")
        override_report_date = request.data.get("report_date")

        # --- Lecture unique
        try:
            if f.name.lower().endswith((".xlsx", ".xls")):
                df_raw = pd.read_excel(f, header=None, engine="openpyxl")
            else:
                df_raw = pd.read_csv(f, header=None, sep=None, engine="python")
        except Exception as e:
            return Response({"detail": f"Read error: {e}"}, status=400)

        # Texte head pour détecter pays/année/mois
        head_text = df_raw.head(12).fillna("").astype(str).agg(" ".join, axis=1).str.cat(sep=" ")
        # Country
        user_country = getattr(getattr(request, "user", None), "pays", None)
        if override_country:
            detected_country = override_country.strip()
        elif user_country:
            detected_country = user_country
        else:
            # heuristique simple : 1er mot capitalisé non numérique
            tokens = [t for t in head_text.split() if t.istitle() and len(t) >= 3 and not t.isdigit()]
            detected_country = tokens[0] if tokens else "Unknown"

        # Year
        if override_year:
            detected_year = int(override_year)
        else:
            yrs = re.findall(r"\b(20\d{2})\b", head_text)
            detected_year = int(yrs[0]) if yrs else None

        # Month
        if override_month:
            m = override_month.lower()
            detected_month = MONTHS_MAP.get(m) or MONTHS_MAP.get(m[:3])
        else:
            # exemple “July” apparaît seul dans l’entête
            found = None
            for mname, midx in MONTHS_MAP.items():
                if re.search(rf"\b{re.escape(mname)}\b", head_text, flags=re.I):
                    found = midx
                    break
            detected_month = found

        # date de rapport (optionnel)
        if override_report_date:
            try:
                detected_report_date = parse_date(override_report_date)
            except Exception:
                detected_report_date = None
        else:
            try:
                detected_report_date = parse_date(head_text, fuzzy=True)
            except Exception:
                detected_report_date = None

        if not detected_year:
            return Response({"detail": "Année introuvable dans l’en-tête ; préciser ?year=... si besoin."}, status=400)
        if not detected_month:
            return Response({"detail": "Mois introuvable dans l’en-tête ; préciser ?month=... si besoin."}, status=400)

        # --- Trouver la ligne d'en-tête tableau (celle qui contient 'Site ID' / 'Site Name')
        header_idx = None
        for i in range(len(df_raw)):
            row = [str(x) for x in df_raw.iloc[i].tolist()]
            row_norm = [_norm_col(x) for x in row]
            if "site id" in row_norm and "site name" in row_norm:
                header_idx = i
                break
        if header_idx is None:
            return Response({"detail": "Ligne d’en-tête 'Site ID / Site Name' introuvable."}, status=400)

        df_body = df_raw.iloc[header_idx:].reset_index(drop=True)
        columns = [str(c).strip() for c in df_body.iloc[0].tolist()]
        df = df_body.iloc[1:].copy()
        df.columns = columns

        # mapping des colonnes (tolérant aux variations)
        norm_cols = {c: _norm_col(c) for c in df.columns}
        def col_like(*cands):
            for real, normed in norm_cols.items():
                if normed in cands:
                    return real
            return None

        C_SITE_ID   = col_like("site id")
        C_SITE_NAME = col_like("site name")
        C_GRID      = col_like("grid")
        C_DG        = col_like("dg")
        C_SOLAR     = col_like("solar")
        C_GRID_KWH  = col_like("grid energy kwh")
        C_SOLAR_KWH = col_like("solar energy kwh")
        C_TEL_KWH   = col_like("telecom load energy kwh")
        C_GRID_PCT  = col_like("grid energy")
        C_RER_PCT   = col_like("rer renewable energy ratio")
        C_ROUTER    = col_like("router monitoring availability")
        C_PWM       = col_like("pwm monitoring availability")
        C_PWC       = col_like("pwc monitoring availability")

        required = [C_SITE_ID, C_SITE_NAME, C_GRID, C_DG, C_SOLAR]
        if any(c is None for c in required):
            return Response({"detail": "Colonnes essentielles manquantes (Site ID, Site Name, GRID, DG, Solar)."}, status=400)

        df = df.where(pd.notna(df), None)

        # Pays
        country, _ = Country.objects.get_or_create(name=detected_country)

        created, upserted = 0, 0
        errors = []

        with transaction.atomic():
            for _, row in df.iterrows():
                sid = (row.get(C_SITE_ID) or "").strip()
                sname = (row.get(C_SITE_NAME) or "").strip()
                if not sid or sid.lower().startswith("total"):
                    continue

                # Site référentiel
                site, _ = Site.objects.get_or_create(
                    site_id=sid,
                    defaults={"country": country, "site_name": sname or sid},
                )
                # si le pays change / nom change, on peut update légèrement
                changed = False
                if site.country_id != country.id:
                    site.country = country
                    changed = True
                if sname and site.site_name != sname:
                    site.site_name = sname
                    changed = True
                if changed:
                    site.save()

                # Statuts & valeurs numériques
                grid_status  = status_from_cell(row.get(C_GRID))
                dg_status    = status_from_cell(row.get(C_DG))
                solar_status = status_from_cell(row.get(C_SOLAR))

                grid_kwh    = safe_int(row.get(C_GRID_KWH))
                solar_kwh   = safe_int(row.get(C_SOLAR_KWH))
                tel_kwh     = safe_int(row.get(C_TEL_KWH))
                grid_pct    = pct_guard(row.get(C_GRID_PCT), decimals=1)
                rer_pct     = pct_guard(row.get(C_RER_PCT), decimals=1)
                router_pct  = pct_guard(row.get(C_ROUTER), decimals=1)
                pwm_pct     = pct_guard(row.get(C_PWM), decimals=1)
                pwc_pct     = pct_guard(row.get(C_PWC), decimals=1)

                obj, _ = SiteEnergyMonthlyStat.objects.update_or_create(
                    site=site, year=detected_year, month=detected_month,
                    defaults=dict(
                        grid_status=grid_status,
                        dg_status=dg_status,
                        solar_status=solar_status,
                        grid_energy_kwh=grid_kwh,
                        solar_energy_kwh=solar_kwh,
                        telecom_load_kwh=tel_kwh,
                        grid_energy_pct=grid_pct,
                        rer_pct=rer_pct,
                        router_availability_pct=router_pct,
                        pwm_availability_pct=pwm_pct,
                        pwc_availability_pct=pwc_pct,
                        source_filename=f.name,
                    )
                )
                upserted += 1

        return Response({
            "country": country.name,
            "year": detected_year,
            "month": detected_month,
            "report_date": detected_report_date.isoformat() if detected_report_date else None,
            "upserted": upserted,
            "errors": errors,
        }, status=status.HTTP_201_CREATED if upserted else status.HTTP_200_OK)
