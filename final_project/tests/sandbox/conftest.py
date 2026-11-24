"""격리 테스트용 고정 장치. 실제 Docker로 전략 이미지를 빌드한다(없으면 건너뛰지 않고 실패한다)."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEST_IMAGE = "nh-mcp-sandbox:test"


@pytest.fixture(scope="session")
def sandbox_image() -> str:
    """전략 이미지를 빌드하고 이름을 돌려준다. 세션마다 한 번만 빌드한다."""
    try:
        version = subprocess.run(
            ["docker", "version"], capture_output=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        version = None
    if version is None or version.returncode != 0:
        pytest.fail("격리 테스트에는 Docker가 필요합니다. Docker를 켜고 다시 돌리세요.")
    build = subprocess.run(
        ["docker", "build", "-f", "deploy/sandbox.Dockerfile", "-t", TEST_IMAGE, "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    if build.returncode != 0:
        pytest.fail(f"전략 이미지 빌드 실패:\n{build.stderr[-2000:]}")
    return TEST_IMAGE
