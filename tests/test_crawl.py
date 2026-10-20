import json
import os
import sys
import time
import types
import urllib.error

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "crawler"))
import crawl  # noqa: E402


# --------------------------------------------------------------------------------------------
# Sample pages shaped like the Odoo 19 shop of the store (structure observed on the live site)
# --------------------------------------------------------------------------------------------
def card(pid, name, price, old=None, slug="item"):
    old_html = f'<del class="text-muted me-1"><span class="oe_currency_value">{old}</span></del>' if old else ""
    return f"""
    <div class="oe_product g-col-6" data-name="{name}">
      <form><div class="oe_product_cart">
        <a class="oe_product_image_link" href="/ar/shop/{slug}-{pid}?category=1"><img class="oe_product_image_img" src="/web/image/product.template/{pid}/image_512"/></a>
        <div class="o_wsale_product_information">
          <h6 class="o_wsale_products_item_title"><a href="/ar/shop/{slug}-{pid}">{name}</a></h6>
          <div class="product_price">{old_html}<span class="oe_currency_value">{price}</span> ل.د</div>
        </div>
      </div></form>
    </div>"""


def listing(cards, last_page=1, extra=""):
    pager = "".join(f'<li class="page-item"><a class="page-link" href="/ar/shop/page/{n}">{n}</a></li>' for n in range(1, last_page + 1))
    return f"<html><body>{extra}<div class='grid'>{''.join(cards)}</div><ul class='pagination'>{pager}</ul></body></html>"


SIDEBAR = """
<nav><ul class="nav">
  <li><a href="/ar/shop">كافة المنتجات</a></li>
  <li><a href="/ar/shop/category/gaming-2">الكمبيوتر والجيمينج</a>
      <ul><li><a href="/ar/shop/category/cases-5">كيسات</a></li></ul></li>
  <li><a href="/ar/shop/category/audio-3">اجهزة الصوت</a></li>
</ul></nav>
<div class="accordion">
  <div class="accordion-item">
    <h2 class="accordion-header"><button><b>العلامة التجارية</b></button></h2>
    <div class="accordion-body">
      <input type="checkbox" name="attribute_value" value="9-1" id="9-1"/><label for="9-1">Apple</label>
      <input type="checkbox" name="attribute_value" value="9-3" id="9-3"/><label for="9-3">Jbl</label>
    </div>
  </div>
  <div class="accordion-item">
    <h2 class="accordion-header"><button><b>اللون</b></button></h2>
    <div class="accordion-body"><input type="checkbox" name="attribute_value" value="4-7" id="4-7"/><label for="4-7">أسود</label></div>
  </div>
</div>"""


def product_page(availability="InStock", name="هيكل XPG", desc="وصف<br>طويل", extra_imgs=True):
    ld = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "description": desc,
        "offers": {"@type": "Offer", "price": 595.0, "priceCurrency": "LYD", "availability": f"https://schema.org/{availability}"},
    }
    org = {"@context": "http://schema.org", "@type": "Organization", "name": "Al-Nukhba"}
    imgs = """
      <img src="/web/image/product.product/75/image_1920/main-name?unique=ab"/>
      <img src="/web/image/product.image/9/image_1920/front?unique=cd"/>
      <div class="indicators"><img src="/web/image/product.product/75/image_128/main-name"/>
      <img src="/web/image/product.image/9/image_128/front"/></div>""" if extra_imgs else ""
    return f"""<html><head>
      <script type="application/ld+json">{json.dumps(org)}</script>
      <script type="application/ld+json">{json.dumps([ld])}</script></head>
      <body><nav><ol class="breadcrumb"><li>كل المنتجات</li><li>كيسات</li></ol></nav>
      <div id="o-carousel-product">{imgs}</div></body></html>"""


# --------------------------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------------------------
def test_parse_number_understands_store_prices_and_arabic_digits():
    assert crawl.parse_number("1,590.000 ل.د") == 1590.0
    assert crawl.parse_number("45.000") == 45.0
    assert crawl.parse_number("١٬٥٩٠٫٥") == 1590.5
    assert crawl.parse_number("اتصل بنا") is None


def test_parse_listing_reads_id_name_price_and_discount():
    cards = crawl.parse_listing(listing([card(75, "هيكل كمبيوتر", "495.000", old="595.000"), card(76, "كيس", "45.000")]))
    assert [c.id for c in cards] == [75, 76]
    assert cards[0].name == "هيكل كمبيوتر"
    assert cards[0].price == 495.0 and cards[0].old_price == 595.0
    assert cards[1].price == 45.0 and cards[1].old_price is None
    assert cards[0].href == "/ar/shop/item-75"


