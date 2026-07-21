"""
Gemini agent. Same job as agent_claude.py, but the google-genai SDK
does automatic function calling: we hand it the raw Python functions
and it runs the tool loop itself. Chat sessions hold history per chat.
"""
import os

from google import genai
from google.genai import types

from tools import (
    get_quote, get_fundamentals, get_price_history,
    compare_tickers, search_mutual_funds, get_fund_nav_history,
)
from portfolio import get_portfolio, add_holding, remove_holding, update_watchlist
from prompts import SYSTEM_PROMPT

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

_TOOLS = [
    get_quote, get_fundamentals, get_price_history,
    compare_tickers, search_mutual_funds, get_fund_nav_history,
    get_portfolio, add_holding, remove_holding, update_watchlist,
]


class GeminiAgent:
    def __init__(self):
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=_TOOLS,  # callables => automatic function calling
        )
        self.sessions: dict[int, object] = {}

    def _session(self, chat_id: int):
        if chat_id not in self.sessions:
            self.sessions[chat_id] = self.client.chats.create(
                model=MODEL, config=self.config
            )
        return self.sessions[chat_id]

    def ask(self, chat_id: int, user_message: str) -> str:
        response = self._session(chat_id).send_message(user_message)
        return (response.text or "(no response)").strip()

    def reset(self, chat_id: int):
        self.sessions.pop(chat_id, None)
