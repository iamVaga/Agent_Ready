"""Chat with the AgentReady agent locally, the way ASI:One will.

Runs the real agent and a small test client together in one Bureau, so
messages are delivered locally: no Agentverse, mailbox or ASI:One needed.

    python tools/local_chat_test.py "Is example.com ready for AI shoppers?"
    python tools/local_chat_test.py --tester "running shoes under ₹10,000 on stride-lab-beta.vercel.app"
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252; replies contain emoji

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agentready_agent"))
sys.path.insert(0, str(ROOT))

from shared.privacy import privacy_print as print  # Redact CLI output at the boundary.

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from uagents import Agent, Bureau, Context  # noqa: E402
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent  # noqa: E402

use_tester = "--tester" in sys.argv
args = [a for a in sys.argv[1:] if a != "--tester"]

if use_tester:
    sys.path.insert(0, str(ROOT))

    from tester_agent.agent import create_agent  # noqa: E402
else:
    from agent import create_agent  # noqa: E402

question = " ".join(args) or "Is example.com ready for AI shoppers?"

agentready = create_agent(mailbox=False, port=8001)
client = Agent(name="test-client", seed=os.getenv("TEST_CLIENT_SEED", "agentready local test client seed"))


@client.on_event("startup")
async def ask(ctx: Context):
    print(f"\n>>> YOU: {question}\n", flush=True)
    await ctx.send(agentready.address, ChatMessage(content=[TextContent(type="text", text=question)]))


@client.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    print("... AgentReady acknowledged, working on it\n", flush=True)


@client.on_message(ChatMessage)
async def on_reply(ctx: Context, sender: str, msg: ChatMessage):
    for item in msg.content:
        if isinstance(item, TextContent):
            print(f"<<< {'TESTER' if use_tester else 'AGENTREADY'}:\n" + item.text + "\n", flush=True)
    # One question, one answer: stop the bureau.
    asyncio.get_running_loop().call_later(0.5, lambda: os._exit(0))


bureau = Bureau(port=8000)
bureau.add(agentready)
bureau.add(client)

if __name__ == "__main__":
    bureau.run()
