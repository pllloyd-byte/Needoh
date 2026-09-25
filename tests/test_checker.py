"""Tests for checker.py. Run with:  python -m unittest discover tests"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import checker  # noqa: E402

SHOP = {"name": "Test Shop", "type": "shopify", "base_url": "https://shop.example", "required": True}
NOW = "2026-01-01T00:00:00+00:00"


def product(key, in_stock, name="NeeDoh Nice Cube"):
    return {"key": f"Test Shop|{key}", "shop": "Test Shop", "name": name,
            "price": "£6.99", "url": f"https://shop.example/products/{key}", "in_stock": in_stock}


def fresh_state():
    return {"products": {}, "shops": {}}


class StockTransitions(unittest.TestCase):
    def test_first_run_is_silent_baseline(self):
        state = fresh_state()
        alerts = checker.process_shop_result(state, SHOP, [product("a", True), product("b", False)], NOW)
        self.assertEqual(alerts, [])
        self.assertTrue(state["products"]["Test Shop|a"]["in_stock"])

    def test_restock_alerts_once(self):
        state = fresh_state()
        checker.process_shop_result(state, SHOP, [product("a", False)], NOW)
        alerts = checker.process_shop_result(state, SHOP, [product("a", True)], NOW)
        self.assertEqual([p["key"] for p in alerts], ["Test Shop|a"])
        # Delivery confirmed -> marked in stock (run() does this after notify succeeds)
        state["products"]["Test Shop|a"]["in_stock"] = True
        self.assertEqual(checker.process_shop_result(state, SHOP, [product("a", True)], NOW), [])

    def test_undelivered_alert_is_retried(self):
        state = fresh_state()
        checker.process_shop_result(state, SHOP, [product("a", False)], NOW)
        self.assertEqual(len(checker.process_shop_result(state, SHOP, [product("a", True)], NOW)), 1)
        # notify failed, so in_stock stayed False -> alert again next time
        self.assertEqual(len(checker.process_shop_result(state, SHOP, [product("a", True)], NOW)), 1)

    def test_new_product_in_stock_alerts(self):
        state = fresh_state()
        checker.process_shop_result(state, SHOP, [product("a", False)], NOW)
        alerts = checker.process_shop_result(state, SHOP, [product("a", False), product("new", True)], NOW)
        self.assertEqual([p["key"] for p in alerts], ["Test Shop|new"])

    def test_new_product_sold_out_does_not_alert(self):
        state = fresh_state()
        checker.process_shop_result(state, SHOP, [product("a", False)], NOW)
        self.assertEqual(checker.process_shop_result(state, SHOP, [product("b", False)], NOW), [])

    def test_sell_out_then_restock_alerts_again(self):
        state = fresh_state()
        checker.process_shop_result(state, SHOP, [product("a", True)], NOW)
        checker.process_shop_result(state, SHOP, [product("a", False)], NOW)
        self.assertEqual(len(checker.process_shop_result(state, SHOP, [product("a", True)], NOW)), 1)

    def test_disappeared_product_counts_as_sold_out(self):
        state = fresh_state()
        checker.process_shop_result(state, SHOP, [product("a", True), product("b", True)], NOW)
        checker.process_shop_result(state, SHOP, [product("b", True)], NOW)
        self.assertFalse(state["products"]["Test Shop|a"]["in_stock"])
        alerts = checker.process_shop_result(state, SHOP, [product("a", True), product("b", True)], NOW)
        self.assertEqual([p["key"] for p in alerts], ["Test Shop|a"])

    def test_empty_glitch_is_ignored(self):
        state = fresh_state()
        checker.process_shop_result(state, SHOP, [product("a", True)], NOW)
        checker.process_shop_result(state, SHOP, [], NOW)
        self.assertTrue(state["products"]["Test Shop|a"]["in_stock"])


class ShopifyParsing(unittest.TestCase):
    def test_availability_and_price(self):
        p = checker.shopify_product(SHOP, {
            "id": 1, "handle": "nice-cube", "title": "NeeDoh Nice Cube &amp; Friends",
            "variants": [{"available": False, "price": "5.00"},
                         {"available": True, "price": "6.99"},
                         {"available": True, "price": "7.99"}]})
        self.assertTrue(p["in_stock"])
        self.assertEqual(p["price"], "from £6.99")
        self.assertEqual(p["name"], "NeeDoh Nice Cube & Friends")
        self.assertEqual(p["url"], "https://shop.example/products/nice-cube")

    def test_sold_out(self):
        p = checker.shopify_product(SHOP, {"id": 2, "handle": "x", "title": "NeeDoh",
                                           "variants": [{"available": False, "price": "4.50"}]})
        self.assertFalse(p["in_stock"])
        self.assertEqual(p["price"], "£4.50")

    def test_keyword_filter(self):
        self.assertTrue(checker._mentions_needoh({"title": "Nee Doh Gumdrop"}))
        self.assertTrue(checker._mentions_needoh({"title": "Gumdrop", "vendor": "NeeDoh"}))
        self.assertTrue(checker._mentions_needoh({"title": "Cube", "tags": ["nee-doh"]}))
        self.assertFalse(checker._mentions_needoh({"title": "Wooden Train", "vendor": "Bigjigs"}))

    def test_collection_fallback(self):
        shop = {**SHOP, "collections": ["missing", "needoh"]}
        body = '{"products": [{"id": 1, "handle": "a", "title": "NeeDoh A", "variants": [{"available": true, "price": "3"}]}]}'

        def fake_fetch(url, want_json=False):
            return (404, "") if "/missing/" in url else (200, body)

        with mock.patch.object(checker, "fetch", fake_fetch):
            products = checker.check_shopify(shop)
        self.assertEqual(len(products), 1)
        self.assertTrue(products[0]["in_stock"])

    def test_blocked_shop_raises(self):
        with mock.patch.object(checker, "fetch", lambda url, want_json=False: (200, "<html>captcha</html>")):
            with self.assertRaises(checker.FetchError):
                checker.check_shopify({**SHOP, "collections": ["needoh"]})


LISTING_HTML = """
<html><body>
<a href="/needoh-gumdrop">Gumdrop</a>
<a href="https://www.shop.example/needoh-nice-cube?ref=list">Nice Cube</a>
<a href="/needoh">All NeeDoh</a>
<a href="/wooden-train">Train</a>
<a href="https://elsewhere.example/needoh-x">Other site</a>
</body></html>
"""

PRODUCT_LD = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"NeeDoh Gumdrop",
 "offers":{"@type":"Offer","price":"8.99","priceCurrency":"GBP","availability":"https://schema.org/InStock"}}
</script></head></html>"""

