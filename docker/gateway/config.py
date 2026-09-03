"""全局配置：数据库、模型端点、日志输出、任务队列参数。

所有值均可通过环境变量覆盖，供 Docker 容器注入，无需改动代码。
"""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# 服务监听
# --------------------------------------------------------------------------- #
HOST: str = os.environ.get("GATEWAY_HOST", "0.0.0.0")
PORT: int = _int("GATEWAY_PORT", 8080)

# --------------------------------------------------------------------------- #
# 数据库（任务回执与状态存储）
# --------------------------------------------------------------------------- #
DB_URL: str = os.environ.get("GATEWAY_DB_URL", "sqlite://./gateway.db")

# --------------------------------------------------------------------------- #
# LLM 端点（OpenAI 兼容网关）
#  - API=responses    : 走 OpenAI Responses API（OpenAIChatCompletionsModel 的响应格式，需网关支持）
#  - API=chat         : 走 Chat Completions（自建/第三方网关最常用）
# 默认 chat，因为 self-hosted / 第三方 OpenAI 兼容网关通常只实现 chat/completions 后端。
# --------------------------------------------------------------------------- #
LLM_BASE_URL: str = os.environ.get("LLM_BASE_URL", "http://llm-gateway:8000/v1")
LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "unknown")
LLM_MODEL: str = os.environ.get("LLM_MODEL", "gpt-4o-mini")
LLM_API: str = os.environ.get("LLM_API", "chat")

# 追踪策略：
#  - 本地追踪保持开启，以便 BusinessLogProcessor 把 Trace/Span 转接到业务日志层（任务追踪）。
#  - 不向 OpenAI 平台上报数据，由 llm_client.configure_default_llm() 的 use_for_tracing=False 保证。
#  - 不要用 OPENAI_AGENTS_DISABLE_TRACING=1 去关（会同时阻断本地 Processor 事件）。

# --------------------------------------------------------------------------- #
# 日志输出层（通用推送接口）
#  - 全部落到业务系统。base_url 由代理层 ASGI 中间件在每次请求注入（见 middleware.py），
#    业务方提供它自己的该端点即可，grep "业务侧自己对接" 见 ../../README.md#--
#  - 若未取到注入值，则回退到 DEFAULT_LOG_SINK_URL。
# --------------------------------------------------------------------------- #
DEFAULT_LOG_SINK_URL: str | None = os.environ.get("LOG_SINK_URL", None)
LOG_SINK_TIMEOUT_S: float = float(os.environ.get("LOG_SINK_TIMEOUT_S", "3"))
LOG_SINK_MAX_QUEUE: int = _int("LOG_SINK_MAX_QUEUE", 1000)
# 推送失败时不要重试（避免阻塞 agent 主线程）；由业务侧自行重试/去重。
LOG_SINK_RETRY: int = _int("LOG_SINK_RETRY", 0)

# --------------------------------------------------------------------------- #
# 任务队列（Tortoise + Polling Model，异步回执）
# --------------------------------------------------------------------------- #
TASK_POOL_SLOTS: int = _int("TASK_POOL_SLOTS", 8)           # gate 并发上限；可调
TASK_QUEUE_MAX: int = _int("TASK_QUEUE_MAX", 100)           # 等待队列上限
TASK_POLL_INTERVAL_S: float = float(os.environ.get("TASK_POLL_INTERVAL_S", "0.2"))
TASK_SWEEP_INTERVAL_S: float = float(os.environ.get("TASK_SWEEP_INTERVAL_S", "2"))
# 兜底：进程崩溃 / 容器重启后，把仍处于 processing 的任务标记为 failed，避免永久悬挂。
TASK_STALE_TIMEOUT_S: float = float(os.environ.get("TASK_STALE_TIMEOUT_S", "600"))