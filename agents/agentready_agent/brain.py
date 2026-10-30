"""What the AgentReady agent says, kept apart from the Fetch.ai wiring.

Everything here is plain async Python: understand a chat message, run a scan
through the AgentReady API, turn the result into a chat reply. agent.py only
moves messages in and out, so this file can be tested without any network
registration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass

import httpx
from openai import AsyncOpenAI

from shared import sites
from shared.privacy import redact, redact_text, private_output

log = logging.getLogger("agentready.brain")

API_URL = os.getenv("AGENTREADY_API_URL", "https://agentready-api-khag.onrender.com").rstrip("/")
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "")
WEB_URL = os.getenv("AGENTREADY_WEB_URL", "https://agentready-five-rust.vercel.app").rstrip("/")
ASI1_API_KEY = os.getenv("ASI1_API_KEY", "")
ASI1_MODEL = os.getenv("ASI1_MODEL", "asi1")

# A scan can wait on a sleeping Render instance (~50s) plus the scan itself (~20s).
SCAN_TIMEOUT_S = 120

_llm = AsyncOpenAI(base_url="https://api.asi1.ai/v1", api_key=ASI1_API_KEY) if ASI1_API_KEY else None

HELP_TEXT = (
    "Hi! I'm **AgentReady**. I check whether AI shopping agents (ChatGPT, Gemini, "
    "Perplexity and others) can actually find, read and buy from a website.\n\n"
    "Send me a website address, for example:\n"
    "- `Is mystore.com ready for AI shoppers?`\n"
    "- `Scan example.com`\n\n"
    "I'll reply with a grade, a score out of 100 and the most important things to fix."
)


@dataclass
class Intent:
    action: str  # "scan" | "help"
    url: str | None = None
    shopping: str | None = None  # what to try buying, e.g. "earbuds under ₹10,000"


def _regex_url(text: str) -> str | None:
    return sites.find_site(text)


async def understand(text: str) -> Intent:
    """Work out what the user wants. ASI:One's model first, a regex as backup."""
    for _attempt in range(2 if _llm else 0):
        try:
            r = await _llm.chat.completions.create(
                model=ASI1_MODEL,
                temperature=0,
                max_tokens=500,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You route messages for AgentReady, an agent that checks whether AI shopping agents can use a website. "
                            "Reply with JSON only: "
                            '{"action": "scan" | "help", "url": string | null, "shopping": string | null}. '
                            'Use "scan" when the user wants a website checked, scanned, audited, tested, or asks if a site '
                            'is ready for AI or AI shoppers; put the website address in "url" exactly as written. '
                            'If they mention something to buy or test buying (a product, category or budget), put a short '
                            'shopping request in "shopping", e.g. "wireless earbuds under ₹10,000"; otherwise null. '
                            'Otherwise use "help".'
                        ),
                    },
                    {"role": "user", "content": redact_text(text)},
                ],
            )
            raw = (r.choices[0].message.content or "").strip()
            if "{" not in raw:
                continue  # empty or truncated answer: ask once more
            raw = raw[raw.find("{") : raw.rfind("}") + 1]
            data = redact(json.loads(raw))
            url = data.get("url") if sites.is_site(data.get("url") or "") else _regex_url(text)
            shopping = (data.get("shopping") or "").strip() or None
            if url:
                # Even if the model said "help", a site in the message means
                # checking it is the more useful answer.
                return Intent("scan", url, shopping)
            return Intent("help")
        except Exception as err:  # model down, bad JSON: fall through to regex
            log.warning("ASI:One intent call failed, using regex: %s", err)

    url = _regex_url(text)
    return Intent("scan", url) if url else Intent("help")


class ScanError(Exception):
    """A scan failed in a way the user should be told about in plain words."""


SERVER_ERROR_RE = re.compile(r"\(HTTP 5\d\d\)|returned an error \(HTTP 5", re.I)


async def wake_site(url: str, attempts: int = 4) -> bool:
    """Load the homepage until it answers, the way a person would retry a slow site.

    Free hosting plans put idle sites to sleep, and a sleeping site answers 502/503
    for up to a minute. Scanning it then would grade the outage, not the website.
    Returns False if the site never came up.
    """
    target = url if url.startswith("http") else f"https://{url}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0), follow_redirects=True) as client:
        for i in range(attempts):
            try:
                r = await client.get(target)
                if r.status_code < 500:
                    return True
            except httpx.HTTPError:
                pass
            if i < attempts - 1:
                await asyncio.sleep(8)
    return False


