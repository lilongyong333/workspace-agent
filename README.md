# Workspace Agent

一个手写循环的文件助理 Agent：接受自然语言指令，通过一组受限工具操作指定的工作目录。
**「下一步做什么」由模型决定，不由代码写死** —— 同一个循环处理语义完全不同的任务。

两种用法：

* **任务模式** —— 指向一个可写工作目录，让它整理、归档、生成索引（笔试主线）。
* **检索模式** —— 把**任意文件夹**注册进索引，对着自己的真实资料提问。
  这个模式下 agent **只读**：写工具根本不出现在它的工具清单里。

**在线 Demo**：https://agent.llynb.cc

> 打开即可用，页面上有三个示例任务（跨文件索引 / 受控清理 / 自由提问）。
> 右侧是工作区文件树，可点开看内容；运行后新增的文件会高亮。
> 每个浏览器会话有独立的工作区副本，多人同时试用互不干扰，随时可「重置工作区」。

---

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env          # 填入 LLM_API_KEY
python agent.py --reset --workspace ./workspace --task "把 drafts 里过期的草稿归档到 archive"
```

`--reset` 会从只读种子 `workspace_seed/` 重建一份可写工作目录。
`--workspace` 可指向**任意目录** —— 本 agent 不依赖任何特定文件名或内容。

运行结束会在当前目录产出 `trace.jsonl`，每步一行：

```json
{"step": 2, "tool": "search", "args": {"pattern": "Project Falcon"}, "result_summary": "14 hits across 10 files"}
```

也可以 `python -m agent ...`，两者等价。

### 检索模式：指向你自己的文件夹

```bash
python agent.py index add --path "D:/公司资料" --label docs
python agent.py index sync --label docs
python agent.py index search "第四季度 预算"        # 直接检索，不经模型
python agent.py ask --label docs --task "预算超支的部门有哪些？给出处" --out 回答.md
```

`ask` 全程**只读**：`write_file` / `move_file` 不在工具清单里，
所以无论提问怎么写、语料里埋了什么诱导，agent 都改不了你的文件。
需要留档就加 `--out` —— 落盘由 CLI 完成，发生在 agent 之外。

支持 pdf / docx / xlsx / pptx / csv / md / 代码 / html 等；`index status` 可查当前
哪些格式可解析、哪些文件解析失败。

### 起 Web Demo

```bash
uvicorn web.app:app --reload      # http://localhost:8000
```

或用 Docker（与线上跑同一个镜像，可复现性的硬保证）：

```bash
docker build -t workspace-agent . && docker run -p 8000:8000 --env-file .env workspace-agent
```

### 跑测试

```bash
pytest tests/ -q                  # 快测试：93 项，离线、免费、约 3 秒
pytest tests/ -q --live           # 追加 5 项端到端：真调模型，约 3 分钟
```

分档不只是省钱：**快测试挂了就没必要浪费一次 live 运行去发现同样的问题。**

---

## 设计要点

完整架构见 [docs/DESIGN.md](docs/DESIGN.md)，需求与验收标准见 [docs/SPEC.md](docs/SPEC.md)，
安全分析见 [docs/THREAT-MODEL.md](docs/THREAT-MODEL.md)，四问答案见 [NOTES.md](NOTES.md)。

### 循环是手写的

题面禁用一切「替你跑 agent 循环」的框架。`agent/loop.py` 里的 `run()` 就是那段控制流：
**组装上下文 → 模型决策 → 执行工具 → 回填结果 → 判定继续或终止**。
模型调用走裸 `httpx`，没有任何 SDK 封装。

三态终止：`DONE`（模型 finish）/ `DEGRADED`（步数或预算触顶，**已落盘产物全部保留**）/ `FAILED`（连续错误）。

### 用接口形状引导模型，而不是用提示词哀求

`read_file` 的签名里**没有「读全文」这个选项**，只有 `offset` + `limit`，返回体带 `total_lines` 与 `has_more`。
`search` 只返回 `path:line` 与命中行，**返回体大小与被搜文件大小无关**。

效果：面对 12,000 行 / 950KB 的日志，模型自己转向 `search`，再按命中行号精确读十几行窗口。

> 提示词会被模型在上下文压力下忽略，接口不会。

### 最强的安全防线是「能力不存在」

工具集里**没有任何删除能力**。工作区里两处注入都要求删文件 ——
只要这个能力不存在，无论模型被说服到什么程度，攻击都无法达成。
另有一条测试锁死这条约束（`test_no_delete_capability_exists`）。

其余四层：文件内容一律包进 `<file_content>` 标签并声明为数据、注入特征标记（**只标记不删改**，
因为被注入的文件本身往往是任务必须处理的对象）、路径沙箱、写操作审计。

同一条思路延伸出**只读模式**：检索用户自己的资料目录时，`write_file` / `move_file`
直接不进工具清单。这条边界不依赖提示词 —— 模型调用不了一个它看不见的工具。

> 实测：让它「把结论写一份 summary.md 到资料库根目录」，它照常完成了检索与引用，
> 然后如实报告「本环境未提供任何写入工具，无法落盘」，**没有谎称已写入**。
> 语料 39 个文件前后逐一比对，零改动。

### 检索：两条 FTS 路 + 一条子串兜底，RRF 融合

SQLite FTS5 建两份索引 —— `unicode61`（英文、标识符、数字）与 `trigram`（中文、子串），
用 RRF（倒数排名融合，k=60）合并。**不同索引的 BM25 分数不可比，但排名可比**，
所以既不用归一化也不用调权重，单路失效也不影响整体。

第三条路解决中文的真实盲区：`trigram` 需要 ≥3 字符，而 `unicode61` 会把相邻汉字
合并成一个 token（「超出预算」是一个词）—— 于是「预算」「合同」「发票」这类
**双字词两条路都搜不到**。补一条子串扫描兜底，与前两路平权参与融合。

索引会陈旧，这是引入索引后的**新风险**：命中的证据可能已经被改过。
每条命中在交给模型前都用内容哈希复核，过期的直接剔除，并明确告知模型剔了什么。

---

## 公网部署的防滥用做法

API key 挂在公网服务后面，四层防护，阈值全部走环境变量：

| 层 | 机制 | 变量 |
|---|---|---|
| 1 | 访问口令（留空则不校验，便于本地开发） | `DEMO_ACCESS_CODE` |
| 2 | 单 IP 每小时任务数上限 | `DEMO_RATE_LIMIT_PER_HOUR`（默认 20） |
| 3 | **单次任务 token 硬上限**，触顶转 `DEGRADED` 而非无限跑 | `DEMO_MAX_TOKENS_PER_TASK`（默认 8 万） |
| 4 | 全局日 token 预算，用尽即拒绝新任务 | `DEMO_DAILY_TOKEN_BUDGET`（默认 200 万） |

外加 Cloudflare 在最前面做 DNS 与 DDoS 防护。配额状态在 `GET /api/config` 可见。

第 3 层是关键：**限流只防高频，防不住一个把步数跑满的恶意长任务**，所以必须有单任务成本上限。

---

## 环境变量

| 变量 | 说明 | 必需 |
|---|---|---|
| `LLM_API_KEY` | 模型 API key，**仅服务端读取** | ✅ |
| `LLM_PROVIDER` | `deepseek` / `openai` / `gemini` / `zhipu` / `moonshot` / `anthropic` | |
| `LLM_MODEL` | 如 `deepseek-v4-flash` | |
| `AGENT_MAX_STEPS` | 步数上限，默认 40 | |
| `AGENT_TOKEN_BUDGET` | token 预算，默认 20 万 | |

换模型提供方只需改环境变量，循环代码一行不动。

---

## 项目结构

```
agent/
  loop.py       手写 agent 循环 ★ 核心
  tools.py      6 个基础工具 + 2 个索引工具 + 注入标记
  sandbox.py    路径边界 · 只读模式 · 无删除能力
  context.py    上下文预算与历史裁剪
  llm.py        多 provider 适配（裸 HTTP）
  trace.py      事件流 → trace.jsonl / SSE
  prompts.py    system prompt（刻意不含任何任务专用词汇）
  index/
    store.py    SQLite FTS5 双索引 + 子串兜底 + RRF 融合
    parsers.py  pdf/docx/xlsx/pptx/csv/… → 结构化块（含编码兜底）
    chunker.py  结构感知切块：不跨标题、表格带表头、块带面包屑
    indexer.py  三级增量同步（mtime+size → sha256 → 重解析）
    verify.py   命中新鲜度复核 —— 索引陈旧 = 会报出错误数字
