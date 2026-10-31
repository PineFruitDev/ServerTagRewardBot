import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { after, before, beforeEach, describe, test } from "node:test";
import { convertV4MiniflareOptions, Log, LogLevel, Miniflare } from "miniflare";

// Synthetic fixture only. Never read wrangler.toml, .dev.vars, or process.env.
const KEY = "local-test-only";
const options = {
  modules: true,
  scriptPath: fileURLToPath(new URL("../src/index.js", import.meta.url)),
  compatibilityDate: "2026-08-01",
  cf: false,
  log: new Log(LogLevel.NONE),
  outboundService: () => new Response("Outbound requests disabled in tests", { status: 503 }),
};

function localWorker(overrides = {}) {
  return new Miniflare({
    ...convertV4MiniflareOptions({ ...options, ...overrides }),
    telemetry: { enabled: false },
    enableDevRegistry: false,
  });
}

describe("tagbot-store HTTP contract with local D1", { concurrency: false }, () => {
  let worker, db;

  before(async () => {
    worker = localWorker({
      bindings: { TAGBOT_STORE_KEY: KEY },
      d1Databases: ["DB"],
      d1Persist: false,
    });
    db = await worker.getD1Database("DB");
    const migration = await readFile(new URL("../migrations/0001_initial.sql", import.meta.url), "utf8");
    await db.batch(migration.split(";").filter((sql) => sql.trim()).map((sql) => db.prepare(sql)));
  });

  beforeEach(async () => {
    await db.batch([db.prepare("DELETE FROM guild_config"), db.prepare("DELETE FROM tag_streaks")]);
  });

  after(async () => { await worker?.dispose(); });

  async function request(path, { method = "GET", body, authorization = `Bearer ${KEY}`, rawBody } = {}) {
    const response = await worker.dispatchFetch(`https://store.test${path}`, {
      method,
      headers: {
        ...(authorization === null ? {} : { Authorization: authorization }),
        "Content-Type": "application/json",
      },
      body: rawBody ?? (body === undefined ? undefined : JSON.stringify(body)),
    });
    assert.equal(response.headers.get("content-type"), "application/json");
    assert.equal(response.headers.get("cache-control"), "no-store");
    return { status: response.status, body: await response.json() };
  }

  async function ok(path, opts) {
    const response = await request(path, opts);
    assert.equal(response.status, 200, JSON.stringify(response.body));
    return response.body;
  }

  async function mark(guild, now, marks) {
    return ok(`/streaks/${guild}/mark`, {
      method: "POST", body: { now, marks: marks.map(([user_id, repping]) => ({ user_id, repping })) },
    });
  }

  async function row(guild = "1", uid = "42") {
    return (await ok(`/streaks/${guild}/${uid}`)).row;
  }

  test("health is authenticated and responses cannot be cached", async () => {
    assert.deepEqual(await ok("/health"), { ok: true });
  });

  for (const [name, authorization] of [
    ["missing", null], ["empty", ""], ["wrong scheme", "Basic test"],
    ["empty bearer", "Bearer "], ["wrong key", "Bearer incorrect"],
  ]) {
    test(`rejects ${name} authorization without writing`, async () => {
      assert.deepEqual(await request("/guilds/1", {
        method: "PUT", body: { role_id: "10" }, authorization,
      }), { status: 401, body: { error: "unauthorized" } });
      assert.deepEqual(await ok("/guilds"), { guilds: [] });
    });
  }

  test("missing configuration and unauthorized access never require a DB binding", async () => {
    const unconfigured = localWorker();
    const noDB = localWorker({ bindings: { TAGBOT_STORE_KEY: KEY } });
    try {
      const missing = await unconfigured.dispatchFetch("https://store.test/health");
      assert.equal(missing.status, 503);
      assert.deepEqual(await missing.json(), { error: "store not configured" });
      const denied = await noDB.dispatchFetch("https://store.test/guilds");
      assert.equal(denied.status, 401);
      const failed = await noDB.dispatchFetch("https://store.test/guilds", {
        headers: { Authorization: `Bearer ${KEY}` },
      });
      assert.equal(failed.status, 500);
      assert.deepEqual(await failed.json(), { error: "internal error" });
    } finally {
      await Promise.all([unconfigured.dispose(), noDB.dispose()]);
    }
  });

  test("guild lifecycle preserves exact IDs, isolates guilds, and re-enables configuration", async () => {
    const gid = "123456789012345678", role = "987654321098765432";
    assert.deepEqual(await ok(`/guilds/${gid}`), { role_id: null });
    assert.deepEqual(await ok(`/guilds/${gid}`, { method: "PUT", body: { role_id: role } }), { ok: true });
    await ok("/guilds/2", { method: "PUT", body: { role_id: "20" } });
    assert.deepEqual(await ok("/guilds"), { guilds: [[gid, role], ["2", "20"]] });
    assert.deepEqual(await ok(`/guilds/${gid}`), { role_id: role });
    assert.deepEqual(await ok(`/guilds/${gid}/disable`, { method: "POST" }), { was_configured: true });
    assert.deepEqual(await ok(`/guilds/${gid}`), { role_id: null });
    assert.deepEqual(await ok("/guilds"), { guilds: [["2", "20"]] });
    assert.deepEqual(await ok("/guilds/99/disable", { method: "POST" }), { was_configured: false });
    await ok(`/guilds/${gid}`, { method: "PUT", body: { role_id: "30" } });
    assert.deepEqual(await ok(`/guilds/${gid}`), { role_id: "30" });
    assert.deepEqual(await ok("/guilds/2"), { role_id: "20" });
  });

  test("safe integer IDs remain supported and large string IDs stay exact", async () => {
    await ok("/guilds/1", { method: "PUT", body: { role_id: 10 } });
    assert.deepEqual(await ok("/guilds/1"), { role_id: "10" });
    await mark("1", 100, [[42, true], ["987654321098765432", true]]);
    assert.equal((await row("1", "42")).user_id, "42");
    assert.equal((await row("1", "987654321098765432")).user_id, "987654321098765432");
  });

  for (const [name, path, opts] of [
    ["invalid guild", "/guilds/nope", {}],
    ["overlong guild", `/guilds/${"1".repeat(21)}`, {}],
    ["invalid user", "/streaks/1/nope", {}],
    ["missing role", "/guilds/1", { method: "PUT", body: {} }],
    ["negative role", "/guilds/1", { method: "PUT", body: { role_id: -1 } }],
    ["unsafe numeric role", "/guilds/1", { method: "PUT", body: { role_id: Number.MAX_SAFE_INTEGER + 1 } }],
    ["broken JSON", "/guilds/1", { method: "PUT", rawBody: "{" }],
    ["null JSON", "/guilds/1", { method: "PUT", body: null }],
    ["scalar JSON", "/guilds/1", { method: "PUT", body: "text" }],
    ["missing marks", "/streaks/1/mark", { method: "POST", body: {} }],
    ["empty marks", "/streaks/1/mark", { method: "POST", body: { marks: [] } }],
    ["non-list marks", "/streaks/1/mark", { method: "POST", body: { marks: {} } }],
    ["too many marks", "/streaks/1/mark", { method: "POST", body: { marks: Array(51).fill({ user_id: "42", repping: true }) } }],
  ]) {
    test(`rejects ${name}`, async () => {
      assert.equal((await request(path, opts)).status, 400);
      assert.deepEqual(await ok("/guilds"), { guilds: [] });
      assert.deepEqual(await ok("/streaks/1"), { rows: [] });
    });
  }

  for (const bad of [null, {}, { user_id: "bad", repping: true },
    { user_id: Number.MAX_SAFE_INTEGER + 1, repping: true },
    { user_id: "43", repping: "true" }, { user_id: "43", repping: 1 }]) {
    test(`invalid mark ${JSON.stringify(bad)} rejects the entire batch`, async () => {
      const result = await request("/streaks/1/mark", {
        method: "POST", body: { now: 100, marks: [{ user_id: "42", repping: true }, bad] },
      });
      assert.equal(result.status, 400);
      assert.deepEqual(await ok("/streaks/1"), { rows: [] });
    });
  }

  test("accepts the maximum 50-mark batch", async () => {
    const marks = Array.from({ length: 50 }, (_, i) => [String(i + 1), true]);
    assert.deepEqual(await mark("1", 100, marks), { ok: true, count: 50 });
    assert.equal((await ok("/streaks/1")).rows.length, 50);
  });

  test("streak transitions are idempotent and preserve totals across re-equipping", async () => {
    assert.equal(await row(), null);
    await mark("1", 90, [["42", false]]);
    assert.equal(await row(), null);
    await mark("1", 100, [["42", true]]);
    await mark("1", 120, [["42", true]]);
    assert.deepEqual(await row(), { user_id: "42", active_since: 100, total_seconds: 0, longest_seconds: 0 });
    await mark("1", 150, [["42", false]]);
    await mark("1", 170, [["42", false]]);
    assert.deepEqual(await row(), { user_id: "42", active_since: null, total_seconds: 50, longest_seconds: 50 });
    await mark("1", 200, [["42", true]]);
    await mark("1", 275, [["42", false]]);
    assert.deepEqual(await row(), { user_id: "42", active_since: null, total_seconds: 125, longest_seconds: 75 });
  });

  test("clock rollback cannot subtract time and users are isolated by guild", async () => {
    await mark("1", 100, [["42", true], ["43", true]]);
    await mark("2", 80, [["42", true]]);
    await mark("1", 90, [["42", false]]);
    assert.deepEqual(await row(), { user_id: "42", active_since: null, total_seconds: 0, longest_seconds: 0 });
    assert.equal((await row("1", "43")).active_since, 100);
    assert.equal((await row("2")).active_since, 80);
    assert.equal(await row("2", "43"), null);
  });

  test("concurrent duplicate starts and stops count each interval once", async () => {
    await Promise.all(Array.from({ length: 5 }, () => mark("1", 100, [["42", true]])));
    await Promise.all(Array.from({ length: 5 }, () => mark("1", 150, [["42", false]])));
    assert.deepEqual(await row(), { user_id: "42", active_since: null, total_seconds: 50, longest_seconds: 50 });
  });

  test("omitted timestamps use the current clock", async () => {
    const before = Math.floor(Date.now() / 1000);
    await ok("/streaks/1/mark", { method: "POST", body: { marks: [{ user_id: "42", repping: true }] } });
    const saved = await row();
    assert.ok(saved.active_since >= before && saved.active_since <= Math.floor(Date.now() / 1000));
  });

  test("unknown paths and unsupported methods return 404", async () => {
    for (const [path, method] of [["/unknown", "GET"], ["/health", "POST"], ["/guilds/1", "DELETE"]]) {
      assert.deepEqual(await request(path, { method }), { status: 404, body: { error: "not found" } });
    }
  });
});
