---
name: usage-dashboard
description: This skill should be used when the user wants to view, analyze, or visualize their WorkBuddy usage in finer detail than the built-in display — specifically WorkBuddy 积分 (points) consumption AND token consumption, shown side by side, with hourly bar charts and per-model breakdowns. Triggers on phrases like "查看我的积分用量", "每小时用了多少积分", "哪个模型最耗积分", "token 消耗", or "生成用量报表".
agent_created: true
---

# WorkBuddy 用量报表（积分 + Token）

## Overview
读取 WorkBuddy 本地数据，按"小时 × 模型"聚合**两个并列指标**并生成一个自包含 HTML 报表：
1. **WorkBuddy 积分** —— 套餐积分体系的消耗（来自 `workbuddy.db` 的 `session_usage.credit_json`）。
2. **Token 消耗** —— 运行 trace 的输入/输出/缓存 token（来自 `traces/*/trace_*.json` 与升级后的 `trace_*.jsonl` 事件流，脚本均兼容）。

报表含 KPI 卡片、堆叠柱状图、模型排行、可排序明细表与时间筛选；积分明细额外显示「时间可信度」。

## When to Use
- 用户想看 WorkBuddy 积分（points）消耗明细：每小时 / 每个模型。
- 用户想看 token 消耗，或想把积分与 token 并列对比。
- 用户觉得内置用量展示太粗。
- 触发语：积分用量、每小时积分、哪个模型最耗积分、token 消耗、用量报表。

## Key Distinction（务必分清）
- **积分 ≠ token**。积分是 WorkBuddy 套餐积分体系（截图"套餐与积分"页里的数值），token 是模型调用量。两者来源、口径、数量级都不同，报表并列展示但不要混为一谈。
- 积分数值是**小数**（如 8,236.70）；token 是**大整数**（如 3,092,911,398）。
- **总额精确，拆分按证据分级**：积分总额 = `credit_json` 各项之和，严格守恒；"哪一天 / 哪个模型"依赖 UUIDv7 时间戳和本地 trace。每条记录带 `est`(时间可信度) 与 `src`(归因来源)，回复用户时不要把兜底推断说成实测值。
- **每笔积分都有模型名**：本机实测「未指定」为 0 条。所有模型（含 WorkBuddy 内置与用户自配）都有名字，兜底也要给出名字并标注来源。

## Workflow
1. **生成报表**：运行脚本。
   ```bash
   <python> <skill>/scripts/gen_report.py
   ```
   默认读 `~/.workbuddy/workbuddy.db`(积分) 与 `~/.workbuddy/traces`(token)，输出 `usage-report.html` 到**当前工作区**。
   可传参：`--db <path> --traces <dir> --out <file>`。
   脚本零依赖（仅 Python 标准库），用任意 Python 3 即可；在 WorkBuddy 内置终端可直接 `python gen_report.py`。
2. **展示结果**：用 `present_files` 打开生成的 `usage-report.html`，并附一句概要（积分合计 + token 合计 + 各自 Top 模型）。
3. **口径说明**（回复用户时必带）：
   - **积分**：`session_usage.credit_json` 各项之和。键为 **UUIDv7**，前 12 hex = 每笔消费的毫秒时间戳，逐条解码归入真实消费时刻（`est=exact`）。旧格式随机键没有时间信息，只能按 trace token 加权（`est=approx`）或归到最后同步时刻（`est=unknown`）——**unknown 只保证总额守恒，不代表当天真实消耗**。
   - **模型归因（四级，按可靠性递减）**：
     - L1 `time-window`：全局 generation 里 ±60s 窗口内**时间最近**的计费模型，同会话（`sessionId` 相同）候选优先。这是主路径——本机 trace 约 **89% 的 generation 没有 `sessionId`**，只按会话匹配必然大面积落到「未指定」。
     - L2 `trace-dominant`：同会话 trace 里出现最多的计费模型。
     - L3 `session-model`：`sessions.model` 归一化后的真实模型名。
     - L3' `tier-alias`：会话只记了档位名（`fast-model` 等），无 trace 的老会话仍显示原名而非「未指定」。
     - L4 `unattributed`：连会话主模型都没有才归「未指定」（本机实测 0）。
   - **免费模型**：`FREE_MODELS` 只放**用户确认**的自配不计费模型 `deepseek-v4-flash`（无 `.1`）。**WorkBuddy 内置模型名带 `free`/`lite`（`hy4free`、`wb-lite`、`hy3` 等）只是档位命名，仍会消耗积分，不能按名称推断为免费**。计费的 `deepseek-v4.1-flash`（带 `.1`）切勿混淆。`MODEL_ALIASES` 合并连字符/点号变体，`TIER_ALIASES` 仅用于识别档位名、不参与 L1 匹配。
   - **Token**：trace 的 `total_tokens`（= 输入 + 输出，实测全量相等）；缓存 token 是 `prompt_tokens` 的子集，明细列叫「其中缓存」，**不可与输入相加**。按 generation 开始时刻归桶。
   - **时间粒度**：筛选结果只涉及**一天**时（"今天"，或自定义起止同一天）按**小时**展示；跨多天时按**天**展示（避免几百根小时柱）。
   - **质量指标必报**：报 KPI 时一并说明时间不可考积分、估算积分、时间窗口归因占比、未指定模型积分四项。

## Resources
### scripts/
- `gen_report.py` —— 零依赖（标准库）报表生成器：读 db 取积分、扫描 traces 取 token、建全局模型时间索引、聚合、输出自包含 HTML。图表用 Chart.js CDN；数字视图纯 HTML，离线可看。
  - `FREE_MODELS` / `MODEL_ALIASES` / `TIER_ALIASES` 集中在文件头部，计费口径有变化时改这里。

### references/
- `data-schema.md` —— 两个数据源的表结构/字段、聚合口径、实测覆盖率与已知限制的权威说明。改动口径或排查问题时先读此文件。

## Notes
- 只读分析，绝不修改 `workbuddy.db` 或 traces。
- **不要按模型名猜免费**：`hy4free` / `wb-lite` 是 WorkBuddy 内置模型，实测有积分消耗（旧版误排除它们，导致大量积分被错判为「未指定」）。只有用户明确确认的自配模型才进 `FREE_MODELS`；报表页脚显示当前生效清单。
- traces 保留期有限（约 1 个月）。9 月老会话的 trace 已清理，这部分积分靠 L3 会话主模型兜底（实测约 43%），属推断而非实测，报表用 `src` 标明。
- 活跃会话的 `credit_json` 可能长时间为空或为 0，"今天"的积分偏小是常态（积分同步有延迟）。
