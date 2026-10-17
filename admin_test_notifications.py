"""관리자 테스트 센터에서 보내는 비파괴 Discord 알림 payload."""

from __future__ import annotations

import datetime as dt

from order_editing import build_purchase_log_payload


KST = dt.timezone(dt.timedelta(hours=9))

TEST_NOTIFICATION_TYPES = {
    "purchase": {"label": "구매 승인 요청", "setting": "approval_channel_id"},
    "purchase_log": {"label": "구매로그", "setting": "log_channel_id"},
    "buyer_info": {"label": "구매자정보", "setting": "buyer_info_channel_id"},
    "review": {"label": "구매후기", "setting": "review_channel_id"},
    "inquiry": {"label": "문의알림", "setting": "inquiry_alert_channel_id"},
    "live_chat": {"label": "실시간 채팅상담", "setting": "live_chat_channel_id"},
    "receipt": {"label": "구매 영수증 DM", "setting": None},
    "shipping": {"label": "배송 알림 DM", "setting": None},
}

TEST_OUTCOMES = {
    "APPROVED": ("승인", 0x2ECC71, "✅"),
    "REJECTED": ("거절", 0xE74C3C, "❌"),
    "EXPIRED": ("시간초과", 0xF1C40F, "⏱️"),
}


def _v2(content: str, color: int = 0x5865F2) -> dict:
    return {
        "flags": 1 << 15,
        "allowed_mentions": {"parse": []},
        "components": [{
            "type": 17,
            "accent_color": color,
            "components": [{"type": 10, "content": content}],
        }],
    }


def _test_header(label: str) -> str:
    return f"-# 🧪 관리자 기능 테스트 · 실제 주문이 아닙니다.\n\n## {label}"


def build_test_notification(kind: str, data: dict, target_user_id: int) -> dict:
    """테스트 종류에 맞는 Discord payload와 웹 미리보기 문구를 만든다."""
    if kind not in TEST_NOTIFICATION_TYPES:
        raise ValueError("지원하지 않는 테스트 종류입니다.")

    outcome = str(data.get("outcome") or "APPROVED").upper()
    if outcome not in TEST_OUTCOMES:
        raise ValueError("지원하지 않는 구매 처리 결과입니다.")
    outcome_label, outcome_color, outcome_icon = TEST_OUTCOMES[outcome]
    product = str(data.get("product") or "테스트 상품").strip()
    options = str(data.get("options") or "색상: 테스트 컬러").strip()
    message = str(data.get("message") or "기능 테스트용 메시지입니다.").strip()
    quantity = int(data.get("quantity") or 1)
    amount = int(data.get("amount") or 35_000)
    order_id = str(data.get("order_id") or "TEST-WEB-000001").strip()
    now = dt.datetime.now(KST)
    product_lines = f"`{product}`\n> {options}" if options else f"`{product}`"

    if kind == "purchase_log":
        order = {
            "order_id": order_id,
            "product": product,
            "quantity": quantity,
            "amount": f"{amount:,}원",
            "points_used": 0,
            "cash_amount": amount,
            "processed_at": now,
            "_formatted_items": f"• **{product}** × {quantity}" + (f"\n  └ {options}" if options else ""),
        }
        payload = build_purchase_log_payload(order, None)
        original = payload["components"][0]["components"][0]["content"]
        content = f"-# 🧪 관리자 기능 테스트 · 실제 구매로그가 아닙니다.\n\n{original}"
        payload["components"][0]["components"][0]["content"] = content
        return {"payload": payload, "preview": content, "label": "구매로그", "dm": False}

    if kind == "purchase":
        detail = {
            "APPROVED": "테스트 결제가 승인된 상태입니다.",
            "REJECTED": "테스트 결제가 거절된 상태입니다.",
            "EXPIRED": "입금 확인 시간이 지나 테스트 주문이 자동 취소된 상태입니다.",
        }[outcome]
        content = (
            f"{_test_header(f'{outcome_icon} 구매 처리 · {outcome_label}')}\n\n"
            f"`👤` **구매자:** <@{target_user_id}>\n"
            f"`🧾` **주문번호:** `{order_id}`\n"
            f"`📦` **상품:**\n{product_lines}\n"
            f"`🔢` **수량:** `{quantity}`\n"
            f"`💰` **금액:** `{amount:,}원`\n"
            f"`⚡` **처리결과:** **{outcome_label}**\n\n{detail}"
        )
        return {"payload": _v2(content, outcome_color), "preview": content, "label": f"구매 {outcome_label}", "dm": False}

    if kind == "buyer_info":
        content = (
            f"{_test_header('📋 구매자 상세 배송 정보')}\n\n"
            f"`👤` **구매자:** <@{target_user_id}>\n"
            f"`🧾` **주문번호:** `{order_id}`\n"
            "`👤` **성함:** `테스트 구매자`\n"
            "`📞` **연락처:** `010-0000-0000`\n"
            "`🚚` **배송방식:** `일반택배`\n"
            "`🏠` **주소:** `테스트 주소`\n"
            "`📝` **배송메모:** `테스트 메시지`\n"
            f"`📦` **상품:**\n{product_lines}\n"
            f"`💰` **금액:** `{amount:,}원`"
        )
        return {"payload": _v2(content), "preview": content, "label": "구매자정보", "dm": False}

    if kind == "review":
        content = (
            f"{_test_header('⭐ 구매후기')}\n\n"
            f"**작성자:** <@{target_user_id}>\n"
            f"**상품:** `{product}`\n"
            "**별점:** `★★★★★`\n\n"
            f"{message}"
        )
        return {"payload": _v2(content), "preview": content, "label": "구매후기", "dm": False}

    if kind == "inquiry":
        content = (
            f"{_test_header('📨 새로운 문의')}\n\n"
            f"**작성자:** <@{target_user_id}> (`{target_user_id}`)\n"
            "**문의 유형:** `일반문의`\n"
            f"**문의 내용:**\n{message}\n\n"
            "-# 실제 문의관리 목록에는 등록되지 않습니다."
        )
        return {"payload": _v2(content), "preview": content, "label": "문의알림", "dm": False}

    if kind == "live_chat":
        content = (
            f"{_test_header('💬 실시간 채팅상담 연결')}\n\n"
            f"**상담 유저:** <@{target_user_id}> (`{target_user_id}`)\n"
            f"**문의 내용:**\n{message}\n\n"
            "-# 테스트이므로 상담 스레드는 생성되지 않습니다."
        )
        return {"payload": _v2(content), "preview": content, "label": "채팅상담", "dm": False}

    if kind == "receipt":
        content = (
            f"{_test_header('🧾 V4P3 SP0T 구매 영수증')}\n\n"
            f"**주문번호:** `{order_id}`\n"
            f"**구매 상품:**\n{product_lines}\n"
            f"**수량:** `{quantity}`\n"
            f"**실제 입금액:** `{amount:,}원`\n\n"
            "테스트 영수증입니다. 실제 구매내역에는 반영되지 않습니다."
        )
        return {"payload": _v2(content), "preview": content, "label": "영수증 DM", "dm": True}

    content = (
        f"{_test_header('🚚 운송장이 발급되었습니다')}\n\n"
        f"`주문번호` `{order_id}`\n"
        "`운송장` `TEST-1234-5678`\n"
        f"`상품` `{product}`\n"
        "`진행상황` **배송준비중 (60%)**\n\n"
        "테스트 배송 알림입니다. 실제 배송정보는 변경되지 않습니다."
    )
    return {"payload": _v2(content), "preview": content, "label": "배송 알림 DM", "dm": True}

