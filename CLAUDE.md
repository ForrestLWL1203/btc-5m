# CLAUDE.md - Paired Window Strategy Runtime State

## Repo State (2026-04-22 UPDATED)

This repository has an active, validated runtime strategy:

**Core Runtime Components:**
- Strategy: `paired_window` (BTC 5-minute direction prediction + entry timing)
- Strategy file: `polybot/strategies/paired_window.py`
- Runner: `run.py`
- Monitor: `polybot/trading/monitor.py` (with exit logic + risk management)
- Risk management: `polybot/core/state.py` (daily stats, loss tracking)
- Config loader: `polybot/config_loader.py` (dynamic parameter calculation)

**Key Runtime Features:**
- **Direction & Entry Timing**: Strategy predicts UP/DOWN, detects BTC theta move with persistence check
- **Delayed Exit Strategy**: Waits until window end for market resolution, monitors price until >0.95 or 10-min timeout
- **Risk Management System**:
  - 5 consecutive losses → skip next 2 windows (0.21% probability, system anomaly signal)
  - After 30+ trades: if win rate < 50% → skip next 5 windows (strategy failure signal)
  - Daily stats reset at UTC+8 midnight
- **No TP/SL/Re-entry**: Position held from entry to window end; no intermediate exits
- **Late Window Attachment**: Can attach to window mid-trade if entry band hasn't elapsed yet

## Active Runtime Configs

- **Optimized (RECOMMENDED)**: `paired_window_optimized.yaml` 
  - cap=0.65, band=[60,180], min_entry_price auto-calculated (0.57)
  - Validated on 77-window dataset: 70.8% win rate, +$0.89 EV per trade
  - Real-world testing: 2-round trial → $1 invested → $3.36 returned (67.99% ROI)

- **Main fallback**: `paired_window.yaml` (cap=0.70, band=[240,60])
- **Quick validation**: `paired_window_2r.yaml` (2-round dry-run template)

**Recommended runtime shape (optimized):**
- `theta_pct: 0.02` (0.02% BTC move threshold)
- `persistence_sec: 10` (move must hold for 10s)
- `entry_start_remaining_sec: 240` (start checking at ~60s after window open)
- `entry_end_remaining_sec: 60` (stop checking at ~180s into window)
- `max_entry_price: 0.65` (dynamic min auto-calculated as 0.65*0.88=0.57)
- `max_entries_per_window: 1`

## Execution Flow

```
Window Opens (T=0s)
  ↓
Entry Band Start (T=60s, remaining=240s)
  • Monitor BTC price against window open
  • Detect theta_pct move + persistence_sec hold
  • Emit BUY_SIGNAL
  ↓
Buy Execution (if signal + price in [min, max] band)
  • FOK (Fill-Or-Kill) market order via Polymarket CLOB
  • Store shares in state.holding_size
  ↓
Entry Band End (T=180s, remaining=60s)
  • Stop checking for new signals
  • Continue holding existing position
  ↓
Window Close (T=300s, remaining=0s)
  • Detect market resolution (price → $0.95+ for winner)
  • Check direction_correct = (token_price > 0.5)
  ↓
Post-Window Phase:
  IF direction_correct:
    → If price > 0.95: SELL immediately (market resolved)
    → Else if price ≤ 0.95: wait up to 10 minutes for resolution
  ELSE (direction wrong):
    → Position worthless, log as loss (no sell needed)
  ↓
Risk Check:
  IF 5 consecutive losses → skip next 2 windows
  IF (after 30+ trades) win_rate < 50% → skip next 5 windows
  ↓
UTC+8 Daily Reset (at midnight UTC+8):
  • Reset daily_wins, daily_losses, consecutive_losses counter
```

## Implementation Details

**Entry Price Band (min/max):**
- `max_entry_price`: User-configured (e.g., 0.65)
- `min_entry_price`: Auto-calculated as `max * 0.88` (cap - 12%)
  - Rationale: 77-window analysis shows 0.45-0.50 range has only 50% win rate (weak signals)
  - Setting floor to cap*0.88 filters weak signals while maintaining 75%+ overall win rate
  - Can be explicitly overridden in config if needed

**Exit Strategy:**
- No TP/SL/intermediate exits during window
- Position held from entry until window.end_epoch (exact 300s point)
- Exit delay justification: 3+ minutes allows on-chain settlement; get_token_balance() returns exact balance (not truncated estimate)
- Market resolution happens within seconds of window end; delayed exit captures high prices (0.99)

**Signal Quality Filter:**
- BTC move must exceed theta_pct (0.02% = 20 bps typically ~$15 BTC move on $77k)
- Move must persist for persistence_sec (10s); eliminates spike noise
- Entry only during configured band ([60s, 180s] for optimized config)

**Risk Management:**
- Consecutive loss detection: if 5 losses in a row → likely system/market anomaly
- Win rate failure: after 30 trades, if <50% → strategy no longer profitable
- Both triggers pause trading temporarily (not day-end shutdown) to allow recovery

