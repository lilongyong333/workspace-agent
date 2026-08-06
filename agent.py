#!/usr/bin/env python3
"""顶层入口 —— 与题面示例的命令形式完全一致：

    python agent.py --workspace ./workspace --task "..."

等价于 ``python -m agent``（真正的实现在 agent/ 包里）。

留这个薄壳的唯一理由：评审可能直接复制题面里的命令来跑。
让复制粘贴就能工作，比要求对方记住另一种写法更友好。

（顺带说明为什么不会与 agent/ 包冲突：直接运行本文件时它被加载为
 ``__main__``，而不是名为 ``agent`` 的模块；``import agent`` 仍然解析到
 同目录下的 agent/ 包 —— Python 的路径查找中，包优先于同名模块。）
"""

from __future__ import annotations

import sys

from agent.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
