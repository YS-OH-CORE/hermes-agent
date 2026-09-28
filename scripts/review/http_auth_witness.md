# HTTP response attribution witness for Hermes #114591

This is a test procedure. Consult the workflow's actual exit status, JUnit
report, and JSON observations for execution results; source inspection alone
does not establish a reproduced failure.

## Scope and credit

Implementation under test: teknium1's
[PR #114591](https://github.com/NousResearch/hermes-agent/pull/114591), pinned to
`646031aa6c87f118e8c0a9b8d4feb79b32fba93c`.
The server-wide response-attribution concern was already raised by
[kvnloo](https://github.com/NousResearch/hermes-agent/pull/114591#issuecomment-5851605557).
This supplemental case exercises the optional HTTP GET stream. It does not
claim to reproduce the review's two simultaneous tools/call scenario.

Hermes holds `_rpc_lock` around each ordinary tool RPC, while the SDK's optional
GET stream runs separately. At this PR revision the response hook records any
401 on the owned client, without a request ID or HTTP-method distinction.
The next otherwise generic handler error can consume that server-wide timestamp.

In pinned [MCP SDK 2.0.0](https://github.com/modelcontextprotocol/python-sdk/blob/6f69a3758ebf2ee55ce050f58b470ce11af71133/src/mcp/client/streamable_http.py),
GET failures are caught within the background reader; exhaustion of its retry
budget returns from that reader without closing the POST request path.
This makes a rejected GET followed by an independently rejected tool POST a
candidate for a real-transport attribution regression.

## Cases

The local synthetic server permits initialize, tools/list, and a healthy probe.
It independently controls the status of the optional GET and the rejected probe.

| GET status | Rejected tools/call status | Required tool diagnosis |
|---|---|---|
| 405 | 500 | Generic tool failure; no needs_reauth |
| 401 | 500 | Generic tool failure; no needs_reauth |
| 405 | 401 | Sign-in required; needs_reauth is true |

For each case, the real registered tool is called in the order healthy,
rejected, healthy. The test checks both healthy results, the three wire-level
tool requests, and the absence of another initialize/tools/list after the first
tool request. These checks guard against reconnects or replays explaining the
observed result. They make no claim about TCP socket reuse.

The test waits for a second GET carrying this fixture's MCP session identifier.
The initial connection preflight has no session identifier and is excluded.
Since the SDK retries after processing the first session-bearing response, this
establishes that the first real response hook has already run. The test neither patches
the classifier/transport nor reads or changes the private 401 timestamp.
The production RPC lock remains active. The canonical fixtures provide a fresh
temporary HERMES_HOME and remove credentials.

## Reproduce

Use the original source revision and copy in only
`tests/tools/test_mcp_http_auth_witness.py` from this verification branch.

```bash
uv python install 3.11
uv sync --locked --python 3.11 --extra dev
source .venv/bin/activate
bash scripts/run_tests.sh --jobs 1 --file-retries 0 --file-timeout 180 \
  --include-integration tests/tools/test_mcp_http_auth_witness.py \
  -- -m integration -v --tb=short
```

The explicit marker selection matters: this revision's default pytest options
exclude integration tests. The workflow keeps the full original checkout,
builds a new environment with dependency caching disabled, and copies in only
the test. It preserves the actual nonzero exit status when an assertion fails;
there is no expected-failure wrapper that makes a failed test look green.

The test covers three response combinations on one pinned revision. It does
not validate successful OAuth login/refresh, all transport failures, or a repair.

Zero × Youngseok Oh
