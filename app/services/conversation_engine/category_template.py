import json
import logging

from app.core.redis_client import get_cache, set_cache

logger = logging.getLogger(__name__)
CATEGORY_TEMPLATE_TTL = 60 * 60


async def load_category_template(category: str) -> list[dict]:
    """根据物资种类加载种类模版"""

    key = f"dep:tmp:category_template:{category}"
    data = await get_cache(key)
    if data:
        return json.loads(data)

    templates = {
        "IT设备": [
            {
                "name": "device_model",
                "type": "string",
                "required": True,
                "default": None,
                "follow_up_prompt": "请提供设备型号（如 MacBook Pro 14寸）",
                "description": "设备型号（如 MacBook Pro 14寸）",
            },
            {
                "name": "asset_tag",
                "type": "string",
                "required": False,
                "default": None,
                "follow_up_prompt": "如属于旧设备更换，请提供原资产编号",
                "description": "资产编号（如已有旧设备更换请填写）",
            },
        ],
        "办公用品": [],
        "劳保用品": [
            {
                "name": "size",
                "type": "string",
                "required": True,
                "default": None,
                "enum": ["S", "M", "L", "XL"],
                "follow_up_prompt": "请选择尺寸规格（S/M/L/XL）",
                "description": "尺寸规格（如 S/M/L/XL）",
            },
        ],
    }

    extra_slots = templates.get(category, [])

    await set_cache(key, json.dumps(extra_slots,ensure_ascii=False), expire=CATEGORY_TEMPLATE_TTL)

    logger.info(
        "已加载物资品类动态模板 category=%s slots=%s",
        category,
        [slot["name"] for slot in extra_slots],
    )
    return extra_slots
