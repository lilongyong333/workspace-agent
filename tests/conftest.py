"""pytest 配置。

把测试分成两档：

* **默认档**（快、免费、离线）—— 沙箱、工具、断言库自身的正确性。
  每次改代码都该跑，秒级完成。
* **live 档**（慢、花钱、需 API key）—— 真的把 agent 跑起来验黄金答案。
  用 ``--live`` 显式开启，避免 CI 或日常开发无意中烧掉额度。

分档的意义不只是省钱：**如果快测试挂了，就没必要浪费一次 live 运行去发现同样的问题。**
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SEED = ROOT / "workspace_seed"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="运行需要真实模型 API 的测试（会产生费用）",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: 需要真实模型 API 的端到端测试")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live") or os.getenv("RUN_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="需要 --live（会调用真实模型 API 并产生费用）")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture()
def seed_dir() -> Path:
    return SEED


@pytest.fixture()
def fresh_ws(tmp_path: Path) -> Path:
    """一份干净的工作目录副本。每个用例独立，互不污染。"""
    ws = tmp_path / "workspace"
    shutil.copytree(SEED, ws)
    return ws
