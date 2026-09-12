"""只接受完整、无歧义的本人查询；不做子串关键词路由。"""
from datetime import date, timedelta
import re


def parse_attendance_query(text: str, today: date) -> dict | None:
    text = text.strip().strip("。！？!? ")
    text = re.sub(r"[，,]\s*谢谢$", "谢谢", text)
    match = re.fullmatch(
        r"(?:请|麻烦)?(?:帮我)?(?:查询|查一下|查)(?:一下)?(?:我的|我)?"
        r"(?P<body>.+?)(?:谢谢)?", text,
    )
    if match is None:
        return None
    body = match["body"]
    slots = {"query_target": "self"}
    month = re.fullmatch(r"(本月|上月)(考勤|迟到次数|早退次数)", body)
    day = re.fullmatch(r"(今天|昨天)(?:的)?打卡(?:记录)?", body)
    year = re.fullmatch(r"(今年|去年|[0-9]{4}年|[0-9]{2}年)?(?:的)?假期余额", body)
    if month:
        target = today if month[1] == "本月" else today.replace(day=1) - timedelta(days=1)
        slots.update(query_month=target.strftime("%Y-%m"),
                     query_type="all" if month[2] == "考勤" else "late_count")
    elif day:
        target = today - timedelta(days=int(day[1] == "昨天"))
        slots.update(query_date=target.isoformat(), query_type="punch_record")
    elif year:
        value = year[1]
        number = today.year
        if value == "去年":
            number -= 1
        elif value and value != "今年":
            digits = value[:-1]
            number = int(digits) + (2000 if len(digits) == 2 else 0)
        if not 1900 <= number <= 9999:
            return None
        slots.update(query_year=number, query_type="leave_balance")
    else:
        return None
    return {"intent": "attendance_query", "confidence": 1.0, "extracted_slots": slots}


def can_use_fast_path(session) -> bool:
    if session.pending_slot or session.pending_intent or session.suspended_contexts:
        return False
    return not session.intent_code or session.workflow_state == "completed"
