# Polybot — Polymarket Up/Down Trading Bot

自动化 Polymarket BTC/ETH 涨跌预测市场交易机器人。支持多市场（BTC/ETH × 5m/15m/4h）、可插拔策略、YAML 配置、WebSocket 实时定价。

## 快速开始

```bash
# 安装依赖 (Python 3.11+)
pip3.11 install -r requirements.txt

# Dry run（模拟，不下单）
python3.11 run.py --market btc-updown-5m --side up --amount 1 --tp-pct 0.30 --sl-pct 0.30 --dry

# 实盘交易 — 固定方向
python3.11 run.py --market btc-updown-5m --side up --amount 1 --tp-pct 0.30 --sl-pct 0.30 --rounds 1

# 实盘交易 — 动量自动预测方向
python3.11 run.py --market btc-updown-5m --strategy momentum --amount 1 --tp-price 0.80 --sl-pct 0.50 --rounds 1

# YAML 配置模式
python3.11 run.py --config strategy.yaml --dry

# 交互模式（引导式输入）
python3.11 run.py --dry
```

## 策略说明

### 策略类型

| 策略 | `--strategy` | 说明 |
|---|---|---|
| FixedSideStrategy | `immediate`（默认） | 固定方向，窗口开即买 |
| MomentumStrategy | `momentum` | 自动预测方向（7 指标加权投票） |

### MomentumStrategy — 自动方向预测

通过 Binance K线数据 + 7 个技术指标加权投票预测每窗口的买入方向。

**7 信号加权（MomentumPredictor V3）**：

| 信号 | 权重 | 说明 |
|---|---|---|
| 趋势方向 | 20% | 最近 N 根 K 线涨跌比例 |
| EMA 交叉 | 15% | 短期/长期 EMA 差值 |
| RSI | 10% | 相对强弱指标（<40 超卖，>60 超买） |
| 成交量确认 | 5% | 量价配合 |
| MACD 直方图 | 20% | 动量 + 趋势方向 |
| 布林 %B | 15% | 价格在布林带中的位置 |
| 价格 ROC | 15% | N 周期价格变化率 |

- 使用 Binance K线数据（支持 5m/15m/4h）
- 数据不足 15 根 K 线时跳过该窗口
- CLI 启用：`--strategy momentum`
- YAML 启用：`strategy.type: momentum`

### FixedSideStrategy（默认）

窗口开启后立即以首个价格买入，不做价格区间判断。买入后持续监控止盈/止损。

```
窗口开始 → 获取实时价格 → 立即买入 → 监控止盈/止损
                                      ↓
                               价格触及止盈/止损？
                               ↓ 是
                              卖出 → 允许重入？
                              ↓ 是          ↓ 否
                        等待下一信号    等待窗口结束 → 下一轮
                        重新买入
```

### 止盈 (TP) / 止损 (SL)

支持**百分比**和**绝对价格**两种模式，可自由组合：

| 模式 | 参数 | 阈值计算 | 示例 |
|---|---|---|---|
| 百分比 | `tp_pct` / `sl_pct` | `entry × (1 ± pct)` | tp_pct=0.30 → 入场 50¢ 时 65¢ 卖出 |
| 绝对价格 | `tp_price` / `sl_price` | 固定价格 | tp_price=0.80 → 到 80¢ 即卖出 |

- 两种模式可混合使用（如百分比止盈 + 绝对价格止损）
- 同时设置百分比和绝对价格时，**绝对价格优先**
- **止损时间门槛**：不同窗口有不同的止损等待期，防止过早止损错过反弹

| 窗口时长 | 止损允许时机 | 说明 |
|---|---|---|
| 5m | 窗口过半（2m30s 后） | 短窗口，过半后允许 |
| 15m | 剩余 ≤ 5min | 中等窗口，最后 5 分钟允许 |
| 4h | 剩余 ≤ 1h | 长窗口，最后 1 小时允许 |

价格判断使用**乐观/悲观聚合**：
- 止盈取 `max(midpoint, last_trade_price, best_ask)` — 任一信号触发即卖出
- 止损取 `min(midpoint, last_trade_price, best_bid)` — 任一信号触发即卖出

### 重入机制

止损/止盈卖出后，可在同一窗口内重新买入。重入有价格门槛：

- `max_sl_reentry` — 止损后允许重入次数（0=禁用）
- `max_tp_reentry` — 止盈后允许重入次数（0=禁用）
- 每个窗口独立计数，达到上限后该窗口阻断买入

**重入价格条件**：

| 重入类型 | 条件 | 逻辑 |
|---|---|---|
| SL 重入 | 当前价 ≥ 原始入场价 × 0.95 | 价格反弹到接近入场价，确认假跌破 |
| TP 重入 | 当前价 ≤ 入场价 × (1 + tp_pct × 0.5) | 价格从高点回落至少一半涨幅，避免追高 |

