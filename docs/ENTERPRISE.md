# 从 Demo 到企业级 —— 差距分析与落地路线

> 当前系统是一个**设计良好的玩具**：架构清晰、安全边界扎实、测试完备，
> 但它的每一个假设都建立在"32 个小文本文件"之上。
>
> 本文档回答：**要变成"指向任意文件夹就能用"的真实产品，还差什么，怎么一步步补。**

---

## 第一部分 · 诚实的现状评估

### 1.1 会在哪里崩

| 维度 | 现在 | 崩溃点 | 根因 |
|---|---|---|---|
| **检索性能** | 全量线性扫描 | **1 万文件即不可用** | `search()` 每次调用重新读每个文件，无索引 |
| **文件格式** | 仅 UTF-8 文本 | **遇到 PDF/Office 即失效** | 无解析层，二进制被当文本读成乱码 |
| **语料来源** | 固定 `workspace_seed/` | 用户无法指向自己的目录 | 路径是硬编码的种子复制 |
| **数据新鲜度** | 每次从磁盘读（总是新鲜） | 加索引后会引入陈旧 | 无变更检测机制 |
| **并发** | 单进程 + 内存 dict | 多用户互相拖慢 | 无队列、无 worker 池 |
| **持久化** | 会话在重启后消失 | 无法回溯、无法续跑 | 无数据库 |
| **多租户** | 一个共享口令 | 无法区分用户、无法隔离数据 | 无认证与 ACL |
| **成本控制** | 全局日预算 | 一个用户能耗尽所有人的额度 | 配额没有按主体划分 |
| **可观测** | trace.jsonl + SSE | 无法回答"上周三谁花了多少钱" | 无指标、无长期存储 |
| **质量保障** | 15 条黄金答案 | 换模型/改提示词即失控 | 无评测集、无 CI 门禁 |

### 1.2 有哪些设计是对的、可以保留

不是全部重写。下面这些经得起放大：

| 已有设计 | 为什么能撑住 |
|---|---|
| **能力边界（无删除工具）** | 规模越大越重要。企业场景要升级成**权限模型**，但"默认无破坏性能力"这个原则不变 |
| **路径沙箱** | 需要从进程级升级到租户级，但 `resolve()` 的实现是对的 |
| **手写循环 + 三态终止** | 与规模无关，直接复用 |
| **接口形状引导模型**（强制分页、返回体与文件大小解耦） | 规模越大越关键 |
| **注入"只标记不删改"** | 语料越杂，注入越多，这个策略更需要 |
| **trace 与 SSE 同源** | 可观测性的正确起点，往上接指标系统即可 |

---

## 第二部分 · 路线图总览

```
P1 检索内核        →  P2 检索质量       →  P3 平台化        →  P4 可运营
任意文件夹能用         答得准             多人能用            能长期跑
─────────────         ─────────────      ─────────────      ─────────────
SQLite FTS5 索引       混合检索           多租户 + ACL        指标 + 成本归因
多格式解析             语义向量           任务队列            评测集 + CI 门禁
增量同步               重排序             对象存储            审计日志
内容哈希溯源           查询改写           SSO                 SLO 与告警

约 3–5 天              约 3–5 天          约 1–2 周           持续
```

**每个阶段都独立可用**，不是全做完才有价值。P1 做完就已经是一个真实可用的本地文件搜索 agent。

---

## 第三部分 · P1 检索内核（最关键的一步）

> **目标**：`python -m agent index --path D:/我的资料`，然后就能问它任何问题。
> 支持 PDF/Word/Excel/PPT/代码/文本，10 万文件量级毫秒响应。

### 3.1 为什么选 SQLite + FTS5

| 方案 | 优点 | 缺点 | 判断 |
|---|---|---|---|
| **SQLite FTS5** | 零依赖、单文件、内置 BM25、百万文档毫秒级、事务安全 | 单机 | ✅ **选它** |
| Elasticsearch / OpenSearch | 分布式、功能全 | 要起集群、内存大户、运维成本高 | P3 再考虑 |
| Meilisearch / Typesense | 易用、快 | 额外进程、中文分词需配置 | 备选 |
| 纯向量库（Chroma/Qdrant） | 语义强 | **精确匹配差**（找"E007"这种反而不准）、成本高 | 只作补充 |
| 继续裸 grep | 零成本 | 万级文件即死 | ❌ |

