from django.urls import path

from .views import (
    FuelEfmsMonthlyListView,
    FuelEfmsDashboardView,
    FuelEfmsSyncRunListView,

    FuelMonthlyTrackingView,
    FuelEnocJournalView,
    FuelSyncRunsView,
)

urlpatterns = [
    path("efms/monthly/", FuelEfmsMonthlyListView.as_view(), name="fuel-efms-monthly"),
    path("efms/dashboard/", FuelEfmsDashboardView.as_view(), name="fuel-efms-dashboard"),
    path("efms/sync-runs/", FuelEfmsSyncRunListView.as_view(), name="fuel-efms-sync-runs"),
    path("tracking/monthly/", FuelMonthlyTrackingView.as_view(), name="fuel-tracking-monthly"),
    path("journal/enoc/", FuelEnocJournalView.as_view(), name="fuel-enoc-journal"),
    path("sync-runs/", FuelSyncRunsView.as_view(), name="fuel-sync-runs"),
]



