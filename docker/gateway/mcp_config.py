"""MCP 工具配置：从配置文件(mcp_servers.json)构建标准 MCP 服务器列表。

skill 往往配套标准 MCP 工具（github/文件/浏览器等或自建业务 MCP）。这些是独立提供的标准
MCP 服务器，与 hermes 本体无关。通过 MCPServerManager 统一管理生命周期：只把连接成功的
server 暴露给 agent，单个服务器故障不影响整体。

配置文件 mcp_servers.json（JSON 数组），每项支持两种 type：
  {"type": "stdio", "name": "...", "command": "...", "args": [...], "env": {...}}
  {"type": "streamable_http", "name": "...", "url": "...", "headers": {...}}
缺失/损坏的配置项会被跳过并记日志，绝不抛断主流程。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.mcp import MCPServer, MCPServerStdio, MCPServerStreamableHttp

from . import config, logger


def load_mcp_config(path: str) -> list[dict[str, Any]]:
    """读取 mcp_servers.json；文件不存在/非法返回空列表。"""
    p = Path(path)
    if not p.is_file():
        logger.log("warn", "mcp_config_missing", {"path": path})
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        logger.log("error", "mcp_config_invalid", {"path": path, "error": repr(e)})
        return []


def build_servers(cfg_items: list[dict[str, Any]]) -> list[MCPServer]:
    """按配置构建 MCPServer 列表；跳过未知类型/缺失关键字段的项。"""
    servers: list[MCPServer] = []
    for item in cfg_items:
        try:
            typ = item.get("type")
            name = item.get("name") or "mcp"
            if typ == "stdio":
                servers.append(
                    MCPServerStdio(
                        {
                            "command": item["command"],
                            "args": item.get("args", []),
                            "env": item.get("env") or None,
                        },
                        name=name,
                        cache_tools_list=True,
                    )
                )
            elif typ == "streamable_http":
                servers.append(
                    MCPServerStreamableHttp(
                        {
                            "url": item["url"],
                            "headers": item.get("headers") or None,
                        },
                        name=name,
                        cache_tools_list=True,
                    )
                )
            else:
                logger.log("warn", "mcp_unknown_type", {"name": name, "type": typ})
        except (KeyError, TypeError) as e:
            logger.log("warn", "mcp_skip_invalid", {"item": str(item), "error": repr(e)})
    return servers


async def build_server_list() -> list[MCPServer]:
    """从配置构造服务器列表（供 app lifespan 的 MCPServerManager 使用）。"""
    return build_servers(load_mcp_config(config.MCP_CONFIG_PATH))