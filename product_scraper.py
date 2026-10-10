"""비비빈스와 일렉샵의 공개 기기 상품 정보를 수집한다."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

import aiohttp
from bs4 import BeautifulSoup, Comment
from PIL import Image, ImageOps, UnidentifiedImageError


PRICE_MARKUP = 3_000
USER_AGENT = "Mozilla/5.0 (compatible; V4P3-SP0T-ProductSync/1.0)"
BIBIBINS_BASE = "https://xn--jk1bo8sa06werixle.com"
ELECSHOP_BASE = "https://xn--352bl9av7r2tn.com"
ELECSHOP_DEVICES = f"{ELECSHOP_BASE}/product-category/%EA%B0%9C%EB%B0%A9%ED%98%95-%EA%B8%B0%EA%B8%B0/"
PRODUCT_LOGO_COVER_PATH = Path(__file__).with_name("product_logo_cover.png")
MAX_PRODUCT_IMAGE_BYTES = 15 * 1024 * 1024
MAX_PRODUCT_IMAGE_PIXELS = 40_000_000


@dataclass(slots=True)
class ScrapedProduct:
    source_site: str
    source_key: str
    source_url: str
    category: str
    name: str
    description: str
    source_price: int
    price: int
    image_url: str
    option_label: str
    options: list[str]
    stock: int
    image_data: bytes | None = None
    image_mime: str = ""


def _number(text: str) -> int:
    match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)", text or "")
    return int(float(match.group(1).replace(",", ""))) if match else 0


def _unique(values) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _image_url(node, base_url: str) -> str:
    image = node.select_one("img")
    if image:
        for attr in ("data-large_image", "data-src", "data-lazy-src", "ec-data-src", "src"):
            value = image.get(attr)
            if value and "prod_loading.gif" not in value and "adult.png" not in value:
                return urljoin(base_url, value)
    for comment in node.find_all(string=lambda value: isinstance(value, Comment)):
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', str(comment), re.I)
        if match:
            return urljoin(base_url, match.group(1))
    return urljoin(base_url, image.get("src")) if image and image.get("src") else ""


def _bibibins_card(node, category: str, page_url: str) -> ScrapedProduct | None:
    link = node.select_one('a[href*="/product/"]')
    image = node.select_one(".prdImg img")
    name = (image.get("alt") if image else "") or ""
    if not name:
        name_spans = node.select(".name a > span:not(.title)")
        name = name_spans[-1].get_text(" ", strip=True) if name_spans else ""
    price_node = node.select_one('[column_name="product_price"]')
    if not link or not name or not price_node:
        return None
    product_match = re.search(r"anchorBoxId_(\d+)", node.get("id", ""))
    if not product_match:
        product_match = re.search(r"/(\d+)/category/", link.get("href", ""))
    source_key = product_match.group(1) if product_match else link.get("href", "")
    source_price = _number(price_node.get_text(" ", strip=True))
    if not source_price:
        return None
    summary = node.select_one('[column_name="summary_desc"]')
    summary_values = summary.select("span:not(.title)") if summary else []
    option_nodes = node.select(".option option[value], .option li")
    options = _unique(item.get_text(" ", strip=True) for item in option_nodes)
    if not options:
        options = ["색상은 구매티켓에서 확인"]
    return ScrapedProduct(
        source_site="bibibins",
        source_key=source_key,
        source_url=urljoin(page_url, link.get("href")),
        category=category,
        name=name.strip(),
        description=summary_values[-1].get_text(" ", strip=True) if summary_values else "",
        source_price=source_price,
        price=source_price + PRICE_MARKUP,
        image_url=_image_url(node, page_url),
        option_label="색상",
        options=options,
        stock=0 if "soldout" in " ".join(node.get("class", [])).lower() else 99,
    )


async def _soup(session: aiohttp.ClientSession, url: str) -> BeautifulSoup:
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
        response.raise_for_status()
        return BeautifulSoup(await response.read(), "html.parser")


async def _login_bibibins(session: aiohttp.ClientSession) -> bool:
    member_id = os.getenv("BIBIBINS_MEMBER_ID", "").strip()
    member_password = os.getenv("BIBIBINS_MEMBER_PASSWORD", "").strip()
    if not member_id and not member_password:
        return False
    if not member_id or not member_password:
        raise RuntimeError("비비빈스 로그인 환경변수의 아이디와 비밀번호를 모두 설정해 주세요.")

    login_url = f"{BIBIBINS_BASE}/member/login.html"
    login_soup = await _soup(session, login_url)
    form = login_soup.select_one('form[action*="/Member/login"]')
    if not form:
        raise RuntimeError("비비빈스 로그인 화면을 읽지 못했습니다.")
    payload = {
        field.get("name"): field.get("value", "")
        for field in form.select('input[type="hidden"][name]')
    }
    payload.update({"member_id": member_id, "member_passwd": member_password})
    action = urljoin(login_url, form.get("action") or "/exec/front/Member/login/")
    async with session.post(
        action, data=payload, headers={"Referer": login_url},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        response.raise_for_status()
        await response.read()

    async with session.get(
        f"{BIBIBINS_BASE}/myshop/index.html",
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        response.raise_for_status()
        await response.read()
        if "/member/login" in response.url.path:
            raise RuntimeError("비비빈스 로그인에 실패했습니다. 계정 정보와 성인 인증 상태를 확인해 주세요.")
    return True


async def _login_elecshop(session: aiohttp.ClientSession) -> bool:
    member_id = os.getenv("ELECSHOP_MEMBER_ID", "").strip()
    member_password = os.getenv("ELECSHOP_MEMBER_PASSWORD", "").strip()
    if not member_id and not member_password:
        return False
    if not member_id or not member_password:
        raise RuntimeError("일렉샵 로그인 환경변수의 아이디와 비밀번호를 모두 설정해 주세요.")

    account_url = f"{ELECSHOP_BASE}/my-account/"
    login_soup = await _soup(session, account_url)
    form = login_soup.select_one("form.woocommerce-form-login")
    if not form:
        # 이미 유효한 로그인 쿠키가 있는 경우에도 성공으로 처리한다.
        return True
    payload = {
        field.get("name"): field.get("value", "")
        for field in form.select('input[type="hidden"][name]')
    }
    submit = form.select_one('button[name="login"]')
    payload.update({
        "username": member_id,
        "password": member_password,
        "rememberme": "forever",
        "login": submit.get("value", "로그인") if submit else "로그인",
    })
    action = urljoin(account_url, form.get("action") or account_url)
    async with session.post(
        action, data=payload, headers={"Referer": account_url},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as response:
        response.raise_for_status()
        verification = BeautifulSoup(await response.read(), "html.parser")
        if verification.select_one("form.woocommerce-form-login"):
            raise RuntimeError("일렉샵 로그인에 실패했습니다. 계정 정보를 확인해 주세요.")
    return True


def _bibibins_detail_options(soup: BeautifulSoup) -> list[str]:
    options = []
    selectors = (
        '.xans-product-option select option[value]',
        'select[id^="product_option_id"] option[value]',
        '.ec-product-button li[data-value]',
        '.ec-product-button li[option_value]',
    )
    for selector in selectors:
        for node in soup.select(selector):
            value = node.get("data-value") or node.get("option_value") or node.get("value")
            text = node.get_text(" ", strip=True) or value or ""
            normalized = re.sub(r"\s*\([^)]*(?:품절|재고)[^)]*\)\s*$", "", text).strip()
            if not value:
                continue
            if normalized and not any(word in normalized for word in ("선택", "필수", "옵션")):
                options.append(normalized)
    return _unique(options)


async def _hydrate_bibibins_options(
    session: aiohttp.ClientSession, products: list[ScrapedProduct],
) -> None:
    semaphore = asyncio.Semaphore(3)

    async def load(product: ScrapedProduct):
        async with semaphore:
            try:
                soup = await _soup(session, product.source_url)
                options = _bibibins_detail_options(soup)
                if options:
                    product.options = options
                    product.option_label = _option_label(options)
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError):
                return
            await asyncio.sleep(0.12)

    await asyncio.gather(*(load(product) for product in products))


def _source_site_for_url(url: str) -> str:
    """링크 가져오기에 허용된 쇼핑몰인지 확인한다 (SSRF 방지 포함)."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("http 또는 https로 시작하는 올바른 상품 링크를 입력해 주세요.")
    try:
        hostname = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise RuntimeError("상품 링크의 주소를 확인해 주세요.") from exc
    allowed = {
        urlparse(BIBIBINS_BASE).hostname: "bibibins",
        urlparse(ELECSHOP_BASE).hostname: "elecshop",
    }
    site = allowed.get(hostname)
    if not site:
        raise RuntimeError("비비빈스 또는 일렉샵 링크만 가져올 수 있습니다.")
    return site


