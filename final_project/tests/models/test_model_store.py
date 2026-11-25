"""모델 파일 보관: 사용자 폴더의 <sha256> 파일과 DB 목록, 크기·개수 상한, 같은 파일은 한 줄."""

import asyncio
import hashlib
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nh_mcp.config import load_settings
from nh_mcp.domain import MAX_MODEL_BYTES, ModelHash, UserId
from nh_mcp.models.store import (
    DRAFT_PREFIX,
    MAX_FILES,
    ModelStore,
    TooBig,
    UploadResult,
    display_name,
    remove_drafts,
)
from nh_mcp.store import db, users

NOW = datetime(2026, 12, 1, 1, 0, tzinfo=UTC)


class Shelf:
    """두 사용자와 tmp 폴더의 보관소."""

    def __init__(self, root: Path) -> None:
        self.root = root / "models"
        self.conn = db.connect(":memory:")
        self.store = ModelStore(self.root, self.conn, lambda: NOW)
        self.alice: UserId = users.get_or_create(self.conn, "g-a", "a@example.com", NOW)
        self.bob: UserId = users.get_or_create(self.conn, "g-b", "b@example.com", NOW)

    def upload(self, user: UserId, content: bytes, name: str = "model.pkl") -> UploadResult:
        draft = self.store.draft(user)
        draft.write(content)
        return asyncio.run(self.store.commit(user, draft, name))

    def files_on_disk(self) -> list[str]:
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())


def sha(content: bytes) -> ModelHash:
    return ModelHash(hashlib.sha256(content).hexdigest())


