# Phase 0 — Invite reliability

**Goal:** a friend can tap a link and land in a cellar. Clubs (phase 2) reuse this `/start` pipe. Today links expire in 7 days (one use), redeem 500’d on Postgres `FOR UPDATE` + `LEFT JOIN` for `kind=new`, and mint can still 500.

**Depends on:** nothing. **Unblocks:** phase 2, and any growth at all.

**Flag:** none. This is a fix to existing `/invite`, always on.

## Success metrics

Query existing tables. No new analytics model.

| Signal | Query (sketch) | “It worked” |
| --- | --- | --- |
| Mint succeeds | `HouseholdInvite` created, HTTP 201 not 500 | 7 days with **zero** mint 5xx in gunicorn/scanner logs |
| Redeem succeeds | `used_at` set, `TelegramUser` + `Token` + `HouseholdMembership` | 7 days with **zero** redeem 5xx; `test_lock_invite_sql_does_not_join` still green |
| Time-to-redeem | `used_at - created` | Median under the new TTL; fewer 410s than before (count `410` on redeem) |
| Cancel still works | `/invite cancel` deletes newest unexpired unused | No regression in `test_cancel_*` |

Staff eyeball: `/stats` or admin `HouseholdInvite` list (`code`, `kind`, `expires_at`, `used_at`).

## User journeys

### J0.1 Member mints a join link (happy)

1. Lee (already in Hillside) sends `/invite`.
2. Bot `POST /api/scanner/invites/` `{kind: "join"}` with `X-Household` (or `household_id` if several).
3. Bot replies with HTML card + Share button. Lee forwards that message (not a second private one).

English:

```
Join Hillside on Whiney.

Tap this, then Start:
https://t.me/<bot>?start=Ab3xY9k2

Forward this, or tap Share.

Works until 28 Nov, one use.
```

Button: `Share invite` → `https://t.me/share/url?url=...&text=Join Hillside on Whiney. Tap the link, then Start.`

Afrikaans (catalog `invite.join.share`):

```
Sluit by Hillside aan op Whiney.

Tik hier, dan Start:
https://t.me/<bot>?start=Ab3xY9k2

Stuur aan, of tik Deel.

Werk tot 28 Nov, een keer.
```

Button: `Deel uitnodiging`.

Copy change vs today: TTL line still uses `format_expiry`; after the TTL PR it says 30 days (`Works until {date}, one use.`). `EXPIRY_FALLBACK` today is `"One use, lasts 7 days."` → `"One use, lasts 30 days."`

### J0.2 Friend redeems (happy)

Unchanged flow: `/start Ab3xY9k2` → preview (staff) → Join → redeem (staff) → `_hot_add`. Existing cards: `JOIN_CARD`, `I1_COPY` / `I2_COPY` for `kind=new`. Keep `FRESH_LINK`, `UNREACHABLE` (transport ≠ expired).

### J0.3 Staff new-cellar link

`/invite new` or `/newcellar`. Unchanged except TTL and 500 hardening. Staff must pass `household_id` when minting **join** even if there is one cellar.

### J0.4 Cancel

`/invite cancel` — newest unexpired unused for this cellar (or staff new-cellar with null household). English already: `Cancelled the invite to {name} ({code}).`

## Telegram messages and buttons

No new commands. Touch existing strings in `telegram_invites.py` only where TTL or errors change.

| Situation | EN | AF | Buttons |
| --- | --- | --- | --- |
| Mint 5xx / IntegrityError | Couldn't make that invite. Try again in a minute. | Kon nie daai uitnodiging maak nie. Probeer oor 'n minuut. | none |
| Bad `X-Household` (was 500) | Couldn't make that invite. | Kon nie daai uitnodiging maak nie. | none |
| Expired / used (`410`) | That invite has expired or was already used. Ask them for a fresh link. | Daai uitnodiging het verval of is al gebruik. Vra vir 'n nuwe skakel. | none |
| Transport 5xx on preview/redeem | Couldn't reach the cellar just now — tap the link again in a minute. | Kon nie die kelder nou bereik nie — tik die skakel oor 'n minuut weer. | none |
| Network to Django down | same as transport | same | none |

Do **not** say “expired” on 5xx (`classify_invite_result` already maps this — keep it).

## Edge cases and errors

| Case | Today | Spec |
| --- | --- | --- |
| `kind=new` redeem `SELECT FOR UPDATE` with `select_related("household")` | Postgres 500 | Keep `lock_invite_qs` **without** join (`test_lock_invite_sql_does_not_join`). Reviewer gate. |
| `_mint_invite` 5× `IntegrityError` then raise | 500 | Catch, log, `503` `{"error": "could not mint a unique invite code"}`. Bot shows mint-fail copy. |
| Member mint, `X-Household` not an int | 500 in `_resolve_join_household` | `try/int` like `ScannerAPIView.get_household`; 400 `{"error": "No household found"}`. |
| Staff join mint missing `household_id` | 400 | Keep. |
| Double tap Join | second 410 unless same `chat_id` replay | Keep replay (`_replay`). |
| Expired row, `/invite cancel` | skipped (correct) | Keep. |
| Code collision household vs future club | n/a until phase 2 | Phase 0: add a comment on `generate_invite_code` that phase 2 must unique across both tables. Optional: extract `mint_unique_code()` now (small, worth it). |
| Bot username missing (`getMe` failed) | link broken | Keep existing warning; mint still returns code. |

