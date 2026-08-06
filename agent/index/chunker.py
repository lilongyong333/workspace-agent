"""切块 —— 检索质量的地基。

## 为什么不能固定长度硬切

最常见的错误做法是「每 512 字符切一刀」。这会把表格切成两半、
把一句话拦腰截断、让一段代码丢掉函数签名。检索到这样的块，
模型拿到的是残缺信息。

## 本实现的三条规则

**1. 永不跨越标题边界。** 标题是天然的语义分割点。

**2. 表格整体成块，且每块都带表头。**
没有表头的数据行毫无意义 —— 「研发部 | 1200000」到底是预算还是实际？

**3. 每块前置面包屑。**
这是收益最大、最容易被忽略的一招：

    [财务/2025Q4预算.xlsx > Sheet: 部门明细 > 表头: 部门|预算|实际]
    研发部 | 1,200,000 | 1,187,432

有了这一行，块脱离原文也能被正确理解，而且面包屑本身也进全文索引
（权重 2.0），命中标题/表头比命中正文更能说明相关性。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .parsers import ParsedDoc, TextBlock

TARGET_CHARS = 900       # 目标块长
OVERLAP_CHARS = 140      # 相邻块重叠，避免答案正好落在切口上
HARD_MAX_CHARS = 2400    # 单块硬上限
MIN_CHUNK_CHARS = 8      # 低于此长度视为噪声碎片（页码、分隔线、空行）
#
# 这个阈值曾经是 24，直接导致「一行文档整篇消失」：
#   # 新内容
#   量子计算实验室预算 500 万元。
# 全文 22 字符 < 24，唯一的块被丢弃，文件在索引里**彻底不存在**，且不报错。
# 教训：对搜索索引而言，漏掉真实内容（假阴性）远比多收一点噪声昂贵 ——
# 噪声块 BM25 分低、自然沉底；漏掉的内容用户永远搜不到，也永远不知道为什么。
# 下面 chunk_document 结尾还有一层兜底，保证非空文档至少产出一个块。


@dataclass
class Chunk:
    text: str
    breadcrumb: str
    locator: dict[str, Any]


def build_breadcrumb(rel_path: str, heading_path: Sequence[str]) -> str:
    parts = [rel_path, *[h for h in heading_path if h]]
    crumb = " > ".join(parts)
    return crumb[:400]


def chunk_document(doc: ParsedDoc, rel_path: str) -> list[Chunk]:
    """把 ParsedDoc 的块序列重组成适合检索的块。"""
    out: list[Chunk] = []
    buf: list[TextBlock] = []
    buf_len = 0

    def flush() -> None:
        nonlocal buf, buf_len
        if not buf:
            return
        text = "\n\n".join(b.text for b in buf).strip()
        if len(text) >= MIN_CHUNK_CHARS:
            out.append(Chunk(
                text=text[:HARD_MAX_CHARS],
                breadcrumb=build_breadcrumb(rel_path, buf[0].heading_path),
                locator=dict(buf[0].locator),
            ))
        buf, buf_len = [], 0

    for block in doc.blocks:
        # 表格与超长块独立成块，绝不与其他内容混合
        if block.kind == "table" or len(block.text) > TARGET_CHARS:
            flush()
            for piece in _split_long(block.text):
                if len(piece) >= MIN_CHUNK_CHARS:
                    out.append(Chunk(
                        text=piece,
                        breadcrumb=build_breadcrumb(rel_path, block.heading_path),
                        locator=dict(block.locator),
                    ))
            continue

        # 遇到标题：先收口，再让标题成为后续块的起点
        if block.kind == "heading":
            flush()
            buf, buf_len = [block], len(block.text)
            continue

        buf.append(block)
        buf_len += len(block.text)
        if buf_len >= TARGET_CHARS:
            flush()

    flush()

    # 兜底：文档明明有内容，却一个块都没产出 —— 说明整篇都短于 MIN_CHUNK_CHARS，
    # 或者内容全部卡在各个 flush 的长度门槛下面。
    # 这类文件（一行备忘、一条说明、短 README）必须进索引：
    # 否则它对搜索**彻底不存在**，而且全程没有任何报错。
    # 多收一个短块的代价接近零，漏掉一整个文件的代价是「搜不到 = 不存在」的错误结论。
    if not out and doc.blocks:
        whole = "\n\n".join(b.text for b in doc.blocks).strip()
        if whole:
            first = doc.blocks[0]
            out.append(Chunk(
                text=whole[:HARD_MAX_CHARS],
                breadcrumb=build_breadcrumb(rel_path, first.heading_path),
                locator=dict(first.locator),
            ))

    # 相邻块之间加重叠：把上一块的尾部接到下一块开头，
    # 避免答案恰好被切口分开导致两边都检索不到。
    return _apply_overlap(out)


def _split_long(text: str) -> list[str]:
    """超长文本按段落累积切分，段落本身超长则按行切。"""
    pieces: list[str] = []
    buf: list[str] = []
    size = 0
    for para in text.split("\n"):
        if size + len(para) > TARGET_CHARS and buf:
            pieces.append("\n".join(buf))
            buf, size = [], 0
        if len(para) > HARD_MAX_CHARS:
            if buf:
                pieces.append("\n".join(buf)); buf, size = [], 0
            for i in range(0, len(para), HARD_MAX_CHARS):
                pieces.append(para[i : i + HARD_MAX_CHARS])
            continue
        buf.append(para)
        size += len(para) + 1
    if buf:
        pieces.append("\n".join(buf))
    return [p.strip() for p in pieces if p.strip()]


def _apply_overlap(chunks: list[Chunk]) -> list[Chunk]:
    """给相邻块加重叠 —— **但绝不跨越章节边界**。

    重叠的目的是防止答案正好落在切口上导致两边都检索不到。
    但如果无差别地把上一块尾部拼进下一块，就会把不同章节的内容混进同一块，
    直接违背「永不跨越标题边界」这条更重要的规则 ——
    检索到那样的块，模型会以为两段话属于同一个上下文。

    因此只在 **breadcrumb 相同**（即同一章节的连续切分）时才重叠。

    （这是我自己引入的设计冲突，由 test_chunker_never_crosses_headings 抓出。
     两条规则相遇时，边界规则优先。）
    """
    if len(chunks) < 2 or OVERLAP_CHARS <= 0:
        return chunks
    out = [chunks[0]]
    for prev, cur in zip(chunks, chunks[1:]):
        same_section = prev.breadcrumb == cur.breadcrumb
        tail = prev.text[-OVERLAP_CHARS:].strip() if same_section else ""
        merged = f"…{tail}\n\n{cur.text}" if tail else cur.text
        out.append(Chunk(text=merged[:HARD_MAX_CHARS],
                         breadcrumb=cur.breadcrumb, locator=cur.locator))
    return out
