# RaheeCraft — Environment Variables

Everything here is read from the environment. Nothing is required for the
existing Stage 1–3 feature set; each variable has a safe default.

Secrets (`AGENT_SECRET`, `PLAYIT_TOKEN`, `GOOGLE_CLIENT_SECRET`, admin
passwords, …) belong in Kaggle Secrets or the deployment's env file — never in
the notebook source, and never in git. `.gitignore` already excludes `.env*`,
`credentials.json`, `*.token`, `*.key`, and `*.secret`.

## Server location and identity

| Variable | Default | Used by |
|---|---|---|
| `SERVER_DIR` | `/kaggle/working/minecraft_server` | agent, backend paths, paper download |
| `WORLD_DIR` | `$SERVER_DIR/world` | agent status/player tracking |
| `LOG_FILE` | `$SERVER_DIR/logs/latest.log` | agent log streaming |
| `SERVER_PORT` | `25565` | notebook config + Playit target notes |
| `LEVEL_NAME` | `world` | notebook `server.properties` |

## Paper download (`backend/paper_download.py`, notebook Cell 6)

| Variable | Default | Notes |
|---|---|---|
| `MINECRAFT_VERSION` | `1.21.4` | Paper build to resolve |
| `PAPER_API_BASE` | `https://api.papermc.io/v2` | official PaperMC API only |
| `PAPER_FORCE_DOWNLOAD` | `0` | `1` replaces an existing `paper.jar` |
| `PAPER_JAR_MAX_BYTES` | `209715200` (200 MB) | download ceiling |
| `PAPER_JAR_MIN_BYTES` | `1024` | rejects stub responses |

## Java and lifecycle (agent)

| Variable | Default | Notes |
|---|---|---|
| `JAVA_XMS` | `1024` | initial heap, MB |
| `JAVA_XMX` | `2048` | max heap, MB |
| `START_TIMEOUT_SECONDS` | `30` (agent) / `600` (notebook) | readiness window |
| `STOP_TIMEOUT_SECONDS` | `60` | graceful stop window before SIGKILL |

`agent/process_info.py` is the single source of truth for "is Paper running?"
and "is Paper ready?". Readiness comes from Paper's own `Done (…)` line in
`$SERVER_DIR/logs/latest.log`, scoped to the most recent boot.

## Agent connection

| Variable | Default |
|---|---|
| `AGENT_ID` | *(required)* |
| `AGENT_SECRET` | *(required)* |
| `BACKEND_URL` | *(required)* |
| `BACKEND_API_PATH` | `/api/agent` |
| `POLL_INTERVAL_SECONDS` | `30` |
| `CONNECT_TIMEOUT` | `10` |
| `MAX_RETRIES` | `3` |
| `RETRY_BASE_DELAY` | `1.0` |
| `ENABLE_PLAYIT` | `true` |
| `ENABLE_DRIVE_BACKUP` | `true` |

## Stage 4 operation limits (`backend/operation_registry.py`)

| Variable | Default |
|---|---|
| `STAGE4_MAX_EDIT_BYTES` | `2097152` (2 MB) |
| `STAGE4_MAX_UPLOAD_BYTES` | `268435456` (256 MB) |
| `STAGE4_MAX_DOWNLOAD_BYTES` | `268435456` (256 MB) |
| `STAGE4_MAX_ARCHIVE_BYTES` | `4294967296` (4 GB) |
| `STAGE4_UPLOAD_CHUNK_BYTES` | `4194304` (4 MB) |
| `STAGE4_MAX_CONCURRENT_OPERATIONS` | `6` |
| `STAGE4_RESULT_BYTES_CAP` | `262144` (256 KB) |
| `STAGE4_PLUGIN_UPLOAD_BYTES` | `67108864` (64 MB) |

## Playit.gg

| Variable | Notes |
|---|---|
| `PLAYIT_TOKEN` | account token from the Playit dashboard |

The connector does not take a local-port argument. The public → local mapping
is configured per tunnel in the Playit dashboard and **must target
`127.0.0.1:25565`** for the Java server. The code reports the public address it
is actually given; it never invents one.

The address the connector was given (`*.playit.gg` or `*.ply.gg`, host and
port) is persisted to `$PLAYIT_DIR/public_address.json` as it is discovered and
read back by the agent for the panel; the connector log
(`$PLAYIT_DIR/playit.log`) is a fallback source. Neither file is a credential.
Note that Playit can report a bare IP instead of a domain when the account
setting **"Show IPs instead of domains"** is on; the panel does not trust a bare
IP as a join address.
