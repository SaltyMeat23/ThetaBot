"""RobinhoodMCPBroker mapping seams (pinned against live RH MCP schemas, 2026-06-18).

These cover the pure, fully-verifiable pieces: order-arg shape, OCC construction, ref_id
determinism, data-envelope unwrapping, position mapping (record + enriched instrument), and
the account_number capability gate. The two TODO(verify-live) spots — exact position-record
field names and placed-order response fields — are best-effort and not asserted strictly.
"""
from datetime import date

import pytest

from agentic.brokers.robinhood_mcp import RobinhoodMCPBroker
from agentic.domain.enums import Direction, OptionType, Strategy
from agentic.domain.models import Order


def _broker(account_number="1234567890"):
    return RobinhoodMCPBroker(account_number=account_number)


def test_build_close_order_args_uses_legs_and_strings():
    order = Order(
        decision_id="d1", position_id="p1", occ_symbol="AAPL260622C00250000",
        option_id="683f926d-2462-4402-bc01-bd4bfbdbcf24",
        quantity=2, limit_price=1.5, is_paper=False, client_order_id="close-abc",
    )
    args = _broker()._build_close_order_args(order)
    assert args["account_number"] == "1234567890"
    assert args["legs"] == [{
        "option_id": "683f926d-2462-4402-bc01-bd4bfbdbcf24",
        "side": "buy",
        "position_effect": "close",
    }]
    assert args["quantity"] == "2"          # string, not int
    assert args["price"] == "1.50"          # 2-decimal string
    assert args["type"] == "limit"
    assert args["time_in_force"] == "gfd"
    assert "ref_id" in args
    # Legacy placeholder keys must be gone.
    assert "occ_symbol" not in args and "limit_price" not in args and "direction" not in args


def test_build_close_order_args_requires_option_id():
    order = Order(
        decision_id="d1", position_id="p1", occ_symbol="AAPL260622C00250000",
        quantity=1, limit_price=1.0, is_paper=False,
    )  # option_id is None
    with pytest.raises(RuntimeError):
        _broker()._build_close_order_args(order)


def test_ref_id_is_deterministic_uuid():
    o1 = Order(decision_id="d", position_id="p", occ_symbol="X", option_id="i",
               quantity=1, limit_price=1.0, is_paper=False, client_order_id="close-xyz")
    o2 = Order(decision_id="d", position_id="p", occ_symbol="X", option_id="i",
               quantity=1, limit_price=1.0, is_paper=False, client_order_id="close-xyz")
    o3 = Order(decision_id="d", position_id="p", occ_symbol="X", option_id="i",
               quantity=1, limit_price=1.0, is_paper=False, client_order_id="close-other")
    rid = RobinhoodMCPBroker._ref_id(o1)
    assert rid == RobinhoodMCPBroker._ref_id(o2)   # same key -> same ref_id (retry-safe)
    assert rid != RobinhoodMCPBroker._ref_id(o3)
    assert len(rid) == 36 and rid.count("-") == 4  # canonical UUID form


def test_build_occ_symbol():
    occ = RobinhoodMCPBroker._build_occ_symbol("AAPL", date(2026, 6, 22), OptionType.CALL, 250.0)
    assert occ == "AAPL260622C00250000"
    put = RobinhoodMCPBroker._build_occ_symbol("F", date(2026, 1, 16), OptionType.PUT, 12.5)
    assert put == "F260116P00012500"


def test_iter_records_unwraps_data_envelope():
    raw = {"data": {"positions": [{"a": 1}, {"a": 2}]}, "guide": "..."}
    recs = RobinhoodMCPBroker._iter_records(raw)
    assert recs == [{"a": 1}, {"a": 2}]
    # single-object payload under data
    one = RobinhoodMCPBroker._first_record({"data": {"id": "o1", "state": "filled"}})
    assert one == {"id": "o1", "state": "filled"}


def test_list_positions_args_filters_short_open():
    args = _broker()._build_close_order_args  # noqa: F841 (touch to ensure import ok)
    a = _broker()._list_positions_args()
    assert a == {"account_number": "1234567890", "nonzero": True, "type": "short"}


