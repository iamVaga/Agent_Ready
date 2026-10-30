"""How the three agents work as one team: read the shared memory, hand work over, tell the user.

Pure helpers around shared.memory. The agents call them from their chat handlers
and from a short timer that picks up tasks other agents have posted.
"""

from __future__ import annotations

from shared.privacy import redact_text


import asyncio
import os
import re
import time
from typing import Awaitable, Callable

from uagents import Context
from uagents_core.contrib.protocols.chat import ChatMessage, EndSessionContent, TextContent

from fixer_agent import github
from shared import memory, sites

# Task queue names and the names users see.
AGENTS = {"agentready": "AgentReady", "tester": "Tester", "fixer": "Fixer"}

# The Fixer opens a pull request on its own after a failed test, when the repo is known and allowed.
AUTO_FIX = os.getenv("TEAM_AUTO_FIX", "true").lower() == "true"
# The Fixer also merges its pull request (which deploys the store) without waiting for "merge".
AUTO_MERGE = os.getenv("TEAM_AUTO_MERGE", "false").lower() == "true"
# A fix that is queued, running, waiting for "apply" or waiting for a merge isn't requested again.
FIX_IN_HAND = {"requested", "working", "waiting", "done", "draft", "merging", "merged"}

_STATUS_RE = re.compile(r"\b(status|what('?s| is) (going on|happening|the plan)|what happen\w*|any update\w*|updates?\?|where are we|progress|what do you know)\b", re.I)
_MINE_RE = re.compile(r"\b(my|our|same|again)\b|\bre-?(scan|test)\b", re.I)
# ASI:One's own assistant answers every message our agents send. Its replies are long commentary, so
# long messages are never treated as requests, and commands only count in short messages.
CHATTER_CHARS = int(os.getenv("TEAM_CHATTER_CHARS", "300"))
COMMAND_CHARS = 160
QUIET_S = 600  # the same help or "already scanned" note at most once per 10 minutes
STUCK_S = 45 * 60  # a fix still "working" after this long has died
_MENTIONS_RE = re.compile(r"^(\s*@\S+)+\s*")
_FIX_RE = re.compile(r"\b(fix|repair|go ahead|do it|apply)\b|\b(shopper|agent|ai)[- ]ready\b|^\s*(yes|yep|yeah|sure)\b", re.I)
_MERGE_RE = re.compile(r"^\s*(yes,?\s*)?(please\s+)?merge\b", re.I)


def find_site(text: str) -> str | None:
    return sites.find_site(text)


def find_repo(text: str) -> str | None:
    ref = github.parse_repo(text)
    return f"{ref[0]}/{ref[1]}" if ref else None


# Everything known about one store. A different store starts from a clean slate, so a repo
# or a pull request from an earlier conversation is never applied to the wrong site.
PER_SITE = ("repo", "task", "scan", "scan_id", "shop", "fix", "fix_state", "fix_site", "pr_url", "pending")


def same_site(a: str, b: str) -> bool:
    host = lambda u: re.sub(r"^(https?://)?(www\.)?", "", u.strip().lower()).split("/")[0]  # noqa: E731
    return host(a) == host(b)


async def set_site(user: str, site: str, repo: str | None = None) -> dict:
    """Make `site` the user's current store, forgetting what belonged to a different one."""
    known = await memory.facts(user)
    if known.get("site") and not same_site(site, known["site"]):
        await memory.update(user, scanned_at=0, **{k: "" for k in PER_SITE})
    return await memory.update(user, site=site, repo=repo)


async def learn(user: str, me: str, text: str) -> dict:
    """Record the message and what it says about the user's store; return everything known."""
    await memory.remember(user, AGENTS[me], "user", text)
    site, repo = find_site(text), find_repo(text)
    known = await set_site(user, site, repo) if site else await memory.update(user, repo=repo)
    if known.get("site") and not sites.is_site(known["site"]):
        known = await memory.update(user, site="")  # e.g. a GitHub link saved as the site by an older version
    return known


def without_mentions(text: str) -> str:
    return _MENTIONS_RE.sub("", text).strip()


def is_chatter(text: str) -> bool:
    """Long, assistant-style text: never a request to act on."""
    return len(without_mentions(text)) > CHATTER_CHARS


def _command(text: str) -> str | None:
    t = without_mentions(text)
    return t if len(t) <= COMMAND_CHARS else None


def refers_to_known_site(text: str) -> bool:
    t = _command(text)
    return bool(t and _MINE_RE.search(t))


def wants_fix(text: str) -> bool:
    t = _command(text)
    return bool(t and _FIX_RE.search(t))


def wants_merge(text: str) -> bool:
    t = _command(text)
    return bool(t and _MERGE_RE.search(t))


def wants_status(text: str) -> bool:
    t = _command(text)
    return bool(t and _STATUS_RE.search(t))


async def first_in_a_while(user: str, what: str, seconds: int = QUIET_S) -> bool:
    """True at most once per `seconds` for this user, so repeated replies can't bounce back and forth."""
    key, now = f"quiet_{what}", time.time()
    if now - (await memory.facts(user)).get(key, 0) < seconds:
        return False
    await memory.update(user, **{key: now})
    return True


