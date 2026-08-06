"""索引层测试。

覆盖四件事：**建得对、增量快、检索准、陈旧能识别**。
最后一条最关键 —— 它是索引带来的**新风险**，没有它索引就是在制造错误答案。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.index.chunker import chunk_document                     # noqa: E402
from agent.index.indexer import sync_root                          # noqa: E402
from agent.index.parsers import ParsedDoc, TextBlock, parse, is_supported  # noqa: E402
from agent.index.store import IndexStore                           # noqa: E402
from agent.index.verify import verify_hits                         # noqa: E402


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    ws = tmp_path / "corpus"
    (ws / "docs").mkdir(parents=True)
    (ws / "data").mkdir()
    (ws / "docs" / "budget.md").write_text(
        "# 2025 第四季度预算\n\n## 部门明细\n研发部服务器采购金额为 1,187,432 元。\n"
        "## 备注\n供应商合同 2026-02-01 到期。\n", encoding="utf-8")
    (ws / "docs" / "notes.md").write_text(
        "# Q4 Notes\n\nServer procurement exceeded the budget by 3%.\n", encoding="utf-8")
    (ws / "data" / "vendors.csv").write_text(
        "vendor,contract_end,amount\nMeridian,2026-02-01,1187432\nAcme,2026-12-01,55000\n",
        encoding="utf-8")
    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "junk.js").write_text("should be skipped", encoding="utf-8")
    return ws


@pytest.fixture()
def store(tmp_path: Path) -> IndexStore:
    s = IndexStore(tmp_path / "idx.db")
    yield s
    s.close()


# ======================================================================
# 建索引
# ======================================================================
def test_index_builds_and_skips_noise_dirs(store: IndexStore, corpus: Path) -> None:
    root = store.add_root(str(corpus), label="t")
    prog = sync_root(store, root)
    assert prog.files_indexed == 3, "应索引 3 个文件"
    assert prog.chunks_written > 0
    # node_modules 必须被跳过，否则一个前端项目能把索引撑爆
    assert not store.search("should be skipped", limit=5)


def test_incremental_sync_skips_unchanged(store: IndexStore, corpus: Path) -> None:
    """无变更的二次扫描必须几乎不做事 —— 这是 10 万文件可用的前提。"""
    root = store.add_root(str(corpus), label="t")
    sync_root(store, root)
    again = sync_root(store, root)
    assert again.files_indexed == 0
    assert again.files_skipped >= 3


def test_changed_file_is_reindexed(store: IndexStore, corpus: Path) -> None:
    root = store.add_root(str(corpus), label="t")
    sync_root(store, root)
    assert not store.search("量子计算", limit=3)

    (corpus / "docs" / "budget.md").write_text(
        "# 新内容\n量子计算实验室预算 500 万元。\n", encoding="utf-8")
    prog = sync_root(store, root)
    assert prog.files_indexed == 1
    assert store.search("量子计算", limit=3)


def test_deleted_file_is_removed_from_index(store: IndexStore, corpus: Path) -> None:
    root = store.add_root(str(corpus), label="t")
    sync_root(store, root)
    assert store.search("Meridian", limit=3)

    (corpus / "data" / "vendors.csv").unlink()
    prog = sync_root(store, root)
    assert prog.files_removed == 1
    assert not store.search("Meridian", limit=3)


# ======================================================================
# 检索
# ======================================================================
def test_chinese_multi_char_via_trigram(store: IndexStore, corpus: Path) -> None:
    root = store.add_root(str(corpus), label="t")
    sync_root(store, root)
    hits = store.search("服务器", limit=3)
    assert hits and "gram" in hits[0].matched_by


def test_chinese_two_char_falls_back_to_substring(store: IndexStore, corpus: Path) -> None:
    """双字中文词是两条 FTS 路的共同盲区，必须有子串兜底。

    trigram 需要 ≥3 字符；unicode61 把相邻汉字合并成一个 token。
    「预算」「合同」「发票」这类词在中文里极其常见，漏了就是致命缺陷。
    """
    root = store.add_root(str(corpus), label="t")
    sync_root(store, root)
    for q in ("预算", "合同"):
        hits = store.search(q, limit=3)
        assert hits, f"双字词「{q}」应能检索到"
        assert "substr" in hits[0].matched_by


def test_substring_path_runs_even_when_word_path_hits(store: IndexStore, tmp_path: Path) -> None:
    """兜底路必须与前两路**平权**，不能是「前面全空才跑」。

    真实语料实测：「设备」在 5 个块里出现，word 路只命中 2 个
    （那两处前后是空格/标点，token 边界恰好对上），
    如果兜底只在「前两路全空」时触发，另外 3 个块永远召不回 ——
    而且因为返回了结果，用户根本不会怀疑漏了东西。假阴性比空结果更危险。
    """
    ws = tmp_path / "recall"
    ws.mkdir()
    # A: 「设备」独立成词，unicode61 能切出 token → word 路命中
    (ws / "a.md").write_text("# A\n设备 清单如下，共 3 台。\n", encoding="utf-8")
    # B: 「设备」嵌在长中文连串里，被合并进一个大 token → word 路必然漏
    (ws / "b.md").write_text("# B\n本次巡检发现设备状态接口响应异常需要复查。\n", encoding="utf-8")

    root = store.add_root(str(ws), label="r")
    sync_root(store, root)

    hits = store.search("设备", limit=10)
    files = {h.rel_path for h in hits}
    assert files == {"a.md", "b.md"}, f"两个文件都应召回，实际 {files}"
    assert all("设备" in h.text or "设备" in h.breadcrumb for h in hits), "不得引入不含该词的噪声"


def test_english_and_numbers_via_word_index(store: IndexStore, corpus: Path) -> None:
    root = store.add_root(str(corpus), label="t")
    sync_root(store, root)
    for q in ("procurement", "1,187,432"):
        hits = store.search(q, limit=3)
        assert hits, f"「{q}」应能检索到"


def test_no_false_positives(store: IndexStore, corpus: Path) -> None:
    """语料里没有的词必须零命中 —— 兜底路径不能把召回换成噪声。"""
    root = store.add_root(str(corpus), label="t")
    sync_root(store, root)
    assert not store.search("发票", limit=3)
    assert not store.search("zzzznonexistent", limit=3)


def test_hits_carry_locator_and_breadcrumb(store: IndexStore, corpus: Path) -> None:
    """引用要能精确到位置，否则用户无法核对。"""
    root = store.add_root(str(corpus), label="t")
    sync_root(store, root)
    hits = store.search("服务器", limit=1)
    assert hits[0].locator, "缺少定位信息"
    assert hits[0].breadcrumb.startswith("docs/budget.md"), "缺少面包屑"


# ======================================================================
# 陈旧识别 —— 索引引入的新风险
# ======================================================================
def test_verify_flags_deleted_source(store: IndexStore, corpus: Path) -> None:
    root = store.add_root(str(corpus), label="t")
    sync_root(store, root)
    hits = store.search("服务器", limit=3)
    assert hits

    (corpus / "docs" / "budget.md").unlink()
    report = verify_hits(store, hits)
    assert not report.fresh, "源文件已删除，不应还有新鲜证据"
    assert "已删除" in report.stale[0]["reason"]
    assert "请勿引用" in report.as_note()


def test_verify_flags_modified_source(store: IndexStore, corpus: Path) -> None:
    """内容改了但索引没更新 —— 这是最危险的情况：会报出错误数字。"""
    root = store.add_root(str(corpus), label="t")
    sync_root(store, root)
    hits = store.search("服务器", limit=3)

    time.sleep(0.01)
    (corpus / "docs" / "budget.md").write_text("完全不同的内容\n", encoding="utf-8")
    report = verify_hits(store, hits)
    assert not report.fresh
    assert "修改" in report.stale[0]["reason"]


def test_verify_passes_when_untouched(store: IndexStore, corpus: Path) -> None:
    root = store.add_root(str(corpus), label="t")
    sync_root(store, root)
    hits = store.search("服务器", limit=3)
    report = verify_hits(store, hits)
    assert len(report.fresh) == len(hits)
    assert not report.has_stale


# ======================================================================
# 解析与切块
# ======================================================================
def test_parse_failure_is_recorded_not_swallowed(tmp_path: Path) -> None:
    """解析失败必须留档。静默丢弃 = 用户永远搞不清为什么搜不到。"""
    f = tmp_path / "x.exe"
    f.write_bytes(b"\x00\x01\x02")
    doc = parse(f)
    assert doc.error and not doc.ok
    assert not is_supported(f)


def test_parse_handles_gbk_encoding(tmp_path: Path) -> None:
    """中文环境下 GBK 文件很常见，不能只认 UTF-8。"""
    f = tmp_path / "gbk.txt"
    f.write_bytes("季度预算报告".encode("gb18030"))
    doc = parse(f)
    assert doc.ok and "季度预算报告" in doc.blocks[0].text


def test_csv_chunks_carry_header(tmp_path: Path) -> None:
    """没有表头的数据行毫无意义 —— 「研发部 | 1200000」是预算还是实际？"""
    f = tmp_path / "t.csv"
    f.write_text("dept,budget,actual\n" + "\n".join(f"d{i},100,90" for i in range(80)),
                 encoding="utf-8")
    doc = parse(f)
    body = [b for b in doc.blocks if b.kind == "table"]
    assert len(body) >= 2
    assert all("dept,budget,actual" in b.text for b in body), "每个表格块都必须带表头"


def test_chunker_never_crosses_headings(tmp_path: Path) -> None:
    doc = ParsedDoc(blocks=[
        TextBlock("第一章", kind="heading", heading_path=["第一章"], locator={"line": 1}),
        TextBlock("内容 A" * 20, heading_path=["第一章"], locator={"line": 2}),
        TextBlock("第二章", kind="heading", heading_path=["第二章"], locator={"line": 3}),
        TextBlock("内容 B" * 20, heading_path=["第二章"], locator={"line": 4}),
    ])
    chunks = chunk_document(doc, "t.md")
    mixed = [c for c in chunks if "内容 A" in c.text and "内容 B" in c.text]
    assert not mixed, "块不应跨越标题边界"
    assert all(c.breadcrumb.startswith("t.md") for c in chunks)


def test_short_file_is_never_silently_dropped(store: IndexStore, tmp_path: Path) -> None:
    """短文件必须进索引 —— 这是最阴险的一类 bug。

    曾经 MIN_CHUNK_CHARS=24，整篇只有 22 字符的文件唯一的块被丢弃，
    文件在索引里彻底不存在，而且全程零报错：用户搜不到，也永远查不出原因。
    「搜不到」会被直接理解成「不存在」，于是索引开始生产错误结论。
    """
    ws = tmp_path / "tiny"
    ws.mkdir()
    (ws / "memo.md").write_text("# 新内容\n量子计算实验室预算 500 万元。\n", encoding="utf-8")
    (ws / "ip.txt").write_text("服务器 IP 10.0.0.5\n", encoding="utf-8")

    root = store.add_root(str(ws), label="tiny")
    prog = sync_root(store, root)
    assert prog.files_indexed == 2
    assert prog.chunks_written >= 2, "两个短文件都必须产出块"
    assert store.search("量子计算", limit=3), "短文件内容必须可检索"
    assert store.search("10.0.0.5", limit=3)


def test_single_chunk_document_survives(tmp_path: Path) -> None:
    """切块器层面的同一保证：非空文档永远至少产出一个块。"""
    doc = ParsedDoc(blocks=[TextBlock("很短", kind="heading", heading_path=["很短"],
                                      locator={"line": 1})])
    chunks = chunk_document(doc, "t.md")
    assert len(chunks) == 1 and "很短" in chunks[0].text


def test_corpus_stats_answers_what_is_here(store: IndexStore, corpus: Path) -> None:
    """回答「这里有什么」不该靠逐个 read_file。"""
    root = store.add_root(str(corpus), label="t")
    sync_root(store, root)
    st = store.corpus_stats()
    assert st["documents"] == 3
    assert st["chunks"] > 0
    assert {d["dir"] for d in st["top_directories"]} == {"docs", "data"}
    assert {e["ext"] for e in st["by_extension"]} == {"md", "csv"}
