"""리소스 strategy://contract: 전략 계약 전부와 남는 제약. AI가 범위 밖 전략을 몰래 비슷한 것으로
바꿔 짜지 않게 할 수 없는 것을 적는다.

숫자는 모두 그 값을 쓰는 곳의 상수에서 만든다(글이 코드와 어긋나지 않게). 격리 실행 자원은
docker run 고정 인자에서 읽는다. 전략 이미지의 라이브러리 버전은 서버 이미지에 Dockerfile이 없어
여기 목록으로 두고, tests/test_deploy.py가 Dockerfile과 같은지 본다.
"""

from typing import get_args

from nh_mcp.domain import MAX_MODEL_BYTES, SandboxFailure
from nh_mcp.models.store import MAX_FILES
from nh_mcp.ops.rate_limit import BACKTEST_BUCKET, LIMITS, WINDOW
from nh_mcp.sandbox.container import (
    FIXED_ARGS,
    MAX_LINE,
    MAX_STDOUT,
    STDOUT_PER_EVENT,
    WALL_SECONDS,
)
from nh_mcp.sandbox.wire import MAX_LEVELS
from nh_mcp.services.backtest import (
    MAX_BARS,
    MAX_CODE_BYTES,
    MAX_EVENTS,
    MAX_PARAMS_BYTES,
    MAX_POSITION,
    MAX_SYMBOLS,
    MAX_TICK_EVENTS,
    PER_USER,
)
from nh_mcp.services.recording import MAX_SUBSCRIPTIONS
from nh_mcp.store.ticks import RETENTION

CONTRACT_URI = "strategy://contract"
# 전략 이미지(deploy/sandbox.Dockerfile)의 Python과 라이브러리 버전.
PYTHON = "3.12"
LIBRARIES: tuple[tuple[str, str], ...] = (
    ("numpy", "2.5.5"),
    ("pandas", "3.1.1"),
    ("scipy", "1.18.1"),
    ("scikit-learn", "1.9.1"),
    ("statsmodels", "0.15.0"),
    ("lightgbm", "4.7.0"),
    ("torch", "2.15.0+cpu"),
)
KB = 1024
MB = 1024 * 1024


def docker_value(flag: str, prefix: str = "") -> str:
    """고정 인자에서 flag 바로 뒤 값 가운데 prefix로 시작하는 것(prefix는 떼고)."""
    for i, arg in enumerate(FIXED_ARGS[:-1]):
        value = FIXED_ARGS[i + 1]
        if arg == flag and value.startswith(prefix):
            return value.removeprefix(prefix)
    raise LookupError(f"docker 고정 인자에 {flag} {prefix}가 없다")


def _megabytes(docker_size: str) -> str:
    # docker의 m은 MiB다. 문서와 오류 문구가 MB로 쓰므로 맞춘다.
    return docker_size.removesuffix("m") + "MB"


def _libraries() -> str:
    return ", ".join(
        f"{name} {version.removesuffix('+cpu')}(CPU)"
        if version.endswith("+cpu")
        else f"{name} {version}"
        for name, version in LIBRARIES
    )


