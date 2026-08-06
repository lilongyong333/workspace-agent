"""索引子系统 —— 让 agent 能指向**任意文件夹**并秒级检索。

模块划分：

* ``store``    SQLite + FTS5 双词法索引（unicode61 管英文/代码，trigram 管中文）
* ``parsers``  多格式解析，可选依赖缺失时优雅降级
* ``chunker``  结构感知切块 + 面包屑
* ``indexer``  遍历与增量同步（mtime → sha256 三级判断）
* ``verify``   证据时效校验，防止索引陈旧导致的错误答案
"""

from .store import Hit, IndexStore, Root
from .verify import VerifyReport, verify_hits

__all__ = ["IndexStore", "Hit", "Root", "verify_hits", "VerifyReport"]
