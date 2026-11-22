from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agents.agent import Agent


async def response_stream(content: str = "", tool_name: str | None = None, call_id: str = "call_1"):
    tool_calls = None
    if tool_name:
        tool_calls = [SimpleNamespace(
            index=0, id=call_id,
            function=SimpleNamespace(name=tool_name, arguments=json.dumps(
                {"file_path": "README.md"} if tool_name == "read_file" else {"pattern": "*", "path": "."}
            )),
        )]
    yield SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=content, tool_calls=tool_calls),
            finish_reason="tool_calls" if tool_name else "stop",
        )],
    )


class DeepSeekConfigTests(unittest.TestCase):
    def _agent(self, *, mode: str | None = None, **kwargs) -> Agent:
        env = {} if mode is None else {"RESEARCH_DEEPSEEK_THINKING": mode}
        options = dict(
            model="deepseek-v4-pro", api_base="https://api.deepseek.com",
            api_key="test-placeholder", custom_system_prompt="test system prompt",
            enable_memory_prefetch=False, enable_mcp=False, enable_online_evolution=False,
        )
        options.update(kwargs)
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock()),
        ))
        with (
            patch.dict(os.environ, env, clear=True),
            patch("agents.agent.openai.AsyncOpenAI", return_value=client),
            patch("agents.agent.anthropic.AsyncAnthropic", return_value=SimpleNamespace()),
        ):
            return Agent(**options)

    def test_official_endpoints_default_to_disabled_for_main_and_side_requests(self) -> None:
        for base in (
            "https://api.deepseek.com", "https://api.deepseek.com/",
            "https://api.deepseek.com/v1", "https://api.deepseek.com/v1/",
        ):
            with self.subTest(base=base):
                agent = self._agent(api_base=base)
                create = agent._openai_client.chat.completions.create
                create.return_value = response_stream("OK")
                with patch.object(agent, "_emit_text"), patch("agents.agent.stop_spinner"):
                    result = asyncio.run(agent._call_openai_stream())
                main_request = create.call_args.kwargs
                self.assertEqual(result["choices"][0]["message"]["content"], "OK")
                self.assertEqual(main_request["extra_body"], {"thinking": {"type": "disabled"}})
                self.assertTrue(main_request["stream"])

                create.return_value = SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))],
                    usage=SimpleNamespace(prompt_tokens=11, completion_tokens=2),
                )
                self.assertEqual(asyncio.run(agent._build_side_query(max_tokens=700)("system", "task")), "[]")
                self.assertEqual(create.call_args.kwargs["extra_body"], main_request["extra_body"])
                self.assertEqual(create.call_args.kwargs["max_tokens"], 700)
                self.assertEqual((agent.side_input_tokens, agent.side_output_tokens), (11, 2))

    def test_explicit_disabled_configuration_also_applies_to_sub_agents(self) -> None:
        for is_sub_agent in (False, True):
            with self.subTest(is_sub_agent=is_sub_agent):
                agent = self._agent(mode=" DISABLED ", is_sub_agent=is_sub_agent)
                create = agent._openai_client.chat.completions.create
                create.return_value = response_stream()
                asyncio.run(agent._call_openai_stream())
                self.assertEqual(create.call_args.kwargs["extra_body"], {"thinking": {"type": "disabled"}})

    def test_invalid_configuration_fails_before_sdk_client_creation(self) -> None:
        for env_name, value in (
            ("RESEARCH_DEEPSEEK_THINKING", "false"),
            ("RESEARCH_DEEPSEEK_THINKING", ""),
            ("RESEARCH_DEEPSEEK_REASONING_EFFORT", "medium"),
            ("RESEARCH_DEEPSEEK_SIDE_THINKING", "false"),
            ("RESEARCH_DEEPSEEK_SIDE_REASONING_EFFORT", "ultra"),
            ("RESEARCH_DEEPSEEK_PRICE_PERIOD", "unknown"),
        ):
            env = {env_name: value}
            with (
                self.subTest(env_name=env_name, value=value),
                patch.dict(os.environ, env, clear=True),
                patch("agents.agent.openai.AsyncOpenAI") as client_factory,
                patch("agents.agent.build_system_prompt") as prompt_builder,
            ):
                with self.assertRaisesRegex(ValueError, env_name):
                    Agent(model="deepseek-v4-pro", api_base="https://api.deepseek.com",
                          api_key="test-placeholder")
                client_factory.assert_not_called()
                prompt_builder.assert_not_called()

    def test_other_models_and_endpoints_do_not_receive_deepseek_parameters(self) -> None:
        for model, base in (
            ("gpt-4o", "https://api.openai.com/v1"),
            ("deepseek-chat", "https://api.deepseek.com"),
            ("deepseek-v4-pro", "https://gateway.example.test/v1"),
            ("deepseek-v4-pro", "https://api.deepseek.com.example.test/v1"),
            ("deepseek-v4-pro", "https://api.deepseek.com/anthropic"),
            ("deepseek-v4-pro", "https://api.deepseek.com/v1/other"),
            ("deepseek-v4-pro", "http://api.deepseek.com"),
        ):
            with self.subTest(model=model, base=base):
                agent = self._agent(mode="invalid-but-unrelated", model=model, api_base=base)
                create = agent._openai_client.chat.completions.create
                create.return_value = response_stream()
                asyncio.run(agent._call_openai_stream())
                self.assertNotIn("extra_body", create.call_args.kwargs)
                create.return_value = SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))], usage=None,
                )
                asyncio.run(agent._build_side_query()("system", "task"))
                self.assertNotIn("extra_body", create.call_args.kwargs)

    def test_anthropic_configuration_keeps_existing_thinking_behavior(self) -> None:
        agent = self._agent(
            mode="enabled", api_base=None, model="claude-sonnet-4-6",
            anthropic_base_url="https://api.anthropic.com", thinking=True,
        )
        self.assertFalse(agent.use_openai)
        self.assertEqual(agent._thinking_mode, "adaptive")
        deepseek = self._agent(
            mode="enabled", api_base=None, anthropic_base_url="https://api.deepseek.com/anthropic",
        )
        self.assertFalse(deepseek.use_openai)

    def test_two_consecutive_tool_calls_and_followup_keep_thinking_disabled(self) -> None:
        agent = self._agent()
        create = agent._openai_client.chat.completions.create
        create.side_effect = [
            response_stream(tool_name="read_file", call_id="read_1"),
            response_stream(tool_name="grep_search", call_id="search_1"),
            response_stream("Done"), response_stream("Follow-up"),
        ]
        with (
            patch.object(agent, "_execute_tool_call", new_callable=AsyncMock, return_value="mock tool result") as execute,
            patch.object(agent, "_emit_text"),
            patch("agents.agent.check_permission", return_value={"action": "allow"}),
            patch("agents.agent.start_spinner"), patch("agents.agent.stop_spinner"),
            patch("agents.agent.print_tool_call"), patch("agents.agent.print_tool_result"),
            patch("agents.agent.print_cost"),
        ):
            asyncio.run(agent._chat_openai("Read and search"))
            asyncio.run(agent._chat_openai("Continue"))
        self.assertEqual(execute.await_count, 2)
        self.assertEqual(create.await_count, 4)
        for call in create.call_args_list:
            self.assertEqual(call.kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        history = create.call_args_list[-1].kwargs["messages"]
        self.assertEqual([msg["tool_call_id"] for msg in history if msg["role"] == "tool"], ["read_1", "search_1"])
        self.assertEqual(agent._openai_messages[-1]["content"], "Follow-up")

    def test_compaction_uses_model_response_and_can_continue(self) -> None:
        agent = self._agent()
        agent._openai_messages.extend([
            {"role": "user", "content": "Remember blue"},
            {"role": "assistant", "content": "OK"},
            {"role": "user", "content": "Continue"},
            {"role": "assistant", "content": "OK"},
        ])
        memory = {
            "episode_memory": {"task_description": "Remember blue", "key_events": [], "current_progress": "saved"},
            "working_memory": {"immediate_goal": "Continue", "current_challenges": "", "next_actions": []},
            "tool_memory": {"tools_used": [], "derived_rules": []},
        }
        create = agent._openai_client.chat.completions.create
        create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(memory)))], usage=None,
        )
        with patch("agents.agent.save_folded_session_memory"), patch("agents.agent.fallback_folded_memory") as fallback:
            self.assertTrue(asyncio.run(agent._compact_openai(trigger="manual")))
            fallback.assert_not_called()
        self.assertIn("Remember blue", agent._openai_messages[-1]["content"])
        self.assertEqual(create.call_args.kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        create.return_value = response_stream("Continue after compact")
        with (
            patch.object(agent, "_emit_text"),
            patch("agents.agent.start_spinner"), patch("agents.agent.stop_spinner"),
            patch("agents.agent.print_cost"),
        ):
            asyncio.run(agent._chat_openai("What next?"))
        self.assertEqual(create.call_args.kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(agent._openai_messages[-1]["content"], "Continue after compact")


if __name__ == "__main__":
    unittest.main()
