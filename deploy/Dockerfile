FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src
RUN npm run build

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
COPY profiles /opt/huntweave/profiles
COPY --from=frontend /frontend/dist /opt/huntweave/frontend/dist
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin huntweave \
    && mkdir /evidence /runner-state && chown huntweave:huntweave /evidence /runner-state
USER 10001:10001
CMD ["python", "-m", "huntweave.api.supervisor"]

FROM control AS development
USER root
RUN chown -R huntweave:huntweave /opt/huntweave/backend/src /opt/huntweave/backend/migrations
USER 10001:10001

FROM control AS checks
USER root
RUN uv sync --locked --no-managed-python
COPY backend/tests ./tests
# The whole deployment directory, not only its Python entry points: the checks read the Compose
# files and this Dockerfile as the artifacts that decide the deployment boundary.
COPY deploy /opt/huntweave/deploy
USER 10001:10001
ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider", "-m", "not integration", "tests"]
