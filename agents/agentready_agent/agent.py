"""AgentReady as a Fetch.ai uAgent speaking the Agent Chat Protocol.

Run it:   python agentready_agent/agent.py
Settings: agents/.env (see .env.example). With AGENT_MAILBOX=true the agent
connects to Agentverse so ASI:One can reach it; with false it stays local.
"""

from __future__ import annotations



import asyncio
import os
import re
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uagents import Agent, Context, Protocol  # noqa: E402
from uagents_core.contrib.protocols.chat import (  # noqa: E402
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

import brain  # noqa: E402
from shared.privacy import redact_text
from shared import memory, team  # noqa: E402

# Our API lets this agent skip its per-IP limit, so the agent limits each
# chat user instead. In memory is enough: a restart forgiving everyone is fine.
SCANS_PER_HOUR = int(os.getenv("AGENT_SCANS_PER_USER_PER_HOUR", "25"))
# The AgentReady Tester agent we ask to try shopping on each site.
TESTER_ADDRESS = os.getenv("TESTER_AGENT_ADDRESS", "").strip()
TESTER_TIMEOUT_S = int(os.getenv("TESTER_TIMEOUT_S", "150"))
# After a merge: how often and how many times the Tester checks the live site while it redeploys.
VERIFY_WAIT_S = int(os.getenv("VERIFY_WAIT_S", "90"))
VERIFY_TRIES = int(os.getenv("VERIFY_TRIES", "6"))
RESCAN_S = 600  # the same site isn't rescanned within 10 minutes unless the user asks again
_recent: dict[str, deque[float]] = defaultdict(deque)


def _allow(sender: str) -> bool:
    now = time.time()
    q = _recent[sender]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= SCANS_PER_HOUR:
        return False
    q.append(now)
    return True


def text_message(text: str, end_session: bool = True) -> ChatMessage:
    content = [TextContent(type="text", text=redact_text(text))]
    if end_session:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(content=content)


PROFILE_README = "agentready.md"
PROFILE_DESCRIPTION = "Checks whether AI shopping agents can find, read and buy from your online store, runs a live shopping test, and shows what to fix."

chat = Protocol(spec=chat_protocol_spec)


@chat.on_message(ChatMessage)
async def handle_chat(ctx: Context, sender: str, msg: ChatMessage):
    # Acknowledge first, always: ASI:One waits for it before showing progress.
    await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id))

    text = " ".join(item.text for item in msg.content if isinstance(item, TextContent)).strip()
    if any(isinstance(item, StartSessionContent) for item in msg.content) and not text:
        return
    if not text:
        return

    if TESTER_ADDRESS and sender == TESTER_ADDRESS:
        return  # a late Tester report: already handled (or timed out) inside ask_tester

    ctx.logger.info(f"Message from {sender[:16]}…: [message content omitted]")
    if team.is_chatter(text):
        # Long commentary, typically ASI:One's assistant reacting to our last message. Answering it would start a loop.
        ctx.logger.info(f"Ignoring long message from {sender[:16]}… ({len(text)} chars)")
        return

    known = await team.learn(sender, "agentready", text)
    if team.wants_status(text):
        await ctx.send(sender, text_message(await memory.summary(sender)))
        return
    site_in_text = team.find_site(text)
    if team.wants_merge(text):
        reply = await team.start_merge("agentready", sender, known)
        await ctx.send(sender, text_message(reply, end_session=not await team.work_in_progress(sender)))
        return
    if not site_in_text and (team.find_repo(text) or team.wants_fix(text)):
        # "fix it", or the GitHub repo sent after we asked for it: the Fixer takes over from here.
        reply = await team.start_fix("agentready", sender, known)
        await ctx.send(sender, text_message(reply, end_session=not await team.work_in_progress(sender)))
        return

    intent = await brain.understand(text)
    if intent.action != "scan" and known.get("site") and team.refers_to_known_site(text):
        # "scan my store again": the site was given to any of the three agents earlier.
        intent = brain.Intent("scan", known["site"], known.get("task"))
    if intent.action == "scan" and not _allow(sender):
        await ctx.send(
            sender,
            text_message(f"You've run {SCANS_PER_HOUR} scans in the last hour. Please try again a little later."),
        )
        return

    if intent.action != "scan" or not intent.url or "github.com" in intent.url.lower():
        if await team.first_in_a_while(sender, "help"):
            await ctx.send(sender, text_message(brain.HELP_TEXT))
        return
    if (intent.url == known.get("site") and time.time() - known.get("scanned_at", 0) < RESCAN_S
            and not team.refers_to_known_site(text)):
        # The same site again within minutes, usually an echo of our own reply: don't rescan.
        if await team.first_in_a_while(sender, "rescan"):
            await ctx.send(sender, text_message(f"I scanned {intent.url} a few minutes ago. Say **scan it again** for a fresh scan, "
                                                "**fix it** to have the Fixer fix it, or **status** to see where things are."))
        return

    # Two things at once: our scanner grades the site, and the Tester agent
    # actually tries to shop on it. The owner gets both in one answer.
    (scan_reply, scan_id), tester_reply = await asyncio.gather(
        brain.reply_to(text, intent),
        ask_tester(ctx, intent.url, intent.shopping),
    )
    passed = tester_passed(tester_reply)
    await team.set_site(sender, intent.url)
    # Shared with the Tester and Fixer, so any of them knows which site and scan "my store" means.
    known = await memory.update(
        sender, site=intent.url, task=intent.shopping, scan_id=scan_id, scanned_at=time.time(),
        scan=f"{intent.url} (scan {scan_id})" if scan_id else None,
        shop=f"{passed}/5 steps on {intent.url}" if passed is not None else None,
    )
    await memory.remember(sender, "AgentReady", "agent", f"Scanned {intent.url}; shopping test {passed if passed is not None else '?'}/5")
    if passed == 5:
        handoff = None
    elif team.wants_fix(text):  # "make mystore.com shopper ready": fix it straight away
        handoff = await team.start_fix("agentready", sender, known, intent.shopping)
    else:
        handoff = await team.request_fix("agentready", sender, known, passed, intent.shopping) if passed is not None else None
    await ctx.send(sender, text_message(combine(scan_reply, tester_reply, handoff),
                                        end_session=not await team.work_in_progress(sender)))


