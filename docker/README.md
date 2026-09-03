# openai-agents 网关（Docker）

基于 OpenAI Agents SDK 的对外 Agent 调用服务。对外提供 REST 接口（**异步回执、无鉴权**），并把每次 agent 运行的日志抽象成统一协议推送到**业务系统**做任务追踪。

## 目录结构

```
docker/
  Dockerfile              # 镜像（安装已发布 openai-agents 稳定版）
  docker-compose.yml      # 一键编排（含自建 LLM 网关占位）
  requirements.txt
  gateway/
    __init__.py           # cli 入口
    run.py                # 本地运行（uvicorn）
    app.py                # FastAPI 服务（主入口）
    middleware.py         # ASGI 中间件：每请求注入 task_id / trace_id
    config.py             # 全局配置（全部可用环境变量覆盖）
    models.py             # Tortoise ORM 模型 + 状态机
    runner.py             # TaskPool 异步队列 + 任务执行器
    logger.py             # 通用日志输出层接口（业务系统接入口）
    log_sink_http.py      # 参考实现：HTTP 上抛到业务系统
    trace_bridge.py       # 把仓库 Trace/Span 转接到日志层 (TracingProcessor)
    llm_client.py         # 接入自建/第三方 OpenAI 兼容网关（改 base_url）
```

## 对外接口（无鉴权）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/tasks` | 提交一个 agent 任务，**立即返回 `task_id`**（202，异步执行）|
| GET  | `/tasks/{task_id}` | 查询任务状态与回执（轮询）|
| GET  | `/health` | 存活检查 |

### 提交任务

```bash
curl -X POST http://localhost:8080/tasks \
  -H 'Content-Type: application/json' \
  -d '{"input": "帮我整理本周待办", "agent_name": "helper", "instructions": "你是助手", "config": {}}'

# 响应（立即返回，任务后台异步跑）
{"task_id": "abcd1234...", "status": "pending"}
```

### 查询回执（轮询）

```bash
curl http://localhost:8080/tasks/abcd1234...
# pending -> running -> succeeded/failed/cancelled
{"task_id":"...","status":"succeeded","output_text":"...","error_detail":null,
 "trace_id":"gateway-run-...","agent_name":"helper","runs_ms":1234}
```

> **异步回执机制**：`TaskPool` 用有界 asyncio 队列 + worker 池执行任务，提交仅入队不入队则 429。业务侧轮询 `/tasks/{id}`，或用下面的事件日志驱动。

## 模型接入（自建/第三方 OpenAI 兼容网关）

通过环境变量配置，职责见 `config.py`：

| 变量 | 默认 | 说明 |
|------|------|------|
| `LLM_BASE_URL` | `http://llm-gateway:8000/v1` | 网关地址（**改 base_url 场景**）|
| `LLM_API_KEY` | `unknown` | 网关 key |
| `LLM_MODEL` | `gpt-4o-mini` | 模型名 |
| `LLM_API` | `chat` | `chat`(Chat Completions) / `responses`(Responses) |

自建网关一般支持 `chat`（`/chat/completions`），这也更省兼容成本。

## 日志输出层（业务系统自己对接）

核心抽象见 `gateway/logger.py` —— 业务系统只需实现 **一个** `LogSink` 接口即可对接，不依赖本工程/仓库内部类型：

```python
from gateway.logger import LogSink, set_log_sink

class MyBusinessSink(LogSink):
    def emit(self, record: dict) -> None:   # record 已含 task_id / trace_id / event / level
        # 推送/入库/投递到业务系统，自行处理重试去重
        send_to_business_system(record)

set_log_sink(MyBusinessSink())   # 应用启动时注册一次
```

- **按任务追踪**：每个日志 record 都会带上当前调用的 `task_id`（请求头 `X-Task-Id` 注入）与 `trace_id`（agent run 的 trace）。
- **接入仓库追踪**：`trace_bridge.py` 实现官方 `TracingProcessor`，把每次 run 的 `Trace`/`Span`（agent 回合、工具调用、生成、handoff 等结构化轨迹）自动转接进日志层，满足"任务追踪"需求。
- **参考实现**：`log_sink_http.py` 用 `httpx` 上抛到业务系统 HTTP 端点（`LOG_SINK_URL`），失败吞掉、不重试（避免阻塞 agent）。业务侧重试/去重。
- **事件类型**：`agent_run_start` / `agent_run_end` / `span_start` / `span_end` / `agent_run_error` / `task_finished` 等。

## 构建与运行

方式一：docker compose（推荐）
```bash
cd docker
LLM_API_KEY=sk-xxx LLM_MODEL=gpt-4o-mini LOG_SINK_URL=http://your-business-system/ingest \
  docker compose up --build
```

方式二：手动 build
```bash
docker build -f docker/Dockerfile -t agent-gateway .
docker run -p 8080:8080 \
  -e LLM_BASE_URL=http://llm-gateway:8000/v1 -e LLM_API_KEY=sk-xxx \
  -e LOG_SINK_URL=http://your-business-system/ingest \
  agent-gateway
```

本地开发（非容器）：`cd docker && pip install -r requirements.txt && python -m gateway.run`

## 关键设计决策

- **用已发布稳定版**：镜像装 PyPI 的 `openai-agents`（当前 `0.22.0`），不 COPY 仓库 `main` 源码，避免带入未发布改动、更可维护。
- **无鉴权**：按你要求暂不做鉴权；上线正式环境前应在网关前置反向代理做 token 校验。
- **同步不阻塞**：提交只入队、立即返回，长任务后台执行，不占用请求线程。
- **日志旁路**：日志推送到业务系统为旁路，任何失败都不影响 agent 主流程与任务状态。
- **追踪不上报 OpenAI**：本地 tracing 保持开启（供 `BusinessLogProcessor` 取轨迹推你的业务系统），但通过 `set_default_openai_client(..., use_for_tracing=False)` **不把数据上报 OpenAI**。**切勿**设置 `OPENAI_AGENTS_DISABLE_TRACING=1`，那会同时关掉本地 Processor 事件。（注：控制的其实是执行代理的 key，不涉及 `DISABLE_OPENAI_TRACING` 这个变量。）