# 数据源与口径说明（usage-dashboard v3）

报表并列两个指标：**WorkBuddy 积分** 与 **Token 消耗**。二者来源不同，切勿混淆。

---

## 指标一：WorkBuddy 积分（套餐积分体系）

### 数据源
`~/.workbuddy/workbuddy.db`（SQLite，WAL 模式，只读打开）

### 关键表 `session_usage`（每会话一条）
- `session_id` TEXT
- `used` INTEGER —— **不是积分，也不是累计 token**；是上下文/token 维度（≤ `size` 上下文窗口），v2 已弃用该列
- `size` INTEGER —— 上下文窗口大小（如 300000 / 192000）
- `updated_at` INTEGER —— 毫秒级 UTC 时间戳（会话用量最后更新时刻）
- `credit_json` TEXT —— JSON：键为 32 位哈希、值为该项积分。**各项之和 = 该会话消耗的 WorkBuddy 积分**（本指标主口径）

### 关联表 `sessions`
- `id`（= session_usage.session_id）、`model`（会话主模型）、`title`、`created_at`

### 口径
- 会话积分 = `sum(credit_json.values())`；模型 = `sessions.model`（会话级）。
- **每小时归桶（v3 修正）**：`session_usage.updated_at` 只是会话用量"最后同步时刻"，会把跨多天的会话全天量压到一个小时。故改用该会话在 `traces/` 中的各 trace 活跃时间，按 trace `totalTokens` 占比把会话积分**摊分到对应小时**；无 trace 的会话回退到 `updated_at`。模型仍取会话主模型。
- **时效守卫（v4 修正，2026-10-14）**：`credit_json` 是会话累计快照。若该会话全部 trace 都远早于 `updated_at`（相差 >24h，典型：长会话几天后再次打开、积分才同步上来），按 trace 摊分会把新积分错误摊回历史时段（实测：10-10 会话累计 583.91 被摊回 10-10，导致 10-14 当天只剩 34.04）。此时放弃摊分、回退到 `updated_at` 归桶。注意该回退会**高估**同步当天、**低估**真实消耗日——`credit_json` 无时间维度，跨天会话的积分本地无法精确拆天。
- **积分同步有延迟**：活跃会话的 `credit_json` 可能长时间为空/为 0（实测当前会话 0.00），"今天"的积分在会话结束后才逐步补齐，当天数字偏小是常态。
- **traces 断供（2026-10-11 起）**：`traces/` 最新文件停在 10-11 15:46，之后无新 trace 写入（疑似 WorkBuddy 更新后改了存储位置或停止写本地 trace）。影响：①Token 指标此后无数据；②新会话积分全部走 `updated_at` 回退路径。待排查新位置。
- 单位：积分，两位小数。
- 校验：全量求和 ≈ 截图"平台奖励积分 已使用"数值（截至 2026-10-09 约 5,331.78）。

### 已知限制
- `credit_json` 的键是哈希，本地无映射可还原为模型名 → 不支持会话内部多模型细分。
- 摊分依赖 `traces/`（保留期约 1 个月）：有 trace 的会话按 token 占比分配，无 trace 的会话退回 `updated_at`（归属可能偏差）。约 88% 会话有 trace 覆盖，已足够纠正时间压缩。

---

## 指标二：Token 消耗（运行 trace）

### 数据源
`~/.workbuddy/traces/<pid>/trace_*`（每个 agent 工作流一轮一个文件）。WorkBuddy 升级后**格式变了**，脚本两种都支持：
- **旧版** `trace_<id>.json`：单个 JSON 对象 `{trace, spans}`，模型/token 在 `trace.modelInfo`。约 2026-10-11 15:46 之后不再产生。
- **新版** `trace_<id>.jsonl`：JSON Lines 事件流（每行一个事件），约 2026-10-11 之后开始产生。数据仍在、只是换了扩展名+结构，**不是停写**。

### 关键字段
- **旧版**：`trace.startedAt`(ISO UTC,Z) / `sessionId` / `totalTokens` / `modelInfo.{models, totalInputTokens, totalOutputTokens, totalCachedTokens}`。
- **新版**：`trace_start` / `trace_update` 事件的 `trace` 对象含 `startedAt` / `sessionId`；模型与 token 在 **`generation` 类型 span 的 `toolOutput`**（原始 API 响应字符串）里：`usage.{prompt_tokens, completion_tokens, total_tokens, prompt_tokens_details.cached_tokens, completion_tokens_details.cached_tokens}`，`model` 也在其中。

### 口径
- token = 输入+输出；缓存 token 单列展示。
- 旧版：模型取 `modelInfo.models`（多模型按数量均分）。新版：逐条 `generation` span 取其 `model` + `usage.total_tokens` 聚合。
- 每小时：按 trace `startedAt`（新版按各 `generation` span 的 `startedAt`）转本地时区(+8) 归桶。
- 单位：整数 token。

### 展示粒度
- 筛选结果只涉及**一天**时（"今天"，或自定义起止同一天）按**小时**聚合；跨多天时按**天**聚合（避免出现几百根小时柱）。两个指标同一切换逻辑。

### 已知限制
- traces 有保留期（约 1 个月），更早的数据可能已被清理。
- 无模型信息的 trace（约一半，纯工具调用）不计入。

---

## 模型名归一化
去掉 `custom-local:` 前缀，使两指标模型名对齐（如 `custom-local:gpt-5.6-sol` → `gpt-5.6-sol`）。

## 两个指标为何差异大（正常）
各模型积分倍率不同（本地配置 `models.json` / `local_storage` 可见 `credits` 倍率），且有夜间折扣、限时免费等促销。例如 hy4-preview-f token 最多但积分少；glm-5.3-flash token 少但积分多。故需并列展示。
