#!/usr/bin/env node
// MCP server for the Delta Exchange REST API (v2).
//
// Env vars:
//   DELTA_API_KEY, DELTA_API_SECRET  - required for account/order tools
//   DELTA_BASE_URL                   - default https://api.india.delta.exchange
//                                      global:  https://api.delta.exchange
//                                      testnet: https://cdn-ind.testnet.deltaex.org
//   DELTA_ENABLE_TRADING=true        - required to expose order placing/cancelling tools

import crypto from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const BASE_URL = (process.env.DELTA_BASE_URL || "https://api.india.delta.exchange").replace(/\/$/, "");
const API_KEY = process.env.DELTA_API_KEY || "";
const API_SECRET = process.env.DELTA_API_SECRET || "";
const TRADING_ENABLED = process.env.DELTA_ENABLE_TRADING === "true";

function buildQuery(params = {}) {
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  if (!entries.length) return "";
  return "?" + new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString();
}

async function deltaRequest(method, path, { query, body, auth = false } = {}) {
  const queryString = buildQuery(query);
  const payload = body ? JSON.stringify(body) : "";
  const headers = { "Content-Type": "application/json", "User-Agent": "delta-exchange-mcp/1.0" };

  if (auth) {
    if (!API_KEY || !API_SECRET) {
      throw new Error("DELTA_API_KEY and DELTA_API_SECRET must be set for this tool.");
    }
    // Signature = HMAC_SHA256(secret, method + timestamp + path + query_string + body)
    const timestamp = Math.floor(Date.now() / 1000).toString();
    const signature = crypto
      .createHmac("sha256", API_SECRET)
      .update(method + timestamp + path + queryString + payload)
      .digest("hex");
    headers["api-key"] = API_KEY;
    headers["timestamp"] = timestamp;
    headers["signature"] = signature;
  }

  const res = await fetch(BASE_URL + path + queryString, {
    method,
    headers,
    body: payload || undefined,
  });
  const text = await res.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    data = text;
  }
  if (!res.ok || (data && data.success === false)) {
    throw new Error(`Delta API ${res.status}: ${typeof data === "string" ? data : JSON.stringify(data)}`);
  }
  return data;
}

function ok(data) {
  return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
}

function fail(err) {
  return { isError: true, content: [{ type: "text", text: String(err?.message || err) }] };
}

function tool(server, name, description, schema, handler) {
  server.tool(name, description, schema, async (args) => {
    try {
      return ok(await handler(args));
    } catch (err) {
      return fail(err);
    }
  });
}

const server = new McpServer({ name: "delta-exchange", version: "1.0.0" });

// ---------- Public market data ----------

tool(server, "get_products", "List tradable products (futures, perpetuals, options) on Delta Exchange.", {
  contract_types: z.string().optional().describe("Comma separated, e.g. perpetual_futures,call_options,put_options"),
  states: z.string().optional().describe("e.g. live,expired"),
}, (q) => deltaRequest("GET", "/v2/products", { query: q }));

tool(server, "get_product", "Get one product by symbol (e.g. BTCUSD).", {
  symbol: z.string(),
}, ({ symbol }) => deltaRequest("GET", `/v2/products/${encodeURIComponent(symbol)}`));

tool(server, "get_tickers", "Get tickers for all products, optionally filtered by contract type.", {
  contract_types: z.string().optional().describe("e.g. perpetual_futures"),
}, (q) => deltaRequest("GET", "/v2/tickers", { query: q }));

tool(server, "get_ticker", "Get the live ticker (price, mark price, funding, OI, volume) for a symbol.", {
  symbol: z.string().describe("e.g. BTCUSD"),
}, ({ symbol }) => deltaRequest("GET", `/v2/tickers/${encodeURIComponent(symbol)}`));

tool(server, "get_orderbook", "Get the L2 orderbook for a symbol.", {
  symbol: z.string(),
  depth: z.number().int().optional(),
}, ({ symbol, depth }) => deltaRequest("GET", `/v2/l2orderbook/${encodeURIComponent(symbol)}`, { query: { depth } }));

tool(server, "get_recent_trades", "Get recent public trades for a symbol.", {
  symbol: z.string(),
}, ({ symbol }) => deltaRequest("GET", `/v2/trades/${encodeURIComponent(symbol)}`));

