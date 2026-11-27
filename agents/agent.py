#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from .paths import RESEARCH_DIRNAME, workspace_dir
from .tool_result import ToolExecutionResult, ToolFailure
from .web_cache import WebCache
from .artifacts import CandidateLedger
from .web_tools import annotate_search_results, external_content, fetch_search_results, normalize_web_args
from .tool_snapshot import replay_tool_snapshot, write_tool_snapshot
from dataclasses import replace
from types import SimpleNamespace
from typing import Callable, Awaitable, Any

import anthropic
import openai

from agents.deepseek import (
    CONTEXT_WINDOW, MAX_OUTPUT_TOKENS, DeepSeekConfig, is_official_v4_pro,
    normalize_usage, estimate_cost_usd,
)

from agents.event_log import SessionEventLog
from agents.mcp_client import McpManager
from agents.memory import _project_hash, MemoryPrefetch, start_memory_prefetch, format_memories_for_injection
from agents.prompt import build_system_prompt
from agents.session_memory import (
    FOLD_COMPONENT_SYSTEMS,
    FOLD_SESSION_MEMORY_SYSTEM,
    MAX_TAIL_TOKENS,
    TAIL_WINDOW_FRACTION,
    append_anthropic_message,
    select_fold_tail,
    build_anthropic_transcript,
    build_folding_user_prompt,
    build_openai_transcript,
    fallback_folded_memory,
    format_folded_memory,
    parse_folded_memory,
    parse_fold_component,
)
from agents.session import save_folded_session_memory, save_session, validate_session_id
from agents.subagent import get_sub_agent_config
from agents.tools import ToolDef, tool_definitions, execute_tool, CONCURRENCY_SAFE_TOOLS, check_permission, \
    get_active_tool_definitions, validate_tool_arguments
from agents.ui import print_info, print_divider, print_assistant_text, print_sub_agent_start, print_sub_agent_end, \
    start_spinner, stop_spinner, print_cost, print_tool_call, print_tool_result, print_confirmation, print_retry, \
    print_error


_tool_span = ContextVar("tool_span", default=None)

# 指数退避重试


def _is_retryable(error: Exception) -> bool:
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status in (429, 503, 529):
        return True
    msg = str(error)
    if "overloaded" in msg or "ECONNRESET" in msg or "ETIMEDOUT" in msg:
        return True
    return False


def _safe_utf8_text(value: object) -> str:
    return str(value).encode("utf-8", errors="replace").decode("utf-8")


