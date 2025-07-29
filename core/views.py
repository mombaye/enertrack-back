from django.shortcuts import render


from rest_framework import viewsets
from .models import Site
from .serializers import SiteSerializer


# Create your views here.
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response


from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser
import pandas as pd
from .models import Site
from rest_framework import status



@api_view(['GET'])
def ping(request):
    return Response({"status": "OK", "message": "EnerTrack API is up"})




@api_view(['GET'])
@permission_classes([IsAuthenticated])
def protected_ping(request):
    return Response({"message": f"Bonjour {request.user.username}, accès autorisé."})





# core/views.py
class SiteViewSet(viewsets.ModelViewSet):
    queryset = Site.objects.all()  # <- À AJOUTER
    serializer_class = SiteSerializer

    def get_queryset(self):
        user_country = self.request.user.pays
        return Site.objects.filter(country=user_country).order_by('zone', 'name')



class SiteImportView(APIView):
    parser_classes = [MultiPartParser]

    def post(self, request, format=None):
        file = request.FILES.get('file')
        if not file:
            return Response({"error": "No file provided."}, status=400)

        df = pd.read_excel(file)
        created = 0
        user_country = request.user.pays

        for _, row in df.iterrows():
         
            zone = row['Site ID'].split("_")[0].upper()
            Site.objects.update_or_create(
                site_id=row['Site ID'],
                defaults={
                    'name': row['Site Name'],
                    'is_new': row['Site neuf ou existant'] == 'Site neuf',
                    'installation_date': row.get('Date d\'installation'),
                    'activation_date': row.get('Date mise en service'),
                    'is_billed': row.get('Statut Facturation') == 'Oui',
                    'contratual_typology': row.get('Typologie contractuelle'),
                    'real_typology': row.get('Typologie réelle'),
                    'billing_typology': row.get('Typologie de facturation'),
                    'power_kw': row.get('Puissance contractuelle'),
                    'batch_aktivco': row.get('Batch Aktivco'),
                    'batch_operational': row.get('Batch opérationel'),
                    'zone': zone,
                    'country': user_country  # Par défaut
                }
            )
            created += 1

        return Response({"message": f"{created} sites importés."}, status=201)
