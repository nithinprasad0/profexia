"""B1-A schema (migration 0006): constraints, scoped schema parity and migration round trips.

Constraint and parity tests run against the shared `<db>_test` database (migrated to
head by conftest) inside transactions that are always rolled back.

Migration tests create their own throwaway `profexia_b1a_mig_<random>_test` database,
migrate it step by step, and drop only that database afterwards. No other database
is ever created, altered or dropped.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from app.core.config import settings

ROOT = Path(__file__).resolve().parents[1]

B1A_TABLES = {"skill_categories", "member_skill_offers", "member_skill_wants", "skill_suggestions"}
B1A_COLUMNS = {
    "skills": {"slug", "category_id", "is_active", "created_at", "updated_at"},
    "profiles": {"headline", "location", "timezone", "visibility", "show_location", "wants_visibility",
                 "created_at", "updated_at"},
}
B1A_OBJECTS_ON_EXISTING_TABLES = {
    "skills": {"fk_skills_category_id", "ck_skills_slug_format", "uq_skills_slug", "uq_skills_name_lower",
               "ix_skills_category_id"},
    "profiles": {"ck_profiles_visibility", "ck_profiles_wants_visibility"},
}


def _asyncpg_dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _with_db(url: str, name: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, f"/{name}", p.query, p.fragment))


async def _connect(url: str):
    import asyncpg

    return await asyncpg.connect(_asyncpg_dsn(url))


# --------------------------------------------------------------------------- slug planning (pure)


def _migration_module():
    import importlib.util

    path = ROOT / "alembic" / "versions" / "0006_member_profiles_skills.py"
    spec = importlib.util.spec_from_file_location("migration_0006", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M0006 = _migration_module()
SLUG_RE = __import__("re").compile(M0006.SLUG_PATTERN)


def _assert_valid_plan(plan: dict, taken: set = frozenset()):
    slugs = list(plan.values())
    assert len(slugs) == len(set(slugs)), "duplicate slug generated"
    assert not set(slugs) & set(taken), "existing slug reused"
    for slug in slugs:
        assert 1 <= len(slug) <= 120, slug
        assert SLUG_RE.fullmatch(slug), slug


def test_slug_model_constants_match_migration():
    from app.domain import models

    assert (models.SLUG_PATTERN, models.SKILL_SLUG_MAX) == (M0006.SLUG_PATTERN, M0006.SKILL_SLUG_MAX)
    assert models.LEVEL_RANK == M0006._LEVEL_RANK


@pytest.mark.parametrize("name", ["a" * 300, "C++ " * 80, "x-" * 200, "Ä" * 150 + "b", "# " * 100 + "z",
                                  "word " * 30 + "!" * 50])
def test_slug_long_names_truncated_and_valid(name):
    plan = M0006._plan_skill_slugs([(1, name)], set())
    _assert_valid_plan(plan)
    assert not plan[1].endswith("-")


def test_slug_names_equal_after_truncation_get_unique_slugs():
    rows = [(10, "x" * 130 + "A"), (11, "x" * 130 + "B"), (12, "x" * 125)]
    plan = M0006._plan_skill_slugs(rows, set())
    _assert_valid_plan(plan)
    assert plan[10] == "x" * 120
    assert plan[11] == "x" * (120 - 3) + "-11"
    assert plan[12] == "x" * (120 - 3) + "-12"


def test_slug_collision_with_preexisting_slug_is_never_reused():
    taken = {"python", "python-1", "rust"}
    plan = M0006._plan_skill_slugs([(1, "Python"), (2, "Rust"), (3, "Go")], taken)
    _assert_valid_plan(plan, taken)
    assert plan == {1: "python-1-2", 2: "rust-2", 3: "go"}


def test_slug_suffix_colliding_with_a_natural_slug():
    # "node-js-3" is both the suffixed form of id 3 and the natural slug of id 7.
    plan = M0006._plan_skill_slugs([(7, "node-js-3"), (3, "Node JS"), (2, "Node.js")], set())
    _assert_valid_plan(plan)
    assert plan == {2: "node-js", 3: "node-js-3", 7: "node-js-3-7"}


def test_slug_fallback_for_non_ascii_names():
    plan = M0006._plan_skill_slugs([(5, "日本語"), (6, "Skill 5"), (8, "中文")], set())
    _assert_valid_plan(plan)
    assert plan == {5: "skill-5", 6: "skill-5-6", 8: "skill-8"}


@pytest.mark.parametrize("skill_id", [2_147_483_647, 999_999_999, 10**18])
def test_slug_suffix_with_large_ids(skill_id):
    long = "z" * 200
    taken = {"z" * 120, M0006._with_suffix("z" * 120, f"-{skill_id}")}
    plan = M0006._plan_skill_slugs([(skill_id, long)], taken)
    _assert_valid_plan(plan, taken)
    assert plan[skill_id].endswith(f"-{skill_id}-2")


def test_slug_planning_is_deterministic_and_order_independent():
    import random

    rnd = random.Random(20261116)
    alphabet = "abcXYZ019 +#.-_/é日"
    rows = [(i, "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 100)))) for i in range(1, 400)]
    rows += [(1000 + i, "Same Name") for i in range(20)]  # heavy collisions
    taken = {"same-name", "same-name-1000", "skill-1"}
    first = M0006._plan_skill_slugs(rows, taken)
    shuffled = rows[:]
    rnd.shuffle(shuffled)
    assert M0006._plan_skill_slugs(shuffled, taken) == first
    assert M0006._plan_skill_slugs(rows, set(taken)) == first
    _assert_valid_plan(first, taken)


# --------------------------------------------------------------------------- constraints


async def _in_rolled_back_tx(body):
    conn = await _connect(settings.database_url)
    tx = conn.transaction()
    await tx.start()
    try:
        await body(conn)
    finally:
        await tx.rollback()
        await conn.close()


async def _fixture_rows(conn):
    tag = uuid.uuid4().hex[:10]
    role_id = await conn.fetchval("SELECT id FROM roles WHERE name = 'learner'")
    user_id = await conn.fetchval(
        "INSERT INTO users (email, username, password_hash, role_id) VALUES ($1, $2, 'x', $3) RETURNING id",
        f"b1a{tag}@example.com", f"b1a{tag}", role_id)
    category_id = await conn.fetchval(
        "INSERT INTO skill_categories (slug, name) VALUES ($1, $2) RETURNING id", f"cat-{tag}", f"Cat {tag}")
    skill_id = await conn.fetchval(
        "INSERT INTO skills (name, slug, category_id) VALUES ($1, $2, $3) RETURNING id",
        f"Skill {tag}", f"skill-{tag}", category_id)
    return tag, user_id, category_id, skill_id


async def _violates(conn, exc_name: str, sql: str, *args):
    import asyncpg

    exc = getattr(asyncpg.exceptions, exc_name)
    with pytest.raises(exc):
        async with conn.transaction():  # savepoint, so the outer transaction stays usable
            await conn.execute(sql, *args)


def test_duplicate_offer_rejected(integration):
    async def body(conn):
        _, user_id, _, skill_id = await _fixture_rows(conn)
        sql = "INSERT INTO member_skill_offers (user_id, skill_id, level) VALUES ($1, $2, 'expert')"
        await conn.execute(sql, user_id, skill_id)
        await _violates(conn, "UniqueViolationError", sql, user_id, skill_id)
    asyncio.run(_in_rolled_back_tx(body))


def test_duplicate_want_rejected(integration):
    async def body(conn):
        _, user_id, _, skill_id = await _fixture_rows(conn)
        sql = ("INSERT INTO member_skill_wants (user_id, skill_id, current_level, target_level) "
               "VALUES ($1, $2, 'none', 'beginner')")
        await conn.execute(sql, user_id, skill_id)
        await _violates(conn, "UniqueViolationError", sql, user_id, skill_id)
    asyncio.run(_in_rolled_back_tx(body))


def test_same_skill_may_be_offered_and_wanted(integration):
    async def body(conn):
        _, user_id, _, skill_id = await _fixture_rows(conn)
        await conn.execute("INSERT INTO member_skill_offers (user_id, skill_id, level) VALUES ($1, $2, 'beginner')",
                           user_id, skill_id)
        await conn.execute("INSERT INTO member_skill_wants (user_id, skill_id, current_level, target_level) "
                           "VALUES ($1, $2, 'beginner', 'expert')", user_id, skill_id)
    asyncio.run(_in_rolled_back_tx(body))


@pytest.mark.parametrize("sql,args", [
    ("INSERT INTO member_skill_offers (user_id, skill_id, level) VALUES ($1, $2, $3)", ("guru",)),
    ("INSERT INTO member_skill_offers (user_id, skill_id, level) VALUES ($1, $2, $3)", ("none",)),
    ("INSERT INTO member_skill_wants (user_id, skill_id, current_level, target_level) VALUES ($1, $2, $3, 'expert')",
     ("expert",)),
    ("INSERT INTO member_skill_wants (user_id, skill_id, current_level, target_level) VALUES ($1, $2, 'none', $3)",
     ("none",)),
    ("INSERT INTO member_skill_wants (user_id, skill_id, current_level, target_level) VALUES ($1, $2, 'none', $3)",
     ("Beginner",)),
])
def test_invalid_proficiency_level_rejected(integration, sql, args):
    async def body(conn):
        _, user_id, _, skill_id = await _fixture_rows(conn)
        await _violates(conn, "CheckViolationError", sql, user_id, skill_id, *args)
    asyncio.run(_in_rolled_back_tx(body))


@pytest.mark.parametrize("current,target,ok", [
    ("none", "beginner", True), ("beginner", "expert", True), ("advanced", "expert", True),
    ("beginner", "beginner", False), ("advanced", "intermediate", False), ("intermediate", "beginner", False),
])
def test_want_target_must_exceed_current(integration, current, target, ok):
    async def body(conn):
        _, user_id, _, skill_id = await _fixture_rows(conn)
        sql = ("INSERT INTO member_skill_wants (user_id, skill_id, current_level, target_level) "
               "VALUES ($1, $2, $3, $4)")
        if ok:
            await conn.execute(sql, user_id, skill_id, current, target)
        else:
            await _violates(conn, "CheckViolationError", sql, user_id, skill_id, current, target)
    asyncio.run(_in_rolled_back_tx(body))


def test_skill_delete_restricted_when_referenced(integration):
    async def body(conn):
        tag, user_id, _, skill_id = await _fixture_rows(conn)
        await conn.execute("INSERT INTO member_skill_offers (user_id, skill_id, level) VALUES ($1, $2, 'expert')",
                           user_id, skill_id)
        await _violates(conn, "ForeignKeyViolationError", "DELETE FROM skills WHERE id = $1", skill_id)

        want_skill = await conn.fetchval(
            "INSERT INTO skills (name, slug) VALUES ($1, $2) RETURNING id", f"W {tag}", f"w-{tag}")
        await conn.execute("INSERT INTO member_skill_wants (user_id, skill_id, current_level, target_level) "
                           "VALUES ($1, $2, 'none', 'expert')", user_id, want_skill)
        await _violates(conn, "ForeignKeyViolationError", "DELETE FROM skills WHERE id = $1", want_skill)

        merged = await conn.fetchval(
            "INSERT INTO skills (name, slug) VALUES ($1, $2) RETURNING id", f"M {tag}", f"m-{tag}")
        await conn.execute(
            "INSERT INTO skill_suggestions (suggested_by_id, name, normalized_name, status, resulting_skill_id) "
            "VALUES ($1, 'x', $2, 'merged', $3)", user_id, f"x{tag}", merged)
        await _violates(conn, "ForeignKeyViolationError", "DELETE FROM skills WHERE id = $1", merged)

        unreferenced = await conn.fetchval(
            "INSERT INTO skills (name, slug) VALUES ($1, $2) RETURNING id", f"U {tag}", f"u-{tag}")
        await conn.execute("DELETE FROM skills WHERE id = $1", unreferenced)
    asyncio.run(_in_rolled_back_tx(body))


def test_category_delete_restricted_when_referenced(integration):
    async def body(conn):
        tag, user_id, category_id, _ = await _fixture_rows(conn)
        await _violates(conn, "ForeignKeyViolationError", "DELETE FROM skill_categories WHERE id = $1", category_id)

        suggestion_cat = await conn.fetchval(
            "INSERT INTO skill_categories (slug, name) VALUES ($1, $2) RETURNING id", f"s-{tag}", f"S {tag}")
        await conn.execute("INSERT INTO skill_suggestions (suggested_by_id, name, normalized_name, category_id) "
                           "VALUES ($1, 'y', $2, $3)", user_id, f"y{tag}", suggestion_cat)
        await _violates(conn, "ForeignKeyViolationError", "DELETE FROM skill_categories WHERE id = $1",
                        suggestion_cat)

        unused = await conn.fetchval(
            "INSERT INTO skill_categories (slug, name) VALUES ($1, $2) RETURNING id", f"n-{tag}", f"N {tag}")
        await conn.execute("DELETE FROM skill_categories WHERE id = $1", unused)
    asyncio.run(_in_rolled_back_tx(body))


def test_deleting_user_cascades_to_own_inventory_only(integration):
    async def body(conn):
        _, user_id, _, skill_id = await _fixture_rows(conn)
        await conn.execute("INSERT INTO member_skill_offers (user_id, skill_id, level) VALUES ($1, $2, 'expert')",
                           user_id, skill_id)
        await conn.execute("DELETE FROM users WHERE id = $1", user_id)
        assert await conn.fetchval("SELECT count(*) FROM member_skill_offers WHERE user_id = $1", user_id) == 0
        assert await conn.fetchval("SELECT count(*) FROM skills WHERE id = $1", skill_id) == 1
    asyncio.run(_in_rolled_back_tx(body))


@pytest.mark.parametrize("slug", ["Python", "c++", "-python", "python-", "py--thon", "py thon", "py_thon", ""])
def test_invalid_slug_format_rejected(integration, slug):
    async def body(conn):
        tag = uuid.uuid4().hex[:10]
        await _violates(conn, "CheckViolationError", "INSERT INTO skills (name, slug) VALUES ($1, $2)",
                        f"N {tag}", slug)
        await _violates(conn, "CheckViolationError", "INSERT INTO skill_categories (slug, name) VALUES ($1, $2)",
                        slug, f"N {tag}")
    asyncio.run(_in_rolled_back_tx(body))


def test_valid_slugs_and_duplicate_slug(integration):
    async def body(conn):
        tag = uuid.uuid4().hex[:10]
        for slug in (f"a{tag}", f"c-plus-plus-{tag}", f"node-js-{tag}"):
            await conn.execute("INSERT INTO skills (name, slug) VALUES ($1, $2)", f"N {slug}", slug)
        await _violates(conn, "UniqueViolationError", "INSERT INTO skills (name, slug) VALUES ($1, $2)",
                        f"Other {tag}", f"a{tag}")
        await conn.execute("INSERT INTO skill_categories (slug, name) VALUES ($1, $2)", f"cat-{tag}", f"A {tag}")
        await _violates(conn, "UniqueViolationError", "INSERT INTO skill_categories (slug, name) VALUES ($1, $2)",
                        f"cat-{tag}", f"B {tag}")
        await _violates(conn, "UniqueViolationError", "INSERT INTO skill_categories (slug, name) VALUES ($1, $2)",
                        f"cat2-{tag}", f"A {tag}")
    asyncio.run(_in_rolled_back_tx(body))


def test_skill_names_unique_case_insensitively(integration):
    async def body(conn):
        tag = uuid.uuid4().hex[:10]
        await conn.execute("INSERT INTO skills (name, slug) VALUES ($1, $2)", f"Python{tag}", f"p1-{tag}")
        for variant in (f"python{tag}", f"PYTHON{tag}", f"Python{tag}"):
            await _violates(conn, "UniqueViolationError", "INSERT INTO skills (name, slug) VALUES ($1, $2)",
                            variant, f"p2-{tag}")
    asyncio.run(_in_rolled_back_tx(body))


def test_one_pending_suggestion_per_normalized_name(integration):
    async def body(conn):
        tag, user_id, _, skill_id = await _fixture_rows(conn)
        sql = "INSERT INTO skill_suggestions (suggested_by_id, name, normalized_name) VALUES ($1, 'Rust', $2)"
        await conn.execute(sql, user_id, f"rust{tag}")
        await _violates(conn, "UniqueViolationError", sql, user_id, f"rust{tag}")
        # Once resolved, the same name may be suggested again.
        await conn.execute("UPDATE skill_suggestions SET status = 'rejected' WHERE normalized_name = $1",
                           f"rust{tag}")
        await conn.execute(sql, user_id, f"rust{tag}")
        # approved/merged require the resulting skill; pending/rejected must not have one.
        await _violates(conn, "CheckViolationError",
                        "INSERT INTO skill_suggestions (suggested_by_id, name, normalized_name, status) "
                        "VALUES ($1, 'Go', $2, 'approved')", user_id, f"go{tag}")
        await _violates(conn, "CheckViolationError",
                        "INSERT INTO skill_suggestions (suggested_by_id, name, normalized_name, resulting_skill_id) "
                        "VALUES ($1, 'Go', $2, $3)", user_id, f"go{tag}", skill_id)
        await _violates(conn, "CheckViolationError",
                        "INSERT INTO skill_suggestions (suggested_by_id, name, normalized_name, status) "
                        "VALUES ($1, 'Go', $2, 'bogus')", user_id, f"go{tag}")
    asyncio.run(_in_rolled_back_tx(body))


def test_profile_privacy_defaults_and_values(integration):
    async def body(conn):
        _, user_id, _, _ = await _fixture_rows(conn)
        # Exactly what the pre-B1-A registration code inserts.
        await conn.execute("INSERT INTO profiles (user_id, display_name) VALUES ($1, 'Legacy')", user_id)
        row = await conn.fetchrow("SELECT visibility, show_location, wants_visibility, headline, location, timezone, "
                                  "created_at, updated_at FROM profiles WHERE user_id = $1", user_id)
        assert (row["visibility"], row["show_location"], row["wants_visibility"]) == ("members", False, "private")
        assert row["headline"] is None and row["location"] is None and row["timezone"] is None
        assert row["created_at"] is not None and row["updated_at"] is not None
        for column in ("visibility", "wants_visibility"):
            await _violates(conn, "CheckViolationError",
                            f"UPDATE profiles SET {column} = 'public' WHERE user_id = $1", user_id)
        await conn.execute("UPDATE profiles SET visibility = 'private', wants_visibility = 'members' "
                           "WHERE user_id = $1", user_id)
    asyncio.run(_in_rolled_back_tx(body))


def test_legacy_skill_insert_still_works(integration):
    """The deployed POST /skills inserts only name + description; slug must stay optional."""
    async def body(conn):
        tag = uuid.uuid4().hex[:10]
        row = await conn.fetchrow("INSERT INTO skills (name, description) VALUES ($1, 'd') "
                                  "RETURNING slug, category_id, is_active, created_at", f"Legacy {tag}")
        assert row["slug"] is None and row["category_id"] is None and row["is_active"] is True
        assert row["created_at"] is not None
        await conn.execute("INSERT INTO skills (name, description) VALUES ($1, 'd')", f"Legacy2 {tag}")
    asyncio.run(_in_rolled_back_tx(body))


@pytest.mark.parametrize("table,column,length", [
    ("member_skill_offers", "description", 1000), ("member_skill_wants", "notes", 1000),
    ("profiles", "headline", 120), ("profiles", "location", 120), ("profiles", "timezone", 64),
    ("skill_suggestions", "name", 100), ("skill_suggestions", "description", 500),
])
def test_length_limits(integration, table, column, length):
    async def body(conn):
        got = await conn.fetchval("SELECT character_maximum_length FROM information_schema.columns "
                                  "WHERE table_name = $1 AND column_name = $2", table, column)
        assert got == length
    asyncio.run(_in_rolled_back_tx(body))


# --------------------------------------------------------------------------- timestamps


def test_updated_at_semantics(integration):
    """server_default fills timestamps on INSERT; SQLAlchemy onupdate bumps updated_at for
    ORM flushes and Core update(); raw SQL does not (documented, no trigger by design)."""
    from sqlalchemy import select, text, update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.domain.models import MemberSkillOffer, Profile, Role, Skill, SkillCategory, User

    async def run():
        engine = create_async_engine(settings.database_url)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        tag = uuid.uuid4().hex[:10]
        try:
            async with Session() as s:  # transaction 1: INSERTs
                role = await s.scalar(select(Role).where(Role.name == "learner"))
                user = User(email=f"ts{tag}@example.com", username=f"ts{tag}", password_hash="x", role_id=role.id)
                user.profile = Profile(display_name="ts")
                category = SkillCategory(slug=f"ts-{tag}", name=f"TS {tag}")
                skill = Skill(name=f"TS {tag}", slug=f"ts-{tag}", category=category)
                s.add_all([user, category, skill])
                await s.flush()
                offer = MemberSkillOffer(user_id=user.id, skill_id=skill.id, level="beginner")
                s.add(offer)
                await s.commit()
                objs = [user.profile, category, skill, offer]
                for o in objs:
                    await s.refresh(o)
                    assert o.created_at is not None and o.updated_at == o.created_at
                inserted = {type(o): o.updated_at for o in objs}

            async with Session() as s:  # transaction 2: ORM attribute updates
                profile = await s.get(Profile, objs[0].id)
                profile.headline = "changed"
                (await s.get(SkillCategory, category.id)).sort_order = 5
                (await s.get(Skill, skill.id)).is_active = False
                (await s.get(MemberSkillOffer, offer.id)).level = "expert"
                await s.commit()

            async with Session() as s:
                orm_updated = {m: (await s.get(m, o.id, populate_existing=True)).updated_at
                               for m, o in zip((Profile, SkillCategory, Skill, MemberSkillOffer), objs)}
            assert all(orm_updated[m] > inserted[m] for m in orm_updated), (inserted, orm_updated)

            async with Session() as s:  # transaction 3: Core update() through SQLAlchemy
                await s.execute(update(MemberSkillOffer).where(MemberSkillOffer.id == offer.id)
                                .values(description="core"))
                await s.commit()
            async with Session() as s:
                core_updated = (await s.get(MemberSkillOffer, offer.id, populate_existing=True)).updated_at
            assert core_updated > orm_updated[MemberSkillOffer]

            async with Session() as s:  # transaction 4: raw SQL does NOT bump updated_at
                await s.execute(text("UPDATE member_skill_offers SET description = 'raw' WHERE id = :id"),
                                {"id": offer.id})
                await s.commit()
            async with Session() as s:
                raw = await s.get(MemberSkillOffer, offer.id, populate_existing=True)
            assert raw.description == "raw" and raw.updated_at == core_updated
        finally:
            async with Session() as s:  # remove only this test's rows (test database)
                await s.execute(text("DELETE FROM users WHERE username = :u"), {"u": f"ts{tag}"})
                await s.execute(text("DELETE FROM skills WHERE slug = :s"), {"s": f"ts-{tag}"})
                await s.execute(text("DELETE FROM skill_categories WHERE slug = :s"), {"s": f"ts-{tag}"})
                await s.commit()
            await engine.dispose()

    asyncio.run(run())


# --------------------------------------------------------------------------- scoped parity


def _b1a_include_object(seen: set):
    def include(obj, name, type_, reflected, compare_to):
        if type_ == "table":
            keep = name in B1A_TABLES or name in B1A_COLUMNS
        else:
            table = getattr(getattr(obj, "table", None), "name", None)
            if table in B1A_TABLES:
                keep = True
            elif type_ == "column":
                keep = name in B1A_COLUMNS.get(table, set())
            else:
                keep = name in B1A_OBJECTS_ON_EXISTING_TABLES.get(table, set())
        if keep:
            seen.add((type_, name))
        return keep
    return include


def test_b1a_schema_matches_models(integration):
    """Models must describe exactly what migration 0006 created (B1-A objects only).

    Legacy drift on users/enrollments/payment_orders is deliberately out of scope.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.domain.models import Base

    seen: set = set()

    def compare(sync_conn):
        ctx = MigrationContext.configure(sync_conn, opts={
            "compare_type": True,
            "include_object": _b1a_include_object(seen),
        })
        return compare_metadata(ctx, Base.metadata)

    async def run():
        engine = create_async_engine(settings.database_url)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(compare)
        finally:
            await engine.dispose()

    diffs = asyncio.run(run())
    assert diffs == [], f"B1-A model/database differences: {diffs}"
    # Guard against a vacuous comparison: every B1-A table and new column was inspected.
    for table in B1A_TABLES:
        assert ("table", table) in seen
    for columns in B1A_COLUMNS.values():
        for column in columns:
            assert ("column", column) in seen


