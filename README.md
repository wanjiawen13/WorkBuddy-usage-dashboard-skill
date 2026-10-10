# usage-dashboard (WorkBuddy 用量报表技能)

一个 WorkBuddy 用户级技能：把你本机的 WorkBuddy **积分（套餐积分）** 与 **Token 消耗** 并列展示，生成自包含 HTML 报表（堆叠柱状图 + KPI + 模型排行 + 可排序明细表 + 时间筛选）。

内置的用量展示较粗，这个技能提供更细的维度：

- 每个模型各消耗多少积分 / Token
- 按时间分布（**"今天"按小时、其余按天**）
- 积分按会话各 trace 的真实活跃时间摊分，避免把整天的量压到一个小时

## 功能

- **WorkBuddy 积分**：来自 `workbuddy.db` 的 `session_usage.credit_json`（套餐积分，小数）。按会话各 trace 的 token 占比摊分到对应时间；"每个模型"取会话主模型。
- **Token 消耗**：来自 `traces/*/trace_*.json`（输入 + 输出，缓存单列），按 trace 开始时刻归桶。
- 两个指标**并列**展示，粒度与来源不同，仅供对照。
- 时间筛选：全部 / 今天 / 近 7 天 / 自定义。看"今天"按小时，其余按天。

## 环境要求

- 任意 **Python 3**（脚本零第三方依赖，仅用标准库）。
- 本机已安装并使用过 WorkBuddy（数据位于 `~/.workbuddy/`）。

> 数据路径用 `expanduser("~/.workbuddy/...")` 解析，到别人机器会自动指向对方数据，无需改路径。

## 安装

### 方式 A：作为 WorkBuddy 技能（推荐）

```bash
git clone https://github.com/<你的用户名>/usage-dashboard-skill.git
# 把仓库里的技能目录放进 WorkBuddy 的 skills 目录
cp -r usage-dashboard-skill ~/.workbuddy/skills/usage-dashboard
# Windows 用：
# xcopy usage-dashboard-skill C:\Users\<用户名>\.workbuddy\skills\usage-dashboard /E /I
```

重启 WorkBuddy 后，对它说"生成积分用量报表 / 看每小时积分"等即可触发；也可直接 `/usage-dashboard`。

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

- 顶部两个并列汇总卡：积分合计 / Token 合计。
- Tab 切换「WorkBuddy 积分」与「Token 消耗」。
- 图表按"小时 × 模型"（今天）或"天 × 模型"（其余）堆叠。
- 下方有模型排行与可排序明细表；支持时间筛选。
- 数字视图为纯 HTML，离线也能看；柱状图需加载 Chart.js（CDN）。

## 口径说明

1. 积分 = 会话消耗记录 `credit_json` 各项之和（单位：积分，两位小数）。`session_usage.updated_at` 只是最后同步时刻，因此积分按会话各 trace 的 token 占比摊分到对应时间。
2. Token = 运行 trace 的输入 + 输出（缓存单列），整数。
3. 两者来源与粒度不同（积分按会话、Token 按 trace），仅作并列参考。

详细字段与限制见 [references/data-schema.md](references/data-schema.md)。

## 隐私

脚本只读分析，绝不修改 `workbuddy.db` 或 traces。生成的报表只包含你本机用量统计，不含任何账号或对话内容；分享前请自行确认不泄露敏感信息。

## 许可

[MIT](LICENSE)
