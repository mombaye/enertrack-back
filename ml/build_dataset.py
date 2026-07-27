# ml/build_dataset.py
from __future__ import annotations

import calendar
import os
import sys
from functools import lru_cache

import django
import pandas as pd
import requests

if not os.environ.get("DJANGO_SETTINGS_MODULE"):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "enertrack_backend.settings")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    django.setup()

from django.db.models import Sum
from billing.models import MonthlySynthesis
from financial.models import FinancialEvaluation, SiteMonthlyLoad
from estimation.models import EstimationResult
from ml.features.events import build_event_features
from ml.features.meteo import fetch_zone_meteo_cached
from ml.features.zones import normalize_zone


SOURCE_SCORE = {
    "GRID": 4,
    "ACM": 3,
    "HISTO": 2,
    "THEORIQUE": 1,
    "TARGET": 1,
    "NC": 0,
    "HORS_SCOPE": 0,
}

def _month_add(year: int, month: int, delta: int) -> tuple[int, int]:
    m0 = year * 12 + (month - 1) + delta
    return m0 // 12, (m0 % 12) + 1


def _month_days(year: int, month: int) -> int:
    return calendar.monthrange(int(year), int(month))[1]


def _safe_month_projected(conso, nb_jours, year, month):
    conso = float(conso or 0)
    days = float(nb_jours or 0)
    if days <= 0:
        return conso
    return conso / max(days, 1) * _month_days(int(year), int(month))


def _safe_avg(values):
    vals = [float(v) for v in values if v is not None and pd.notna(v) and float(v) > 0]
    return sum(vals) / len(vals) if vals else None




