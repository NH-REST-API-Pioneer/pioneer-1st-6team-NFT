"""설정.

NH 주소는 모의 서버로 고정한다. 실전 주소로 바꾸는 환경 변수나 인자는 두지 않는다.
"""

from nh_mcp.domain import Env

NH_BASE_URL = "https://apidemo.futures.co.kr"
NH_STREAM_URL = "wss://apidemo.futures.co.kr"
ENV: Env = "demo"
