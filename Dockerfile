FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY examples ./examples
ENV PATH=/app/.venv/bin:$PATH
EXPOSE 12411 12412
ENTRYPOINT ["gmail-mock", "--host", "0.0.0.0", "--http-port", "12411", "--https-port", "12412"]
