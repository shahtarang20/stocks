# Master prompt: "My saved setups" for Rent a GPU (an ALTERNATIVE, nothing is removed)

Give this whole file to the engineer or AI that will build the feature. It is written to be followed in order. Read section 1 first: it is the rule that overrides everything else.

---

## 1. The one rule that overrides everything

**This feature is an ALTERNATIVE beside what already exists. It adds a new way to start a rental. It does not replace, remove, rename, hide or change the behaviour of anything that exists today.**

After the feature is built, ALL of these must still work exactly as they do now:

| Existing feature | Where it lives | Must stay |
|---|---|---|
| Ready-made templates (`pytorch-jupyter`, `vision-jupyter`) | `apps/api/app/data/rental_templates.json`, `GET /v1/rental/templates`, Rent page radio "A ready-made template" | unchanged |
| "My own image" (public image pinned by `@sha256:`, terminal or web page, custom command) | Rent page radio "My own image" | unchanged |
| Kits (ready-made tools) | `/dashboard/kits`, `kits/` | unchanged, untouched |
| Rental creation | `POST /v1/sessions` and its rules in `apps/api/app/services/sessions.py` (`create_session`, `_prepare_rental`, `_rental_limits`) | **request and response shapes unchanged**, no new required field |
| Interactive sessions, export, autosave, billing | `apps/api/app/api/routers/sessions.py`, the agent | unchanged |
| Host opt-in, `computecoin-agent rental ...`, network filter | `provider-agent/` | **do not touch the agent at all** |
| Services, BYOC jobs, private tiers, multi-node | their routers | unchanged |
| Every existing test | `apps/api/tests`, `apps/web/src`, `provider-agent/tests`, `rental/tests`, `kits/tests` | **must pass UNCHANGED** (if you must edit an existing test, you have broken the rule: stop and reconsider) |

A saved setup is only a **stored copy of form values** that the user can load back into the existing Rent form. Starting a rental from a saved setup goes through the **same** `POST /v1/sessions` and the **same** validation as any other rental. There is no second, privileged path.

If at any point a design choice would remove or change an item in the table above, stop and choose the additive option instead.

---

## 2. What you are building (plain words)

A signed-in customer can:
1. On **Rent a GPU**, fill in the form as today (template or own image, size, internet choices, start command, price, times), then press **Save this setup**, give it a name, and it is stored privately for them.
2. Next time, choose a third option under "Start from": **One of my saved setups**, pick one, and the form is filled in. They can change anything, tick the warning and start, exactly as today.
3. See a short list of their saved setups with **Rename** and **Delete**.

Out of scope (do NOT build): sharing a setup with other people, a public catalogue, saving input files, saving the chosen computer, building images from a Dockerfile, any change to the agent, any change to billing.

---

## 3. Locked rules of this project (do not break)

- **Billing and the ledger are untouched** (project rule L3). No change under `app/services/ledger.py`, `billing.py`, or the session settlement.
- **No secrets in the repository** (L7). Never write a password, token, key or database address into a file, test, doc or command output you paste.
- **Design note first** (L8). Add a short section to `docs/architecture/RENTAL-container-rental-design.md` (a new numbered section after section 13) BEFORE the code. It states the table, the routes, the errors and the "alternative, nothing removed" rule.
- **OS-specific code only in `provider-agent/computecoin_agent/platform/`.** This feature has none; do not touch `provider-agent/`.
- Work inside `/home/root509/stocks` only. Do not install anything system-wide. Do not add any dependency.
- Do **not** commit or push unless the owner says so. Do not start extra agents or forks: do the work yourself.
- Report honestly: if a test fails, say so with the output. Never claim something works that you did not run.

---

## 4. Where things are (read these before writing code)

API (`apps/api`):
- `app/services/rental.py`: the pure checks you must REUSE, not copy: `registry_allowed`, `IMAGE_RE`, `clean_command`, `clean_relpath`, `egress_allowlist`, `get_template`, `available_templates`, the limits constants.
- `app/services/sessions.py`: `_prepare_rental` (create rules 3 to 7), `_rental_limits` (memory, cores, workspace). Read them; call the same functions for validation. If you must extract a shared function so both create and setups use it, **extract only**, keep the behaviour identical, and prove it with the existing tests passing unchanged.
- `app/api/routers/sessions.py`: where the rental routes live (`GET /v1/rental/templates` is here). Put the new routes in the **same router file** or a new small router included the same way.
- `app/models/sessions.py`, `app/db/base.py`: model style. Latest migration is `alembic/versions/0044_container_rental.py` (idempotent style: copy it). The new one is `0045_rental_setups.py` with `down_revision = "0044_container_rental"`, and it only adds one table. `downgrade` drops only that table.
- `app/core/config.py`: settings live here (see `RENTAL_*`).
- `app/deps.py`: `require_verified_user`, `get_current_user`.
- Tests: `apps/api/tests/test_container_rental.py` (patterns: `rental_on` fixture, `_login`, `_create`, `_machine`), `tests/helpers.py` (`register_verify_login`, `auth_headers`, `add_user_row`).

