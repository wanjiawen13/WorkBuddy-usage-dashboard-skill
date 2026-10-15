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
- 会话积分 = `sum(credit_json.values())`。
- **时间归属（v4 修正，2026-10-15 核心）**：`credit_json` 的键是 **UUIDv7**（128-bit，前 12 hex = 该条目消费时刻的毫秒时间戳）。逐条解码后把每笔积分归入**真实消费时间**（精确到条目级），无需再依赖 trace 估算。旧格式随机ID（解码溢出/越界，多见于 2025-09-24 之前的会话）与无可信时间的条目，才回退到 trace token 加权（带 24h 时效守卫）或 `updated_at`。验证：各会话键解码出的时间区间与 `updated_at`、真实使用日完全吻合（如 10-14 17:16 会话 607 积分，解码落点 10-10/10-13/10-14，与同步时刻一致）。
- **模型归因（v3.3 修正）**：`sessions.model` 只记"会话选的主模型"，实际请求可能被路由到其他模型（实测 10-14 会话选 gpt-5.6-terra，实际全为 deepseek-v4.1-flash / glm-5.3-flash）。故按每笔积分的 UUIDv7 时间匹配**同会话最近的 trace 生成轮次（实际模型）**；无匹配时回退该会话 trace 的多数模型，再回退会话主模型。
- **积分同步有延迟**：活跃会话的 `credit_json` 可能长时间为空/为 0（实测当前会话 0.00），"今天"的积分在会话结束后才逐步补齐，当天数字偏小是常态。
- **traces 格式变更（2026-10-11 15:46 起，v3.2 修正）**：并非断供。WorkBuddy 升级后 trace 从 `trace_*.json`（单对象）改为 `trace_*.jsonl`（事件流），模型/token 移到 generation span 的 `toolOutput`；脚本已双格式兼容。
- 单位：积分，两位小数。
- 校验：全量求和 ≈ 截图"平台奖励积分 已使用"数值（截至 2026-10-09 约 5,331.78）。

### 已知限制
- 旧格式会话（约 2025-09-24 前创建且后期未更新的长会话）的积分键非 UUIDv7，时间/模型只能回退近似，可能有误差。
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
