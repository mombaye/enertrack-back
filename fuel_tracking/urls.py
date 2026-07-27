# fuel_tracking/urls.py

from django.urls import path

from fuel_tracking.views import (
    FuelCphMatrixView,
    FuelEfmsDashboardView,
    FuelEfmsMonthlyListView,
    FuelEfmsSyncRunListView,
    FuelEnocJournalView,
    FuelMonthlyTrackingView,
    FuelSourceStatusView,
    FuelSyncRunsView,
    FuelSiteReferenceView,
    FuelTrackingExportView,
)

urlpatterns = [
    path("efms/monthly/", FuelEfmsMonthlyListView.as_view(), name="fuel-efms-monthly"),
    path("efms/dashboard/", FuelEfmsDashboardView.as_view(), name="fuel-efms-dashboard"),
    path("efms/sync-runs/", FuelEfmsSyncRunListView.as_view(), name="fuel-efms-sync-runs"),

    path("tracking/monthly/", FuelMonthlyTrackingView.as_view(), name="fuel-monthly-tracking"),
    path("journal/enoc/", FuelEnocJournalView.as_view(), name="fuel-enoc-journal"),
    path("source-status/", FuelSourceStatusView.as_view(), name="fuel-source-status"),
    path("sync-runs/", FuelSyncRunsView.as_view(), name="fuel-sync-runs"),
    path("sites/", FuelSiteReferenceView.as_view(), name="fuel-site-reference"),
    path("cph-matrix/", FuelCphMatrixView.as_view(), name="fuel-cph-matrix"),
    path("export/", FuelTrackingExportView.as_view(), name="fuel-tracking-export"),
]