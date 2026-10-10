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
from datetime import datetime, timezone, timedelta

HOME = os.path.expanduser("~")
DEFAULT_DB = os.path.join(HOME, ".workbuddy", "workbuddy.db")
DEFAULT_TRACES = os.path.join(HOME, ".workbuddy", "traces")
OUT_NAME = "usage-report.html"

# 本地时区 (UTC+8, 无夏令时)
TZ = timezone(timedelta(hours=8))


def norm_model(m):
    """归一化模型名：去掉 custom-local: 前缀，统一空值。"""
    m = (m or "").strip()
    if not m:
        return "未指定"
    if m.startswith("custom-local:"):
        m = m.split(":", 1)[1]
    return m


def load_credits(db_path, traces_by_session):
    """WorkBuddy 积分：每个会话 credit_json 求和，并按该会话的 trace 活跃时间摊到各小时。

    重要：session_usage.updated_at 只是"最后同步时刻"，无法反映真实消耗时间，
    会把整天的量压到一个小时。故改用该会话的运行 trace 时间（按 token 加权）把会话
    积分摊分到各小时；模型仍取 sessions.model（会话级口径不变）。
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
        model = norm_model(r["model"])
        title = (r["title"] or "(无标题)")
        traces = [t for t in (traces_by_session.get(r["sid"]) or []) if t.get("tok", 0) > 0]
        if traces:
            wsum = sum(t["tok"] for t in traces)
            for t in traces:
                share = credits * (t["tok"] / wsum) if wsum > 0 else credits / len(traces)
                out.append({
                    "ts": t["ts"], "d": t["d"], "h": t["h"],
                    "model": model, "value": round(share, 4), "label": title,
                })
        else:
            ts = (r["upd"] or 0) / 1000.0
            dt = datetime.fromtimestamp(ts, tz=TZ)
            out.append({
                "ts": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "d": dt.strftime("%Y-%m-%d"),
                "h": dt.strftime("%Y-%m-%d %H:00"),
                "model": model, "value": round(credits, 4), "label": title,
            })
    out.sort(key=lambda x: x["ts"])
    return out


def scan_traces(traces_dir):
    """扫描 traces 目录：返回 (token 记录列表, session_id -> trace 时间/权重 索引)。

    token 记录用于 Token 指标；session 索引用于把会话积分按活跃时间摊分。
    """
    if not os.path.isdir(traces_dir):
        print("WARN: traces 目录不存在:", traces_dir)
        return [], {}
    files = glob.glob(os.path.join(traces_dir, "*", "*.json"))
    token_records = []
    by_session = {}
    for f in files:
        try:
            j = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        t = j.get("trace") or {}
        started = t.get("startedAt")
        if not started:
            continue
        try:
            dt = datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(TZ)
        except Exception:
            continue
        mi = t.get("modelInfo") or {}
        inp = int(mi.get("totalInputTokens") or 0)
        outp = int(mi.get("totalOutputTokens") or 0)
        cached = int(mi.get("totalCachedTokens") or 0)
        total = int(t.get("totalTokens") or (inp + outp))
        ts = dt.strftime("%Y-%m-%d %H:%M:%S")
        hh = dt.strftime("%Y-%m-%d %H:00")
        dd = dt.strftime("%Y-%m-%d")

        sid = t.get("sessionId")
        if sid:
            by_session.setdefault(sid, []).append({"ts": ts, "d": dd, "h": hh, "tok": total})

        models = [norm_model(m) for m in (mi.get("models") or [])]
        if not models:
            continue
        n = len(models)  # 多模型 trace 极少，按模型数量均分
        label = (t.get("name") or "trace")
        for m in models:
            token_records.append({
                "ts": ts, "d": dd, "h": hh, "model": m,
                "value": round(total / n, 2),
                "input": round(inp / n, 2),
                "output": round(outp / n, 2),
                "cached": round(cached / n, 2),
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
  // 粒度：仅"今天"按小时，其余（全部/近7天/自定义）按天
  gran = (curFilter==='today') ? 'hour' : 'day';
  const recs = applyFilter(metricRecords());
  const agg = aggregate(recs);
  document.getElementById('chartTitle').textContent =
    (gran==='hour' ? '每小时用量' : '每天用量') + '（按模型堆叠）';
  document.getElementById('hint').textContent = (isPts
    ? '指标：WorkBuddy 积分（来自会话消耗记录）。按该会话各 trace 的 token 占比把会话积分摊分到对应时间；“每个模型”取会话主模型。无 trace 的会话回退到会话最后同步时刻。'
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
  +'③ 两者来源与粒度不同（积分按会话、Token 按 trace），仅供并列参考。本报表为本地只读分析，不修改原始数据。';

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
