"""The Fixer's job, start to finish.

    diagnose()  clone the store's repo, work out its stack, let the Tester shop on the
                live site, and turn what failed into a plan of fixes for the owner to pick.
    apply()     run the original store locally and test it (before), let ASI:One make the
                chosen fixes, rebuild and test again (after), retry if needed, then open a
                pull request with the before/after proof. Nothing is merged or deployed.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from shared.privacy import private_output, redact_text

from tester_agent.shopper import Report, format_report, run as shop

from . import coder, github, playbook, workspace
from .playbook import Fix, Plan
from .workspace import SKIP_DIRS, Server, Stack

WORK_DIR = Path(os.getenv("FIXER_WORK_DIR") or Path(tempfile.gettempdir()) / "agentready-fixer")
NL = chr(10)
ROUNDS = int(os.getenv("FIXER_ROUNDS", "3"))

Progress = Callable[[str], Awaitable[None]]


async def _quiet(_: str):
    pass


@dataclass
class Job:
    site: str
    task: str
    repo: github.Repo
    ws: Path
    stack: Stack
    live: dict
    live_text: str
    plan: Plan
    created: float = field(default_factory=time.time)


@dataclass
class Result:
    ok: bool
    message: str
    before: dict | None = None
    after: dict | None = None
    pr_url: str | None = None
    branch: str | None = None
    files: list[str] = field(default_factory=list)
    summary: str = ""
    diff: str = ""
    draft: bool = False  # opened as a draft: the change didn't build, broke pages, or didn't improve the test


def _rmtree(p: Path):
    resolved = p.resolve()
    if p.is_symlink() or resolved.parent != WORK_DIR.resolve() or not p.name.startswith("job-"):
        raise ValueError("Refusing cleanup outside managed temporary jobs")
    def onerror(func, path, _):
        os.chmod(path, stat.S_IWRITE)
        func(path)
    shutil.rmtree(p, onerror=onerror)


async def diagnose(site: str, owner: str, name: str, task: str, progress: Progress = _quiet) -> Job:
    repo = await github.get_repo(owner, name)
    if not repo.can_push:
        raise github.GitHubError(f"I can read {repo.full} but can't push to it. Give the Fixer write access (add it as a collaborator) and try again.")
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    import uuid
    ws = WORK_DIR / ("job-" + uuid.uuid4().hex)
    await progress(f"Reading the code of {repo.full} and testing {site} live…")
    clone = asyncio.to_thread(github.clone, repo, ws)
    tasks = [asyncio.create_task(shop(site, redact_text(task))), asyncio.create_task(clone)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    failure = next((r for r in results if isinstance(r, BaseException)), None)
    if failure:
        if ws.exists(): _rmtree(ws)
        raise failure
    live = results[0]
    stack = workspace.detect(ws)
    live_d = live.to_dict()
    return Job(site, redact_text(task), repo, ws, stack, live_d, format_report(live), playbook.plan(live_d, stack))


@private_output
def plan_text(job: Job) -> str:
    lines = [f"## Fix plan for {job.repo.full}", "",
             f"Stack: **{job.stack.label}**. Live shopping test: **{job.live['passed']} / 5 steps**.", ""]
    if not job.plan.fixes:
        lines.append("Nothing for me to fix: the shopping test found no problems I can solve in code.")
    for i, f in enumerate(job.plan.fixes, 1):
        lines.append(f"{i}. **{f.title}**{' (I will check first, it may already work)' if f.check_first else ''}  \n   {f.why}")
    for n in job.plan.notes:
        lines += ["", f"ℹ️ {n}"]
    if job.plan.fixes:
        lines += ["", "Reply **apply all**, or pick some, e.g. **apply 1,3**. I'll make the changes, test the patched store "
                      "with the Tester, and open a pull request for you to review. Nothing goes live until you merge it."]
    return "\n".join(lines)


def pick(job: Job, text: str) -> list[Fix]:
    if re.search(r"\ball\b", text, re.I):
        return list(job.plan.fixes)
    nums = {int(n) for n in re.findall(r"\d+", text)}
    return [f for i, f in enumerate(job.plan.fixes, 1) if i in nums]


async def _test_local(ws: Path, stack: Stack, site_task: str) -> tuple[Report, list[str]]:
    """The Tester's run on the local store, plus any broken stylesheet, script or image on the pages it used."""
    async with Server(ws, stack) as srv:
        report = await shop(srv.url, site_task)
        return report, await _broken_assets(srv.url, report)


def _broken_text(broken: list[str]) -> str:
    if not broken:
        return ""
    return NL + NL + "BROKEN for human visitors (stylesheets, scripts or images that don't load):" + NL + NL.join("- " + b for b in broken)