def _cover_store_logo(source: bytes, source_site: str) -> bytes:
    """상품 이미지의 쇼핑몰 로고 영역을 사용자가 제공한 흰색 이미지로 덮는다."""
    if source_site != "bibibins":
        raise ValueError("지원하지 않는 상품 이미지 출처입니다.")
    try:
        with Image.open(BytesIO(source)) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened).convert("RGBA")
        width, height = image.size
        if width < 40 or height < 40 or width * height > MAX_PRODUCT_IMAGE_PIXELS:
            raise ValueError("상품 이미지 크기를 처리할 수 없습니다.")
        background = Image.new("RGBA", image.size, "white")
        background.alpha_composite(image)
        boxes = (
            (
                round(width * 0.015), round(height * 0.015),
                round(width * 0.49), round(height * 0.18),
            ),
            (
                round(width * 0.70), round(height * 0.01),
                round(width * 0.99), round(height * 0.18),
            ),
        )
        with Image.open(PRODUCT_LOGO_COVER_PATH) as opened_cover:
            cover = ImageOps.exif_transpose(opened_cover).convert("RGBA")
        for box in boxes:
            resized_cover = cover.resize(
                (box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS,
            )
            background.alpha_composite(resized_cover, (box[0], box[1]))
        output = BytesIO()
        background.convert("RGB").save(output, format="WEBP", quality=92, method=4)
        return output.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("상품 이미지를 처리하지 못했습니다.") from exc


async def _prepare_product_images(
    session: aiohttp.ClientSession, products: list[ScrapedProduct],
) -> None:
    semaphore = asyncio.Semaphore(4)

    async def load(product: ScrapedProduct):
        # 일렉샵 상품 이미지에는 제거할 쇼핑몰 로고가 없으므로 원본 URL을 유지한다.
        if product.source_site != "bibibins" or not product.image_url:
            return
        async with semaphore:
            try:
                async with session.get(
                    product.image_url, timeout=aiohttp.ClientTimeout(total=30),
                ) as response:
                    response.raise_for_status()
                    if response.content_length and response.content_length > MAX_PRODUCT_IMAGE_BYTES:
                        return
                    source = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        source.extend(chunk)
                        if len(source) > MAX_PRODUCT_IMAGE_BYTES:
                            return
                product.image_data = await asyncio.to_thread(
                    _cover_store_logo, bytes(source), product.source_site,
                )
                product.image_mime = "image/webp"
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return

    await asyncio.gather(*(load(product) for product in products))


def _with_query(url: str, **values) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({key: str(value) for key, value in values.items()})
    return urlunparse(parsed._replace(query=urlencode(query)))


async def _scrape_bibibins_url(
    session: aiohttp.ClientSession, url: str, hydrate_options: bool = False,
) -> list[ScrapedProduct]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    product_match = re.search(r"/(\d+)(?:/category/|/?)", parsed.path)
    target_key = product_match.group(1) if "/product/" in parsed.path and product_match else None
    if not target_key:
        values = query.get("product_no", [])
        target_key = values[0] if values and values[0].isdigit() else None

    category_match = re.search(r"/category/(\d+)", parsed.path)
    category_number = (
        (query.get("cate_no") or [None])[0]
        or (category_match.group(1) if category_match else None)
    )
    category_numbers = [category_number] if category_number and str(category_number).isdigit() else [50, 52]
    products: dict[str, ScrapedProduct] = {}
    for number in category_numbers:
        if "/product/list" in parsed.path and str(number) == str(category_number):
            base_url = url
        else:
            base_url = f"{BIBIBINS_BASE}/product/list.html?cate_no={number}"
        seen_in_category: set[str] = set()
        for page in range(1, 21):
            page_url = _with_query(base_url, cate_no=number, page=page)
            soup = await _soup(session, page_url)
            cards = soup.select(".xans-product-listnormal .prdList > li")
            if not cards:
                break
            new_on_page = 0
            for card in cards:
                product = _bibibins_card(card, "선택 카테고리", page_url)
                if not product:
                    continue
                if target_key and product.source_key != target_key:
                    continue
                if product.source_key not in seen_in_category:
                    seen_in_category.add(product.source_key)
                    new_on_page += 1
                products[product.source_key] = product
            if target_key and target_key in products:
                result = [products[target_key]]
                if hydrate_options:
                    await _hydrate_bibibins_options(session, result)
                return result
            if not new_on_page:
                break
            await asyncio.sleep(0.12)
    if target_key:
        raise RuntimeError("해당 비비빈스 상품을 공개 목록에서 찾지 못했습니다.")
    if not products:
        raise RuntimeError("링크에서 비비빈스 상품을 찾지 못했습니다.")
    result = list(products.values())
    if hydrate_options:
        await _hydrate_bibibins_options(session, result)
    return result


async def scrape_bibibins(
    session: aiohttp.ClientSession, hydrate_options: bool = False,
) -> list[ScrapedProduct]:
    products: dict[str, ScrapedProduct] = {}
    categories = (("입호흡 기기", 50), ("폐호흡 기기", 52))
    for category, category_number in categories:
        category_keys: set[str] = set()
        for page in range(1, 21):
            url = f"{BIBIBINS_BASE}/product/list.html?cate_no={category_number}&page={page}"
            soup = await _soup(session, url)
            cards = soup.select(".xans-product-listnormal .prdList > li")
            if not cards:
                break
            new_on_page = 0
            for card in cards:
                product = _bibibins_card(card, category, url)
                if product:
                    if product.source_key not in category_keys:
                        category_keys.add(product.source_key)
                        new_on_page += 1
                    existing = products.get(product.source_key)
                    if existing is None or category == "입호흡 기기":
                        products[product.source_key] = product
            if not new_on_page:
                break
            await asyncio.sleep(0.12)
    if not products:
        raise RuntimeError("비비빈스 공개 상품을 찾지 못했습니다.")
    result = list(products.values())
    if hydrate_options:
        await _hydrate_bibibins_options(session, result)
    return result


DTL_WORDS = (
    "폐호흡", "모드기기", "모드 기기", "박스모드", "박스 모드", "200w", "220w",
    "젠200", "클라우드 플라스크", "드래그 5", "드래그5", "아르거스 mt",
)


def _device_category(name: str) -> str:
    lowered = name.lower()
    return "폐호흡 기기" if any(word in lowered for word in DTL_WORDS) else "입호흡 기기"


def _elec_options(soup: BeautifulSoup) -> list[str]:
    options = []
    for select in soup.select("form.variations_form .variations select"):
        options.extend(option.get_text(" ", strip=True) for option in select.select("option[value]") if option.get("value"))
    if options:
        return _unique(options)
    form = soup.select_one("form.variations_form[data-product_variations]")
    if form:
        try:
            variations = json.loads(form.get("data-product_variations", "[]"))
            return _unique(value for item in variations for value in item.get("attributes", {}).values())
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return ["색상은 구매티켓에서 확인"]


COLOR_WORDS = (
    "블랙", "화이트", "그레이", "실버", "골드", "레드", "오렌지", "옐로우", "그린",
    "블루", "네이비", "퍼플", "핑크", "브라운", "카키", "민트", "투명", "클리어",
)


def _option_label(options: list[str]) -> str:
    if options == ["색상은 구매티켓에서 확인"]:
        return "색상"
    color_count = sum(any(word in option for word in COLOR_WORDS) for option in options)
    return "색상" if color_count * 2 > len(options) else "맛"


def _elec_currency(soup: BeautifulSoup) -> str:
    for script in soup.select('script[type="application/ld+json"]'):
        match = re.search(r'"priceCurrency"\s*:\s*"([A-Z]{3})"', script.get_text(" ", strip=True))
        if match:
            return match.group(1)
    return "USD" if "$" in (soup.select_one(".summary .price") or soup).get_text(" ", strip=True) else "KRW"


async def _usd_krw_rate(session: aiohttp.ClientSession) -> float:
    configured = os.getenv("ELECSHOP_USD_KRW_RATE", "").strip()
    if configured:
        return float(configured)
    try:
        async with session.get("https://open.er-api.com/v6/latest/USD", timeout=aiohttp.ClientTimeout(total=10)) as response:
            response.raise_for_status()
            rate = float((await response.json())["rates"]["KRW"])
            if rate > 0:
                return rate
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError):
        pass
    return 1_400.0


