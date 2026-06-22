# prediction/urls.py
from django.urls import path
from .views import PredictionView, PredictionBulkForecastView

urlpatterns = [
    path("prediction/forecast/", PredictionView.as_view(), name="prediction-forecast"),
    path("prediction/forecast-bulk/", PredictionBulkForecastView.as_view(), name="prediction-forecast-bulk"),

]

# Dans urls.py principal : path("", include("prediction.urls")),