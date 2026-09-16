"""MCP 配置管理：文件存储 + 校验 + 掩码 + 连通性/工具清单测试 + 热重载。

设计（无 DB、无重启）：
  - 种子文件：config.MCP_CONFIG_PATH（宿主机手工维护、只读挂载）——作为初始/兜底配置，永不改写。
  - 托管文件：config.MCP_MANAGED_PATH（默认 <DATA_DIR>/mcp_servers.json，可写）——由 /mcp/servers
    接口维护，原子替换（temp + os.replace）并保留 .bak。
托管文件存在时以它为准；不存在时回落到种子文件；首次写入自动继承当前生效内容，不丢配置。

密钥处理：GET 一律返回掩码（****xxxx）；PUT 收到掩码值表示"保持不变"，由 merge_masked 还原为原值。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import config, logger
from .mcp_config import build_servers

MASK_PREFIX = "****"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TYPES = ("stdio", "streamable_http")
_SECRET_KEYS_HINT = ("key", "token", "secret", "password", "passwd", "auth", "cookie", "credential")


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def managed_path() -> Path:
    return Path(config.MCP_MANAGED_PATH)


def seed_path() -> Path:
    return Path(config.MCP_CONFIG_PATH)


def _read_json(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.log("error", "mcp_config_invalid", {"path": str(path), "error": repr(e)})
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def load_effective() -> tuple[list[dict[str, Any]], str]:
    """返回 (配置项列表, 来源)。来源：managed / seed / empty。"""
    mp = managed_path()
    if mp.is_file():
        return _read_json(mp), "managed"
    sp = seed_path()
    if sp.is_file():
        return _read_json(sp), "seed"
    return [], "empty"


def save_managed(items: list[dict[str, Any]]) -> dict[str, Any]:
    """原子写托管文件：先写同目录临时文件再 os.replace，保留 .bak。"""
    mp = managed_path()
    mp.parent.mkdir(parents=True, exist_ok=True)
    backup: str | None = None
    if mp.is_file():
        backup = str(mp.with_suffix(mp.suffix + ".bak"))
        try:
            Path(backup).write_text(mp.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as e:  # 备份失败不阻断保存
            logger.log("warn", "mcp_backup_failed", {"path": backup, "error": repr(e)})
            backup = None
    tmp = mp.with_suffix(mp.suffix + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, mp)
    return {"path": str(mp), "count": len(items), "backup": backup}


# --------------------------------------------------------------------------- #
# 掩码 / 还原
# --------------------------------------------------------------------------- #
def _is_secret_key(key: str) -> bool:
    k = (key or "").lower()
    return any(h in k for h in _SECRET_KEYS_HINT)


def mask_secret(value: str) -> str:
    v = str(value or "")
    if len(v) >= 12:
        return f"{MASK_PREFIX}{v[-4:]}"
    return MASK_PREFIX


def mask_item(item: dict[str, Any]) -> dict[str, Any]:
    """返回掩码后的副本：env/headers 中的敏感值打码（其余原样，便于页面识别目标地址）。"""
    out = dict(item)
    for field in ("env", "headers"):
        kv = out.get(field)
        if isinstance(kv, dict):
            out[field] = {k: (mask_secret(v) if _is_secret_key(k) else v) for k, v in kv.items()}
    return out


def mask_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [mask_item(i) for i in items]


def _restore_secret(new: str, old: str | None) -> str | None:
    """掩码值 → 还原为原值；掩码但原值缺失 → 丢弃（避免把掩码存进去）。"""
    s = str(new or "")
    if s.startswith(MASK_PREFIX):
        return old if old is not None else None
    return new


def merge_masked(new_items: list[dict[str, Any]], old_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把前端回传的掩码密钥还原成已存原值（按 name 匹配）。"""
    old_by_name = {str(i.get("name") or ""): i for i in old_items}
    merged: list[dict[str, Any]] = []
    for item in new_items:
        cur = dict(item)
        old = old_by_name.get(str(cur.get("name") or "")) or {}
        for field in ("env", "headers"):
            kv = cur.get(field)
            if not isinstance(kv, dict):
                continue
            old_kv = old.get(field) if isinstance(old.get(field), dict) else {}
            fixed: dict[str, str] = {}
            for k, v in kv.items():
                val = _restore_secret(v, old_kv.get(k))
                if val is not None and str(val) != "":
                    fixed[str(k)] = str(val)
            cur[field] = fixed
        merged.append(cur)
    return merged


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    item = dict(item or {})
    item["name"] = str(item.get("name") or "").strip()
    item["type"] = str(item.get("type") or "").strip()
    if item["type"] == "stdio":
        item["command"] = str(item.get("command") or "").strip()
        item["args"] = [str(a) for a in item.get("args") or []]
        item["env"] = {str(k): str(v) for k, v in (item.get("env") or {}).items()}
        for k in ("url", "headers"):
            item.pop(k, None)
    elif item["type"] == "streamable_http":
        item["url"] = str(item.get("url") or "").strip()
        item["headers"] = {str(k): str(v) for k, v in (item.get("headers") or {}).items()}
        for k in ("command", "args", "env"):
            item.pop(k, None)
    return item


