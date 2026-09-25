#!/usr/bin/env python3
"""NeeDoh stock checker.

Checks the shops listed in shops.json, compares what is in stock with the
last run (saved in state.json), and sends a push notification via ntfy.sh
when a NeeDoh product comes into stock.

Uses only the Python standard library, so there is nothing to install.

Usage:
    python checker.py                       # normal run
    python checker.py --dry-run             # check shops, print results, send/save nothing
    python checker.py --test-notification   # just send a test push to your phone
"""

import argparse
import datetime as dt
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
SHOPS_FILE = os.path.join(HERE, "shops.json")
STATE_FILE = os.path.join(HERE, "state.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
TIMEOUT = 25
KEYWORD_RE = re.compile(r"nee[\s\-_]?doh", re.IGNORECASE)
IN_STOCK_WORDS = ("instock", "in stock", "limitedavailability", "onlineonly")
CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}

# Send a "this shop keeps failing" push after this many failed runs in a row
# (24 runs x 5 minutes = about 2 hours). Only for shops marked "required".
FAILURE_ALERT_AFTER = 24
MAX_PRODUCT_PAGES = 40       # per html shop, to stay polite
MAX_INDIVIDUAL_ALERTS = 15   # beyond this, remaining alerts are grouped into one push


class FetchError(Exception):
    pass


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def fetch(url, want_json=False):
    """Download a URL and return (status, text). Raises FetchError on network errors."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json" if want_json else
                  "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }
    last_error = None
    for attempt in range(2):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.status, _decode(resp.read(), resp.headers)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt == 0:
                last_error = f"HTTP {e.code}"
                time.sleep(3)
                continue
            return e.code, ""
        except Exception as e:  # timeouts, DNS, TLS, connection resets...
            last_error = f"{type(e).__name__}: {e}"
            time.sleep(2)
    raise FetchError(last_error)


def _decode(body, headers):
    encoding = (headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip":
        body = gzip.decompress(body)
    elif encoding == "deflate":
        body = zlib.decompress(body)
    charset = headers.get_content_charset() or "utf-8"
    return body.decode(charset, errors="replace")


# --------------------------------------------------------------------------
# Shopify shops (products.json)
# --------------------------------------------------------------------------

def check_shopify(shop):
    base = shop["base_url"].rstrip("/")
    raw = None
    tried = []
    for handle in shop.get("collections", []):
        url = f"{base}/collections/{handle}/products.json"
        tried.append(url)
        raw = _shopify_pages(url, max_pages=10)
        if raw is not None:
            # An empty collection is fine: some shops hide NeeDoh while it's sold out.
            log(f"  using collection '{handle}' ({len(raw)} products)")
            break
    use_filter = shop.get("keyword_filter", False)
    if raw is None and shop.get("search_all_products_fallback"):
        url = f"{base}/products.json"
        tried.append(url)
        log("  no NeeDoh collection found, searching the whole shop for 'NeeDoh'")
        raw = _shopify_pages(url, max_pages=30)
        use_filter = True
    if raw is None:
        raise FetchError("not found: " + ", ".join(tried))

    products = {}
    for p in raw:
        if use_filter and not _mentions_needoh(p):
            continue
        item = shopify_product(shop, p)
        products[item["key"]] = item

    # Also ask the shop's own search, to catch NeeDoh items not added to the collection.
    extra = 0
    for p in _shopify_search(base):
        if _mentions_needoh(p):
            item = _search_product(shop, p)
            if item["key"] not in products:
                products[item["key"]] = item
                extra += 1
    if extra:
        log(f"  +{extra} more from the shop's search")
    return list(products.values())


def _shopify_pages(url, max_pages):
    """Read every page of a Shopify products.json. Returns None if it doesn't exist."""
    items = []
    for page in range(1, max_pages + 1):
        status, text = fetch(f"{url}?limit=250&page={page}", want_json=True)
        if status == 404 and page == 1:
            return None
        if status != 200:
            if items:
                break
            raise FetchError(f"HTTP {status} from {url}")
        try:
            batch = json.loads(text).get("products", [])
        except (ValueError, AttributeError):
            raise FetchError(f"{url} did not return JSON (the shop may be blocking us)")
        items.extend(batch)
        if len(batch) < 250:
            break
    return items


