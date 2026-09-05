"""Robinhood agentic MCP broker (primary execution path).

The app acts as an MCP *client* against https://agent.robinhood.com/mcp/trading.

Per Robinhood's "Trading with your agent" docs the agent can place **long equities and
options orders** and exposes option tools to *load chains/contracts, get quotes, and
view/review/place/cancel options orders*. This adapter targets those tools. The exact
tool *names* and request/response *shapes* are not pinned in public docs, so:

  * ``connect()`` calls ``list_tools()`` once and resolves a small set of logical
    **roles** (list_positions, place_option_order, get_option_order, cancel_option_order,
    review_option_order, option_instruments) to concrete tool names via substring
    heuristics — this is Task #1.
  * The mapping/parsing seams below were pinned against the live RH MCP tool schemas
    (probed 2026-06-18). Confirmed real shapes: every options tool requires
    ``account_number``; orders are placed as ``legs:[{option_id, side, position_effect}]``
    with ``type``/``price`` (strings) and a UUID ``ref_id`` idempotency key; responses are
    wrapped ``{"data": {...}, "guide": ...}``. Two spots remain inference until a real RH
    short position / placed order can be observed (paper-soak uses the paper broker, not
    this path) and are marked ``TODO(verify-live)``: the exact ``get_option_positions``
    *record* field names, and the placed-order response state fields.

Session model: we open a fresh MCP session **per call** rather than holding one open for
the process lifetime. Broker calls are infrequent (one ``get_open_positions`` per ~60s
cycle; order calls are rare), so per-call sessions avoid stale-connection bugs at a
negligible latency cost. Under OAuth the sessions share ONE provider and are serialized by a
lock — Robinhood rotates the refresh token on each use, so concurrent refreshes would
invalidate each other and force a manual re-login. Quotes come from the market-data provider,
not the broker.

Requires the 'mcp' extra (``pip install -e .[mcp]``) and ROBINHOOD_MCP_TOKEN.

IMPORTANT — closing shorts: this platform only ever *closes* short premium (covered
calls / cash-secured puts). Closing a short = **buy-to-close**: a BUY order carrying a
``close`` position-effect. The docs' "long options orders" wording refers to buy-side
orders, which buy-to-close is; the ``review_option_order`` (dry-run) tool is the
definitive check that a close on a short is accepted before we ever arm live.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import Any

from ..config import get_secret
from ..domain.enums import Direction, OptionType, PositionStatus, Strategy
from ..domain.models import EquityHolding, Order, Position
from .base import BrokerCapabilities, ExecutionBroker

log = logging.getLogger("agentic.brokers.rh_mcp")

MCP_URL = "https://agent.robinhood.com/mcp/trading"

# Logical role -> ordered substring hints used to resolve a concrete tool name from the
# probe. First tool whose lower-cased name contains ALL hints in any one tuple wins.
# Multiple tuples per role = alternative naming schemes to try in priority order.
_ROLE_HINTS: dict[str, tuple[tuple[str, ...], ...]] = {
    "list_positions": (("option", "position"), ("options", "position"), ("position",)),
    "place_option_order": (("option", "order", "place"), ("place", "option", "order"),
                           ("option", "order")),
    "review_option_order": (("option", "order", "review"), ("review", "option"),
                            ("option", "simulate"), ("option", "preview")),
    "get_option_order": (("option", "order", "get"), ("get", "option", "order"),
                         ("option", "order", "status")),
    "cancel_option_order": (("option", "order", "cancel"), ("cancel", "option", "order")),
    "option_chain": (("option", "chain"),),
    "option_instruments": (("option", "instrument"),),
    "get_portfolio": (("portfolio",),),
    "equity_positions": (("equity", "position"),),
}

# UUIDv5 namespace for deriving a stable RH ``ref_id`` from our (non-UUID) client_order_id.
_REF_ID_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")


class RobinhoodMCPBroker(ExecutionBroker):
    def __init__(self, url: str = MCP_URL, account_number: str = ""):
        self.url = url
        self._account_number = account_number
        self._token = get_secret("ROBINHOOD_MCP_TOKEN")
        self._tools: list[str] = []
        self._tool_defs: dict[str, dict] = {}  # tool name -> {description, input_schema} (diagnostic)
        self._roles: dict[str, str] = {}     # logical role -> resolved tool name
        self._supports_options = False
        self._connected = False
        # OAuth: ONE shared provider + a lock that serializes MCP sessions. Robinhood rotates the
        # refresh token on every use, so two concurrent loops (monitor/reconcile/scanner) refreshing
        # at once invalidate each other's token and drop to a full interactive re-auth — the cause of
        # the recurring manual re-logins. Serializing sessions keeps the rotated token consistent and
        # persisted between calls, so refresh stays headless.
        self._provider: Any = None
        self._session_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    async def connect(self) -> None:
        """Open an MCP session, probe available tools, and resolve tool roles (Task #1)."""
        if not self._sdk_available():
            log.warning("mcp SDK not installed; install the 'mcp' extra. RH MCP unavailable.")
            return
        from .rh_oauth import maybe_seed_oauth_from_env, oauth_available
        if maybe_seed_oauth_from_env():
            log.info("Seeded data/rh_oauth.json from RH_OAUTH_JSON env (first boot).")
        if not self._token and not oauth_available():
            log.warning("No Robinhood auth (set ROBINHOOD_MCP_TOKEN, or run rh_login to create "
                        "data/rh_oauth.json); cannot connect to Robinhood MCP.")
            return

        try:
            async with self._session() as session:
                result = await session.list_tools()
                self._tools = [t.name for t in result.tools]
                self._tool_defs = {
                    t.name: {"description": getattr(t, "description", None),
                             "input_schema": getattr(t, "inputSchema", None)}
                    for t in result.tools
                }
        except Exception as exc:  # noqa: BLE001 — never crash startup on a broker probe
            log.warning("Robinhood MCP connect/list_tools failed: %s", exc)
            return

        self._connected = True
        self._roles = self._resolve_roles(self._tools)
        # We can place option closes only if an account is configured AND both a place and a
        # positions tool resolved. Without account_number every RH options tool 400s, so this
        # also doubles as a live-arming safety gate.
        self._supports_options = bool(
            self._account_number
            and self._roles.get("place_option_order")
            and self._roles.get("list_positions")
        )
        log.info(
            "Robinhood MCP connected: %d tools, options_orders=%s",
            len(self._tools), self._supports_options,
        )
        log.info("Robinhood MCP tools: %s", ", ".join(sorted(self._tools)) or "(none)")
        log.info("Resolved tool roles: %s", self._roles or "(none)")

    def capabilities(self) -> BrokerCapabilities:
        if not self._connected:
            note = "Not connected (token/SDK missing or probe failed)"
        elif not self._account_number:
            note = "Connected, but robinhood.account_number is unset — read-only, use fallback."
        elif not self._supports_options:
            missing = [r for r in ("place_option_order", "list_positions") if r not in self._roles]
            note = f"Connected; option tools NOT fully resolved (missing {missing}) — use fallback."
        else:
            note = f"Connected; option order tools resolved: {self._roles}"
        return BrokerCapabilities(
            name="robinhood_mcp",
            supports_options_orders=self._supports_options,
            is_paper=False,
            notes=note,
        )

    # ------------------------------------------------------------------ reads
    async def get_open_positions(self, account_number: str | None = None) -> list[Position]:
        if not self._connected:
            return []
        role = self._roles.get("list_positions")
        if not role:
            log.debug("No list_positions tool resolved; returning [].")
            return []
        raw = await self._call_tool(role, self._list_positions_args(account_number))
        records = self._iter_records(raw)
        # get_option_positions records carry option_id but NOT strike / call-put — enrich
        # those from get_option_instruments (one batched lookup by id).
        option_ids = [str(r["option_id"]) for r in records if r.get("option_id")]
        instruments = await self._fetch_instruments(option_ids)
        positions: list[Position] = []
        for item in records:
            try:
                pos = self._map_position(item, instruments)
            except Exception as exc:  # noqa: BLE001 — skip a malformed row, not the cycle
                log.warning("Skipping unmappable position record: %s (%s)", item, exc)
                continue
            if pos is not None:
                positions.append(pos)
        return positions

    async def get_order(self, order: Order) -> Order:
        role = self._roles.get("get_option_order")
        if not role or not order.broker_order_id:
            return order
        # get_option_orders is a LIST tool; fetch recent orders (a per-id filter isn't confirmed
        # supported) and match our order by id, so the fill poll reads the RIGHT order's state.
        raw = await self._call_tool(role, {"account_number": self._account_number})
        rec = self._match_order_record(raw, order.broker_order_id)
        return self._apply_order_status(order, rec) if rec else order

    # ------------------------------------------------------------------ writes
    async def submit_close_order(self, order: Order) -> Order:
        """Submit a buy-to-close option order. Idempotent on ``order.client_order_id``.

        We pass ``client_order_id`` through to the broker so a retried submit with the same
        key does not create a duplicate fill (the broker dedups). If the broker has no such
        field, the executor's persist-before-submit + get_order reconciliation is the
        backstop.
        """
        if not self._supports_options:
            raise RuntimeError(
                "Robinhood MCP did not expose option order tools; configure broker_fallback."
            )
        return await self._place_and_confirm(
            order, self._build_close_order_args(order), position_effect="close")

    async def review_close_order(self, order: Order) -> dict[str, Any]:
        """Dry-run a close via the review/simulate tool. Used to confirm buy-to-close on a
        short is accepted *before* arming live (no order is placed). Returns the raw review
        payload for inspection/audit; raises if no review tool resolved."""
        role = self._roles.get("review_option_order")
        if not role:
            raise RuntimeError("No option-order review/simulate tool resolved on this account.")
        return await self._call_tool(role, self._build_close_order_args(order))

    async def get_buying_power(self, account_number: str | None = None) -> float:
        """Spendable cash/buying power for new CSPs, via the get_portfolio tool.

        Verified 2026-07-17 against a live account: RH nests the authoritative figure at
        ``buying_power.buying_power`` (a string), with top-level ``cash`` equal to it on a cash
        account. Read the nested field first, then fall back to a scalar / cash for other shapes.
        ``account_number`` overrides the configured account (read-only multi-account support).
        """
        role = self._roles.get("get_portfolio")
        if not role:
            return 0.0
        acct = account_number or self._account_number
        args: dict[str, Any] = {"account_number": acct} if acct else {}
        raw = await self._call_tool(role, args)
        rec = self._first_record(raw)
        bp = rec.get("buying_power")
        if isinstance(bp, dict):  # nested: {"buying_power": "1500.0000", ...}
            for k in ("buying_power", "unleveraged_buying_power"):
                v = bp.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
        for k in ("buying_power", "options_buying_power", "withdrawable_amount", "cash",
                  "portfolio_cash", "cash_available_for_withdrawal"):
            v = rec.get(k)
            if v is None or isinstance(v, dict):
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return 0.0

    async def get_equity_positions(self, account_number: str | None = None) -> list[EquityHolding]:
        """Long share positions for covered calls + assignment detection. ``account_number``
        overrides the configured account (read-only multi-account support).

        TODO(verify-live): confirm field names against a real get_equity_positions payload
        (RH conventionally: symbol, quantity, average_buy_price)."""
        role = self._roles.get("equity_positions")
        if not role:
            return []
        acct = account_number or self._account_number
        args: dict[str, Any] = {"account_number": acct} if acct else {}
        raw = await self._call_tool(role, args)
        out: list[EquityHolding] = []
        for rec in self._iter_records(raw):
            sym = rec.get("symbol") or rec.get("ticker") or rec.get("chain_symbol")
            qty_raw = rec.get("quantity") or rec.get("shares") or 0
            cost = (rec.get("average_buy_price") or rec.get("average_cost")
                    or rec.get("avg_cost") or rec.get("cost_basis") or 0)
            try:
                qty = int(float(qty_raw))
            except (TypeError, ValueError):
                continue
            if not sym or qty <= 0:
                continue
            try:
                avg = float(cost)
            except (TypeError, ValueError):
                avg = 0.0
            out.append(EquityHolding(symbol=str(sym).upper(), quantity=qty, average_cost=avg))
        return out

    async def get_account_value(self, account_number: str | None = None) -> float:
        """Total account/portfolio value, for per-name % sizing. Falls back to buying power.
        ``account_number`` overrides the configured account (read-only multi-account support)."""
        val = await self._portfolio_field_async(
            ("total_value", "equity", "total_equity", "market_value", "portfolio_equity",
             "total_market_value"), account_number)
        return val if val > 0 else await self.get_buying_power(account_number)

    async def _portfolio_field_async(self, keys: tuple[str, ...],
                                     account_number: str | None = None) -> float:
        role = self._roles.get("get_portfolio")
        if not role:
            return 0.0
        acct = account_number or self._account_number
        args: dict[str, Any] = {"account_number": acct} if acct else {}
        raw = await self._call_tool(role, args)
        rec = self._first_record(raw)
        for k in keys:
            v = rec.get(k)
            if v is None or isinstance(v, dict):
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return 0.0

    async def _portfolio_field(self, keys: tuple[str, ...]) -> float:
        return await self._portfolio_field_async(keys)

    async def list_accounts(self) -> list[dict[str, Any]]:
        """All Robinhood accounts on the login (READ-ONLY, for the advisory calculator only — never
        traded). Returns the raw per-account dicts (account_number, brokerage_account_type, nickname,
        agentic_allowed, option_level, type). The get_accounts tool is account-agnostic and isn't in
        the role map; its payload nests as ``{"data": {"accounts": [...]}}`` which ``_iter_records``
        doesn't unwrap, so index it directly. Fail-open to just the configured account."""
        fallback = [{"account_number": self._account_number}] if self._account_number else []
        if not self._connected or "get_accounts" not in self._tools:
            return fallback
        try:
            raw = await self._call_tool("get_accounts", {})
        except Exception as exc:  # noqa: BLE001 — advisory read; never raise
            log.warning("list_accounts failed: %s", exc)
            return fallback
        data = raw.get("data", raw) if isinstance(raw, dict) else {}
        accts = data.get("accounts") if isinstance(data, dict) else None
        return [a for a in (accts or []) if isinstance(a, dict)] or fallback

    async def resolve_option_id(self, occ_symbol: str) -> str | None:
        """Resolve an OCC symbol to the RH option-instrument UUID via get_option_instruments."""
        from ..marketdata.alpaca_md import parse_occ_symbol
        role = self._roles.get("option_instruments")
        parsed = parse_occ_symbol(occ_symbol)
        if not role or parsed is None:
            return None
        underlying, exp, opt_type, strike = parsed
        raw = await self._call_tool(role, {
            "chain_symbol": underlying,
            "expiration_dates": exp.isoformat(),
            "strike_price": f"{strike:.4f}",
            "type": opt_type,
            "tradability": "tradable",
        })
        rec = self._first_record(raw)
        return str(rec.get("id")) if rec.get("id") else None

    async def submit_open_order(self, order: Order) -> Order:
        """Submit a sell-to-open (CSP). Idempotent on order.client_order_id via ref_id."""
        if not self._supports_options:
            raise RuntimeError(
                "Robinhood MCP did not expose option order tools; configure broker_fallback."
            )
        return await self._place_and_confirm(
            order, self._build_open_order_args(order), position_effect="open")

    async def review_open_order(self, order: Order) -> dict[str, Any]:
        """Dry-run a sell-to-open via the review tool (no order placed)."""
        role = self._roles.get("review_option_order")
        if not role:
            raise RuntimeError("No option-order review/simulate tool resolved on this account.")
        return await self._call_tool(role, self._build_open_order_args(order))

    def _build_open_order_args(self, order: Order) -> dict[str, Any]:
        """Build a sell-to-open request: a SHORT (sell) put with position_effect=open, as a
        limit order collecting a credit. Same single-leg schema as close, side/effect flipped."""
        if not order.option_id:
            raise RuntimeError(
                "Order has no option_id; cannot build an RH open order (resolve the contract's "
                "instrument UUID before submitting)."
            )
        return {
            "account_number": self._account_number,
            "legs": [{
                "option_id": order.option_id,
                "side": "sell",                # sell-to-open a short put
                "position_effect": "open",
            }],
            "quantity": str(order.quantity),
            "type": "limit",
            "price": f"{order.limit_price:.2f}",
            "time_in_force": "gfd",
            "ref_id": self._ref_id(order),
        }

    async def cancel_order(self, order: Order) -> None:
        role = self._roles.get("cancel_option_order")
        if not role or not order.broker_order_id:
            return
        await self._call_tool(
            role, {"account_number": self._account_number, "order_id": order.broker_order_id}
        )

    # ------------------------------------------------------------------ MCP plumbing
    @staticmethod
    def _sdk_available() -> bool:
        try:
            import mcp  # noqa: F401
            return True
        except ImportError:
            return False

    @asynccontextmanager
    async def _session(self):
        """Yield an initialized MCP ClientSession against the trading endpoint.

        Auth: a static ROBINHOOD_MCP_TOKEN bearer (override, rare) if set; otherwise a SINGLE
        shared OAuth provider backed by data/rh_oauth.json. OAuth sessions are serialized by
        ``_session_lock`` so concurrent loops never race the refresh-token rotation — a parallel
        refresh would invalidate the token and force a manual re-login. Calls are short and
        infrequent, so one-at-a-time is a negligible cost for headless-refresh stability.
        """
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        if self._token:
            headers = {"Authorization": f"Bearer {self._token}"}
            async with streamablehttp_client(self.url, headers=headers) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            from .rh_oauth import FileTokenStorage, build_provider
            if self._provider is None:
                self._provider = build_provider(self.url, FileTokenStorage())
            async with self._session_lock:
                async with streamablehttp_client(self.url, auth=self._provider) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call one MCP tool and return its parsed structured content (or text)."""
        async with self._session() as session:
            result = await session.call_tool(name, arguments=arguments)
        if getattr(result, "isError", False):
            raise RuntimeError(f"MCP tool {name} returned an error: {result}")
        # Prefer structured content; fall back to concatenated text blocks.
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            return structured
        texts = [getattr(c, "text", "") for c in getattr(result, "content", [])]
        return "".join(texts)

    @staticmethod
    def _resolve_roles(tools: list[str]) -> dict[str, str]:
        lowered = {t.lower(): t for t in tools}
        roles: dict[str, str] = {}
        for role, hint_sets in _ROLE_HINTS.items():
            for hints in hint_sets:
                match = next(
                    (orig for low, orig in lowered.items() if all(h in low for h in hints)),
                    None,
                )
                if match:
                    roles[role] = match
                    break
        return roles

    # ------------------------------------------------------------------ mapping seams
    # Shapes pinned against the live RH MCP schemas (2026-06-18); see module docstring.
    def _list_positions_args(self, account_number: str | None = None) -> dict[str, Any]:
        # get_option_positions requires account_number; nonzero=true → open only,
        # type=short → just the premium we sold (covered calls / cash-secured puts).
        return {"account_number": account_number or self._account_number,
                "nonzero": True, "type": "short"}

    @staticmethod
    def _iter_records(raw: Any) -> list[dict[str, Any]]:
        """Normalize a tool result into a list of record dicts.

        RH MCP wraps payloads as ``{"data": {...}, "guide": ...}`` — unwrap ``data`` first,
        then pull the inner collection (positions / orders / instruments / results / items).
        """
        if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
            raw = raw["data"]
        if isinstance(raw, list):
            return [r for r in raw if isinstance(r, dict)]
        if isinstance(raw, dict):
            for key in ("results", "positions", "orders", "instruments", "items"):
                val = raw.get(key)
                if isinstance(val, list):
                    return [r for r in val if isinstance(r, dict)]
            return [raw]
        return []

    @classmethod
    def _first_record(cls, raw: Any) -> dict[str, Any]:
        """First record dict from a (possibly wrapped/listed) tool result, else ``{}``."""
        recs = cls._iter_records(raw)
        return recs[0] if recs else {}

    @classmethod
    def _match_order_record(cls, raw: Any, order_id: str) -> dict[str, Any]:
        """The order record whose ``id`` equals ``order_id`` from a (list) tool result, else ``{}``."""
        for rec in cls._iter_records(raw):
            if str(rec.get("id")) == str(order_id):
                return rec
        return {}

    @staticmethod
    def _order_created_key(rec: dict[str, Any]) -> str:
        """Sort key for 'most recently created order' — RH created_at is a fixed-format ISO ts,
        so a lexicographic compare orders them correctly."""
        return str(rec.get("created_at") or rec.get("updated_at") or "")

    async def _place_and_confirm(
        self, order: Order, args: dict[str, Any], *, position_effect: str
    ) -> Order:
        """Place an option order, then ensure the returned Order carries a broker id + state.

        RH's ``place_option_order`` response does not reliably surface the created order's id/state,
        which left our orders stuck at PENDING with ``broker_order_id=None`` — so the executor's
        fill poll had nothing to track, timed out, and marked genuine fills FAILED (while RH filled
        the order and reconcile booked an estimated P&L). When the place response lacks an id, we
        confirm the order from RH's recent order list.
        """
        role = self._roles["place_option_order"]
        raw = await self._call_tool(role, args)
        result = self._apply_order_status(order, self._first_record(raw))
        if not result.broker_order_id:
            rec = await self._resolve_placed_order(result, position_effect=position_effect)
            if rec:
                result = self._apply_order_status(result, rec)
        return result

    async def _resolve_placed_order(
        self, order: Order, *, position_effect: str
    ) -> dict[str, Any]:
        """Find the just-placed order in RH's recent order list (get_option_orders) when the place
        response didn't carry a usable id. Matches the MOST RECENT order whose leg is our
        ``option_id`` with the given ``position_effect`` (open/close). RH doesn't echo our ref_id,
        so we match on option_id + effect + recency — safe because closes are serialized and we hold
        one CSP per underlying. Best-effort: returns ``{}`` (caller keeps the order) on any failure.
        """
        role = self._roles.get("get_option_order")
        if not role or not order.option_id:
            return {}
        try:
            raw = await self._call_tool(role, {"account_number": self._account_number})
        except Exception as exc:  # noqa: BLE001 — confirmation is best-effort
            log.warning("Post-submit order confirmation failed for %s: %s", order.occ_symbol, exc)
            return {}
        best: dict[str, Any] = {}
        best_key = ""
        for rec in self._iter_records(raw):
            legs = rec.get("legs") or []
            matched = any(
                isinstance(leg, dict)
                and str(leg.get("option_id")) == str(order.option_id)
                and str(leg.get("position_effect", "")).lower() == position_effect
                for leg in legs
            )
            if not matched:
                continue
            key = self._order_created_key(rec)
            if not best or key > best_key:
                best, best_key = rec, key
        if best:
            log.info("Confirmed %s order %s via get_option_orders (state=%s).",
                     position_effect, best.get("id"), best.get("state"))
        return best

    async def _fetch_instruments(self, option_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Map option_id -> instrument record (strike_price, type, chain_symbol, expiration).

        get_option_instruments accepts a comma-separated ``ids`` filter; returns ``{}`` if no
        instruments tool resolved (positions then fall back to any in-record fields)."""
        role = self._roles.get("option_instruments")
        if not role or not option_ids:
            return {}
        raw = await self._call_tool(role, {"ids": ",".join(option_ids)})
        out: dict[str, dict[str, Any]] = {}
        for rec in self._iter_records(raw):
            iid = rec.get("id")
            if iid:
                out[str(iid)] = rec
        return out

    @staticmethod
    def _ref_id(order: Order) -> str:
        """Stable UUID idempotency key. Our client_order_id (``close-<hex>``) isn't a UUID,
        so derive a deterministic UUIDv5 from it — same logical order ⇒ same ref_id on retry."""
        return str(uuid.uuid5(_REF_ID_NS, order.client_order_id))

    @staticmethod
    def _build_occ_symbol(
        underlying: str, expiration: date, option_type: OptionType, strike: float
    ) -> str:
        """Construct the OCC option symbol (RH instrument records don't include it)."""
        cp = "C" if option_type is OptionType.CALL else "P"
        return f"{underlying}{expiration:%y%m%d}{cp}{int(round(strike * 1000)):08d}"

    @staticmethod
    def _map_position(
        rec: dict[str, Any], instruments: dict[str, dict[str, Any]]
    ) -> Position | None:
        """Map a get_option_positions record (+ enriched instrument) -> domain Position.

        Only SHORT option positions are relevant (we close premium we sold). The positions
        query already filters type=short, but we re-check defensively. Strike, call/put, and
        the OCC symbol come from the instrument lookup (``instruments`` keyed by option_id);
        chain_symbol/quantity/average_price/expiration come from the position record.

        TODO(verify-live): the position-record field names below (quantity, average_price,
        type, chain_symbol, expiration_date) and the per-share-vs-per-contract scaling of
        average_price are inferred from the tool guide — confirm against a real RH short.
        """
        oid = str(rec.get("option_id") or "")
        inst = instruments.get(oid, {})

        try:
            qty = int(float(rec.get("quantity") or rec.get("qty") or 0))
        except (TypeError, ValueError):
            return None
        if qty == 0:
            return None
        # Short positions: type=short (RH), or negative qty, or an explicit sell side flag.
        side = str(rec.get("type") or rec.get("side") or rec.get("position_type") or "").lower()
        if not (qty < 0 or side in ("short", "sell", "sell_to_open")):
            return None

        opt_type = str(inst.get("type") or rec.get("option_type") or rec.get("type") or "").upper()
        option_type = OptionType.PUT if opt_type.startswith("P") else OptionType.CALL
        strategy = (
            Strategy.COVERED_CALL if option_type is OptionType.CALL else Strategy.CASH_SECURED_PUT
        )
        underlying = str(inst.get("chain_symbol") or rec.get("chain_symbol")
                         or rec.get("underlying") or "").upper()
        strike = float(inst.get("strike_price") or rec.get("strike_price") or rec.get("strike") or 0.0)
        exp_raw = (inst.get("expiration_date") or rec.get("expiration_date")
                   or rec.get("expiration") or rec.get("expiry"))
        expiration = date.fromisoformat(str(exp_raw)[:10]) if exp_raw else None
        credit = (rec.get("average_price") or rec.get("average_open_price")
                  or rec.get("credit_received") or rec.get("avg_price"))

        if expiration is None or credit is None or not underlying or strike <= 0:
            return None
        # VERIFIED LIVE 2026-07-22: RH's average_price is PER-CONTRACT dollars ($0.20/share x 100 =
        # 20.0), not per-share. Scale to per-share by the contract multiplier so credit_received
        # matches the rest of the system (per-share) — else P&L and the profit-target rule are 100x off.
        mult = float(rec.get("trade_value_multiplier") or 100)
        credit_per_share = abs(float(credit)) / (mult if mult > 0 else 100)
        occ = str(rec.get("occ_symbol") or rec.get("symbol")
                  or RobinhoodMCPBroker._build_occ_symbol(underlying, expiration, option_type, strike))
        return Position(
            occ_symbol=occ,
            underlying=underlying,
            option_type=option_type,
            strategy=strategy,
            direction=Direction.SHORT,
            quantity=abs(qty),
            strike=strike,
            expiration=expiration,
            credit_received=credit_per_share,
            open_avg_price=credit_per_share,
            broker_position_id=str(rec.get("id") or rec.get("position_id") or "") or None,
            option_id=oid or None,
            status=PositionStatus.OPEN,
        )

    def _build_close_order_args(self, order: Order) -> dict[str, Any]:
        """Build the place/review request for a buy-to-close (single-leg).

        Closing a SHORT option = BUY with position_effect=close (confirmed by the live
        review dry-run). The order is fully identified by option_id; chain_symbol/
        underlying_type (fee/collateral hints) are omitted intentionally — they are optional
        and we don't carry them on the Order.
        """
        if not order.option_id:
            raise RuntimeError(
                "Order has no option_id; cannot build an RH close order. The position read "
                "must capture option_id (see get_open_positions)."
            )
        return {
            "account_number": self._account_number,
            "legs": [{
                "option_id": order.option_id,
                "side": "buy",              # buy-to-close a short
                "position_effect": "close",
            }],
            "quantity": str(order.quantity),
            "type": "limit",
            "price": f"{order.limit_price:.2f}",
            "time_in_force": "gfd",         # good-for-day
            "ref_id": self._ref_id(order),  # UUID idempotency key
        }

    @staticmethod
    def _apply_order_status(order: Order, raw: Any) -> Order:
        """Merge a broker order response into our Order.

        Field names verified against live RH ``get_option_orders`` (2026-08-14): ``id``, ``state``
        ("filled"/"unconfirmed"/"cancelled"/…), executed qty ``processed_quantity``, price ``price``.
        The ``place_option_order`` response itself does NOT reliably carry the id, so submit paths
        confirm via ``_place_and_confirm`` -> ``_resolve_placed_order`` (see those). Other field
        aliases are kept for non-RH brokers.
        """
        from ..domain.enums import OrderStatus

        rec = raw if isinstance(raw, dict) else {}
        order.broker_order_id = str(
            rec.get("id") or rec.get("order_id") or order.broker_order_id or ""
        ) or None
        broker_state = str(rec.get("state") or rec.get("status") or "").lower()
        _STATE_MAP = {
            "filled": OrderStatus.FILLED,
            "partially_filled": OrderStatus.PARTIAL,
            "partial": OrderStatus.PARTIAL,
            "queued": OrderStatus.SUBMITTED,
            "confirmed": OrderStatus.SUBMITTED,
            "unconfirmed": OrderStatus.SUBMITTED,
            "pending": OrderStatus.SUBMITTED,
            "cancelled": OrderStatus.CANCELLED,
            "canceled": OrderStatus.CANCELLED,
            "rejected": OrderStatus.REJECTED,
            "failed": OrderStatus.REJECTED,
        }
        if broker_state in _STATE_MAP:
            order.status = _STATE_MAP[broker_state]
        elif order.broker_order_id:
            order.status = OrderStatus.SUBMITTED
        # RH reports the executed quantity as ``processed_quantity`` (verified live 2026-08-14);
        # keep filled_quantity/cumulative_quantity as fallbacks for other brokers/shapes.
        filled = (rec.get("processed_quantity") or rec.get("filled_quantity")
                  or rec.get("cumulative_quantity"))
        if filled is not None:
            try:
                order.filled_qty = int(float(filled))
            except (TypeError, ValueError):
                pass
        avg = rec.get("average_price") or rec.get("avg_fill_price") or rec.get("price")
        if avg is not None:
            try:
                order.avg_fill_price = float(avg)
            except (TypeError, ValueError):
                pass
        return order
