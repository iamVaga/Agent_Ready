"""Run the Tester from the command line.

    python tools/run_tester.py https://example-store.com "running shoes under ₹10,000"
    python tools/run_tester.py --demo            # all five demo stores
    python tools/run_tester.py --demo --json results.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.privacy import privacy_print as print  # Redact CLI output at the boundary.

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from tester_agent.shopper import format_report, run  # noqa: E402

# The demo stores and a realistic shopping request for each.
DEMO = [
    ("https://stride-lab-beta.vercel.app", "running shoes under ₹10,000"),
    ("https://nest-and-grain.vercel.app", "a dining chair under ₹10,000"),
    ("https://masala-mandi.onrender.com", "garam masala under ₹500"),
    ("https://tara-handmade.vercel.app", "a pearl necklace under ₹6,000"),
    ("https://voltkart.onrender.com", "wireless earbuds under ₹10,000"),
]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site", nargs="?")
    ap.add_argument("task", nargs="?", default="")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args()

    jobs = DEMO if args.demo else [(args.site, args.task)]
    if not jobs[0][0]:
        ap.error("give a site and task, or --demo")
    reports = []
    for site, task in jobs:
        r = await run(site, task)
        reports.append(r)
        print(format_report(r))
        print("\n" + "=" * 80 + "\n")

    if len(reports) > 1:
        print("SUMMARY")
        for r in reports:
            print(f"  {r.passed}/5  {r.site}  ({'; '.join(('✓' if s.passed else '✗') + s.name for s in r.steps)})")
    if args.json:
        Path(args.json).write_text(json.dumps([r.to_dict() for r in reports], indent=2, ensure_ascii=False), encoding="utf-8")


asyncio.run(main())
