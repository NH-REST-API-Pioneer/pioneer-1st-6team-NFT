"""NH 실전 서버 주소가 저장소 어디에도 들어가지 않게 막는다(안전 불변식 1)."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 이 파일에 실전 주소를 그대로 적으면 이 검사에 걸리므로 나눠 적는다.
REAL_HOST = "api" + ".futures.co.kr"


def mentions_real_host(text: str) -> bool:
    return REAL_HOST in text.lower()


def repo_files() -> list[Path]:
    # 커밋되는 파일과 아직 커밋하지 않은 새 파일. .gitignore와 .git/info/exclude는 빠진다.
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [ROOT / line for line in listed.splitlines() if (ROOT / line).is_file()]


def test_checker_catches_real_host_and_allows_demo() -> None:
    assert mentions_real_host(f"https://{REAL_HOST}/trade/v1/domestic/order")
    assert mentions_real_host(f"WSS://{REAL_HOST.upper()}")
    assert not mentions_real_host("https://apidemo.futures.co.kr/trade/v1/domestic/order")


def test_no_repo_file_mentions_real_host() -> None:
    files = repo_files()
    assert ROOT / "src/nh_mcp/config.py" in files
    offenders = [
        str(path.relative_to(ROOT))
        for path in files
        if mentions_real_host(path.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert offenders == []
