"""Seed the starter skill catalogue (categories + skills) — explicit, insert-only, idempotent.

This is never run automatically (not by docker-entrypoint.sh, not at app startup).
Run it by hand:

    python -m app.infrastructure.seed_catalog              # dry run: shows the plan, writes nothing
    python -m app.infrastructure.seed_catalog --apply      # writes (verified local database only)

Target classification. A database is "local" only if every signal agrees:
ENVIRONMENT is not production; the driver dials exactly one loopback host
(localhost, 127.0.0.0/8, ::1, the docker-compose "postgres" service, *.localhost);
no SSL is requested; and the connected server itself uses no SSL and shows no Neon
signature. Everything else (remote hosts, `?host=` overrides, multiple hosts, Unix
sockets, missing hosts, tunnels to a managed server) is "protected".

Writing to a protected database needs:
    --apply --confirm-production --target <endpoint>/<database>
and typing "seed <endpoint>/<database>" at an interactive prompt. The identity is
verified against the host the driver actually dials and current_database(); if it
cannot be verified (multiple hosts, Unix socket, a Neon server reached through a
tunnel or IP), the script refuses. The dry run prints the identity to use.

Behaviour:
- The database must be at a supported Alembic revision (0006). Nothing is migrated.
- Rows are matched by slug, by case-insensitive name (same rule as the
  uq_skills_name_lower index) and by compact key (slugified name without
  separators, with Cyrillic/Greek look-alike letters mapped to Latin, plus explicit
  aliases from the catalogue file), so "NodeJS" vs "Node.js", "Golang" vs Go and
  "Руthon" vs "Python" are all detected.
- Records that already match exactly are left alone ("present"); differences in
  display name, description, sort order, category or active flag are reported but
  never changed. Existing rows are never updated, deactivated or deleted.
- Anything ambiguous is a conflict. By default a conflict aborts the run before any
  write; --skip-conflicts seeds everything else and leaves conflicting items out.
- Writes happen in one transaction that holds an advisory lock (one seed at a time)
  and SHARE ROW EXCLUSIVE locks on skill_categories and skills (no concurrent
  inserts between planning and inserting), with lock_timeout 5s.
- The database URL comes from DATABASE_URL (settings) and is never printed.

Exit codes: 0 ok, 1 refused, 2 conflicts, 3 invalid catalogue file, 4 database error
(not committed), 5 COMMIT outcome unknown.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from sqlalchemy import text
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.domain.models import SKILL_SLUG_MAX, SLUG_PATTERN, Skill, SkillCategory

DEFAULT_CATALOG = Path(__file__).resolve().parent / "catalog" / "skills_v1.json"
# Review this script before adding a new revision: a later migration may change these tables.
SUPPORTED_REVISIONS = frozenset({"0006_member_profiles_skills"})
# Arbitrary fixed key for pg_try_advisory_xact_lock ("profexia catalogue seed").
ADVISORY_LOCK_KEY = 7_041_202_606
LOCK_TIMEOUT = "5s"
LOOPBACK_NAMES = frozenset({"localhost", "postgres"})  # "postgres" = docker-compose service

CATEGORY_SLUG_MAX = 80
CATEGORY_NAME_MAX = 80
SKILL_NAME_MAX = 100
MAX_ALIASES = 5
_SLUG_RE = re.compile(SLUG_PATTERN)

EXIT_OK, EXIT_REFUSED, EXIT_CONFLICTS, EXIT_BAD_DATA, EXIT_DB_ERROR, EXIT_OUTCOME_UNKNOWN = 0, 1, 2, 3, 4, 5


class SeedError(Exception):
    exit_code = EXIT_DB_ERROR


class CatalogDataError(SeedError):
    exit_code = EXIT_BAD_DATA


class SeedRefused(SeedError):
    exit_code = EXIT_REFUSED


class SeedOutcomeUnknown(SeedError):
    exit_code = EXIT_OUTCOME_UNKNOWN


class SeedConflicts(SeedError):
    exit_code = EXIT_CONFLICTS

    def __init__(self, plan: "Plan"):
        super().__init__(f"{len(plan.conflicts)} conflict(s); nothing was written")
        self.plan = plan


# --------------------------------------------------------------------------- normalisation

# Cyrillic and Greek letters that render like Latin ones. Targeted (not fuzzy): used
# only to see through look-alike spellings of existing names.
_LOOKALIKES = str.maketrans({
    # Cyrillic
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "У": "Y", "Х": "X", "Ѕ": "S", "І": "I", "Ј": "J", "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "ѕ": "s", "і": "i", "ј": "j", "һ": "h", "ԁ": "d", "ԛ": "q", "ԝ": "w",
    # Greek
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O",
    "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X", "α": "a", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p",
    "υ": "u", "χ": "x",
})


def slugify(name: str) -> str:
    """Same rule as migration 0006's backfill: ASCII-fold, '+'->plus, '#'->sharp, '-' joins."""
    value = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    value = value.replace("+", " plus ").replace("#", " sharp ")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:SKILL_SLUG_MAX].strip("-")


