# Skill catalogue seed

`app/infrastructure/seed_catalog.py` loads the starter skill catalogue from
`app/infrastructure/catalog/skills_v1.json` into `skill_categories` and `skills`
(tables from migration `0006_member_profiles_skills`).

It is **never run automatically**. `docker-entrypoint.sh` and app startup only seed roles.
Seeding a database always takes an explicit command.

## Commands

```bash
# 1. Dry run (default): validates the file, identifies the target, checks the revision,
#    prints the plan. Writes nothing (READ ONLY transaction).
python -m app.infrastructure.seed_catalog

# 2. Apply to a verified local database (docker compose / CI)
python -m app.infrastructure.seed_catalog --apply

# 3. Apply to a protected database (production, Neon, anything not verified local).
#    The dry run prints the exact --target value. You must then type: seed <target>
python -m app.infrastructure.seed_catalog --apply --confirm-production --target ep-example-123456/neondb
```

The database comes from `DATABASE_URL`, read through the app settings. The URL and credentials
are never printed. The only thing shown is the target identity: the endpoint label and the
database name.

## Target identification

A database counts as **local** only if *all* of these hold:
- `ENVIRONMENT` is not `production`.
- The driver dials exactly one loopback host: `localhost`, `127.0.0.0/8`, `::1`, the
  docker-compose `postgres` service, or `*.localhost`. This is resolved the way
  SQLAlchemy/asyncpg actually connect, so a `?host=` override in the URL counts.
- No SSL is requested in the URL.
- The connected **server** uses no SSL for this session and shows no Neon signature
  (`neon.*` settings or the `neon` preload library).

Everything else is **protected**, including:
- `localhost?host=<remote>`;
- several hosts, a Unix socket, or no host at all;
- an SSH tunnel or proxy on `127.0.0.1` that forwards to Neon.

On a protected database, `--apply` requires all three of:
1. `--confirm-production`.
2. `--target <endpoint>/<database>`. This is verified against the host the driver actually
   dials and against `current_database()` on the server:
   - On Neon, the endpoint is the host's first label without `-pooler`, for example
     `ep-late-glade-a10huyxh`.
   - On an IP or loopback host, it is the host itself.
   - The script **refuses** when the identity can't be verified:
     - several hosts;
     - a Unix socket;
     - no host;
     - a Neon server reached through a tunnel, proxy or IP.
3. Typing `seed <endpoint>/<database>` at an interactive terminal. The phrase is specific to
   that target. Without a terminal the script refuses.

The server facts (database name, SSL, Neon signature) are read once before confirmation.
They are read **again inside the write transaction**, and any difference aborts the run.

| Exit code | Meaning |
|---|---|
| 0 | Dry run or apply succeeded |
| 1 | Refused: unsupported revision, missing or mismatched target or confirmation, unverifiable identity, or another seed running |
| 2 | Conflicts found; nothing written |
| 3 | Invalid catalogue file; database not touched |
| 4 | Database error before COMMIT; the transaction was not committed, so nothing was written |
| 5 | COMMIT failed; **outcome unknown**. Run the dry run to see what was written. Re-running is safe |

## Behaviour

- **Revision gate.** The script runs only at a supported Alembic revision
  (currently `0006_member_profiles_skills`) and never migrates anything. When a later
  migration ships, the seed tests fail until someone reviews the script and adds the new
  revision to `SUPPORTED_REVISIONS`.
- **Validation first.** The file is checked before connecting:
  - slug format and length;
  - names must be printable ASCII;
  - each skill slug must equal its slugified name;
  - no unknown keys;
  - known categories;
  - no two entries may overlap by slug, case-insensitive name, or *compact key* (aliases
    included), so a second run can never find its own rows ambiguous.
- **Matching.** Each catalogue item is compared with existing rows by:
  - slug;
  - `lower(name)`, the same rule as the `uq_skills_name_lower` index;
  - compact key: the slugified name with separators removed, so `NodeJS`, `Node JS` and
    `Node.js` all become `nodejs`;
  - compact key with Cyrillic/Greek look-alike letters mapped to Latin, so a Cyrillic
    `Руthon` is detected as `python`;
  - explicit aliases from the file, for example `Golang` for Go.

  There is no edit-distance fuzzy matching, so `Java` does not match `JavaScript`.

  Outcomes:
  - **create**: nothing matches.
  - **present**: exactly one row with the same slug and the same case-insensitive name. It
    is left as it is. Differences in display name (such as letter case), description, sort
    order, category or active flag are *reported*, never changed.
  - **conflict**: anything else, for example:
    - a legacy skill created through the old `POST /skills`, which has no slug, with the
      same or a near-identical name;
    - a slug used by a different name;
    - a look-alike name.

    Skills in a conflicting category count as conflicts too.