def _shopify_search(base):
    """Shopify's predictive search (max 10 results). Best effort: returns [] on any problem."""
    url = (f"{base}/search/suggest.json?q=needoh"
           "&resources[type]=product&resources[limit]=10")
    try:
        status, text = fetch(url, want_json=True)
        if status != 200:
            return []
        return json.loads(text)["resources"]["results"]["products"]
    except Exception:
        return []


def _search_product(shop, p):
    try:
        price = f"{shop.get('currency_symbol', '£')}{float(p.get('price_min') or p.get('price')):.2f}"
    except (TypeError, ValueError):
        price = "price unknown"
    return {
        "key": f"{shop['name']}|{p.get('id') or p.get('handle')}",
        "shop": shop["name"],
        "name": html.unescape(p.get("title") or "Unknown product").strip(),
        "price": price,
        "url": f"{shop['base_url'].rstrip('/')}/products/{p.get('handle')}",
        "in_stock": bool(p.get("available")),
    }


def recheck_shopify_product(shop, key, entry):
    """Look up one product page directly (for products that dropped out of the lists).
    Returns an updated product, or None if it's gone or couldn't be checked."""
    try:
        status, text = fetch(entry["url"] + ".js", want_json=True)
        if status != 200:
            return None
        p = json.loads(text)
    except Exception:
        return None
    try:
        price = f"{shop.get('currency_symbol', '£')}{int(p.get('price_min', p.get('price'))) / 100:.2f}"
    except (TypeError, ValueError):
        price = entry.get("price", "price unknown")
    return {"key": key, "shop": shop["name"], "name": entry["name"], "price": price,
            "url": entry["url"], "in_stock": bool(p.get("available"))}


def _mentions_needoh(p):
    tags = p.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    text = " ".join([p.get("title") or "", p.get("vendor") or "",
                     p.get("product_type") or "", p.get("handle") or ""] + list(tags))
    return bool(KEYWORD_RE.search(text))


def shopify_product(shop, p):
    base = shop["base_url"].rstrip("/")
    variants = p.get("variants") or []
    in_stock = any(v.get("available") for v in variants)
    pool = [v for v in variants if v.get("available")] or variants
    prices = []
    for v in pool:
        try:
            prices.append(float(v.get("price")))
        except (TypeError, ValueError):
            pass
    symbol = shop.get("currency_symbol", "£")
    price = f"{symbol}{min(prices):.2f}" if prices else "price unknown"
    if len(set(prices)) > 1:
        price = "from " + price
    return {
        "key": f"{shop['name']}|{p.get('id') or p.get('handle')}",
        "shop": shop["name"],
        "name": html.unescape(p.get("title") or "Unknown product").strip(),
        "price": price,
        "url": f"{base}/products/{p.get('handle')}",
        "in_stock": in_stock,
    }


# --------------------------------------------------------------------------
# Other shops (read the web page itself - best effort)
# --------------------------------------------------------------------------

class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


LD_JSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL)


def json_ld_products(page_html, page_url):
    """Find schema.org Product data embedded in a page. Returns list of dicts."""
    found = []
    for block in LD_JSON_RE.findall(page_html):
        try:
            data = json.loads(block.strip(), strict=False)
        except ValueError:
            continue
        _walk_ld(data, found)
    products = []
    for node in found:
        name = node.get("name")
        availability, price, url = _offer_info(node.get("offers"))
        if availability is None and isinstance(node.get("hasVariant"), list):
            # ProductGroup: in stock if any variant is
            states = [_offer_info(v.get("offers")) for v in node["hasVariant"] if isinstance(v, dict)]
            avail = [s[0] for s in states if s[0] is not None]
            if avail:
                availability = any(avail)
                price = price or next((s[1] for s in states if s[1]), None)
        if not name or availability is None:
            continue
        link = node.get("url") or url or page_url
        products.append({
            "name": html.unescape(str(name)).strip(),
            "url": urllib.parse.urljoin(page_url, str(link)),
            "price": price or "price unknown",
            "in_stock": availability,
        })
    return products


def _walk_ld(data, found):
    if isinstance(data, list):
        for item in data:
            _walk_ld(item, found)
    elif isinstance(data, dict):
        types = data.get("@type")
        types = types if isinstance(types, list) else [types]
        if "Product" in types or "ProductGroup" in types:
            found.append(data)
            return
        for key in ("@graph", "itemListElement", "item", "mainEntity"):
            if key in data:
                _walk_ld(data[key], found)


def _offer_info(offers):
    """Return (in_stock or None, price text or None, url or None) from schema.org offers."""
    if offers is None:
        return None, None, None
    offer_list = offers if isinstance(offers, list) else [offers]
    availability, price, url = None, None, None
    for o in offer_list:
        if not isinstance(o, dict):
            continue
        if "offers" in o and o.get("@type") == "AggregateOffer":
            inner = _offer_info(o["offers"])
            if inner[0] is not None:
                availability = bool(availability) or inner[0]
        a = o.get("availability")
        if a:
            is_in = str(a).replace(" ", "").lower().rsplit("/", 1)[-1] in (
                "instock", "limitedavailability", "onlineonly")
            availability = bool(availability) or is_in
        p = o.get("price", o.get("lowPrice"))
        if p not in (None, "") and price is None:
            try:
                symbol = CURRENCY_SYMBOLS.get(o.get("priceCurrency", "GBP"), "")
                price = f"{symbol}{float(p):.2f}"
            except (TypeError, ValueError):
                price = str(p)
        url = url or o.get("url")
    return availability, price, url


META_AVAIL_RE = re.compile(
    r'<meta[^>]+(?:property|itemprop|name)=["\'](?:product:availability|og:availability|availability)["\']'
    r'[^>]*content=["\']([^"\']+)["\']', re.IGNORECASE)
ITEMPROP_AVAIL_RE = re.compile(
    r'itemprop=["\']availability["\'][^>]*(?:href|content)=["\']([^"\']+)["\']', re.IGNORECASE)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
OG_TITLE_RE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']+)["\']', re.IGNORECASE)


def fallback_page_product(page_html, page_url):
    """For product pages without JSON-LD: look for availability meta tags."""
    m = META_AVAIL_RE.search(page_html) or ITEMPROP_AVAIL_RE.search(page_html)
    if not m:
        return None
    value = m.group(1).replace("_", " ").replace("-", " ").lower().rsplit("/", 1)[-1]
    in_stock = any(w in value or w in value.replace(" ", "") for w in IN_STOCK_WORDS)
    t = OG_TITLE_RE.search(page_html) or TITLE_RE.search(page_html)
    name = html.unescape(t.group(1)).strip() if t else page_url
    return {"name": name, "url": page_url, "price": "price unknown", "in_stock": in_stock}


def needoh_links(page_html, page_url, base_url):
    parser = _LinkParser()
    parser.feed(page_html)
    host = urllib.parse.urlparse(base_url).netloc.lower().removeprefix("www.")
    listing = urllib.parse.urlparse(page_url).path.rstrip("/")
    seen, out = set(), []
    for href in parser.links:
        full = urllib.parse.urljoin(page_url, href)
        parts = urllib.parse.urlparse(full)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc.lower().removeprefix("www.") != host:
            continue
        path = parts.path.rstrip("/")
        if not KEYWORD_RE.search(path) or path == listing:
            continue
        clean = f"{parts.scheme}://{parts.netloc}{parts.path}"
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def check_html(shop):
    by_url = {}
    links = []
    errors = []
    for listing_url in shop["listing_urls"]:
        try:
            status, text = fetch(listing_url)
        except FetchError as e:
            errors.append(f"{listing_url}: {e}")
            continue
        if status != 200:
            errors.append(f"{listing_url}: HTTP {status}")
            continue
        for p in json_ld_products(text, listing_url):
            if KEYWORD_RE.search(p["name"]) or KEYWORD_RE.search(p["url"]):
                by_url.setdefault(p["url"], p)
        for link in needoh_links(text, listing_url, shop["base_url"]):
            if link not in links:
                links.append(link)

    if not by_url:
        # The listing page didn't say what's in stock, so visit each product page.
        for link in links[:MAX_PRODUCT_PAGES]:
            try:
                status, text = fetch(link)
            except FetchError as e:
                log(f"  could not open {link}: {e}")
                continue
            if status != 200:
                continue
            found = json_ld_products(text, link)
            product = found[0] if found else fallback_page_product(text, link)
            if product:
                product["url"] = link
                by_url.setdefault(link, product)
            time.sleep(0.5)

    if not by_url:
        detail = "; ".join(errors) if errors else f"found {len(links)} NeeDoh links but no stock info"
        raise FetchError(f"no products found (site may be blocking automated checks) - {detail}")

    products = []
    for url, p in by_url.items():
        path = urllib.parse.urlparse(url).path.rstrip("/")
        products.append({**p, "key": f"{shop['name']}|{path}", "shop": shop["name"]})
    return products


