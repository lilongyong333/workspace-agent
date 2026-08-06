"""命令行入口。

    python -m agent --workspace ./workspace --task "..."

题面要求「能本地一条命令跑，且 workspace 路径可指定」——
评审会克隆仓库，在**内容不同的新 workspace** 上运行，所以路径必须是参数。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

from .llm import LLMError
from .loop import AgentRunner
from .trace import TraceRecorder

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "workspace_seed"


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")

    p = argparse.ArgumentParser(
        prog="python -m agent",
        description="文件助理 Agent —— 自然语言指令，通过受限工具操作工作目录。",
    )
    p.add_argument("--workspace", default="./workspace", help="工作目录路径（会被就地修改）")
    p.add_argument("--task", required=True, help="自然语言任务")
    p.add_argument("--trace", default="trace.jsonl", help="轨迹输出路径")
    p.add_argument("--max-steps", type=int, default=None)
    p.add_argument(
        "--reset",
        action="store_true",
        help="运行前用 workspace_seed 重置工作目录（反复试验时很方便）",
    )
    args = p.parse_args(argv)

    ws = Path(args.workspace)
    if args.reset:
        if ws.exists():
            shutil.rmtree(ws)
        shutil.copytree(SEED, ws)
        print(f"[reset] 已从 workspace_seed 重建 {ws}")

    if not ws.is_dir():
        print(
            f"错误：工作目录不存在: {ws}\n"
            f"提示：加 --reset 可从内置种子创建一份。",
            file=sys.stderr,
        )
        return 2

    trace = TraceRecorder(jsonl_path=Path(args.trace))

    # 边跑边打印，而不是跑完再输出 —— 长任务下过程可见性很重要
    def on_event(ev: dict) -> None:
        kind = ev.get("type")
        if kind == "tool":
            flag = "  " if ev.get("ok") else "!!"
            print(f"{flag} [{ev['step']:>2}] {ev['tool']:<11} {ev.get('result_summary', '')}")
        elif kind == "thinking":
            print(f"   [{ev['step']:>2}] 💭 {ev['text'][:150]}")
        elif kind == "note":
            print(f"   [{ev['step']:>2}] ⚙  {ev['text']}")

    trace.subscribe(on_event)

    try:
        runner = AgentRunner(ws, trace=trace, max_steps=args.max_steps)
        result = runner.run(args.task)
    except LLMError as exc:
        print(f"\n模型配置或调用错误：{exc}", file=sys.stderr)
        return 3

    print("\n" + "=" * 62)
    print(f"结局: {result.outcome.value.upper()}   步数: {result.steps}")
    print(f"产物: {', '.join(result.deliverables) or '(无)'}")
    u = result.usage
    print(
        f"用量: {u['calls']} 次调用, {u['total_tokens']} tokens "
        f"(输入 {u['prompt_tokens']} / 输出 {u['completion_tokens']})"
    )
    print(f"轨迹: {args.trace}")
    print("-" * 62)
    print(result.summary)

    # DEGRADED 也算有交付，退出码 0；只有 FAILED 才非零
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
