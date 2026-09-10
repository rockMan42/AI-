from datetime import UTC, datetime
from zoneinfo import ZoneInfo


SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    """
    返回不带 tzinfo 的 UTC 时间。

    MySQL DATETIME 不保存时区信息，因此数据库层统一约定：
    所有无时区 datetime 都表示 UTC。
    """
    return datetime.now(UTC).replace(tzinfo=None)


def as_utc(value: datetime) -> datetime:
    """将数据库中的 UTC 时间补充为带时区的 datetime。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_shanghai(value: datetime) -> datetime:
    """将 UTC 时间转换为北京时间。"""
    return as_utc(value).astimezone(SHANGHAI_TIMEZONE)


def format_shanghai(
    value: datetime | None,
    format_string: str = "%Y-%m-%d %H:%M:%S",
) -> str:
    if value is None:
        return ""
    return as_shanghai(value).strftime(format_string)