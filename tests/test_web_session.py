"""Web 会话与索引联动测试。

核心是两件事：
  * 每个会话工作区**默认就有索引** —— 否则模型只能逐个 read_file，
    一句「总结一下主要内容」实测要 40K~54K tokens、42 次 read_file，
    逼近单任务上限，经常以 DEGRADED 收场。
  * 重置工作区必须**同时清掉索引** —— 否则重置后第一次提问会拿到上一轮的
    残留内容，而且因为"有出处"显得格外可信。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import web.app as A                                      # noqa: E402
from agent.index.store import IndexStore                 # noqa: E402
from agent.sandbox import Sandbox                        # noqa: E402
from agent.tools import ToolBox                          # noqa: E402

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把会话目录与索引库都指向临时路径，避免污染真实 .index/"""
    monkeypatch.setattr(A, "SESSIONS_DIR", tmp_path / "sessions")
    store = IndexStore(tmp_path / "idx.db")
    monkeypatch.setattr(A, "_store", store)
    yield store
    store.close()


def test_session_workspace_is_indexed_by_default(isolated: IndexStore) -> None:
    """默认演示就该有语料提纲，不需要用户先去注册目录。"""
    ws = A.ensure_session(SID, reset=True)
    ids = A._ensure_session_indexed(isolated, SID, ws)
    assert ids, "会话工作区必须产生一个可检索的 root"

    data = ToolBox(Sandbox(ws), store=isolated, root_ids=ids).execute("describe_corpus", {}).data
    assert data["engine"] == "index"
    assert data["documents"] > 0
    # 关键：概览里必须有**内容**（提纲），不能只有形状（统计）
    assert data["outline"], "describe_corpus 必须带逐文件提纲"
    assert any(d["preview"] for d in data["outline"]), "提纲必须含正文预览"


def test_incremental_sync_picks_up_new_files(isolated: IndexStore) -> None:
    """agent 跑完任务会改工作区，下一次提问前必须能看到改动。"""
    ws = A.ensure_session(SID, reset=True)
    A._ensure_session_indexed(isolated, SID, ws)
    assert not isolated.search("季度预算超支", limit=3)

    (ws / "新增.md").write_text("# 季度预算超支\n研发部超支 3%。\n", encoding="utf-8")
    A._ensure_session_indexed(isolated, SID, ws)
    assert isolated.search("季度预算超支", limit=3), "增量同步没抓到新文件"


def test_reset_drops_the_session_index(isolated: IndexStore) -> None:
    """重置之后，上一轮的内容必须彻底搜不到。

    索引不清的话，工作区已经恢复原样，检索却还能返回旧文件的片段 ——
    模型据此作答会给出「有出处的错误答案」，比搜不到危险得多。
    """
    ws = A.ensure_session(SID, reset=True)
    (ws / "上一轮产物.md").write_text("# 独一无二的标记词 zebra9animal\n", encoding="utf-8")
    A._ensure_session_indexed(isolated, SID, ws)
    assert isolated.search("zebra9animal", limit=3)

    A.ensure_session(SID, reset=True)
    assert isolated.get_root(label=A.SESSION_ROOT_PREFIX + SID) is None
    assert not isolated.search("zebra9animal", limit=3), "重置后仍能搜到上一轮内容"


def test_sessions_do_not_see_each_others_workspaces(isolated: IndexStore) -> None:
    """会话隔离在索引层同样成立 —— 多人同时试用不能互相看到对方的文件。"""
    other = "99999999-8888-7777-6666-555555555555"
    ws_a = A.ensure_session(SID, reset=True)
    ws_b = A.ensure_session(other, reset=True)
    (ws_a / "a.md").write_text("# 只属于A的标记 alphamark7\n", encoding="utf-8")
    (ws_b / "b.md").write_text("# 只属于B的标记 betamark7\n", encoding="utf-8")

    ids_a = A._ensure_session_indexed(isolated, SID, ws_a)
    ids_b = A._ensure_session_indexed(isolated, other, ws_b)

    a_hits = {h.rel_path for h in isolated.search("betamark7", limit=5, root_ids=ids_a)}
    b_hits = {h.rel_path for h in isolated.search("alphamark7", limit=5, root_ids=ids_b)}
    assert not a_hits, "会话 A 不该看到会话 B 的文件"
    assert not b_hits, "会话 B 不该看到会话 A 的文件"


# ----------------------------------------------------------------------
# 模型选择：只认服务端已配置 key 的 provider
# ----------------------------------------------------------------------
def test_run_rejects_unconfigured_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """前端不能指定一个服务端没配 key 的 provider。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    c = TestClient(A.app)
    sid = c.post("/api/session").json()["session_id"]

    r = c.post("/api/run", json={"task": "x", "session_id": sid, "provider": "openai"})
    assert r.status_code == 400 and "未在服务端配置" in r.json()["detail"]


def test_run_rejects_arbitrary_model_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型名也必须在白名单内。

    否则前端可以传任意字符串当模型名，让服务器拿你的 key 去打一个
    你从未打算调用的模型 —— 计费和内容都不受控。
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    c = TestClient(A.app)
    sid = c.post("/api/session").json()["session_id"]

    r = c.post("/api/run", json={"task": "x", "session_id": sid,
                                 "provider": "deepseek", "model": "evil-model"})
    assert r.status_code == 400


def test_config_never_leaks_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """模型列表是公开端点 —— 里面绝不能出现 key 的任何片段。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("LLM_API_KEY", "sk-super-secret-value-42")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    c = TestClient(A.app)

    raw = c.get("/api/config").text
    assert "sk-super-secret" not in raw
    assert "super-secret-value" not in raw


def test_readonly_flag_removes_write_tools() -> None:
    """Web 端的只读开关必须真的把写工具从工具清单里拿掉。

    这条能力原本只有 CLI 的 ask 有，Web 上演示不了 ——
    而它恰恰是最能说明设计取向的一条：边界靠能力不存在，不靠提示词。
    """
    from agent.tools import build_tool_schemas

    rw = {s["function"]["name"] for s in build_tool_schemas(has_index=True)}
    ro = {s["function"]["name"] for s in build_tool_schemas(has_index=True, read_only=True)}
    assert {"write_file", "move_file"} <= rw
    assert not ({"write_file", "move_file"} & ro)
