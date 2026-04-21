# Polybot

Polymarket BTC/ETH up/down research and execution repo, currently centered on a
single active runtime candidate: `paired_window`.

## Current Status

The old retired strategy work is gone. The repository now has:

- Runtime strategy: `paired_window`
- Runtime entry point: `/Users/forrestliao/workspace/run.py`
- Market discovery and websocket monitoring infrastructure
- Paired BTC + Polymarket data collection
- Offline paired-window replay and parameter analysis

The current runtime logic is intentionally simple:

- decide direction and entry timing
- enter once per window at most
- hold until near window end
- no TP / SL / re-entry logic

## Main Files

- Runtime strategy: `/Users/forrestliao/workspace/polybot/strategies/paired_window.py`
- Runtime monitor: `/Users/forrestliao/workspace/polybot/trading/monitor.py`
- Config loader: `/Users/forrestliao/workspace/polybot/config_loader.py`
- Data collection: `/Users/forrestliao/workspace/tools/collect_data.py`
- Basic analysis: `/Users/forrestliao/workspace/analysis/analyze_data.py`
- Strategy replay: `/Users/forrestliao/workspace/analysis/analyze_paired_strategy.py`

## Runtime

Dry-run validation:

```bash
rm -f log/*
python3.11 run.py --config paired_window_2r.yaml --dry
```

Main config:

```bash
python3.11 run.py --config paired_window.yaml --dry
```

The runtime now supports attaching to an already-started window as long as the
configured `entry band` has not elapsed yet. It no longer skips purely because
the window is more than 60 seconds old.

Before each new dry-run, clear old files in `/Users/forrestliao/workspace/log/`
so the generated log and jsonl outputs belong only to that run.

## Data Collection

```bash
python3.11 tools/collect_data.py --market btc-updown-5m --windows 10
```

## Research

```bash
python3.11 analysis/analyze_data.py data/collect_btc-updown-5m_*.jsonl
python3.11 analysis/analyze_paired_strategy.py data/collect_btc-updown-5m_<ts>.jsonl
```

Use `analysis/analyze_paired_strategy.py` for parameter work tied to the live
`paired_window` strategy. It matches the current execution style much better
than the older deleted analysis scripts.

## Current Default Strategy Parameters

The active runtime configs currently use:

- `theta_pct: 0.02`
- `entry_start_remaining_sec: 270`
- `entry_end_remaining_sec: 120`
- `persistence_sec: 10`
- `min_entry_price: 0.60`
- `max_entry_price: 0.70`
- `max_entries_per_window: 1`

Conceptually, the strategy does:

1. Observe BTC relative to the window open
2. During the configured `entry band`, wait for a persistent move
3. Pick `UP` if BTC is above open, `DOWN` if BTC is below open
4. Buy only if the target token is inside the configured entry price band
5. Hold until close

## Recent 2-Hour Parameter Snapshot

Latest discussed sample:

- data file: `/Users/forrestliao/workspace/data/collect_btc-updown-5m_1776768514.jsonl`
- windows: `24`
- replay assumptions:
  - `theta_pct = 0.02`
  - `persistence_sec = 10`
  - hold to window end
  - no early exits

### Max Entry Price `0.61`

| Entry Band | N | Wins | Win Rate | Avg Entry | EV / Trade |
|---|---:|---:|---:|---:|---:|
| `[60,180]` | 8 | 4 | 50.00% | 0.5363 | -0.0362 |
| `[60,240]` | 12 | 6 | 50.00% | 0.5642 | -0.0642 |
| `[120,240]` | 11 | 6 | 54.55% | 0.5618 | -0.0164 |
| `[120,270]` | 14 | 10 | 71.43% | 0.5486 | +0.1657 |
| `[180,270]` | 12 | 9 | 75.00% | 0.5408 | +0.2092 |

Best in this sample: `[180,270]`

### Max Entry Price `0.65`

| Entry Band | N | Wins | Win Rate | Avg Entry | EV / Trade |
|---|---:|---:|---:|---:|---:|
| `[60,180]` | 12 | 8 | 66.67% | 0.5783 | +0.0883 |
| `[60,240]` | 17 | 10 | 58.82% | 0.5906 | -0.0024 |
| `[120,240]` | 17 | 10 | 58.82% | 0.5906 | -0.0024 |
| `[120,270]` | 20 | 13 | 65.00% | 0.5885 | +0.0615 |
| `[180,270]` | 19 | 12 | 63.16% | 0.5884 | +0.0432 |

Best in this sample: `[60,180]`

### Max Entry Price `0.70`

| Entry Band | N | Wins | Win Rate | Avg Entry | EV / Trade |
|---|---:|---:|---:|---:|---:|
| `[60,180]` | 15 | 9 | 60.00% | 0.6113 | -0.0113 |
| `[60,240]` | 20 | 13 | 65.00% | 0.6145 | +0.0355 |
| `[120,240]` | 20 | 13 | 65.00% | 0.6145 | +0.0355 |
| `[120,270]` | 20 | 13 | 65.00% | 0.5930 | +0.0570 |
| `[180,270]` | 19 | 12 | 63.16% | 0.5932 | +0.0384 |

Best in this sample: `[120,270]`

## Testing

```bash
pytest -q
```

After the latest cleanup, the test suite is aligned with the current
paired-window runtime rather than the deleted legacy TP/SL execution path.
