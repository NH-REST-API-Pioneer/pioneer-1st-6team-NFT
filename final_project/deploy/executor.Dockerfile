# 전략 실행기 이미지. docker 소켓으로 전략 컨테이너를 띄우는 작은 서버다.
# 비밀값은 넣지 않는다. 실행할 때 SANDBOX_TOKEN, SANDBOX_IMAGE만 환경 변수로 받는다.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.18 /uv /usr/local/bin/uv
# docker 명령(CLI)만 복사한다. 서버 Docker 주 버전(29)에 맞춘 공식 이미지다.
COPY --from=docker:29.4.0-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# root가 아닌 계정으로 돈다. docker 소켓은 compose의 group_add(DOCKER_GID)로 받는다.
RUN useradd --system --uid 10002 --home-dir /nonexistent nhexec
USER nhexec

# docker 명령이 설정 파일을 쓸 곳(읽기 전용 파일 시스템이라 /tmp는 tmpfs).
ENV PATH=/app/.venv/bin:$PATH \
    DOCKER_CONFIG=/tmp/.docker
EXPOSE 8100
CMD ["python", "-m", "nh_mcp.sandbox.executor"]
