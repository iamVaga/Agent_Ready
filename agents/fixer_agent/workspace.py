"""A store's code on disk: work out its stack, install it, build it and run it locally.

The Fixer uses this to prove a change works before opening a pull request: it
starts the patched store on this machine and lets the Tester shop on it.
Only fixed, known commands run here (install, build, start). The coding model
cannot run commands of its own.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from shared.privacy import redact_text

SKIP_DIRS = {".git", "node_modules", ".next", "dist", "build", ".venv", "venv", "__pycache__", ".vercel", "coverage", ".turbo"}
TEXT_EXT = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".json", ".html", ".htm", ".ejs", ".hbs", ".njk", ".css", ".py", ".txt",
            ".md", ".toml", ".yaml", ".yml", ".xml", ".jinja", ".jinja2", ".j2", ".svelte", ".vue", ".astro", ".liquid", ".php", ".rb", ".erb"}


def _exe(name: str) -> str:
    found = shutil.which(name) or shutil.which(name + ".cmd")
    if not found and os.name == "nt":
        cand = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / f"{name}.cmd"
        found = str(cand) if cand.exists() else None
    return found or name


@dataclass
class Stack:
    kind: str            # nextjs | vite-spa | node-server | flask | django | static | unknown
    label: str           # human description, e.g. "Next.js 16 (App Router)"
    install: list[list[str]] = field(default_factory=list)
    build: list[str] | None = None
    start: list[str] | None = None   # {port} is replaced
    notes: list[str] = field(default_factory=list)
    root: str = ""       # sub-folder holding the store in a monorepo, e.g. "frontend"


# Where the storefront usually lives in a repo that also holds a backend or several apps.
APP_DIRS = ("frontend", "web", "client", "site", "storefront", "www", "app", "apps/web", "apps/storefront", "packages/web")


def app_dir(ws: Path, stack: Stack) -> Path:
    return ws / stack.root if stack.root else ws


def detect(ws: Path) -> Stack:
    """The store's stack, at the top of the repo or in the sub-folder that holds the site."""
    stack = _detect_dir(ws)
    if stack.kind != "unknown":
        return stack
    subdirs = [d for d in ws.iterdir() if d.is_dir() and d.name not in SKIP_DIRS and not d.name.startswith(".")]
    subdirs += [d for parent in ("apps", "packages") if (ws / parent).is_dir() for d in (ws / parent).iterdir() if d.is_dir()]
    deployed = lambda d: any((d / f).exists() for f in ("vercel.json", "netlify.toml", "render.yaml"))  # noqa: E731
    named = lambda d: (d.relative_to(ws).as_posix() in APP_DIRS)  # noqa: E731
    for d in sorted(subdirs, key=lambda d: (not deployed(d), not named(d), d.name)):
        sub = _detect_dir(d)
        if sub.kind == "unknown" or (sub.kind == "node-server" and not deployed(d) and not named(d)):
            continue
        sub.root = d.relative_to(ws).as_posix()
        sub.label += f", in `{sub.root}/`"
        sub.notes.append(f"The storefront is in `{sub.root}/` (the repo holds more than one app): make changes there.")
        return sub
    return stack


def _detect_dir(ws: Path) -> Stack:
    pkg_path = ws / "package.json"
    if pkg_path.exists():
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        scripts = pkg.get("scripts", {})
        npm = _exe("npm")
        install = [[npm, "ci" if (ws / "package-lock.json").exists() else "install", "--no-audit", "--no-fund"]]
        build = [npm, "run", "build"] if "build" in scripts else None
        if "next" in deps:
            app_router = (ws / "src" / "app").exists() or (ws / "app").exists()
            return Stack("nextjs", f"Next.js {deps['next'].lstrip('^~')} ({'App Router' if app_router else 'Pages Router'})",
                         install, build, [npm, "run", "start", "--", "-p", "{port}"])
        if "vite" in deps and "preview" in scripts:
            ui = "React" if "react" in deps else "Vue" if "vue" in deps else "Svelte" if "svelte" in deps else "JS"
            return Stack("vite-spa", f"Vite + {ui} single-page app (renders in the browser)", install, build,
                         [npm, "run", "preview", "--", "--port", "{port}", "--strictPort", "--host", "127.0.0.1"],
                         ["Hosted as static files: there is no server code at request time."])
        if "start" in scripts:
            fw = next((f for f in ("express", "fastify", "koa", "hapi") if f in deps or f"@{f}/core" in deps), "Node")
            views = next((v for v in ("ejs", "pug", "handlebars", "nunjucks") if v in deps or f"express-{v}" in deps), None)
            return Stack("node-server", f"{fw.title()} server" + (f" with {views.upper()} templates" if views else ""),
                         install, build, [npm, "run", "start"])
    req = ws / "requirements.txt"
    if req.exists() or (ws / "pyproject.toml").exists():
        text = req.read_text(encoding="utf-8", errors="ignore").lower() if req.exists() else ""
        py = str(ws / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python")
        install = [[sys.executable, "-m", "venv", str(ws / ".venv")]]
        if req.exists():
            install.append([py, "-m", "pip", "install", "-q", "-r", "requirements.txt"])
        if "django" in text or (ws / "manage.py").exists():
            return Stack("django", "Django", install, None, [py, "manage.py", "runserver", "127.0.0.1:{port}", "--noreload"])
        if "flask" in text:
            module = next((m for m in ("app", "wsgi", "main", "server") if (ws / f"{m}.py").exists()), "app")
            return Stack("flask", "Flask with Jinja templates", install, None,
                         [py, "-m", "flask", "--app", module, "run", "--port", "{port}", "--host", "127.0.0.1"])
    if (ws / "index.html").exists():
        return Stack("static", "Static HTML, CSS and JavaScript files", [], None,
                     [sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1", "--directory", str(ws)],
                     ["Hosted as static files: there is no server code at request time."])
    return Stack("unknown", "Unrecognised stack")


def file_tree(ws: Path, limit: int = 400) -> list[str]:
    out = []
    for p in sorted(ws.rglob("*")):
        rel = p.relative_to(ws)
        if any(part in SKIP_DIRS for part in rel.parts) or p.is_dir():
            continue
        if p.name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml"):
            continue
        out.append(rel.as_posix() + ("" if p.suffix.lower() in TEXT_EXT else "  (binary)"))
        if len(out) >= limit:
            out.append("…")
            break
    return out


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def execution_allowed() -> bool:
    # Set only by an operator inside a disposable sandbox without host mounts,
    # credentials or internal-network access. Environment scrubbing is not isolation.
    return os.getenv("FIXER_ISOLATED_EXECUTION") == "1"


def child_env() -> dict:
    keys = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG"}
    return {k: v for k, v in os.environ.items() if k.upper() in keys}


async def _run(cmd: list[str], cwd: Path, timeout: int, env: dict | None = None) -> tuple[int, str]:
    if not execution_allowed():
        return 126, "Repository execution disabled: run Fixer in an isolated sandbox and set FIXER_ISOLATED_EXECUTION=1."
    def go():
        try:
            out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=timeout, env={**child_env(), **(env or {})})
            return out.returncode, (out.stdout + "\n" + out.stderr)
        except subprocess.TimeoutExpired:
            return 124, f"Timed out after {timeout}s"
    return await asyncio.to_thread(go)


async def install(ws: Path, stack: Stack) -> tuple[bool, str]:
    for cmd in stack.install:
        code, out = await _run(cmd, app_dir(ws, stack), 600)
        if code != 0:
            return False, out[-3000:]
    return True, ""


async def build(ws: Path, stack: Stack) -> tuple[bool, str]:
    """Build the store. For Python stores, at least check every file compiles."""
    if stack.build:
        code, out = await _run(stack.build, app_dir(ws, stack), 600, {"NODE_ENV": "production", "NEXT_TELEMETRY_DISABLED": "1"})
        return code == 0, _tail_errors(out)
    if stack.kind in ("flask", "django"):
        code, out = await _run([sys.executable, "-m", "compileall", "-q", "-x", r"[\\/]\.venv[\\/]", "."], app_dir(ws, stack), 120)
        if code != 0:
            return False, out[-3000:]
        if stack.kind == "django":
            # Many stores load their catalogue in a data migration: without it the local copy is empty.
            py = str(app_dir(ws, stack) / ".venv" / ("Scripts" if os.name == "nt" else "bin") / "python")
            code, out = await _run([py, "manage.py", "migrate", "--noinput"], app_dir(ws, stack), 300)
            return code == 0, out[-3000:]
        return True, ""
    return True, ""


def _tail_errors(out: str) -> str:
    lines = [l for l in out.splitlines() if l.strip()]
    errs = [l for l in lines if re.search(r"error|failed|cannot|unexpected|not found|TS\d{4}", l, re.I)]
    return "\n".join((errs[:40] or lines[-40:]))[-4000:]


class Server:
    """The store running locally on a free port, for the Tester to shop on."""

    def __init__(self, ws: Path, stack: Stack):
        self.ws, self.stack = ws, stack
        self.port = _free_port()
        self.proc: subprocess.Popen | None = None
        self.log_path = ws.parent / f"{ws.name}-server.log"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def __aenter__(self) -> "Server":
        if not execution_allowed():
            raise RuntimeError("Repository execution requires an isolated sandbox")
        if not self.stack.start:
            raise RuntimeError("I don't know how to start this store locally.")
        cmd = [c.replace("{port}", str(self.port)) for c in self.stack.start]
        # No NODE_ENV=production here: many apps then mark cookies "secure", which browsers (and the
        # Tester) drop over plain http on 127.0.0.1. Frameworks like Next.js set it themselves.
        env = child_env()
        env.update({"PORT": str(self.port), "FLASK_DEBUG": "0", "NEXT_TELEMETRY_DISABLED": "1"})

        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        self.proc = subprocess.Popen(cmd, cwd=app_dir(self.ws, self.stack), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                                     text=True, encoding="utf-8", errors="replace",
                                     creationflags=flags, start_new_session=os.name != "nt")
        def capture():
            tail = ""
            for line in self.proc.stdout:
                tail = (tail + redact_text(line)).encode("utf-8")[-16000:].decode("utf-8", errors="ignore")
                self.log_path.write_text(tail, encoding="utf-8")
        self.log_thread = threading.Thread(target=capture, daemon=True)
        self.log_thread.start()
        async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
            for _ in range(90):
                if self.proc.poll() is not None:
                    break
                try:
                    await c.get(self.url + "/")
                    return self
                except httpx.HTTPError:
                    await asyncio.sleep(1)
        await self.__aexit__()
        raise RuntimeError("The store did not start locally:\n" + self.log()[-1500:])

    def log(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    async def __aexit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True)
            else:
                os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                pass
        if hasattr(self, "log_thread"):
            await asyncio.to_thread(self.log_thread.join, 2)
