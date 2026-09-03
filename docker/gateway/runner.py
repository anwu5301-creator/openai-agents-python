"""任务执行器：把 pending 任务真正跑成 agent run，并推进任务状态。

关键设计（异步回执 / 进度 push）：
- TaskPool 用有界 asyncio.Queue + worker 池消费待执行任务；提交立即入队返回 task_id。
- 并发上限由 queue 大小（有界）与 worker 数共同约束，队列满则 429。
- 状态迁移统一经 validate_transition，非法迁移抛错并记录。
- Agent 组装：skills（list/load/run_skill_script 工具）与 MCP 服务器（active_servers）由
  启动时注入的 AgentConfig 提供，所有任务全局共享（全局作用域）。
- 取消：POST /tasks/{id}/cancel 可取消任务：
    * 排队中（未开始）→ 直接从待执行队列摘除，不执行。
    * 执行中 → 通过 asyncio.Task.cancel() 尽力中断；SDK 在 await 点抛出
      CancelledError，run_task 捕获后把任务置为 cancelled（而非 succeeded/failed）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from agents import Agent, Runner

from . import config, logger, models as m


@dataclass
class AgentConfig:
    """agent 组装所需的全局上下文：skill 工具 + 待连接的 MCP server 列表。"""

    skill_tools: list = field(default_factory=list)
    mcp_server_list: list = field(default_factory=list)


@dataclass
class _QueueItem:
    """待执行队列元素。cancelled=True 表示已排队但被取消，worker 取到后直接跳过。"""

    task_id: str
    cancelled: bool = False


async def run_task(task_id: str, agent_cfg: AgentConfig | None = None) -> None:
    """单任务执行：构造 agent（含 skill 工具与 MCP 服务器）并跑。

    支持取消：外部通过 asyncio.Task.cancel() 中断。若在 await Runner.run 处收到
    CancelledError，则将任务置为 cancelled 并结束（不会误标 succeeded/failed）。
    """
    task = await m.TaskModel.get_or_none(id=task_id)
    if task is None:
        return
    try:
        task.validate_transition(m.TaskStatus.RUNNING)
    except m.IllegalStatusTransition:
        return
    task.status = m.TaskStatus.RUNNING
    task.trace_id = m.new_run_id()
    await task.save()

    # 构造 agent（业务侧在 config_json 里自定义 instructions/agent_name）
    cfg = task.config_json or {}
    tools = list((agent_cfg.skill_tools if agent_cfg else []))
    instructions = task.instructions or cfg.get("instructions") or "你是一个通用助手。"
    if tools:
        instructions = (
            instructions
            + "\n\n你可使用以下技能工具（list_skills 查看可用技能）。"
        )

    async def _run_with_servers() -> "object":
        """在 MCP 服务器连接窗口内构造 agent 并运行。

        用 MCPServerManager 管理连接生命周期：只把连接成功的 server 挂给 agent，
        并在本次 run 结束后清理连接，避免跨 run 泄漏。
        """
        from agents.mcp import MCPServerManager

        server_list = agent_cfg.mcp_server_list if agent_cfg else []
        async with MCPServerManager(server_list) as manager:
            # mcp_servers 必须为 list（空列表也合法），不能传 None
            agent = Agent(
                name=task.agent_name or cfg.get("agent_name") or "gateway-agent",
                instructions=instructions,
                model=config.LLM_MODEL,
                tools=tools if tools else None,
                mcp_servers=list(manager.active_servers),
            )
            # 用 Runner.run()（异步）；注意 0.22.0 没有 run_async，异步入口是 run()
            return await Runner.run(agent, input=task.input_text or "")

    t0 = time.monotonic()
    error_detail: str | None = None
    output_text: str | None = None
    cancelled = False
    try:
        result = await _run_with_servers()
        output_text = result.final_output
        logger.log("info", "agent_run_end", {"task_id": task_id, "runs_ms": _ms(t0)})
    except asyncio.CancelledError:
        # 被 POST /tasks/{id}/cancel 中断
        cancelled = True
        logger.log("info", "agent_run_cancelled", {"task_id": task_id, "runs_ms": _ms(t0)})
    except Exception as e:  # noqa: BLE001
        error_detail = repr(e)
        logger.log("error", "agent_run_error", {"task_id": task_id, "error": error_detail})
    finally:
        task.runs_ms = _ms(t0)
        try:
            if cancelled:
                task.validate_transition(m.TaskStatus.CANCELLED)
                task.status = m.TaskStatus.CANCELLED
            elif error_detail is not None:
                task.validate_transition(m.TaskStatus.FAILED)
                task.status = m.TaskStatus.FAILED
                task.error_detail = error_detail
            else:
                task.validate_transition(m.TaskStatus.SUCCEEDED)
                task.status = m.TaskStatus.SUCCEEDED
                task.output_text = output_text
            await task.save()
        except m.IllegalStatusTransition:
            # 极端：已取消后又被 worker 迟到写入；仅记日志，不再覆盖状态。
            logger.log("warn", "task_status_conflict", {"task_id": task_id})

    # 进度/完成回执都经由日志层 push（业务侧通过 /tasks/{id} 或订阅事件获得）
    logger.log("info", "task_finished", {"task_id": task_id, "status": task.status})


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


class TaskPool:
    """有界队列 + worker 池执行 pending 任务，提供异步回执与取消。

    取消语义：
    - 队列中尚未开始的：把该 _QueueItem 标记 cancelled，worker 取到后跳过（不执行）。
    - 执行中：找到该 task 对应的 asyncio.Task 并 cancel()，run_task 收到
      CancelledError 后把任务置为 cancelled。
    """

    def __init__(
        self,
        agent_cfg: AgentConfig | None = None,
        slots: int | None = None,
        queue_max: int | None = None,
    ) -> None:
        self.agent_cfg = agent_cfg or AgentConfig()
        self.slots = slots or config.TASK_POOL_SLOTS
        self.queue_max = queue_max or config.TASK_QUEUE_MAX
        self._queue: asyncio.Queue[_QueueItem] = asyncio.Queue(maxsize=self.queue_max)
        # 队列内索引：task_id -> 元素（用于取消排队中的任务）
        self._pending: dict[str, _QueueItem] = {}
        # 正在执行的 task_id -> asyncio.Task 句柄（用于中断）
        self._running: dict[str, asyncio.Task] = {}
        self._workers: list[asyncio.Task] = []

    async def start(self) -> None:
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"agent-worker-{i}")
            for i in range(self.slots)
        ]

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def enqueue(self, task_id: str) -> bool:
        """异步提交；队列满返回 False（调用方可据此回 429）。"""
        item = _QueueItem(task_id=task_id)
        try:
            self._queue.put_nowait(item)
            self._pending[task_id] = item
            return True
        except asyncio.QueueFull:
            return False

    async def cancel(self, task_id: str) -> str | None:
        """取消任务。

        返回取消来源（用于日志/回执）：
        - "queued"   队列中且被移除/标记取消（未执行）
        - "running"  执行中被 asyncio 中断
        - None       未找到该任务
        """
        # 执行中 → 直接中断
        t = self._running.pop(task_id, None)
        if t is not None:
            t.cancel()
            return "running"
        # 排队中 → 标记取消并摘除
        item = self._pending.pop(task_id, None)
        if item is not None:
            item.cancelled = True
            # 立即把 DB 状态置为 cancelled，不必等 worker 消费该 item（避免延迟）。
            await self._mark_cancelled_if_pending(task_id)
            return "queued"
        return None

    async def list_pending(self) -> list[str]:
        """当前仍在队列中（未开始）的 task_id（便于外部监控）。"""
        return list(self._pending.keys())

    async def _worker(self, idx: int) -> None:
        while True:
            item = await self._queue.get()
            try:
                self._pending.pop(item.task_id, None)
                if item.cancelled:
                    # 入队后取消：不执行，直接把任务标记为 cancelled（若仍是 pending）
                    await self._mark_cancelled_if_pending(item.task_id)
                    continue
                # 登记执行句柄，供 cancel() 中断
                self._running[item.task_id] = asyncio.current_task()
                try:
                    await run_task(item.task_id, self.agent_cfg)
                except asyncio.CancelledError:
                    # worker 自身被 stop() 取消；任务状态已由 run_task 的 finally 处理
                    raise
                finally:
                    self._running.pop(item.task_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.log("error", "worker_error", {"task_id": item.task_id, "error": repr(e)})
            finally:
                self._queue.task_done()

    async def _mark_cancelled_if_pending(self, task_id: str) -> None:
        """把仍为 pending 状态的任务标记为 cancelled（用于"排队即取消"）。"""
        try:
            task = await m.TaskModel.get_or_none(id=task_id)
            if task is None:
                return
            task.validate_transition(m.TaskStatus.CANCELLED)
            task.status = m.TaskStatus.CANCELLED
            await task.save()
            logger.log("info", "task_finished", {"task_id": task_id, "status": task.status})
        except Exception:  # noqa: BLE001
            pass