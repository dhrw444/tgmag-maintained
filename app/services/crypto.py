"""敏感字段的加解密与脱敏。

密钥只有 FERNET_KEY 一个，换密钥等于让库里所有 *_encrypted 字段失效。
加解密都在进程内同步完成（Fernet 是纯 CPU 的 AES-CBC + HMAC），
没有 IO，所以不需要 async 包装。

encrypt/decrypt 对 None 直接透传，而不是抛异常 —— 调用方大量在
"这个字段可能没填"的路径上调用，让它们每处都判空只会制造重复代码。
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from app.config import settings

fernet = Fernet(settings.fernet_key.encode())


def encrypt_text(value: str | None) -> str | None:
    """加密为 ASCII 字符串（Fernet token 本身是 base64）。None 原样返回。"""
    if value is None:
        return None
    return fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(value: str | None) -> str | None:
    """解密 encrypt_text 的输出。None 原样返回；密钥不对会抛 InvalidToken。"""
    if value is None:
        return None
    return fernet.decrypt(value.encode("ascii")).decode("utf-8")


def mask_phone(phone: str) -> str:
    """手机号掩码，用于列表展示。

    保留前 4 位（含国家码 + 区号）和后 3 位，中间固定 4 个星号 ——
    星号数量不随号码长度变化，避免从掩码长度反推出号码位数。
    短号码（<=7）只留前 2 位，因为再截就剩不下可读信息了。
    """
    clean = phone.strip()
    if len(clean) <= 7:
        return clean[:2] + "***"
    return clean[:4] + "****" + clean[-3:]
