# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is dual-licensed under either the MIT license found in the
# LICENSE-MIT file in the root directory of this source tree or the Apache
# License, Version 2.0 found in the LICENSE-APACHE file in the root directory
# of this source tree. You may select, at your option, one of the
# above-listed licenses.

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

NATIVELINK_VERSION = "1.6.4"
NATIVELINK_ARCHIVE = "nativelink-1.6.4-x86_64-unknown-linux-musl.tar.gz"
NATIVELINK_SHA256 = "81f0140f7d2f167c875e2ef1ce7825d92ac86c09b355441a9772b577a0c3f3bd"


class NativeLink:
    def __init__(self, binary: Path, config: Path, cache: Path, log: Path, port: int):
        self._log = log.open("w")
        self._process = subprocess.Popen(
            [str(binary), str(config)],
            env={**os.environ, "NATIVELINK_CACHE_ROOT": str(cache)},
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self.stop()
                pytest.fail(
                    f"NativeLink exited before becoming ready:\n{log.read_text()}"
                )
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return
            except OSError:
                time.sleep(0.1)
        self.stop()
        pytest.fail(f"NativeLink did not become ready:\n{log.read_text()}")

    def stop(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        if not self._log.closed:
            self._log.close()


class Buck:
    def __init__(self, binary: Path, workspace: Path, isolation_dir: str):
        self._binary = binary
        self._workspace = workspace
        self._isolation_dir = isolation_dir
        self._env = {
            **os.environ,
            "BUCK2_HARD_ERROR": "false",
            "BUCK2_TEST_BLOCK_ON_UPLOAD": "true",
            "BUCK2_TEST_DISABLE_DAEMON_CGROUP": "true",
            "BUCK2_TEST_DISABLE_LOG_UPLOAD": "true",
            "NANO_PRELUDE": str(
                Path(__file__).parents[3] / "tests/e2e_util/nano_prelude"
            ),
        }

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(self._binary), f"--isolation-dir={self._isolation_dir}", *args],
            cwd=self._workspace,
            env=self._env,
            text=True,
            capture_output=True,
            check=False,
        )
        if check and result.returncode != 0:
            pytest.fail(
                f"buck2 {' '.join(args)} failed with {result.returncode}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result

    def test(self, target: str, *extra: str, check: bool = True):
        return self.run(
            "test",
            "-c",
            "test.allow_cache_uploads=true",
            "-c",
            "test.remote_cache_enabled=true",
            "-c",
            f"test.python={sys.executable}",
            *extra,
            target,
            "-v=0",
            check=check,
        )

    def what_ran(self) -> list[dict[str, Any]]:
        output = self.run(
            "log", "what-ran", "--format", "json", "--emit-cache-queries"
        ).stdout
        return [json.loads(line) for line in output.splitlines() if line.strip()]

    def successful_uploads(self) -> list[dict[str, Any]]:
        events = self.run("log", "show").stdout
        uploads = []
        for line in events.splitlines():
            event = json.loads(line)
            upload = (
                event.get("Event", {})
                .get("data", {})
                .get("SpanEnd", {})
                .get("data", {})
                .get("CacheUpload")
            )
            if upload is not None and upload["success"]:
                uploads.append(upload)
        return uploads

    def reset(self) -> None:
        self.run("kill", check=False)
        shutil.rmtree(self._workspace / "buck-out", ignore_errors=True)


@pytest.mark.skipif(
    sys.platform == "win32" or os.environ.get("BUCK2_NATIVE_TEST_CACHE_SMOKE") != "1",
    reason="the cache interoperability smoke is enabled explicitly on Unix",
)
def test_local_native_test_cache_archive_restore(tmp_path: Path) -> None:
    project = tmp_path / "project"
    live_cache = tmp_path / "cache"
    cache_archive = tmp_path / "cache.tar.gz"
    nativelink_config = tmp_path / "nativelink.json5"
    nativelink_log = tmp_path / "nativelink.log"
    shutil.copytree(Path(__file__).with_name("test_native_test_cache_data"), project)
    live_cache.mkdir()

    port = _unused_port()
    buckconfig = project.joinpath(".buckconfig")
    buckconfig.write_text(buckconfig.read_text().replace("{PORT}", str(port)))
    nativelink_config.write_text(_nativelink_config(port))

    buck2 = Path(os.environ["BUCK2"])
    nativelink = _nativelink(tmp_path)
    buck = Buck(buck2, project, f"native-test-cache-{os.getpid()}-{port}")
    server = NativeLink(nativelink, nativelink_config, live_cache, nativelink_log, port)
    try:
        buck.test("//:cacheable")
        first = _test_run(buck.what_ran(), "cacheable")
        assert first["reproducer"]["executor"] == "Local"
        assert buck.successful_uploads()
        assert not _used_remote_execution(buck.what_ran())

        server.stop()
        _archive_cache(live_cache, cache_archive)
        buck.reset()
        shutil.rmtree(live_cache)
        live_cache.mkdir()
        _restore_cache(cache_archive, live_cache)
        server = NativeLink(
            nativelink, nativelink_config, live_cache, nativelink_log, port
        )

        buck.test("//:cacheable")
        restored = _test_run(buck.what_ran(), "cacheable")
        assert restored["reproducer"]["executor"] == "Cache"
        assert not _used_remote_execution(buck.what_ran())

        assert buck.test("//:failing", check=False).returncode != 0
        assert _test_run(buck.what_ran(), "failing")["reproducer"]["executor"] == (
            "Local"
        )
        assert not buck.successful_uploads()
        buck.reset()
        assert buck.test("//:failing", check=False).returncode != 0
        assert _test_run(buck.what_ran(), "failing")["reproducer"]["executor"] == (
            "Local"
        )

        buck.test("//:not_opted_in")
        buck.test("//:not_opted_in")
        not_opted_in = buck.what_ran()
        assert _test_run(not_opted_in, "not_opted_in")["reproducer"]["executor"] == (
            "Local"
        )
        assert not any(entry["reason"] == "cache_query" for entry in not_opted_in)

        buck.test("//:upload_disabled", "-c", "test.allow_cache_uploads=false")
        assert (
            _test_run(buck.what_ran(), "upload_disabled")["reproducer"]["executor"]
            == "Local"
        )
        assert not buck.successful_uploads()
        buck.reset()
        buck.test("//:upload_disabled", "-c", "test.allow_cache_uploads=false")
        assert (
            _test_run(buck.what_ran(), "upload_disabled")["reproducer"]["executor"]
            == "Local"
        )

        buck.test("//:cacheable", "--no-remote-cache")
        assert _test_run(buck.what_ran(), "cacheable")["reproducer"]["executor"] == (
            "Local"
        )

        project.joinpath("resource.txt").write_text("alpha\n")
        buck.test("//:cacheable")
        assert _test_run(buck.what_ran(), "cacheable")["reproducer"]["executor"] == (
            "Cache"
        )

        project.joinpath("resource.txt").write_text("changed\n")
        buck.test("//:cacheable")
        assert _test_run(buck.what_ran(), "cacheable")["reproducer"]["executor"] == (
            "Local"
        )
        assert buck.successful_uploads()

        buck.test("//:generated_cacheable")
        project.joinpath("producer-input.txt").write_text("changed\n")
        buck.test("//:generated_cacheable")
        generated = buck.what_ran()
        assert any(
            entry["reason"] == "build"
            and "generate_test_binary" in entry.get("identity", "")
            and entry["reproducer"]["executor"] == "Local"
            for entry in generated
        )
        assert (
            _test_run(generated, "generated_cacheable")["reproducer"]["executor"]
            == "Cache"
        )
    finally:
        server.stop()
        buck.reset()


def _nativelink(root: Path) -> Path:
    configured = os.environ.get("NATIVELINK_BIN")
    if configured is not None:
        return Path(configured)
    archive = root / NATIVELINK_ARCHIVE
    urllib.request.urlretrieve(
        "https://github.com/TraceMachina/nativelink/releases/download/"
        f"v{NATIVELINK_VERSION}/{NATIVELINK_ARCHIVE}",
        archive,
    )
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    assert actual == NATIVELINK_SHA256
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(root)
    binary = root / "nativelink"
    binary.chmod(0o755)
    return binary


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _nativelink_config(port: int) -> str:
    return f"""{{
  stores: [
    {{
      name: "AC_STORE",
      filesystem: {{
        content_path: "${{NATIVELINK_CACHE_ROOT}}/ac/content",
        temp_path: "${{NATIVELINK_CACHE_ROOT}}/ac/temp",
        eviction_policy: {{ max_bytes: 1000000000 }},
      }},
    }},
    {{
      name: "CAS_STORE",
      filesystem: {{
        content_path: "${{NATIVELINK_CACHE_ROOT}}/cas/content",
        temp_path: "${{NATIVELINK_CACHE_ROOT}}/cas/temp",
        eviction_policy: {{ max_bytes: 4000000000 }},
      }},
    }},
  ],
  servers: [{{
    name: "cache",
    listener: {{ http: {{ socket_address: "127.0.0.1:{port}" }} }},
    services: {{
      cas: [{{ instance_name: "main", cas_store: "CAS_STORE" }}],
      ac: [{{ instance_name: "main", ac_store: "AC_STORE" }}],
      bytestream: [{{ instance_name: "main", cas_store: "CAS_STORE" }}],
    }},
  }}],
}}"""


def _archive_cache(cache: Path, archive: Path) -> None:
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(cache / "ac", "ac")
        tar.add(cache / "cas", "cas")


def _restore_cache(archive: Path, cache: Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(cache)


def _test_run(entries: list[dict[str, Any]], target: str) -> dict[str, Any]:
    matches = [
        entry
        for entry in entries
        if entry["reason"] == "test.run"
        and entry.get("identity") == target
        and entry.get("reproducer", {}).get("executor") != "CacheQuery"
    ]
    assert len(matches) == 1, matches
    return matches[0]


def _used_remote_execution(entries: list[dict[str, Any]]) -> bool:
    return any(entry.get("reproducer", {}).get("executor") == "Re" for entry in entries)
