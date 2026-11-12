#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 用量报表生成器 (v4.2)

并列展示两个指标：
  1) WorkBuddy 积分   —— 来自 workbuddy.db 的 session_usage.credit_json（每会话积分）
  2) Token 消耗       —— 来自 traces/*/trace_*.json 与 trace_*.jsonl（输入/输出/缓存 token + 模型 + 时间）

积分记录带 est(时间可信度) 与 src(模型归因来源) 标记；
无法可靠归时的积分仍计入总额，但报表显式提示，不伪装成当天真实消耗。

按"小时 × 模型"聚合，生成自包含 usage-report.html（堆叠柱状图 + KPI + 模型排行 + 明细表 + 时间筛选）。

用法:
    python gen_report.py
    python gen_report.py --db "<path>" --traces "<dir>" --out report.html

依赖: 仅 Python 标准库。图表用 Chart.js (CDN)；数字视图为纯 HTML，离线可看数字。
"""

import sqlite3
import json
import sys
import os
import glob
import re
import bisect
from datetime import datetime, timezone, timedelta

HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, ".workbuddy", "workbuddy.db")
DEFAULT_TRACES = os.path.join(HOME, ".workbuddy", "traces")
OUT_NAME = "usage-report.html"

# 本地时区 (UTC+8, 无夏令时)
TZ = timezone(timedelta(hours=8))

# credit_json 的键是 128-bit ID，前 12 hex = UUIDv7 的 48-bit 毫秒时间戳。
# 该时间戳需落在此区间才可信；旧格式随机ID解码会溢出或落在遥远未来，判为不可信。
_U7_MIN = datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp()
_U7_MAX = datetime(2030, 12, 31, tzinfo=timezone.utc).timestamp()
_HEX32 = re.compile(r"^[0-9a-f]{32}$")

# 不消耗 WorkBuddy 积分的「自配」模型，积分归因时跳过。
# 只放用户确认过的项：deepseek-v4-flash（无 .1，走内网自配）不计费；
# deepseek-v4.1-flash（带 .1，WorkBuddy 内置）计费，切勿混淆。
# ⚠ WorkBuddy 内置模型名带 free/lite（hy4free / wb-lite / hy3 等）只是档位名，
#   它们仍有自己的积分倍率、仍会消耗积分，**不能按名称推断为免费**。
#   旧版把 hy4free / wb-lite 放进这里导致大量积分被错判为「未指定」，已修正。
FREE_MODELS = {"deepseek-v4-flash"}

# 模型名别名：本机 models.json 用连字符 id，API 响应返回带点号的名字，二者是同一模型。
# 只合并有配置佐证的项，其余变体保留原名以免误并。
MODEL_ALIASES = {
    "deepseek-v4-1-flash": "deepseek-v4.1-flash",
}

# sessions.model 里的档位/模式别名，不是真实模型名。
# 实测这些会话全部无 trace 覆盖，绝不能拿它们做积分模型归因。
TIER_ALIASES = {"fast-model", "balanced-model", "deep-model", "auto", "default"}


def _paid(m):
    """返回计费模型名；自配免费模型、档位别名、空值一律返回 None。"""
    m = norm_model(m)
    if not m or m in FREE_MODELS or m in TIER_ALIASES or m == "未指定":
        return None
    return m


def _named_model(m):
    """取一个可直接展示的名字：只排除空值，不排除免费模型，也不排除档位别名。

    用户要求每笔积分都有名字、不能出现「未指定」。自配免费模型与档位别名
    （fast-model 等）作为会话主模型时照样显示原名，由 src 标注这是兜底推断，
    避免把无 trace 的老会话硬塞成「未指定」。
    """
    m = norm_model(m)
    if not m or m == "未指定":
        return None
    return m


def decode_uuid7(key):
    """解析 credit_json 键里的 UUIDv7 时间戳（前 12 hex = 毫秒）。命中返回本地时区 datetime，否则 None。"""
    if not _HEX32.match(key):
        return None
    try:
        ts = int(key[:12], 16) / 1000.0
        if _U7_MIN <= ts <= _U7_MAX:
            return datetime.fromtimestamp(ts, tz=TZ)
    except Exception:
        return None
    return None


def build_model_index(all_gens):
    """把全局 [(epoch, model, sessionId)] 按时间排序，返回 (gens, epochs) 供二分查找。"""
    gens = sorted(all_gens, key=lambda x: x[0])
    return gens, [g[0] for g in gens]


def _model_at_time(index, dt, sid=None, window=60):
    """在 ±window 秒的全局 generation 窗口里找产生这笔积分最可能的【计费】模型。

    关键事实：本机 trace 里约 89% 的 generation **没有 sessionId**（只有少数
    trace_start 事件带 sessionId），所以「只按同会话匹配」会大面积失败并产生
    「未指定」。而 credit_json 的 UUIDv7 时间戳与该次 generation 的 startedAt
    通常只差 1~6 秒，是更可靠的关联依据。
    排序：同会话候选优先（有 sessionId 时），其次按时间差最小；免费模型与档位名跳过。
    """
    gens, eps = index
    if not gens:
        return None
    t = dt.timestamp()
    lo = bisect.bisect_left(eps, t - window)
    hi = bisect.bisect_right(eps, t + window)
    best = None
    for epoch, m, gsid in gens[lo:hi]:
        mm = norm_model(m)
        if not _paid(mm):
            continue
        key = (0 if (sid and gsid and gsid == sid) else 1, abs(epoch - t))
        if best is None or key < best[0]:
            best = (key, mm)
    return best[1] if best else None


def norm_model(m):
    """归一化模型名：去掉 custom-local: 前缀、合并有佐证的别名、统一空值。"""
    m = (m or "").strip()
    if not m:
        return "未指定"
    if m.startswith("custom-local:"):
        m = m.split(":", 1)[1]
    return MODEL_ALIASES.get(m, m)


def load_credits(db_path, traces_by_session, model_index):
    """WorkBuddy 积分：每个会话 credit_json 求和。

    时间归属：credit_json 键是 UUIDv7，前 12 hex = 消费时刻的毫秒时间戳，逐条归入真实
    时刻；旧格式随机 ID 才回退 trace 加权摊分或 updated_at。

    模型归因（四级，按可靠性递减）：
      L1 time-window     全局 generation 里 ±60s 窗口内最近的计费模型（同会话优先）
      L2 trace-dominant  同会话 trace 里出现最多的计费模型
      L3 session-model   sessions.model 的真实模型名
      L3' tier-alias     会话只记了档位名（fast-model 等），仍显示原名而非「未指定」
      L4 unattributed    连会话主模型都没有 -> "未指定"（本机实测为 0）
    L1 是主路径：trace 里约 89% 的 generation 没有 sessionId，只按会话匹配必然
    大面积落到「未指定」。WorkBuddy 内置模型（含 hy4free / wb-lite）都有名字、
    都会消耗积分，不能按名称当作免费模型排除；档位别名不参与 L1 匹配。

    每条记录带两个质量标记：
      est = exact(键内真实时间) / approx(trace token 加权摊分) / unknown(只能归到同步时刻)
      src = time-window / trace-dominant / session-model / tier-alias / unattributed
    unknown 的积分只保证总额守恒，不代表当天真实消耗，报表必须显式提示。
    """
    if not os.path.exists(db_path):
        print("WARN: db 不存在:", db_path)
        return []
    uri = "file:{}?mode=ro".format(db_path)
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT su.session_id AS sid, su.updated_at AS upd, su.credit_json AS cj,
               s.model AS model, s.title AS title
        FROM session_usage su
        LEFT JOIN sessions s ON s.id = su.session_id
    """).fetchall()
    conn.close()

    # 每会话按时间排序的 (epoch, model)，用于"条目时间 -> 实际模型"匹配
    gens_by_session = {}
    for sid, trs in traces_by_session.items():
        gens_by_session[sid] = sorted(
            ((t["epoch"], t.get("model") or "") for t in trs if t.get("tok", 0) > 0),
            key=lambda x: x[0])

    out = []

    def _items(cj_text):
        """把 credit_json 解析为 [(key, float_value)]，跳过非数值项，避免单条脏数据丢失整会话。"""
        try:
            raw = json.loads(cj_text or "{}")
        except Exception:
            return []
        items = []
        for k, v in raw.items():
            try:
                items.append((k, float(v)))
            except (TypeError, ValueError):
                continue
        return items

    for r in rows:
        items = _items(r["cj"])
        credits = sum(v for _, v in items)
        if credits <= 0:
            continue
        sid = r["sid"]
        title = (r["title"] or "(无标题)")
        # L3 兜底：会话主模型名照原样可用；档位别名单独标 src=tier-alias 以便识别
        model0 = _named_model(r["model"])
        model0_src = ("tier-alias" if model0 and norm_model(model0) in TIER_ALIASES
                      else "session-model" if model0 else None)
        gens = gens_by_session.get(sid) or []
        dom_model = None
        if gens:
            mc = {}
            for _, m in gens:
                pm = _paid(m)
                if pm:
                    mc[pm] = mc.get(pm, 0) + 1
            if mc:
                dom_model = sorted(mc.items(), key=lambda kv: -kv[1])[0][0]

        # 拆分：可信 UUIDv7 时间条目 vs 不可信（旧格式/溢出）条目
        valid, legacy_val = [], 0.0
        for k, v in items:
            dt = decode_uuid7(k)
            if dt is not None:
                valid.append((dt, v))
            else:
                legacy_val += v

        def emit(dt, model, value, est="exact", src="unattributed"):
            out.append({
                "ts": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "d": dt.strftime("%Y-%m-%d"),
                "h": dt.strftime("%Y-%m-%d %H:00"),
                "model": _named_model(model) or "未指定",
                "value": round(value, 4),
                "label": title,
                "est": est,
                "src": src,
            })

        if valid:
            for dt, v in valid:
                hit = _model_at_time(model_index, dt, sid)
                model = hit or dom_model or model0 or "未指定"
                src = ("time-window" if hit else
                       "trace-dominant" if dom_model else
                       model0_src or "unattributed")
                emit(dt, model, v, "exact", src)
            if legacy_val > 0:
                upd_dt = datetime.fromtimestamp((r["upd"] or 0) / 1000.0, tz=TZ)
                model = dom_model or model0 or "未指定"
                src = "trace-dominant" if dom_model else model0_src or "unattributed"
                emit(upd_dt, model, legacy_val, "unknown", src)
        else:
            # 旧格式会话：所有键都无时间信息，只能摊分或压到同步时刻，一律标 approx/unknown
            traces = [t for t in (traces_by_session.get(sid) or []) if t.get("tok", 0) > 0]
            upd_s = (r["upd"] or 0) / 1000.0
            if traces and upd_s - max(t["epoch"] for t in traces) > 86400:
                traces = []
            if traces:
                wsum = sum(t["tok"] for t in traces)
                for t in traces:
                    share = credits * (t["tok"] / wsum) if wsum > 0 else credits / len(traces)
                    out.append({
                        "ts": t["ts"], "d": t["d"], "h": t["h"],
                        "model": _named_model(t.get("model")) or model0 or "未指定",
                        "value": round(share, 4), "label": title,
                        "est": "approx", "src": "trace-dominant",
                    })
            else:
                emit(datetime.fromtimestamp(upd_s, tz=TZ), model0 or "未指定", credits,
                     "unknown", model0_src or "unattributed")
    out.sort(key=lambda x: x["ts"])
    return out


def _parse_json(j):
    """解析旧版 trace_<id>.json（单对象 {trace, spans}）。返回统一的 trace 描述字典。"""
    t = j.get("trace") or {}
    started = t.get("startedAt")
    sid = t.get("sessionId")
    name = t.get("name")
    mi = t.get("modelInfo") or {}
    inp = int(mi.get("totalInputTokens") or 0)
    outp = int(mi.get("totalOutputTokens") or 0)
    cached = int(mi.get("totalCachedTokens") or 0)
    total = int(t.get("totalTokens") or (inp + outp))
    models = [norm_model(m) for m in (mi.get("models") or [])]
    n = len(models) or 1
    generations = []
    for m in models:
        generations.append({
            "model": m, "inp": inp / n, "outp": outp / n,
            "cached": cached / n, "total": total / n, "ts": started,
        })
    return {"startedAt": started, "sessionId": sid, "name": name, "generations": generations}


def _parse_jsonl(path):
    """解析新版 trace_<id>.jsonl（事件流，每行一个事件）。
    模型/token 在 generation span 的 toolOutput（原始 API 响应）里。
    """
    started = None
    sid = None
    name = None
    generations = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                ty = o.get("type")
                if ty in ("trace_start", "trace_update"):
                    tr = o.get("trace") or {}
                    if started is None:
                        started = tr.get("startedAt")
                    sid = tr.get("sessionId") or sid
                    name = tr.get("name") or name
                elif ty == "span_end":
                    sp = o.get("span") or {}
                    if sp.get("type") != "generation":
                        continue
                    gts = sp.get("startedAt")
                    out = sp.get("toolOutput")
                    if not out:
                        continue
                    try:
                        arr = json.loads(out)
                    except Exception:
                        continue
                    items = arr if isinstance(arr, list) else [arr]
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        model = norm_model(item.get("model"))
                        us = item.get("usage") or {}
                        inp = int(us.get("prompt_tokens") or 0)
                        outp = int(us.get("completion_tokens") or 0)
                        total = int(us.get("total_tokens") or (inp + outp))
                        pd = us.get("prompt_tokens_details") or {}
                        cd = us.get("completion_tokens_details") or {}
                        cached = int(pd.get("cached_tokens") or 0) + int(cd.get("cached_tokens") or 0)
                        if model and total > 0:
                            generations.append({
                                "model": model, "inp": inp, "outp": outp,
                                "cached": cached, "total": total, "ts": gts,
                            })
    except Exception:
        pass
    return {"startedAt": started, "sessionId": sid, "name": name, "generations": generations}


def scan_traces(traces_dir):
    """扫描 traces 目录：返回 (token 记录列表, session_id 索引, 全局模型时间索引)。

    兼容两种 trace 格式：
      - 旧版 trace_<id>.json（单对象 {trace, spans}，模型/token 在 trace.modelInfo）
      - 新版 trace_<id>.jsonl（事件流；模型/token 在 generation span 的 toolOutput）
    token 记录用于 Token 指标；session 索引用于会话内摊分；
    全局模型时间索引用于按积分消费时刻就近匹配实际模型（不依赖 sessionId）。
    """
    if not os.path.isdir(traces_dir):
        print("WARN: traces 目录不存在:", traces_dir)
        return [], {}, ([], [])
    files = glob.glob(os.path.join(traces_dir, "*", "*.json"))
    files += glob.glob(os.path.join(traces_dir, "*", "*.jsonl"))
    token_records = []
    by_session = {}
    all_gens = []
    for f in files:
        try:
            if f.endswith(".jsonl"):
                parsed = _parse_jsonl(f)
            else:
                parsed = _parse_json(json.load(open(f, encoding="utf-8")))
        except Exception:
            continue
        started = parsed.get("startedAt")
        if not started:
            continue
        try:
            dt = datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(TZ)
        except Exception:
            continue
        ts = dt.strftime("%Y-%m-%d %H:%M:%S")
        hh = dt.strftime("%Y-%m-%d %H:00")
        dd = dt.strftime("%Y-%m-%d")

        sid = parsed.get("sessionId")
        label = parsed.get("name") or "trace"
        for g in parsed.get("generations", []):
            gts = g.get("ts") or ts
            try:
                gdt = datetime.fromisoformat(gts.replace("Z", "+00:00")).astimezone(TZ)
                g_ts = gdt.strftime("%Y-%m-%d %H:%M:%S")
                g_hh = gdt.strftime("%Y-%m-%d %H:00")
                g_dd = gdt.strftime("%Y-%m-%d")
                g_epoch = gdt.timestamp()
            except Exception:
                g_ts, g_hh, g_dd, g_epoch = ts, hh, dd, dt.timestamp()
            if sid:
                by_session.setdefault(sid, []).append({
                    "ts": g_ts, "d": g_dd, "h": g_hh, "tok": float(g["total"]),
                    "model": g["model"], "epoch": g_epoch,
                })
            all_gens.append((g_epoch, g["model"], sid))
            token_records.append({
                "ts": g_ts, "d": g_dd, "h": g_hh, "model": g["model"],
                "value": round(g["total"], 2),
                "input": round(g["inp"], 2),
                "output": round(g["outp"], 2),
                "cached": round(g["cached"], 2),
                "label": label,
            })
    token_records.sort(key=lambda x: x["ts"])
    return token_records, by_session, build_model_index(all_gens)


def build_html(credits, tokens, meta):
    meta = dict(meta)
    meta["freeModels"] = sorted(FREE_MODELS)
    meta["aliases"] = MODEL_ALIASES
    data = {"meta": meta, "credits": credits, "tokens": tokens}
    # 防止会话标题里出现 </script> 提前闭合内联脚本（JS 字符串里 \/ 等价于 /）
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")

    html = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>WorkBuddy 用量报表</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{--bg:#f6f7f9;--card:#fff;--ink:#1f2329;--sub:#6b7280;--line:#e5e7eb;--accent:#2563eb;--pts:#7c3aed;--tok:#0891b2;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;}
  .wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px;}
  h1{font-size:22px;margin:0 0 4px;}
  .meta{color:var(--sub);font-size:13px;margin-bottom:18px;}
  .filters{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px;align-items:center}
  .filters button{border:1px solid var(--line);background:#fff;color:var(--ink);padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px;}
  .filters button.active{background:var(--accent);color:#fff;border-color:var(--accent);}
  .filters input[type=date]{border:1px solid var(--line);border-radius:8px;padding:5px 8px;font-size:13px;}
  .filters .sep{color:var(--sub);font-size:12px;margin-left:4px;}
  .summary{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:18px;}
  .scard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;border-left:4px solid var(--line);}
  .scard.pts{border-left-color:var(--pts);} .scard.tok{border-left-color:var(--tok);}
  .scard .st{font-size:13px;color:var(--sub);margin-bottom:6px;}
  .scard .sv{font-size:26px;font-weight:700;}
  .scard .ss{font-size:12px;color:var(--sub);margin-top:6px;}
  .tabs{display:flex;gap:8px;margin:8px 0 14px;}
  .tabs button{border:1px solid var(--line);background:#fff;color:var(--ink);padding:8px 18px;border-radius:10px;cursor:pointer;font-size:14px;font-weight:600;}
  .tabs button.active.pts{background:var(--pts);color:#fff;border-color:var(--pts);}
  .tabs button.active.tok{background:var(--tok);color:#fff;border-color:var(--tok);}
  .kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:18px;}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}
  .kpi .v{font-size:22px;font-weight:700;}
  .kpi .l{font-size:12px;color:var(--sub);margin-top:2px;}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px;}
  .card h2{font-size:15px;margin:0 0 12px;}
  .chart-box{position:relative;height:420px;}
  .rank-row{display:flex;align-items:center;gap:10px;margin:8px 0;font-size:13px;}
  .rank-name{width:170px;flex:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .rank-bar{flex:1;height:18px;background:#eef1f5;border-radius:6px;overflow:hidden;}
  .rank-bar>i{display:block;height:100%;}
  .rank-val{width:210px;flex:none;text-align:right;color:var(--sub);font-variant-numeric:tabular-nums;}
  .tblwrap{max-height:420px;overflow:auto;}
  table{width:100%;border-collapse:collapse;font-size:13px;}
  th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap;}
  td.title{max-width:380px;overflow:hidden;text-overflow:ellipsis;}
  th{color:var(--sub);font-weight:600;cursor:pointer;user-select:none;position:sticky;top:0;background:var(--card);}
  td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;}
  .tag{display:inline-block;padding:1px 8px;border-radius:999px;background:#eef2ff;color:#3730a3;font-size:12px;}
  .note{font-size:12px;color:var(--sub);line-height:1.6;}
  .empty{color:var(--sub);padding:20px;text-align:center;}
  .hint{font-size:12px;color:var(--sub);margin:-4px 0 10px;}
</style>
</head>
<body>
<div class="wrap">
  <h1>WorkBuddy 用量报表</h1>
  <div class="meta" id="meta"></div>

  <div class="filters">
    <button data-f="all" class="active">全部</button>
    <button data-f="today">今天</button>
    <button data-f="7d">近 7 天</button>
    <span class="sep">自定义:</span>
    <input type="date" id="from"/>
    <span class="sep">至</span>
    <input type="date" id="to"/>
    <button data-f="custom">应用</button>
  </div>

  <div class="summary" id="summary"></div>

  <div class="tabs">
    <button data-m="credits" class="active pts">WorkBuddy 积分</button>
    <button data-m="tokens" class="tok">Token 消耗</button>
  </div>

  <div class="hint" id="hint"></div>
  <div class="kpis" id="kpis"></div>

  <div class="card">
    <h2 id="chartTitle">每小时用量（按模型堆叠）</h2>
    <div class="chart-box"><canvas id="chart"></canvas></div>
    <div class="empty" id="chartEmpty" style="display:none">当前筛选无数据</div>
  </div>

  <div class="card">
    <h2>各模型总量排行</h2>
    <div id="rank"></div>
  </div>

  <div class="card">
    <h2>明细 <span class="note">(点击表头排序)</span></h2>
    <div class="tblwrap"><table id="tbl"><thead></thead><tbody></tbody></table></div>
  </div>

  <div class="note" id="foot"></div>
</div>

<script>
const DATA = __DATA__;
const PALETTE = ["#2563eb","#e23b3b","#16a34a","#d97706","#7c3aed","#0891b2",
  "#db2777","#65a30d","#dc2626","#0d9488","#9333ea","#ca8a04","#4f46e5","#059669","#f97316","#0ea5e9"];
const META = DATA.meta;

document.getElementById('meta').textContent =
  `生成时间 ${META.generatedAt} ｜ 积分源 ${META.dbPath} ｜ Token 源 ${META.tracesDir} ｜ 积分记录 ${DATA.credits.length} 条 / Token 记录 ${DATA.tokens.length} 条`;

let maxH = ""; let maxD = "";
[...DATA.credits, ...DATA.tokens].forEach(r=>{ if(r.h>maxH)maxH=r.h; if(r.d>maxD)maxD=r.d; });

let metric = 'credits';
let chart = null;
let curFilter = 'all';
let sortKey = 'ts', sortDir = -1;
let gran = 'day';  // 时间粒度：'day'(按天) 或 'hour'(按小时)。仅"今天"用 hour，其余用 day。

function metricRecords(){ return metric==='credits' ? DATA.credits : DATA.tokens; }

// 当前粒度下，一条记录归入哪个时间桶的键
function bkey(r){ return gran==='hour' ? r.h : r.d; }

function applyFilter(recs){
  if(curFilter==='today') return recs.filter(r=>r.d===maxD);
  if(curFilter==='7d'){
    if(!maxH) return recs;
    const end=new Date(maxH.replace(' ','T')+':00');
    const start=new Date(end.getTime()-7*24*3600*1000);
    return recs.filter(r=>{const t=new Date(r.h.replace(' ','T')+':00');return t>=start&&t<=end;});
  }
  if(curFilter==='custom'){
    const f=document.getElementById('from').value, t=document.getElementById('to').value;
    return recs.filter(r=>(!f||r.d>=f)&&(!t||r.d<=t));
  }
  return recs;
}

function aggregate(recs){
  const bMap={}, modelTotals={}, hm={}; let total=0;
  recs.forEach(r=>{
    const b=bkey(r);
    bMap[b]=true;
    modelTotals[r.model]=(modelTotals[r.model]||0)+r.value;
    (hm[b]=hm[b]||{}); hm[b][r.model]=(hm[b][r.model]||0)+r.value;
    total+=r.value;
  });
  const buckets=Object.keys(bMap).sort();
  const models=Object.keys(modelTotals).sort((a,b)=>modelTotals[b]-modelTotals[a]);
  return {buckets,models,modelTotals,hm,total};
}

function fmt(v,isPts){ return isPts ? (+v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2}) : Math.round(+v).toLocaleString(); }

function topModel(recs,isPts){
  const m={}; recs.forEach(r=>m[r.model]=(m[r.model]||0)+r.value);
  // "未指定"不是模型，只排真实模型；其占比在卡片第二行单独披露
  const k=Object.keys(m).filter(x=>x!=='未指定').sort((a,b)=>m[b]-m[a])[0];
  return k?`${k}（${fmt(m[k],isPts)}）`:'-';
}

function renderSummary(){
  const cRecs=applyFilter(DATA.credits), tRecs=applyFilter(DATA.tokens);
  const cTot=cRecs.reduce((a,r)=>a+r.value,0), tTot=tRecs.reduce((a,r)=>a+r.value,0);
  const sumBy=(f)=>cRecs.filter(f).reduce((a,r)=>a+r.value,0);
  const unknown=sumBy(r=>r.est==='unknown'), approx=sumBy(r=>r.est==='approx');
  const traced=sumBy(r=>r.src==='time-window'), unattr=sumBy(r=>r.model==='未指定');
  document.getElementById('summary').innerHTML=`
    <div class="scard pts"><div class="st">WorkBuddy 积分（已消耗）</div><div class="sv">${fmt(cTot,true)}</div>
      <div class="ss">最高模型：${topModel(cRecs,true)} · ${cRecs.length} 条消费记录</div>
      <div class="ss">时间不可考 ${fmt(unknown,true)} / 估算 ${fmt(approx,true)}；时间窗口归因 ${cTot?(traced/cTot*100).toFixed(1):0}%，未指定模型 ${fmt(unattr,true)}</div></div>
    <div class="scard tok"><div class="st">Token 消耗（输入+输出）</div><div class="sv">${fmt(tTot,false)}</div>
      <div class="ss">最高模型：${topModel(tRecs,false)} · ${tRecs.length} 条 trace（仅本地留存期内）</div></div>`;
}

function renderKpis(agg,isPts){
  const unit = metric==='credits' ? '积分' : 'tokens';
  const named = agg.models.filter(x=>x!=='未指定');
  const top = named[0]||'-';
  const topVal = top!=='-'?agg.modelTotals[top]:0;
  const pct = agg.total?(topVal/agg.total*100).toFixed(1):'0';
  const span = agg.buckets.length?`${agg.buckets[0]} ~ ${agg.buckets[agg.buckets.length-1]}`:'-';
  const unitLbl = gran==='hour' ? '小时' : '天';
  document.getElementById('kpis').innerHTML=`
    <div class="kpi"><div class="v">${fmt(agg.total,isPts)}</div><div class="l">${unit}合计</div></div>
    <div class="kpi"><div class="v" style="font-size:14px">${span}</div><div class="l">时间区间(${unitLbl})</div></div>
    <div class="kpi"><div class="v" style="font-size:15px">${top} <span class="note">${pct}%</span></div><div class="l">消耗最高模型</div></div>`;
}

function renderChart(agg,isPts){
  const box=document.getElementById('chart'), empty=document.getElementById('chartEmpty');
  if(agg.buckets.length===0){ box.style.display='none'; empty.style.display='block'; if(chart){chart.destroy();chart=null;} return; }
  box.style.display='block'; empty.style.display='none';
  const datasets=agg.models.map((m,i)=>({label:m,data:agg.buckets.map(b=>agg.hm[b][m]||0),backgroundColor:PALETTE[i%PALETTE.length]}));
  const ylab = metric==='credits'?'积分':'tokens';
  const cfg={type:'bar',
    data:{labels:agg.buckets,datasets},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
      plugins:{legend:{position:'bottom',labels:{boxWidth:12,font:{size:11}}},
        tooltip:{callbacks:{label:c=>`${c.dataset.label}: ${fmt(c.parsed.y,isPts)} ${ylab}`}}},
      scales:{x:{stacked:true,ticks:{maxRotation:60,minRotation:45,font:{size:10}}},
        y:{stacked:true,beginAtZero:true,title:{display:true,text:ylab}}}}};
  if(chart)chart.destroy();
  chart=new Chart(box.getContext('2d'),cfg);
}

function renderRank(agg,isPts){
  const el=document.getElementById('rank');
  if(agg.models.length===0){ el.innerHTML='<div class="empty">无数据</div>'; return; }
  const max=agg.modelTotals[agg.models[0]]||1;
  el.innerHTML=agg.models.map((m,idx)=>{const v=agg.modelTotals[m];const w=(v/max*100).toFixed(1);
    const pct=agg.total?(v/agg.total*100).toFixed(1):'0';
    return `<div class="rank-row"><div class="rank-name" title="${m}">${m}</div>
      <div class="rank-bar"><i style="width:${w}%;background:${PALETTE[idx%PALETTE.length]}"></i></div>
      <div class="rank-val">${fmt(v,isPts)} ｜ ${pct}%</div></div>`;}).join('');
}

function renderTable(recs,isPts){
  const tbl=document.getElementById('tbl');
  const EST_LBL={exact:'精确',approx:'估算',unknown:'不可考'};
  const cols = metric==='credits'
    ? [['ts','时间',''],['label','标题','title'],['model','模型',''],['est','时间可信度',''],['value','积分','num']]
    : [['ts','时间',''],['model','模型',''],['input','输入','num'],['output','输出','num'],['cached','其中缓存','num'],['value','合计','num']];
  tbl.querySelector('thead').innerHTML='<tr>'+cols.map(c=>`<th data-k="${c[0]}" class="${c[2]}">${c[1]}</th>`).join('')+'</tr>';
  const rows=[...recs].sort((a,b)=>{let va=a[sortKey],vb=b[sortKey];
    if(typeof va==='number'||typeof vb==='number'){va=+va||0;vb=+vb||0;}
    if(va<vb)return -1*sortDir;if(va>vb)return 1*sortDir;return 0;});
  const body=rows.map(r=>'<tr>'+cols.map(c=>{
    const k=c[0];let v=r[k];
    if(k==='est')return `<td>${EST_LBL[v]||v}</td>`;
    if(k==='model')return `<td><span class="tag">${v}</span></td>`;
    if(k==='label')return `<td class="title" title="${String(v).replace(/"/g,'&quot;')}">${String(v).replace(/</g,'&lt;')}</td>`;
    if(['value','input','output','cached'].includes(k))return `<td class="num">${fmt(v,isPts)}</td>`;
    return `<td>${v}</td>`;}).join('')+'</tr>').join('');
  tbl.querySelector('tbody').innerHTML=body;
  tbl.querySelectorAll('th').forEach(th=>th.addEventListener('click',()=>{const k=th.dataset.k;
    if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=['value','input','output','cached'].includes(k)?-1:1;}
    renderTable(applyFilter(metricRecords()),isPts);}));
}

function render(){
  const isPts = metric==='credits';
  const recs = applyFilter(metricRecords());
  // 粒度：筛选结果只涉及"一天"→ 按小时；跨多天 → 按天。
  // 对"今天"、自定义单日（起止同一天）都生效；全部/近7天/跨天自定义按天。
  const daySet = new Set(recs.map(r=>r.d));
  gran = daySet.size <= 1 ? 'hour' : 'day';
  const agg = aggregate(recs);
  document.getElementById('chartTitle').textContent =
    (gran==='hour' ? '每小时用量' : '每天用量') + '（按模型堆叠）';
  document.getElementById('hint').textContent = (isPts
    ? '指标：WorkBuddy 积分。键内 UUIDv7 时间戳归入真实消费时刻；模型按该时刻 ±60s 窗口内最近的计费 generation 匹配（同会话优先），无匹配依次回退会话多数模型、会话主模型名；档位别名不参与窗口匹配，仅在无 trace 的老会话作为兜底名字显示。自配不计费模型：'+META.freeModels.join('、')+'。内置模型名带 free/lite 仍计费。标记"不可考"的积分只保证总额，不代表当天真实消耗。'
    : '指标：Token 消耗（运行 trace，输入+输出；"其中缓存"是输入的子集，不可与输入相加）。按 generation 开始时刻归桶。')
    + (gran==='hour' ? ' 当前粒度：小时。' : ' 当前粒度：天。');
  renderSummary();
  renderKpis(agg,isPts);
  renderChart(agg,isPts);
  renderRank(agg,isPts);
  renderTable(recs,isPts);
}

document.querySelectorAll('.filters button').forEach(b=>{
  b.addEventListener('click',()=>{const f=b.dataset.f;
    if(f==='custom'){curFilter='custom';}
    else{curFilter=f;document.getElementById('from').value='';document.getElementById('to').value='';}
    document.querySelectorAll('.filters button').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');render();});
});
document.querySelectorAll('.tabs button').forEach(b=>{
  b.addEventListener('click',()=>{metric=b.dataset.m;
    document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');sortKey='ts';sortDir=-1;render();});
});

document.getElementById('foot').innerHTML =
  '口径说明：① 积分 = session_usage.credit_json 各项之和（单位积分，两位小数），总额守恒；'
  +'② Token = trace 的 total_tokens（= prompt + completion）；"其中缓存"是 prompt_tokens 的子集，单列展示、不重复计入合计；'
  +'③ 积分时间：键内 UUIDv7 毫秒时间戳为"精确"；旧格式随机ID 只能按 trace token 加权"估算"或归到最后同步时刻"不可考"，后者不代表当天真实消耗；'
  +'④ 模型归因：L1 全局 ±60s 时间窗口内最近的计费 generation（同会话优先）→ L2 会话多数模型 → L3 会话主模型名；trace 里约 89% 的 generation 无 sessionId，故不能只按会话匹配；内置模型名带 free/lite 仍计费，仅 '+META.freeModels.join('、')+' 为用户确认的自配不计费模型（可编辑 gen_report.py 的 FREE_MODELS）'
  +'；档位别名（fast-model 等）只作老会话兜底名字显示，src=tier-alias；别名合并：'+Object.entries(META.aliases).map(([a,b])=>a+'→'+b).join('、')+'；'
  +'⑤ traces 有保留期，Token 只覆盖本地留存期；积分与 Token 来源、粒度不同，仅供并列参考。本地只读分析，不修改原始数据。';

render();
</script>
</body>
</html>
"""
    return html.replace("__DATA__", data_json)


def main():
    args = sys.argv[1:]
    db_path = DEFAULT_DB
    traces_dir = DEFAULT_TRACES
    out_name = OUT_NAME
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--db":
            db_path = args[i + 1]; i += 2; continue
        if a == "--traces":
            traces_dir = args[i + 1]; i += 2; continue
        if a == "--out":
            out_name = args[i + 1]; i += 2; continue
        if not a.startswith("--"):
            db_path = a
        i += 1

    print("积分源:", db_path)
    print("Token 源:", traces_dir)
    tokens, by_session, model_index = scan_traces(traces_dir)
    credits = load_credits(db_path, by_session, model_index)
    print("积分记录:", len(credits), "合计:", round(sum(r['value'] for r in credits), 2))
    print("  时间不可考:", round(sum(r['value'] for r in credits if r['est'] == 'unknown'), 2),
          "估算:", round(sum(r['value'] for r in credits if r['est'] == 'approx'), 2),
          "未指定模型:", round(sum(r['value'] for r in credits if r['model'] == '未指定'), 2))
    print("Token 记录:", len(tokens), "合计:", int(sum(r['value'] for r in tokens)))

    meta = {
        "generatedAt": datetime.now(tz=TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "dbPath": db_path,
        "tracesDir": traces_dir,
    }
    html = build_html(credits, tokens, meta)
    out_path = os.path.join(os.getcwd(), out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成:", out_path)


if __name__ == "__main__":
    main()
