import re


_OPEN_ID_PATTERN = re.compile(r"ou_[0-9a-fA-F]{32}\Z")


def valid_feishu_open_id(value: str | None) -> bool:
    return isinstance(value, str) and _OPEN_ID_PATTERN.fullmatch(value) is not None
