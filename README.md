# BTC 5-Min Polymarket Trading Bot

自动化 Polymarket BTC 5 分钟涨跌市场交易机器人，基于 Python 3.11 异步架构，通过 WebSocket 获取实时价格，使用 FAK 市价单快速成交。

## 策略说明

机器人持续监控 Polymarket 上的 `btc-updown-5m` 系列 5 分钟涨跌预测市场。每个市场窗口持续 5 分钟，预测窗口结束时 BTC 价格相对于窗口开始时的涨跌方向。

### 核心逻辑

```
窗口开始 → 获取实时价格 → 价格在买入区间？→ 买入 → 监控止损/止盈
                                  ↓ 否                      ↓
                            等待价格进入区间          价格触及止损/止盈？
                                                        ↓ 是
                                                   卖出 → 允许重入？
                                                        ↓ 是
                                                   等待价格回到区间 → 重新买入
                                                        ↓ 否
                                                   等待当前窗口结束 → 下一个窗口
```

### 止损 / 止盈 / 重入

- **止损**：持仓后价格跌破止损阈值，立即卖出。根据 `max-reentry` 参数决定是否允许在同一窗口内重新买入。
- **止盈**：持仓后价格突破止盈阈值，立即卖出。根据 `max-tp-reentry` 参数决定是否允许重入。
- **重入限额**：每个窗口独立计数。达到限额后该窗口永久阻断买入，等待下一个窗口。
- 止损/止盈判断优先使用 `last_trade_price`（真实成交价），比 midpoint（买卖价均值）更及时。

### 窗口切换

- 窗口结束前 5 秒（`WINDOW_END_BUFFER`）提前结束监控，避免边界问题。
- 窗口结束后自动链式切换到下一个窗口，无需重新搜索市场。
- 如果当前窗口已开始超过 5 秒，自动跳过并等待下一个窗口。

## 安装

```bash
# 需要 Python 3.11+
pip install -r requirements.txt

# 配置 Polymarket CLI（首次使用）
polymarket setup
```

## 使用方法

### 交互模式（引导式配置）

```bash
python3.11 btc5m_trade.py --dry
```

程序会依次提示输入所有交易参数。

### 命令行模式（直接指定参数）

```bash
# 模拟运行（不实际下单）
python3.11 btc5m_trade.py \
  --side up \
  --amount 1 \
  --buy-low 0.45 \
  --buy-high 0.55 \
  --stop-loss 0.35 \
  --take-profit 0.80 \
  --max-reentry 1 \
  --max-tp-reentry 0 \
  --dry

# 实盘交易（去掉 --dry）
python3.11 btc5m_trade.py \
  --side up \
  --amount 5 \
  --buy-low 0.45 \
  --buy-high 0.55 \
  --stop-loss 0.30 \
  --take-profit 0.80 \
  --max-reentry 0 \
  --max-tp-reentry 0
```

### 参数说明

| 参数 | 说明 | 示例 |
|---|---|---|
| `--side` | 交易方向：`up`（看涨）或 `down`（看跌） | `up` |
| `--amount` | 每笔交易金额（美元） | `1` |
| `--buy-low` | 买入区间下限（0-1） | `0.45`（45¢） |
| `--buy-high` | 买入区间上限（0-1） | `0.55`（55¢） |
| `--stop-loss` | 止损阈值，低于此价卖出 | `0.35`（35¢） |
| `--take-profit` | 止盈阈值，高于此价卖出 | `0.80`（80¢） |
| `--max-reentry` | 止损后最大重入次数 | `1` |
| `--max-tp-reentry` | 止盈后最大重入次数 | `0` |
| `--dry` | 模拟模式，只记录不实际下单 | — |

### 网络代理

如果无法直接访问 Polymarket API，需要设置代理：

```bash
export http_proxy=http://127.0.0.1:7897
export https_proxy=http://127.0.0.1:7897
python3.11 btc5m_trade.py --dry
```

## 项目结构

```
btc5m/
├── config.py      # 所有配置常量（阈值、重试次数、API 地址等）
├── auth.py        # 从 Polymarket CLI 配置加载凭证，构建 ClobClient
├── client.py      # ClobClient 单例，提供价格查询和 tick size 缓存
├── market.py      # 市场发现，通过 slug 精确查询 Gamma API
├── stream.py      # WebSocket 实时价格流（支持断线重连）
├── trading.py     # 交易操作：FAK 市价单 + GTD 限价单回退
├── monitor.py     # 异步监控循环，事件驱动的买入/止损/止盈
└── notify.py      # macOS 通知

btc5m_trade.py     # 异步入口（asyncio.run）
requirements.txt   # Python 依赖
```

## 架构细节

### 订单机制

1. **FAK（Fill-And-Kill）市价单**：允许部分成交，未成交部分自动重试。适合快速进出市场。
2. **GTD（Good-Til-Date）限价单回退**：如果 FAK 重试全部失败，以当前 midpoint 价格挂限价单，自动在窗口结束时过期。无需维护 heartbeat。

### 市场发现

slug 格式为 `btc-updown-5m-{UnixEpoch}`，其中 epoch 即为窗口开始时间戳。通过计算当前时间对应的 5 分钟边界，直接向 Gamma API 查询单条市场记录，无需批量拉取。

### WebSocket 价格流

- 连接 `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- 处理 `best_bid_ask`、`price_change`、`last_trade_price` 三类事件
- 断线自动重连，指数退避（1s → 30s，最多 10 次）
- 每 10 秒发送 `{}` 保活

### 匹配引擎重启

HTTP 425 错误表示 Polymarket 匹配引擎正在重启（每周二 7:00 AM ET）。遇到时自动指数退避重试（2s → 30s，最多 3 次）。

## 日志

日志输出到控制台和 `log/btc5m_trade.log`（轮转 10MB，保留 5 份备份）。

```
21:40:21.756 WARNING — STOP-LOSS triggered at UP=0.34 (<35¢) [1/2] [source=last_trade_price] reentry=True
21:40:52.632 INFO    — Price 0.455 moved into buy range — buying now!
21:45:58.851 WARNING — TAKE-PROFIT triggered at UP=0.805 (>80¢) [source=best_bid_ask] reentry=False
```

## 依赖

- **Python 3.11+**
- `py-clob-client >= 0.34.6` — Polymarket CLOB SDK
- `websockets >= 12.0` — WebSocket 客户端
- `python-dotenv` — 环境变量
- `requests` — Gamma API 市场查询
- `eth-account` — 钱包签名

## 风险提示

本工具仅供学习和研究用途。加密货币预测市场交易存在高风险，可能导致资金损失。使用前请充分了解 Polymarket 的交易规则和费用结构。