def _elec_product_id(soup: BeautifulSoup, url: str) -> str:
    form = soup.select_one("form.variations_form[data-product_id]")
    if form and form.get("data-product_id"):
        return str(form.get("data-product_id"))
    body = soup.select_one("body")
    for class_name in body.get("class", []) if body else []:
        match = re.fullmatch(r"postid-(\d+)", class_name)
        if match:
            return match.group(1)
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def _parse_elec_detail(
    soup: BeautifulSoup, url: str, product_id: str, usd_krw_rate: float,
    in_stock: bool = True,
) -> ScrapedProduct | None:
    name_node = soup.select_one("h1.product_title")
    price_node = soup.select_one(".summary .price")
    if not name_node or not price_node:
        return None
    price_match = re.search(r"[0-9][0-9,.]*", price_node.get_text(" ", strip=True))
    if not price_match:
        return None
    raw_price = float(price_match.group().replace(",", ""))
    currency = _elec_currency(soup)
    source_price = round(raw_price * usd_krw_rate) if currency == "USD" else round(raw_price)
    description_node = soup.select_one(".woocommerce-product-details__short-description")
    options = _elec_options(soup)
    return ScrapedProduct(
        source_site="elecshop",
        source_key=str(product_id),
        source_url=url,
        category=_device_category(name_node.get_text(" ", strip=True)),
        name=name_node.get_text(" ", strip=True),
        description=description_node.get_text(" ", strip=True) if description_node else "",
        source_price=source_price,
        price=source_price + PRICE_MARKUP,
        image_url=_image_url(soup.select_one(".woocommerce-product-gallery") or soup, url),
        option_label=_option_label(options),
        options=options,
        stock=99 if in_stock else 0,
    )


