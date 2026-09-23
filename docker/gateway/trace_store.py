"""自建 Trace 存储：把 openai-agents 每次 run 的 Trace/Span 序列化落盘，供 HTTP 查询。

与 BusinessLogProcessor（推送业务日志）互补：这里把完整的 span 树（LLM 生成、
工具调用、handoff、error 等）追加到 data/traces.jsonl（JSONL，持久化），提供
GET /traces 查询，从而不依赖 OpenAI 官方 Trace Viewer 就能自建监控。

实现上是标准 TracingProcessor 的旁路收集：on_span_end 记录每个 span 的 export()
序列化结果，on_trace_end 组装整棵 trace 追加到 JSONL。所有异常一律吞掉，绝不影响
agent 执行。

实时日志（2026-09-22）：运行中的任务也能查到 trace —— 每个 span 结束即把
「当前已收集的 span 快照」更新到内存 _live（按 trace_id 隔离，天然支持多任务并发），
on_trace_end 时正式写 JSONL 并移除 live 快照。查询接口优先 jsonl 正式行，其次
recent 内存副本，最后 live 运行中快照（带 live=True 标记）。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from agents.tracing import Span, Trace, TracingProcessor

from . import config


def _serializable(value: Any) -> Any:
    """尽力把任意对象转成可 JSON 序列化的形式（防御未知类型）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(v) for v in value]
    return str(value)


class TraceStoreProcessor(TracingProcessor):
    """收集每次 run 的 Trace/Span，保存到 JSONL 持久化，供 /traces 查询。

    TRACE_STORE_ENABLED=False（或 config 默认）时可跳过存储，仅保留内存最近若干条。
    """

    def __init__(self) -> None:
        # 运行中 trace 的实时快照：trace_id -> {trace_id, name, t0, spans, live}
        # dict 按 trace_id 隔离 → 多任务并发互不串扰（修复旧版单例 _spans 的并发覆盖）。
        self._live: dict[str, dict[str, Any]] = {}
        self._enabled = config.TRACE_STORE_ENABLED if hasattr(config, "TRACE_STORE_ENABLED") else True
        # 最近 N 条 trace 的裸内存副本（TRACE_STORE_ENABLED 关闭时也能看最近执行）
        self._recent: list[dict[str, Any]] = []
        self._max_recent = 50

    # --- TracingProcessor 钩子 ---

    def on_trace_start(self, trace: Trace) -> None:
        try:
            tid: str = trace.trace_id
        except Exception:  # noqa: BLE001
            tid = "unknown"
        try:
            name = trace.name
        except Exception:  # noqa: BLE001
            name = None
        self._live[tid] = {
            "trace_id": tid,
            "name": name,
            "t0": time.monotonic(),
            "spans": [],
            "live": True,
        }

    def on_span_start(self, span: Span) -> None:
        # 无需在 start 时记录；end 时统一 export。
        pass

    def on_span_end(self, span: Span) -> None:
        try:
            exported = span.export()
        except Exception:  # noqa: BLE001
            exported = None
        if exported is None:
            return
        try:
            tid: str = span.trace_id
        except Exception:  # noqa: BLE001
            tid = ""
        live = self._live.get(tid)
        if live is not None:
            live.setdefault("spans", []).append(_serializable(exported))
            try:
                live["runs_ms"] = int((time.monotonic() - live["t0"]) * 1000)
            except Exception:  # noqa: BLE001
                pass

    def on_trace_end(self, trace: Trace) -> None:
        try:
            tid: str = trace.trace_id
        except Exception:  # noqa: BLE001
            tid = "unknown"
        live = self._live.pop(tid, None) or {}
        data = {
            "trace_id": tid,
            "name": live.get("name") or getattr(trace, "name", None),
            "runs_ms": live.get("runs_ms") or 0,
            "span_count": len(live.get("spans") or []),
            "spans": live.get("spans") or [],
        }
        self._keep_recent(data)
        # 追加到 JSONL 文件：同步、确定、可靠。同时也是自建 trace 的持久化存储。
        self._append_jsonl(data)

    # --- 实时快照（运行中查询） ---

    def get_live(self, trace_id: str) -> dict[str, Any] | None:
        """运行中的 trace 快照（含已完成 span 树），未运行/已完成返回 None。"""
        live = self._live.get(trace_id)
        if live is None:
            return None
        return {
            "trace_id": live["trace_id"],
            "name": live.get("name"),
            "runs_ms": live.get("runs_ms") or 0,
            "spans": live.get("spans") or [],
            "live": True,
        }

    def live_rows(self) -> list[dict[str, Any]]:
        """全部运行中 trace 的列表视图（span_count 实时）。"""
        rows: list[dict[str, Any]] = []
        for live in self._live.values():
            rows.append(
                {
                    "trace_id": live["trace_id"],
                    "name": live.get("name"),
                    "span_count": len(live.get("spans") or []),
                    "live": True,
                }
            )
        return rows

    # --- 持久化与查询 ---

    def _append_jsonl(self, data: dict[str, Any]) -> None:
        """把 trace 追加到 <data_dir>/traces.jsonl（一行一条）。失败静默。"""
        try:
            path = self.jsonl_path()
            line = {
                "trace_id": data["trace_id"],
                "name": data["name"],
                "runs_ms": data["runs_ms"],
                "spans": data["spans"],
            }
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def jsonl_path(self) -> str:
        return os.path.join(config.DATA_DIR, "traces.jsonl")

    def read_jsonl(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """从 JSONL 文件读取最近的 trace（行序 = 完成顺序）。"""
        rows: list[dict[str, Any]] = []
        try:
            path = self.jsonl_path()
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            pass
        # JSONL 是完成顺序（旧→新），倒序得到最近在前
        rows.reverse()
        end = offset + limit
        return rows[offset:end]

    def find_trace(self, trace_id: str) -> dict[str, Any] | None:
        """按 trace_id 在 JSONL 里定位单条 trace（懒扫描）。

        read_jsonl 会全文件 json.loads 每一行（文件膨胀到几百 MB 后单次查询
        数十秒，超过 WeKnora 侧 10s 的网关调用超时，表现为
        "context deadline exceeded"）。这里逐行做字符串粗筛：只有含目标
        trace_id 字面的行才 json.loads 并精确比对，命中即返回。
        """
        if not trace_id:
            return None
        needle = f'"trace_id": "{trace_id}"'
        try:
            path = self.jsonl_path()
            if not os.path.isfile(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if needle not in line:
                        continue
                    try:
                        row = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    if row.get("trace_id") == trace_id:
                        return row
        except OSError:
            pass
        return None

    # --- 内存最近记录 ---

    def _keep_recent(self, data: dict[str, Any]) -> None:
        row = {
            "trace_id": data["trace_id"],
            "name": data["name"],
            "runs_ms": data["runs_ms"],
            "spans": data["spans"],
        }
        self._recent.insert(0, row)
        if len(self._recent) > self._max_recent:
            self._recent.pop()

    def recent(self) -> list[dict[str, Any]]:
        return list(self._recent)

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass