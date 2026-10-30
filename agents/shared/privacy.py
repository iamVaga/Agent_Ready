"""Privacy boundaries. Operational HTTP credentials never belong in observability data."""
import functools
import html
import logging
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, unquote

MASK = "[REDACTED]"
SENSITIVE = re.compile(r"email|phone|mobile|whatsapp|address|street|postal|zip|password|secret|token|api.?key|authorization|cookie|session|card|cvv|cvc|iban|expiry|expiration|routing|swift|account.?number|customer|first.?name|last.?name|full.?name", re.I)
EMAIL = re.compile(r"[\w.+%-]+@[\w.-]+\.[a-z]{2,}", re.I)
PHONE = re.compile(r"(?<!\w)\+?\d[\d ().-]{6,}\d(?!\w)")
ADDRESS = re.compile(r"\b\d{1,6}\s+(?:[\w.-]+\s+){0,5}(?:street|st|road|rd|avenue|ave|lane|ln|drive|boulevard|blvd|nagar)\b(?:\s*,?\s*(?:apt|unit|suite)\s*[\w-]+)?", re.I)
SECRETS = re.compile(r"\b(?:sk-[a-zA-Z0-9_-]{8,}|gh[pousr]_[a-zA-Z0-9_]{10,}|AKIA[A-Z0-9]{16}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b")
ASSIGN = re.compile(r"""(?i)(["']?(?:[\w-]*(?:token|secret|password|api[_-]?key|cookie|authorization|session|cvv|cvc|iban|card[_-]?number)[\w-]*)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;<>&]+)""")
URL = re.compile(r"https?://[^\s<>\"']+", re.I)
PUBLIC_QUERY = {"id", "product", "product_id", "variant", "variant_id", "quantity", "page", "sort", "category", "slug", "q", "s"}


def safe_url(value: str, log=False) -> str:
    try:
        u = urlsplit(html.unescape(value))
        host = u.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        netloc = host + ((":" + str(u.port)) if u.port else "")
        query = [] if log else [(k, redact_text(v, urls=False)) for k,v in parse_qsl(u.query) if k.lower() in PUBLIC_QUERY and not SENSITIVE.search(k)]
        path = redact_text(u.path, urls=False)
        path = re.sub(r"(?<=/)[A-Za-z0-9_-]{32,}(?=/|$)", MASK, path)
        return urlunsplit((u.scheme, netloc, path, urlencode(query), ""))
    except ValueError:
        return MASK


def redact_text(value: str, urls=True) -> str:
    value = unquote(html.unescape(str(value)))
    if urls:
        value = URL.sub(lambda m: safe_url(m.group()), value)
    value = re.sub(r"(?im)\b(?:cookie|set-cookie|authorization)\s*:\s*[^\r\n]+", MASK, value)
    value = EMAIL.sub(MASK, value)
    value = SECRETS.sub(MASK, value)
    value = re.sub(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b", MASK, value)
    value = ASSIGN.sub(lambda m: m[1] + MASK, value)
    value = ADDRESS.sub(MASK, value)
    value = PHONE.sub(lambda m: MASK if 8 <= sum(c.isdigit() for c in m[0]) <= 19 else m[0], value)
    return value


def redact(value):
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, BaseException):
        return type(value).__name__
    if isinstance(value, dict):
        return {k: MASK if SENSITIVE.search(str(k)) and not isinstance(v, bool) else redact(v) for k,v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def private_output(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        return redact(fn(*args, **kwargs))
    return wrapped


class PrivacyFilter(logging.Filter):
    def filter(self, record):
        # Omit exception values/tracebacks: HTTP libraries can embed request bodies.
        record.msg = redact_text(record.getMessage())
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


_logging_installed = False

def install_logging():
    global _logging_installed
    if _logging_installed:
        return
    _logging_installed = True
    original = logging.getLogRecordFactory()
    def factory(*args, **kwargs):
        record = original(*args, **kwargs)
        record.msg = redact_text(URL.sub(lambda m: safe_url(m.group(), log=True), record.getMessage()))
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return record
    logging.setLogRecordFactory(factory)


SYNTHETIC_IDENTITY = {"name": "AgentReady Test", "email": "agentready-test@example.invalid", "phone": "+12025550100", "address": "1 Test Street"}
CART_FIELDS = re.compile(r"^(?:product_?id|variant_?id|id|quantity|qty|sku|size|color|colour|variant|product|add-to-cart|action|csrf(?:middlewaretoken|_token)?|_token|authenticity_token|nonce|_wpnonce|options\[[^\]]+\])$", re.I)
PAYMENT = re.compile(r"checkout|payment|pay(?:ment)?[-_/]?now|place[-_/]?order|purchase|billing|credit|card|cvv|cvc|iban|password", re.I)


def cart_fields(fields):
    result = []
    for key, value in fields:
        if PAYMENT.search(key) or (SENSITIVE.search(key) and not re.search(r"csrf|authenticity|nonce|^_token$", key, re.I)):
            raise ValueError("Sensitive form submission blocked")
        if not CART_FIELDS.fullmatch(key):
            raise ValueError("Unrecognized cart field blocked")
        if key.lower() == "action" and value.lower() not in ("add", "add_to_cart", "add-to-cart", "cart_add", "update", "update_cart"):
            raise ValueError("Non-cart action blocked")
        if EMAIL.search(value) or ADDRESS.search(value) or SECRETS.search(value):
            raise ValueError("Personal form value blocked")
        result.append((key, value))
    return result


def synthetic_value(key: str) -> str:
    if re.search(r"email", key, re.I): return SYNTHETIC_IDENTITY["email"]
    if re.search(r"phone|mobile|tel", key, re.I): return SYNTHETIC_IDENTITY["phone"]
    if re.search(r"address|street", key, re.I): return SYNTHETIC_IDENTITY["address"]
    return SYNTHETIC_IDENTITY["name"]


def scrub_html(value: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(value or "", "html.parser")
    for node in soup.find_all(["input", "textarea", "select"]):
        key = str(node.get("name", "")) + " " + str(node.get("autocomplete", "")) + " " + str(node.get("type", ""))
        if SENSITIVE.search(key) or re.search(r"(?:^|\s)(?:name|tel)(?:$|\s)", key):
            if not re.search(r"csrf|authenticity|nonce|^_token", key, re.I):
                node["value"] = synthetic_value(key)
                if node.name in ("textarea", "select"):
                    node.clear()
    for node in soup.find_all("script"):
        if node.get("type") == "application/ld+json":
            import json
            try: node.string = json.dumps(redact(json.loads(node.string or "{}")))
            except (ValueError, TypeError): node.decompose()
        else:
            node.clear()
    for text in list(soup.find_all(string=True)):
        if text.parent and text.parent.name == "script":
            continue
        text.replace_with(redact_text(str(text)))
    for node in soup.find_all(True):
        for key in list(node.attrs):
            if key.startswith("on") or key.startswith("data-"):
                del node[key]
            elif key in ("href", "action", "src"):
                # Keep operational URLs ephemeral; request policy checks every hop.
                continue
            elif key != "value" or not CART_FIELDS.fullmatch(str(node.get("name", ""))):
                val = node[key]
                node[key] = redact_text(val) if isinstance(val, str) else val
    return str(soup)


def privacy_print(*values, **kwargs):
    import builtins
    builtins.print(*(redact_text(v) if isinstance(v, str) else redact(v) for v in values), **kwargs)
