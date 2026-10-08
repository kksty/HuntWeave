"""Fixed lab-only network helper. Never interprets a Shell command from a tool."""

import json
import signal
import subprocess
import sys
import time
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

from network_policy import Endpoint, NetworkPolicy


def restore(rules):
    subprocess.run(["iptables-restore", "--wait", "5"], input=rules.encode(), check=True)


mode = sys.argv[1]
if mode == "gateway":
    restore(NetworkPolicy().gateway_rules())
    Path("/tmp/gateway-ready").write_text("default-deny")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(60)
elif mode == "policy":
    values = json.loads(sys.argv[2])
    policy = NetworkPolicy(
        tuple(
            Endpoint(IPv4Address(item["address"]), item["port"]) for item in values["permissions"]
        ),
        tuple(IPv4Network(item) for item in values["protected"]),
    )
    restore(policy.gateway_rules())
    print(json.dumps({"policy": "applied", "permissions": len(policy.permissions)}))
else:
    raise ValueError("Unknown fixed helper operation")
