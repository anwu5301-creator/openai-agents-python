"""通用日志输出层接口。

对 agent 内部的日志做一次抽象，定义统一的推送协议，业务系统只要实现这一个接口即可对接。
既不依赖 Tortoise / FastAPI，也不依赖仓库内部类型，业务方可无缝接入。

业务系统需实现并注册一个 `LogSink`，并在每次调用时传入当前任务的 TraceID
（AgentRunner 通过当前 run 的 Trace 自动读取），从而实现"按任务追踪"。
"""

from __future__ import annotations

import abc
import contextvars
from typing import Any

# 当前任务上下文贯穿的 trace_id / task_id，由 ASGI 中间件注入（见 middleware.py）。
_current_task_id: contextvars.ContextVar[str | None] = ContextVar(
    "dsh_gateway_current_task_id", default=None
)
_current_trace_id: contextvars.ContextVar[str | None] = ContextVar(
    "dsh_gateway_current_trace_id", default=None
)


def set_current_task_id(task_id: str | None) -> None:
    _current_task_id.set(task_id)


def set_current_trace_id(trace_id: str | None) -> None:
    _current_trace_id.set(trace_id)


def get_current_task_id() -> str | None:
    return _current_task_id.get()


def get_current_trace_id() -> str | None:
    return _current_trace_id.get()


class LogSink(abc.ABC):
    """通用日志输出目标。业务系统实现此接口即完成对接，不依赖本工程/仓库内部类型。"""

    @abc.abstractmethod
    def emit(self, record: dict[str, Any]) -> None:
        """同步推送一条日志记录。

        实现方负责重试/去重/限流，但不应长时间阻塞调用线程太久
        （会在各自的异步任务/后台线程中被调用）。失败应被吞掉、不得抛给上层。
        """
        raise NotImplementedError


class VoidLogSink(LogSink):
    """空实现：丢弃所有日志。在未配置业务系统时使用，避免空指针。"""

    def emit(self, record: dict[str, Any]) -> None:
        pass


_sink: LogSink = VoidLogSink()


def set_log_sink(sink: LogSink) -> None:
    """注册业务系统日志输出实现（应用启动时调用一次）。"""
    global _sink
    _sink = sink


def get_log_sink() -> LogSink:
    return _sink


def log(level: str, event: str, payload: dict[str, Any] | None = None, *, raw: str | None = None) -> None:
    """向业务系统推送一条结构化日志。payload 业务侧自定，仅要求含 task_id/trace_id。"""
    if payload is None:
        payload = {}
    payload.setdefault("task_id", get_current_task_id())
    payload.setdefault("trace_id", get_current_trace_id())
    payload.setdefault("event", event)
    payload.setdefault("level", level)
    if raw is not None:
        payload.setdefault("raw", raw)
    try:
        _sink.emit(payload)
    except Exception:  # noqa: BLE001 - 日志推送失败绝不影响 agent 主流程
        pass