"""Run the Fixer from the command line.

    python tools/run_fixer.py <site> <github repo> "<shopping task>"            # plan only
    python tools/run_fixer.py <site> <github repo> "<task>" --apply all --dry   # fix and test, no PR
    python tools/run_fixer.py <site> <github repo> "<task>" --apply 1,3         # fix, test, open a PR
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.privacy import privacy_print as print  # Redact CLI output at the boundary.
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from shared.privacy import redact, redact_text

from fixer_agent import fixer, github  # noqa: E402


async def say(text: str):
    print(redact_text(f"… {text}"), flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site")
    ap.add_argument("repo")
    ap.add_argument("task", nargs="?", default="any product")
    ap.add_argument("--apply")
    ap.add_argument("--dry", action="store_true", help="make and test the changes, but open no pull request")
    ap.add_argument("--keep", action="store_true", help="keep the working copy for inspection")
    a = ap.parse_args()

    owner, name = github.parse_repo(a.repo) or (None, None)
    job = await fixer.diagnose(a.site, owner, name, a.task, say)
    print(job.live_text, "\n")
    print(fixer.plan_text(job), "\n")
    if not a.apply:
        fixer.cleanup(job)
        return
    res = await fixer.apply(job, fixer.pick(job, a.apply), say, open_pr=not a.dry)
    print("\nRESULT:", "ok" if res.ok else "not ok", res.message)
    if res.before and res.after:
        print(f"Before {res.before['passed']}/5 → after {res.after['passed']}/5")
        for b, s in zip(res.before["steps"], res.after["steps"]):
            print(f"  {b['name']:<16} {'✅' if b['passed'] else '❌'} → {'✅' if s['passed'] else '❌'}  {s['detail'][:110]}")
    print("Files:", ", ".join(res.files))
    print("Summary:\n" + res.summary)
    if res.pr_url:
        print("Pull request:", res.pr_url)
    out = ROOT / "results" / f"fixer-{name}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(redact({"before": res.before, "after": res.after, "files": res.files, "summary": res.summary,
                               "pr": res.pr_url, "diff": res.diff}), ensure_ascii=False, indent=1), encoding="utf-8")
    print("Saved", out, "| working copy:", job.ws)
    if not a.keep:
        fixer.cleanup(job)


if __name__ == "__main__":
    asyncio.run(main())
