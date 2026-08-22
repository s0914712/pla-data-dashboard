# Classifier 4.0 — Phase 1

## Goal

Improve surge discrimination before changing any risk threshold. The working
hypothesis is that recent misses are upstream classifier misses: the realized
surges had low OOS probability percentiles, so a threshold-only change cannot
recover them.

This branch is shadow-only. It does not change `ADAPTIVE_RISK_ENABLED`, the
production `SurgeForecaster`, prediction CSV semantics, or historical source data.

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
budget, and `recall / alert_rate`. Improvement should be directionally stable
across multiple OOS windows.

## Daily live shadow

`daily_prediction.yml` runs the production model first, then executes:

```bash
python -m scripts.analysis.classifier4_shadow
```

The shadow runner uses the same latest `data/JapanandBattleship.csv` checkout and
produces two isolated files:

```text
data/predictions/classifier4_shadow.csv
data/predictions/classifier4_comparison.json
```

The CSV retains one D+1 forecast per target date and backfills `actual_sorties`
when the outcome becomes available. The JSON contains today's production/B/C
probabilities plus cumulative live metrics. Neither file is consumed by
`predict_surge_daily.py`, so challenger failure cannot change the formal forecast
or risk level.

B/C use the same balanced HistGradientBoosting classifier family and held-out
Platt-calibration pattern as the production surge classifier. They differ only by
the added feature groups.

Live scoring begins only after 10 resolved dates. Metrics are ROC-AUC, PR-AUC,
Brier skill, Recall@20% alert budget, and recall / actual alert rate.

## LINE report

The existing `LINEcron.yml` remains the delivery mechanism and keeps the same
`LINECHANNELACCESSTOKEN` / `USERID` secrets. After the normal daily brief, it sends
one optional shadow message containing the formal D+1 point forecast and surge
probability, Challenger B and C probabilities, a disagreement/spread indicator,
and cumulative live metrics after enough resolved days.

The second message is `continue-on-error`; missing shadow data can never block the
normal LINE brief. B/C are explicitly labeled shadow-only.

Scheduling is intentionally unchanged: production and challengers run at 10:00
Taiwan time, and the 07:00 LINE brief uses the most recently completed committed
forecast set, matching the repository's existing production cadence.

## Point-in-time guard

Any new external event source must retain both `event_time` and `published_at`.
Before aggregation, records with `published_at > feature_cutoff_time` are rejected.
Missing publication timestamps are rejected rather than silently assumed historical.

The existing daily combined CSV does not retain exact publication timestamps. For
the Phase-1 event-ledger adapter, ROC MND/Japan MOD rows are conservatively marked
with the report date at 06:00 Asia/Taipei and the assumption is explicit in
`source_ref`. This adapter is for feature research, not proof of exact intraday
availability.

## Crawler plan

Do **not** build a second full crawler first. The repository already has ROC MND
and Japan MOD collection logic. Phase 1 normalizes those outputs to a common
`OfficialSignal` schema so we can measure whether naval/strait information adds OOS
signal before adding network-pipeline complexity.

If C beats B/A materially, Phase 2 should upgrade source acquisition in this order:

1. **ROC MND collector** — preserve source URL, report interval, exact fetch time,
   publication time where available, aircraft count, vessel count, official-ship
   count and map availability. Save immutable raw HTML before parsing.
2. **Japan MOD / Joint Staff collector** — save listing metadata and original PDFs;
   parse event date separately from report date; retain ship type, hull number,
   direction, geography, carrier/AOR/AGI presence and extraction confidence.
3. **PRC official statements** — only after official operational signals prove
   useful; extract event types rather than generic sentiment.

Raw source objects should be immutable. Parsed records go to an event ledger; daily
ML features are rebuildable derivatives.

## Tests

```bash
python -m unittest scripts.analysis.test_classifier4_phase1
```

The tests verify deterministic feature construction, incremental-append invariance,
point-in-time cutoff enforcement, and publication-time retention.

## Promotion rule

No production integration in Phase 1. A later PR may wire the winning feature set
into `SurgeForecaster` only if walk-forward OOS and live shadow results show stable
improvement, especially in recall at the fixed alert budget, without a material
calibration collapse.
