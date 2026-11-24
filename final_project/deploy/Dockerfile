# NH선물 MCP 서버 이미지. 비밀값은 이미지에 넣지 않는다(실행할 때 환경 변수로 받는다).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.18 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv

# 의존성 먼저(소스만 바뀌면 이 층은 다시 쓰인다).
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# root가 아닌 계정으로 돈다. 쓰는 곳은 /data(볼륨)와 /tmp뿐이다.
RUN useradd --system --uid 10001 --home-dir /nonexistent nhmcp \
    && mkdir -p /data \
    && chown nhmcp /data
USER nhmcp

ENV PATH=/app/.venv/bin:$PATH \
    DB_PATH=/data/nh.db \
    FASTMCP_HOME=/data/fastmcp \
    BIND_HOST=0.0.0.0
EXPOSE 8000
CMD ["python", "-m", "nh_mcp.server"]