def compact(value: str) -> str:
    """Slugified value without separators: 'Node.js', 'NodeJS', 'node-js' -> 'nodejs'."""
    return slugify(value).replace("-", "")


def lookalike_ascii(name: str) -> str:
    return name.translate(_LOOKALIKES)


# --------------------------------------------------------------------------- catalogue data


@dataclass(frozen=True)
class CategorySpec:
    slug: str
    name: str
    description: str | None
    sort_order: int

    @property
    def keys(self) -> frozenset[str]:
        return frozenset({compact(self.slug), compact(self.name)} - {""})


@dataclass(frozen=True)
class SkillSpec:
    slug: str
    name: str
    category: str
    description: str | None
    aliases: tuple[str, ...] = ()

    @property
    def keys(self) -> frozenset[str]:
        return frozenset({compact(self.slug), compact(self.name), *(compact(a) for a in self.aliases)} - {""})


@dataclass(frozen=True)
class Catalog:
    version: int
    categories: tuple[CategorySpec, ...]
    skills: tuple[SkillSpec, ...]


def _check_text(where: str, value, max_len: int, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CatalogDataError(f"{where}: must be a non-empty string")
    if value != value.strip() or "  " in value:
        raise CatalogDataError(f"{where}: no leading/trailing or repeated spaces")
    if not value.isascii() or not value.isprintable():
        raise CatalogDataError(f"{where}: must be printable ASCII")
    if len(value) > max_len:
        raise CatalogDataError(f"{where}: longer than {max_len} characters")
    return value


def _check_slug(where: str, value, max_len: int) -> str:
    if not isinstance(value, str) or not _SLUG_RE.fullmatch(value) or len(value) > max_len:
        raise CatalogDataError(f"{where}: slug must match {SLUG_PATTERN} and be at most {max_len} characters")
    return value


def _check_keys(where: str, item, required: set[str], optional: set[str]) -> None:
    if not isinstance(item, dict):
        raise CatalogDataError(f"{where}: must be an object")
    missing, unknown = required - item.keys(), item.keys() - required - optional
    if missing or unknown:
        raise CatalogDataError(f"{where}: missing {sorted(missing)} / unknown {sorted(unknown)} keys")


def _check_disjoint(label: str, items) -> None:
    """No two entries may share a slug, a case-insensitive name or a compact key; otherwise a
    second run would see the first run's rows as ambiguous matches."""
    owner: dict[str, str] = {}
    for spec in items:
        for key in {f"slug:{spec.slug}", f"name:{spec.name.lower()}"} | {f"key:{k}" for k in spec.keys}:
            if key in owner:
                raise CatalogDataError(f"{label}: {spec.slug!r} overlaps {owner[key]!r} ({key})")
            owner[key] = spec.slug


def validate_catalog(raw) -> Catalog:
    """Validate the whole file before the database is touched."""
    _check_keys("catalogue", raw, {"version", "categories", "skills"}, set())
    if raw["version"] != 1:
        raise CatalogDataError("catalogue: unsupported version (expected 1)")
    if not isinstance(raw["categories"], list) or not raw["categories"]:
        raise CatalogDataError("catalogue: 'categories' must be a non-empty list")
    if not isinstance(raw["skills"], list) or not raw["skills"]:
        raise CatalogDataError("catalogue: 'skills' must be a non-empty list")

    categories: list[CategorySpec] = []
    for i, item in enumerate(raw["categories"]):
        where = f"categories[{i}]"
        _check_keys(where, item, {"slug", "name", "sort_order"}, {"description"})
        sort_order = item["sort_order"]
        if not isinstance(sort_order, int) or isinstance(sort_order, bool) or not 0 <= sort_order <= 1_000_000:
            raise CatalogDataError(f"{where}: sort_order must be an integer 0..1000000")
        categories.append(CategorySpec(
            slug=_check_slug(f"{where}.slug", item["slug"], CATEGORY_SLUG_MAX),
            name=_check_text(f"{where}.name", item["name"], CATEGORY_NAME_MAX),
            description=_check_text(f"{where}.description", item.get("description"), 2000, required=False),
            sort_order=sort_order,
        ))

    skills: list[SkillSpec] = []
    for i, item in enumerate(raw["skills"]):
        where = f"skills[{i}]"
        _check_keys(where, item, {"slug", "name", "category"}, {"description", "aliases"})
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
            raise CatalogDataError(f"{where}.aliases: must be a list of at most {MAX_ALIASES} names")
        spec = SkillSpec(
            slug=_check_slug(f"{where}.slug", item["slug"], SKILL_SLUG_MAX),
            name=_check_text(f"{where}.name", item["name"], SKILL_NAME_MAX),
            category=_check_slug(f"{where}.category", item["category"], CATEGORY_SLUG_MAX),
            description=_check_text(f"{where}.description", item.get("description"), 2000, required=False),
            aliases=tuple(_check_text(f"{where}.aliases[{j}]", a, SKILL_NAME_MAX) for j, a in enumerate(aliases)),
        )
        # Canonical slugs keep near-duplicate detection symmetric (slug == slugify(name)).
        if slugify(spec.name) != spec.slug:
            raise CatalogDataError(f"{where}: slug {spec.slug!r} must equal slugify(name) = {slugify(spec.name)!r}")
        if any(not compact(a) for a in spec.aliases):
            raise CatalogDataError(f"{where}.aliases: every alias needs letters or digits")
        skills.append(spec)

    _check_disjoint("categories", categories)
    _check_disjoint("skills", skills)
    category_slugs = {c.slug for c in categories}
    for spec in skills:
        if spec.category not in category_slugs:
            raise CatalogDataError(f"skills: {spec.slug!r} references unknown category {spec.category!r}")
    return Catalog(version=raw["version"], categories=tuple(categories), skills=tuple(skills))


def load_catalog(path: Path = DEFAULT_CATALOG) -> Catalog:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogDataError(f"cannot read catalogue file {Path(path).name}: {type(exc).__name__}") from exc
    return validate_catalog(raw)


# --------------------------------------------------------------------------- planning


@dataclass(frozen=True)
class Existing:
    id: int
    slug: str | None
    name: str
    name_lower: str  # PostgreSQL lower(name): the exact rule of the unique index
    description: str | None
    is_active: bool
    category_id: int | None = None
    sort_order: int | None = None

    @property
    def plain_keys(self) -> frozenset[str]:
        return frozenset({compact(self.name), *([compact(self.slug)] if self.slug else [])} - {""})

    @property
    def lookalike_keys(self) -> frozenset[str]:
        return frozenset({compact(lookalike_ascii(self.name))} - {""})


@dataclass
class Item:
    kind: str  # "category" | "skill"
    slug: str
    action: str  # "create" | "present" | "conflict"
    reason: str = ""


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    category_ids: dict[str, int] = field(default_factory=dict)  # present categories: slug -> id

    def _of(self, action: str, kind: str | None = None) -> list[Item]:
        return [i for i in self.items if i.action == action and (kind is None or i.kind == kind)]

    @property
    def conflicts(self) -> list[Item]:
        return self._of("conflict")

    def to_create(self, kind: str) -> list[Item]:
        return self._of("create", kind)

    def counts(self) -> dict[str, dict[str, int]]:
        return {kind: {a: len(self._of(a, kind)) for a in ("create", "present", "conflict")}
                for kind in ("category", "skill")}


def _describe(row: Existing, spec_keys: frozenset[str]) -> str:
    note = " (look-alike characters)" if (row.lookalike_keys & spec_keys) and not (row.plain_keys & spec_keys) \
        else ""
    return f"id={row.id} slug={row.slug or 'NULL'} name={row.name!r}{note}"


def _match(spec, rows: list[Existing]) -> tuple[Existing | None, str]:
    """Return (exact_row, "") for an exact match, (None, "") for no match, (None, reason) for a conflict."""
    lower, keys = spec.name.lower(), spec.keys
    hits = [r for r in rows
            if r.slug == spec.slug or r.name_lower == lower or (r.plain_keys | r.lookalike_keys) & keys]
    if not hits:
        return None, ""
    if len(hits) == 1 and hits[0].slug == spec.slug and hits[0].name_lower == lower:
        return hits[0], ""
    return None, "existing " + "; ".join(_describe(r, keys) for r in sorted(hits, key=lambda r: r.id))


def _differences(row: Existing, spec, checks) -> str:
    diffs = [f for f, same in (("name", row.name == spec.name),
                               ("description", row.description == spec.description), *checks) if not same]
    return f"left unchanged; differs: {', '.join(diffs)}" if diffs else ""


async def _load_existing(conn: AsyncConnection) -> tuple[list[Existing], list[Existing]]:
    cats = (await conn.execute(text(
        "SELECT id, slug, name, lower(name), description, is_active, NULL, sort_order "
        "FROM skill_categories ORDER BY id"))).all()
    skills = (await conn.execute(text(
        "SELECT id, slug, name, lower(name), description, is_active, category_id, NULL "
        "FROM skills ORDER BY id"))).all()
    return [Existing(*r) for r in cats], [Existing(*r) for r in skills]


async def build_plan(conn: AsyncConnection, catalog: Catalog) -> Plan:
    existing_cats, existing_skills = await _load_existing(conn)
    plan = Plan()
    blocked: set[str] = set()
    for spec in catalog.categories:
        row, reason = _match(spec, existing_cats)
        if reason:
            plan.items.append(Item("category", spec.slug, "conflict", reason))
            blocked.add(spec.slug)
        elif row is None:
            plan.items.append(Item("category", spec.slug, "create"))
        else:
            plan.category_ids[spec.slug] = row.id
            plan.items.append(Item("category", spec.slug, "present", _differences(
                row, spec, (("sort_order", row.sort_order == spec.sort_order), ("inactive", row.is_active)))))
    for spec in catalog.skills:
        row, reason = _match(spec, existing_skills)
        if reason:
            plan.items.append(Item("skill", spec.slug, "conflict", reason))
        elif row is None and spec.category in blocked:
            plan.items.append(Item("skill", spec.slug, "conflict", f"category {spec.category!r} is in conflict"))
        elif row is None:
            plan.items.append(Item("skill", spec.slug, "create"))
        else:
            expected_cat = plan.category_ids.get(spec.category)
            plan.items.append(Item("skill", spec.slug, "present", _differences(row, spec, (
                ("category", expected_cat is not None and row.category_id == expected_cat),
                ("inactive", row.is_active)))))
    return plan


# --------------------------------------------------------------------------- target identification


@dataclass(frozen=True)
class Target:
    """What the driver will actually dial, derived from the URL exactly as asyncpg receives it."""
    hosts: tuple[str, ...]
    ssl_requested: bool
    endpoint: str | None  # verifiable endpoint label, None when ambiguous
    problems: tuple[str, ...]  # reasons the URL alone is not a verified local target


@dataclass(frozen=True)
class ServerFacts:
    """Read from the connected server (no secrets)."""
    database: str
    ssl: bool
    neon: bool


def _is_loopback(host: str) -> bool:
    h = host.lower().strip("[]")
    if h in LOOPBACK_NAMES or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def describe_target(database_url: str) -> Target:
    """Resolve the effective host(s) with SQLAlchemy's own asyncpg argument builder, so
    `?host=` overrides and multi-host URLs are seen exactly as the driver sees them."""
    try:
        url = make_url(database_url)
        if url.get_backend_name() != "postgresql" or url.get_driver_name() != "asyncpg":
            return Target((), False, None, ("unrecognised database URL",))
        _, kw = PGDialect_asyncpg().create_connect_args(url)
    except Exception:  # noqa: BLE001 - never echo the URL
        return Target((), False, None, ("unparseable database URL",))
    raw = kw.get("host")
    hosts = tuple(raw) if isinstance(raw, (list, tuple)) else ((raw,) if raw else ())
    ssl = kw.get("ssl")
    ssl_requested = ssl not in (None, False, "disable")
    problems = []
    if not hosts:
        problems.append("no explicit host (driver defaults / PGHOST)")
    elif len(hosts) > 1:
        problems.append("multiple hosts")
    elif hosts[0].startswith("/"):
        problems.append("Unix socket")
    elif not _is_loopback(hosts[0]):
        problems.append("non-loopback host")
    if ssl_requested:
        problems.append("SSL requested")
    endpoint = None
    if len(hosts) == 1 and not hosts[0].startswith("/"):
        h = hosts[0].lower().strip("[]")
        # Managed DNS names (e.g. Neon) identify the endpoint by their first label; IPs and
        # loopback names are used whole.
        endpoint = h if _is_ip(h) or _is_loopback(h) or "." not in h else h.split(".", 1)[0].removesuffix("-pooler")
    return Target(hosts, ssl_requested, endpoint, tuple(problems))


async def server_facts(conn: AsyncConnection) -> ServerFacts:
    row = (await conn.execute(text(
        "SELECT current_database(), "
        "COALESCE((SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()), false), "
        "EXISTS (SELECT 1 FROM pg_settings WHERE name LIKE 'neon.%' "
        "        OR (name = 'shared_preload_libraries' AND setting ~ '(^|[ ,])neon'))"))).one()
    return ServerFacts(database=row[0], ssl=bool(row[1]), neon=bool(row[2]))


def classify(target: Target, facts: ServerFacts, environment: str) -> list[str]:
    """Reasons the target is protected; an empty list means a verified local database."""
    reasons = list(target.problems)
    if environment == "production":
        reasons.insert(0, "ENVIRONMENT=production")
    if facts.ssl:
        reasons.append("server connection uses SSL")
    if facts.neon:
        reasons.append("Neon server detected")
    return reasons


def verified_identity(target: Target, facts: ServerFacts) -> str:
    """'<endpoint>/<database>' checked against the dialled host and the server, or SeedRefused."""
    if target.endpoint is None:
        raise SeedRefused("target identity cannot be verified (multiple hosts, Unix socket or no host); "
                          "nothing was written")
    if facts.neon and not target.hosts[0].lower().endswith(".neon.tech"):
        raise SeedRefused("a Neon server was reached through a non-Neon hostname (tunnel, proxy or IP), so "
                          "its endpoint cannot be verified; connect with the endpoint hostname. Nothing was written")
    return f"{target.endpoint}/{facts.database}"


# --------------------------------------------------------------------------- execution


async def check_revision(conn: AsyncConnection) -> str:
    if await conn.scalar(text("SELECT to_regclass('public.alembic_version')")) is None:
        raise SeedRefused("database has no alembic_version table; run migrations first")
    revisions = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    if len(revisions) != 1 or revisions[0] not in SUPPORTED_REVISIONS:
        raise SeedRefused(f"database revision {revisions!r} is not supported "
                          f"(expected one of {sorted(SUPPORTED_REVISIONS)}); nothing was written")
    return revisions[0]


async def _insert_category(conn: AsyncConnection, spec: CategorySpec) -> int:
    t = SkillCategory.__table__
    return await conn.scalar(t.insert().values(
        slug=spec.slug, name=spec.name, description=spec.description, sort_order=spec.sort_order,
    ).returning(t.c.id))


async def _insert_skill(conn: AsyncConnection, spec: SkillSpec, category_id: int) -> int:
    t = Skill.__table__
    return await conn.scalar(t.insert().values(
        slug=spec.slug, name=spec.name, description=spec.description, category_id=category_id,
    ).returning(t.c.id))


async def seed_in_transaction(conn: AsyncConnection, catalog: Catalog, *, apply: bool,
                              skip_conflicts: bool = False, expected: ServerFacts | None = None) -> Plan:
    """Plan (and with apply=True, insert) inside the caller's open transaction."""
    if expected is not None and await server_facts(conn) != expected:
        raise SeedRefused("the connected database differs from the one verified before writing; nothing was written")
    await check_revision(conn)
    if apply:
        await conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        await conn.execute(text("SET LOCAL statement_timeout = '60s'"))
        if not await conn.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY}):
            raise SeedRefused("another catalogue seed is running; nothing was written")
        # Blocks concurrent INSERT/UPDATE/DELETE (e.g. admin API) until commit, so nothing can
        # appear between planning and inserting. FK checks from course tags are not blocked.
        await conn.execute(text("LOCK TABLE skill_categories, skills IN SHARE ROW EXCLUSIVE MODE"))
    plan = await build_plan(conn, catalog)
    if plan.conflicts and not skip_conflicts:
        raise SeedConflicts(plan)
    if not apply:
        return plan
    by_slug_cat = {c.slug: c for c in catalog.categories}
    by_slug_skill = {s.slug: s for s in catalog.skills}
    for item in plan.to_create("category"):
        plan.category_ids[item.slug] = await _insert_category(conn, by_slug_cat[item.slug])
    for item in plan.to_create("skill"):
        spec = by_slug_skill[item.slug]
        await _insert_skill(conn, spec, plan.category_ids[spec.category])
    return plan


