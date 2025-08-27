import datetime
import math
from celery.result import AsyncResult
from rest_framework import viewsets

from invoices.tasks import import_factures_task
from invoices.utils.parsers import safe_date, safe_decimal, safe_float, safe_int, safe_str
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
import traceback
from django.utils import timezone


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
    


    @action(detail=False, methods=["get"], url_path="kpi-stats")
    def kpi_stats(self, request):
        """
        Pour chaque site, retourne les moyennes (HT, TTC, consommation)
        sur les 3 derniers mois, année en cours et année précédente.
        """
        user_country = self.request.user.pays
        today = timezone.now().date()
        start_year = today.replace(month=1, day=1)
        start_3_months = (today.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)  # 1er du mois dernier
        start_3_months = (start_3_months - datetime.timedelta(days=2*30)).replace(day=1)  # Environ 3 mois avant

        prev_year_start = (today.replace(year=today.year - 1, month=1, day=1))
        prev_year_end = start_year - datetime.timedelta(days=1)

        # Prépare le résultat
        results = []
        sites = Site.objects.filter(country=user_country)

        for site in sites:
            qs = Facture.objects.filter(site=site)

            # 3 derniers mois
            last_3m = qs.filter(date_facture__gte=start_3_months, date_facture__lte=today)
            # Année en cours
            current_year = qs.filter(date_facture__gte=start_year, date_facture__lte=today)
            # Année précédente
            previous_year = qs.filter(date_facture__gte=prev_year_start, date_facture__lte=prev_year_end)

            results.append({
                "site_id": site.id,
                "site_name": site.name,
                "kpi_last_3_months": {
                    "avg_montant_ht": last_3m.aggregate(avg=Avg("montant_ht"))["avg"] or 0,
                    "avg_montant_ttc": last_3m.aggregate(avg=Avg("montant_ttc"))["avg"] or 0,
                    "avg_consommation_kwh": last_3m.aggregate(avg=Avg("consommation_kwh"))["avg"] or 0,
                },
                "kpi_current_year": {
                    "avg_montant_ht": current_year.aggregate(avg=Avg("montant_ht"))["avg"] or 0,
                    "avg_montant_ttc": current_year.aggregate(avg=Avg("montant_ttc"))["avg"] or 0,
                    "avg_consommation_kwh": current_year.aggregate(avg=Avg("consommation_kwh"))["avg"] or 0,
                },
                "kpi_previous_year": {
                    "avg_montant_ht": previous_year.aggregate(avg=Avg("montant_ht"))["avg"] or 0,
                    "avg_montant_ttc": previous_year.aggregate(avg=Avg("montant_ttc"))["avg"] or 0,
                    "avg_consommation_kwh": previous_year.aggregate(avg=Avg("consommation_kwh"))["avg"] or 0,
                },
            })

      
        return Response(results)


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
        Renvoie la liste brute (pas paginée) des factures entre deux dates (start_date, end_date),
        triée par nom de site puis date décroissante.
        """
        user_country = self.request.user.pays
        qs = Facture.objects.filter(site__country=user_country)
        start_date = self.request.query_params.get("start_date")
        end_date = self.request.query_params.get("end_date")
        if start_date:
            qs = qs.filter(date_facture__gte=parse_date(start_date))
        if end_date:
            qs = qs.filter(date_facture__lte=parse_date(end_date))
        qs = qs.order_by('site__name', '-date_facture')  # <---- ici le tri !
        data = FactureSerializer(qs, many=True).data
        return Response(data)






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
                    "typologie": safe_str(row.get('TYPOLOGIE')),
                    "categorie": safe_str(row.get('CATEGORIE')),
                    "societe": safe_str(row.get('SOCIÉTÉ')),
                    "type_police": safe_str(row.get('TYPE POLICE')),
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
                    'consommation_kwh': safe_decimal(row.get('CONS FACTURÉE')),
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


class FactureAsyncImportView(APIView):
    parser_classes = [MultiPartParser]

    def post(self, request):
        file = request.FILES.get('file')
        if not file:
            return Response({"error": "No file provided"}, status=400)
        task = import_factures_task.delay(file.read())
        return Response({"task_id": task.id}, status=202)


class ImportStatusView(APIView):
    def get(self, request, task_id):
        result = AsyncResult(task_id)
        if result.ready():
            data = result.result
            # Si c'est une exception, convertis en texte complet (traceback)
            if isinstance(data, Exception):
                data = ''.join(traceback.format_exception_only(type(data), data))
            return Response({
                "status": result.status,
                "result": data
            })
        return Response({"status": result.status})
