# Vendored from vendor-token-broker (unmodified)

These files are byte-for-byte copies from
[chief-builder/vendor-token-broker](https://github.com/chief-builder/vendor-token-broker)
at commit `20d1426622a0c05478c4331b82ad8952411f9a4a`, under its MIT license
([LICENSE](LICENSE)). Stewardry does not import them at runtime. Offline
contract tests read them:

- `src/mcp_gateway/server.py`: the gateway's `ToolError` messages, which must
  equal Stewardry's error catalog (ADR 0010).
- `src/mcp_gateway/snapshots/atlassian.json` and `src/mcp_gateway/upstreams.json`:
  the tools and arguments Stewardry's Atlassian adapter may send.

[MANIFEST.json](MANIFEST.json) lists each file's SHA-256. The offline tests
check the copies against the manifest. `make vendored` (run in CI) checks
the manifest against the pinned commit through the GitHub API.

**Never edit these files.** To move the pin, copy the files again from the
new commit, update the manifest, and let the contract tests show what
changed.