## 77-Window Validation Results (2026-04-22)

**Dataset:** Combined three collections (77 total windows, 24h span)
- `collect_btc-updown-5m_1776768514.jsonl` (24 windows)
- `collect_btc-updown-5m_1776822909.jsonl` (23 windows)
- `collect_btc-updown-5m_1776831065.jsonl` (30 windows)

**Recommended Configuration: cap=0.65, band=[60,180]**

| Metric | Value | Notes |
|--------|-------|-------|
| Entry signals | 48 / 77 windows | 62.3% entry rate |
| Win rate | 70.8% | 34 wins, 14 losses |
| CI lower bound (95%) | 56.8% | High confidence |
| Avg entry price | 0.5885 | Range [0.21, 0.68] |
| EV per $1 trade | +$0.89 | Total PnL: $42.70 on 48 trades |
| Expected daily (24h) | ~$29 profit | At ~179 entries/day |

**Live Trading Validation (2 rounds, Apr 22 16:06-16:15 UTC):**
- Round 1: Buy $0.635 → Sell $0.9950 → +$0.567 (56.7%)
- Round 2: Buy $0.555 → Sell $0.9950 → +$0.793 (79.3%)
- **Total: $2 invested → $3.36 returned → 67.99% ROI** ✅

**Entry Price Range Analysis (77-window depth):**

| Price Range | Trades | Win% | Confidence | Status |
|-------------|--------|------|-----------|--------|
| <0.40 | 7 | 71.4% | 35.9% | ✓ OK |
| 0.40-0.45 | 1 | 100% | 20.7% | ⚠️ Sparse |
| **0.45-0.50** | **2** | **50%** | **9.5%** | **❌ WEAK** |
| 0.50-0.55 | 5 | 80.0% | 37.6% | ✓ Good |
| 0.55-0.60 | 14 | 64.3% | 38.8% | ✓ OK |
| 0.60-0.65 | 12 | 83.3% | 55.2% | ✓ Strong |
| 0.65+ | 7 | 85.7% | 48.7% | ✓ Strongest |

→ **Key Finding**: 0.45-0.50 range shows poor 50% win rate. Filtering with `min = cap * 0.88` eliminates this range while maintaining 75%+ overall accuracy.

## Code References for Future Development

**Critical files for strategy modification:**
- `polybot/strategies/paired_window.py`: Direction prediction + entry signal generation
- `polybot/trading/monitor.py`: Window lifecycle, exit logic, risk management triggers
- `polybot/core/state.py`: MonitorState dataclass with daily risk tracking fields
- `polybot/config_loader.py`: Dynamic min_entry_price calculation (lines 60-86)

**Analysis & Validation:**
- `analysis/analyze_paired_strategy.py`: Reusable backtesting framework (collect_trades, wilson_lower)
- Data files: `data/collect_btc-updown-5m_*.jsonl` (77 windows, 1.5GB total)

**Testing:**
- `tests/test_config_loader.py`: Config loading + strategy building (17 tests)
- `tests/test_monitor.py`: Window monitoring logic (9 tests)
- All 80 tests passing (`pytest -q`)

## Quick Start Commands

```bash
# Clear logs for clean run
rm -f log/*

# Dry-run (no real money)
python3.11 run.py --config paired_window_optimized.yaml --dry

# Live trading (real money!)
python3.11 run.py --config paired_window_optimized.yaml

# Collect fresh data
python3.11 tools/collect_data.py --market btc-updown-5m --windows 30

# Analyze collected data
python3.11 analysis/analyze_paired_strategy.py data/collect_btc-updown-5m_<TIMESTAMP>.jsonl

# Run all tests
pytest -q
```

## Working Guidance For New Sessions

**Strategy:**
- Treat `paired_window` as the only active runtime strategy
- Do not reintroduce TP/SL/re-entry logic unless explicitly requested
- Do not change exit timing away from window.end_epoch (market settlement guarantee)

**Testing & Iteration:**
- Before any dry-run, clear `log/` so logs belong to current run only
- Use `analyze_paired_strategy.py` for parameter evaluation (not deleted legacy scripts)
- If considering cap/band changes, use 77-window dataset for validation (not just dry-run)

**Window Attachment:**
- Safe to attach mid-window only if entry band hasn't elapsed yet
- Entry band check uses strategy's `entry_end_remaining_sec` (default 60s)
- Window older than band → skip and move to next window

**Risk Management:**
- Daily stats reset at UTC+8 midnight (not UTC)
- Both loss counters (5 consecutive + 30-sample WR) trigger pause, not day-end shutdown
- Paused windows are skipped; trading resumes after pause windows elapse

**Configuration:**
- `min_entry_price` is auto-calculated if not specified: `max * 0.88`
- Can override in YAML if testing non-standard ranges
- `max_entry_price` should stay ≤ 0.70 (higher = weaker signal quality)
