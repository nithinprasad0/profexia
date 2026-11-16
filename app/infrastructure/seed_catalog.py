"""Seed the starter skill catalogue (categories + skills) — explicit, insert-only, idempotent.

This is never run automatically (not by docker-entrypoint.sh, not at app startup).
Run it by hand:

    python -m app.infrastructure.seed_catalog              # dry run: shows the plan, writes nothing
    python -m app.infrastructure.seed_catalog --apply      # writes (local/dev database)

A production database (ENVIRONMENT=production) or any non-local database host is
"protected": --apply additionally needs --confirm-production and typing the exact
phrase "seed profexia production" at an interactive prompt.

Behaviour:
- The database must be at a supported Alembic revision (0006). Nothing is migrated.
- Rows are matched by slug, by case-insensitive name (same rule as the
  uq_skills_name_lower index) and by slugified name (catches near-duplicates such
  as "Node JS" vs "Node.js").
- Records that already match exactly are left alone ("present"); differences in
  non-identity fields (description, sort order, category, active flag) are
  reported but never changed. Existing rows are never updated, deactivated or
  deleted.
- Anything ambiguous is a conflict. By default a conflict aborts the run before any
  write; --skip-conflicts seeds everything else and leaves conflicting items out.
- All writes happen in one transaction guarded by an advisory lock, so a run either
  fully succeeds or leaves the database unchanged, and two runs cannot interleave.
- The database URL comes from DATABASE_URL (settings) and is never printed.

Exit codes: 0 ok, 1 refused, 2 conflicts, 3 invalid catalogue file, 4 database error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.domain.models import SKILL_SLUG_MAX, SLUG_PATTERN, Skill, SkillCategory

DEFAULT_CATALOG = Path(__file__).resolve().parent / "catalog" / "skills_v1.json"
SUPPORTED_REVISIONS = frozenset({"0006_member_profiles_skills"})
CONFIRM_PHRASE = "seed profexia production"
# Arbitrary fixed key for pg_try_advisory_xact_lock ("profexia catalogue seed").
ADVISORY_LOCK_KEY = 7_041_202_606
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})

CATEGORY_SLUG_MAX = 80
CATEGORY_NAME_MAX = 80
SKILL_NAME_MAX = 100
_SLUG_RE = re.compile(SLUG_PATTERN)

EXIT_OK, EXIT_REFUSED, EXIT_CONFLICTS, EXIT_BAD_DATA, EXIT_DB_ERROR = 0, 1, 2, 3, 4


class SeedError(Exception):
    exit_code = EXIT_DB_ERROR


class CatalogDataError(SeedError):
    exit_code = EXIT_BAD_DATA


class SeedRefused(SeedError):
    exit_code = EXIT_REFUSED


class SeedConflicts(SeedError):
    exit_code = EXIT_CONFLICTS

    def __init__(self, plan: "Plan"):
        super().__init__(f"{len(plan.conflicts)} conflict(s); nothing was written")
        self.plan = plan


# --------------------------------------------------------------------------- catalogue data


@dataclass(frozen=True)
class CategorySpec:
    slug: str
    name: str
    description: str | None
    sort_order: int


@dataclass(frozen=True)
class SkillSpec:
    slug: str
    name: str
    category: str
    description: str | None


@dataclass(frozen=True)
class Catalog:
    version: int
    categories: tuple[CategorySpec, ...]
    skills: tuple[SkillSpec, ...]


def slugify(name: str) -> str:
    """Same rule as migration 0006's backfill: ASCII-fold, '+'->plus, '#'->sharp, '-' joins."""
    value = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    value = value.replace("+", " plus ").replace("#", " sharp ")
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:SKILL_SLUG_MAX].strip("-")


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
        _check_keys(where, item, {"slug", "name", "category"}, {"description"})
        spec = SkillSpec(
            slug=_check_slug(f"{where}.slug", item["slug"], SKILL_SLUG_MAX),
            name=_check_text(f"{where}.name", item["name"], SKILL_NAME_MAX),
            category=_check_slug(f"{where}.category", item["category"], CATEGORY_SLUG_MAX),
            description=_check_text(f"{where}.description", item.get("description"), 2000, required=False),
        )
        # Canonical slugs keep near-duplicate detection symmetric (slug == slugify(name)).
        if slugify(spec.name) != spec.slug:
            raise CatalogDataError(f"{where}: slug {spec.slug!r} must equal slugify(name) = {slugify(spec.name)!r}")
        skills.append(spec)

    for label, items in (("categories", categories), ("skills", skills)):
        for attr, norm in (("slug", str), ("name", str.lower)):
            seen: dict[str, str] = {}
            for spec in items:
                key = norm(getattr(spec, attr))
                if key in seen:
                    raise CatalogDataError(f"{label}: duplicate {attr} {getattr(spec, attr)!r}")
                seen[key] = spec.slug
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


def _describe(row: Existing) -> str:
    return f"id={row.id} slug={row.slug or 'NULL'} name={row.name!r}"


