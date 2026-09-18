"""Saved briefs: BriefStore round-trip + pruning, /api/brief persists each generation,
/api/briefs lists newest-first with the latest full body, /api/briefs/{id} fetches one."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from agentic.config import Settings
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.briefs import BriefStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app

T0 = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


def test_store_save_recent_get_latest_prune(tmp_path):
    st = BriefStore(Database(tmp_path / "b.db"))
    ids = [st.save(f"Brief {i}", f"# body {i}", has_ai=(i % 2 == 0), meta={"open_positions": i},
                   created_at=T0 + timedelta(days=i), keep=0) for i in range(4)]
    rec = st.recent()
    assert [r["title"] for r in rec] == ["Brief 3", "Brief 2", "Brief 1", "Brief 0"]   # newest first
    assert "body" not in rec[0] and rec[0]["chars"] == len("# body 3")                 # list view is light
    assert rec[0]["has_ai"] is False and rec[1]["has_ai"] is True and rec[1]["meta"] == {"open_positions": 2}
    one = st.get(ids[1])
    assert one["body"] == "# body 1" and one["title"] == "Brief 1"
    assert st.latest()["id"] == ids[3]
    assert st.get("nope") is None
    assert st.prune(2) == 2 and [r["title"] for r in st.recent()] == ["Brief 3", "Brief 2"]
    assert st.delete(ids[3]) is True and st.latest()["id"] == ids[2]
    # save() prunes by default so the archive stays bounded
    st.save("x", "y", keep=1)
    assert len(st.recent()) == 1


def _client(tmp_path, with_store=True):
    db = Database(tmp_path / "dash.db")
    audit = AuditStore(db)
    deps = WebDeps(
        settings=Settings(mode="paper"), signals=SignalStore(db),
        killswitch=KillSwitch(db, audit), approval_gate=None, audit=audit,
        positions=PositionStore(db), orders=OrderStore(db), decisions=DecisionStore(db),
        briefs=BriefStore(db) if with_store else None,
    )
    return TestClient(create_app(deps)), deps


def test_brief_endpoint_persists_and_archive_endpoints_serve_it(tmp_path):
    client, deps = _client(tmp_path)
    assert client.get("/api/briefs").json() == {"available": True, "briefs": [], "latest": None}

    d = client.get("/api/brief").json()
    assert d["id"] and d["body"] and d["has_ai"] is False and d["created_at"]
    lst = client.get("/api/briefs").json()
    assert lst["available"] and [b["id"] for b in lst["briefs"]] == [d["id"]]
    assert lst["latest"]["id"] == d["id"] and lst["latest"]["body"] == d["body"]
    assert lst["briefs"][0]["meta"]["open_positions"] == 0 and "body" not in lst["briefs"][0]

    one = client.get(f"/api/briefs/{d['id']}").json()
    assert one["body"] == d["body"] and one["title"] == d["title"]
    assert client.get("/api/briefs/does-not-exist").status_code == 404

    # a second generation is a second row, newest first
    d2 = client.get("/api/brief").json()
    ids = [b["id"] for b in client.get("/api/briefs").json()["briefs"]]
    assert ids[0] == d2["id"] and set(ids) == {d["id"], d2["id"]}


def test_brief_endpoint_without_store_still_returns_the_brief(tmp_path):
    client, _ = _client(tmp_path, with_store=False)
    d = client.get("/api/brief").json()
    assert d["body"] and d["id"] is None
    assert client.get("/api/briefs").json() == {"available": False, "briefs": [], "latest": None}
    assert client.get("/api/briefs/x").status_code == 404


def test_dashboard_ships_archive_and_layout_markers(tmp_path):
    page = _client(tmp_path)[0].get("/dashboard").text
    for marker in ('id="brief-list"', 'id="brief-run"', 'id="brief-meta"', "Generate new",
                   # phone bottom tab bar icons + collapsible cards + readability pills
                   'class="ti"', "initCollapsibles", 'data-fold="closed"', ".pill.avoid", "biasPill",
                   # Watchlist pane leads with the daily read, not the controls
                   "Setups today"):
        assert marker in page, f"missing dashboard marker: {marker!r}"
    assert page.index('id="setups"') < page.index("<h2>Scanner</h2>")
    # the history tab is now cards, not bare h3 headings
    assert "<h3>All positions</h3>" not in page and "<h2>All positions</h2>" in page
