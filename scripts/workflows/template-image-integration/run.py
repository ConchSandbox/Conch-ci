#!/usr/bin/env python3
"""Black-box integration coverage for Conch Image and Template lifecycles."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from typing import Any
from urllib.parse import quote


OCI_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
TEMPLATE_RECORD_PREFIX = "io.conch.template/"
TEMPORARY_TEMPLATE_FETCH_PREFIX = "localhost/conch/template-fetch:"
DIGEST_TEMPLATE_RECORD_PREFIX = "localhost/conch/template:sha256-"
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class IntegrationError(RuntimeError):
    """Raised when a black-box assertion fails."""


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float = 30.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self.socket_path))
        self.sock = connection


class Suite:
    def __init__(
        self,
        conch_source: Path,
        config: Path,
        unix_socket: Path,
        rootfs_image_ref: str,
        template_image_ref: str,
        artifact_dir: Path,
    ) -> None:
        self.conch_source = conch_source
        self.conch = conch_source / "bin" / "conch"
        self.config = config
        self.unix_socket = unix_socket
        self.rootfs_image_ref = rootfs_image_ref
        self.template_image_ref = template_image_ref
        self.artifact_dir = artifact_dir
        self.transcript = artifact_dir / "commands.log"
        self.rootfs_repository, self.rootfs_digest = parse_digest_reference(
            rootfs_image_ref
        )
        self.template_repository, self.template_digest = parse_digest_reference(
            template_image_ref
        )

    def log(self, message: str) -> None:
        line = f"[template-image-integration] {message}"
        print(line, flush=True)
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")

    def run_cli(
        self,
        *arguments: str,
        expect_success: bool = True,
        expected_error_code: str | None = None,
        timeout: int = 1800,
    ) -> str:
        command = [str(self.conch), *arguments]
        self.log("$ " + " ".join(command))
        environment = os.environ.copy()
        environment.update(
            {
                "NO_PROXY": "localhost,127.0.0.1",
                "no_proxy": "localhost,127.0.0.1",
            }
        )
        completed = subprocess.run(
            command,
            cwd=self.conch_source,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        combined = completed.stdout + completed.stderr
        with self.transcript.open("a", encoding="utf-8") as stream:
            stream.write(combined)
            if combined and not combined.endswith("\n"):
                stream.write("\n")
            stream.write(f"[exit {completed.returncode}]\n")
        if combined:
            print(combined, end="" if combined.endswith("\n") else "\n", flush=True)
        if expect_success and completed.returncode != 0:
            raise IntegrationError(
                f"command failed with exit {completed.returncode}: {command!r}"
            )
        if not expect_success and completed.returncode == 0:
            raise IntegrationError(f"command unexpectedly succeeded: {command!r}")
        if expected_error_code and expected_error_code not in combined:
            raise IntegrationError(
                f"command error did not contain {expected_error_code!r}: {combined!r}"
            )
        return completed.stdout

    def api_post(
        self,
        path: str,
        payload: dict[str, Any],
        expected_status: int = 200,
    ) -> dict[str, Any]:
        connection = UnixHTTPConnection(self.unix_socket)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            raw = response.read()
        finally:
            connection.close()
        try:
            decoded = json.loads(raw) if raw else {}
        except json.JSONDecodeError as error:
            raise IntegrationError(
                f"{path} returned invalid JSON with status {response.status}: {raw!r}"
            ) from error
        if response.status != expected_status:
            raise IntegrationError(
                f"{path} returned status {response.status}, want {expected_status}: {decoded!r}"
            )
        if not isinstance(decoded, dict):
            raise IntegrationError(f"{path} returned non-object JSON: {decoded!r}")
        return decoded

    def image_records(self) -> list[dict[str, Any]]:
        response = self.api_post("/api/image/list", {})
        records = response.get("images")
        if not isinstance(records, list):
            raise IntegrationError(f"invalid image list response: {response!r}")
        return records

    def template_records(self) -> list[dict[str, Any]]:
        response = self.api_post("/api/template/list", {})
        records = response.get("items")
        if not isinstance(records, list):
            raise IntegrationError(f"invalid template list response: {response!r}")
        return records

    def image_record(self, name: str) -> dict[str, Any]:
        matches = [item for item in self.image_records() if item.get("name") == name]
        if len(matches) != 1:
            raise IntegrationError(
                f"expected one image record named {name!r}, got {matches!r}"
            )
        return matches[0]

    def assert_image_absent(self, name: str) -> None:
        matches = [item for item in self.image_records() if item.get("name") == name]
        if matches:
            raise IntegrationError(f"image record {name!r} is still present: {matches!r}")

    def inspect_template(self, name: str) -> dict[str, Any]:
        response = self.api_post("/api/template/inspect", {"name": name})
        if response.get("name") != name:
            raise IntegrationError(
                f"template inspect returned the wrong name for {name!r}: {response!r}"
            )
        return response

    def assert_template_absent(self, name: str) -> None:
        response = self.api_post(
            "/api/template/inspect", {"name": name}, expected_status=404
        )
        if response.get("code") != "template.not_found":
            raise IntegrationError(
                f"missing template {name!r} returned the wrong error: {response!r}"
            )

    def snapshot(self, label: str) -> None:
        payload = {
            "images": self.image_records(),
            "templates": self.template_records(),
        }
        target = self.artifact_dir / f"state-{label}.json"
        target.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def image_pull(self, reference: str, expect_success: bool = True) -> None:
        self.run_cli(
            "image",
            "pull",
            "--config",
            str(self.config),
            "--plain-http",
            reference,
            expect_success=expect_success,
            expected_error_code=None if expect_success else "image.invalid_argument",
        )

    def image_push(self, local_name: str, remote_reference: str) -> None:
        self.run_cli(
            "image",
            "push",
            "--config",
            str(self.config),
            "--plain-http",
            "--timeout",
            "30m",
            local_name,
            remote_reference,
        )

    def image_remove(self, name: str, expect_success: bool = True) -> None:
        self.run_cli(
            "image",
            "rm",
            "--config",
            str(self.config),
            name,
            expect_success=expect_success,
            expected_error_code=None if expect_success else "image.invalid_argument",
        )

    def image_list(self, show_all: bool = False) -> str:
        arguments = ["image", "ls", "--config", str(self.config)]
        if show_all:
            arguments.append("--all")
        return self.run_cli(*arguments)

    def template_pull(
        self,
        reference: str,
        expect_success: bool = True,
    ) -> tuple[str, str] | None:
        output = self.run_cli(
            "template",
            "pull",
            "--config",
            str(self.config),
            "--plain-http",
            reference,
            expect_success=expect_success,
            expected_error_code=None
            if expect_success
            else "template.invalid_artifact",
        )
        if not expect_success:
            return None
        name_match = re.search(r"(?m)^Template Name: (.+)$", output)
        id_match = re.search(r"(?m)^Template ID: (sha256:[0-9a-f]{64})$", output)
        if name_match is None or id_match is None:
            raise IntegrationError(f"invalid template pull output: {output!r}")
        return name_match.group(1), id_match.group(1)

    def template_push(self, name: str, remote_reference: str) -> None:
        self.run_cli(
            "template",
            "push",
            "--config",
            str(self.config),
            "--plain-http",
            "--timeout",
            "30m",
            name,
            remote_reference,
        )

    def template_unpack(self, name: str) -> None:
        self.run_cli(
            "template",
            "unpack",
            "--config",
            str(self.config),
            name,
        )

    def template_remove(self, name: str) -> None:
        self.run_cli(
            "template",
            "rm",
            "--config",
            str(self.config),
            name,
        )

    def mutate_remote_manifest(self, reference: str, label: str) -> str:
        repository, tag = parse_tag_reference(reference)
        manifest, media_type, current_digest = registry_get_manifest(reference)
        document = json.loads(manifest)
        if not isinstance(document, dict):
            raise IntegrationError(f"registry manifest is not an object: {document!r}")
        annotations = document.setdefault("annotations", {})
        if not isinstance(annotations, dict):
            raise IntegrationError(f"registry manifest annotations are invalid: {document!r}")
        annotations["org.opencontainers.image.version"] = f"conch-ci-{label}-v2"
        mutated = json.dumps(
            document,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        mutated_digest = registry_put_manifest(repository, tag, mutated, media_type)
        if mutated_digest == current_digest:
            raise IntegrationError(f"manifest mutation did not change {reference}")
        (self.artifact_dir / f"manifest-{label}.json").write_bytes(mutated)
        self.log(
            f"mutated registry manifest {reference}: {current_digest} -> {mutated_digest}"
        )
        return mutated_digest

    def assert_remote_digest(self, reference: str, expected: str) -> None:
        _, _, actual = registry_get_manifest(reference)
        if actual != expected:
            raise IntegrationError(
                f"remote digest for {reference} is {actual}, want {expected}"
            )

    def assert_record_hygiene(self) -> None:
        names = [str(item.get("name", "")) for item in self.image_records()]
        temporary = [
            name for name in names if name.startswith(TEMPORARY_TEMPLATE_FETCH_PREFIX)
        ]
        canonical = [
            name for name in names if name.startswith(DIGEST_TEMPLATE_RECORD_PREFIX)
        ]
        if temporary:
            raise IntegrationError(
                f"temporary Template fetch records were not removed: {temporary!r}"
            )
        if canonical:
            raise IntegrationError(
                f"digest-derived Template records unexpectedly exist: {canonical!r}"
            )

    def run(self) -> None:
        self.log("pulling immutable Image and Template fixtures")
        self.image_pull(self.rootfs_image_ref)
        pulled = self.template_pull(self.template_image_ref)
        if pulled != (self.template_image_ref, self.template_digest):
            raise IntegrationError(
                f"immutable Template pull returned {pulled!r}, want "
                f"{(self.template_image_ref, self.template_digest)!r}"
            )
        self.assert_record_hygiene()
        self.snapshot("fixtures")

        self.validate_image_retarget()
        self.validate_template_retarget()
        self.validate_multiple_template_names()
        self.validate_image_template_name_collision()

        self.assert_record_hygiene()
        self.snapshot("final")
        self.log("Template/Image integration suite passed")

    def validate_image_retarget(self) -> None:
        self.log("validating mutable Image tag retargeting")
        moving = make_tag_reference(self.rootfs_repository, "ci-image-moving")
        exported = make_tag_reference(self.rootfs_repository, "ci-image-export")
        self.image_push(self.rootfs_image_ref, moving)
        self.image_pull(moving)
        first = self.image_record(moving)
        if first.get("target_digest") != self.rootfs_digest:
            raise IntegrationError(f"initial moving Image record is wrong: {first!r}")
        created_at = first.get("created_at")

        second_digest = self.mutate_remote_manifest(moving, "image")
        self.image_pull(moving)
        second = self.image_record(moving)
        if second.get("target_digest") != second_digest:
            raise IntegrationError(f"retargeted Image record is wrong: {second!r}")
        if second.get("created_at") != created_at:
            raise IntegrationError(
                f"Image record creation time changed during retarget: {first!r} -> {second!r}"
            )
        if len([item for item in self.image_records() if item.get("name") == moving]) != 1:
            raise IntegrationError(f"Image retarget created duplicate records for {moving}")
        self.image_push(moving, exported)
        self.assert_remote_digest(exported, second_digest)
        self.snapshot("image-retarget")

    def validate_template_retarget(self) -> None:
        self.log("validating mutable Template Name retargeting and no-clobber")
        moving = make_tag_reference(self.template_repository, "ci-template-moving")
        exported = make_tag_reference(self.template_repository, "ci-template-export")
        self.template_push(self.template_image_ref, moving)
        first_pull = self.template_pull(moving)
        if first_pull != (moving, self.template_digest):
            raise IntegrationError(f"initial moving Template pull is wrong: {first_pull!r}")
        first = self.inspect_template(moving)
        created_at = first.get("created_at")

        second_digest = self.mutate_remote_manifest(moving, "template")
        second_pull = self.template_pull(moving)
        if second_pull != (moving, second_digest):
            raise IntegrationError(f"retargeted Template pull is wrong: {second_pull!r}")
        second = self.inspect_template(moving)
        if second.get("template_id") != second_digest:
            raise IntegrationError(f"retargeted Template record is wrong: {second!r}")
        if second.get("created_at") != created_at:
            raise IntegrationError(
                f"Template record creation time changed during retarget: {first!r} -> {second!r}"
            )
        if len([item for item in self.template_records() if item.get("name") == moving]) != 1:
            raise IntegrationError(f"Template retarget created duplicate records for {moving}")

        self.template_unpack(moving)
        self.template_unpack(moving)

        # Replace the remote tag with an ordinary OCI image. A rejected pull
        # must leave the already-installed Template target unchanged.
        self.image_push(self.rootfs_image_ref, moving)
        self.template_pull(moving, expect_success=False)
        after_rejection = self.inspect_template(moving)
        if after_rejection.get("template_id") != second_digest:
            raise IntegrationError(
                f"rejected OCI pull clobbered Template {moving}: {after_rejection!r}"
            )
        self.template_push(moving, exported)
        self.assert_remote_digest(exported, second_digest)
        self.assert_record_hygiene()
        self.snapshot("template-retarget")

    def validate_multiple_template_names(self) -> None:
        self.log("validating multiple Template Names for one immutable ID")
        first_name = make_tag_reference(self.template_repository, "ci-template-alias-one")
        second_name = make_tag_reference(self.template_repository, "ci-template-alias-two")
        for name in (first_name, second_name):
            self.template_push(self.template_image_ref, name)
            pulled = self.template_pull(name)
            if pulled != (name, self.template_digest):
                raise IntegrationError(f"Template alias pull is wrong: {pulled!r}")
        self.template_remove(first_name)
        self.template_remove(first_name)
        self.assert_template_absent(first_name)
        if self.inspect_template(second_name).get("template_id") != self.template_digest:
            raise IntegrationError(f"removing {first_name} damaged {second_name}")
        self.snapshot("template-aliases")

    def validate_image_template_name_collision(self) -> None:
        self.log("validating Image/Template isolation for the same logical name")
        collision = make_tag_reference(self.template_repository, "ci-name-collision")
        image_export = make_tag_reference(
            self.template_repository, "ci-name-collision-image-export"
        )
        template_export = make_tag_reference(
            self.template_repository, "ci-name-collision-template-export"
        )
        internal_name = TEMPLATE_RECORD_PREFIX + collision

        self.image_push(self.rootfs_image_ref, collision)
        self.image_pull(collision)
        if self.image_record(collision).get("target_digest") != self.rootfs_digest:
            raise IntegrationError(f"ordinary collision Image has the wrong target")

        self.template_push(self.template_image_ref, collision)
        pulled = self.template_pull(collision)
        if pulled != (collision, self.template_digest):
            raise IntegrationError(f"collision Template pull is wrong: {pulled!r}")
        if self.image_record(internal_name).get("target_digest") != self.template_digest:
            raise IntegrationError(f"internal collision Template image record is wrong")

        # The remote tag now holds a Boot Index. The rejected Image pull must
        # not overwrite the ordinary local Image record of the same name.
        self.image_pull(collision, expect_success=False)
        if self.image_record(collision).get("target_digest") != self.rootfs_digest:
            raise IntegrationError(f"rejected Boot Index pull clobbered {collision}")

        self.template_pull(self.rootfs_image_ref, expect_success=False)
        self.assert_image_absent(TEMPLATE_RECORD_PREFIX + self.rootfs_image_ref)

        self.image_push(collision, image_export)
        self.assert_remote_digest(image_export, self.rootfs_digest)
        self.template_push(collision, template_export)
        self.assert_remote_digest(template_export, self.template_digest)

        visible = self.image_list(show_all=False)
        all_records = self.image_list(show_all=True)
        if collision not in visible:
            raise IntegrationError(f"ordinary Image is hidden from image ls: {visible!r}")
        if TEMPLATE_RECORD_PREFIX in visible:
            raise IntegrationError(
                f"internal Template records leaked into default image ls: {visible!r}"
            )
        if internal_name not in all_records:
            raise IntegrationError(
                f"internal Template record is missing from image ls --all: {all_records!r}"
            )

        self.image_remove(internal_name, expect_success=False)
        self.run_cli(
            "image",
            "push",
            "--config",
            str(self.config),
            "--plain-http",
            internal_name,
            make_tag_reference(self.template_repository, "ci-reserved-push"),
            expect_success=False,
            expected_error_code="image.invalid_argument",
        )
        self.image_pull(internal_name, expect_success=False)

        self.template_remove(collision)
        self.assert_template_absent(collision)
        if self.image_record(collision).get("target_digest") != self.rootfs_digest:
            raise IntegrationError(f"Template rm damaged ordinary Image {collision}")

        self.template_pull(collision)
        self.image_remove(collision)
        self.assert_image_absent(collision)
        if self.inspect_template(collision).get("template_id") != self.template_digest:
            raise IntegrationError(f"Image rm damaged Template {collision}")
        self.template_remove(collision)
        self.template_remove(collision)
        self.assert_template_absent(collision)
        self.assert_record_hygiene()
        self.snapshot("name-collision")


def parse_digest_reference(reference: str) -> tuple[str, str]:
    if not reference.startswith("localhost:5000/") or "@" not in reference:
        raise IntegrationError(f"expected a localhost digest reference: {reference!r}")
    repository, digest = reference.rsplit("@", 1)
    if not DIGEST_PATTERN.fullmatch(digest):
        raise IntegrationError(f"invalid digest reference: {reference!r}")
    return repository, digest


def parse_tag_reference(reference: str) -> tuple[str, str]:
    if not reference.startswith("localhost:5000/"):
        raise IntegrationError(f"expected a localhost tag reference: {reference!r}")
    slash = reference.rfind("/")
    colon = reference.rfind(":")
    if colon <= slash:
        raise IntegrationError(f"reference has no tag: {reference!r}")
    repository = reference[:colon]
    tag = reference[colon + 1 :]
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise IntegrationError(f"invalid registry tag: {tag!r}")
    return repository, tag


def make_tag_reference(repository: str, tag: str) -> str:
    reference = f"{repository}:{tag}"
    parse_tag_reference(reference)
    return reference


def registry_path(repository: str, object_name: str) -> str:
    prefix = "localhost:5000/"
    if not repository.startswith(prefix):
        raise IntegrationError(f"unsupported registry repository: {repository!r}")
    repository_path = repository[len(prefix) :]
    return f"/v2/{quote(repository_path, safe='/')}/manifests/{quote(object_name, safe=':@')}"


def registry_get_manifest(reference: str) -> tuple[bytes, str, str]:
    if "@" in reference:
        repository, object_name = parse_digest_reference(reference)
    else:
        repository, object_name = parse_tag_reference(reference)
    connection = http.client.HTTPConnection("localhost", 5000, timeout=30)
    try:
        connection.request(
            "GET",
            registry_path(repository, object_name),
            headers={"Accept": OCI_ACCEPT},
        )
        response = connection.getresponse()
        body = response.read()
        media_type = response.getheader("Content-Type", "").split(";", 1)[0]
        digest = response.getheader("Docker-Content-Digest", "")
    finally:
        connection.close()
    if response.status != 200:
        raise IntegrationError(
            f"registry GET {reference} returned {response.status}: {body!r}"
        )
    calculated = "sha256:" + hashlib.sha256(body).hexdigest()
    if digest != calculated:
        raise IntegrationError(
            f"registry digest mismatch for {reference}: header={digest!r} calculated={calculated!r}"
        )
    if not media_type:
        raise IntegrationError(f"registry omitted media type for {reference}")
    return body, media_type, digest


def registry_put_manifest(
    repository: str,
    tag: str,
    manifest: bytes,
    media_type: str,
) -> str:
    connection = http.client.HTTPConnection("localhost", 5000, timeout=30)
    try:
        connection.request(
            "PUT",
            registry_path(repository, tag),
            body=manifest,
            headers={
                "Content-Type": media_type,
                "Content-Length": str(len(manifest)),
            },
        )
        response = connection.getresponse()
        body = response.read()
        digest = response.getheader("Docker-Content-Digest", "")
    finally:
        connection.close()
    if response.status != 201:
        raise IntegrationError(
            f"registry PUT {repository}:{tag} returned {response.status}: {body!r}"
        )
    calculated = "sha256:" + hashlib.sha256(manifest).hexdigest()
    if digest != calculated:
        raise IntegrationError(
            f"registry PUT digest mismatch: header={digest!r} calculated={calculated!r}"
        )
    return digest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--conch-source", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--unix-socket", type=Path, required=True)
    parser.add_argument("--rootfs-image-ref", required=True)
    parser.add_argument("--template-image-ref", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    conch_source = arguments.conch_source.resolve()
    config = arguments.config.resolve()
    unix_socket = arguments.unix_socket.resolve()
    artifact_dir = arguments.artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    if not (conch_source / "bin" / "conch").is_file():
        raise IntegrationError(f"Conch CLI is missing below {conch_source}")
    if not config.is_file():
        raise IntegrationError(f"Conch config is missing: {config}")
    if not unix_socket.exists():
        raise IntegrationError(f"Conch API socket is missing: {unix_socket}")

    suite = Suite(
        conch_source=conch_source,
        config=config,
        unix_socket=unix_socket,
        rootfs_image_ref=arguments.rootfs_image_ref,
        template_image_ref=arguments.template_image_ref,
        artifact_dir=artifact_dir,
    )
    try:
        suite.run()
    except Exception:
        try:
            suite.snapshot("failure")
        except Exception as snapshot_error:
            print(
                f"warning: could not capture failure state: {snapshot_error}",
                file=sys.stderr,
            )
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