def _match(spec_slug: str, spec_name: str, rows: list[Existing]) -> tuple[Existing | None, str]:
    """Return (exact_row, "") for an exact match, (None, "") for no match, (None, reason) for a conflict."""
    lower = spec_name.lower()
    hits = [r for r in rows
            if r.slug == spec_slug or r.name_lower == lower or slugify(r.name) == spec_slug
            or (r.slug is not None and r.slug == slugify(spec_name))]
    if not hits:
        return None, ""
    if len(hits) == 1 and hits[0].slug == spec_slug and hits[0].name_lower == lower:
        return hits[0], ""
    return None, "existing " + "; ".join(_describe(r) for r in sorted(hits, key=lambda r: r.id))


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
        row, reason = _match(spec.slug, spec.name, existing_cats)
        if reason:
            plan.items.append(Item("category", spec.slug, "conflict", reason))
            blocked.add(spec.slug)
        elif row is None:
            plan.items.append(Item("category", spec.slug, "create"))
        else:
            plan.category_ids[spec.slug] = row.id
            diffs = [f for f, same in (("description", row.description == spec.description),
                                       ("sort_order", row.sort_order == spec.sort_order),
                                       ("inactive", row.is_active)) if not same]
            plan.items.append(Item("category", spec.slug, "present",
                                   f"left unchanged; differs: {', '.join(diffs)}" if diffs else ""))
    for spec in catalog.skills:
        row, reason = _match(spec.slug, spec.name, existing_skills)
        if reason:
            plan.items.append(Item("skill", spec.slug, "conflict", reason))
        elif row is None and spec.category in blocked:
            plan.items.append(Item("skill", spec.slug, "conflict", f"category {spec.category!r} is in conflict"))
        elif row is None:
            plan.items.append(Item("skill", spec.slug, "create"))
        else:
            expected_cat = plan.category_ids.get(spec.category)
            diffs = [f for f, same in (("description", row.description == spec.description),
                                       ("category", expected_cat is not None and row.category_id == expected_cat),
                                       ("inactive", row.is_active)) if not same]
            plan.items.append(Item("skill", spec.slug, "present",
                                   f"left unchanged; differs: {', '.join(diffs)}" if diffs else ""))
    return plan


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
                              skip_conflicts: bool = False) -> Plan:
    """Plan (and with apply=True, insert) inside the caller's open transaction."""
    await check_revision(conn)
    if apply:
        await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        await conn.execute(text("SET LOCAL statement_timeout = '60s'"))
        if not await conn.scalar(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY}):
            raise SeedRefused("another catalogue seed is running; nothing was written")
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


async def run(engine: AsyncEngine, catalog: Catalog, *, apply: bool, skip_conflicts: bool = False) -> Plan:
    """One transaction: committed only if every insert succeeds; dry runs are READ ONLY."""
    try:
        if apply:
            async with engine.begin() as conn:
                return await seed_in_transaction(conn, catalog, apply=True, skip_conflicts=skip_conflicts)
        async with engine.connect() as conn:
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            try:
                return await seed_in_transaction(conn, catalog, apply=False, skip_conflicts=skip_conflicts)
            finally:
                await conn.rollback()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
        raise SeedError(f"database rejected an insert (constraint {constraint or 'unknown'}); "
                        "the transaction was rolled back, nothing was written") from None
    except DBAPIError as exc:
        raise SeedError(f"database error ({type(exc.orig).__name__}); "
                        "the transaction was rolled back, nothing was written") from None


# --------------------------------------------------------------------------- CLI


def is_protected_target(database_url: str, environment: str) -> bool:
    host = (urlsplit(database_url).hostname or "").lower()
    is_local = host in LOCAL_HOSTS or host.endswith(".localhost")
    return environment == "production" or not is_local


def _read_phrase() -> str | None:
    if not sys.stdin.isatty():
        return None
    try:
        return input(f'Type "{CONFIRM_PHRASE}" to continue: ').strip()
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


def main(argv: list[str] | None = None, *, read_phrase: Callable[[], str | None] = _read_phrase) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.infrastructure.seed_catalog",
                                     description="Seed the starter skill catalogue (dry run unless --apply).")
    parser.add_argument("--apply", action="store_true", help="write to the database (default: dry run)")
    parser.add_argument("--skip-conflicts", action="store_true",
                        help="seed non-conflicting items and leave conflicting ones out (default: abort)")
    parser.add_argument("--confirm-production", action="store_true",
                        help="required with --apply for production or any non-local database")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    from app.core.config import settings
    from app.core.database import engine_options

    try:
        catalog = load_catalog(args.catalog)
        protected = is_protected_target(settings.database_url, settings.environment)
        print(f"Catalogue v{catalog.version}: {len(catalog.categories)} categories, {len(catalog.skills)} skills. "
              f"Target: {'PROTECTED (production or remote host)' if protected else 'local database'}. "
              f"Mode: {'APPLY' if args.apply else 'dry run (no writes)'}.")
        if args.apply and protected:
            if not args.confirm_production:
                raise SeedRefused("protected database: --apply also requires --confirm-production")
            if read_phrase() != CONFIRM_PHRASE:
                raise SeedRefused("confirmation phrase not entered (an interactive terminal is required)")
        async def _run_once() -> Plan:
            engine = create_async_engine(settings.database_url, **engine_options(settings.database_url))
            try:
                return await run(engine, catalog, apply=args.apply, skip_conflicts=args.skip_conflicts)
            finally:
                await engine.dispose()

        try:
            plan = asyncio.run(_run_once())
        except SeedError:
            raise
        except Exception as exc:  # noqa: BLE001 - connection/driver errors may embed host or DB names: class only
            raise SeedError(f"database unavailable or failed ({type(exc).__name__}); nothing was written") from None
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
