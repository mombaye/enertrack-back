import datetime
import math
from rest_framework import viewsets
from .models import Facture
from .serializers import FactureSerializer
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import action
from django.db.models import Avg, Count
import pandas as pd
from core.models import Site
import decimal


class FactureViewSet(viewsets.ModelViewSet):
    queryset = Facture.objects.all().order_by('-date_facture')
    serializer_class = FactureSerializer

    def get_queryset(self):
        user_country = self.request.user.pays
        qs = Facture.objects.filter(site__country=user_country).order_by('-date_facture')
        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")
        if start_date:
            qs = qs.filter(date_facture__gte=parse_date(start_date))
        if end_date:
            qs = qs.filter(date_facture__lte=parse_date(end_date))
        return qs

    @action(detail=False, methods=["get"], url_path="stats")
    def stats(self, request):
        user_country = self.request.user.pays
        qs = Facture.objects.filter(site__country=user_country)

        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")
        if start_date:
            qs = qs.filter(date_facture__gte=parse_date(start_date))
        if end_date:
            qs = qs.filter(date_facture__lte=parse_date(end_date))

        # Statistiques groupées par site
        stats = qs.values(
            'site',
            'site__name',
            'site__site_id'
        ).annotate(
            avg_montant_ht=Avg('montant_ht'),
            avg_montant_tco=Avg('montant_tco'),
            avg_montant_redevance=Avg('montant_redevance'),
            avg_montant_tva=Avg('montant_tva'),
            avg_montant_ttc=Avg('montant_ttc'),
            avg_montant_htva=Avg('montant_htva'),
            avg_consommation=Avg('consommation_kwh'),
            count=Count('id'),
        ).order_by('site__name')

        return Response(list(stats))

    @action(detail=False, methods=["get"], url_path="between")
    def between(self, request):
        """
        Renvoie la liste brute (pas paginée) des factures entre deux dates (start_date, end_date)
        """
        user_country = self.request.user.pays
        qs = Facture.objects.filter(site__country=user_country)
        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")
        if start_date:
            qs = qs.filter(date_facture__gte=parse_date(start_date))
        if end_date:
            qs = qs.filter(date_facture__lte=parse_date(end_date))
        data = FactureSerializer(qs, many=True).data
        return Response(data)




def safe_decimal(val):
    # Gère aussi NaN et None
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    try:
        return decimal.Decimal(str(val).replace(',', '.'))
    except (TypeError, decimal.InvalidOperation, ValueError):
        return None

def safe_float(val):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None

def safe_int(val):
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def safe_date(val):
    """
    Gère les dates sous forme string, datetime, float Excel, ou None.
    """
    # Cas 1 : datetime.datetime ou datetime.date natif
    if isinstance(val, (datetime.datetime, datetime.date)):
        return val if isinstance(val, datetime.date) else val.date()
    # Cas 2 : Numérique Excel (nombre de jours depuis 1899-12-30)
    if isinstance(val, (float, int)) and not pd.isna(val):
        # Excel start = 1899-12-30
        excel_start = datetime.datetime(1899, 12, 30)
        try:
            return (excel_start + datetime.timedelta(days=int(val))).date()
        except Exception:
            return None
    # Cas 3 : String (y compris format français 01/02/2025 ou ISO 2025-02-01)
    if isinstance(val, str):
        val = val.strip()
        if not val:
            return None
        # D'abord tente parse_date (ISO)
        d = parse_date(val)
        if d:
            return d
        # Puis tente format FR
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                return datetime.datetime.strptime(val, fmt).date()
            except Exception:
                continue
        # Ajoute ici d'autres formats si besoin
    return None


def safe_str(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return str(val).strip()

class FactureImportView(APIView):
    parser_classes = [MultiPartParser]

    def post(self, request, format=None):
        file = request.FILES.get('file')
        if not file:
            return Response({"error": "No file provided."}, status=400)

        df = pd.read_excel(file)
        created = 0

        for _, row in df.iterrows():
            try:
                site = Site.objects.get(name=row['SITE'])
            except Site.DoesNotExist:
                continue

            date_facture = safe_date(row.get('DATE FACTURE'))
            if not date_facture:

                # Option 1 : on saute la ligne
                continue

            Facture.objects.update_or_create(
                facture_number=safe_str(row['FACTURE']),
                defaults={
                    'site': site,
                    'police_number': safe_str(row.get('N° POLICE')),
                    'contrat_number': safe_str(row.get('N°COMPTE CONTRAT')),
                    'date_facture': safe_date(row.get('DATE FACTURE')),
                    'date_echeance': safe_date(row.get('ÉCHÉANCE')),
                    'montant_ht': safe_decimal(row.get('MONTANT HT')),
                    'montant_tco': safe_decimal(row.get('MONTANT TCO')),
                    'montant_redevance': safe_decimal(row.get('MONTANT REDEVANCE')),
                    'montant_tva': safe_decimal(row.get('MONTANT TVA')),
                    'montant_ttc': safe_decimal(row.get('MONTANT TTC')),
                    'montant_htva': safe_decimal(row.get('MONTANT HTVA')),
                    'montant_energie': safe_decimal(row.get('MONTANT ENERGIE')),
                    'montant_cosphi': safe_decimal(row.get('MONTANT COSPHI')),
                    'date_ai': safe_date(row.get('DATE AI')),
                    'date_ni': safe_date(row.get('DATE NI')),
                    'index_ai_k1': safe_int(row.get('INDEX AI K1')),
                    'index_ai_k2': safe_int(row.get('INDEX AI K2')),
                    'index_ni_k1': safe_int(row.get('INDEX NI K1')),
                    'index_ni_k2': safe_int(row.get('INDEX NI K2')),
                    'consommation_kwh': safe_decimal(row.get('CONSOMMATION KWH')),
                    'rappel_majoration': safe_decimal(row.get('RAPPEL MAJORATION')),
                    'nb_jours': safe_int(row.get('NOMBRE DE JOURS')),
                    'ps': safe_float(row.get('PS')),
                    'max_relevee': safe_float(row.get('MAX RELEVEE')),
                    'statut': safe_str(row.get('STATUT')),
                    'observation': safe_str(row.get('OBSERVATION')),
                    'prime_fixe': safe_decimal(row.get('PRIME FIXE')),
                    'conso_reactif': safe_decimal(row.get('CONSO REACTIF')),
                    'cos_phi': safe_float(row.get('COS PHI')),
                    'mois_echeance': safe_str(row.get('MOIS ECHEANCE')),
                    'annee_echeance': safe_int(row.get('ANNEE ECHEANCE')),
                    'mois_business': safe_str(row.get('MOIS BUSINESS')),
                    'annee_business': safe_int(row.get('ANNÉE')),
                    'type_tarif': safe_str(row.get('TYPE DE TARIF')),
                    'type_compte': safe_str(row.get('TYPE COMPTE')),
                    'numero_compteur': safe_str(row.get('N° COMPTEUR')),
                }
            )

            created += 1

        return Response({"message": f"{created} factures importées."}, status=201)
