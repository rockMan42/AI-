import asyncio
import logging

from fastapi import HTTPException

from app.services import holiday
from app.services.holiday_cards import notice_stats_card
from app.services.holiday_cron import HolidayError

log = logging.getLogger(__name__)

async def handle_holiday_card_data(data: dict) -> dict:
    event = data.get("event") or {}
    value = (event.get("action") or {}).get("value") or {}
    open_id = (event.get("operator") or {}).get("open_id", "")
    if value.get("operation") == "list":
        offset = value.get("offset", 0)
        keyword = value.get("keyword")
        if (type(offset) is not int or not 0 <= offset <= 100000
                or keyword is not None and (not isinstance(keyword, str) or len(keyword) > 200)):
            return {"toast": {"type": "error", "content":
                    "通知列表卡片已失效，请重新查询"}}
        try:
            async with asyncio.timeout(5):
                result = await holiday.list_notices(
                    open_id, keyword=keyword, offset=offset, limit=5,
                )
            from app.services.holiday_cards import notice_list_card
            return {"toast": {"type": "success", "content": "通知列表已更新"},
                    "card": {"type": "raw", "data": notice_list_card(
                        result["items"], result["has_more"], offset=offset,
                        keyword=keyword,
                    )}}
        except HolidayError as exc:
            if exc.status_code == 403:
                raise HTTPException(403, str(exc)) from None
            return {"toast": {"type": "error", "content": str(exc)}}
        except TimeoutError:
            return {"toast": {"type": "warning", "content":
                    "列表查询超时，请稍后重试"}}
        except Exception:
            log.exception("holiday_list_card_failed")
            return {"toast": {"type": "error", "content":
                    "通知列表暂时无法查看，请稍后重试"}}
    notice_id = value.get("notice_id")
    if type(notice_id) is not int or notice_id <= 0:
        return {"toast": {"type": "error", "content": "通知卡片已失效，请重新查询"}}
    try:
        async with asyncio.timeout(5):
            if value.get("operation") == "remind":
                await holiday.request_reminder(open_id, notice_id)
                message = "催办已安排，仅提醒尚未确认的同事"
            elif value.get("operation") == "report":
                await holiday.request_report(open_id, notice_id)
                message = "统计报告已安排发送"
            elif value.get("operation") == "stats":
                message = "统计已更新"
            else:
                raise HolidayError("通知卡片已失效，请重新查询", 409)
            notice = await holiday.detail(open_id, notice_id)
            stats = await holiday.receipt_stats(open_id, notice_id)
        return {"toast": {"type": "success", "content": message},
                "card": {"type": "raw", "data": notice_stats_card(notice, stats)}}
    except HolidayError as exc:
        if exc.status_code == 403:
            raise HTTPException(403, str(exc)) from None
        return {"toast": {"type": "error", "content": str(exc)}}
    except TimeoutError:
        return {"toast": {"type": "warning", "content":
                "处理结果暂未确认，请稍后刷新统计"}}
    except Exception:
        log.exception("holiday_stats_card_failed")
        return {"toast": {"type": "error", "content":
                "通知暂时无法处理，请稍后重试"}}