def test_b1a_constraints_and_indexes_match_models(integration):
    """Autogenerate ignores CHECK constraints and partial-index predicates; verify those by reflection."""
    from sqlalchemy import CheckConstraint, ForeignKeyConstraint

    from app.domain.models import Base

    async def reflect(conn):
        checks = {r["conname"]: r["def"] for r in await conn.fetch(
            "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE contype = 'c' "
            "AND conrelid::regclass::text = ANY($1::text[])", list(B1A_TABLES | set(B1A_COLUMNS)))}
        fks = {(r["tbl"], r["col"]): r["action"] for r in await conn.fetch(
            "SELECT c.conrelid::regclass::text AS tbl, a.attname AS col, c.confdeltype::text AS action "
            "FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
            "WHERE c.contype = 'f' AND c.conrelid::regclass::text = ANY($1::text[])",
            list(B1A_TABLES | set(B1A_COLUMNS)))}
        indexes = {r["indexname"]: r["indexdef"] for r in await conn.fetch(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = ANY($1::text[])",
            list(B1A_TABLES | set(B1A_COLUMNS)))}
        return checks, fks, indexes

    async def run():
        conn = await _connect(settings.database_url)
        try:
            return await reflect(conn)
        finally:
            await conn.close()

    checks, fks, indexes = asyncio.run(run())
    action = {"RESTRICT": "r", "CASCADE": "c", "SET NULL": "n", None: "a"}
    for table_name in B1A_TABLES | set(B1A_COLUMNS):
        table = Base.metadata.tables[table_name]
        for constraint in table.constraints:
            if isinstance(constraint, CheckConstraint):
                assert constraint.name in checks, f"missing CHECK {constraint.name}"
            if isinstance(constraint, ForeignKeyConstraint) and (
                    table_name in B1A_TABLES or constraint.name in B1A_OBJECTS_ON_EXISTING_TABLES[table_name]):
                for column in constraint.columns:
                    assert fks[(table_name, column.name)] == action[constraint.ondelete], (table_name, column.name)
        for index in table.indexes:
            assert index.name in indexes, f"missing index {index.name}"
            assert ("UNIQUE" in indexes[index.name]) == bool(index.unique), index.name
    assert "WHERE ((status)::text = 'pending'::text)" in indexes["uq_skill_suggestions_pending_name"]
    assert "lower((name)::text)" in indexes["uq_skills_name_lower"]
    # Every B1-A CHECK in the database is declared in the models (no strays).
    declared = {c.name for t in B1A_TABLES | set(B1A_COLUMNS) for c in Base.metadata.tables[t].constraints
                if isinstance(c, CheckConstraint)}
    assert set(checks) == declared


