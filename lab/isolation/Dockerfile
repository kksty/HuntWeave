FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS probe
# /workspace is where a tool container's private volume is mounted, and Docker gives a fresh volume
# the ownership of the image's directory it covers. Without it the volume comes up root-owned and the
# unprivileged tool user (10001) cannot write into its own workspace — which is what a real call's
# private reproduction material is. The production tool images provide the same directory; the lab
# image now does too, so a probe can exercise a workspace that really holds bytes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends iptables=1.8.9-2 iproute2=6.1.0-3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin probe \
    && mkdir /workspace \
    && chown probe:probe /workspace
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY backend/src/huntweave/execution/network_policy.py /lab/network_policy.py
COPY lab/isolation/network.py lab/isolation/fixture.py /lab/
USER 10001:10001
CMD ["python", "/lab/fixture.py", "hold"]

FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv
FROM python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258 AS lab
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy PATH=/opt/venv/bin:$PATH PYTHONPATH=/opt/huntweave/src
WORKDIR /opt/huntweave
COPY --from=uv /uv /usr/local/bin/uv
COPY backend/pyproject.toml backend/uv.lock ./
# The main dependencies carry the Docker client and pydantic; the lab group adds the host probe's
# psutil. Both agents below are lab-only images: the product's Runner never builds them.
RUN uv sync --locked --no-dev --group lab --no-install-project --no-managed-python
COPY backend/src ./src

FROM lab AS manager
COPY lab/isolation/manager.py ./manager.py
CMD ["python", "/opt/huntweave/manager.py"]

FROM lab AS sandbox
COPY profiles /opt/huntweave/profiles
COPY lab/isolation/lifecycle.py ./lifecycle.py
CMD ["python", "/opt/huntweave/lifecycle.py"]

FROM lab AS egress
COPY profiles /opt/huntweave/profiles
COPY lab/isolation/egress.py ./egress.py
CMD ["python", "/opt/huntweave/egress.py"]

FROM lab AS action
COPY profiles /opt/huntweave/profiles
COPY lab/isolation/action.py ./action.py
CMD ["python", "/opt/huntweave/action.py"]

FROM lab AS retention
COPY profiles /opt/huntweave/profiles
COPY lab/isolation/retention.py ./retention.py
CMD ["python", "/opt/huntweave/retention.py"]