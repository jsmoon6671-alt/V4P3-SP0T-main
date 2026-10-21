"""URL.KR 배송조회 페이지가 사용하는 AJAX 요청으로 배송 정보를 조회합니다."""

import asyncio
import datetime
import re

import aiohttp


BASE_URL = "https://url.kr/p/delivery/"
KST = datetime.timezone(datetime.timedelta(hours=9))
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": BASE_URL,
    "Origin": "https://url.kr",
    "Accept": "application/json",
}

# 기존 패널의 코드도 새 패널과 동일한 사이트 조회 항목으로 연결합니다.
LEGACY_CARRIERS = {
    "24": "kr.cvsnet", "46": "kr.cupost",
    "04": "kr.cjlogistics", "01": "kr.epost",
}


class DeliveryTrackingError(Exception):
    """사용자에게 안내할 수 있는 배송조회 오류."""


def normalize_waybill(value):
    number = re.sub(r"[\s-]", "", value)
    if not re.fullmatch(r"[0-9]{1,50}", number):
        raise DeliveryTrackingError("운송장 번호는 숫자로 입력해 주세요. 공백과 하이픈은 생략할 수 있습니다.")
    return number


async def _request_json(session, method, endpoint, **kwargs):
    try:
        async with session.request(method, BASE_URL + endpoint, **kwargs) as response:
            if response.status != 200:
                raise DeliveryTrackingError(
                    f"배송조회 사이트에 연결할 수 없습니다 (HTTP {response.status}). 잠시 후 다시 시도해 주세요."
                )
            return await response.json(content_type=None)
    except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
        raise DeliveryTrackingError("배송조회 사이트의 응답이 지연되거나 연결에 실패했습니다. 잠시 후 다시 시도해 주세요.") from exc
    except (ValueError, UnicodeError) as exc:
        raise DeliveryTrackingError("배송조회 사이트의 응답 형식이 변경되었거나 일시적으로 조회할 수 없습니다.") from exc


async def fetch_carriers():
    async with aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as session:
        data = await _request_json(session, "GET", "get_carriers.php")
    if not isinstance(data, list):
        raise DeliveryTrackingError("택배사 목록을 불러올 수 없습니다. 잠시 후 다시 시도해 주세요.")
    carriers = []
    seen = set()
    for carrier in data:
        if not isinstance(carrier, dict):
            continue
        carrier_id, name = carrier.get("id"), carrier.get("name")
        if (isinstance(carrier_id, str) and re.fullmatch(r"kr\.[a-z0-9_]+", carrier_id)
                and isinstance(name, str) and name.strip() and carrier_id not in seen
                and len(name) <= 70 and len(name + "|" + carrier_id) <= 100):
            carriers.append({"id": carrier_id, "name": name.strip()})
            seen.add(carrier_id)
    if not carriers:
        raise DeliveryTrackingError("현재 조회 가능한 택배사가 없습니다. 잠시 후 다시 시도해 주세요.")
    if len(carriers) > 25:
        raise DeliveryTrackingError("택배사 목록이 변경되었습니다. 배송조회 패널 업데이트가 필요합니다.")
    return carriers


async def track_shipment(carrier_id, waybill):
    number = normalize_waybill(waybill)
    carrier_id = LEGACY_CARRIERS.get(carrier_id, carrier_id)
    if not isinstance(carrier_id, str) or not re.fullmatch(r"kr\.[a-z0-9_]+", carrier_id):
        raise DeliveryTrackingError("택배사 정보가 올바르지 않습니다. 새 /배송조회 패널을 이용해 주세요.")
    async with aiohttp.ClientSession(headers=HEADERS, timeout=aiohttp.ClientTimeout(total=20)) as session:
        data = await _request_json(
            session, "POST", "tracking.php",
            data={"carrier": carrier_id, "trackingNumber": number},
        )
    if not isinstance(data, dict):
        raise DeliveryTrackingError("배송조회 사이트에서 올바른 조회 결과를 받지 못했습니다.")
    if data.get("error"):
        error = data["error"]
        if not isinstance(error, str):
            error = "배송 정보를 찾을 수 없습니다."
        raise DeliveryTrackingError(error[:500])
    if not isinstance(data.get("status"), str) or not data["status"].strip():
        raise DeliveryTrackingError("배송 정보를 찾을 수 없습니다. 택배사와 운송장 번호를 확인해 주세요.")
    history = data.get("allProgress")
    if history is not None and (not isinstance(history, list) or any(not isinstance(item, dict) for item in history)):
        raise DeliveryTrackingError("배송 이력의 응답 형식이 변경되었습니다. 잠시 후 다시 시도해 주세요.")
    return data


def safe_text(value, fallback="정보 미제공", limit=180):
    if value is None or not str(value).strip():
        return fallback
    text = re.sub(r"\s+", " ", str(value)).strip()[:limit]
    text = text.replace("@", "@\u200b")
    return re.sub(r"([\\`*_~|<>\[\]])", r"\\\1", text)


def _format_time(value):
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=KST)
        return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OverflowError):
        return safe_text(value, "시간 미제공", 60)


def _format_status(value, limit=180):
    if isinstance(value, str):
        # 사이트가 완성된 문장 뒤에 덧붙인 중복 어미만 제거합니다.
        value = re.sub(r"(다[.!?。])\s*하였습니다[.!?。]?\s*$", r"\1", value)
    return safe_text(value, limit=limit)


def format_tracking_result(courier_name, number, data):
    content = (
        f"## 📦 {safe_text(courier_name)}\n\n"
        f"운송장 번호 : `{normalize_waybill(number)}`\n"
        f"현재 상태 : {_format_status(data.get('status'))}\n"
        f"현재 위치 : {safe_text(data.get('location'))}\n\n"
        f"= 받는분 정보 =\n"
        f"받는분 : {safe_text(data.get('receiver'))}\n\n"
        f"보내는분 : {safe_text(data.get('sender'))}\n\n"
        "= 배송 이력 (최근순 · 한국 시간) =\n"
    )
    history = list(reversed(data.get("allProgress") or []))
    if not history:
        content += "아직 배송 이력이 등록되지 않았습니다."
    for index, item in enumerate(history):
        location = item.get("location")
        if isinstance(location, dict):
            location = location.get("name")
        status = item.get("status")
        if isinstance(status, dict):
            status = status.get("text")
        entry = (
            f"[ {_format_time(item.get('time'))} ]\n"
            f"+ 위치: {safe_text(location)}\n"
            f"+ 상태: {_format_status(item.get('description') or status, limit=300)}\n\n"
        )
        if len(content) + len(entry) > 3700:
            content += f"\n이전 이력 {len(history) - index}건은 메시지 길이 제한으로 생략했습니다."
            break
        content += entry
    return content.rstrip()
