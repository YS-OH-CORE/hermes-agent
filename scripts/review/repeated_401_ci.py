"""Record the original and candidate #125991 behavior using the canonical runner.

Verification support by Zero × Youngseok Oh (Zero/ChatGPT assisted).
The original lane must reproduce the specific second-401 assertion failure.
Its test exit remains 1 in the receipt; a successful oracle is not a passing test.
"""
from __future__ import annotations

import argparse
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
import traceback
import xml.etree.ElementTree as ET


REFS = {
    "original": "ce962e05ad9acbfa75f506da7b635236a96d733a",
    "candidate": "861f94dc4326b9de45fff009202b48415aadb9bc",
}
TREES = {
    "original": "07776847a83b231e8d026ff3771a94ed4cfb9012",
    "candidate": "41abf3e73863f0a16fbde1b2704cca10e4634b34",
}
TEST = "tests/e2e/core/mcp_plugins/test_mcp_streamable_http.py"
TEST_SHA256 = "20a819856089fc837d71eb1da14809caa6a98b00e0bfb52e6738a8fd4c919ce2"
AUTH_TEST = "test_401_on_tools_call_is_reported_as_an_auth_failure"
NEXT_TEST = "test_the_call_after_a_401_reaches_the_server"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def check_snapshot(lane: str) -> None:
    require(git("rev-parse", "HEAD") == REFS[lane], "unexpected runtime commit")
    require(git("rev-parse", "HEAD^{tree}") == TREES[lane], "unexpected runtime tree")
    require(digest(Path(TEST)) == TEST_SHA256, "frozen regression test changed")
    expected = TEST if lane == "original" else ""
    require(git("diff", "--name-only") == expected, "unexpected tracked source changes")
    require(not git("diff", "--cached", "--name-only"), "unexpected staged changes")
    require(not git("ls-files", "--deleted"), "checkout is missing tracked files")


