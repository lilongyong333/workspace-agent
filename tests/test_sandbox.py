"""沙箱边界测试。

每个用例都对应一种真实攻击形态。安全代码不测等于没写。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.sandbox import Sandbox, SandboxError  # noqa: E402


@pytest.fixture()
def sb(tmp_path: Path) -> Sandbox:
    root = tmp_path / "ws"
    (root / "drafts").mkdir(parents=True)
    (root / "notes").mkdir()
    (root / "drafts" / "a.md").write_text("---\nstatus: obsolete\n---\nhello\n", encoding="utf-8")
    (root / "notes" / "b.md").write_text("world\n", encoding="utf-8")
    # 工作目录之外的"机密"文件，用于验证逃逸确实被拦
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    return Sandbox(root)


# ----------------------------------------------------------------------
# 逃逸防御
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "evil",
    [
        "../secret.txt",              # 相对路径向上逃逸
        "../../etc/passwd",           # 多级向上
        "drafts/../../secret.txt",    # 绕道逃逸
        "notes/../../secret.txt",
        "./../secret.txt",
    ],
)
def test_relative_escape_rejected(sb: Sandbox, evil: str) -> None:
    with pytest.raises(SandboxError, match="逃逸"):
        sb.resolve(evil)


@pytest.mark.parametrize(
    "evil",
    ["/etc/passwd", "C:\\Windows\\System32\\config", "/tmp/x", "//server/share"],
)
def test_absolute_path_rejected(sb: Sandbox, evil: str) -> None:
    with pytest.raises(SandboxError, match="绝对路径"):
        sb.resolve(evil)


def test_sibling_prefix_not_confused(tmp_path: Path) -> None:
    """经典漏洞回归：用 startswith 判断前缀会把 /ws-evil 误判为在 /ws 内。

    本实现用 Path.relative_to 做真实的路径层级判断，不受此影响。
    """
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws-evil").mkdir()
    (tmp_path / "ws-evil" / "x.txt").write_text("pwned", encoding="utf-8")
    sb = Sandbox(tmp_path / "ws")
    with pytest.raises(SandboxError):
        sb.resolve("../ws-evil/x.txt")


# ----------------------------------------------------------------------
# 正常路径应当放行
# ----------------------------------------------------------------------
@pytest.mark.parametrize("ok", ["drafts/a.md", "./notes/b.md", "notes", "."])
def test_legit_paths_allowed(sb: Sandbox, ok: str) -> None:
    assert sb.resolve(ok).exists()


def test_rel_is_posix_style(sb: Sandbox) -> None:
    """相对路径统一用 / 分隔，保证 Windows 与 Linux 上断言一致。"""
    assert sb.rel(sb.resolve("drafts/a.md")) == "drafts/a.md"


# ----------------------------------------------------------------------
# 目录列举
# ----------------------------------------------------------------------
def test_list_dir_exposes_size(sb: Sandbox) -> None:
    """size_bytes 必须暴露 —— 模型靠它决定该 read 还是该 search。"""
    entries = {e.name: e for e in sb.list_dir("drafts")}
    assert entries["a.md"].kind == "file"
    assert entries["a.md"].size_bytes > 0


def test_list_dir_recursive(sb: Sandbox) -> None:
    paths = {e.rel_path for e in sb.list_dir(".", recursive=True)}
    assert {"drafts/a.md", "notes/b.md"} <= paths


# ----------------------------------------------------------------------
# 写操作边界
# ----------------------------------------------------------------------
def test_write_creates_parent_dirs(sb: Sandbox) -> None:
    """archive/ 不存在则创建 —— T2 明确要求。"""
    rel = sb.write_text("archive/MANIFEST.md", "- a.md\n")
    assert rel == "archive/MANIFEST.md"
    assert (sb.root / "archive" / "MANIFEST.md").read_text(encoding="utf-8") == "- a.md\n"


def test_write_outside_rejected(sb: Sandbox) -> None:
    with pytest.raises(SandboxError):
        sb.write_text("../escaped.txt", "pwned")
    assert not (sb.root.parent / "escaped.txt").exists()


def test_move_within_workspace(sb: Sandbox) -> None:
    src, dst = sb.move("drafts/a.md", "archive/a.md")
    assert (src, dst) == ("drafts/a.md", "archive/a.md")
    assert not (sb.root / "drafts" / "a.md").exists()
    assert (sb.root / "archive" / "a.md").exists()


def test_move_refuses_overwrite(sb: Sandbox) -> None:
    """静默覆盖等于变相删除 —— 而删除正是本沙箱要杜绝的能力。"""
    sb.write_text("archive/a.md", "existing")
    with pytest.raises(SandboxError, match="拒绝覆盖"):
        sb.move("drafts/a.md", "archive/a.md")
    assert (sb.root / "drafts" / "a.md").exists()          # 源文件仍在
    assert (sb.root / "archive" / "a.md").read_text(encoding="utf-8") == "existing"


def test_move_escape_rejected(sb: Sandbox) -> None:
    with pytest.raises(SandboxError):
        sb.move("drafts/a.md", "../stolen.md")
    assert (sb.root / "drafts" / "a.md").exists()


# ----------------------------------------------------------------------
# 能力边界：删除能力必须不存在
# ----------------------------------------------------------------------
def test_no_delete_capability_exists() -> None:
    """工作目录里两处注入都要求删文件。

    最可靠的防御不是让模型学会拒绝，而是让危险操作根本不存在。
    这个测试锁死这条设计约束，防止后人"顺手加个 delete 工具"。
    """
    forbidden = {"delete", "remove", "unlink", "rmtree", "rmdir", "purge"}
    exposed = {name for name in dir(Sandbox) if not name.startswith("_")}
    assert not (exposed & forbidden), f"沙箱不应暴露删除能力，但发现: {exposed & forbidden}"


# ----------------------------------------------------------------------
# 大文件保护
# ----------------------------------------------------------------------
def test_oversized_read_rejected(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "big.log").write_text("x" * 5000, encoding="utf-8")
    sb = Sandbox(root, max_read_bytes=1000)
    with pytest.raises(SandboxError, match="过大"):
        sb.read_lines("big.log")


def test_iter_lines_streams_without_size_limit(tmp_path: Path) -> None:
    """search 走流式迭代，因此不受 max_read_bytes 限制 —— 这正是大文件的解法。"""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "big.log").write_text("\n".join(f"line {i}" for i in range(5000)), encoding="utf-8")
    sb = Sandbox(root, max_read_bytes=1000)
    hits = [n for n, line in sb.iter_lines("big.log") if "line 4999" in line]
    assert hits == [5000]


# ----------------------------------------------------------------------
# 只读模式 —— 把 agent 指向用户真实资料目录时的核心保证
# ----------------------------------------------------------------------
def test_read_only_blocks_write_and_move(tmp_path: Path) -> None:
    """只读沙箱必须拒绝一切写入与移动，读取照常。

    这条边界的由来是一个真实事故：早期 ``ask`` 把索引根直接当工作目录，
    agent 于是把自己生成的答案写进了被索引的语料里，
    下一次检索时那份自产文件排到命中第一名 —— 自我引用回路。
    换成用户的公司文档，就是 AI 静默改了正式文件。
    """
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "contract.md").write_text("原始合同内容\n", encoding="utf-8")

    sb = Sandbox(root, read_only=True)

    assert "原始合同内容" in "".join(sb.read_lines("contract.md")), "只读不影响读"
    assert sb.list_dir(".")

    with pytest.raises(SandboxError, match="只读"):
        sb.write_text("answer.md", "AI 生成的答案")
    with pytest.raises(SandboxError, match="只读"):
        sb.move("contract.md", "archive/contract.md")

    # 语料必须原封不动
    assert {p.name for p in root.iterdir()} == {"contract.md"}
    assert (root / "contract.md").read_text(encoding="utf-8") == "原始合同内容\n"


def test_read_only_mode_hides_write_tools_from_model() -> None:
    """更强的一层：只读时写工具**根本不出现在工具清单里**。

    与「没有删除工具」同源 —— 模型无法调用它看不见的工具。
    留着工具再在运行时拒绝，等于把攻击面从「会不会被说服」
    换成「拒绝逻辑有没有漏判」，边界强度反而下降。
    """
    from agent.tools import build_tool_schemas

    rw = {s["function"]["name"] for s in build_tool_schemas(has_index=True)}
    ro = {s["function"]["name"] for s in build_tool_schemas(has_index=True, read_only=True)}

    assert {"write_file", "move_file"} <= rw
    assert not ({"write_file", "move_file"} & ro), "只读模式不得暴露任何写工具"
    # 检索能力必须完整保留，否则只读模式就没用了
    assert {"search", "read_file", "list_dir", "describe_corpus", "get_chunk"} <= ro