async def _commit(trans) -> None:  # separate so tests can simulate a lost COMMIT acknowledgement
    await trans.commit()


def _not_committed(exc: Exception) -> SeedError:
    # Before COMMIT is sent PostgreSQL never persists the transaction, whether or not
    # our explicit ROLLBACK reached the server, so "nothing was written" is certain here.
    if isinstance(exc, IntegrityError):
        constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
        return SeedError(f"database rejected an insert (constraint {constraint or 'unknown'}); "
                         "the transaction was not committed, nothing was written")
    return SeedError(f"database error ({type(getattr(exc, 'orig', exc)).__name__}); "
                     "the transaction was not committed, nothing was written")


async def run(engine: AsyncEngine, catalog: Catalog, *, apply: bool, skip_conflicts: bool = False,
              expected: ServerFacts | None = None) -> Plan:
    """Dry runs use a READ ONLY transaction. Apply uses one transaction, committed only if
    every step succeeds; a failed COMMIT is reported as an unknown outcome."""
    if not apply:
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SET TRANSACTION READ ONLY"))
                try:
                    return await seed_in_transaction(conn, catalog, apply=False, skip_conflicts=skip_conflicts,
                                                     expected=expected)
                finally:
                    await conn.rollback()
        except DBAPIError as exc:
            raise _not_committed(exc) from None

    stage, plan = "connect", None
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            stage = "work"
            try:
                plan = await seed_in_transaction(conn, catalog, apply=True, skip_conflicts=skip_conflicts,
                                                 expected=expected)
            except BaseException:
                try:
                    await trans.rollback()
                except Exception:  # noqa: BLE001 - the server discards an uncommitted transaction anyway
                    pass
                raise
            stage = "commit"
            await _commit(trans)
            stage = "committed"
    except SeedError:
        raise
    except Exception as exc:
        if stage == "committed":  # failure while releasing the connection after a successful COMMIT
            return plan
        if stage == "commit":
            raise SeedOutcomeUnknown(
                f"COMMIT failed ({type(getattr(exc, 'orig', exc)).__name__}); the outcome is UNKNOWN. "
                "Run the dry run again to see what was written (re-running is safe).") from None
        if isinstance(exc, DBAPIError):
            raise _not_committed(exc) from None
        raise
    return plan


