"""Catalogue seed script (app/infrastructure/seed_catalog.py).

Pure validation tests need nothing. Database tests run against disposable
PostgreSQL databases: one migrated template per module, cloned per test, and
dropped afterwards (only databases this module created, by name).
"""

import asyncio
import copy
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import normalize_database_url, settings
from app.infrastructure import seed_catalog as sc

ROOT = Path(__file__).resolve().parents[1]
RAW = json.loads(sc.DEFAULT_CATALOG.read_text(encoding="utf-8"))
CATALOG = sc.validate_catalog(RAW)
N_CATEGORIES, N_SKILLS = len(CATALOG.categories), len(CATALOG.skills)


# --------------------------------------------------------------------------- catalogue file (pure)


def test_shipped_catalogue_is_valid_and_canonical():
    assert (N_CATEGORIES, N_SKILLS) == (8, 45)
    assert all(s.slug == sc.slugify(s.name) for s in CATALOG.skills)
    assert {s.category for s in CATALOG.skills} == {c.slug for c in CATALOG.categories}
    assert len({c.sort_order for c in CATALOG.categories}) == N_CATEGORIES
    for s in ("c-plus-plus", "c-sharp", "node-js", "html-css", "ui-ux-design", "go-programming-language"):
        assert s in {k.slug for k in CATALOG.skills}
    assert "go" not in {k.slug for k in CATALOG.skills}  # L5: no ambiguous permanent slug
    go = next(k for k in CATALOG.skills if k.slug == "go-programming-language")
    assert go.name == "Go (Programming Language)" and set(go.aliases) == {"Go", "Golang"}


def _mutated(fn):
    raw = copy.deepcopy(RAW)
    fn(raw)
    return raw


BAD_CATALOGUES = {
    "duplicate skill slug": lambda r: r["skills"].append(dict(r["skills"][0])),
    "case-insensitive duplicate name": lambda r: r["skills"].append(
        {"slug": "python", "name": "PYTHON", "category": "programming"}),
    "duplicate category name (case)": lambda r: r["categories"].append(
        {"slug": "music-2", "name": "MUSIC", "sort_order": 99}),
    "bad slug format": lambda r: r["skills"][0].update(slug="Python_3"),
    "slug not canonical": lambda r: r["skills"][0].update(slug="py"),
    "unknown category": lambda r: r["skills"][0].update(category="cooking"),
    "unknown key": lambda r: r["skills"][0].update(level="expert"),
    "missing key": lambda r: r["categories"][0].pop("sort_order"),
    "non-ascii name": lambda r: r["skills"][0].update(name="Pythön", slug="python"),
    "leading space": lambda r: r["skills"][0].update(name=" Python"),
    "name too long": lambda r: r["skills"][0].update(name="a" * 101, slug="a" * 101),
    "unsupported version": lambda r: r.update(version=2),
    "empty skills": lambda r: r.update(skills=[]),
    "boolean sort order": lambda r: r["categories"][0].update(sort_order=True),
    "not an object": lambda r: r["skills"].__setitem__(0, "python"),
    # L1: entries whose compact keys overlap would make a second run ambiguous.
    "category compact-key overlap (design-media / Media)": lambda r: r["categories"].append(
        {"slug": "design-media", "name": "Media", "sort_order": 90}),
    "skill compact-key overlap (NodeJS vs Node.js)": lambda r: r["skills"].append(
        {"slug": "nodejs", "name": "NodeJS", "category": "web-development"}),
    "alias overlaps another skill": lambda r: r["skills"][0].update(aliases=["Java"]),
    "alias without letters or digits": lambda r: r["skills"][0].update(aliases=["--"]),
    "aliases not a list": lambda r: r["skills"][0].update(aliases="Py"),
    "non-ascii alias": lambda r: r["skills"][0].update(aliases=["Pyth\u00f6n"]),
}


@pytest.mark.parametrize("case", sorted(BAD_CATALOGUES))
def test_invalid_catalogue_rejected_before_database(case):
    with pytest.raises(sc.CatalogDataError):
        sc.validate_catalog(_mutated(BAD_CATALOGUES[case]))


