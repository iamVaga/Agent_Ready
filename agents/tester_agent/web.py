"""A plain-HTTP 'browser' that sees a website the way most AI shopping agents do.

No JavaScript is executed. Cookies are kept within one session (like an agent
working through a task), robots.txt is honoured, and every request is logged so
the final report can show its evidence.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlencode, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from shared.privacy import safe_url, cart_fields, PAYMENT, SENSITIVE, scrub_html
from urllib.parse import parse_qsl, unquote

USER_AGENT = (
    "Mozilla/5.0 (compatible; AgentReady-Shopper/1.0; +https://agentready-five-rust.vercel.app) "
    "AI shopping agent test"
)
TIMEOUT = httpx.Timeout(45.0, connect=20.0)  # free-tier hosts can take ~40 s to wake

MAX_BODY = 5 * 1024 * 1024


def public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return (ip.is_global and not ip.is_multicast and
            not (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo or
                 ip in ipaddress.ip_network("2001::/23") or
                 ip in ipaddress.ip_network("3fff::/20"))))


async def resolve_target(url: httpx.URL) -> str:
    if (url.scheme not in ("http", "https") or url.userinfo or
            url.port not in (None, 443 if url.scheme == "https" else 80)):
        raise httpx.RequestError("Unsafe outbound URL")
    host = url.host.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise httpx.RequestError("Unsafe hostname")
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            answers = await asyncio.get_running_loop().getaddrinfo(host, url.port or 80, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise httpx.RequestError("DNS resolution failed") from exc
        addresses = list(dict.fromkeys(a[4][0] for a in answers))
    if not addresses or not all(public_ip(a) for a in addresses):
        raise httpx.RequestError("Unsafe destination IP")
    return addresses[0]


class PublicTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        address = await resolve_target(request.url)
        # A separate pool per hop prevents TLS connection reuse between hostnames
        # sharing an IP. Keep the logical URL for cookies, Host, and certificate SNI.
        pinned = httpx.Request(request.method, request.url.copy_with(host=address),
                               headers={**dict(request.headers), "accept-encoding": "identity"}, stream=request.stream,
                               extensions={**request.extensions, "sni_hostname": request.url.host})
        async with httpx.AsyncHTTPTransport(trust_env=False) as transport:
            response = await transport.handle_async_request(pinned)
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                await response.aclose()
                raise httpx.RequestError("Unexpected compressed response")
            data = bytearray()
            try:
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BODY:
                        raise httpx.RequestError("Response too large")
            finally:
                await response.aclose()
            headers = [(k, v) for k, v in response.headers.multi_items()
                       if k not in ("content-encoding", "content-length")]
            return httpx.Response(response.status_code, headers=headers, content=bytes(data))


def public_client(**kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=PublicTransport(), trust_env=False, follow_redirects=False, **kwargs)


async def _safe_request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    for hop in range(6):
        parsed = urlparse(url)
        path = unquote(parsed.path)
        if re.search(r"/(?:account|customers?|orders?|profile|admin|login|sign-?in)(?:/|$)", path, re.I):
            raise httpx.RequestError("Private customer endpoint blocked")
        for key, value in parse_qsl(parsed.query):
            if key.lower() in ("action", "do", "submit") and PAYMENT.search(value):
                raise httpx.RequestError("Payment action blocked")
            if SENSITIVE.search(key) and not re.search(r"csrf|nonce|^_token$", key, re.I):
                raise httpx.RequestError("Sensitive URL parameters blocked")
        if method != "GET" and PAYMENT.search(path):
            raise httpx.RequestError("Checkout/payment submission blocked")
        r = await client.request(method, url, **kwargs)
        if r.status_code not in (301, 302, 303, 307, 308) or "location" not in r.headers:
            return r
        if hop == 5:
            raise httpx.RequestError("Too many redirects")
        target = urljoin(str(r.url), r.headers["location"])
        # Never send form contents or caller headers to a different origin.
        if httpx.URL(target).copy_with(path="/", query=None, fragment=None) != httpx.URL(url).copy_with(path="/", query=None, fragment=None):
            kwargs = {}
            method = "GET"
        elif r.status_code == 303 or (r.status_code in (301, 302) and method == "POST"):
            method, kwargs = "GET", {}
        url = target
    raise httpx.RequestError("Too many redirects")

async def safe_request(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    try:
        return await asyncio.wait_for(_safe_request(client, method, url, **kwargs), timeout=45)
    except asyncio.TimeoutError as exc:
        raise httpx.RequestError("Outbound deadline exceeded") from exc


# Well-known AI agents and crawlers whose access we report on.
AI_AGENTS = ["GPTBot", "ChatGPT-User", "OAI-SearchBot", "ClaudeBot", "Claude-User", "PerplexityBot", "Google-Extended", "CCBot"]


@dataclass
class Page:
    url: str
    status: int
    text: str
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


@dataclass
class Robots:
    found: bool
    parser: RobotFileParser | None = None
    blocked_agents: list[str] = field(default_factory=list)

    def allows(self, url: str) -> bool:
        if not self.parser:
            return True
        # We act as a generic automated agent, so the '*' group applies.
        return self.parser.can_fetch("AgentReady-Shopper", url)


class Browser:
    def __init__(self, base_url: str):
        if not re.match(r"^https?://", base_url):
            base_url = "https://" + base_url
        p = urlparse(base_url)
        self.origin = f"{p.scheme}://{p.netloc}"
        self.start_url = base_url
        self.client = public_client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"},
            timeout=TIMEOUT,
        )
        self.robots: Robots | None = None
        self.log: list[str] = []

    async def close(self):
        self.client.cookies.clear()
        await self.client.aclose()
        self.log.clear()

    def same_site(self, url: str) -> bool:
        return urlparse(url).netloc == urlparse(self.origin).netloc

    def abs(self, href: str, base: str | None = None) -> str:
        return urljoin(base or self.origin + "/", href)

    async def load_robots(self) -> Robots:
        page = await self.get(self.origin + "/robots.txt", check_robots=False)
        if not page.ok or "html" in page.content_type:
            self.robots = Robots(found=False)
            return self.robots
        rp = RobotFileParser()
        rp.parse(page.text.splitlines())
        blocked = [a for a in AI_AGENTS if not rp.can_fetch(a, self.origin + "/")]
        self.robots = Robots(found=True, parser=rp, blocked_agents=blocked)
        return self.robots

    async def get(self, url: str, check_robots: bool = True) -> Page:
        if check_robots and self.robots and not self.robots.allows(url):
            self.log.append(f"SKIP (robots.txt) GET {safe_url(url, log=True)}")
            return Page(url, 999, "")
        try:
            r = await safe_request(self.client, "GET", url)
            self.log.append(f"GET {safe_url(url, log=True)} -> {r.status_code}")
            return Page(str(r.url), r.status_code, scrub_html(r.text), r.headers.get("content-type", ""))
        except httpx.HTTPError as err:
            self.log.append(f"GET {safe_url(url, log=True)} -> error {type(err).__name__}")
            return Page(url, 0, "")

    async def post_form(self, url: str, data: list[tuple[str, str]], multipart: bool = False, referer: str | None = None) -> Page:
        if self.robots and not self.robots.allows(url):
            self.log.append(f"SKIP (robots.txt) POST {safe_url(url, log=True)}")
            return Page(url, 999, "")
        try:
            data = cart_fields(data)
            if PAYMENT.search(urlparse(url).path) or not self.same_site(url):
                raise ValueError("Sensitive or cross-site submission blocked")
        except ValueError:
            self.log.append("POST blocked by privacy policy")
            return Page(url, 0, "")
        headers = {"Referer": safe_url(referer, log=True)} if referer else {}
        try:
            if multipart:
                boundary = "----AgentReady" + secrets.token_hex(8)
                crlf = chr(13) + chr(10)
                parts = [f'--{boundary}{crlf}Content-Disposition: form-data; name="{k}"{crlf}{crlf}{v}{crlf}' for k, v in data]
                body = ("".join(parts) + f"--{boundary}--{crlf}").encode()
                headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
                r = await safe_request(self.client, "POST", url, content=body, headers=headers)
            else:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
                r = await safe_request(self.client, "POST", url, content=urlencode(data).encode(), headers=headers)
            self.log.append(f"POST {safe_url(url, log=True)} -> {r.status_code}")
            return Page(str(r.url), r.status_code, scrub_html(r.text), r.headers.get("content-type", ""))
        except httpx.HTTPError as err:
            self.log.append(f"POST {safe_url(url, log=True)} -> error {type(err).__name__}")
            return Page(url, 0, "")

    async def get_many(self, urls: list[str], limit: int = 6) -> list[Page]:
        sem = asyncio.Semaphore(limit)

        async def one(u):
            async with sem:
                return await self.get(u)

        return await asyncio.gather(*(one(u) for u in urls))


async def fresh_get(url: str) -> Page:
    """Fetch with a brand-new client: no cookies. This is what a human sees when they open a link."""
    async with public_client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT) as c:
        try:
            r = await safe_request(c, "GET", url)
            return Page(str(r.url), r.status_code, scrub_html(r.text), r.headers.get("content-type", ""))
        except httpx.HTTPError:
            return Page(url, 0, "")
