"""模型适配层 —— 裸 HTTP，不用任何 SDK。

题面禁止使用「替你跑 agent 循环」的层（LangChain / LangGraph / CrewAI /
OpenAI Agents SDK / Claude Agent SDK / tool_runner 等）。本模块因此只做一件事：

    **把一次 messages + tools 的请求发出去，把模型的决策解析回来。**

它不循环、不重试工具、不管上下文裁剪 —— 那些是 loop.py 和 context.py 的职责。
职责单一带来的好处是：换模型提供方只需改环境变量，循环代码一行不动。

支持两种 wire format：

* ``openai``    —— DeepSeek / OpenAI / 智谱 GLM / Kimi / Gemini(OpenAI 兼容端点)
* ``anthropic`` —— Claude 系列（消息与工具格式不同，单独适配）

Gemini 官方提供 OpenAI 兼容端点，因此走第一种即可，不必写第三个适配器。
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

Wire = Literal["openai", "anthropic"]

# 各提供方的默认端点与 wire format。
# 用户可用 LLM_BASE_URL 覆盖，以支持自建代理或私有部署。
_PROVIDERS: dict[str, tuple[str, Wire]] = {
    "deepseek": ("https://api.deepseek.com", "openai"),
    "openai": ("https://api.openai.com/v1", "openai"),
    "zhipu": ("https://open.bigmodel.cn/api/paas/v4", "openai"),
    "moonshot": ("https://api.moonshot.cn/v1", "openai"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "openai"),
    "anthropic": ("https://api.anthropic.com/v1", "anthropic"),
    # 通义千问（阿里云百炼）。DashScope 提供 OpenAI 兼容端点，
    # 所以走同一条 openai wire，不需要为它写第二套请求/响应转换。
    # 这正是「多 provider 适配」的意义：新增一家通常只是加一行。
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "openai"),
    "dashscope": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "openai"),
}


class LLMError(RuntimeError):
    """模型调用失败且已耗尽重试。由 loop 转成 FAILED 终止。"""


# ----------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------
@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Usage:
    """累计用量 —— Web Demo 要显示「LLM 调用次数与 token 消耗」。"""

    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def add(self, prompt: int, completion: int, cached: int = 0) -> None:
        self.calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.cached_tokens += cached

    def as_dict(self) -> dict[str, int]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class LLMReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    raw_assistant_message: dict[str, Any] = field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True)
class LLMConfig:
    provider: str
    api_key: str
    model: str
    base_url: str
    wire: Wire
    timeout_seconds: float = 120.0
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> "LLMConfig":
        provider = os.getenv("LLM_PROVIDER", "deepseek").strip().lower()
        default_url, wire = _PROVIDERS.get(provider, ("", "openai"))
        base_url = (os.getenv("LLM_BASE_URL") or default_url).rstrip("/")
        key = os.getenv("LLM_API_KEY", "").strip()

        if not key:
            raise LLMError(
                "缺少 LLM_API_KEY。本地请在 .env 中配置；线上请在部署平台的环境变量中配置。"
            )
        if not base_url:
            raise LLMError(f"未知 provider {provider!r} 且未提供 LLM_BASE_URL")

        return cls(
            provider=provider,
            api_key=key,
            model=os.getenv("LLM_MODEL") or "deepseek-v4-flash",
            base_url=base_url,
            wire=wire,
            timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", "120")),
            max_retries=int(os.getenv("LLM_MAX_RETRIES", "3")),
        )


# ----------------------------------------------------------------------
# 客户端
# ----------------------------------------------------------------------
class LLMClient:
    def __init__(self, config: LLMConfig | None = None) -> None:
        self.cfg = config or LLMConfig.from_env()
        self.usage = Usage()
        self._client = httpx.Client(timeout=self.cfg.timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- 公开接口 ------------------------------------------------------
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> LLMReply:
        """发一次请求，拿回模型的决策（文本 + 可能的工具调用）。"""
        if self.cfg.wire == "anthropic":
            url, payload, headers = self._build_anthropic(messages, tools, system)
        else:
            url, payload, headers = self._build_openai(messages, tools, system)

        data = self._post_with_retry(url, payload, headers)

        reply = (
            self._parse_anthropic(data)
            if self.cfg.wire == "anthropic"
            else self._parse_openai(data)
        )
        self.usage.add(reply.prompt_tokens, reply.completion_tokens, reply.cached_tokens)
        return reply

    # -- 重试 ----------------------------------------------------------
    def _post_with_retry(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        """只重试**瞬时**故障（429 / 5xx / 网络），不重试 4xx 参数错误。

        重试 4xx 是浪费 —— 请求本身不合法，重发一百次也一样。
        用指数退避 + 抖动，避免多个会话同时重试形成尖峰。
        """
        last: Exception | None = None

        for attempt in range(self.cfg.max_retries + 1):
            try:
                resp = self._client.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = exc
            else:
                if resp.status_code < 400:
                    return resp.json()

                detail = resp.text[:300]
                if resp.status_code == 429 or resp.status_code >= 500:
                    last = LLMError(f"HTTP {resp.status_code}: {detail}")
                else:
                    # 4xx 立即失败，把服务端的错误原文带出去便于定位
                    raise LLMError(f"HTTP {resp.status_code}: {detail}")

            if attempt < self.cfg.max_retries:
                backoff = (2**attempt) + random.uniform(0, 0.5)
                time.sleep(backoff)

        raise LLMError(f"模型调用失败，已重试 {self.cfg.max_retries} 次: {last}")

    # -- OpenAI 兼容 ---------------------------------------------------
    def _build_openai(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str | None,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        msgs = list(messages)
        if system:
            msgs = [{"role": "system", "content": system}, *msgs]

        payload: dict[str, Any] = {"model": self.cfg.model, "messages": msgs}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        return (
            f"{self.cfg.base_url}/chat/completions",
            payload,
            {
                "Authorization": f"Bearer {self.cfg.api_key}",
                "Content-Type": "application/json",
            },
        )

    def _parse_openai(self, data: dict[str, Any]) -> LLMReply:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}

        calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls.append(
                ToolCall(
                    id=tc.get("id") or f"call_{len(calls)}",
                    name=fn.get("name") or "",
                    # 模型偶尔会吐出非法 JSON —— 转成结构化错误让循环去纠正，
                    # 而不是在这里抛异常炸掉整个任务
                    args=_safe_json(fn.get("arguments")),
                )
            )

        return LLMReply(
            text=msg.get("content") or "",
            tool_calls=calls,
            finish_reason=choice.get("finish_reason") or "",
            raw_assistant_message=msg,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(usage.get("prompt_cache_hit_tokens") or 0),
        )

    # -- Anthropic -----------------------------------------------------
    def _build_anthropic(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        system: str | None,
    ) -> tuple[str, dict[str, Any], dict[str, str]]:
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": 8192,
            "messages": messages,
        }
        if system:
            payload["system"] = system
        if tools:
            # Anthropic 的工具 schema 是扁平的，没有 OpenAI 那层 function 包装
            payload["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"].get("parameters", {"type": "object"}),
                }
                for t in tools
            ]

        return (
            f"{self.cfg.base_url}/messages",
            payload,
            {
                "x-api-key": self.cfg.api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
        )

    def _parse_anthropic(self, data: dict[str, Any]) -> LLMReply:
        text_parts: list[str] = []
        calls: list[ToolCall] = []

        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(
                        id=block.get("id") or f"call_{len(calls)}",
                        name=block.get("name") or "",
                        args=block.get("input") or {},
                    )
                )

        usage = data.get("usage") or {}
        return LLMReply(
            text="".join(text_parts),
            tool_calls=calls,
            finish_reason=data.get("stop_reason") or "",
            raw_assistant_message={"role": "assistant", "content": data.get("content") or []},
            prompt_tokens=int(usage.get("input_tokens") or 0),
            completion_tokens=int(usage.get("output_tokens") or 0),
            cached_tokens=int(usage.get("cache_read_input_tokens") or 0),
        )


def _safe_json(raw: Any) -> dict[str, Any]:
    """把模型给的参数解析成 dict；解析不了就返回一个可被工具层识别的错误载荷。

    模型吐出非法 JSON 是常态而非异常。让循环看到一个结构化的错误
    并自我纠正，远好过让整个任务崩掉。
    """
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"__invalid_args__": parsed}
    except (json.JSONDecodeError, TypeError):
        return {"__invalid_args__": str(raw)[:400]}
