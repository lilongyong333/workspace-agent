"""证据时效校验 —— 索引带来的新风险，必须专门解决。

## 问题

没有索引时，每次检索都直接读磁盘，所以**结果永远是新鲜的**。
加了索引之后，索引与磁盘之间出现了时间差：

* 用户删了一份合同 → 索引还在 → agent 引用一份**已经不存在**的文件
* 用户改了金额     → 索引是旧的 → agent 报出**错误的数字**

这不是理论风险。任何"扫描一次、之后查索引"的系统都有这个问题，
而且它比检索不到更危险 —— 检索不到用户会追问，报错数字用户会直接相信。

## 解法

**回答之前，逐块校验源文件是否仍匹配。**

判据是 ``content_sha256``：文档级哈希决定文件整体是否变过，
块级哈希决定这一段文本是否还在。不匹配的块**直接丢弃**并触发重索引。

丢弃后若证据不足，就如实告诉用户「相关文档已变更，正在重新索引」，
**而不是拿旧数据编一个答案**。

> 这与检索系统的一条通用原则一致：
> **索引必须能证明自己没过期，否则就要拒绝使用。**
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from .store import Hit, IndexStore

_HASH_CHUNK = 1 << 20


@dataclass
class VerifyReport:
    fresh: list[Hit] = field(default_factory=list)
    stale: list[dict[str, str]] = field(default_factory=list)
    reindex_doc_ids: set[int] = field(default_factory=set)

    @property
    def has_stale(self) -> bool:
        return bool(self.stale)

    def as_note(self) -> str:
        """给模型看的说明。让它知道有证据被丢弃了，而不是默默少了几条。"""
        if not self.stale:
            return ""
        lines = [f"- {s['path']}（{s['reason']}）" for s in self.stale[:8]]
        more = f"\n…另有 {len(self.stale) - 8} 条" if len(self.stale) > 8 else ""
        return (
            "⚠ 以下检索结果对应的源文件在索引之后发生了变化，已被丢弃，"
            "请勿引用它们：\n" + "\n".join(lines) + more +
            "\n如果剩余证据不足以回答，请如实说明「相关文档已变更，索引正在更新」，"
            "不要基于已失效的内容作答。"
        )


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while blk := fh.read(_HASH_CHUNK):
            h.update(blk)
    return h.hexdigest()


def verify_hits(store: IndexStore, hits: list[Hit]) -> VerifyReport:
    """逐块校验新鲜度。

    实现要点：**按文档聚合，一个文档只算一次哈希**。
    同一份文档常常命中多个块，逐块重算哈希会把校验成本放大数倍。
    """
    report = VerifyReport()
    doc_cache: dict[int, tuple[bool, str]] = {}   # doc_id -> (是否新鲜, 原因)

    for hit in hits:
        state = doc_cache.get(hit.doc_id)
        if state is None:
            state = _check_document(store, hit.doc_id)
            doc_cache[hit.doc_id] = state

        ok, reason = state
        if ok:
            report.fresh.append(hit)
        else:
            report.stale.append({"path": hit.rel_path, "reason": reason})
            report.reindex_doc_ids.add(hit.doc_id)

    return report


def _check_document(store: IndexStore, doc_id: int) -> tuple[bool, str]:
    row = store.get_document(doc_id)
    if row is None:
        return False, "索引记录已删除"

    path = Path(row["abs_path"])
    if not path.exists():
        return False, "文件已删除或移动"

    try:
        st = path.stat()
    except OSError as exc:
        return False, f"无法访问（{exc.__class__.__name__}）"

    # 快路径：mtime 与 size 都没变，几乎不可能内容变了，省掉一次全文件哈希
    if st.st_mtime_ns == row["mtime_ns"] and st.st_size == row["size_bytes"]:
        return True, ""

    # 慢路径：确实可能变了，算哈希定论
    try:
        if _file_sha256(path) == row["content_sha256"]:
            # 只是被触碰过，顺手更新时间戳，下次就走快路径
            store.touch_document(doc_id, st.st_mtime_ns)
            store.conn.commit()
            return True, ""
    except OSError as exc:
        return False, f"读取失败（{exc.__class__.__name__}）"

    return False, "文件内容已修改"
