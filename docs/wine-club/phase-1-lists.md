# Phase 1 — To-drink lists

**Goal:** photograph a bottle you don’t own (shop, mate’s house) and remember it **without** putting it in stock. Come back when you buy it and move it into the cellar.

**Depends on:** nothing (phase 0 is unrelated). **Unblocks:** phase 3 club lists / “add from feed.”

**Flag:** `ServiceConfig.feature_lists` = `on`/`off`. Default off until rollout.

This is the cheap solo-user adoption loop. It does not need clubs.

## Success metrics

| Signal | How | “It worked” |
| --- | --- | --- |
| People add to the list | `WineListItem` rows with `moved_at IS NULL` | ≥ 3 households have ≥ 1 item within 2 weeks of flag-on |
| People come back | `moved_at` set via to-cellar | ≥ 1 move-to-cellar in that window (even one real buy is the loop) |
| Not polluting stock | `StorageItem` count vs list items | List items **never** appear in `GET /api/wines/in-stock/` or `/fridge` |
| Button discoverable | label-card taps `td` vs `look` vs `grok` (scanner logs `[callback]`) | To-drink used at least as often as Just looking among unmatched labels |
| Repeat use | items per household | A household with 3+ items is a stronger signal than one souvenir row |

Staff: Django admin on `WineList` / `WineListItem`. Optional `/stats` line: `lists: 12 items (4 moved)`.

## User journeys

### J1.1 Unmatched label → To-drink (happy)

Sam photographs a bottle in a shop. Triage = label, no household match. Card as today (Grok summary, maybe `REFS_PENDING_NOTE`), buttons:

```
[✅ Add to cellar]
[📝 To-drink list]
[🍷 Drank this]
[👀 Just looking]
[That's the wrong wine]
```

Afrikaans labels: `Voeg by kelder` / `To-drink-lys` / `Dit gedrink` / `Kyk net`.

Tap **To-drink list** → `POST /api/lists/<id>/items/` with the card’s merged payload. Card stays; buttons drop (same as Just looking) plus note:

EN: `📝 On your To-drink list.`
AF: `📝 Op jou To-drink-lys.`

If a live reference lookup is still running, it may still edit ratings into the card (same as Just looking). Do not resurrect Add/To-drink buttons.

### J1.2 Already in stock

Wine matches household stock. Show To-drink anyway (second-bottle reminder). Unique `(list, wine_id)`: second tap edits `payload` / bumps a `touched_at`, does not duplicate. Copy: `Already on your To-drink list.`

If they wanted stock, they use Add to cellar / the existing match buttons.

### J1.3 Browse and move to cellar

`/list` (also in `/` menu and `/help`).

Empty:

```
📝 To drink

Nothing here yet.

Photograph a bottle you don't own — then tap To-drink list.
```

AF: `Niks hier nie. Neem 'n foto van 'n bottel wat jy nie besit nie — dan tik To-drink-lys.`

With items (page of 5, like `/recent`):

```
📝 To drink  1–5 of 8

1. 🍷 Chenin Blanc 2022
    Coastal Cellars · Stellenbosch
2. ...

[Move to cellar] [Remove]   ← per row, or tap row then actions
[Older 5]
```

Tap **Move to cellar** on item 1 → fridge picker if several storages (reuse add-stock picker) → `POST .../to-cellar/` → drank/add path creates `Wine` if needed + `StorageItem` → item `moved_at` set → card:

EN: `✅ Chenin Blanc 2022 is in the cellar.`
AF: `✅ Chenin Blanc 2022 is in die kelder.`

Badges fire because this is a real `award_for_add`. Then the normal add-stock buttons (drawer, undo) if the add path returns them.

**Remove:** `DELETE` item. Copy: `Removed from To-drink.`

### J1.4 Just looking still exists

Tap Just looking → `TasteIntent.look`, `JUST_LOOKING_NOTE`, **not** a list row. Different verb: ephemeral vs remember.

### J1.5 Flag off / no list

`/list` → `To-drink lists aren't on yet.`
To-drink button hidden if flag off (don’t show a dead control).

### J1.6 Typed / barcode unknown

