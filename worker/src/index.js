/**
 * tagbot-store - minimal JSON API over D1 for ServerTagRewardBot.
 *
 * Every request needs `Authorization: Bearer <TAGBOT_STORE_KEY>`.
 * Covers exactly what GuildConfig and StreakStore need:
 *
 *   GET  /health                         -> { ok: true }
 *   GET  /guilds                         -> { guilds: [[guild_id, role_id], ...] }   (enabled only)
 *   GET  /guilds/:gid                    -> { role_id: string | null }               (enabled only)
 *   PUT  /guilds/:gid        {role_id}   -> { ok: true }                             (upsert + enable)
 *   POST /guilds/:gid/disable            -> { was_configured: bool }
 *   GET  /streaks/:gid                   -> { rows: [{user_id, active_since, total_seconds, longest_seconds}] }
 *   GET  /streaks/:gid/:uid              -> { row: {...} | null }
 *   POST /streaks/:gid/mark  {now, marks: [{user_id, repping}]}  -> { ok: true, count }
 *
 * Streak math for "mark" runs in SQL so each transition is atomic (no
 * read-modify-write race between the bot's live events and full syncs).
 */

const SNOWFLAKE = /^\d{1,20}$/;
const MAX_MARKS = 50; // stays within D1's per-invocation query limit on every plan

export default {
  async fetch(request, env) {
    try {
      if (!env.TAGBOT_STORE_KEY) return json({ error: "store not configured" }, 503);
      if (!(await authorized(request, env.TAGBOT_STORE_KEY))) return json({ error: "unauthorized" }, 401);
      return await route(request, env.DB);
    } catch (err) {
      if (err instanceof HttpError) return json({ error: err.message }, err.status);
      console.error("unhandled", err && err.name, err && err.message);
      return json({ error: "internal error" }, 500);
    }
  },
};

async function route(request, db) {
  const url = new URL(request.url);
  const parts = url.pathname.split("/").filter(Boolean);
  const method = request.method;

  if (parts.length === 1 && parts[0] === "health" && method === "GET") {
    return json({ ok: true });
  }

  if (parts[0] === "guilds") {
    if (parts.length === 1 && method === "GET") {
      const { results } = await db
        .prepare("SELECT guild_id, role_id FROM guild_config WHERE enabled = 1 ORDER BY guild_id")
        .all();
      return json({ guilds: results.map((r) => [r.guild_id, r.role_id]) });
    }
    const gid = snowflake(parts[1], "guild_id");
    if (parts.length === 2 && method === "GET") {
      const row = await db
        .prepare("SELECT role_id FROM guild_config WHERE guild_id = ? AND enabled = 1")
        .bind(gid)
        .first();
      return json({ role_id: row ? row.role_id : null });
    }
    if (parts.length === 2 && method === "PUT") {
      const body = await readJson(request);
      const roleId = snowflake(body.role_id, "role_id");
      await db
        .prepare(
          `INSERT INTO guild_config (guild_id, role_id, enabled) VALUES (?, ?, 1)
           ON CONFLICT(guild_id) DO UPDATE SET
             role_id = excluded.role_id, enabled = 1, updated_at = datetime('now')`
        )
        .bind(gid, roleId)
        .run();
      return json({ ok: true });
    }
    if (parts.length === 3 && parts[2] === "disable" && method === "POST") {
      const res = await db
        .prepare("UPDATE guild_config SET enabled = 0, updated_at = datetime('now') WHERE guild_id = ?")
        .bind(gid)
        .run();
      return json({ was_configured: (res.meta && res.meta.changes) > 0 });
    }
  }

  if (parts[0] === "streaks" && parts.length >= 2) {
    const gid = snowflake(parts[1], "guild_id");
    if (parts.length === 2 && method === "GET") {
      const { results } = await db
        .prepare(
          "SELECT user_id, active_since, total_seconds, longest_seconds FROM tag_streaks WHERE guild_id = ?"
        )
        .bind(gid)
        .all();
      return json({ rows: results });
    }
    if (parts.length === 3 && parts[2] === "mark" && method === "POST") {
      const body = await readJson(request);
      // isSafeInteger, not isInteger: 1e21 is an integer to JavaScript, so it
      // passed and was stored as active_since, where it cannot survive a round
      // trip through JSON intact. Same reason snowflake() screens IDs. This
      // screens unrepresentable values, not implausible ones: a future
      // timestamp inside the safe range is still accepted, since the bound
      // between "skewed clock" and "wrong" is a policy this layer does not own.
      const now = Number.isSafeInteger(body.now) && body.now > 0 ? body.now : Math.floor(Date.now() / 1000);
      const marks = Array.isArray(body.marks) ? body.marks : null;
      if (!marks || marks.length === 0 || marks.length > MAX_MARKS) {
        throw new HttpError(400, `marks must be a list of 1-${MAX_MARKS} items`);
      }
      const stmts = marks.map((m) => markStatement(db, gid, snowflake(m && m.user_id, "user_id"), m.repping, now));
      await db.batch(stmts);
      return json({ ok: true, count: stmts.length });
    }
    if (parts.length === 3 && method === "GET") {
      const uid = snowflake(parts[2], "user_id");
      const row = await db
        .prepare(
          "SELECT user_id, active_since, total_seconds, longest_seconds FROM tag_streaks WHERE guild_id = ? AND user_id = ?"
        )
        .bind(gid, uid)
        .first();
      return json({ row: row || null });
    }
  }

  throw new HttpError(404, "not found");
}

function markStatement(db, gid, uid, repping, now) {
  if (typeof repping !== "boolean") throw new HttpError(400, "repping must be a boolean");
  if (repping) {
    // Start a streak unless one is already running.
    return db
      .prepare(
        `INSERT INTO tag_streaks (guild_id, user_id, active_since) VALUES (?, ?, ?)
         ON CONFLICT(guild_id, user_id) DO UPDATE SET
           active_since = COALESCE(tag_streaks.active_since, excluded.active_since)`
      )
      .bind(gid, uid, now);
  }
  // Close a running streak (SET expressions all read the pre-update row).
  return db
    .prepare(
      `UPDATE tag_streaks SET
         total_seconds   = total_seconds + MAX(0, ?1 - active_since),
         longest_seconds = MAX(longest_seconds, MAX(0, ?1 - active_since)),
         active_since    = NULL
       WHERE guild_id = ?2 AND user_id = ?3 AND active_since IS NOT NULL`
    )
    .bind(now, gid, uid);
}

async function authorized(request, key) {
  const header = request.headers.get("Authorization") || "";
  if (!header.startsWith("Bearer ")) return false;
  const enc = new TextEncoder();
  // Hash both sides so lengths match, then compare in constant time.
  const [a, b] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(header.slice(7))),
    crypto.subtle.digest("SHA-256", enc.encode(key)),
  ]);
  return crypto.subtle.timingSafeEqual(a, b);
}

function snowflake(value, name) {
  // Discord IDs usually exceed JavaScript's safe integer range. Require those
  // IDs as strings so JSON number rounding cannot target a different record.
  if (typeof value === "number" && !Number.isSafeInteger(value)) {
    throw new HttpError(400, `invalid ${name}`);
  }
  const s = typeof value === "number" ? String(value) : value;
  if (typeof s !== "string" || !SNOWFLAKE.test(s)) throw new HttpError(400, `invalid ${name}`);
  return s;
}

async function readJson(request) {
  try {
    const body = await request.json();
    if (body && typeof body === "object") return body;
  } catch (_) {
    /* fall through */
  }
  throw new HttpError(400, "body must be a JSON object");
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}