**关键理由**：这个产品的形态是**跑在用户自己设备上**的文件助理。
SQLite 是嵌入式的，不需要用户装任何服务——这一条压倒一切。
而且 FTS5 的 BM25 实现质量很高，对"找某个具体词"这类查询比向量检索**更准**。

> 中文分词：FTS5 默认 tokenizer 对中文按字切分，效果尚可但不理想。
> 方案是自定义 tokenizer（用 jieba 预分词后存入索引），P2 再优化。

### 3.2 数据模型

```sql
-- 文档级：一个文件一行
CREATE TABLE documents (
    id            INTEGER PRIMARY KEY,
    root_id       INTEGER NOT NULL,          -- 属于哪个已注册的根目录
    rel_path      TEXT    NOT NULL,
    abs_path      TEXT    NOT NULL,
    mime          TEXT,
    size_bytes    INTEGER,
    mtime_ns      INTEGER,                   -- 快速判断"可能变了"
    content_sha256 TEXT   NOT NULL,          -- 权威判断"确实变了"
    parsed_at     INTEGER,
    parser        TEXT,                      -- 用哪个解析器抽出来的
    parse_error   TEXT,                      -- 解析失败也要记录，不能静默丢
    UNIQUE(root_id, rel_path)
);

-- 块级：检索的最小单位
CREATE TABLE chunks (
    id            INTEGER PRIMARY KEY,
    doc_id        INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,          -- 在文档中的序号
    text          TEXT    NOT NULL,
    -- 定位信息：让引用能精确到位置，而不只是"在这个文件里"
    locator       TEXT    NOT NULL,          -- JSON: {page:3} / {line:120} / {sheet:"Q1",row:45}
    token_estimate INTEGER,
    content_sha256 TEXT   NOT NULL           -- 块级哈希，用于答案时效校验
);

-- 全文索引（external content 模式，不重复存文本）
CREATE VIRTUAL TABLE chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

-- 已注册的根目录
CREATE TABLE roots (
    id            INTEGER PRIMARY KEY,
    path          TEXT UNIQUE NOT NULL,
    label         TEXT,
    include_globs TEXT,                       -- JSON 数组
    exclude_globs TEXT,
    last_scan_at  INTEGER,
    status        TEXT                        -- idle | scanning | error
);

-- 索引作业记录，用于观测与排障
CREATE TABLE index_runs (
    id            INTEGER PRIMARY KEY,
    root_id       INTEGER,
    started_at    INTEGER, finished_at INTEGER,
    files_seen    INTEGER, files_parsed INTEGER,
    files_skipped INTEGER, files_failed INTEGER,
    chunks_written INTEGER,
    error         TEXT
);
```

**三个设计要点**：

1. **`content_sha256` 同时存在文档级和块级**。文档级用于增量同步（跳过没变的文件），
   块级用于**回答时校验**——见 §3.6。
2. **`parse_error` 必须落库**。解析失败的文件不能静默消失，否则用户会问
   "我明明有这份合同，为什么搜不到"。
3. **`locator` 存 JSON 而非固定字段**。PDF 是页码、Excel 是 sheet+行、
   代码是行号——用 JSON 才能统一。

### 3.3 解析层：从"只能读文本"到"什么都能读"

```
agent/ingest/
├─ registry.py      按扩展名 + magic bytes 分派
├─ parsers/
│  ├─ text.py       txt/md/log/json/yaml/代码 —— 直接读，检测编码
│  ├─ pdf.py        pypdf 优先，失败回退 pdfplumber；扫描件走 OCR
│  ├─ office.py     python-docx / openpyxl / python-pptx
│  ├─ email.py      .eml / .msg
│  ├─ archive.py    zip/tar 内部递归（限深度与总量，防 zip bomb）
│  └─ ocr.py        可选，tesseract / PaddleOCR
└─ chunker.py       结构感知切块
```