def prepare(args: argparse.Namespace) -> None:
    args.evidence.mkdir(parents=True, exist_ok=False)
    require(git("rev-parse", "HEAD") == REFS[args.lane], "unexpected runtime commit")
    require(not git("status", "--porcelain", "--untracked-files=no"), "checkout is not clean")
    require(digest(args.frozen_test) == TEST_SHA256, "unexpected frozen test input")
    original_test_hash = digest(Path(TEST))
    shutil.copyfile(args.frozen_test, TEST)
    check_snapshot(args.lane)
    patch = subprocess.check_output(["git", "diff", "--binary"])
    (args.evidence / "working-tree.patch").write_bytes(patch)
    inputs = ["pyproject.toml", "uv.lock", "pm/lock.json", "scripts/run_tests.sh", TEST,
              "tools/mcp_tool_handlers.py", ".github/actions/setup-pm/action.yml"]
    write_json(args.evidence / "inputs.json", {
        "lane": args.lane,
        "runtime_commit": REFS[args.lane],
        "runtime_tree": TREES[args.lane],
        "checkout": "ordinary full checkout; no sparse checkout or omitted tracked paths",
        "tracked_file_count": len(git("ls-files").splitlines()),
        "original_test_sha256": original_test_hash,
        "frozen_test_sha256": TEST_SHA256,
        "working_tree_patch_sha256": hashlib.sha256(patch).hexdigest(),
        "files": {p: digest(Path(p)) for p in inputs},
        "workflow_commit": os.environ.get("GITHUB_SHA"),
        "run_id": os.environ.get("GITHUB_RUN_ID"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "runner_os": os.environ.get("RUNNER_OS"),
        "runner_arch": os.environ.get("RUNNER_ARCH"),
        "runner_image": os.environ.get("ImageVersion"),
        "preparation_unix_time": time.time(),
        "dependency_build": "pm.build_env, frozen committed inputs, dev+test groups, new output",
        "restored_tool_or_dependency_caches": False,
    })


def run_gate(args: argparse.Namespace, name: str, file: str, count: int,
             selection: str | None = None, expected_red: bool = False) -> dict:
    check_snapshot(args.lane)
    dest = args.evidence / name
    dest.mkdir()
    base = Path(os.environ["RUNNER_TEMP"]) / f"repeated-401-{args.lane}-{name}"
    command = ["bash", "scripts/run_tests.sh", "--jobs", "1", "--file-retries", "0",
               "--file-timeout", "900", "--include-integration", file]
    if selection:
        command += ["-k", selection]
    command += ["--", f"--basetemp={base}", f"--junitxml={dest / 'junit.xml'}"]
    receipt = {"command": command, "cwd": str(Path.cwd()), "started_unix_time": time.time(),
               "expected": "specific second-401 assertion failure" if expected_red else f"{count} passed",
               "file_retries": 0, "workers": 1}
    write_json(dest / "execution.json", receipt)
    print("RUN", json.dumps(command), flush=True)
    with (dest / "runner.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", env={**os.environ,
                                   "HERMES_PYTHON": sys.executable,
                                   "HERMES_TEST_WORKERS": "1", "HERMES_TEST_FILE_RETRIES": "0"})
        for line in process.stdout:
            log.write(line)
            print(line, end="", flush=True)
        receipt["exit_code"] = process.wait()
    receipt["finished_unix_time"] = time.time()
    write_json(dest / "execution.json", receipt)
    # The fixture's synthetic HTTP request log is useful corroboration. Do not
    # upload the whole temporary HOME, credentials/config, state DB, or environment.
    if name == "e2e":
        source = base / "unauth0" / "http_inbound.jsonl"
        if source.is_file() and not source.is_symlink():
            shutil.copyfile(source, dest / "http_inbound.jsonl")
        receipt["http_inbound_log_copied"] = (dest / "http_inbound.jsonl").is_file()
        write_json(dest / "execution.json", receipt)
    require((dest / "junit.xml").is_file(), f"{name}: missing JUnit report")
    cases = ET.parse(dest / "junit.xml").getroot().findall(".//testcase")
    observed = []
    for case in cases:
        state = next((tag for tag in ("error", "failure", "skipped") if case.find(tag) is not None), "passed")
        observed.append({"name": case.get("name"), "class": case.get("classname"), "state": state})
    result = {"gate": name, "exit_code": receipt["exit_code"], "cases": observed,
              "passed": sum(c["state"] == "passed" for c in observed),
              "failed": sum(c["state"] == "failure" for c in observed),
              "errors": sum(c["state"] == "error" for c in observed),
              "skipped": sum(c["state"] == "skipped" for c in observed)}
    write_json(dest / "observed.json", result)
    require(len(cases) == count, f"{name}: unexpected test count")
    require(result["errors"] == result["skipped"] == 0, f"{name}: preparation error or skipped test")
    if name == "e2e":
        require({c.get("name") for c in cases} == {AUTH_TEST, NEXT_TEST}, "unexpected E2E selection")
    if expected_red:
        require(result["exit_code"] == 1 and result["failed"] == result["passed"] == 1,
                "original lane did not reproduce the expected red/green pair")
        failure = next(c for c in cases if c.find("failure") is not None)
        require(failure.get("name") == AUTH_TEST, "a different original test failed")
        detail = "".join(failure.find("failure").itertext())
        require("needs_reauth" in detail and "At index 1 diff: (None, None) != (True, 'web')" in detail,
                "original failure was not the second response's lost auth classification")
        result["interpretation"] = "expected regression reproduced; the original test failed"
    else:
        require(result["exit_code"] == 0 and result["passed"] == count,
                f"{name}: selected gate did not pass")
        result["interpretation"] = "selected tests passed"
    result["oracle_accepted"] = True
    write_json(dest / "observed.json", result)
    print("OBSERVED", json.dumps(result), flush=True)
    return result


def verify(args: argparse.Namespace) -> None:
    expected_env = Path(os.environ["RUNNER_TEMP"]) / "hermes-mcp-test-env"
    require(Path(sys.prefix) == expected_env and sys.prefix != sys.base_prefix,
            "verification is not running in the freshly built PM test environment")
    check_snapshot(args.lane)
    write_json(args.evidence / "environment.json", {
        "python": sys.version, "executable": sys.executable, "prefix": sys.prefix,
        "platform": platform.platform(),
        "installed": dict(sorted((d.metadata["Name"], d.version)
                                  for d in importlib.metadata.distributions() if d.metadata["Name"])),
    })
    gates = [run_gate(args, "e2e", TEST, 2, f"{AUTH_TEST} or {NEXT_TEST}", args.lane == "original")]
    if args.lane == "candidate":
        for name, file, count, selection in [
            ("errors", "tests/tools/test_mcp_tool_errors.py", 9, None),
            ("handling", "tests/tools/test_mcp_tool_401_handling.py", 3, None),
            ("recorder", "tests/tools/test_mcp_sse_fallback.py", 1,
             "test_opaque_sdk_rejection_is_reported_with_the_servers_status_and_body"),
        ]:
            gates.append(run_gate(args, name, file, count, selection))
    check_snapshot(args.lane)
    summary = {"lane": args.lane, "runtime_commit": REFS[args.lane],
               "frozen_test_sha256": TEST_SHA256, "gates": gates,
               "all_oracles_accepted": True, "test_passes": sum(g["passed"] for g in gates),
               "test_failures": sum(g["failed"] for g in gates), "file_retries": 0}
    write_json(args.evidence / "result.json", summary)
    print("RESULT", json.dumps(summary), flush=True)
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as out:
        out.write(f"### {args.lane}: `{REFS[args.lane]}`\n\n")
        out.write(f"Observed **{summary['test_passes']} passed, {summary['test_failures']} failed**. ")
        out.write("The original lane accepts only the specific second-401 regression.\n\n"
                  if args.lane == "original" else "All 15 selected candidate tests passed.\n\n")
        out.write("One worker, zero file retries, fresh PM dev+test environment, same frozen E2E test.\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "verify"))
    parser.add_argument("lane", choices=REFS)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--frozen-test", type=Path)
    args = parser.parse_args()
    try:
        (prepare if args.mode == "prepare" else verify)(args)
    except Exception:
        args.evidence.mkdir(parents=True, exist_ok=True)
        (args.evidence / f"{args.mode}-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