def test_unreadable_catalogue_file(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(sc.CatalogDataError):
        sc.load_catalog(bad)


def test_design_media_media_split_is_rejected_instead_of_self_conflicting():
    """L1 regression: this pair used to validate and then conflict with itself on run 2."""
    raw = _mutated(BAD_CATALOGUES["category compact-key overlap (design-media / Media)"])
    with pytest.raises(sc.CatalogDataError, match="overlaps"):
        sc.validate_catalog(raw)


# --------------------------------------------------------------------------- target identification (H1, M1)

NEON = "ep-late-example-123456"
TARGETS = {
    # url -> (effective hosts, verified-local URL?, endpoint)
    "postgresql+asyncpg://u:p@127.0.0.1:5432/x": (("127.0.0.1",), True, "127.0.0.1"),
    "postgresql+asyncpg://u:p@localhost/x": (("localhost",), True, "localhost"),
    "postgresql+asyncpg://u:p@postgres:5432/x": (("postgres",), True, "postgres"),
    "postgresql+asyncpg://u:p@db.localhost/x": (("db.localhost",), True, "db.localhost"),
    "postgresql+asyncpg://u:p@[::1]:5432/x": (("::1",), True, "::1"),
    # H1 regression: the netloc says localhost but asyncpg dials the ?host= value.
    f"postgresql+asyncpg://u:p@localhost/neondb?host={NEON}-pooler.c-2.eu-central-1.aws.neon.tech":
        ((f"{NEON}-pooler.c-2.eu-central-1.aws.neon.tech",), False, NEON),
    normalize_database_url(f"postgresql://u:p@{NEON}.c-2.eu-central-1.aws.neon.tech/neondb?sslmode=require"):
        ((f"{NEON}.c-2.eu-central-1.aws.neon.tech",), False, NEON),
    "postgresql+asyncpg://u:p@127.0.0.1/x?ssl=require": (("127.0.0.1",), False, "127.0.0.1"),
    "postgresql+asyncpg://u:p@/x?host=localhost:5432&host=127.0.0.1:5433": (("localhost", "127.0.0.1"), False, None),
    "postgresql+asyncpg://u:p@/x?host=/var/run/postgresql": (("/var/run/postgresql",), False, None),
    "postgresql+asyncpg://u:p@/x": ((), False, None),
    "postgresql+asyncpg://u:p@10.0.0.5/x": (("10.0.0.5",), False, "10.0.0.5"),
    "mysql://u:p@localhost/x": ((), False, None),
}


@pytest.mark.parametrize("url", list(TARGETS), ids=[str(i) for i in range(len(TARGETS))])
def test_describe_target_uses_effective_driver_host(url):
    hosts, local, endpoint = TARGETS[url]
    t = sc.describe_target(url)
    assert (t.hosts, not t.problems, t.endpoint) == (hosts, local, endpoint)


def test_describe_target_never_raises_or_echoes_url():
    t = sc.describe_target("postgresql+asyncpg://u:s3cret@[bad")
    assert t.problems and "s3cret" not in repr(t)


LOCAL_FACTS = sc.ServerFacts("profexia", ssl=False, neon=False)


def test_classify_requires_every_signal_to_be_local():
    local = sc.describe_target("postgresql+asyncpg://u:p@127.0.0.1/profexia")
    assert sc.classify(local, LOCAL_FACTS, "development") == []
    assert "ENVIRONMENT=production" in sc.classify(local, LOCAL_FACTS, "production")
    # Localhost tunnel to a managed server: the URL looks local, the server does not.
    assert sc.classify(local, sc.ServerFacts("neondb", ssl=True, neon=False), "development")
    assert sc.classify(local, sc.ServerFacts("neondb", ssl=False, neon=True), "development")


def test_identity_is_verified_or_refused():
    neon = sc.describe_target(f"postgresql+asyncpg://u:p@{NEON}-pooler.c-2.eu-central-1.aws.neon.tech/neondb")
    assert sc.verified_identity(neon, sc.ServerFacts("neondb", True, True)) == f"{NEON}/neondb"
    tunnel = sc.describe_target("postgresql+asyncpg://u:p@127.0.0.1:6543/neondb")
    with pytest.raises(sc.SeedRefused, match="non-Neon hostname"):
        sc.verified_identity(tunnel, sc.ServerFacts("neondb", True, True))
    assert sc.verified_identity(tunnel, LOCAL_FACTS) == "127.0.0.1/profexia"
    for url in ("postgresql+asyncpg://u:p@/x?host=a.example:1&host=b.example:2",
                "postgresql+asyncpg://u:p@/x?host=/tmp", "postgresql+asyncpg://u:p@/x"):
        with pytest.raises(sc.SeedRefused, match="cannot be verified"):
            sc.verified_identity(sc.describe_target(url), LOCAL_FACTS)


# --------------------------------------------------------------------------- duplicate detection (M2, pure)


def _existing(name, slug=None, i=1):
    return sc.Existing(i, slug, name, name.lower(), None, True)


def _spec(slug):
    return next(s for s in CATALOG.skills if s.slug == slug)


@pytest.mark.parametrize("existing,slug", [
    ("NodeJS", "node-js"),
    ("node.JS", "node-js"),
    ("Golang", "go-programming-language"),
    ("Go", "go-programming-language"),
    ("JS", "javascript"),
    ("CPP", "c-plus-plus"),
    ("C Sharp", "c-sharp"),
    ("\u0420\u0443thon", "python"),            # Cyrillic Er + U
    ("\u03a1ython", "python"),                  # Greek Rho
    ("\uff30\uff59\uff54\uff48\uff4f\uff4e", "python"),  # full-width
])
def test_near_duplicates_are_conflicts(existing, slug):
    row, reason = sc._match(_spec(slug), [_existing(existing)])
    assert row is None and reason


def test_lookalike_conflict_is_labelled():
    _, reason = sc._match(_spec("python"), [_existing("\u0420\u0443thon")])
    assert "look-alike characters" in reason


@pytest.mark.parametrize("existing,slug", [
    ("Java", "javascript"), ("JavaScript", "java"), ("Gopher Care", "go-programming-language"),
    ("Rust Belt History", "rust"), ("Pythonista Club", "python"), ("C", "c-plus-plus"), ("R", "rust"),
])
def test_no_excessive_false_positives(existing, slug):
    assert sc._match(_spec(slug), [_existing(existing)]) == (None, "")


# --------------------------------------------------------------------------- disposable databases


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, f"/{name}", p.query, p.fragment))


