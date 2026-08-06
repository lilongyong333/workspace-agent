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

from .context import ContextManager
from .llm import LLMClient, LLMError, ToolCall
from .prompts import (
    SYSTEM_PROMPT,
    budget_warning,
    nudge_barren,
    nudge_no_tool,
    nudge_stuck,
)
from .sandbox import Sandbox
from .tools import ToolBox, build_tool_schemas
from .trace import TraceRecorder


# 连续多少个「工具调用全军覆没」的回合后判定失败。
# 按回合而非按调用计数，理由见 run() 中 ③ 处注释。
MAX_DEAD_TURNS = 3


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

    两种打转形态，必须分别检测 —— 这是实战踩出来的教训：

    **形态 A：参数完全相同的重复调用。** 最容易想到，也最容易检测。

    **形态 B：参数每次都不同，但一直没有产出。** 更隐蔽也更常见。
    实际观察到的案例：模型为了确认一个日志文件的日期范围，
    连续用不同正则去搜（``^2025-12-2[5-9]``、``^2025-12-1[6-9]`` …），
    每次命中 0 条。因为参数不同，形态 A 的检测**完全不会触发**，
    结果 6 万多 token 烧在一个根本不必要的探测上。

    因此除了签名重复，还要追踪「这一步有没有带来新信息」。
    """

    def __init__(self, repeat_limit: int = 3, barren_limit: int = 4) -> None:
        self.repeat_limit = repeat_limit
        self.barren_limit = barren_limit
        self._last_signature: str | None = None
        self._repeats = 0
        self._barren = 0
        self._seen_reads: set[str] = set()
        self._no_tool_strikes = 0

    def observe_calls(self, calls: list[ToolCall]) -> bool:
        """形态 A：签名重复。返回 True 表示疑似卡住。"""
        signature = "|".join(f"{c.name}:{sorted(c.args.items())!r}" for c in calls)
        if signature == self._last_signature:
            self._repeats += 1
        else:
            self._last_signature = signature
            self._repeats = 1
        return self._repeats >= self.repeat_limit

    def observe_result(self, call: ToolCall, result: Any) -> None:
        """形态 B：累计"这一步有没有带来新信息"。

        判定为**有产出**的情形：
        * 成功的写/移动（改变了世界状态）
        * search 命中数 > 0
        * 首次读取某个文件
        * list_dir（结构信息总是有用的）

        其余（失败、0 命中的搜索、重复读同一文件）计为"空转"。
        """
        productive = False
        if result.ok:
            data = result.data
            if call.name in ("write_file", "move_file"):
                productive = True
            elif call.name == "search":
                productive = data.get("total_hits", 0) > 0
            elif call.name == "read_file":
                key = f"{data.get('path')}:{data.get('offset')}"
                productive = key not in self._seen_reads
                self._seen_reads.add(key)
            elif call.name == "list_dir":
                productive = True

        self._barren = 0 if productive else self._barren + 1

    def is_barren(self) -> bool:
        return self._barren >= self.barren_limit

    def reset_barren(self) -> None:
        """注入过纠偏提示后清零，给模型一次改正的机会再判定。"""
        self._barren = 0

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
        store: Any | None = None,
        root_ids: list[int] | None = None,
        read_only: bool = False,
    ) -> None:
        self.sandbox = Sandbox(workspace, read_only=read_only)
        self.tools = ToolBox(
            self.sandbox,
            max_result_bytes=int(os.getenv("AGENT_TOOL_RESULT_MAX_BYTES", "8192")),
            store=store,
            root_ids=root_ids,
        )
        # 工具清单按能力装配：
        #   有索引才暴露 describe_corpus / get_chunk；
        #   只读模式直接不给 write_file / move_file —— 模型调不到看不见的工具。
        self.tool_schemas = build_tool_schemas(
            has_index=store is not None, read_only=read_only
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
        self._notified_stale: set[str] = set()

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
        dead_turns = 0

        for step in range(1, self.max_steps + 1):
            self.trace.step_start(step)

            # ---- ① 在预算内组装上下文 --------------------------------
            payload = self.context.build(self.messages)

            # ---- ② 模型决策 -----------------------------------------
            try:
                reply = self.llm.complete(
                    payload[1:], tools=self.tool_schemas, system=payload[0]["content"]
                )
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
            # 统计本回合的成败。**必须按回合计，不能按单次调用计** ——
            # 模型可以在一个回合里并行发出十几个调用，而它只在回合结束后
            # 才看到全部结果。若按调用数判死，一次批量幻觉文件名就会被算成
            # "连续 N 次失败"直接终止，模型连一次纠正机会都没有。
            # （实测踩过：一个回合 8 次成功 + 5 次幻觉路径，被误判为致命失败。）
            turn_ok = 0
            turn_failed = 0

            for call in reply.tool_calls:
                if call.name == "finish":
                    summary = str(call.args.get("summary") or "任务完成。")
                    declared = call.args.get("deliverables") or []
                    return self._finish(
                        Outcome.DONE, summary, step, declared=list(declared)
                    )

                result = self.tools.execute(call.name, call.args)
                guard.observe_result(call, result)
                if result.ok:
                    turn_ok += 1
                else:
                    turn_failed += 1

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

            # 证据校验发现陈旧内容 —— 必须显式告知模型，
            # 否则它只会看到"少了几条结果"，不知道那是被主动丢弃的。
            for note in self.tools.stale_notes:
                if note not in self._notified_stale:
                    self._notified_stale.add(note)
                    self.trace.note(step, "检测到索引陈旧，已丢弃失效证据并提示模型")
                    self.messages.append({"role": "user", "content": note})

            # ---- ⑤ 判定：继续还是终止 ---------------------------------
            # 只有**整个回合一无所获**才算一次失败回合。
            # 部分成功（哪怕只有一个）说明模型仍在推进，应当继续。
            if turn_ok == 0 and turn_failed > 0:
                dead_turns += 1
                if dead_turns >= MAX_DEAD_TURNS:
                    return self._finish(
                        Outcome.FAILED,
                        f"连续 {MAX_DEAD_TURNS} 个回合的工具调用全部失败，终止以避免空转。",
                        step,
                    )
                self.trace.note(
                    step, f"本回合 {turn_failed} 次调用全部失败（{dead_turns}/{MAX_DEAD_TURNS}）"
                )
            else:
                dead_turns = 0

            # 预算判定用**实际消耗的 API token**。
            #
            # 早先这里写的是 estimate_tokens(messages) + usage.total_tokens，
            # 那是重复计数：usage.total_tokens 已经把每一轮重发的历史都算进去了，
            # 再加一次当前历史的估算，会让预算提前约 20% 触顶。
            # 实测中一次任务实际只花了 64.6k，却被判成 80k 而提前终止。
            spent = self.llm.usage.total_tokens
            if spent >= self.token_budget:
                return self._finish(
                    Outcome.DEGRADED,
                    f"token 预算（{self.token_budget}）耗尽，已交付此前完成的产物。",
                    step,
                )

            # 打转检测：两种形态分别处理
            if guard.observe_calls(reply.tool_calls):
                self.trace.note(step, "检测到重复动作，注入纠偏提示")
                self.messages.append(
                    {"role": "user", "content": nudge_stuck(reply.tool_calls[0].name)}
                )
            elif guard.is_barren():
                # 参数每次都变、但一直没有新信息 —— 比重复调用更隐蔽也更烧钱
                self.trace.note(step, "连续多步无新信息，提示改变策略")
                self.messages.append({"role": "user", "content": nudge_barren()})
                guard.reset_barren()

            # 提前预警，让模型有机会先落盘再被截断 ——
            # 这一条直接决定 DEGRADED 时还剩下什么可交付。
            #
            # 必须同时看步数与 token 两种压力：早先只看步数，
            # 结果任务在第 10/40 步就撞上 token 预算，预警根本没机会触发，
            # DEGRADED 来得毫无征兆。
            pressure = max(step / self.max_steps, spent / max(self.token_budget, 1))
            if pressure >= 0.75 and not warned:
                warned = True
                left_steps = self.max_steps - step
                left_tokens = max(self.token_budget - spent, 0)
                self.trace.note(
                    step, f"接近预算上限，提示模型收尾（剩 {left_steps} 步 / {left_tokens} tokens）"
                )
                self.messages.append(
                    {"role": "user", "content": budget_warning(left_steps, left_tokens)}
                )

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