def test_parse_last_page():
    assert crawl.parse_last_page(listing([], last_page=61)) == 61
    assert crawl.parse_last_page(listing([], last_page=1)) == 1
    assert crawl.parse_last_page("<html></html>") == 1


def test_categories_keep_only_the_top_level():
    cats = crawl.parse_categories(SIDEBAR.replace('<li><a href="/ar/shop">كافة المنتجات</a></li>', ""))
    assert {c.id for c in cats} == {2, 3}
    assert {c.name for c in cats} == {"الكمبيوتر والجيمينج", "اجهزة الصوت"}


def test_brand_filters_only_from_the_brand_group():
    brands = crawl.parse_brand_filters(SIDEBAR)
    assert [(b.value, b.name) for b in brands] == [("9-1", "Apple"), ("9-3", "Jbl")]


@pytest.mark.parametrize("raw,expected", [
    ("https://schema.org/InStock", "available"),
    ("https://schema.org/OutOfStock", "out"),
    ("http://schema.org/LimitedAvailability", "low"),
    ("https://schema.org/PreOrder", "on_request"),
    ("", None),
    ("https://schema.org/Strange", None),
])
def test_map_availability(raw, expected):
    assert crawl.map_availability(raw) == expected


def test_parse_product_status_description_and_gallery():
    d = crawl.parse_product(product_page("OutOfStock"), "https://www.alnukhbastore.com")
    assert d["stock"] == "out"
    assert d["name"] == "هيكل XPG"
    assert d["description"] == "وصف\nطويل"
    # one entry per picture (the small thumbnails are the same pictures), large size 1024
    assert d["images"] == [
        "https://www.alnukhbastore.com/web/image/product.product/75/image_1024/main-name",
        "https://www.alnukhbastore.com/web/image/product.image/9/image_1024/front",
    ]
    assert crawl.parse_product(product_page("InStock"), "https://x.test")["stock"] == "available"


def test_parse_product_without_structured_data_has_no_status():
    d = crawl.parse_product("<html><body>nothing</body></html>", "https://x.test")
    assert d["stock"] is None and d["images"] == []


# --------------------------------------------------------------------------------------------
# Whole run against a fake website (no network)
# --------------------------------------------------------------------------------------------
class FakeSite:
    """Serves the pages of a tiny shop: 3 products over two pages, 2 categories, 2 brands."""

    def __init__(self):
        self.hits = []
        self.block = False
        self.sold_out = {2}
        self.products = {
            1: ("هاتف", "1,850.000", "9-1", 2),
            2: ("سماعة", "99.000", "9-3", 3),
            3: ("كيس", "495.000", "9-1", 2),
        }

    def page_for(self, ids, last, page):
        return listing([card(i, self.products[i][0], self.products[i][1]) for i in ids], last_page=last)

    def get(self, path):
        self.hits.append(path)
        if self.block:
            raise crawl.Blocked("429")
        p = path.split("?")[0]
        q = path.split("?")[1] if "?" in path else ""
        if p == "/robots.txt":
            return "User-agent: *\nAllow: /\n"
        if "category/gaming-2" in p:
            return self.page_for([1, 3], 1, 1)
        if "category/audio-3" in p:
            return self.page_for([2], 1, 1)
        if "attribute_value=9-1" in q:
            return self.page_for([1, 3], 1, 1)
        if "attribute_value=9-3" in q:
            return self.page_for([2], 1, 1)
        if p.endswith("/shop"):
            return listing([card(i, self.products[i][0], self.products[i][1]) for i in (1, 2)], last_page=2, extra=SIDEBAR)
        if p.endswith("/shop/page/2"):
            return self.page_for([3], 2, 2)
        pid = crawl.trailing_id(p)
        if pid in self.products:
            return product_page("OutOfStock" if pid in self.sold_out else "InStock", name=self.products[pid][0])
        raise urllib.error.HTTPError(path, 404, "nf", {}, None)


def run_with(fake, tmp_path, prev=None, **overrides):
    out = tmp_path / "site"
    args = types.SimpleNamespace(
        site="https://www.alnukhbastore.com", lang_prefix="/ar", out=str(out),
        prev_state=str(prev) if prev else "", budget=1, interval=0.0, max_pages=10,
        min_products=1, remap_hours=6.0, max_minutes=5.0,
    )
    for k, v in overrides.items():
        setattr(args, k, v)
    original = crawl.Http.get
    crawl.Http.get = lambda self, path: fake.get(path)
    try:
        code = crawl.run(args, log=lambda *_: None)
    finally:
        crawl.Http.get = original
    return code, out


