"""AgentReady Fixer as a Fetch.ai uAgent speaking the Agent Chat Protocol.

Chat with it in ASI:One:
    "Fix voltkart.onrender.com, code at github.com/me/voltkart-store"   -> tests the site, replies with a fix plan
    "apply all"  or  "apply 1,3"                                         -> makes, tests and opens a pull request
    "retest"                                                             -> runs the shopping test on the live site again

Run it:   python fixer_agent/agent.py
Settings: agents/.env (FIXER_AGENT_SEED, FIXER_ALLOWED_REPOS, GITHUB_TOKEN, ASI1_API_KEY).
"""

from __future__ import annotations



import asyncio
import os
import re
import sys
import time
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

from fixer_agent import fixer, github  # noqa: E402
from shared.privacy import redact_text
from shared import memory, sites, team  # noqa: E402
from tester_agent.shopper import format_report, run as shop  # noqa: E402

PROFILE_README = "fixer.md"
PROFILE_DESCRIPTION = "Fixes online stores so AI shopping agents can buy from them: tests the site, changes the code on GitHub, proves it with a before/after shopping test, and opens a pull request."
JOB_TTL_S = 3600
DRY_RUN = os.getenv("FIXER_DRY_RUN", "false").lower() == "true"  # make and test fixes, but open no pull request

HELP = (
    "I'm the **AgentReady Fixer**. I change your store's code so AI shopping agents can find, compare, add to cart "
    "and hand your customers a checkout link, and I prove it with a before/after shopping test.\n\n"
    "Send me your site and its GitHub repo, for example:\n"
    "- `Fix voltkart.onrender.com, code at github.com/you/your-store`\n"
    "- `Fix github.com/you/your-store for wireless earbuds under ₹10,000`\n\n"
    "I reply with a fix plan; you choose what to apply. Then I open a pull request. Nothing goes live until you merge it."
)

_jobs: dict[str, fixer.Job] = {}
_busy = asyncio.Lock()   # one store build at a time on this machine


def message(text: str, end: bool = True) -> ChatMessage:
    content = [TextContent(type="text", text=redact_text(text))]
    if end:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(content=content)


def _task_from(text: str) -> str:
    t = re.sub(r"https?://\S+|(?:[\w-]+\.)+[a-z]{2,}\S*", " ", text, flags=re.I)
    t = re.sub(r"\b(fix|please|my|store|site|website|shop|code|repo|is|at|on|in|for|the|and|it|github)\b", " ", t, flags=re.I)
    t = " ".join(t.replace(",", " ").split()).strip(" :-.?")
    return t if re.search(r"[a-z]{3,}", t, re.I) else "any product"


def _site_from(text: str) -> str | None:
    return sites.find_site(text)


chat = Protocol(spec=chat_protocol_spec)


