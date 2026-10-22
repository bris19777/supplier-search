"""The Tailscale watchdog: reading the state and repairing what's missing."""

import asyncio
import json

from supsearch.tailscale import TailscaleWatch, read_state

OFFICE = {"HostName": "desktop-7900x", "Online": True, "ExitNode": True}
RUNNING_WITH_EXIT = {"BackendState": "Running", "ExitNodeStatus": {"ID": "n1", "Online": True},
                     "Peer": {"k1": OFFICE, "k2": {"HostName": "laptop", "Online": False}}}


def test_read_state():
    assert read_state(RUNNING_WITH_EXIT, "desktop-7900x") == ("office", "desktop-7900x")
    asleep = {**RUNNING_WITH_EXIT, "Peer": {"k1": {**OFFICE, "Online": False}}}
    assert read_state(asleep, "desktop-7900x") == ("office_offline", "desktop-7900x")
    assert read_state({"BackendState": "Running"}, "desktop-7900x") == ("no_exit_node", "desktop-7900x")
    assert read_state({"BackendState": "NeedsLogin"}, "x")[0] == "logged_out"
    assert read_state({"BackendState": "Starting"}, "x")[0] == "connecting"
    assert read_state({}, "x")[0] == "logged_out"


class Scripted(TailscaleWatch):
    """Answers `tailscale ...` calls from a list of states and records the commands."""

    def __init__(self, states, fail=()):
        super().__init__()
        self.authkey, self.exit_node, self.configured = "tskey-auth-x", "desktop-7900x", True
        self.states, self.fail, self.calls = list(states), set(fail), []

    async def _ts(self, *args, timeout=90):
        self.calls.append(args[0])
        if args[0] == "status":
            return 0, json.dumps(self.states.pop(0) if len(self.states) > 1 else self.states[0])
        return (1, f"{args[0]} failed") if args[0] in self.fail else (0, "")


def test_reconnects_after_a_failed_boot():
    w = Scripted([{"BackendState": "NeedsLogin"}, RUNNING_WITH_EXIT])
    assert asyncio.run(w.check()) == "office"
    assert w.calls == ["status", "up", "set", "status"]          # logged in again, exit node set
    assert w.info()["egress"] == "office" and w.info()["egress_detail"] == "desktop-7900x"


def test_reselects_a_dropped_exit_node():
    w = Scripted([{"BackendState": "Running"}, RUNNING_WITH_EXIT])
    assert asyncio.run(w.check()) == "office"
    assert w.calls == ["status", "set", "status"]


def test_healthy_does_nothing_and_errors_are_reported():
    w = Scripted([RUNNING_WITH_EXIT])
    asyncio.run(w.check())
    assert w.calls == ["status"]
    w = Scripted([{"BackendState": "NeedsLogin"}], fail={"up"})
    assert asyncio.run(w.check()) == "logged_out"
    assert "up failed" in w.info()["egress_error"] and "set" not in w.calls


def test_not_configured_is_direct():
    w = TailscaleWatch()
    w.configured = False
    assert w.info() == {"egress": "direct", "egress_detail": ""}
