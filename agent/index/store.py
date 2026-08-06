"""索引存储层 —— SQLite + FTS5。

## 为什么是 SQLite 而不是 Elasticsearch / 向量库

这个产品的形态是**跑在用户自己设备上的文件助理**。SQLite 是嵌入式的，
单文件、零依赖、不需要用户装任何服务 —— 这一条压倒一切。

而 FTS5 的 BM25 实现质量很高：对「找 E007 这个错误码」这类精确查询，
它比向量检索**更准**，因为向量会把精确标识符糊成语义邻居。

## 双词法索引 + RRF

单一分词器覆盖不了中英混合语料：

* ``unicode61`` —— 有词边界，英文/代码/标识符的 BM25 排序好，索引小
* ``trigram``   —— 三元组切分，**中文与任意子串都能匹配**，但英文排序略糙

两个索引各查一次，用 RRF（Reciprocal Rank Fusion）融合排名。
RRF 不需要调权重、不需要训练，对两路结果做倒数加权即可，鲁棒性很好。

这也为后续接入向量检索留好了位置 —— 再加一路候选进 RRF 就行，
架构不用改。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 已注册的根目录。用户可以注册任意多个。
CREATE TABLE IF NOT EXISTS roots (
    id            INTEGER PRIMARY KEY,
    path          TEXT UNIQUE NOT NULL,
    label         TEXT NOT NULL,
    include_globs TEXT NOT NULL DEFAULT '[]',
    exclude_globs TEXT NOT NULL DEFAULT '[]',
    created_at    INTEGER NOT NULL,
    last_scan_at  INTEGER,
    status        TEXT NOT NULL DEFAULT 'idle'
);

-- 文档级：一个文件一行
CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY,
    root_id        INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    rel_path       TEXT NOT NULL,
    abs_path       TEXT NOT NULL,
    ext            TEXT,
    size_bytes     INTEGER NOT NULL,
    -- mtime + size 用于快速跳过；content_sha256 才是权威判据
    mtime_ns       INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    parser         TEXT,
    parsed_at      INTEGER,
    -- 解析失败必须落库。静默丢弃会让用户问"我明明有这份合同，为什么搜不到"
    parse_error    TEXT,
    title          TEXT,
    UNIQUE(root_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_documents_root ON documents(root_id);
CREATE INDEX IF NOT EXISTS idx_documents_sha  ON documents(content_sha256);

-- 块级：检索的最小单位
CREATE TABLE IF NOT EXISTS chunks (
    id             INTEGER PRIMARY KEY,
    doc_id         INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal        INTEGER NOT NULL,
    -- 面包屑：文件名 > 章节 > 表头。让块脱离上下文也能被理解，
    -- 是切块环节收益最大、最容易被忽略的一招。
    breadcrumb     TEXT NOT NULL DEFAULT '',
    text           TEXT NOT NULL,
    -- JSON，因为定位信息形态各异：PDF 是页码、Excel 是 sheet+行、代码是行号
    locator        TEXT NOT NULL DEFAULT '{}',
    content_sha256 TEXT NOT NULL,
    token_estimate INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

-- 词法索引 A：英文 / 代码 / 标识符
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_word USING fts5(
    breadcrumb, text,
    content='chunks', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

-- 词法索引 B：中文 / 任意子串
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts_gram USING fts5(
    breadcrumb, text,
    content='chunks', content_rowid='id',
    tokenize='trigram'
);

-- external content 模式必须靠触发器保持同步
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts_word(rowid, breadcrumb, text) VALUES (new.id, new.breadcrumb, new.text);
    INSERT INTO chunks_fts_gram(rowid, breadcrumb, text) VALUES (new.id, new.breadcrumb, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts_word(chunks_fts_word, rowid, breadcrumb, text)
        VALUES ('delete', old.id, old.breadcrumb, old.text);
    INSERT INTO chunks_fts_gram(chunks_fts_gram, rowid, breadcrumb, text)
        VALUES ('delete', old.id, old.breadcrumb, old.text);
END;

-- 索引作业记录：用于观测与排障
CREATE TABLE IF NOT EXISTS index_runs (
    id             INTEGER PRIMARY KEY,
    root_id        INTEGER NOT NULL,
    started_at     INTEGER NOT NULL,
    finished_at    INTEGER,
    files_seen     INTEGER NOT NULL DEFAULT 0,
    files_indexed  INTEGER NOT NULL DEFAULT 0,
    files_skipped  INTEGER NOT NULL DEFAULT 0,
    files_failed   INTEGER NOT NULL DEFAULT 0,
    files_removed  INTEGER NOT NULL DEFAULT 0,
    chunks_written INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);
"""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
@dataclass
class Root:
    id: int
    path: str
    label: str
    include_globs: list[str] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    last_scan_at: int | None = None
    status: str = "idle"