web/
  app.py        FastAPI + SSE + 会话隔离 + 防滥用 + 索引管理 API
  static/       单页界面（⌘K 检索面板、流式光标、目录注册）
workspace_seed/ 只读种子（32 个文件，随仓库交付）
tests/
  golden.py     黄金答案断言库（换 workspace 只需重算常量）
```

---

## 完成度

**已完成**：两个主线任务（黄金答案 T1 13/13、T2 11/11）、五个声明的挑战点全部处理、
在线 Demo 与实时 trace、`trace.jsonl`、会话隔离、防滥用、93 快测试 + 5 端到端测试。

**超出题面最低要求的部分**：任意文件夹索引与检索（pdf/docx/xlsx/pptx/…）、
中文检索的三路召回、增量同步、证据新鲜度复核、只读边界、Web 端目录注册与 ⌘K 检索。
逐项交付说明与自验方法见 [docs/PROGRESS.md](docs/PROGRESS.md)。

**已知不足**（详见 [NOTES.md](NOTES.md)）：

- 无产物自检回路 —— 索引写完不会自己复核每条是否成立。**这是我认为最该补的一件事。**
- 多文件读取是串行的，未做并行工具调用。
- 历史摘要用规则拼接而非模型总结，长任务下质量一般。
- 注入检测是特征匹配，新型注入可能漏检（兜底靠「无删除能力」这一层）。
- 单任务闭环，无多轮会话记忆。

**一处需求歧义与我的取舍**：题面「生成 `archive/MANIFEST.md`，每行 `- <文件名>`」
可严格读作「文件中只能有 `- xxx` 行」，也可读作「登记条目用此格式」。
我取宽松读法（允许 markdown 标题），因为模型加不加标题是随机的，
按无关格式细节断死会造出 flaky 测试。判据写在 `tests/golden.py` 注释里。

---

## License

MIT
