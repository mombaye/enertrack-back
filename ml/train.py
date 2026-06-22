# ml/train.py
from __future__ import annotations

import os
import pickle
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "enertrack_backend.settings")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
django.setup()

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error

DATASET_PATH = "ml/data/train.parquet"
MODEL_PATH = "ml/models/lgbm_conso.pkl"
FEATURES_PATH = "ml/models/features.pkl"
METADATA_PATH = "ml/models/metadata.pkl"


FEATURES = [
    "month",
    "year",
    "is_hivernage",
    "is_hivernage_meteo",
    "load_w",
    "hors_catalogue",
    "source_score",
    "estimation_available",
    "conso_estimee_kwh",
    "acm_conso_kwh",
    "grid_conso_kwh",
    "histo_conso_30j",
    "conso_lag_1m",
    "conso_lag_2m",
    "conso_lag_3m",
    "conso_lag_6m",
    "conso_lag_12m",
    "conso_lag_24m",
    "same_month_last_year",
    "same_month_2y_ago",
    "same_month_avg",
    "rolling_3m_conso",
    "rolling_6m_conso",
    "trend_3m_vs_12m",
    "recurrence_score",
    "nb_jours_factures",
    "observed_month_ratio",
    "is_partial_billing",
    "temp_max_mean",
    "temp_min_mean",
    "precip_total",
    "humidity_max",
    "et0_mean",
    "event_magal_pressure",
    "event_gamou_pressure",
    "event_tabaski_pressure",
    "event_korite_pressure",
    "event_tamkharit_pressure",
    "event_magal_darou_pressure",
    "event_layene_pressure",
    "total_event_pressure",
]


TARGET = "conso_month_projected_kwh"


def _period_int(df: pd.DataFrame) -> pd.Series:
    return df["year"].astype(int) * 100 + df["month"].astype(int)


def _mape(y_true, y_pred):
    return np.mean(np.abs((y_true - y_pred) / np.maximum(y_true, 1))) * 100


def main():
    if not os.path.exists(DATASET_PATH):
        raise FileNotFoundError("Dataset introuvable. Lance d'abord : python ml/build_dataset.py")

    df = pd.read_parquet(DATASET_PATH)

    missing = [c for c in FEATURES + [TARGET] if c not in df.columns]
    for col in missing:
        df[col] = 0

    df = df.copy()
    df["period_int"] = _period_int(df)

    df_clean = df.dropna(subset=[TARGET]).copy()
    df_clean = df_clean[df_clean[TARGET].astype(float) > 0]
    df_clean = df_clean.sort_values(["period_int", "site_id"]).reset_index(drop=True)

    for col in FEATURES:
        df_clean[col] = pd.to_numeric(df_clean[col], errors="coerce")

    # Fallbacks intelligents pour les lags manquants.
    for col in ["conso_lag_1m", "conso_lag_2m", "conso_lag_3m", "conso_lag_6m", "conso_lag_12m", "conso_lag_24m", "same_month_last_year", "same_month_2y_ago", "same_month_avg", "rolling_3m_conso", "rolling_6m_conso"]:
        df_clean[col] = df_clean[col].fillna(df_clean.groupby("site_id")[TARGET].transform("median"))

    df_clean[FEATURES] = df_clean[FEATURES].replace([np.inf, -np.inf], np.nan).fillna(0)


        X = df_clean[FEATURES]
    y = df_clean[TARGET].astype(float)

    periods = sorted(df_clean["period_int"].unique())
    if len(periods) < 8:
        print("Pas assez de périodes pour une validation walk-forward solide. Entraînement direct.")
        splits = []
    else:
        split_points = np.array_split(periods, 5)
        splits = []
        for i in range(1, len(split_points)):
            val_periods = set(split_points[i])
            train_periods = set(np.concatenate(split_points[:i]))
            train_idx = df_clean.index[df_clean["period_int"].isin(train_periods)].to_numpy()
            val_idx = df_clean.index[df_clean["period_int"].isin(val_periods)].to_numpy()
            if len(train_idx) and len(val_idx):
                splits.append((train_idx, val_idx))

    print(f"Lignes d'entraînement : {len(X)}")
    print(f"Sites : {df_clean['site_id'].nunique()} · Périodes : {df_clean['period_int'].min()} → {df_clean['period_int'].max()}")
    print(f"Features : {len(FEATURES)}")

    maes = []
    mapes = []

    for fold, (train_idx, val_idx) in enumerate(splits, start=1):
        X_train, X_val = X.loc[train_idx], X.loc[val_idx]
        y_train, y_val = y.loc[train_idx], y.loc[val_idx]
        model = lgb.LGBMRegressor(
            objective="regression_l1",
            n_estimators=1200,
            learning_rate=0.025,
            num_leaves=31,
            min_child_samples=25,
            subsample=0.85,
            colsample_bytree=0.85,
            reg_alpha=0.2,
            reg_lambda=0.3,
            random_state=42,
            n_jobs=-1,
        )
        model.fit(
            X_train,
            y_train,
            eval_set=[(X_val, y_val)],
            callbacks=[lgb.early_stopping(80, verbose=False), lgb.log_evaluation(0)],
        )
        pred = model.predict(X_val)
        mae = mean_absolute_error(y_val, pred)
        mape = _mape(y_val.values, pred)
        maes.append(mae)
        mapes.append(mape)
        print(f"Fold {fold} — MAE: {mae:.0f} kWh · MAPE: {mape:.1f}%")

    if maes:
        print(f"\nMAE moyen : {np.mean(maes):.0f} kWh · MAPE moyen : {np.mean(mapes):.1f}%")

    final_model = lgb.LGBMRegressor(
        objective="regression_l1",
        n_estimators=1200,
        learning_rate=0.025,
        num_leaves=31,
        min_child_samples=25,
        subsample=0.85,
        colsample_bytree=0.85,
        reg_alpha=0.2,
        reg_lambda=0.3,
        random_state=42,
        n_jobs=-1,
    )
    final_model.fit(X, y)

    importance = pd.Series(final_model.feature_importances_, index=FEATURES).sort_values(ascending=False)
    print("\nTop features :")
    print(importance.head(20).to_string())

    os.makedirs("ml/models", exist_ok=True)
    with open(MODEL_PATH, "wb") as f:
        pickle.dump(final_model, f)
    with open(FEATURES_PATH, "wb") as f:
        pickle.dump(FEATURES, f)
    with open(METADATA_PATH, "wb") as f:
        pickle.dump({
            "model_version": "lgbm_v2_hybrid_seasonal_events_partial",
            "target": TARGET,
            "features": FEATURES,
            "train_rows": int(len(X)),
            "mae_mean": float(np.mean(maes)) if maes else None,
            "mape_mean": float(np.mean(mapes)) if mapes else None,
        }, f)

    print(f"\nModèle sauvegardé → {MODEL_PATH}")
    print(f"Features sauvegardées → {FEATURES_PATH}")


if __name__ == "__main__":
    main()