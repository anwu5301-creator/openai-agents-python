"""自建 Trace 存储：把 openai-agents run Trace/Span 持久化，供 HTTP 查询。

与 BusinessLogProcessor（推送业务日志）互补：这里把完整的 span 树（LLM 生成、
工具调用、handoff、error 等）持久化，提供 GET /traces 查询，从而不依赖
OpenAI Trace Viewer 就能自建监控。

存储模型（2026-09-30 改造，替代原 data/traces.jsonl 单文件追加）：
  - 摘要索引：TraceModel（SQLite gateway.db 的 agent_traces 表，Tortoise 自动建表）。
    一行 = 一条 trace 的摘要（trace_id PK、name、runs_ms、span_count、object_key、
    created_at）。列表查询与按 id 定位走主键/索引 O(1) 命中，不再全文件扫描。
  - 大对象：单条 trace 的完整 span 树（实测中位数 3KB、p99 达 21MB、最大 91MB）
    作为一个 JSON 对象存 MinIO，key 形如 traces/<YYYY>/<MM>/<DD>/<trace_id>.json。
    未配置 MINIO_ENDPOINT 时退化到本地文件 <data_dir>/traces/<key>，MinIO 缺失
    时功能不挂、部署环境无需改动也能跑。

  旧的 data/traces.jsonl 仅作只读兜底：查询不到 DB 行时（如迁移前遗留数据）
  回退到 JSONL 懒扫描 + 内存 recent/live，保证存量数据仍可查。

实现上是标准 TracingProcessor 的旁路收集：on_span_end 记录每个 span export()
序列化结果，on_trace_end 组装整棵 trace，写大对象 + upsert 摘要行。所有异常一律
吞掉，绝不影响 agent 执行。

实时日志（2026-09-22）：运行中的任务也能查到 trace span 结束即把
「当前已收集的 span 快照」更新到内存 _live（按 trace_id 隔离，天然支持多任务并发），
on_trace_end 时正式持久化并移除 live 快照。查询接口优先 DB+对象，其次 recent
内存副本，最后 live 运行中快照（带 live=True 标记）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from io import BytesIO
from datetime import datetime, timezone
from typing import Any

from agents.tracing import Span, Trace, TracingProcessor

from . import config


def _serializable(value: Any) -> Any:
    """尽力把任意对象转成可 JSON 序列化的形式（防御未知类型）。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable(v) for v in value]
    return str(value)