def validate_item(item: dict[str, Any]) -> list[str]:
    """单项校验，返回错误信息列表（空 = 合法）。"""
    errs: list[str] = []
    name = item.get("name")
    typ = item.get("type")
    if not name or not _NAME_RE.match(str(name)):
        errs.append("name 必填，且只允许字母数字开头、含 . _ - 的 1-64 位字符")
    if typ not in _TYPES:
        errs.append(f"type 必须是 {'/'.join(_TYPES)} 之一")
    if typ == "stdio":
        if not item.get("command"):
            errs.append("stdio 类型必须提供 command")
        if item.get("args") and not isinstance(item["args"], list):
            errs.append("args 必须是字符串数组")
        if item.get("env") and not isinstance(item["env"], dict):
            errs.append("env 必须是字符串键值对象")
    elif typ == "streamable_http":
        url = str(item.get("url") or "")
        if not url:
            errs.append("streamable_http 类型必须提供 url")
        elif not re.match(r"^https?://", url):
            errs.append("url 必须以 http:// 或 https:// 开头")
        if item.get("headers") and not isinstance(item["headers"], dict):
            errs.append("headers 必须是字符串键值对象")
    return errs


def validate_items(items: list[dict[str, Any]]) -> list[str]:
    """整体校验，返回 【name】错误 形式的列表（空 = 合法）。"""
    errs: list[str] = []
    if not isinstance(items, list):
        return ["servers 必须是数组"]
    seen: set[str] = set()
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            errs.append(f"第 {idx + 1} 项不是对象")
            continue
        name = str(item.get("name") or f"#{idx + 1}")
        if name in seen:
            errs.append(f"【{name}】name 重复")
        seen.add(name)
        errs.extend(f"【{name}】{e}" for e in validate_item(item))
    return errs


# --------------------------------------------------------------------------- #
# 连通性 / 工具清单
# --------------------------------------------------------------------------- #
def _tool_dict(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "input_schema", None) or {}
    params: list[str] = []
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            params = sorted(str(k) for k in props)
    desc = (getattr(tool, "description", "") or "").strip().replace("\n", " ")
    return {"name": str(getattr(tool, "name", "")), "description": desc[:300], "params": params}


async def test_item(item: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
    """连接单个 MCP server 并取回工具清单（页面"测试"按钮用）。绝不抛异常。"""
    t0 = time.monotonic()
    name = str(item.get("name") or "mcp")
    transport = str(item.get("type") or "")
    timeout = float(timeout_s or config.MCP_TEST_TIMEOUT_S)

    def _fail(msg: str) -> dict[str, Any]:
        return {
            "ok": False,
            "name": name,
            "transport": transport,
            "tool_count": 0,
            "tools": [],
            "error": msg,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
        }

    servers = build_servers([item])
    if not servers:
        return _fail("配置无效：type 或必填字段不合法（stdio 需 command；streamable_http 需 url）")
    server = servers[0]
    try:
        async with server:
            tools = await asyncio.wait_for(server.list_tools(), timeout=timeout)
    except asyncio.TimeoutError:
        return _fail(f"连接超时（>{timeout:g}s）")
    except Exception as e:  # noqa: BLE001 - 任何连接/协议错误都要回显给页面
        detail = str(e).strip().replace("\n", " ")[:400]
        return _fail(f"{type(e).__name__}: {detail}" if detail else type(e).__name__)
    tool_list = [_tool_dict(t) for t in tools]
    return {
        "ok": True,
        "name": name,
        "transport": transport,
        "tool_count": len(tool_list),
        "tools": tool_list,
        "error": None,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }
