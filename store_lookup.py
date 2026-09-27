"""GS25·CU 공식 매장찾기와 디스코드 주소 조회 패널."""

import asyncio
import re

import aiohttp
import discord
from bs4 import BeautifulSoup


BRANDS = ("GS25", "CU")
SELECT_ID = "store_address_brand"
GS_URL = "https://www.gsretail.com/api/homepage/brand/storeSearch/selectGs25Stores"
CU_URL = "https://cu.bgfretail.com/store/list_Ajax.do"
CU_PAGE = "https://cu.bgfretail.com/store/list.do?category=store"
_SEARCH_SLOTS = asyncio.Semaphore(4)


class StoreLookupError(Exception):
    """사용자에게 표시할 검색 오류."""


def normalize_query(brand, value):
    if brand not in BRANDS:
        raise StoreLookupError("GS25 또는 CU만 조회할 수 있습니다.")
    value = re.sub(r"\s+", " ", str(value)).strip()
    value = re.sub(r"^" + brand + r"\s*", "", value, flags=re.I).strip()
    if not 2 <= len(value) <= 60:
        raise StoreLookupError("브랜드명을 제외한 지점명을 2~60자로 입력해 주세요.")
    return value


def _key(value):
    return re.sub(r"\s+", "", value).casefold()


def parse_gs(data):
    if not isinstance(data, list):
        raise StoreLookupError("GS25 매장검색 응답 형식이 변경되었습니다. 잠시 후 다시 시도해 주세요.")
    stores = []
    for row in data:
        if not isinstance(row, dict) or row.get("storeType", "GS25") != "GS25":
            continue
        name, address = row.get("shopName"), row.get("rnAddress") or row.get("address")
        if isinstance(name, str) and isinstance(address, str) and name.strip() and address.strip():
            stores.append({"name": name.strip(), "address": address.strip()})
    return stores


def parse_cu(html, page):
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one(".detail_store table")
    if table is None:
        raise StoreLookupError("CU 매장검색 응답 형식이 변경되었습니다. 잠시 후 다시 시도해 주세요.")
    stores = []
    for row in table.select("tbody tr"):
        name, address = row.select_one(".name"), row.select_one("address")
        if name and address:
            name, address = name.get_text(" ", strip=True), address.get_text(" ", strip=True)
            if name and address:
                stores.append({"name": name if name.upper().startswith("CU") else "CU" + name,
                               "address": address})
    if not stores and "등록된 게시물이 없습니다" not in table.get_text():
        raise StoreLookupError("CU 매장검색 결과를 읽을 수 없습니다. 잠시 후 다시 시도해 주세요.")
    next_page = False
    for link in soup.select("#paging [onclick]"):
        match = re.search(r"newsPage\(['\"]?(\d+)", link.get("onclick", ""))
        if match and int(match.group(1)) > page:
            next_page = True
    return stores, next_page


async def _response(session, method, url, *, json=False, **kwargs):
    async with session.request(method, url, **kwargs) as response:
        if response.status != 200:
            raise StoreLookupError("공식 매장검색에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.")
        return await response.json(content_type=None) if json else await response.text()


async def _search(brand, query):
    async with aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=15)
    ) as session:
        if brand == "GS25":
            data = await _response(session, "GET", GS_URL, json=True, params={"shopName": query},
                                   headers={"Accept": "application/json", "Referer": "https://www.gsretail.com/brand/gs25"})
            return parse_gs(data)
        # CU는 빈 필터도 함께 보내야 매장명 검색이 정상 동작합니다.
        await _response(session, "GET", CU_PAGE)
        params = dict.fromkeys(("listType", "jumpoCode", "jumpoLotto", "jumpoToto", "jumpoCash",
                                "jumpoHour", "jumpoCafe", "jumpoDelivery", "jumpoBakery", "jumpoFry",
                                "jumpoMultiDevice", "jumpoPosCash", "jumpoBattery", "jumpoAdderss",
                                "jumpoSido", "jumpoGugun", "jumpodong", "searchWord"), "")
        params["jumpoName"] = query
        results = []
        for page in range(1, 7):
            params["pageIndex"] = str(page)
            html = await _response(session, "POST", CU_URL, data=params,
                                   headers={"Referer": CU_PAGE, "X-Requested-With": "XMLHttpRequest"})
            stores, more = parse_cu(html, page)
            results.extend(stores)
            if not more:
                return results
            if len(results) > 25:
                break
        raise StoreLookupError("검색 결과가 많습니다. 지점명을 더 자세히 입력해 주세요.")


