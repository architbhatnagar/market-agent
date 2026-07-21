"""
Claude agent: a tool-use loop over the Anthropic API.
Claude decides which market-data tools to call, we execute them,
feed results back, and return the final synthesized answer.
"""
import os
import anthropic

from tools import TOOL_DEFINITIONS, run_tool
from prompts import SYSTEM_PROMPT

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOOL_ROUNDS = 8
MAX_HISTORY_MESSAGES = 20  # rolling window to keep token costs sane


class ClaudeAgent:
    def __init__(self):
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.histories: dict[int, list] = {}

    def ask(self, chat_id: int, user_message: str) -> str:
        messages = self.histories.get(chat_id, []) + [
            {"role": "user", "content": user_message}
        ]

        for _ in range(MAX_TOOL_ROUNDS):
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=1500,
                system=SYSTEM_PROMPT,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                self.histories[chat_id] = messages[-MAX_HISTORY_MESSAGES:]
                text = "".join(b.text for b in response.content if b.type == "text")
                return text.strip() or "(no response)"

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": run_tool(block.name, block.input),
                    })
            messages.append({"role": "user", "content": tool_results})

        self.histories[chat_id] = messages[-MAX_HISTORY_MESSAGES:]
        return "I hit my tool-call limit on that one — try breaking the question into smaller parts."

    def reset(self, chat_id: int):
        self.histories.pop(chat_id, None)