**每个解析器的统一契约**：

```python
@dataclass
class ParsedDoc:
    blocks: list[TextBlock]      # 带定位信息的文本块
    meta: dict[str, Any]         # 标题、作者、创建时间等
    parser: str
    warnings: list[str]          # 例如"第 12 页是图片，未 OCR"

@dataclass
class TextBlock:
    text: str
    locator: dict[str, str | int]   # {"page": 3} / {"sheet": "Q1", "row": 45}
    kind: str                       # heading | paragraph | table | code | caption
```

**关键决策：不做"全文一坨"，要保留结构。**
因为 `kind` 和 `locator` 直接决定了两件事：切块质量、以及引用能不能精确到页。

### 3.4 切块策略（这是检索质量的地基）

**通用错误做法**：固定 512 字符硬切。这会把一个表格切成两半、把一句话拦腰截断。

**本方案**：结构感知 + 重叠滑窗

```python
def chunk(doc: ParsedDoc, target=800, overlap=120, hard_max=1500) -> list[Chunk]:
    """
    规则优先级：
    1. 永不跨越 heading 边界 —— 标题是天然的语义分割点
    2. 表格整体成块（超长则按行分组，但每块都带表头）
    3. 代码按函数/类边界切（用 ast 或缩进启发式）
    4. 其余按段落累积到 target，段落间保留 overlap
    5. 每块前置"面包屑"：文件名 > 章节标题 —— 让块脱离上下文也能被理解
    """
```

**面包屑是最容易被忽略但收益最大的一招**：

```
[财务/2025Q4预算.xlsx > Sheet: 部门明细 > 表头: 部门|预算|实际]
研发部 | 1,200,000 | 1,187,432
```

没有面包屑的话，检索到"研发部 | 1,200,000"这一行，模型不知道这是预算还是实际、哪一年、哪个表。

### 3.5 增量同步：只处理变了的

```python
def sync(root: Root) -> IndexRun:
    """
    三级判断，从便宜到贵：
    1. mtime_ns + size 都没变        → 跳过（99% 的文件走这条）
    2. mtime 变了但 content_sha256 相同 → 只更新 mtime（触碰但没改内容）
    3. content_sha256 变了            → 重新解析 + 重建该文档的所有块
    另外：数据库里有、磁盘上没有的 → 删除（文件被移走/删除）
    """
```

**性能预期**（实测量级）：

| 场景 | 10 万文件 |
|---|---|
| 首次全量索引 | 20–60 分钟（取决于 PDF 占比） |
| 无变更增量扫描 | **10–30 秒**（只 stat，不读内容） |
| 100 个文件变更 | < 1 分钟 |

**实时性**：用 `watchdog` 监听文件系统事件，防抖 2 秒后触发增量索引。
用户保存一个文档，几秒后就能搜到。

### 3.6 ⚠️ 索引引入的新风险：陈旧答案

**这是加索引最容易被忽略的代价。**

现在的实现每次都从磁盘读，所以**永远是新鲜的**。加了索引之后：

> 用户删了一份合同 → 索引还在 → agent 引用了一份已经不存在的文件
> 用户改了金额 → 索引是旧的 → agent 报了错误的数字

**解法：回答前做证据校验**

```python
def verify_evidence(chunks: list[Chunk]) -> tuple[list[Chunk], list[str]]:
    """回答前逐块校验：源文件是否仍存在、内容哈希是否仍匹配。

    不匹配的块**直接丢弃**，并触发该文档的重新索引。
    如果丢弃后证据不足，就如实告诉用户"相关文档已变更，正在重新索引"，
    而不是拿旧数据编一个答案。
    """
    valid, stale = [], []
    for c in chunks:
        doc = store.get_document(c.doc_id)
        if not Path(doc.abs_path).exists():
            stale.append(f"{doc.rel_path}（已删除）"); continue
        if current_sha256(doc.abs_path) != doc.content_sha256:
            stale.append(f"{doc.rel_path}（已修改）")
            queue_reindex(doc); continue
        valid.append(c)
    return valid, stale
```