- **Conflicts abort** the whole run before anything is written. `--skip-conflicts` seeds
  everything else and leaves the conflicting items out.
  - The admin catalogue API that will rename, merge and deactivate skills doesn't exist yet;
    it is planned for B1-A PR 3.
  - Until then, resolve conflicts with a reviewed SQL transaction, or leave the item out.
- **Insert-only.** Existing categories and skills, course–skill links and member
  offers/wants are never updated, deactivated or deleted.
- **One transaction, locked.** In apply mode, before planning, the script:
  - takes `pg_try_advisory_xact_lock`, so only one seed runs at a time;
  - takes `SHARE ROW EXCLUSIVE` locks on `skill_categories` and `skills`, so no other writer
    (such as the admin API) can insert between planning and inserting;
  - uses `lock_timeout` 5 s and `statement_timeout` 60 s.

  Course tagging is not blocked: foreign-key checks need only a weaker lock. If a writer
  holds the tables for more than 5 s, the seed aborts and nothing is written.
- **Idempotent.** A second run reports everything as `present` and writes nothing.

## Catalogue v1 (8 categories, 45 skills)

| Category (slug) | Skills (slugs) |
|---|---|
| Programming (`programming`) | python, javascript, typescript, java, c-plus-plus (C++), c-sharp (C#), go-programming-language (Go), rust, sql |
| Web Development (`web-development`) | html-css, react, node-js, fastapi, django, rest-api-design |
| Data & AI (`data-ai`) | data-analysis, machine-learning, statistics, excel, power-bi |
| Design & Media (`design`) | ui-ux-design, figma, graphic-design, photography, video-editing |
| Languages (`languages`) | english, hindi, malayalam, tamil, spanish, french, german, japanese |
| Music (`music`) | guitar, piano, singing, music-theory |
| Business & Communication (`business`) | public-speaking, digital-marketing, product-management, accounting |
| Academics (`academics`) | mathematics, physics, chemistry, biology |

Aliases are used for duplicate detection only and are not stored:
- JavaScript: `JS`
- C++: `CPP`
- Go (Programming Language): `Go`, `Golang`

**Rationale:**
- This is the starter list proposed in the B1-A plan.
- It covers the skills a skill-barter community most often trades:
  - technical skills, matching the platform's existing course and coding features;
  - creative skills, music and academics;
  - the Indian languages most relevant to the initial audience (Hindi, Malayalam, Tamil),
    alongside widely learnt foreign languages.
- It is deliberately small. Members will be able to propose more through skill suggestions,
  which arrive with the catalogue API.
- Categories get a one-line description. Skills have no description, so as not to invent copy.
- Slugs are permanent identifiers. That is why Go is `go-programming-language` rather than
  the ambiguous `go`.

## Changing the catalogue

- Add items to the file, or add a new `skills_v2.json` with its own review. Re-running the
  seed inserts only the new items.
- Removing an item from the file does **not** remove it from the database. To retire a
  seeded skill:
  - after B1-A PR 3: deactivate it through the admin catalogue API;
  - until then: run a reviewed SQL transaction that sets `is_active = false, updated_at = now()`.

## Rolling back a seed

Seeded rows can be referenced by course tags and member offers/wants, and those references
are `ON DELETE RESTRICT`. **Deactivate, don't delete:** set `is_active = false` and
`updated_at = now()`. Right after a mistaken seed, with nothing referencing the rows yet,
you can delete the new rows by slug in a reviewed transaction.

## Production runbook

Render's free plan has no shell, so you run the seed from your own machine, inside the
repository's virtualenv. Load the production URL **without it reaching shell history**, and
clear it afterwards. Don't put it in `.env`.

```bash
# bash: read -s does not echo and is not recorded in history
read -rs -p "DATABASE_URL: " DATABASE_URL && export DATABASE_URL; echo
python -m app.infrastructure.seed_catalog                      # dry run; note the printed identity
python -m app.infrastructure.seed_catalog --apply --confirm-production --target <identity>
unset DATABASE_URL
```

```powershell
# PowerShell: Read-Host -AsSecureString does not echo; the variable lives only in this session
$env:DATABASE_URL = [System.Net.NetworkCredential]::new('', (Read-Host 'DATABASE_URL' -AsSecureString)).Password
python -m app.infrastructure.seed_catalog
python -m app.infrastructure.seed_catalog --apply --confirm-production --target <identity>
Remove-Item Env:DATABASE_URL
```

Use the endpoint **hostname** URL from the Neon console (direct or `-pooler`), not a tunnel.
Otherwise the identity can't be verified and the script refuses.

1. Check the target is at `0006_member_profiles_skills` (`alembic current`).
2. Create a Neon backup branch.
3. Do a dry run against production. Check the printed identity
   (`ep-…/neondb`, the production endpoint) and the plan: 8 + 45 creates, 0 conflicts.
4. Get approval, then run `--apply --confirm-production --target <identity>` and type the
   phrase.
5. Run the dry run again. Expect `present` for every item.
6. `GET /api/v1/skills` now returns the 45 skills. Its shape is unchanged.
