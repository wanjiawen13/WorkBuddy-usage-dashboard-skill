# 数据源与口径说明（usage-dashboard v4.3，2026-11-11 复核）

报表并列两个指标：**WorkBuddy 积分** 与 **Token 消耗**。二者来源不同，切勿混淆。
**核心原则：总额精确守恒；时间与模型拆分按证据分级标注；每笔积分都要给出模型名，不轻易用「未指定」搪塞。**

---

## 指标一：WorkBuddy 积分（套餐积分体系）

### 数据源
`~/.workbuddy/workbuddy.db`（SQLite，WAL 模式，只读打开）

### 关键表 `session_usage`（每会话一条）
- `session_id` TEXT
- `used` INTEGER —— **不是积分，也不是累计 token**；是上下文/token 维度（≤ `size` 上下文窗口），已弃用
- `size` INTEGER —— 上下文窗口大小（如 300000 / 192000）
- `updated_at` INTEGER —— 毫秒级 UTC 时间戳（会话用量最后更新时刻）
- `credit_json` TEXT —— JSON：**键为 UUIDv7**（前 12 hex = 消费毫秒时间戳）或旧格式随机 32-hex；值为该项积分。**各项之和 = 该会话消耗的 WorkBuddy 积分**（本指标主口径）

### 关联表 `sessions`
- `id`（= session_usage.session_id）、`title`、`created_at`、`model`
- `sessions.model` 绝大多数是**真实模型名**（hy3 61 个 / deepseek-v4.1-flash 48 个 / custom-local:deepseek-v4-flash 20 个 / hy4free 19 个 / glm-5.3-flash 19 个 …），只有 7 个是档位别名（fast-model 4 / balanced-model 2 / deep-model 1）。**可以作为 L3 兜底归因**，旧版一刀切禁用是过度修正。

### 口径
- 会话积分 = `sum(credit_json.values())`；全量求和严格守恒，逐条 `round(...,4)` 后再汇总，误差可忽略。
- **时间归属（`est` 字段）**：
  - `exact`：键是 UUIDv7，解码前 12 hex 得毫秒时间戳，归入真实消费时刻。实测 671/814 条目可解码，**解码时间 100% 落在 `[created_at, updated_at]` 内，0 越界**。
  - `approx`：旧格式随机键、但该会话有近 24h 内的 trace，按 token 占比摊分到各小时。
  - `unknown`：旧格式随机键且无可用 trace，整段历史积分只能归到 `updated_at`。**这不代表当天真实消耗，只保证总额守恒**，报表必须显式提示。
  - 实测（2026-11-11）：旧格式 143 条目 / **3564.83 积分（43.3%）** 集中在 16 个会话，基本都是 `unknown`。
- **模型归因（`src` 字段，四级按可靠性递减）**：
  | 级别 | src | 规则 | 实测积分 |
  |---|---|---|---|
  | L1 | `time-window` | **全局** generation 里 ±60s 窗口内时间最近的计费模型，同 `sessionId` 候选优先 | 2877.56 |
  | L2 | `trace-dominant` | 同会话 trace 里出现最多的计费模型 / 旧格式按 token 占比摊分 | 1748.33 |
  | L3 | `session-model` | `sessions.model` 归一化后的真实模型名 | 3531.70 |
  | L3' | `tier-alias` | 会话只记了档位名（fast-model 等），仍显示原名 | 79.11 |
  | L4 | `unattributed` | 连会话主模型都没有 →「未指定」 | **0** |
  - **L1 为什么必须用全局时间窗口**：本机 11445 个 generation 里 **10169 个（89%）没有 `sessionId`**；1512 个 jsonl 中只有约 31 个 `trace_start` 事件带 `sessionId`，唯一 sid 仅 23 个、与 `session_usage` 交集 22 个。只按「同会话」匹配必然大面积落到「未指定」。而积分条目的 UUIDv7 时间与 generation `startedAt` 通常只差 **1~6 秒**（671 个 valid 条目中 544 个在 60s 内有 generation，其中 540 个在 10s 内），是最可靠的关联依据。
  - 每条记录同时带 `est` 与 `src`，明细表显示「时间可信度」，汇总卡片显示各类占比。
- **免费模型**：`FREE_MODELS = {deepseek-v4-flash}`（**唯一一项，用户确认的自配不计费模型**，无 `.1`）。
  - `deepseek-v4.1-flash`（带 `.1`）是 WorkBuddy 内置计费模型，切勿混淆。
  - **`hy4free` / `wb-lite` / `hy3` 等是 WorkBuddy 内置模型**，名字里的 free/lite 只是档位命名，**仍消耗积分**（实测 `wb-lite` 156.96 积分、`hy3` 639.49 积分）。旧版按名称启发式把它们排除，是造成大量「未指定」的另一个根因，已修正。**不要按模型名猜免费**。
- **别名合并**：`MODEL_ALIASES = {deepseek-v4-1-flash → deepseek-v4.1-flash}`。本机 `models.json` 用连字符 id，API 响应返回点号名，二者是同一模型。
- **档位名**：`TIER_ALIASES = {fast-model, balanced-model, deep-model, auto, default}`。不参与 L1/L2 匹配；仅在无 trace 的老会话里作为 L3' 兜底名字显示（实测 3 条 / 79.11 积分）。Token 指标如实显示 trace 返回的原始模型名。
- **积分同步有延迟**：活跃会话 `credit_json` 可能长时间为空/为 0，"今天"的积分偏小是常态。
- **traces 格式变更（2026-10-11 起）**：从 `trace_*.json` 改为 `trace_*.jsonl` 事件流，模型/token 移到 generation span 的 `toolOutput`；脚本双格式兼容。
- 单位：积分，两位小数。
- 校验：2026-11-11 全量求和 **8,236.70**（旧文档写的 5,331.78 是 2026-10-09 快照，已过期）。

