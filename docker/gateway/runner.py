"""任务执行器：把 pending 任务真正跑成 agent run，并推进任务状态。

关键设计（异步回执 / 进度 push）：
- TaskPool 用有界 asyncio.Queue + worker 池消费待执行任务；提交立即入队返回 task_id。
- 并发上限由 queue 大小（有界）与 worker 数共同约束，队列满则 429。
- 状态迁移统一经 validate_transition，非法迁移抛错并记录。
"""

from __future__ import annotations

import asyncio
import time

from agents import Agent, Runner

from . import config, logger, models as m


async def run_task(task_id: str) -> None:
    """单任务执行：底层直接跑（时序见 courseware）。"""
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
    agent = Agent(
        name=task.agent_name or cfg.get("agent_name") or "gateway-agent",
        instructions=task.instructions or cfg.get("instructions") or "你是一个通用助手。",
        model=config.LLM_MODEL,
        tools=[],
    )

    t0 = time.monotonic()
    error_detail: str | None = None
    output_text: str | None = None
    try:
        # 这里可直接用 Runner.run_async；如需挂钩生命周期回调需走 AgentRunner.run() 起 run。
        result = await Runner.run_async(
            agent,
            input=task.input_text or "",
        )
        output_text = result.final_output
        logger.log("info", "agent_run_end", {"task_id": task_id, "runs_ms": _ms(t0)})
    except Exception as e:  # noqa: BLE001
        error_detail = repr(e)
        logger.log("error", "agent_run_error", {"task_id": task_id, "error": error_detail})
    finally:
        task.runs_ms = _ms(t0)
        if error_detail is not None:
            task.validate_transition(m.TaskStatus.FAILED)
            task.status = m.TaskStatus.FAILED
            task.error_detail = error_detail
        else:
            task.validate_transition(m.TaskStatus.SUCCEEDED)
            task.status = m.TaskStatus.SUCCEEDED
            task.output_text = output_text
        await task.save()

    # 进度/完成回执都经由日志层 push（业务侧通过 /tasks/{id} 或订阅事件获得）
    logger.log("info", "task_finished", {"task_id": task_id, "status": task.status})


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


class TaskPool:
    """有界队列 + worker 池执行 pending 任务，提供异步回执。"""

    def __init__(
        self,
        slots: int | None = None,
        queue_max: int | None = None,
    ) -> None:
        self.slots = slots or config.TASK_POOL_SLOTS
        self.queue_max = queue_max or config.TASK_QUEUE_MAX
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=self.queue_max)
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
        try:
            self._queue.put_nowait(task_id)
            return True
        except asyncio.QueueFull:
            return False

    async def _worker(self, idx: int) -> None:
        while True:
            task_id = await self._queue.get()
            try:
                await run_task(task_id)
            except Exception as e:  # noqa: BLE001
                logger.log("error", "worker_error", {"task_id": task_id, "error": repr(e)})
            finally:
                self._queue.task_done()