# --------------------------------------------------------------------------
# Notifications (ntfy.sh)
# --------------------------------------------------------------------------

def notify(title, message, click=None, tags=None, priority=4):
    """Send a push via ntfy. Returns True on success."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    if not topic:
        log(f"  [no NTFY_TOPIC set - would have sent] {title}: {message}")
        return False
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority, "tags": tags or []}
    if click:
        payload["click"] = click
        payload["actions"] = [{"action": "view", "label": "Open shop page", "url": click}]
    headers = {"Content-Type": "application/json", "User-Agent": "needoh-stock-checker"}
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(server, data=json.dumps(payload).encode(), headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200:
                    return True
        except Exception as e:
            log(f"  ntfy send failed ({e}), retrying")
            time.sleep(2 * (attempt + 1))
    return False


def alert_product(p):
    return notify(
        title=f"NeeDoh in stock at {p['shop']}!",
        message=f"{p['name']}\n{p['price']}\n{p['url']}",
        click=p["url"],
        tags=["tada", "shopping_cart"],
        priority=5,
    )


# --------------------------------------------------------------------------
# State + main logic
# --------------------------------------------------------------------------

def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        state = {}
    state.setdefault("products", {})
    state.setdefault("shops", {})
    return state


def save_state(state, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def process_shop_result(state, shop, products, now):
    """Update state with a successful check. Returns list of products to alert on."""
    name = shop["name"]
    shop_state = state["shops"].setdefault(name, {})
    known = {k: v for k, v in state["products"].items() if v.get("shop") == name}
    first_time = not shop_state.get("initialized")

    if not products and any(v.get("in_stock") for v in known.values()):
        # An empty answer when we previously saw stock is more likely a glitch than
        # everything selling out at once; ignore it so we don't re-alert on everything.
        log("  got 0 products (previously had stock) - ignoring this result")
        return []

    alerts = []
    seen = set()
    for p in products:
        seen.add(p["key"])
        prev = known.get(p["key"])
        was_in_stock = bool(prev and prev.get("in_stock"))
        if p["in_stock"] and not was_in_stock and not first_time:
            alerts.append(p)
            # stays marked "out of stock" until the alert is actually delivered
            p = {**p, "in_stock": False}
        entry = {"shop": name, "name": p["name"], "price": p["price"],
                 "url": p["url"], "in_stock": p["in_stock"],
                 "first_seen": (prev or {}).get("first_seen", now)}
        if prev is None or prev.get("in_stock") != p["in_stock"]:
            entry["last_change"] = now
        else:
            entry["last_change"] = prev.get("last_change", now)
        state["products"][p["key"]] = entry

    # Products that disappeared from the shop count as sold out.
    for key, prev in known.items():
        if key not in seen and prev.get("in_stock"):
            prev["in_stock"] = False
            prev["last_change"] = now

    shop_state["initialized"] = True
    return alerts


def recheck_missing(state, shop, products):
    """Products that were in stock but vanished from this check: look them up directly,
    so a product dropping out of search results isn't mistaken for selling out."""
    seen = {p["key"] for p in products}
    missing = [(k, v) for k, v in state["products"].items()
               if v.get("shop") == shop["name"] and v.get("in_stock") and k not in seen]
    found = []
    for key, entry in missing[:20]:
        item = recheck_shopify_product(shop, key, entry)
        if item:
            found.append(item)
    return found


def record_failure(state, shop, error, now, send):
    shop_state = state["shops"].setdefault(shop["name"], {})
    shop_state["consecutive_failures"] = shop_state.get("consecutive_failures", 0) + 1
    shop_state["last_error"] = str(error)[:300]
    if (shop.get("required") and send
            and shop_state["consecutive_failures"] >= FAILURE_ALERT_AFTER
            and not shop_state.get("failure_alerted")):
        if notify(f"Stock checker can't reach {shop['name']}",
                  f"It has failed {shop_state['consecutive_failures']} checks in a row.\n"
                  f"Last error: {str(error)[:200]}",
                  tags=["warning"], priority=3):
            shop_state["failure_alerted"] = True


def record_success(state, shop, now, send):
    shop_state = state["shops"].setdefault(shop["name"], {})
    if shop_state.get("failure_alerted") and send:
        notify(f"Stock checker is reaching {shop['name']} again", "Checks are working normally.",
               tags=["white_check_mark"], priority=2)
    shop_state["consecutive_failures"] = 0
    shop_state["failure_alerted"] = False
    shop_state.pop("last_error", None)
    shop_state["last_success"] = now