def _admin(sql: str) -> None:
    import asyncpg

    async def go():
        conn = await asyncpg.connect(_with_db(settings.database_url, "postgres").replace("+asyncpg", "", 1))
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(go())


def _drop(name: str) -> None:
    assert name.startswith("profexia_seed_") and name.endswith("_test")
    _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="module")
def seed_template():
    # conftest switches DATABASE_URL to a "<db>_test" database only when PostgreSQL is reachable.
    if not urlsplit(settings.database_url).path.endswith("_test"):
        pytest.skip("PostgreSQL not reachable (run inside docker compose or CI)")
    name = f"profexia_seed_tmpl_{uuid.uuid4().hex[:8]}_test"
    _admin(f'CREATE DATABASE "{name}"')
    try:
        env = {**os.environ, "DATABASE_URL": _with_db(settings.database_url, name), "PYTHONPATH": str(ROOT)}
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env,
                       check=True, capture_output=True)
        yield name
    finally:
        _drop(name)


@pytest.fixture
def db_url(integration, seed_template):
    name = f"profexia_seed_{uuid.uuid4().hex[:8]}_test"
    _admin(f'CREATE DATABASE "{name}" TEMPLATE "{seed_template}"')
    try:
        yield _with_db(settings.database_url, name)
    finally:
        _drop(name)


def _engine(url):
    return create_async_engine(normalize_database_url(url))


def db(url):
    """Run `fn(conn)` in its own committed transaction on the disposable DB."""
    def runner(fn):
        async def go():
            engine = _engine(url)
            try:
                async with engine.begin() as conn:
                    return await fn(conn)
            finally:
                await engine.dispose()
        return asyncio.run(go())
    return runner


def seed(url, **kw):
    async def go():
        engine = _engine(url)
        try:
            return await sc.run(engine, CATALOG, **kw)
        finally:
            await engine.dispose()
    return asyncio.run(go())


async def _snapshot(conn):
    cats = (await conn.execute(text(
        "SELECT id, slug, name, description, sort_order, is_active, created_at, updated_at "
        "FROM skill_categories ORDER BY id"))).all()
    skills = (await conn.execute(text(
        "SELECT id, slug, name, description, category_id, is_active, created_at, updated_at "
        "FROM skills ORDER BY id"))).all()
    return [tuple(r) for r in cats], [tuple(r) for r in skills]


async def _exec(conn, sql, **params):
    return (await conn.execute(text(sql), params))


def scalar(url, sql, **params):
    async def fn(conn):
        return (await conn.execute(text(sql), params)).scalar()
    return db(url)(fn)


# --------------------------------------------------------------------------- behaviour


def test_dry_run_reports_plan_and_writes_nothing(db_url):
    plan = seed(db_url, apply=False)
    assert plan.counts() == {"category": {"create": N_CATEGORIES, "present": 0, "conflict": 0},
                             "skill": {"create": N_SKILLS, "present": 0, "conflict": 0}}
    assert db(db_url)(_snapshot) == ([], [])


