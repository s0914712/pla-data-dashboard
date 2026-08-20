#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build point-in-time ML features from MSA military navigation warnings.

This module is intentionally challenger-only. It does not alter the production
sortie forecaster. It turns the accumulated MSA warning table into event-level
features plus target-date point-in-time aggregates for walk-forward evaluation.

PIT rule: a warning becomes usable only at ``scraped_at`` (first seen by this
pipeline). A recovered source publication timestamp is retained for diagnostics,
but never makes an event visible earlier than first_seen.

The dependency-free geometry routines are modeling approximations for relative
distance/area features; they are not intended for navigation or legal boundary
interpretation.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

UTC = timezone.utc
LOCAL_TZ = ZoneInfo("Asia/Taipei")
EARTH_RADIUS_KM = 6371.0088

TAIWAN_POLYGON: List[Tuple[float, float]] = [
    (25.30, 121.55), (24.95, 121.95), (24.20, 121.85), (23.45, 121.55),
    (22.65, 121.10), (21.90, 120.85), (21.90, 120.25), (22.45, 120.10),
    (23.20, 120.05), (24.00, 120.30), (24.75, 120.65), (25.30, 121.00),
]
TAIWAN_STRAIT_POLYGON: List[Tuple[float, float]] = [
    (25.60, 119.20), (25.60, 121.05), (24.20, 120.75),
    (22.00, 120.45), (22.00, 117.80), (23.50, 118.20),
]

LIVE_FIRE_RE = re.compile(
    r"实弹|實彈|火炮射击|火炮射擊|LIVE\s*FIR(?:E|ING)|LIVE[- ]FIRE",
    re.I,
)
SHOOTING_RE = re.compile(r"射击训练|射擊訓練|射击|射擊|FIRING|SHOOTING", re.I)
MILITARY_RE = re.compile(
    r"军事|軍事|军演|軍演|演习|演習|MILITARY|EXERCISE|MISSION|火箭发射|火箭發射",
    re.I,
)


@dataclass(frozen=True)
class ActivityPeriod:
    start_date: date
    end_date: date
    start_hhmm: Optional[str] = None
    end_hhmm: Optional[str] = None
    daily: bool = False


