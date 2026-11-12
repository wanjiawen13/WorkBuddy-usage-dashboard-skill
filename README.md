# usage-dashboard (WorkBuddy 用量报表技能)

一个 WorkBuddy 用户级技能：把本机的 WorkBuddy **积分（套餐积分）** 与 **Token 消耗** 并列展示，生成自包含 HTML 报表（堆叠柱状图 + KPI + 模型排行 + 可排序明细表 + 时间筛选）。

内置的用量展示较粗，这个技能提供更细的维度：

- 每个模型各消耗多少积分 / Token
- 按时间分布（**只看一天时按小时、跨多天按天**）
- 积分按 `credit_json` 键内 UUIDv7 时间戳精确到条目，不再把整天的量压到一小时
- 每笔积分都带「时间可信度」与模型归因来源，实测值与兜底推断分开呈现

## 功能

- **WorkBuddy 积分**：来自 `workbuddy.db` 的 `session_usage.credit_json`（套餐积分，小数）。键是 UUIDv7，前 12 hex = 消费毫秒时间戳，逐条归入真实时刻；旧格式随机键才回退 trace 加权或最后同步时刻，并标 `approx` / `unknown`。
- **模型归因**：按 ±60s 全局时间窗口里最近的 generation 匹配（同会话优先），依次回退会话多数模型、会话主模型名、档位名。本机实测「未指定」为 0 条。
- **Token 消耗**：来自 `traces/*/trace_*.json` 与 `trace_*.jsonl`（双格式兼容），取 `total_tokens`（= 输入 + 输出）；「其中缓存」是 `prompt_tokens` 的子集，单列展示、不与输入相加。按 generation 开始时刻归桶。
- 两个指标**并列**展示，粒度与来源不同，仅供对照。
- 时间筛选：全部 / 今天 / 近 7 天 / 自定义。**筛选结果只涉及一天时按小时，跨多天按天。**

## 环境要求

- 任意 **Python 3**（脚本零第三方依赖，仅用标准库）。
- 本机已安装并使用过 WorkBuddy（数据位于 `~/.workbuddy/`）。

> 数据路径用 `expanduser("~/.workbuddy/...")` 解析，到别人机器会自动指向对方数据，无需改路径。

## 安装

### 方式 A：作为 WorkBuddy 技能（推荐）

```bash
git clone https://github.com/wanjiawen13/WorkBuddy-usage-dashboard-skill.git
cp -r WorkBuddy-usage-dashboard-skill ~/.workbuddy/skills/usage-dashboard
```

Windows（PowerShell）：

```powershell
git clone https://github.com/wanjiawen13/WorkBuddy-usage-dashboard-skill.git
New-Item -ItemType Directory -Force "$HOME\.workbuddy\skills" | Out-Null
Copy-Item -Recurse -Force .\WorkBuddy-usage-dashboard-skill "$HOME\.workbuddy\skills\usage-dashboard"
```

重启 WorkBuddy 后，对它说「生成积分用量报表」「看每小时积分」「token 消耗」等即可触发。

### 方式 B：纯脚本运行（不想装技能）

只需 `scripts/gen_report.py`：

```bash
python scripts/gen_report.py
# 可选参数：
#   --db <path>      指定 workbuddy.db
#   --traces <dir>   指定 traces 目录
#   --out <file>     指定输出 HTML
```

会在**当前目录**生成 `usage-report.html`。

## 使用

运行后打开 `usage-report.html`：

- 顶部两个并列汇总卡：积分合计 / Token 合计；积分卡额外显示时间不可考、估算、时间窗口归因占比、未指定模型四项质量指标。
- Tab 切换「WorkBuddy 积分」与「Token 消耗」。
- 图表按「小时 × 模型」（只看一天）或「天 × 模型」（跨多天）堆叠。
- 积分明细含「时间可信度」列（精确 / 估算 / 不可考）；Token 明细含「其中缓存」列。
- 下方有模型排行与可排序明细表；支持时间筛选。
- 数字视图为纯 HTML，离线也能看；柱状图需加载 Chart.js（CDN）。

## 口径说明

1. 积分 = `session_usage.credit_json` 各项之和（单位：积分，两位小数），总额严格守恒。时间来自键内 UUIDv7 毫秒时间戳；旧格式随机键无法定时的部分会标「不可考」，只保证总额、不代表当天真实消耗。
2. Token = trace 的 `total_tokens`（= 输入 + 输出，整数）；缓存 token 是输入的子集，单列展示，不重复计入合计。
3. 模型归因优先用全局时间窗口内最近的计费 generation（同会话优先），再依次回退会话多数模型、会话主模型名；只把 `fast-model` 等档位别名排除在窗口匹配之外。
4. 免费模型：`FREE_MODELS` 只放用户确认的自配不计费模型 `deepseek-v4-flash`（无 `.1`）。WorkBuddy 内置模型名带 `free` / `lite`（`hy4free`、`wb-lite`）仍会消耗积分，不能按名称推断。
5. 两者来源与粒度不同（积分按会话条目、Token 按 trace），仅作并列参考。

详细字段、归因级别与实测数据见 [references/data-schema.md](references/data-schema.md)。

## 已知限制

- 本机 trace 里约 89% 的 generation 没有 `sessionId`，因此归因依赖时间关联而非会话 ID；并发调用密集时可能有几秒级误配。
- traces 保留期约 1 个月，更早的会话既无 Token 数据，积分也只能靠会话主模型兜底（报表用 `src` 标明）。
- 积分同步有延迟，活跃会话当天数字偏小是常态。

## 隐私

脚本只读分析，绝不修改 `workbuddy.db` 或 traces。生成的报表只包含你本机用量统计，不含任何账号或对话内容；分享前请自行确认不泄露敏感信息。

## 许可

[MIT](LICENSE)