async def _elec_detail(
    session: aiohttp.ClientSession, card, usd_krw_rate: float,
) -> ScrapedProduct | None:
    link = card.select_one('a[href*="/product/"]')
    product_id = card.get("data-product_id")
    if not link or not product_id:
        return None
    url = urljoin(ELECSHOP_BASE, link.get("href"))
    soup = await _soup(session, url)
    return _parse_elec_detail(
        soup, url, str(product_id), usd_krw_rate,
        "outofstock" not in " ".join(card.get("class", [])).lower(),
    )


async def _scrape_elecshop_url(
    session: aiohttp.ClientSession, url: str,
) -> list[ScrapedProduct]:
    rate = await _usd_krw_rate(session)
    soup = await _soup(session, url)
    if soup.select_one("h1.product_title"):
        product = _parse_elec_detail(soup, url, _elec_product_id(soup, url), rate)
        if not product:
            raise RuntimeError("일렉샵 상품 상세정보를 읽지 못했습니다.")
        return [product]

    cards_by_id = {}
    current_url = url
    visited: set[str] = set()
    for _ in range(20):
        if current_url in visited:
            break
        visited.add(current_url)
        for card in soup.select(".products .product[data-product_id]"):
            cards_by_id[str(card.get("data-product_id"))] = card
        next_link = soup.select_one(".woocommerce-pagination a.next, a.next.page-numbers")
        if not next_link or not next_link.get("href"):
            break
        next_url = urljoin(current_url, next_link.get("href"))
        if _source_site_for_url(next_url) != "elecshop":
            break
        current_url = next_url
        soup = await _soup(session, current_url)
    cards = list(cards_by_id.values())
    if not cards:
        raise RuntimeError("링크에서 일렉샵 상품을 찾지 못했습니다.")
    semaphore = asyncio.Semaphore(4)

    async def load(card):
        async with semaphore:
            return await _elec_detail(session, card, rate)

    results = await asyncio.gather(*(load(card) for card in cards), return_exceptions=True)
    products = [result for result in results if isinstance(result, ScrapedProduct)]
    if not products:
        raise RuntimeError("일렉샵 상품 상세정보를 읽지 못했습니다.")
    return list({product.source_key: product for product in products}.values())


