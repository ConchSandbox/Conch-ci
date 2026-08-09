import base64
import http.client
import os
import re
import socket
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
    print(f"[e2b-workload-smoke] {message}", flush=True)


REQUEST_TIMEOUT = int(os.environ.get("CONCH_E2B_SDK_HTTP_TIMEOUT", "300"))
NETWORK_TEST_URL = os.environ.get(
    "CONCH_E2B_NETWORK_TEST_URL", "https://example.com/"
)
NETWORK_TEST_IP = "223.5.5.5"  # Alibaba Cloud Public DNS
NETWORK_TEST_PORT = 443
INBOUND_REQUEST = b"conch-e2b-inbound-ping\n"
INBOUND_RESPONSE = b"conch-e2b-inbound-ok\n"
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


def dump_guest_logs(e2b):
    for path in (
        "/var/log/conch-agent/conch-agent.log",
        "/var/log/conch-agent/envd.log",
        "/var/log/conch-agent/code-interpreter.log",
        "/var/log/conch-agent/service.log",
    ):
        try:
            log(f"guest log: {path}")
            print(e2b.files.read(path), flush=True)
        except Exception as exc:
            log(f"guest log unavailable: {path}: {exc}")


def logs_stdout_text(result):
    return "\n".join(getattr(line, "text", line) for line in result.logs.stdout)


def validate_guest_url_access(e2b):
    log(f"validating sandbox URL access with DNS: {NETWORK_TEST_URL}")
    result = e2b.run_code(
        f"""
import time
import urllib.request

url = {NETWORK_TEST_URL!r}
for attempt in range(1, 6):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            status = response.status
            body = response.read(4096)
        if status != 200:
            raise RuntimeError(f"{{url}} returned HTTP {{status}}")
        if not body:
            raise RuntimeError(f"{{url}} returned an empty response")
        print(f"url-ok url={{url}} status={{status}} bytes={{len(body)}}")
        break
    except Exception:
        if attempt == 5:
            raise
        time.sleep(2)
""",
        language="python",
    )
    result_text = logs_stdout_text(result)
    if "url-ok" not in result_text:
        raise RuntimeError(
            "sandbox URL access check failed: "
            f"stdout={result_text!r} error={getattr(result, 'error', None)!r}"
        )
    log(result_text.strip())


def validate_guest_outbound_network(e2b):
    log(
        "validating sandbox outbound TCP connectivity: "
        f"{NETWORK_TEST_IP}:{NETWORK_TEST_PORT}"
    )
    result = e2b.run_code(
        f"""
import socket
import time

ip = {NETWORK_TEST_IP!r}
port = {NETWORK_TEST_PORT}
for attempt in range(1, 6):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect((ip, port))
            peer = connection.getpeername()
        print(
            f"network-ok target={{ip}}:{{port}} peer={{peer[0]}}:{{peer[1]}}"
        )
        break
    except OSError:
        if attempt == 5:
            raise
        time.sleep(2)
""",
        language="python",
    )
    result_text = logs_stdout_text(result)
    if "network-ok" not in result_text:
        raise RuntimeError(
            "sandbox outbound network check failed: "
            f"stdout={result_text!r} error={getattr(result, 'error', None)!r}"
        )
    log(result_text.strip())


def validate_guest_inbound_network(e2b, sandbox_ip):
    log("starting one-shot sandbox TCP listener for runner-to-sandbox validation")
    result = e2b.run_code(
        f"""
import socket
import threading

expected_request = {INBOUND_REQUEST!r}
response = {INBOUND_RESPONSE!r}
listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("0.0.0.0", 0))
listener.listen(1)
listener_port = listener.getsockname()[1]

def serve_runner_connection():
    with listener:
        listener.settimeout(30)
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(10)
            request = b""
            while len(request) < len(expected_request):
                chunk = connection.recv(len(expected_request) - len(request))
                if not chunk:
                    break
                request += chunk
            if request != expected_request:
                connection.sendall(b"unexpected-request\\n")
                return
            connection.sendall(response)

threading.Thread(target=serve_runner_connection, daemon=True).start()
print(f"inbound-listener-ready port={{listener_port}}")
""",
        language="python",
    )
    result_text = logs_stdout_text(result)
    match = re.search(r"(?m)^inbound-listener-ready port=([0-9]+)$", result_text)
    if match is None:
        raise RuntimeError(
            "sandbox inbound listener did not start: "
            f"stdout={result_text!r} error={getattr(result, 'error', None)!r}"
        )
    listener_port = int(match.group(1))

    deadline = time.monotonic() + 30
    last_error = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                (sandbox_ip, listener_port), timeout=5
            ) as connection:
                connection.settimeout(5)
                connection.sendall(INBOUND_REQUEST)
                response = b""
                while len(response) < len(INBOUND_RESPONSE):
                    chunk = connection.recv(len(INBOUND_RESPONSE) - len(response))
                    if not chunk:
                        break
                    response += chunk
            if response != INBOUND_RESPONSE:
                raise RuntimeError(
                    "sandbox inbound listener returned unexpected response: "
                    f"{response!r}"
                )
            log(f"inbound-ok source=runner target={sandbox_ip}:{listener_port}")
            return
        except OSError as exc:
            last_error = exc
            time.sleep(1)
    raise RuntimeError(
        f"runner could not reach sandbox TCP listener at "
        f"{sandbox_ip}:{listener_port}: {last_error}"
    )


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
        code = (
            "import urllib.request; "
            f"print(urllib.request.urlopen({url!r}, timeout=3).read().decode())"
        )
        encoded = base64.b64encode(code.encode()).decode()
        result = e2b.commands.run(
            f'python -c "import base64; exec(base64.b64decode(\'{encoded}\'))"'
        )
        marker = response_body.decode()
        succeeded = result.exit_code == 0 and marker in result.stdout
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


