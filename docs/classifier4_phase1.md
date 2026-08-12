# Classifier 4.0 — Phase 1

## Goal

Improve surge discrimination before changing any risk threshold.  The working
hypothesis is that recent misses are upstream classifier misses: the realized
surges had low OOS probability percentiles, so a threshold-only change cannot
recover them.

This branch is shadow-only.  It does not change `ADAPTIVE_RISK_ENABLED`, the
production `SurgeForecaster`, prediction CSVs, or historical files under
`data/`.

## Feature ablation

Three feature sets are compared on identical walk-forward splits:

- **A — production**: current `pla_surge_model.build_features()` only.
- **B — + temporal/regime**: momentum, acceleration, short/long baseline ratios,
  volatility regime, and observation density.
- **C — + temporal + naval**: PLAN vessel tempo, strait activity, multi-strait
  flags, carrier/joint activity, air/sea ratio, and joint air-naval z-score.

Run locally:

```bash
python -m scripts.analysis.classifier4_ablation \
  --data data/JapanandBattleship.csv \
  --windows 365,730,1285
```

Primary decision metrics are ROC-AUC, PR-AUC, Brier skill, recall at a 20% alert
budget, and `recall / alert_rate`.  The new features should not be promoted on
one short window alone; improvement should be directionally stable across
multiple OOS windows.

## Point-in-time guard

Any new external event source must retain both `event_time` and `published_at`.
Before aggregation, records with `published_at > feature_cutoff_time` are
rejected. Missing publication timestamps are rejected rather than silently
assumed historical.

The existing daily combined CSV does not retain exact publication timestamps.
For the Phase-1 event-ledger adapter, ROC MND/Japan MOD rows are conservatively
marked with the report date at 06:00 Asia/Taipei and the assumption is explicit
in `source_ref`. This adapter is for feature research, not proof of exact
intraday availability.

## Crawler plan

Do **not** build a second full crawler first. The repository already has ROC MND
and Japan MOD collection logic. Phase 1 normalizes those outputs to a common
`OfficialSignal` schema. That lets us measure whether naval/strait information
adds OOS discrimination before paying the complexity cost of another network
pipeline.

If C beats B/A materially, Phase 2 should upgrade source acquisition in this
order:

1. **ROC MND collector** — preserve source URL, report interval, exact fetch time,
   exact page publication time where available, aircraft count, vessel count,
   official-ship count and map availability. Save immutable raw HTML before
   parsing.
2. **Japan MOD / Joint Staff collector** — save listing metadata and original
   PDFs; parse event date separately from report date; retain ship type, hull
   number, direction, geography, carrier/AOR/AGI presence and confidence of the
   date/geography extraction.
3. **PRC official statements** — only after official operational signals prove
   useful. Extract event types (combat-readiness patrol, joint exercise,
   live-fire, carrier activity) rather than generic sentiment.

Raw source objects should be immutable. Parsed records go to an event ledger;
daily ML features are rebuildable derivatives. A parser improvement should
never require re-downloading history.

Recommended storage contract for Phase 2:

```text
data/raw/roc_mnd/YYYY/MM/DD/...
data/raw/japan_mod/YYYY/MM/DD/...
data/derived/event_ledger.parquet
```

Do not commit generated raw/derived files from experiments until their schema
and retention policy are reviewed.

## Tests

Pure-function tests:

```bash
python -m unittest scripts.analysis.test_classifier4_phase1
```

They verify deterministic feature construction, incremental-append invariance,
point-in-time cutoff enforcement, and publication-time retention in the event
ledger.

## Promotion rule

No production integration in Phase 1.  A later PR may wire the winning feature
set into `SurgeForecaster` only if walk-forward OOS results show meaningful and
stable improvement, especially in recall at the fixed alert budget, without a
material calibration collapse.
