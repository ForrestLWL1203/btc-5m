# Polybot — Polymarket BTC 延迟套利交易机器人

自动化 Polymarket BTC/ETH 涨跌预测市场交易机器人。利用 BTC 在 Binance 上领先 Polymarket ~0.75s 的反应延迟进行套利。

## 快速开始

```bash
# 安装依赖 (Python 3.11+)
pip3.11 install -r requirements.txt

# Dry run（推荐）
python3.11 run.py --config latency_arb.yaml --dry

# CLI 模式
python3.11 run.py --market btc-updown-5m --amount 1 --tp-price 0.80 --sl-pct 0.05 --dry

# 实盘交易（去掉 --dry）
python3.11 run.py --config latency_arb.yaml
```

## 数据收集与分析

```bash
# 收集 BTC + Polymarket 配对数据（10 个窗口）
python3.11 tools/collect_data.py --market btc-updown-5m --windows 10

# 分析延迟、反应模型、edge 机会
python3.11 analysis/analyze_data.py data/collect_btc-updown-5m_*.jsonl

# Edge 质量分桶分析（edge × flow × velocity）
python3.11 analysis/analyze_edge_quality.py data/collect_btc-updown-5m_*.jsonl

# Edge 衰减分析（最优持仓时间、半衰期）
python3.11 analysis/analyze_edge_decay.py data/collect_btc-updown-5m_*.jsonl

# 参数扫描（edge / cooldown / 重入上限 / 相位限额）
python3.11 analysis/analyze_param_scan.py data/collect_btc-updown-5m_*.jsonl
```

## 策略说明

### LatencyArbStrategy — BTC 延迟套利

利用 BTC 在 Binance 上领先 Polymarket ~0.75s 的反应延迟进行套利。

**核心逻辑**：
1. Binance WS 实时接收 BTC 交易流，计算 6 个特征（ret_2s, ret_5s, velocity, abs_vel, flow_imbalance, data_age_ms）
2. 代入线性回归模型预测 UP token 价格变动（edge）
3. edge 过阈值且持续一小段时间后入场，方向由 edge 符号决定：正=UP，负=DOWN
4. 持仓后优先看 edge 反转/衰减、`max_hold_sec`，TP/SL 主要作为兜底退出

**数据驱动校准**（10 窗口分析结果）：

| Edge 区间 | 胜率 | 净 PnL（扣费后） | 判定 |
|-----------|------|-----------------|------|
| < 0.01 | ~35% | 负 | 纯噪声 |
| 0.01-0.02 | ~33% | 负 | 噪声 |
| **≥ 0.02** | **47.6%** | **+0.031** | **真 alpha** |
| ≥ 0.05 | ~28% | +0.121 | 极端事件，稀有 |

**入场过滤**：`edge_threshold` + `persistence_ms` + 数据新鲜度 + `min_entry_price <= price <= max_entry_price` + 窗口前 4 分钟 + 最小重入间隔 + edge re-arm + 分阶段限额

**退出触发**：edge 反转、edge 衰减到阈值比例、TP/SL、持仓超过 `max_hold_sec`、窗口结束强制卖出。买入后前 `edge_decay_grace_ms` 内仅屏蔽 `edge_decayed` 快退，`edge_reversed` 仍立即退出。

**多笔交易**：每个 5 分钟窗口可交易多次，但现在有显式风控：
- `max_edge_reentry` 控制 edge exit 后还能重来几次
- `max_entries_per_window` 控制单窗口总入场次数
- `min_reentry_gap_sec` 避免几秒内连续打满
- `edge_rearm_threshold` 要求 edge 先“降温”再允许下一次触发
- `phase_one_sec / max_entries_phase_one` 把前 90 秒作为主战场
- `phase_two_sec / max_entries_phase_two` 控制中段最多补几笔
- `disable_after_sec` 在后段直接禁止开新仓

```yaml
# latency_arb.yaml
strategy:
  type: latency_arb
  coefficients:
    ret_2s: 0.985070
    ret_5s: -0.163321
    velocity: 0.001246
    abs_vel: 0.002184
  edge_threshold: 0.02
  max_data_age_ms: 800
  min_entry_price: 0.25
  max_entry_price: 0.70
  max_hold_sec: 2.0
  edge_decay_grace_ms: 300
  persistence_ms: 200
  cooldown_sec: 1.0
  min_reentry_gap_sec: 3.0
  edge_rearm_threshold: 0.01
  phase_one_sec: 90
  max_entries_phase_one: 2
  phase_two_sec: 180
  max_entries_phase_two: 3
  disable_after_sec: 180

params:
  amount: 1.0
  tp_price: 0.80
  sl_pct: 0.05
  max_tp_reentry: 0
  max_edge_reentry: 3
  max_entries_per_window: 4
```

### 止盈 (TP) / 止损 (SL)

支持**百分比**和**绝对价格**两种模式，可自由组合：

| 模式 | 参数 | 阈值计算 | 示例 |
|---|---|---|---|
| 百分比 | `tp_pct` / `sl_pct` | `entry × (1 ± pct)` | tp_pct=0.30 → 入场 50¢ 时 65¢ 卖出 |
| 绝对价格 | `tp_price` / `sl_price` | 固定价格 | tp_price=0.80 → 到 80¢ 即卖出 |

- 两种模式可混合使用
- 同时设置时，**绝对价格优先**
- **止损时间门槛**：5m 窗口过半后才允许止损
- 当前 `latency_arb.yaml` 默认使用 `tp_price: 0.80 + sl_pct: 0.05`
- 对 latency-arb 而言，`edge_exit + max_hold` 是主退出逻辑，`stop_loss` 更像安全兜底

### 重入与窗口风控

