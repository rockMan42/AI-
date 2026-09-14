"""
飞书回调鉴权
"""
import hashlib
import hmac
import json
import time
import re
from datetime import datetime
from fastapi import HTTPException, Request

from app.config.settings import get_settings
from app.utils.aes_cipher import AESCipher


async def decode_feishu_event(request: Request) -> dict:
    settings = get_settings()
    raw = await request.body()
    if len(raw) > 256 * 1024:
        raise HTTPException(413, "请求过大")

    try:
        data = json.loads(raw)
        if data.get("encrypt"):
            data = json.loads(
                AESCipher(settings.encrypt_key).decrypt_string(data["encrypt"])
            )
        if not isinstance(data, dict):
            raise ValueError()
    except Exception:
        raise HTTPException(400, "事件格式无效") from None

    token = (
        data.get("token")
        if data.get("type") == "url_verification"
        else (data.get("header") or {}).get("token")
    )
    expected_token = settings.feishu_verification_token
    if (
        not expected_token
        or not isinstance(token, str)
        or not hmac.compare_digest(token, expected_token)
    ):
        raise HTTPException(403, "事件身份验证失败")

    # 地址校验请求可能不带签名，已校验其Verification Token。
    if data.get("type") == "url_verification":
        return data

    if not settings.encrypt_key:
        raise HTTPException(503, "请配置飞书事件Encrypt Key")

    timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
    nonce = request.headers.get("X-Lark-Request-Nonce", "")
    signature = request.headers.get("X-Lark-Signature", "")


    # 签名必须使用原始请求头，不能使用解析后的时间。
    expected = hashlib.sha256(
        (timestamp + nonce + settings.encrypt_key).encode() + raw
    ).hexdigest()

    if not nonce or not hmac.compare_digest(expected, signature):
        raise HTTPException(403, "事件签名无效")

    try:
        request_time = parse_feishu_timestamp(timestamp)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(403, "事件时间戳格式无效") from None

    if abs(time.time() - request_time) > 300:
        raise HTTPException(403, "事件时间戳已过期或服务器时间异常")

    header = data.get("header") or {}
    if header.get("app_id") != settings.feishu_app_id:
        raise HTTPException(403, "事件应用不匹配")

    return data


def parse_feishu_timestamp(value: str) -> float:
    # 原有格式：Unix 秒级时间戳
    if re.fullmatch(r"[0-9]{1,12}", value):
        return float(int(value))

    # 兼容此次实际收到的日期字符串：
    # 2026-09-14 16:59:21.28647025 +0800 CST m=+350231.933491422
    match = re.fullmatch(
        r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2} "
        r"[0-9]{2}:[0-9]{2}:[0-9]{2})"
        r"(?:\.(?P<fraction>[0-9]{1,9}))?"
        r" (?P<offset>[+-][0-9]{4})"
        r"(?: [A-Za-z][A-Za-z0-9+-]*)?"
        r"(?: m=[+-][0-9]+(?:\.[0-9]+)?)?",
        value,
    )
    if match is None:
        raise ValueError("不支持的请求时间戳格式")

    # datetime 支持微秒精度：不足补零，超出截断。
    fraction = (match["fraction"] or "").ljust(6, "0")[:6]
    normalized = f"{match['date']}.{fraction} {match['offset']}"

    # 使用明确的数字时区 +0800，忽略时区简称及单调时钟后缀。
    parsed = datetime.strptime(
        normalized,
        "%Y-%m-%d %H:%M:%S.%f %z",
    )
    return parsed.timestamp()