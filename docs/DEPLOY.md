# 部署手册

> 目标：`agent.llynb.cc` → Railway 容器。
> 策略：**先部署空壳把管道跑通，再往里填 agent 核心。**

---

## 为什么不用 Cloudflare

`llynb.cc` 已托管在 Cloudflare，但 Cloudflare **跑不了这个应用**：

| 产品 | 能跑吗 | 原因 |
|---|---|---|
| Pages | ❌ | 静态托管，无 Python 运行时 |
| Workers (JS/TS) | ❌ | V8 isolate，**无文件系统** |
| Workers Python (Pyodide) | ❌ | beta，**同样无可写文件系统** |

这个 Agent 的全部工作就是在文件系统上移动文件：

```python
sandbox.move("drafts/x.md", "archive/x.md")
```

Workers 里没有对应物。**这不是配置问题，是执行模型不兼容。**

**解法**：应用跑在 Railway（真实 Linux 容器），Cloudflare 仍然在最前面做 DNS + DDoS 防护 + 限流。域名照用，还白拿一层防护。

---

## 架构

```
用户
 └─→ agent.llynb.cc          Cloudflare DNS (CNAME, 橙色云可开)
      └─→ xxx.up.railway.app  Railway 容器
           └─→ Docker 镜像     python:3.11-slim + FastAPI + uvicorn
                └─→ sessions/  每会话从 workspace_seed/ 复制的独立副本
```

### 为什么临时文件系统对我们是好事

Railway 容器重启会重置文件系统。一般项目这是麻烦，**对本项目恰好契合**：

| 本项目设计 | Railway 特性 | 结果 |
|---|---|---|
| 每会话从 seed 复制独立副本 | 重启回到镜像初始态 | ✅ 天然一致 |
| 需要「重置 workspace」按钮 | 重置 = 重新复制 seed | ✅ 实现简单 |
| 无任何需长期保存的数据 | 不必挂持久卷 | ✅ 省成本省配置 |

**不需要 Railway Volume。**

---

## 部署步骤

### 步骤 1 — 代码推到 GitHub

Railway 从 GitHub 仓库部署，所以先要有远端仓库。

**方式 A：已装并登录 `gh`**

```bash
cd ~/Desktop/workspace-agent
gh repo create workspace-agent --public --source=. --remote=origin --push
```

**方式 B：手动**

1. 打开 https://github.com/new，仓库名 `workspace-agent`，选 **Public**（题目要求公开），**不要**勾选任何初始化文件
2. 然后：

```bash
cd ~/Desktop/workspace-agent
git remote add origin https://github.com/<你的用户名>/workspace-agent.git
git push -u origin main
```

> ⚠️ 推送前确认 `.env` **没有**进入版本控制：
> ```bash
> git ls-files | grep -c "^\.env$"    # 必须输出 0
> ```

### 步骤 2 — Railway 建项目

1. 打开 https://railway.com，**用 GitHub 账号登录**（不需要绑信用卡）
2. `New Project` → `Deploy from GitHub repo` → 授权并选择 `workspace-agent`
3. Railway 会自动识别根目录的 `Dockerfile` 并开始构建

> 若它错误地选了 Nixpacks：进入服务 → `Settings` → `Build` → 把 Builder 改为 **Dockerfile**。

### 步骤 3 — 配置环境变量

Railway 服务页 → `Variables` → 逐条添加：

| 变量 | 值 |
|---|---|
| `LLM_PROVIDER` | `deepseek` |
| `LLM_API_KEY` | 你的 DeepSeek key |
| `LLM_MODEL` | `deepseek-v4-flash` |
| `LLM_BASE_URL` | `https://api.deepseek.com` |
| `AGENT_MAX_STEPS` | `40` |
| `AGENT_TOKEN_BUDGET` | `200000` |
| `DEMO_ACCESS_CODE` | 自定一个口令（防滥用） |
| `DEMO_RATE_LIMIT_PER_HOUR` | `20` |
| `DEMO_MAX_TOKENS_PER_TASK` | `80000` |
| `DEMO_DAILY_TOKEN_BUDGET` | `2000000` |

> **不要**设置 `PORT` —— Railway 自动注入，Dockerfile 里已用 `${PORT}` 接收。

### 步骤 4 — 拿到公网地址

`Settings` → `Networking` → `Generate Domain`，得到 `xxx.up.railway.app`。

**验证部署成功**：

```bash
curl https://xxx.up.railway.app/health
```

期望输出：

```json
{
  "status": "ok",
  "seed_file_count": 32,
  "seed_present": true,
  "config": { "llm_api_key_present": true, ... }
}
```

`seed_file_count` 必须是 **32**；不是就说明 `.dockerignore` 误排除了种子文件。
`llm_api_key_present` 必须是 `true`；不是就说明环境变量没生效。

### 步骤 5 — 绑定自定义域名

**Railway 侧**：`Settings` → `Networking` → `Custom Domain` → 填 `agent.llynb.cc`，它会给你一个 CNAME 目标值。

**Cloudflare 侧**：DNS → `Add record`

| 字段 | 值 |
|---|---|
| Type | `CNAME` |
| Name | `agent` |
| Target | Railway 给的那个值 |
| Proxy | 🟠 Proxied（开启，白拿 DDoS 防护与限流） |

> 若开橙色云后出现证书错误：Cloudflare → SSL/TLS → 加密模式设为 **Full (strict)**。

---

## 本地用同一个镜像跑

这是可复现性的硬保证——**本地和线上跑的是同一个 Docker 镜像**：

```bash
docker build -t workspace-agent .
docker run -p 8000:8000 --env-file .env workspace-agent
# → http://localhost:8000/health
```

不用 Docker 也行：

```bash
pip install -r requirements.txt
uvicorn web.app:app --reload
```

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 构建失败 `UnicodeDecodeError` | `requirements.txt` 含非 ASCII 字符（Windows 上 pip 用 GBK 读） | 保持该文件纯 ASCII |
| `/health` 返回 `seed_file_count: 0` | `.dockerignore` 误排除了 `workspace_seed/` | 检查该文件 |
| `llm_api_key_present: false` | 环境变量未生效 | Railway `Variables` 页核对，改完会自动重部署 |
| 容器起来就退出 | 端口没绑对 | 确认 `CMD` 里用了 `${PORT}` 且 host 是 `0.0.0.0` |
| 自定义域名 525/526 | Cloudflare SSL 模式不对 | 改为 Full (strict) |
| 每次重启文件没了 | 正常现象 | 本项目按设计不需要持久化，见上文 |

---

## 成本

| 项 | 说明 |
|---|---|
| Railway | $5/月免费额度。本应用空闲时几乎不耗，评审试玩期间预计消耗 < $1 |
| DeepSeek | 单次 T1/T2 任务约 ¥0.01–0.05；日预算上限已在环境变量里设死 |
| Cloudflare | 免费档足够 |