def main():
    log(f"using e2b={version('e2b')} e2b-code-interpreter={version('e2b-code-interpreter')}")
    log("creating Conch sandbox")
    conch_sandbox = ConchSandbox.create(
        template_id=os.environ["CONCH_TEMPLATE_ID"],
        sandbox_id=os.environ["CONCH_SANDBOX_ID"],
        vcpu_num=2,
        vcpu_max=2,
        ram_mb=2048,
        network={"denyOut": [os.environ["CONCH_NETWORK_TEST_DENY_IP"]]},
    )
    log(f"created Conch sandbox: sandbox_id={conch_sandbox.sandbox_id} ip={conch_sandbox.ip}")
    wait_conch_health(conch_sandbox)

    sandbox_ip = conch_sandbox.ip
    envd_url = f"http://{sandbox_ip}:49983"
    code_interpreter_url = f"http://{sandbox_ip}:49999"
    wait_http(f"{envd_url}/health")
    e2b = new_code_interpreter_sandbox(envd_url, sandbox_ip)
    try:
        wait_http(f"{code_interpreter_url}/health", expected_body=("OK", '"OK"'))
    except Exception:
        dump_guest_logs(e2b)
        raise

    validate_guest_url_access(e2b)
    validate_guest_outbound_network(e2b)
    validate_guest_inbound_network(e2b, sandbox_ip)

    log("validating E2B SDK file and command operations")
    base = "/tmp/conch-e2b-sdk-test"
    e2b.files.make_dir(base)
    e2b.files.write(f"{base}/hello.txt", "hello-from-e2b")
    if e2b.files.read(f"{base}/hello.txt") != "hello-from-e2b":
        raise RuntimeError("E2B file read returned unexpected content")
    listed = [entry.name for entry in e2b.files.list(base)]
    if "hello.txt" not in listed:
        raise RuntimeError(f"E2B file list missing hello.txt: {listed}")

    command = e2b.commands.run("pwd && printf '\\ncommand-ok'")
    if command.exit_code != 0:
        raise RuntimeError(
            f"E2B command failed: exit={command.exit_code} stdout={command.stdout!r} stderr={command.stderr!r}"
        )
    if "command-ok" not in command.stdout:
        raise RuntimeError(f"E2B command stdout missing marker: {command.stdout!r}")

    result = e2b.run_code(
        "import os\nprint('code-ok')\nprint(os.getcwd())",
        language="python",
    )
    result_text = logs_stdout_text(result)
    if "code-ok" not in result_text:
        raise RuntimeError(f"code interpreter stdout missing marker: {result_text!r}")

    e2b.run_code("stateful_value = 41", language="python")
    stateful = e2b.run_code("print(stateful_value + 1)", language="python")
    stateful_text = logs_stdout_text(stateful)
    if "42" not in stateful_text:
        raise RuntimeError(f"code interpreter did not preserve state: {stateful_text!r}")

    e2b.files.write(f"{base}/shared.txt", "shared-through-envd")
    shared = e2b.run_code(f"print(open('{base}/shared.txt').read())", language="python")
    shared_text = logs_stdout_text(shared)
    if "shared-through-envd" not in shared_text:
        raise RuntimeError(f"code interpreter cannot read envd-written file: {shared_text!r}")

    validate_network_policy(conch_sandbox, e2b)

    log("conch e2b workload smoke ok")


if __name__ == "__main__":
    main()