def _sanitize_for_utf8(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_utf8_text(value)
    if isinstance(value, list):
        return [_sanitize_for_utf8(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_for_utf8(item) for item in value)
    if isinstance(value, dict):
        return {
            _sanitize_for_utf8(key): _sanitize_for_utf8(item)
            for key, item in value.items()
        }
    return value


async def _with_retry(fn, max_retries: int = 3):
    for attempt in range(max_retries + 1):
        try:
            return await fn()
        except Exception as error:
            if attempt >= max_retries or not _is_retryable(error):
                raise
            delay = min(1000 * (2 ** attempt), 30000) / 1000 + (hash(str(time.time())) % 1000) / 1000
            status = getattr(error, "status_code", None) or getattr(error, "status", None)
            reason = f"HTTP {status}" if status else (getattr(error, "code", None) or "network error")
            print_retry(attempt + 1, max_retries, reason)
            await asyncio.sleep(delay)

MODEL_CONTEXT = {
    "claude-opus-4-6": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-sonnet-4-20250514": 200000,
    "claude-haiku-4-5-20251001": 200000,
    "claude-opus-4-20250514": 200000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "deepseek-chat":200000
}

def _get_context_windows(model:str)->int:
    return MODEL_CONTEXT.get(model, 200000)


#多层级压缩常数
SNIP_THRESHOLD = 0.60
AUTO_COMPACT_THRESHOLD = 0.70
SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
SNIPPABLE_TOOLS = {"read_file", "grep_search", "list_files", "run_shell"}
MICROCOMPACT_IDLE_S = 5 * 60  # 5 minutes

KEEP_RECENT_RESULTS = 3



def _get_max_output_tokens(model: str) -> int:
    m = model.lower()
    if "opus-4-6" in m:
        return 64000
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    return 16384

#转换tool的形式到openai
def _to_openai_tools(tools: list[ToolDef]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


class Agent:
    def __init__(self,
                 *,
                 permission_mode:str="default",
                 model:str="deepseek-chat",
                 api_base: str | None=None,
                 anthropic_base_url: str | None=None,
                 api_key: str | None=None,
                 thinking: bool | None=None,
                 reasoning_effort: str | None=None,
                 side_thinking: str | None=None,
                 side_reasoning_effort: str | None=None,
                 deepseek_price_period: str | None=None,
                 max_cost_usd: float | None=None,
                 max_turns: int | None=None,
                 max_output_tokens: int | None=None,
                 confirm_fn:Callable[[str], Awaitable[bool]] | None=None,
                 output_fn: Callable[[str], Awaitable[None]] | None = None,
                 custom_system_prompt: str | None=None,
                 custom_tools: list[ToolDef] | None=None,
                 is_sub_agent: bool=False,
                 agent_role: str | None=None,
                 permitted_mcp_tools: list[str] | None=None,
                 web_cache: WebCache | None=None,
                 enable_folding: bool=True,
                 max_fold_limit: int | None=None,
                 fold_memory_mode: str="single_json",
                 auto_compact_threshold: float=AUTO_COMPACT_THRESHOLD,
                 enable_memory_prefetch: bool=True,
                 enable_mcp: bool=True,
                 enable_online_evolution: bool=True,
                 fold_observer: Callable[[dict[str, Any]], None] | None=None,):
        if not 0 < auto_compact_threshold < 1:
            raise ValueError("auto_compact_threshold must be between 0 and 1")
        if max_fold_limit is not None and max_fold_limit < 0:
            raise ValueError("max_fold_limit must be non-negative")
        if fold_memory_mode not in {"single_json", "three_parallel"}:
            raise ValueError("fold_memory_mode must be single_json or three_parallel")
        if max_output_tokens is not None and max_output_tokens < 2:
            raise ValueError("max_output_tokens must be at least 2")
        self.permission_mode = permission_mode
        self.thinking = bool(thinking)
        self.model = model
        self.use_openai = bool(api_base)
        self.is_sub_agent = is_sub_agent
        self.agent_role = agent_role or ("sub_agent" if is_sub_agent else "main")
        self._permitted_mcp_tools = None if permitted_mcp_tools is None else set(permitted_mcp_tools)
        self._web_cache = web_cache if web_cache is not None else WebCache()
        self.tool_mode = os.getenv("RESEARCH_TOOL_MODE", "live").strip().lower()
        if self.tool_mode not in {"live", "record", "replay"}:
            raise ValueError("RESEARCH_TOOL_MODE must be live, record or replay")
        if self.tool_mode == "replay":
            snapshot = os.getenv("RESEARCH_TOOL_SNAPSHOT")
            if not snapshot:
                raise ValueError("RESEARCH_TOOL_SNAPSHOT must select a recorded snapshot in replay mode")
            self._replay_snapshot = Path(snapshot).expanduser()
        self.trace_id = uuid.uuid4().hex
        self._api_base = api_base
        self._anthropic_base_url = anthropic_base_url
        self._api_key = api_key
        self._deepseek = None
        if self.use_openai and is_official_v4_pro(model, api_base):
            self._deepseek = DeepSeekConfig.resolve(
                thinking=thinking, effort=reasoning_effort, side_thinking=side_thinking,
                side_effort=side_reasoning_effort, price_period=deepseek_price_period,
            )
            self.thinking = self._deepseek.thinking == "enabled"
            if max_output_tokens is None:
                max_output_tokens = self._deepseek.default_output_tokens()
            if max_output_tokens > MAX_OUTPUT_TOKENS:
                raise ValueError(f"DeepSeek max_output_tokens must not exceed {MAX_OUTPUT_TOKENS}.")
        elif reasoning_effort is not None:
            raise ValueError("--reasoning-effort requires deepseek-v4-pro with the official HTTPS OpenAI endpoint.")
        self._openai_request_options = self._resolve_openai_request_options()
        self._side_request_options = self._deepseek.request_options(side=True) if self._deepseek else {}
        self.enable_folding = enable_folding
        self.max_fold_limit = max_fold_limit
        self.fold_memory_mode = fold_memory_mode
        self.auto_compact_threshold = auto_compact_threshold
        self.enable_memory_prefetch = enable_memory_prefetch
        self.enable_mcp = enable_mcp
        self.enable_online_evolution = enable_online_evolution
        self.fold_observer = fold_observer
        available_tools = tool_definitions if custom_tools is None else custom_tools
        self._activated_tools: set[str] = set()
        self.tools = [tool for tool in available_tools if not tool["name"].startswith("mcp__")
                      and (enable_folding or tool["name"] != "compact_context")]
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.max_output_tokens = max_output_tokens
        self.confirm_fn = confirm_fn
        self.output_fn = output_fn
        self._custom_system_prompt = custom_system_prompt
        self.effective_window = (CONTEXT_WINDOW - max_output_tokens - 20000
                                 if self._deepseek else _get_context_windows(model) - 20000)
        self.session_id = uuid.uuid4().hex[:8]
        self._candidate_ledger = CandidateLedger(workspace_dir(), self.session_id)
        self._event_log = SessionEventLog(self.session_id, "openai" if self.use_openai else "anthropic")
        self.session_start_time= time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())

        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.side_input_tokens = 0
        self.side_output_tokens = 0
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0
        self.reasoning_tokens = 0
        self._deepseek_cost_usd = 0.0
        self._reset_context_budget()
        self.last_finish_reason: str | None = None
        self.current_turns = 0
        self.last_api_call_time = 0


        self._aborted = False
        #存储异步任务
        self._current_task:asyncio.Task | None = None
        #权限白名单
        self._confirmed_paths: set[str] = set()


        # 计划模式”（Plan Mode）状态的变量
        self._pre_plan_mode: str | None=None
        self._plan_file_path: str | None=None
        self._plan_approval_fn : Callable[[str], Awaitable[dict[str, Any]]] | None=None
        self._context_cleared : bool=False

        #思考模式
        self._thinking_mode = self._resolve_thinking_mode()

        #子agent的输出缓存
        self._output_buffer: list[str] | None=None
        self._turn_output_buffer: list[str] | None = None

        # 编辑前读取
        self._read_file_state: dict[str, float] ={}

        #MCP集成
        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        #记忆回溯
        #记忆agent已经回答过的信息
        self._already_surfaced_memories: set[str] = set()
        #当前会话占用的字节数
        self._session_memory_bytes = 0

        #区分message的历史消息
        self._anthropic_messages: list[str] = []
        self._openai_messages: list[str] = []
        self._last_retrieved_skill_reference: dict[str, Any] | None = None
        self._last_retrieved_skill_hits: list[dict[str, Any]] = []
        self._pending_skill_extraction_window: dict[str, Any] | None = None
        self._background_skill_tasks: set[asyncio.Task] = set()
        self._folded_session_memories: list[dict[str, Any]] = []
        self._fold_last_time: float = 0.0
        self._fold_count: int = 0
        self._tool_error_streak: int = 0
        self._same_tool_repeat_count: int = 0
        self._last_tool_name: str = ""

        #构建系统提示词
        self._base_system_prompt = custom_system_prompt or build_system_prompt(self.tools, self._activated_tools)

        if self.permission_mode == "plan":
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt

        #初始化大模型客户端
        if self.use_openai:
            self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key)
            self._anthropic_client = None
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        else:
            kwargs : dict[str,Any] = {}
            if api_key:
                kwargs["api_key"] = api_key
            if anthropic_base_url:
                kwargs["base_url"] = anthropic_base_url
            self._anthropic_client = anthropic.AsyncAnthropic(**kwargs)
            self._openai_client = None

        self._refresh_runtime_system_prompt()

    def _active_tool_definitions(self) -> list[ToolDef]:
        return get_active_tool_definitions(self.tools, self._activated_tools)

    def _resolve_openai_request_options(self) -> dict[str, Any]:
        return self._deepseek.request_options() if self._deepseek else {}

    def _validate_deepseek_history(self, messages: list[dict]) -> None:
        if self._deepseek and self.thinking and self._active_tool_definitions():
            for message in messages:
                if message.get("role") == "assistant" and not isinstance(message.get("reasoning_content"), str):
                    raise ValueError(
                        "DeepSeek thinking with tools requires reasoning_content on every retained assistant message. "
                        "Start a new session without --resume, or use --no-thinking to resume old history. "
                        "You may /compact in non-thinking mode before resuming with thinking."
                    )

    def _record_openai_usage(self, usage, *, side: bool = False) -> None:
        normalized = normalize_usage(usage)
        if side:
            self.side_input_tokens += normalized["prompt_tokens"]
            self.side_output_tokens += normalized["completion_tokens"]
        else:
            self.total_input_tokens += normalized["prompt_tokens"]
            self.total_output_tokens += normalized["completion_tokens"]
            self._record_context_usage(normalized["prompt_tokens"])
        if self._deepseek:
            self.cache_hit_tokens += normalized["cache_hit_tokens"]
            self.cache_miss_tokens += normalized["cache_miss_tokens"]
            self.reasoning_tokens += normalized["reasoning_tokens"]
            self._deepseek_cost_usd += estimate_cost_usd(normalized, self._deepseek.price_period)

    def _print_cost(self) -> None:
        if self._deepseek:
            print_cost(self.total_input_tokens + self.side_input_tokens,
                       self.total_output_tokens + self.side_output_tokens,
                       estimated_cost_usd=self._get_current_cost_usd(),
                       cost_label=f"{self._deepseek.price_period} estimate")
        else:
            print_cost(self.total_input_tokens, self.total_output_tokens)

    #判断返回模型的思考模式
    def _resolve_thinking_mode(self) -> str:
        if not self.thinking:
            return "disabled"
        if not self._model_supports_thinking():
            return "disabled"

        if self._model_supports_adaptive_thinking():
            return "adaptive"
        return "enabled"

    def _model_supports_thinking(self) -> bool:
        m = self.model.lower()
        if "claude-3-" in m or "3-5-" in m or "3-7-" in m:
            return False
        if "claude" in m and any(x in m for x in ("opus", "sonnet", "haiku")):
            return True
        return False
    def _model_supports_adaptive_thinking(self) -> bool:
        m = self.model.lower()
        return "opus-4-6" in m or "sonnet-4-6" in m

    #生成一个用于保存 AI 计划（Plan）的 Markdown 文件的绝对路径。
    def _generate_plan_file_path(self) -> str:
        d = Path.home() / RESEARCH_DIRNAME / "plans"
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"plan-{self.session_id}.md")

    def _build_plan_mode_prompt(self) -> str:
        return f"""

    # Plan Mode Active

    Plan mode is active. You MUST NOT make any edits (except the plan file below), run non-readonly tools, or make any changes to the system.

    ## Plan File: {self._plan_file_path}
    Write your plan incrementally to this file using write_file or edit_file. This is the ONLY file you are allowed to edit.

    ## Workflow
    1. **Explore**: Read code to understand the task. Use read_file, list_files, grep_search.
    2. **Design**: Design your implementation approach. Use the agent tool with type="plan" if the task is complex.
    3. **Write Plan**: Write a structured plan to the plan file including:
       - **Context**: Why this change is needed
       - **Steps**: Implementation steps with critical file paths
       - **Verification**: How to test the changes
    4. **Exit**: Call exit_plan_mode when your plan is ready for user review.

    IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""

    #判断当前的任务所有的任务是否完成
    @property
    def is_processing(self)->bool:
        return self._current_task is not None and not self._current_task.done()

    #大模型调用的工厂方法,构建一个用于记忆召回（memory recall）的 sideQuery 可调用对象，兼容anthropic, openai。
    def _build_side_query(self, *, max_tokens: int = 256):
        if self._anthropic_client:
            client = self._anthropic_client
            model = self.model
            async def _sq(system:str, user_message:str)->str:

                resp = await client.messages.create(
                    model=model, max_tokens=max(1, int(max_tokens)), system=system,
                messages=[{"role": "user", "content": user_message}],
                )
                usage = getattr(resp, "usage", None)
                self.side_input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
                self.side_output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                if not text.strip():
                    block_types = [str(getattr(b, "type", "")) for b in getattr(resp, "content", [])]
                    logging.warning(
                        "side_query returned empty Anthropic-compatible response: model=%s stop_reason=%s content_block_types=%s",
                        getattr(resp, "model", model),
                        getattr(resp, "stop_reason", ""),
                        block_types,
                    )
                return text
            return _sq
        if self._openai_client:
            client = self._openai_client
            model = self.model
            async def _sq_openai(system:str, user_message:str)->str:
                if self._deepseek and self._check_budget()["exceeded"]:
                    raise ValueError("Auxiliary model request skipped: budget exhausted.")
                resp = await client.chat.completions.create(
                    model=model,
                    max_tokens=max(1, int(max_tokens)),
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user_message},
                    ],
                    **self._side_request_options,
                )
                usage = getattr(resp, "usage", None)
                self._record_openai_usage(usage, side=True)
                if not resp.choices:
                    logging.warning("side_query returned no OpenAI-compatible choices: model=%s", model)
                    return ""
                choice = resp.choices[0]
                if self._deepseek and getattr(choice, "finish_reason", None) in {"length", "content_filter"}:
                    raise ValueError("DeepSeek auxiliary output was truncated or filtered; increase its token budget or disable side thinking.")
                content = choice.message.content or ""
                if not content.strip():
                    logging.warning(
                        "side_query returned empty OpenAI-compatible response: model=%s finish_reason=%s message=%s",
                        model,
                        getattr(choice, "finish_reason", ""),
                        choice.message,
                    )
                return content
            return _sq_openai
        return None
    #异步任务取消（Abort）
    def abort(self) -> None:
        self._aborted = True
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def set_confirm_fn(self, fn:Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn

    def set_plan_approval_fn(self, fn:Callable[[str], Awaitable[dict[str, Any]]]) -> None:
        self._plan_approval_fn = fn


    #计划模式开关（“状态切换与现场保护”机制）
    def toggle_plan_mode(self) -> str:
        """
               1. 退出计划模式（从 plan 切回原模式）
               当当前模式已经是 plan 时，执行 if 分支：
               恢复之前的状态：self.permission_mode = self._pre_plan_mode or "default"。
                   在进入计划模式时，程序会把原本的模式保存在 _pre_plan_mode 里。退出时，就把它重新拿出来赋值回去，恢复到切换前的状态。
               清理计划模式的痕迹：把 _pre_plan_mode 和 _plan_file_path（计划文件路径）清空，并将系统提示词 _system_prompt 恢复为最基础的 _base_system_prompt。
               同步 OpenAI 消息：如果底层使用的是 OpenAI 接口，它还会同步更新消息列表里的第一条系统提示词，确保 AI 的上下文也跟着切换回来。
               反馈返回：打印退出提示，并返回恢复后的模式名称。

               2. 进入计划模式（从其他模式切入 plan）
       当当前模式不是 plan 时，执行 else 分支：
       保护当前现场：self._pre_plan_mode = self.permission_mode。先把当前正在使用的模式（比如正常模式或自动接受模式）暂存起来，方便以后能原路返回。
       切换并初始化：将当前模式设为 "plan"，生成一个专属的计划文件路径，并扩展系统提示词。通过拼接 _build_plan_mode_prompt()，给 AI 注入“只动脑不动手、输出结构化计划”的专属指令。
       同步 OpenAI 消息：同样地，如果使用 OpenAI，也会实时更新上下文里的系统提示词。
       反馈与返回：打印进入提示（包含计划文件的路径），并返回 "plan"。
        """
        if self.permission_mode == "plan":
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] =self._system_prompt
            print_info(f"Exited plan mode -> {self.permission_mode} mode")
            return self.permission_mode
        else:
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path = self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            self._refresh_runtime_system_prompt()
            print_info(f"Entered plan mode. Plan file: {self._plan_file_path}")
            return "plan"

    def get_token_usage(self) -> dict:
        return {"input":self.total_input_tokens, "output":self.total_output_tokens}

    #主入口

    async def chat(self, user_message: str) -> None:
        try:
            await self._chat_turn(user_message)
        finally:
            await self._event_log.flush()

    async def _chat_turn(self, user_message: str) -> None:
        parent = _tool_span.get()
        self.trace_id = parent[0] if parent else uuid.uuid4().hex
        original_user_message = _safe_utf8_text(user_message)
        self._event_log.record(self.trace_id, "user_message", {"content": original_user_message})
        #懒加载MCP服务在第一次chat的时候
        if self.enable_mcp and not self._mcp_initialized:
            self._mcp_initialized = True
            try:
                await self._mcp_manager.load_and_connect(self.agent_role, permitted_tools=self._permitted_mcp_tools)
                mcp_defs = self._mcp_manager.get_tool_definitions(self.agent_role)
                if mcp_defs:
                    self.tools = self.tools + mcp_defs
            except asyncio.CancelledError:
                self._mcp_initialized = False
                await self._mcp_manager.disconnect_all()
                raise
            except Exception as e:
                print_error(f"MCP init failed: {e}")

        ready_skill_extraction_window: dict[str, Any] | None = None
        self._last_retrieved_skill_reference = None
        self._last_retrieved_skill_hits = []
        if not self.is_sub_agent:
            ready_skill_extraction_window = self._pop_pending_skill_extraction_window(original_user_message)
            user_message, self._last_retrieved_skill_reference = self._augment_user_message_with_skill_context(
                original_user_message
            )

        self._aborted = False
        self._turn_output_buffer = []
        coro = self._chat_openai(user_message) if self.use_openai else self._chat_anthropic(user_message)
        self._current_task = asyncio.create_task(coro)
        try:
            await self._current_task
        except asyncio.CancelledError:
            self._aborted = True
            if asyncio.current_task().cancelling():
                self._turn_output_buffer = None
                if not self.is_sub_agent:
                    self._auto_save()
                raise

        finally:
            self._current_task = None
        assistant_text = "".join(self._turn_output_buffer or []).strip()
        self._turn_output_buffer = None
        if not self.is_sub_agent and not self._aborted:
            self._schedule_background_skill_task(self._run_skill_usage_tracking(original_user_message, assistant_text))
            if ready_skill_extraction_window:
                self._schedule_background_skill_task(self._run_online_skill_evolution(ready_skill_extraction_window))
            self._set_pending_skill_extraction_window(
                original_user_message=original_user_message,
                assistant_text=assistant_text,
                retrieved_reference=self._last_retrieved_skill_reference,
            )
        if not self.is_sub_agent:
            print_divider()
            self._auto_save()



   #执行一次对话，收集本轮模型输出文本，并返回本轮消耗的 token 数
    async def run_once(self, prompt:str)->None:
        self._output_buffer = []
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        prev_side = (self.side_input_tokens, self.side_output_tokens)
        prev_usage = (self.cache_hit_tokens, self.cache_miss_tokens, self.reasoning_tokens, self._deepseek_cost_usd)
        try:
            await self.chat(prompt)
        finally:
            if self.is_sub_agent:
                await self._mcp_manager.disconnect_all()
                self._mcp_initialized = False
                self.tools = [tool for tool in self.tools if not tool["name"].startswith("mcp__")]
        text = "".join(self._output_buffer)
        self._output_buffer = None
        return {
            "text": text,
            "tokens":{
                "input":self.total_input_tokens-prev_in,
                "output":self.total_output_tokens-prev_out
            },
            "side_tokens": {"input": self.side_input_tokens - prev_side[0], "output": self.side_output_tokens - prev_side[1]},
            "deepseek_usage": {
                "cache_hit_tokens": self.cache_hit_tokens - prev_usage[0],
                "cache_miss_tokens": self.cache_miss_tokens - prev_usage[1],
                "reasoning_tokens": self.reasoning_tokens - prev_usage[2],
                "estimated_cost_usd": self._deepseek_cost_usd - prev_usage[3],
            },
        }

    def _merge_sub_agent_usage(self, result: dict) -> None:
        side = result.get("side_tokens", {})
        self.side_input_tokens += side.get("input", 0)
        self.side_output_tokens += side.get("output", 0)
        usage = result.get("deepseek_usage", {})
        self.cache_hit_tokens += usage.get("cache_hit_tokens", 0)
        self.cache_miss_tokens += usage.get("cache_miss_tokens", 0)
        self.reasoning_tokens += usage.get("reasoning_tokens", 0)
        self._deepseek_cost_usd += usage.get("estimated_cost_usd", 0.0)

    def _sub_agent_model_options(self) -> dict:
        if not self._deepseek:
            return {}
        return {"thinking": self.thinking, "max_output_tokens": self.max_output_tokens,
                "reasoning_effort": self._deepseek.effort,
                "side_thinking": self._deepseek.side_thinking,
                "side_reasoning_effort": self._deepseek.side_effort,
                "deepseek_price_period": self._deepseek.price_period}

    #输出工具：统一处理模型输出文本。根据当前是否处于“收集输出”的模式
    # 决定是把文本存进缓冲区，还是直接打印到终端。
    async def _emit_text(self, text: str, *, thinking: bool = False) -> None:
        text = _safe_utf8_text(text)
        if self._turn_output_buffer is not None:
            self._turn_output_buffer.append(text)
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        elif self.output_fn is None:
            print_assistant_text(text)
        if self.output_fn is not None and not thinking:
            await self.output_fn(text)

    def _build_fold_guidance_section(self) -> str:
        if self._custom_system_prompt is not None or not self.enable_folding:
            return ""
        if self._fold_limit_reached():
            return "\n\n# Runtime Fold Guidance\n- Context folding limit reached. Continue without calling `compact_context`.\n"
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0.0
        last_fold = "never" if not self._fold_last_time else f"{int((time.time() - self._fold_last_time) / 60)}m ago"
        return (
            "\n\n# Runtime Fold Guidance\n"
            f"- Current context utilization: {utilization:.0%}\n"
            f"- Recent tool error streak: {self._tool_error_streak}\n"
            f"- Same tool repeat count: {self._same_tool_repeat_count}\n"
            f"- Last fold: {last_fold}\n"
            "- If the context is getting long, the same tool is being retried without progress, or tool failures are accumulating, call `compact_context` before trying more tools.\n"
            "- If you folded very recently and the next step is clear, prefer continuing rather than folding again.\n"
        )

    def _refresh_runtime_system_prompt(self) -> None:
        if self._custom_system_prompt is not None:
            return
        self._base_system_prompt = build_system_prompt(self.tools, self._activated_tools)
        if self.permission_mode == "plan":
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
        else:
            self._system_prompt = self._base_system_prompt
        self._system_prompt += self._build_fold_guidance_section()
        if self.use_openai and self._openai_messages:
            self._openai_messages[0]["content"] = self._system_prompt

    def _record_tool_outcome(self, tool_name: str, success: bool) -> None:
        if tool_name == self._last_tool_name:
            self._same_tool_repeat_count += 1
        else:
            self._same_tool_repeat_count = 1
        self._last_tool_name = tool_name
        if success:
            self._tool_error_streak = 0
        else:
            self._tool_error_streak += 1

    def _record_fold_event(self) -> None:
        self._fold_last_time = time.time()
        self._fold_count += 1
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""

    def _augment_user_message_with_skill_context(self, user_message: str) -> tuple[str, dict[str, Any] | None]:
        try:
            from .skills import format_retrieved_skill_context

            context, top_ref = format_retrieved_skill_context(user_message, limit=3)
        except Exception:
            return user_message, None
        if top_ref and isinstance(top_ref.get("all_hits"), list):
            self._last_retrieved_skill_hits = list(top_ref.get("all_hits") or [])
        if not context.strip():
            return user_message, top_ref
        return f"{user_message}\n\n{context}", top_ref

    def _strip_runtime_injections(self, text: str) -> str:
        return re.sub(r"\n*<retrieved_skills>.*?</retrieved_skills>\s*", "", str(text or ""), flags=re.DOTALL).strip()

    def _message_text(self, msg: dict[str, Any]) -> str:
        content = msg.get("content")
        if isinstance(content, str):
            return self._strip_runtime_injections(content)
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(str(block.get("text") or ""))
                    elif "content" in block and block.get("type") not in {"tool_result", "tool_use"}:
                        parts.append(str(block.get("content") or ""))
            return self._strip_runtime_injections("\n".join(parts))
        return ""

    def _recent_dialog_messages(self, *, max_messages: int = 8) -> list[dict[str, str]]:
        raw_messages = self._openai_messages if self.use_openai else self._anthropic_messages
        out: list[dict[str, str]] = []
        for msg in raw_messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            text = self._message_text(msg)
            if text:
                out.append({"role": role, "content": text})
        return out[-max(2, int(max_messages)) :]

    async def _confirm_online_skill_write(self, summary: str) -> bool:
        if self.permission_mode in {"bypassPermissions", "acceptEdits"}:
            return True
        if self.permission_mode in {"plan", "dontAsk"}:
            return False
        if self.confirm_fn is None:
            return False
        print_confirmation(summary)
        try:
            return bool(await self.confirm_fn(summary))
        except Exception:
            return False

    async def _confirm_background_online_skill_write(self, summary: str) -> bool:
        return self.permission_mode in {"bypassPermissions", "acceptEdits"}

    def _online_evolution_enabled(self) -> bool:
        if not self.enable_online_evolution:
            return False
        raw = os.environ.get("RESEARCH_AUTO_SKILL_EVOLUTION", "1").strip().lower()
        return raw not in {"0", "false", "no", "off"}

    def _schedule_background_skill_task(self, coro) -> None:
        if self.permission_mode == "plan":
            try:
                coro.close()
            except Exception:
                pass
            return
        task = asyncio.create_task(coro)
        self._background_skill_tasks.add(task)

        def _done(done_task: asyncio.Task) -> None:
            self._background_skill_tasks.discard(done_task)
            try:
                done_task.result()
            except Exception:
                pass

        task.add_done_callback(_done)

    async def drain_background_skill_tasks(self) -> None:
        tasks = [task for task in self._background_skill_tasks if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _pop_pending_skill_extraction_window(self, next_user_feedback: str) -> dict[str, Any] | None:
        pending = self._pending_skill_extraction_window
        self._pending_skill_extraction_window = None
        if not pending:
            return None
        messages = list(pending.get("messages") or [])
        feedback = _safe_utf8_text(next_user_feedback).strip()
        if feedback:
            messages.append({"role": "user", "content": feedback})
        pending["messages"] = messages[-10:]
        pending["next_user_feedback"] = feedback
        return pending

    def _set_pending_skill_extraction_window(
        self,
        *,
        original_user_message: str,
        assistant_text: str,
        retrieved_reference: dict[str, Any] | None,
    ) -> None:
        if not original_user_message.strip() or not assistant_text.strip():
            return
        self._pending_skill_extraction_window = {
            "messages": self._recent_dialog_messages(max_messages=8),
            "latest_user": original_user_message,
            "latest_assistant": assistant_text,
            "retrieved_reference": self._compact_retrieved_reference(retrieved_reference),
            "session_id": self.session_id,
        }

    def _compact_retrieved_reference(self, ref: dict[str, Any] | None) -> dict[str, Any] | None:
        if not ref:
            return None
        return {k: v for k, v in ref.items() if k != "all_hits"}

    async def _run_online_skill_evolution(self, window: dict[str, Any], *, interactive_confirm: bool = False) -> None:
        if not self._online_evolution_enabled() or self.permission_mode == "plan":
            return
        messages = list(window.get("messages") or [])
        if not messages:
            return

        side_query = self._build_side_query(max_tokens=2200)
        if side_query is None:
            return

        try:
            from .online_skill_evolution import online_ingest
        except Exception:
            return

        result = await online_ingest(
            messages=messages,
            side_query=side_query,
            retrieved_reference=window.get("retrieved_reference") or None,
            hint=str(window.get("hint") or ""),
            confirm_write=self._confirm_online_skill_write if interactive_confirm else self._confirm_background_online_skill_write,
            target=os.environ.get("RESEARCH_AUTO_SKILL_TARGET", "project"),
        )
        if result.get("ok"):
            if result.get("action") in {"add", "merge"}:
                self._refresh_runtime_system_prompt()
                print_info(f"Online skill {result.get('action')}: {result.get('skill')}")
        elif result.get("action") not in {"add_denied", "merge_denied"}:
            print_error(f"Online skill evolution failed: {result.get('error') or result}")

    async def _run_skill_usage_tracking(self, original_user_message: str, assistant_text: str) -> None:
        if not self._online_evolution_enabled() or self.permission_mode == "plan":
            return
        hits = list(self._last_retrieved_skill_hits or [])
        if not hits or not assistant_text.strip():
            return
        side_query = self._build_side_query(max_tokens=700)
        try:
            from .online_skill_evolution import judge_retrieved_skill_usage
            from .skills import record_usage_judgments

            judgments = await judge_retrieved_skill_usage(
                hits=hits,
                user_message=original_user_message,
                assistant_text=assistant_text,
                side_query=side_query,
            )
            result = record_usage_judgments(judgments)
            if result.get("pruned"):
                self._refresh_runtime_system_prompt()
        except Exception:
            return

    async def extract_now(self, hint: str = "") -> dict[str, Any]:
        pending = self._pending_skill_extraction_window
        if not pending:
            return {"ok": False, "error": "no pending online skill extraction window"}
        window = dict(pending)
        window["hint"] = hint
        await self._run_online_skill_evolution(window, interactive_confirm=True)
        self._pending_skill_extraction_window = None
        return {"ok": True}


    def clear_history(self)->None:
        self._anthropic_messages = []
        self._openai_messages = []
        self._pending_skill_extraction_window = None
        self._last_retrieved_skill_reference = None
        self._last_retrieved_skill_hits = []
        self._fold_last_time = 0.0
        self._fold_count = 0
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content":self._system_prompt})
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.side_input_tokens = 0
        self.side_output_tokens = 0
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0
        self.reasoning_tokens = 0
        self._deepseek_cost_usd = 0.0
        self._reset_context_budget()
        print_info("Conversation cleared.")

    def show_cost(self):
        total = self._get_current_cost_usd()
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        note = ""
        if self._deepseek:
            note = (f"\n  DeepSeek {self._deepseek.price_period} USD estimate (2026-10-12 rates), including auxiliary requests."
                    f"\n  Cache: {self.cache_hit_tokens} hit / {self.cache_miss_tokens} miss; reasoning: {self.reasoning_tokens} tokens."
                    " Actual charges are determined by the provider.")
        print_info(
            f"Tokens: {self.total_input_tokens + self.side_input_tokens} in / {self.total_output_tokens + self.side_output_tokens} out\n  Estimated cost: ${total:.4f}{budget_info}{turn_info}{note}")

    #获取当前的花费，
    def _get_current_cost_usd(self) -> float:
        if self._deepseek:
            return self._deepseek_cost_usd
        return ((self.total_input_tokens + self.side_input_tokens) / 1_000_000) * 3 + ((self.total_output_tokens + self.side_output_tokens) / 1_000_000) * 15

    #检查预算
    def _check_budget(self) -> dict:
        if self.max_cost_usd is not None and self._get_current_cost_usd() >= self.max_cost_usd:
            return {"exceeded": True, "reason": f"Cost limit reached (${self._get_current_cost_usd():.4f} >= ${self.max_cost_usd})"}
        if self.max_turns is not None and self.current_turns >= self.max_turns:
            return {"exceeded": True, "reason": f"Turn limit reached ({self.current_turns} >= {self.max_turns})"}
        return {"exceeded": False}

    #压缩会话
    async def compact(self)->None:
        if not self.enable_folding:
            print_info("Context folding is disabled for this run.")
            return
        compacted = await self._compact_conversation(trigger="manual")
        if not compacted:
            print_info("Nothing to compact yet.")
        else:
            # 主动压缩后立即保存，允许旧历史在关闭思考压缩后安全切换模式。
            self._auto_save()
            await self._event_log.flush()


    #恢复会话信息
    def restore_session(self, data:dict)->None:
        metadata = data.get("metadata", {})
        if metadata.get("project_hash") and metadata["project_hash"] != _project_hash():
            raise ValueError("Cannot resume a session from a different project.")
        protocol = "openai" if self.use_openai else "anthropic"
        other_history = "anthropicMessages" if self.use_openai else "openaiMessages"
        if metadata.get("protocol", protocol) != protocol or data.get(other_history):
            raise ValueError(f"Cannot resume session: model protocol does not match {protocol}.")
        session_id = metadata.get("session_id", metadata.get("id"))
        if session_id is not None:
            validate_session_id(session_id)
        if data.get("openaiMessages"):
            self._validate_deepseek_history(data["openaiMessages"])
        if data.get("anthropicMessages"):
            self._anthropic_messages = self._normalize_anthropic_messages(_sanitize_for_utf8(data["anthropicMessages"]))
        if data.get("openaiMessages"):
            self._openai_messages = _sanitize_for_utf8(data["openaiMessages"])
        if isinstance(data.get("foldedSessionMemories"), list):
            self._folded_session_memories = _sanitize_for_utf8(data["foldedSessionMemories"])
        if session_id is not None:
            if session_id != self.session_id:
                self._activated_tools.clear()
            self.session_id = session_id
            self._candidate_ledger = CandidateLedger(workspace_dir(), session_id)
            self.session_start_time = metadata.get("created_at") or metadata.get("startTime") or self.session_start_time
            if self.permission_mode == "plan":
                self._plan_file_path = self._generate_plan_file_path()
        self._reset_context_budget()
        self._refresh_runtime_system_prompt()
        self._event_log = SessionEventLog(self.session_id, protocol)
        self._event_log.record(self.trace_id, "session_resumed", {
            "message_count": self._get_message_count(), "schema_version": metadata.get("schema_version", 1),
        })
        print_info(f"Session {self.session_id} restored ({self._get_message_count()} messages).")



#整理 Anthropic 的历史消息，修正部分角色错误，并丢弃不合法的工具调用消息。
    def _normalize_anthropic_messages(self, messages: list[dict]) -> list[dict]:
        role_normalized = []
        for msg in messages:
            copied = dict(msg)
            content = copied.get("content")
            if copied.get("role") == "user" and isinstance(content, list):
                if any(isinstance(block, dict) and block.get("type") == "tool_use" for block in content):
                    copied["role"] = "assistant"
            role_normalized.append(copied)

        normalized = []
        i = 0
        while i < len(role_normalized):
            msg = role_normalized[i]
            tool_use_ids = self._anthropic_tool_use_ids(msg)
            if not tool_use_ids:
                normalized.append(msg)
                i += 1
                continue

            next_msg = role_normalized[i + 1] if i + 1 < len(role_normalized) else None
            result_ids = self._anthropic_tool_result_ids(next_msg) if next_msg else set()
            if tool_use_ids.issubset(result_ids):
                normalized.append(msg)
                normalized.append(next_msg)
                i += 2
                continue

            i += 1
        return normalized

    @staticmethod
    def _anthropic_tool_use_ids(msg: dict | None) -> set[str]:
        if not msg or msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            return set()
        return {
            block.get("id")
            for block in msg["content"]
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id")
        }

    @staticmethod
    def _anthropic_tool_result_ids(msg: dict | None) -> set[str]:
        if not msg or msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            return set()
        return {
            block.get("tool_use_id")
            for block in msg["content"]
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("tool_use_id")
        }

    def _get_message_count(self) -> int:
        return len(self._openai_messages) if self.use_openai else len(self._anthropic_messages)

    def _auto_save(self) -> None:
        try:
            save_session(self.session_id, {
                "metadata": {
                    "id": self.session_id,
                    "session_id": self.session_id,
                    "project_hash": _project_hash(),
                    "protocol": "openai" if self.use_openai else "anthropic",
                    "created_at": self.session_start_time,
                    "updated_at": datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                    "schema_version": 2,
                    "model": self.model,
                    "cwd": str(workspace_dir()),
                    "startTime": self.session_start_time,
                    "messageCount": self._get_message_count(),
                    "thinking": self.thinking,
                    "reasoningEffort": self._deepseek.effort if self._deepseek else None,
                },
                "anthropicMessages": _sanitize_for_utf8(self._anthropic_messages) if not self.use_openai else None,
                "openaiMessages": _sanitize_for_utf8(self._openai_messages) if self.use_openai else None,
                "foldedSessionMemories": _sanitize_for_utf8(self._folded_session_memories),
            })
        except Exception:
            pass

    #自动压缩
    def _reset_context_budget(self) -> None:
        self.last_input_token_count = 0
        self.last_accounted_msg_index = 0
        self._accounted_system_prompt = ""

    def _record_context_usage(self, tokens: int) -> None:
        if tokens > 0:
            self.last_input_token_count = tokens
            self.last_accounted_msg_index = self._get_message_count()
            self._accounted_system_prompt = self._system_prompt

    @staticmethod
    def _estimate_tokens(value: Any) -> int:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        # UTF-8 captures multibyte text. Digits and punctuation in code/JSON
        # can tokenize individually, so give them a higher weight than prose.
        size = len(text.encode("utf-8", errors="replace"))
        dense = sum(not char.isalpha() and not char.isspace() for char in text)
        return (size + 2 * dense + 2) // 3 + 4

    async def _ensure_context_budget(self) -> None:
        if not self.enable_folding or self._fold_limit_reached():
            return
        messages = self._openai_messages if self.use_openai else self._anthropic_messages
        estimate = self.last_input_token_count
        if not self.last_accounted_msg_index:
            tools = self._active_tool_definitions()
            estimate += self._estimate_tokens(_to_openai_tools(tools) if self.use_openai else tools)
            if not self.use_openai:
                estimate += self._estimate_tokens(self._system_prompt)
        elif self._accounted_system_prompt != self._system_prompt:
            estimate += self._estimate_tokens(self._system_prompt) - self._estimate_tokens(self._accounted_system_prompt)
        estimate += sum(self._estimate_tokens(msg) for msg in messages[self.last_accounted_msg_index:])
        if estimate > self.effective_window * self.auto_compact_threshold:
            print_info("Context window filling up, compacting conversation...")
            await self._compact_conversation(trigger="auto")

    def _fold_limit_reached(self) -> bool:
        return self.max_fold_limit is not None and self._fold_count >= self.max_fold_limit

    async def _compact_conversation(self, *, trigger: str = "manual") -> bool:
        if not self.enable_folding or self._fold_limit_reached():
            return False
        messages = self._openai_messages if self.use_openai else self._anthropic_messages
        minimum = 3 if not self.use_openai and trigger == "auto" else 4
        if len(messages) < minimum:
            return False
        pending = (messages[-1] if trigger == "auto" and messages[-1].get("role") == "user"
                   and isinstance(messages[-1].get("content"), str) else None)
        history = messages[:-1] if pending else messages
        build_transcript = build_openai_transcript if self.use_openai else build_anthropic_transcript
        transcript = build_transcript(_sanitize_for_utf8(history))
        if not transcript.strip():
            return False

        # Manual compaction in non-thinking mode also upgrades legacy DeepSeek
        # snapshots for a subsequent thinking-mode resume with tools.
        require_reasoning = bool(self._deepseek and (self.thinking or trigger == "manual")
                                 and self._active_tool_definitions())
        # The active compact_context call has no receipt yet, so the complete-
        # batch check in select_fold_tail excludes it and any sibling receipts.
        tail_source = history
        first = int(self.use_openai)
        if self._folded_session_memories and len(tail_source) > first:
            previous = tail_source[first]
            if (previous.get("role") == "user" and isinstance(previous.get("content"), str)
                    and previous["content"].startswith("<session-folded-memory>")):
                tail_source = tail_source[first + 1:]
        tail = select_fold_tail(tail_source, openai=self.use_openai,
                                budget=min(int(self.effective_window * TAIL_WINDOW_FRACTION), MAX_TAIL_TOKENS),
                                estimate=self._estimate_tokens, require_reasoning=require_reasoning)
        memory = await self._generate_folded_session_memory(transcript, allow_fallback=trigger != "tool")
        self._notify_fold_observer(trigger, transcript, memory)
        summary = {"role": "user", "content": format_folded_memory(memory)}
        if self.use_openai:
            view = [messages[0], summary, *tail]
            if pending:
                view.append(pending)
            self._openai_messages = view
        else:
            view = [summary]
            if tail and tail[0]["role"] == "user":
                view.append({"role": "assistant", "content": "Understood."})
            for message in tail:
                append_anthropic_message(view, message)
            if pending:
                append_anthropic_message(view, pending)
            self._anthropic_messages = view
        # A tool/threshold fold immediately issues another request. A trailing
        # assistant answer would otherwise be treated as an assistant prefill.
        if trigger in ("auto", "tool") and view[-1]["role"] == "assistant":
            view.append({"role": "user", "content": "Continue the task from the folded session memory."})

        self._record_fold_event()
        self._record_folded_session_memory(trigger, memory, len(messages), len(tail), pending is not None)
        self._reset_context_budget()
        self._refresh_runtime_system_prompt()
        print_info("Conversation compacted.")
        return True

    async def _generate_folded_session_memory(self, transcript: str, *, allow_fallback: bool = True) -> dict[str, Any]:
        def fallback():
            if not allow_fallback:
                raise ToolFailure("Context compaction failed to generate a summary. Conversation history was preserved.")
            return fallback_folded_memory(transcript)

        if self.fold_memory_mode == "three_parallel":
            side_query = self._build_side_query(max_tokens=2500)
            if side_query is None:
                return fallback()
            prompt = "Extract the requested memory from this conversation.\n\nConversation transcript:\n" + transcript
            names = tuple(FOLD_COMPONENT_SYSTEMS)
            results = await asyncio.gather(
                *(side_query(FOLD_COMPONENT_SYSTEMS[name], prompt) for name in names),
                return_exceptions=True,
            )
            if any(isinstance(result, BaseException) for result in results):
                logging.warning("parallel fold memory generation failed; preserving transcript in fallback memory")
                return fallback()
            try:
                return {name: parse_fold_component(name, raw) for name, raw in zip(names, results)}
            except (ValueError, TypeError, json.JSONDecodeError):
                logging.warning("parallel fold memory response was malformed; preserving transcript in fallback memory")
                return fallback()
        side_query = self._build_side_query(max_tokens=6000)
        if side_query is None:
            return fallback()
        try:
            raw = await side_query(FOLD_SESSION_MEMORY_SYSTEM, build_folding_user_prompt(transcript))
            return parse_folded_memory(raw)
        except Exception:
            return fallback()

    def _notify_fold_observer(self, trigger: str, transcript: str, memory: dict[str, Any]) -> None:
        if self.fold_observer is None:
            return
        try:
            self.fold_observer({
                "trigger": trigger,
                "fold_memory_mode": self.fold_memory_mode,
                "transcript": transcript,
                "memory": memory,
                "pre_fold_input_tokens": self.last_input_token_count,
            })
        except Exception:
            logging.exception("fold observer failed")

    def _record_folded_session_memory(self, trigger: str, memory: dict[str, Any],
                                      previous_count: int, retained_count: int, pending_user: bool) -> None:
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "trigger": trigger,
            "fold_memory_mode": self.fold_memory_mode,
            "session_id": self.session_id,
            "pre_fold_input_tokens": self.last_input_token_count,
            "effective_window": self.effective_window,
            "folded_memory_characters": len(format_folded_memory(memory)),
            **memory,
        }
        self._event_log.record(self.trace_id, "context_fold", {
            "trigger": "threshold" if trigger == "auto" else trigger,
            "pre_fold_message_count": previous_count,
            "start_index": int(self.use_openai), "end_index_exclusive": previous_count - int(pending_user),
            "retained_tail_messages": retained_count, "pending_user_messages": int(pending_user), "memory": memory,
        })
        self._folded_session_memories.append(record)
        try:
            save_folded_session_memory(self.session_id, _sanitize_for_utf8(record))
        except Exception:
            pass

    #多层级压缩流水线
    def _run_compression_pipeline(self)->None:
        if self.use_openai:
            changes = {"budget": self._budget_tool_results_openai(),
                       "snip": self._snip_stale_results_openai(), "idle": self._microcompact_openai()}
        else:
            changes = {"budget": self._budget_tool_results_anthropic(),
                       "snip": self._snip_stale_results_anthropic(), "idle": self._microcompact_anthropic()}
        affected = {stage: indices for stage, indices in changes.items() if indices}
        if affected:
            self._event_log.record(self.trace_id, "micro_compact", {
                "message_indices_by_stage": affected, "message_count": self._get_message_count(),
                "pre_compact_input_tokens": self.last_input_token_count,
            })
            self._reset_context_budget()

    #第一层级压缩，预算压缩
    def _budget_tool_results_anthropic(self)-> list[int]:
        #计算利用率：utilization = 已用Token / 有效窗口大小。
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        #如果利用率低于 50%，说明空间还很充裕，直接返回，不做任何处理。
        if utilization < 0.5:
            return []
        #动态预算（Budget）：危急状态（>70%）：如果利用率很高，允许单个工具结果保留 15,000 个字符。
        # 警戒状态（50%-70%）：如果利用率中等，只允许保留 30000 个字符。
        budget = 15000 if utilization > 0.7 else 30000

        changed = []
        for index, msg in enumerate(self._anthropic_messages):

            #只处理 role 为 "user" 的消息。在工具调用流程中，工具的执行结果通常是以“用户”的身份反馈给模型的。

            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and len(block["content"]) > budget:
                    #计算保留长度 (keep)：keep = (budget - 80) // 2 这里预留了约 80 个字符的空间给中间的提示语，剩下的长度平分给开头和结尾。
                    keep = (budget - 80) // 2
                    #重组新内容 = 开头部分 + 提示语 + 结尾部分
                    block["content"] = block["content"][:keep] + f"\n\n[... budgeted: {len(block['content']) - keep * 2} chars truncated ...]\n\n" + block["content"][-keep:]
                    changed.append(index)
        return changed

    def _budget_tool_results_openai(self)-> list[int]:
        #计算利用率：utilization = 已用Token / 有效窗口大小。
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        #如果利用率低于 50%，说明空间还很充裕，直接返回，不做任何处理。
        if utilization < 0.5:
            return []
        #动态预算（Budget）：危急状态（>70%）：如果利用率很高，允许单个工具结果保留 15,000 个字符。
        # 警戒状态（50%-70%）：如果利用率中等，只允许保留 30000 个字符。
        budget = 15000 if utilization > 0.7 else 30000

        changed = []
        for index, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and len(msg["content"]) > budget:
                keep = (budget - 80) // 2
                msg["content"] = msg["content"][:keep] + f"\n\n[... budgeted: {len(msg['content']) - keep * 2} chars truncated ...]\n\n" + msg["content"][-keep:]
                changed.append(index)
        return changed


    #第二级策略：修剪过期的工具执行结果
    def _snip_stale_results_anthropic(self) -> list[int]:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return []
        results = []
        for mindex,  msg in enumerate(self._anthropic_messages):
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue

            for bindex, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] != SNIP_PLACEHOLDER:
                    tool_use_id = block.get("tool_use_id")
                    # 对每个 tool_result，通过 tool_use_id 反查它来自哪个工具
                    tool_info = self._find_tool_use_by_id(tool_use_id)
                    if tool_info and tool_info["name"] in SNIPPABLE_TOOLS:
                        results.append({"mindex": mindex, "bindex": bindex, "name": tool_info["name"], "file_path": tool_info.get("input", {}).get("file_path")})

        if len(results) <= KEEP_RECENT_RESULTS:
            return []

        to_snip =  set()
        seen_files: dict[str, list[int]] = {}

        for i, r in enumerate(results):
            if r["name"] == "read_file" and r.get("file_path"):
                seen_files.setdefault(r["file_path"], []).append(i)
        #如果一个文件被读取了多次，只保留最后一次读取的结果，把前面几次读取的内容全部标记为“修剪”（Snip）。
        for indices in seen_files.values():
            if len (indices) >1 :
                for j in indices[:-1]:
                    to_snip.add (j)

        snip_before = len(results) - KEEP_RECENT_RESULTS
        for i in range (snip_before):
            to_snip.add(i)

        for idx in to_snip:
            r = results[idx]
            self._anthropic_messages[r["mindex"]]["content"][r["bindex"]]["content"] = SNIP_PLACEHOLDER
        return [results[idx]["mindex"] for idx in sorted(to_snip)]

    def _snip_stale_results_openai(self) -> list[int]:
        utilization = self.last_input_token_count / self.effective_window if self.effective_window else 0
        if utilization < SNIP_THRESHOLD:
            return []
        tool_msgs = []
        for i, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] != SNIP_PLACEHOLDER:
                tool_msgs.append(i)
        if len(tool_msgs) <= KEEP_RECENT_RESULTS:
            return []
        snip_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(snip_count):
            self._openai_messages[tool_msgs[i]]["content"] = SNIP_PLACEHOLDER
        return tool_msgs[:snip_count]

    #微压缩

    #基于“时间”的上下文瘦身策略，
    #如果已经很久没说话了，说明之前的工具执行结果你已经看完了，那就把它们清理掉，腾出空间

    def _microcompact_anthropic(self) -> list[int]:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return []

        all_results = []
        for mindex, msg in enumerate(self._anthropic_messages):
            if msg.get("role")!="user" or not isinstance(msg.get("content"), list):
                continue
            for bindex, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), str) and block["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                    all_results.append((mindex, bindex))

        clear_count = len(all_results) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            mi, bi = all_results[i]
            self._anthropic_messages[mi]["content"][bi]["content"] = "[Old result cleared]"
        return [mi for mi, _ in all_results[:max(0, clear_count)]]

    def _microcompact_openai(self) -> list[int]:
        if not self.last_api_call_time or (time.time() - self.last_api_call_time) < MICROCOMPACT_IDLE_S:
            return []
        tool_msgs = []
        for i, msg in enumerate(self._openai_messages):
            if msg.get("role") == "tool" and isinstance(msg.get("content"), str) and msg["content"] not in (SNIP_PLACEHOLDER, "[Old result cleared]"):
                tool_msgs.append(i)
        clear_count = len(tool_msgs) - KEEP_RECENT_RESULTS
        for i in range(max(0, clear_count)):
            self._openai_messages[tool_msgs[i]]["content"] = "[Old result cleared]"
        return tool_msgs[:max(0, clear_count)]

    def _find_tool_use_by_id(self, tool_use_id: int) -> dict | None:
        for msg in self._anthropic_messages:
            if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
                continue

            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id") == tool_use_id:
                    return {"name": block["name"], "input": block.get("input", {})}

    #大结果持久化
    #如果工具返回的结果太大（超过 30KB），不要硬塞进上下文里，而是把它存成一个临时文件。
    # 然后在对话里只留一个‘文件路径’和‘内容预览’。如果模型后面还需要看完整内容，它可以再次调用工具去读取这个文件

    def _persist_large_result(self, tool_name: str, result: str, *, call_id: str | None = None,
                              is_error: bool = False) -> str:
        content = _safe_utf8_text(result)
        size = len(content.encode("utf-8"))
        path = None
        if size > 30 * 1024:
            directory = Path.home() / RESEARCH_DIRNAME / "tool-results"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{uuid.uuid4().hex}-{tool_name}.txt"
            path.write_text(content, encoding="utf-8")
            lines = content.split("\n")
            preview = "\n".join(lines[:200])[:8192]
            content = (
                f"[Result too large ({size / 1024:.1f} KB, {len(lines)} lines). "
                f"Full output saved to {path}. "
                f"You can use read_file to see the full result.]\n\n"
                f"Preview (first 200 lines):\n{preview}"
            )
        if call_id is not None:
            self._event_log.tool_result(self.trace_id, call_id, tool_name, content,
                                        is_error=is_error, artifact_path=path)
        return content

    def _anthropic_skipped_result(self, tool, reason: str) -> dict:
        content = self._persist_large_result(tool.name, reason, call_id=tool.id, is_error=True)
        return {"type": "tool_result", "tool_use_id": tool.id, "content": content, "is_error": True}

    #执行工具入口

    @staticmethod
    def _validate_tool_call_identity(call_id, name, seen_ids: set[str]) -> None:
        """Reject broken envelopes before recording history or executing any call."""
        if not isinstance(call_id, str) or not call_id.strip():
            raise ValueError("Tool call is missing a valid call ID; no tools executed.")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Tool call is missing a valid function name; no tools executed.")
        if call_id in seen_ids:
            raise ValueError(f"Duplicate tool call ID: {call_id}; no tools executed.")
        seen_ids.add(call_id)

    async def _execute_tool_call(self, name: str, inp: dict) -> ToolExecutionResult:
        start = time.monotonic()
        timestamp = datetime.now(timezone.utc).isoformat()
        parent = _tool_span.get()
        span_id = uuid.uuid4().hex
        token = _tool_span.set((self.trace_id, span_id))
        result = None
        try:
            if name not in {tool["name"] for tool in self.tools}:
                raise ToolFailure(f"Tool not available in this run: {name}")
            is_web_tool = name in {"web_search", "web_fetch"}
            if is_web_tool:
                inp = normalize_web_args(name, inp)
            if self.tool_mode == "replay":
                result = replay_tool_snapshot(self._replay_snapshot, name, inp)
            elif is_web_tool:
                result = await self._web_cache.execute(name, inp, self._dispatch_tool)
                if name == "web_search" and result.ok:
                    result = await asyncio.to_thread(annotate_search_results, result, self._candidate_ledger)
            else:
                raw = await self._dispatch_tool(name, inp)
                result = raw if isinstance(raw, ToolExecutionResult) else ToolExecutionResult(raw)
        except asyncio.CancelledError:
            result = ToolExecutionResult.failure("Tool execution cancelled.", "CancelledError")
            raise
        except ToolFailure as exc:
            result = ToolExecutionResult.failure(str(exc), type(exc).__name__)
        except Exception as exc:
            result = ToolExecutionResult.failure(f"Error executing tool: {exc}", type(exc).__name__)
        finally:
            _tool_span.reset(token)
            if result is not None:
                self._write_tool_trace(name, result, timestamp, start, span_id, parent)
        if self.tool_mode == "record":
            write_tool_snapshot(self.trace_id, span_id, name, inp, result)
        return result

    def _write_tool_trace(self, name: str, result: ToolExecutionResult, timestamp: str,
                          start: float, span_id: str, parent: tuple[str, str] | None) -> None:
        row = {
            "timestamp": timestamp, "session_id": self.session_id, "trace_id": self.trace_id, "span_id": span_id,
            "parent_span_id": parent[1] if parent else None, "agent_role": self.agent_role,
            "tool_name": name, "source_type": "mcp" if name.startswith("mcp__") else "builtin",
            "latency_ms": round((time.monotonic() - start) * 1000, 3),
            "output_chars": len(result.content), "cache_hit": result.meta.get("cache_hit", False), "ok": result.ok,
            "error_type": result.meta.get("error_type"),
        }
        try:
            directory = Path.home() / RESEARCH_DIRNAME / "traces"
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / f"{self.trace_id}.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row) + "\n")
        except OSError as exc:
            logging.warning("Cannot write tool trace: %s", exc)

    async def _dispatch_tool(self, name: str, inp: dict) -> str | ToolExecutionResult:
        if name == "web_search":
            return await fetch_search_results(**inp)
        if name == "update_candidate_status":
            row = await asyncio.to_thread(self._candidate_ledger.update, **inp)
            return json.dumps(row, ensure_ascii=False)
        if name == "list_candidates":
            rows = await asyncio.to_thread(self._candidate_ledger.list_candidates, **inp)
            return json.dumps(rows, ensure_ascii=False)
        if name == "compact_context":
            return await self._execute_compact_context_tool(inp)
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await self._execute_plan_mode_tool(name)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name == "skill":
            return await self._execute_skill_tool(inp)
        if self._mcp_manager.is_mcp_tool(name):
            result = await self._mcp_manager.call_tool(name, inp)
            if name.split("__", 2)[-1].startswith("browser_"):
                return replace(result, content=external_content(name, result.content))
            return result
        result = await execute_tool(
            name, inp, self._read_file_state, available_tools=self.tools, activated_tools=self._activated_tools,
        )
        if name == "read_file" and ".playwright-mcp" in Path(inp["file_path"]).parts:
            return external_content(inp["file_path"], result)
        if name in {"skill_create", "skill_evolve"}:
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and parsed.get("ok"):
                    self._refresh_runtime_system_prompt()
            except Exception:
                pass
        return result

    async def _execute_compact_context_tool(self, inp: dict) -> ToolExecutionResult:
        if not self.enable_folding:
            raise ToolFailure("Context folding is disabled for this run. Continue without compacting.")
        if self._fold_limit_reached():
            raise ToolFailure(f"Context folding limit reached ({self.max_fold_limit}). Continue without compacting.")
        reason = str(inp.get("reason") or "").strip()
        compacted = await self._compact_conversation(trigger="tool")
        if not compacted:
            raise ToolFailure("No context compaction was performed because there is not enough conversation history yet.")
        suffix = f"\nReason: {reason}" if reason else ""
        return ToolExecutionResult(
            "Context compacted into structured session memory. "
            "Continue from the folded memory now present in the conversation context."
            f"{suffix}", meta={"compacted": True},
        )


    async def _execute_skill_tool(self, inp: dict) -> str:
        from .skills import execute_skill
        result = execute_skill(inp.get("skill_name", ""), inp.get("args", ""))

        if not result:
            raise ToolFailure(f"Unknown skill: {inp.get('skill_name', '')}")

        #fork 表示这个 skill 不直接把 prompt 塞回当前对话，而是要启动一个子 Agent 单独完成任务。
        if result["context"] == "fork":
            budget = self._check_budget()
            if budget["exceeded"]:
                raise ToolFailure(f"Skill fork skipped: {budget['reason']}")
            # result["allowed_tools"] - 直接访问
            tools = (
                [t for t in self.tools if t["name"] in  result["allowed_tools"] ]
                #result.get("allowed_tools") - 安全访问
                # 存在key：返回对应的值（可能是 None、[]、["tool1"] 等）
                # 不存在key：返回 None（不会抛异常）
                if result.get("allowed_tools")
                else  [t for t in self.tools if t["name"] != "agent"]
            )

            tools = [t for t in tools if t["name"] not in {"enter_plan_mode", "exit_plan_mode"}]
            print_sub_agent_start("skill-fork", inp.get("skill_name", ""))
            sub_agent = Agent(
                model=self.model,
                **self._sub_agent_model_options(),
                api_base=self._api_base,
                anthropic_base_url=self._anthropic_base_url,
                api_key=self._api_key,
                custom_system_prompt=result["prompt"],
                custom_tools=tools,
                is_sub_agent=True,
                agent_role="skill-fork",
                permitted_mcp_tools=result.get("allowed_tools"),
                web_cache=self._web_cache,
                confirm_fn=self.confirm_fn,
                permission_mode=self.permission_mode,
                max_cost_usd=(max(0.0, self.max_cost_usd - self._get_current_cost_usd())
                              if self.max_cost_usd is not None else None),
                max_turns=(max(0, self.max_turns - self.current_turns)
                           if self.max_turns is not None else None),
                enable_folding=self.enable_folding,
                max_fold_limit=self.max_fold_limit,
                fold_memory_mode=self.fold_memory_mode,
                auto_compact_threshold=self.auto_compact_threshold,
                enable_memory_prefetch=self.enable_memory_prefetch,
                enable_mcp=self.enable_mcp,
                enable_online_evolution=self.enable_online_evolution,
            )
            try:
                sub_result = await sub_agent.run_once(inp.get("args") or "Execute this skill task.")
                self.total_input_tokens += sub_result["tokens"]["input"]
                self.total_output_tokens += sub_result["tokens"]["output"]
                self._merge_sub_agent_usage(sub_result)
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                return sub_result["text"] or "(Skill produced no output)"
            except Exception as e:
                print_sub_agent_end("skill-fork", inp.get("skill_name", ""))
                raise ToolFailure(f"Skill fork error: {e}")

        return f'[Skill "{inp.get("skill_name", "")}" activated]\n\n{result["prompt"]}'

    async def _execute_plan_mode_tool(self, name):
        if name == "enter_plan_mode":
            if self.permission_mode == "plan":
                return "Already in plan mode."
            self._pre_plan_mode = self.permission_mode
            self.permission_mode = "plan"
            self._plan_file_path =  self._generate_plan_file_path()
            self._system_prompt = self._base_system_prompt + self._build_plan_mode_prompt()
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            print_info("Entered plan mode (read-only). Plan file: " + self._plan_file_path)
            return f"Entered plan mode. You are now in read-only mode.\n\nYour plan file: {self._plan_file_path}\nWrite your plan to this file. This is the only file you can edit.\n\nWhen your plan is complete, call exit_plan_mode."
        if name == "exit_plan_mode":
            if self.permission_mode != "plan":
                raise ToolFailure("Not in plan mode.")
            plan_content = "(No plan file found)"
            if self._plan_file_path and Path(self._plan_file_path).exists():
                try:
                    plan_content = Path(self._plan_file_path).read_text(encoding="utf-8")
                except Exception as exc:
                    plan_content = f"(Unable to read plan file: {exc})\n\nPlan file: {self._plan_file_path}"
            # 交互式审批流程（如果有审批函数）
            if self._plan_approval_fn:
                result = await self._plan_approval_fn(plan_content)
                choice = result.get("choice", "manual-execute")

                if choice =="keep-planning":
                    feedback = result.get("feedback") or "Please revise the plan."
                    return (
                        f"User rejected the plan and wants to keep planning.\n\n"
                        f"User feedback: {feedback}\n\n"
                        f"Please revise your plan based on this feedback. When done, call exit_plan_mode again."
                    )

                if choice == "plan-only":
                    self._aborted = True
                    return (
                        f"Plan completed. One-shot plan mode does not execute changes.\n\n"
                        f"Plan file: {self._plan_file_path}\n\n"
                        f"## Plan:\n{plan_content}"
                    )

                if choice == "clear-and-execute":
                    target_mode = "acceptEdits"
                elif choice == "execute":
                    target_mode = "acceptEdits"
                else:  # manual-execute
                    target_mode = self._pre_plan_mode or "default"

                #离开计划模式
                self.permission_mode = target_mode
                self._pre_plan_mode = None
                saved_plan_path = self._plan_file_path
                self._plan_file_path = None
                self._system_prompt = self._base_system_prompt
                self._refresh_runtime_system_prompt()
                if self.use_openai and self._openai_messages:
                    self._openai_messages[0]["content"] = self._system_prompt

                if choice == "clear-and-execute":
                    self._clear_history_keep_system()
                    self._context_cleared = True
                    print_info(f"Plan approved. Context cleared, executing in {target_mode} mode.")
                    return (
                        f"User approved the plan. Context was cleared. Permission mode: {target_mode}\n\n"
                        f"Plan file: {saved_plan_path}\n\n"
                        f"## Approved Plan:\n{plan_content}\n\n"
                        f"Proceed with implementation."
                    )
                print_info(f"Plan approved. Executing in {target_mode} mode.")
                return (
                    f"User approved the plan. Permission mode: {target_mode}\n\n"
                    f"## Approved Plan:\n{plan_content}\n\n"
                    f"Proceed with implementation."
                )
            # 没有审批函数时的回退（例如子代理）
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            self._refresh_runtime_system_prompt()
            if self.use_openai and self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt

            print_info("Exited plan mode. Restored to " + self.permission_mode + " mode.")
            return f"Exited plan mode. Permission mode restored to: {self.permission_mode}\n\n## Your Plan:\n{plan_content}"

        raise ToolFailure(f"Unknown plan mode tool: {name}")

    def _clear_history_keep_system(self) -> None:
        """清空历史信息，但是保留系统prompt."""
        self._anthropic_messages = []
        self._openai_messages = []
        if self.use_openai:
            self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self._reset_context_budget()
        self._fold_last_time = 0.0
        self._fold_count = 0
        self._tool_error_streak = 0
        self._same_tool_repeat_count = 0
        self._last_tool_name = ""

    async def _execute_agent_tool(self, inp:dict) -> str:
        budget = self._check_budget()
        if budget["exceeded"]:
            raise ToolFailure(f"Sub-agent skipped: {budget['reason']}")
        agent_type = inp.get("type", "general")
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")
        print_sub_agent_start(agent_type, description)

        config = get_sub_agent_config(agent_type)

        sub_agent = Agent(
            model=self.model,
            **self._sub_agent_model_options(),
            api_base=self._api_base,
            anthropic_base_url=self._anthropic_base_url,
            api_key=self._api_key,
            custom_system_prompt=config["system_prompt"],
            custom_tools=[t for t in config["tools"] if t["name"] not in {"enter_plan_mode", "exit_plan_mode"}],
            is_sub_agent=True,
            agent_role=agent_type,
            permitted_mcp_tools=config.get("allowed_tools"),
            web_cache=self._web_cache,
            confirm_fn=self.confirm_fn,
            permission_mode=self.permission_mode,
            max_cost_usd=(max(0.0, self.max_cost_usd - self._get_current_cost_usd())
                          if self.max_cost_usd is not None else None),
            max_turns=(max(0, self.max_turns - self.current_turns)
                       if self.max_turns is not None else None),
            enable_folding=self.enable_folding,
            max_fold_limit=self.max_fold_limit,
            fold_memory_mode=self.fold_memory_mode,
            auto_compact_threshold=self.auto_compact_threshold,
            enable_memory_prefetch=self.enable_memory_prefetch,
            enable_mcp=self.enable_mcp,
            enable_online_evolution=self.enable_online_evolution,
        )
        try:
            result = await sub_agent.run_once(prompt)
            self.total_input_tokens += result["tokens"]["input"]
            self.total_output_tokens += result["tokens"]["output"]
            self._merge_sub_agent_usage(result)
            print_sub_agent_end(agent_type, description)
            return result["text"] or "(Sub-agent produced no output)"
        except Exception as e:
            print_sub_agent_end(agent_type, description)
            raise ToolFailure(f"Sub-agent error: {e}")

#--------------Anthropic 后端---------------
    async def  _chat_anthropic(self, user_message: str) -> None:
        self._anthropic_messages = self._normalize_anthropic_messages(_sanitize_for_utf8(self._anthropic_messages))
        user_message = _safe_utf8_text(user_message)
        # 先把本轮用户输入放入 Anthropic 消息历史，后续每轮模型调用都会带上这段上下文。
        self._anthropic_messages.append({"role": "user", "content": user_message})

        # 异步内存预取：主 agent 才需要查 memory，sub agent 不额外注入记忆。
        # 这里只启动后台任务，不阻塞当前模型调用流程。
        memory_prefetch:MemoryPrefetch | None = None
        if not self.is_sub_agent and self.enable_memory_prefetch:
            sq = self._build_side_query()
            if sq:
                memory_prefetch = start_memory_prefetch(
                    user_message, sq,
                    self._already_surfaced_memories, self._session_memory_bytes,
                )
        while True:
            # 外部请求中止时，结束整个 agent loop。
            if self._aborted:
                break

            # 每轮调用模型前尝试压缩上下文，避免消息历史过长。
            self._run_compression_pipeline()

            # 如果记忆预取任务已经完成，就把取回来的 memory 内容追加到最后一条用户消息里。
            # consumed 用来保证同一批 memory 只注入一次。
            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                memory_prefetch.consumed = True
                try:
                    memories = memory_prefetch.task.result()
                    if memories:
                        injection_text = format_memories_for_injection(memories)
                        injection_text = _safe_utf8_text(injection_text)
                        last = self._anthropic_messages[-1] if self._anthropic_messages else None
                        if last and last.get("role") == "user":
                            content = last.get("content", "")
                            if isinstance(content, str):
                                # 字符串不可变，需要重新赋值回 message。
                                last["content"] = content + "\n\n" + injection_text
                            elif isinstance(content, list):
                                # list 是可变对象，append 会直接修改 last["content"] 指向的列表。
                                content.append({"type": "text", "text": injection_text})
                        else:
                            # 如果最后一条不是 user message，就单独追加一条用户消息承载 memory。
                            self._anthropic_messages.append({"role": "user", "content": injection_text})

                        for m in memories:
                            # 记录本 session 已经注入过的 memory，后续检索时可避免重复 surfaced。
                            self._already_surfaced_memories.add(m.path)
                            self._session_memory_bytes += m.size
                except:
                    # memory 注入失败不应该中断主对话流程。
                    pass

            if not self.is_sub_agent:
                start_spinner()


            # 等完整响应成功后才执行工具。流式请求可能重试，提前执行会让同一工具产生重复副作用。
            await self._ensure_context_budget()
            response = await self._call_anthropic_stream()
            self.last_finish_reason = response.stop_reason
            raw_blocks = getattr(response, "raw_content", response.content)
            self._event_log.record(self.trace_id, "assistant_message", {
                "message": {"role": "assistant", "content": [self._raw_anthropic_block(b) for b in raw_blocks]},
                "stop_reason": response.stop_reason,
            })
            if not self.is_sub_agent:
                stop_spinner()

            # 记录本次模型调用的耗时点和 token 消耗，用于成本展示与预算控制。
            self.last_api_call_time = time.time()
            self.total_input_tokens += response.usage.input_tokens
            self.total_output_tokens += response.usage.output_tokens
            self._record_context_usage(
                response.usage.input_tokens + (getattr(response.usage, "cache_read_input_tokens", 0) or 0)
                + (getattr(response.usage, "cache_creation_input_tokens", 0) or 0)
            )

            # Anthropic 的响应内容里可能混有 text block 和 tool_use block，这里只挑出工具调用。
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            seen_ids: set[str] = set()
            for tu in tool_uses:
                self._validate_tool_call_identity(getattr(tu, "id", None), getattr(tu, "name", None), seen_ids)
            prepared = {}
            for tu in tool_uses:
                try:
                    inp = validate_tool_arguments(tu.name, tu.input, self.tools,
                                                  encoded=getattr(tu, "input_is_json", False))
                    prepared[tu.id] = (inp, None)
                except ValueError as exc:
                    prepared[tu.id] = (None, f"Error: Invalid arguments for {tu.name}: {exc}")

            blocks = []
            for block in response.content:
                serialized = self._block_to_dict(block)
                if block.type == "tool_use":
                    inp, error = prepared[block.id]
                    # Messages requires an object in tool_use.input even for bad JSON.
                    # Preserve rejected input as diagnostic data, never as executable arguments.
                    serialized["input"] = inp if error is None else (
                        block.input if isinstance(block.input, dict)
                        else {"_invalid_tool_arguments": block.input}
                    )
                blocks.append(serialized)

            # 把模型返回的所有 content block 写入消息历史，后续 tool_result 要与这些 tool_use 对应。
            self._anthropic_messages.append({
                "role": "assistant",
                "content": blocks,
            })

            # 没有工具调用，说明模型已经给出最终回复，本轮对话结束。
            if not tool_uses:
                if not self.is_sub_agent:
                    print_cost(self.total_input_tokens, self.total_output_tokens)
                break

            # 有工具调用时，进入下一轮工具执行。这里同时检查 turn/budget 限制。
            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                self._anthropic_messages.append({
                    "role": "user",
                    "content": [
                        self._anthropic_skipped_result(tu, f"Tool execution skipped: {budget['reason']}")
                        for tu in tool_uses
                    ],
                })
                break


            # 收集本轮所有工具结果，之后作为 tool_result 消息回传给模型。
            tool_results: list[dict] = []
            context_break = False
            try:
                context_break = await self._run_anthropic_tools(tool_uses, prepared, tool_results)
            finally:
                if not context_break:
                    # Serial execution leaves only a suffix outstanding, including
                    # a call interrupted during approval or process cleanup.
                    for pending in tool_uses[len(tool_results):]:
                        tool_results.append(self._anthropic_skipped_result(
                            pending, "Tool execution interrupted: no result available.",
                        ))
                    self._anthropic_messages.append({"role": "user", "content": tool_results})

            self._context_cleared = False

            self._refresh_runtime_system_prompt()

    async def _run_anthropic_tools(self, tool_uses, prepared, tool_results) -> bool:
        for tu in tool_uses:
            if self._aborted:
                break

            inp, error = prepared[tu.id]
            if error is not None:
                self._record_tool_outcome(tu.name, False)
                tool_results.append(self._anthropic_skipped_result(tu, error))
                continue
            print_tool_call(tu.name, inp)

            # 工具只在完整响应及权限检查后执行。
            perm = check_permission(tu.name, inp, self.permission_mode, self._plan_file_path)
            if perm["action"] == "deny":
                # 权限拒绝时，也要返回一个 tool_result，让模型知道该工具调用失败的原因。
                print_info(f"Denied: {perm.get('message', '')}")
                self._record_tool_outcome(tu.name, False)
                tool_results.append(self._anthropic_skipped_result(tu, f"Action denied: {perm.get('message', '')}"))
                continue

            if perm["action"] == "confirm":
                if not await self._confirm_tool_call(tu.name, perm):
                    self._record_tool_outcome(tu.name, False)
                    tool_results.append(self._anthropic_skipped_result(tu, "User denied this action."))
                    continue

            # 权限通过后执行工具，并把大输出持久化为可回传的摘要或引用。
            outcome = await self._execute_tool_call(tu.name, inp)
            res = self._persist_large_result(tu.name, outcome.content,
                                             call_id=tu.id, is_error=outcome.is_error)
            print_tool_result(tu.name, res)
            self._record_tool_outcome(tu.name, outcome.ok)

            if tu.name == "compact_context" and self.tool_mode != "replay" and outcome.meta.get("compacted"):
                return True

            if self._context_cleared:
                # 工具执行过程中如果清理了上下文，就把结果作为新的用户消息写入，
                # 并停止继续处理本轮剩余工具，避免旧上下文和新上下文混在一起。
                self._context_cleared = False
                self._anthropic_messages.append({"role": "user", "content": res})
                return True

            # Anthropic 要求 tool_result 使用 tool_use_id 对应到前面的 tool_use block。
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": res,
                                 "is_error": outcome.is_error})
        return False

    @staticmethod
    def _raw_anthropic_block(block) -> dict:
        if hasattr(block, "model_dump"):
            return block.model_dump(mode="json", exclude_none=True)
        # Streamed tool inputs can be malformed JSON; keep the exact input
        # used by the validator, before it builds the model-facing diagnostic.
        return {key: value for key, value in vars(block).items() if key != "input_is_json"}

    @staticmethod
    def _block_to_dict(block) -> dict:
        if block.type == "text":
            return {"type": "text", "text": _safe_utf8_text(block.text)}
        if block.type == "tool_use":
            raw_input = dict(block.input) if hasattr(block.input, 'items') else block.input
            return {"type": "tool_use", "id": _safe_utf8_text(block.id), "name": _safe_utf8_text(block.name), "input": _sanitize_for_utf8(raw_input)}
        # Fallback
        return {"type": _safe_utf8_text(block.type)}

    async def _call_anthropic_stream(self):

        async def _do():
            max_output = self.max_output_tokens or _get_max_output_tokens(self.model)

            # A new user input may directly follow a manually folded user summary or tool result.
            wire_messages = []
            for message in self._anthropic_messages:
                append_anthropic_message(wire_messages, message)
            create_params: dict[str, Any] = {
                "model": self.model,
                "max_tokens": max_output if self._thinking_mode != "disabled" else (self.max_output_tokens or 16384),
                "system": _safe_utf8_text(self._system_prompt),
                "tools": _sanitize_for_utf8(self._active_tool_definitions()),
                "messages": _sanitize_for_utf8(wire_messages),
            }
            #如果开启了思考模式，就给 Anthropic 请求加上 thinking 参数。
            if self._thinking_mode  in ("adaptive", "enabled"):
                create_params["thinking"]={"type": "enabled", "budget_tokens": max_output - 1}

            first_text = True

            # The SDK's messages.stream() parses partial tool JSON eagerly. Use raw
            # events so malformed arguments can become tool_result errors after the
            # complete response, without aborting other calls or accepting partial JSON.
            blocks: dict[int, Any] = {}
            open_blocks: set[int] = set()
            usage = SimpleNamespace(input_tokens=0, output_tokens=0)
            stop_reason = None
            complete = False
            stream = await self._anthropic_client.messages.create(**create_params, stream=True)
            async with stream:
                async for event in stream:
                    if event.type == "message_start":
                        usage = event.message.usage
                    elif event.type == "content_block_start":
                        cb = event.content_block
                        if cb.type == "tool_use":
                            cb = SimpleNamespace(type="tool_use", id=cb.id, name=cb.name,
                                                 input=cb.input, input_is_json=False)
                        blocks[event.index] = cb
                        open_blocks.add(event.index)
                    elif event.type == "content_block_delta":
                        delta = event.delta
                        block = blocks[event.index]
                        if hasattr(delta, "text"):
                            block.text += delta.text
                            if first_text:
                                stop_spinner()
                                await self._emit_text("\n")
                                first_text = False
                            await self._emit_text(delta.text)
                        elif hasattr(delta, "thinking"):
                            block.thinking = getattr(block, "thinking", "") + delta.thinking
                            if first_text:
                                stop_spinner()
                                await self._emit_text("\n  [thinking] ", thinking=True)
                                first_text = False
                            await self._emit_text(delta.thinking, thinking=True)
                        elif hasattr(delta, "signature"):
                            block.signature = getattr(block, "signature", "") + delta.signature
                        elif hasattr(delta, "partial_json") and block.type == "tool_use":
                            if not block.input_is_json:
                                block.input = ""
                                block.input_is_json = True
                            block.input += delta.partial_json
                    elif event.type == "content_block_stop":
                        open_blocks.remove(event.index)
                    elif event.type == "message_delta":
                        stop_reason = event.delta.stop_reason
                        usage.output_tokens = event.usage.output_tokens
                        if getattr(event.usage, "input_tokens", None) is not None:
                            usage.input_tokens = event.usage.input_tokens
                    elif event.type == "message_stop":
                        complete = True

            if not complete or open_blocks or stop_reason is None:
                raise ValueError("Incomplete Anthropic response; no tools executed.")
            return SimpleNamespace(
                content=[block for _, block in sorted(blocks.items()) if block.type != "thinking"],
                raw_content=[block for _, block in sorted(blocks.items())],
                usage=usage, stop_reason=stop_reason,
            )
#调用 _do()，如果遇到可重试错误，就由 _with_retry() 负责重试。
        return await _with_retry(_do)

    #openAI后端

    async def _chat_openai(self, user_message:str) -> None:
        user_message = _safe_utf8_text(user_message)
        self._openai_messages.append({"role": "user", "content": user_message})

        #预取句柄 MemoryPrefetch
        memory_prefetch: MemoryPrefetch | None = None
        if not self.is_sub_agent and self.enable_memory_prefetch:
            sq = self._build_side_query()
            if sq:
                memory_prefetch = start_memory_prefetch(
                    user_message, sq,
                    self._already_surfaced_memories, self._session_memory_bytes,
                )

        while True:
            if self._aborted:
                break

            self._run_compression_pipeline()

            if memory_prefetch and memory_prefetch.settled and not memory_prefetch.consumed:
                memory_prefetch.consumed = True
                try:
                    memories = memory_prefetch.task.result()
                    if memories:
                        injection_text = format_memories_for_injection(memories)
                        injection_text = _safe_utf8_text(injection_text)
                        last = self._openai_messages[-1] if self._openai_messages else None

                        if last and last.get("role") == "user":
                            last["content"] = (last.get("content") or "") + "\n\n" + injection_text
                        else:
                            self._openai_messages.append({"role": "user", "content": injection_text})

                        for m in memories:
                            self._already_surfaced_memories.add(m.path)
                            self._session_memory_bytes += len(m.content.encode())
                except Exception:
                    pass

            if not self.is_sub_agent:
                start_spinner()

            await self._ensure_context_budget()
            response = await self._call_openai_stream()

            if not self.is_sub_agent:
                stop_spinner()

            self.last_api_call_time = time.time()

            if response.get("usage"):
                self._record_openai_usage(response["usage"])

            choice = response.get("choices", [{}])[0] if response.get("choices") else {}
            self.last_finish_reason = choice.get("finish_reason")
            message = choice.get("message", {})

            self._event_log.record(self.trace_id, "assistant_message", {
                "message": message, "finish_reason": self.last_finish_reason,
            })

            # Keep DeepSeek protocol checks separate from recoverable argument errors.
            if self._deepseek:
                if self.last_finish_reason not in {"stop", "tool_calls"}:
                    raise ValueError("DeepSeek response incomplete or filtered; no tools executed. Increase --max-output-tokens or lower reasoning effort.")
                self._validate_deepseek_history([message])
                if bool(message.get("tool_calls")) != (self.last_finish_reason == "tool_calls"):
                    raise ValueError("DeepSeek tool-call finish reason does not match its message; no tools executed.")

            tool_calls = message.get("tool_calls")
            if tool_calls is not None and not isinstance(tool_calls, list):
                raise ValueError("Invalid tool_calls structure; no tools executed.")
            seen_ids: set[str] = set()
            for tc in tool_calls or []:
                if (not isinstance(tc, dict) or tc.get("type") != "function"
                        or not isinstance(tc.get("function"), dict)
                        or "arguments" not in tc["function"]):
                    raise ValueError("Incomplete function call structure; no tools executed.")
                self._validate_tool_call_identity(tc.get("id"), tc["function"].get("name"), seen_ids)

            self._openai_messages.append(message)

            tool_calls = message.get("tool_calls")

            if not tool_calls:
                if not self.is_sub_agent:
                    self._print_cost()
                break

            self.current_turns += 1
            budget = self._check_budget()
            if budget["exceeded"]:
                print_info(f"Budget exceeded: {budget['reason']}")
                self._append_openai_skipped_tool_results(tool_calls, f"Tool execution skipped: {budget['reason']}")
                break

            oai_checked: list[dict] = []
            for tc in tool_calls:
                if tc.get("type") != "function":
                    continue
                if self._aborted:
                    oai_checked.append({
                        "tc": tc,
                        "fn": tc.get("function", {}).get("name", ""),
                        "inp": {},
                        "allowed": False,
                        "result": "Tool execution skipped: request aborted.",
                    })
                    continue

                fn_name = tc["function"]["name"]
                try:
                    inp = validate_tool_arguments(fn_name, tc["function"]["arguments"], self.tools, encoded=True)
                except ValueError as exc:
                    self._record_tool_outcome(fn_name, False)
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": None, "allowed": False,
                                        "result": f"Error: Invalid arguments for {fn_name}: {exc}"})
                    continue

                print_tool_call(fn_name, inp)

                perm = check_permission(fn_name, inp, self.permission_mode, self._plan_file_path)

                if perm["action"] == "deny":
                    print_info(f"Denied: {perm.get('message', '')}")
                    self._record_tool_outcome(fn_name, False)
                    oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False,
                                        "result": f"Action denied: {perm.get('message', '')}"})
                    continue
                if perm["action"] == "confirm":
                    try:
                        approved = await self._confirm_tool_call(fn_name, perm)
                    except asyncio.CancelledError:
                        self._append_openai_skipped_tool_results(tool_calls, "Tool approval interrupted.")
                        raise
                    if not approved:
                        self._record_tool_outcome(fn_name, False)
                        oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": False,
                                            "result": "User denied this action."})
                        continue
                oai_checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})

            oai_batches: list[dict] = []
            for ct in oai_checked:
                safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
                if safe and oai_batches and oai_batches[-1]["concurrent"]:
                    oai_batches[-1]["items"].append(ct)
                else:
                    oai_batches.append({"concurrent": safe, "items": [ct]})

            oai_context_break = False
            try:
                for batch in oai_batches:
                    if oai_context_break or self._aborted:
                        break

                    if batch["concurrent"]:
                        async def _run_oai_safe(ct_item: dict) -> tuple[dict, str, bool]:
                            outcome = await self._execute_tool_call(ct_item["fn"], ct_item["inp"])
                            res = self._persist_large_result(ct_item["fn"], outcome.content,
                                                            call_id=ct_item["tc"]["id"], is_error=outcome.is_error)
                            print_tool_result(ct_item["fn"], res)
                            return ct_item, res, outcome.ok

                        tasks = [asyncio.create_task(_run_oai_safe(ct)) for ct in batch["items"]]
                        try:
                            await asyncio.gather(*tasks)
                        finally:
                            unfinished = [task for task in tasks if not task.done()]
                            for task in unfinished:
                                task.cancel()
                            try:
                                if unfinished:
                                    await asyncio.gather(*unfinished, return_exceptions=True)
                            finally:
                                # A sibling can be cancelled after a result was logged. Keep
                                # that real receipt before filling the remaining IDs as skipped.
                                for task in tasks:
                                    if task.done() and not task.cancelled() and task.exception() is None:
                                        ct_item, res, ok = task.result()
                                        self._record_tool_outcome(ct_item["fn"], ok)
                                        self._openai_messages.append(
                                            {"role": "tool", "tool_call_id": ct_item["tc"]["id"], "content": res})
                    else:
                        for ct in batch["items"]:
                            if not ct["allowed"]:
                                call_id = ct["tc"]["id"]
                                res = self._persist_large_result(ct["fn"], ct["result"], call_id=call_id, is_error=True)
                                self._openai_messages.append({"role": "tool", "tool_call_id": call_id, "content": res})
                                continue

                            outcome = await self._execute_tool_call(ct["fn"], ct["inp"])
                            res = self._persist_large_result(ct["fn"], outcome.content,
                                                            call_id=ct["tc"]["id"], is_error=outcome.is_error)
                            print_tool_result(ct["fn"], res)
                            self._record_tool_outcome(ct["fn"], outcome.ok)

                            if ct["fn"] == "compact_context" and self.tool_mode != "replay" and outcome.meta.get("compacted"):
                                oai_context_break = True
                                break

                            if self._context_cleared:
                                self._context_cleared = False
                                self._openai_messages.append({"role": "user", "content": res})
                                oai_context_break = True
                                break

                            self._openai_messages.append(
                                {"role": "tool", "tool_call_id": ct["tc"]["id"], "content": res})
            except asyncio.CancelledError:
                self._append_openai_skipped_tool_results(tool_calls, "Tool execution interrupted: no result available.")
                raise

            if self._aborted and not oai_context_break:
                self._append_openai_skipped_tool_results(tool_calls, "Tool execution interrupted: no result available.")

            self._context_cleared = False
            self._refresh_runtime_system_prompt()

    def _append_openai_skipped_tool_results(self, tool_calls: list[dict], reason: str) -> None:
        """Ensure every OpenAI tool_call gets a matching tool message."""
        existing_ids = set()
        for message in reversed(self._openai_messages):
            if message.get("role") != "tool":
                break
            existing_ids.add(message.get("tool_call_id"))
        for tc in tool_calls:
            if tc.get("type") != "function":
                continue
            tool_call_id = tc.get("id")
            if tool_call_id and tool_call_id not in existing_ids:
                content = self._persist_large_result(tc["function"]["name"], reason, call_id=tool_call_id, is_error=True)
                self._openai_messages.append(
                    {"role": "tool", "tool_call_id": tool_call_id, "content": content}
                )
                existing_ids.add(tool_call_id)

    async def _call_openai_stream(self) -> dict:
        async def _do():
            self._validate_deepseek_history(self._openai_messages)
            create_params = dict(
                model=self.model,
                tools=_sanitize_for_utf8(_to_openai_tools(self._active_tool_definitions())),
                messages=_sanitize_for_utf8(self._openai_messages),
                stream=True,
                stream_options={"include_usage": True},
                **self._openai_request_options,
            )
            if self.max_output_tokens is not None:
                create_params["max_tokens"] = self.max_output_tokens
            if self._deepseek and not create_params["tools"]:
                create_params.pop("tools")
            stream = await self._openai_client.chat.completions.create(**create_params)

            content = ""
            reasoning_content = None
            first_text = True
            tool_calls: dict[int, dict] = {}
            finish_reason = ""
            usage = None

            try:
                async for chunk in stream:
                    if chunk.usage:
                        usage = normalize_usage(chunk.usage)

                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    if self._deepseek and delta is not None:
                        fragment = getattr(delta, "reasoning_content", None)
                        if fragment is not None:
                            if reasoning_content is None:
                                reasoning_content = ""
                                if fragment and not self.is_sub_agent:
                                    print_info("DeepSeek thinking...")
                            # 保存协议字段，不混入最终回答、子 Agent 输出或 Skills 提取文本。
                            reasoning_content += _safe_utf8_text(fragment)

                    if delta and delta.content:
                        if first_text:
                            stop_spinner()
                            await self._emit_text("\n")
                            first_text = False
                        await self._emit_text(delta.content)
                        content += _safe_utf8_text(delta.content)

                    if delta and delta.tool_calls:
                        for tc in delta.tool_calls:
                            existing = tool_calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                            if tc.id:
                                existing["id"] += _safe_utf8_text(tc.id)
                            if tc.function:
                                if tc.function.name:
                                    existing["name"] += _safe_utf8_text(tc.function.name)
                                if tc.function.arguments:
                                    existing["arguments"] += _safe_utf8_text(tc.function.arguments)

                    if chunk.choices[0].finish_reason:
                        finish_reason = chunk.choices[0].finish_reason
            finally:
                close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
                if close:
                    await close()

            assembled = None
            if tool_calls:
                assembled = [
                    {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                    for _, tc in sorted(tool_calls.items())
                ]

            message = {"role": "assistant", "content": content or None, "tool_calls": assembled}
            if reasoning_content is not None:
                message["reasoning_content"] = reasoning_content
            return {
                "choices": [{
                    "message": message,
                    "finish_reason": finish_reason if self._deepseek else (finish_reason or "stop"),
                }],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
            }

        return await _with_retry(_do)

    async def _confirm_tool_call(self, tool_name: str, permission: dict) -> bool:
        message = permission.get("message", "")
        # Repeated shell and file changes need fresh approval, even with identical arguments.
        always_confirm = tool_name in {"run_shell", "write_file", "edit_file"}
        if not always_confirm and message in self._confirmed_paths:
            return True
        if not await self._confirm_dangerous(message):
            return False
        if not always_confirm:
            self._confirmed_paths.add(message)
        return True

    async def _confirm_dangerous(self, command: str) -> bool:
        print_confirmation(command)
        if self.confirm_fn:
            return await self.confirm_fn(command)
        # Fallback: blocking input
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False
