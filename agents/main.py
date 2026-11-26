"""CLI entry point and interactive REPL — mirrors cli.ts."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values, find_dotenv, load_dotenv

from .paths import use_workspace, workspace_dir

from .agent import Agent
from .ui import (
    print_welcome,
    print_user_prompt,
    print_error,
    print_info,
    print_plan_for_approval,
    print_plan_approval_options,
    print_goodbye,
    print_interrupted,
    print_memory_entries,
    print_skill_entries,
    print_warning,
)
from .session import load_session, get_latest_session_id
from .memory import list_memories
from .skills import (
    create_skill,
    discover_skills,
    evolve_skill,
    execute_skill,
    get_skill_by_name,
    record_feedback,
    skill_stats,
)
from .online_skill_eval import format_online_skill_eval_async


def parse_args(prog: str = "python -m agents.main") -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="ResearchAgent — an evolvable research assistant CLI",
        add_help=False,
    )
    parser.add_argument("prompt", nargs="*", help="One-shot prompt")
    parser.add_argument("--yolo", "-y", action="store_true", help="Skip all confirmation prompts")
    parser.add_argument("--plan", action="store_true", help="Plan mode: read-only")
    parser.add_argument("--accept-edits", action="store_true", help="Auto-approve file edits")
    parser.add_argument("--dont-ask", action="store_true", help="Auto-deny confirmations (for CI)")
    thinking = parser.add_mutually_exclusive_group()
    thinking.add_argument("--thinking", dest="thinking", action="store_true", help="Enable thinking")
    thinking.add_argument("--no-thinking", dest="thinking", action="store_false", help="Disable thinking")
    parser.set_defaults(thinking=None)
    parser.add_argument("--reasoning-effort", choices=("low", "high", "max"), default=None,
                        help="DeepSeek V4 Pro thinking effort")
    parser.add_argument("--max-output-tokens", type=int, default=None, help="Output budget including reasoning tokens")
    parser.add_argument("--model", "-m", default=None, help="Model to use")
    parser.add_argument("--api-base", default=None, help="OpenAI-compatible API base URL")
    parser.add_argument("--resume", nargs="?", const="", metavar="SESSION_ID",
                        help="Resume a session by ID, or the latest for this project and protocol")
    parser.add_argument("--max-cost", type=float, default=None, help="Max USD spend")
    parser.add_argument("--max-turns", type=int, default=None, help="Max agentic turns")
    parser.add_argument("--no-folding", action="store_true", help="Disable full session folding")
    parser.add_argument("--fold-threshold", type=float, default=0.70, help="Auto-fold at this fraction of the context window")
    parser.add_argument("--help", "-h", action="store_true", help="Show help")
    parser.add_argument("-p", "--print", dest="print_once", action="store_true",
                        help="Run the given task once and exit")
    args = parser.parse_args()
    if args.print_once and not " ".join(args.prompt).strip():
        parser.error("--print requires a task")
    return args


def _resolve_permission_mode(args: argparse.Namespace) -> str:
    if args.yolo:
        return "bypassPermissions"
    if args.plan:
        return "plan"
    if args.accept_edits:
        return "acceptEdits"
    if args.dont_ask:
        return "dontAsk"
    return "default"


def _clean_env(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _load_env_file() -> None:
    env_path = find_dotenv(usecwd=True)
    if env_path:
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)


def _load_cli_env() -> None:
    # An alias in a lower-priority file must not override an exported credential.
    alias_groups = (
        {"APIKEY", "MINI_CLAUDE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"},
        {"API", "MINI_CLAUDE_API_BASE", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"},
    )
    for path in (workspace_dir() / ".env", Path.home() / ".researchagent" / ".env"):
        if not path.is_file():
            continue
        shadowed = set().union(*(group for group in alias_groups
                                 if any(_clean_env(os.getenv(key)) for key in group)))
        for key, value in dotenv_values(path).items():
            if value is not None and key not in shadowed:
                os.environ.setdefault(key, value)


def _is_anthropic_compatible_base_url(base_url: str | None) -> bool:
    if not base_url:
        return False
    parsed = urlparse(base_url)
    path = (parsed.path or "").lower().rstrip("/")
    return path.endswith("/anthropic") or "/anthropic/" in path


def _resolve_api_config(cli_api_base: str | None) -> tuple[str | None, str | None, bool]:
    generic_api_key = _clean_env(os.environ.get("APIKEY")) or _clean_env(os.environ.get("MINI_CLAUDE_API_KEY"))
    openai_api_key = _clean_env(os.environ.get("OPENAI_API_KEY"))
    anthropic_api_key = _clean_env(os.environ.get("ANTHROPIC_API_KEY"))

    generic_api_base = _clean_env(os.environ.get("API")) or _clean_env(os.environ.get("MINI_CLAUDE_API_BASE"))
    openai_api_base = _clean_env(os.environ.get("OPENAI_BASE_URL"))
    anthropic_api_base = _clean_env(os.environ.get("ANTHROPIC_BASE_URL"))

    resolved_api_base = _clean_env(cli_api_base) or generic_api_base or openai_api_base or anthropic_api_base

    if resolved_api_base:
        if _is_anthropic_compatible_base_url(resolved_api_base):
            return resolved_api_base, generic_api_key or anthropic_api_key or openai_api_key, False
        return resolved_api_base, generic_api_key or openai_api_key or anthropic_api_key, True

    if anthropic_api_key or anthropic_api_base:
        return anthropic_api_base, generic_api_key or anthropic_api_key or openai_api_key, False

    if openai_api_key or openai_api_base:
        return openai_api_base or "https://api.openai.com/v1", generic_api_key or openai_api_key, True

    if generic_api_key:
        return None, generic_api_key, False

    return None, None, False


async def run_repl(agent: Agent, initial_prompt: str | None = None) -> None:
    # 交互入口也必须在事件循环关闭前回收 MCP；异常和 EOF 共用此路径。
    previous_sigint = signal.getsignal(signal.SIGINT)
    try:
        await _run_repl(agent, initial_prompt)
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        try:
            await agent.drain_background_skill_tasks()
        finally:
            await agent._mcp_manager.disconnect_all()


async def _run_repl(agent: Agent, initial_prompt: str | None = None) -> None:
    """Interactive REPL loop."""

    async def confirm_fn(message: str) -> bool:
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    agent.set_confirm_fn(confirm_fn)

    async def plan_approval_fn(plan_content: str) -> dict:
        print_plan_for_approval(plan_content)
        print_plan_approval_options()
        while True:
            try:
                choice = input("  Enter choice (1-4): ").strip()
            except EOFError:
                return {"choice": "manual-execute"}
            if choice == "1":
                return {"choice": "clear-and-execute"}
            elif choice == "2":
                return {"choice": "execute"}
            elif choice == "3":
                return {"choice": "manual-execute"}
            elif choice == "4":
                try:
                    feedback = input("  Feedback (what to change): ").strip()
                except EOFError:
                    feedback = ""
                return {"choice": "keep-planning", "feedback": feedback or None}
            else:
                print_warning("Invalid choice. Enter 1, 2, 3, or 4.")

    agent.set_plan_approval_fn(plan_approval_fn)

    sigint_count = 0

    def handle_sigint(sig, frame):
        nonlocal sigint_count
        if agent._aborted is False and agent._output_buffer is not None:
            # Agent is processing
            agent.abort()
            print_interrupted()
            sigint_count = 0
            print_user_prompt()
        else:
            sigint_count += 1
            if sigint_count >= 2:
                # 让 input 的退出处理接管，随后执行异步清理。
                raise KeyboardInterrupt
            print_warning("Press Ctrl+C again to exit.")
            print_user_prompt()

    signal.signal(signal.SIGINT, handle_sigint)
    print_welcome()
    if initial_prompt:
        try:
            await agent.chat(initial_prompt)
        except Exception as exc:
            print_error(str(exc))

    while True:
        print_user_prompt()
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print_goodbye()
            break

        inp = line.strip()
        sigint_count = 0

        if not inp:
            continue
        if inp in ("exit", "quit"):
            print_goodbye()
            break

        # REPL commands
        if inp == "/clear":
            agent.clear_history()
            continue
        if inp == "/plan":
            agent.toggle_plan_mode()
            continue
        if inp == "/cost":
            agent.show_cost()
            continue
        if inp == "/compact":
            try:
                await agent.compact()
            except Exception as e:
                print_error(str(e))
            continue
        if inp == "/memory":
            memories = list_memories()
            if not memories:
                print_info("No memories saved yet.")
            else:
                print_memory_entries(memories)
            continue
        if inp == "/skills":
            skills = discover_skills()
            if not skills:
                print_info("No skills found. Add skills to .research/skills/<name>/SKILL.md")
            else:
                print_skill_entries(skills)
            continue
        if inp == "/skill-stats":
            print_info(skill_stats())
            continue
        if inp == "/skill-eval":
            side_query = agent._build_side_query(max_tokens=2400)
            print_info(await format_online_skill_eval_async(side_query=side_query))
            continue
        if inp.startswith("/extract_now"):
            hint = inp[len("/extract_now") :].strip()
            result = await agent.extract_now(hint)
            if result.get("ok"):
                print_info("Ran online skill extraction for the current pending window.")
            else:
                print_error(str(result.get("error") or result))
            continue
        if inp.startswith("/skill-feedback "):
            _, rest = inp.split(" ", 1)
            parts = rest.strip().split(" ", 2)
            if len(parts) < 2:
                print_error("Usage: /skill-feedback <skill-name> <rating> [note]")
                continue
            note = parts[2] if len(parts) > 2 else ""
            record_feedback(parts[0], parts[1], note)
            print_info(f"Recorded feedback for skill: {parts[0]}")
            continue
        if inp.startswith("/skill-evolve "):
            _, rest = inp.split(" ", 1)
            parts = rest.strip().split(" ", 1)
            if len(parts) < 2:
                print_error("Usage: /skill-evolve <skill-name> <durable lesson>")
                continue
            result = evolve_skill(parts[0], parts[1], rationale="Manual REPL evolution", target="active")
            if result.get("ok"):
                print_info(f"Evolved skill {result.get('skill')} to version {result.get('version')}")
            else:
                print_error(str(result.get("error") or result))
            continue
        if inp.startswith("/skill-create "):
            _, rest = inp.split(" ", 1)
            parts = [part.strip() for part in rest.split("|", 3)]
            if len(parts) < 4 or not all(parts[:4]):
                print_error("Usage: /skill-create <name> | <description> | <when-to-use> | <instructions>")
                continue
            result = create_skill(
                name=parts[0],
                description=parts[1],
                when_to_use=parts[2],
                instructions=parts[3],
                target="project",
                context="inline",
                user_invocable=False,
                evidence="Manual REPL skill creation",
            )
            if result.get("ok"):
                print_info(f"Created skill {result.get('skill')} at {result.get('file')}")
            else:
                print_error(str(result.get("error") or result))
            continue

        # Skill invocation: /<skill-name> [args]
        if inp.startswith("/"):
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
            skill = get_skill_by_name(cmd_name)
            if skill and skill.user_invocable:
                print_info(f"Invoking skill: {skill.name}")
                try:
                    if skill.context == "fork":
                        await agent.chat(f'Use the skill tool to invoke "{skill.name}" with args: {cmd_args or "(none)"}')
                    else:
                        result = execute_skill(skill.name, cmd_args)
                        if not result:
                            print_error(f"Unknown skill: {skill.name}")
                            continue
                        await agent.chat(result["prompt"])
                except Exception as e:
                    if "abort" not in str(e).lower():
                        print_error(str(e))
                continue

        # Normal chat
        try:
            await agent.chat(inp)
        except Exception as e:
            if "abort" not in str(e).lower():
                print_error(str(e))


async def run_one_shot(agent: Agent, prompt: str) -> None:
    if agent.permission_mode == "plan":
        async def plan_only_approval_fn(plan_content: str) -> dict:
            print_plan_for_approval(plan_content)
            return {"choice": "plan-only"}

        agent.set_plan_approval_fn(plan_only_approval_fn)

    try:
        await agent.chat(prompt)
        await agent.drain_background_skill_tasks()
    finally:
        await agent._mcp_manager.disconnect_all()


def cli_main() -> None:
    with use_workspace(Path.cwd()):
        _run_cli(installed=True)


def main() -> None:
    _run_cli(installed=False)


def _run_cli(*, installed: bool) -> None:
    """CLI 程序入口：准备运行配置，创建 Agent，并按参数选择一次性执行或交互模式。"""
    # 解析命令行参数，例如 --plan、--resume、--model，以及可选的一次性 prompt。
    # 看看用户传入了哪些参数，再加载环境配置
    args = parse_args("researchagent" if installed else "python -m agents.main")
    if installed:
        _load_cli_env()
    else:
        _load_env_file()

    if args.help:
        # 自定义帮助文本，展示 ResearchAgent 支持的启动参数和 REPL 内置命令。
        print("""