### 轮数控制

- `--rounds N` 或 YAML `rounds: N` — 运行 N 个完整窗口后退出
- 省略或 `rounds: null` — 无限循环运行
- **已跳过的窗口不计数**（如启动时当前窗口已过半）

## 使用方法

### 方式一：命令行参数（推荐）

```bash
# 固定方向 — 买涨，BTC 5分钟市场
python3.11 run.py \
  --market btc-updown-5m \
  --side up \
  --amount 1 \
  --tp-pct 0.30 \
  --sl-pct 0.30 \
  --rounds 1

# 自动预测方向 — 动量策略
python3.11 run.py \
  --market btc-updown-5m \
  --strategy momentum \
  --amount 1 \
  --tp-price 0.80 \
  --sl-pct 0.50 \
  --rounds 1

# 绝对价格止盈 + 百分比止损
python3.11 run.py \
  --market eth-updown-5m \
  --side down \
  --amount 1 \
  --tp-price 0.80 \
  --sl-pct 0.30
```

### 方式二：YAML 配置

```bash
cp strategy.yaml.example strategy.yaml
# 编辑 strategy.yaml
python3.11 run.py --config strategy.yaml --dry
```

配置文件示例：

```yaml
market:
  asset: btc              # btc 或 eth
  timeframe: 5m           # 5m, 15m, 4h

strategy:
  type: immediate         # immediate（固定方向）或 momentum（自动预测）
  side: up                # immediate 策略的固定方向

params:
  amount: 1.0             # 每笔交易金额 (USD)
  tp_pct: 0.30            # 止盈 +30%（与 tp_price 二选一，绝对优先）
  sl_pct: 0.30            # 止损 -30%（与 sl_price 二选一，绝对优先）
  tp_price: null          # 绝对价格止盈（如 0.80 = 80¢）
  sl_price: null          # 绝对价格止损
  max_sl_reentry: 0       # 止损后不重入
  max_tp_reentry: 0       # 止盈后不重入

rounds: 1                 # 运行 1 轮后停止（省略则无限运行）
```

### 方式三：交互模式

```bash
python3.11 run.py --dry
```

程序会依次提示输入交易参数。

### 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--config` | — | YAML 配置文件路径（覆盖其他参数） |
| `--market` | btc-updown-5m | 市场预设（见支持市场表） |
| `--strategy` | immediate | 策略：`immediate`（固定方向）或 `momentum`（自动预测） |
| `--side` | up | 交易方向（immediate 策略必填） |
| `--amount` | 5.0 | 每笔交易金额（美元） |
| `--tp-pct` | 0.50 | 止盈百分比（0.30 = 入场价 +30% 卖出） |
| `--sl-pct` | 0.30 | 止损百分比（0.30 = 入场价 -30% 卖出） |
| `--tp-price` | — | 绝对价格止盈（与 tp-pct 二选一，绝对优先） |
| `--sl-price` | — | 绝对价格止损（与 sl-pct 二选一，绝对优先） |
| `--max-reentry` | 0 | 止损后最大重入次数 |
| `--max-tp-reentry` | 0 | 止盈后最大重入次数 |
| `--rounds` | ∞ | 完整窗口轮数（省略=无限） |
| `--dry` | off | 模拟模式，只记录不实际下单 |

## 环境配置

### 1. Python 3.11+

```bash
# macOS
brew install python@3.11

# Ubuntu
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt install python3.11 python3.11-venv
```

### 2. 项目依赖

```bash
git clone https://github.com/ForrestLWL1203/btc-5m.git
cd btc-5m
pip3.11 install -r requirements.txt
```

### 3. Polymarket 钱包

```bash
# 方式一：CLI 引导（推荐）
npm install -g polymarket
polymarket setup

# 方式二：手动创建
mkdir -p ~/.config/polymarket
cat > ~/.config/polymarket/config.json << 'EOF'
{
  "private_key": "你的私钥",
  "chain_id": 137,
  "signature_type": "proxy"
}
EOF
chmod 600 ~/.config/polymarket/config.json
```

| 字段 | 值 | 说明 |
|---|---|---|
| `private_key` | `0x...` | Polygon 钱包私钥 |
| `chain_id` | `137` | Polygon 主网 |
| `signature_type` | `"proxy"` | Magic Link 钱包用 `proxy`，EOA 用 `eoa` |

钱包需要在 **Polygon 网络** 上持有 USDC。

### 4. 网络代理（中国大陆）

```bash
# .env 文件（已 .gitignore）
HTTPS_PROXY=http://127.0.0.1:7897
HTTP_PROXY=http://127.0.0.1:7897
```