def run(args):
    with open(SHOPS_FILE, encoding="utf-8") as f:
        shops = json.load(f)["shops"]
    if args.only:
        shops = [s for s in shops if s["name"].lower() == args.only.lower()]
    state = load_state(args.state)
    before = json.dumps(state, sort_keys=True)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    send = not args.dry_run

    summary_rows = []
    all_alerts = []
    new_shops = []
    for shop in shops:
        log(f"Checking {shop['name']} ...")
        was_initialized = state["shops"].get(shop["name"], {}).get("initialized")
        try:
            products = check_shopify(shop) if shop["type"] == "shopify" else check_html(shop)
        except Exception as e:  # never let one shop break the others
            log(f"  FAILED: {e}")
            record_failure(state, shop, e, now, send)
            summary_rows.append((shop["name"], "failed", str(e)[:150]))
            continue
        if shop["type"] == "shopify":
            products += recheck_missing(state, shop, products)
        in_stock = [p for p in products if p["in_stock"]]
        log(f"  {len(products)} NeeDoh products, {len(in_stock)} in stock")
        if args.dry_run:
            for p in products:
                log(f"    [{'IN STOCK' if p['in_stock'] else 'sold out'}] {p['name']} - {p['price']}")
        record_success(state, shop, now, send)
        alerts = process_shop_result(state, shop, products, now)
        if not was_initialized:
            new_shops.append((shop["name"], len(products), len(in_stock)))
        all_alerts.extend(alerts)
        summary_rows.append((shop["name"], "ok", f"{len(products)} products, {len(in_stock)} in stock"))

    # Send the alerts.
    for i, p in enumerate(all_alerts):
        log(f"ALERT: {p['shop']} - {p['name']} - {p['price']} - {p['url']}")
        if not send:
            continue
        if i < MAX_INDIVIDUAL_ALERTS:
            ok = alert_product(p)
            time.sleep(1)
        else:
            ok = True  # included in the grouped message below
        if ok:
            state["products"][p["key"]]["in_stock"] = True
    extra = all_alerts[MAX_INDIVIDUAL_ALERTS:]
    if send and extra:
        lines = [f"• {p['shop']}: {p['name']} ({p['price']})" for p in extra]
        notify(f"...and {len(extra)} more NeeDoh items in stock", "\n".join(lines)[:3500],
               tags=["tada"], priority=4)

    if new_shops and send:
        lines = [f"• {n}: {total} products, {stock} in stock" for n, total, stock in new_shops]
        notify("NeeDoh stock checker is watching",
               "Started watching:\n" + "\n".join(lines) +
               "\nYou'll get a push when anything new comes into stock.",
               tags=["eyes"], priority=3)

    # Touch the file at least monthly so GitHub sees activity and keeps the schedule on.
    today = now[:10]
    if state.get("heartbeat", "")[:7] != today[:7]:
        state["heartbeat"] = today

    if send and json.dumps(state, sort_keys=True) != before:
        save_state(state, args.state)
        log("State saved.")

    write_github_summary(summary_rows, all_alerts)


def write_github_summary(rows, alerts):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write("## NeeDoh stock check\n\n| Shop | Result | Details |\n|---|---|---|\n")
        for name, result, detail in rows:
            f.write(f"| {name} | {'✅' if result == 'ok' else '⚠️'} {result} | "
                    f"{detail.replace('|', '/')} |\n")
        if alerts:
            f.write("\n### Alerts sent\n\n")
            for p in alerts:
                f.write(f"- **{p['shop']}**: [{p['name']}]({p['url']}) - {p['price']}\n")


def main():
    ap = argparse.ArgumentParser(description="NeeDoh stock checker")
    ap.add_argument("--dry-run", action="store_true",
                    help="check shops and print results, but don't notify or save")
    ap.add_argument("--test-notification", action="store_true",
                    help="send a test push notification and exit")
    ap.add_argument("--only", help="only check the shop with this name")
    ap.add_argument("--state", default=STATE_FILE, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.test_notification:
        ok = notify("NeeDoh stock checker test",
                    "It works! You'll get alerts like this when NeeDoh comes into stock.",
                    click="https://ntfy.sh", tags=["tada"], priority=4)
        log("Test notification sent." if ok else "Test notification FAILED - is NTFY_TOPIC set?")
        sys.exit(0 if ok else 1)

    run(args)


if __name__ == "__main__":
    main()