async def scrape_products_from_url(url: str) -> list[ScrapedProduct]:
    """허용된 쇼핑몰의 상품/목록 링크에서 상품을 가져온다."""
    url = (url or "").strip()
    site = _source_site_for_url(url)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"}
    connector = aiohttp.TCPConnector(limit_per_host=4)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        if site == "bibibins":
            logged_in = await _login_bibibins(session)
            products = await _scrape_bibibins_url(session, url, hydrate_options=logged_in)
        else:
            await _login_elecshop(session)
            products = await _scrape_elecshop_url(session, url)
        await _prepare_product_images(session, products)
        return products


async def scrape_elecshop(session: aiohttp.ClientSession) -> list[ScrapedProduct]:
    soup = await _soup(session, ELECSHOP_DEVICES)
    cards = soup.select(".products .product[data-product_id]")
    if not cards:
        raise RuntimeError("일렉샵 공개 상품을 찾지 못했습니다.")
    rate = await _usd_krw_rate(session)
    semaphore = asyncio.Semaphore(4)

    async def load(card):
        async with semaphore:
            return await _elec_detail(session, card, rate)

    results = await asyncio.gather(*(load(card) for card in cards), return_exceptions=True)
    products = [result for result in results if isinstance(result, ScrapedProduct)]
    if not products:
        raise RuntimeError("일렉샵 상품 상세정보를 읽지 못했습니다.")
    return products