> 这与前面 Growatt 那道笔试里 `content_sha256` 的要求是同一个思路：
> **索引必须能证明自己没过期，否则就要拒绝使用。**

### 3.7 工具层的改造

现有 6 个工具保留，新增/改造 3 个：

| 工具 | 变化 |
|---|---|
| `search` | **底层换成 FTS5**，保留原签名。新增 `mode` 参数：`index`（默认，快）/ `live`（绕过索引直接扫，用于验证新鲜度） |
| `list_dir` | 不变（仍走文件系统） |
| `read_file` | 不变（仍走文件系统，保证读到的是最新内容） |
| **`semantic_search`**（新） | 向量检索，P2 上线 |
| **`get_chunk`**（新） | 按 chunk_id 取带定位信息的原文，供引用 |
| **`describe_corpus`**（新） | 返回语料统计：多少文件、什么类型、时间跨度、最近变更——回答"这里有什么"这类问题不用逐个读文件 |

**`describe_corpus` 解决的正是你撞到的那个问题**：
问"这个工作区里都有些什么"时，模型不该逐个 `read_file`，
而应该一次拿到结构化统计。

### 3.8 P1 的交付标准

```bash
# 注册任意目录
python -m agent index add --path "D:/我的资料" --label "工作文档"

# 首次索引（带进度）
python -m agent index sync --label "工作文档"
# → 扫描 12,483 个文件 | 解析 11,902 | 跳过 501（不支持格式）| 失败 80
# → 生成 87,431 个块 | 耗时 8m32s

# 直接问
python -m agent ask --label "工作文档" --task "去年 Q4 的服务器采购一共花了多少钱？列出相关文档"
```

**验收清单**：

- [ ] 支持 txt/md/pdf/docx/xlsx/pptx/csv/常见代码 ≥ 10 种格式
- [ ] 1 万文件语料，单次 `search` P95 < 100 ms
- [ ] 增量扫描（无变更）1 万文件 < 30 秒
- [ ] 解析失败的文件在 `index status` 中可见，不静默丢失
- [ ] 引用能精确到「文件 + 页码/行号/单元格」
- [ ] 文件被改动后，回答会拒用陈旧块并触发重索引
- [ ] 加密/损坏文件不会中断整个索引作业

---

## 第四部分 · P2 检索质量

> **P1 让它能跑，P2 让它答得准。**

### 4.1 混合检索

单一 BM25 的盲区：用户问"服务器采购花了多少"，文档里写的是"IT 硬件支出"。
词不匹配，BM25 完全找不到。

```
查询
 ├─→ BM25（FTS5）           精确词、专有名词、错误码  → 候选 50
 ├─→ 向量检索（语义）        同义、改写、跨语言        → 候选 50
 └─→ 元数据过滤             时间范围、文件类型、目录  → 收窄
         ↓
   RRF 融合（Reciprocal Rank Fusion）
         ↓
   重排序（cross-encoder 或 LLM 打分）  → 最终 top 8
```

**RRF 是关键**：不需要调权重，对两路结果的排名做倒数加权，鲁棒且无需训练。

```python
def rrf(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    scores = defaultdict(float)
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: -x[1])
```

**向量方案选择**（本地优先）：

| 方案 | 说明 |
|---|---|
| `sqlite-vec` 扩展 | 和 FTS5 同一个库，零额外依赖 ✅ 推荐 |
| `bge-small-zh` / `text-embedding-3-small` | 前者本地跑（~130MB），后者调 API |
| Qdrant / LanceDB | 语料超千万时再上 |

### 4.2 查询改写

用户问的和文档写的往往不是一套话术。在检索**之前**加一步：

```python
# 用小模型（便宜）把问题扩展成多个检索式
"去年Q4服务器采购花了多少"
  → ["2025 Q4 服务器 采购 金额", "IT 硬件 支出 第四季度",
     "server procurement Q4 2025 cost"]
```

三路都检索，结果 RRF 融合。**成本极低（一次小模型调用），召回提升显著。**

### 4.3 引用与可验证性

企业场景的硬要求：**每个数字都要能点回原文**。

