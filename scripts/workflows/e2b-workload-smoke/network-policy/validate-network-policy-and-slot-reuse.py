import base64
import http.client
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from importlib.metadata import version
from urllib.parse import urlparse

import requests

sys.path.insert(0, os.getcwd())

from conch import Sandbox as ConchSandbox
from e2b.connection_config import ConnectionConfig
from e2b_code_interpreter import Sandbox as CodeInterpreterSandbox
from packaging.version import Version


def log(message):
    print(f"[e2b-network-policy] {message}", flush=True)


REQUEST_TIMEOUT = int(os.environ.get("CONCH_E2B_SDK_HTTP_TIMEOUT", "300"))
_request = requests.sessions.Session.request


def request_with_timeout(self, method, url, **kwargs):
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    return _request(self, method, url, **kwargs)


requests.sessions.Session.request = request_with_timeout


def expected_body_matches(body, expected_body):
    if expected_body is None:
        return True
    if isinstance(expected_body, (tuple, list, set)):
        return body.strip() in expected_body
    return body.strip() == expected_body


def wait_http(url, expected_body=None, timeout=180):
    log(f"waiting for HTTP health: {url}")
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                body = response.read().decode()
                if response.status not in (200, 204):
                    raise RuntimeError(f"{url} returned {response.status}: {body!r}")
                if not expected_body_matches(body, expected_body):
                    raise RuntimeError(f"{url} body={body!r}, want {expected_body!r}")
                return body
        except Exception as exc:
            last_error = exc
            time.sleep(2)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def wait_conch_health(sandbox, timeout=180):
    log(f"waiting for Conch agent health: sandbox_id={sandbox.sandbox_id} ip={sandbox.ip}")
    deadline = time.monotonic() + timeout
    last_health = None
    while time.monotonic() < deadline:
        try:
            response = sandbox.client.health_check()
            if response.get("status") != "OK":
                raise RuntimeError(f"unexpected health response: {response!r}")
            last_health = response
            return last_health
        except Exception as exc:
            last_health = {"status": "ERROR", "message": str(exc)}
        time.sleep(2)
    raise RuntimeError(f"timed out waiting for Conch health check: {last_health}")


def wait_e2b_commands(e2b, sandbox_ip, timeout=30):
    log(f"waiting for E2B command API: sandbox_ip={sandbox_ip}")
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = e2b.commands.run("true")
            if result.exit_code == 0:
                return
            last_error = RuntimeError(
                f"command exited {result.exit_code}: "
                f"stdout={result.stdout!r}, stderr={result.stderr!r}"
            )
        except Exception as exc:
            last_error = exc
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for E2B command API: {last_error}")


def new_code_interpreter_sandbox(envd_url, sandbox_ip):
    config = ConnectionConfig(debug=True, sandbox_url=envd_url)
    sandbox = CodeInterpreterSandbox(
        sandbox_id="debug_sandbox_id",
        sandbox_domain=None,
        envd_version=Version("0.6.1"),
        envd_access_token=None,
        traffic_access_token=None,
        connection_config=config,
    )
    envd_host = urlparse(envd_url).hostname or sandbox_ip
    sandbox.get_host = lambda port: f"{envd_host}:{port}"
    return sandbox


