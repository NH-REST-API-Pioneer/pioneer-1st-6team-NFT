import os

import pytest

from nh_mcp.domain import UserId
from nh_mcp.keys.vault import KEY_BYTES, Slot, Vault, VaultError

A_KEY = Slot(UserId(1), "domestic", "app_key")


def test_roundtrip() -> None:
    vault = Vault(os.urandom(KEY_BYTES))
    assert vault.unseal(vault.seal("가짜-앱키-1234", A_KEY), A_KEY) == "가짜-앱키-1234"


def test_same_plaintext_seals_differently() -> None:
    vault = Vault(os.urandom(KEY_BYTES))
    assert vault.seal("같은 값", A_KEY) != vault.seal("같은 값", A_KEY)


@pytest.mark.parametrize(
    "other",
    [
        Slot(UserId(2), "domestic", "app_key"),
        Slot(UserId(1), "overseas", "app_key"),
        Slot(UserId(1), "domestic", "app_secret"),
    ],
)
def test_sealed_value_does_not_open_in_another_slot(other: Slot) -> None:
    vault = Vault(os.urandom(KEY_BYTES))
    sealed = vault.seal("가짜-앱키", A_KEY)
    with pytest.raises(VaultError):
        vault.unseal(sealed, other)


def test_wrong_master_key_fails() -> None:
    sealed = Vault(os.urandom(KEY_BYTES)).seal("가짜-앱키", A_KEY)
    with pytest.raises(VaultError):
        Vault(os.urandom(KEY_BYTES)).unseal(sealed, A_KEY)


def test_tampered_value_fails() -> None:
    vault = Vault(os.urandom(KEY_BYTES))
    sealed = bytearray(vault.seal("가짜-앱키", A_KEY))
    sealed[-1] ^= 1
    with pytest.raises(VaultError):
        vault.unseal(bytes(sealed), A_KEY)


def test_master_key_must_be_32_bytes() -> None:
    with pytest.raises(VaultError):
        Vault(os.urandom(16))
