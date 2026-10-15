#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 用量报表生成器 (v2)

并列展示两个指标：
  1) WorkBuddy 积分   —— 来自 workbuddy.db 的 session_usage.credit_json（每会话积分）
  2) Token 消耗       —— 来自 traces/*/trace_*.json（每轮 trace 的输入/输出/缓存 token + 模型 + 时间）

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


def _model_at_time(gens, dt):
    """在生成轮次列表 [(epoch, model)] 里找时间最接近 dt 的模型（6h 内）。"""
    if not gens:
        return None
    target = dt.timestamp()
    best, best_d = None, None
    for epoch, m in gens:
        d = abs(epoch - target)
        if best_d is None or d < best_d:
            best_d, best = d, m
    if best_d is not None and best_d <= 6 * 3600:
        return best
    return None


def norm_model(m):
    """归一化模型名：去掉 custom-local: 前缀，统一空值。"""
    m = (m or "").strip()
    if not m:
        return "未指定"
    if m.startswith("custom-local:"):
        m = m.split(":", 1)[1]
    return m


def load_credits(db_path, traces_by_session):
    """WorkBuddy 积分：每个会话 credit_json 求和。

    时间归属（核心修正）：credit_json 的键是 UUIDv7，前 12 hex = 该条目消费时刻的
    毫秒时间戳，可精确到条目级。优先用它把每笔积分归入真实消费时间；旧格式随机ID
    （解码越界/溢出）或没有可信时间的条目才回退。

    模型归因：sessions.model 只记"会话选的主模型"，实际请求可能被路由到其他模型。
    因此按条目的 UUIDv7 时间匹配同会话最近的 trace 生成轮次（实际模型）；无匹配时
    用会话 trace 的多数模型，再回退会话主模型。

    回退路径（旧格式会话/无 trace）：沿用 trace token 加权摊分到各小时；无 trace 才
    回退 updated_at。
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
    for r in rows:
        credits = 0.0
        if r["cj"]:
            try:
                credits = float(sum(json.loads(r["cj"]).values()))
            except Exception:
                credits = 0.0
        if credits <= 0:
            continue
        sid = r["sid"]
        model0 = norm_model(r["model"])
        title = (r["title"] or "(无标题)")
        gens = gens_by_session.get(sid) or []
        dom_model = None
        if gens:
            mc = {}
            for _, m in gens:
                if m:
                    mc[m] = mc.get(m, 0) + 1
            if mc:
                dom_model = sorted(mc.items(), key=lambda kv: -kv[1])[0][0]

        # 拆分：可信 UUIDv7 时间条目 vs 不可信（旧格式/溢出）条目
        valid, legacy_val = [], 0.0
        try:
            cj = json.loads(r["cj"])
        except Exception:
            cj = {}
        for k, v in cj.items():
            dt = decode_uuid7(k)
            if dt is not None:
                valid.append((dt, float(v)))
            else:
                legacy_val += float(v)

        def emit(dt, model, value):
            out.append({
                "ts": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "d": dt.strftime("%Y-%m-%d"),
                "h": dt.strftime("%Y-%m-%d %H:00"),
                "model": model, "value": round(value, 4), "label": title,
            })

        if valid:
            for dt, v in valid:
                emit(dt, _model_at_time(gens, dt) or dom_model or model0, v)
            if legacy_val > 0:
                upd_dt = datetime.fromtimestamp((r["upd"] or 0) / 1000.0, tz=TZ)
                emit(upd_dt, dom_model or model0, legacy_val)
        else:
            # 旧格式会话：沿用 trace token 加权摊分（带 24h 时效守卫），无 trace 回退 updated_at
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
                        "model": t.get("model") or model0,
                        "value": round(share, 4), "label": title,
                    })
            else:
                emit(datetime.fromtimestamp(upd_s, tz=TZ), model0, credits)
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
    """扫描 traces 目录：返回 (token 记录列表, session_id -> trace 时间/权重 索引)。

    兼容两种 trace 格式：
      - 旧版 trace_<id>.json（单对象 {trace, spans}，模型/token 在 trace.modelInfo）
      - 新版 trace_<id>.jsonl（事件流；模型/token 在 generation span 的 toolOutput）
    token 记录用于 Token 指标；session 索引用于把会话积分按活跃时间摊分。
    """
    if not os.path.isdir(traces_dir):
        print("WARN: traces 目录不存在:", traces_dir)
        return [], {}
    files = glob.glob(os.path.join(traces_dir, "*", "*.json"))
    files += glob.glob(os.path.join(traces_dir, "*", "*.jsonl"))
    token_records = []
    by_session = {}
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
            token_records.append({
                "ts": g_ts, "d": g_dd, "h": g_hh, "model": g["model"],
                "value": round(g["total"], 2),
                "input": round(g["inp"], 2),
                "output": round(g["outp"], 2),
                "cached": round(g["cached"], 2),
                "label": label,
            })
    token_records.sort(key=lambda x: x["ts"])
    return token_records, by_session


def build_html(credits, tokens, meta):
    data = {"meta": meta, "credits": credits, "tokens": tokens}
    data_json = json.dumps(data, ensure_ascii=False)

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
  const k=Object.keys(m).sort((a,b)=>m[b]-m[a])[0];
  return k?`${k}（${fmt(m[k],isPts)}）`:'-';
}

function renderSummary(){
  const cRecs=applyFilter(DATA.credits), tRecs=applyFilter(DATA.tokens);
  const cTot=cRecs.reduce((a,r)=>a+r.value,0), tTot=tRecs.reduce((a,r)=>a+r.value,0);
  document.getElementById('summary').innerHTML=`
    <div class="scard pts"><div class="st">WorkBuddy 积分（已消耗）</div><div class="sv">${fmt(cTot,true)}</div>
      <div class="ss">最高模型：${topModel(cRecs,true)} · ${cRecs.length} 条会话</div></div>
    <div class="scard tok"><div class="st">Token 消耗（输入+输出）</div><div class="sv">${fmt(tTot,false)}</div>
      <div class="ss">最高模型：${topModel(tRecs,false)} · ${tRecs.length} 条 trace</div></div>`;
}

function renderKpis(agg,isPts){
  const unit = metric==='credits' ? '积分' : 'tokens';
  const top = agg.models[0]||'-';
  const topVal = agg.models[0]?agg.modelTotals[agg.models[0]]:0;
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
  const cols = metric==='credits'
    ? [['ts','时间',''],['label','标题','title'],['model','模型',''],['value','积分','num']]
    : [['ts','时间',''],['model','模型',''],['input','输入','num'],['output','输出','num'],['cached','缓存','num'],['value','合计','num']];
  tbl.querySelector('thead').innerHTML='<tr>'+cols.map(c=>`<th data-k="${c[0]}" class="${c[2]}">${c[1]}</th>`).join('')+'</tr>';
  const rows=[...recs].sort((a,b)=>{let va=a[sortKey],vb=b[sortKey];
    if(typeof va==='number'||typeof vb==='number'){va=+va||0;vb=+vb||0;}
    if(va<vb)return -1*sortDir;if(va>vb)return 1*sortDir;return 0;});
  const body=rows.map(r=>'<tr>'+cols.map(c=>{
    const k=c[0];let v=r[k];
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
    ? '指标：WorkBuddy 积分（来自会话消耗记录）。每笔积分用 credit_json 键内的 UUIDv7 时间戳归入真实消费时刻，模型按该时刻匹配同会话最近的实际调用模型；旧格式/无 trace 的会话回退到 trace 加权或最后同步时刻。'
    : '指标：Token 消耗（来自运行 trace，输入+输出，缓存单列）。按 trace 开始时刻归入对应时间桶。')
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
  '口径说明：① WorkBuddy 积分 = 会话消耗记录(session_usage.credit_json)各项之和，单位积分(两位小数)；'
  +'② Token 消耗 = 运行 trace 的输入+输出 token，缓存 token 单列展示；'
  +  '③ 两者来源与粒度不同（积分按会话条目、Token 按 trace），仅供并列参考。本报表为本地只读分析，不修改原始数据。'
  +'④ 口径：积分时间来自 credit_json 键内 UUIDv7 时间戳（精确到条目）；旧格式随机ID/无 trace 会话回退 trace 加权或最后同步时刻，可能高估同步当天。Token 来自本地 traces（JSON/JSONL 双格式）。';

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
    tokens, by_session = scan_traces(traces_dir)
    credits = load_credits(db_path, by_session)
    print("积分记录:", len(credits), "合计:", round(sum(r['value'] for r in credits), 2))
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