Web (`apps/web/src`):
- `app/dashboard/rent/page.tsx`: the Rent form (the two radios "A ready-made template" and "My own image" are here and STAY).
- `lib/rental.ts`: `RentalForm`, `EMPTY_FORM`, `buildRentalRequest`, error texts (`rentalErrorText`), guidance constants. `lib/sessions.ts`: `sessionsApi`. `lib/api.ts`: the client.
- `components/rental/customer-rental-guide.tsx`: the "How renting a graphics card works" box (update its wording, do not remove steps).
- `app/(marketing)/help/page.tsx`, `docs/PLATFORM_GUIDE.md` section 1.13: update the text.
- Tests: `lib/rental.test.ts`, `components/rental/*.test.tsx`, `a11y/pages.test.tsx` (add entries).

---

## 5. Data and API contract

### 5.1 Table `rental_setups`
| Column | Type | Rule |
|---|---|---|
| `id` | uuid, primary key | |
| `owner_id` | uuid, foreign key to `users.id`, `ON DELETE CASCADE`, indexed | the only person who can see or use it |
| `name` | string, 1 to 60 characters | trimmed, no control characters, **unique per owner, ignoring case** |
| `template` | string or null | a template slug, or null |
| `image_digest` | string or null | a pinned custom image, or null; **exactly one of `template` and `image_digest`** |
| `mode` | `pty` or `tcp` | |
| `tcp_port` | integer or null | required when `mode` is `tcp` |
| `command` | JSON list or null | same rules as `clean_command` |
| `gpu_count`, `memory_mb`, `cpus`, `workspace_gb` | integers | same ranges as `_rental_limits` and session create |
| `max_price_per_gpu_hour` | decimal | a default the user can change |
| `max_duration_seconds`, `idle_timeout_seconds` | integers | same ranges as session create |
| `egress` | JSON list of `host:port` | each entry must pass the existing allow-list |
| `created_at`, `updated_at` | timestamps | |

**Never stored:** the "I accept the warning" tick (`accept_rental_terms`: the user must tick it every time), input files, the chosen computer, any key or token.

### 5.2 Routes (all need a signed-in, email-verified user; all return only the caller's own rows)
- `GET /v1/rental/setups` returns the list, each with `available` (true/false) and `problem` (null, or a code such as `template_not_found`, `image_registry_not_allowed`). A setup whose template was removed, or whose registry is no longer allowed, is still shown, marked unavailable, and can be renamed or deleted.
- `POST /v1/rental/setups` creates one. 201 on success.
- `PATCH /v1/rental/setups/{id}` renames and/or updates the settings.
- `DELETE /v1/rental/setups/{id}` deletes. 204.

### 5.3 Rules and error codes (follow the existing `{"detail": "<code>"}` style)
- Container rental switched off on the platform: `container_rental_not_enabled` (same code and status as today).
- Name empty, too long or bad characters: 422 `setup_name_invalid`. Duplicate name for this user: 409 `setup_name_taken`.
- More than `RENTAL_MAX_SAVED_SETUPS` (new setting, default 20) per user: 409 `too_many_saved_setups`.
- Anything inside the settings: **reuse the existing validators and the existing error codes** (`image_must_be_pinned_by_sha256_digest`, `image_registry_not_allowed`, `egress_not_allowed`, `rental_limit_out_of_range`, `invalid_command`, `template_not_found`, ...). Do not invent a second set.
- Someone else's id, or an id that does not exist: **404 for both** (never reveal that it exists).
- Rate limit the write routes like other write routes in this API.

### 5.4 Using a saved setup
The website fills the form from the setup and the user submits **the normal rental request**. The server never "starts a setup". So every rule at use time is checked again by `POST /v1/sessions`. This is deliberate: it keeps one validation path and means a stale setup can never bypass a rule.

---

## 6. Website contract

On the Rent page:
- Keep the two radios exactly as they are. **Add a third**: "One of my saved setups". It is disabled, with a one-line reason, when the user has none.
- Under "Start from", show a short line for each choice saying who it is for:
  - Ready-made template: "You write code, we provide the environment."
  - My own image: "You already have a container image. It must be public."
  - Saved setup: "Repeat something you set up before."
- Choosing a saved setup shows a picker; picking one **fills the form** (source, template or image, command, port, sizes, internet choices, price, times). Everything stays editable. Unavailable setups are shown greyed with the reason in plain words and cannot be loaded.
- A **Save this setup** button near the Start button opens a small dialog (name field, Save, Cancel). Errors appear in plain words using `rentalErrorText` (add the three new codes there).
- A small **My saved setups** list with Rename and Delete (Delete asks for confirmation).
- The warning checkbox is never filled from a setup.
- Plain language everywhere, no jargon, no raw error codes shown to a person. Accessible: labels, focus order, `aria-live` for save and delete results.

