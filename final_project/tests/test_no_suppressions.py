"""lint와 타입 오류를 주석으로 덮지 못하게 막는다. 원인을 고친다."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 이 파일 자신이 걸리지 않게 나눠 적는다.
SUPPRESSION = re.compile("#\\s*(" + "noqa" + "|type:\\s*ignore|pyright:\\s*ignore)", re.IGNORECASE)


def test_checker_catches_suppressions() -> None:
    assert SUPPRESSION.search("x = 1  # " + "noqa: E501")
    assert SUPPRESSION.search("y = f()  # type: " + "ignore[arg-type]")
    assert SUPPRESSION.search("z = g()  # pyright: " + "ignore")
    assert not SUPPRESSION.search("# 주석 안에서 noqa를 설명만 하는 줄")


def test_no_suppression_comments_in_code() -> None:
    files = sorted([*ROOT.glob("src/**/*.py"), *ROOT.glob("tests/**/*.py")])
    assert ROOT / "src/nh_mcp/server.py" in files
    offenders = [
        f"{path.relative_to(ROOT)}:{number}"
        for path in files
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if SUPPRESSION.search(line)
    ]
    assert offenders == []
