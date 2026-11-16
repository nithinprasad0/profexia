# Skill catalogue seed

`app/infrastructure/seed_catalog.py` loads the starter skill catalogue from
`app/infrastructure/catalog/skills_v1.json` into `skill_categories` and `skills`
(tables from migration `0006_member_profiles_skills`).

It is **never run automatically**. `docker-entrypoint.sh` and app startup only seed roles.
Seeding a database always takes an explicit command.

## Commands

```bash
# 1. Dry run (default): validates the file, prints the target identity, checks the revision,
#    prints the plan. Writes nothing (READ ONLY transaction).
python -m app.infrastructure.seed_catalog

# 2. Apply. This is the same for every database, local or not. Copy the identity printed by
#    the dry run, then type the phrase it asks for: seed <identity>
python -m app.infrastructure.seed_catalog --apply --confirm-production --target <identity>
#    e.g. --target localhost:5432/profexia        (docker compose)
#         --target ep-example-123456/neondb       (Neon)
```

The database comes from `DATABASE_URL`, read through the app settings. The URL and credentials
are never printed. The only thing shown is the target identity: host and port (or the Neon
endpoint ID) and the database name.

## Target identification

**Every database is treated as protected. There is no automatic "local" exemption.** A client
cannot prove that a database is local:
- `localhost` or `127.0.0.1` may be an SSH tunnel or proxy to any server;
- a name such as `postgres` may resolve to any machine.

The script also reports SSL use and a Neon server signature (`neon.*` settings or the `neon`
preload library). These are **informational**. They do not and cannot detect every remote
database behind a tunnel. A tunnel to a non-Neon server that terminates TLS locally looks
exactly like a local server.

So every `--apply` requires all three of:
1. `--confirm-production`.
2. `--target <identity>`, where the identity is `<endpoint>/<database>`. The endpoint comes
   from the host and port SQLAlchemy hands to asyncpg, so `?host=` and `?port=` overrides in
   the URL are taken into account:
   - A Neon hostname (`ep-<id>[-pooler].<region>….neon.tech`) gives the Neon endpoint ID,
     for example `ep-example-123456`. The pooler and direct hostnames give the same ID.
   - **Any other single host** gives the full host and port, for example
     `db.internal.example.com:5432`, `127.0.0.1:55432` or `[::1]:6000`. The host is never
     shortened, and the port is included, so two tunnels on different ports never share an
     identity.
   - The database is `current_database()` reported by the server.
   - **Port rules:**
     - The port comes from the URL (`:port` or `?port=`). With no port, it is 5432.
     - If the URL has no port **and `PGPORT` is set**, the effective port is ambiguous, so
       there is no identity. The dry run says so and `--apply` is refused.
     - Invalid ports are refused, never silently defaulted: `:0`, an empty or non-numeric
       `?port=`, out-of-range values, or `port` given twice.
   - **Pinning.** The verified host and port are passed to the driver as explicit
     connection arguments. The driver therefore dials exactly the endpoint you confirmed;
     `PGHOST` and `PGPORT` can't redirect it.
   - The script **refuses** when the identity can't be verified:
     - several hosts;
     - a Unix socket;
     - no host;
     - an unrecognised host;
     - an invalid or ambiguous port;
     - a server with a Neon signature reached through a non-Neon hostname, such as a tunnel,
       proxy or IP.
   - A target that doesn't match is refused before the prompt and before any write.
3. Typing `seed <identity>` at an interactive terminal. The phrase is specific to that target.
   Without a terminal the script refuses.

**One connection for the whole run.**
1. The script opens one connection.
2. It reads the server facts (database name, SSL, Neon signature) in a read-only
   transaction, then ends that transaction.
3. It prompts you while the connection is **idle**: no transaction is open and no locks are
   held while it waits.
4. The write transaction starts on **that same connection**. It re-reads the server facts
   before taking locks or writing, and any difference aborts the run.

The connection is closed on success, on refusal, on errors and on Ctrl-C. The write can't
silently land on a different server reached by a fresh connection.

**What this does *not* guarantee:**
- **The identity doesn't prove where the endpoint leads.** It proves which host and port
  the client dials and which database name the server reports, not *where* that host and
  port really lead. You have to recognise the identity, and the runbook below requires
  using the Neon endpoint hostname in production, not a tunnel.
- **"Same connection" means the same client socket.** Through a transaction-mode pooler
  (Neon's `-pooler` host), the pooler may hand each transaction to a different backend
  process. They all belong to the same Neon endpoint and database.
- **A long pause can lose the connection.** If the confirmation takes a long time, the server
  or pooler may close the idle connection; Neon suspends idle computes. The write then fails
  before COMMIT, so nothing is written, and you just run it again.

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
Only then is the identity the Neon endpoint ID (`ep-…/neondb`).
- Through a tunnel to Neon, the script refuses if it detects the Neon signature.
- Through a tunnel to any other server, the identity would just be the tunnel's
  `host:port`. That proves nothing about where the tunnel leads.

1. Check the target is at `0006_member_profiles_skills` (`alembic current`).
2. Create a Neon backup branch.
3. Do a dry run against production. Check the printed identity
   (`ep-…/neondb`, the production endpoint) and the plan: 8 + 45 creates, 0 conflicts.
4. Get approval, then run `--apply --confirm-production --target <identity>` and type the
   phrase.
5. Run the dry run again. Expect `present` for every item.
6. `GET /api/v1/skills` now returns the 45 skills. Its shape is unchanged.
