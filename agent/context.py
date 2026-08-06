"""上下文预算 —— 决定「往模型脑子里塞什么、不塞什么」。

长任务的上下文是线性膨胀的：每一步的工具结果都会永久留在 messages 里。
不管的话，二十步之后前面的内容就会把窗口撑爆。

三道闸：

1. **单次工具结果上限** —— 在 tools.py 里施加（截断 + 提示改用更精确的调用）
2. **历史裁剪** —— 本模块：保留 system、原始任务、最近 K 轮完整消息，
   更早的轮次折叠成一段结论性摘要
3. **总 token 预算** —— 在 loop.py 里施加（触顶转 DEGRADED）

裁剪最容易犯的错是**把任务本身挤掉**，导致 agent 跑着跑着忘了要干嘛。
因此第 0、1 条消息（system 与原始任务）被硬性钉死，永不参与裁剪。
"""

from __future__ import annotations

import json
from typing import Any

# 保留最近多少轮的完整消息。太小会丢细节，太大等于没裁。
DEFAULT_KEEP_RECENT = 12
# 早期轮次折叠成的摘要长度上限。
SUMMARY_MAX_CHARS = 1500


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """粗略估算 token 数。

    刻意不引入 tiktoken 之类的依赖：本项目要跨多个模型提供方，
    每家分词器都不同，精确计数意义不大。这里只需要一个**单调的信号**
    来触发裁剪与预算保护，量级对了就够用。

    经验系数：中英混合文本约 3 字符/token。
    """
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif content is not None:
            total += len(json.dumps(content, ensure_ascii=False))
        for tc in m.get("tool_calls") or []:
            total += len(json.dumps(tc, ensure_ascii=False))
    return total // 3


class ContextManager:
    def __init__(self, keep_recent: int = DEFAULT_KEEP_RECENT) -> None:
        self.keep_recent = keep_recent

    def build(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """产出这一轮实际发给模型的消息列表。

        前两条（system + 原始任务）永不裁剪；中间部分超长时折叠成摘要。
        """
        if len(messages) <= self.keep_recent + 2:
            return messages

        head = messages[:2]                       # system + 原始任务，钉死
        middle = messages[2:-self.keep_recent]
        tail = messages[-self.keep_recent :]

        # 裁剪不能把 tool 消息与它对应的 assistant tool_calls 拆散，
        # 否则多数提供方会直接 400。向前扩展 tail 直到边界干净。
        while tail and tail[0].get("role") == "tool" and middle:
            tail.insert(0, middle.pop())

        if not middle:
            return head + tail

        return head + [self._summarize(middle)] + tail

    def _summarize(self, chunk: list[dict[str, Any]]) -> dict[str, Any]:
        """把早期轮次折叠成**结论性**摘要。

        刻意只保留「做过什么、得到什么结论」，丢弃过程细节：
        agent 后续决策需要的是"我已经知道 X"，不是"我当时读了哪 200 行"。
        """
        tools_used: list[str] = []
        facts: list[str] = []

        for m in chunk:
            for tc in m.get("tool_calls") or []:
                fn = (tc.get("function") or {}).get("name")
                args = (tc.get("function") or {}).get("arguments")
                if fn:
                    brief = str(args)[:90] if args else ""
                    tools_used.append(f"{fn}({brief})")
            if m.get("role") == "tool":
                text = str(m.get("content") or "")
                # 只留每条工具结果的头部 —— 通常摘要信息就在最前面
                facts.append(text[:200].replace("\n", " "))

        body = (
            "【早期步骤已折叠】\n"
            f"已执行的工具调用（按顺序）：{'; '.join(tools_used[:40])}\n"
            f"已获得的信息片段：{' | '.join(facts[:25])}"
        )[:SUMMARY_MAX_CHARS]

        return {"role": "assistant", "content": body}
