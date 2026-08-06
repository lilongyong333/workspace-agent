"""Web Demo —— FastAPI + SSE。

题面对 demo 的要求里，有一条被单独强调为「整个 demo 的灵魂」：

    实时看到 agent 的每一步：调了什么工具、参数是什么、结果摘要

因此本模块的核心不是页面，而是**把 agent 的事件流原样、实时地送到浏览器**。
它与本地 ``trace.jsonl`` 共用同一个 TraceRecorder —— 网页上看到的
和交付的轨迹文件是**同一份真相**，不会出现"演示好看但日志对不上"。

## 会话隔离

每个浏览器会话有独立的工作目录副本（从 ``workspace_seed/`` 复制）。
多名评审可以同时把玩而互不干扰，"重置"也只是重新复制一次。

这个设计顺带解决了部署问题：Railway 容器的文件系统是临时的，
而我们本来就不需要持久化。

## 防滥用

公网部署意味着 API key 挂在公网服务后面。四层防护：

1. 访问口令（可选，未配置则不校验，便于本地开发）
2. 单 IP 每小时任务数上限
3. 单次任务 token 硬上限
4. 全局日 token 预算

策略与阈值全部走环境变量，见 ``.env.example``。
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from agent.llm import LLMClient, LLMError
from agent.loop import AgentRunner
from agent.sandbox import Sandbox, SandboxError
from agent.trace import TraceRecorder

ROOT = Path(__file__).resolve().parents[1]
SEED_DIR = ROOT / "workspace_seed"
SESSIONS_DIR = ROOT / "sessions"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Workspace Agent", version="0.2.0")


# ======================================================================
# 配置
# ======================================================================
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


ACCESS_CODE = os.getenv("DEMO_ACCESS_CODE", "").strip()
RATE_LIMIT_PER_HOUR = _env_int("DEMO_RATE_LIMIT_PER_HOUR", 20)
MAX_TOKENS_PER_TASK = _env_int("DEMO_MAX_TOKENS_PER_TASK", 80_000)
DAILY_TOKEN_BUDGET = _env_int("DEMO_DAILY_TOKEN_BUDGET", 2_000_000)
MAX_TASK_CHARS = 2_000


# ======================================================================
# 防滥用
# ======================================================================
class Guard:
    """进程内的简易配额控制。

    刻意不引 Redis：单实例部署下内存足够，容器重启即重置，
    而重启本来就会重置工作目录 —— 状态模型保持一致，不引入新的运维面。
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._day = date.today()
        self._day_tokens = 0
        self._lock = threading.Lock()

    def check_rate(self, ip: str) -> None:
        now = time.time()
        with self._lock:
            bucket = self._hits[ip]
            while bucket and now - bucket[0] > 3600:
                bucket.popleft()
            if len(bucket) >= RATE_LIMIT_PER_HOUR:
                raise HTTPException(
                    429,
                    f"该 IP 每小时最多发起 {RATE_LIMIT_PER_HOUR} 个任务，请稍后再试。",
                )
            bucket.append(now)

    def check_daily_budget(self) -> None:
        with self._lock:
            if date.today() != self._day:
                self._day, self._day_tokens = date.today(), 0
            if self._day_tokens >= DAILY_TOKEN_BUDGET:
                raise HTTPException(429, "今日 token 预算已用尽，明天再来。")

    def add_tokens(self, n: int) -> None:
        with self._lock:
            self._day_tokens += n

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "daily_tokens_used": self._day_tokens,
                "daily_token_budget": DAILY_TOKEN_BUDGET,
                "rate_limit_per_hour": RATE_LIMIT_PER_HOUR,
                "max_tokens_per_task": MAX_TOKENS_PER_TASK,
                "access_code_required": bool(ACCESS_CODE),
            }


guard = Guard()


def require_access(code: str | None) -> None:
    if ACCESS_CODE and (code or "").strip() != ACCESS_CODE:
        raise HTTPException(401, "访问口令不正确。")


def client_ip(request: Request) -> str:
    # Cloudflare / Railway 都会带 X-Forwarded-For
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


# ======================================================================
# 会话
# ======================================================================
def session_dir(session_id: str) -> Path:
    """会话工作目录。session_id 必须是我们自己签发的 UUID，杜绝路径注入。"""
    try:
        uuid.UUID(session_id)
    except (ValueError, AttributeError):
        raise HTTPException(400, "非法的会话标识")
    return SESSIONS_DIR / session_id


def ensure_session(session_id: str, reset: bool = False) -> Path:
    ws = session_dir(session_id)
    if reset and ws.exists():
        shutil.rmtree(ws)
    if not ws.exists():
        ws.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(SEED_DIR, ws)
    return ws


# ======================================================================
# 运行管理
# ======================================================================
@dataclass
class Run:
    id: str
    session_id: str
    task: str
    events: queue.Queue = field(default_factory=queue.Queue)
    done: threading.Event = field(default_factory=threading.Event)


_runs: dict[str, Run] = {}
_runs_lock = threading.Lock()


