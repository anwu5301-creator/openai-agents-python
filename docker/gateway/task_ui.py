"""自建 Task 进度查看的 Web 页面（纯内嵌 HTML/CSS/JS，无外部构建）。

提供页面：
  GET /ui/tasks                任务进度列表：状态彩色徽章、可按状态筛选、自动轮询刷新、
                                行内取消按钮。
  GET /ui/tasks/{task_id}      单任务详情：状态、参数、输出/错误、trace 跳转、取消按钮。

数据来源：同服务的 JSON 接口 GET /tasks（列表）、GET /tasks/{id}（详情）、
POST /tasks/{id}/cancel（取消）。
"""

from __future__ import annotations

import html
import json as _json
from typing import Any

_CSS = """
body { font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; background: #0f1419; color: #e6e6e6; margin: 0; padding: 20px 24px; }
h1 { font-size: 20px; font-weight: 600; margin: 0; }
a { color: #4da3ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.hint { color: #8b8b8b; font-size: 13px; }
.topbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.back { font-size: 13px; }
.card { background: #161b22; border: 1px solid #2a2f36; border-radius: 10px; padding: 16px 18px; margin-bottom: 16px; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid #22272f; vertical-align: middle; }
th { color: #a0a0a0; font-weight: 500; }
tr:hover td { background: #161b22; }
/* 状态徽章 */
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
.badge.pending  { background: #3a3a3a; color: #d0d0d0; }
.badge.running  { background: #153e61; color: #7cc0ff; }
.badge.running  { animation: pulse 1.6s ease-in-out infinite; }
.badge.succeeded{ background: #123f24; color: #5ee08a; }
.badge.failed   { background: #56161f; color: #ff8099; }
.badge.cancelled{ background: #3a2d12; color: #e6c35c; }
@keyframes pulse { 0%,100%{opacity:1;} 50%{opacity:.55;} }
/* 筛选 */
.filters { display: flex; gap: 8px; margin-bottom: 14px; flex-wrap: wrap; }
.chip { border:1px solid #2a2f36; background:#161b22; color:#c9ced6; border-radius:999px; padding:5px 14px; font-size:12px; cursor:pointer; }
.chip.active { background:#2c3a4d; border-color:#4da3ff; color:#7fc1ff; }
.chip[data-v=all]:hover{background:#1c222c;}
/* 按钮 */
.btn { display:inline-block; border:none; border-radius:6px; padding:6px 14px; font-size:12px; cursor:pointer; color:#fff; background:#2a3542; }
.btn:hover{background:#38485a;}
.btn.danger{background:#5c1520; color:#ffb3bf;}
.btn.danger:hover{background:#7a1c2b;}
.btn:disabled{opacity:.5; cursor:not-allowed;}
/* 详情 */
.kv { margin: 8px 0; }
.kv b { color:#c9a0ff; font-size:12px; font-family:monospace; display:block; margin-bottom:2px; }
pre { background:#0b0e13; border:1px solid #2a2f36; border-radius:6px; padding:10px; overflow:auto; font-size:12px; line-height:1.5; color:#d9e0e8; margin:0; white-space:pre-wrap; word-break:break-word; }
.meta { font-family:monospace; color:#5a6472; font-size:11px; }
.row-actions { display:flex; gap:8px; }
.statusbar { display:flex; align-items:center; gap:10px; margin-bottom:14px; }
.refresh { font-size:12px; color:#8b8b8b; }
"""

_PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>{css}</style></head>
<body>{body}</body></html>"""

_STATUS_LABEL = {
    "pending": "排队中",
    "running": "执行中",
    "succeeded": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}


def _page(title: str, body: str) -> str:
    return _PAGE.format(title=title, css=_CSS, body=body)


def _badge(status: str) -> str:
    return f'<span class="badge {html.escape(status)}">{html.escape(_STATUS_LABEL.get(status, status))}</span>'


def render_tasks_html(
    items: list[dict[str, Any]],
    active_status: str | None,
    limit: int,
) -> str:
    """任务进度列表页。"""
    if items:
        rows_html = ""
        for t in items:
            timer = ""
            if t.get("runs_ms") is not None:
                timer = f'<span class="meta">{t["runs_ms"]} ms</span>'
            created = _fmt_age(t.get("created_at"))
            trace_link = ""
            if t.get("trace_id"):
                trace_link = (
                    '<a class="meta" href="/ui/' + html.escape(t["trace_id"]) + '">trace</a>'
                )
            cancel_cell = ""
            if t.get("status") in ("pending", "running"):
                cancel_cell = (
                    f'<button class="btn danger cancel-btn" data-id="{html.escape(t["task_id"])}">取消</button>'
                )
            rows_html += (
                "<tr>"
                f'<td><a href="/ui/tasks/{html.escape(t["task_id"])}">{html.escape(t["task_id"])}</a></td>'
                f"<td>{_badge(t['status'])}</td>"
                f'<td>{html.escape(str(t.get("agent_name") or ""))}</td>'
                + f"<td>{created}</td>"
                + f"<td>{timer}</td>"
                + f"<td>{trace_link}</td>"
                + f"<td>{cancel_cell}</td>"
                + "</tr>"
            )
    else:
        rows_html = '<tr><td colspan="7" style="text-align:center;color:#888;padding:24px">暂无任务{filter_hint}</td></tr>'.format(
            filter_hint="（当前筛选下）" if active_status else "，提交一个任务后刷新"
        )

    filter_chips = ['<button class="chip" data-v="all" onclick="location.href=\'/ui/tasks\'">全部</button>']
    for st in ("pending", "running", "succeeded", "failed", "cancelled"):
        act = " active" if active_status == st else ""
        filter_chips.append(
            f'<button class="chip{act}" data-v="{st}" onclick="location.href=\'/ui/tasks?status={st}\'">{_STATUS_LABEL[st]}</button>'
        )

    body = f"""
    <div class="topbar">
      <a class="back" href="/ui">← Trace</a>
      <h1>任务进度</h1>
      <span class="hint">{limit} 条 / 最近优先</span>
    </div>
    <div class="filters">{''.join(filter_chips)}</div>
    <div class="card" style="padding:8px;">
      <table>
        <thead><tr><th>task_id</th><th>状态</th><th>agent</th><th>提交时间</th><th>耗时</th><th></th><th></th></tr></thead>
        <tbody>{rows_html}</tbody>
      </table>
    </div>
    <div class="statusbar">
      <span class="refresh" id="updated"></span>
      <button class="btn" onclick="location.reload()">刷新</button>
    </div>
    <script>
      // 行内取消：调用取消接口后刷新
      document.querySelectorAll('.cancel-btn').forEach(b => {{
        b.addEventListener('click', async () => {{
          const id = b.dataset.id;
          b.disabled = true; b.textContent = '取消中…';
          try {{
            await fetch('/tasks/' + encodeURIComponent(id) + '/cancel', {{ method: 'POST' }});
          }} catch(e) {{ console.error(e); }}
          setTimeout(() => location.reload(), 600);
        }});
      }});
      // 自动轮询：每 5 秒刷新一次（用于"执行中"任务的进度推进）
      const upd = document.getElementById('updated');
      upd.textContent = '上次更新: ' + new Date().toLocaleTimeString();
      const filter = {json_dumps(active_status)};
      const q = filter ? ('?status=' + encodeURIComponent(filter)) : '';
      setInterval(() => {{
        fetch('/ui/tasks' + q, {{ method: 'GET', headers: {{'Accept':'text/html'}} }})
          .then(r => r.text())
          .then(t => {{ if (t) {{ document.body.innerHTML = (new DOMParser().parseFromString(t,'text/html')).body.innerHTML; upd.textContent='上次更新: '+new Date().toLocaleTimeString(); }} }})
          .catch(()=>{{}});
      }}, 5000);
    </script>
    """
    return _page("任务进度", body)


def render_task_detail_html(task: dict[str, Any]) -> str:
    """单任务详情页。task 为 GET /tasks/{id} 返回的 dict。"""
    tid = task["task_id"]
    status = task.get("status")
    st = f'<div class="card" style="padding:10px 18px;margin:0 auto 16px;max-width:900px"><span class="hint">状态：</span>{_badge(status)}<span class="meta" style="margin-left:12px">{html.escape(tid)}</span></div>'

    blocks = [st]

    # 基本信息
    info = {
        "agent_name": task.get("agent_name"),
        "runs_ms": f'{task.get("runs_ms")} ms' if task.get("runs_ms") is not None else None,
        "trace_id": task.get("trace_id"),
    }
    info_html = ""
    for k, v in info.items():
        if v is None:
            continue
        if k == "trace_id" and v:
            info_html += f'<div class="kv"><b>{k}</b><pre><a href="/ui/{html.escape(v)}">打开 trace 详情 →</a></pre></div>'
        else:
            info_html += f'<div class="kv"><b>{k}</b><pre>{html.escape(v)}</pre></div>'
    if info_html:
        blocks.append(f'<div class="card"><b>基本信息</b>{info_html}</div>')

    # 输出 / 错误
    if task.get("output_text"):
        blocks.append(f'<div class="card"><b>输出</b><div class="kv"><pre>{html.escape(task["output_text"])}</pre></div></div>')
    if task.get("error_detail"):
        blocks.append(f'<div class="card"><b style="color:#ff8099">错误</b><div class="kv"><pre>{html.escape(task["error_detail"])}</pre></div></div>')

    # 操作
    actions = []
    if status in ("pending", "running"):
        actions.append(f'<button class="btn danger" id="cancelBtn" data-id="{html.escape(tid)}">取消该任务</button>')
    actions.append('<button class="btn" onclick="location.reload()">刷新</button>')
    actions.append(f'<a class="btn" href="/ui/tasks">← 返回列表</a>')
    blocks.append(f'<div class="card" style="display:flex;gap:10px;align-items:center">{ "".join(actions) }</div>')

    # 自动轮询：running 任务每 5s 刷新
    auto = f"""
      <script>
      const cb = document.getElementById('cancelBtn');
      if (cb) cb.addEventListener('click', async () => {{
        cb.disabled=true; cb.textContent='取消中…';
        try {{ await fetch('/tasks/' + encodeURIComponent(cb.dataset.id) + '/cancel', {{method:'POST'}}); }}
        catch(e){{ console.error(e); }}
        setTimeout(()=>location.reload(), 700);
      }});
      const st = {json_dumps(status)};
      if (st === 'pending' || st === 'running') {{
        setInterval(()=>location.reload(), 5000);
      }}
      </script>
    """

    body = f"""
    <div class="topbar">
      <a class="back" href="/ui/tasks">← 任务列表</a>
      <h1>任务 <code>{html.escape(tid)}</code></h1>
    </div>
    {''.join(blocks)}
    {auto}
    """
    return _page(f"任务 {tid[:12]}…", body)


def _fmt_age(iso: str | None) -> str:
    """把 ISO 时间转成相对时间（xx 分钟前）。"""
    if not iso:
        return ""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:  # noqa: BLE001
        return html.escape(iso)
    if secs < 60:
        return "刚刚"
    if secs < 3600:
        return f"{int(secs/60)} 分钟前"
    if secs < 86400:
        return f"{int(secs/3600)} 小时前"
    return f"{int(secs/86400)} 天前"


# 复用 trace_ui 的 JSON 序列化（独立实现避免跨模块耦合）
def json_dumps(v: Any) -> str:
    import json as _json

    return _json.dumps(v, ensure_ascii=False)