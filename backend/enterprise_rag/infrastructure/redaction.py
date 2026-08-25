"""脱敏工具：递归隐藏日志、审计和遥测中的凭据及私密内容。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

_KEY_PATTERN = re.compile(r"(secret|token|password|authorization|api[_-]?key)", re.IGNORECASE)
_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[a-z0-9._~+/-]+=*")
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.DOTALL,
)
_RESTRICTED_ID_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def redact_text(value: str) -> str:
    """从文本中替换 Bearer 凭据、私钥和受限身份号码。"""
    value = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    value = _PRIVATE_KEY_PATTERN.sub("[REDACTED PRIVATE KEY]", value)
    return _RESTRICTED_ID_PATTERN.sub("[REDACTED ID]", value)


def redact(value: Any) -> Any:
    """递归脱敏字符串、映射和序列，保留其他值的原类型。"""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _KEY_PATTERN.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact(item) for item in value]
    return value
