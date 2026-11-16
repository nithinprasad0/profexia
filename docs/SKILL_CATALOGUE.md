# Skill catalogue seed

`app/infrastructure/seed_catalog.py` loads the starter skill catalogue from
`app/infrastructure/catalog/skills_v1.json` into `skill_categories` and `skills`
(tables from migration `0006_member_profiles_skills`).

It is **never run automatically**. `docker-entrypoint.sh` and app startup only seed roles.
Seeding a database always takes an explicit command.

## Commands

```bash
# 1. Dry run (default): validates the file, checks the revision, prints the plan. Writes nothing.
python -m app.infrastructure.seed_catalog

# 2. Apply to a local / development database
python -m app.infrastructure.seed_catalog --apply

# 3. Apply to production or any non-local host (prompts for: seed profexia production)
python -m app.infrastructure.seed_catalog --apply --confirm-production
```

The target database comes from `DATABASE_URL`, read through the app settings, and is never printed.

A target is **protected** if `ENVIRONMENT=production` or the database host is anything other than
`localhost`, `127.0.0.1`, `::1`, `postgres` or `*.localhost`. On a protected target,
`--apply` also needs:
- `--confirm-production`, and
- the phrase typed at an interactive terminal.

Without a terminal, the script refuses. A dry run against a protected target is allowed, because
it runs in a `READ ONLY` transaction.

| Exit code | Meaning |
|---|---|
| 0 | Dry run or apply succeeded |
| 1 | Refused: unsupported revision, missing confirmation, or another seed running |
| 2 | Conflicts found; nothing written |
| 3 | Invalid catalogue file; database not touched |
| 4 | Database error; transaction rolled back |

## Behaviour

- **Revision gate.** The script runs only at a supported Alembic revision
  (currently `0006_member_profiles_skills`) and never migrates anything. When a later
  migration ships, review the script and add the new revision to `SUPPORTED_REVISIONS`.
- **Validation first.** The file is checked before connecting:
  - slug format and length;
  - unique slugs;
  - case-insensitively unique names;
  - known categories;
  - no unknown keys;
  - printable-ASCII names;
  - each skill slug equal to its slugified name.
- **Matching.** Each catalogue item is compared with existing rows by:
  - slug;
  - `lower(name)`, the same rule as the `uq_skills_name_lower` index;
  - slugified name, which catches near-duplicates such as `Node JS` against `Node.js`.

  Outcomes:
  - **create**: nothing matches.
  - **present**: exactly one row with the same slug and name. It is left as it is. Differences
    in description, sort order, category or active flag are *reported*, never changed.
  - **conflict**: anything else, for example:
    - a legacy skill created through the old `POST /skills`, which has no slug, with the
      same name;
    - a slug used by a different name;
    - the same name under another slug.

    Skills in a conflicting category count as conflicts too.
- **Conflicts abort** the whole run before anything is written. `--skip-conflicts` seeds
  everything else and leaves the conflicting items out. Resolve conflicts deliberately,
  for example with the admin catalogue API.
- **Insert-only.** Existing categories and skills, course–skill links and member
  offers/wants are never updated, deactivated or deleted.
- **One transaction.**
  - `pg_try_advisory_xact_lock` stops two seeds from interleaving.
  - `lock_timeout` is 5 s and `statement_timeout` is 60 s.
  - Any failure, including a unique-index violation from a concurrent insert, rolls back
    everything.
- **Idempotent.** A second run reports everything as `present` and writes nothing.

## Catalogue v1 (8 categories, 45 skills)

| Category (slug) | Skills (slugs) |
|---|---|
| Programming (`programming`) | python, javascript, typescript, java, c-plus-plus (C++), c-sharp (C#), go, rust, sql |
| Web Development (`web-development`) | html-css, react, node-js, fastapi, django, rest-api-design |
| Data & AI (`data-ai`) | data-analysis, machine-learning, statistics, excel, power-bi |
| Design & Media (`design`) | ui-ux-design, figma, graphic-design, photography, video-editing |
| Languages (`languages`) | english, hindi, malayalam, tamil, spanish, french, german, japanese |
| Music (`music`) | guitar, piano, singing, music-theory |
| Business & Communication (`business`) | public-speaking, digital-marketing, product-management, accounting |
| Academics (`academics`) | mathematics, physics, chemistry, biology |

**Rationale:**
- This is the starter list proposed in the B1-A plan.
- It covers the skills a skill-barter community most often trades:
  - technical skills, matching the platform's existing course and coding features;
  - creative skills, music and academics;
  - the Indian languages most relevant to the initial audience (Hindi, Malayalam, Tamil),
    alongside widely learnt foreign languages.
- It is deliberately small. Members can propose more through skill suggestions, and admins
  approve them.
- Categories get a one-line description. Skills have no description, so as not to invent copy.
- The slugs are permanent identifiers, because the API will not rename slugs. Display names
  can be refined later through the admin API without re-seeding.

## Changing the catalogue

- Add items to the file, or add a new `skills_v2.json` with its own review. Re-running the
  seed inserts only the new items.
- Removing an item from the file does **not** remove it from the database. Deactivate
  skills through the admin API.
- If you change rows by hand with SQL, set `updated_at = now()` yourself; see the
  migration 0006 notes.

## Rolling back a seed

Seeded rows can be referenced by course tags and member offers/wants, and those references
are `ON DELETE RESTRICT`. **Deactivate, don't delete:** set `is_active = false` and
`updated_at = now()`. Right after a mistaken seed, with nothing referencing the rows yet,
you can delete the new rows by slug in a reviewed transaction.

## Production runbook

1. Check the target is at `0006_member_profiles_skills` (`alembic current`).
2. Create a Neon backup branch.
3. Do a dry run against production and review the plan (expect 8 + 45 creates, 0 conflicts).
4. Get approval, then run `--apply --confirm-production` from an interactive terminal.
5. Run the dry run again. Expect `present` for every item.
6. `GET /api/v1/skills` now returns the 45 skills. Its shape is unchanged.
