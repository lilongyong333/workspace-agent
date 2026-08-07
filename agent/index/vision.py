"""视觉解析 —— 让扫描件 PDF 和图片也能进索引。

## 为什么需要这一层

`pypdfium2` 抽的是 PDF 里的**文本层**。扫描出来的 PDF 根本没有文本层，
图表、印章、手写签名同理。这类文件走完整个索引流程之后，
在库里等于**不存在** —— 而且不报错。

用户搜不到，只会以为"这份合同没收录"，实际是收录了但里面是空的。
这跟切块器那个「短文件整篇消失」是同一类问题：静默的假阴性。

## 设计取舍

**默认关闭。** 视觉解析每页都要调一次模型，慢且花钱。
一个 200 页的扫描件能轻松烧掉几十万 token。
所以必须显式打开（`VISION_OCR=1`），并且有页数上限。

**只在文本层为空时才启用。** 有文本层就用文本层 ——
又快又准又免费，没有任何理由去问模型。

**失败不影响主流程。** 视觉调用超时或报错时，
记录原因、当作"这一页没有文本"继续走，而不是让整个索引任务崩掉。

## 一个实测得出的坑

选纯文本模型时，两家的失败方式完全不同：

    deepseek-v4-flash   HTTP 400  unknown variant `image_url`   ← 吵闹的失败
    qwen-max            HTTP 200  「请提供图片」                  ← 静默的失败

后者更危险：不报错，只是给一个听起来合理的空答案。
所以这里**主动校验模型是否具备视觉能力**，不具备就直接拒绝启用，
而不是发出去等一个看起来正常的空回复。
"""

from __future__ import annotations

import base64
import logging
import os

import httpx

from ..llm import LLMConfig, LLMError, model_supports_vision

log = logging.getLogger(__name__)

# 单个文档最多送多少页去做视觉解析 —— 成本闸门
MAX_VISION_PAGES = int(os.getenv("VISION_MAX_PAGES", "12"))
# 文本层少于这么多字符，才认为"这一页没有文本层"
TEXT_LAYER_MIN_CHARS = int(os.getenv("VISION_TEXT_MIN_CHARS", "24"))
VISION_TIMEOUT = float(os.getenv("VISION_TIMEOUT_SECONDS", "90"))

PROMPT = (
    "请把这张图片里的所有文字**原样**转写出来，保持阅读顺序。"
    "如果是表格，用 Markdown 表格还原，保留表头。"
    "如果有图表，先转写其中的文字与数值，再用一句话说明它在表达什么。"
    "只输出内容本身，不要加任何前言、说明或评价。"
    "图中没有任何文字时，只回答：（无文字）"
)


class VisionUnavailable(RuntimeError):
    """未配置视觉模型，或配置的模型不具备视觉能力。"""


def vision_enabled() -> bool:
    return os.getenv("VISION_OCR", "").strip().lower() in ("1", "true", "yes", "on")


def _config() -> LLMConfig:
    provider = os.getenv("VISION_PROVIDER", "qwen").strip().lower()
    try:
        cfg = LLMConfig.for_provider(provider, os.getenv("VISION_MODEL"))
    except LLMError as exc:
        raise VisionUnavailable(str(exc)) from None

    if not model_supports_vision(cfg.model):
        # 关键：宁可直接拒绝，也不要发出去换一个"看起来正常"的空回答。
        raise VisionUnavailable(
            f"模型 {cfg.model!r} 不具备视觉能力。"
            f"请设置 VISION_MODEL（如 qwen-vl-max）。"
            f"注意 qwen-max 收到图片不会报错，只会假装没看见 —— 那是最难排查的一种失败。"
        )
    return cfg


def describe_image(png_bytes: bytes, hint: str = "") -> str:
    """把一张图片转成可检索的文字。失败时抛异常，由调用方决定降级方式。"""
    cfg = _config()
    b64 = base64.b64encode(png_bytes).decode()
    prompt = PROMPT + (f"\n（这是 {hint}）" if hint else "")

    payload = {
        "model": cfg.model,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
        ]}],
    }
    r = httpx.post(
        f"{cfg.base_url}/chat/completions",
        headers={"Authorization": f"Bearer {cfg.api_key}"},
        json=payload,
        timeout=VISION_TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"视觉接口 HTTP {r.status_code}: {r.text[:200]}")

    text = (r.json()["choices"][0]["message"]["content"] or "").strip()
    return "" if text in ("（无文字）", "(无文字)") else text
