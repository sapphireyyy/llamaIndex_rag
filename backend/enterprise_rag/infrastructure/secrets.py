"""密钥引用基础设施：从进程环境或本地 dotenv 文件解析开发密钥。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values


class EnvironmentSecretStore:
    """开发环境密钥绑定，优先读取环境变量，再读取本地 `.env` 文件。"""

    def __init__(self, env_file: Path = Path(".env")) -> None:
        """保存本地环境文件路径，解析时优先使用进程环境变量。"""
        self.env_file = env_file

    def _file_value(self, name: str) -> str | None:
        """从本地 dotenv 文件读取指定名称，找不到时返回空值。"""
        if not self.env_file.is_file():
            return None
        value = dotenv_values(self.env_file).get(name)
        return value if isinstance(value, str) and value else None

    async def resolve(self, reference: str) -> str:
        """解析 env:// 引用，找不到实际值时抛出异常且不返回秘密内容。"""
        if not reference.startswith("env://"):
            raise ValueError("unsupported secret reference")
        name = reference.removeprefix("env://")
        if value := os.environ.get(name):
            return value
        if value := self._file_value(name):
            return value
        raise KeyError(f"secret reference {reference!r} is unavailable")

    async def status(self, reference: str) -> bool:
        """只报告引用是否可解析，不返回密钥值。"""
        if not reference.startswith("env://"):
            return False
        name = reference.removeprefix("env://")
        return name in os.environ or self._file_value(name) is not None


class SecretBindingView:
    """面向 API 的密钥绑定摘要，不包含实际密钥。"""

    def __init__(self, binding_id: str, reference: str, valid: bool) -> None:
        """保存绑定标识、引用和有效性状态。"""
        self.binding_id = binding_id
        self.reference = reference
        self.valid = valid

    def as_dict(self) -> dict[str, object]:
        """转换为前端可展示的非秘密字典。"""
        return {"id": self.binding_id, "reference": self.reference, "valid": self.valid}
