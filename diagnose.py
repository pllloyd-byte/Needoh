"""One-off diagnostics: shows what each shop returns to GitHub's servers."""
import json, re, urllib.request, urllib.error, gzip

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"

def get(url, cookie=None, accept="*/*"):
    h = {"User-Agent": UA, "Accept": accept, "Accept-Language": "en-GB,en;q=0.9", "Accept-Encoding": "gzip"}
    if cookie: h["Cookie"] = cookie
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=25)
        body, status, final, hdrs = r.read(), r.status, r.url, r.headers
    except urllib.error.HTTPError as e:
        body, status, final, hdrs = e.read(), e.code, url, e.headers
    except Exception as e:
        return f"ERROR {e}", "", {}
    if hdrs.get("Content-Encoding") == "gzip":
        try: body = gzip.decompress(body)
        except Exception: pass
    text = body.decode("utf-8", "replace")
    info = f"{status} final={final} server={hdrs.get('server')} len={len(text)}"
    return info, text, hdrs

def products(url, cookie=None):
    info, text, _ = get(url, cookie, "application/json")
    try:
        ps = json.loads(text).get("products", [])
        titles = [p["title"] for p in ps][:5]
        avail = sum(any(v.get("available") for v in p.get("variants", [])) for p in ps)
        print(f"  {url} cookie={bool(cookie)}\n    {info} products={len(ps)} available={avail} e.g. {titles}")
    except Exception:
        print(f"  {url} cookie={bool(cookie)}\n    {info} NOT JSON: {text[:200]!r}")

GB = "localization=GB; cart_currency=GBP"
for base, handles in [("https://jukupop.com", ["needoh-squishy-fidget-toys-shop-stress-balls-fidget-fun"]),
                      ("https://plushparadise.co.uk", ["needoh"])]:
    print(f"=== {base}")
    info, text, h = get(base + "/meta.json")
    print("  meta.json:", info, text[:300])
    for hd in handles:
        for c in (None, GB):
            products(f"{base}/collections/{hd}/products.json?limit=250", c)
        products(f"{base}/en-gb/collections/{hd}/products.json?limit=250")
        info, text, h = get(f"{base}/collections/{hd}")
        links = sorted(set(re.findall(r'/products/([a-z0-9\-]+)', text)))
        print(f"  collection HTML: {info} product links={len(links)} e.g. {links[:5]}")
        print("  title:", re.findall(r"<title>(.*?)</title>", text, re.S)[:1])
        if links:
            info, text, h = get(f"{base}/products/{links[0]}.js", accept="application/json")
            print(f"  product .js: {info} {text[:200]!r}")
    for c in (None, GB):
        products(f"{base}/products.json?limit=250", c)
    info, text, h = get(f"{base}/search/suggest.json?q=needoh&resources[type]=product&resources[limit]=10", accept="application/json")
    print("  suggest:", info, text[:300])

for url in ["https://www.mulberrybush.co.uk/needoh", "https://www.menkind.co.uk/toys/fidget-toys/needoh",
            "https://www.urbanoutfitters.com/en-gb/search?q=needoh", "https://www.mulberrybush.co.uk/products.json?limit=5"]:
    info, text, h = get(url, accept="text/html")
    print(f"=== {url}\n  {info}\n  headers: { {k: v for k, v in h.items() if k.lower() in ('server','cf-ray','x-akamai-transformed','set-cookie','x-shopid','powered-by','x-powered-by','via')} }\n  body: {text[:300]!r}")
