from typing import Literal

from pydantic import BaseModel, ConfigDict


class Capabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["1"] = "1"
    mode: Literal["demonstration"] = "demonstration"
    fake_execution_ready: bool = False
    real_execution_ready: Literal[False] = False
    reason_code: Literal["environment_unsupported"] = "environment_unsupported"
    profile_status: Literal["unvalidated"] = "unvalidated"