async def search_stores(brand, value):
    query = normalize_query(brand, value)
    try:
        async def run():
            async with _SEARCH_SLOTS:
                return await _search(brand, query)
        results = await asyncio.wait_for(run(), timeout=35)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise StoreLookupError("매장검색 연결이 지연되거나 실패했습니다. 잠시 후 다시 시도해 주세요.") from exc
    except (ValueError, UnicodeError) as exc:
        raise StoreLookupError("매장검색 응답을 읽을 수 없습니다. 잠시 후 다시 시도해 주세요.") from exc
    filtered, seen = [], set()
    for store in results:
        name = re.sub(r"^" + brand, "", store["name"], flags=re.I)
        identity = (store["name"], store["address"])
        if _key(query) in _key(name) and identity not in seen:
            seen.add(identity)
            filtered.append(store)
    if not filtered:
        raise StoreLookupError("해당 지점을 찾지 못했습니다. 브랜드와 지점명을 확인해 주세요.")
    if len(filtered) > 25:
        raise StoreLookupError("검색 결과가 많습니다. 지점명을 더 자세히 입력해 주세요.")
    return filtered


def brand_selector():
    return {"type": 1, "components": [{"type": 3, "custom_id": SELECT_ID,
            "placeholder": "편의점 브랜드를 선택하세요", "min_values": 1, "max_values": 1,
            "options": [{"label": brand, "value": brand, "emoji": {"name": "🏪"}} for brand in BRANDS]}]}


def lookup_panel():
    return {"flags": 1 << 15, "allowed_mentions": {"parse": []}, "components": [{
        "type": 17, "accent_color": 0xFFFFFF, "components": [
            {"type": 10, "content": "**🏪 편의점 주소 조회**"},
            {"type": 14, "divider": True, "spacing": 1},
            {"type": 10, "content": "아래에서 **GS25 또는 CU**를 선택한 뒤\n편의점명을 입력하면 주소를 조회할 수 있습니다."},
            {"type": 14, "divider": True, "spacing": 1}, brand_selector()]}]}


def result_embed(brand, store):
    clean = lambda text: discord.utils.escape_markdown(discord.utils.escape_mentions(text))
    return discord.Embed(title=f"🏪 {brand} 편의점 조회", color=0xFFFFFF,
                         description=f"편의점명 : {clean(store['name'])}\n편의점주소 : {clean(store['address'])}")


class StoreResultsView(discord.ui.View):
    def __init__(self, user_id, brand, stores):
        super().__init__(timeout=300)
        self.user_id, self.brand, self.stores = user_id, brand, stores
        select = discord.ui.Select(placeholder="주소를 확인할 지점을 선택하세요", options=[
            discord.SelectOption(label=store["name"][:100], description=store["address"][:100], value=str(index))
            for index, store in enumerate(stores)])
        select.callback = self.choose
        self.add_item(select)

    async def interaction_check(self, interaction):
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("본인의 조회 결과만 선택할 수 있습니다.", ephemeral=True)
        return False

    async def choose(self, interaction):
        try:
            index = int(interaction.data["values"][0])
            if not 0 <= index < len(self.stores):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            await interaction.response.send_message("지점을 다시 선택해 주세요.", ephemeral=True)
            return
        await interaction.response.edit_message(content=None, embed=result_embed(self.brand, self.stores[index]), view=self)


class StoreNameModal(discord.ui.Modal):
    def __init__(self, brand):
        super().__init__(title=f"{brand} 편의점 주소 조회", timeout=300)
        self.brand = brand
        self.store_name = discord.ui.TextInput(label="편의점 지점명", placeholder="편의점 지점명을 입력해 주세요. (브랜드명은 생략 가능)",
                                             min_length=2, max_length=60)
        self.add_item(self.store_name)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            stores = await search_stores(self.brand, self.store_name.value)
        except StoreLookupError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if len(stores) == 1:
            await interaction.followup.send(embed=result_embed(self.brand, stores[0]), ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.followup.send("검색된 지점 중 주소를 확인할 곳을 선택해 주세요.", ephemeral=True,
                                            view=StoreResultsView(interaction.user.id, self.brand, stores),
                                            allowed_mentions=discord.AllowedMentions.none())


async def handle_brand_selection(interaction):
    values = (interaction.data or {}).get("values", [])
    if len(values) != 1 or values[0] not in BRANDS:
        await interaction.response.send_message("GS25 또는 CU를 선택해 주세요.", ephemeral=True)
        return
    await interaction.response.send_modal(StoreNameModal(values[0]))