def build_training_dataframe() -> pd.DataFrame:
    print("[1/7] Chargement billing MonthlySynthesis…")
    billing_qs = (
        MonthlySynthesis.objects
        .filter(
            source__site__invoice_payment__iexact="Aktivco",
            source__site__grid_fee=True,
            source__payment_status__in=["PAID", "UNPAID"],
        )
        .values(
            "source__site__site_id",
            "source__site__name",
            "source__site__zone",
            "source__site__billing_typology",
            "year",
            "month",
        )
        .annotate(
            conso_kwh=Sum("conso"),
            montant_ht=Sum("montant_hors_tva"),
            montant_ttc=Sum("montant_ttc"),
            abonnement=Sum("abonnement_calcule"),
                        energie_nrj=Sum("energie_calculee"),
            penalite=Sum("penalite_abonnement_calculee"),
            cosphi=Sum("montant_cosinus_phi"),
            nb_jours=Sum("days_covered"),
        )
    )

    df = pd.DataFrame(list(billing_qs))
    if df.empty:
        raise ValueError("Aucune donnée billing trouvée — vérifier les filtres Aktivco/grid_fee")

    df.rename(columns={
        "source__site__site_id": "site_id",
        "source__site__name": "site_name",
        "source__site__zone": "zone",
        "source__site__billing_typology": "typology",
    }, inplace=True)

    df["zone_raw"] = df["zone"]
    df["zone"] = df.apply(lambda r: normalize_zone(r.get("zone"), r.get("site_name"), r.get("site_id")), axis=1)
    df["month_days"] = df.apply(lambda r: _month_days(r["year"], r["month"]), axis=1)
    df["observed_month_ratio"] = (pd.to_numeric(df["nb_jours"], errors="coerce").fillna(30) / df["month_days"]).clip(0, 1)
    df["is_partial_billing"] = (df["observed_month_ratio"] < 0.85).astype(int)
     df["conso_month_projected_kwh"] = df.apply(lambda r: _safe_month_projected(r["conso_kwh"], r["nb_jours"], r["year"], r["month"]), axis=1)

    print(f"    → {len(df)} lignes billing · {df['site_id'].nunique()} sites")

    print("[2/7] Jointure FinancialEvaluation…")
    fe = pd.DataFrame(list(FinancialEvaluation.objects.values(
        "site__site_id", "year", "month",
        "marge", "marge_statut", "redevance",
        "load_w", "hors_catalogue",
        "recurrence_mois_nok", "recurrence_type",
        "nb_jours_factures", "periode_courte",
    ))).rename(columns={"site__site_id": "site_id"})
    df = df.merge(fe, on=["site_id", "year", "month"], how="left")
    print(f"    → {len(fe)} lignes financial")

    print("[3/7] Jointure EstimationResult…")
    er = pd.DataFrame(list(
        EstimationResult.objects
        .filter(batch__status="DONE")
        .values(
            "site__site_id", "batch__year", "batch__month",
            "conso_estimee_kwh", "montant_estime",
            "source_utilisee", "fiabilite_grid",
            "acm_conso_kwh", "grid_conso_kwh",
            "histo_conso_30j", "histo_nb_mois",
        ))).rename(columns={
        "site__site_id": "site_id",
        "batch__year": "year",
        "batch__month": "month",
    })
    if not er.empty:
        er["source_score"] = er["source_utilisee"].map(SOURCE_SCORE).fillna(0)
    df = df.merge(er, on=["site_id", "year", "month"], how="left")
    df["source_score"] = df.get("source_score", 0).fillna(0)
    df["estimation_available"] = df.get("conso_estimee_kwh").notna().astype(int) if "conso_estimee_kwh" in df.columns else 0
    print(f"    → {len(er)} lignes estimation")

    print("[4/7] Jointure SiteMonthlyLoad…")
    loads = pd.DataFrame(list(SiteMonthlyLoad.objects.values(
        "site__site_id", "year", "month", "load_w", "source"
    ))).rename(columns={"site__site_id": "site_id", "load_w": "load_w_from_table"})
    df = df.merge(loads, on=["site_id", "year", "month"], how="left")
    if "load_w_from_table" in df.columns:
        df["load_w"] = df["load_w"].fillna(df["load_w_from_table"])

    print("[5/7] Features temporelles exactes…")
    df = df.sort_values(["site_id", "year", "month"]).reset_index(drop=True)
    value_map = {(r.site_id, int(r.year), int(r.month)): float(r.conso_month_projected_kwh or 0) for r in df.itertuples()}

        for lag in [1, 2, 3, 6, 12, 24]:
        vals = []
        for r in df.itertuples():
            y, m = _month_add(int(r.year), int(r.month), -lag)
            vals.append(value_map.get((r.site_id, y, m)))
        df[f"conso_lag_{lag}m"] = vals

    for win in [3, 6]:
        vals = []
        for r in df.itertuples():
            past = []
            for lag in range(1, win + 1):
                y, m = _month_add(int(r.year), int(r.month), -lag)
                past.append(value_map.get((r.site_id, y, m)))
            vals.append(_safe_avg(past))
        df[f"rolling_{win}m_conso"] = vals

    df["same_month_last_year"] = df["conso_lag_12m"]
    df["same_month_2y_ago"] = df["conso_lag_24m"]
    df["same_month_avg"] = df[["same_month_last_year", "same_month_2y_ago"]].mean(axis=1, skipna=True)
    df["trend_3m_vs_12m"] = df.apply(
        lambda r: (float(r["rolling_3m_conso"]) / float(r["same_month_last_year"]) - 1)
        if pd.notna(r.get("rolling_3m_conso")) and pd.notna(r.get("same_month_last_year")) and float(r.get("same_month_last_year") or 0) > 0
        else 0,
        axis=1,
    )
        df["period_key"] = df["year"] * 100 + df["month"]
    df["is_hivernage"] = df["month"].isin([6, 7, 8, 9, 10]).astype(int)
    df["marge_statut_bin"] = (df["marge_statut"] == "NOK").astype(int)
    df["recurrence_score"] = pd.to_numeric(df["recurrence_mois_nok"], errors="coerce").fillna(0)
    df["hors_catalogue"] = df["hors_catalogue"].fillna(False).astype(int)
    df["load_w"] = pd.to_numeric(df["load_w"], errors="coerce").fillna(0)
    df["nb_jours_factures"] = pd.to_numeric(df.get("nb_jours_factures"), errors="coerce").fillna(df["nb_jours"]).fillna(30)

    print("[6/7] Features événements…")
    year_start = int(df["year"].min())
    year_end = int(df["year"].max())
    df = build_event_features(df, year_start, year_end)

    print("[7/7] Météo par zone/mois…")
    meteo_records = []
    for _, row in df[["zone", "year", "month"]].drop_duplicates().iterrows():
        z = normalize_zone(row["zone"])
        y, m = int(row["year"]), int(row["month"])
        meteo_records.append({"zone": z, "year": y, "month": m, **fetch_zone_meteo_cached(z, y, m)})

    if meteo_records:
        meteo_df = pd.DataFrame(meteo_records).drop_duplicates(["zone", "year", "month"])
        df = df.merge(meteo_df, on=["zone", "year", "month"], how="left")
        df["is_hivernage_meteo"] = (pd.to_numeric(df["precip_total"], errors="coerce").fillna(0) > 20).astype(int)
    else:
        df["is_hivernage_meteo"] = df["is_hivernage"]

    print(f"\nDataset final : {df.shape[0]} lignes × {df.shape[1]} colonnes")
    print(f"Sites : {df['site_id'].nunique()} · période : {df['year'].min()}-{df['month'].min():02d} → {df['year'].max()}-{df['month'].max():02d}")
    print(f"NaN target : {df['conso_month_projected_kwh'].isna().sum()} · NaN lag12 : {df['conso_lag_12m'].isna().sum()}")

    return df

if __name__ == "__main__":
    os.makedirs("ml/data", exist_ok=True)
    out = build_training_dataframe()
    out.to_parquet("ml/data/train.parquet", index=False)
    print("Dataset sauvegardé → ml/data/train.parquet")