Unmatched barcode flow that already asks for a label photo: after the label card, same To-drink button. No To-drink on a barcode-only prompt that has no Grok payload yet (nothing to snapshot).

## Telegram flows (commands and callbacks)

**Command:** `/list` — `PUBLIC_COMMANDS` + `set_bot_presence`. Description: `Things to drink or buy`. AF menu: `Om te drink of te koop` (Telegram menu is per-user language if we set it; today the menu is English-only — keep English menu text until a dedicated i18n PR; body copy uses the catalog).

**Callbacks** (64-byte budget, reuse `_cb`):

| action | meaning | extra |
| --- | --- | --- |
| `td` | add current label card to To-drink | `b:<temp_key>` or `w:<wine_id>` |
| `lm` | `/list` page | `p:<page>` |
| `lc` | move item to cellar | `i:<list_item_id>` |
| `lx` | remove item | `i:<list_item_id>` |
| `ls` | pick storage then move | `i:`, `p:<storage_id>` as today |

`list_item_id` fits; do not put names in callback_data.

Handler module: `scanner-service/integrations/telegram_lists.py`. Wire from `telegram_bot.py` like invites (don’t grow the 8k-line file).

### Copy catalog (new keys)

| key | EN | AF |
| --- | --- | --- |
| `list.btn.add` | To-drink list | To-drink-lys |
| `list.note.added` | On your To-drink list. | Op jou To-drink-lys. |
| `list.note.already` | Already on your To-drink list. | Reeds op jou To-drink-lys. |
| `list.note.in_cellar` | Already in the cellar — add another bottle from here, or open /fridge. | Reeds in die kelder — voeg nog 'n bottel hier by, of maak /fridge oop. |
| `list.empty` | Nothing here yet.\n\nPhotograph a bottle you don't own — then tap To-drink list. | Niks hier nie.\n\nNeem 'n foto van 'n bottel wat jy nie besit nie — dan tik To-drink-lys. |
| `list.moved` | {name} is in the cellar. | {name} is in die kelder. |
| `list.removed` | Removed from To-drink. | Van To-drink afgehaal. |
| `list.off` | To-drink lists aren't on yet. | To-drink-lyste is nog nie aan nie. |
| `list.err` | Couldn't update that list. Try again. | Kon nie die lys bywerk nie. Probeer weer. |
| `list.title` | To drink | Om te drink |

## Edge cases and errors

| Case | Behaviour |
| --- | --- |
| Flag off | Hide button; `/list` one-liner; API 404 `not_enabled` |
| No household | Same 403 as other scanner APIs (`You do not belong to a household yet.`) |
| Empty Grok name | Do not add; answer `I need a name on the label first.` (keep card) |
| Duplicate wine_id on list | 200 with existing item (idempotent), not 409 |
| Duplicate free-text (no wine_id) | Allow two rows (names collide); user can Remove |
| Move to cellar, wine unique constraint | Find existing `Wine` by household unique key, then stock add; do not 500 |
| Move, `recent_duplicate` 409 from stock add | Show existing duplicate confirm UI; on confirm, still set `moved_at` |
| Remove already moved | 409 `already_moved` |
| `/fridge` / in-stock / drink-by | Ignore list rows (tests) |
| Badge on add-to-list | **No.** Badges on move-to-cellar / stock add only |
| Club feed | Not in this phase |
| Named extra lists (`Holiday`) | Not in this phase. One default list per household |
| Partner sees Sam’s shop finds | Yes — household-scoped (decided). `added_by` shown as `· Sam` on the row if ≠ viewer |
| Scanner restart mid-card | temp_key in pending_store; same as Add to cellar. If pending gone: `That card's finished — send the photo again` |
| Item payload huge | Cap JSON at ~16 KiB; strip image bytes; keep fields `WineCreateSerializer` needs |

## Data model

App: `wine_cellar.apps.wine` (not club). Migration `0030_winelist.py` (next after current `0029_tasteintent_ask`). Confirm the number at implement time if another wine migration has landed.

