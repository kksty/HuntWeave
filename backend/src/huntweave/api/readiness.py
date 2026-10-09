"""The control side's view of execution readiness: observed from the execution side, not assumed."""

from collections.abc import Callable
from time import monotonic

import httpx
from pydantic import ValidationError

from huntweave.contracts.capabilities import Capabilities


class CapabilityProbe:
    """The last observed capability answer, cached briefly and never treated as a current check.

    Run views are rendered often, so a short cache keeps a hung execution side from adding its
    timeout to every response. The cache exists for display only: dispatch still submits every
    call to the execution side instead of consulting this, and an answer that cannot be obtained
    is reported as an observation gap rather than as readiness.
    """

    def __init__(self, read: Callable[[], Capabilities], ttl_seconds: float = 5.0):
        self.read = read
        self.ttl_seconds = ttl_seconds
        self.cached: Capabilities | None = None
        self.read_at = 0.0

    def current(self) -> Capabilities:
        if self.cached is not None and monotonic() - self.read_at < self.ttl_seconds:
            return self.cached
        try:
            observed = self.read()
        except (httpx.HTTPError, ValidationError, ValueError):
            observed = Capabilities.unobserved("runner_unavailable")
        self.cached, self.read_at = observed, monotonic()
        return observed