async def scrape_device_stores() -> tuple[dict[str, list[ScrapedProduct]], dict[str, str]]:
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5"}
    connector = aiohttp.TCPConnector(limit_per_host=4)
    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        async def load_bibibins():
            logged_in = await _login_bibibins(session)
            products = await scrape_bibibins(session, hydrate_options=logged_in)
            await _prepare_product_images(session, products)
            return products

        async def load_elecshop():
            await _login_elecshop(session)
            products = await scrape_elecshop(session)
            await _prepare_product_images(session, products)
            return products

        results = await asyncio.gather(
            load_bibibins(), load_elecshop(), return_exceptions=True,
        )
    products: dict[str, list[ScrapedProduct]] = {}
    errors: dict[str, str] = {}
    for site, result in zip(("bibibins", "elecshop"), results):
        if isinstance(result, Exception):
            errors[site] = str(result)
        else:
            products[site] = result
    if not products:
        raise RuntimeError("두 쇼핑몰의 상품 정보를 모두 불러오지 못했습니다.")
    return products, errors


async def _store_product_image(conn, guild_id: int, product_id: int, product: ScrapedProduct) -> str:
    if product.source_site != "bibibins":
        await conn.execute(
            "DELETE FROM web_product_images WHERE product_id=$1 AND guild_id=$2",
            product_id, guild_id,
        )
        return ""
    if product.image_data:
        content_hash = hashlib.sha256(product.image_data).hexdigest()
        await conn.execute(
            """
            INSERT INTO web_product_images
                (product_id,guild_id,mime_type,content_hash,image_data,updated_at)
            VALUES ($1,$2,$3,$4,$5,CURRENT_TIMESTAMP)
            ON CONFLICT (product_id) DO UPDATE SET
                guild_id=EXCLUDED.guild_id, mime_type=EXCLUDED.mime_type,
                content_hash=EXCLUDED.content_hash, image_data=EXCLUDED.image_data,
                updated_at=CURRENT_TIMESTAMP
            """,
            product_id, guild_id, product.image_mime or "image/webp",
            content_hash, product.image_data,
        )
    else:
        content_hash = await conn.fetchval(
            "SELECT content_hash FROM web_product_images WHERE product_id=$1 AND guild_id=$2",
            product_id, guild_id,
        )
    if not content_hash:
        return ""
    image_url = f"/api/products/{product_id}/image?v={str(content_hash)[:12]}"
    await conn.execute(
        "UPDATE web_products SET image_url=$3 WHERE id=$1 AND guild_id=$2",
        product_id, guild_id, image_url,
    )
    return image_url