ResearchAgent — an evolvable research assistant CLI

Usage: researchagent [options] [initial task]
       researchagent -p TASK
       python -m agents.main [options] [one-shot task]

Options:
  --print, -p         Execute one task and exit (requires task text)
  --yolo, -y          Skip all confirmation prompts (bypassPermissions mode)
  --plan              Plan mode: read-only, describe changes without executing
  --accept-edits      Auto-approve file edits, still confirm every shell command
  --dont-ask          Auto-deny anything needing confirmation (for CI)
  --thinking          Enable thinking (supported Claude / official DeepSeek V4 Pro)
  --no-thinking       Disable thinking, overriding environment configuration
  --reasoning-effort  DeepSeek V4 Pro effort: low / high / max
  --max-output-tokens Output budget, including reasoning tokens
  --model, -m         Model to use (default: deepseek-chat, or MODEL env)
  --api-base URL      Override API base URL from CLI or .env
  --resume [ID]       Resume by ID, or the latest session for this project and protocol
  --max-cost USD      Stop when estimated cost exceeds this amount
  --max-turns N       Stop after N agentic turns
  --no-folding        Disable full session folding
  --fold-threshold X  Auto-fold threshold (0 < X < 1, default: 0.70)
  --help, -h          Show this help

REPL commands:
  /clear              Clear conversation history
  /plan               Toggle plan mode (read-only <-> normal)
  /cost               Show token usage and cost
  /compact            Manually compact conversation
  /memory             List saved memories
  /skills             List available skills
  /skill-stats        Show skill usage and evolution stats
  /skill-eval         Evaluate online skill evolution quality
  /extract_now        Extract the current pending online skill window: /extract_now [hint]
  /skill-feedback     Record feedback: /skill-feedback <skill> <rating> [note]
  /skill-evolve       Evolve a skill: /skill-evolve <skill> <durable lesson>
  /skill-create       Create a skill: /skill-create <name> | <description> | <when-to-use> | <instructions>
  /<skill-name>       Invoke a skill (e.g. /commit "fix types")

