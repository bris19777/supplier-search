"""Keeping the office exit node connected, and reporting its real state.

start.sh starts tailscaled (userspace, with a local proxy) and makes a first
attempt to log in and select the exit node. That single attempt can fail on a
passing hiccup (the control server not reachable yet at boot), and the
connection can drop later (after a Fly suspend/resume, or while the office PC
sleeps). This watchdog checks every minute and repairs what is missing, and
its status is what the UI shows.
"""

import asyncio
import json
import os
import time

SOCKET = "/tmp/tailscaled.sock"


def read_state(status, exit_node):
    """`tailscale status --json` -> (state, detail).

    state: "office"          traffic leaves through the exit node, which is online
           "office_offline"  the exit node is selected but offline (PC asleep / off)
           "no_exit_node"    connected, but the exit node isn't selected
           "connecting"      tailscaled is still starting up
           "logged_out"      tailscale isn't logged in / running
    """
    if status.get("BackendState") in ("Starting", "NoState"):
        return "connecting", status.get("BackendState")
    if status.get("BackendState") != "Running":
        return "logged_out", status.get("BackendState") or "unknown"
    ens = status.get("ExitNodeStatus")
    if not ens:
        return "no_exit_node", exit_node
    online = bool(ens.get("Online"))
    name = exit_node
    for peer in (status.get("Peer") or {}).values():
        if peer.get("ExitNode"):
            name = peer.get("HostName") or name
            online = bool(peer.get("Online", online))
    return ("office" if online else "office_offline"), name


class TailscaleWatch:
    def __init__(self, interval=60):
        self.authkey = os.environ.get("TS_AUTHKEY", "")
        self.exit_node = os.environ.get("TS_EXIT_NODE", "")
        self.hostname = os.environ.get("TS_HOSTNAME", "supplier-search")
        self.configured = bool(self.authkey)
        self.interval = interval
        self.state, self.detail, self.error, self.checked = ("off", "", "", None)
        self._task = None

    async def _ts(self, *args, timeout=90):
        proc = await asyncio.create_subprocess_exec(
            "tailscale", f"--socket={SOCKET}", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 1, "timeout"
        return proc.returncode, out.decode(errors="replace")

    async def check(self):
        """One round: read the state and repair the step that is missing."""
        rc, out = await self._ts("status", "--json", timeout=20)
        try:
            status = json.loads(out) if rc == 0 or out.lstrip().startswith("{") else {}
        except ValueError:
            status = {}
        state, detail = read_state(status, self.exit_node)
        self.error = ""
        if state == "logged_out":
            # --reset: start from clean preferences, so a saved exit node doesn't make `up` refuse
            rc, out = await self._ts("up", "--reset", f"--auth-key={self.authkey}",
                                     f"--hostname={self.hostname}", "--timeout=60s")
            if rc != 0:
                self.error = out.strip()[-300:]
        if state in ("logged_out", "no_exit_node") and self.exit_node and not self.error:
            rc, out = await self._ts("set", f"--exit-node={self.exit_node}", timeout=30)
            if rc != 0:
                self.error = out.strip()[-300:]
        if state in ("logged_out", "no_exit_node"):
            rc, out = await self._ts("status", "--json", timeout=20)
            try:
                state, detail = read_state(json.loads(out), self.exit_node)
            except ValueError:
                pass
        self.state, self.detail, self.checked = state, detail, time.time()
        return state

    async def _loop(self):
        while True:
            try:
                state = await self.check()
            except Exception as e:  # e.g. tailscale binary missing; keep reporting, keep trying
                self.state, self.error = "logged_out", f"{type(e).__name__}: {e}"[:300]
                state = self.state
            await asyncio.sleep(self.interval if state == "office" else 20)

    def start(self):
        if self.configured and self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def info(self):
        if not self.configured:
            return {"egress": "direct", "egress_detail": ""}
        return {"egress": self.state, "egress_detail": self.detail, "egress_error": self.error,
                "egress_checked": self.checked}


if __name__ == "__main__":  # python -m supsearch.tailscale: what the watchdog sees right now
    async def _show():
        w = TailscaleWatch()
        rc, out = await w._ts("status", "--json", timeout=20)
        print(read_state(json.loads(out), w.exit_node) if rc == 0 else out)
    asyncio.run(_show())