tool(server, "get_candles", "Get OHLC candles. start/end are unix seconds; defaults to the last 100 candles.", {
  symbol: z.string().describe("e.g. BTCUSD, or MARK:BTCUSD for mark price"),
  resolution: z.enum(["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "1d", "1w"]),
  start: z.number().int().optional(),
  end: z.number().int().optional(),
}, ({ symbol, resolution, start, end }) => {
  const secs = { "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "1d": 86400, "1w": 604800 }[resolution];
  const e = end ?? Math.floor(Date.now() / 1000);
  const s = start ?? e - secs * 100;
  return deltaRequest("GET", "/v2/history/candles", { query: { symbol, resolution, start: s, end: e } });
});

// ---------- Account (authenticated, read-only) ----------

tool(server, "get_wallet_balances", "Get wallet balances for all assets.", {},
  () => deltaRequest("GET", "/v2/wallet/balances", { auth: true }));

tool(server, "get_positions", "Get all open margined positions.", {},
  () => deltaRequest("GET", "/v2/positions/margined", { auth: true }));

tool(server, "get_orders", "List orders (default: open orders).", {
  states: z.string().optional().describe("open,pending,closed,cancelled"),
  product_ids: z.string().optional().describe("Comma separated product ids"),
  page_size: z.number().int().optional(),
}, ({ states = "open", ...rest }) => deltaRequest("GET", "/v2/orders", { query: { states, ...rest }, auth: true }));

tool(server, "get_order_history", "Get closed/cancelled order history.", {
  product_ids: z.string().optional(),
  page_size: z.number().int().optional(),
}, (q) => deltaRequest("GET", "/v2/orders/history", { query: q, auth: true }));

tool(server, "get_fills", "Get trade fills (executions).", {
  product_ids: z.string().optional(),
  page_size: z.number().int().optional(),
}, (q) => deltaRequest("GET", "/v2/fills", { query: q, auth: true }));

tool(server, "get_leverage", "Get the order leverage set for a product.", {
  product_id: z.number().int(),
}, ({ product_id }) => deltaRequest("GET", `/v2/products/${product_id}/orders/leverage`, { auth: true }));

// ---------- Trading (only when DELTA_ENABLE_TRADING=true) ----------

if (TRADING_ENABLED) {
  tool(server, "place_order", "Place an order on Delta Exchange. Uses real funds unless DELTA_BASE_URL points at testnet.", {
    product_symbol: z.string().describe("e.g. BTCUSD"),
    side: z.enum(["buy", "sell"]),
    size: z.number().int().positive().describe("Number of contracts"),
    order_type: z.enum(["limit_order", "market_order"]),
    limit_price: z.string().optional().describe("Required for limit_order"),
    time_in_force: z.enum(["gtc", "ioc", "fok"]).optional(),
    post_only: z.boolean().optional(),
    reduce_only: z.boolean().optional(),
    stop_order_type: z.enum(["stop_loss_order", "take_profit_order"]).optional(),
    stop_price: z.string().optional(),
    client_order_id: z.string().optional(),
  }, (body) => {
    if (body.order_type === "limit_order" && !body.limit_price) {
      throw new Error("limit_price is required for limit_order");
    }
    return deltaRequest("POST", "/v2/orders", { body, auth: true });
  });

  tool(server, "cancel_order", "Cancel an open order.", {
    id: z.number().int(),
    product_id: z.number().int(),
  }, (body) => deltaRequest("DELETE", "/v2/orders", { body, auth: true }));

  tool(server, "cancel_all_orders", "Cancel all open orders, optionally for one product.", {
    product_id: z.number().int().optional(),
  }, (body) => deltaRequest("DELETE", "/v2/orders/all", { body, auth: true }));

  tool(server, "set_leverage", "Set order leverage for a product.", {
    product_id: z.number().int(),
    leverage: z.string().describe("e.g. \"10\""),
  }, ({ product_id, leverage }) =>
    deltaRequest("POST", `/v2/products/${product_id}/orders/leverage`, { body: { leverage }, auth: true }));
}

await server.connect(new StdioServerTransport());
