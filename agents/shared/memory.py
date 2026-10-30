"""Memory and task bus shared by the AgentReady, Tester and Fixer agents.

One place holds what each user has told any of the agents (site, repo, shopping
task), what the agents found (scan score, shopping test result, pull request),
a short transcript, and a queue of tasks the agents hand to each other. With
REDIS_URL set (Railway) all three services share it; without it, it lives in
this process only, which is enough to try the agents locally one at a time.
Everything is keyed by the chat user's address.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid

from .privacy import redact

log = logging.getLogger("agentready.memory")

TTL_S = 24 * 3600
TRANSCRIPT_CAP = 12

_backend = None


class _Local:
    """In-process stand-in for Redis: the few operations we use."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.expires: dict[str, float] = {}

    def _expire(self, key):
        for expired in [k for k, expiry in self.expires.items() if expiry <= time.time()]:
            self.kv.pop(expired, None)
            self.lists.pop(expired, None)
            self.expires.pop(expired, None)

    async def get(self, key):
        self._expire(key)
        return self.kv.get(key)

    async def set(self, key, value):
        self._expire(key)
        self.kv[key] = value
        self.expires[key] = time.time() + TTL_S

    async def push(self, key, value, cap=0):
        self._expire(key)
        self.expires[key] = time.time() + TTL_S
        items = self.lists.setdefault(key, [])
        items.append(value)
        if cap:
            del items[:-cap]

    async def pop(self, key):
        self._expire(key)
        items = self.lists.get(key)
        return items.pop(0) if items else None

    async def items(self, key):
        self._expire(key)
        return list(self.lists.get(key, []))


class _Redis:
    """Redis on Railway. If it can't be reached, the agents keep answering without memory."""

    def __init__(self, url: str):
        import redis.asyncio as aioredis

        self.r = aioredis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)

    async def _try(self, op, default=None):
        try:
            return await op
        except Exception as err:
            log.warning("Shared memory unavailable: %s", err)
            return default

    async def get(self, key):
        return await self._try(self.r.get(key))

    async def set(self, key, value):
        await self._try(self.r.set(key, value, ex=TTL_S))

    async def push(self, key, value, cap=0):
        await self._try(self._push(key, value, cap))

    async def _push(self, key, value, cap):
        await self.r.rpush(key, value)
        if cap:
            await self.r.ltrim(key, -cap, -1)
        await self.r.expire(key, TTL_S)

    async def pop(self, key):
        return await self._try(self.r.lpop(key))

    async def items(self, key):
        return await self._try(self.r.lrange(key, 0, -1), [])


def _store():
    global _backend
    if _backend is None:
        url = os.getenv("REDIS_URL", "").strip()
        if url:
            _backend = _Redis(url)
        else:
            log.warning("REDIS_URL not set: memory is local to this process")
            _backend = _Local()
    return _backend


async def facts(user: str) -> dict:
    raw = await _store().get(f"facts:{user}")
    return redact(json.loads(raw)) if raw else {}


async def update(user: str, **values) -> dict:
    """Merge values into the user's facts. None values are ignored."""
    current = await facts(user)
    current.update({k: v for k, v in values.items() if v is not None})
    await _store().set(f"facts:{user}", json.dumps(redact(current)))
    return redact(current)


async def remember(user: str, agent: str, role: str, text: str):
    """Add a line to the shared transcript. role: user | agent | event."""
    entry = {"t": int(time.time()), "agent": agent, "role": role, "text": text[:600]}
    await _store().push(f"chat:{user}", json.dumps(redact(entry)), cap=TRANSCRIPT_CAP)


async def transcript(user: str, n: int = 8) -> list[dict]:
    return [redact(json.loads(i)) for i in (await _store().items(f"chat:{user}"))[-n:]]


async def post_task(to: str, kind: str, user: str, **payload) -> str:
    """Hand work to another agent. It picks it up within a few seconds."""
    task = {"id": uuid.uuid4().hex[:8], "kind": kind, "user": user, **payload}
    await _store().push(f"tasks:{to}", json.dumps(redact(task)))
    return task["id"]


async def next_task(me: str) -> dict | None:
    raw = await _store().pop(f"tasks:{me}")
    return redact(json.loads(raw)) if raw else None


async def summary(user: str) -> str:
    """What the team knows and has done for this user, in plain words."""
    f, log_ = await facts(user), await transcript(user, 8)
    if not f and not log_:
        return "I don't have anything on you yet. Send me your site (and its GitHub repo) to get started."
    lines = ["## What the AgentReady team knows", ""]
    for key, label in (("site", "Site"), ("repo", "GitHub repo"), ("task", "Shopping task"),
                       ("scan", "Latest scan"), ("shop", "Latest shopping test"),
                       ("fix", "Fix status"), ("pr_url", "Pull request")):
        if f.get(key):
            lines.append(f"- **{label}:** {f[key]}")
    if log_:
        lines += ["", "**Recent activity**"]
        lines += [f"- {e['agent']}: {_first_line(e['text'])}" for e in log_ if e["role"] != "user"][-5:]
    return "\n".join(lines)


def _first_line(text: str) -> str:
    plain = (re.sub(r"[*#`]", "", ln).strip() for ln in text.splitlines())
    line = next((ln for ln in plain if ln), "")
    return re.sub(r"^(Fixer|Tester|AgentReady):\s*", "", line)[:140]