def contract_text(models_url: str) -> str:
    cpu = docker_value("--ulimit", "cpu=").split(":")[0]
    tmp = docker_value("--tmpfs", "/tmp:").split(",")[0].removeprefix("size=")
    seed = docker_value("-e", "PYTHONHASHSEED=")
    learning = "·".join(name for name, _ in LIBRARIES[2:])
    failed = ", ".join(
        r for r in get_args(SandboxFailure.__value__) if not r.startswith("invalid_")
    )
    return f"""# 전략 계약과 남는 제약

backtest_strategy와 preview_strategy가 받는 전략의 전부다. 여기에 없는 것은 할 수 없다.

## 1. 계약

- 흐름: data.streams = {{종목코드: [흐름...]}}, 종목 {MAX_SYMBOLS}개까지. 실행은 둘 중 하나다. 봉 \
실행은 "minute"(1분봉)·"day"(일봉)이고 NH에서 받는다. 틱 실행은 "tick"(체결)·"book"(호가)에 \
"day"를 더할 수 있고, 그 사용자가 set_tick_recording으로 저장한 것만 쓴다.
- "minute"과 "tick"·"book"은 섞지 않는다. 실시간 1분봉은 그 분이 끝나고 몇 초 뒤에 와서 백테스트와 \
같은 순서를 낼 수 없다. 틱 실행에서 1분봉이 필요하면 코드가 체결로 만든다.
- 코드: on_event(event, ctx) 필수, init(ctx) 선택(처음 한 번). 서버가 이벤트를 하나 넣고 출력을 \
받은 뒤에 다음 이벤트를 넣는다. 아직 오지 않은 이벤트는 볼 수 없다.
- 이벤트(가격은 문자열, at은 KST): 봉 {{type:"bar", symbol, interval, at(봉 시작), open, high, \
low, close, volume}}, 체결 {{type:"tick", symbol, at, price, qty, bid, ask}}, 호가 {{type:"book", \
symbol, at, bids:[[가격, 잔량]...], asks}}(최대 {MAX_LEVELS}단계), 내 체결 {{type:"fill", symbol, \
at, side, qty, price}}.
- 순서: 봉은 끝난 시각 순서(같으면 일봉 먼저, 그다음 종목코드), 틱·호가는 받은 순서. 일봉은 그날이 \
끝난 뒤에만 온다. 체결은 그 체결을 낳은 이벤트보다 먼저 온다.
- ctx: params, positions(종목별 체결된 포지션), history(symbol, interval)(지난 봉, 이번 것 제외), \
model_path(모델 파일 경로 또는 None).
- 출력: None(그대로) 또는 {{종목코드: {{"position": 정수(롱 +, 숏 -, 0 정리), "price": \
"지정가"}}}}. price가 없으면 시장가다. 종목은 선언한 것만, |position|은 \
settings.max_position(1~{MAX_POSITION}) 이하, 가격은 그 종목 틱 단위. 어기면 실패한다.
- 체결 가정: 판단한 뒤 시작한 그 종목의 첫 봉·체결에서만. 시장가는 그 시가(틱 실행은 체결가)에서 \
불리한 쪽으로 1틱. 지정가는 저가가 지정가보다 낮을 때(팔 때는 고가가 높을 때)만 지정가에 전량이고, \
닿기만 해서는 체결되지 않는다. 호가로는 체결하지 않는다. 새 목표는 그 종목의 걸린 주문을 취소한다.
- 손절·익절은 전략이 목표를 바꾸는 것으로 표현한다. 서버가 대신 걸어 두는 조건부 주문은 없다.
- 예제(strategy://examples/): mean-reversion(한 종목), pair-spread(두 종목), scale-in-limit(수량 \
조절과 지정가), tick-momentum(틱 실행, 체결로 1분봉을 만든다).

## 2. 남는 제약

- 안전(바꾸지 않음): 코드는 네트워크·서버 환경 변수·DB·키에 닿지 못하고 직접 주문하지 못한다(목표 \
포지션만). 사용자 한도, 승인 범위, 거래 시간, 틱 단위를 넘을 수 없다.
- NH: 과거 체결·호가가 없다. 그 사용자가 저장을 켠 뒤 받은 것만, 평일 주간장만, {RETENTION.days}일 \
보관. 구독은 APP Key당 {MAX_SUBSCRIPTIONS}건(틱 저장과 전략 합산). 끊긴 동안의 틱은 다시 받지 못해 \
빈 구간으로 남는다. 국내 선물만(옵션·해외 없음). 야간 주문과 야간 분봉 없음(모의). 모의 1분봉은 \
2026-09-07부터 촘촘하고 일봉은 2023-12-15부터다.
- 데이터 크기: 봉 실행은 흐름마다 {MAX_BARS:,}봉, 합계 {MAX_EVENTS:,}개. 틱 실행은 일봉을 합쳐 \
{MAX_TICK_EVENTS:,}개. 넘으면 가장 최근 것만 넣고 truncated로 알린다. 코드 \
{MAX_CODE_BYTES // KB}KB, params는 JSON {MAX_PARAMS_BYTES // KB}KB(실수 금지, 소수는 "0.02"처럼 \
문자열).
- 서버 자원: CPU {cpu}초, 벽시계 {WALL_SECONDS:g}초(라이브러리 불러오기 포함), 메모리 \
{_megabytes(docker_value("--memory"))}(모델 파일 포함), 프로세스·스레드 \
{docker_value("--pids-limit")}개, 출력 한 줄 {MAX_LINE // KB}KB·합계 이벤트당 \
{STDOUT_PER_EVENT}바이트(최소 {MAX_STDOUT // MB}MB), 쓸 수 있는 곳은 /tmp {_megabytes(tmp)}뿐, GPU \
없음. 사용자당 동시 백테스트 {PER_USER}개, {WINDOW.seconds // 60}분에 {LIMITS[BACKTEST_BUCKET]}번.
- 라이브러리: 표준 라이브러리(Python {PYTHON}), {_libraries()}. 설치는 못 한다. {learning}를 모두 \
불러오면 메모리를 약 360MB 쓴다(측정).
- 모델 파일: {models_url} 화면에서 올린다. 파일당 {MAX_MODEL_BYTES // MB}MB, 사용자당 \
{MAX_FILES}개. strategy.model에 sha256을 넣으면 컨테이너 안 ctx.model_path에서만 읽힌다.
- 외부 데이터: NH 데이터만 넣는다. 다른 시세, 뉴스, 경제 지표는 쓸 수 없다.
- 재현성: random·numpy·torch(불러올 때) 시드 {seed}, PYTHONHASHSEED={seed}. 현재 시각이나 \
os.urandom을 읽으면 같은 입력에서도 결과가 갈린다.
- 시뮬레이션: 체결은 가정이다(지정가 대기 순서를 모르고, 호가로는 체결하지 않는다). 마지막 \
이벤트의 신호는 체결할 곳이 없어 버린다. 끝까지 남은 포지션은 마지막 가격으로 평가만 하고 순손익에 \
넣지 않는다. 알림은 없다(사용자가 물어야 답한다).
- 지연: 초단타는 안 된다. 컨테이너 이벤트 왕복 약 0.32ms(Mac에서 측정, 서버는 아직), 주문 REST \
60~70ms(측정).

## 3. 범위 밖이면

사용자의 전략이 위 계약이나 제약 밖이면 비슷한 다른 전략으로 몰래 바꿔 짜지 않는다. 무엇이 안 되고 \
왜 안 되는지 먼저 말하고, 어떻게 바꿔 시험할지 사용자와 정한다. 예: 외부 데이터, 1분봉과 틱 섞기, \
서버가 거는 손절 주문, 대기 순서를 따지는 지정가 체결, 옵션·해외 종목.

backtest_strategy가 거절하는 사유:
- INVALID_INPUT: 입력 형식, 코드·params 크기, 종목 수, 흐름 섞기, 선물이 아닌 종목, 없는 모델 \
파일, 저장한 체결·호가가 없는 기간, 데이터 부족, 틱 크기를 모르는 종목, 코드를 불러오지 못함, \
계약에 맞지 않는 출력.
- STRATEGY_FAILED(error.rule): {failed}. 코드의 오류이거나 위 자원 제한을 넘은 것이다.
- TEMPORARILY_UNAVAILABLE: 실행기가 바쁘거나 동시 한도·속도 제한에 걸림. 잠시 뒤 다시 한다.
"""
