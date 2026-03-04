from django.urls import path
from .views import GridOutageDailyImportView, GridOutageAlarmImportView

urlpatterns = [
    path("grid-outages/daily/import/", GridOutageDailyImportView.as_view()),
    path("grid-outages/alarms/import/", GridOutageAlarmImportView.as_view()),
]