- `max_sl_reentry` — 止损后允许重入次数（0=禁用）
- `max_tp_reentry` — 止盈后允许重入次数（0=禁用）
- `max_edge_reentry` — edge 退出后允许重入次数（0=禁用）
- `max_entries_per_window` — 每窗口最大入场次数（None=不限）
- `min_reentry_gap_sec` — 任意两次买入之间的最小间隔
- `edge_rearm_threshold` — 上一次交易后，必须等 `abs(edge)` 先回落到该值以下，才允许再次触发
- `phase_one_sec / max_entries_phase_one` — 前段相位的限额
- `phase_two_sec / max_entries_phase_two` — 中段相位的限额
- `disable_after_sec` — 超过该秒数后不再允许新开仓
- `edge_decay_grace_ms` — 买入后短暂忽略 `edge_decayed`，避免 300ms 内立刻被轻微衰减打掉
- 当前 5m 策略允许脚本在窗口开始后 60 秒内接管，不再要求必须卡开窗头几秒启动

## 使用方法

### 方式一：YAML 配置（推荐）

```bash
python3.11 run.py --config latency_arb.yaml --dry
```

### 方式二：命令行参数

```bash
python3.11 run.py --market btc-updown-5m --amount 1 --tp-price 0.80 --sl-pct 0.05 --dry
```

### 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--config` | — | YAML 配置文件路径 |
| `--market` | btc-updown-5m | 市场预设 |
| `--strategy` | latency_arb | 策略（目前仅 latency_arb） |
| `--amount` | 5.0 | 每笔交易金额（美元） |
| `--tp-pct` | 0.50 | 止盈百分比 |
| `--sl-pct` | 0.30 | 止损百分比 |
| `--tp-price` | — | 绝对价格止盈 |
| `--sl-price` | — | 绝对价格止损 |
| `--max-reentry` | 0 | 止损后最大重入次数 |
| `--max-tp-reentry` | 0 | 止盈后最大重入次数 |
| `--rounds` | ∞ | 完整窗口轮数 |
| `--dry` | off | 模拟模式，只记录不实际下单，并输出近似每笔/每窗收益 |

当前主配置没有使用 `sl_price`，而是使用 `sl_pct: 0.05` 作为 latency-arb 的兜底止损。

## 环境配置

### 1. Python 3.11+

```bash
brew install python@3.11
```

### 2. 项目依赖

```bash
git clone https://github.com/ForrestLWL1203/btc-5m.git
cd btc-5m
pip3.11 install -r requirements.txt
```

### 3. Polymarket 钱包

```bash
npm install -g polymarket
polymarket setup
```

钱包需要在 **Polygon 网络** 上持有 USDC。

### 4. 网络代理（中国大陆）

```bash
# .env 文件
HTTPS_PROXY=http://127.0.0.1:7897
HTTP_PROXY=http://127.0.0.1:7897
```

## 项目结构

```
polybot/                        # 交易包
├── core/                       # 核心基础设施
│   ├── auth.py                 # 钱包凭证 + ClobClient 初始化
│   ├── client.py               # ClobClient 单例，REST 查询，预缓存
│   ├── config.py               # 默认常量
│   ├── log_formatter.py        # 结构化日志
│   └── state.py                # MonitorState + target_side + 进场时间戳 + 窗口统计
├── market/                     # 市场数据层
│   ├── binance.py              # BinanceTradeFeed — BTC WS + 特征提取
│   ├── market.py               # MarketWindow + slug 精确发现 + 未来窗口链路
│   ├── series.py               # MarketSeries — 市场身份定义
│   └── stream.py               # WebSocket 实时价格流
├── strategies/                 # 交易策略
│   ├── base.py                 # Strategy ABC
│   └── latency_arb.py          # LatencyArbStrategy — BTC 延迟套利 + 分层入场风控
├── trading/                    # 订单执行 + 监控
│   ├── monitor.py              # 异步监控 + edge 退出 + dry-run 收益统计 + deferred replay
│   └── trading.py              # FOK + GTD 订单执行
├── config_loader.py            # YAML 加载 + 策略注册表
└── trade_config.py             # TradeConfig — 通用参数

run.py                          # 入口点
tools/collect_data.py           # 双 WS 数据收集器
analysis/common.py              # 分析脚本共享能力（load_data / fit model / lookup）
analysis/analyze_data.py        # 延迟/反应模型分析
analysis/analyze_edge_quality.py # Edge 质量分桶分析
analysis/analyze_edge_decay.py  # Edge 衰减/最优持仓分析
analysis/analyze_param_scan.py  # 参数扫描（threshold / cooldown / reentry cap / phase cap）
latency_arb.yaml                # 延迟套利配置（主）
latency_arb_fast.yaml           # 快速变体（cooldown 0.5s）
latency_arb_probe.yaml          # 实验参数
```

## 支持市场

| `--market` 参数 | Asset | Timeframe | 窗口时长 |
|---|---|---|---|
| btc-updown-5m | BTC | 5m | 5 分钟 |
| btc-updown-15m | BTC | 15m | 15 分钟 |
| btc-updown-4h | BTC | 4h | 4 小时 |
| eth-updown-5m | ETH | 5m | 5 分钟 |
| eth-updown-15m | ETH | 15m | 15 分钟 |
| eth-updown-4h | ETH | 4h | 4 小时 |

## 风险提示

本工具仅供学习和研究用途。加密货币预测市场交易存在高风险，可能导致资金损失。使用前请充分了解 Polymarket 的交易规则和费用结构。

## Dry-run 说明

- `dry-run` 会完整跑策略、记录逐笔买卖、输出近似每笔和每窗收益
- 近似收益按成交价差估算，不含手续费、滑点和真实成交偏差
- 当前 `dry-run` 已避免触发真实 `cancel-all` / `sell` 路径，适合做窗口级行为观察
