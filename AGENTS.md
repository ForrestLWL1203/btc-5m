# AGENTS.md

## Project Overview

Polymarket BTC/ETH up/down research and execution repository.

The previous retired strategy has been removed. The current active
runtime candidate is `paired_window`, and the repository is now organized
around:

- paired BTC + Polymarket data collection
- paired-window offline replay and parameter analysis
- a narrow runtime execution path for the current strategy

## Active Runtime

- Strategy: `paired_window`
- Strategy file: `/Users/forrestliao/workspace/polybot/strategies/paired_window.py`
- Runner: `/Users/forrestliao/workspace/run.py`
- Runtime monitor: `/Users/forrestliao/workspace/polybot/trading/monitor.py`
- Config loader: `/Users/forrestliao/workspace/polybot/config_loader.py`

Current runtime behavior:

- decide direction and entry timing only
- no TP / SL / re-entry logic
- hold to near window end after entry
- allow attaching to an already-started window if the strategy's `entry band`
  is still open

## What Still Exists

- `/Users/forrestliao/workspace/tools/collect_data.py` for paired BTC +
  Polymarket capture
- `/Users/forrestliao/workspace/analysis/analyze_data.py` for general paired
  tick analysis
- `/Users/forrestliao/workspace/analysis/analyze_paired_strategy.py` for
  paired-window replay
- Trading primitives under `/Users/forrestliao/workspace/polybot/trading/`
- Market discovery and websocket code under
  `/Users/forrestliao/workspace/polybot/market/`

## What Was Removed

- retired runtime configs and strategy code
- old TP/SL/re-entry execution path in the runtime monitor
- legacy last-minute strategy analysis script
- old trade-config tests and other tests tied to deleted logic
- stale data files and split-output directories not needed for the current
  strategy

## Runtime Configs

- Main config: `/Users/forrestliao/workspace/paired_window.yaml`
- Two-round validation config:
  `/Users/forrestliao/workspace/paired_window_2r.yaml`

Current default parameters in the active config family:

- `theta_pct: 0.02`
- `entry_start_remaining_sec: 270`
- `entry_end_remaining_sec: 120`
- `persistence_sec: 10`
- `min_entry_price: 0.60`
- `max_entry_price: 0.70`
- `max_entries_per_window: 1`

## Run / Research

```bash
rm -f log/*
python3.11 tools/collect_data.py --market btc-updown-5m --windows 10
python3.11 analysis/analyze_data.py data/collect_btc-updown-5m_*.jsonl
python3.11 analysis/analyze_paired_strategy.py data/collect_btc-updown-5m_<ts>.jsonl
python3.11 run.py --config paired_window_2r.yaml --dry
pytest -q
```

## Current Data To Keep

Only the current paired-window working dataset is kept under `data/`:

- `/Users/forrestliao/workspace/data/collect_btc-updown-5m_1776768514.jsonl`

## Recent 2-Hour Strategy Snapshot

Using:

- `/Users/forrestliao/workspace/data/collect_btc-updown-5m_1776768514.jsonl`
- `theta_pct = 0.02`
- `persistence_sec = 10`
- hold to window end

Best `entry band` by max entry price cap in that sample:

- `cap=0.61` -> best band `[180,270]`
- `cap=0.65` -> best band `[60,180]`
- `cap=0.70` -> best band `[120,270]`

More detailed band-level numbers are documented in:

- `/Users/forrestliao/workspace/README.md`
- `/Users/forrestliao/workspace/CLAUDE.md`

## Guidance For New Sessions

- Treat `paired_window` as the only active runtime strategy
- Do not assume `run.py` is disabled; it is active again
- Do not reintroduce TP/SL/re-entry logic unless explicitly requested
- Before any new dry-run, clear `/Users/forrestliao/workspace/log/` first
- Prefer `analysis/analyze_paired_strategy.py` for parameter work tied to the
  live runtime behavior
