from importlib import resources

import nh_mcp


def test_package_ships_py_typed_marker() -> None:
    # py.typed가 없으면 이 패키지를 쓰는 쪽에서 pyright가 타입 정보를 버린다.
    assert resources.files(nh_mcp).joinpath("py.typed").is_file()
