"""ASGI 中间件：为每个请求注入任务上下文（task_id / trace_id），供 logger 贯穿使用。

- 从请求头 X-Task-Id 读取（提交任务时网关生成并返回，业务侧回传即可按任务追踪）。
- 若无则留空；agent run 内部真正使用的 trace_id 仍由仓库自身生成（见 run 事件）。
"""

from __future__ import annotations

from typing import Any


class TaskContextMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        from . import logger

        logger.set_current_task_id(headers.get("x-task-id"))
        logger.set_current_trace_id(headers.get("x-trace-id"))
        await self.app(scope, receive, send)