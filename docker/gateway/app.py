"""对外 Agent 调用网关（ASGI 服务，REST，异步回执，无鉴权）。

对外接口：
    POST /tasks                提交一个 agent 任务（异步，立即返回 task_id）
    GET  /tasks/{task_id}      查询任务状态与回执（轮询）
    GET  /health               存活检查

模型接入自建/第三方 OpenAI 兼容网关：见 config.py 的 LLM_* 环境变量。
日志：请求经 TaskContextMiddleware 注入任务上下文，agent run 的 Trace/Span 由
      BusinessLogProcessor 转接到通用日志层（业务系统自己对接，见 log_sink_http.py）。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import config, logger, models as m
from .middleware import TaskContextMiddleware
from .log_sink_http import default_log_sink
from .logger import set_log_sink
from .runner import TaskPool
from .trace_bridge import BusinessLogProcessor

app = FastAPI(title="openai-agents gateway", version="0.1.0")

# --- CORS（无鉴权，开放给业务系统） ---
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(TaskContextMiddleware)

_pool: TaskPool | None = None


class SubmitRequest(BaseModel):
    input: str = Field(..., description="发给 agent 的用户输入")
    agent_name: str | None = Field(None, description="agent 名称（可选）")
    instructions: str | None = Field(None, description="自定义 system prompt（可选）")
    config: dict = Field(default_factory=dict, description="透传给 agent 的附加配置（示例见 runner）")


@app.on_event("startup")
async def startup() -> None:
    global _pool
    from tortoise import Tortoise

    await Tortoise.init(db_url=config.DB_URL, modules={"models": ["gateway.models"]})
    await Tortoise.generate_schemas()

    # 接入自建/第三方 OpenAI 兼容网关（base_url / api_key / api）。
    from .llm_client import configure_default_llm
    configure_default_llm()

    # 业务系统日志层：默认 HTTP 上抛；业务侧可修改为自定义 Sink。
    set_log_sink(default_log_sink())

    # 接入仓库的 TracingProcessor，把 run 的 span 转接到业务日志层。
    # 注意：不要调用 set_tracing_disabled(True) —— 那会全局关闭 trace/span 生成，
    #      导致本 Processor 收不到事件。本地追踪保持开启；"不向 OpenAI 上报"由
    #      llm_client.configure_default_llm() 里的 use_for_tracing=False 保证。
    from agents.tracing import add_trace_processor
    add_trace_processor(BusinessLogProcessor())

    _pool = TaskPool(slots=config.TASK_POOL_SLOTS)
    await _pool.start()


@app.on_event("shutdown")
async def shutdown() -> None:
    if _pool:
        await _pool.stop()


@app.post("/tasks", status_code=202)
async def submit(req: SubmitRequest, request: Request) -> dict:
    """异步提交任务：仅入队，立即返回 task_id。业务侧用 GET /tasks/{id} 轮询或订阅日志。"""
    assert _pool is not None
    task_id = m.new_task_id()
    task = await m.TaskModel.create(
        id=task_id,
        status=m.TaskStatus.PENDING,
        agent_name=req.agent_name,
        instructions=req.instructions,
        input_text=req.input,
        config_json=req.config,
    )
    ok = await _pool.enqueue(task_id)
    if not ok:
        task.status = m.TaskStatus.FAILED
        task.error_detail = "queue-full"
        await task.save()
        raise HTTPException(status_code=429, detail="任务队列已满")
    # 返回回执；业务侧记录 trace_id/request-id 以便后续追踪。
    return {"task_id": task_id, "status": task.status}


@app.get("/tasks/{task_id}")
async def get_task(task_id: str) -> dict:
    task = await m.TaskModel.get_or_none(id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    result = m.TaskResult(
        task_id=task.id,
        status=task.status,
        output_text=task.output_text,
        error_detail=task.error_detail,
        trace_id=task.trace_id,
        agent_name=task.agent_name,
        runs_ms=task.runs_ms,
    )
    return result.to_dict()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}