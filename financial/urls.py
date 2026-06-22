from django.urls import path
from . import views

urlpatterns = [
    # ── Catalogue redevance ──────────────────────────────────────────────────
    path("fee-rules/import/", views.FinancialFeeRuleImportView.as_view(), name="fee-rule-import"),
    path("fee-rules/",        views.FinancialFeeRuleListView.as_view(),   name="fee-rule-list"),

    # ── Loads mensuels par site ───────────────────────────────────────────────
    path("loads/import/",   views.SiteMonthlyLoadImportView.as_view(),  name="load-import"),
    path("loads/",          views.SiteMonthlyLoadListView.as_view(),    name="load-list"),
    path("loads/<int:pk>/", views.SiteMonthlyLoadUpdateView.as_view(),  name="load-update"),

    # ── Calcul évaluation financière ─────────────────────────────────────────
    path("evaluate/",                          views.FinancialEvaluateView.as_view(),       name="evaluate"),
    path("evaluations/",                       views.FinancialEvaluationListView.as_view(), name="evaluation-list"),
    path("evaluations/stats/",                 views.FinancialEvaluationStatsView.as_view(),name="evaluation-stats"),
    path("evaluations/<str:site_id>/detail/",  views.SiteMargeDetailView.as_view(),         name="evaluation-detail"),

    # ── Dashboards ───────────────────────────────────────────────────────────
    path("dashboard/factures-vs-redevances/", views.DashboardFacturesRedevancesView.as_view(), name="dashboard-factures"),
    path("dashboard/marge-par-site/",         views.DashboardMargeParSiteView.as_view(),        name="dashboard-marge"),
    path("dashboard/sites-recurrents/",       views.DashboardSitesRecurrentsView.as_view(),     name="dashboard-recurrents"),
    path("suivi-conso/", views.SuiviConsoView.as_view(), name="suivi-conso"),

   
]

# ─────────────────────────────────────────────────────────────────────────────
# PATCH core/models.py — GridTargetRule (aucune migration cassée)
# ─────────────────────────────────────────────────────────────────────────────
# Ajouter uniquement ce champ dans GridTargetRule existant :
#
#   load_w = models.IntegerField(
#       null=True, blank=True, db_index=True,
#       help_text="Load exact en Watts (pour module Évaluation Financière)"
#   )
#
# Et dans class Meta.indexes, ajouter :
#   models.Index(fields=["configuration", "site_type", "load_w"]),
#
# Ce champ est optionnel et n'impacte PAS les modules Certification/Estimation
# qui continuent d'utiliser load_band.
# ─────────────────────────────────────────────────────────────────────────────