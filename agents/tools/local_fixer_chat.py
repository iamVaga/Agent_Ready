"""Chat with the Fixer agent locally, the way ASI:One will: several messages in a row.

Each message is sent once the Fixer has finished answering the previous one.
Set FIXER_DRY_RUN=true to make and test the fixes without opening a pull request.

    python tools/local_fixer_chat.py "Fix github.com/me/my-store for running shoes" "apply all"
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.privacy import privacy_print as print  # Redact CLI output at the boundary.

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from uagents import Agent, Bureau, Context  # noqa: E402
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, EndSessionContent, TextContent  # noqa: E402

from fixer_agent.agent import create_agent  # noqa: E402

messages = sys.argv[1:] or ["hello"]
fixer = create_agent(mailbox=False, port=8003)
client = Agent(name="fixer-test-client", seed="agentready local fixer test client seed")
state = {"i": 0}


async def send_next(ctx: Context):
    if state["i"] >= len(messages):
        asyncio.get_running_loop().call_later(0.5, lambda: os._exit(0))
        return
    text = messages[state["i"]]
    state["i"] += 1
    print(f"\n>>> YOU: {text}\n", flush=True)
    await ctx.send(fixer.address, ChatMessage(content=[TextContent(type="text", text=text)]))


@client.on_event("startup")
async def start(ctx: Context):
    await send_next(ctx)


@client.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


@client.on_message(ChatMessage)
async def on_reply(ctx: Context, sender: str, msg: ChatMessage):
    for item in msg.content:
        if isinstance(item, TextContent):
            print("<<< FIXER:\n" + item.text + "\n", flush=True)
    if any(isinstance(i, EndSessionContent) for i in msg.content):
        await send_next(ctx)


bureau = Bureau(port=8010)
bureau.add(fixer)
bureau.add(client)

if __name__ == "__main__":
    bureau.run()
