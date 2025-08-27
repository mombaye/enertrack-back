from datetime import datetime
from decimal import Decimal
import pandas as pd

from django.db import transaction
from django.db.models import Q
from rest_framework import viewsets, status, mixins
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response

from .models import ImportBatch, SonatelInvoice, MonthlySynthesis
from .serializers import (
    ImportBatchSerializer,
    SonatelInvoiceSerializer,
    MonthlySynthesisSerializer,
)
from .utils import parse_decimal_fr, iter_month_slices

# ... (COLUMN_MAP, DATE_COLS, DEC_COLS identiques)

from django.db.models import Q  # <-- ajoute ceci



# === Mapping des en-têtes Excel -> champs modèle ===
COLUMN_MAP = {
    "Numero Compte Contrat": "numero_compte_contrat",
    "Partenaire": "partenaire",
    "Localite": "localite",
    "Arrondissement": "arrondissement",
    "Rue": "rue",
    "Numero Facture": "numero_facture",
    "Date comptable Facture": "date_comptable_facture",
    "Montant Total Energie": "montant_total_energie",
    "Montant Redevance": "montant_redevance",
    "Montant TCO": "montant_tco",
    "Montant Hors TVA": "montant_hors_tva",
    "Montant TVA": "montant_tva",
    " Montant Facture TTC ": "montant_ttc",  # certains fichiers contiennent des espaces
    "Date Debut Periode Facturation": "date_debut_periode",
    "Date Fin Periode Facturation": "date_fin_periode",
    "Ancien index K1": "ancien_index_k1",
    "Ancien Index K2": "ancien_index_k2",
    "Nouvel index K1": "nouvel_index_k1",
    "Nouvel Index K2": "nouvel_index_k2",
    "Consommation Facturée": "conso_facturee",
    "AGENCE": "agence",
    "N° Compteur": "numero_compteur",
}

DATE_COLS = {"date_comptable_facture", "date_debut_periode", "date_fin_periode"}
DEC_COLS = {
    "montant_total_energie",
    "montant_redevance",
    "montant_tco",
    "montant_hors_tva",
    "montant_tva",
    "montant_ttc",
    "ancien_index_k1",
    "ancien_index_k2",
    "nouvel_index_k1",
    "nouvel_index_k2",
    "conso_facturee",
}




def _to_date(x):
    # vides / NaN
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = str(x).strip()
    if s == "" or s.lower() in {"nan", "none", "#n/a", "n/a"}:
        return None

    # objets datetime/pandas
    if isinstance(x, (pd.Timestamp, )):
        return x.date()
    from datetime import date as _date, datetime as _dt
    if isinstance(x, _date) and not isinstance(x, _dt):
        return x
    if isinstance(x, _dt):
        return x.date()

    # parse auto
    try:
        ts = pd.to_datetime(s, dayfirst=True, errors="coerce")
        if ts is not None and not pd.isna(ts):
            return ts.date()
    except Exception:
        pass

    # formats courants
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y", "%Y.%m.%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue

    # nombres Excel
    try:
        sn = s.replace(",", ".")
        if sn.replace(".", "", 1).lstrip("-").isdigit():
            val = float(sn)
            if 30000 <= val <= 60000:
                ts = pd.to_datetime(val, unit="D", origin="1899-12-30", errors="coerce")
                if ts is not None and not pd.isna(ts):
                    return ts.date()
    except Exception:
        pass

    return None


def _build_monthly_payloads(inv: SonatelInvoice):
    """
    Prépare les objets MonthlySynthesis (non sauvegardés) pour une ligne brute.
    Répartition au prorata du nombre de jours couverts dans chaque mois.
    """
    start, end = inv.date_debut_periode, inv.date_fin_periode
    if not start or not end or end < start:
        return []

    total_days = (end - start).days + 1
    if total_days <= 0:
        return []

    conso = inv.conso_facturee or Decimal("0")
    m_energie = inv.montant_total_energie or Decimal("0")
    m_ttc = inv.montant_ttc or Decimal("0")

    payloads = []
    # iter_month_slices: (y, m, seg_start, seg_end, days_in_month, days_covered)
    for y, m, _seg_start, _seg_end, _days_in_month, days_covered in iter_month_slices(start, end):
        ratio = Decimal(days_covered) / Decimal(total_days)
        payloads.append(
            MonthlySynthesis(
                source=inv,
                year=y,
                month=m,
                period_start=start,
                period_end=end,
                period_total_days=total_days,
                days_covered=days_covered,
                conso=(conso * ratio) if inv.conso_facturee is not None else None,
                montant_energie=(m_energie * ratio) if inv.montant_total_energie is not None else None,
                montant_ttc=(m_ttc * ratio) if inv.montant_ttc is not None else None,
                numero_compte_contrat=inv.numero_compte_contrat,
                numero_facture=inv.numero_facture,
            )
        )
    return payloads