LISTING_LD = """<script type="application/ld+json">
{"@type":"ItemList","itemListElement":[
 {"@type":"ListItem","item":{"@type":"Product","name":"NeeDoh Dream Drop","url":"/needoh-dream-drop",
   "offers":{"availability":"http://schema.org/OutOfStock","price":7,"priceCurrency":"GBP"}}},
 {"@type":"ListItem","item":{"@type":"Product","name":"Other Toy","url":"/other",
   "offers":{"availability":"InStock"}}}]}
</script>"""


class HtmlParsing(unittest.TestCase):
    def test_links(self):
        links = checker.needoh_links(LISTING_HTML, "https://www.shop.example/needoh", "https://www.shop.example")
        self.assertEqual(links, ["https://www.shop.example/needoh-gumdrop",
                                 "https://www.shop.example/needoh-nice-cube"])

    def test_product_page_json_ld(self):
        [p] = checker.json_ld_products(PRODUCT_LD, "https://www.shop.example/needoh-gumdrop")
        self.assertTrue(p["in_stock"])
        self.assertEqual(p["price"], "£8.99")

    def test_listing_json_ld_filters_non_needoh(self):
        shop = {"name": "H", "type": "html", "base_url": "https://www.shop.example",
                "listing_urls": ["https://www.shop.example/needoh"]}
        with mock.patch.object(checker, "fetch", lambda url, want_json=False: (200, LISTING_LD)):
            products = checker.check_html(shop)
        self.assertEqual([p["name"] for p in products], ["NeeDoh Dream Drop"])
        self.assertFalse(products[0]["in_stock"])
        self.assertEqual(products[0]["price"], "£7.00")

    def test_visits_product_pages(self):
        shop = {"name": "H", "type": "html", "base_url": "https://www.shop.example",
                "listing_urls": ["https://www.shop.example/needoh"]}
        pages = {"https://www.shop.example/needoh": LISTING_HTML,
                 "https://www.shop.example/needoh-gumdrop": PRODUCT_LD,
                 "https://www.shop.example/needoh-nice-cube":
                     '<meta property="og:title" content="NeeDoh Nice Cube">'
                     '<meta property="product:availability" content="out of stock">'}
        with mock.patch.object(checker, "fetch", lambda url, want_json=False: (200, pages[url])), \
             mock.patch.object(checker.time, "sleep"):
            products = {p["name"]: p for p in checker.check_html(shop)}
        self.assertTrue(products["NeeDoh Gumdrop"]["in_stock"])
        self.assertFalse(products["NeeDoh Nice Cube"]["in_stock"])

    def test_blocked_site_raises(self):
        shop = {"name": "H", "type": "html", "base_url": "https://www.shop.example",
                "listing_urls": ["https://www.shop.example/needoh"]}
        with mock.patch.object(checker, "fetch", lambda url, want_json=False: (403, "")):
            with self.assertRaises(checker.FetchError):
                checker.check_html(shop)


class EndToEnd(unittest.TestCase):
    def test_run_sends_alert_and_saves(self):
        import json
        import tempfile
        state_path = os.path.join(tempfile.mkdtemp(), "state.json")
        stock = {"Jukupop": False}

        def fake_check(shop):
            return [{"key": f"{shop['name']}|a", "shop": shop["name"], "name": "NeeDoh Cube",
                     "price": "£5.00", "url": "https://x.example/a",
                     "in_stock": stock.get(shop["name"], False)}]

        sent = []

        def fake_notify(title, message, **kwargs):
            sent.append(title)
            return True

        args = mock.Mock(dry_run=False, only=None, state=state_path)
        with mock.patch.object(checker, "check_shopify", side_effect=fake_check), \
             mock.patch.object(checker, "check_html", side_effect=fake_check), \
             mock.patch.object(checker, "notify", side_effect=fake_notify), \
             mock.patch.object(checker.time, "sleep"):
            checker.run(args)                        # first run: baseline summary only
            self.assertEqual(sent, ["NeeDoh stock checker is watching"])
            sent.clear()
            stock["Jukupop"] = True
            checker.run(args)                        # restock at one shop
            self.assertEqual(sent, ["NeeDoh in stock at Jukupop!"])
            sent.clear()
            checker.run(args)                        # still in stock: no repeat alert
            self.assertEqual(sent, [])
        with open(state_path) as f:
            self.assertTrue(json.load(f)["products"]["Jukupop|a"]["in_stock"])


if __name__ == "__main__":
    unittest.main()