async def _broken_assets(origin: str, report: Report) -> list[str]:
    """Humans still use these pages: a stylesheet that 404s passes the Tester but breaks the shop."""
    pages = [origin + "/"] + [p["url"] for p in report.compared] + ([report.checkout_url] if report.checkout_url else [])
    broken: list[str] = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
        for page in dict.fromkeys(pages):
            try:
                r = await c.get(page)
            except httpx.HTTPError:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            refs = [t.get("href") for t in soup.find_all("link", rel=lambda v: v and "stylesheet" in v)]
            refs += [t.get("src") for t in soup.find_all(["script", "img"])]
            for ref in dict.fromkeys(filter(None, refs)):
                url = urljoin(str(r.url), ref)
                if not url.startswith(origin):
                    continue  # other sites' assets (fonts, CDNs) aren't ours to check
                try:
                    status = (await c.get(url)).status_code
                except httpx.HTTPError:
                    status = 0
                if status >= 400 or status == 0:
                    broken.append(f"{urlparse(page).path or '/'} loads {urlparse(url).path} → HTTP {status or 'no response'}")
    return broken[:20]


def _drop_unused_scripts(ws: Path, ran: set[str] = frozenset()) -> list[str]:
    """Delete new scripts the model wrote for itself (debug helpers) that nothing in the repo uses."""
    # A script the model ran is how its generated files get rebuilt: always kept.
    scripts = [f for f in github.untracked_files(ws) if "scripts/" in f and Path(f).suffix in (".mjs", ".js", ".cjs", ".py")]
    # Debugging helpers never belong in the pull request, even if the model ran them.
    debug = [f for f in scripts if re.match(r"(debug|probe|tmp|temp|scratch|check|inspect)[_-]", Path(f).name, re.I)]
    for f in debug:
        (ws / f).unlink(missing_ok=True)
    new = [f for f in scripts if f not in debug and f not in ran]
    if not new:
        return debug
    texts = []
    for rel in workspace.file_tree(ws, 3000):
        if rel.endswith("(binary)") or rel == "…" or rel in new:
            continue
        try:
            texts.append((ws / rel).read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            pass
    corpus = "\n".join(texts)
    dropped = [f for f in new if Path(f).name not in corpus]
    for f in dropped:
        (ws / f).unlink(missing_ok=True)
    return debug + dropped


def _exclude_build_dirs(ws: Path):
    """Make sure installs and builds never end up in the commit, even if .gitignore misses them."""
    excl = ws / ".git" / "info" / "exclude"
    excl.parent.mkdir(parents=True, exist_ok=True)
    junk = ["*.log", "*.sqlite3", "*.sqlite", "*.db", "*.pyc", ".env", ".env.*"]
    excl.write_text("\n".join([f"{d}/" for d in sorted(SKIP_DIRS - {".git"})] + junk) + "\n", encoding="utf-8")


async def apply(job: Job, fixes: list[Fix], progress: Progress = _quiet, open_pr: bool = True) -> Result:
    ws, stack = job.ws, job.stack
    if stack.kind == "unknown":
        return Result(False, "I couldn't recognise how this store is built, so I can't run and test it safely. "
                             "I can fix stores built with Next.js, Vite + React/Vue/Svelte, Node/Express, Flask, Django or plain HTML.")
    _exclude_build_dirs(ws)

    await progress(f"Installing the store ({stack.label})…")
    ok, out = await workspace.install(ws, stack)
    if not ok:
        return Result(False, "Installing the store's dependencies failed:\n```\n" + out[-1200:] + "\n```")
    ok, out = await workspace.build(ws, stack)
    if not ok:
        return Result(False, "The store doesn't build as it is, before any change of mine:\n```\n" + out[-1200:] + "\n```")
    await progress("Running the original store locally and testing it (before)…")
    before, _ = await _test_local(ws, stack, job.task)
    if before.passed < job.live["passed"]:
        # The local copy is worse than the live site (missing data, settings or services): testing changes
        # against it would prove nothing, so stop instead of "fixing" a broken environment.
        failed = next((s for s in before.steps if not s.passed), None)
        return Result(False, f"My local copy of the store doesn't behave like the live site ({before.passed}/5 locally, "
                             f"{job.live['passed']}/5 live; locally: {failed.detail if failed else 'unknown'}), so I can't test "
                             "changes reliably. This usually means it needs data or settings that only exist on the server.",
                      before.to_dict())

    last: dict = {"report": before}

    async def build():
        return await workspace.build(ws, stack)

    async def test() -> str:
        ok, out = await workspace.build(ws, stack)
        if not ok:
            return "Build FAILED, so the store can't be tested:\n" + out
        try:
            report, broken = await _test_local(ws, stack, job.task)
        except RuntimeError as err:
            return str(err)
        last["report"] = report
        return format_report(report) + _broken_text(broken)

    targets = {s for f in fixes for s in f.steps}
    session = coder.Session(ws, stack, build, test, progress)
    after: Report | None = None
    feedback = ""
    broken: list[str] = []
    for round_no in range(1, ROUNDS + 1):
        await progress(f"Making the changes (round {round_no} of up to {ROUNDS})…" if round_no > 1 else "Making the changes with ASI:One…")
        extra = feedback
        if after is not None and not feedback:
            still = [s for s in after.steps if s.name in targets and not s.passed]
            extra = ("Your previous changes are still in the repo. The Tester's latest run on the patched store:\n"
                     + format_report(after) + "\n\nThese selected steps still fail: " + ", ".join(s.name for s in still)
                     + ". Find out why and fix it.")
        session.summary, feedback = "", ""
        await coder.work(session, fixes, format_report(before), extra)
        if not github.changed_files(ws):
            continue
        await progress("Checking the result: building and testing the patched store…")
        ok, out = await workspace.build(ws, stack)
        if not ok:
            after = None
            feedback = "Your changes are still in the repo, but the build now FAILS:\n" + out + "\nFix the build first."
            continue
        after, broken = await _test_local(ws, stack, job.task)
        if broken:
            feedback = ("Your changes are still in the repo. The Tester's latest run:" + NL + format_report(after)
                        + _broken_text(broken) + NL + "Fix these broken files: humans use these pages too.")
            continue
        if all(s.passed for s in after.steps if s.name in targets):
            break

    dropped = _drop_unused_scripts(ws, {l[4:] for l in session.log if l.startswith("ran ")})
    if dropped:
        await progress("Removed leftover helper scripts: " + ", ".join(dropped))
    files = github.changed_files(ws)
    if not files:
        return Result(False, "I couldn't find a change that works, so I didn't touch your code.", before.to_dict())
    # Code changed but it isn't a clean win: still propose it, as a draft that can't be merged by accident.
    draft = None
    if after is None:
        draft = "My changes don't build yet."
    elif broken:
        draft = "My changes broke files that human visitors need:\n" + "\n".join(f"- {b}" for b in broken)
    elif after.passed <= before.passed:
        draft = f"My changes didn't improve the shopping test ({before.passed}/5 before, {after.passed}/5 after)."
    shown = after or before
    result = Result(not draft, draft or "", before.to_dict(), after.to_dict() if after else None, files=files,
                    summary=session.summary, diff=redact_text(github.diff(ws)), draft=bool(draft))
    if not open_pr:
        result.message = (draft + " " if draft else "") + "Dry run: changes made and tested locally, no pull request opened."
        return result
    await progress("Opening a draft pull request…" if draft else "Opening a pull request…")
    branch = f"agentready/fix-{time.strftime('%Y%m%d-%H%M')}"
    title = (f"Draft: AI shopping agent fixes ({before.passed}/5 → {after.passed if after else '?'}/5), needs review" if draft
             else f"Make the store usable by AI shopping agents ({before.passed}/5 → {after.passed}/5)")
    body = pr_body(job, fixes, before, shown, session.summary)
    if draft:
        body = f"> ⚠️ **Draft, needs review.** {draft} The changes are here so you can see and finish them.\n\n" + body
    await asyncio.to_thread(github.commit_and_push, ws, branch, title)
    result.pr_url = await github.open_pr(job.repo, branch, title, body, draft=bool(draft))
    result.branch = branch
    return result


def _row(b, a) -> str:
    mark = lambda s: "✅" if s.passed else "❌"
    return f"| {b.name} | {mark(b)} | {mark(a)} |"


@private_output
def pr_body(job: Job, fixes: list[Fix], before: Report, after: Report, summary: str) -> str:
    rows = "\n".join(_row(b, a) for b, a in zip(before.steps, after.steps))
    picked = "\n".join(f"- **{f.title}**: {f.why}" for f in fixes)
    after_detail = "\n".join(f"{'✅' if s.passed else '❌'} **{s.name}**: {s.detail}" for s in after.steps)
    return f"""Opened by the **AgentReady Fixer** agent.

AI shopping agents (ChatGPT, Claude, Perplexity, Gemini…) read your site's HTML and mostly don't run JavaScript.
The AgentReady Tester tried to shop on this store: *“{job.task}”*.

## Result: {before.passed}/5 → {after.passed}/5

| Step | Before | After |
|---|---|---|
{rows}

Both runs used the same local build of this repo (before and after this change) and the same Tester.

## What was fixed
{picked}

## Changes
{summary or '- See the diff.'}

## Tester report after the change
{after_detail}

Please review and test before merging. After it is deployed, ask the Fixer to **retest** {job.site} to confirm it live.
"""


def cleanup(job: Job):
    try:
        _rmtree(job.ws)
        log = job.ws.parent / f"{job.ws.name}-server.log"
        if log.is_file() and not log.is_symlink(): log.unlink()
    except OSError:
        pass
    job.live.clear()
    job.live_text = ""
    job.task = ""
