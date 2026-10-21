"""A dependency-free browser dashboard for the local gateway.

This is not a production HMI. It exists so a first-time user can see what the
pipeline produced without reading JSON in a terminal.
"""

from __future__ import annotations

DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edge-Cloud 视觉测量上位机</title>
<style>
  :root {
    --bg: #0b1020; --panel: #121a2f; --line: #26324f; --text: #e8edf7;
    --muted: #8e9bb5; --ok: #3ddc97; --warn: #ffb454; --bad: #ff6b6b;
    --accent: #6ea8fe;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 14px/1.5 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }
  header { padding: 18px 24px; border-bottom: 1px solid var(--line);
           display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; }
  h1 { font-size: 20px; margin: 0; font-weight: 650; }
  .sub { color: var(--muted); font-size: 13px; }
  main { padding: 20px 24px 40px; max-width: 1400px; margin: 0 auto; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
           gap: 12px; margin-bottom: 20px; }
  .card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
          padding: 14px 16px; }
  .card .k { color: var(--muted); font-size: 12px; letter-spacing: .04em; }
  .card .v { font-size: 22px; font-weight: 650; margin-top: 4px; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 999px;
          font-size: 12px; border: 1px solid var(--line); }
  .ok { color: var(--ok); border-color: #1f6b4d; }
  .warn { color: var(--warn); border-color: #7a5620; }
  .bad { color: var(--bad); border-color: #7a2b2b; }
  table { width: 100%; border-collapse: collapse; background: var(--panel);
          border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
  th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line);
           vertical-align: top; }
  th { color: var(--muted); font-size: 12px; font-weight: 600; background: #0f1729; }
  tr:last-child td { border-bottom: 0; }
  td.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; }
  .empty { padding: 28px; color: var(--muted); text-align: center; }
  .bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin: 14px 0; }
  code { background: #0a0f1d; border: 1px solid var(--line); padding: 1px 5px;
         border-radius: 4px; color: #cfe1ff; }
  footer { color: var(--muted); margin-top: 18px; font-size: 12px; }
  a { color: var(--accent); }
</style>
</head>
<body>
<header>
  <h1>Edge-Cloud 视觉测量上位机</h1>
  <span class="sub">端侧结构化事件 · 云端复核 · Edge-Mem · 测量链</span>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="k">MQTT</div><div class="v" id="mqtt">—</div></div>
    <div class="card"><div class="k">已接收事件</div><div class="v" id="stored">—</div></div>
    <div class="card"><div class="k">复核队列</div><div class="v" id="queue">—</div></div>
    <div class="card"><div class="k">LLM</div><div class="v" id="llm">—</div></div>
    <div class="card"><div class="k">Edge-Mem 热/温/冷</div><div class="v" id="memory">—</div></div>
    <div class="card"><div class="k">刷新</div><div class="v" id="updated" style="font-size:14px">—</div></div>
  </div>

  <div class="bar">
    <span class="sub">显示最近 12 条事件；绿色 = accept，黄色 = review。端到端延迟是 capture→publish。</span>
    <span id="error" class="sub" style="color:var(--bad)"></span>
  </div>

  <table>
    <thead>
      <tr>
        <th>时间</th><th>设备</th><th>标签</th><th>置信度</th>
        <th>门控</th><th>端到端</th><th>云端决策</th>
      </tr>
    </thead>
    <tbody id="rows">
      <tr><td colspan="7" class="empty">等待事件…先运行 <code>python -m ecvision.edge --source synthetic --max-frames 20</code></td></tr>
    </tbody>
  </table>

  <footer>
    接口：<a href="/docs">/docs</a> · <a href="/metrics">/metrics</a> ·
    <a href="/api/v1/events">/api/v1/events</a>。
    这是本地演示页，不是生产 HMI，也不替代安全 PLC。
  </footer>
</main>
<script>
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({
  "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
}[c]));

function setPill(el, text, cls) {
  el.textContent = text;
  el.className = "v";
  el.innerHTML = '<span class="pill ' + cls + '">' + esc(text) + "</span>";
}

function decisionText(d) {
  if (!d) return '<span class="sub">待复核</span>';
  return esc(d.source) + " / " + esc(d.risk_level);
}

async function refresh() {
  try {
    const [h, events] = await Promise.all([
      fetch("/healthz", {cache: "no-store"}).then((r) => r.json()),
      fetch("/api/v1/events?limit=12", {cache: "no-store"}).then((r) => r.json()),
    ]);
    setPill($("mqtt"), h.mqtt_connected ? "connected" : "offline",
            h.mqtt_connected ? "ok" : "bad");
    $("stored").textContent = h.stored_events;
    $("queue").textContent = h.queue_depth;
    setPill($("llm"), h.llm_enabled ? "enabled" : "fallback",
            h.llm_enabled ? "ok" : "warn");
    const m = h.memory;
    $("memory").textContent = m ? (m.events + " / " + m.summaries + " / " + m.cold_events) : "n/a";
    $("updated").textContent = new Date().toLocaleTimeString();
    $("error").textContent = "";

    const rows = $("rows");
    if (!events.length) {
      rows.innerHTML = '<tr><td colspan="7" class="empty">等待事件…先运行 <code>python -m ecvision.edge --source synthetic --max-frames 20</code></td></tr>';
      return;
    }
    rows.innerHTML = events.map((r) => {
      const e = r.event;
      const s = e.sample;
      const gate = e.gate.decision === "accept" ? "ok" : "warn";
      const e2e = s && typeof s.end_to_end_ms === "number" ? s.end_to_end_ms.toFixed(1) + " ms" : "—";
      return "<tr>" +
        '<td class="mono">' + esc(new Date(e.ts).toLocaleTimeString()) + "</td>" +
        "<td>" + esc(e.device_id) + "</td>" +
        "<td>" + esc(e.inference.label) + "</td>" +
        '<td class="mono">' + (e.inference.confidence * 100).toFixed(1) + "%</td>" +
        '<td><span class="pill ' + gate + '">' + esc(e.gate.decision) + "</span></td>" +
        '<td class="mono">' + esc(e2e) + "</td>" +
        '<td class="mono">' + decisionText(r.decision) + "</td>" +
      "</tr>";
    }).join("");
  } catch (err) {
    $("error").textContent = "网关未启动或接口不可达：" + err;
  }
}
refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""
