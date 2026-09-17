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

import asyncio

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import config, logger, mcp_admin, models as m, task_ui, trace_ui
from .log_sink_http import default_log_sink
from .logger import set_log_sink
from .middleware import TaskContextMiddleware
from .runner import AgentConfig, TaskPool
from .trace_bridge import BusinessLogProcessor
from .trace_store import TraceStoreProcessor

app = FastAPI(title="openai-agents gateway", version="0.1.0")

# --- CORS（无鉴权，开放给业务系统） ---
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.add_middleware(TaskContextMiddleware)

_pool: TaskPool | None = None
_trace_store: TraceStoreProcessor | None = None
_agent_cfg: AgentConfig | None = None
_mcp_lock: asyncio.Lock | None = None


class SubmitRequest(BaseModel):
    input: str = Field(..., description="发给 agent 的用户输入")
    agent_name: str | None = Field(None, description="agent 名称（可选）")
    instructions: str | None = Field(None, description="自定义 system prompt（可选）")
    config: dict = Field(default_factory=dict, description="透传给 agent 的附加配置（示例见 runner）")


@app.on_event("startup")
async def startup() -> None:
    global _pool, _trace_store, _agent_cfg
    from tortoise import Tortoise

    # Tortoise 1.x 用 contextvar 持有连接上下文；FastAPI 的请求处理在不同 asyncio task 运行，
    # 需 _enable_global_fallback=True 让连接可跨 task 访问（否则报 "No TortoiseContext is active"）。
    await Tortoise.init(
        db_url=config.DB_URL,
        modules={"models": ["gateway.models"]},
        _enable_global_fallback=True,
    )
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
    # 自建 Trace 存储：收集完整 span 树落库，提供 GET /traces 查询（不依赖 OpenAI Viewer）。
    _trace_store = TraceStoreProcessor()
    add_trace_processor(_trace_store)

    # Skill 与 MCP 工具的全局组装（全局共享）：
    #  - skills: 扫描 SKILLS_DIR 生成 list/load/run_skill_script 工具。
    #  - mcp: 从 mcp_servers.json 构造 server 列表，每次 run 由 MCPServerManager 管理连接。
    from .skill_tool import refresh_skill_registry, build_skill_tools
    from .mcp_config import build_servers

    refresh_skill_registry()
    skill_tools = build_skill_tools()
    mcp_items, mcp_source = mcp_admin.load_effective()
    mcp_server_list = build_servers(mcp_items)
    _agent_cfg = AgentConfig(skill_tools=skill_tools, mcp_server_list=mcp_server_list)
    logger.log(
        "info",
        "agent_config_ready",
        {
            "skills": len(skill_tools),
            "mcp_servers": len(mcp_server_list),
            "mcp_source": mcp_source,
            "mcp_names": [str(i.get("name") or "") for i in mcp_items],
        },
    )

    _pool = TaskPool(agent_cfg=_agent_cfg, slots=config.TASK_POOL_SLOTS)
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


@app.get("/tasks")
async def list_tasks(status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
    """列出最近的任务（默认按提交时间倒序），支持按状态筛选。返回不含 output 明细的概要。"""
    q = m.TaskModel.all()
    if status:
        if status not in (m.TaskStatus.PENDING, m.TaskStatus.RUNNING,
                          m.TaskStatus.SUCCEEDED, m.TaskStatus.FAILED, m.TaskStatus.CANCELLED):
            raise HTTPException(status_code=400, detail=f"非法状态: {status}")
        q = q.filter(status=status)
    rows = await q.order_by("-created_at").limit(max(1, min(limit, 200))).offset(max(0, offset))
    items = [
        {
            "task_id": t.id,
            "status": t.status,
            "agent_name": t.agent_name,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "runs_ms": t.runs_ms,
            "trace_id": t.trace_id,
        }
        for t in rows
    ]
    return {"items": items, "count": len(items)}


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
        sdk_trace_id=(task.config_json or {}).get("sdk_trace_id"),
    )
    return result.to_dict()