def chat_message(text: str, end: bool = True) -> ChatMessage:
    """end=False keeps the chat session open, so later updates still show in the user's conversation."""
    content = [TextContent(type="text", text=redact_text(text))]
    if end:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(content=content)


# While one of these is going on, more messages are coming: the session stays open.
ONGOING = {"requested", "working", "merging", "merged"}


async def work_in_progress(user: str) -> bool:
    return (await memory.facts(user)).get("fix_state") in ONGOING


async def tell_user(ctx: Context, me: str, user: str, text: str, via: str | None = None, final: bool = True):
    """Message the user from a background job and keep it in the shared record.

    via: the agent the user is chatting with. The message goes out through it, so it
    lands in the conversation the user already has open. final=False: more updates follow.
    """
    await memory.remember(user, AGENTS[me], "agent", text)
    if via and via != me and via in AGENTS:
        await memory.post_task(via, "notify", user, text=text, final=final)
    else:
        await ctx.send(user, chat_message(text, end=final))


async def deliver(ctx: Context, task: dict):
    """A message another agent wants passed to the user."""
    await ctx.send(task["user"], chat_message(task["text"], end=task.get("final", True)))


async def start_fix(by: str, user: str, known: dict, task: str | None = None) -> str:
    """The user asked for a fix: hand it to the Fixer, or say what's missing. Returns the reply."""
    site, repo = known.get("site") or None, known.get("repo")
    if not site and not repo:
        return "Which website should I fix? Send me its address and GitHub repo, e.g. `mystore.com, code at github.com/you/your-store`."
    if not repo:
        await memory.update(user, pending="fix")
        return (f"To fix **{site}** the Fixer needs its code. Send me the GitHub repo, e.g. `github.com/you/your-store`, "
                "and I'll hand everything over.")
    owner, _, name = repo.partition("/")
    if not github.allowed(owner, name):
        return (f"`{repo}` isn't connected to AgentReady yet, so the Fixer can't change it. "
                "Ask the AgentReady team to add it to the Fixer's allowed repos (`FIXER_ALLOWED_REPOS`).")
    state = known.get("fix_state") if not site or known.get("fix_site") == site else None
    if state in ("requested", "working") and time.time() - known.get("fix_at", 0) > STUCK_S:
        state = None  # the job died (e.g. a restart): let the user start again
    if state in ("requested", "working"):
        return f"The Fixer is already working on {site}. I'll pass its updates on to you here."
    if state == "done" and known.get("pr_url"):
        return f"The fix for {site} is ready: {known['pr_url']}\n\nReply **merge** and I'll merge it and have the Tester check the live site."
    if state in ("merging", "merged"):
        return f"The fix for {site} is merged. The Tester is checking the live site; I'll send you the result here."
    await memory.update(user, fix="requested", fix_state="requested", fix_site=site, pending="", fix_at=time.time())
    await memory.remember(user, AGENTS[by], "event", f"Asked the Fixer to fix {site or 'the site'} ({repo})")
    await memory.post_task("fixer", "fix", user, site=site, repo=repo, task=task or known.get("task") or "any product", by=by)
    return (f"**Handed to the Fixer.** It will read `{repo}`, change the code so AI agents can shop on {site or 'your live site'}, "
            "test the patched store and open a pull request. I'll pass its updates on to you here; this usually takes 5 to 15 minutes.")


async def request_fix(by: str, user: str, known: dict, passed: int, task: str | None) -> str | None:
    """After a failed shopping test, hand the fix over on our own if we can. Returns a line for the user, or None."""
    site, repo = known.get("site"), known.get("repo")
    if not (AUTO_FIX and site and repo and passed < 5):
        return None
    owner, _, name = repo.partition("/")
    if not github.allowed(owner, name):
        return None
    if known.get("fix_state") in FIX_IN_HAND and known.get("fix_site") == site:
        return None
    return f"The shopping test passed {passed}/5. " + await start_fix(by, user, known, task)


async def start_merge(by: str, user: str, known: dict) -> str:
    """The user approved the Fixer's pull request: merge it, then have the Tester check the live site."""
    pr_url, repo = known.get("pr_url"), known.get("repo")
    if pr_url and known.get("fix_state") == "draft":
        return f"{pr_url} is a draft: the change isn't a clean win yet, so I won't merge it. Review it on GitHub, or say **fix it** to try again."
    if not pr_url or known.get("fix_state") != "done":
        return "There's no pull request waiting to be merged."
    await memory.update(user, fix="merging", fix_state="merging")
    await memory.post_task("fixer", "merge", user, repo=repo, pr_url=pr_url, site=known.get("site"),
                           task=known.get("task") or "any product", by=by)
    return f"Merging {pr_url}. Once the site has redeployed, the Tester will check it live and I'll send you the result."


Handler = Callable[[Context, dict], Awaitable[None]]


async def pump(ctx: Context, me: str, handlers: dict[str, Handler]):
    """Run the tasks other agents have posted for this agent."""
    while (task := await memory.next_task(me)):
        handler = handlers.get(task["kind"])
        if not handler:
            ctx.logger.warning(f"No handler for task {task['kind']!r}")
            continue
        asyncio.create_task(_run(ctx, handler, task))


async def _run(ctx: Context, handler: Handler, task: dict):
    try:
        await handler(ctx, task)
    except Exception:
        ctx.logger.exception(f"Task {task['kind']} failed")
