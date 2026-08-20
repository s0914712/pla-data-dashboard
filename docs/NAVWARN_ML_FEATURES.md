# Navigation Warning ML Feature Layer

This branch introduces a **shadow / challenger-only** feature builder for MSA military navigation warnings. It does not modify the production sortie predictor.

## Point-in-time rule

For model training and backtests, a warning is available only from `first_seen_at` (`scraped_at`) onward. `published_at` is retained when it can be recovered from the archived MSA content, but is not used to retroactively make a warning visible before the pipeline actually observed it.

## Event-level features

The builder derives:

- `lead_hours`: hours from strict PIT availability (`first_seen_at`) to the first activity window.
- `source_lead_hours`: hours from source publication time to first activity, when exact publication time is recoverable.
- `duration_hours`: total active hours, respecting notices that say “daily HHMM-HHMM”.
- `distance_to_taiwan`: approximate minimum km from warning geometry to a coarse Taiwan modeling polygon.
- `distance_to_strait`: approximate minimum km from warning geometry to a coarse Taiwan Strait modeling geofence.
- `area_km2`: approximate polygon area, summing explicitly separable multi-zone warnings.
- `region`: coarse modeling region (`TAIWAN_STRAIT`, `BASHI_CHANNEL`, `NORTH_TAIWAN`, `EAST_TAIWAN`, `EAST_CHINA_SEA`, `YELLOW_SEA`, `BOHAI`, `SOUTH_CHINA_SEA`, `OTHER`).
- `active_on_target_date`: whether any activity period overlaps the prediction target date.
- `new_warning_last_24h`: whether the pipeline first observed the warning in the previous 24 hours.
- `military_exercise`: structured military activity indicator.
- `live_fire`: confirmed live-fire keyword indicator.
- `coordinate_confidence`: 0–1 geometry quality score.

Additional diagnostics include `published_at`, `first_seen_at`, `available_at`, `start_at`, `end_at`, `window_span_hours`, `polygon_count`, `warning_type`, and `shooting_training`.

The built-in geographic polygons are **modeling geofences only**, not legal or political boundary definitions.

## Coordinate recovery

The feature builder re-parses archived notice text if the primary scraper has no normalized coordinates. This includes D-M-S variants such as `23-04-33N 116-33-14E`, which appear in current MSA data and were previously capable of producing `coordinate_count=0`.

## Daily PIT aggregate

The target-date output contains compact ML-ready columns such as:

- `navwarn_new_24h`
- `navwarn_active_target`
- `navwarn_active_target_live_fire`
- `navwarn_active_target_military`
- `navwarn_taiwan_strait_active`
- `navwarn_near_taiwan_200km_active`
- `navwarn_min_distance_taiwan_km`
- `navwarn_min_distance_strait_km`
- `navwarn_area_active_total_km2`
- `navwarn_area_active_max_km2`
- `navwarn_duration_active_total_hours`
- `navwarn_min_lead_hours_new_24h`
- `navwarn_live_fire_new_24h`
- `navwarn_coordinate_confidence_mean_active`

## CLI

Single target date:

```bash
python scripts/features/build_navwarn_features.py \
  --as-of 2026-08-20T10:00:00+08:00 \
  --target-date 2026-08-21
```

Historical audit range using the production-time cutoff (default 10:00 Asia/Taipei):

```bash
python scripts/features/build_navwarn_features.py \
  --from-date 2026-03-15 \
  --to-date 2026-08-21
```

Default outputs:

- `data/features/navwarn_events_features.csv`
- `data/features/navwarn_daily_pit.csv`

## Tests

```bash
python -m unittest tests.test_navwarn_features -v
```

The initial test set covers exact publication time/lead time, D-M-S coordinate recovery, multi-zone area handling, daily activity duration, and strict PIT exclusion of warnings first seen after the cutoff.

## Deliberate limitations in v1

1. Distances and areas use dependency-free approximations; they are suitable for ranking / ML features, not navigation.
2. Some historical MSA rows do not retain exact publication time. `first_seen_at` is therefore the authoritative PIT timestamp.
3. Multiple polygons are split only when the notice text explicitly makes the grouping recoverable; ambiguous geometry receives a lower confidence score.
4. These features must remain challenger-only until the existing source-audit / walk-forward gates show stable incremental value.
