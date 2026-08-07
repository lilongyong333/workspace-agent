#!/usr/bin/env python3
"""容器入口：把持久卷的属主交给应用用户，然后**降权**再启动服务。

## 为什么需要这一步

平台挂载的持久卷，挂载点属主由平台决定，通常是 root。
容器若直接以非特权用户启动，就写不进这个目录 ——
表现是建会话 500，而且完全看不出跟「挂了个卷」有关系。

这是容器 + 持久卷的经典冲突：安全上想跑非 root，
但卷的属主又只有 root 能改。

## 做法：root-then-drop

以 root 进入，**只用它做一次 chown**，随后 setuid 到应用用户再 exec。
应用进程自始至终是非特权的，root 只存在于下面这十几行里。

只用标准库，不依赖 gosu / su-exec / setpriv ——
基础镜像里有没有这些工具是个需要验证的假设，而 os.setuid 不是。
"""

from __future__ import annotations

import os
import sys

APP_UID = APP_GID = 10001
PORT = os.getenv("PORT", "8000")
CMD = ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", PORT]


def _handover(path: str) -> None:
    """把数据目录及其内容交给应用用户。"""
    os.makedirs(path, exist_ok=True)
    os.chown(path, APP_UID, APP_GID)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chown(os.path.join(root, name), APP_UID, APP_GID)
            except OSError:
                pass          # 单个文件改不动不该拖垮启动


def main() -> None:
    data_dir = os.getenv("DATA_DIR")

    if os.geteuid() == 0:
        if data_dir:
            try:
                _handover(data_dir)
                print(f"[entrypoint] {data_dir} 已移交 uid={APP_UID}", flush=True)
            except OSError as exc:
                # 不中止：应用自己有降级路径（写不进去就退回非持久目录并如实上报），
                # 直接崩掉反而让人看不到任何线索。
                print(f"[entrypoint] 准备 {data_dir} 失败: {exc}", file=sys.stderr, flush=True)

        # 降权。**顺序不能反**：先 setgid 再 setuid ——
        # 一旦 setuid 成功就再也没有权限改组了。
        os.setgroups([])
        os.setgid(APP_GID)
        os.setuid(APP_UID)
        os.environ.setdefault("HOME", "/home/appuser")

    os.execvp(CMD[0], CMD)


if __name__ == "__main__":
    main()
