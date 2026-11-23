# Phase 5 — social redesign: P1 threads and swipe-reply

Flag `feature_club_threads` (ServiceConfig, default off, scanner reads it at startup). It only takes effect when `feature_clubs` **and** `feature_club_comments` are also on. Off: everything behaves as phase 3 (`refresh_event_comment_card` edits the card, Say something is a 2-minute in-memory prompt).

## Behaviour (flag on)
- **Swipe-reply** (Telegram `reply_to_message`) to any bot message recorded for an event (feed card, reply ping) is a comment on that event, 280 chars max (`club.comment.too_long`). No Say something step. Replies to anything else fall through to normal chat. Resolved via `GET /api/clubs/threads/resolve/?chat_id=&message_id=` (caller must own the chat and be in the club).
- **Say something / Reply** stays. The prompt lasts 15 minutes and lives in a `CardStore` (prefix `whiney:clubcm:`, Redis when `REDIS_URL` is set), so it survives a scanner restart. The next message is taken as the comment only inside 2 minutes of the tap or as a Telegram reply to the prompt, card or ping; later text is asked first (`Post` / `No, it's for me`, `cl:pt` / `cl:pn`; the second hands the original text to the normal chat router). A command, label, Cancel or other flow drops it.
- **Ping**: each comment pings every member except its author, `feed_muted` memberships and members who muted the thread. The ping is a new message with `reply_to_message_id` = the member's own card delivery (else their latest ping). Only a 400 about the reply target ("message to be replied not found" / "reply message not found") resends the ping fresh with the card line quoted ("On: ..."). Pings for retracted events are skipped, and a ping body is capped at about 3500 characters (newest comments, then "+N more" and a `Thread (n)` button). One unsent notice per (recipient, event) is enforced by a partial unique constraint; a lost enqueue race merges into the existing row. Swipe-replying to a retracted event answers `club.thread.retracted`; a resolve network/API error (not 404) answers `club.thread.post_failed`. Telegram failures (403, 400, network) are written to `ClubNotice.error` and `ClubEventDelivery.error`, never raised.
- **Batching**: comments on the same event within 2 minutes (`BATCH_WINDOW`) merge into one `ClubNotice` per recipient (`payload.comment_ids`) and go out as one combined ping. Rate limit: at most 6 pings per recipient per minute; extras are deferred a minute.
- **Buttons** on a ping: `Reply` (`cl:rp:<event>`, same 15-minute prompt) and `Mute thread` / `Unmute thread` (`cl:mt:<event>`, toggles; the button flips in place). Feed rows get `Thread (n)` (`cl:th:<club>:<event>:<page>`): the whole thread, paged below 3600 characters.
- Every comment is shown (no cap of 3). `POST /api/clubs/ask/` kind `comments` adds `threads` (full threads of matching events) when the flag is on.

## Data
- `ClubEventDelivery.kind` (`card` / `ping` / `ask`), index `(chat_id, message_id)`; the unique `(event, chat_id)` constraint now applies to `kind=card` only.
- `ClubNotice` outbox: recipient, club, event, kind, unique `dedupe_key`, payload, due_at, sent_at, error. Flushed by Celery `flush_club_notices` (scheduled 2 min after a comment, plus a beat safety net every minute). Phase 4 will add quiet hours and digests here.
- `ClubThreadMute` (user, event), unique.
- API: `GET /api/clubs/threads/resolve/`, `GET/POST /api/clubs/events/<event_id>/mute/` (POST toggles, or `{muted: bool}`). Both 404 `not_enabled` with the flag off.

## Tests
`wine-cellar/tests/test_club_threads.py`, `scanner-service/tests/test_club_threads.py`. Telegram HTTP is always mocked.

# P2 reactions and Want this

Flag `feature_club_reactions` (ServiceConfig, default off; needs `feature_clubs`). Off: cards, pings and the `cl:td` To-drink button behave exactly as before.

- **Palette** 🍷 😍 👍 😂 🤔: one compact row on opened-wine feed cards and comment pings (`reaction_rows` / `reaction_row` in `club/reactions.py`; `reaction_buttons` in `telegram_clubs.py`). A button shows its count when above zero ("🍷 3"). Callback `cl:rx:<event>:<i>` toggles the tapper's `ClubReaction` (unique event, user, emoji). The tapper gets a toast (no new message) and their own keyboard's emoji row is refreshed in place. Phase 3 relay asks reuse the same helper.
- **Count edits**: `refresh_event_reactions` edits members' delivered card copies (`editMessageReplyMarkup`). One pass per event per 30 s: the cache key `club:rxrefresh:<event>` is the dedupe key, the task runs 30 s after the first tap and reads the counts then. "message is not modified" and "message to edit not found" are harmless; other errors are logged. Retracted events are skipped. Comment-ping copies show counts as of when they were sent (only the tapper's own copy updates live).
- **Owner pings**: only the event's actor, never for their own taps. `ClubNotice` kind `reaction`, payload `{"rx": {user_id: [emoji]}, "wants": [user_id]}`, 2-minute batch window, one pending row per (recipient, event, kind). Un-react inside the window removes that line (empty notice is deleted); after the send nothing is retracted. Text: "Sam and Pat reacted 🍷😍 to your 2019 Merlot", plus "Pat saved it to their To-drink list" (or "... saved your <wine> to ..." when no reaction line). Owner `feed_muted` and thread mute are honoured at send time. No buttons; recorded as a `ping` delivery so a swipe-reply to it comments on the event.
- **Want this** (`cl:wt:<event>`): replaces the To-drink button on cards while the flag is on and `feature_lists` is on. `POST /api/clubs/events/<id>/want/` reuses the feed-to-list code (`add_event_to_list`, idempotent, source `club_feed`) for the tapper's own personal list and records a `ClubWant` row; the owner is told once per (event, tapper). Lists off: toast `club.td.off`. A stale `cl:wt` with the flag off falls back to the old To-drink path.
- **Sharing levels**: reactions and wants need club membership (the audience the card was sent to); non-members get 404. Retracted events: 409 `retracted`, scanner toast `club.rx.retracted`, nothing changes.
- **API**: `POST /api/clubs/events/<id>/react/` `{"i": 0-4}` returns `{on, emoji, counts, mine}`; `POST /api/clubs/events/<id>/want/`. Both 404 `not_enabled` with the flag off.
- **Ask**: `POST /api/clubs/ask/` kind `comments` adds `reactions` (per matching opened event: emoji counts with names, `wanted_by`) when the flag is on.
- Models: `ClubReaction`, `ClubWant` (migration club 0006). `ClubNotice` partial unique now includes `kind` (edited into unpushed 0004/0005).
- Tests: `wine-cellar/tests/test_club_reactions.py`, `scanner-service/tests/test_club_reactions.py`.