def validate_network_policy(sandbox, e2b):
    response_body = b"conch-network-policy-ok"
    guest_http_port = 49983
    allow_ip = os.environ["CONCH_NETWORK_TEST_ALLOW_IP"]
    deny_ip = os.environ["CONCH_NETWORK_TEST_DENY_IP"]
    stop_server = threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", 0))
    listener.listen(8)
    listener.settimeout(1)
    http_port = listener.getsockname()[1]

    def serve_http():
        response = (
            b"HTTP/1.1 200 OK\r\n"
            + f"Content-Length: {len(response_body)}\r\n".encode()
            + b"Connection: close\r\n\r\n"
            + response_body
        )
        while not stop_server.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if stop_server.is_set():
                    return
                raise
            with connection:
                connection.settimeout(3)
                try:
                    connection.recv(4096)
                    connection.sendall(response)
                except OSError:
                    pass

    def update_network(description, **policy):
        if not sandbox.update_network(**policy):
            raise RuntimeError(f"{description} network update failed")

    def guest_http(address, should_succeed):
        url = f"http://{address}:{http_port}/"
        success_prefix = "guest-http-success="
        error_prefix = "guest-http-error="
        code = f"""
import urllib.request

try:
    body = urllib.request.urlopen({url!r}, timeout=3).read().decode()
except OSError as exc:
    print({error_prefix!r} + f"{{type(exc).__name__}}: {{exc}}")
else:
    print({success_prefix!r} + body)
"""
        encoded = base64.b64encode(code.encode()).decode()
        result = e2b.commands.run(
            f'python -c "import base64; exec(base64.b64decode(\'{encoded}\'))"'
        )
        marker = response_body.decode()
        success_lines = [
            line for line in result.stdout.splitlines() if line.startswith(success_prefix)
        ]
        error_lines = [
            line for line in result.stdout.splitlines() if line.startswith(error_prefix)
        ]
        if success_lines:
            if success_lines[-1] != success_prefix + marker:
                raise RuntimeError(
                    f"guest HTTP returned an unexpected body for {url}: "
                    f"stdout={result.stdout!r}"
                )
            succeeded = True
        elif error_lines:
            succeeded = False
        else:
            raise RuntimeError(
                f"guest HTTP returned no recognized result for {url}: "
                f"exit={result.exit_code}, stdout={result.stdout!r}, "
                f"stderr={result.stderr!r}"
            )
        if succeeded != should_succeed:
            raise RuntimeError(
                f"guest HTTP expectation failed for {url}: "
                f"expected success={should_succeed}, exit={result.exit_code}, "
                f"stdout={result.stdout!r}, stderr={result.stderr!r}"
            )

    def host_http(source_ip, should_succeed):
        connection = http.client.HTTPConnection(
            sandbox.ip,
            guest_http_port,
            timeout=3,
            source_address=(source_ip, 0),
        )
        succeeded = False
        detail = ""
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            response.read()
            succeeded = response.status in (200, 204)
            detail = f"HTTP {response.status}"
        except OSError as exc:
            detail = repr(exc)
        finally:
            connection.close()
        if succeeded != should_succeed:
            raise RuntimeError(
                f"host HTTP expectation failed from {source_ip} to "
                f"{sandbox.ip}:{guest_http_port}: "
                f"expected success={should_succeed}, got {detail}"
            )

    def assert_persisted_network(sandbox_id, expected):
        actual = ConchSandbox.get(sandbox_id).network or {}
        if actual != expected:
            raise RuntimeError(f"persisted network policy is {actual!r}, want {expected!r}")

    server_thread = threading.Thread(target=serve_http, daemon=True)
    server_thread.start()
    try:
        policy = {"denyOut": [deny_ip]}
        log("validating creation-time network policy")
        assert_persisted_network(sandbox.sandbox_id, policy)
        guest_http(allow_ip, True)
        guest_http(deny_ip, False)

        log("validating live egress policy replacement")
        update_network("egress replacement", deny_out=[allow_ip])
        guest_http(allow_ip, False)
        guest_http(deny_ip, True)

        log("validating ingress allow and deny rules")
        update_network("ingress", allow_in=[allow_ip], deny_in=[deny_ip])
        host_http(allow_ip, True)
        host_http(deny_ip, False)

        log("validating allow_internet_access=false")
        update_network("disable internet access", allow_internet_access=False)
        guest_http(allow_ip, False)
        guest_http(deny_ip, False)

        log("validating policy updates while suspended and restoration on resume")
        if not sandbox.suspend():
            raise RuntimeError("sandbox suspend failed")
        update_network("suspended", deny_out=[deny_ip])
        if not sandbox.resume():
            raise RuntimeError("sandbox resume failed")
        wait_conch_health(sandbox)
        wait_http(f"http://{sandbox.ip}:{guest_http_port}/health")
        e2b = new_code_interpreter_sandbox(
            f"http://{sandbox.ip}:{guest_http_port}",
            sandbox.ip,
        )
        guest_http(allow_ip, True)
        guest_http(deny_ip, False)
    finally:
        stop_server.set()
        listener.close()
        server_thread.join(timeout=5)