def _execute(run: Run) -> None:
    """在后台线程里跑 agent，事件经队列送往 SSE。

    AgentRunner 是同步阻塞的（httpx 同步客户端），因此放线程里跑，
    而不是硬塞进事件循环 —— 这样不会阻塞其他会话的请求。
    """
    trace = TraceRecorder()
    trace.subscribe(lambda ev: run.events.put(ev))

    try:
        ws = ensure_session(run.session_id)
        runner = AgentRunner(
            ws,
            llm=LLMClient(),
            trace=trace,
            token_budget=MAX_TOKENS_PER_TASK,
        )
        result = runner.run(run.task)
        guard.add_tokens(result.usage.get("total_tokens", 0))
        run.events.put(
            {
                "type": "result",
                "outcome": result.outcome.value,
                "summary": result.summary,
                "deliverables": result.deliverables,
                "steps": result.steps,
                "usage": result.usage,
            }
        )
    except LLMError as exc:
        run.events.put({"type": "error", "message": f"模型调用失败：{exc}"})
    except Exception as exc:  # noqa: BLE001 - 任何异常都要让前端看到，不能静默挂死
        run.events.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        run.done.set()


# ======================================================================
# API
# ======================================================================
@app.post("/api/session")
async def create_session(reset: bool = Query(False), session_id: str | None = Query(None)) -> JSONResponse:
    """新建或重置会话工作目录。"""
    sid = session_id if (session_id and reset) else str(uuid.uuid4())
    if session_id and reset:
        try:
            uuid.UUID(session_id)
        except ValueError:
            sid = str(uuid.uuid4())
    ensure_session(sid, reset=reset)
    return JSONResponse({"session_id": sid, "reset": reset})


@app.post("/api/run")
async def start_run(
    request: Request,
    payload: dict[str, Any],
    x_access_code: str | None = Header(None),
) -> JSONResponse:
    require_access(x_access_code or payload.get("access_code"))
    guard.check_daily_budget()
    guard.check_rate(client_ip(request))

    task = str(payload.get("task") or "").strip()
    if not task:
        raise HTTPException(400, "任务不能为空")
    if len(task) > MAX_TASK_CHARS:
        raise HTTPException(400, f"任务过长（上限 {MAX_TASK_CHARS} 字符）")

    session_id = str(payload.get("session_id") or "")
    ensure_session(session_id)

    run = Run(id=str(uuid.uuid4()), session_id=session_id, task=task)
    with _runs_lock:
        _runs[run.id] = run
    threading.Thread(target=_execute, args=(run,), daemon=True).start()
    return JSONResponse({"run_id": run.id})


@app.get("/api/events/{run_id}")
async def stream_events(run_id: str) -> StreamingResponse:
    """SSE 事件流 —— demo 的灵魂。

    用 EventSource（只支持 GET）消费，所以 run 的创建与事件订阅分成两个端点。
    """
    with _runs_lock:
        run = _runs.get(run_id)
    if run is None:
        raise HTTPException(404, "run 不存在或已过期")

    def gen() -> Iterator[str]:
        while True:
            try:
                ev = run.events.get(timeout=1.0)
            except queue.Empty:
                if run.done.is_set() and run.events.empty():
                    break
                yield ": keepalive\n\n"   # 防止中间层掐断空闲连接
                continue
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev.get("type") in ("result", "error"):
                break
        yield "data: {\"type\": \"end\"}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # 关掉反代缓冲，否则"实时"会变成"一次性"
        },
    )


@app.get("/api/files")
async def list_files(session_id: str) -> JSONResponse:
    ws = ensure_session(session_id)
    sb = Sandbox(ws)
    entries = [
        {"path": e.rel_path, "type": e.kind, "size": e.size_bytes}
        for e in sb.list_dir(".", recursive=True)
    ]
    entries.sort(key=lambda e: (e["path"].count("/"), e["path"]))
    return JSONResponse({"session_id": session_id, "entries": entries})


@app.get("/api/file")
async def read_file(session_id: str, path: str) -> JSONResponse:
    """读单个文件供预览。经沙箱解析，路径逃逸同样被拒。"""
    ws = ensure_session(session_id)
    sb = Sandbox(ws)
    try:
        lines = sb.read_lines(path)
    except SandboxError as exc:
        raise HTTPException(400, str(exc)) from None

    MAX_PREVIEW = 400
    return JSONResponse(
        {
            "path": path,
            "total_lines": len(lines),
            "truncated": len(lines) > MAX_PREVIEW,
            "content": "".join(lines[:MAX_PREVIEW]),
        }
    )


@app.get("/api/config")
async def config() -> JSONResponse:
    return JSONResponse(
        {
            "model": os.getenv("LLM_MODEL", "(unset)"),
            "provider": os.getenv("LLM_PROVIDER", "(unset)"),
            **guard.snapshot(),
        }
    )


@app.get("/health")
async def health() -> JSONResponse:
    key = os.getenv("LLM_API_KEY", "")
    seed = sum(1 for p in SEED_DIR.rglob("*") if p.is_file())
    return JSONResponse(
        {
            "status": "ok",
            "version": app.version,
            "seed_file_count": seed,
            "seed_present": seed > 0,
            "config": {
                "llm_provider": os.getenv("LLM_PROVIDER", "(unset)"),
                "llm_model": os.getenv("LLM_MODEL", "(unset)"),
                "llm_api_key_present": bool(key),
                "llm_api_key_hint": f"{key[:6]}…{len(key)}chars" if key else None,
                "access_code_enabled": bool(ACCESS_CODE),
                "max_steps": _env_int("AGENT_MAX_STEPS", 40),
                "token_budget": _env_int("AGENT_TOKEN_BUDGET", 200_000),
            },
        }
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
