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


# ----------------------------------------------------------------------
# 文件夹上传
# ----------------------------------------------------------------------
def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(A, "SESSIONS_DIR", tmp_path / "sessions")
    store = IndexStore(tmp_path / "idx.db")
    monkeypatch.setattr(A, "_store", store)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    c = TestClient(A.app)
    return c, store


def test_upload_returns_immediately_and_indexes_in_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上传必须**立刻返回**，索引在后台跑。

    实测：8 个文件 / 791KB，其中 6 个扫描件，开启视觉解析后同步索引要 57.9 秒。
    Cloudflare 免费版 100 秒切断连接，浏览器直接抛 "Failed to fetch" ——
    看起来像上传功能坏了，其实文件早就存好了，坏的只是"等结果"这件事。
    """
    import time

    c, store = _client(tmp_path, monkeypatch)
    sid = c.post("/api/session").json()["session_id"]

    md = "# 2025 Q4 预算\n研发部 1,187,432 元\n".encode()
    csv = b"vendor,amount\nMeridian,55000\n"
    r = c.post("/api/upload", data={"session_id": sid}, files=[
        ("files", ("我的资料/预算/2025Q4.md", md, "text/markdown")),
        ("files", ("我的资料/合同.csv", csv, "text/csv")),
    ])
    assert r.status_code == 200
    body = r.json()
    assert body["saved"] == 2
    assert body["indexing"] is True, "响应应表明索引仍在进行"
    assert body["root_id"], "前端要靠 root_id 轮询进度"

    # 文件在返回时就该已经落盘了 —— 后台跑的只是索引
    ws = A.SESSIONS_DIR / sid
    assert (ws / "uploads" / "我的资料" / "预算" / "2025Q4.md").is_file()

    for _ in range(100):                      # 等后台线程完成，最多 10 秒
        if A._index_jobs.get(body["root_id"], {}).get("status") == "idle":
            break
        time.sleep(0.1)

    assert store.search("1,187,432", limit=3), "后台索引完成后必须可检索"
    assert store.search("Meridian", limit=3)
    store.close()


def test_upload_rejects_hostile_paths_instead_of_sanitizing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """恶意路径必须**被拒绝并报告**，不能悄悄清洗后照常保存。

    第一版把 ".." 段过滤掉再拼路径，于是
    ../../../../etc/cron.d/evil 变成 uploads/etc/cron.d/evil 存了下来，
    还报告「上传成功」。沙箱边界确实没破，但一次明确的攻击尝试
    被抹平成了正常上传 —— 用户和日志都看不到它发生过。

    清洗掩盖攻击，拒绝暴露攻击。
    """
    c, store = _client(tmp_path, monkeypatch)
    sid = c.post("/api/session").json()["session_id"]

    hostile = ["../../../../etc/cron.d/evil", "C:/Windows/System32/evil.txt",
               "/etc/passwd", "a/../../b/evil"]
    r = c.post("/api/upload", data={"session_id": sid},
               files=[("files", (p, b"pwned", "text/plain")) for p in hostile]
                     + [("files", ("ok/正常.md", b"# hi\n", "text/markdown"))])
    body = r.json()

    assert body["saved"] == 1, "只有那个正常文件该被保存"
    assert body["skipped_total"] == len(hostile)
    reasons = " ".join(s["reason"] for s in body["skipped"])
    assert "拒绝" in reasons

    ws = A.SESSIONS_DIR / sid
    written = {p.name for p in ws.rglob("*") if p.is_file()}
    assert "evil" not in written and "evil.txt" not in written and "passwd" not in written
    store.close()


def test_upload_enforces_file_count_limit(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """没有数量闸，一次上传就能把容器磁盘写满或让索引跑到天荒地老。"""
    c, store = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(A, "MAX_UPLOAD_FILES", 3)
    sid = c.post("/api/session").json()["session_id"]

    r = c.post("/api/upload", data={"session_id": sid},
               files=[("files", (f"f{i}.md", b"x", "text/markdown")) for i in range(5)])
    assert r.status_code == 400
    store.close()


# ----------------------------------------------------------------------
# 模型能力：视觉模型不能驱动 agent 循环
# ----------------------------------------------------------------------
def test_vision_only_model_is_excluded_from_the_picker(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """不支持工具调用的模型必须**从选择器里消失**，而不是标个警告了事。

    实测（同一套 tools 定义打同一个端点）：
        qwen-vl-max   tool_calls 为空，只回一段文本
        qwen-max      正常返回 tool_calls

    选中 qwen-vl-max 会发生什么：模型不调工具，改用文本写出一段
    Python 伪代码（write_file(...)、finish(...)），循环一步都推进不了，
    界面上却"看起来有回复"—— 又一个静默失败。

    与「没有删除工具」同一条原则：选不了的东西才是真的选不了。
    """
    from agent.llm import available_providers, model_supports_tools

    monkeypatch.setenv("QWEN_API_KEY", "sk-test")
    monkeypatch.setenv("QWEN_MODEL", "qwen-vl-max")     # 用户为了 OCR 这么配是很自然的
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")

    models = {m["provider"]: m["model"] for m in available_providers()}
    assert "qwen" in models, "千问不该整个消失 —— 应退回它能调工具的缺省模型"
    assert models["qwen"] != "qwen-vl-max"
    assert all(model_supports_tools(m) for m in models.values())


def test_tool_capability_detection() -> None:
    from agent.llm import model_supports_tools, model_supports_vision

    assert not model_supports_tools("qwen-vl-max")
    assert model_supports_tools("qwen-max")
    assert model_supports_tools("deepseek-v4-flash")
    # 视觉与工具是两件独立的事，不能互相推导
    assert model_supports_vision("qwen-vl-max")
    assert not model_supports_vision("qwen-max")


# ----------------------------------------------------------------------
# 会话粘性
# ----------------------------------------------------------------------
def test_reopening_returns_to_the_same_session(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """带着已有 id 再进来，必须**回到同一个会话**，而不是新建。

    这条曾经是反的：只有"重置"时才认前端传来的 id，正常打开页面一律新建 uuid。
    于是每刷新一次就换一个全新工作区，上传的文件与索引全都"不见了" ——
    而磁盘上其实好端端地躺在上一个会话目录里。

    用户看到的现象是「重新进入界面，之前的资料都消失了」，
    很自然会怀疑持久化没生效。症状指向存储，根因在会话路由 ——
    这类错位最费排查时间。
    """
    c, store = _client(tmp_path, monkeypatch)

    first = c.post("/api/session").json()["session_id"]
    ws = A.SESSIONS_DIR / first
    (ws / "我上传的.md").write_text("# 独一无二 tigermark3\n", encoding="utf-8")

    again = c.post("/api/session", params={"session_id": first}).json()["session_id"]
    assert again == first, "带着已有 id 再进来不该换会话"
    assert (ws / "我上传的.md").is_file(), "工作区内容必须原样保留"
    store.close()


def test_reset_keeps_the_id_but_rebuilds_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重置是"同一个会话、干净工作区"，不是"换一个会话"。"""
    c, store = _client(tmp_path, monkeypatch)

    sid = c.post("/api/session").json()["session_id"]
    (A.SESSIONS_DIR / sid / "临时.md").write_text("x", encoding="utf-8")

    same = c.post("/api/session", params={"session_id": sid, "reset": "true"}).json()
    assert same["session_id"] == sid
    assert not (A.SESSIONS_DIR / sid / "临时.md").exists(), "重置应清掉旧内容"
    assert (A.SESSIONS_DIR / sid / "notes").is_dir(), "种子应被重新铺开"
    store.close()


def test_bogus_session_id_falls_back_to_a_new_one(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """非法 id 不能被直接拿去拼路径 —— 那是路径注入的入口。"""
    c, store = _client(tmp_path, monkeypatch)
    for bad in ("../../etc", "not-a-uuid", "a/b"):
        got = c.post("/api/session", params={"session_id": bad}).json()["session_id"]
        assert got != bad
        import uuid as _u
        _u.UUID(got)                       # 必须是合法 uuid
    store.close()
