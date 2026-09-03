"""开发/本地运行入口：uvicorn 启动 gateway 服务。

生产环境用 Dockerfile 内的 CMD（见下）。
"""

from gateway import cli

if __name__ == "__main__":
    cli()