@chat.on_message(ChatMessage)
async def handle_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(acknowledged_msg_id=msg.msg_id))
    text = " ".join(i.text for i in msg.content if isinstance(i, TextContent)).strip()
    if not text:
        return
    ctx.logger.info(f"Message from {sender[:16]}…: [message content omitted]")
    if team.is_chatter(text):
        # Long commentary, typically ASI:One's assistant reacting to our last message. Answering it would start a loop.
        ctx.logger.info(f"Ignoring long message from {sender[:16]}… ({len(text)} chars)")
        return
    known = await team.learn(sender, "fixer", text)
    if team.wants_status(text):
        await ctx.send(sender, message(await memory.summary(sender)))
        return
    job = _jobs.get(sender)
    if job and time.time() - job.created > JOB_TTL_S:
        fixer.cleanup(job)
        _jobs.pop(sender, None)
        job = None

    if re.match(r"\s*apply\b", text, re.I):
        if not job:
            await ctx.send(sender, message("There's no fix plan waiting. Send me your site and GitHub repo first.\n\n" + HELP))
            return
        fixes = fixer.pick(job, text)
        if not fixes:
            await ctx.send(sender, message(f"Which fixes? Reply **apply all** or numbers from the plan, e.g. **apply 1,2** (1 to {len(job.plan.fixes)})."))
            return
        await ctx.send(sender, message(
            f"On it: {len(fixes)} fix{'es' if len(fixes) > 1 else ''} for {job.repo.full}. I'll install and test your store before and "
            "after the change. This usually takes 5 to 15 minutes; I'll message you when the pull request is ready.", end=False))
        asyncio.create_task(_apply(ctx, sender, job, fixes))
        return

    if team.wants_merge(text):
        await ctx.send(sender, message(await team.start_merge("fixer", sender, known)))
        return

    if re.match(r"\s*re-?test\b", text, re.I):
        site = _site_from(text) or (job.site if job else None) or known.get("site")
        if not site:
            await ctx.send(sender, message("Which site should I retest? e.g. `retest voltkart.onrender.com`"))
            return
        report = await shop(site, job.task if job else (known.get("task") or _task_from(text)))
        await memory.update(sender, site=site, shop=f"{report.passed}/5 steps on {site}")
        await memory.remember(sender, "Fixer", "agent", f"Retested {site}: {report.passed}/5")
        await ctx.send(sender, message(format_report(report)))
        return

    repo_ref = github.parse_repo(text)
    if not repo_ref and known.get("repo") and (re.match(r"\s*fix\b", text, re.I) or team.refers_to_known_site(text)):
        # "fix my store": the repo was given to any of the three agents earlier.
        repo_ref = tuple(known["repo"].split("/", 1))
    if not repo_ref:
        if await team.first_in_a_while(sender, "help"):
            await ctx.send(sender, message(HELP))
        return
    owner, name = repo_ref
    if not github.allowed(owner, name):
        await ctx.send(sender, message(
            f"github.com/{owner}/{name} isn't connected to AgentReady yet, so I can't change its code. "
            "Ask the AgentReady team to connect it, or try one of our demo stores."))
        return
    try:
        repo = await github.get_repo(owner, name)
    except github.GitHubError as err:
        await ctx.send(sender, message(str(err)))
        return
    site = _site_from(text) or repo.homepage or known.get("site")
    if not site:
        await ctx.send(sender, message(f"What's the live address of the store in {repo.full}? e.g. `Fix mystore.com, code at {repo.url}`"))
        return
    task = _task_from(text)
    if task == "any product":
        task = known.get("task") or task
    await ctx.send(sender, message(f"Reading {repo.full} and testing {site} like an AI shopper (“{task}”). About a minute…", end=False))
    try:
        new = await fixer.diagnose(site, owner, name, task)
    except Exception as err:
        ctx.logger.exception("Diagnose failed")
        await ctx.send(sender, message(f"Sorry, I couldn't prepare a fix plan: {err}"))
        return
    if job:
        fixer.cleanup(job)
    _jobs[sender] = new
    await team.set_site(sender, site, repo.full)
    await memory.update(sender, site=site, repo=repo.full, task=task, shop=f"{new.live['passed']}/5 steps on {site}", fix="plan ready, waiting for your go-ahead", fix_state="waiting", fix_site=site)
    await ctx.send(sender, message(new.live_text + "\n\n---\n\n" + fixer.plan_text(new)))


async def _apply(ctx: Context, sender: str, job: fixer.Job, fixes):
    async def step(text: str):
        await ctx.send(sender, message(f"⏳ {text}", end=False))

    await team.tell_user(ctx, "fixer", sender, await _apply_fixes(ctx, sender, job, fixes, on_step=step))