def had_server_error(scan: dict) -> bool:
    return any(SERVER_ERROR_RE.search(c.get("humanExplanation") or "") for c in scan.get("checks", []))


async def run_scan(url: str, refresh: bool = False) -> dict:
    headers = {"Content-Type": "application/json"}
    if AGENT_API_KEY:
        headers["X-Agent-Key"] = AGENT_API_KEY
    payload = {"url": url, **({"refresh": True} if refresh else {})}
    try:
        async with httpx.AsyncClient(timeout=SCAN_TIMEOUT_S) as client:
            res = await client.post(f"{API_URL}/api/scan", json=payload, headers=headers)
    except httpx.TimeoutException as err:
        raise ScanError("The scanner took too long to answer. Please try again in a minute.") from err
    except httpx.HTTPError as err:
        raise ScanError("I couldn't reach the scanner right now. Please try again shortly.") from err

    try:
        body = res.json()
    except ValueError:
        body = {}
    if res.status_code >= 400 or not body.get("success"):
        # The API already writes its errors for humans ("That doesn't look like
        # a website address", "limit of 5 scans per hour").
        raise ScanError(body.get("error") or f"The scan failed (HTTP {res.status_code}).")
    return body["data"]


_STATUS_ICON = {"fail": "❌", "warning": "⚠️", "pass": "✅"}


def _first_sentence(text: str, limit: int = 220) -> str:
    text = " ".join((text or "").split())
    cut = text.find(". ")
    if 0 < cut < limit:
        return text[: cut + 1]
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@private_output
def format_report(scan: dict) -> str:
    domain = scan.get("domain", "your site")

    if scan.get("noWebsite"):
        return f"I couldn't find a website at **{domain}**. Please check the address and try again."
    if scan.get("unreadable"):
        return (
            f"**{domain}** answered, but I couldn't read any content from it, so I can't grade it. "
            "This often means the site blocks automated visitors, including AI agents."
        )

    checks = [c for c in scan.get("checks", []) if c.get("status") != "skipped"]
    problems = sorted(
        (c for c in checks if c.get("status") in ("fail", "warning")),
        key=lambda c: (c["status"] != "fail", -(c.get("pointsPossible") or 0)),
    )
    passed = [c for c in checks if c.get("status") == "pass"]

    lines = [
        f"## {domain}: grade **{scan.get('overallGrade')}** ({scan.get('overallScore')}/100)",
        "",
        scan.get("summary", ""),
        "",
    ]
    if scan.get("jsRenderWarning"):
        lines += [
            "> ⚠️ Your pages look empty until JavaScript runs. Most AI agents don't run "
            "JavaScript, so they may see almost nothing.",
            "",
        ]
    if problems:
        lines.append("**What to fix first:**")
        for c in problems[:3]:
            icon = _STATUS_ICON.get(c["status"], "•")
            pts = f"{c.get('pointsAwarded', 0)}/{c.get('pointsPossible', 0)} pts"
            lines.append(f"{icon} **{c['title']}** ({pts}): {_first_sentence(c.get('humanExplanation', ''))}")
        lines.append("")
    if passed:
        lines.append("**Already working:** " + ", ".join(c["title"] for c in passed))
        lines.append("")
    if scan.get("partial"):
        lines += ["_Some pages took too long to load, so part of this result is incomplete._", ""]

    lines.append(f"Full report with ready-to-paste fixes: {WEB_URL}/?url={domain}")
    return "\n".join(lines).strip()


async def reply_to(text: str, intent: Intent | None = None) -> tuple[str, str | None]:
    """Return (reply text, scanId or None) for one incoming chat message."""
    intent = intent or await understand(text)
    if intent.action != "scan" or not intent.url:
        return HELP_TEXT, None
    awake = await wake_site(intent.url)
    try:
        scan = await run_scan(intent.url)
        if had_server_error(scan):
            # The site may still have been waking up: give it a moment and look again.
            await asyncio.sleep(10)
            await wake_site(intent.url, attempts=2)
            scan = await run_scan(intent.url, refresh=True)
    except ScanError as err:
        return f"Sorry, I couldn't scan **{intent.url}**: {err}", None

    report = format_report(scan)
    if had_server_error(scan) or not awake:
        report = (
            f"⚠️ **{scan.get('domain', intent.url)} was returning server errors (HTTP 5xx) while I checked**, "
            "so this grade mostly reflects the outage, not your website. This usually means the site was asleep "
            "or overloaded. Please make sure it's up and ask me again.\n\n" + report
        )
    return report, scan.get("scanId")