# --------------------------------------------------------------------------- migrations


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True)


@pytest.fixture
def throwaway_db(integration):
    """A brand-new database owned by this test; dropped (and only it) afterwards."""
    import asyncpg

    name = f"profexia_b1a_mig_{uuid.uuid4().hex[:8]}_test"
    admin_dsn = _asyncpg_dsn(_with_db(settings.database_url, "postgres"))

    async def admin(sql: str):
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(admin(f'CREATE DATABASE "{name}"'))
    try:
        yield _with_db(settings.database_url, name)
    finally:
        assert name.startswith("profexia_b1a_mig_") and name.endswith("_test")
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


SNAPSHOT_SQL = {
    "columns": "SELECT table_name, column_name, data_type, is_nullable, column_default, character_maximum_length "
               "FROM information_schema.columns WHERE table_schema = 'public' AND table_name <> 'alembic_version'",
    "constraints": "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint "
                   "WHERE connamespace = 'public'::regnamespace AND conrelid <> 0 "
                   "AND conrelid::regclass::text <> 'alembic_version'",
    "indexes": "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
               "AND tablename <> 'alembic_version'",
}


async def _snapshot(url: str) -> dict[str, set]:
    conn = await _connect(url)
    try:
        return {k: {tuple(r) for r in await conn.fetch(sql)} for k, sql in SNAPSHOT_SQL.items()}
    finally:
        await conn.close()


