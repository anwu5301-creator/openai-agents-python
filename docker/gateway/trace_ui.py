"""自建 Trace 的 Web 展示页（纯内嵌 HTML/CSS/JS，无外部构建）。

提供两个 HTML 页面：
  GET /ui               trace 列表（最近 N 条）
  GET /ui/{trace_id}    单次 run 的 span 树（按 parent_id 层级折叠展示）

页面通过同服务已有的 JSON 接口（/traces、/traces/{id}）取数，在浏览器渲染，
便于自建监控，无需依赖 OpenAI 官方 Trace Viewer。
"""

from __future__ import annotations

import html
import json
from typing import Any

_CSS = """
body { font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; background: #0f1419; color: #e6e6e6; margin: 0; padding: 20px 24px; }
h1 { font-size: 20px; font-weight: 600; }
a { color: #4da3ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.hint { color: #8b8b8b; font-size: 13px; }
.topbar { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.back { font-size: 13px; }
table { border-collapse: collapse; width: 100%; max-width: 900px; font-size: 13px; }
th, td { text-align: left; padding: 8px 12px; border-bottom: 1px solid #2a2f36; }
th { color: #a0a0a0; font-weight: 500; }
tr:hover td { background: #161b22; }
.head { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: 6px; background: #161b22; font-size: 13px; cursor: default; }
.head.has-children { cursor: pointer; }
.head:hover { background: #1c222c; }
.caret { width: 14px; color: #8b8b8b; display: inline-block; }
.chips { background: #24303f; color: #7fc1ff; border-radius: 4px; padding: 1px 6px; font-size: 11px; font-family: monospace; white-space: nowrap; }
.name { font-weight: 500; }
.dur { color: #8b8b8b; font-size: 12px; margin-left: auto; }
.meta { color: #5a6472; font-size: 11px; font-family: monospace; }
.body { margin-left: 24px; border-left: 1px solid #2a2f36; padding-left: 14px; }
.kv { margin: 6px 0; }
.kv b { color: #c9a0ff; font-size: 12px; font-family: monospace; display: block; margin-bottom: 2px; }
pre { background: #0b0e13; border: 1px solid #2a2f36; border-radius: 6px; padding: 10px; overflow: auto; font-size: 12px; line-height: 1.45; color: #d9e0e8; margin: 0 0 8px; }
"""

_PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>{css}</style></head>
<body>{body}</body></html>"""


def _page(title: str, body: str) -> str:
    return _PAGE.format(title=title, css=_CSS, body=body)


def render_trace_list_html(items: list[dict[str, Any]]) -> str:
    """trace 列表页 HTML。items 为 [{'trace_id','name','span_count'}, ...]"""
    if not items:
        rows = '<tr><td colspan="3" style="text-align:center;color:#888">暂无 trace，先提交一个任务再刷新</td></tr>'
    else:
        rows = "".join(
            "<tr>"
            f"<td><a href=\"/ui/{html.escape(r['trace_id'])}\">{html.escape(r['trace_id'])}</a></td>"
            f"<td>{html.escape(str(r.get('name') or ''))}</td>"
            f"<td>{int(r.get('span_count') or 0)}</td>"
            "</tr>"
            for r in items
        )
    body = f"""
    <h1>Agent Traces</h1>
    <p class="hint">最近 {len(items)} 条。点 trace_id 查看完整 span 树。</p>
    <table>
      <thead><tr><th>trace_id</th><th>name</th><th>spans</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p class="hint"><code>GET /traces</code> 返回同一数据的 JSON。</p>
    """
    return _page("Agent Traces", body)


def _type_label(sd: dict | None) -> str:
    if not sd:
        return "span"
    t = sd.get("type") or "unknown"
    if t == "custom" and isinstance(sd.get("data"), dict) and sd["data"].get("sdk_span_type"):
        return f"{t}:{sd['data']['sdk_span_type']}"
    return t


def _build_tree(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {
        s["id"]: dict(s, _children=[]) for s in spans if s.get("id")
    }
    roots: list[dict[str, Any]] = []
    for s in spans:
        node = by_id.get(s.get("id"))
        if not node:
            continue
        pid = s.get("parent_id")
        if pid and pid in by_id:
            by_id[pid]["_children"].append(node)
        else:
            roots.append(node)
    return roots


def _span_to_html(node: dict[str, Any], depth: int = 0) -> str:
    sd = node.get("span_data") or {}
    t = _type_label(sd)
    name = sd.get("name") or sd.get("model") or t
    has_children = bool(node.get("_children"))
    dur_ms = None
    if node.get("started_at") and node.get("ended_at"):
        try:
            from datetime import datetime as _dt
            dur_ms = int(
                (_dt.fromisoformat(node["ended_at"]) - _dt.fromisoformat(node["started_at"])).total_seconds() * 1000
            )
        except Exception:  # noqa: BLE001
            dur_ms = None

    kv = []
    if sd.get("input") is not None:
        kv.append(("input", sd["input"]))
    if sd.get("output") is not None:
        kv.append(("output", sd["output"]))
    for k in ("model", "usage", "tools", "handoffs", "error", "data"):
        if sd.get(k) is not None:
            kv.append((k, sd[k]))
    kv_html = ""
    for k, v in kv:
        kv_html += f'<div class="kv"><b>{k}</b><pre>{html.escape(json.dumps(v, ensure_ascii=False, indent=2))}</pre></div>'
    if dur_ms is not None:
        kv_html += f'<div class="kv"><b>duration</b><pre>{dur_ms} ms</pre></div>'

    children_html = ""
    if has_children:
        children_html = '<div class="children">' + "".join(_span_to_html(c, depth + 1) for c in node["_children"]) + "</div>"

    cls = "head has-children" if has_children else "head leaf"
    display = "display:none;" if has_children else ""
    dur_html = f'<span class="dur">{dur_ms} ms</span>' if dur_ms is not None else ""
    return (
        f'<div class="span">'
        f'<div class="{cls}" data-toggle>'
        f'<span class="caret">{"▸" if has_children else "·"}</span>'
        f'<span class="chips">{html.escape(t)}</span>'
        f'<span class="name">{html.escape(str(name))}</span>'
        + dur_html
        + f'<span class="meta">{html.escape(str(node.get("id") or ""))}</span>'
        + f"</div>"
        + f'<div class="body" style="{display}">{kv_html}{children_html}</div>'
        + f"</div>"
    )


def render_trace_detail_html(trace_id: str, name: str | None, spans: list[Any] | None) -> str:
    """单 trace 详情页 HTML（span 树，可折叠）。"""
    spans = spans or []
    if not spans:
        tree_html = '<p class="hint">该 trace 无 span 数据。</p>'
    else:
        roots = _build_tree(spans)
        tree_html = "".join(_span_to_html(r) for r in roots)
    body = f"""
    <div class="topbar">
      <a class="back" href="/ui">\u2190 返回列表</a>
      <h1>Trace <code>{html.escape(trace_id)}</code></h1>
      <span class="hint">{html.escape(name or '')}</span>
    </div>
    {tree_html}
    <script>
    document.querySelectorAll('.head.has-children').forEach(h => {{
      h.addEventListener('click', () => {{
        const body = h.nextElementSibling;
        const show = body.style.display !== 'none';
        body.style.display = show ? 'none' : '';
        h.querySelector('.caret').textContent = show ? '\\u25b8' : '\\u25be';
      }});
    }});
    </script>
    """
    return _page(f"Trace {trace_id}", body)