def test_map_position_enriches_from_instrument():
    rec = {
        "option_id": "oid-1", "type": "short", "quantity": 3,
        # $1.20/share credit -> RH reports per-contract 120.00 (x100 multiplier).
        "average_price": "120.00", "trade_value_multiplier": 100,
        "chain_symbol": "AAPL", "expiration_date": "2026-06-22",
    }
    instruments = {"oid-1": {
        "id": "oid-1", "strike_price": "250.0000", "type": "call",
        "chain_symbol": "AAPL", "expiration_date": "2026-06-22",
    }}
    pos = RobinhoodMCPBroker._map_position(rec, instruments)
    assert pos is not None
    assert pos.option_id == "oid-1"
    assert pos.option_type is OptionType.CALL
    assert pos.strategy is Strategy.COVERED_CALL
    assert pos.direction is Direction.SHORT
    assert pos.quantity == 3
    assert pos.strike == 250.0
    assert pos.occ_symbol == "AAPL260622C00250000"
    assert pos.credit_received == 1.2


def test_map_position_skips_long():
    rec = {"option_id": "oid-2", "type": "long", "quantity": 1,
           "average_price": "1.0", "chain_symbol": "AAPL", "expiration_date": "2026-06-22"}
    instruments = {"oid-2": {"id": "oid-2", "strike_price": "250", "type": "call"}}
    assert RobinhoodMCPBroker._map_position(rec, instruments) is None


def test_capabilities_requires_account_number():
    # Not connected yet -> read-only note, regardless of account.
    caps = _broker(account_number="").capabilities()
    assert caps.supports_options_orders is False
    assert caps.is_paper is False


@pytest.mark.asyncio
async def test_get_buying_power_reads_nested_field_and_account_value(monkeypatch):
    # Real get_portfolio shape verified 2026-07-17 against account 1234567890.
    portfolio = {"data": {
        "total_value": "1500", "equity_value": "0", "options_value": "0", "cash": "1500",
        "pending_deposits": "0", "currency": "USD",
        "buying_power": {"buying_power": "1500.0000", "unleveraged_buying_power": "1500.0000",
                         "display_currency": "USD"},
    }}
    b = _broker()
    b._roles = {"get_portfolio": "get_portfolio"}

    async def fake_call(name, args):
        return portfolio

    monkeypatch.setattr(b, "_call_tool", fake_call)
    assert await b.get_buying_power() == 1500.0       # nested buying_power.buying_power
    assert await b.get_account_value() == 1500.0      # top-level total_value


@pytest.mark.asyncio
async def test_get_buying_power_falls_back_to_scalar_and_cash(monkeypatch):
    b = _broker()
    b._roles = {"get_portfolio": "get_portfolio"}

    async def flat(name, args):
        return {"data": {"buying_power": "2200.50", "cash": "2200.50"}}   # scalar shape

    monkeypatch.setattr(b, "_call_tool", flat)
    assert await b.get_buying_power() == 2200.5


def test_map_position_scales_per_contract_average_price():
    # VERIFIED LIVE 2026-07-22: RH average_price is per-CONTRACT dollars; must divide by 100.
    oid = "opt-abc"
    rec = {"option_id": oid, "quantity": "1", "type": "short", "average_price": "-20.0000",
           "trade_value_multiplier": 100, "expiration_date": "2026-07-31", "id": "pos1"}
    instruments = {oid: {"chain_symbol": "ONDS", "strike_price": "7.5", "type": "put",
                         "expiration_date": "2026-07-31"}}
    pos = RobinhoodMCPBroker._map_position(rec, instruments)
    assert pos is not None
    assert pos.credit_received == 0.20          # 20.0 per-contract / 100 = 0.20 per-share (NOT 20.0)
    assert pos.open_avg_price == 0.20
    assert pos.strike == 7.5 and pos.quantity == 1 and pos.underlying == "ONDS"


