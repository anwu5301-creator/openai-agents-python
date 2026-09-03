"""接入自建/第三方 OpenAI 兼容网关（改 base_url 场景）。

根据 config 选择默认 OpenAI client：
- API=chat       : 用 openai.AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)
                    并 set_default_openai_client(...)，自建网关一般只需 chat/completions。
- API=responses  : 若网关支持 Responses API，同样可复用 AsyncOpenAI（默认 transport）。

用法：app startup 时调用 configure_default_llm()。
"""

from __future__ import annotations

from openai import AsyncOpenAI

from . import config


def build_async_client() -> AsyncOpenAI:
    return AsyncOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY)


def configure_default_llm() -> None:
    client = build_async_client()
    if config.LLM_API == "responses":
        from agents import set_default_openai_api
        set_default_openai_api("responses")
    else:
        from agents import set_default_openai_api
        set_default_openai_api("chat_completions")
    from agents import set_default_openai_client
    # use_for_tracing=False：不让网关 key 也用于上传追踪，追踪走我们自己的日志层。
    set_default_openai_client(client, use_for_tracing=False)