class ImportBatchViewSet(viewsets.GenericViewSet, mixins.ListModelMixin):
    queryset = ImportBatch.objects.all().order_by("-imported_at")
    serializer_class = ImportBatchSerializer
    parser_classes = (MultiPartParser, FormParser)

    @action(methods=["post"], detail=False, url_path="import")
    def import_file(self, request, *args, **kwargs):
        f = request.FILES.get("file")
        if not f:
            return Response({"detail": "Aucun fichier fourni"}, status=400)

        with transaction.atomic():
            batch = ImportBatch.objects.create(source_filename=f.name)
            df = pd.read_excel(f, dtype=str)

            cols = {c.strip(): c for c in df.columns}
            rename_map = {cols.get(src, src): dst for src, dst in COLUMN_MAP.items() if src in cols}
            df = df.rename(columns=rename_map)

            created_count = 0
            updated_count = 0
            monthly_total = 0

            for _, row in df.iterrows():
                data = {}
                for k in COLUMN_MAP.values():
                    if k not in df.columns:
                        continue
                    val = row.get(k, None)
                    if k in DATE_COLS:
                        data[k] = _to_date(val)
                    elif k in DEC_COLS:
                        data[k] = parse_decimal_fr(val)
                    else:
                        data[k] = None if (pd.isna(val) or val == "") else str(val)

                if not data.get("numero_facture") or not data.get("date_debut_periode") or not data.get("date_fin_periode"):
                    continue

                existing = SonatelInvoice.objects.filter(
                    numero_compte_contrat=data.get("numero_compte_contrat"),
                    numero_facture=data.get("numero_facture"),
                    date_debut_periode=data.get("date_debut_periode"),
                    date_fin_periode=data.get("date_fin_periode"),
                ).first()

                if existing:
                    for k, v in data.items():
                        setattr(existing, k, v)
                    existing.batch = batch
                    existing.save()

                    existing.months.all().delete()
                    payloads = _build_monthly_payloads(existing)
                    MonthlySynthesis.objects.bulk_create(payloads)
                    updated_count += 1
                    monthly_total += len(payloads)
                else:
                    inv = SonatelInvoice.objects.create(batch=batch, **data)
                    payloads = _build_monthly_payloads(inv)
                    MonthlySynthesis.objects.bulk_create(payloads)
                    created_count += 1
                    monthly_total += len(payloads)

            return Response(
                {
                    "batch": ImportBatchSerializer(batch).data,
                    "rows_created": created_count,
                    "rows_updated": updated_count,
                    "monthly_rows_created": monthly_total,
                },
                status=status.HTTP_201_CREATED,
            )


class SonatelInvoiceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = SonatelInvoice.objects.select_related("batch").all().order_by("-date_comptable_facture")
    serializer_class = SonatelInvoiceSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        q = self.request.query_params.get("search")
        if q:
            qs = qs.filter(
                Q(numero_facture__icontains=q) |
                Q(numero_compte_contrat__icontains=q) |
                Q(numero_compteur__icontains=q)
            )
        return qs


class MonthlySynthesisViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = MonthlySynthesis.objects.select_related("source").all().order_by("-year", "-month")
    serializer_class = MonthlySynthesisSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        year = self.request.query_params.get("year")
        month = self.request.query_params.get("month")
        account = self.request.query_params.get("account")
        facture = self.request.query_params.get("facture")
        if year:
            qs = qs.filter(year=int(year))
        if month:
            qs = qs.filter(month=int(month))
        if account:
            qs = qs.filter(numero_compte_contrat=account)
        if facture:
            qs = qs.filter(numero_facture=facture)
        return qs
