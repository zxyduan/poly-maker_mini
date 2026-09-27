# poly-maker_mini — 统一 SDK 迁移说明（实盘排障手册）

> 依据官方「从旧版 SDK 迁移到统一 SDK - Polymarket Documentation」完成
> （附在 `docs/polymarket-docs/`，含 Data API v1→v2 文档）。
> 迁移 commit：`3f06deb`；日志增强 commit：见 `git log`。

## 1. 依赖变更

| 移除 | 加入 |
| --- | --- |
| `py-clob-client-v2==1.0.2` | `polymarket-client>=0.11.0`（import 名 `polymarket`） |
| `py-builder-relayer-client>=0.0.2` | （其功能由统一 SDK 的 `merge_positions` 承接） |
| `py-builder-signing-sdk`（传递依赖） | |

`uv sync` 后确认：`uv pip list | grep polymarket` → `polymarket-client 0.11.0`。
mypy 的 `ignore_missing_imports` 覆盖已同步调整（SDK 自带类型标注，无需覆盖）。

## 2. 代码改动点

### src/polymaker/execution/gateway.py（核心）
- `connect()`：`ClobClient + create_or_derive_api_key` → `AsyncSecureClient.create(private_key, wallet=…)`。
  SDK 自动完成：凭据自举/派生、钱包类型链上分类（EOA / Gnosis Safe / DepositWallet）、签名配置。
  `wallet` 传 `BROWSER_ADDRESS`（资金/持仓地址），`signer` 由 SDK 给出。
  `signature_type` 不再传给 SDK（自动识别）；config 字段保留（merge/doctor 仍在用）。
- `place()`：`create_limit_order(token_id, price, size, side, post_only)` + `post_orders([...])`。
  `post_only` 从「批量提交参数」变为「每单创建参数」——maker-only 语义由 SDK 在单内强制。
  tick size / neg-risk / 费用 / 签名全部由 SDK 逐单解析，不再传 `PartialCreateOrderOptions`。
- `cancel()` → `cancel_orders(order_ids=[...])`；`cancel_asset()` → `cancel_market_orders(asset_id=…)`；
  `cancel_all()` 不变。全部原生 async，不再走线程池。
- `market_order()` → `place_market_order(...)`：**BUY 传 `amount`（USD），SELL 传 `shares`**（语义不同，已分支处理）。
  返回 SDK 的 `AcceptedOrder / RejectedOrder` 模型（snake_case：`making_amount/taking_amount/status`）。
- `open_orders()` → `list_open_orders().iter_items()` 分页器；`OpenOrder` 模型字段直接映射（id/asset_id/side/price/original_size/size_matched）。
- `positions()` → `list_positions(user=funder)`：**Data API v1 → v2**（v1 于 2026-10-24 停用），
  字段 `asset_id / current_size / avg_price`。
- `balance_allowance()` → `get_balance_allowance(asset_type="COLLATERAL")`，返回 `BalanceAllowance` 模型（`balance` 为 6 位小数的原始整数）。
- 新增 `aclose()`：engine 停机时关闭 SDK 客户端（engine.py 同步改为 `await gateway.aclose()`）。
- 删除 `_tick_str`（SDK 自行解析 tick）。

### src/polymaker/l2auth.py（新增）
统一 SDK **不覆盖**交易所 `/heartbeats` 死契（dead-man switch）端点。
`hmac_signature()` 复刻 SDK 内部 `build_hmac_signature`（base64url(secret) + HMAC-SHA256 over
`ts+method+path+body`）；`l2_headers()` 从 `client.credentials + client.signer` 组装 `POLY_*` 头。
`tests/test_l2auth.py` 用 SDK 自身函数逐字节交叉校验，防止漂移。

### src/polymaker/merge.py
- DepositWallet（signature_type 1/3）合并：`RelayClient + BuilderConfig` → 
  `AsyncSecureClient.create(api_key=BuilderApiKey(key/secret/passphrase))` + `merge_positions(condition_id, amount)`。
  gasless（relayer 付费），`handle.wait()` 在回滚/超时抛异常（`TransactionFailedError` / `TimeoutError`）。
- EOA（0）/ Gnosis Safe（2）仍走原 web3 直接提交路径（未使用任何旧 SDK，未改动）。
- `neg_risk` 参数保留但 deposit 路径不再自行构建调用（SDK 内部解析市场上下文）。

### 凭据与响应适配
- `userstream/client.py`、`doctor.py`、`moneydoctor.py`：`creds.api_key/api_secret/api_passphrase` →
  `creds.key/secret/passphrase`（SDK `ApiKeyCreds` 字段名）。
- `moneydoctor._fill`、`doctor._extract_balance`：兼容 SDK 模型（snake_case）与旧 dict 形态。
- `userstream.run()` 新增凭据缺失快速失败日志（`user_ws_no_creds`），避免无限重连盲跑。

