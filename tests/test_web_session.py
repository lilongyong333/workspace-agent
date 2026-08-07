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
