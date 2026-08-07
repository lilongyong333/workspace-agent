"""索引编排 —— 遍历、增量判断、解析、切块、落库。

## 增量同步的三级判断（从便宜到贵）

1. ``mtime_ns`` 与 ``size`` 都没变 → **直接跳过**（99% 的文件走这条，只 stat 不读内容）
2. mtime 变了但 ``content_sha256`` 相同 → 只更新时间戳（文件被触碰但内容没改）
3. ``content_sha256`` 变了 → 重新解析并重建该文档的全部块

外加：库里有、磁盘上没有的文档 → 删除（文件被移走或删除）。

这套判断让「10 万文件无变更扫描」从数十分钟降到十几秒。
"""

from __future__ import annotations

import fnmatch
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import parsers
from .chunker import chunk_document
from .store import IndexStore, Root, sha256_bytes

# 永远跳过的目录，避免把 node_modules 之类的东西吞进来
DEFAULT_EXCLUDE_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "dist", "build", ".next", ".nuxt", "target", ".gradle", ".tox",
    ".index", "sessions", ".DS_Store", "$RECYCLE.BIN", "System Volume Information",
}

# 读文件算哈希时的分块大小
_HASH_CHUNK = 1 << 20


@dataclass
class IndexProgress:
    files_seen: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    files_removed: int = 0
    chunks_written: int = 0
    current: str = ""
    errors: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "files_seen": self.files_seen,
            "files_indexed": self.files_indexed,
            "files_skipped": self.files_skipped,
            "files_failed": self.files_failed,
            "files_removed": self.files_removed,
            "chunks_written": self.chunks_written,
            "current": self.current,
        }


ProgressCb = Callable[[IndexProgress], None]


def _file_sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _walk(root: Path, include: list[str], exclude: list[str]) -> Iterator[Path]:
    """遍历目录。就地裁剪 dirnames 以避免进入被排除的目录（比事后过滤快得多）。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if d not in DEFAULT_EXCLUDE_DIRS and not d.startswith(".")
        ]
        base = Path(dirpath)
        for name in filenames:
            path = base / name
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            if exclude and any(fnmatch.fnmatch(rel, pat) for pat in exclude):
                continue
            if include and not any(fnmatch.fnmatch(rel, pat) for pat in include):
                continue
            yield path


def sync_root(
    store: IndexStore,
    root: Root,
    progress_cb: ProgressCb | None = None,
    # 每扫多少个文件回报一次进度。
    #
    # 曾经是 50 —— 而一个典型语料就几十个文件，files_seen % 50 == 0
    # 从来不成立，进度回调**一次都不触发**，界面上永远停在「已扫 0 / 索引 0」，
    # 看起来像卡死。
    #
    # 现在是 1：回调只是更新一个 dict，十万文件也就几十毫秒总开销，
    # 而"慢文件正在处理哪一个"对用户的价值远大于这点成本 ——
    # 扫描件一页要调一次视觉模型，几秒起步，没有实时的当前文件名，
    # 用户无法区分"在干活"和"挂了"。
    progress_every: int = 1,
) -> IndexProgress:
    """增量同步一个根目录。"""
    root_path = Path(root.path)
    if not root_path.is_dir():
        raise FileNotFoundError(f"根目录不存在: {root_path}")

    run_id = store.start_run(root.id)
    store.set_root_status(root.id, "scanning")
    prog = IndexProgress()
    known = store.get_document_state(root.id)   # rel_path -> (doc_id, mtime, size, sha)
    seen_rel: set[str] = set()

    try:
        for path in _walk(root_path, root.include_globs, root.exclude_globs):
            prog.files_seen += 1
            rel = path.relative_to(root_path).as_posix()
            seen_rel.add(rel)
            prog.current = rel

            # 第一个文件就报一次，别让界面在开头空等一拍
            if progress_cb and (prog.files_seen == 1
                                or prog.files_seen % progress_every == 0):
                progress_cb(prog)

            try:
                st = path.stat()
            except OSError:
                prog.files_skipped += 1
                continue

            prior = known.get(rel)

            # 一级：mtime + size 都没变 → 跳过，连读都不读
            if prior and prior[1] == st.st_mtime_ns and prior[2] == st.st_size:
                prog.files_skipped += 1
                continue

            if not parsers.is_supported(path):
                prog.files_skipped += 1
                continue

            # 二级：算内容哈希，与库里比对
            try:
                sha = _file_sha256(path)
            except OSError as exc:
                prog.files_failed += 1
                prog.errors.append((rel, f"读取失败: {exc}"))
                continue

            if prior and prior[3] == sha:
                store.touch_document(prior[0], st.st_mtime_ns)
                prog.files_skipped += 1
                continue

            # 三级：内容确实变了（或是新文件）→ 解析 + 重建块
            parsed = parsers.parse(path)
            doc_id = store.upsert_document(
                root_id=root.id, rel_path=rel, abs_path=str(path),
                ext=path.suffix.lower().lstrip(".") or None,
                size_bytes=st.st_size, mtime_ns=st.st_mtime_ns,
                content_sha256=sha, parser=parsed.parser,
                parsed_at=int(time.time()),
                parse_error=parsed.error, title=parsed.title,
            )

            if parsed.error:
                # 解析失败也要留档，且清空旧块避免残留陈旧内容
                store.replace_chunks(doc_id, [])
                prog.files_failed += 1
                prog.errors.append((rel, parsed.error))
                continue

            chunks = chunk_document(parsed, rel)
            n = store.replace_chunks(
                doc_id,
                [{"text": c.text, "breadcrumb": c.breadcrumb, "locator": c.locator}
                 for c in chunks],
            )
            prog.chunks_written += n
            prog.files_indexed += 1

            if prog.files_indexed % 200 == 0:
                store.conn.commit()

        # 磁盘上已消失的文档 → 从索引移除
        gone = [meta[0] for rel, meta in known.items() if rel not in seen_rel]
        prog.files_removed = store.delete_documents(gone)

        store.conn.commit()
        store.set_root_status(root.id, "idle", scanned=True)
        store.finish_run(
            run_id,
            files_seen=prog.files_seen, files_indexed=prog.files_indexed,
            files_skipped=prog.files_skipped, files_failed=prog.files_failed,
            files_removed=prog.files_removed, chunks_written=prog.chunks_written,
        )
    except Exception as exc:
        store.conn.commit()
        store.set_root_status(root.id, "error")
        store.finish_run(run_id, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        prog.current = ""
        if progress_cb:
            progress_cb(prog)

    return prog