async def _apply_fixes(ctx: Context, sender: str, job: fixer.Job, fixes, team_flow: bool = False, on_step=None) -> str:
    """Make, test and propose the fixes; returns the message for the user. on_step(text) shows each step live."""
    async def progress(step: str):
        ctx.logger.info(f"[{job.repo.full}] {step}")
        await memory.update(sender, fix=step)
        if on_step:
            await on_step(step)

    try:
        async with _busy:
            res = await fixer.apply(job, fixes, progress, open_pr=not DRY_RUN)
    except Exception as err:
        ctx.logger.exception("Apply failed")
        await memory.update(sender, fix=f"failed: {type(err).__name__}", fix_state="failed")
        return f"Sorry, fixing {job.repo.full} failed unexpectedly ({type(err).__name__}: {err})."
    finally:
        _jobs.pop(sender, None)
        fixer.cleanup(job)
    if res.pr_url and res.draft:
        await memory.update(sender, fix="draft pull request open, needs review", fix_state="draft", pr_url=res.pr_url)
    elif res.pr_url:
        await memory.update(sender, fix="pull request open, waiting for you to merge", fix_state="done", pr_url=res.pr_url)
    else:
        await memory.update(sender, fix="no pull request", fix_state="failed")
    if res.after:
        await memory.update(sender, shop=f"{res.after['passed']}/5 steps on {job.site} after the fix")
    head = ("📝 Draft pull request" if res.draft else "✅ Pull request ready") if res.pr_url else "⚠️ No pull request"
    lines = [f"## {head}: {job.repo.full}", ""]
    if res.before and res.after:
        lines += [f"**AI shopping test: {res.before['passed']}/5 → {res.after['passed']}/5**", "", "| Step | Before | After |", "|---|---|---|"]
        lines += [f"| {b['name']} | {'✅' if b['passed'] else '❌'} | {'✅' if a['passed'] else '❌'} |"
                  for b, a in zip(res.before["steps"], res.after["steps"])]
        lines.append("")
    if res.message:
        lines += [res.message, ""]
    if res.summary:
        lines += ["**What I changed**", res.summary, ""]
    if res.pr_url and res.draft:
        lines += [f"Review it here: {res.pr_url}", "",
                  "It's a draft because the change isn't a clean win yet, so I won't merge it. Finish it on GitHub, or say **fix it** to have me try again."]
    elif res.pr_url and team_flow:
        lines += [f"Review it here: {res.pr_url}", "",
                  "Reply **merge** and I'll merge it. Once the site has redeployed, the Tester checks it live and you get the result here."
                  if not team.AUTO_MERGE else "Merging it now; the Tester will check the live site after the deploy."]
    elif res.pr_url:
        lines += [f"Review it here: {res.pr_url}", "",
                  f"Reply **merge** to merge it, or merge it yourself and send **retest**; I'll check {job.site} live."]
    return "\n".join(lines)


async def fix_for_team(ctx: Context, task: dict):
    """A task another agent handed over: diagnose, then fix and open the pull request without being asked again."""
    user, repo, by = task["user"], task["repo"], task.get("by")
    owner, _, name = repo.partition("/")
    site = task.get("site") if sites.is_site(task.get("site") or "") else None
    ctx.logger.info(f"Team task {task['id']}: fix {repo} for {site or '(site from repo)'}, asked by {by}")

    async def tell(text: str, final: bool = True):
        await team.tell_user(ctx, "fixer", user, text, via=by, final=final)

    async def step(text: str):
        await tell(f"⏳ **Fixer:** {text}", final=False)

    async def fail(text: str):
        ctx.logger.warning(f"Team task {task['id']}: {text}")
        await memory.update(user, fix=f"failed: {text}", fix_state="failed")
        await tell(f"**Fixer:** {text}")

    await memory.update(user, fix="working", fix_state="working", fix_site=site, fix_at=time.time())
    if not site:
        try:
            homepage = (await github.get_repo(owner, name)).homepage
        except Exception as err:
            await fail(f"I can't open {repo}: {err}")
            return
        site = homepage if sites.is_site(homepage or "") else None
        if not site:
            await fail(f"I don't know where {repo} is live. Send me the site address, e.g. `mystore.com`, then say **fix it**.")
            return
        await memory.update(user, site=site, fix_site=site)
    await tell(f"⏳ **Fixer:** I'm on it. Reading the code of {repo} and testing {site}.", final=False)
    try:
        job = await fixer.diagnose(site, owner, name, task["task"])
    except Exception as err:
        ctx.logger.exception("Team diagnose failed")
        await fail(f"I couldn't prepare a fix for {repo}: {err}")
        return
    ctx.logger.info(f"Team task {task['id']}: live test {job.live['passed']}/5, stack {job.stack.label}, {len(job.plan.fixes)} fix(es)")
    if not job.plan.fixes:
        await memory.update(user, fix="nothing to fix in code", fix_state="failed")
        await tell(job.live_text + "\n\n---\n\n" + fixer.plan_text(job))
        fixer.cleanup(job)
        return
    await tell(f"⏳ **Fixer:** plan ready, {len(job.plan.fixes)} fix(es) for a {job.stack.label} store: "
               + "; ".join(fx.title for fx in job.plan.fixes)
               + ". Applying them and testing the patched store; this usually takes 5 to 15 minutes.", final=False)
    result = await _apply_fixes(ctx, user, job, list(job.plan.fixes), team_flow=True, on_step=step)
    ctx.logger.info(f"Team task {task['id']}: done: {result.splitlines()[0]}")
    known = await memory.facts(user)
    await tell(result, final=not (team.AUTO_MERGE and known.get("fix_state") == "done"))
    if team.AUTO_MERGE and known.get("fix_state") == "done":
        await memory.update(user, fix_state="merging")
        await merge_for_team(ctx, {**task, "site": site, "pr_url": known["pr_url"]})


