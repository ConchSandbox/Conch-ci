import os
import sys
import time
import urllib.request
from urllib.parse import urlparse

import grpc
import requests

sys.path.insert(0, os.getcwd())

from api.py_proto import agent_pb2
from conch import Sandbox as ConchSandbox
from e2b.connection_config import ConnectionConfig
from e2b_code_interpreter import Sandbox as CodeInterpreterSandbox
from packaging.version import Version


def log(message):
    print(f"[e2b-sdk-e2e] {message}", flush=True)


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
            response = sandbox.client.stub.HealthCheck(agent_pb2.Empty(), timeout=5)
            last_health = {"status": "OK", "message": response.message}
            return last_health
        except grpc.RpcError as exc:
            last_health = {
                "status": "ERROR",
                "code": str(exc.code()),
                "message": exc.details(),
            }
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


def main():
    log("creating Conch sandbox")
    conch_sandbox = ConchSandbox.create(
        config_path=os.environ["CONCH_SDK_CONFIG"],
        image_name=os.environ["CONCH_E2B_BOOT_IMAGE"],
        namespace=os.environ["CONCH_NAMESPACE"],
        sandbox_id=os.environ["CONCH_SANDBOX_ID"],
        vcpu_num=2,
        ram_mb=2048,
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

    log("conch e2b sdk e2e ok")


if __name__ == "__main__":
    main()