def _sqlstate(exc):
    return getattr(getattr(getattr(exc, "orig", None), "__cause__", None), "sqlstate", None)


def test_dry_run_transaction_is_read_only_and_rejects_insert(db_url, monkeypatch):
    real, seen = sc.build_plan, {}

    async def probing(conn, catalog):
        plan = await real(conn, catalog)
        seen["read_only"] = await conn.scalar(text("SELECT current_setting('transaction_read_only')"))
        try:
            await conn.execute(text("INSERT INTO skill_categories (slug, name) VALUES ('probe', 'Probe')"))
        except DBAPIError as exc:
            seen["insert"] = _sqlstate(exc)
        return plan

    monkeypatch.setattr(sc, "build_plan", probing)
    seed(db_url, apply=False)
    assert seen == {"read_only": "on", "insert": "25006"}  # 25006 = read_only_sql_transaction
    assert db(db_url)(_snapshot) == ([], [])
    assert scalar(db_url, "SELECT last_value FROM skill_categories_id_seq") == 1  # no sequence use either
    assert scalar(db_url, "SELECT is_called FROM skill_categories_id_seq") is False


def test_first_run_creates_expected_records(db_url):
    plan = seed(db_url, apply=True)
    assert plan.counts()["category"]["create"] == N_CATEGORIES
    assert plan.counts()["skill"]["create"] == N_SKILLS

    async def check(conn):
        cats = {r.slug: r for r in (await _exec(conn, "SELECT * FROM skill_categories")).all()}
        rows = (await _exec(conn, "SELECT s.slug, s.name, s.description, s.is_active, c.slug AS cat "
                                  "FROM skills s JOIN skill_categories c ON c.id = s.category_id")).all()
        return cats, {r.slug: r for r in rows}

    cats, skills = db(db_url)(check)
    assert set(cats) == {c.slug for c in CATALOG.categories}
    for spec in CATALOG.categories:
        row = cats[spec.slug]
        assert (row.name, row.description, row.sort_order, row.is_active) == (
            spec.name, spec.description, spec.sort_order, True)
    assert set(skills) == {s.slug for s in CATALOG.skills}
    for spec in CATALOG.skills:
        row = skills[spec.slug]
        assert (row.name, row.description, row.is_active, row.cat) == (spec.name, None, True, spec.category)


def test_second_run_is_a_no_op(db_url):
    seed(db_url, apply=True)
    before = db(db_url)(_snapshot)
    plan = seed(db_url, apply=True)
    assert plan.counts() == {"category": {"create": 0, "present": N_CATEGORIES, "conflict": 0},
                             "skill": {"create": 0, "present": N_SKILLS, "conflict": 0}}
    assert all(not i.reason for i in plan.items)
    assert db(db_url)(_snapshot) == before  # same ids, values and timestamps


def test_existing_records_and_relationships_preserved(db_url):
    async def setup(conn):
        role = (await _exec(conn, "INSERT INTO roles (name) VALUES ('teacher') RETURNING id")).scalar()
        teacher = (await _exec(conn, "INSERT INTO users (email, username, password_hash, is_active, role_id) "
                                     "VALUES ('t@example.com', 'seedtester', 'x', true, :r) RETURNING id",
                               r=role)).scalar()
        cat = (await _exec(conn, "INSERT INTO skill_categories (slug, name) VALUES ('cooking', 'Cooking') "
                                 "RETURNING id")).scalar()
        legacy = (await _exec(conn, "INSERT INTO skills (name) VALUES ('Kotlin') RETURNING id")).scalar()
        baking = (await _exec(conn, "INSERT INTO skills (name, slug, category_id) VALUES ('Baking', 'baking', :c) "
                                    "RETURNING id", c=cat)).scalar()
        course = (await _exec(conn, "INSERT INTO courses (title, teacher_id) VALUES ('K', :t) RETURNING id",
                              t=teacher)).scalar()
        await _exec(conn, "INSERT INTO course_skills (course_id, skill_id) VALUES (:c, :s), (:c, :b)",
                    c=course, s=legacy, b=baking)
        return course

    course = db(db_url)(setup)
    before_cats, before_skills = db(db_url)(_snapshot)
    seed(db_url, apply=True)
    after_cats, after_skills = db(db_url)(_snapshot)
    assert after_cats[:len(before_cats)] == before_cats
    assert after_skills[:len(before_skills)] == before_skills
    assert len(after_skills) == len(before_skills) + N_SKILLS

    assert scalar(db_url, "SELECT count(*) FROM course_skills WHERE course_id = :c", c=course) == 2