```python
class WineList(models.Model):
    KIND_PERSONAL = "personal"
    KIND_CLUB = "club"  # unused until phase 3; column exists so we don't migrate twice
    KIND_CHOICES = [(KIND_PERSONAL, "Personal"), (KIND_CLUB, "Club")]

    household = models.ForeignKey(
        "scanner.Household", on_delete=models.CASCADE, related_name="wine_lists",
    )
    name = models.CharField(max_length=40)
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=KIND_PERSONAL)
    club_id = models.PositiveIntegerField(null=True, blank=True)  # no FK yet (club app doesn't exist)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    created = models.DateTimeField(auto_now_add=True)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["household", "name"],
                name="unique_winelist_name_per_household",
            ),
        ]
        indexes = [
            models.Index(fields=["household", "kind"], name="winelist_hh_kind_idx"),
        ]

class WineListItem(models.Model):
    SOURCE_CHOICES = [
        ("label", "Label photo"),
        ("barcode", "Barcode"),
        ("typed", "Typed"),
        ("club_feed", "Club feed"),  # phase 3
    ]
    list = models.ForeignKey(WineList, on_delete=models.CASCADE, related_name="items")
    name = models.CharField(max_length=100)
    producer = models.CharField(max_length=100, blank=True, default="")
    vintage = models.PositiveIntegerField(null=True, blank=True)
    wine_type = models.CharField(max_length=2, blank=True, default="")
    country = models.CharField(max_length=3, blank=True, default="")
    grape = models.CharField(max_length=100, blank=True, default="")
    barcode = models.CharField(max_length=100, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    wine = models.ForeignKey(
        "wine.Wine", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="list_items",
    )
    source = models.CharField(max_length=12, choices=SOURCE_CHOICES, default="label")
    added_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
    )
    created = models.DateTimeField(auto_now_add=True)
    touched_at = models.DateTimeField(auto_now=True)
    moved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["list", "wine"],
                condition=models.Q(wine__isnull=False, moved_at__isnull=True),
                name="unique_open_list_item_per_wine",
            ),
        ]
        indexes = [
            models.Index(fields=["list", "moved_at", "-created"], name="winelistitem_open_idx"),
        ]
        ordering = ["-created"]
```

Default list: get_or_create `name="To drink"`, `kind=personal` on first `/list` or first add. Also create on new-cellar redeem (same place Storage `Cellar` is created) so the row exists before the first photo.

`club_id` integer without FK: avoids a cross-app dependency before phase 2. Phase 3 adds a real FK or a check.

Do **not** subclass `UserContentModel` if we already have `added_by` / `created_by` — pick one provenance field (`added_by` on items, `created_by` on lists).

## API

Base: `ScannerAPIView` (token, `X-Household`). Flag checked in a mixin `ListsEnabledMixin`.

Mount: `path("lists/", ...)` under `/api/` (`wine_cellar/apps/api/urls.py`).

### `GET /api/lists/`

Creates default list if missing.

```json
{
  "lists": [
    {
      "id": 1,
      "name": "To drink",
      "kind": "personal",
      "item_count": 3
    }
  ]
}
```

### `GET /api/lists/<id>/?limit=5&offset=0`

404 if list not in household or `kind=club` (phase 1: club lists 404).

```json
{
  "id": 1,
  "name": "To drink",
  "count": 8,
  "offset": 0,
  "limit": 5,
  "items": [
    {
      "id": 44,
      "name": "Chenin Blanc",
      "producer": "Coastal Cellars",
      "vintage": 2022,
      "wine_type": "WH",
      "country": "ZA",
      "grape": "Chenin Blanc",
      "barcode": "",
      "wine_id": null,
      "source": "label",
      "added_by_name": "Sam",
      "created": "2026-11-01T18:04:00+00:00",
      "moved_at": null
    }
  ]
}
```

Open items only (`moved_at` null) unless `?include_moved=1` (not used by the bot).

### `POST /api/lists/<id>/items/`

```json
{
  "name": "Chenin Blanc",
  "producer": "Coastal Cellars",
  "vintage": 2022,
  "wine_type": "WH",
  "country": "ZA",
  "grape": "Chenin Blanc",
  "barcode": "6001234567890",
  "wine_id": null,
  "source": "label",
  "payload": { "...": "WineCreateSerializer-shaped blob" }
}
```

