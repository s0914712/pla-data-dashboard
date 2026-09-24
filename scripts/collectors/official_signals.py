#!/usr/bin/env python3
"""Normalized official-signal contract for Classifier 4.0.

Phase 1 intentionally reuses the repository's existing ROC MND and Japan MOD
scrapers instead of introducing a second network crawler immediately.  This
module converts their outputs into a point-in-time event ledger.  A later
collector can write the same schema directly from source pages/PDFs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

import pandas as pd


@dataclass(frozen=True)
class OfficialSignal:
    event_time: str
    published_at: str
    source: str
    actor: str
    event_type: str
    geography: str
    severity: float
    confidence: float
    value: float | None = None
    source_ref: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _truthy(value) -> bool:
    if pd.isna(value):
        return False
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    return s in {"1", "1.0", "true", "yes", "y"}


def from_daily_official_row(row: pd.Series) -> list[OfficialSignal]:
    """Normalize one JapanandBattleship.csv row into event-ledger records.

    `published_at` is conservatively anchored to the report date at 06:00+08:00
    when the legacy row has no publication timestamp.  That assumption is made
    explicit in source_ref and should be replaced by source-page timestamps when
    collectors are upgraded.
    """
    date = pd.Timestamp(row["date"])
    published = date.tz_localize("Asia/Taipei") + pd.Timedelta(hours=6)
    event_time = date.tz_localize("Asia/Taipei")
    out: list[OfficialSignal] = []

    aircraft = pd.to_numeric(row.get("pla_aircraft_sorties"), errors="coerce")
    if pd.notna(aircraft):
        out.append(OfficialSignal(
            event_time=event_time.isoformat(), published_at=published.isoformat(),
            source="ROC_MND", actor="PLA", event_type="air_activity",
            geography="Taiwan_ADIZ", severity=float(aircraft), confidence=1.0,
            value=float(aircraft), source_ref="legacy_daily_row_assumed_0600",
        ))

    vessels = pd.to_numeric(row.get("plan_vessel_sorties"), errors="coerce")
    if pd.notna(vessels):
        out.append(OfficialSignal(
            event_time=event_time.isoformat(), published_at=published.isoformat(),
            source="ROC_MND", actor="PLAN", event_type="naval_presence",
            geography="Taiwan_surrounding_waters", severity=float(vessels),
            confidence=1.0, value=float(vessels),
            source_ref="legacy_daily_row_assumed_0600",
        ))

    for col, geography in {
        "與那國": "Yonaguni", "宮古": "Miyako", "大禹": "Osumi", "對馬": "Tsushima"
    }.items():
        if _truthy(row.get(col)):
            out.append(OfficialSignal(
                event_time=event_time.isoformat(), published_at=published.isoformat(),
                source="JAPAN_MOD", actor="PLA_OR_RU", event_type="strait_transit",
                geography=geography, severity=1.0, confidence=0.9, value=1.0,
                source_ref="normalized_from_JapanandBattleship",
            ))

    for col, event_type, severity in [
        ("聯合演訓", "joint_exercise", 3.0),
        ("艦通過", "naval_transit", 1.0),
        ("航母活動", "carrier_activity", 4.0),
    ]:
        if _truthy(row.get(col)):
            out.append(OfficialSignal(
                event_time=event_time.isoformat(), published_at=published.isoformat(),
                source="JAPAN_MOD", actor="PLA", event_type=event_type,
                geography="Western_Pacific", severity=severity, confidence=0.9,
                value=1.0, source_ref="normalized_from_JapanandBattleship",
            ))
    return out


def build_event_ledger(df: pd.DataFrame) -> pd.DataFrame:
    """Create normalized event ledger without mutating source data."""
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce", format="mixed")
    d = d[d["date"].notna()].sort_values("date")
    records = []
    for _, row in d.iterrows():
        records.extend(sig.as_dict() for sig in from_daily_official_row(row))
    return pd.DataFrame.from_records(records)


def aggregate_daily_signals(ledger: pd.DataFrame, cutoff=None) -> pd.DataFrame:
    """Aggregate only knowable events into daily model features."""
    d = ledger.copy()
    d["published_at"] = pd.to_datetime(d["published_at"], errors="coerce", utc=True)
    if cutoff is not None:
        c = pd.Timestamp(cutoff)
        c = c.tz_localize("UTC") if c.tzinfo is None else c.tz_convert("UTC")
        d = d[d["published_at"].notna() & (d["published_at"] <= c)]
    d["event_time"] = pd.to_datetime(d["event_time"], errors="coerce", utc=True)
    d = d[d["event_time"].notna()]
    d["date"] = d["event_time"].dt.tz_convert("Asia/Taipei").dt.tz_localize(None).dt.normalize()
    if d.empty:
        return pd.DataFrame()

    piv = d.pivot_table(
        index="date", columns="event_type", values="severity", aggfunc="sum", fill_value=0.0
    )
    piv.columns = [f"signal_{c}" for c in piv.columns]
    piv["signal_total_severity"] = piv.sum(axis=1)
    piv["signal_event_count"] = d.groupby("date").size().reindex(piv.index).astype(float)
    return piv.sort_index()
