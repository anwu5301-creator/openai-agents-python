"""openai-agents Docker 网关。对外提供 agent 调用 REST 接口（异步回执），并把运行日志抽象后推到业务系统。"""

__version__ = "0.1.0"


def cli() -> None:
    """运行入口: uvicorn gateway.app:app。"""
    import uvicorn

    from . import config

    uvicorn.run(
        "gateway.app:app",
        host=config.HOST,
        port=config.PORT,
        lifespan="on",
    )