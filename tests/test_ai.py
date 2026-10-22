"""AI search: the request sent to Claude, the tool loop, and mapping the answer.

Claude is replaced by a scripted fake client (no API key or network needed); the
supplier search behind the tools is the real one, against the mock shop.
"""

import json
from types import SimpleNamespace as NS

import pytest
import yaml
from fastapi.testclient import TestClient

from supsearch import ai
from test_e2e import server  # noqa: F401  (mock supplier site fixture)


def usage(i=100, o=50):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def tool_call(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


def answer(obj):
    return NS(type="text", text=json.dumps(obj, ensure_ascii=False))


class FakeClaude:
    """Returns the scripted responses in order and records every request."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.script.pop(0)


def make_app(tmp_path, monkeypatch, server_url, env=None):
    from supsearch import app as appmod, config, fetch
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(fetch, "DATA_DIR", str(tmp_path / "data"))
    for k in ("ADMIN_PASSWORD", "APP_PASSWORD", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    sites = {"settings": {"channel": ""}, "sites": [{
        "id": "a", "name": "חנות א", "fetch": "http", "vat_included": True,
        "search_url": server_url + "/a/search?q={query}", "item": "div.p",
        "fields": {"title": "a", "url": "a@href", "price": ".price", "stock": ".st", "mpn": ".mpn"}}]}
    (tmp_path / "sites.yaml").write_text(yaml.safe_dump(sites, allow_unicode=True), encoding="utf-8")
    return TestClient(appmod.create_app(str(tmp_path / "sites.yaml"), str(tmp_path / "none.yaml")),
                      client=("127.0.0.1", 50000))


def test_ai_search_loop_and_answer(tmp_path, monkeypatch, server):  # noqa: F811
    fake = FakeClaude([
        NS(stop_reason="tool_use", usage=usage(1000, 200), content=[
            NS(type="thinking", thinking="", signature="sig"),
            tool_call("t1", "search_suppliers", query="kingston", in_stock_only=True),
            tool_call("t2", "search_suppliers", query="ssd 2tb", in_stock_only=False),
        ]),
        NS(stop_reason="end_turn", usage=usage(3000, 400), content=[answer({
            "understanding": "SSD של Kingston בנפח 1TB",
            "matches": [{"product_id": "p1", "match": "exact", "reason": "אותו דגם"},
                        {"product_id": "p99", "match": "close", "reason": "מזהה שלא הוחזר"}],
            "note": ""})]),
    ])
    monkeypatch.setattr(ai, "make_client", lambda key: fake)
    with make_app(tmp_path, monkeypatch, server, env={"ANTHROPIC_API_KEY": "sk-ant-test"}) as c:
        assert c.get("/api/info").json()["ai"] is True
        r = c.post("/api/ai/search", json={"query": "kingston 1tb"})
    assert r.status_code == 200, r.text
    j = r.json()

    first = fake.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default" and first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["output_config"]["effort"] == "medium"
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert all(t["strict"] for t in first["tools"]) and first["tool_choice"] == {"type": "auto"}
    assert "thinking" not in first and "temperature" not in first

    # the second request carries the assistant turn unchanged and both results in one message
    second = fake.requests[1]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"][0].type == "thinking"
    results = second[2]["content"]
    assert [x["tool_use_id"] for x in results] == ["t1", "t2"]
    found = json.loads(results[0]["content"])["products"]
    assert found[0]["id"] == "p1" and found[0]["supplier"] == "חנות א"
    assert all(p["in_stock"] is True for p in found)        # in_stock_only respected
    assert any(p["in_stock"] is False for p in json.loads(results[1]["content"])["products"])

    assert j["understanding"] == "SSD של Kingston בנפח 1TB"
    assert [m["match"] for m in j["matches"]] == ["exact"]   # unknown id p99 dropped
    assert j["matches"][0]["title"] == "Kingston SSD 1TB NV3 NVMe"
    assert j["matches"][0]["price_inc_vat"] == 249.0 and j["matches"][0]["reason"] == "אותו דגם"
    assert j["searches"] == ["kingston", "ssd 2tb"]
    assert j["usage"]["input"] == 4000 and j["usage"]["cost_usd"] == pytest.approx(0.028)


def test_last_round_must_answer(tmp_path, monkeypatch, server):  # noqa: F811
    loop = [NS(stop_reason="tool_use", usage=usage(),
               content=[tool_call(f"t{i}", "search_suppliers", query="ssd", in_stock_only=True)])
            for i in range(ai.MAX_ROUNDS)]
    final = NS(stop_reason="end_turn", usage=usage(),
               content=[answer({"understanding": "x", "matches": [], "note": "לא נמצא"})])
    fake = FakeClaude(loop + [final])
    monkeypatch.setattr(ai, "make_client", lambda key: fake)
    with make_app(tmp_path, monkeypatch, server, env={"ANTHROPIC_API_KEY": "k"}) as c:
        j = c.post("/api/ai/search", json={"query": "ssd"}).json()
    assert len(fake.requests) == ai.MAX_ROUNDS + 1
    assert fake.requests[-1]["tool_choice"] == {"type": "none"}
    assert j["matches"] == [] and j["note"] == "לא נמצא"


@pytest.mark.parametrize("response,message", [
    (NS(stop_reason="refusal", usage=usage(), content=[]), "נדחתה"),
    (NS(stop_reason="end_turn", usage=usage(), content=[NS(type="text", text="not json")]), "מסודרת"),
])
def test_ai_failures_reported(tmp_path, monkeypatch, server, response, message):  # noqa: F811
    monkeypatch.setattr(ai, "make_client", lambda key: FakeClaude([response]))
    with make_app(tmp_path, monkeypatch, server, env={"ANTHROPIC_API_KEY": "k"}) as c:
        r = c.post("/api/ai/search", json={"query": "ssd"})
    assert r.status_code == 502 and message in r.json()["detail"]


def test_ai_key_admin_only_and_never_returned(tmp_path, monkeypatch, server):  # noqa: F811
    async def fake_check(key):
        return key.endswith("good"), "המפתח תקין" if key.endswith("good") else "המפתח שגוי או בוטל"
    monkeypatch.setattr(ai, "check_key", fake_check)
    with make_app(tmp_path, monkeypatch, server, env={"APP_PASSWORD": "admin-pass"}) as c:
        c.post("/api/login", json={"username": "admin", "password": "admin-pass"})
        assert c.post("/api/ai/search", json={"query": "x"}).status_code == 400   # no key yet
        assert c.get("/api/info").json()["ai"] is False
        r = c.put("/api/admin/ai-key", json={"key": "sk-ant-api03-secretvalue-good"}).json()
        assert r["ok"] is True
        status = c.get("/api/admin/ai-key")
        assert "secretvalue" not in status.text and status.json()["saved"]["hint"] == "good"
        assert "secretvalue" not in (tmp_path / "data" / "secrets.json").read_text()
        assert c.post("/api/admin/ai-key/test").json()["ok"] is True
        assert c.get("/api/info").json()["ai"] is True
        c.post("/api/admin/users", json={"username": "dana", "password": "dana-pass"})
        c.post("/api/logout")
        c.post("/api/login", json={"username": "dana", "password": "dana-pass"})
        assert c.get("/api/admin/ai-key").status_code == 403
        assert c.put("/api/admin/ai-key", json={"key": "x"}).status_code == 403
        assert c.get("/ai").status_code == 200                                     # users can use it