async def verify_live(ctx: Context, task: dict):
    """After the Fixer's pull request is merged: wait for the deploy, then have the Tester shop on the live site."""
    user, site, shopping = task["user"], task["site"], task.get("task")
    report, passed = None, None
    for attempt in range(VERIFY_TRIES):
        await asyncio.sleep(VERIFY_WAIT_S)
        report = await ask_tester(ctx, site, shopping) or report
        passed = tester_passed(report)
        if passed == 5:
            break
        ctx.logger.info(f"Live check {attempt + 1}/{VERIFY_TRIES} on {site}: {passed}/5, waiting for the deploy")
    if passed is not None:
        await memory.update(user, shop=f"{passed}/5 steps on {site} (live, after the fix)",
                            fix="fixed and live" if passed == 5 else "merged, live test still failing",
                            fix_state="live" if passed == 5 else "failed")
    head = (f"## ✅ {site} is fixed and live" if passed == 5
            else f"## ⚠️ {site} after the merge: {passed if passed is not None else '?'}/5")
    note = ("The Tester just shopped on the live site after the deploy:" if passed == 5 else
            "The fix is merged, but the live site still fails the shopping test. The deploy may not have finished: "
            "say **scan my store again** in a few minutes.")
    await team.tell_user(ctx, "agentready", user, f"{head}\n\n{note}\n\n{report or 'The Tester did not answer.'}")


def tester_passed(report: str | None) -> int | None:
    m = re.search(r"(\d) / 5 steps", report or "")
    return int(m.group(1)) if m else None


async def ask_tester(ctx: Context, url: str, shopping: str | None) -> str | None:
    """Ask the AgentReady Tester agent to try shopping on the site. Returns its report text."""
    if not TESTER_ADDRESS:
        return None
    request = f"{shopping or 'any product'} on {url}"
    ctx.logger.info(f"Asking Tester: {request!r}")
    try:
        reply, status = await ctx.send_and_receive(
            TESTER_ADDRESS,
            ChatMessage(content=[TextContent(type="text", text=request)]),
            response_type=ChatMessage,
            timeout=TESTER_TIMEOUT_S,
        )
    except Exception:
        ctx.logger.exception("Tester request failed")
        return None
    if not isinstance(reply, ChatMessage):
        ctx.logger.warning(f"No report from Tester: {getattr(status, 'detail', status)}")
        return None
    return " ".join(i.text for i in reply.content if isinstance(i, TextContent)).strip() or None


FIXER_HINT = (
    "\n\n---\n\n**Want it fixed?** Reply **fix it** with your store's GitHub repo, e.g. `fix it, code at github.com/you/your-store`. "
    "The AgentReady Fixer will change the code, test it and open a pull request, all from this chat."
)


def combine(scan_reply: str, tester_reply: str | None, handoff: str | None = None) -> str:
    if not tester_reply:
        return scan_reply + "\n\n_The shopping test by the AgentReady Tester agent didn't finish in time, so only the scan is shown._" + FIXER_HINT
    hint = "\n\n---\n\n" + handoff if handoff else "" if "5 / 5 steps" in tester_reply else FIXER_HINT
    # A checkout link with the product already in the cart goes first: the shopper only has to pay.
    buy = re.search(r"### 🛒 Ready to buy.*?\n(?:https?://\S+)(?:\n\n⚠️ Before paying:[^\n]*)?", tester_reply, re.S)
    return (
        (buy.group(0) + "\n\n---\n\n" if buy else "")
        + "# Part 1: AI readiness scan\n\n" + scan_reply
        + "\n\n---\n\n# Part 2: Can an AI agent actually buy here?\n"
        + "_Our Tester agent just tried it, step by step:_\n\n" + tester_reply + hint
    )


@chat.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


def create_agent(mailbox: bool | None = None, port: int | None = None) -> Agent:
    seed = os.getenv("AGENTREADY_AGENT_SEED")
    if not seed:
        raise RuntimeError("Set AGENTREADY_AGENT_SEED in agents/.env (any long secret phrase).")
    if mailbox is None:
        mailbox = os.getenv("AGENT_MAILBOX", "false").lower() == "true"
    port = port or int(os.getenv("PORT", "8001"))

    kwargs = dict(name="AgentReady", seed=seed, port=port)
    if mailbox:
        kwargs.update(mailbox=True, publish_agent_details=True, description=PROFILE_DESCRIPTION)
    else:
        kwargs.update(endpoint=[f"http://127.0.0.1:{port}/submit"])

    agent = Agent(**kwargs)
    # publish_manifest lets Agentverse and ASI:One see that we speak the chat protocol.
    # Agentverse profile README. Read as UTF-8 here: uAgents' own readme_path
    # uses the platform default encoding, which breaks on ₹ and emoji on Windows.
    agent._readme = (Path(__file__).resolve().parents[1] / "profiles" / PROFILE_README).read_text(encoding="utf-8")
    agent.include(chat, publish_manifest=mailbox)

    @agent.on_interval(period=3.0)
    async def pick_up_team_tasks(ctx: Context):
        await team.pump(ctx, "agentready", {"notify": team.deliver, "verify": verify_live})

    return agent


if __name__ == "__main__":
    create_agent().run()