def test_present_records_with_differences_are_reported_not_changed(db_url):
    async def setup(conn):
        await _exec(conn, "INSERT INTO skill_categories (slug, name, description, sort_order) "
                          "VALUES ('programming', 'Programming', 'Custom text', 5)")
        await _exec(conn, "INSERT INTO skills (name, slug, is_active) VALUES ('Python', 'python', false)")

    db(db_url)(setup)
    before_cats, before_skills = db(db_url)(_snapshot)
    plan = seed(db_url, apply=True)
    by_key = {(i.kind, i.slug): i for i in plan.items}
    assert by_key[("category", "programming")].action == "present"
    assert "description" in by_key[("category", "programming")].reason
    assert "sort_order" in by_key[("category", "programming")].reason
    python = by_key[("skill", "python")]
    assert python.action == "present" and "category" in python.reason and "inactive" in python.reason
    after_cats, after_skills = db(db_url)(_snapshot)
    assert after_cats[0] == before_cats[0] and after_skills[0] == before_skills[0]  # untouched
    # Seeded skills in that category attach to the existing (present) category row.
    attached = scalar(db_url, "SELECT count(*) FROM skills WHERE category_id = :i", i=before_cats[0][0])
    assert attached == sum(1 for s in CATALOG.skills if s.category == "programming") - 1


def test_display_name_difference_is_reported(db_url):
    db(db_url)(lambda conn: _exec(conn, "INSERT INTO skills (name, slug) VALUES ('python', 'python')"))
    before = db(db_url)(_snapshot)[1][0]
    plan = seed(db_url, apply=True)
    item = next(i for i in plan.items if i.slug == "python" and i.kind == "skill")
    assert item.action == "present" and "name" in item.reason.split("differs: ")[1].split(", ")
    assert db(db_url)(_snapshot)[1][0] == before  # not renamed


def test_repeated_runs_are_idempotent(db_url):
    seed(db_url, apply=True)
    first = db(db_url)(_snapshot)
    for _ in range(3):
        plan = seed(db_url, apply=True)
        assert plan.counts()["skill"] == {"create": 0, "present": N_SKILLS, "conflict": 0}
        assert plan.counts()["category"] == {"create": 0, "present": N_CATEGORIES, "conflict": 0}
        assert all(not i.reason for i in plan.items)
    assert db(db_url)(_snapshot) == first


CONFLICTS = {
    "legacy skill, case-only name match, no slug": ("skill", "python",
        "INSERT INTO skills (name) VALUES ('PYTHON')"),
    "slug taken by a different name": ("skill", "python",
        "INSERT INTO skills (name, slug) VALUES ('Python 3', 'python')"),
    "near-duplicate name (slugified)": ("skill", "node-js",
        "INSERT INTO skills (name) VALUES ('Node JS')"),
    "same name, different slug": ("skill", "react",
        "INSERT INTO skills (name, slug) VALUES ('React', 'reactjs')"),
    "category name taken under another slug": ("category", "programming",
        "INSERT INTO skill_categories (slug, name) VALUES ('coding', 'PROGRAMMING')"),
    "category slug taken by another name": ("category", "music",
        "INSERT INTO skill_categories (slug, name) VALUES ('music', 'Music & Dance')"),
    # M2 regressions
    "compact key: NodeJS": ("skill", "node-js", "INSERT INTO skills (name) VALUES ('NodeJS')"),
    "alias: Golang": ("skill", "go-programming-language", "INSERT INTO skills (name) VALUES ('Golang')"),
    "alias: Go": ("skill", "go-programming-language", "INSERT INTO skills (name, slug) VALUES ('Go', 'go')"),
    "Cyrillic look-alike": ("skill", "python", "INSERT INTO skills (name) VALUES ('\u0420\u0443thon')"),
}


@pytest.mark.parametrize("case", sorted(CONFLICTS))
def test_conflicts_abort_without_writing(db_url, case):
    kind, slug, sql = CONFLICTS[case]
    db(db_url)(lambda conn: _exec(conn, sql))
    before = db(db_url)(_snapshot)
    with pytest.raises(sc.SeedConflicts) as info:
        seed(db_url, apply=True)
    conflicts = {(i.kind, i.slug) for i in info.value.plan.conflicts}
    assert (kind, slug) in conflicts
    assert db(db_url)(_snapshot) == before
    with pytest.raises(sc.SeedConflicts):  # dry run reports the same conflicts
        seed(db_url, apply=False)


