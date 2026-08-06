"""命令行入口。

两组命令：

    # 一次性任务（原有行为，不需要索引）
    python agent.py --workspace ./workspace --task "..."

    # 索引任意文件夹，然后基于索引提问
    python agent.py index add  --path "D:/我的资料" --label 工作
    python agent.py index sync --label 工作
    python agent.py index status
    python agent.py ask --label 工作 --task "去年 Q4 服务器采购花了多少？"
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .index.indexer import IndexProgress, sync_root
from .index.parsers import available_parsers
from .index.store import IndexStore
from .llm import LLMError
from .loop import AgentRunner
from .trace import TraceRecorder

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "workspace_seed"
DEFAULT_DB = ROOT / ".index" / "index.db"


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _print_event(ev: dict) -> None:
    kind = ev.get("type")
    if kind == "tool":
        flag = "  " if ev.get("ok") else "!!"
        print(f"{flag} [{ev['step']:>2}] {ev['tool']:<16} {ev.get('result_summary', '')}")
    elif kind == "thinking":
        print(f"   [{ev['step']:>2}] 💭 {ev['text'][:150]}")
    elif kind == "note":
        print(f"   [{ev['step']:>2}] ⚙  {ev['text']}")


def _run_agent(ws: Path, task: str, trace_path: Path, store=None,
               root_ids=None, max_steps: int | None = None,
               read_only: bool = False, out_path: Path | None = None) -> int:
    trace = TraceRecorder(jsonl_path=trace_path)
    trace.subscribe(_print_event)
    try:
        runner = AgentRunner(ws, trace=trace, max_steps=max_steps,
                             store=store, root_ids=root_ids, read_only=read_only)
        result = runner.run(task)
    except LLMError as exc:
        print(f"\n模型配置或调用错误：{exc}", file=sys.stderr)
        return 3

    u = result.usage
    print("\n" + "=" * 66)
    print(f"结局: {result.outcome.value.upper()}   步数: {result.steps}")
    print(f"产物: {', '.join(result.deliverables) or '(无)'}")
    print(f"用量: {u['calls']} 次调用, {u['total_tokens']} tokens "
          f"(输入 {u['prompt_tokens']} / 输出 {u['completion_tokens']})")
    print(f"轨迹: {trace_path}")
    print("-" * 66)
    print(result.summary)

    # 只读模式下 agent 自己写不了文件，报告落盘由**操作者**决定。
    # 这条分工是刻意的：写盘动作发生在 agent 之外，
    # 因此无论模型被语料里的注入怎么诱导，都不可能把内容写进用户的语料目录。
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result.summary, encoding="utf-8")
        print(f"\n已保存回答 -> {out_path}")

    return 0 if result.ok else 1


# ======================================================================
# index 子命令
# ======================================================================
def cmd_index(args: argparse.Namespace) -> int:
    store = IndexStore(args.db)

    if args.index_cmd == "add":
        root = store.add_root(args.path, label=args.label,
                              include_globs=args.include or (),
                              exclude_globs=args.exclude or ())
        print(f"已注册 [{root.label}] -> {root.path}")
        print("下一步：python agent.py index sync --label " + root.label)
        return 0

    if args.index_cmd == "list":
        roots = store.list_roots()
        if not roots:
            print("尚未注册任何目录。用 `index add --path <目录>` 添加。")
            return 0
        for r in roots:
            last = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.last_scan_at)) \
                if r.last_scan_at else "从未"
            run = store.last_run(r.id)
            docs = run["files_indexed"] if run else 0
            print(f"  [{r.id}] {r.label:<16} {r.path}")
            print(f"       上次同步 {last} | 状态 {r.status} | 最近一次索引 {docs} 个文件")
        return 0

    if args.index_cmd == "remove":
        root = store.get_root(label=args.label) or store.get_root(root_id=args.root_id or -1)
        if root is None:
            print("找不到该根目录", file=sys.stderr); return 2
        store.remove_root(root.id)
        print(f"已移除 [{root.label}]（原文件未受影响）")
        return 0

    if args.index_cmd == "sync":
        roots = ([store.get_root(label=args.label)] if args.label else store.list_roots())
        roots = [r for r in roots if r]
        if not roots:
            print("没有可同步的目录", file=sys.stderr); return 2

        for root in roots:
            print(f"\n同步 [{root.label}] {root.path}")
            t0 = time.time()
            last_line = [""]

            def cb(p: IndexProgress) -> None:
                line = (f"  扫描 {p.files_seen} | 索引 {p.files_indexed} | "
                        f"跳过 {p.files_skipped} | 失败 {p.files_failed} | {p.current[:48]}")
                pad = " " * max(0, len(last_line[0]) - len(line))
                print(f"\r{line}{pad}", end="", flush=True)
                last_line[0] = line

            try:
                p = sync_root(store, root, progress_cb=cb)
            except Exception as exc:
                print(f"\n  失败: {exc}", file=sys.stderr); continue

            print(f"\r  完成: 扫描 {p.files_seen} | 新建/更新 {p.files_indexed} | "
                  f"跳过 {p.files_skipped} | 失败 {p.files_failed} | "
                  f"移除 {p.files_removed} | 生成 {p.chunks_written} 块 | "
                  f"耗时 {time.time() - t0:.1f}s" + " " * 20)
            if p.errors:
                print(f"  解析失败样例（共 {len(p.errors)} 个）：")
                for path, err in p.errors[:5]:
                    print(f"    - {path}: {err[:90]}")
        return 0

    if args.index_cmd == "status":
        st = store.corpus_stats()
        print(f"索引库: {args.db}")
        print(f"文档 {st['documents']} | 块 {st['chunks']} | 总大小 {_human(st['total_bytes'])}")
        print("\n可解析格式:")
        for fmt, ok in available_parsers().items():
            print(f"  {'✓' if ok else '✗'} {fmt}" + ("" if ok else "   （缺少依赖）"))
        if st["by_extension"]:
            print("\n按扩展名:")
            for e in st["by_extension"][:12]:
                print(f"  {e['ext']:<12} {e['count']:>7} 个   {_human(e['bytes'] or 0)}")
        if st["top_directories"]:
            print("\n顶层目录:")
            for d in st["top_directories"][:12]:
                print(f"  {d['dir']:<28} {d['count']:>7} 个")
        if st["parse_failures"]:
            print(f"\n解析失败（前 {len(st['parse_failures'])} 个）:")
            for f in st["parse_failures"]:
                print(f"  - {f['path']}: {f['error'][:80]}")
        return 0

    if args.index_cmd == "search":
        hits = store.search(args.query, limit=args.limit)
        if not hits:
            print("无命中"); return 0
        for h in hits:
            loc = " ".join(f"{k}={v}" for k, v in h.locator.items())
            print(f"\n[{h.root_label}] {h.rel_path}  {loc}  (via {'+'.join(h.matched_by)})")
            if h.breadcrumb:
                print(f"  {h.breadcrumb[:110]}")
            print(f"  {h.text[:220].strip()}")
        return 0

    return 2


# ======================================================================
def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")

    p = argparse.ArgumentParser(
        prog="agent",
        description="文件助理 Agent —— 自然语言指令，通过受限工具操作文件。",
    )
    sub = p.add_subparsers(dest="cmd")

    # -- 兼容原有用法：不带子命令时直接跑任务 --
    p.add_argument("--workspace", default="./workspace")
    p.add_argument("--task")
    p.add_argument("--trace", default="trace.jsonl")
    p.add_argument("--max-steps", type=int, default=None)
    p.add_argument("--reset", action="store_true", help="用 workspace_seed 重置工作目录")
    p.add_argument("--db", default=str(DEFAULT_DB), help="索引库路径")

    # -- index --
    ip = sub.add_parser("index", help="管理索引")
    isub = ip.add_subparsers(dest="index_cmd", required=True)
    a = isub.add_parser("add", help="注册一个目录")
    a.add_argument("--path", required=True); a.add_argument("--label")
    a.add_argument("--include", action="append"); a.add_argument("--exclude", action="append")
    isub.add_parser("list", help="列出已注册目录")
    s_ = isub.add_parser("sync", help="增量同步"); s_.add_argument("--label")
    isub.add_parser("status", help="索引统计与可解析格式")
    q = isub.add_parser("search", help="直接检索（不经模型）")
    q.add_argument("query"); q.add_argument("--limit", type=int, default=8)
    rm = isub.add_parser("remove", help="移除注册（不删原文件）")
    rm.add_argument("--label"); rm.add_argument("--root-id", type=int)

    # -- ask --
    ap = sub.add_parser("ask", help="基于索引提问")
    ap.add_argument("--task", required=True)
    ap.add_argument("--label", help="限定某个已注册目录，省略则全部")
    ap.add_argument("--out", help="把回答保存到该路径（由 CLI 写盘，不经模型）")
    ap.add_argument("--trace", default="trace.jsonl")
    ap.add_argument("--max-steps", type=int, default=None)

    args = p.parse_args(argv)

    if args.cmd == "index":
        return cmd_index(args)

    if args.cmd == "ask":
        store = IndexStore(DEFAULT_DB)
        roots = store.list_roots()
        if not roots:
            print("尚未索引任何目录。先运行：\n"
                  "  python agent.py index add --path <你的目录>\n"
                  "  python agent.py index sync", file=sys.stderr)
            return 2
        if args.label:
            root = store.get_root(label=args.label)
            if root is None:
                print(f"找不到标签 {args.label}", file=sys.stderr); return 2
            targets, ws = [root.id], Path(root.path)
        else:
            targets, ws = [r.id for r in roots], Path(roots[0].path)
        # ask 一律**只读**。用户在这里注册的是自己的真实资料目录
        # （合同、财务表、公司文档），他要的是检索，不是让 AI 改他的文件。
        # 报告需要落盘就加 --out，由 CLI 写到语料之外。
        print(f"基于索引提问（{len(targets)} 个目录，工作根 {ws}，"
              f"\033[1m只读\033[0m —— 未向模型提供任何写工具）\n")
        return _run_agent(ws, args.task, Path(args.trace), store=store,
                          root_ids=targets, max_steps=args.max_steps,
                          read_only=True,
                          out_path=Path(args.out) if args.out else None)

    # -- 无子命令：原有的一次性任务 --
    if not args.task:
        p.print_help(); return 2

    ws = Path(args.workspace)
    if args.reset:
        if ws.exists():
            shutil.rmtree(ws)
        shutil.copytree(SEED, ws)
        print(f"[reset] 已从 workspace_seed 重建 {ws}")
    if not ws.is_dir():
        print(f"错误：工作目录不存在: {ws}\n提示：加 --reset 可从内置种子创建一份。",
              file=sys.stderr)
        return 2

    return _run_agent(ws, args.task, Path(args.trace), max_steps=args.max_steps)


if __name__ == "__main__":
    raise SystemExit(main())