def test_map_position_defaults_multiplier_to_100():
    oid = "opt-x"
    rec = {"option_id": oid, "quantity": "2", "type": "short", "average_price": "35.0"}  # no mult
    instruments = {oid: {"chain_symbol": "F", "strike_price": "13", "type": "put",
                         "expiration_date": "2026-08-15"}}
    pos = RobinhoodMCPBroker._map_position(rec, instruments)
    assert pos is not None and pos.credit_received == 0.35   # 35.0 / 100


@pytest.mark.asyncio
async def test_oauth_sessions_serialized_and_provider_reused(monkeypatch):
    """Concurrent broker calls must serialize onto ONE shared OAuth provider so they never race
    the refresh-token rotation (the cause of the recurring manual re-logins)."""
    import asyncio
    from contextlib import asynccontextmanager

    import agentic.brokers.rh_oauth as oauth

    active = {"n": 0, "max": 0}
    builds = {"n": 0}

    def fake_build_provider(url, storage, **kw):
        builds["n"] += 1
        return object()

    monkeypatch.setattr(oauth, "build_provider", fake_build_provider)
    monkeypatch.setattr(oauth, "FileTokenStorage", lambda *a, **k: object())

    @asynccontextmanager
    async def fake_http(url, **kw):
        yield ("r", "w", None)

    class FakeSession:
        def __init__(self, r, w): ...
        async def __aenter__(self):
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            return self
        async def __aexit__(self, *a):
            active["n"] -= 1
        async def initialize(self): ...
        async def call_tool(self, name, arguments=None):
            await asyncio.sleep(0)  # a real race would interleave here if sessions weren't serialized
            return type("R", (), {"isError": False, "structuredContent": {"ok": name}, "content": []})()

    monkeypatch.setattr("mcp.client.streamable_http.streamablehttp_client", fake_http)
    monkeypatch.setattr("mcp.ClientSession", FakeSession)

    b = RobinhoodMCPBroker(account_number="acct")
    b._token = None  # force the OAuth path

    results = await asyncio.gather(*[b._call_tool("t", {}) for _ in range(5)])

    assert active["max"] == 1          # never two OAuth sessions open at once (serialized)
    assert builds["n"] == 1            # provider built once and reused across all calls
    assert all(r == {"ok": "t"} for r in results)


# --- order fill-tracking: capture broker_order_id + fill state from the real RH shapes ----------
# RH's place_option_order response doesn't reliably carry the created order's id, so submits were
# stuck PENDING with broker_order_id=None -> the fill poll timed out and marked real fills FAILED.
# These cover the real get_option_orders record shape (verified live 2026-08-14) and the confirm
# fallback.

from agentic.domain.enums import OrderStatus  # noqa: E402


def _order(**kw):
    base = dict(decision_id="d", position_id="p", occ_symbol="SMR260821P00009000",
                quantity=1, limit_price=0.14, is_paper=False, option_id="opt9",
                client_order_id="close-1")
    base.update(kw)
    return Order(**base)


def _rh_order(oid, effect, *, state="filled", created="2026-08-13T14:23:29Z", option_id="opt9"):
    return {"id": oid, "state": state, "price": "0.14000000", "processed_quantity": "1.00000",
            "pending_quantity": "0.00000", "chain_symbol": "SMR", "created_at": created,
            "legs": [{"option_id": option_id, "side": "buy", "position_effect": effect}]}


def test_apply_status_reads_processed_quantity_and_state():
    out = _broker()._apply_order_status(_order(), _rh_order("6a7dd361", "close"))
    assert out.broker_order_id == "6a7dd361"
    assert out.status == OrderStatus.FILLED
    assert out.filled_qty == 1            # from processed_quantity, not filled_quantity
    assert out.avg_fill_price == 0.14


def test_match_order_record_by_id():
    raw = {"data": {"results": [{"id": "a"}, {"id": "b"}]}}
    assert RobinhoodMCPBroker._match_order_record(raw, "b") == {"id": "b"}
    assert RobinhoodMCPBroker._match_order_record(raw, "zzz") == {}