def test_first_run_builds_catalogue_details_categories_brands_and_status(tmp_path):
    fake = FakeSite()
    code, out = run_with(fake, tmp_path)
    assert code == 0
    cat = json.loads((out / "v1" / "catalog.json").read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in cat["products"]}
    assert set(by_id) == {1, 2, 3}
    assert by_id[2]["stock"] == "out" and by_id[1]["stock"] == "available"
    assert by_id[1]["price"] == 1850.0
    assert by_id[1]["brand"] == "Apple" and by_id[2]["brand"] == "Jbl"
    assert by_id[1]["category_name"] == "الكمبيوتر والجيمينج" and by_id[2]["category_id"] == 3
    assert by_id[1]["image_url"] == "https://www.alnukhbastore.com/web/image/product.template/1/image_512"
    assert by_id[1]["is_new"] is False and by_id[1]["added_at"] is None  # present before we started watching
    cats = {c["id"]: c for c in cat["categories"]}
    assert cats[2]["count"] == 2 and cats[3]["count"] == 1
    assert cats[3]["image_url"] is None  # its only product is sold out
    detail = json.loads((out / "v1" / "products" / "1.json").read_text(encoding="utf-8"))
    assert detail["description"] == "وصف\nطويل" and len(detail["images"]) == 2
    # nothing but plain GETs of public pages
    assert all(h.startswith("/") for h in fake.hits)


def test_second_run_rechecks_only_the_budget_and_flags_new_products(tmp_path):
    fake = FakeSite()
    code, out = run_with(fake, tmp_path)
    assert code == 0
    state = out / "v1" / "state.json"
    # a new product appears and product 2 comes back in stock
    fake.products[4] = ("شاحن", "60.000", "9-1", 2)
    fake.sold_out = set()
    orig_get = fake.get

    def get(path):
        p = path.split("?")[0]
        if p.endswith("/shop/page/2"):
            return listing([card(3, "كيس", "495.000"), card(4, "شاحن", "60.000")], last_page=2)
        return orig_get(path)

    fake.get = get
    fake.hits.clear()
    code, out2 = run_with(fake, tmp_path / "second", prev=state, budget=2)
    assert code == 0
    page_hits = [h for h in fake.hits if h.startswith("/ar/shop/item-")]
    assert len(page_hits) == 2  # only the budget of product pages was opened
    cat = json.loads((out2 / "v1" / "catalog.json").read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in cat["products"]}
    assert by_id[4]["is_new"] is True and by_id[4]["added_at"]
    assert by_id[1]["is_new"] is False
    assert by_id[4]["stock"] == "available"  # new products are always checked first


def test_website_asking_to_stop_keeps_everything_and_writes_nothing(tmp_path):
    fake = FakeSite()
    fake.block = True
    code, out = run_with(fake, tmp_path)
    assert code == 4
    assert not (out / "v1" / "catalog.json").exists()


def test_too_few_products_never_replaces_good_data(tmp_path):
    fake = FakeSite()
    code, out = run_with(fake, tmp_path, min_products=50)
    assert code == 2
    assert not (out / "v1" / "catalog.json").exists()


def test_the_crawler_only_ever_reads(tmp_path):
    src = open(os.path.join(os.path.dirname(__file__), "..", "crawler", "crawl.py"), encoding="utf-8").read()
    assert 'method="GET"' in src
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert f'method="{verb}"' not in src
    assert "urlencode" in src and "data=" not in src.replace("data = ", "")


# ---------------------------------------------------------------------------------------------
# Arabic words in addresses (found on the first real run)
# ---------------------------------------------------------------------------------------------
def test_encode_url_percent_encodes_arabic_and_keeps_encoded_parts():
    raw = "https://www.alnukhbastore.com/ar/shop/category/\u0647\u0648\u0627\u062a\u0641-5/page/2?attribute_value=9-1"
    out = crawl.encode_url(raw)
    assert out.isascii()
    assert out.startswith("https://www.alnukhbastore.com/ar/shop/category/%D9%87")
    assert out.endswith("/page/2?attribute_value=9-1")
    assert crawl.encode_url(out) == out  # already encoded: unchanged


def test_http_get_sends_only_ascii_addresses(monkeypatch):
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    def fake_urlopen(req, timeout=0):
        seen.append(req.full_url)
        assert req.get_method() == "GET"
        return Resp()

    monkeypatch.setattr(crawl.urllib.request, "urlopen", fake_urlopen)
    http = crawl.Http("https://www.alnukhbastore.com", min_interval=0)
    assert http.get("/ar/shop/\u0643\u064a\u0633-76") == "ok"
    assert seen and seen[0].isascii()
