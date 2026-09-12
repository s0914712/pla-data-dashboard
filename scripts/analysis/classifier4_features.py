#!/usr/bin/env python3
"""Classifier 4.0 Phase 1 feature builder.

This module is deliberately side-car only: it does not alter the production
SurgeForecaster.  It builds candidate temporal + naval features for ablation.
Every feature at row t is computed from information available on or before t.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

DEFAULT_DATE_COL = "date"
AIRCRAFT_COL = "pla_aircraft_sorties"
VESSEL_COL = "plan_vessel_sorties"

STRAIT_COLUMNS = ["與那國", "宮古", "大禹", "對馬"]
ACTIVITY_COLUMNS = ["聯合演訓", "艦通過", "航母活動"]


def _as_bool_numeric(s: pd.Series) -> pd.Series:
    """Convert mixed bool/0/1/text flags to float 0/1 without inventing data."""
    if pd.api.types.is_bool_dtype(s):
        return s.astype(float)
    numeric = pd.to_numeric(s, errors="coerce")
    text = s.astype(str).str.strip().str.lower()
    mapped = text.map({"true": 1.0, "false": 0.0, "yes": 1.0, "no": 0.0})
    return numeric.where(numeric.notna(), mapped)


def to_daily_frame(df: pd.DataFrame, date_col: str = DEFAULT_DATE_COL) -> pd.DataFrame:
    """Return a calendar-aligned daily frame; missing reports remain NaN, not zero."""
    d = df.copy()
    d[date_col] = pd.to_datetime(d[date_col], errors="coerce", format="mixed")
    d = d[d[date_col].notna()].sort_values(date_col)
    d = d.groupby(date_col, as_index=False).last()
    idx = pd.date_range(d[date_col].min(), d[date_col].max(), freq="D")
    return d.set_index(date_col).reindex(idx).rename_axis("date")


def build_temporal_features(daily: pd.DataFrame) -> pd.DataFrame:
    """Candidate temporal/regime features beyond the current production set."""
    y = pd.to_numeric(daily[AIRCRAFT_COL], errors="coerce")
    out = pd.DataFrame(index=daily.index)

    ma3 = y.rolling(3, min_periods=2).mean()
    ma7 = y.rolling(7, min_periods=3).mean()
    ma14 = y.rolling(14, min_periods=4).mean()
    ma30 = y.rolling(30, min_periods=7).mean()
    ma90 = y.rolling(90, min_periods=14).mean()
    ma180 = y.rolling(180, min_periods=30).mean()

    out["c4_momentum_3_14"] = ma3 - ma14
    out["c4_momentum_7_30"] = ma7 - ma30
    out["c4_baseline_ratio_7_90"] = ma7 / (ma90 + 1.0)
    out["c4_baseline_ratio_30_180"] = ma30 / (ma180 + 1.0)

    delta = y.diff()
    out["c4_delta_1d"] = delta
    out["c4_delta_3d_mean"] = delta.rolling(3, min_periods=1).mean()
    out["c4_acceleration_1d"] = delta.diff()
    out["c4_acceleration_3d"] = delta.diff().rolling(3, min_periods=1).mean()
    out["c4_volatility_7d"] = y.rolling(7, min_periods=3).std()
    out["c4_volatility_ratio_7_30"] = (
        y.rolling(7, min_periods=3).std()
        / (y.rolling(30, min_periods=7).std() + 1.0)
    )
    out["c4_obs_density_90"] = y.notna().rolling(90, min_periods=1).mean()
    return out


def build_naval_features(daily: pd.DataFrame) -> pd.DataFrame:
    """Derive naval/strait operational-tempo candidates from existing official data."""
    out = pd.DataFrame(index=daily.index)

    vessels = pd.to_numeric(daily.get(VESSEL_COL), errors="coerce")
    if vessels is not None:
        out["c4_vessels_today"] = vessels
        out["c4_vessels_ma3"] = vessels.rolling(3, min_periods=1).mean()
        out["c4_vessels_ma7"] = vessels.rolling(7, min_periods=2).mean()
        out["c4_vessels_delta3"] = vessels.diff(3)
        vmean = vessels.rolling(30, min_periods=7).mean()
        vstd = vessels.rolling(30, min_periods=7).std()
        out["c4_vessels_z30"] = (vessels - vmean) / (vstd + 1.0)

    strait_flags = []
    for col in STRAIT_COLUMNS:
        if col in daily.columns:
            flag = _as_bool_numeric(daily[col])
            out[f"c4_strait_{col}"] = flag
            strait_flags.append(flag.fillna(0.0))
    if strait_flags:
        strait_count = pd.concat(strait_flags, axis=1).sum(axis=1)
        out["c4_strait_count_today"] = strait_count
        out["c4_strait_activity_3d"] = strait_count.rolling(3, min_periods=1).sum()
        out["c4_strait_activity_7d"] = strait_count.rolling(7, min_periods=1).sum()
        out["c4_multi_strait"] = (strait_count >= 2).astype(float)

    activity_flags = []
    for col in ACTIVITY_COLUMNS:
        if col in daily.columns:
            flag = _as_bool_numeric(daily[col])
            out[f"c4_activity_{col}"] = flag
            activity_flags.append(flag.fillna(0.0))
    if activity_flags:
        activity = pd.concat(activity_flags, axis=1).sum(axis=1)
        out["c4_operational_activity_today"] = activity
        out["c4_operational_activity_7d"] = activity.rolling(7, min_periods=1).sum()

    aircraft = pd.to_numeric(daily.get(AIRCRAFT_COL), errors="coerce")
    if aircraft is not None and vessels is not None:
        out["c4_air_sea_ratio"] = (aircraft + 1.0) / (vessels + 1.0)
        az = (aircraft - aircraft.rolling(30, min_periods=7).mean()) / (
            aircraft.rolling(30, min_periods=7).std() + 1.0
        )
        vz = (vessels - vessels.rolling(30, min_periods=7).mean()) / (
            vessels.rolling(30, min_periods=7).std() + 1.0
        )
        out["c4_air_naval_joint_z"] = az + vz

    return out


def build_classifier4_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build all Phase-1 candidate features, preserving daily calendar alignment."""
    daily = to_daily_frame(df)
    return pd.concat(
        [build_temporal_features(daily), build_naval_features(daily)], axis=1
    )
