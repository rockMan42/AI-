import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config.settings import get_settings

RULE_TYPES = ("approval", "attendance", "reimbursement")


class RuleError(ValueError):
    def __init__(self, message, status=422, code="RULE_INVALID", fields=None):
        super().__init__(message)
        self.status_code = status
        self.code = code
        self.fields = fields or []


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def pack(value, rule_type, *, sensitive=False):
    settings = get_settings()
    if not sensitive and rule_type not in settings.rules_sensitive_types:
        return {"plain": value}
    key_id = settings.rules_encryption_key_id
    try:
        key = base64.b64decode(settings.rules_encryption_keys[key_id], validate=True)
        if len(key) != 32:
            raise ValueError()
        nonce = os.urandom(12)
        ciphertext = AESGCM(key).encrypt(
            nonce, canonical(value).encode(), rule_type.encode()
        )
    except (ValueError, KeyError):
        raise RuleError("规则加密密钥未正确配置", 503, "RULE_KEY_UNAVAILABLE") from None
    return {
        "key_id": key_id,
        "ciphertext": base64.b64encode(nonce + ciphertext).decode(),
    }


def unpack(envelope, rule_type):
    if "plain" in envelope:
        # 深复制，调用者无法修改进程共享快照。
        return json.loads(canonical(envelope["plain"]))
    try:
        key = base64.b64decode(
            get_settings().rules_encryption_keys[envelope["key_id"]], validate=True
        )
        raw = base64.b64decode(envelope["ciphertext"], validate=True)
        return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], rule_type.encode()))
    except Exception:
        raise RuleError("规则快照解密失败", 503, "RULE_KEY_UNAVAILABLE") from None


def diff(before, after, path=""):
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in sorted(before.keys() | after.keys()):
            pointer = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if key not in before or key not in after:
                changes.append(
                    {
                        "path": pointer,
                        "before": before.get(key),
                        "after": after.get(key),
                    }
                )
            else:
                changes.extend(diff(before[key], after[key], pointer))
        return changes
    return (
        []
        if before == after
        else [{"path": path or "/", "before": before, "after": after}]
    )
