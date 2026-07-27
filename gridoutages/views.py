from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser, FormParser

from .utils import import_grid_outage_daily, import_grid_outage_alarms


class GridOutageDailyImportView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, *args, **kwargs):
        file = request.FILES.get("file")
        if not file:
            return Response({"detail": "file is required"}, status=status.HTTP_400_BAD_REQUEST)

        created, updated = import_grid_outage_daily(file)
        return Response(
            {"created": created, "updated": updated},
            status=status.HTTP_200_OK,
        )


class GridOutageAlarmImportView(APIView):
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, *args, **kwargs):
        file = request.FILES.get("file")
        if not file:
            return Response({"detail": "file is required"}, status=status.HTTP_400_BAD_REQUEST)

        created, updated = import_grid_outage_alarms(file)
        return Response(
            {"created": created, "updated": updated},
            status=status.HTTP_200_OK,
        )