def validate_cross_sandbox_conntrack_isolation(sandbox):
    response_body = b"conch-conntrack-ok"
    source_port = 40123
    target_ip = os.environ["CONCH_NETWORK_TEST_ALLOW_IP"]
    stop_server = threading.Event()
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind((target_ip, 0))
    server.settimeout(1)
    target_port = server.getsockname()[1]

    def serve_udp():
        while not stop_server.is_set():
            try:
                payload, peer = server.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                if stop_server.is_set():
                    return
                raise
            try:
                server.sendto(response_body + b":" + payload, peer)
            except OSError:
                if stop_server.is_set():
                    return
                raise

    def guest_udp(e2b, marker):
        code = f"""
import socket

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(("0.0.0.0", {source_port}))
sock.settimeout(3)
sock.connect(({target_ip!r}, {target_port}))
sock.send({marker!r})
try:
    response = sock.recv(4096)
except (socket.timeout, OSError):
    print("conntrack-udp-blocked")
else:
    print("conntrack-udp-reply=" + response.decode())
finally:
    sock.close()
"""
        encoded = base64.b64encode(code.encode()).decode()
        result = e2b.commands.run(
            f'python -c "import base64; exec(base64.b64decode(\'{encoded}\'))"'
        )
        reply_marker = f"conntrack-udp-reply={response_body.decode()}:{marker.decode()}"
        if result.exit_code != 0:
            raise RuntimeError(
                "guest UDP probe failed unexpectedly: "
                f"exit={result.exit_code}, stdout={result.stdout!r}, "
                f"stderr={result.stderr!r}"
            )
        if reply_marker in result.stdout:
            return True, result
        if "conntrack-udp-blocked" in result.stdout:
            return False, result
        raise RuntimeError(
            "guest UDP probe returned no recognized result: "
            f"stdout={result.stdout!r}, stderr={result.stderr!r}"
        )

    def run_root(*args):
        result = subprocess.run(
            [
                "sudo",
                "-n",
                "env",
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                *args,
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"root command failed: {args!r}: exit={result.returncode}, "
                f"stdout={result.stdout!r}, stderr={result.stderr!r}"
            )
        return result.stdout

    def find_slot_netns(sandbox_ip):
        output = run_root(
            "find",
            "/run/conch/netns",
            "-mindepth",
            "1",
            "-maxdepth",
            "1",
            "-print",
        )
        for path in sorted(filter(None, output.splitlines())):
            if re.fullmatch(r"/run/conch/netns/slot-[0-9]+", path) is None:
                continue
            addresses = run_root(
                "nsenter", f"--net={path}", "ip", "-o", "-4", "address", "show"
            )
            if re.search(rf"\binet {re.escape(sandbox_ip)}/[0-9]+\b", addresses):
                return path
        raise RuntimeError(f"cannot find network slot for sandbox IP {sandbox_ip}")

    def conntrack_has_established_tuple(netns_path):
        entries = run_root(
            "nsenter", f"--net={netns_path}", "cat", "/proc/net/nf_conntrack"
        )
        pattern = re.compile(
            rf"\budp\b.*\bdst={re.escape(target_ip)} "
            rf"sport={source_port} dport={target_port}\b"
        )
        # A replied UDP flow can match ctstate ESTABLISHED without being ASSURED.
        return any(
            pattern.search(line) and "[UNREPLIED]" not in line
            for line in entries.splitlines()
        )

    def wait_for_established_conntrack(netns_path, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if conntrack_has_established_tuple(netns_path):
                return True
            time.sleep(0.1)
        return conntrack_has_established_tuple(netns_path)

    def wait_for_path(path, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(path):
                return
            time.sleep(0.1)
        raise RuntimeError(f"timed out waiting for controlled CNI refill at {path}")

    server_thread = threading.Thread(target=serve_udp, daemon=True)
    server_thread.start()
    try:
        log("validating conntrack isolation across reused network slots")
        sandbox_ip = sandbox.ip
        e2b = new_code_interpreter_sandbox(
            f"http://{sandbox_ip}:49983",
            sandbox_ip,
        )
        received, result = guest_udp(e2b, b"sandbox-a")
        if not received:
            raise RuntimeError(
                "sandbox A could not establish the conntrack seed flow: "
                f"stdout={result.stdout!r}, stderr={result.stderr!r}"
            )

        slot_netns = find_slot_netns(sandbox_ip)
        if not wait_for_established_conntrack(slot_netns):
            raise RuntimeError(
                f"established conntrack seed tuple not found in {slot_netns}: "
                f"target={target_ip}:{target_port} source_port={source_port}"
            )
        wait_for_path(
            os.path.join(os.environ["CONCH_CNI_CONTROL_DIR"], "refill-blocked")
        )

        if not ConchSandbox.delete_sandbox(sandbox.sandbox_id):
            raise RuntimeError("sandbox A deletion failed")
        if conntrack_has_established_tuple(slot_netns):
            raise RuntimeError(
                f"conntrack seed tuple remained in {slot_netns} after sandbox A deletion: "
                f"target={target_ip}:{target_port} source_port={source_port}"
            )
        log(f"released {slot_netns}: conntrack seed cleared")

        replacement = ConchSandbox.create(
            template_id=os.environ["CONCH_TEMPLATE_ID"],
            sandbox_id=os.environ["CONCH_REUSE_SANDBOX_ID"],
            vcpu_num=2,
            vcpu_max=2,
            ram_mb=2048,
            network={"denyOut": [target_ip]},
        )
        log(
            "created replacement sandbox: "
            f"sandbox_id={replacement.sandbox_id} ip={replacement.ip}"
        )
        if replacement.ip != sandbox_ip:
            raise RuntimeError(
                f"replacement sandbox did not reuse {slot_netns}: "
                f"ip={replacement.ip}, want {sandbox_ip}"
            )
        replacement_netns = find_slot_netns(replacement.ip)
        if replacement_netns != slot_netns:
            raise RuntimeError(
                f"replacement sandbox used {replacement_netns}, want {slot_netns}"
            )
        wait_conch_health(replacement)
        wait_http(f"http://{replacement.ip}:49983/health")
        replacement_e2b = new_code_interpreter_sandbox(
            f"http://{replacement.ip}:49983",
            replacement.ip,
        )
        wait_e2b_commands(replacement_e2b, replacement.ip)
        bypassed, result = guest_udp(replacement_e2b, b"sandbox-b")
        if bypassed:
            raise RuntimeError(
                "network slot reuse let sandbox B bypass denyOut: "
                f"stdout={result.stdout!r}, stderr={result.stderr!r}"
            )
        log("replacement sandbox denyOut blocked the reused UDP tuple")
    finally:
        stop_server.set()
        server.close()
        server_thread.join(timeout=5)


def main():
    log(
        f"using e2b={version('e2b')} "
        f"e2b-code-interpreter={version('e2b-code-interpreter')}"
    )
    log("creating sandbox A with its initial network policy")
    conch_sandbox = ConchSandbox.create(
        template_id=os.environ["CONCH_TEMPLATE_ID"],
        sandbox_id=os.environ["CONCH_SANDBOX_ID"],
        vcpu_num=2,
        vcpu_max=2,
        ram_mb=2048,
        network={"denyOut": [os.environ["CONCH_NETWORK_TEST_DENY_IP"]]},
    )
    log(
        "created sandbox A: "
        f"sandbox_id={conch_sandbox.sandbox_id} ip={conch_sandbox.ip}"
    )
    wait_conch_health(conch_sandbox)

    sandbox_ip = conch_sandbox.ip
    envd_url = f"http://{sandbox_ip}:49983"
    wait_http(f"{envd_url}/health")
    e2b = new_code_interpreter_sandbox(envd_url, sandbox_ip)

    validate_network_policy(conch_sandbox, e2b)
    validate_cross_sandbox_conntrack_isolation(conch_sandbox)

    log("sandbox network policy and slot-reuse regression ok")


if __name__ == "__main__":
    main()
