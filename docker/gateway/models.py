"""任务状态机与 Tortoise ORM 模型。

状态迁移（唯一合法路径）：
    pending -> running -> (succeeded | failed | cancelled)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from tortoise import fields
from tortoise.models import Model


class TaskStatus(str):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


VALID_TRANSITIONS: dict[str, set[str]] = {
    # PENDING -> RUNNING：被 worker 拾取执行；PENDING -> CANCELLED：排队中被取消（不执行）。
    TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.SUCCEEDED: set(),
    TaskStatus.FAILED: set(),
    TaskStatus.CANCELLED: set(),
}


class IllegalStatusTransition(Exception):
    pass


class TaskModel(Model):
    """一个 agent 调用任务及其回执信息。"""

    id = fields.CharField(max_length=64, pk=True)
    status = fields.CharField(max_length=16, default=TaskStatus.PENDING, index=True)
    created_at = fields.DatetimeField(auto_now_add=True, index=True)
    updated_at = fields.DatetimeField(auto_now=True)
    trace_id = fields.CharField(max_length=64, null=True, index=True, default=None)
    # request echo
    agent_name = fields.CharField(max_length=128, null=True, default=None)
    instructions = fields.TextField(null=True, default=None)
    input_text = fields.TextField(null=True, default=None)
    config_json = fields.JSONField(null=True, default=None)
    # 结果 / 错误
    output_text = fields.TextField(null=True, default=None)
    error_detail = fields.TextField(null=True, default=None)
    runs_ms = fields.BigIntField(null=True, default=None)

    class Meta:
        table = "agent_tasks"

    def validate_transition(self, new_status: str) -> None:
        if new_status == self.status:
            return
        allowed = VALID_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            raise IllegalStatusTransition(
                f"任务 {self.id}: 非法状态迁移 {self.status} -> {new_status}"
            )


@dataclass
class TraceResult:
    trace_id: str
    name: str | None
    created_at: str | None
    spans: list | None

    def to_dict(self) -> dict:
        return {
            "trace_id": self.trace_id,
            "name": self.name,
            "created_at": self.created_at,
            "spans": self.spans,
        }


class TaskResult:
    """异步回执的最终载荷（供 /tasks/{id} 与日志 push 复用）。"""

    task_id: str
    status: str
    output_text: str | None
    error_detail: str | None
    trace_id: str | None
    agent_name: str | None
    runs_ms: int | None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "output_text": self.output_text,
            "error_detail": self.error_detail,
            "trace_id": self.trace_id,
            "agent_name": self.agent_name,
            "runs_ms": self.runs_ms,
        }


def new_task_id() -> str:
    return uuid.uuid4().hex


def new_run_id() -> str:
    """agent run 的 trace_id。形状: gateway-run-<hex>"""
    return f"gateway-run-{uuid.uuid4().hex}"


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None