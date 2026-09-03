"""业务系统对接的参考实现：把通用日志 emT/protocol 转发到业务系统的 HTTP 端点。

这是「业务侧自己对接」的起点：
1. 工程/代理侧只需调用 `logger.log(...)`（见 logger.py 的接口）。
2. 业务侧在此实现自己的 `LogSink`（不一定引用本实现），并注册 `set_log_sink`。
3. 默认实现 `HttpLogSink` 用 httpx 异步上抛到业务系统端点；失败吞掉、不重试（见 config）。

业务系统只需提供 `POST {url}`，接收如下的 JSON body：
    {"task_id": "...", "trace_id": "gateway-run-...", "event": "agent_run_end",
     "level": "info", "runs_ms": 1234, "output_text": "..." , ...}
可自行持久化/索引，以实现任务维度追踪。
"""

from __future__ import annotations

from typing import Any

import httpx

from . import config
from .logger import LogSink


class HttpLogSink(LogSink):
    """把日志推送到业务系统的 HTTP 接口。仅同步推送,不重试（避免阻塞 agent 主线程）。"""

    def __init__(self, url: str, timeout_s: float | None = None) -> None:
        self._url = url
        self._timeout = timeout_s if timeout_s is not None else config.LOG_SINK_TIMEOUT_S

    def emit(self, record: dict[str, Any]) -> None:
        if not self._url:
            return
        try:
            httpx.post(self._url, json=record, timeout=self._timeout)
        except Exception:  # noqa: BLE001 - 推送失败不影响主流程，由业务侧自行重试/去重
            pass


def default_log_sink() -> LogSink:
    """根据配置构造默认 sink。业务侧可覆盖此函数（或直接在 app.startup 里 set_log_sink）注册自己的实现。"""
    url = config.DEFAULT_LOG_SINK_URL
    if url:
        return HttpLogSink(url)
    # 未配置业务系统端点时，返回 Noop（丢弃），保证流程不中断。
    return _noop()


class _Noop(LogSink):
    def emit(self, record: dict[str, Any]) -> None:
        pass


def _noop() -> LogSink:
    return _Noop()