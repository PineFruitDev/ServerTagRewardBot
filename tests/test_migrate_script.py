"""Offline coverage of the one-time SQLite to D1 migration.

The script writes member IDs into a temp SQL file and upserts them into a real
D1 database, so the properties worth pinning are that a dry run never reaches
wrangler, that the temp file is always removed, and that a mismatched import
fails loudly. Every wrangler invocation here is a fake; nothing leaves the
process.
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "migrate_sqlite_to_d1.py"
# Build test databases from the Worker's own migration so a schema change here
# surfaces as a failure instead of a silent divergence.
SCHEMA = ROOT / "worker" / "migrations" / "0001_initial.sql"


def load_module():
    spec = importlib.util.spec_from_file_location("migrate_sqlite_to_d1", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def new_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


@pytest.fixture
def migrate():
    return load_module()


@pytest.fixture
def source_db(tmp_path):
    """A legacy data.db holding two guilds and two streaks."""
    path = tmp_path / "data.db"
    conn = new_db(path)
    conn.executemany("INSERT INTO guild_config VALUES (?, ?, ?, ?)", [
        ("1", "10", 1, "2025-01-01 00:00:00"),
        ("2", "20", 0, "2025-01-02 00:00:00"),
    ])
    conn.executemany("INSERT INTO tag_streaks VALUES (?, ?, ?, ?, ?)", [
        ("1", "3001", 500, 60, 30),
        ("1", "3002", None, 90, 90),
    ])
    conn.commit()
    conn.close()
    return path


@pytest.mark.parametrize("value,expected", [
    (None, "NULL"),
    (0, "0"),
    (42, "42"),
    (-7, "-7"),
    ("abc", "'abc'"),
    ("", "''"),
    # Escaping matters: these rows are concatenated into SQL by hand.
    ("O'Brien", "'O''Brien'"),
    ("'; DROP TABLE tag_streaks; --", "'''; DROP TABLE tag_streaks; --'"),
])
def test_sql_literal_quotes_and_escapes(migrate, value, expected):
    assert migrate.sql_literal(value) == expected


def test_sql_literal_escaping_survives_a_round_trip(migrate):
    """An escaped literal must parse back to the original string."""
    nasty = "it's a 'tag', isn't it"
    conn = sqlite3.connect(":memory:")
    assert conn.execute(f"SELECT {migrate.sql_literal(nasty)}").fetchone()[0] == nasty
    conn.close()


def test_read_source_returns_rows_per_table(migrate, source_db):
    source = migrate.read_source(str(source_db))
    assert set(source) == {"guild_config", "tag_streaks"}
    assert source["guild_config"] == [
        ("1", "10", 1, "2025-01-01 00:00:00"),
        ("2", "20", 0, "2025-01-02 00:00:00"),
    ]
    assert source["tag_streaks"] == [("1", "3001", 500, 60, 30), ("1", "3002", None, 90, 90)]


def test_read_source_opens_the_database_read_only(migrate, source_db):
    """mode=ro keeps a mistyped path from being created or mutated."""
    missing = source_db.parent / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        migrate.read_source(str(missing))
    assert not missing.exists()


def test_source_fingerprint_counts_and_checksums(migrate, source_db):
    # guild_config: 2 rows, 1 enabled. tag_streaks: 2 rows, 150 total, 120 longest, 1 active.
    assert migrate.source_fingerprint(str(source_db)) == {
        "guild_config": [2, 1, 0, 0],
        "tag_streaks": [2, 150, 120, 1],
    }


def test_source_fingerprint_of_an_empty_database(migrate, tmp_path):
    path = tmp_path / "empty.db"
    new_db(path).close()
    # COALESCE keeps the sums as 0 rather than NULL, so comparison still works.
    assert migrate.source_fingerprint(str(path)) == {
        "guild_config": [0, 0, 0, 0],
        "tag_streaks": [0, 0, 0, 0],
    }


def test_d1_fingerprint_parses_wrangler_json(migrate, monkeypatch):
    payload = json.dumps([{"results": [
        {"t": "guild_config", "n": 2, "a": 1, "b": 0, "c": 0},
        {"t": "tag_streaks", "n": 5, "a": 150, "b": 120, "c": 1},
    ]}])
    calls = []
    monkeypatch.setattr(migrate, "wrangler", lambda *a: calls.append(a) or payload)
    assert migrate.d1_fingerprint(["--remote"]) == {
        "guild_config": [2, 1, 0, 0],
        "tag_streaks": [5, 150, 120, 1],
    }
    # The same query runs on both sides, and it is read-only.
    assert calls[0][:3] == ("d1", "execute", migrate.DB_NAME)
    assert "--json" in calls[0] and migrate.FINGERPRINT_SQL in calls[0]


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_wrangler_prefers_a_local_binary(migrate, monkeypatch):
    monkeypatch.setattr(migrate.shutil, "which", lambda name: "/bin/wrangler" if name == "wrangler" else None)
    seen = {}

    def run(cmd, **kwargs):
        seen.update(cmd=cmd, kwargs=kwargs)
        return FakeCompleted(stdout="ok")

    monkeypatch.setattr(migrate.subprocess, "run", run)
    assert migrate.wrangler("d1", "execute") == "ok"
    assert seen["cmd"] == ["/bin/wrangler", "d1", "execute"]
    assert seen["kwargs"]["cwd"] == migrate.WORKER_DIR


def test_wrangler_falls_back_to_npx(migrate, monkeypatch):
    monkeypatch.setattr(migrate.shutil, "which", lambda name: "/bin/npx" if name == "npx" else None)
    seen = {}
    monkeypatch.setattr(migrate.subprocess, "run",
                        lambda cmd, **kw: seen.update(cmd=cmd) or FakeCompleted(stdout="ok"))
    assert migrate.wrangler("d1") == "ok"
    assert seen["cmd"] == ["/bin/npx", "--yes", "wrangler", "d1"]


def test_wrangler_without_node_explains_itself(migrate, monkeypatch):
    monkeypatch.setattr(migrate.shutil, "which", lambda name: None)
    monkeypatch.setattr(migrate.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not run a subprocess"))
    with pytest.raises(SystemExit, match="install Node.js"):
        migrate.wrangler("d1")


def test_wrangler_failure_surfaces_stderr_tail(migrate, monkeypatch):
    monkeypatch.setattr(migrate.shutil, "which", lambda name: "/bin/wrangler" if name == "wrangler" else None)
    monkeypatch.setattr(migrate.subprocess, "run",
                        lambda *a, **k: FakeCompleted(returncode=1, stderr="x" * 5000 + "BOOM"))
    with pytest.raises(SystemExit) as exc:
        migrate.wrangler("d1", "execute", migrate.DB_NAME)
    message = str(exc.value)
    assert "BOOM" in message
    # Only the tail is shown, so a huge wrangler dump cannot flood the terminal.
    assert len(message) < 2200


class FakeWrangler:
    """Records wrangler calls and serves a fingerprint that grows after import."""

    def __init__(self, before=None, after=None):
        self.calls: list[tuple] = []
        self.sql = None
        self.sql_path = None
        self._before = before if before is not None else {
            "guild_config": [0, 0, 0, 0], "tag_streaks": [0, 0, 0, 0],
        }
        self._after = after
        self.imported = False

    def __call__(self, *args):
        self.calls.append(args)
        if "--file" in args:
            # Capture the generated SQL before the script deletes it.
            self.sql_path = args[args.index("--file") + 1]
            self.sql = Path(self.sql_path).read_text(encoding="utf-8")
            self.imported = True
            return ""
        state = self._after if self.imported else self._before
        return json.dumps([{"results": [
            {"t": table, "n": v[0], "a": v[1], "b": v[2], "c": v[3]} for table, v in state.items()
        ]}])


def run_main(migrate, monkeypatch, argv, fake):
    monkeypatch.setattr(migrate, "wrangler", fake)
    monkeypatch.setattr(sys, "argv", ["migrate_sqlite_to_d1.py", *argv])
    migrate.main()


def test_dry_run_never_contacts_d1(migrate, monkeypatch, source_db, capsys):
    """The default invocation must be inert; --apply is the only write path."""
    fake = FakeWrangler()
    run_main(migrate, monkeypatch, [str(source_db)], fake)
    assert fake.calls == []
    out = capsys.readouterr().out
    assert "dry run" in out and "pass --apply to import" in out
    assert "'guild_config': [2, 1, 0, 0]" in out


def test_apply_imports_then_verifies(migrate, monkeypatch, source_db, capsys):
    expected = {"guild_config": [2, 1, 0, 0], "tag_streaks": [2, 150, 120, 1]}
    fake = FakeWrangler(after=expected)
    run_main(migrate, monkeypatch, [str(source_db), "--apply"], fake)

    # Fingerprint before, import, fingerprint after.
    assert len(fake.calls) == 3
    assert fake.calls[1][:3] == ("d1", "execute", migrate.DB_NAME)
    assert "--remote" in fake.calls[1] and "--yes" in fake.calls[1]
    assert "counts and checksums match" in capsys.readouterr().out


def test_apply_generates_upserts_for_every_row(migrate, monkeypatch, source_db):
    fake = FakeWrangler(after={"guild_config": [2, 1, 0, 0], "tag_streaks": [2, 150, 120, 1]})
    run_main(migrate, monkeypatch, [str(source_db), "--apply"], fake)

    lines = fake.sql.strip().splitlines()
    assert lines == [
        "INSERT OR REPLACE INTO guild_config (guild_id, role_id, enabled, updated_at) "
        "VALUES ('1', '10', 1, '2025-01-01 00:00:00');",
        "INSERT OR REPLACE INTO guild_config (guild_id, role_id, enabled, updated_at) "
        "VALUES ('2', '20', 0, '2025-01-02 00:00:00');",
        "INSERT OR REPLACE INTO tag_streaks (guild_id, user_id, active_since, total_seconds, longest_seconds) "
        "VALUES ('1', '3001', 500, 60, 30);",
        # A missing active_since stays NULL rather than becoming the string "None".
        "INSERT OR REPLACE INTO tag_streaks (guild_id, user_id, active_since, total_seconds, longest_seconds) "
        "VALUES ('1', '3002', NULL, 90, 90);",
    ]
    # Re-running is safe only because every statement upserts.
    assert all(line.startswith("INSERT OR REPLACE") for line in lines)


def test_generated_sql_replays_onto_the_real_schema(migrate, monkeypatch, source_db, tmp_path):
    """Replay the script's own SQL into a fresh D1-shaped database twice.

    Re-running the migration is only safe because the primary keys in
    0001_initial.sql turn INSERT OR REPLACE into an upsert, so this asserts the
    generated SQL and that schema agree.
    """
    fake = FakeWrangler(after={"guild_config": [2, 1, 0, 0], "tag_streaks": [2, 150, 120, 1]})
    run_main(migrate, monkeypatch, [str(source_db), "--apply"], fake)

    replica = tmp_path / "replica.db"
    conn = new_db(replica)
    conn.executescript(fake.sql)
    conn.executescript(fake.sql)
    conn.commit()
    conn.close()
    assert migrate.source_fingerprint(str(replica)) == migrate.source_fingerprint(str(source_db))


def test_apply_removes_the_temp_sql_file(migrate, monkeypatch, source_db):
    """The SQL holds member IDs, so it must not survive the run."""
    fake = FakeWrangler(after={"guild_config": [2, 1, 0, 0], "tag_streaks": [2, 150, 120, 1]})
    run_main(migrate, monkeypatch, [str(source_db), "--apply"], fake)
    assert fake.sql_path and not os.path.exists(fake.sql_path)


def test_temp_sql_file_is_removed_when_import_fails(migrate, monkeypatch, source_db):
    captured = {}

    def exploding(*args):
        if "--file" in args:
            captured["path"] = args[args.index("--file") + 1]
            raise SystemExit("wrangler blew up")
        return json.dumps([{"results": [
            {"t": "guild_config", "n": 0, "a": 0, "b": 0, "c": 0},
            {"t": "tag_streaks", "n": 0, "a": 0, "b": 0, "c": 0},
        ]}])

    with pytest.raises(SystemExit):
        run_main(migrate, monkeypatch, [str(source_db), "--apply"], exploding)
    assert captured["path"] and not os.path.exists(captured["path"])


def test_mismatched_import_exits_non_zero(migrate, monkeypatch, source_db, capsys):
    """A partial import must fail rather than report success."""
    fake = FakeWrangler(after={"guild_config": [2, 1, 0, 0], "tag_streaks": [1, 60, 30, 1]})
    with pytest.raises(SystemExit) as exc:
        run_main(migrate, monkeypatch, [str(source_db), "--apply"], fake)
    assert exc.value.code == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_mangled_values_fail_even_when_counts_match(migrate, monkeypatch, source_db, capsys):
    """Row counts alone cannot vouch for an import, so checksums are compared too."""
    fake = FakeWrangler(after={"guild_config": [2, 1, 0, 0], "tag_streaks": [2, 149, 120, 1]})
    with pytest.raises(SystemExit) as exc:
        run_main(migrate, monkeypatch, [str(source_db), "--apply"], fake)
    assert exc.value.code == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_existing_rows_are_announced_before_upserting(migrate, monkeypatch, source_db, capsys):
    expected = {"guild_config": [2, 1, 0, 0], "tag_streaks": [2, 150, 120, 1]}
    fake = FakeWrangler(before={"guild_config": [1, 1, 0, 0], "tag_streaks": [0, 0, 0, 0]}, after=expected)
    run_main(migrate, monkeypatch, [str(source_db), "--apply"], fake)
    assert "D1 already has rows" in capsys.readouterr().out


def test_local_rehearsal_targets_a_persist_dir(migrate, monkeypatch, source_db, tmp_path):
    """--local must keep every call away from the remote database."""
    persist = str(tmp_path / "d1state")
    fake = FakeWrangler(after={"guild_config": [2, 1, 0, 0], "tag_streaks": [2, 150, 120, 1]})
    run_main(migrate, monkeypatch, [str(source_db), "--apply", "--local", persist], fake)
    assert fake.calls
    for call in fake.calls:
        assert "--remote" not in call
        assert "--local" in call and persist in call


def test_missing_db_path_is_rejected(migrate, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["migrate_sqlite_to_d1.py"])
    monkeypatch.setattr(migrate, "wrangler", lambda *a: pytest.fail("must not run wrangler"))
    with pytest.raises(SystemExit) as exc:
        migrate.main()
    assert exc.value.code == 2


def test_script_compiles_under_the_current_interpreter():
    """The script is run by hand, so a syntax error would only show up in prod."""
    assert subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True).returncode == 0