@dataclass
class Hit:
    chunk_id: int
    doc_id: int
    root_label: str
    rel_path: str
    abs_path: str
    breadcrumb: str
    text: str
    locator: dict[str, Any]
    content_sha256: str
    score: float
    matched_by: list[str] = field(default_factory=list)


class IndexStore:
    """索引库。默认落在 ``<repo>/.index/index.db``。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "IndexStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 根目录
    # ------------------------------------------------------------------
    def add_root(
        self,
        path: str | Path,
        label: str | None = None,
        include_globs: Sequence[str] = (),
        exclude_globs: Sequence[str] = (),
    ) -> Root:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"不是一个目录: {resolved}")
        label = label or resolved.name
        self.conn.execute(
            """INSERT INTO roots(path, label, include_globs, exclude_globs, created_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                   label=excluded.label,
                   include_globs=excluded.include_globs,
                   exclude_globs=excluded.exclude_globs""",
            (
                str(resolved),
                label,
                json.dumps(list(include_globs)),
                json.dumps(list(exclude_globs)),
                int(time.time()),
            ),
        )
        self.conn.commit()
        return self.get_root(path=str(resolved))  # type: ignore[return-value]

    def get_root(self, *, root_id: int | None = None, label: str | None = None,
                 path: str | None = None) -> Root | None:
        if root_id is not None:
            row = self.conn.execute("SELECT * FROM roots WHERE id=?", (root_id,)).fetchone()
        elif label is not None:
            row = self.conn.execute("SELECT * FROM roots WHERE label=?", (label,)).fetchone()
        elif path is not None:
            row = self.conn.execute("SELECT * FROM roots WHERE path=?", (path,)).fetchone()
        else:
            return None
        return self._row_to_root(row) if row else None

    def list_roots(self) -> list[Root]:
        return [self._row_to_root(r) for r in self.conn.execute("SELECT * FROM roots ORDER BY id")]

    def remove_root(self, root_id: int) -> None:
        self.conn.execute("DELETE FROM roots WHERE id=?", (root_id,))
        self.conn.commit()

    @staticmethod
    def _row_to_root(row: sqlite3.Row) -> Root:
        return Root(
            id=row["id"],
            path=row["path"],
            label=row["label"],
            include_globs=json.loads(row["include_globs"]),
            exclude_globs=json.loads(row["exclude_globs"]),
            last_scan_at=row["last_scan_at"],
            status=row["status"],
        )

    def set_root_status(self, root_id: int, status: str, scanned: bool = False) -> None:
        if scanned:
            self.conn.execute(
                "UPDATE roots SET status=?, last_scan_at=? WHERE id=?",
                (status, int(time.time()), root_id),
            )
        else:
            self.conn.execute("UPDATE roots SET status=? WHERE id=?", (status, root_id))
        self.conn.commit()

    # ------------------------------------------------------------------
    # 文档
    # ------------------------------------------------------------------
    def get_document_state(self, root_id: int) -> dict[str, tuple[int, int, int, str]]:
        """返回 {rel_path: (doc_id, mtime_ns, size, sha256)}，供增量比对。

        一次性取回全部状态而不是逐个查询 —— 10 万文件下这是数量级的差别。
        """
        rows = self.conn.execute(
            "SELECT id, rel_path, mtime_ns, size_bytes, content_sha256 FROM documents WHERE root_id=?",
            (root_id,),
        )
        return {r["rel_path"]: (r["id"], r["mtime_ns"], r["size_bytes"], r["content_sha256"])
                for r in rows}

    def upsert_document(self, **kw: Any) -> int:
        cur = self.conn.execute(
            """INSERT INTO documents
                 (root_id, rel_path, abs_path, ext, size_bytes, mtime_ns,
                  content_sha256, parser, parsed_at, parse_error, title)
               VALUES (:root_id,:rel_path,:abs_path,:ext,:size_bytes,:mtime_ns,
                       :content_sha256,:parser,:parsed_at,:parse_error,:title)
               ON CONFLICT(root_id, rel_path) DO UPDATE SET
                 abs_path=excluded.abs_path, ext=excluded.ext,
                 size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns,
                 content_sha256=excluded.content_sha256, parser=excluded.parser,
                 parsed_at=excluded.parsed_at, parse_error=excluded.parse_error,
                 title=excluded.title
               RETURNING id""",
            kw,
        )
        return int(cur.fetchone()[0])

    def touch_document(self, doc_id: int, mtime_ns: int) -> None:
        """内容没变、只是 mtime 变了 —— 更新时间戳即可，不必重新解析。"""
        self.conn.execute("UPDATE documents SET mtime_ns=? WHERE id=?", (mtime_ns, doc_id))

    def delete_documents(self, doc_ids: Iterable[int]) -> int:
        ids = list(doc_ids)
        if not ids:
            return 0
        self.conn.executemany("DELETE FROM documents WHERE id=?", [(i,) for i in ids])
        return len(ids)

    def get_document(self, doc_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()

    # ------------------------------------------------------------------
    # 块
    # ------------------------------------------------------------------
    def replace_chunks(self, doc_id: int, chunks: Sequence[dict[str, Any]]) -> int:
        """整篇替换。文档变了就全量重建它的块 —— 比做差异比对简单且不会出错。"""
        self.conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        self.conn.executemany(
            """INSERT INTO chunks(doc_id, ordinal, breadcrumb, text, locator,
                                  content_sha256, token_estimate)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    doc_id, i, c.get("breadcrumb", ""), c["text"],
                    json.dumps(c.get("locator", {}), ensure_ascii=False),
                    sha256_text(c["text"]), max(1, len(c["text"]) // 3),
                )
                for i, c in enumerate(chunks)
            ],
        )
        return len(chunks)

    def get_chunk(self, chunk_id: int) -> Hit | None:
        row = self.conn.execute(
            """SELECT c.*, d.rel_path, d.abs_path, r.label AS root_label
               FROM chunks c JOIN documents d ON d.id=c.doc_id
                             JOIN roots r ON r.id=d.root_id
               WHERE c.id=?""",
            (chunk_id,),
        ).fetchone()
        return self._row_to_hit(row, 1.0, ["direct"]) if row else None

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        limit: int = 20,
        root_ids: Sequence[int] | None = None,
        path_prefix: str | None = None,
        ext: Sequence[str] | None = None,
    ) -> list[Hit]:
        """双索引检索 + RRF 融合。

        两个索引对同一查询会给出不同排名：``unicode61`` 擅长英文词与标识符，
        ``trigram`` 擅长中文与子串。RRF 用排名倒数加权融合，
        既不用调权重也不用训练，且单路失败不影响整体。
        """
        if not query.strip():
            return []

        rankings: list[list[int]] = []
        matched: dict[int, list[str]] = defaultdict(list)

        for table, tag in (("chunks_fts_word", "word"), ("chunks_fts_gram", "gram")):
            ids = self._fts_query(table, query, limit * 3, root_ids, path_prefix, ext)
            if ids:
                rankings.append(ids)
                for i in ids:
                    matched[i].append(tag)

        # 第三路：短 CJK 词的子串扫描。
        #
        # 为什么必须有这一路：中文里「预算」「合同」「采购」「发票」这类
        # **双字词极其常见**，但两条 FTS 路都覆盖不到 ——
        #   * trigram 需要 ≥3 字符才能构成三元组，2 字查询无法匹配
        #   * unicode61 会把相邻汉字合并成单个 token（「超出预算」是一个词），
        #     搜「预算」匹配不上
        #
        # 这一路曾经写成 `if not rankings`（只在前两路全空时兜底），
        # 是个隐蔽的召回 bug：真实语料实测「设备」原文存在于 5 个块，
        # word 路恰好命中 2 个（那两处「设备」前后是空格或标点，token 边界对上了），
        # 于是兜底不再触发，另外 3 个「设备」嵌在长中文连串里的块被永久漏掉 ——
        # 而且返回了结果，用户不会怀疑漏了。
        #
        # 正确做法是让它和前两路平权，由 RRF 融合排名。
        # 代价是一次全表 LIKE 扫描（万级块约几十毫秒）；
        # 子串匹配是精确的，多跑一路只增召回不引噪声。
        if _has_short_cjk(query):
            ids = self._like_query(query, limit * 3, root_ids, path_prefix, ext)
            if ids:
                rankings.append(ids)
                for i in ids:
                    matched[i].append("substr")

        if not rankings:
            return []

        fused = _rrf(rankings)[: limit]
        return self._hydrate([cid for cid, _ in fused],
                             {cid: s for cid, s in fused}, matched)

    def _fts_query(
        self, table: str, query: str, limit: int,
        root_ids: Sequence[int] | None, path_prefix: str | None, ext: Sequence[str] | None,
    ) -> list[int]:
        where = [f"{table} MATCH ?"]
        params: list[Any] = [_to_fts_query(query, gram=table.endswith("gram"))]

        if root_ids:
            where.append(f"d.root_id IN ({','.join('?' * len(root_ids))})")
            params += list(root_ids)
        if path_prefix:
            where.append("d.rel_path LIKE ?")
            params.append(f"{path_prefix.rstrip('/')}/%")
        if ext:
            where.append(f"d.ext IN ({','.join('?' * len(ext))})")
            params += [e.lower().lstrip(".") for e in ext]

        # breadcrumb 权重 2.0、正文 1.0 —— 命中标题/表头比命中正文更说明相关
        sql = f"""
            SELECT c.id
            FROM {table}
            JOIN chunks c ON c.id = {table}.rowid
            JOIN documents d ON d.id = c.doc_id
            WHERE {' AND '.join(where)}
            ORDER BY bm25({table}, 2.0, 1.0)
            LIMIT ?
        """
        params.append(limit)
        try:
            return [r[0] for r in self.conn.execute(sql, params)]
        except sqlite3.OperationalError:
            # 查询语法不被该分词器接受（例如 trigram 下的短词），跳过这一路
            return []

    def _like_query(
        self, query: str, limit: int,
        root_ids: Sequence[int] | None, path_prefix: str | None, ext: Sequence[str] | None,
    ) -> list[int]:
        """子串扫描兜底。按词命中数排序，命中越多越靠前。"""
        terms = [t for t in query.split() if t][:5] or [query]
        where = ["(" + " OR ".join("c.text LIKE ? OR c.breadcrumb LIKE ?" for _ in terms) + ")"]
        params: list[Any] = []
        for t in terms:
            pat = f"%{t}%"
            params += [pat, pat]

        if root_ids:
            where.append(f"d.root_id IN ({','.join('?' * len(root_ids))})")
            params += list(root_ids)
        if path_prefix:
            where.append("d.rel_path LIKE ?")
            params.append(f"{path_prefix.rstrip('/')}/%")
        if ext:
            where.append(f"d.ext IN ({','.join('?' * len(ext))})")
            params += [e.lower().lstrip(".") for e in ext]

        score = " + ".join("(c.text LIKE ?)" for _ in terms)
        params += [f"%{t}%" for t in terms]

        sql = f"""
            SELECT c.id, ({score}) AS hits
            FROM chunks c JOIN documents d ON d.id = c.doc_id
            WHERE {' AND '.join(where)}
            ORDER BY hits DESC, length(c.text) ASC
            LIMIT ?
        """
        params.append(limit)
        try:
            return [r[0] for r in self.conn.execute(sql, params)]
        except sqlite3.OperationalError:
            return []

    def _hydrate(
        self, chunk_ids: Sequence[int], scores: dict[int, float], matched: dict[int, list[str]]
    ) -> list[Hit]:
        if not chunk_ids:
            return []
        rows = self.conn.execute(
            f"""SELECT c.*, d.rel_path, d.abs_path, r.label AS root_label
                FROM chunks c JOIN documents d ON d.id=c.doc_id
                              JOIN roots r ON r.id=d.root_id
                WHERE c.id IN ({','.join('?' * len(chunk_ids))})""",
            list(chunk_ids),
        ).fetchall()
        by_id = {r["id"]: r for r in rows}
        return [
            self._row_to_hit(by_id[cid], scores.get(cid, 0.0), matched.get(cid, []))
            for cid in chunk_ids if cid in by_id
        ]

    @staticmethod
    def _row_to_hit(row: sqlite3.Row, score: float, matched: list[str]) -> Hit:
        return Hit(
            chunk_id=row["id"], doc_id=row["doc_id"], root_label=row["root_label"],
            rel_path=row["rel_path"], abs_path=row["abs_path"],
            breadcrumb=row["breadcrumb"], text=row["text"],
            locator=json.loads(row["locator"] or "{}"),
            content_sha256=row["content_sha256"], score=score, matched_by=matched,
        )

    # ------------------------------------------------------------------
    # 统计 —— 回答"这里有什么"不该靠逐个读文件
    # ------------------------------------------------------------------
    def corpus_stats(self, root_ids: Sequence[int] | None = None) -> dict[str, Any]:
        cond, params = ("WHERE root_id IN (%s)" % ",".join("?" * len(root_ids)), list(root_ids)) \
            if root_ids else ("", [])

        total = self.conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(size_bytes),0) b FROM documents {cond}", params
        ).fetchone()
        by_ext = self.conn.execute(
            f"""SELECT ext, COUNT(*) n, SUM(size_bytes) b FROM documents {cond}
                GROUP BY ext ORDER BY n DESC LIMIT 25""", params
        ).fetchall()
        failed = self.conn.execute(
            f"""SELECT rel_path, parse_error FROM documents
                {cond + (' AND' if cond else 'WHERE')} parse_error IS NOT NULL LIMIT 20""", params
        ).fetchall()
        chunks = self.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        top_dirs = self.conn.execute(
            f"""SELECT CASE WHEN instr(rel_path,'/')>0
                            THEN substr(rel_path,1,instr(rel_path,'/')-1) ELSE '(根)' END AS d,
                       COUNT(*) n
                FROM documents {cond} GROUP BY d ORDER BY n DESC LIMIT 20""", params
        ).fetchall()

        return {
            "documents": total["n"],
            "total_bytes": total["b"],
            "chunks": chunks,
            "by_extension": [{"ext": r["ext"] or "(无)", "count": r["n"], "bytes": r["b"]}
                             for r in by_ext],
            "top_directories": [{"dir": r["d"], "count": r["n"]} for r in top_dirs],
            "parse_failures": [{"path": r["rel_path"], "error": r["parse_error"]} for r in failed],
            "roots": [{"id": r.id, "label": r.label, "path": r.path,
                       "last_scan_at": r.last_scan_at, "status": r.status}
                      for r in self.list_roots()],
        }

    # ------------------------------------------------------------------
    def start_run(self, root_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO index_runs(root_id, started_at) VALUES (?,?) RETURNING id",
            (root_id, int(time.time())),
        )
        # 必须先取完 RETURNING 的结果再 commit —— 游标未消费时提交会报
        # "cannot commit transaction - SQL statements in progress"
        run_id = int(cur.fetchone()[0])
        self.conn.commit()
        return run_id

    def finish_run(self, run_id: int, **counts: Any) -> None:
        fields = ", ".join(f"{k}=?" for k in counts)
        self.conn.execute(
            f"UPDATE index_runs SET finished_at=?, {fields} WHERE id=?",
            [int(time.time()), *counts.values(), run_id],
        )
        self.conn.commit()

    def last_run(self, root_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM index_runs WHERE root_id=? ORDER BY id DESC LIMIT 1", (root_id,)
        ).fetchone()