```json
{
  "answer": "2025 Q4 服务器采购共计 ¥1,187,432",
  "citations": [{
    "doc": "财务/2025Q4预算.xlsx",
    "locator": {"sheet": "部门明细", "row": 45},
    "excerpt": "研发部 | 1,200,000 | 1,187,432",
    "content_sha256": "a3f2...",
    "verified_at": "2026-08-07T10:23:11Z"
  }]
}
```

前端点击引用 → 打开原文 → **高亮到具体单元格**。

---

## 第五部分 · P3 平台化

### 5.1 从单进程到可服务

```
              ┌──────────────┐
浏览器 ──SSE──│  API (FastAPI)│──入队──┐
              └──────────────┘        │
                     │                ▼
                     │          ┌──────────┐
              ┌──────▼──────┐   │  Redis   │
              │ PostgreSQL  │   │  队列    │
              │ 会话/审计/  │   └────┬─────┘
              │ 配额        │        │
              └─────────────┘   ┌────▼─────────┐
                                │ Agent Worker │ ×N
                                │ (独立进程)    │
                                └────┬─────────┘
                                     │
                            ┌────────▼────────┐
                            │ 索引服务         │
                            │ SQLite/PG+pgvec │
                            └─────────────────┘
```

**为什么必须拆 worker**：现在 agent 跑在 API 进程的线程里。
一个长任务会占住线程，多用户时互相拖慢，且**进程重启就丢**。
拆出去之后：任务可排队、可重试、可续跑、可限流到具体用户。

### 5.2 多租户与权限

```sql
CREATE TABLE tenants (id, name, plan, created_at);
CREATE TABLE users (id, tenant_id, email, role);          -- owner|admin|member|viewer
CREATE TABLE root_grants (root_id, subject_type, subject_id, permission);
                                    -- user|group        -- read|write|admin
```

**三条必须守住的规则**：

1. **检索时过滤，不是检索后过滤。** ACL 条件要下推到 SQL WHERE，
   否则会出现"搜到了但显示无权限"——这本身就泄漏了文件存在性。
2. **能力边界升级为权限模型。** 现在是"谁都不能删"；
   企业版是"有 `write` 权限的用户，其 agent 才拥有 `move_file` 工具"。
   工具集**按用户权限动态装配**，而不是全局固定。
3. **每个租户独立索引库。** 物理隔离比逻辑隔离可靠。

### 5.3 会话持久化与审计

```sql
CREATE TABLE runs (
  id, tenant_id, user_id, root_id, task, outcome,
  steps, tokens_in, tokens_out, cost_cents,
  started_at, finished_at
);
CREATE TABLE run_events (run_id, seq, type, payload_json, at);
CREATE TABLE audit_log (
  tenant_id, user_id, action, target_path, before_sha, after_sha, at
);
```

**审计日志是企业采购的硬门槛**。
"这个 AI 上周把我的文件移到哪去了" 必须能回答，且不可篡改。

---

## 第六部分 · P4 可运营

### 6.1 可观测

| 层 | 采集什么 |
|---|---|
| **业务指标** | 任务成功率、平均步数、DEGRADED 比例、引用被点击率 |
| **检索指标** | 召回@k、检索延迟 P50/P95、零结果查询率 |
| **成本指标** | 按租户/用户/任务归因的 token 与金额 |
| **系统指标** | 队列深度、worker 利用率、索引滞后时长 |

**零结果查询率**是最被低估的指标——它直接指出用户想要但你没索引到的内容。

技术栈：OpenTelemetry → Prometheus + Grafana，或直接接 Langfuse / Phoenix。

### 6.2 评测集与 CI 门禁

**这是从"能跑"到"敢改"的关键。**

现在的 15 条黄金答案是好起点，但企业级需要：

```
evals/
├─ retrieval/          200+ 条 (查询, 应命中的文档) 对
├─ e2e/                50+ 条 (任务, 验收断言)
├─ adversarial/        注入、越权、诱导删除
└─ regression/         每个线上 bug 都固化成一条
```

**CI 门禁**：