### 其他
- `probe_heartbeat.py`：改用 `AsyncSecureClient.create` + `l2auth`（main 改为 async）。
- `pyproject.toml`：依赖与 mypy overrides；`README.md`：SDK 描述同步。
- `tests/sim_heartbeat_patch.py`：假客户端改为适配 `l2_headers`；`tests/test_execution.py`：删除 `_tick_str` 测试。

## 3. 实盘排障日志索引

日志均为 structlog 键值对，`grep '<event>' journal/*.log`（或控制台）即可定位。

| 日志事件 | 节点 | 含义 / 排查建议 |
| --- | --- | --- |
| `client_bootstrap_failed` | connect | SDK 建连失败（凭据派生 / 钱包分类 / RPC）。检查 PK、网络、geo-block（代理）、`chain_id` |
| `gateway_connected` | connect | 成功。看 `wallet_type`（EOA/DEPOSIT_WALLET/SAFE）、`creds_ready`、`sdk_version`——三个最该先核对的值 |
| `rate_limit_warning` | connect 回调 | **订单/撤单响应带出的限流预警**（`warning=True` 或 remaining<20）。出现即需降频/停单，否则将被打回 |
| `clock_ok` / `clock_drift` | connect | 本地时钟 vs 交易所。drift>5s 会影响 L2 签名，先 `ntpdate` 再跑 |
| `place_failed` | place | 整批下单异常。带 `token_ids/post_only`，查 SDK 解析 tick/neg-risk 失败、网络、代理 |
| `place_resp` | place | 每批下单的原始响应摘要（含 post_only）。核对 sides/prices/sizes 是否与策略一致 |
| `order_rejected` | place | **逐单被拒**，带 `oid/message/code`。常见：价格越 tick、post-only 撞单、大小低于最小 |
| `cancel_sent` / `cancel_asset_sent` | cancel | 撤单已提交（带 id 摘要）。确认日志出现再相信撤单成功 |
| `cancel_failed` / `cancel_asset_failed` | cancel | 撤单失败——**不要从本地状态删除这些单**，等下次 reconcile/重试 |
| `open_orders_read` / `positions_read` | 周期对账 | debug 级。数量异常（0 或暴增）说明对账/状态有偏差 |
| `heartbeat_failed` / `heartbeat_recovered` | 心跳 | 死契失败计数；连续失败会触发引擎停报并等恢复 |
| `merge_client_bootstrap_failed` | merge | builder 凭据无效/网络问题（gasless 路径建连失败） |
| `merge_positions_submitting` | merge | 合并已发起（condition/amount/sig_type） |
| `merge_sent_deposit_wallet` | merge | 合并成功（tx 哈希）。**`wait()` 失败会抛异常** → `merge_failed` |
| `merge_failed` | merge | 合并失败（含回滚/超时），err 里带原因 |
| `user_ws_no_creds` | userstream | 网关未连接就启动用户流——按启动顺序修正 |
| `user_ws_subscribed` / `user_ws_dropped` / `user_ws_error` | userstream | 用户流连/断/错。断连后引擎会强制 REST 对账（`on_reconnect`） |

## 4. 上实盘前的检查清单

1. `uv sync` 成功；`uv pip list | grep polymarket` 显示 0.11.x
2. `python probe_heartbeat.py --config-dir livecfg` → VERDICT: acknowledged on `/heartbeats`
3. paper 模式完整跑一轮（下单/撤单/对账日志齐全）
4. `gateway_connected` 里 `wallet_type` 与 config 的 `signature_type` 对应（0→EOA、2→SAFE、1/3→DEPOSIT_WALLET）
5. deposit 钱包合并前确认 `.env` 有 builder 三件套（`POLY_BUILDER_KEY/SECRET/PASSPHRASE`）
6. 观察 `rate_limit_warning` 是否出现；出现则调低 `rate_budget_fraction`

## 5. 已知遗留（迁移前已存在，未改动）

- `tests/test_scanner.py` 导入不存在的 `ScanSettings`，收集即失败（HEAD 亦如此）
- mypy：`cli.py`（`Config.scan` 等 20 处）与 `engine.py:450`（延迟导入缺失的 `polymaker.strategy.wool`）共 21 个历史错误
- ruff：`query_rewards.py` / `cli.py` / `one_way.py` 共 26 个历史告警
- 实盘网络路径（真实连 CLOB、下单/合并）本环境无钱包凭据，未做 live 验证

## 6. 验证方式

- `uv run pytest -q --ignore=tests/test_scanner.py` → 112 passed, 2 skipped（跳过为网络 live 测试）
- `uv run python tests/sim_heartbeat_patch.py` → 4/4 通过
- `uv run ruff check`（迁移触及文件）→ 0 错误
- `uv run mypy src/polymaker`（迁移触及文件）→ 0 错误
