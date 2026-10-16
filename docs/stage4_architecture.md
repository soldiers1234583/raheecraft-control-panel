# RaheeCraft Stage 4 — Control Plane Extension
## Files, Plugins, Versions, Worlds, Backups

This directory holds the **shared Stage 4 objects** that the backend and the
Kaggle-side agent both import:

- `operation_registry.py` — the single source of truth for the versioned
  operation schema, the fixed allowlist, the patience/size limits, and the
  redaction rules.
- `paths.py` — server-side sandbox helpers used by backend endpoints for
  defense-in-depth validation of any path the browser supplies.
- `file_ops.py` — backend-side, read-only helpers that describe the server
  tree from `SERVER_DIR` and compute storage/usage metadata without trusting
  user-supplied paths.

The heavy file operations (browse, read, write, upload, download, mkdir,
rename, delete, editor) run **only on the agent**. The backend validates
authorization, rate, size, type, and path shape, then queues a typed operation
for the agent. The agent re-validates everything against the real filesystem
and reports a terminal result the same way power actions do.

### Why this layout

Stage 4 adds many new operation types. Putting the schema, limits, and
allowlist in one file (`operation_registry`) keeps the backend and the agent
in sync and makes the audit/redaction story consistent. Splitting the
read-only backend helpers into `file_ops.py` keeps `app.py` focused on routing
and auth.

### Security model (summary)

- Browser may suggest a path; it is never trusted.
- Backend validates the path shape against `paths.SafeServerPath` before
  queueing.
- Agent canonicalizes, sandbox-validates, and symlink-rejects at execution
  time, immediately before each filesystem call.
- Symlinks are rejected by default; the only supported "link-like" feature is
  the editor's recoverable backup, stored inside the server dir as a normal
  timestamped file.
- Editable types are allowlisted; unknown/binary files are downloads only.
- Uploads and editor saves are atomic (write-then-rename) into the target
  location, with bounded temp space.
- Every mutation is audited with actor + operation_id + target + outcome.

### Test approach

- `backend/tests/test_stage4_files.py` exercises schema, limits, redaction,
  sandbox path rules, registry allowlist, and backend endpoints against
  `test_fixtures/s4build/minecraft_server` (a throwaway in-repo tree).
- Agent-side safety (symlink rejection, traversal rejection, atomic writes,
  editor encoding boundaries, upload size bounds) is verified by importing the
  agent's `file_ops` implementation in a separate agent test file and running
  it against the same fixture tree.
- No test mutates the real server directory, real world data, or real Drive
  backups. Real integration requires a live Kaggle agent + a real (or Render)
  backend.

### Environment / notebook changes

- New env vars are documented in `docs/stage4_env.md`. None are required to run
  the existing Stage 1-3 feature set.

### Notebook flow (actual, not aspirational)

The Kaggle notebook now runs in this order. Cell numbers are the real ones in
`minecraft_kaggle_setup.ipynb`; do not rely on older numbering from past
summaries.

1. Cell 1 — environment diagnostics (Java, RAM, network).
2. Cell 2 — configuration and dependency installation.
3. Cell 3 — Google Drive authentication (only needed for Drive backups).
4. Cell 4 — Drive connection test.
5. Cell 5 — world restoration. An existing world is never overwritten; a
   missing world is only restored when Drive backup is enabled.
6. Cell 6 — Paper JAR download and verification (`backend/paper_download.py`).
7. Cell 7 — `eula.txt` and `server.properties`.
8. Cell 8 — start the control agent in a background thread
   (`agent.main.ControlAgent`).
9. Cell 9 — start Paper through the agent's `ActionExecutor` and wait for
   Paper's own readiness line. This is the single authoritative lifecycle; the
   notebook does not build its own `java` command.
10. Cell 10 — Playit.gg connector (started after the readiness check).
11. Cell 11 — connection info and health checks.
12. Cell 12 — Drive backup center (manual operations).
13. Cell 13 — logs, troubleshooting, shutdown.
14. Cell 14 — run the web panel in the kernel (separate from the Minecraft
    tunnel).

### Process detection is shared

`agent/process_info.py` is the one place that answers "is Paper running?" and
"is Paper ready?". `agent/actions.py`, `agent/status.py`, `agent/file_ops.py`,
and `backend/command_handler.py` all delegate to it. It never uses a substring
match over all process command lines: it requires a real `java` process with
this server's `paper.jar` on its argv, and readiness is a separate question
answered from Paper's own log.