```yaml
- 快测试（离线）        每次 push
- 检索评测（无 LLM）    每次 push，召回@10 不得低于基线 2%
- E2E 评测（真 LLM）    每晚 + 发布前，成功率不得低于基线 5%
- 对抗评测             每次 push，必须 100% 通过（安全不容退化）
```

**换模型、改提示词、调切块策略——都必须先过评测。** 否则你永远不知道
"我这次优化到底是变好了还是变坏了"。

### 6.3 成本控制

| 手段 | 效果 |
|---|---|
| 提示词缓存（system + 工具定义打断点） | 输入成本降 60–90% |
| 分层模型（路由/摘要用小模型，推理用大模型） | 综合成本降 40%+ |
| 检索结果去重与压缩 | 减少无效 token |
| 按租户配额 + 软硬两级告警 | 防止单点烧穿 |
| 结果缓存（相同查询 + 索引未变 → 直接返回） | 高频问题成本归零 |

---

## 第七部分 · 前端重构：从"能看"到"有质感"

### 7.1 现在缺什么

当前界面是**功能正确但缺乏设计系统**：颜色硬编码、间距随意、无动效体系、
无键盘交互、无空状态设计、无骨架屏。

「高级感」不是加渐变和阴影，而是**一致性 + 克制 + 细节**。

### 7.2 设计系统（第一步，也是最重要的一步）

**用语义令牌，不用硬编码颜色**：

```css
:root {
  /* 基础色阶 —— 只在这里定义原始值 */
  --stone-950:#191817; --stone-900:#211f1e; --stone-850:#262423;
  --stone-800:#2e2b29; --stone-700:#3a3634; --stone-500:#6f675f;
  --stone-300:#a29a90; --stone-100:#eeece6;
  --clay-500:#d97757; --clay-400:#e08a6d;
  --sage-500:#7fb069; --amber-500:#d9a441; --rust-500:#d76a6a;

  /* 语义令牌 —— 组件只用这一层 */
  --bg-canvas: var(--stone-950);
  --bg-surface: var(--stone-900);
  --bg-raised: var(--stone-850);
  --bg-hover: var(--stone-800);
  --border-subtle: var(--stone-700);
  --border-strong: var(--stone-500);
  --text-primary: var(--stone-100);
  --text-secondary: var(--stone-300);
  --text-tertiary: var(--stone-500);
  --accent: var(--clay-500);
  --success: var(--sage-500);

  /* 8pt 网格 */
  --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px;
  --sp-6:24px; --sp-8:32px; --sp-12:48px;

  /* 模块化字阶（1.25 比例） */
  --fs-xs:11px; --fs-sm:12.5px; --fs-base:14px;
  --fs-lg:17.5px; --fs-xl:22px; --fs-2xl:27px;

  /* 动效 —— 只用这三条曲线 */
  --ease-out: cubic-bezier(.16,1,.3,1);
  --ease-spring: cubic-bezier(.34,1.56,.64,1);
  --dur-fast:120ms; --dur-base:200ms; --dur-slow:320ms;

  /* 层级 —— 用语义而非随手写 box-shadow */
  --elev-1: 0 1px 2px rgb(0 0 0/.24);
  --elev-2: 0 4px 12px rgb(0 0 0/.28);
  --elev-3: 0 12px 32px rgb(0 0 0/.36);
}
```

**光这一步就能让界面质感提升一大截**，因为它消除了不一致。

### 7.3 具体要加的东西（按性价比排序）

| # | 功能 | 为什么显高级 | 工作量 |
|---|---|---|---|
| **1** | **⌘K 命令面板** | 键盘优先是专业工具的标志（Linear/Raycast/Vercel 都有） | 1 天 |
| **2** | **流式打字光标 + 逐字渐显** | 让等待变成"正在思考"而不是"卡住了" | 半天 |
| **3** | **骨架屏替代 spinner** | spinner 说"不知道要多久"，骨架屏说"马上就好" | 半天 |
| **4** | **工具调用卡片可展开/折叠** | 默认折叠只显示摘要，点开看完整参数与结果 | 1 天 |
| **5** | **引用高亮联动** | 鼠标悬停引用 → 右侧文件预览自动滚动并高亮那一行 | 1 天 |
| **6** | **虚拟滚动文件树** | 10 万文件不卡（react-window / 手写 IntersectionObserver） | 1 天 |
| **7** | **微交互**（spring 动效） | 卡片入场、按钮按压、数字滚动 —— 只动 transform/opacity | 1 天 |
| **8** | **明暗双主题 + 系统跟随** | 语义令牌铺好后，改 3 行就能切 | 半天 |
| **9** | **真实空状态与错误态** | 空状态不是"暂无数据"，而是引导下一步动作 | 半天 |
| **10** | **密度切换**（舒适/紧凑） | 专业用户要信息密度 | 半天 |