Examples:
  researchagent "inspect this project"  # stay in the conversation
  researchagent -p "summarize experiment logs"

  python -m agents.main "analyze this repository's experiment setup"
  python -m agents.main --plan "plan how to reproduce the main experiment"
  python -m agents.main --max-cost 0.50 --max-turns 20 "summarize the experiment logs"
  python -m agents.main --resume
  python -m agents.main  # starts interactive REPL
""")
        sys.exit(0)

    # 将命令行布尔开关统一转换成 Agent 内部使用的权限模式。
    permission_mode = _resolve_permission_mode(args)
    # 模型优先使用命令行参数，其次读取 .env 中的 MODEL，最后回落到默认模型。
    model = args.model or os.environ.get("MODEL") or "deepseek-chat"
    resolved_api_base, resolved_api_key, resolved_use_openai = _resolve_api_config(args.api_base)

    # 没有可用 API key 时无法调用模型，直接提示配置方式并退出。
    if not resolved_api_key:
        print_error(
            "API key is required.\n"
            "  Set APIKEY (+ optional API) in .env for generic config,\n"
            "  or use ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL,\n"
            "  or use OPENAI_API_KEY / OPENAI_BASE_URL."
        )
        sys.exit(1)

    # 创建主 Agent。OpenAI-compatible 和 Anthropic 原生接口使用不同的 base URL 参数名传入。
    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=args.thinking,
        reasoning_effort=args.reasoning_effort,
        max_output_tokens=args.max_output_tokens,
        max_cost_usd=args.max_cost,
        max_turns=args.max_turns,
        api_base=resolved_api_base if resolved_use_openai else None,
        anthropic_base_url=resolved_api_base if not resolved_use_openai else None,
        api_key=resolved_api_key,
        enable_folding=not args.no_folding,
        auto_compact_threshold=args.fold_threshold,
    )

    if args.resume is not None:
        protocol = "openai" if resolved_use_openai else "anthropic"
        session_id = args.resume or get_latest_session_id(protocol)
        if session_id is None:
            print_info("No previous sessions found for this project and protocol.")
        else:
            try:
                session = load_session(session_id)
                if session is None:
                    raise ValueError(f"Session {session_id} was not found or could not be read.")
                meta = session["metadata"]
                if not (meta.get("project_hash") and meta.get("protocol")):
                    if not args.resume:
                        raise ValueError("Legacy snapshot requires an explicit session ID.")
                    print_warning("Legacy snapshot: project or protocol metadata is incomplete.")
                agent.restore_session(session)
            except ValueError as exc:
                print_error(str(exc))
                sys.exit(1)

    # 如果命令行后面带了普通文本参数，就拼成一次性 prompt；否则进入交互式 REPL。
    prompt = " ".join(args.prompt) if args.prompt else None

    if prompt and (args.print_once or not installed):
        # One-shot mode
        # 一次性模式：执行完用户 prompt 后进程结束。
        try:
            asyncio.run(run_one_shot(agent, prompt))
        except Exception as e:
            print_error(str(e))
            sys.exit(1)
    else:
        # Interactive REPL
        # 交互模式：启动循环读取用户输入，直到用户退出。
        asyncio.run(run_repl(agent, prompt) if prompt else run_repl(agent))


if __name__ == "__main__":
    main()