验证连通性：

```bash
curl -s -o /dev/null -w "%{http_code}" "https://gamma-api.polymarket.com/markets?limit=1"
# 返回 200 即正常
```

### 5. Dry Run 验证

```bash
python3.11 run.py --market btc-updown-5m --side up --amount 1 --tp-pct 0.30 --sl-pct 0.30 --dry

# 或测试动量策略
python3.11 run.py --market btc-updown-5m --strategy momentum --amount 1 --tp-pct 0.30 --sl-pct 0.30 --dry
```

看到 `[DRY-RUN MODE]` 和 WebSocket 价格更新表示全部连通。确认无误后去掉 `--dry` 实盘交易。

## 项目结构

```
polybot/                        # 交易包
├── core/                       # 核心基础设施
│   ├── auth.py                 # 钱包凭证 + ClobClient 初始化
│   ├── client.py               # ClobClient 单例，REST 查询，预缓存
│   ├── config.py               # 默认常量
│   ├── log_formatter.py        # 结构化日志（console + JSONL）
│   └── state.py                # MonitorState — 交易状态
├── market/                     # 市场数据层
│   ├── market.py               # MarketWindow + slug 发现
│   ├── series.py               # MarketSeries — 市场身份定义
│   └── stream.py               # WebSocket 实时价格流
├── predict/                    # 自动方向预测
│   ├── history.py              # WindowHistory 环形缓冲区 + Gamma API 回填
│   ├── indicators.py           # 7 个技术指标（EMA, RSI, MACD, Bollinger, ROC 等）
│   ├── kline.py                # KlineCandle + BinanceKlineFetcher
│   └── momentum.py             # MomentumPredictor V3 — 7 信号加权投票
├── strategies/                 # 可插拔交易策略
│   ├── base.py                 # Strategy ABC（get_side + should_buy）
│   ├── immediate.py            # FixedSideStrategy — 固定方向，窗口开即买
│   └── momentum.py             # MomentumStrategy — 自动预测方向
├── trading/                    # 订单执行 + 监控
│   ├── monitor.py              # 异步事件驱动监控循环
│   └── trading.py              # FOK 市价单 + GTD 限价回退
├── config_loader.py            # YAML 加载 + 工厂函数
└── trade_config.py             # TradeConfig — 通用参数 + check_exit()

run.py                          # 入口点
strategy.yaml.example           # 配置文件示例
docs/polymarket_api.md          # Polymarket API 参考文档
tests/                          # 单元测试（186 tests）
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

## 架构细节

### 订单执行

1. **FOK 市价单**：全部成交或全部失败，带重试（最多 10 次，100ms 间隔）
2. **GTD 限价回退**：FOK 全部失败后以 midpoint 挂限价单，自动过期无需心跳

### 余额管理

- 买入 FOK 成功后立即查询 API 获取精确持仓（`get_token_balance`）
- 卖出前再次查询余额确保不超额（安全截断：floor 到 4 位小数 - 1 tick）
- 卖出失败自动重试，每次重试重新查询余额
- 极小残留（< 0.05 shares）自动跳过清理

### 买入延迟优化

- WS 连接后立即预缓存 tick_size / neg_risk / fee_rate（省 ~700ms）
- 传 `PartialCreateOrderOptions` 跳过 SDK 内部 API 调用
- 优化后买入延迟：~0.6s（原 ~1.9s）

### WebSocket 实时定价

- 连接 `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- 事件：`best_bid_ask`、`price_change`、`last_trade_price`
- 窗口切换通过 `switch_tokens()` 复用连接，无需重连
- 断线自动重连（指数退避 1s → 30s）

### 市场发现

Slug 格式：`{slug_prefix}-{UnixEpoch}`，epoch 即窗口开始时间戳。通过 Gamma API 精确查询单条记录。

## 日志

双输出 — 控制台（人类可读）+ `log/` 目录（JSONL 结构化日志 + 人可读 .log 文件）。

```
20:15:00.962 INFO — === BTC 5m Up/Down Trader Started ===
20:15:00.962 INFO — Strategy: MomentumStrategy | Side: UP | Amount: $1.0 | TP: $0.80 | SL: -50%
20:15:03.842 INFO — [TRADE] BUY_FILLED side=DOWN price=0.4750 shares=2.1053
20:15:30.685 WARNING — [SIGNAL] STOP_LOSS price=0.2300 threshold=0.2375
```

## 测试

```bash
python3.11 -m pytest tests/ -v
# 186 tests
```

## 风险提示

本工具仅供学习和研究用途。加密货币预测市场交易存在高风险，可能导致资金损失。使用前请充分了解 Polymarket 的交易规则和费用结构。