def test_upload_is_kept_under_user_and_hash_with_private_permissions(tmp_path: Path) -> None:
    shelf = Shelf(tmp_path)
    content = b"\x80\x04 a pickled model"

    assert shelf.upload(shelf.alice, content, "my model.pkl") == "uploaded"

    path = shelf.root / str(shelf.alice) / sha(content)
    assert path.read_bytes() == content
    assert shelf.files_on_disk() == [f"{shelf.alice}/{sha(content)}"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(shelf.root.stat().st_mode) == 0o700
    [row] = shelf.store.files(shelf.alice)
    assert (row.sha256, row.name, row.size, row.uploaded_at) == (
        sha(content),
        "my model.pkl",
        len(content),
        NOW,
    )
    assert asyncio.run(shelf.store.read(shelf.alice, sha(content))) == content
    assert shelf.store.exists(shelf.alice, sha(content))


def test_same_file_twice_is_one_entry(tmp_path: Path) -> None:
    shelf = Shelf(tmp_path)

    assert shelf.upload(shelf.alice, b"same", "first.pkl") == "uploaded"
    assert shelf.upload(shelf.alice, b"same", "second.pkl") == "uploaded"

    assert [r.name for r in shelf.store.files(shelf.alice)] == ["first.pkl"]
    assert shelf.files_on_disk() == [f"{shelf.alice}/{sha(b'same')}"]


def test_sixth_file_is_refused_and_not_kept(tmp_path: Path) -> None:
    shelf = Shelf(tmp_path)
    for n in range(MAX_FILES):
        assert shelf.upload(shelf.alice, f"model {n}".encode()) == "uploaded"

    assert shelf.upload(shelf.alice, b"model 6") == "too_many"

    assert len(shelf.store.files(shelf.alice)) == MAX_FILES
    assert len(shelf.files_on_disk()) == MAX_FILES
    assert not shelf.store.exists(shelf.alice, sha(b"model 6"))
    # 이미 있는 파일을 다시 올리는 것은 새 파일이 아니다.
    assert shelf.upload(shelf.alice, b"model 0") == "uploaded"
    # 다른 사용자의 개수와는 따로 센다.
    assert shelf.upload(shelf.bob, b"model 6") == "uploaded"


def test_one_byte_over_the_cap_is_refused_while_writing(tmp_path: Path) -> None:
    shelf = Shelf(tmp_path)
    draft = shelf.store.draft(shelf.alice)
    draft.write(bytes(MAX_MODEL_BYTES - 1))
    draft.write(b"x")  # 딱 상한까지는 된다.

    with pytest.raises(TooBig):
        draft.write(b"y")

    assert draft.size == MAX_MODEL_BYTES
    draft.discard()
    assert shelf.files_on_disk() == []
    assert shelf.store.files(shelf.alice) == []


def test_largest_file_is_kept(tmp_path: Path) -> None:
    shelf = Shelf(tmp_path)
    content = os.urandom(MAX_MODEL_BYTES)
    assert shelf.upload(shelf.alice, content) == "uploaded"
    assert asyncio.run(shelf.store.read(shelf.alice, sha(content))) == content


def test_empty_file_is_not_kept(tmp_path: Path) -> None:
    shelf = Shelf(tmp_path)
    assert shelf.upload(shelf.alice, b"") == "empty"
    assert shelf.files_on_disk() == []
    assert shelf.store.files(shelf.alice) == []


def test_another_user_cannot_see_read_or_delete_my_file(tmp_path: Path) -> None:
    shelf = Shelf(tmp_path)
    shelf.upload(shelf.alice, b"alice's model")
    mine = sha(b"alice's model")

    assert shelf.store.files(shelf.bob) == []
    assert not shelf.store.exists(shelf.bob, mine)
    assert asyncio.run(shelf.store.read(shelf.bob, mine)) is None
    assert shelf.store.delete(shelf.bob, mine) is False

    assert asyncio.run(shelf.store.read(shelf.alice, mine)) == b"alice's model"
    # 같은 내용을 B가 올리면 B의 폴더에 따로 둔다.
    assert shelf.upload(shelf.bob, b"alice's model") == "uploaded"
    assert shelf.files_on_disk() == sorted([f"{shelf.alice}/{mine}", f"{shelf.bob}/{mine}"])


def test_delete_removes_entry_and_file(tmp_path: Path) -> None:
    shelf = Shelf(tmp_path)
    shelf.upload(shelf.alice, b"gone soon")

    assert shelf.store.delete(shelf.alice, sha(b"gone soon")) is True

    assert shelf.store.files(shelf.alice) == []
    assert shelf.files_on_disk() == []
    assert asyncio.run(shelf.store.read(shelf.alice, sha(b"gone soon"))) is None
    assert shelf.store.delete(shelf.alice, sha(b"gone soon")) is False


def test_missing_file_reads_as_missing_and_upload_restores_it(tmp_path: Path) -> None:
    shelf = Shelf(tmp_path)
    shelf.upload(shelf.alice, b"model")
    (shelf.root / str(shelf.alice) / sha(b"model")).unlink()

    assert not shelf.store.exists(shelf.alice, sha(b"model"))
    assert asyncio.run(shelf.store.read(shelf.alice, sha(b"model"))) is None

    assert shelf.upload(shelf.alice, b"model") == "uploaded"
    assert asyncio.run(shelf.store.read(shelf.alice, sha(b"model"))) == b"model"


def test_leftover_drafts_are_removed_and_models_stay(tmp_path: Path) -> None:
    shelf = Shelf(tmp_path)
    shelf.upload(shelf.alice, b"kept")
    left = shelf.store.draft(shelf.bob)
    left.write(b"half written")

    assert remove_drafts(shelf.root) == 1

    assert not left.path.exists()
    assert left.path.name.startswith(DRAFT_PREFIX)
    assert shelf.files_on_disk() == [f"{shelf.alice}/{sha(b'kept')}"]
    assert remove_drafts(tmp_path / "nowhere") == 0


@pytest.mark.parametrize(
    ("filename", "shown"),
    [
        ("model.pkl", "model.pkl"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\me\\model.joblib", "model.joblib"),
        ("bad\x00na\nme\u202e.pkl", "badname.pkl"),
        ("  모델 파일.pt  ", "모델 파일.pt"),
        ("", "model"),
        ("dir/", "model"),
        ("a" * 300, "a" * 100),
    ],
)
def test_display_name_is_only_a_label(filename: str, shown: str) -> None:
    assert display_name(filename) == shown


def test_models_dir_sits_next_to_the_database() -> None:
    settings = load_settings(
        {
            "PUBLIC_BASE_URL": "http://localhost:8000",
            "GOOGLE_CLIENT_ID": "fake-client-id",
            "GOOGLE_CLIENT_SECRET": "fake-google-secret-0000",
            "ADMIN_EMAILS": "admin@example.com",
            "DB_PATH": "/data/nh.db",
            "KEY_ENCRYPTION_KEY": "A" * 43 + "=",
            "SANDBOX_URL": "http://executor:8100",
            "SANDBOX_TOKEN": "fake-sandbox-token-" + "0" * 16,
        }
    )
    assert settings.models_dir == Path("/data/models")
