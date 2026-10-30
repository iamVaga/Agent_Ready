"""Send one chat message to a deployed agent through Agentverse (its mailbox), and print the reply.

This uses the same route ASI:One uses, so it checks the agent is reachable from outside.

    python tools/mailbox_ping.py agent1q... "hello"
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.privacy import privacy_print as print

sys.stdout.reconfigure(encoding="utf-8")

from uagents import Agent, Context  # noqa: E402
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent  # noqa: E402

target, text = sys.argv[1], " ".join(sys.argv[2:]) or "hello"
port = int(os.getenv("PING_PORT", "8009"))
client = Agent(name="mailbox-ping", seed="agentready mailbox ping client v1", port=port, endpoint=[f"http://127.0.0.1:{port}/submit"])


@client.on_event("startup")
async def send(ctx: Context):
    await asyncio.sleep(3)  # let registration finish so the reply can find us
    print(f">>> to {target[:18]}…: {text}", flush=True)
    await ctx.send(target, ChatMessage(content=[TextContent(type="text", text=text)]))


@client.on_message(ChatAcknowledgement)
async def ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    print("... acknowledged", flush=True)


@client.on_message(ChatMessage)
async def got(ctx: Context, sender: str, msg: ChatMessage):
    for item in msg.content:
        if isinstance(item, TextContent):
            print("<<< reply:\n" + item.text, flush=True)
    asyncio.get_running_loop().call_later(0.5, lambda: os._exit(0))


if __name__ == "__main__":
    client.run()