### 7.4 关于动效的三条铁律

1. **只动 `transform` 和 `opacity`**。动 `width/height/top/left` 会触发布局重排，掉帧。
2. **时长 120–320ms**。超过 400ms 感觉迟钝，低于 100ms 看不见。
3. **进场用 spring，退场用 ease-out**。进场需要"活泼"，退场要"干脆"。

```css
@keyframes slide-in {
  from { opacity: 0; transform: translateY(6px) }
  to   { opacity: 1; transform: none }
}
.tool-card { animation: slide-in var(--dur-base) var(--ease-spring) }
```

### 7.5 技术栈建议

| 现在 | 建议 | 理由 |
|---|---|---|
| 单文件原生 HTML/JS | **保留**，或升级 SvelteKit / React+Vite | 现在这套够用且零构建；若功能继续膨胀再升级 |
| 手写 CSS | **加设计令牌层**（不必上 Tailwind） | 令牌解决 90% 的一致性问题 |
| 无组件库 | 需要时引 Radix / shadcn | 命令面板、Dialog、Tooltip 这类不值得手写 |
| 无状态管理 | 事件流够用 | 除非加多标签页/多会话 |

**我的判断**：不要为了"高级"去换框架。**先铺设计令牌 + 加那 10 个交互细节**，
质感提升的 80% 来自这里，成本只有重写的 1/10。

---

## 第八部分 · 落地节奏建议

| 阶段 | 内容 | 时长 | 完成后能干什么 |
|---|---|---|---|
| **P1a** | SQLite 索引 + 文本/Markdown/代码解析 + 增量同步 | 1.5 天 | **指向任意文件夹，秒级搜索** |
| **P1b** | PDF / Office 解析 + 结构感知切块 | 1.5 天 | 能读真实办公文档 |
| **P1c** | 证据校验（内容哈希）+ `describe_corpus` 工具 | 1 天 | 不会给出陈旧答案 |
| **F1** | 设计令牌 + ⌘K + 流式光标 + 骨架屏 | 1.5 天 | **界面质感跃升** |
| **P2a** | 向量检索 + RRF 混合 | 2 天 | 同义/改写也能找到 |
| **P2b** | 查询改写 + 重排序 + 引用定位 | 2 天 | 答得准且可验证 |
| **F2** | 引用联动 + 虚拟滚动 + 微交互 + 双主题 | 2 天 | 达到商用产品水准 |
| **P3** | 多租户 + 队列 + 持久化 + 审计 | 1–2 周 | 多人可用 |
| **P4** | 观测 + 评测集 + CI 门禁 + 成本 | 持续 | 敢改、能运营 |

**建议顺序**：`P1a → P1b → F1 → P1c → P2a → F2 → P2b → P3`

理由：**P1a+P1b 做完就是一个真正可用的产品**（任意文件夹 + 多格式 + 快）。
紧接着做 F1（前端质感）是因为它成本低、感知强，能立刻验证价值。
P3 平台化最贵，但只有在确认产品方向后才值得投。

---

## 附：一句话总结

> **现在这个系统的每个假设都建立在"32 个小文本文件"上。**
> 让它变成真产品，最关键的一步不是加功能，而是**把"每次全量扫描"换成"索引 + 增量同步 + 证据校验"**——
> 这一步解决了性能、格式、任意目录三个问题，也带来一个新问题（陈旧答案），
> 而那个新问题的解法（内容哈希校验）恰好是企业级检索系统最核心的正确性保证。