def test_skip_conflicts_seeds_the_rest(db_url):
    db(db_url)(lambda conn: _exec(conn, "INSERT INTO skills (name) VALUES ('python')"))
    db(db_url)(lambda conn: _exec(conn, "INSERT INTO skill_categories (slug, name) VALUES ('music', 'Musik')"))
    before_cats, before_skills = db(db_url)(_snapshot)
    plan = seed(db_url, apply=True, skip_conflicts=True)
    music_skills = {s.slug for s in CATALOG.skills if s.category == "music"}
    assert {i.slug for i in plan.conflicts} == {"python", "music"} | music_skills
    after_cats, after_skills = db(db_url)(_snapshot)
    assert after_cats[0] == before_cats[0] and after_skills[0] == before_skills[0]
    assert len(after_cats) == 1 + N_CATEGORIES - 1
    assert len(after_skills) == 1 + N_SKILLS - 1 - len(music_skills)
    # A later clean run (after an admin resolves the conflicts) still won't duplicate anything.
    with pytest.raises(sc.SeedConflicts):
        seed(db_url, apply=True)
    assert seed(db_url, apply=True, skip_conflicts=True).counts()["skill"]["create"] == 0


def test_failure_mid_run_rolls_back_everything(db_url, monkeypatch):
    real, calls = sc._insert_skill, []

    async def failing(conn, spec, category_id):
        calls.append(spec.slug)
        if len(calls) == 10:
            raise RuntimeError("simulated failure")
        return await real(conn, spec, category_id)

    monkeypatch.setattr(sc, "_insert_skill", failing)
    with pytest.raises(RuntimeError):
        seed(db_url, apply=True)
    assert db(db_url)(_snapshot) == ([], [])
    monkeypatch.setattr(sc, "_insert_skill", real)
    assert seed(db_url, apply=True).counts()["skill"]["create"] == N_SKILLS  # clean retry


def test_database_constraint_violation_rolls_back(db_url, monkeypatch):
    """A row appearing after planning (e.g. a concurrent admin insert) hits the unique
    index; the whole run is rolled back with a clear error."""
    real = sc._insert_skill

    async def racing(conn, spec, category_id):
        if spec.slug == "rust":
            await conn.execute(text("INSERT INTO skills (name) VALUES ('RUST')"))
        return await real(conn, spec, category_id)

    monkeypatch.setattr(sc, "_insert_skill", racing)
    with pytest.raises(sc.SeedError) as info:
        seed(db_url, apply=True)
    assert "uq_skills_name_lower" in str(info.value) and info.value.exit_code == sc.EXIT_DB_ERROR
    assert db(db_url)(_snapshot) == ([], [])


@pytest.mark.parametrize("sql", [
    "UPDATE alembic_version SET version_num = '0005_external_providers'",
    "DROP TABLE alembic_version",
])
def test_refuses_unsupported_revision(db_url, sql):
    db(db_url)(lambda conn: _exec(conn, sql))
    for apply in (False, True):
        with pytest.raises(sc.SeedRefused):
            seed(db_url, apply=apply)
    assert db(db_url)(_snapshot) == ([], [])


def test_concurrent_run_is_refused(db_url):
    async def scenario():
        holder, engine = _engine(db_url), _engine(db_url)
        try:
            async with holder.connect() as other:
                await other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": sc.ADVISORY_LOCK_KEY})
                with pytest.raises(sc.SeedRefused, match="another catalogue seed"):
                    await sc.run(engine, CATALOG, apply=True)
                await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": sc.ADVISORY_LOCK_KEY})
        finally:
            await holder.dispose()
            await engine.dispose()

    asyncio.run(scenario())
    assert db(db_url)(_snapshot) == ([], [])


async def _raw(url):
    import asyncpg
    return await asyncpg.connect(url.replace("+asyncpg", "", 1))


def test_seed_blocks_competing_writer_between_plan_and_insert(db_url, monkeypatch):
    """M3: while the seed holds its table locks, an API-style INSERT cannot slip in."""
    import asyncpg

    real, seen = sc.build_plan, {}

    async def planned_then_compete(conn, catalog):
        plan = await real(conn, catalog)
        writer = await _raw(db_url)
        try:
            await writer.execute("SET lock_timeout = '300ms'")
            try:
                await writer.execute("INSERT INTO skills (name) VALUES ('NodeJS')")
                seen["writer"] = "inserted"
            except asyncpg.exceptions.LockNotAvailableError:
                seen["writer"] = "blocked"
        finally:
            await writer.close()
        return plan

    monkeypatch.setattr(sc, "build_plan", planned_then_compete)
    plan = seed(db_url, apply=True)
    assert seen == {"writer": "blocked"}
    assert plan.counts()["skill"]["create"] == N_SKILLS
    assert scalar(db_url, "SELECT count(*) FROM skills WHERE name = 'NodeJS'") == 0


