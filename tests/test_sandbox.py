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