# ----------------------------------------------------------------------
def _rrf(rankings: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion。

    对每一路结果按**排名**（而非分数）取倒数加权求和。
    好处是不同索引的 BM25 分数不可直接比较，但排名可以 ——
    因此不需要归一化，也不需要调权重。k=60 是文献里的经验值。
    """
    scores: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: -kv[1])


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿" or "぀" <= ch <= "ヿ"


def _has_short_cjk(query: str) -> bool:
    """查询中是否存在长度 < 3 的 CJK 词 —— 那是两条 FTS 路的共同盲区。"""
    return any(len(t) < 3 and any(_is_cjk(c) for c in t) for t in (query.split() or [query]))


def _to_fts_query(query: str, gram: bool) -> str:
    """把自然语言查询转成 FTS5 表达式。

    做两件事：转义双引号、把每个词包成短语。
    对 trigram 索引，长度 < 3 的词会被丢弃（三元组索引对更短的词无能为力），
    全被丢弃时退化为整串短语查询。
    """
    cleaned = query.replace('"', " ").strip()
    if not cleaned:
        return '""'
    terms = [t for t in cleaned.split() if t]
    if gram:
        terms = [t for t in terms if len(t) >= 3] or [cleaned]
    return " OR ".join(f'"{t}"' for t in terms)
