#!/usr/bin/env python3
"""
RaheeCraft Control Agent
=========================
Kaggle-side agent that connects to the control panel backend.

The agent:
1. Registers with the backend
2. Sends periodic heartbeats with server status
3. Reconnects with exponential backoff on failure
4. Executes power/console actions on a worker thread, so a long start or a
   graceful stop never stops the heartbeats (a stale heartbeat used to make the
   panel show "agent offline" mid-start and refuse new actions)
5. Runs the lifecycle manager's health supervisor: unexpected exits are
   detected, reported, and recovered within a bounded, backed-off budget

Usage in Kaggle notebook:
    import os
    os.environ['BACKEND_URL'] = 'https://your-backend-url'
    os.environ['AGENT_ID'] = 'agent-xxxxxxxx'
    os.environ['AGENT_SECRET'] = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
    os.environ['SERVER_DIR'] = '/kaggle/working/minecraft_server'
    
    from agent.main import run_agent
    run_agent()
"""

import os
import sys
import time
import json
import queue
import logging
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.config import Config, config
from agent.status import get_agent_status
from agent.actions import action_executor, ALLOWED_ACTION_TYPES
from agent.lifecycle import lifecycle
from agent.logstream import log_streamer, build_log_payload
from agent.player_tracker import player_tracker

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# A 401/403 answer from an agent endpoint means the backend no longer
# recognises this agent id. That is the normal result of a panel restart or
# redeploy, because the agent registry is in-memory: the id is forgotten while
# the agent keeps its credentials. Retrying the heartbeat forever leaves the
# agent silently invisible to the panel, so the loop must stop and re-register.
AUTH_REJECTED = "auth_rejected"