## Data model

No new models. Change:

```python
# wine_cellar/apps/scanner/models.py
INVITE_TTL = timedelta(days=30)  # was days=7
```

`HouseholdInvite` fields unchanged: `code` CharField(8, unique), `kind`, `household` FK null, `created_by`, `created`, `expires_at`, `used_at`, `used_by`.

Migration: `Alter` nothing required if TTL is only applied on **new** rows (`save()` sets `expires_at` when empty; `_mint_invite` passes `timezone.now() + INVITE_TTL`). Existing unused 7-day rows keep their `expires_at`. Do **not** backfill longer expiry onto already-sent links (people may have screenshot the old “until {date}”).

Index: `code` already unique. Optional `Index(fields=["created_by", "used_at", "expires_at"])` for cancel — skip unless cancel is slow (it won’t be at this scale).

## API

Existing endpoints, behaviour tweaks only.

### `POST /api/scanner/invites/`

Auth: token. Body: `{ "kind": "join"|"new", "household_id": optional int }`.

201 body unchanged (`_serialize_invite`):

```json
{
  "code": "Ab3xY9k2",
  "kind": "join",
  "expires_at": "2026-11-28T12:00:00+00:00",
  "used": false,
  "expired": false,
  "inviter_display_name": "Lee",
  "household": {"id": 4, "name": "Hillside"},
  "household_name": "Hillside"
}
```

New: **503** on mint exhaustion; **400** on bad household id (no 500).

### `POST /api/scanner/invites/preview/`

Staff only. Unchanged. Body `{ "code" }` (code not in the URL, access logs).

### `POST /api/scanner/invites/redeem/`

Staff only. Unchanged contract. Keep lock without join. Keep `logger.exception("invite redeem failed")` but it should not fire for the outer-join bug.

### `POST /api/scanner/invites/cancel/`

Unchanged.

## Permissions and privacy

Unchanged: any household **member** may mint `join`; only **staff** mint `new`, preview, redeem (token in redeem response). Codes masked in logs (`mask_invite_code` → last two chars).

## Notifications / Celery

None. Redeem still pings the inviter (`{display} joined {household}.`) from the bot process, not Celery — keep it; it is one send on an interactive path.

## Web UI

None. Admin already lists invites (`HouseholdInviteAdmin`). Optional: show `expires_at` relative in admin — skip.

## Test plan

Keep all of `wine-cellar/tests/test_invites.py` and `scanner-service/tests/test_invites.py`. Add:

| Test | Assert |
| --- | --- |
| `test_mint_integrityerror_is_503` | mock 5 collisions → 503, no 500 |
| `test_mint_bad_x_household_is_400` | `HTTP_X_HOUSEHOLD=abc` → 400 |
| `test_lock_invite_sql_does_not_join` | still no `JOIN` in SQL |
| `test_locking_a_new_cellar_invite_does_not_outer_join` | still passes |
| `test_new_invite_ttl_is_thirty_days` | `expires_at > now+29d` (today’s test is `+6d`) |
| Bot: mint 503 shows “Couldn't make that invite” | `test_invites.py` |
| Bot: 5xx preview still `UNREACHABLE` not `FRESH_LINK` | existing classify tests |

## Rollout

1. Deploy Django (lock + 503 + int harden) **before** TTL copy so 500s die even if TTL PR lags.
2. Deploy TTL + bot fallback string together so the card’s date and “30 days” match.
3. No flag. No scanner restart required except for bot copy (restart scanner after the copy PR).
4. Watch gunicorn logs 48h for `invite redeem failed` and `IntegrityError`.

## Task breakdown (separate PRs)

| PR | Scope | Effort |
| --- | --- | --- |
| **0a** | `lock_invite_qs` comment + 503 on mint collision + `int()` guard on `_resolve_join_household`; tests | S |
| **0b** | `INVITE_TTL = 30 days`; update `test_member_can_mint_join` (+6d → +29d); `EXPIRY_FALLBACK` + any “7 days” in invite copy (EN + AF keys if catalog exists; else English only and a TODO) | S |
| **0c** (optional) | Extract `mint_unique_code(model)` used by `HouseholdInvite`, documented for phase 2 | S |

Do 0a first. 0b can follow immediately. 0c only if it stays tiny.

**Phase total: S** (two small PRs).
