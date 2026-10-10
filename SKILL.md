---
name: usage-dashboard
description: This skill should be used when the user wants to view, analyze, or visualize their WorkBuddy usage in finer detail than the built-in display — specifically WorkBuddy 积分 (points) consumption AND token consumption, shown side by side, with hourly bar charts and per-model breakdowns. Triggers on phrases like "查看我的积分用量", "每小时用了多少积分", "哪个模型最耗积分", "token 消耗", or "生成用量报表".
agent_created: true
---

# WorkBuddy 用量报表（积分 + Token）

## Overview
读取 WorkBuddy 本地数据，按"小时 × 模型"聚合**两个并列指标**并生成一个自包含 HTML 报表：
1. **WorkBuddy 积分** —— 套餐积分体系的消耗（来自 `workbuddy.db` 的 `session_usage.credit_json`）。
2. **Token 消耗** —— 运行 trace 的输入/输出/缓存 token（来自 `traces/*/trace_*.json`）。

报表含 KPI 卡片、堆叠柱状图、模型排行、可排序明细表与时间筛选。

## When to Use
- 用户想看 WorkBuddy 积分（points）消耗明细：每小时 / 每个模型。
- 用户想看 token 消耗，或想把积分与 token 并列对比。
- 用户觉得内置用量展示太粗。
- 触发语：积分用量、每小时积分、哪个模型最耗积分、token 消耗、用量报表。

## Key Distinction（务必分清）
- **积分 ≠ token**。积分是 WorkBuddy 套餐积分体系（截图"套餐与积分"页里的数值），token 是模型调用量。两者来源、口径、数量级都不同，报表并列展示但不要混为一谈。
- 积分数值是**小数**（如 5,331.78）；token 是**大整数**（如 540,250,029）。

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
   - 积分：会话消耗记录 `credit_json` 各项之和；按该会话各 trace 的 token 占比把会话积分**摊分到对应小时**（因为 `session_usage.updated_at` 只是最后同步时刻，会把整天的量压到一个小时）；"每个模型"取会话主模型；无 trace 的会话回退到 `updated_at`。
   - Token：trace 的输入+输出（缓存单列）；每小时按 trace 开始时刻归桶。
   - **时间粒度**：看"今天"时按**小时**展示；"全部 / 近 7 天 / 自定义"按**天**展示（避免几百根小时柱）。

## Resources
### scripts/
- `gen_report.py` —— 零依赖（标准库）报表生成器：读 db 取积分、扫描 traces 取 token、聚合、输出自包含 HTML。图表用 Chart.js CDN；数字视图纯 HTML，离线可看。

### references/
- `data-schema.md` —— 两个数据源的表结构/字段、聚合口径、已知限制的权威说明。改动口径或排查问题时先读此文件。

## Notes
- 只读分析，绝不修改 `workbuddy.db` 或 traces。
- 积分来源 `session_usage.credit_json` 的键是哈希，无法解析到模型；按模型拆分依赖 `sessions.model`（会话级）。
- token 来源为 `traces/` 目录，保留期有限（约 1 个月），更早的数据可能已被清理。