async def _fetch(url: str, sql: str, *args):
    conn = await _connect(url)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def _seed_production_like(url: str) -> dict[str, int]:
    """Shape of audited production at 0005 (2 learners with profiles), plus legacy skills
    that exercise the slug backfill (production itself has 0 skills)."""
    conn = await _connect(url)
    try:
        for name in ("learner", "teacher", "admin"):
            await conn.execute("INSERT INTO roles (name) VALUES ($1)", name)
        learner = await conn.fetchval("SELECT id FROM roles WHERE name = 'learner'")
        for i in (1, 2):
            uid = await conn.fetchval("INSERT INTO users (email, username, password_hash, role_id) "
                                      "VALUES ($1, $2, 'x', $3) RETURNING id", f"u{i}@example.com", f"u{i}", learner)
            await conn.execute("INSERT INTO profiles (user_id, display_name) VALUES ($1, $2)", uid, f"u{i}")
        ids = {}
        for name in ("C++", "Node.js", "Node JS", "C#", "日本語", "  Data   Science "):
            ids[name] = await conn.fetchval("INSERT INTO skills (name) VALUES ($1) RETURNING id", name)
        # Hard cases: a natural slug equal to another row's suffixed slug, a natural slug
        # equal to the non-ASCII fallback, and two max-length names that only differ
        # after the 120-character slug truncation.
        for name in (f"Node JS {ids['Node JS']}", f"Skill {ids['日本語']}", "C" + "+" * 99, "C" + "+" * 98 + "#"):
            ids[name] = await conn.fetchval("INSERT INTO skills (name) VALUES ($1) RETURNING id", name)
        return ids
    finally:
        await conn.close()


