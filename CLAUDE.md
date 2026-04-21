# CLAUDE.md

## Repo State

This repository now has an active runtime candidate again:

- Runtime strategy: `paired_window`
- Strategy file: `/Users/forrestliao/workspace/polybot/strategies/paired_window.py`
- Runner: `/Users/forrestliao/workspace/run.py`
- Runtime monitor: `/Users/forrestliao/workspace/polybot/trading/monitor.py`

The current runtime behavior is intentionally narrow:

- The strategy decides direction (`UP` or `DOWN`) and entry timing only
- There is no TP / SL / re-entry logic anymore
- After a buy, the position is held until the window is about to end
- `monitor.py` now allows attaching to an already-started window as long as the
  strategy's `entry band` has not elapsed yet

## Active Runtime Configs

- Main runtime config: `/Users/forrestliao/workspace/paired_window.yaml`
- Two-round validation config: `/Users/forrestliao/workspace/paired_window_2r.yaml`

Current default runtime shape:

- `theta_pct: 0.02`
- `persistence_sec: 10`
- `entry_start_remaining_sec: 270`
- `entry_end_remaining_sec: 120`
- `max_entries_per_window: 1`

## Useful Commands

```bash
rm -f log/*
python3.11 tools/collect_data.py --market btc-updown-5m --windows 10
python3.11 analysis/analyze_data.py data/collect_btc-updown-5m_*.jsonl
python3.11 analysis/analyze_paired_strategy.py data/collect_btc-updown-5m_<ts>.jsonl
python3.11 run.py --config paired_window_2r.yaml --dry
pytest -q
```

## Current Strategy Notes

The current paired-window strategy works like this:

1. Record BTC near the window open
2. During the configured `entry band`, wait for BTC to move away from the open
   by at least `theta_pct`
3. Require the move to persist for `persistence_sec`
4. Buy the matching Polymarket side if the token price is inside the configured
   entry price band
5. Hold to near window end, then sell

Important implementation details:

- Entry signals are computed off the `UP` token reference price
- For `DOWN`, the effective entry price is `1 - up_price`
- `run.py` is live again and no longer expected to fail fast
- The old retired runtime path and old TP/SL-style execution path have been removed

## Recent 2-Hour Research Snapshot

Dataset used:

- `/Users/forrestliao/workspace/data/collect_btc-updown-5m_1776768514.jsonl`
- 24 windows total
- Simulation assumptions:
  - `theta_pct = 0.02`
  - `persistence_sec = 10`
  - hold to window end
  - no exit logic besides end-of-window liquidation

Results by max entry price cap and entry band:

### Cap `0.61`

| Entry Band (remaining sec) | N | Wins | Win Rate | Avg Entry | EV / Trade |
|---|---:|---:|---:|---:|---:|
| `[60,180]` | 8 | 4 | 50.00% | 0.5363 | -0.0362 |
| `[60,240]` | 12 | 6 | 50.00% | 0.5642 | -0.0642 |
| `[120,240]` | 11 | 6 | 54.55% | 0.5618 | -0.0164 |
| `[120,270]` | 14 | 10 | 71.43% | 0.5486 | +0.1657 |
| `[180,270]` | 12 | 9 | 75.00% | 0.5408 | +0.2092 |

Best in this sample: `cap=0.61`, `band=[180,270]`

### Cap `0.65`

| Entry Band (remaining sec) | N | Wins | Win Rate | Avg Entry | EV / Trade |
|---|---:|---:|---:|---:|---:|
| `[60,180]` | 12 | 8 | 66.67% | 0.5783 | +0.0883 |
| `[60,240]` | 17 | 10 | 58.82% | 0.5906 | -0.0024 |
| `[120,240]` | 17 | 10 | 58.82% | 0.5906 | -0.0024 |
| `[120,270]` | 20 | 13 | 65.00% | 0.5885 | +0.0615 |
| `[180,270]` | 19 | 12 | 63.16% | 0.5884 | +0.0432 |

Best in this sample: `cap=0.65`, `band=[60,180]`

### Cap `0.70`

| Entry Band (remaining sec) | N | Wins | Win Rate | Avg Entry | EV / Trade |
|---|---:|---:|---:|---:|---:|
| `[60,180]` | 15 | 9 | 60.00% | 0.6113 | -0.0113 |
| `[60,240]` | 20 | 13 | 65.00% | 0.6145 | +0.0355 |
| `[120,240]` | 20 | 13 | 65.00% | 0.6145 | +0.0355 |
| `[120,270]` | 20 | 13 | 65.00% | 0.5930 | +0.0570 |
| `[180,270]` | 19 | 12 | 63.16% | 0.5932 | +0.0384 |

Best in this sample: `cap=0.70`, `band=[120,270]`

## Working Guidance For New Sessions

- Treat `paired_window` as the only active runtime strategy
- Do not reintroduce TP/SL/re-entry logic unless explicitly requested
- If a dry-run starts after window open, do not skip just because the window is
  older than 60s; only skip if the strategy's entry band has already ended
- Before any new dry-run, clear historical files under
  `/Users/forrestliao/workspace/log/` so the resulting logs belong only to the
  current run
- When evaluating parameter changes, use
  `/Users/forrestliao/workspace/analysis/analyze_paired_strategy.py`
  rather than reviving deleted legacy analysis scripts