class TraceStoreProcessor(TracingProcessor):
    """收集每次 run Trace/Span，持久化（MinIO 对象 + SQLite 摘要），供 /traces 查询。

    TRACE_STORE_ENABLED=False（或 config 默认关闭）时跳过持久化，仅保留内存最近若干条。
    """

    def __init__(self) -> None:
        # 运行中 trace 的实时快照：trace_id -> {trace_id, name, t0, spans, live}
        # 按 trace_id 隔离，多任务并发互不串扰（修复旧版单例 _spans 的并发覆盖）。
        self._live: dict[str, dict[str, Any]] = {}
        self._enabled = getattr(config, "TRACE_STORE_ENABLED", True)
        # trace 的裸内存副本（TRACE_STORE_ENABLED 关闭时也能看最近执行）
        self._recent: list[dict[str, Any]] = []
        self._max_recent = 50
        # MinIO 客户端惰性持有；None 表示退化为本地文件。
        self._minio: Any = None
        self._minio_init_tried = False

    # ------------------------------------------------------------------ #
    # TracingProcessor
    # ------------------------------------------------------------------ #

    def on_trace_start(self, trace: Trace) -> None:
        try:
            tid: str = trace.trace_id
        except Exception:  # noqa: BLE001
            tid = "unknown"
        try:
            name = trace.name
        except Exception:  # noqa: BLE001
            name = None
        self._live[tid] = {
            "trace_id": tid,
            "name": name,
            "t0": time.monotonic(),
            "spans": [],
            "live": True,
        }

    def on_span_start(self, span: Span) -> None:
        # 无需在 start 时记录；end 时统一 export。
        pass

    def on_span_end(self, span: Span) -> None:
        try:
            exported = span.export()
        except Exception:  # noqa: BLE001
            exported = None
        if exported is None:
            return
        try:
            tid: str = span.trace_id
        except Exception:  # noqa: BLE001
            tid = ""
        live = self._live.get(tid)
        if live is not None:
            live.setdefault("spans", []).append(_serializable(exported))
            try:
                live["runs_ms"] = int((time.monotonic() - live["t0"]) * 1000)
            except Exception:  # noqa: BLE001
                pass

    def on_trace_end(self, trace: Trace) -> None:
        try:
            tid: str = trace.trace_id
        except Exception:  # noqa: BLE001
            tid = "unknown"
        live = self._live.pop(tid, None) or {}
        data = {
            "trace_id": tid,
            "name": live.get("name") or getattr(trace, "name", None),
            "runs_ms": live.get("runs_ms") or 0,
            "span_count": len(live.get("spans") or []),
            "spans": live.get("spans") or [],
        }
        self._keep_recent(data)
        # 持久化：写大对象 + upsert 摘要行。调度到当前事件循环异步执行（不阻塞 agent）。
        if self._enabled:
            self._schedule_persist(data)

    # ------------------------------------------------------------------ #
    # 实时快照（运行中查询）
    # ------------------------------------------------------------------ #

    def get_live(self, trace_id: str) -> dict[str, Any] | None:
        """运行中的 trace 快照（含已完成 span 树），未运行/已完成返回 None。"""
        live = self._live.get(trace_id)
        if live is None:
            return None
        return {
            "trace_id": live["trace_id"],
            "name": live.get("name"),
            "runs_ms": live.get("runs_ms") or 0,
            "spans": live.get("spans") or [],
            "live": True,
        }

    def live_rows(self) -> list[dict[str, Any]]:
        """全部运行中 trace 的列表视图（span_count 实时）。"""
        rows: list[dict[str, Any]] = []
        for live in self._live.values():
            rows.append(
                {
                    "trace_id": live["trace_id"],
                    "name": live.get("name"),
                    "span_count": len(live.get("spans") or []),
                    "live": True,
                }
            )
        return rows

    # ------------------------------------------------------------------ #
    # 持久化：大对象（MinIO / 本地文件） + 摘要行（SQLite）
    # ------------------------------------------------------------------ #

    def _object_key(self, trace_id: str, when: datetime) -> str:
        """对象 key：traces/YYYY/MM/DD/<trace_id>.json（按完成日期分层，便于后续清理）。"""
        d = when.strftime("%Y/%m/%d")
        return f"traces/{d}/{trace_id}.json"

    def _schedule_persist(self, data: dict[str, Any]) -> None:
        """把异步持久化调度到当前事件循环；无事件循环（单测/同步上下文）时静默跳过。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的事件循环：退化为同步执行（仅内存近期 + 日志），不落库。
            return
        loop.create_task(self._persist(data))

    async def _persist(self, data: dict[str, Any]) -> None:
        """写大对象 + upsert 摘要行。异常全部吞掉，绝不影响 agent 执行。"""
        try:
            from .models import TraceModel

            now = datetime.now(timezone.utc)
            key = self._object_key(data["trace_id"], now)
            payload = {
                "trace_id": data["trace_id"],
                "name": data["name"],
                "runs_ms": data["runs_ms"],
                "span_count": data["span_count"],
                "spans": data["spans"],
            }
            body = json.dumps(payload, ensure_ascii=False)
            # 大对象（可达数十 MB）放线程池，避免阻塞事件循环。
            await asyncio.to_thread(self._put_object, key, body)

            # upsert 摘要行（trace_id 为 PK）。
            row, _ = await TraceModel.update_or_create(
                trace_id=data["trace_id"],
                defaults={
                    "name": (data["name"] or "")[:255] or None,
                    "runs_ms": int(data["runs_ms"] or 0),
                    "span_count": int(data["span_count"] or 0),
                    "object_key": key,
                },
            )
            del row
        except Exception:  # noqa: BLE001
            pass

    # ---- 对象后端：MinIO 或本地文件退化 ----

    def _minio_client(self) -> Any | None:
        """惰性构造 MinIO 客户端；未配置 endpoint 返回 None（走本地文件）。"""
        if self._minio_init_tried:
            return self._minio
        self._minio_init_tried = True
        try:
            if not config.MINIO_ENDPOINT:
                return None
            from minio import Minio
            from io import BytesIO

            self._minio = Minio(
                config.MINIO_ENDPOINT,
                access_key=config.MINIO_ACCESS_KEY, secret_key=config.MINIO_SECRET_KEY,
                secure=config.MINIO_USE_SSL,
            )
            # 确保 bucket 存在（幂等）。
            if not self._minio.bucket_exists(config.MINIO_BUCKET):
                self._minio.make_bucket(config.MINIO_BUCKET)
        except Exception:  # noqa: BLE001
            self._minio = None
        return self._minio

    def _put_object(self, key: str, body: str) -> None:
        client = self._minio_client()
        data = body.encode("utf-8")
        if client is not None:
            client.put_object(
                config.MINIO_BUCKET,
                key,
                BytesIO(data),
                length=len(data),
                content_type="application/json",
            )
        else:
            # 本地退化：<data_dir>/traces/<key>
            path = os.path.join(config.DATA_DIR, "traces", key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)

    def _get_object(self, key: str) -> dict[str, Any] | None:
        client = self._minio_client()
        if client is not None:
            resp = client.get_object(config.MINIO_BUCKET, key)
            try:
                raw = resp.read()
            finally:
                resp.close()
                resp.release_conn()
            return json.loads(raw.decode("utf-8"))
        path = os.path.join(config.DATA_DIR, "traces", key)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        return None

    # ------------------------------------------------------------------ #
    # 查询（DB 摘要 + 对象）
    # ------------------------------------------------------------------ #

    async def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """按 trace_id 定位单条 trace：DB 主键命中 -> 取大对象。未命中返回 None。"""
        if not trace_id:
            return None
        try:
            from .models import TraceModel

            row = await TraceModel.get(trace_id=trace_id)
        except Exception:  # noqa: BLE001
            return None
        if row is None:
            return None
        # 取大对象放线程池（可能数十 MB）。
        obj = None
        if row.object_key:
            try:
                obj = await asyncio.to_thread(self._get_object, row.object_key)
            except Exception:  # noqa: BLE001
                obj = None
        return {
            "trace_id": row.trace_id,
            "name": row.name,
            "runs_ms": row.runs_ms or 0,
            "span_count": row.span_count or 0,
            "spans": (obj or {}).get("spans", []) if obj else [],
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    async def list_traces(
        self, limit: int = 20, offset: int = 0
    ) -> list[dict[str, Any]]:
        """最近 trace 摘要（按完成时间倒序），不含 span 明细（列表页轻量）。"""
        from .models import TraceModel

        qs = TraceModel.all().order_by("-created_at")
        rows = await qs.offset(max(0, offset)).limit(max(1, limit))
        return [
            {
                "trace_id": r.trace_id,
                "name": r.name,
                "span_count": r.span_count or 0,
                "runs_ms": r.runs_ms or 0,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # 旧 JSONL 兜底（迁移前遗留数据 + 无 DB 环境）
    # ------------------------------------------------------------------ #

    def jsonl_path(self) -> str:
        return os.path.join(config.DATA_DIR, "traces.jsonl")

    def read_jsonl(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """从 JSONL 文件读取最近的 trace（行序 = 完成顺序）。仅用于兜底。"""
        rows: list[dict[str, Any]] = []
        try:
            path = self.jsonl_path()
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            rows.reverse()
            end = offset + limit
            return rows[offset:end]
        except OSError:
            return []

    def find_trace(self, trace_id: str) -> dict[str, Any] | None:
        """按 trace_id 在 JSONL 里定位单条 trace（懒扫描）。仅用于兜底。"""
        if not trace_id:
            return None
        needle = f'"trace_id": "{trace_id}"'
        try:
            path = self.jsonl_path()
            if not os.path.isfile(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if needle not in line:
                        continue
                    try:
                        row = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    if row.get("trace_id") == trace_id:
                        return row
        except OSError:
            pass
        return None

    # ------------------------------------------------------------------ #
    # 内存最近记录
    # ------------------------------------------------------------------ #

    def _keep_recent(self, data: dict[str, Any]) -> None:
        row = {
            "trace_id": data["trace_id"],
            "name": data["name"],
            "runs_ms": data["runs_ms"],
            "span_count": data.get("span_count", 0),
            "spans": data["spans"],
        }
        self._recent.insert(0, row)
        if len(self._recent) > self._max_recent:
            self._recent.pop()

    def recent(self) -> list[dict[str, Any]]:
        return list(self._recent)

    def shutdown(self) -> None:
        pass

    def force_flush(self) -> None:
        pass
