"""NH 키를 AES-GCM으로 암호화한다.

암호문은 "사용자 · 시장 · 항목"에 묶는다. 다른 사용자의 자리로 옮긴 암호문은 풀리지 않는다.
"""

import os
from dataclasses import dataclass
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from nh_mcp.domain import Market, UserId

KEY_BYTES = 32
NONCE_BYTES = 12

type Field = Literal["app_key", "app_secret"]


class VaultError(Exception):
    """열쇠가 잘못됐거나, 암호문이 다른 자리에서 왔거나, 변조됐다. 메시지에 키를 넣지 않는다."""


@dataclass(frozen=True)
class Slot:
    user_id: UserId
    market: Market
    field: Field

    def context(self) -> bytes:
        return f"nh_mcp:v1:{self.user_id}:{self.market}:{self.field}".encode()


class Vault:
    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise VaultError(f"암호화 열쇠는 {KEY_BYTES}바이트여야 합니다.")
        self._aead = AESGCM(key)

    def seal(self, plaintext: str, slot: Slot) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext.encode(), slot.context())

    def unseal(self, sealed: bytes, slot: Slot) -> str:
        try:
            plain = self._aead.decrypt(sealed[:NONCE_BYTES], sealed[NONCE_BYTES:], slot.context())
        except InvalidTag:
            raise VaultError("저장된 키를 풀 수 없습니다.") from None
        return plain.decode()
