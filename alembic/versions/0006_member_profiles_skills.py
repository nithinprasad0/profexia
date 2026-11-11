"""B1-A: member profiles, skill offers/wants, skill categories and suggestions

Revision ID: 0006_member_profiles_skills
Revises: 0005_external_providers

Additive only. Creates four tables and adds columns, indexes and constraints to
`skills` and `profiles`. Nothing existing is dropped, renamed or altered:
`users`, `roles`, `user_skills`, `course_skills`, `enrollments` and
`payment_orders` (and their legacy duplicate indexes) are untouched.

Compatibility with the previously deployed application code:
- every new `profiles` column is nullable or has a server default, so the old
  registration flow (Profile(display_name=...)) keeps working;
- `skills.slug` stays NULLABLE in this revision so the old `POST /skills`
  (which does not know about slugs) keeps working during a deploy or a code
  rollback. A later revision makes it NOT NULL once all writers set it.

Locking: `lock_timeout` is set to 5s for this transaction, so the migration
fails fast instead of queueing behind a long-running transaction. The ALTERs on
`skills` (0 rows in production) and `profiles` (2 rows) take brief ACCESS
EXCLUSIVE locks; all defaults are constant or `now()`, so no table rewrite.

DOWNGRADE DESTROYS ALL B1-A DATA (categories, offers, wants, suggestions,
skill slugs/categories/active flags and the new profile fields). Use it only
before real use; afterwards roll forward or restore from a Neon backup branch.
"""
import re
import unicodedata

from alembic import op
import sqlalchemy as sa

revision = "0006_member_profiles_skills"
down_revision = "0005_external_providers"
branch_labels = None
depends_on = None

SLUG_PATTERN = "^[a-z0-9]+(-[a-z0-9]+)*$"
SKILL_SLUG_MAX = 120

OFFER_LEVELS = ("beginner", "intermediate", "advanced", "expert")
CURRENT_LEVELS = ("none", "beginner", "intermediate", "advanced")
TARGET_LEVELS = OFFER_LEVELS
_LEVEL_RANK = {"none": 0, "beginner": 1, "intermediate": 2, "advanced": 3, "expert": 4}


