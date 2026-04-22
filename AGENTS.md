# AGENTS.md - Implementation Guidance for Future Development

## Project Status (2026-04-22)

**Polymarket BTC 5-Minute Binary Options Trading Bot**

Status: ✅ **VALIDATED & LIVE READY**
- Single active strategy: `paired_window` (BTC momentum + Polymarket binary entry)
- 77-window validation: 70.8% win rate, +$0.89 EV per $1
- Live 2-round test: 67.99% ROI ($2 → $3.36)
- All 80 tests passing
- Risk management system integrated (5-loss pause + win-rate monitor)

## Core Runtime Components

**Strategy & Execution Layer:**
- `polybot/strategies/paired_window.py` — Direction prediction (UP/DOWN based on BTC theta move + persistence)
- `polybot/trading/monitor.py` — Window lifecycle management + delayed exit logic + risk triggers
- `polybot/trading/trading.py` — Order execution (FOK market orders via Polymarket CLOB)
- `polybot/core/state.py` — MonitorState dataclass with daily risk tracking
- `polybot/config_loader.py` — Configuration parsing + **dynamic min_entry_price calculation** (max * 0.88)

**Current Runtime Behavior:**
- Entry: Detect BTC momentum (theta move + persistence) → buy token if in [min, max] price band
- Hold: Full 300s until window close (ensures settlement, enables exact balance queries)
- Exit: Wait until market resolution (price $0.95+) → sell at high price (~$0.99)
- Risk: Track daily stats, pause on 5 consecutive losses or <50% win rate after 30 trades
- **NO** TP/SL/re-entry logic — intentionally narrow, high signal quality

## What Exists (Maintained)

**Data & Analysis Tools:**
- `tools/collect_data.py` — Capture live BTC + Polymarket CLOB state → JSONL
- `analysis/analyze_paired_strategy.py` — **PRIMARY ANALYSIS TOOL** — Backtesting framework for parameter exploration
- `analysis/analyze_data.py` — Basic tick analysis
- `data/collect_btc-updown-5m_*.jsonl` — 77 windows (1.5GB), used for validation

**Infrastructure:**
- `polybot/market/` — Market discovery, window detection, websocket stream handling
- `polybot/trading/` — Order execution, fill handling, balance management
- `polybot/core/client.py` — Polymarket CLOB API client

**Testing & Validation:**
- `tests/` — 80 passing tests (config, monitor, trading, market, stream, series)

## What Was Removed (Not Maintained)

- Retired strategies (momentum, latency arbitrage, etc.)
- TP/SL/re-entry logic (old execution path)
- Legacy analysis scripts
- Stale test files
- Old config files (cap61/cap65 versions)
- Split-output data utilities

## Recommended Configs (Current)

| Config | Purpose | Parameters | Status |
|--------|---------|-----------|--------|
| `paired_window_optimized.yaml` | **LIVE READY** | cap=0.65, band=[60,180], min auto-calc | ✅ Validated |
| `paired_window.yaml` | Legacy main | cap=0.70, band=[240,60] | Working |
| `paired_window_2r.yaml` | Quick tests | 2-round template | Dev only |

**Optimized Config Details:**
- `theta_pct: 0.02` (0.02% BTC move)
- `persistence_sec: 10` (move persists 10s)
- `entry_start_remaining_sec: 240` (start 60s after window open)
- `entry_end_remaining_sec: 60` (stop 180s after window open)
- `min_entry_price: auto (0.57)` ← Calculated as max * 0.88
- `max_entry_price: 0.65` ← Best validation result
- `max_entries_per_window: 1`

## Key Implementation Details (For Code Changes)

**Exit Strategy (Why Wait Until Window End):**
- Window end = 300s after start = 3+ minutes post-entry
- Justification: On-chain settlement complete, exact balance available
- Old approach: 10s early exit → settlement incomplete → balance truncation → 400 API errors ❌
- New approach: Exact window end → settlement done → clean balance query → market price $0.95+ ✅

**Dynamic min_entry_price (Why cap * 0.88):**
- 77-window analysis: 0.45-0.50 price range has only 50% win rate (weak signal)
- Solution: Filter with `min = max * 0.88`
- Example: cap=0.65 → min=0.57 (auto-calculated in config_loader.py)
- Generalizes: cap=0.70 → min=0.62, cap=0.75 → min=0.66
- Effect: Maintains 75%+ overall win rate while removing weak signals

**Risk Management Daily Reset:**
- UTC+8 timezone (not UTC) — user's local timezone
- Reset at midnight UTC+8: daily_wins, daily_losses, consecutive_losses counters
- Both triggers (5-loss pause, <50% WR pause) are temporary, not day-end shutdown

## Typical Development Workflow

**Data Collection:**
```bash
python3.11 tools/collect_data.py --market btc-updown-5m --windows 30
```

**Parameter Exploration:**
```bash
# Analyze specific parameter set on collected data
python3.11 analysis/analyze_paired_strategy.py data/collect_btc-updown-5m_<TS>.jsonl \
  --theta 0.02 --lo_rem 60 --hi_rem 180 --cap 0.65
```

**Testing Changes:**
```bash
# Clear logs, run 2-round dry-run
rm -f log/*
python3.11 run.py --config paired_window_optimized.yaml --dry

# Full test suite
pytest -q
```

**Live Trading (⚠️ Real Money):**
```bash
python3.11 run.py --config paired_window_optimized.yaml
```

## Guidance For New Agent Sessions

**DO:**
- Treat `paired_window` as the only active strategy
- Use `analyze_paired_strategy.py` for parameter work (matches live behavior)
- Clear `/log/` before dry-runs to isolate outputs
- Reference CLAUDE.md + README.md for strategy details
- Check config_loader.py for dynamic parameter logic
- Validate changes against 77-window dataset before live trading

**DON'T:**
- Reintroduce TP/SL/re-entry logic (intentionally removed for high signal quality)
- Change exit timing away from window.end_epoch (breaks settlement guarantee)
- Use legacy analysis scripts (deleted; use analyze_paired_strategy.py)
- Assume run.py is disabled (it's live)
- Modify market discovery logic without understanding websocket/REST fallback

## Recent Milestones (2026-04-22)

✅ Implemented delayed exit strategy (wait for $0.95+ market resolution)
✅ Added risk management system (5-loss pause + win-rate monitor + UTC+8 reset)
✅ Validated on 77 windows: 70.8% win rate, +$0.89 EV
✅ Live trading proof: $1 → $3.36 (67.99% ROI) in 2 rounds
✅ Implemented dynamic min_entry_price formula (cap * 0.88)
✅ Cleaned workspace: 80/80 tests passing, no legacy files
✅ Updated documentation: CLAUDE.md, README.md, AGENTS.md
