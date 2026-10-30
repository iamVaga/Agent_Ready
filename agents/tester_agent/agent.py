"""AgentReady Tester as a Fetch.ai uAgent speaking the Agent Chat Protocol.

Message it a website and a shopping task, e.g.
    "Can an AI agent buy running shoes under ₹10,000 on stride-lab-beta.vercel.app?"
It tries to do it and replies with a step-by-step pass/fail report.

Run it:   python tester_agent/agent.py
Settings: agents/.env (TESTER_AGENT_SEED, AGENT_MAILBOX, TESTER_PORT).
"""

from __future__ import annotations



import os
import re
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from uagents import Agent, Context, Protocol  # noqa: E402
from uagents_core.contrib.protocols.chat import (  # noqa: E402
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    TextContent,
    chat_protocol_spec,
)

from shared.privacy import redact_text
from shared import memory, sites, team  # noqa: E402
from tester_agent.shopper import format_report, run  # noqa: E402

TESTS_PER_HOUR = int(os.getenv("TESTER_RUNS_PER_USER_PER_HOUR", "25"))
# Our own AgentReady agent applies its own per-user limit, so it isn't limited here.
TRUSTED = {a.strip() for a in os.getenv("TESTER_TRUSTED_AGENTS", "").split(",") if a.strip()}
_recent: dict[str, deque[float]] = defaultdict(deque)

HELP = (
    "I'm the **AgentReady Tester**. I check whether an AI shopping agent can actually buy from a website: "
    "find a product, compare options, add it to the cart and hand you a checkout link.\n\n"
    "Send me a site and what to shop for, for example:\n"
    "- `Can an AI agent buy running shoes under ₹10,000 on stride-lab-beta.vercel.app?`\n"
    "- `Test nest-and-grain.vercel.app: a dining chair under ₹10,000`"
)


def _allow(sender: str) -> bool:
    if sender in TRUSTED:
        return True
    now, q = time.time(), _recent[sender]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= TESTS_PER_HOUR:
        return False
    q.append(now)
    return True


def split_request(text: str) -> tuple[str | None, str]:
    """Pull the website out of the message; the rest is the shopping task."""
    m = sites.find_site_match(text)
    if not m:
        return None, text
    site = m.group(1).rstrip(".,;:!?)")
    task = (text[: m.start()] + " " + text[m.end():]).strip()
    task = re.sub(r"^(test|check|try|can an ai agent|can ai agents?|on|at|:|-)\s+", "", task, flags=re.I)
    task = re.sub(r"^(buy|find|get|shop for|order)\s+", "", task.strip(), flags=re.I)
    task = re.sub(r"\s+(on|at|from|in)\s*$", "", task.strip(" :-?"), flags=re.I).strip(" :-?")
    return site, task or "any product"


def reply(text: str) -> ChatMessage:
    return ChatMessage(content=[TextContent(type="text", text=redact_text(text)), EndSessionContent(type="end-session")])


PROFILE_README = "tester.md"
PROFILE_DESCRIPTION = "Tries to shop on any website the way an AI agent would and reports where it gets stuck: finding, comparing, cart or checkout."

chat = Protocol(spec=chat_protocol_spec)


@chat.on_message(ChatMessage)
async def handle_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id))
    text = " ".join(i.text for i in msg.content if isinstance(i, TextContent)).strip()
    if not text:
        return
    # Our own agents ask on behalf of a user and record it themselves; a person's chat is shared with the team.
    internal = sender in TRUSTED
    if not internal and team.is_chatter(text):
        ctx.logger.info(f"Ignoring long message from {sender[:16]}… ({len(text)} chars)")
        return
    known = {} if internal else await team.learn(sender, "tester", text)
    if not internal and team.wants_status(text):
        await ctx.send(sender, reply(await memory.summary(sender)))
        return
    site, task = split_request(text)
    if not site and known.get("site") and team.refers_to_known_site(text):
        site = known["site"]
    if not site:
        if internal or await team.first_in_a_while(sender, "help"):
            await ctx.send(sender, reply(HELP))
        return
    if not _allow(sender):
        await ctx.send(sender, reply(f"You've run {TESTS_PER_HOUR} tests in the last hour. Please try again later."))
        return
    ctx.logger.info(f"Testing {site} for {sender[:16]}…: [shopping task omitted]")
    try:
        report = await run(site, task)
        text_out = format_report(report)
        if not internal:
            await team.set_site(sender, site)
            known = await memory.update(sender, site=site, task=task, shop=f"{report.passed}/5 steps on {site}")
            await memory.remember(sender, "Tester", "agent", f"Shopping test on {site}: {report.passed}/5")
            handoff = await team.request_fix("tester", sender, known, report.passed, task)
            if handoff:
                text_out += "\n\n---\n\n" + handoff
        await ctx.send(sender, reply(text_out))
    except Exception as err:  # never leave the user without an answer
        ctx.logger.exception("Tester run failed")
        await ctx.send(sender, reply(f"Sorry, the test on {site} failed unexpectedly ({type(err).__name__}). Please try again."))


@chat.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


def create_agent(mailbox: bool | None = None, port: int | None = None) -> Agent:
    seed = os.getenv("TESTER_AGENT_SEED")
    if not seed:
        raise RuntimeError("Set TESTER_AGENT_SEED in agents/.env (any long secret phrase).")
    if mailbox is None:
        mailbox = os.getenv("AGENT_MAILBOX", "false").lower() == "true"
    port = port or int(os.getenv("TESTER_PORT", "8002"))
    kwargs = dict(name="AgentReady Tester", seed=seed, port=port)
    if mailbox:
        kwargs.update(mailbox=True, publish_agent_details=True, description=PROFILE_DESCRIPTION)
    else:
        kwargs.update(endpoint=[f"http://127.0.0.1:{port}/submit"])
    agent = Agent(**kwargs)
    # Agentverse profile README. Read as UTF-8 here: uAgents' own readme_path
    # uses the platform default encoding, which breaks on ₹ and emoji on Windows.
    agent._readme = (Path(__file__).resolve().parents[1] / "profiles" / PROFILE_README).read_text(encoding="utf-8")
    agent.include(chat, publish_manifest=mailbox)

    @agent.on_interval(period=3.0)
    async def pick_up_team_tasks(ctx: Context):
        await team.pump(ctx, "tester", {"notify": team.deliver})

    return agent


if __name__ == "__main__":
    create_agent().run()
