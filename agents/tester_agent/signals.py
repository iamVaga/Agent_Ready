"""Read what a page offers an agent: visible text, structured data, links, prices and forms."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from shared.privacy import scrub_html, redact

PRICE_RE = re.compile(r"(?:₹|Rs\.?\s?|INR\s?)\s*([0-9]{1,3}(?:,[0-9]{2,3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)", re.I)
RELATED_RE = re.compile(r"you may also like|you might also like|similar products|related products|more from|customers also|frequently bought", re.I)
PRODUCT_PATH_RE = re.compile(r"/(p|product|products|item|items|dp)/[^/?#]+|[?&](p|id|product|slug)=[^&#]+", re.I)
LISTING_PATH_RE = re.compile(r"/(shop|c|category|categories|collections?|rooms?|store|catalog|search)(/|$|\?)|/(shop|catalog|products)\.html", re.I)


def parse_price(text: str) -> float | None:
    m = PRICE_RE.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None


@dataclass
class Form:
    action: str
    method: str
    multipart: bool
    fields: list[tuple[str, str]]
    choices: dict[str, list[str]]
    submit_text: str


@dataclass
class PageSignals:
    title: str = ""
    h1: str = ""
    text: str = ""
    word_count: int = 0
    script_count: int = 0
    jsonld: list[dict] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (href, text)
    forms: list[Form] = field(default_factory=list)
    meta_description: str = ""
    main_text: str = ""
    product_text: str = ""  # from the product title to where related products start

    @property
    def products_ld(self) -> list[dict]:
        out = []
        for block in self.jsonld:
            for node in _walk(block):
                t = node.get("@type")
                types = t if isinstance(t, list) else [t]
                if "Product" in types:
                    out.append(node)
        return out


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def read(html: str) -> PageSignals:
    soup = BeautifulSoup(scrub_html(html), "html.parser")
    sig = PageSignals()
    sig.script_count = len(soup.find_all("script"))
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            sig.jsonld.append(redact(json.loads(s.string or s.get_text() or "{}")))
        except (json.JSONDecodeError, TypeError):
            pass
    md = soup.find("meta", attrs={"name": "description"})
    sig.meta_description = (md.get("content") or "").strip() if md else ""
    sig.title = (soup.title.get_text(strip=True) if soup.title else "")

    for a in soup.find_all("a", href=True):
        sig.links.append((a["href"], " ".join(a.get_text(" ", strip=True).split())))

    for f in soup.find_all("form"):
        fields, choices = [], {}
        for inp in f.find_all(["input", "select", "textarea"]):
            name = inp.get("name")
            if not name:
                continue
            if inp.name == "select":
                opts = [o.get("value", o.get_text(strip=True)) for o in inp.find_all("option")]
                sel = inp.find("option", selected=True)
                choices[name] = opts
                fields.append((name, sel.get("value", sel.get_text(strip=True)) if sel else (opts[0] if opts else "")))
            elif inp.get("type") in ("radio", "checkbox"):
                choices.setdefault(name, []).append(inp.get("value", "on"))
                if inp.has_attr("checked"):
                    fields.append((name, inp.get("value", "on")))
            elif inp.get("type") in ("submit", "button", "image"):
                continue
            else:
                fields.append((name, inp.get("value", "")))
        # a radio group with nothing checked: take the first choice, like a user picking one
        for name, opts in choices.items():
            if not any(k == name for k, _ in fields) and opts:
                fields.append((name, opts[0]))
        # A <button> with no type is a submit button.
        btns = [b for b in f.find_all("button") if (b.get("type") or "submit").lower() == "submit"]
        btns += [i for i in f.find_all("input") if (i.get("type") or "").lower() == "submit"]
        submit_text = " ".join(b.get_text(" ", strip=True) or b.get("value", "") for b in btns)
        sig.forms.append(Form(
            action=f.get("action") or "",
            method=(f.get("method") or "get").lower(),
            multipart="multipart" in (f.get("enctype") or ""),
            fields=fields,
            choices=choices,
            submit_text=submit_text,
        ))

    main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body
    h1 = soup.find("h1")
    sig.h1 = h1.get_text(" ", strip=True) if h1 else ""
    for t in soup(["script", "style", "noscript", "template", "svg"]):
        t.decompose()
    sig.main_text = " ".join(main.get_text(" ").split()) if main else ""
    focus = sig.main_text
    if sig.h1 and sig.h1 in focus:
        focus = focus[focus.find(sig.h1):]
    cut = RELATED_RE.search(focus)
    sig.product_text = focus[: cut.start()] if cut else focus
    sig.text = " ".join(soup.get_text(" ").split())
    sig.word_count = len(sig.text.split())
    return sig