def _in(column: str, values) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _rank(column: str) -> str:
    whens = " ".join(f"WHEN '{k}' THEN {v}" for k, v in _LEVEL_RANK.items())
    return f"(CASE {column} {whens} END)"


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def _slugify(name: str) -> str:
    """Deterministic slug for backfilling pre-existing skills (frozen copy; migrations
    must not import application code that may change later)."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()
    text = text.replace("+", " plus ").replace("#", " sharp ")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:SKILL_SLUG_MAX].strip("-")


def _backfill_skill_slugs(bind) -> None:
    rows = bind.execute(sa.text("SELECT id, name FROM skills WHERE slug IS NULL ORDER BY id")).all()
    taken = {r[0] for r in bind.execute(sa.text("SELECT slug FROM skills WHERE slug IS NOT NULL"))}
    for skill_id, name in rows:
        slug = _slugify(name) or f"skill-{skill_id}"
        if slug in taken:  # collision: suffix with the (stable) skill id
            suffix = f"-{skill_id}"
            slug = slug[: SKILL_SLUG_MAX - len(suffix)].strip("-") + suffix
        taken.add(slug)
        bind.execute(sa.text("UPDATE skills SET slug = :slug WHERE id = :id"), {"slug": slug, "id": skill_id})


def upgrade():
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")

    # Refuse to continue (whole transaction rolls back) rather than silently merge.
    duplicates = bind.execute(sa.text(
        "SELECT lower(name) FROM skills GROUP BY lower(name) HAVING count(*) > 1 ORDER BY 1"
    )).scalars().all()
    if duplicates:
        raise RuntimeError(
            f"0006: {len(duplicates)} skill name(s) differ only by letter case "
            f"(e.g. {duplicates[:5]}); resolve them manually before upgrading."
        )

    # --- skill_categories ---------------------------------------------------------
    op.create_table(
        "skill_categories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        sa.UniqueConstraint("slug", name="uq_skill_categories_slug"),
        sa.UniqueConstraint("name", name="uq_skill_categories_name"),
        sa.CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="ck_skill_categories_slug_format"),
    )

    # --- skills: additive columns -------------------------------------------------
    op.add_column("skills", sa.Column("slug", sa.String(SKILL_SLUG_MAX), nullable=True))
    op.add_column("skills", sa.Column("category_id", sa.Integer(), nullable=True))
    op.add_column("skills", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("skills", sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                                      server_default=sa.func.now()))
    op.add_column("skills", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                                      server_default=sa.func.now()))
    op.create_foreign_key("fk_skills_category_id", "skills", "skill_categories", ["category_id"], ["id"],
                          ondelete="RESTRICT")
    _backfill_skill_slugs(bind)
    op.create_check_constraint("ck_skills_slug_format", "skills", f"slug ~ '{SLUG_PATTERN}'")
    op.create_index("uq_skills_slug", "skills", ["slug"], unique=True)
    op.create_index("uq_skills_name_lower", "skills", [sa.text("lower(name)")], unique=True)
    op.create_index("ix_skills_category_id", "skills", ["category_id"])

    # --- profiles: additive columns -------------------------------------------------
    op.add_column("profiles", sa.Column("headline", sa.String(120), nullable=True))
    op.add_column("profiles", sa.Column("location", sa.String(120), nullable=True))
    op.add_column("profiles", sa.Column("timezone", sa.String(64), nullable=True))
    op.add_column("profiles", sa.Column("visibility", sa.String(20), nullable=False, server_default="members"))
    op.add_column("profiles", sa.Column("show_location", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("profiles", sa.Column("wants_visibility", sa.String(20), nullable=False,
                                        server_default="private"))
    op.add_column("profiles", sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                                        server_default=sa.func.now()))
    op.add_column("profiles", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                                        server_default=sa.func.now()))
    op.create_check_constraint("ck_profiles_visibility", "profiles", _in("visibility", ("members", "private")))
    op.create_check_constraint("ck_profiles_wants_visibility", "profiles",
                               _in("wants_visibility", ("members", "private")))

    # --- member_skill_offers ----------------------------------------------------------
    op.create_table(
        "member_skill_offers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("level", sa.String(20), nullable=False),
        sa.Column("description", sa.String(1000), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "skill_id", name="uq_member_skill_offers_user_skill"),
        sa.CheckConstraint(_in("level", OFFER_LEVELS), name="ck_member_skill_offers_level"),
    )
    op.create_index("ix_member_skill_offers_skill_id", "member_skill_offers", ["skill_id"])

    # --- member_skill_wants -----------------------------------------------------------
    op.create_table(
        "member_skill_wants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("current_level", sa.String(20), nullable=False),
        sa.Column("target_level", sa.String(20), nullable=False),
        sa.Column("notes", sa.String(1000), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("user_id", "skill_id", name="uq_member_skill_wants_user_skill"),
        sa.CheckConstraint(_in("current_level", CURRENT_LEVELS), name="ck_member_skill_wants_current_level"),
        sa.CheckConstraint(_in("target_level", TARGET_LEVELS), name="ck_member_skill_wants_target_level"),
        sa.CheckConstraint(f"{_rank('target_level')} > {_rank('current_level')}",
                           name="ck_member_skill_wants_target_above_current"),
    )
    op.create_index("ix_member_skill_wants_skill_id", "member_skill_wants", ["skill_id"])

    # --- skill_suggestions ------------------------------------------------------------
    op.create_table(
        "skill_suggestions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("suggested_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("normalized_name", sa.String(100), nullable=False),
        sa.Column("category_id", sa.Integer(), sa.ForeignKey("skill_categories.id", ondelete="RESTRICT"),
                  nullable=True),
        sa.Column("description", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("reviewed_by_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.String(500), nullable=True),
        sa.Column("resulting_skill_id", sa.Integer(), sa.ForeignKey("skills.id", ondelete="RESTRICT"),
                  nullable=True),
        *_timestamps(),
        sa.CheckConstraint(_in("status", ("pending", "approved", "rejected", "merged")),
                           name="ck_skill_suggestions_status"),
        sa.CheckConstraint("(status IN ('approved', 'merged')) = (resulting_skill_id IS NOT NULL)",
                           name="ck_skill_suggestions_resulting_skill"),
        sa.CheckConstraint("length(btrim(normalized_name)) > 0", name="ck_skill_suggestions_normalized_name"),
    )
    op.create_index("ix_skill_suggestions_suggested_by_id", "skill_suggestions", ["suggested_by_id"])
    op.create_index("ix_skill_suggestions_status", "skill_suggestions", ["status"])
    op.create_index("uq_skill_suggestions_pending_name", "skill_suggestions", ["normalized_name"], unique=True,
                    postgresql_where=sa.text("status = 'pending'"))


def downgrade():
    # DESTROYS ALL B1-A DATA. Removes only objects created by upgrade().
    op.execute("SET LOCAL lock_timeout = '5s'")

    op.drop_index("uq_skill_suggestions_pending_name", table_name="skill_suggestions")
    op.drop_index("ix_skill_suggestions_status", table_name="skill_suggestions")
    op.drop_index("ix_skill_suggestions_suggested_by_id", table_name="skill_suggestions")
    op.drop_table("skill_suggestions")

    op.drop_index("ix_member_skill_wants_skill_id", table_name="member_skill_wants")
    op.drop_table("member_skill_wants")
    op.drop_index("ix_member_skill_offers_skill_id", table_name="member_skill_offers")
    op.drop_table("member_skill_offers")

    op.drop_constraint("ck_profiles_wants_visibility", "profiles", type_="check")
    op.drop_constraint("ck_profiles_visibility", "profiles", type_="check")
    for column in ("updated_at", "created_at", "wants_visibility", "show_location", "visibility",
                   "timezone", "location", "headline"):
        op.drop_column("profiles", column)

    op.drop_index("ix_skills_category_id", table_name="skills")
    op.drop_index("uq_skills_name_lower", table_name="skills")
    op.drop_index("uq_skills_slug", table_name="skills")
    op.drop_constraint("ck_skills_slug_format", "skills", type_="check")
    op.drop_constraint("fk_skills_category_id", "skills", type_="foreignkey")
    for column in ("updated_at", "created_at", "is_active", "category_id", "slug"):
        op.drop_column("skills", column)

    op.drop_table("skill_categories")
