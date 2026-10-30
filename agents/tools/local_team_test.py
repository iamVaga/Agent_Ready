"""Run AgentReady and the Tester together locally and ask AgentReady one question.

AgentReady scans the site and, at the same time, asks the Tester agent (by its
address) to try shopping there. This checks the two agents cooperate without
needing Agentverse or ASI:One.

    python tools/local_team_test.py "Can AI agents buy earbuds under 10k on voltkart.onrender.com?"
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agentready_agent"))
sys.path.insert(0, str(ROOT))

from shared.privacy import privacy_print as print  # Redact CLI output at the boundary.

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from uagents import Agent, Bureau, Context  # noqa: E402
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent  # noqa: E402

import agent as agentready_module  # noqa: E402
from tester_agent.agent import create_agent as create_tester  # noqa: E402

question = " ".join(sys.argv[1:]) or "Is nest-and-grain.vercel.app ready for AI shoppers?"

agentready = agentready_module.create_agent(mailbox=False, port=8011)
tester = create_tester(mailbox=False, port=8012)
agentready_module.TESTER_ADDRESS = tester.address  # talk to the local Tester
client = Agent(name="owner", seed="agentready local team test owner")


@client.on_event("startup")
async def ask(ctx: Context):
    print(f"\n>>> OWNER: {question}\n", flush=True)
    await ctx.send(agentready.address, ChatMessage(content=[TextContent(type="text", text=question)]))


@client.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    print("... AgentReady acknowledged; scanning and asking the Tester\n", flush=True)


@client.on_message(ChatMessage)
async def on_reply(ctx: Context, sender: str, msg: ChatMessage):
    for item in msg.content:
        if isinstance(item, TextContent):
            print("<<< AGENTREADY:\n" + item.text + "\n", flush=True)
    asyncio.get_running_loop().call_later(0.5, lambda: os._exit(0))


bureau = Bureau(port=8010)
for a in (agentready, tester, client):
    bureau.add(a)

if __name__ == "__main__":
    bureau.run()