class ControlAgent:
    """Control agent that connects to backend."""

    def __init__(self):
        self.running = False
        self.session = None
        # Actions run on their own thread: a start waits for real readiness and
        # a stop waits for Minecraft to save, both far longer than the heartbeat
        # interval. Running them inline starved the heartbeat and made the panel
        # believe the agent had gone away.
        self._actions: "queue.Queue" = queue.Queue()
        self._inflight: Dict[str, float] = {}
        self._inflight_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._shutdown = threading.Event()

    # --- action worker ------------------------------------------------------

    def _start_worker(self) -> None:
        """Start the single action worker thread (idempotent)."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._shutdown.clear()
        self._worker = threading.Thread(target=self._action_worker,
                                        name="rahee-action-worker", daemon=True)
        self._worker.start()

    def _action_worker(self) -> None:
        """Execute queued actions one at a time, reporting each real outcome."""
        while not self._shutdown.is_set():
            try:
                action = self._actions.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run_action(action)
            except Exception as exc:
                logger.error(f"Action worker error: {exc}")

    def _run_action(self, action: dict) -> None:
        import httpx

        action_id = action.get("action_id")
        action_type = action.get("action_type")
        started = time.time()
        logger.info(f"Executing action {action_id}: {action_type} "
                    f"(server is {lifecycle.state.value})")
        try:
            result = action_executor.execute(action)
        except Exception as exc:
            result = {"state": "failed", "error": str(exc), "result": {}}
        finally:
            with self._inflight_lock:
                self._inflight.pop(action_id, None)

        took = round(time.time() - started, 1)
        logger.info(f"Action {action_id} finished: {result['state']} in {took}s "
                    f"(server is {lifecycle.state.value})")

        if action_id and not action_id.startswith("<"):
            try:
                httpx.post(
                    f"{config.BACKEND_URL}/api/agent/actions/{action_id}",
                    headers=self._headers(),
                    json={
                        "state": result["state"],
                        "error": result.get("error"),
                        "result": result.get("result") or {},
                    },
                    timeout=config.CONNECT_TIMEOUT,
                )
            except Exception as exc:
                logger.warning(f"Could not report action result: {exc}")

    def pending_action_count(self) -> int:
        with self._inflight_lock:
            return len(self._inflight)

    def submit_action(self, action: dict) -> bool:
        """Queue one action for the worker. False when it is already queued.

        Duplicate delivery of the same action_id (a poll response replayed
        after a flaky connection) must never spawn a second operation.
        """
        action_id = action.get("action_id")
        if action.get("action_type") not in ALLOWED_ACTION_TYPES:
            return False
        with self._inflight_lock:
            if action_id in self._inflight:
                return False
            if not self._shutdown.is_set():
                self._inflight[action_id] = time.time()
        self._actions.put(action)
        return True

    def stop_worker(self) -> None:
        self._shutdown.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
    
    def register(self) -> bool:
        """
        Register agent with backend.
        
        Returns True if registration successful.
        """
        if not config.AGENT_ID or not config.AGENT_SECRET:
            logger.error("Agent ID or secret not configured")
            return False
        
        if not config.BACKEND_URL:
            logger.error("BACKEND_URL not configured")
            return False
        
        import httpx
        
        url = f"{config.BACKEND_URL}/api/agent/register"
        headers = {
            "X-Agent-ID": config.AGENT_ID,
            "X-Agent-Token": config.AGENT_SECRET,
            "Content-Type": "application/json"
        }
        
        # Add metadata
        metadata = {
            "platform": "kaggle",
            "python_version": sys.version,
            "server_dir": config.SERVER_DIR
        }
        
        try:
            response = httpx.post(
                url,
                headers=headers,
                json={"metadata": metadata},
                timeout=config.CONNECT_TIMEOUT
            )
            
            if response.status_code == 200:
                logger.info(f"Agent registered: {config.AGENT_ID[:8]}...")
                return True
            elif response.status_code == 409:
                logger.info(f"Agent already registered: {config.AGENT_ID[:8]}...")
                return True
            else:
                logger.error(f"Registration failed: {response.status_code} {response.text}")
                return False
                
        except httpx.ConnectError as e:
            logger.warning(f"Cannot connect to backend: {e}")
            return False
        except Exception as e:
            logger.error(f"Registration error: {e}")
            return False
    
    def send_heartbeat(self):
        """
        Send heartbeat with server status to backend.

        Returns True on success, AUTH_REJECTED when the backend refused this
        agent's credentials (the caller must re-register), False on a
        transient failure (retry on the next cycle).
        """
        if not config.AGENT_ID or not config.AGENT_SECRET:
            return False
        
        if not config.BACKEND_URL:
            return False
        
        import httpx
        
        status = get_agent_status()
        # Stage 3: real player state from Paper's structured files
        try:
            status["player_snapshot"] = player_tracker.snapshot()
        except Exception as _e:
            logger.debug(f"player snapshot error: {_e}")
        
        url = f"{config.BACKEND_URL}/api/agent/heartbeat"
        headers = {
            "X-Agent-ID": config.AGENT_ID,
            "X-Agent-Token": config.AGENT_SECRET,
            "Content-Type": "application/json"
        }
        
        try:
            response = httpx.post(
                url,
                headers=headers,
                json=status,
                timeout=config.CONNECT_TIMEOUT
            )
            
            if response.status_code == 200:
                return True
            if response.status_code in (401, 403):
                logger.warning("Heartbeat rejected: %s", response.status_code)
                return AUTH_REJECTED
            logger.warning(f"Heartbeat failed: {response.status_code}")
            return False

        except httpx.ConnectError:
            logger.debug("Backend not reachable")
            return False
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            return False
    
    def _headers(self) -> dict:
        """Authenticated agent request headers."""
        return {
            "X-Agent-ID": config.AGENT_ID,
            "X-Agent-Token": config.AGENT_SECRET,
            "Content-Type": "application/json",
        }

    def push_logs(self) -> bool:
        """Stream new console log lines to the backend (best effort)."""
        import httpx

        lines = log_streamer.read_new_lines()
        # Stage 3: feed join/leave/list lines to the player tracker
        try:
            player_tracker.observe_lines(lines)
        except Exception:
            pass

        from agent.logstream import classify_level
        import time as _time
        payload = {
            "lines": [{"text": l, "level": classify_level(l)} for l in lines],
            "ts": _time.time(),
        }
        if not payload["lines"]:
            return True

        try:
            response = httpx.post(
                f"{config.BACKEND_URL}/api/agent/logs",
                headers=self._headers(),
                json=payload,
                timeout=config.CONNECT_TIMEOUT,
            )
            return response.status_code == 200
        except Exception:
            return False

    def poll_and_execute_actions(self) -> int:
        """Fetch pending actions and hand them to the worker thread.

        Only allowlisted action types are queued (the executor re-validates).
        Returns the number of actions newly queued; execution continues on the
        worker so heartbeats keep flowing during a long start or a graceful
        stop.
        """
        import httpx

        try:
            response = httpx.post(
                f"{config.BACKEND_URL}/api/agent/poll",
                headers=self._headers(),
                json={},
                timeout=config.CONNECT_TIMEOUT,
            )
            if response.status_code != 200:
                return 0
            actions = response.json().get("actions", [])
        except Exception:
            return 0

        queued = 0
        for action in actions:
            if self.submit_action(action):
                queued += 1
                logger.info(f"Queued action {action.get('action_id')}: "
                            f"{action.get('action_type')}")
        return queued

    def _log_lifecycle_event(self, event: str, payload: Dict[str, Any]) -> None:
        """Surface crash/recovery milestones in the notebook output."""
        if event == "restart_scheduled":
            logger.warning(f"Server exited (code {payload.get('exit_code')}); "
                           f"restart attempt {payload.get('attempt')} in "
                           f"{payload.get('backoff'):.0f}s")
        elif event == "restart_succeeded":
            logger.info("Server restarted after an unexpected exit")
        elif event in ("restart_exhausted", "unrecoverable"):
            logger.error(f"Server marked CRASHED: {payload.get('reason')}")

    def run(self):
        """
        Main agent loop.

        1. Register with backend
        2. Send periodic heartbeats (never blocked by a running action)
        3. Queue power/console actions for the worker thread
        4. Stream new console log lines
        5. Supervise the server: detect unexpected exits and recover within a
           bounded budget — but never restart after an intentional Stop
        6. Reconnect on failure with backoff
        """
        logger.info("=" * 50)
        logger.info("RaheeCraft Control Agent starting")
        logger.info(f"Agent ID: {config.AGENT_ID[:8] if config.AGENT_ID else 'Not set'}...")
        logger.info(f"Backend: {config.BACKEND_URL}")
        logger.info(f"Server dir: {config.SERVER_DIR}")
        logger.info("=" * 50)

        self.running = True
        self._start_worker()
        lifecycle.on_event(self._log_lifecycle_event)
        lifecycle.start_supervisor()
        retry_delay = config.RETRY_BASE_DELAY

        try:
            while self.running:
                # Try to register
                if not self.register():
                    logger.warning(f"Registration failed, retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)  # Exponential backoff
                    continue

                # Reset retry delay on successful registration
                retry_delay = config.RETRY_BASE_DELAY

                # Send heartbeats, queue actions, stream logs
                while self.running:
                    success = self.send_heartbeat()

                    if success is AUTH_REJECTED:
                        # The backend forgot this agent (panel restarted, or the
                        # panel is a new instance). Leave the inner loop so the
                        # outer loop re-registers; registration is idempotent.
                        # The streamer is reset so the fresh panel receives
                        # recent console lines instead of showing an empty
                        # console until the server happens to log something.
                        logger.warning("Backend does not recognise this agent; "
                                       "re-registering")
                        log_streamer.reset()
                        break

                    if not success:
                        logger.warning("Heartbeat failed, will retry on next cycle")
                    else:
                        # Queue power actions (start/stop/restart/console); the
                        # worker thread runs them without blocking heartbeats
                        try:
                            self.poll_and_execute_actions()
                        except Exception as e:
                            logger.error(f"Action polling error: {e}")

                        # Stream new console lines (offset-based, no replay)
                        try:
                            self.push_logs()
                        except Exception as e:
                            logger.debug(f"Log push error: {e}")

                    time.sleep(config.POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("Agent interrupted by the user")
        finally:
            self.running = False
            lifecycle.stop_supervisor()
            self.stop_worker()

        logger.info("Agent stopped")


def run_agent():
    """Entry point for running the agent."""
    agent = ControlAgent()
    agent.run()


if __name__ == "__main__":
    run_agent()
