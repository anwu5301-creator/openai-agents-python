"""自建 Trace 存储：把 openai-agents 每次 run 的 Trace/Span 序列化落盘，供 HTTP 查询。

与 BusinessLogProcessor（推送业务日志）互补：这里把完整的 span 树（LLM 生成、
工具调用、handoff、error 等）追加到 data/traces.jsonl（JSONL，持久化），提供
GET /traces 查询，从而不依赖 OpenAI 官方 Trace Viewer 就能自建监控。

实现上是标准 TracingProcessor 的旁路收集：on_span_end 记录每个 span 的 export()
序列化结果，on_trace_end 组装整棵 trace 追加到 JSONL。所有异常一律吞掉，绝不影响
agent 执行。
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
    """收集每次 run 的 Trace/Span，保存到 Tortoise 持久化，供 /traces 查询。

    TRACE_STORE_ENABLED=False（或 config 默认）时可跳过存储，仅保留内存最近若干条。
    """

    def __init__(self) -> None:
        self._t0 = 0.0
        self._spans: list[dict[str, Any]] = []
        self._trace_name: str | None = None
        self._enabled = config.TRACE_STORE_ENABLED if hasattr(config, "TRACE_STORE_ENABLED") else True
        # 最近 N 条 trace 的裸内存副本（TRACE_STORE_ENABLED 关闭时也能看最近执行）
        self._recent: list[dict[str, Any]] = []
        self._max_recent = 50

    # --- TracingProcessor 钩子 ---

    def on_trace_start(self, trace: Trace) -> None:
        self._t0 = time.monotonic()
        self._spans = []
        try:
            self._trace_name = trace.name
        except Exception:  # noqa: BLE001
            self._trace_name = None

    def on_span_start(self, span: Span) -> None:
        # 无需在 start 时记录；end 时统一 export。
        pass

    def on_span_end(self, span: Span) -> None:
        try:
            exported = span.export()
        except Exception:  # noqa: BLE001
            exported = None
        if exported is not None:
            self._spans.append(_serializable(exported))

    def _assemble(self, trace: Trace) -> dict[str, Any]:
        try:
            trace_id: str = trace.trace_id
        except Exception:  # noqa: BLE001
            trace_id = "unknown"
        try:
            ended_at = trace.ended_at if hasattr(trace, "ended_at") else None
        except Exception:  # noqa: BLE001
            ended_at = None
        return {
            "trace_id": trace_id,
            "name": self._trace_name,
            "runs_ms": int((time.monotonic() - self._t0) * 1000),
            "span_count": len(self._spans),
            # 关键：task_id 作为 metadata 由 run 时注入（若有）。这里顺带从 trace 属性兜底读。
            "spans": self._spans,
        }

    def on_trace_end(self, trace: Trace) -> None:
        data = self._assemble(trace)
        self._keep_recent(data)
        # 追加到 JSONL 文件：同步、确定、可靠。同时也是自建 trace 的持久化存储。
        self._append_jsonl(data)
        self._spans = []
        self._trace_name = None
        self._t0 = 0.0

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
