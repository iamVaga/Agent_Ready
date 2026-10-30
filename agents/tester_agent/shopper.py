"""The Tester: try to complete a real shopping task on a website, the way an AI agent would.

Five steps, each passed or failed with evidence:
  1. Reach      - can an agent load the site and read its content without JavaScript?
  2. Find       - can it discover products that match the task?
  3. Compare    - can it read name and price for at least two matches?
  4. Cart       - can it add the chosen product to a cart with what the site exposes?
  5. Checkout   - can it hand the human a link that opens a checkout for that same cart?

Nothing here knows about any particular store. It only uses what the site publishes.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import quote_plus, urlparse

from shared.privacy import redact, redact_text, private_output, cart_fields, safe_url

from . import signals as sig
from .web import Browser, fresh_get

ASI1_API_KEY = os.getenv("ASI1_API_KEY", "")
MAX_PRODUCT_PAGES = 30
MAX_LISTING_PAGES = 8

STOPWORDS = set(("a an the for me find buy get some any under below less than and or with of in to on my i want need please cheap cheapest best good "
                 "add cart bag basket checkout link compare top two one cheaper then give send order purchase shop from at it them pick choose "
                 "can you agent site store website try any product products item items thing something").split())


# Everyday shopping vocabulary, so "earbuds" also finds "AirPods" and "TWS buds".
# Generic, not tied to any store. When ASI:One is configured it handles this instead.
SYNONYMS = {
    "earbud": ["earbud", "buds", "airpods", "tws", "in-ear", "true wireless"],
    "headphone": ["headphone", "over-ear", "on-ear", "headset"],
    "shoe": ["shoe", "sneaker", "trainer", "runner"],
    "sofa": ["sofa", "couch", "settee"],
    "necklace": ["necklace", "pendant", "chain", "strand", "haar"],
    "earring": ["earring", "hoop", "stud", "jhumka", "drop"],
    "phone": ["phone", "smartphone", "mobile"],
    "charger": ["charger", "adapter", "power adapter"],
    "speaker": ["speaker", "soundbar"],
    "watch": ["watch", "smartwatch"],
    "lamp": ["lamp", "light", "pendant light"],
}


def _terms(keyword: str) -> list[str]:
    return SYNONYMS.get(keyword, [keyword])


# ---------------------------------------------------------------- task


@dataclass
class Task:
    text: str
    keywords: list[str]
    max_price: float | None


def parse_task(text: str) -> Task:
    t = text.lower()
    max_price = None
    m = re.search(r"(?:under|below|less than|upto|up to|within|max(?:imum)?)\s*(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*)\s*(k\b)?", t)
    if m:
        max_price = float(m.group(1).replace(",", "")) * (1000 if m.group(2) else 1)
        t = t[: m.start()] + " " + t[m.end():]
    t = re.sub(r"https?://\S+|\b(?:[\w-]+\.)+(?:com|in|app|store|shop|net|org|io|dev)\b\S*", " ", t)
    words = [w for w in re.findall(r"[a-z]+", t) if w not in STOPWORDS and len(w) > 2]
    keywords = [w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 4 else w for w in words]
    return Task(text=text, keywords=keywords[:6], max_price=max_price)


# ---------------------------------------------------------------- results


@dataclass
class Step:
    name: str
    passed: bool
    detail: str
    evidence: list[str] = field(default_factory=list)


@dataclass
class Product:
    url: str
    name: str
    price: float | None
    source: str  # "structured data" | "page text"
    text: str = ""
    page_html: str = ""

    def score(self, task: Task) -> int:
        """Keywords in the product name count three times; in its own description, once."""
        name = self.name.lower()
        hay = f"{self.url} {self.text[:2000]}".lower()
        return sum(3 if any(t in name for t in _terms(k)) else 1 if any(t in hay for t in _terms(k)) else 0 for k in task.keywords)

    def name_hits(self, task: Task) -> int:
        return sum(1 for k in task.keywords if any(t in self.name.lower() for t in _terms(k)))

    def matches(self, task: Task) -> bool:
        if not task.keywords:  # "any product": everything readable counts
            return True
        hay = f"{self.name} {self.url} {self.text[:2000]}".lower()
        found = sum(1 for k in task.keywords if any(t in hay for t in _terms(k)))
        need = len(task.keywords) if len(task.keywords) <= 2 else len(task.keywords) - 1
        return found >= max(1, need)


@dataclass
class Report:
    site: str
    task: str
    steps: list[Step] = field(default_factory=list)
    chosen: dict | None = None
    compared: list[dict] = field(default_factory=list)
    checkout_url: str | None = None
    checkout_note: str | None = None  # something the human must still do before paying, e.g. a minimum order
    blocked_agents: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def passed(self) -> int:
        return sum(s.passed for s in self.steps)

    def to_dict(self):
        d = asdict(self)
        d["passed"] = self.passed
        d["total"] = 5
        if self.checkout_url and safe_url(self.checkout_url) != self.checkout_url:
            d["checkout_url"] = None
            d["checkout_note"] = "Private checkout link withheld. Open the store directly."
        return redact(d)


# ---------------------------------------------------------------- helpers


def _product_from_page(url: str, html: str) -> Product | None:
    s = sig.read(html)
    for node in s.products_ld:
        offers = node.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = offers.get("price") or offers.get("lowPrice")
        try:
            price = float(str(price).replace(",", "")) if price is not None else None
        except ValueError:
            price = None
        if node.get("name"):
            own = " ".join(str(node.get(k, "")) for k in ("description", "category", "brand")) + " " + s.product_text
            return Product(url, str(node["name"]), price, "structured data", own, html)
    name = s.h1
    body = s.main_text or s.text
    if not name or len(body) < 80:
        return None
    # The price shown next to the product name in the main content, not in banners or menus.
    i = body.find(name)
    after = body[i + len(name):] if i >= 0 else body
    price = sig.parse_price(after[:600]) or sig.parse_price(body)
    return Product(url, name, price, "page text", f"{s.meta_description} {s.product_text}", html)


def _candidate_links(b: Browser, base_url: str, s: sig.PageSignals) -> tuple[list[str], list[str]]:
    products, listings = [], []
    for href, _text in s.links:
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = b.abs(href, base_url).split("#")[0]
        if not b.same_site(url):
            continue
        path = urlparse(url).path + ("?" + urlparse(url).query if urlparse(url).query else "")
        if sig.PRODUCT_PATH_RE.search(path):
            products.append(url)
        elif sig.LISTING_PATH_RE.search(path):
            listings.append(url)
    dedupe = lambda xs: list(dict.fromkeys(xs))  # noqa: E731
    return dedupe(products), dedupe(listings)


async def _sitemap_products(b: Browser) -> list[str]:
    page = await b.get(b.origin + "/sitemap.xml")
    if not page.ok or "<urlset" not in page.text:
        return []
    urls = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", page.text)
    return [u for u in urls if sig.PRODUCT_PATH_RE.search(urlparse(u).path + "?" + urlparse(u).query)]


# ---------------------------------------------------------------- steps


async def step_reach(b: Browser, report: Report) -> sig.PageSignals | None:
    # A sleeping free-tier site answers 502/503 for up to a minute. A person would
    # wait and retry, so we do too, before judging anything (robots.txt included).
    home = await b.get(b.start_url, check_robots=False)
    for _ in range(4):
        if home.status and home.status < 500:
            break
        await asyncio.sleep(8)
        home = await b.get(b.start_url, check_robots=False)
    robots = await b.load_robots()
    report.blocked_agents = robots.blocked_agents
    if robots.found and not robots.allows(b.start_url):
        report.steps.append(Step("Reach the site", False, "robots.txt tells automated agents not to visit this site at all.", b.log[-2:]))
        return None
    if not home.ok:
        report.steps.append(Step("Reach the site", False, f"The site did not load for an agent (HTTP {home.status or 'no response'}).", b.log[-2:]))
        return None
    s = sig.read(home.text)
    ev = [f"Homepage: {s.word_count} words of readable text without JavaScript", f"robots.txt: {'found' if robots.found else 'not found'}"]
    if robots.blocked_agents:
        ev.append("robots.txt blocks: " + ", ".join(robots.blocked_agents))
    if s.word_count < 40:
        report.steps.append(Step("Reach the site", False,
                                 f"The page is almost empty without JavaScript ({s.word_count} words). An AI agent sees a blank shell instead of a shop.", ev))
        return s
    report.steps.append(Step("Reach the site", True, "The homepage loads and has readable content without JavaScript.", ev))
    return s


async def step_find(b: Browser, report: Report, home: sig.PageSignals | None, task: Task) -> list[Product]:
    if home is None:
        report.steps.append(Step("Find products", False, "Skipped: the site could not be reached."))
        return []

    ev = []
    llms = await b.get(b.origin + "/llms.txt")
    has_llms = llms.ok and "html" not in llms.content_type and len(llms.text.strip()) > 20
    ev.append(f"llms.txt: {'found' if has_llms else 'not found'}")

    product_urls, listing_urls = _candidate_links(b, b.start_url, home)
    if has_llms:
        for href in re.findall(r"\((https?://[^)\s]+|/[^)\s]*)\)", llms.text):
            u = b.abs(href)
            if b.same_site(u) and sig.PRODUCT_PATH_RE.search(urlparse(u).path + "?" + urlparse(u).query):
                product_urls.append(u)
    sitemap = await _sitemap_products(b)
    ev.append(f"sitemap.xml product URLs: {len(sitemap)}")
    product_urls += sitemap

    # Walk a few listing pages (category, shop, search for the task keywords).
    if task.keywords:
        for guess in ("/search?q=", "/search?query="):
            listing_urls.append(b.origin + guess + "+".join(task.keywords))
    seen_listings = 0
    for page in await b.get_many(list(dict.fromkeys(listing_urls))[:MAX_LISTING_PAGES]):
        if page.ok and "html" in page.content_type:
            seen_listings += 1
            more, _ = _candidate_links(b, page.url, sig.read(page.text))
            product_urls += more
    product_urls = list(dict.fromkeys(product_urls))
    ev.append(f"Listing pages read: {seen_listings} · product links found: {len(product_urls)}")

    products: list[Product] = []
    # Structured data on the homepage itself (e.g. ItemList of Products).
    for node in home.products_ld:
        if node.get("name"):
            products.append(Product(node.get("url") or b.start_url, node["name"], sig.parse_price(json.dumps(node.get("offers", ""))), "structured data"))
    pages = await b.get_many(product_urls[:MAX_PRODUCT_PAGES])
    unreadable = 0
    for page in pages:
        if not page.ok:
            continue
        p = _product_from_page(page.url, page.text)
        if p:
            products.append(p)
        else:
            unreadable += 1
    if unreadable:
        ev.append(f"Product pages with no readable product (needs JavaScript): {unreadable}")
    with_ld = sum(p.source == "structured data" for p in products)
    ev.append(f"Products read: {len(products)} ({with_ld} from structured data)")

    matches = [p for p in products if p.matches(task) and (task.max_price is None or (p.price is not None and p.price <= task.max_price))]
    # Prefer products whose own name says what was asked for.
    best_named = max((p.name_hits(task) for p in matches), default=0)
    named = [p for p in matches if p.name_hits(task) == best_named and best_named > 0]
    if len(named) >= 2:
        matches = named
    matches.sort(key=lambda p: (-p.score(task), p.price or 1e12))
    if not products:
        why = "no product links or product data could be read without JavaScript" if product_urls else "no product links, sitemap, structured data or llms.txt were found"
        report.steps.append(Step("Find products", False, f"Could not find any products: {why}.", ev))
        return []
    if not matches:
        what = f"matched “{task.text}”" if task.keywords else "had a readable price"
        report.steps.append(Step("Find products", False, f"Read {len(products)} products, but none {what}.", ev))
        return []
    report.steps.append(Step("Find products", True, f"Found {len(matches)} products matching the task.", ev))
    return matches


async def step_compare(report: Report, matches: list[Product], task: Task) -> Product | None:
    priced = [p for p in matches if p.price is not None]
    if not matches:
        report.steps.append(Step("Compare options", False, "Skipped: nothing to compare."))
        return None
    if len(priced) < 2:
        report.steps.append(Step("Compare options", False,
                                 f"Only {len(priced)} matching product had a readable price; an agent needs at least two to compare.",
                                 [f"{p.name}: {'₹%s' % int(p.price) if p.price else 'price not readable'}" for p in matches[:4]]))
        return priced[0] if priced else None
    # The two most relevant options (already ranked); between equally relevant ones, the cheaper wins.
    top = priced[:2]
    a, b = top
    if a.score(task) == b.score(task):
        chosen, why = min(top, key=lambda p: p.price), "both fit equally, so it chose the cheaper one"
    else:
        chosen, why = a, f"{a.name} matches the request more closely"
    report.compared = [{"name": p.name, "price": p.price, "url": p.url, "source": p.source} for p in top]
    report.chosen = {"name": chosen.name, "price": chosen.price, "url": chosen.url}
    report.steps.append(Step("Compare options", True,
                             f"Compared {a.name} (₹{int(a.price):,}) with {b.name} (₹{int(b.price):,}); {why}.",
                             [f"Data from {p.source}" for p in top]))
    return chosen


CART_WORDS = re.compile(r"cart|bag|basket", re.I)
ADD_WORDS = re.compile(r"add to (cart|bag|basket)|buy now|add$", re.I)


def _short(name: str) -> str:
    return " ".join(name.split()[:3]).lower()


def _find_cart_link(b: Browser, page_url: str, s: sig.PageSignals) -> str | None:
    for href, text in s.links:
        if CART_WORDS.search(href) or CART_WORDS.fullmatch(text.strip().split(" ")[0] if text else ""):
            u = b.abs(href, page_url)
            if b.same_site(u):
                return u
    return None


async def step_cart(b: Browser, report: Report, chosen: Product | None) -> str | None:
    """Returns the cart page URL when the product is verifiably in the cart."""
    if chosen is None:
        report.steps.append(Step("Add to cart", False, "Skipped: no product was chosen."))
        return None

    manifest = await b.get(b.origin + "/.well-known/ucp")
    ev = [f"Standard agent commerce manifest (/.well-known/ucp): {'found' if manifest.ok and 'json' in manifest.content_type else 'not found'}"]

    # Reload the product page right before buying, as a person would: forms often carry a
    # one-time security token tied to the current session.
    fresh = await b.get(chosen.url)
    s = sig.read(fresh.text if fresh.ok else chosen.page_html)
    forms = [f for f in s.forms if f.method in ("post", "get") and (CART_WORDS.search(f.action) or ADD_WORDS.search(f.submit_text))]
    forms.sort(key=lambda f: f.method != "post")
    # A plain "Add to cart" / "Buy now" link works too: agents follow links.
    add_links = [b.abs(h, chosen.url) for h, t in s.links if t and ADD_WORDS.search(t) and not h.startswith(("#", "javascript:"))]
    add_links = [u for u in add_links if b.same_site(u)]
    if not forms and not add_links:
        report.steps.append(Step("Add to cart", False,
                                 "There is no add-to-cart an agent can use. The button only works through the page's JavaScript, and the site publishes no cart API.",
                                 ev))
        return None

    if forms:
        form = forms[0]
        action = b.abs(form.action, chosen.url) if form.action and not form.action.startswith("javascript:") else chosen.url
    else:
        form, action = None, add_links[0]
    if b.robots and not b.robots.allows(action):
        report.steps.append(Step("Add to cart", False, f"robots.txt tells automated agents not to use {urlparse(action).path}, so a well-behaved agent stops here.", ev))
        return None
    if form and form.method == "post":
        res = await b.post_form(action, form.fields, multipart=form.multipart, referer=chosen.url)
        ev.append(f"Submitted the page's own form: POST {urlparse(action).path or '/'} → HTTP {res.status}")
    elif form:
        try:
            fields = cart_fields(form.fields)
        except ValueError:
            report.steps.append(Step("Add to cart", False, "Privacy policy blocked sensitive form submission."))
            return None
        query = "&".join(f"{quote_plus(k)}={quote_plus(v)}" for k, v in fields)
        res = await b.get(action + ("&" if "?" in action else "?") + query if query else action)
        ev.append(f"Submitted the page's own form: GET {urlparse(action).path or '/'} → HTTP {res.status}")
    else:
        res = await b.get(action)
        ev.append(f"Followed the page's add-to-cart link: {urlparse(action).path or '/'} → HTTP {res.status}")
    if not res.ok:
        report.steps.append(Step("Add to cart", False, f"The add-to-cart form was rejected (HTTP {res.status}).", ev))
        return None

    how = "the site's own form" if form else "the site's add-to-cart link"
    after = sig.read(res.text)
    if CART_WORDS.search(after.h1 or after.title) and _short(chosen.name) in after.text.lower():
        ev.append(f"The page it landed on, {urlparse(res.url).path}, is a cart showing the product")
        report.steps.append(Step("Add to cart", True, f"Added {chosen.name} to the cart using {how}.", ev))
        return res.url
    cart_url = _find_cart_link(b, res.url, after) or _find_cart_link(b, chosen.url, s) or b.origin + "/cart"
    cart = await b.get(cart_url)
    if cart.ok and _short(chosen.name) in sig.read(cart.text).text.lower():
        ev.append(f"Cart page {urlparse(cart.url).path} shows the product")
        report.steps.append(Step("Add to cart", True, f"Added {chosen.name} to the cart using {how}.", ev))
        return cart.url
    report.steps.append(Step("Add to cart", False, "The form was submitted, but the product never appeared in a cart the agent could read.", ev))
    return None


async def step_checkout(b: Browser, report: Report, chosen: Product | None, cart_url: str | None):
    if not cart_url or chosen is None:
        report.steps.append(Step("Checkout link", False, "Skipped: there is no cart to check out."))
        return
    cart = sig.read((await b.get(cart_url)).text)
    link = next((b.abs(h, cart_url) for h, t in cart.links if "checkout" in h.lower() or "checkout" in t.lower()), None)
    link = link or next((b.abs(f.action, cart_url) for f in cart.forms if "checkout" in f.action.lower()), None)
    if not link:
        m = re.search(r"minimum order is[^.·]{0,60}|minimum order[^.·]{0,40}", cart.main_text or cart.text, re.I)
        why = f" The cart page says: “{m.group(0).strip()}”." if m else ""
        report.steps.append(Step("Checkout link", False, "The cart page has no checkout link an agent can find." + why))
        return
    if safe_url(link) != link:
        report.steps.append(Step("Checkout link", False, "Privacy policy withheld a checkout link containing private parameters. Open the store directly."))
        return
    # The human opens the link on their own device: no agent cookies.
    human = await fresh_get(link)
    hs = sig.read(human.text)
    ev = [f"Opened {urlparse(link).path} as the human would (fresh browser, no agent cookies) → {urlparse(human.url).path}"]
    if re.search(r"/(login|signin|sign-in|account/login)", urlparse(human.url).path, re.I) or re.search(r"\blog ?in\b|\bsign ?in\b", hs.h1, re.I):
        report.steps.append(Step("Checkout link", False,
                                 "Checkout needs an account login, and the agent's cart lives in its own session. The human would land on a login page with an empty cart.", ev))
        return
    if _short(chosen.name) in hs.text.lower():
        report.checkout_url = link
        m = re.search(r"(minimum order[^.·]{0,40}?₹\s?[\d,]+)[^.·]{0,20}[.·]?\s*(add ₹\s?[\d,]+ more[^.·]{0,40})?", hs.main_text or hs.text, re.I)
        if m:
            report.checkout_note = ". ".join(x.strip().rstrip(".") for x in m.groups() if x) + "."
        report.steps.append(Step("Checkout link", True, "Produced a checkout link that opens with the chosen product already in the cart.", ev))
        return
    report.steps.append(Step("Checkout link", False,
                             "The cart only exists inside the agent's own session (a cookie or browser storage), so the checkout link opens an empty cart for the human.", ev))


# ---------------------------------------------------------------- run


async def run(site: str, task_text: str) -> Report:
    task_text = redact_text(task_text)
    task = parse_task(task_text)
    b = Browser(site)
    report = Report(site=b.origin, task=task_text)
    t0 = time.time()
    try:
        home = await step_reach(b, report)
        matches = await step_find(b, report, home, task)
        chosen = await step_compare(report, matches, task)
        cart_url = await step_cart(b, report, chosen)
        await step_checkout(b, report, chosen, cart_url)
    finally:
        await b.close()
    report.seconds = round(time.time() - t0, 1)
    return report


@private_output
def format_report(r: Report) -> str:
    verdict = "✅ An AI agent can shop here." if r.passed == 5 else "❌ An AI agent cannot complete a purchase here."
    lines = [f"## Agent shopping test: {urlparse(r.site).netloc}", "", f"Task: “{r.task}”", "",
             f"**Result: {r.passed} / 5 steps.** {verdict}", ""]
    if r.checkout_url:
        lines += [ready_to_buy(r), ""]
    for i, s in enumerate(r.steps, 1):
        lines.append(f"{'✅' if s.passed else '❌'} **{i}. {s.name}**: {s.detail}")
        for e in s.evidence[:4]:
            lines.append(f"   - {e}")
    if r.blocked_agents:
        lines += ["", f"⚠️ robots.txt blocks these AI agents: {', '.join(r.blocked_agents)}"]
    lines += ["", f"_Tested in {r.seconds}s, reading the site as plain HTML without JavaScript, the way most AI agents do._"]
    return "\n".join(lines)


@private_output
def ready_to_buy(r: Report) -> str:
    """The checkout link, with the chosen product already in the cart: the shopper only has to pay."""
    if r.checkout_url and safe_url(r.checkout_url) != r.checkout_url:
        return "Checkout link withheld for privacy. Open the store directly; no order was placed."
    item = r.chosen or {}
    price = f" · ₹{item['price']:,.0f}" if isinstance(item.get("price"), (int, float)) else ""
    name = item.get("name") or "Your product"
    note = ""
    if r.checkout_note:
        note = "\n\n⚠️ Before paying: " + ". ".join(p[:1].upper() + p[1:] for p in r.checkout_note.split(". "))
    return (f"### 🛒 Ready to buy\n**{name}**{price} is already in your cart. "
            f"Open the link and complete the purchase:\n\n👉 [Go to checkout]({r.checkout_url})  \n{r.checkout_url}{note}")
