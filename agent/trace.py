"""轨迹记录 —— 本地落 trace.jsonl，Web 端走 SSE，**同一个事件源**。

题面要求本地运行输出 ``trace.jsonl``，每步一行：

    {"step": n, "tool": "...", "args": {...}, "result_summary": "..."}

同时要求 Web Demo「实时看到 agent 的每一步」，并称之为「整个 demo 的灵魂」。

两者共用一份事件流而不是各写一套，好处是：**你在网页上看到的，
和交上去的 trace.jsonl 是同一份真相**，不会出现"演示好看但日志对不上"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# 订阅者：Web 层注册一个回调把事件推进 SSE 队列
Subscriber = Callable[[dict[str, Any]], None]


@dataclass
class TraceRecorder:
    """收集事件，同时可选地落盘与广播。"""

    jsonl_path: Path | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[Subscriber] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            # 每次运行重置，避免多次运行的轨迹混在一起看不出边界
            self.jsonl_path.write_text("", encoding="utf-8")

    def subscribe(self, fn: Subscriber) -> None:
        self.subscribers.append(fn)

    # ------------------------------------------------------------------
    def emit(self, kind: str, **payload: Any) -> dict[str, Any]:
        event = {"type": kind, **payload}
        self.events.append(event)

        # 只有工具步骤写入 trace.jsonl —— 题面规定的正是这个形状。
        # 思考、状态变更等事件只走 SSE，不污染交付物。
        if self.jsonl_path and kind == "tool":
            line = {
                "step": payload.get("step"),
                "tool": payload.get("tool"),
                "args": payload.get("args", {}),
                "result_summary": payload.get("result_summary", ""),
            }
            with self.jsonl_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")

        for fn in self.subscribers:
            try:
                fn(event)
            except Exception:  # noqa: BLE001 - 订阅者故障绝不能影响 agent 主流程
                pass

        return event

    # -- 语义化的便捷方法 ------------------------------------------------
    def step_start(self, step: int) -> None:
        self.emit("step_start", step=step)

    def thinking(self, step: int, text: str) -> None:
        """模型在调用工具前说的话。对可观测性很有价值 —— 它暴露了模型的意图。"""
        if text.strip():
            self.emit("thinking", step=step, text=text.strip()[:1000])

    def tool(self, step: int, name: str, args: dict[str, Any], summary: str, ok: bool) -> None:
        self.emit("tool", step=step, tool=name, args=args, result_summary=summary, ok=ok)

    def usage(self, snapshot: dict[str, int]) -> None:
        self.emit("usage", **snapshot)

    def finish(self, outcome: str, summary: str, deliverables: Iterable[str], steps: int) -> None:
        self.emit(
            "finish",
            outcome=outcome,
            summary=summary,
            deliverables=list(deliverables),
            steps=steps,
        )

    def note(self, step: int, text: str) -> None:
        """循环层面的干预记录（触发纠偏、检测到卡死等），便于事后复盘。"""
        self.emit("note", step=step, text=text)
