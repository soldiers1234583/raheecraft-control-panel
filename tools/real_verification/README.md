# Real-JVM verification of the server lifecycle

`backend/tests/test_lifecycle.py` proves the lifecycle manager's logic with fake
processes. This directory proves it against **real JVM processes** — real argv,
real `/proc` process detection, real signals, real logs, real exit codes.

## What it runs

`FakePaper.java` compiles to a real runnable `paper.jar`. It behaves like Paper
from the outside: it writes `logs/latest.log` with Paper's own boot marker
(`Starting minecraft server version …`) and readiness marker
(`Done (…)! For help, type "help"`), reads its console from stdin, saves and
exits on `stop`, and can be made to die on demand.

`verify_lifecycle_real.py` drives that JVM through
`agent.lifecycle.ServerLifecycle` — the same object the dashboard, the agent and
the notebook use — and checks:

1. preflight against a real JDK (and that a missing jar / refused EULA is named)
2. start → `RUNNING`, with the PID positively identified by `find_paper_pid`
3. a duplicate Start spawns no second JVM
4. `kill -9` → recorded exit code → bounded restart on a **new** PID
5. Stop → Minecraft's own `stop` reaches the JVM, which exits 0 and saves
6. nothing restarts the server after an intentional Stop
7. an unrecoverable startup failure is not retried
8. repeated crashes exhaust the restart budget → `CRASHED` with a reason
9. Stop during a long startup cancels the start instead of waiting it out
10. Stop escalates to `SIGTERM` when the console channel is unusable
11. a wedged JVM is `SIGKILL`ed and the result says the world may not be saved

Every check prints PASS/FAIL with its evidence; the script exits non-zero if any
check fails.

## Running it

```bash
# 1. build the stand-in server jar (needs a JDK 21 on PATH)
mkdir -p classes && javac -d classes FakePaper.java && python3 - <<'PY'
import zipfile, pathlib
with zipfile.ZipFile("paper.jar", "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("META-INF/MANIFEST.MF",
               "Manifest-Version: 1.0\r\nMain-Class: FakePaper\r\n\r\n")
    for cls in pathlib.Path("classes").rglob("*.class"):
        z.write(cls, str(cls.relative_to("classes")))
PY

# 2. verify (from the repository root)
python3 tools/real_verification/verify_lifecycle_real.py
```

Everything runs in throwaway temp directories: it never touches the real world,
the real server directory, or any existing backup.

## Caveats

- Needs a JDK 21 (`javac` and `java` on PATH) and a Linux `/proc`. The lifecycle
  itself works without `/proc` (it falls back to `ps`), but the strict process
  identity check is only fully exercised where `/proc` exists.
- `java -version` is slow on low-power devices, so the SQLite-free dry run takes
  a few minutes there. It is not part of `run_tests.py` — run it deliberately.
- A real Paper jar is **not** used, so this does not verify Paper's own startup
  time or memory requirements, only the lifecycle contract around it.