def _safe_text(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def _parse_date(value) -> Optional[date]:
    text = _safe_text(value)
    if not text:
        return None
    try:
        return pd.to_datetime(text, errors="raise").date()
    except Exception:
        return None


def _parse_scraped_at(value) -> Optional[datetime]:
    """Parse first-seen time. GitHub Actions emits naive UTC datetimes today."""
    text = _safe_text(value)
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _extract_published_at(content: str) -> Optional[datetime]:
    m = re.search(
        r"发布时间\s*[：:]\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s+"
        r"(\d{1,2})[:：](\d{2})",
        _safe_text(content),
    )
    if not m:
        return None
    y, mo, d, hh, mm = map(int, m.groups())
    try:
        return datetime(y, mo, d, hh, mm, tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def _hhmm_minutes(hhmm: Optional[str]) -> Optional[int]:
    if not hhmm:
        return None
    s = re.sub(r"\D", "", str(hhmm)).zfill(4)
    if len(s) != 4:
        return None
    hh, mm = int(s[:2]), int(s[2:])
    if hh == 24 and mm == 0:
        return 24 * 60
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh * 60 + mm


def _activity_datetime(d: date, hhmm: Optional[str], *, end: bool = False) -> datetime:
    mins = _hhmm_minutes(hhmm)
    if mins is None:
        mins = 24 * 60 if end else 0
    if mins == 24 * 60:
        return datetime.combine(d + timedelta(days=1), time.min, tzinfo=LOCAL_TZ)
    return datetime.combine(d, time(mins // 60, mins % 60), tzinfo=LOCAL_TZ)


def _periods_from_repo_parser(text: str, publish_date) -> List[ActivityPeriod]:
    try:
        import sys
        scripts_dir = str(Path(__file__).resolve().parents[1])
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from nav_warning_dates import parse_periods  # type: ignore
    except Exception:
        return []

    try:
        parsed = parse_periods(text, publish_date)
    except Exception:
        return []
    out: List[ActivityPeriod] = []
    for p in parsed:
        if not getattr(p, "start", None):
            continue
        out.append(
            ActivityPeriod(
                p.start,
                p.end or p.start,
                getattr(p, "start_time", None),
                getattr(p, "end_time", None),
                bool(getattr(p, "daily", False)),
            )
        )
    return out


def _fallback_periods(row) -> List[ActivityPeriod]:
    text = _safe_text(row.get("time_periods"))
    publish = _parse_date(row.get("publish_date"))
    start_col, end_col = _parse_date(row.get("start_date")), _parse_date(row.get("end_date"))
    periods: List[ActivityPeriod] = []

    explicit = re.findall(r"(\d{1,2})/(\d{1,2})\s+(\d{3,4})\s*[-至到]\s*(\d{3,4})", text)
    if explicit:
        year = publish.year if publish else (start_col.year if start_col else date.today().year)
        for mo, d, st, et in explicit:
            try:
                dd = date(year, int(mo), int(d))
            except ValueError:
                continue
            periods.append(ActivityPeriod(dd, dd, st.zfill(4), et.zfill(4), False))
        return periods

    m = re.search(
        r"(\d{1,2})/(\d{1,2})\s*[-至]\s*(\d{1,2})/(\d{1,2})\s*"
        r"(?:每日|每天|DAILY)?\s*(\d{3,4})\s*[-至]\s*(\d{3,4})",
        text,
        re.I,
    )
    if m:
        sm, sd, em, ed, st, et = m.groups()
        year = publish.year if publish else (start_col.year if start_col else date.today().year)
        try:
            s = date(year, int(sm), int(sd))
            eyear = year + 1 if int(em) < int(sm) else year
            e = date(eyear, int(em), int(ed))
            return [ActivityPeriod(
                s, e, st.zfill(4), et.zfill(4),
                "每日" in text or "每天" in text or "DAILY" in text.upper(),
            )]
        except ValueError:
            pass

    m = re.search(
        r"(\d{1,2})/(\d{1,2})\s+(\d{3,4})\s*(?:至|TO|-)\s*"
        r"(\d{1,2})/(\d{1,2})\s+(\d{3,4})",
        text,
        re.I,
    )
    if m:
        sm, sd, st, em, ed, et = m.groups()
        year = publish.year if publish else (start_col.year if start_col else date.today().year)
        try:
            s = date(year, int(sm), int(sd))
            eyear = year + 1 if int(em) < int(sm) else year
            e = date(eyear, int(em), int(ed))
            return [ActivityPeriod(s, e, st.zfill(4), et.zfill(4), False)]
        except ValueError:
            pass

    if start_col:
        periods.append(ActivityPeriod(start_col, end_col or start_col))
    return periods


def parse_activity_periods(row) -> List[ActivityPeriod]:
    text = f"{_safe_text(row.get('title'))} {_safe_text(row.get('content_preview'))}"
    periods = _periods_from_repo_parser(text, row.get("publish_date"))
    return periods or _fallback_periods(row)


def _duration_hours(periods: Sequence[ActivityPeriod]) -> Optional[float]:
    if not periods:
        return None
    total = 0.0
    has_precise = False
    for p in periods:
        sm, em = _hhmm_minutes(p.start_hhmm), _hhmm_minutes(p.end_hhmm)
        if sm is None or em is None:
            continue
        if p.daily:
            day_minutes = em - sm
            if day_minutes < 0:
                day_minutes += 24 * 60
            days = (p.end_date - p.start_date).days + 1
            total += days * day_minutes / 60.0
            has_precise = True
        else:
            start = _activity_datetime(p.start_date, p.start_hhmm)
            end = _activity_datetime(p.end_date, p.end_hhmm, end=True)
            hours = (end - start).total_seconds() / 3600.0
            if hours >= 0:
                total += hours
                has_precise = True
    return round(total, 3) if has_precise else None


def _window_span_hours(periods: Sequence[ActivityPeriod]) -> Optional[float]:
    if not periods:
        return None
    first = min(periods, key=lambda p: (p.start_date, p.start_hhmm or "0000"))
    last = max(periods, key=lambda p: (p.end_date, p.end_hhmm or "2400"))
    start = _activity_datetime(first.start_date, first.start_hhmm)
    end = _activity_datetime(last.end_date, last.end_hhmm, end=True)
    return round(max(0.0, (end - start).total_seconds() / 3600.0), 3)


def _first_start(periods: Sequence[ActivityPeriod]) -> Optional[datetime]:
    if not periods:
        return None
    p = min(periods, key=lambda x: (x.start_date, x.start_hhmm or "0000"))
    return _activity_datetime(p.start_date, p.start_hhmm)


def _last_end(periods: Sequence[ActivityPeriod]) -> Optional[datetime]:
    if not periods:
        return None
    p = max(periods, key=lambda x: (x.end_date, x.end_hhmm or "2400"))
    return _activity_datetime(p.end_date, p.end_hhmm, end=True)


def _parse_decimal_coordinates(value) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for part in _safe_text(value).split(";"):
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)", part)
        if not m:
            continue
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            out.append((lat, lon))
    return _dedupe_points(out)


def _coord_component(deg: str, minute: str, second: Optional[str], direction: str) -> float:
    value = float(deg) + float(minute) / 60.0 + (float(second or 0) / 3600.0)
    if direction.upper() in ("S", "W"):
        value = -value
    return value


def _parse_coordinates_from_text(text: str) -> List[Tuple[float, float]]:
    text = _safe_text(text)
    out: List[Tuple[float, float]] = []

    dms = re.compile(
        r"(\d{1,2})[-°](\d{1,2})[-′'](\d{1,2}(?:\.\d+)?)\s*([NS])"
        r"\s*[/、，,;\s]*\s*"
        r"(\d{1,3})[-°](\d{1,2})[-′'](\d{1,2}(?:\.\d+)?)\s*([EW])",
        re.I,
    )
    for m in dms.finditer(text):
        lat = _coord_component(m.group(1), m.group(2), m.group(3), m.group(4))
        lon = _coord_component(m.group(5), m.group(6), m.group(7), m.group(8))
        out.append((lat, lon))

    dm = re.compile(
        r"(\d{1,2})-(\d{1,2}(?:\.\d+)?)\s*([NS])"
        r"\s*[/、，,;\s]*\s*"
        r"(\d{1,3})-(\d{1,2}(?:\.\d+)?)\s*([EW])",
        re.I,
    )
    for m in dm.finditer(text):
        lat = _coord_component(m.group(1), m.group(2), None, m.group(3))
        lon = _coord_component(m.group(4), m.group(5), None, m.group(6))
        out.append((lat, lon))

    dec = re.compile(
        r"(\d{1,2}(?:\.\d+)?)\s*([NS])\s*[/、，,;\s]+\s*"
        r"(\d{1,3}(?:\.\d+)?)\s*([EW])",
        re.I,
    )
    for m in dec.finditer(text):
        lat = float(m.group(1)) * (-1 if m.group(2).upper() == "S" else 1)
        lon = float(m.group(3)) * (-1 if m.group(4).upper() == "W" else 1)
        out.append((lat, lon))

    return _dedupe_points(out)


def _dedupe_points(points: Iterable[Tuple[float, float]]) -> List[Tuple[float, float]]:
    seen = set()
    out = []
    for lat, lon in points:
        key = (round(lat, 6), round(lon, 6))
        if key not in seen:
            seen.add(key)
            out.append((lat, lon))
    return out


def _split_polygons(points: Sequence[Tuple[float, float]], content: str) -> List[List[Tuple[float, float]]]:
    pts = list(points)
    if len(pts) < 3:
        return []
    upper = _safe_text(content).upper()
    explicit_multi = "以及" in content or ("AREA A" in upper and "AREA B" in upper)
    if explicit_multi and len(pts) in (6, 8, 10, 12) and len(pts) % 2 == 0:
        half = len(pts) // 2
        if half >= 3:
            return [pts[:half], pts[half:]]
    return [pts]


def _xy_km(point: Tuple[float, float], lat0: float) -> Tuple[float, float]:
    lat, lon = point
    x = math.radians(lon) * EARTH_RADIUS_KM * math.cos(math.radians(lat0))
    y = math.radians(lat) * EARTH_RADIUS_KM
    return x, y


def _polygon_area_km2(poly: Sequence[Tuple[float, float]]) -> float:
    if len(poly) < 3:
        return 0.0
    lat0 = sum(p[0] for p in poly) / len(poly)
    xy = [_xy_km(p, lat0) for p in poly]
    area2 = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        area2 += x1 * y2 - x2 * y1
    return abs(area2) / 2.0


def _point_in_polygon(point: Tuple[float, float], poly: Sequence[Tuple[float, float]]) -> bool:
    lat, lon = point
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def _segments_intersect(a, b, c, d) -> bool:
    def orient(p, q, r):
        return (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return (o1 == 0 or o2 == 0 or o1 * o2 < 0) and (o3 == 0 or o4 == 0 or o3 * o4 < 0)


def _point_segment_distance_km(point, a, b) -> float:
    lat0 = (point[0] + a[0] + b[0]) / 3.0
    px, py = _xy_km(point, lat0)
    ax, ay = _xy_km(a, lat0)
    bx, by = _xy_km(b, lat0)
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    qx, qy = ax + t * dx, ay + t * dy
    return math.hypot(px - qx, py - qy)


def _geometry_distance_km(
    polygons: Sequence[Sequence[Tuple[float, float]]],
    target: Sequence[Tuple[float, float]],
) -> Optional[float]:
    if not polygons:
        return None
    best = float("inf")
    for poly in polygons:
        if any(_point_in_polygon(p, target) for p in poly) or any(_point_in_polygon(p, poly) for p in target):
            return 0.0
        for i in range(len(poly)):
            a, b = poly[i], poly[(i + 1) % len(poly)]
            for j in range(len(target)):
                c, d = target[j], target[(j + 1) % len(target)]
                if _segments_intersect(a, b, c, d):
                    return 0.0
            for p in target:
                best = min(best, _point_segment_distance_km(p, a, b))
        for p in poly:
            for j in range(len(target)):
                best = min(best, _point_segment_distance_km(p, target[j], target[(j + 1) % len(target)]))
    return round(best, 3) if math.isfinite(best) else None


def _centroid(points: Sequence[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
    if not points:
        return None
    return (sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points))


def _region(points: Sequence[Tuple[float, float]]) -> str:
    c = _centroid(points)
    if not c:
        return "UNKNOWN"
    lat, lon = c
    if _point_in_polygon(c, TAIWAN_STRAIT_POLYGON):
        return "TAIWAN_STRAIT"
    if 37 <= lat <= 41.8 and 117 <= lon <= 122.7:
        return "BOHAI"
    if 31 <= lat < 39.5 and 119 <= lon <= 126:
        return "YELLOW_SEA"
    if 25 <= lat <= 34 and 120 <= lon <= 130:
        return "EAST_CHINA_SEA"
    if 25.0 <= lat <= 28.5 and 120 <= lon <= 126:
        return "NORTH_TAIWAN"
    if 21.5 <= lat < 25.5 and 121 <= lon <= 126:
        return "EAST_TAIWAN"
    if 18 <= lat < 22.5 and 117 <= lon <= 123.5:
        return "BASHI_CHANNEL"
    if 5 <= lat < 24 and 105 <= lon <= 121:
        return "SOUTH_CHINA_SEA"
    return "OTHER"


def _coordinate_confidence(points, polygons, recovered: bool) -> float:
    n = len(points)
    if n == 0:
        return 0.0
    score = 0.45 if n == 1 else 0.62 if n == 2 else 0.84
    if n >= 4:
        score += 0.05
    if not recovered:
        score += 0.05
    if polygons:
        total_area = sum(_polygon_area_km2(p) for p in polygons)
        if 0 < total_area < 200000:
            score += 0.05
        elif total_area >= 200000:
            score -= 0.15
    return round(max(0.0, min(1.0, score)), 3)


def _warning_type(text: str) -> str:
    t = _safe_text(text)
    if LIVE_FIRE_RE.search(t):
        return "LIVE_FIRE"
    if re.search(r"火箭发射|火箭發射|ROCKET\s*LAUNCH", t, re.I):
        return "ROCKET_LAUNCH"
    if re.search(r"火箭残骸|火箭殘骸|ROCKET\s*DEBRIS", t, re.I):
        return "ROCKET_DEBRIS"
    if re.search(r"演习|演習|EXERCISE", t, re.I):
        return "MILITARY_EXERCISE"
    if re.search(r"军事任务|軍事任務|MILITARY\s*MISSION", t, re.I):
        return "MILITARY_MISSION"
    if re.search(r"军事活动|軍事活動|MILITARY\s*ACTIVIT", t, re.I):
        return "MILITARY_ACTIVITY"
    if SHOOTING_RE.search(t):
        return "SHOOTING_TRAINING"
    return "OTHER_MILITARY"


def build_event_record(row) -> dict:
    title = _safe_text(row.get("title"))
    content = _safe_text(row.get("content_preview"))
    text = f"{title} {content}"
    url = _safe_text(row.get("url"))
    event_id = hashlib.sha1((url or text).encode("utf-8")).hexdigest()[:16]

    first_seen = _parse_scraped_at(row.get("scraped_at"))
    published = _extract_published_at(content)
    periods = parse_activity_periods(row)
    start_local, end_local = _first_start(periods), _last_end(periods)

    points = _parse_decimal_coordinates(row.get("coordinates"))
    recovered = False
    if not points:
        points = _parse_coordinates_from_text(f"{_safe_text(row.get('coordinates_raw'))} {content}")
        recovered = bool(points)
    polygons = _split_polygons(points, content)
    areas = [_polygon_area_km2(p) for p in polygons]

    duration = _duration_hours(periods)
    span = _window_span_hours(periods)
    lead = None
    source_lead = None
    if start_local and first_seen:
        lead = (start_local.astimezone(UTC) - first_seen).total_seconds() / 3600.0
    if start_local and published:
        source_lead = (start_local - published).total_seconds() / 3600.0

    center = _centroid(points)
    military = bool(MILITARY_RE.search(text) or SHOOTING_RE.search(text) or LIVE_FIRE_RE.search(text))
    live_fire = bool(LIVE_FIRE_RE.search(text))
    shooting = bool(SHOOTING_RE.search(text))

    return {
        "warning_id": event_id,
        "publish_date": _safe_text(row.get("publish_date")),
        "published_at": published.isoformat() if published else "",
        "first_seen_at": first_seen.isoformat() if first_seen else "",
        "available_at": first_seen.isoformat() if first_seen else "",
        "start_at": start_local.isoformat() if start_local else "",
        "end_at": end_local.isoformat() if end_local else "",
        "lead_hours": round(lead, 3) if lead is not None else None,
        "source_lead_hours": round(source_lead, 3) if source_lead is not None else None,
        "duration_hours": duration,
        "window_span_hours": span,
        "distance_to_taiwan": _geometry_distance_km(polygons, TAIWAN_POLYGON),
        "distance_to_strait": _geometry_distance_km(polygons, TAIWAN_STRAIT_POLYGON),
        "area_km2": round(sum(areas), 3) if areas else None,
        "area_max_km2": round(max(areas), 3) if areas else None,
        "region": _region(points),
        "military_exercise": int(military),
        "live_fire": int(live_fire),
        "shooting_training": int(shooting),
        "warning_type": _warning_type(text),
        "coordinate_confidence": _coordinate_confidence(points, polygons, recovered),
        "coordinate_count": len(points),
        "polygon_count": len(polygons),
        "centroid_lat": round(center[0], 5) if center else None,
        "centroid_lon": round(center[1], 5) if center else None,
        "channel": _safe_text(row.get("channel")),
        "title": title,
        "url": url,
        "start_date": min((p.start_date for p in periods), default=None),
        "end_date": max((p.end_date for p in periods), default=None),
    }


def build_event_features(df: pd.DataFrame) -> pd.DataFrame:
    records = [build_event_record(row) for _, row in df.iterrows()]
    out = pd.DataFrame(records)
    if out.empty:
        return out
    return out.sort_values(["first_seen_at", "publish_date", "warning_id"], na_position="last").reset_index(drop=True)


def _as_utc(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(UTC)


def _active_on_target(row, target: date) -> bool:
    start = row.get("start_date")
    end = row.get("end_date")
    if isinstance(start, str):
        start = _parse_date(start)
    if isinstance(end, str):
        end = _parse_date(end)
    if not start:
        return False
    end = end or start
    return start <= target <= end


def _min_or_none(series) -> Optional[float]:
    vals = pd.to_numeric(series, errors="coerce").dropna()
    return float(vals.min()) if len(vals) else None


def aggregate_for_target(events: pd.DataFrame, target_date: date, as_of: datetime) -> Tuple[pd.DataFrame, dict]:
    as_of_utc = _as_utc(as_of)
    df = events.copy()
    if df.empty:
        return df, {"target_date": target_date.isoformat(), "as_of": as_of_utc.isoformat()}

    seen = pd.to_datetime(df["first_seen_at"], utc=True, errors="coerce")
    available_mask = seen.notna() & (seen <= as_of_utc)
    df["active_on_target_date"] = df.apply(lambda r: int(_active_on_target(r, target_date)), axis=1)
    df["new_warning_last_24h"] = ((seen > as_of_utc - timedelta(hours=24)) & available_mask).astype(int)
    usable = df[available_mask].copy()
    active = usable[usable["active_on_target_date"] == 1]
    new24 = usable[usable["new_warning_last_24h"] == 1]

    active_area = pd.to_numeric(active.get("area_km2"), errors="coerce") if len(active) else pd.Series(dtype=float)
    active_dur = pd.to_numeric(active.get("duration_hours"), errors="coerce") if len(active) else pd.Series(dtype=float)
    active_conf = pd.to_numeric(active.get("coordinate_confidence"), errors="coerce") if len(active) else pd.Series(dtype=float)

    row = {
        "target_date": target_date.isoformat(),
        "as_of": as_of_utc.isoformat(),
        "navwarn_new_24h": int(len(new24)),
        "navwarn_active_target": int(len(active)),
        "navwarn_active_target_live_fire": int((active.get("live_fire", pd.Series(dtype=int)) == 1).sum()),
        "navwarn_active_target_military": int((active.get("military_exercise", pd.Series(dtype=int)) == 1).sum()),
        "navwarn_taiwan_strait_active": int((active.get("region", pd.Series(dtype=str)) == "TAIWAN_STRAIT").sum()),
        "navwarn_near_taiwan_200km_active": int((pd.to_numeric(active.get("distance_to_taiwan", pd.Series(dtype=float)), errors="coerce") <= 200).sum()),
        "navwarn_min_distance_taiwan_km": _min_or_none(active.get("distance_to_taiwan", pd.Series(dtype=float))),
        "navwarn_min_distance_strait_km": _min_or_none(active.get("distance_to_strait", pd.Series(dtype=float))),
        "navwarn_area_active_total_km2": round(float(active_area.sum()), 3) if active_area.notna().any() else None,
        "navwarn_area_active_max_km2": round(float(active_area.max()), 3) if active_area.notna().any() else None,
        "navwarn_duration_active_total_hours": round(float(active_dur.sum()), 3) if active_dur.notna().any() else None,
        "navwarn_min_lead_hours_new_24h": _min_or_none(new24.get("lead_hours", pd.Series(dtype=float))),
        "navwarn_live_fire_new_24h": int((new24.get("live_fire", pd.Series(dtype=int)) == 1).sum()),
        "navwarn_coordinate_confidence_mean_active": round(float(active_conf.mean()), 3) if active_conf.notna().any() else None,
    }
    return df, row


def build_daily_pit(
    events: pd.DataFrame,
    targets: Sequence[date],
    *,
    forecast_hour_local: int = 10,
    explicit_as_of: Optional[datetime] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    annotated = events.copy()
    rows = []
    for target in targets:
        as_of = explicit_as_of or datetime.combine(target, time(forecast_hour_local), tzinfo=LOCAL_TZ)
        ann, agg = aggregate_for_target(events, target, as_of)
        if len(targets) == 1:
            annotated = ann
        rows.append(agg)
    return annotated, pd.DataFrame(rows)


def _date_range(start: date, end: date) -> List[date]:
    if end < start:
        raise ValueError("to-date must not precede from-date")
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def main() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Build PIT MSA navigation-warning ML features")
    parser.add_argument("--input", default=str(repo_root / "data/navigation_warnings/military_exercises.csv"))
    parser.add_argument("--events-output", default=str(repo_root / "data/features/navwarn_events_features.csv"))
    parser.add_argument("--daily-output", default=str(repo_root / "data/features/navwarn_daily_pit.csv"))
    parser.add_argument("--target-date", help="single target date YYYY-MM-DD")
    parser.add_argument("--from-date", help="first target date YYYY-MM-DD")
    parser.add_argument("--to-date", help="last target date YYYY-MM-DD")
    parser.add_argument("--as-of", help="explicit PIT cutoff ISO datetime; intended for single target")
    parser.add_argument(
        "--forecast-hour-local", type=int, default=10,
        help="historical daily cutoff hour in Asia/Taipei (default: 10)",
    )
    args = parser.parse_args()

    source = pd.read_csv(args.input, encoding="utf-8-sig")
    events = build_event_features(source)

    if args.target_date:
        targets = [_parse_date(args.target_date)]
    elif args.from_date and args.to_date:
        start, end = _parse_date(args.from_date), _parse_date(args.to_date)
        if not start or not end:
            raise SystemExit("Invalid from/to date")
        targets = _date_range(start, end)
    else:
        starts = [_parse_date(v) for v in events.get("start_date", [])]
        ends = [_parse_date(v) for v in events.get("end_date", [])]
        starts = [d for d in starts if d]
        ends = [d for d in ends if d]
        if not starts or not ends:
            raise SystemExit("No target range can be inferred; pass --target-date or --from-date/--to-date")
        targets = _date_range(min(starts), max(ends))

    if any(t is None for t in targets):
        raise SystemExit("Invalid target date")
    if args.as_of and len(targets) != 1:
        raise SystemExit("--as-of is only valid with one --target-date")
    explicit = _as_utc(args.as_of) if args.as_of else None

    annotated, daily = build_daily_pit(
        events,
        targets,  # type: ignore[arg-type]
        forecast_hour_local=args.forecast_hour_local,
        explicit_as_of=explicit,
    )

    events_out = Path(args.events_output)
    daily_out = Path(args.daily_output)
    events_out.parent.mkdir(parents=True, exist_ok=True)
    daily_out.parent.mkdir(parents=True, exist_ok=True)
    annotated.to_csv(events_out, index=False, encoding="utf-8-sig")
    daily.to_csv(daily_out, index=False, encoding="utf-8-sig")
    print(f"event features: {len(annotated)} -> {events_out}")
    print(f"daily PIT rows: {len(daily)} -> {daily_out}")


if __name__ == "__main__":
    main()
