"""把 openai-agents 自带的 Trace/Span 生命周期转接到「业务系统日志层」。

实现 TracingProcessor 接口，在 on_trace_end / on_span_end 时通过通用 logger.log 推送，
从而拿到结构化的任务轨迹（agent 回合、工具调用、生成、handoff 等），满足任务追踪需求。
仅做旁路转发；TraceProcessor 内部异常不会影响 agent 执行（processor_interface 要求吞错）。
"""

from __future__ import annotations

import time
from typing import Any

from agents.tracing import Span, Trace, TracingProcessor

from . import logger


class BusinessLogProcessor(TracingProcessor):
    """把每个 run 的 Trace/Span 摘要推送到业务系统日志层。"""

    def on_trace_start(self, trace: Trace) -> None:
        # 记录 trace 开始时间，结束时用于计算耗时。
        self._t0 = time.monotonic() if not hasattr(self, "_t0") else self._t0

    def on_span_start(self, span: Span) -> None:
        logger.log(
            "debug",
            "span_start",
            {"span_id": _sid(span), "span_type": type(span).__name__},
        )

    def on_span_end(self, span: Span) -> None:
        logger.log(
            "info",
            "span_end",
            {
                "span_id": _sid(span),
                "span_type": type(span).__name__,
                "name": getattr(span, "name", None),
                # 可扩展：在此组装该 span 的 input/output/error
            },
        )

    def on_trace_end(self, trace: Trace) -> None:
        runs_ms = _ms_since(getattr(self, "_t0", None))
        logger.log(
            "info",
            "agent_run_end",
            {
                "trace_id": getattr(trace, "trace_id", None),
                "workflow_name": getattr(trace, "name", None),
                "runs_ms": runs_ms,
                # 业务侧如需查看完整轨迹，可在此把 trace.spans 里关键字段序列化进 payload。
            },
        )
        self._t0 = None

    def shutdown(self) -> None:
        # 业务侧如需刷新队列，可在此触发。
        pass

    def force_flush(self) -> None:
        pass


def _sid(span: Any) -> Any:
    try:
        return span.span_id
    except Exception:  # noqa: BLE001
        return None


def _ms_since(t0: float | None) -> int | None:
    return int((time.monotonic() - t0) * 1000) if t0 is not None else None