Update the guidance (do NOT delete existing steps): `customer-rental-guide.tsx` (one sentence on saving a setup), the Help page (a question "Can I save my settings?"), `docs/PLATFORM_GUIDE.md` section 1.13.

---

## 7. Tests (every one must exist and pass)

API (`apps/api/tests/test_container_rental_setups.py`):
- create, list, rename, update, delete; the happy path for a template setup and for a custom-image setup.
- every rule and error code in 5.3, including the duplicate-name-ignoring-case case, the per-user limit, and `container_rental_not_enabled`.
- **ownership**: user B cannot read, update or delete user A's setup (404), and A's list never contains B's.
- an unavailable setup (template removed, registry no longer allowed) is listed with `available: false` and the right `problem`, and can still be deleted.
- the saved values really round-trip, and the accept-terms tick is never stored.
- **alternative proof**: the existing rental flows still work with no saved setups; creating a rental never reads the setups table.
- a migration test: upgrade, downgrade, upgrade is clean.

Website:
- `lib/rental.ts` helpers: turning a setup into form values and a form into a setup; the new error texts have no underscores.
- component tests for the third option (disabled when empty, fills the form, unavailable setups cannot be loaded), the save dialog, rename and delete.
- add the new pieces to `a11y/pages.test.tsx`.
- a test that the two original radios are still present with their original labels.

---

## 8. Lessons from this project (these caused real failures: do not repeat them)

1. **Run the API tests on Postgres as well as SQLite.** SQLite does not enforce foreign keys; Postgres does. A test that inserts a row pointing at a user, project or machine that does not exist passes on SQLite and fails on Postgres. Create real rows (see `add_user_row` in `tests/helpers.py`). CI runs both.
2. **Run agent-style tests with plain `pytest`**, and never import helpers as `from tests.x import ...` inside `provider-agent/tests` (not a package on CI). (This feature should not touch the agent anyway.)
3. **No test may depend on the clock ticking within one second.** The wallet test failed at random because it asked for a time-stamped challenge twice. Compute a value once and reuse it.
4. **Web tests run in parallel and can time out on a slow machine.** `vitest.config.ts` already has a 30 second timeout; do not lower it.
5. **Never claim something on screen that the code does not do.** Add a test that ties each promise to the code, as `lib/rental.test.ts` does for the host commands, the 10-minute wait and the 50 GB default.
6. **No secrets, passwords or addresses in any file or paste.**
7. **A green tick on a deploy can mean nothing was deployed.** Do not tell the owner it is live until the live API shows the new routes.

---

## 9. Order of work

1. Read sections 1, 3 and 4. Read `docs/architecture/RENTAL-container-rental-design.md`.
2. Write the design addendum (section 14 of that note) first.
3. Migration `0045_rental_setups` and the model.
4. API routes and the new setting `RENTAL_MAX_SAVED_SETUPS`.
5. API tests; run them on SQLite **and** Postgres.
6. Website: client, form option, dialog, list, error texts, guide wording.
7. Website tests, accessibility tests.
8. Docs: platform guide, help page, design note.
9. Run **every** suite (section 10). Fix anything you broke.
10. Report: what was added, the test numbers, and what you could not verify. Do not commit or push unless told.

## 10. Commands to run before saying "done"

```
cd apps/api      && .venv/bin/python -m pytest -q                       # SQLite (about 30 minutes in full)
cd apps/api      && TEST_DATABASE_URL=<a throw-away local Postgres> .venv/bin/python -m pytest -q
cd provider-agent && python -m pytest -q                                # must be unchanged: 0 edits to this folder
cd .             && python -m pytest -q rental/tests kits/tests
cd apps/web      && npx tsc --noEmit && npx next lint && npx vitest run && npm run build
```

## 11. Definition of done (all must be true)

- [ ] A user can save, list, rename, delete and load a setup, and start a rental from it.
- [ ] The two original radios, the templates, "My own image", Kits and every route in the table in section 1 behave as before.
- [ ] No existing test was edited, and all existing tests pass.
- [ ] The new tests pass on SQLite and on Postgres.
- [ ] The migration upgrades and downgrades cleanly and only adds one table.
- [ ] A user can never see or use another user's setup.
- [ ] No file in `provider-agent/` changed. No ledger or billing file changed.
- [ ] The on-screen guide and the platform guide describe the new option in plain words.
- [ ] No secret was written anywhere.

## 12. After the build (the owner does this)
1. Push, and watch **Actions → CI** for green.
2. Click **Deploy** on the Railway `stocks` service (the migration runs by itself when the API starts).
3. Check the live API has the new route (it should answer 401, not 404, without a login).
4. Vercel updates the website by itself.

Estimated size: about 1,000 to 1,200 lines, of which about 550 are code. No new account, secret or paid service is needed.
