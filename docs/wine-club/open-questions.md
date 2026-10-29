# Open questions

**Decided** items stay decided (see [README.md](README.md)). Do not reopen them in a phase PR.

Everything below is still open. **Recommended** is the build default. Flip it in review, then update this file in the same PR as the code.

| # | Question | Recommended | Phase |
| --- | --- | --- | --- |
| 1 | Household invite TTL | **30 days**, still one-use. Club invites **30 days**, `max_uses=20`. | 0 / 2 |
| 2 | Taste-off scale | Feed stays 👎/—/👍. Taste-off **1–5**. Do not use `Wine.rating` 0–10. | 3 |
| 3 | Catch-up Drunk | Emit feed events **on Apply**, one per bottle, not on staging taps. Moves never. | 2 |
| 4 | `/switch` vs peek household | **Ask** `Use {name} for {club}?` Do not silently follow `/switch`. | 2 |
| 5 | Owner widens sharing | DM + Stay/Leave. New level only after Stay (`accepted_sharing_version`). Unacked members stay at **previous** level. Narrowing applies immediately and auto-acks. | 2 |
| 6 | Shared household / veto | **Decided.** No veto. Nameless heads-up once. No club or member names. Sharing only inside clubs. Collapse peek per household. | 2 |
| 7 | Digest vs per-open | Per-open until a club has **15 members**, then offer a daily digest (separate flag later). Don’t build digest in 2e. | 2+ |
| 8 | Badge unlocks in the feed | **Off.** `/club` member badges only. | 2 |
| 9 | Global cellar peek | **Forbidden.** Constraint `kind=global` ⇒ `cellar_visibility=none`. | 4 |
| 10 | `/start` preview | **One** staff preview URL: household then club. Body includes policy for clubs. | 2 |
| 11 | Destroying a club | Last member leave **deletes** the club and events. Owner with others must hand over. | 2 |
| 12 | Invite code uniqueness | Unique across `HouseholdInvite` **and** `ClubInvite`. Same 8-char alphabet. | 0c / 2a |
| 13 | Lists vs Just looking | **Keep both.** Just looking = ephemeral. To-drink = remember. | 1 |
| 14 | To-drink if already in stock | **Allow.** Unique open `(list, wine_id)`. | 1 |
| 15 | Lists household- vs user-scoped | **Household-scoped**, `added_by` provenance. | 1 |
| 16 | Default club policy | Create flow **requires a pick**; UI default **summary + badges on**. | 2 |
| 17 | Who may host a taste-off | **Any member.** Owner (or host) may close. | 3 |
| 18 | Undo restore vs list | Phase 1: move-to-cellar is one-way on the list row. Do not auto-restore To-drink on stock undo. | 1 |
| 19 | Global opens in the feed | **Yes**, with join copy that says so. Mute available. Do not auto-post household drink-by into global. | 4 |
| 20 | Afrikaans in the `/` menu | Menu stays English until a dedicated i18n PR. **Body** copy uses the catalog (EN+AF) from the first new string. | all |
| 21 | `WineList.club_id` without FK in phase 1 | Integer column now; real FK in phase 3. Avoids importing `club` before it exists. | 1 / 3 |
| 22 | Comment edits to old DMs | Edit original delivery if ≤3 comments and `message_id` known; otherwise only `/club feed`. | 3 |

When a recommended answer is implemented, tick it in the phase PR description (`Implements open-questions #1, #12`) so review can see the defaults landing.
