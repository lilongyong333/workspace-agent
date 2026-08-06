"""Web Demo —— FastAPI 应用。

当前阶段：**部署管道验证骨架**。
只提供健康检查与配置自检，用于在写完 agent 之前先把
「GitHub → Railway 构建 → 容器启动 → 公网可访问」这条链路跑通。

后续会在此基础上长出：
* ``POST /api/run``   启动任务，SSE 实时推送每一步工具调用（题面称之为「demo 的灵魂」）
* ``GET  /api/files`` 浏览会话工作目录
* ``POST /api/reset`` 从 workspace_seed 重置
* 用量统计与防滥用

刻意先部署空壳的理由：构建失败、端口不对、环境变量没配这类问题，
如果留到最后一小时才暴露，就没有修复窗口了。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

ROOT = Path(__file__).resolve().parents[1]
SEED_DIR = ROOT / "workspace_seed"

app = FastAPI(
    title="Workspace Agent",
    description="手写循环的文件助理 Agent",
    version="0.1.0",
)


def _config_status() -> dict[str, object]:
    """自检：不泄漏任何密钥，只报告"配没配"。

    线上排障时这是第一站 —— 大多数部署问题都是环境变量没注入。
    """
    key = os.getenv("LLM_API_KEY", "")
    return {
        "llm_provider": os.getenv("LLM_PROVIDER", "(unset)"),
        "llm_model": os.getenv("LLM_MODEL", "(unset)"),
        # 只报告长度与前缀，绝不回显密钥本身
        "llm_api_key_present": bool(key),
        "llm_api_key_hint": f"{key[:6]}…{len(key)}chars" if key else None,
        "access_code_enabled": bool(os.getenv("DEMO_ACCESS_CODE")),
        "max_steps": int(os.getenv("AGENT_MAX_STEPS", "40")),
        "token_budget": int(os.getenv("AGENT_TOKEN_BUDGET", "200000")),
    }


@app.get("/health")
async def health() -> JSONResponse:
    """存活探针 + 配置自检。部署后第一个要访问的地址。"""
    seed_files = sorted(p.relative_to(SEED_DIR).as_posix() for p in SEED_DIR.rglob("*") if p.is_file())
    return JSONResponse(
        {
            "status": "ok",
            "version": app.version,
            "seed_file_count": len(seed_files),
            "seed_present": len(seed_files) > 0,
            "config": _config_status(),
        }
    )


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    cfg = _config_status()
    key_badge = "已配置" if cfg["llm_api_key_present"] else "缺失"
    key_color = "#16a34a" if cfg["llm_api_key_present"] else "#dc2626"
    seed_count = sum(1 for p in SEED_DIR.rglob("*") if p.is_file())

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workspace Agent</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
         max-width: 46rem; margin: 3rem auto; padding: 0 1.25rem; line-height: 1.7; }}
  code {{ background: rgba(127,127,127,.16); padding: .15em .4em; border-radius: 4px; }}
  .badge {{ display:inline-block; padding:.1em .6em; border-radius:999px;
            font-size:.85em; color:#fff; background:{key_color}; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  td, th {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid rgba(127,127,127,.25); }}
  .muted {{ opacity:.65; font-size:.92em; }}
</style>
</head>
<body>
  <h1>Workspace Agent</h1>
  <p class="muted">手写循环的文件助理 Agent —— 部署管道已就绪，等待接入 agent 核心。</p>

  <table>
    <tr><th>模型提供方</th><td>{cfg["llm_provider"]}</td></tr>
    <tr><th>模型</th><td><code>{cfg["llm_model"]}</code></td></tr>
    <tr><th>API Key</th><td><span class="badge">{key_badge}</span></td></tr>
    <tr><th>种子文件</th><td>{seed_count} 个</td></tr>
    <tr><th>步数上限</th><td>{cfg["max_steps"]}</td></tr>
  </table>

  <p class="muted">
    健康检查：<a href="/health"><code>/health</code></a>
  </p>
</body>
</html>"""