def test_competing_writer_blocks_seed_then_is_detected(db_url, monkeypatch):
    """M3: an uncommitted competing INSERT makes the seed time out (nothing written); once it
    commits, the next seed sees it as a near-duplicate conflict instead of duplicating it."""
    monkeypatch.setattr(sc, "LOCK_TIMEOUT", "500ms")

    async def scenario():
        writer = await _raw(db_url)
        engine = _engine(db_url)
        try:
            tx = writer.transaction()
            await tx.start()
            await writer.execute("INSERT INTO skills (name) VALUES ('NodeJS')")
            with pytest.raises(sc.SeedError, match="not committed") as info:
                await sc.run(engine, CATALOG, apply=True)
            assert info.value.exit_code == sc.EXIT_DB_ERROR
            await tx.commit()
            with pytest.raises(sc.SeedConflicts) as conflicts:
                await sc.run(engine, CATALOG, apply=True)
            assert ("skill", "node-js") in {(i.kind, i.slug) for i in conflicts.value.plan.conflicts}
        finally:
            await writer.close()
            await engine.dispose()

    asyncio.run(scenario())
    assert scalar(db_url, "SELECT count(*) FROM skill_categories") == 0
    assert scalar(db_url, "SELECT count(*) FROM skills") == 1


def test_identity_rechecked_inside_write_transaction(db_url):
    actual = db(db_url)(sc.server_facts)
    for wrong in (sc.ServerFacts("some_other_db", actual.ssl, actual.neon),
                  sc.ServerFacts(actual.database, actual.ssl, True)):
        with pytest.raises(sc.SeedRefused, match="differs"):
            seed(db_url, apply=True, expected=wrong)
    assert db(db_url)(_snapshot) == ([], [])
    assert seed(db_url, apply=True, expected=actual).counts()["skill"]["create"] == N_SKILLS


@pytest.mark.parametrize("committed", [True, False])
def test_commit_failure_reports_unknown_outcome(db_url, monkeypatch, committed):
    """L3: a lost COMMIT acknowledgement must not be reported as a rollback."""
    async def flaky_commit(trans):
        if committed:
            await trans.commit()
        raise ConnectionResetError("simulated: connection lost during COMMIT")

    monkeypatch.setattr(sc, "_commit", flaky_commit)
    with pytest.raises(sc.SeedOutcomeUnknown, match="UNKNOWN") as info:
        seed(db_url, apply=True)
    assert info.value.exit_code == sc.EXIT_OUTCOME_UNKNOWN and "rolled back" not in str(info.value)
    assert len(db(db_url)(_snapshot)[1]) == (N_SKILLS if committed else 0)
    monkeypatch.undo()
    plan = seed(db_url, apply=True)  # re-running is safe either way
    assert plan.counts()["skill"]["create"] == (0 if committed else N_SKILLS)
    assert len(db(db_url)(_snapshot)[1]) == N_SKILLS


# --------------------------------------------------------------------------- command line