async def merge_for_team(ctx: Context, task: dict):
    """Merge the Fixer's pull request, then ask AgentReady to have the Tester check the live site."""
    user, repo, pr_url, by = task["user"], task["repo"], task["pr_url"], task.get("by")
    owner, _, name = repo.partition("/")
    number = github.pr_number(pr_url)
    try:
        if not number:
            raise github.GitHubError(f"{pr_url} isn't a pull request link.")
        await github.merge_pr(owner, name, number)
    except Exception as err:
        ctx.logger.exception("Merge failed")
        await memory.update(user, fix=f"merge failed: {err}", fix_state="done")
        await team.tell_user(ctx, "fixer", user, f"**Fixer:** I couldn't merge {pr_url}: {err}", via=by)
        return
    await memory.update(user, fix="merged, waiting for the deploy", fix_state="merged")
    await team.tell_user(ctx, "fixer", user, f"⏳ **Fixer:** merged {pr_url}. Waiting for {task['site']} to redeploy; "
                         "then the Tester shops on the live site and you get the result here.", via=by, final=False)
    await memory.post_task("agentready", "verify", user, site=task["site"], task=task.get("task"))


@chat.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


def create_agent(mailbox: bool | None = None, port: int | None = None) -> Agent:
    seed = os.getenv("FIXER_AGENT_SEED")
    if not seed:
        raise RuntimeError("Set FIXER_AGENT_SEED in agents/.env (any long secret phrase).")
    if mailbox is None:
        mailbox = os.getenv("AGENT_MAILBOX", "false").lower() == "true"
    port = port or int(os.getenv("FIXER_PORT", "8003"))
    kwargs = dict(name="AgentReady Fixer", seed=seed, port=port)
    if mailbox:
        kwargs.update(mailbox=True, publish_agent_details=True, description=PROFILE_DESCRIPTION)
    else:
        kwargs.update(endpoint=[f"http://127.0.0.1:{port}/submit"])
    agent = Agent(**kwargs)
    # Read as UTF-8: uAgents' own readme_path uses the platform encoding, which breaks on ₹ on Windows.
    agent._readme = (ROOT / "profiles" / PROFILE_README).read_text(encoding="utf-8")
    agent.include(chat, publish_manifest=mailbox)

    @agent.on_interval(period=3.0)
    async def pick_up_team_tasks(ctx: Context):
        if not _busy.locked():
            for user, job in list(_jobs.items()):
                if time.time() - job.created > JOB_TTL_S:
                    _jobs.pop(user, None)
                    fixer.cleanup(job)
        await team.pump(ctx, "fixer", {"fix": fix_for_team, "merge": merge_for_team})

    return agent


if __name__ == "__main__":
    create_agent().run()
