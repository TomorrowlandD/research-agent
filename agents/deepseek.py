"""DeepSeek V4 Pro configuration and usage helpers (official Chat Completions)."""
from __future__ import annotations

from dataclasses import dataclass
import os
from urllib.parse import urlparse

CONTEXT_WINDOW = 1048576
MAX_OUTPUT_TOKENS = 393216
# USD / 1M tokens, official pricing verified on 2026-10-12.
# 默认按峰时价格保守估算，避免本地时钟、节假日和跨时段请求造成低估。
PRICES = {"peak": (0.044, 1.32, 3.96), "off_peak": (0.022, 0.66, 1.98)}


def is_official_v4_pro(model: str, api_base: str | None) -> bool:
    base = urlparse(api_base or "")
    return (
        model == "deepseek-v4-pro" and base.scheme == "https"
        and base.hostname == "api.deepseek.com"
        and base.path.rstrip("/") in {"", "/v1"}
        and not base.username and not base.password and not base.query and not base.fragment
    )


def _choice(value: str | None, env_name: str, default: str, choices: set[str]) -> str:
    value = (value if value is not None else os.environ.get(env_name, default)).strip().lower()
    if value not in choices:
        raise ValueError(f"{env_name} must be one of: {', '.join(sorted(choices))}.")
    return value


@dataclass(frozen=True)
class DeepSeekConfig:
    thinking: str
    effort: str
    side_thinking: str
    side_effort: str
    price_period: str

    @classmethod
    def resolve(cls, *, thinking: bool | None, effort: str | None,
                side_thinking: str | None, side_effort: str | None,
                price_period: str | None) -> DeepSeekConfig:
        # 显式构造参数（含 CLI）优先于环境；环境优先于本地默认值。
        mode = ("enabled" if thinking else "disabled") if thinking is not None else None
        return cls(
            _choice(mode, "RESEARCH_DEEPSEEK_THINKING", "disabled", {"enabled", "disabled"}),
            _choice(effort, "RESEARCH_DEEPSEEK_REASONING_EFFORT", "high", {"low", "high", "max"}),
            _choice(side_thinking, "RESEARCH_DEEPSEEK_SIDE_THINKING", "disabled", {"enabled", "disabled"}),
            _choice(side_effort, "RESEARCH_DEEPSEEK_SIDE_REASONING_EFFORT", "low", {"low", "high", "max"}),
            _choice(price_period, "RESEARCH_DEEPSEEK_PRICE_PERIOD", "peak", set(PRICES)),
        )

    def request_options(self, *, side: bool = False) -> dict:
        mode = self.side_thinking if side else self.thinking
        options = {"extra_body": {"thinking": {"type": mode}}}
        if mode == "enabled":
            options["reasoning_effort"] = self.side_effort if side else self.effort
        return options

    def default_output_tokens(self) -> int:
        if self.thinking == "disabled":
            return 16384
        return 131072 if self.effort == "max" else 65536


def _field(value, name: str, default=0):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def normalize_usage(usage) -> dict[str, int]:
    prompt = int(_field(usage, "prompt_tokens") or 0)
    output = int(_field(usage, "completion_tokens") or 0)
    details = _field(usage, "prompt_tokens_details", None)
    hits = _field(usage, "prompt_cache_hit_tokens", _field(usage, "cache_hit_tokens", None))
    if hits is None:
        hits = _field(details, "cached_tokens", 0)
    hits = max(0, min(prompt, int(hits or 0)))
    return {
        "prompt_tokens": prompt, "completion_tokens": output,
        "cache_hit_tokens": hits, "cache_miss_tokens": prompt - hits,
        # completion_tokens 已含思考 token，不能再次相加计费。
        "reasoning_tokens": int(_field(usage, "reasoning_tokens",
                                      _field(_field(usage, "completion_tokens_details", None), "reasoning_tokens")) or 0),
    }


def estimate_cost_usd(usage: dict[str, int], period: str) -> float:
    hit, miss, output = PRICES[period]
    return (usage["cache_hit_tokens"] * hit + usage["cache_miss_tokens"] * miss
            + usage["completion_tokens"] * output) / 1_000_000