### 已知限制
- 本机 traces 最早约 10-13，**9 月会话的 trace 已清理**，这部分积分（约 43%）只能靠 L3 会话主模型兜底，属推断而非实测，报表用 `src=session-model` 标明。
- `unknown` 记录会高估同步当天：例如 9-18 显示 2363.18 积分，当天 token 仅 306,359；9-16 有 807.21 积分但当天无 token。看单日趋势时必须扣除这部分。
- L1 在 ±60s 窗口内有多个模型时，同会话优先、否则取时间最近者；并发会话极密集时仍可能误配几秒级的相邻调用。
- 101 个 `sessions` 行没有对应 `session_usage` 记录（可能免费调用或未同步），属数据源边界。

---

## 指标二：Token 消耗（运行 trace）

### 数据源
`~/.workbuddy/traces/<pid>/trace_*`（每个 agent 工作流一轮一个文件）。两种格式都支持：
- **旧版** `trace_<id>.json`：单个 JSON 对象 `{trace, spans}`，模型/token 在 `trace.modelInfo`。本机仅剩 5 个文件（2026-09-18 ~ 10-07）。
- **新版** `trace_<id>.jsonl`：JSON Lines 事件流，模型/token 在 generation span 的 `toolOutput`；本机约 1512 个文件。

### 关键字段
- **旧版**：`trace.startedAt`(ISO UTC,Z) / `sessionId` / `totalTokens` / `modelInfo.{models, totalInputTokens, totalOutputTokens, totalCachedTokens}`。
- **新版**：`trace_start` / `trace_update` 的 `trace` 对象含 `startedAt` / `sessionId`（**可选，多数缺失**）；模型与 token 在 `span_end` 且 `type=generation` 的 `toolOutput`（原始 API 响应字符串）里：`usage.{prompt_tokens, completion_tokens, total_tokens, prompt_tokens_details.cached_tokens}`、`model`。

### 口径
- token = `usage.total_tokens`。实测 11366 条**全部满足** `total_tokens == prompt_tokens + completion_tokens`，所以「输入+输出」的说法等价成立。
- **缓存 token 是 `prompt_tokens` 的子集**（实测 11368 条 0 越界），单列展示为「其中缓存」，**不与输入相加、不重复计入合计**。
- **无重复计数**：11751 个 generation span id 全唯一；`toolOutput` 为单元素 list，1 span = 1 item；`.json` 与 `.jsonl` 无同名文件；只取 `span_end` 不取 `span_start`，不会双算。
- `scan_traces` 额外返回**全局模型时间索引** `[(epoch, model, sessionId)]`（按 epoch 排序 + 二分查找），供积分 L1 归因使用；这一索引不依赖 `sessionId`。
- 旧版：模型取 `modelInfo.models`（多模型按数量均分）。新版：逐条 `generation` span 取其 `model` + `total_tokens` 聚合。
- 每小时：按各 `generation` span 的 `startedAt`（旧版按 trace `startedAt`）转本地时区(+8) 归桶。
- 单位：整数 token。实测全量约 30.9 亿（会随当前会话持续增长，数值为生成时刻快照）。

### 展示粒度
- 筛选结果只涉及**一天**时（"今天"，或自定义起止同一天）按**小时**聚合；跨多天时按**天**聚合（避免几百根小时柱）。两个指标同一切换逻辑。

### 已知限制
- traces 有保留期（约 1 个月），更早的数据已被清理；Token 指标只覆盖留存期，不能与积分做全周期对账。
- 无模型信息、无 `toolOutput` 的纯工具调用 span 不计入。

---

## 模型名归一化

| 处理 | 规则 | 影响指标 |
|---|---|---|
| 去前缀 | `custom-local:xxx` → `xxx` | 两者 |
| 别名合并 | `deepseek-v4-1-flash` → `deepseek-v4.1-flash` | 两者 |
| 自配免费模型排除 | `FREE_MODELS`（仅 `deepseek-v4-flash`） | 仅积分 L1/L2 归因 |
| 档位名识别 | `TIER_ALIASES` 不参与 L1/L2，仅作 L3' 兜底 | 仅积分归因 |
| 空值 | → `未指定` | 两者 |

本机存在**三套命名**并存，排查时别混：`models.json` 用连字符（`deepseek-v4-1-flash`），API 响应用点号（`deepseek-v4.1-flash`），`_srv_models.json` 用无 `.1` 的 `deepseek-v4-flash` 且带 USD pricing。

## 两个指标为何差异大（正常）
各模型积分倍率不同，且可能有夜间折扣、限时免费等促销，token 量与积分不成正比。
实测 token 最多的是 `deepseek-v4.1-flash`（约 16.4 亿），`hy4-preview-f` 仅约 113 万 —— 旧文档"hy4-preview-f token 最多"的说法是错的。
另外积分可精确到条目级，Token 只覆盖 trace 留存期，两者时间跨度本就不同，仅供并列参考。
