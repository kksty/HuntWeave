FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS control

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH PYTHONPATH=/opt/huntweave/backend/src \
    LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false
WORKDIR /opt/huntweave/backend
COPY --from=uv /uv /usr/local/bin/uv
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --no-managed-python
COPY backend/src ./src
COPY backend/migrations ./migrations
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin huntweave \
    && mkdir /evidence && chown huntweave:huntweave /evidence
USER 10001:10001
CMD ["python", "-m", "huntweave.api.supervisor"]

FROM control AS checks
USER root
RUN uv sync --locked --no-managed-python
COPY backend/tests ./tests
COPY deploy/*.py /opt/huntweave/deploy/
USER 10001:10001
ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider", "-m", "not integration", "tests"]