201 item object. 200 if unique wine hit (idempotent). 400 if `name` blank. 404 list. `payload` stored as-is after stripping keys not in an allowlist (`name`, `producer`, `vintage`, `grape_variety`, `region`, `appellation`, `country`, `abv`, `wine_type`, `category`, `barcode`, `description`, `vineyard`, `size`, `critic_score`, `vivino_rating`, `market_price`, `market_price_currency`, … — same as create wine). **Never store image bytes.**

### `DELETE /api/lists/<id>/items/<item_id>/`

200 `{ "status": "removed", "id": 44 }`. 409 if `moved_at` set. 404 if not in household.

### `POST /api/lists/<id>/items/<item_id>/to-cellar/`

```json
{ "storage_name": "Cellar", "row": null, "price": null }
```

Server:

1. If `moved_at`: 409 `already_moved`.
2. Resolve `Wine`: `wine_id` if set and still in household; else `WineCreateSerializer` from `payload` + snapshot fields; on unique constraint, fetch existing.
3. `StockAddView` logic (extract a function rather than HTTP-to-self): create `StorageItem`.
4. Set `item.wine`, `item.moved_at=now()`.
5. Return stock-add payload + `list_item_id` so the bot can show undo/drawer/achievements.

```json
{
  "item_id": 9001,
  "wine_id": 55,
  "wine_name": "Chenin Blanc 2022",
  "stock_count": 1,
  "storage": "Cellar",
  "action": "added",
  "achievements": [],
  "list_item_id": 44
}
```

Undo of that stock add does **not** automatically restore the list row in phase 1 (document as known; phase 3 can fix). Simpler: leave it moved.

## Permissions and privacy

Household-scoped, same as stock. Any household member can add/remove/move. No extra role. Not visible to other households. Not visible to clubs in this phase.

`added_by` is provenance, not an ACL.

## Notifications / Celery

None. No fan-out. Do not ping the partner that Sam listed a shop bottle.

## Web UI

Optional `/list/` template, Django + Pure CSS, `HouseholdScopedMixin`. **Not required to ship.** Admin:

- `WineListAdmin`: household, name, kind, item count
- `WineListItemAdmin`: list, name, vintage, moved_at, added_by

No React.

## Test plan

Django (`tests/test_winelist.py`):

- default list created on GET
- add item, not in `WineInStockView`
- unique open `(list, wine)`
- to-cellar creates stock + `moved_at`; second to-cellar 409
- to-cellar unique-wine collision finds existing
- to-cellar fires `award_for_add` (with gamification on)
- flag off → 404
- other household 404
- payload rejects giant / image keys

Bot (`tests/test_lists.py`):

- unmatched label keyboard contains To-drink when flag on, not when off
- tap `td` calls API, note appended, buttons gone
- Just looking does **not** call list API
- `/list` empty / page / move / remove
- Afrikaans fixture: `list.note.added` key exists

Scanner integration: `WineCellarAPI.add_list_item`, `list_items`, `move_list_item_to_cellar`, `delete_list_item`.

## Rollout

1. Migrate + API behind flag **off**.
2. Bot understands flag (hide button). Deploy scanner.
3. Flip `feature_lists=on` in Django admin `ServiceConfig`. Restart scanner **or** config refresh so `/list` is in the menu (`set_bot_presence`).
4. Watch 24h: 4xx on `/api/lists/`, empty `/fridge` regressions (there must be none).
5. If rollback: flag off (API 404, button hidden). Rows stay.

## Task breakdown (separate PRs)

| PR | Scope | Effort |
| --- | --- | --- |
| **1a** | `WineList` / `WineListItem` models, migration, admin, default-list helper, `feature_lists` read | S |
| **1b** | API GET/POST/DELETE + tests (`test_winelist.py`), flag mixin | M |
| **1c** | `POST .../to-cellar/` extracting shared stock-add helper; badge + unique-constraint tests | M |
| **1d** | `WineCellarAPI` methods; `telegram_lists.py`; label-card button; `/list`; copy catalog EN+AF for new keys; `PUBLIC_COMMANDS` + presence | M |
| **1e** | Wire new-cellar redeem to create default list; CLAUDE.md; `/help` line | S |

**1a → 1b → 1c** (Django) then **1d** (bot) then **1e**. Do not combine 1b and 1d.

**Phase total: M** (four PRs, one of them S).
