from django.urls import path

from . import views

urlpatterns = [
    # ── Historique de référence (import unique) ─────────────────────────────
    path("snapshots/import/", views.BOMarginSnapshotImportView.as_view(), name="bo-snapshot-import"),
    path("snapshots/",         views.BOMarginSnapshotListView.as_view(),   name="bo-snapshot-list"),

    # ── Workflow BO in-app ───────────────────────────────────────────────────
    path("requests/",                    views.BOAnalysisRequestListCreateView.as_view(), name="bo-request-list-create"),
    path("requests/bulk/",                views.BOAnalysisRequestBulkCreateView.as_view(), name="bo-request-bulk-create"),
    path("requests/<int:pk>/",           views.BOAnalysisRequestDetailView.as_view(),     name="bo-request-detail"),
    path("requests/<int:pk>/submit/",    views.BOAnalysisSubmitView.as_view(),            name="bo-request-submit"),
]
