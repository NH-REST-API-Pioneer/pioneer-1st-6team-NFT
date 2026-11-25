"""모델 파일 보관. 파일은 `<모델 폴더>/<사용자 id>/<sha256>`, 목록은 DB의 `models` 표에 둔다.

서버는 파일을 열어 해석하지 않는다(안전 불변식 12). 올릴 때 바이트를 세고 SHA-256을 재고, 백테스트할
때 바이트를 그대로 읽어 실행기로 넘길 뿐이다. 경로에는 사용자 id(정수)와 검사한 해시만 들어간다.
사용자가 붙인 이름은 화면에만 쓴다.

쓰기는 같은 폴더의 임시 파일(`.upload-`로 시작)에 하고 fsync한 뒤 이름을 바꾼다. 서버가 쓰다
멈추면 임시 파일만 남고, 다음 시작 때 `remove_drafts`가 지운다. 파일을 먼저 두고 목록에 넣으므로,
그 사이에 멈추면 목록에 없는 파일만 남는다(아무도 닿지 못하고, 같은 파일을 다시 올리면 덮인다).
"""

import asyncio
import hashlib
import os
import re
import sqlite3
import tempfile
import unicodedata
from pathlib import Path
from typing import Literal

from nh_mcp.domain import MAX_MODEL_BYTES, Clock, ModelHash, UserId
from nh_mcp.store import models
from nh_mcp.store.models import ModelRow

# 사용자당 모델 파일 수(사람 결정, 2026-11-25).
MAX_FILES = 5
DRAFT_PREFIX = ".upload-"
NAME_MAX = 100
# 이름이 비면 쓰는 이름.
UNNAMED = "model"
_PATH_PARTS = re.compile(r"[/\\]")

type UploadResult = Literal["uploaded", "too_many", "empty"]


class TooBig(Exception):
    """파일이 MAX_MODEL_BYTES를 넘었다."""


def display_name(filename: str) -> str:
    """화면에 보여 줄 이름. 경로 앞부분을 떼고 제어·서식 문자를 빼고 NAME_MAX자로 자른다."""
    last = _PATH_PARTS.split(filename)[-1]
    kept = "".join(ch for ch in last if not unicodedata.category(ch).startswith("C")).strip()
    return kept[:NAME_MAX] or UNNAMED


class Draft:
    """올리는 중인 파일 하나. 그 사용자 폴더의 임시 파일에 받는 대로 쓰며 크기와 해시를 잰다."""

    def __init__(self, folder: Path) -> None:
        # mkstemp는 0600으로 만든다.
        fd, name = tempfile.mkstemp(prefix=DRAFT_PREFIX, dir=folder)
        self.path = Path(name)
        self._file = os.fdopen(fd, "wb")
        self._hash = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> None:
        """이어 쓴다. 상한을 넘게 되면 쓰지 않고 TooBig."""
        if self.size + len(data) > MAX_MODEL_BYTES:
            raise TooBig
        self._file.write(data)
        self._hash.update(data)
        self.size += len(data)

    def finish(self) -> ModelHash:
        """디스크까지 쓰고 닫는다. 내용의 SHA-256을 돌려준다."""
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        return ModelHash(self._hash.hexdigest())

    def discard(self) -> None:
        self._file.close()
        self.path.unlink(missing_ok=True)


def remove_drafts(root: Path) -> int:
    """지난 실행이 쓰다 남긴 임시 파일을 지운다. 올리는 중인 것이 없는 서버 시작 때 부른다."""
    left = sorted(root.glob(f"*/{DRAFT_PREFIX}*"))
    for path in left:
        path.unlink(missing_ok=True)
    return len(left)


class ModelStore:
    def __init__(self, root: Path, conn: sqlite3.Connection, clock: Clock) -> None:
        self._root = root
        self._conn = conn
        self._clock = clock

    def files(self, user_id: UserId) -> list[ModelRow]:
        return models.list_for(self._conn, user_id)

    def exists(self, user_id: UserId, sha256: ModelHash) -> bool:
        """이 사용자의 목록에 있고 파일도 있는가. 다른 사용자의 해시는 없는 것이다."""
        return (
            models.get(self._conn, user_id, sha256) is not None
            and self._path(user_id, sha256).is_file()
        )

    async def read(self, user_id: UserId, sha256: ModelHash) -> bytes | None:
        """이 사용자의 파일 내용. 목록에 없거나 파일이 없으면 None."""
        if models.get(self._conn, user_id, sha256) is None:
            return None
        try:
            return await asyncio.to_thread(self._path(user_id, sha256).read_bytes)
        except FileNotFoundError:
            return None

    def draft(self, user_id: UserId) -> Draft:
        folder = self._root / str(user_id)
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.mkdir(mode=0o700, exist_ok=True)
        return Draft(folder)

    async def commit(self, user_id: UserId, draft: Draft, name: str) -> UploadResult:
        """다 받은 초안을 보관한다. 같은 사용자의 같은 파일은 한 줄로 남는다(다시 올려도 같다)."""
        if draft.size == 0:
            draft.discard()
            return "empty"
        sha256 = await asyncio.to_thread(draft.finish)
        # 여기부터 await가 없다. 같은 사용자가 동시에 올려도 개수 확인과 추가 사이에 끼지 못한다.
        known = models.get(self._conn, user_id, sha256) is not None
        if not known and models.count(self._conn, user_id) >= MAX_FILES:
            draft.discard()
            return "too_many"
        # 목록에 있는데 파일이 없어진 경우도 이것으로 되살아난다.
        os.replace(draft.path, self._path(user_id, sha256))
        models.add(self._conn, user_id, ModelRow(sha256, name, draft.size, self._clock()))
        return "uploaded"

    def delete(self, user_id: UserId, sha256: ModelHash) -> bool:
        """목록에서 빼고 파일을 지운다. 이 사용자에게 없던 해시면 False."""
        if not models.remove(self._conn, user_id, sha256):
            return False
        self._path(user_id, sha256).unlink(missing_ok=True)
        return True

    def _path(self, user_id: UserId, sha256: ModelHash) -> Path:
        return self._root / str(user_id) / sha256