async def import_scraped_products(
    conn, guild_id: int, category_id: int, products: list[ScrapedProduct],
    price_locked: bool = False,
) -> dict:
    """링크에서 가져온 상품을 관리자가 선택한 카테고리에 추가하거나 갱신한다."""
    inserted = updated = 0
    names: list[str] = []
    async with conn.transaction():
        for product in products:
            source = await conn.fetchrow(
                """
                SELECT product_id FROM web_product_sources
                WHERE guild_id=$1 AND source_site=$2 AND source_key=$3
                """,
                guild_id, product.source_site, product.source_key,
            )
            if source:
                product_id = int(source["product_id"])
                await conn.execute(
                    """
                    UPDATE web_products
                    SET category_id=$3,name=$4,description=$5,price=$6,stock=$7,image_url=$8,
                        option_label=$9,options=$10,is_active=TRUE,updated_at=CURRENT_TIMESTAMP
                    WHERE id=$1 AND guild_id=$2
                    """,
                    product_id, guild_id, category_id, product.name, product.description,
                    product.price, product.stock, product.image_url,
                    product.option_label, product.options,
                )
                updated += 1
            else:
                product_id = await conn.fetchval(
                    """
                    INSERT INTO web_products
                        (guild_id,category_id,name,description,price,stock,image_url,option_label,options,is_active)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)
                    RETURNING id
                    """,
                    guild_id, category_id, product.name, product.description, product.price,
                    product.stock, product.image_url, product.option_label, product.options,
                )
                inserted += 1
            await conn.execute(
                """
                INSERT INTO web_product_sources
                    (guild_id,source_site,source_key,product_id,source_url,source_price,price_locked,disabled_by_admin,synced_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,FALSE,CURRENT_TIMESTAMP)
                ON CONFLICT (guild_id,source_site,source_key) DO UPDATE SET
                    product_id=EXCLUDED.product_id, source_url=EXCLUDED.source_url,
                    source_price=EXCLUDED.source_price, price_locked=EXCLUDED.price_locked,
                    disabled_by_admin=FALSE,
                    synced_at=CURRENT_TIMESTAMP
                """,
                guild_id, product.source_site, product.source_key, product_id,
                product.source_url, product.source_price, price_locked,
            )
            await _store_product_image(conn, guild_id, int(product_id), product)
            names.append(product.name)
    return {
        "inserted": inserted,
        "updated": updated,
        "total": len(products),
        "unknown_options": sum(
            product.options == ["색상은 구매티켓에서 확인"] for product in products
        ),
        "names": names[:20],
    }


