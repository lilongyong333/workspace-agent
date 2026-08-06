# Workspace Agent

一个手写循环的文件助理 Agent：接受自然语言指令，通过一组受限工具操作指定的工作目录。
**「下一步做什么」由模型决定，不由代码写死** —— 同一个循环处理语义完全不同的任务。

**在线 Demo**：https://workspace-agent-production-50c8.up.railway.app

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
pytest tests/ -q                  # 快测试：58 项，离线、免费、秒级
pytest tests/ -q --live           # 端到端：13 项，真调模型，约 3 分钟
```

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
  tools.py      6 个工具 + 注入标记
  sandbox.py    路径边界 · 写白名单 · 无删除能力
  context.py    上下文预算与历史裁剪
  llm.py        多 provider 适配（裸 HTTP）
  trace.py      事件流 → trace.jsonl / SSE
  prompts.py    system prompt（刻意不含任何任务专用词汇）
web/
  app.py        FastAPI + SSE + 会话隔离 + 防滥用
  static/       单页界面
workspace_seed/ 只读种子（32 个文件，随仓库交付）
tests/
  golden.py     黄金答案断言库（换 workspace 只需重算常量）
```

---

## 完成度

**已完成**：两个主线任务（黄金答案 T1 13/13、T2 11/11）、五个声明的挑战点全部处理、
在线 Demo 与实时 trace、`trace.jsonl`、会话隔离、防滥用、58 快测试 + 13 端到端测试。

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
