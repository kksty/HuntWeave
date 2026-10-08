import os

import httpx

from huntweave.config import runner_token
from huntweave.contracts.capabilities import Capabilities


def get_capabilities(url: str | None = None) -> Capabilities:
    base = url or os.environ.get("HUNTWEAVE_RUNNER_URL", "http://runner:8001")
    response = httpx.get(
        f"{base}/v1/capabilities",
        headers={"Authorization": f"Bearer {runner_token()}"},
        timeout=3,
        trust_env=False,
    )
    response.raise_for_status()
    return Capabilities.model_validate(response.json())