async def sync_scraped_products(conn, guild_id: int, grouped: dict[str, list[ScrapedProduct]]) -> dict:
    """수집 결과를 상품 DB에 반영하고 출처별로 사라진 상품은 판매 중지한다."""
    inserted = updated = hidden = 0
    async with conn.transaction():
        category_ids = {}
        for sort_order, name in enumerate(("입호흡 기기", "폐호흡 기기"), start=1):
            category = await conn.fetchrow(
                "SELECT id FROM web_categories WHERE guild_id=$1 AND name=$2", guild_id, name,
            )
            if category:
                category_ids[name] = int(category["id"])
            else:
                category_ids[name] = await conn.fetchval(
                    "INSERT INTO web_categories (guild_id,name,sort_order) VALUES ($1,$2,$3) RETURNING id",
                    guild_id, name, sort_order * 10,
                )

        for site, products in grouped.items():
            seen_keys = []
            for product in products:
                seen_keys.append(product.source_key)
                source = await conn.fetchrow(
                    """
                    SELECT product_id, disabled_by_admin, price_locked FROM web_product_sources
                    WHERE guild_id=$1 AND source_site=$2 AND source_key=$3
                    """,
                    guild_id, site, product.source_key,
                )
                category_id = category_ids[product.category]
                if source:
                    product_id = int(source["product_id"])
                    await conn.execute(
                        """
                        UPDATE web_products
                        SET category_id=$3,name=$4,description=$5,
                            price=CASE WHEN $12 THEN price ELSE $6 END,
                            stock=$7,image_url=$8,
                            option_label=$9,options=$10,is_active=$11,updated_at=CURRENT_TIMESTAMP
                        WHERE id=$1 AND guild_id=$2
                        """,
                        product_id, guild_id, category_id, product.name, product.description,
                        product.price, product.stock, product.image_url,
                        product.option_label, product.options, not bool(source["disabled_by_admin"]),
                        bool(source["price_locked"]),
                    )
                    updated += 1
                else:
                    product_id = await conn.fetchval(
                        """
                        INSERT INTO web_products
                            (guild_id,category_id,name,description,price,stock,image_url,option_label,options,is_active)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)
                        RETURNING id
                        """,
                        guild_id, category_id, product.name, product.description,
                        product.price, product.stock, product.image_url,
                        product.option_label, product.options,
                    )
                    inserted += 1
                await conn.execute(
                    """
                    INSERT INTO web_product_sources
                        (guild_id,source_site,source_key,product_id,source_url,source_price,synced_at)
                    VALUES ($1,$2,$3,$4,$5,$6,CURRENT_TIMESTAMP)
                    ON CONFLICT (guild_id,source_site,source_key) DO UPDATE SET
                        product_id=EXCLUDED.product_id, source_url=EXCLUDED.source_url,
                        source_price=EXCLUDED.source_price, synced_at=CURRENT_TIMESTAMP
                    """,
                    guild_id, site, product.source_key, product_id,
                    product.source_url, product.source_price,
                )
                await _store_product_image(conn, guild_id, int(product_id), product)

            hidden_result = await conn.execute(
                """
                UPDATE web_products p SET is_active=FALSE, updated_at=CURRENT_TIMESTAMP
                FROM web_product_sources s
                WHERE s.product_id=p.id AND s.guild_id=$1 AND s.source_site=$2
                  AND NOT (s.source_key = ANY($3::text[])) AND p.is_active=TRUE
                """,
                guild_id, site, seen_keys,
            )
            hidden += int(hidden_result.rsplit(" ", 1)[-1])
    return {"inserted": inserted, "updated": updated, "hidden": hidden}
