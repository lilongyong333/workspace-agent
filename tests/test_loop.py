"""循环控制流测试。

这里每一条都对应一个**真实发生过的 bug**。它们是用一句最普通的提问
「这个工作区里都有些什么？帮我总结一下」跑出来的 —— 那次运行
烧掉 64,666 token、以 DEGRADED 收场，暴露出四个缺陷。

教训：**黄金答案测试只覆盖了设计好的任务路径。**
真正的边界要靠开放式提问去撞。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.llm import LLMReply, ToolCall, Usage  # noqa: E402
from agent.loop import MAX_DEAD_TURNS, AgentRunner, Outcome, _ProgressGuard  # noqa: E402
from agent.tools import ToolResult  # noqa: E402


# ======================================================================
# Bug 1 · 打转检测只认"参数相同"，认不出"参数每次都变但一直没产出"
# ======================================================================
def _call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id="x", name=name, args=args)


def test_repeat_detection_still_works() -> None:
    """形态 A：参数完全相同的重复调用。"""
    g = _ProgressGuard(repeat_limit=3)
    calls = [_call("search", pattern="foo")]
    assert not g.observe_calls(calls)
    assert not g.observe_calls(calls)
    assert g.observe_calls(calls)          # 第 3 次触发


def test_barren_streak_detected_despite_varying_args() -> None:
    """形态 B —— 这是漏掉的那个。

    复现真实场景：模型用不同正则反复探测日志的日期范围，
    每次 0 命中。参数每次都不同，所以形态 A 永远不触发。
    """
    g = _ProgressGuard(barren_limit=4)
    for pattern in ("^2025-12-2[5-9]", "^2025-12-1[6-9]", "^2025-12-0[5-9]", "^2025-12-1[0-5]"):
        calls = [_call("search", pattern=pattern, regex=True)]
        assert not g.observe_calls(calls), "参数不同，签名重复检测本就不该触发"
        g.observe_result(calls[0], ToolResult(ok=True, data={"total_hits": 0}))
    assert g.is_barren(), "连续 4 次零命中搜索应被判定为空转"


def test_productive_calls_reset_barren_counter() -> None:
    g = _ProgressGuard(barren_limit=4)
    for _ in range(3):
        g.observe_result(_call("search", pattern="x"), ToolResult(ok=True, data={"total_hits": 0}))
    assert not g.is_barren()
    # 一次有命中的搜索就清零
    g.observe_result(_call("search", pattern="y"), ToolResult(ok=True, data={"total_hits": 2}))
    for _ in range(3):
        g.observe_result(_call("search", pattern="z"), ToolResult(ok=True, data={"total_hits": 0}))
    assert not g.is_barren()


def test_rereading_same_slice_counts_as_barren() -> None:
    """重复读同一段内容不产生新信息。"""
    g = _ProgressGuard(barren_limit=2)
    r = ToolResult(ok=True, data={"path": "a.md", "offset": 0})
    g.observe_result(_call("read_file", path="a.md"), r)   # 首次：有产出
    assert not g.is_barren()
    g.observe_result(_call("read_file", path="a.md"), r)   # 第二次：空转
    g.observe_result(_call("read_file", path="a.md"), r)
    assert g.is_barren()


def test_write_always_counts_as_progress() -> None:
    """写操作改变了世界状态，永远算有进展。"""
    g = _ProgressGuard(barren_limit=2)
    for _ in range(5):
        g.observe_result(_call("write_file", path="o.md"), ToolResult(ok=True, data={"written": "o.md"}))
    assert not g.is_barren()


# ======================================================================
# Bug 2 · 失败按"单次调用"计数，一个回合里的批量幻觉被误判为致命失败
# ======================================================================
@dataclass
class FakeLLM:
    """按脚本回放模型回复，不发任何网络请求。"""

    script: list[LLMReply]
    usage: Usage = field(default_factory=Usage)
    _i: int = 0

    def complete(self, messages, tools=None, system=None) -> LLMReply:  # noqa: ANN001
        reply = self.script[min(self._i, len(self.script) - 1)]
        self._i += 1
        self.usage.add(10, 10)
        return reply


def _reply(*calls: ToolCall) -> LLMReply:
    return LLMReply(
        tool_calls=list(calls),
        raw_assistant_message={"role": "assistant", "content": "", "tool_calls": []},
    )


def _runner(ws: Path, script: list[LLMReply], **kw: Any) -> AgentRunner:
    return AgentRunner(ws, llm=FakeLLM(script), **kw)


def test_partial_success_turn_is_not_fatal(fresh_ws: Path) -> None:
    """一个回合里 1 次成功 + 5 次幻觉路径 —— **不该判死**。

    这正是真实踩到的场景：模型并行发了 13 个调用，8 成功 5 失败，
    旧实现按调用计数直接判定"连续 5 次失败"而终止，
    模型连看到错误、自我纠正的机会都没有。
    """
    hallucinated = [_call("read_file", path=f"logs/nope-{i}.log") for i in range(5)]
    script = [
        _reply(_call("list_dir", path="."), *hallucinated),   # 1 成功 + 5 失败
        _reply(_call("finish", summary="done")),
    ]
    result = _runner(fresh_ws, script).run("总结这个目录")
    assert result.outcome is Outcome.DONE, f"部分成功的回合被误判: {result.summary}"


def test_consecutive_dead_turns_do_terminate(fresh_ws: Path) -> None:
    """连续多个回合**全军覆没**才判死。"""
    dead = _reply(*[_call("read_file", path=f"nope-{i}.md") for i in range(3)])
    result = _runner(fresh_ws, [dead] * (MAX_DEAD_TURNS + 2)).run("x")
    assert result.outcome is Outcome.FAILED
    assert str(MAX_DEAD_TURNS) in result.summary


def test_one_success_resets_dead_turn_counter(fresh_ws: Path) -> None:
    """中间只要有一个回合有产出，计数就该清零。"""
    dead = _reply(_call("read_file", path="nope.md"))
    alive = _reply(_call("list_dir", path="."))
    script = [dead, dead, alive, dead, dead, _reply(_call("finish", summary="ok"))]
    assert _runner(fresh_ws, script).run("x").outcome is Outcome.DONE


# ======================================================================
# Bug 3 · token 预算重复计数，导致提前约 20% 触顶
# ======================================================================
def test_budget_uses_actual_api_usage_only(fresh_ws: Path) -> None:
    """预算判据必须是**实际消耗的 API token**。

    旧实现是 estimate_tokens(messages) + usage.total_tokens，
    而 usage.total_tokens 已含每轮重发的历史 —— 重复计数，
    实测一次任务只花 64.6k 却被算成 80k 而提前终止。
    """
    busy = _reply(_call("list_dir", path="."))
    runner = _runner(fresh_ws, [busy] * 50, token_budget=100)   # 每轮 +20
    result = runner.run("x")
    assert result.outcome is Outcome.DEGRADED
    # 每轮 20 token，预算 100 → 应在第 5 轮左右触顶；
    # 若仍在重复计数，会明显早于此
    assert result.usage["total_tokens"] >= 100
    assert result.steps >= 5


# ======================================================================
# Bug 4 · 收尾预警只看步数，撞上 token 预算时毫无征兆
# ======================================================================
def test_warning_fires_on_token_pressure_not_only_steps(fresh_ws: Path) -> None:
    """在步数还很充裕、但 token 快用完时，也必须提前预警。

    否则 DEGRADED 会毫无征兆地降临，模型没机会先把成果落盘。
    """
    busy = _reply(_call("list_dir", path="."))
    runner = _runner(fresh_ws, [busy] * 50, max_steps=40, token_budget=100)
    result = runner.run("x")
    notes = [e for e in result.events if e.get("type") == "note" and "预算" in e.get("text", "")]
    assert notes, "token 压力达到阈值时应注入收尾预警"
    # 预警必须发生在终止之前，才有意义
    assert notes[0]["step"] < result.steps


def test_degraded_still_reports_written_deliverables(fresh_ws: Path) -> None:
    """DEGRADED 不是空手而归：已落盘的产物必须出现在结果里。"""
    script = [
        _reply(_call("write_file", path="partial.md", content="halfway\n")),
        *[_reply(_call("list_dir", path=".")) for _ in range(50)],
    ]
    result = _runner(fresh_ws, script, token_budget=100).run("x")
    assert result.outcome is Outcome.DEGRADED
    assert "partial.md" in result.deliverables
    assert "partial.md" in result.summary


def test_deliverables_exclude_hallucinated_paths(fresh_ws: Path) -> None:
    """模型声称写了但实际不存在的文件，必须被剔除。"""
    script = [_reply(_call("finish", summary="done", deliverables=["ghost.md", "also-ghost.md"]))]
    result = _runner(fresh_ws, script).run("x")
    assert result.deliverables == []
