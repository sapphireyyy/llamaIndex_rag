"""内置安全护栏：检查用户输入、检索内容和模型输出中的风险信号。"""

from __future__ import annotations

import re


class BuiltinGuardrails:
    """基于规则的轻量护栏，覆盖提示注入、凭据和受限标识检测。"""

    capabilities = frozenset({"input", "retrieved-content", "output"})

    async def inspect_input(self, text: str) -> list[str]:
        """检查用户输入是否试图覆盖系统指令或索取秘密。"""
        patterns = {
            "prompt_injection": r"ignore (?:all |the )?(?:previous|system)|忽略.{0,8}(?:指令|系统)",
            "system_exfiltration": r"(?:reveal|show|print).{0,12}(?:system prompt|instructions)",
            "secret_request": r"(?:dump|export|show).{0,12}(?:secret|password|token|密钥|口令)",
        }
        return [name for name, pattern in patterns.items() if re.search(pattern, text, re.I)]

    async def inspect_retrieved(self, text: str) -> list[str]:
        """检查检索文本是否包含可能诱导模型执行的伪系统指令。"""
        patterns = [
            r"ignore (?:all |the )?(?:previous|system)",
            r"assistant\s*:\s*",
            r"system\s*:\s*",
        ]
        return [
            "retrieved_prompt_injection"
            for pattern in patterns
            if re.search(pattern, text, re.I)
        ]

    async def inspect_output(self, text: str) -> list[str]:
        """检查模型输出中是否出现私钥、凭据或受限身份号码。"""
        findings: list[str] = []
        if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", text):
            findings.append("private_key")
        if re.search(r"\b(?:sk|api)[-_][A-Za-z0-9]{20,}\b", text):
            findings.append("credential")
        if re.search(r"\b\d{3}-\d{2}-\d{4}\b", text):
            findings.append("restricted_identifier")
        return findings