def _cli(url, *args, environment="development"):
    env = {**os.environ, "DATABASE_URL": url, "ENVIRONMENT": environment, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-m", "app.infrastructure.seed_catalog", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)


def _assert_no_connection_details(result, url, *, identity_shown=False):
    """Never the URL, credentials or host:port. The database name may appear only as part of
    the target identity printed for protected databases."""
    output = result.stdout + result.stderr
    parts = urlsplit(url)
    secrets = [url, f"{parts.username}:{parts.password}", f"{parts.hostname}:{parts.port}"]
    if not identity_shown:
        secrets.append(parts.path.lstrip("/"))
    for secret in secrets:
        assert secret and secret not in output


def _identity(url):
    return f"127.0.0.1/{urlsplit(url).path.lstrip('/')}"


def test_cli_dry_run_then_apply_then_rerun(db_url):
    r = _cli(db_url)
    assert r.returncode == 0, r.stderr
    assert f"would create: {N_SKILLS}" in r.stdout and "nothing was written" in r.stdout
    assert "verified local database" in r.stdout
    assert db(db_url)(_snapshot) == ([], [])

    r = _cli(db_url, "--apply")
    assert r.returncode == 0, r.stderr
    assert f"created: {N_SKILLS}" in r.stdout
    r = _cli(db_url, "--apply")
    assert r.returncode == 0, r.stderr
    assert "created: 0" in r.stdout and f"already present: {N_SKILLS}" in r.stdout
    _assert_no_connection_details(r, db_url)


def test_cli_conflict_exit_code(db_url):
    db(db_url)(lambda conn: _exec(conn, "INSERT INTO skills (name) VALUES ('Python')"))
    r = _cli(db_url, "--apply")
    assert r.returncode == sc.EXIT_CONFLICTS
    assert "CONFLICT" in r.stdout and "python" in r.stdout and "ABORTED" in r.stderr
    _assert_no_connection_details(r, db_url)


@pytest.mark.parametrize("extra", [
    [], ["--confirm-production"], ["--target", "IDENTITY"],
    ["--confirm-production", "--target", "127.0.0.1/some_other_db"],
    ["--confirm-production", "--target", "IDENTITY"],  # correct, but no interactive terminal
])
def test_cli_protected_target_requires_target_and_confirmation(db_url, extra):
    extra = [_identity(db_url) if a == "IDENTITY" else a for a in extra]
    r = _cli(db_url, "--apply", *extra, environment="production")
    assert r.returncode == sc.EXIT_REFUSED, r.stdout + r.stderr
    assert db(db_url)(_snapshot) == ([], [])
    _assert_no_connection_details(r, db_url, identity_shown=True)


def test_cli_protected_dry_run_prints_identity(db_url):
    r = _cli(db_url, environment="production")
    assert r.returncode == 0 and "PROTECTED" in r.stdout and f"identity: {_identity(db_url)}" in r.stdout
    assert db(db_url)(_snapshot) == ([], [])
    _assert_no_connection_details(r, db_url, identity_shown=True)


def test_cli_localhost_host_override_bypass_is_refused_before_connecting():
    """H1 regression: netloc 'localhost' with ?host=<remote> used to be treated as local."""
    url = ("postgresql+asyncpg://neon_user:s3cret-pw@localhost/neondb"
           "?host=ep-late-example-123456-pooler.c-2.eu-central-1.aws.neon.tech")
    r = _cli(url, "--apply")
    assert r.returncode == sc.EXIT_REFUSED and "--target" in r.stderr
    assert "s3cret-pw" not in r.stdout + r.stderr and "neon.tech" not in r.stdout + r.stderr


def test_cli_unreachable_database_reports_class_only():
    url = "postgresql+asyncpg://nobody:s3cret-pw@127.0.0.1:1/profexia_nowhere_test"
    r = _cli(url)
    assert r.returncode == sc.EXIT_DB_ERROR and "Traceback" not in r.stderr
    _assert_no_connection_details(r, url)
    assert "s3cret-pw" not in r.stdout + r.stderr


def test_main_target_specific_confirmation_phrase(db_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", normalize_database_url(db_url))
    monkeypatch.setattr(settings, "environment", "production")
    args = ["--apply", "--confirm-production", "--target", _identity(db_url)]
    asked = []
    assert sc.main(args, read_phrase=lambda p: asked.append(p) or "seed profexia production") == sc.EXIT_REFUSED
    assert asked == [f"seed {_identity(db_url)}"]
    assert db(db_url)(_snapshot) == ([], [])
    assert sc.main(args, read_phrase=lambda p: p) == sc.EXIT_OK
    assert len(db(db_url)(_snapshot)[1]) == N_SKILLS


@pytest.mark.parametrize("facts", [
    {"ssl": True, "neon": False}, {"ssl": False, "neon": True}, {"ssl": True, "neon": True},
])
def test_main_localhost_tunnel_to_managed_server_is_protected(db_url, monkeypatch, facts):
    """H1 regression: a loopback URL whose server looks managed (SSL / Neon) is protected, and a
    Neon server behind a loopback/IP host can never be written to (identity unverifiable)."""
    monkeypatch.setattr(settings, "database_url", normalize_database_url(db_url))
    monkeypatch.setattr(settings, "environment", "development")
    real_probe = sc._probe

    async def managed_probe(engine):
        actual = await real_probe(engine)
        return sc.ServerFacts(actual.database, **facts)

    monkeypatch.setattr(sc, "_probe", managed_probe)
    assert sc.main(["--apply"], read_phrase=lambda p: p) == sc.EXIT_REFUSED
    full = ["--apply", "--confirm-production", "--target", _identity(db_url)]
    # Neon behind a loopback host: refused outright. SSL-only: identity verifies, but the
    # in-transaction re-check sees the real (non-SSL) server and refuses.
    assert sc.main(full, read_phrase=lambda p: p) == sc.EXIT_REFUSED
    assert db(db_url)(_snapshot) == ([], [])
