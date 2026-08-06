"""Agent 循环 —— 本项目的核心。

题面禁止使用任何「替你跑 agent 循环」的框架，判定标准是：

    模型返回工具调用后，「执行工具 → 回填结果 → 决定继续还是终止」
    这段控制流必须是你自己写的代码。

下面这个 ``run()`` 就是那段控制流。它没有任何框架介入，
每一行的存在理由都写在注释里。

即便没有这条禁令，本项目也应该手写，因为需要三样框架不易干净暴露的东西：

1. **逐步干预点** —— 工具结果在回填前要经过截断与注入标记
2. **自定义终止语义** —— 「步数打满要交付部分结果」不是任何框架的默认行为
3. **精确的上下文控制** —— 按预算裁剪历史，而不是无脑追加
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .context import ContextManager, estimate_tokens
from .llm import LLMClient, LLMError, ToolCall
from .prompts import SYSTEM_PROMPT, budget_warning, nudge_no_tool, nudge_stuck
from .sandbox import Sandbox
from .tools import TOOL_SCHEMAS, ToolBox
from .trace import TraceRecorder


class Outcome(StrEnum):
    DONE = "done"           # 模型主动 finish
    DEGRADED = "degraded"   # 步数/预算触顶 —— **仍然交付已有产物**
    FAILED = "failed"       # 连续错误或模型调用失败


@dataclass
class RunResult:
    outcome: Outcome
    summary: str
    deliverables: list[str] = field(default_factory=list)
    steps: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """DEGRADED 也算「有交付」—— 用户宁可拿到 80% 加一句诚实说明，
        也不要一个 500 错误。只有 FAILED 才是真的空手而归。"""
        return self.outcome in (Outcome.DONE, Outcome.DEGRADED)


class _ProgressGuard:
    """检测原地打转。

    模型卡住的典型表现是反复以相同参数调用同一个工具。
    不管的话，步数会被白白烧光却毫无进展。
    """

    def __init__(self, repeat_limit: int = 3) -> None:
        self.repeat_limit = repeat_limit
        self._last_signature: str | None = None
        self._repeats = 0
        self._no_tool_strikes = 0

    def observe(self, calls: list[ToolCall]) -> bool:
        """返回 True 表示"疑似卡住"。"""
        signature = "|".join(f"{c.name}:{sorted(c.args.items())!r}" for c in calls)
        if signature == self._last_signature:
            self._repeats += 1
        else:
            self._last_signature = signature
            self._repeats = 1
        return self._repeats >= self.repeat_limit

    def no_tool(self) -> int:
        self._no_tool_strikes += 1
        return self._no_tool_strikes


class AgentRunner:
    def __init__(
        self,
        workspace: str | Path,
        llm: LLMClient | None = None,
        trace: TraceRecorder | None = None,
        max_steps: int | None = None,
        token_budget: int | None = None,
    ) -> None:
        self.sandbox = Sandbox(workspace)
        self.tools = ToolBox(
            self.sandbox,
            max_result_bytes=int(os.getenv("AGENT_TOOL_RESULT_MAX_BYTES", "8192")),
        )
        self.llm = llm or LLMClient()
        self.trace = trace or TraceRecorder()
        self.context = ContextManager()
        self.max_steps = max_steps or int(os.getenv("AGENT_MAX_STEPS", "40"))
        self.token_budget = token_budget or int(os.getenv("AGENT_TOKEN_BUDGET", "200000"))

        self.messages: list[dict[str, Any]] = []
        # 记录所有实际发生的写操作 —— DEGRADED 时靠它交付部分产物，
        # 而不是依赖模型自己声明（模型可能还没来得及说就被截断了）。
        self.written: list[str] = []

    # ==================================================================
    # 主循环
    # ==================================================================
    def run(self, task: str) -> RunResult:
        self.messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]
        guard = _ProgressGuard()
        warned = False

        for step in range(1, self.max_steps + 1):
            self.trace.step_start(step)

            # ---- ① 在预算内组装上下文 --------------------------------
            payload = self.context.build(self.messages)

            # ---- ② 模型决策 -----------------------------------------
            try:
                reply = self.llm.complete(payload[1:], tools=TOOL_SCHEMAS, system=payload[0]["content"])
            except LLMError as exc:
                return self._finish(Outcome.FAILED, f"模型调用失败：{exc}", step)

            self.trace.usage(self.llm.usage.as_dict())
            self.trace.thinking(step, reply.text)
            self.messages.append(self._assistant_message(reply))

            # ---- 模型只说话不调工具 -----------------------------------
            if not reply.tool_calls:
                if guard.no_tool() >= 2:
                    # 给过一次纠偏仍不调工具，视作它认为已经做完了。
                    # 用它最后的自然语言输出作为 summary，别丢掉信息。
                    return self._finish(
                        Outcome.DEGRADED,
                        reply.text or "模型停止调用工具且未说明原因。",
                        step,
                    )
                self.trace.note(step, "模型未调用工具，注入纠偏提示")
                self.messages.append({"role": "user", "content": nudge_no_tool()})
                continue

            # ---- ③ 执行工具 ------------------------------------------
            for call in reply.tool_calls:
                if call.name == "finish":
                    summary = str(call.args.get("summary") or "任务完成。")
                    declared = call.args.get("deliverables") or []
                    return self._finish(
                        Outcome.DONE, summary, step, declared=list(declared)
                    )

                result = self.tools.execute(call.name, call.args)

                # 记录真实写操作，作为 DEGRADED 时的产物凭据
                if result.ok:
                    for key in ("written", "moved_to"):
                        if key in result.data:
                            self.written.append(str(result.data[key]))

                self.trace.tool(step, call.name, call.args, result.summary(), result.ok)

                # ---- ④ 回填结果 --------------------------------------
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": self._render_result(result),
                    }
                )

            # ---- ⑤ 判定：继续还是终止 ---------------------------------
            if self.tools.consecutive_errors >= 5:
                return self._finish(
                    Outcome.FAILED,
                    "连续 5 次工具调用失败，终止以避免空转。",
                    step,
                )

            used = estimate_tokens(self.messages) + self.llm.usage.total_tokens
            if used >= self.token_budget:
                return self._finish(
                    Outcome.DEGRADED,
                    f"token 预算（{self.token_budget}）耗尽，已交付此前完成的产物。",
                    step,
                )

            if guard.observe(reply.tool_calls):
                self.trace.note(step, "检测到重复动作，注入纠偏提示")
                self.messages.append(
                    {"role": "user", "content": nudge_stuck(reply.tool_calls[0].name)}
                )

            # 提前预警，让模型有机会先落盘再被截断 ——
            # 这一条直接决定 DEGRADED 时还剩下什么可交付。
            remaining = self.max_steps - step
            if remaining <= 3 and not warned:
                warned = True
                self.trace.note(step, f"接近步数上限，提示模型收尾（剩余 {remaining} 步）")
                self.messages.append({"role": "user", "content": budget_warning(remaining)})

        # ---- 步数打满 ------------------------------------------------
        return self._finish(
            Outcome.DEGRADED,
            f"达到步数上限（{self.max_steps} 步），已交付此前完成的产物。",
            self.max_steps,
        )

    # ==================================================================
    # 辅助
    # ==================================================================
    def _assistant_message(self, reply: Any) -> dict[str, Any]:
        """把模型回复还原成可回传的 assistant 消息。

        直接复用提供方返回的原始结构，避免自己拼装时丢字段
        （tool_calls 的 id 必须与后续 tool 消息严格对应，否则会 400）。
        """
        if reply.raw_assistant_message:
            return reply.raw_assistant_message
        return {"role": "assistant", "content": reply.text}

    def _render_result(self, result: Any) -> str:
        """工具结果 → 回填给模型的文本。

        失败时把可用文件列表一并带上，让模型能自我纠正而不是反复猜。
        """
        import json as _json

        if result.ok:
            return _json.dumps(result.data, ensure_ascii=False)[: self.tools.max_result_bytes]

        payload: dict[str, Any] = {"error": result.error}
        if result.data.get("available"):
            payload["available_paths"] = result.data["available"]
        return _json.dumps(payload, ensure_ascii=False)

    def _finish(
        self,
        outcome: Outcome,
        summary: str,
        steps: int,
        declared: list[str] | None = None,
    ) -> RunResult:
        """收口。

        产物清单以**实际发生的写操作**为准，模型声明的作为补充。
        理由：模型可能声称写了却没写（幻觉），也可能写了却没来得及声明（被截断）。
        以真实副作用为准，两种情况都不会错。
        """
        deliverables = list(dict.fromkeys([*self.written, *(declared or [])]))
        # 复核一遍：只保留确实存在于工作目录中的路径，剔除幻觉
        verified = []
        for rel in deliverables:
            try:
                if self.sandbox.resolve(rel).exists():
                    verified.append(rel)
            except Exception:  # noqa: BLE001
                continue

        if outcome is Outcome.DEGRADED and verified:
            summary = f"{summary}\n\n已完成并落盘的产物：{', '.join(verified)}"

        self.trace.finish(outcome.value, summary, verified, steps)
        return RunResult(
            outcome=outcome,
            summary=summary,
            deliverables=verified,
            steps=steps,
            usage=self.llm.usage.as_dict(),
            events=self.trace.events,
        )
