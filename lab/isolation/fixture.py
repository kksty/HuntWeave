"""Only fixed local fixture protocols; every byte is synthetic lab data."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def event(value):
    print(json.dumps(value), flush=True)


def serve_connection(connection):
    with connection:
        while connection.recv(16):
            event({"received": True})
            connection.sendall(b"lab-ok")


def serve(port):
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", port))
        server.listen()
        event({"listening": port})
        while True:
            connection, _ = server.accept()
            threading.Thread(target=serve_connection, args=(connection,), daemon=True).start()


def connect(address, port):
    try:
        with socket.create_connection((address, port), timeout=0.6) as connection:
            connection.sendall(b"lab-probe")
            return connection.recv(16) == b"lab-ok"
    except OSError:
        return False


mode = sys.argv[1]
if mode == "target":
    for port in (7000, 7001):
        threading.Thread(target=serve, args=(port,), daemon=True).start()
    while True:
        time.sleep(60)
elif mode == "hold":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(60)
elif mode == "probe":
    event({"connected": connect(sys.argv[2], int(sys.argv[3]))})
elif mode == "tcp-open":
    try:
        with socket.create_connection((sys.argv[2], int(sys.argv[3])), timeout=0.6):
            event({"connected": True})
    except OSError:
        event({"connected": False})
elif mode == "udp":
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        try:
            client.sendto(b"lab-udp", (sys.argv[2], 7000))
            event({"sent": True})
        except PermissionError:
            event({"sent": False, "denied": True})
elif mode == "dns":
    # Valid synthetic query for an RFC 6761 invalid name; the local OUTPUT rule must drop it.
    query = bytes.fromhex("123401000001000000000000") + b"\x05probe\x07invalid\x00\x00\x01\x00\x01"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        try:
            client.sendto(query, ("127.0.0.11", 53))
            event({"sent": True})
        except PermissionError:
            event({"sent": False, "denied": True})
elif mode == "host-ip":
    event({"host_ip": socket.gethostbyname("host.docker.internal")})
elif mode == "stream":
    address = sys.argv[2]
    with socket.create_connection((address, 7000), timeout=1) as connection:
        Path("/tmp/stream-ready").write_text("connected")
        while True:
            try:
                connection.sendall(b"lab-stream")
                assert connection.recv(16) == b"lab-ok"
                with Path("/tmp/stream-heartbeats").open("a") as output:
                    output.write(str(time.monotonic()) + "\n")
                time.sleep(0.05)
            except (OSError, AssertionError):
                Path("/tmp/stream-ended").write_text(str(time.monotonic()))
                break
elif mode == "spawn-stream":
    child = subprocess.Popen([sys.executable, __file__, "stream", sys.argv[2]])
    event({"child": child.pid})
elif mode == "double-fork":
    child = os.fork()
    if child == 0:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        Path("/tmp/orphan-ready").write_text(str(os.getpid()))
        while True:
            connect(sys.argv[2], 7000)
            time.sleep(0.05)
    os.waitpid(child, 0)
    event({"parent_exited": True})
elif mode == "orphan-info":
    pid = Path("/tmp/orphan-ready").read_text().strip()
    status = Path("/proc", pid, "status").read_text().splitlines()
    ppid = int(next(line for line in status if line.startswith("PPid:")).split()[1])
    uid = int(next(line for line in status if line.startswith("Uid:")).split()[1])
    event({"ppid": ppid, "uid": uid})
elif mode == "ipv6":
    event(
        {
            "addresses": Path("/proc/net/if_inet6").read_text().strip(),
            "disabled": Path("/proc/sys/net/ipv6/conf/all/disable_ipv6").read_text().strip(),
            "connected": connect("::1", 7000),
        }
    )
elif mode == "privileges":
    attempts = {}
    for name, command in (
        ("firewall", ["iptables", "-F"]),
        ("route", ["ip", "route", "replace", "default", "via", sys.argv[2]]),
    ):
        result = subprocess.run(command, capture_output=True, check=False)
        attempts[name] = result.returncode != 0
    try:
        socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_TCP).close()
        attempts["raw_socket"] = False
    except PermissionError:
        attempts["raw_socket"] = True
    event({"uid": os.getuid(), "denied": attempts})
else:
    raise ValueError("Unknown fixed fixture operation")
