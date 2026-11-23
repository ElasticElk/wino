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