@app.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str) -> dict:
    """取消任务：排队中直接摘除，执行中尽力中断（asyncio cancel）。

    返回 {task_id, cancelled: bool, detail: "queued"|"running"|"already_done"|"not_found"}。
    幂等：对已结束/已取消任务返回 cancelled=False + detail。
    """
    task = await m.TaskModel.get_or_none(id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 队列中/执行中 → 交由 TaskPool 处理；已完成的任务无需取消
    if task.status not in (m.TaskStatus.PENDING, m.TaskStatus.RUNNING):
        return {"task_id": task_id, "cancelled": False, "detail": "already_done", "status": task.status}

    source = await _pool.cancel(task_id) if _pool is not None else None
    if source is None:
        # 不在队列也不在执行（可能刚被 worker 抢走但状态未落库）：按已结束处理
        fresh = await m.TaskModel.get_or_none(id=task_id)
        st = fresh.status if fresh else task.status
        if st in (m.TaskStatus.SUCCEEDED, m.TaskStatus.FAILED, m.TaskStatus.CANCELLED):
            return {"task_id": task_id, "cancelled": False, "detail": "already_done", "status": st}
        # 兜底：尽力直接标记取消
        try:
            task.validate_transition(m.TaskStatus.CANCELLED)
            task.status = m.TaskStatus.CANCELLED
            await task.save()
            return {"task_id": task_id, "cancelled": True, "detail": "forced", "status": task.status}
        except m.IllegalStatusTransition:
            return {"task_id": task_id, "cancelled": False, "detail": "already_done", "status": task.status}

    return {"task_id": task_id, "cancelled": True, "detail": source, "status": m.TaskStatus.CANCELLED}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/skills")
async def list_skills_http() -> dict:
    """列出当前可用技能（name/description/scripts），供外部平台（WeKnora/data_supply）选型。"""
    from .skill_tool import list_skill_specs
    return {"data": list_skill_specs()}


@app.post("/skills/install")
async def install_skill_http(file: UploadFile = File(...), name: str | None = Form(None)) -> dict:
    """安装技能 ZIP：解压到 SKILLS_DIR 并热刷新注册表（供 WeKnora 技能管理调用）。

    请求：multipart/form-data，字段 file=<skill.zip>，可选 name=<覆盖技能名>。
    返回 {name, installed, skill_count_after, scripts}。
    """
    from .skill_tool import install_skill_zip
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        result = install_skill_zip(data, force_name=name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


@app.get("/skills/{skill_name}")
async def get_skill_detail_http(skill_name: str) -> dict:
    """查看技能详情：SKILL.md 全文 + 文件清单（供 WeKnora 技能管理查看）。"""
    from .skill_tool import get_skill_detail
    try:
        result = get_skill_detail(skill_name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return result


@app.delete("/skills/{skill_name}")
async def delete_skill_http(skill_name: str) -> dict:
    """删除已安装技能（从可写安装目录移除并热刷新注册表；只读预装目录内的技能不可删）。"""
    from .skill_tool import delete_skill
    try:
        result = delete_skill(skill_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


# --------------------------------------------------------------------------- #
# MCP 服务器配置管理（文件存储 + 热重载，供 WeKnora「MCP 管理」页调用）
#   GET    /mcp/servers              列出当前生效配置（env/headers 中的密钥已掩码）
#   PUT    /mcp/servers              整表替换 → 校验 → 原子落盘 → 热重载（不重启容器）
#   POST   /mcp/servers/test         连接单个 server 并回显工具清单（页面「测试」按钮）
#   DELETE /mcp/servers/{name}       删除单个 server 并热重载
# 鉴权：请求头 X-Internal-Token 需等于 MCP_ADMIN_TOKEN（未配置该 env 时不校验）。
# --------------------------------------------------------------------------- #
def _require_admin(request: Request) -> None:
    token = (config.MCP_ADMIN_TOKEN or "").strip()
    if not token:
        return
    if (request.headers.get("X-Internal-Token") or "").strip() != token:
        raise HTTPException(status_code=401, detail="X-Internal-Token 无效")


def _mcp_lock_obj() -> asyncio.Lock:
    global _mcp_lock
    if _mcp_lock is None:
        _mcp_lock = asyncio.Lock()
    return _mcp_lock


async def _reload_mcp_servers() -> dict:
    """按生效配置重建 MCP 服务器列表并热替换进 AgentConfig。

    每个任务在 runner 里读取 agent_cfg.mcp_server_list，MCP 连接是 per-run 的，
    因此替换该列表即可让新配置对后续任务生效，无需重启容器。
    """
    from .mcp_config import build_servers

    async with _mcp_lock_obj():
        items, source = mcp_admin.load_effective()
        servers = build_servers(items)
        if _agent_cfg is not None:
            _agent_cfg.mcp_server_list = servers
        names = [str(i.get("name") or "") for i in items]
        logger.log("info", "mcp_reloaded", {"source": source, "count": len(servers), "names": names})
        return {"source": source, "count": len(servers), "names": names}


@app.get("/mcp/servers")
async def list_mcp_servers() -> dict:
    """列出当前生效的 MCP server 配置（密钥掩码）+ 生效来源（managed/seed）。"""
    items, source = mcp_admin.load_effective()
    return {
        "data": mcp_admin.mask_items(items),
        "source": source,
        "count": len(items),
        "managed_path": str(mcp_admin.managed_path()),
        "seed_path": str(mcp_admin.seed_path()),
    }


class McpServersRequest(BaseModel):
    servers: list[dict] = Field(default_factory=list, description="MCP server 全量配置（覆盖式替换）")


@app.put("/mcp/servers")
async def put_mcp_servers(
    req: McpServersRequest,
    request: Request,
    verify: bool = False,
    force: bool = False,
    timeout: float | None = None,
) -> dict:
    """整表替换 MCP 配置：校验 → 原子落盘（留 .bak）→ 热重载。

    密钥字段回传掩码（****xxxx）表示"保持不变"，会自动还原为已存原值。
    verify=true 时逐个做连通性测试并回显工具清单。
    force=true 才允许用空列表清空（防空下发误清网关配置）。
    """
    _require_admin(request)
    current, _src = mcp_admin.load_effective()
    items = [mcp_admin.normalize_item(i) for i in req.servers]
    errors = mcp_admin.validate_items(items)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    if not items and current and not force:
        raise HTTPException(
            status_code=400,
            detail=(
                f"拒绝用空列表覆盖（当前生效 {len(current)} 个 server）。"
                "若确认要清空网关 MCP 配置，请加 ?force=true 重试。"
            ),
        )
    items = mcp_admin.merge_masked(items, current)
    saved = mcp_admin.save_managed(items)
    reload_result = await _reload_mcp_servers()
    result: dict = {"saved": saved, "reload": reload_result, "data": mcp_admin.mask_items(items)}
    if verify:
        result["tests"] = [await mcp_admin.test_item(i, timeout) for i in items]
    return result


@app.post("/mcp/servers/test")
async def test_mcp_server(request: Request, payload: dict, timeout: float | None = None) -> dict:
    """测试连通性：body 传 {"server": {...}}（页面表单，可未保存）或 {"name": "..."}（已保存项）。

    返回 {ok, name, transport, tool_count, tools:[{name,description,params}], error, elapsed_ms}。
    """
    _require_admin(request)
    payload = payload if isinstance(payload, dict) else {}
    item = payload.get("server")
    current, _src = mcp_admin.load_effective()
    if not item:
        name = str(payload.get("name") or "")
        item = next((i for i in current if str(i.get("name") or "") == name), None)
        if item is None:
            raise HTTPException(status_code=404, detail=f"未找到 MCP server: {name}")
    item = mcp_admin.normalize_item(item)
    errors = mcp_admin.validate_item(item)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    item = mcp_admin.merge_masked([item], current)[0]
    return await mcp_admin.test_item(item, timeout)


@app.delete("/mcp/servers/{server_name}")
async def delete_mcp_server(server_name: str, request: Request) -> dict:
    """删除单个 MCP server（写入托管配置）并热重载。"""
    _require_admin(request)
    current, source = mcp_admin.load_effective()
    kept = [i for i in current if str(i.get("name") or "") != server_name]
    if len(kept) == len(current):
        raise HTTPException(status_code=404, detail=f"未找到 MCP server: {server_name}")
    saved = mcp_admin.save_managed(kept)
    reload_result = await _reload_mcp_servers()
    return {"saved": saved, "reload": reload_result, "removed": server_name, "previous_source": source}


@app.get("/traces/{trace_id}")
async def get_trace(trace_id: str) -> dict:
    """返回一次 agent run 的完整追踪（trace + span 树），来源为 data/traces.jsonl。"""
    found = None
    if _trace_store is not None:
        for r in _trace_store.read_jsonl(limit=200):
            if r["trace_id"] == trace_id:
                found = r
                break
        # 兜底：内存 recent 里也找
        if found is None:
            for r in _trace_store.recent():
                if r["trace_id"] == trace_id:
                    found = r
                    break
    if found is None:
        raise HTTPException(status_code=404, detail="trace 不存在")
    return m.TraceResult(
        trace_id=found["trace_id"],
        name=found["name"],
        created_at=None,
        spans=found["spans"],
    ).to_dict()


@app.get("/traces")
async def list_traces(limit: int = 20, offset: int = 0) -> dict:
    """列出最近的 trace（按完成时间倒序），不含 span 明细，便于总览。"""
    rows = _trace_store.read_jsonl(limit=max(1, min(limit, 100)), offset=max(0, offset)) if _trace_store else []
    items = [
        {
            "trace_id": r["trace_id"],
            "name": r["name"],
            "span_count": len(r["spans"]),
        }
        for r in rows
    ]
    return {"items": items, "count": len(items)}


@app.get("/ui", response_class=HTMLResponse)
async def trace_ui_list() -> str:
    """Trace 展示页：最近 trace 列表（浏览器可视化）。"""
    rows = _trace_store.read_jsonl(limit=50) if _trace_store else []
    items = [
        {"trace_id": r["trace_id"], "name": r["name"], "span_count": len(r["spans"])}
        for r in rows
    ]
    return trace_ui.render_trace_list_html(items)


@app.get("/ui/tasks", response_class=HTMLResponse)
async def tasks_ui_list(status: str | None = None, limit: int = 200) -> str:
    """任务进度展示页：列表（状态彩色徽章、筛选、自动轮询、行内取消）。"""
    q = m.TaskModel.all()
    if status:
        if status not in (m.TaskStatus.PENDING, m.TaskStatus.RUNNING,
                          m.TaskStatus.SUCCEEDED, m.TaskStatus.FAILED, m.TaskStatus.CANCELLED):
            raise HTTPException(status_code=400, detail=f"非法状态: {status}")
        q = q.filter(status=status)
    rows = await q.order_by("-created_at").limit(max(1, min(limit, 500)))
    items = [
        {
            "task_id": t.id,
            "status": t.status,
            "agent_name": t.agent_name,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "runs_ms": t.runs_ms,
            "trace_id": t.trace_id,
        }
        for t in rows
    ]
    return task_ui.render_tasks_html(items, active_status=status, limit=limit)


@app.get("/ui/tasks/{task_id}", response_class=HTMLResponse)
async def task_ui_detail(task_id: str) -> str:
    """任务详情页：状态、输出/错误、trace 跳转、取消按钮。"""
    task = await m.TaskModel.get_or_none(id=task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    detail = m.TaskResult(
        task_id=task.id,
        status=task.status,
        output_text=task.output_text,
        error_detail=task.error_detail,
        trace_id=task.trace_id,
        agent_name=task.agent_name,
        runs_ms=task.runs_ms,
    ).to_dict()
    return task_ui.render_task_detail_html(detail)


@app.get("/ui/{trace_id}", response_class=HTMLResponse)
async def trace_ui_detail(trace_id: str) -> str:
    """Trace 展示页：单个 run 的 span 树（可折叠展开）。"""
    found = None
    if _trace_store is not None:
        for r in _trace_store.read_jsonl(limit=500):
            if r["trace_id"] == trace_id:
                found = r
                break
        if found is None:
            for r in _trace_store.recent():
                if r["trace_id"] == trace_id:
                    found = r
                    break
    if found is None:
        raise HTTPException(status_code=404, detail="trace 不存在")
    return trace_ui.render_trace_detail_html(found["trace_id"], found["name"], found["spans"])
