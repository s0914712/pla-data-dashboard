#!/usr/bin/env python3
"""Point-in-time cutoff helpers for Classifier 4.0 experiments."""

from __future__ import annotations

from dataclasses import dataclass
import pandas as pd


@dataclass(frozen=True)
class CutoffResult:
    kept_rows: int
    dropped_future_rows: int
    cutoff: pd.Timestamp


def apply_feature_cutoff(
    frame: pd.DataFrame,
    cutoff,
    published_col: str = "published_at",
) -> tuple[pd.DataFrame, CutoffResult]:
    """Drop records not knowable at prediction time.

    The comparison is inclusive: a record published exactly at cutoff is allowed.
    NaN publication times are rejected rather than silently assumed historical.
    """
    if published_col not in frame.columns:
        raise KeyError(f"missing required point-in-time column: {published_col}")

    c = pd.Timestamp(cutoff)
    published = pd.to_datetime(frame[published_col], errors="coerce", utc=True)
    if c.tzinfo is None:
        c = c.tz_localize("UTC")
    else:
        c = c.tz_convert("UTC")

    mask = published.notna() & (published <= c)
    filtered = frame.loc[mask].copy()
    return filtered, CutoffResult(
        kept_rows=int(mask.sum()),
        dropped_future_rows=int((~mask).sum()),
        cutoff=c,
    )


def assert_point_in_time(frame: pd.DataFrame, cutoff, published_col="published_at") -> None:
    """Hard assertion used by builders/backtests before feature aggregation."""
    filtered, result = apply_feature_cutoff(frame, cutoff, published_col)
    if len(filtered) != len(frame):
        raise AssertionError(
            f"point-in-time violation: {result.dropped_future_rows} rows are future/unknown "
            f"relative to cutoff {result.cutoff.isoformat()}"
        )