@pytest.mark.asyncio
async def test_resolve_placed_order_picks_most_recent_matching_effect(monkeypatch):
    b = _broker()
    b._roles = {"get_option_order": "get_option_orders"}
    orders = {"data": {"results": [
        _rh_order("old-close", "close", created="2026-08-01T10:00:00Z"),
        _rh_order("new-close", "close", created="2026-08-13T14:23:29Z"),   # ours (most recent)
        _rh_order("an-open", "open", created="2026-08-13T15:00:00Z"),       # wrong effect
        _rh_order("other-name", "close", created="2026-08-13T16:00:00Z", option_id="DIFF"),
    ]}}

    async def fake(name, args):
        return orders

    monkeypatch.setattr(b, "_call_tool", fake)
    rec = await b._resolve_placed_order(_order(), position_effect="close")
    assert rec["id"] == "new-close"


@pytest.mark.asyncio
async def test_submit_close_confirms_when_place_response_lacks_id(monkeypatch):
    b = _broker()
    b._supports_options = True
    b._roles = {"place_option_order": "place_option_order", "get_option_order": "get_option_orders"}
    orders = {"data": {"results": [_rh_order("6a7dd361", "close")]}}

    async def fake(name, args):
        if name == "place_option_order":
            return {"data": {}}          # RH place response with no usable id
        return orders                    # get_option_orders list -> confirm here

    monkeypatch.setattr(b, "_call_tool", fake)
    out = await b.submit_close_order(_order())
    assert out.broker_order_id == "6a7dd361"    # recovered via confirmation, not left None
    assert out.status == OrderStatus.FILLED
    assert out.filled_qty == 1


@pytest.mark.asyncio
async def test_get_order_matches_by_id_from_list(monkeypatch):
    b = _broker()
    b._roles = {"get_option_order": "get_option_orders"}
    orders = {"data": {"results": [
        {"id": "aaa", "state": "unconfirmed", "processed_quantity": "0.00000"},
        _rh_order("bbb", "close"),
    ]}}

    async def fake(name, args):
        return orders

    monkeypatch.setattr(b, "_call_tool", fake)
    out = await b.get_order(_order(broker_order_id="bbb"))
    assert out.status == OrderStatus.FILLED and out.filled_qty == 1


# --- multi-account reads (advisory calculator; read-only) ----------------------------------------

@pytest.mark.asyncio
async def test_list_accounts_parses_nested_accounts(monkeypatch):
    b = _broker()
    b._connected = True
    b._tools = ["get_accounts", "get_portfolio"]

    async def fake(name, args):
        assert name == "get_accounts"
        return {"data": {"accounts": [
            {"account_number": "A1", "agentic_allowed": True, "brokerage_account_type": "individual"},
            {"account_number": "A2", "agentic_allowed": False, "brokerage_account_type": "ira_roth"},
        ]}}

    monkeypatch.setattr(b, "_call_tool", fake)
    accts = await b.list_accounts()
    assert [a["account_number"] for a in accts] == ["A1", "A2"]
    assert accts[0]["agentic_allowed"] is True


@pytest.mark.asyncio
async def test_list_accounts_failopen_when_tool_absent():
    b = _broker(account_number="1234567890")
    b._connected = True
    b._tools = ["get_portfolio"]                 # no get_accounts tool
    assert await b.list_accounts() == [{"account_number": "1234567890"}]


@pytest.mark.asyncio
async def test_reads_use_account_override(monkeypatch):
    b = _broker()                                 # configured 1234567890
    b._roles = {"equity_positions": "get_equity_positions", "get_portfolio": "get_portfolio"}
    seen = []

    async def fake(name, args):
        seen.append((name, args.get("account_number")))
        return {"data": {"results": [], "buying_power": "0"}}

    monkeypatch.setattr(b, "_call_tool", fake)
    await b.get_equity_positions("OTHER1")
    await b.get_buying_power("OTHER2")
    assert ("get_equity_positions", "OTHER1") in seen
    assert ("get_portfolio", "OTHER2") in seen
