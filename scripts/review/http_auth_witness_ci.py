"""Run one unmodified-runtime regression file and retain its actual exit status."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time


HEAD = "646031aa6c87f118e8c0a9b8d4feb79b32fba93c"
TEST = "tests/tools/test_mcp_http_auth_witness.py"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def main():
    output = Path(os.environ["RUNNER_TEMP"]) / "http-auth-witness-receipt"
    output.mkdir()
    frozen = Path(os.environ["RUNNER_TEMP"]) / "http-auth-witness-test.py"
    assert git("rev-parse", "HEAD") == HEAD
    assert git("status", "--porcelain", "--untracked-files=all") == ""
    assert not Path(TEST).exists()
    shutil.copyfile(frozen, TEST)
    basetemp = Path(os.environ["RUNNER_TEMP"]) / "http-auth-witness-pytest"
    command = ["bash", "scripts/run_tests.sh", "--jobs", "1", "--file-retries", "0",
               "--file-timeout", "180", "--include-integration", TEST, "-v", "--tb=short",
               "--", "-m", "integration", f"--basetemp={basetemp}",
               f"--junitxml={output / 'junit.xml'}"]
    source_paths = ["tools/mcp_tool_handlers.py", "tools/mcp_tool_errors.py",
                    "tools/mcp_tool_transport.py", "tools/mcp_tool.py", "uv.lock", "pyproject.toml"]
    inputs = {
        "runtime_head": HEAD, "runtime_tree": git("rev-parse", "HEAD^{tree}"),
        "tracked_file_count": len(git("ls-files").splitlines()),
        "test_path": TEST, "test_sha256": sha256(TEST),
        "source_sha256": {p: sha256(p) for p in source_paths}, "command": command,
        "workflow_sha": os.environ["GITHUB_SHA"], "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "production_changes": git("diff", "--stat"),
    }
    (output / "inputs.json").write_text(json.dumps(inputs, indent=2) + "\n")
    environment = {"python": sys.version, "platform": platform.platform(), "packages": {
        p: importlib.metadata.version(p) for p in ["mcp", "httpx2", "pytest", "pytest-asyncio", "openai"]}}
    (output / "environment.json").write_text(json.dumps(environment, indent=2) + "\n")
    started = time.monotonic()
    timed_out = False
    try:
        run = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, timeout=300)
        log, exit_code = run.stdout, run.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        log = exc.stdout or ""
        if isinstance(log, bytes):
            log = log.decode("utf-8", errors="replace")
        exit_code = None
    (output / "runner.log").write_text(log)
    print(log)
    observations = []
    for path in sorted(basetemp.rglob("observation.json")):
        if path.parent.is_symlink():
            continue
        value = json.loads(path.read_text())
        target = f"observation-get-{value['get_status']}-call-{value['call_status']}.json"
        if target in observations:
            continue
        shutil.copyfile(path, output / target)
        observations.append(target)
    result = {"canonical_exit_code": exit_code, "outer_timeout": timed_out,
              "elapsed_seconds": time.monotonic() - started,
              "observations": observations, "test_sha256_after": sha256(TEST),
              "production_changes_after": git("diff", "--stat")}
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 124 if timed_out else exit_code


if __name__ == "__main__":
    raise SystemExit(main())