# --------------------------------------------------------------------------- CLI


def _read_phrase(phrase: str) -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        return input(f'Type "{phrase}" to continue: ').strip()
    except (EOFError, KeyboardInterrupt):  # e.g. NUL stdin on Windows reports isatty() == True
        print()
        return None


def _print_plan(plan: Plan, *, apply: bool, out=print) -> None:
    for item in plan.items:
        if item.action != "present" or item.reason:
            out(f"  {item.action.upper():8} {item.kind:8} {item.slug}" + (f"  ({item.reason})" if item.reason else ""))
    counts = plan.counts()
    verb = "created" if apply else "would create"
    for kind in ("category", "skill"):
        c = counts[kind]
        out(f"{kind + ' records':16} {verb}: {c['create']}, already present: {c['present']}, "
            f"conflicts: {c['conflict']}")


async def _probe(engine: AsyncEngine) -> ServerFacts:
    async with engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        try:
            return await server_facts(conn)
        finally:
            await conn.rollback()


def main(argv: list[str] | None = None, *,
         read_phrase: Callable[[str], str | None] = _read_phrase) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.infrastructure.seed_catalog",
                                     description="Seed the starter skill catalogue (dry run unless --apply).")
    parser.add_argument("--apply", action="store_true", help="write to the database (default: dry run)")
    parser.add_argument("--skip-conflicts", action="store_true",
                        help="seed non-conflicting items and leave conflicting ones out (default: abort)")
    parser.add_argument("--confirm-production", action="store_true",
                        help="required with --apply for any protected (non-verified-local) database")
    parser.add_argument("--target", metavar="ENDPOINT/DATABASE",
                        help="required with --apply for protected databases; the dry run prints the value")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    from app.core.config import settings
    from app.core.database import engine_options

    def with_engine(fn):
        async def go():
            engine = create_async_engine(settings.database_url, **engine_options(settings.database_url))
            try:
                return await fn(engine)
            finally:
                await engine.dispose()
        try:
            return asyncio.run(go())
        except SeedError:
            raise
        except Exception as exc:  # noqa: BLE001 - driver errors may embed host or DB names: class only
            raise SeedError(f"database unavailable or failed ({type(exc).__name__}); nothing was written") from None

    try:
        catalog = load_catalog(args.catalog)
        target = describe_target(settings.database_url)
        url_protected = bool(target.problems) or settings.environment == "production"
        if args.apply and url_protected and not (args.confirm_production and args.target):
            raise SeedRefused("protected database: --apply also requires --confirm-production and "
                              "--target ENDPOINT/DATABASE (run the dry run to see the target)")
        facts = with_engine(_probe)
        reasons = classify(target, facts, settings.environment)
        print(f"Catalogue v{catalog.version}: {len(catalog.categories)} categories, {len(catalog.skills)} skills. "
              f"Mode: {'APPLY' if args.apply else 'dry run (no writes)'}.")
        identity = None
        if reasons:
            try:
                identity = verified_identity(target, facts)
            except SeedRefused as exc:
                if args.apply:
                    raise
                print(f"Target: PROTECTED ({'; '.join(reasons)}); identity NOT verifiable: {exc}")
            else:
                print(f"Target: PROTECTED ({'; '.join(reasons)}); identity: {identity}")
        else:
            print("Target: verified local database (loopback host, no SSL, not a managed server).")
        if args.apply and reasons:
            if not (args.confirm_production and args.target):
                raise SeedRefused("protected database: --apply also requires --confirm-production and "
                                  "--target ENDPOINT/DATABASE")
            if args.target != identity:
                raise SeedRefused(f"--target {args.target!r} does not match the connected database "
                                  f"({identity}); nothing was written")
            phrase = f"seed {identity}"
            if read_phrase(phrase) != phrase:
                raise SeedRefused("confirmation phrase not entered (an interactive terminal is required)")
        plan = with_engine(lambda engine: run(engine, catalog, apply=args.apply,
                                              skip_conflicts=args.skip_conflicts, expected=facts))
    except SeedConflicts as exc:
        _print_plan(exc.plan, apply=False)
        print(f"ABORTED: {exc}. Resolve the conflicts or re-run with --skip-conflicts.", file=sys.stderr)
        return exc.exit_code
    except SeedError as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        return exc.exit_code
    _print_plan(plan, apply=args.apply)
    print("Done." if args.apply else "Dry run only: nothing was written. Re-run with --apply to seed.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