def _only(snapshot: dict, tables: set, include: bool) -> dict:
    return {k: {r for r in rows if (r[0] in tables) == include} for k, rows in snapshot.items()}


def test_migration_0006_upgrade_downgrade_reupgrade(throwaway_db):
    url = throwaway_db
    r = _alembic(url, "upgrade", "0005_external_providers")
    assert r.returncode == 0, r.stderr
    skill_ids = asyncio.run(_seed_production_like(url))
    before = asyncio.run(_snapshot(url))

    r = _alembic(url, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    rows = asyncio.run(_fetch(url, "SELECT version_num FROM alembic_version"))
    assert [x[0] for x in rows] == ["0006_member_profiles_skills"]
    after = asyncio.run(_snapshot(url))

    # Tables other than skills/profiles: byte-for-byte identical schema (users, roles,
    # user_skills, course_skills, enrollments, payment_orders, ... and legacy indexes).
    untouched_before = _only(before, {"skills", "profiles"}, include=False)
    untouched_after = {k: {r for r in rows if r[0] not in {"skills", "profiles"} | B1A_TABLES}
                       for k, rows in after.items()}
    assert untouched_before == untouched_after
    # skills/profiles: purely additive; nothing removed or altered.
    for k in before:
        old = _only(before, {"skills", "profiles"}, include=True)[k]
        assert old <= after[k], f"{k} changed: {old - after[k]}"
    assert any(r[1] == "users_email_key" for r in after["constraints"])
    assert any(r[1] == "ix_users_email" for r in after["indexes"])

    # Existing profiles received the privacy defaults.
    profiles = asyncio.run(_fetch(url, "SELECT visibility, show_location, wants_visibility FROM profiles"))
    assert len(profiles) == 2
    assert all(tuple(p) == ("members", False, "private") for p in profiles)

    # Deterministic slug backfill; collisions resolved with the skill id.
    slugs = {r["name"]: r["slug"] for r in asyncio.run(_fetch(url, "SELECT name, slug FROM skills"))}
    assert slugs["C++"] == "c-plus-plus"
    assert slugs["Node.js"] == "node-js"
    assert slugs["Node JS"] == f"node-js-{skill_ids['Node JS']}"
    assert slugs["C#"] == "c-sharp"
    assert slugs["日本語"] == f"skill-{skill_ids['日本語']}"
    assert slugs["  Data   Science "] == "data-science"
    nj = f"Node JS {skill_ids['Node JS']}"
    assert slugs[nj] == f"node-js-{skill_ids['Node JS']}-{skill_ids[nj]}"
    sk = f"Skill {skill_ids['日本語']}"
    assert slugs[sk] == f"skill-{skill_ids['日本語']}-{skill_ids[sk]}"
    long_a, long_b = "C" + "+" * 99, "C" + "+" * 98 + "#"
    assert len(slugs[long_a]) == 120 and slugs[long_a].startswith("c-plus-plus")
    assert slugs[long_b].endswith(f"-{skill_ids[long_b]}") and len(slugs[long_b]) <= 120
    # Same result as the pure planner, all unique, all valid (the unique index and
    # CHECK constraint were created successfully on top of them).
    assert slugs == {name: M0006._plan_skill_slugs([(i, name) for name, i in skill_ids.items()], set())[i]
                     for name, i in skill_ids.items()}
    _assert_valid_plan(dict(enumerate(slugs.values())))

    # Downgrade restores the exact 0005 schema and keeps pre-existing data.
    r = _alembic(url, "downgrade", "-1")
    assert r.returncode == 0, r.stderr
    assert asyncio.run(_snapshot(url)) == before
    counts = asyncio.run(_fetch(url, "SELECT (SELECT count(*) FROM users), (SELECT count(*) FROM profiles), "
                                     "(SELECT count(*) FROM skills)"))
    assert tuple(counts[0]) == (2, 2, len(skill_ids))

    # Re-upgrade (twice: idempotent) gives the same 0006 schema again.
    for _ in range(2):
        r = _alembic(url, "upgrade", "head")
        assert r.returncode == 0, r.stderr
    assert asyncio.run(_snapshot(url)) == after
    # Deterministic: the re-run backfill produced exactly the same slugs.
    again = {r["name"]: r["slug"] for r in asyncio.run(_fetch(url, "SELECT name, slug FROM skills"))}
    assert again == slugs


def test_migration_0006_aborts_on_case_duplicate_skill_names(throwaway_db):
    url = throwaway_db
    assert _alembic(url, "upgrade", "0005_external_providers").returncode == 0

    async def seed():
        conn = await _connect(url)
        try:
            await conn.execute("INSERT INTO skills (name) VALUES ('Python'), ('python')")
        finally:
            await conn.close()

    asyncio.run(seed())
    r = _alembic(url, "upgrade", "head")
    assert r.returncode != 0
    assert "differ only by letter case" in r.stderr
    # Transactional DDL: nothing from 0006 was applied.
    rows = asyncio.run(_fetch(url, "SELECT version_num FROM alembic_version"))
    assert [x[0] for x in rows] == ["0005_external_providers"]
    tables = asyncio.run(_fetch(url, "SELECT to_regclass('skill_categories') IS NULL, "
                                     "(SELECT count(*) FROM information_schema.columns "
                                     " WHERE table_name = 'skills' AND column_name = 'slug')"))
    assert tuple(tables[0]) == (True, 0)
