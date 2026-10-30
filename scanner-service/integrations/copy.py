"""Bot dialogue catalog.

Slash commands and the Telegram / menu stay English. Message copy (cards,
buttons on messages, DMs, errors) is keyed here so a third locale is a
catalog addition, not a handler rewrite.

English is the source of truth. Missing locale or key falls back to English.
"""

from __future__ import annotations

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "invite.join.share": (
            "Join {name} on Whiney.\n\n"
            "Tap this, then Start:\n"
            "{link}\n\n"
            "Forward this, or tap Share.\n\n"
            "Works until {date}, one use."
        ),
        "invite.share.btn": "Share invite",
        "invite.expiry.fallback": "One use, lasts 30 days.",
        "invite.mint.fail": "Couldn't make that invite. Try again in a minute.",
        "invite.mint.bad_hh": "Couldn't make that invite.",
        "invite.fresh": (
            "That invite has expired or was already used. Ask them for a fresh link."
        ),
        "invite.unreachable": (
            "Couldn't reach the cellar just now — tap the link again in a minute."
        ),
        "list.btn.add": "To-drink list",
        "list.note.added": "On your To-drink list.",
        "list.note.already": "Already on your To-drink list.",
        "list.note.in_cellar": (
            "Already in the cellar — add another bottle from here, or open /fridge."
        ),
        "list.need_name": "I need a name on the label first.",
        "list.empty": (
            "Nothing here yet.\n\n"
            "Photograph a bottle you don't own — then tap To-drink list."
        ),
        "list.moved": "{name} is in the cellar.",
        "list.removed": "Removed from To-drink.",
        "list.off": "To-drink lists aren't on yet.",
        "list.err": "Couldn't update that list. Try again.",
        "list.title": "To drink",
        "list.btn.move": "Move to cellar",
        "list.btn.remove": "Remove",
        "list.card.gone": "That card's finished — send the photo again.",
        "list.status.ready": "Lists: ready",
        "list.status.resting": "Lists: off",
        "list.older": "Older 5",
        "list.newer": "Newer 5",
        "list.where": "Where are you storing this?",
        "list.drawer": "Which drawer?",
        "list.dup.body": (
            "Already added {name} a moment ago.\n\nAdd another bottle?"
        ),
        "list.dup.add_another": "Add another bottle?",
        "list.dup.thats_the_one": "That's the one",
        "club.off": "Wine clubs aren't on yet.",
        "club.err": "Couldn't reach the club just now.",
        "club.not_member": "You're not in that club.",
        "club.forbidden": "Only the owner can change that.",
        "club.name_taken": "You already have a club with that name.",
        "club.fresh": (
            "That invite has expired or was already used. Ask them for a fresh link."
        ),
        "club.unreachable": (
            "Couldn't reach the cellar just now — tap the link again in a minute."
        ),
        "club.pitch": (
            "Wine clubs are for drinking with people who have their own cellars.\n\n"
            "Not a shared fridge — you each keep your own."
        ),
        "club.found.btn": "Found a club",
        "club.found.name": "What should we call it?",
        "club.share.ask": (
            "What does {name} share?\n\nEveryone who joins shares this with the club."
        ),
        "club.share.summary": "Type and count",
        "club.share.full": "Full cellar",
        "club.share.none": "Drink feed only",
        "club.share.badges_on": "Badges: on",
        "club.share.badges_off": "Badges: off",
        "club.create.btn": "Create club",
        "club.invite.body": (
            "Join {name} on Whiney.\n\n{policy}\n\nTap this, then Start:\n{link}\n\n"
            "Forward this, or tap Share.\n\nWorks until {date}, up to {n} uses."
        ),
        "club.policy.summary": (
            "Joining shares a type-and-count summary of your cellar and your badges "
            "with the other members. Drink-opens are posted automatically. Not a shared fridge."
        ),
        "club.policy.full": (
            "Joining shares the names and vintages in your cellar (no prices or drawers) "
            "and your badges. Drink-opens are posted automatically. Not a shared fridge."
        ),
        "club.policy.none": (
            "Joining posts drink-opens automatically. Your cellar stays private."
        ),
        "club.policy_of.summary": "a type-and-count summary of",
        "club.policy_of.full": "the names and vintages in",
        "club.policy_of.none": "only the drink feed from",
        "club.join.known": (
            "{inviter} invited you to {club}.\n\n"
            "Joining shares {policy_of} {household}, including bottles anyone "
            "in that cellar added. Drink-opens are posted to the club. Not a shared fridge."
        ),
        "club.join.btn": "Join {name}",
        "club.join.not_now": "Not now",
        "club.join.pick_hh": "Which cellar should {club} see?",
        "club.not_now.ok": "No problem. That link still works until {date}.",
        "club.feed.opened": "{name} opened a {vintage} {wine}",
        "club.feed.put_back": "{name} put the {vintage} {wine} back.",
        "club.feed.joined": "{name} joined",
        "club.muted": (
            "Muted {name}. Opens still go to the club; you won't get pings. /club then Unmute."
        ),
        "club.unmuted": "Unmuted {name}.",
        "club.left": "You've left {name}.",
        "club.handover.need": "Hand the club to someone else first.",
        "club.handover.btn": "Hand over",
        "club.hh.notice": (
            "Just so you know — this cellar is now shared\n"
            "inside a wine club.\n\n"
            "Only people in the club can see it, and only\n"
            "what the club allows. Whiney never shares a\n"
            "cellar any other way."
        ),
        "club.widen.body": (
            "{club} now shares the {level}.\n\n"
            "You already share a {old}. Stay to share the new level, or leave."
        ),
        "club.widen.stay": "Stay",
        "club.widen.leave": "Leave",
        "club.switch.ask": "Use {household} for {club}?",
        "club.switch.yes": "Yes",
        "club.switch.keep": "Keep {household}",
        "club.home.invite": "Invite",
        "club.home.members": "Members",
        "club.home.feed": "Feed",
        "club.home.mute": "Mute",
        "club.home.unmute": "Unmute",
        "club.home.settings": "Settings",
        "club.home.leave": "Leave",
        "club.peek.btn": "Show cellar",
        "club.badges.btn": "Badges",
        "club.peek.summary_title": "{household} · summary",
        "club.peek.full_title": "{household} · full cellar",
        "club.peek.more": "and {n} more",
        "club.help": "Wine clubs",
        "club.status.ready": "Clubs: ready",
        "club.status.resting": "Clubs: off",
        "club.end.btn": "End this club",
        "club.end.confirm": (
            "End {name}?\n\nEvery member has to agree. If anyone says no, "
            "or doesn't reply within 48 hours, the club stays.\n\nThis can't be undone."
        ),
        "club.end.ask": "Ask everyone",
        "club.end.cancel": "Cancel",
        "club.end.ballot": (
            "{name} wants to end {club}.\n\n"
            "If everyone says yes, the club and its feed go away. "
            "If anyone says no, or doesn't reply in time, nothing changes."
        ),
        "club.end.yes": "Yes, end it",
        "club.end.no": "No, keep it",
        "club.end.status": (
            "Ending {club} — waiting on {n} votes.\n"
            "Yes: {yes_n} · No: {no_n}\n"
            "Waiting: {waiting}\n\n"
            "Closes {date} if not everyone has voted."
        ),
        "club.end.rejected": "{name} voted to keep {club}. The club stays.",
        "club.end.passed": "{club} has ended.",
        "club.end.expired": "Not everyone voted in time. {club} stays.",
        "club.end.cancelled": "Cancelled. {club} stays.",
        "club.end.in_progress": (
            "This club is deciding whether to close. Ask for a new link later."
        ),
        "club.end.over": "That vote is over.",
        "club.picker.title": "Your wine clubs",
        "club.leave.ask": "Leave {name}?",
        "club.leave.yes": "Leave",
        "club.leave.keep": "Stay",
        "club.feed.empty": "Nobody's opened a bottle yet. Opens show up here.",
        "club.members.empty": "Just you so far. Invite someone.",
        "club.peek.none": "This club only shares the drink feed.",
        "club.badges.off": "Badges aren't shared here.",
        "club.name.retry": "What should we call it? 2–40 characters.",
        "club.created": "{name} is live.",
        "club.back": "Back",
        "club.feed.newer": "Newer",
        "club.feed.older": "Older",
        "club.home.n_members": "{n} members",
        "club.invite.cancelled": "Cancelled invite {code}.",
        "club.invite.share": "Join {name} on Whiney.",
        "club.type.RE": "Red",
        "club.type.WH": "White",
        "club.type.RO": "Rosé",
        "club.type.SP": "Sparkling",
        "club.type.DE": "Dessert",
        "club.type.FO": "Fortified",
        "club.type.OR": "Orange",
        "club.named.ok": "OK — here's what joining shares.",
        "club.hh.taken": "That name's taken — pick another",
        "club.comment.btn": "Say something",
        "club.comment.prompt": "Reply to this message with a short note.",
        "club.comment.saved": "Noted.",
        "club.comment.too_long": "Keep it under 280 characters.",
        "club.comment.again": "Tap Say something again.",
        "club.comment.more": "and {n} more",
        "club.td.btn": "To-drink list",
        "club.td.added": "On your To-drink list.",
        "club.td.off": "To-drink lists aren't on yet.",
        "club.list.title": "Club list",
        "club.list.empty": "Nothing on the club list yet.",
        "club.list.add_btn": "Add",
        "club.list.add_prompt": "Name a wine to add.",
        "club.list.added": "Added.",
        "club.list.removed": "Removed.",
        "club.list.remove_btn": "Remove",
        "club.to.title": "Name this tasting?",
        "club.to.score": "How was it? 1 (low) – 5 (high)",
        "club.to.closed": "Tasting closed.",
        "club.to.need_two": "Add at least two wines.",
        "club.to.open": "Open scoring",
        "club.to.close": "Close tasting",
        "club.to.running": "{host} is running a taste-off: {title}. Score 1–5.",
        "club.to.summary_only": (
            "This club only shares a summary — type a name instead."
        ),
        "club.to.already": "There's already a tasting open.",
        "club.to.add": "Add a wine",
        "club.to.from_list": "From club list",
        "club.to.type_btn": "Type a name",
        "club.to.added_bottle": "Added {label}.",
        "club.to.scored": "You scored {label} {n}.",
        "club.to.host_open": "Scoring is open. Close when you're done.",
        "club.to.setup": "Bottles ({n}). Add at least two, then open scoring.",
        "club.to.cancel_setup": "Cancel tasting",
        "club.to.n_scores": "{n} scores",
        "club.home.tasteoff": "Taste-off",
        "club.to.from_cellar": "From a cellar",
        "club.to.off": "Taste-offs aren't on yet.",
        "club.comment.off": "Comments aren't on yet.",
        "club.discover.title": "Clubs for everyone",
        "club.discover.btn": "Discover",
        "club.discover.empty": "No clubs for everyone yet.",
        "club.global.consent": (
            "{name} is a Whiney club for everyone.\n\n"
            "Joining means drink-opens (if you drink a bottle, the club can see that you opened it) "
            "and a note from us now and then. Your cellar stays private."
        ),
        "club.global.private_cellar": "Your cellar stays private.",
        "club.global.join": "Join",
        "club.global.not_now": "Not now",
        "club.global.frozen": "This club is paused.",
        "club.global.kicked": "You've been removed from {name}.",
        "club.global.joined": "You're in {name}.",
        "club.global.post.prompt": "What should {name} hear this week?",
        "club.global.posted": "Posted to {name}.",
        "club.global.staff_only": "That's a staff tool.",
        "club.global.off": "Clubs for everyone aren't on yet.",
        "chat.club.no_clubs": "You're not in a wine club yet. Send /club to start one or join.",
        "chat.club.pick_club": "Which club?",
        "chat.club.pick_member": "Who in {club}?",
        "chat.club.this_club": "this club",
        "chat.club.not_found_club": "I don't see a club called {name}. Pick one of yours.",
        "chat.club.not_found_member": "I don't see anyone called {name} in {club}.",
        "chat.club.private_list": "{name}'s To-drink list is private.",
        "chat.club.global_cellar": "{club} doesn't share cellars — only drink-opens.",
        "chat.club.badges_hidden": "Badges aren't shared in {club}.",
        "chat.club.summary_only": "{club} shares counts, not bottle names.",
        "chat.club.private_cellar_member": "{name}'s cellar isn't shared with {club}.",
        "chat.club.private_cellar": "Cellars aren't shared in {club}.",
        "chat.club.nothing": "I couldn't look that up in the club just now.",
        "chat.club.down": "Couldn't reach the club just now. Try again in a minute.",
        "chat.club.member_owner": "{name} (owner)",
        "chat.club.members_empty": "Just you so far.",
        "chat.club.members": "In {club}:\n{list}",
        "chat.club.my_clubs": "Your clubs:\n{list}",
        "chat.club.cellar_summary_grape": "{name} has {n} {grape} in the cellar {club} can see.",
        "chat.club.cellar_summary": "{name} has {n} bottles in the cellar {club} can see.",
        "chat.club.cellar_none": "Nothing matching that in {name}'s cellar for {club}.",
        "chat.club.cellar_full": "{name} ({club}): {n}\n{list}",
        "chat.club.who_has_summary_note": "Some members only share counts, not names.",
        "chat.club.who_has_none": "Nobody in {club} has {query} that I can see.",
        "chat.club.who_has_summary_line": "• {name}: {n} (counts only)",
        "chat.club.who_has": "In {club}, {query}:\n{list}",
        "chat.club.oldest_none": "No vintages I can see in {club}.",
        "chat.club.oldest": "Oldest in {club} is {year}:\n{list}",
        "chat.club.feed_none_member": "{name} hasn't opened anything lately in {club}.",
        "chat.club.feed_none": "Nothing opened lately in {club}.",
        "chat.club.feed": "Opened in {club}:\n{list}",
        "chat.club.who_drinks_none": "No opens logged in {club} yet.",
        "chat.club.who_drinks": "Opens in {club}:\n{list}",
        "chat.club.avg_tasteoff": "{name}'s taste-off average is {n}/5.",
        "chat.club.avg_rating_none": "No club votes from {name} yet.",
        "chat.club.avg_rating": "{name}'s club votes: 👍{up} —{meh} 👎{down}.",
        "chat.club.top_rated_none": "No votes in {club} yet.",
        "chat.club.top_rated": "Top-voted in {club}:\n{list}",
        "chat.club.tasteoff_none": "No taste-off in {club} just now.",
        "chat.club.tasteoff_next": "Next in {club}: {title} ({status}).",
        "chat.club.tasteoff_won": "{title}: {names} ({n}).",
        "chat.club.tasteoff_scores": "{title}:\n{list}",
        "chat.club.badges_none": "{name} has no badges to show.",
        "chat.club.badges": "{name}'s badges:\n{list}",
        "chat.club.badge_progress_none": "Nobody in this club is sharing badge progress.",
        "chat.club.badge_progress": "{name} is closest: {title} {have}/{nxt}.",
        "chat.club.list_empty_personal": "Your To-drink list is empty.",
        "chat.club.list_empty": "Nothing on the {club} list yet.",
        "chat.club.list_personal": "On your To-drink list:\n{list}",
        "chat.club.list": "{club} list:\n{list}",
        "chat.club.comments_none": "Nobody's said anything about {query} in the club.",
        "chat.club.comments": "On {query}:\n{list}",
        "chat.offtopic": (
            "I'm only any good at wine, I'm afraid. Ask me what to open tonight, "
            "what goes with dinner, what to order when you're out, or what's in your fridge. "
            "Or ask how to add someone, start a cellar, or send /help."
        ),
        "chat.flag_off": "I can't help with that yet, but I can tell you about your own cellar.",
        "chat.clarify": "I didn't quite catch that. What would you like me to look up?",
        "chat.unknown": (
            "I'm not sure what you need. I can search your cellar, suggest a bottle, "
            "or talk you through how Whiney works."
        ),
        "chat.add_photo": "Send a photo of the label and I'll add it.",
        "chat.add_photo_named": "Send a photo of the {name} label and I'll add it.",
        "chat.empty.title": "Your cellar is empty",
        "chat.empty.body": (
            "📷 <b>Send me a photo of a bottle</b> and I'll add it.\n\n"
            "Point your camera at the front label — I'll read the producer, grape, "
            "region and vintage, write tasting notes, and look up ratings and price."
        ),
        "chat.empty.unconfigured": (
            "\n\n<i>{where} has no shelves yet. When you've got a fridge or "
            "rack, add it from the website and say how many drawers it has — "
            "or just send bottles and I'll keep them here until you do.</i>"
        ),
        "chat.empty.storage": "{where} is empty",
        "chat.empty.no_storage": "No storage locations yet",
        "chat.list.removed_named": "📝 Took {name} off your To-drink list.",
        "chat.list.not_on_list": (
            "{name} isn't on your To-drink list. Send /list to browse it, or name another bottle."
        ),
        "chat.list.which": (
            "A few bottles match that. Which one should come off the list?"
        ),
        "chat.fact.named_hit": "{query} in the cellar:",
        "chat.dont_know": "I don't know — I can only go on what's in your cellar.",
        "chat.list_add_need_wine": "Which bottle should go on the list? Send a label photo, or name it.",
        "chat.btn.stock": "What's in stock",
        "chat.btn.drink": "What should I drink",
        "chat.btn.help": "How Whiney works",
        "chat.resting": (
            "💤 My chat side is having a rest for a moment. Everything else still works: "
            "/stock, /fridge, or type a wine name to search. Try me again in a minute."
        ),
        "chat.rec_slow": "⏳ That took longer than it should. Want me to try again?",
        "chat.rec_quick": "🍷 My sommelier brain is offline, so here's a quick pick from your cellar:",
        "chat.cellar_down": (
            "🍷 I can't reach your cellar right now, so I can't check the shelves. "
            "Give it a minute and tap Try again."
        ),
        "chat.write_failed": "Couldn't save that just now, and nothing was changed. Tap Try again.",
        "chat.wine_gone": "That wine isn't in your cellar any more.",
        "chat.write_rejected": "That value didn't look right.",
        "chat.ask_lost": "I lost track of that suggestion. Just ask me again.",
        "chat.followup_footer": "(I can't remember follow-ups right now, so just ask again for another.)",
        "chat.finished": "That one's finished — just ask again.",
        "chat.place.ask_location": (
            "📍 Tap the button to share where you are and I'll find somewhere nearby "
            "with a good wine list for your taste. Or just type a suburb or town."
        ),
        "chat.place.down": "I couldn't search for places just now. Try again in a minute.",
        "chat.place.none": (
            "I couldn't find anywhere there I'd vouch for. Try a nearby suburb or town."
        ),
        "chat.place.searching": "🔎 Searching the web for places near you…",
        "chat.place.reading": "🍷 Reading wine lists…",
        "chat.place.reviews": "⭐ Checking reviews…",
        "chat.place.matching": "⚖️ Matching them to your taste…",
        "chat.place.narrowing": "🗺️ Narrowing it down to the best few…",
        "chat.place.still": "⏳ Still searching — good wine lists take a moment…",
        "chat.place.got_location": "📍 Got your location.",
        "chat.place.got_area": "📍 Got it — {area}.",
        "chat.place.thanks": "📍 Thanks — noted where you are.",
        "chat.place.header": "📍 <b>Good wine near you</b>",
        "chat.place.header_area": "📍 <b>Good wine in {area}</b>",
        "chat.place.footnote": "<i>Found with a live web search — worth checking they're open.</i>",
        "chat.help.invite": (
            "To add someone to this cellar, send /invite. I'll give you a link to share. "
            "They open it, pick a name, and they're in."
        ),
        "chat.help.new_cellar_staff": (
            "To start a new cellar, send /newcellar. I'll give you a link; they pick a name "
            "and get their own cellar. To add someone to <i>this</i> one instead, send /invite."
        ),
        "chat.help.new_cellar": (
            "A new cellar needs a staff invite. If /newcellar is in your / menu, send that. "
            "Otherwise ask whoever set Whiney up. To add someone to <i>this</i> cellar, send /invite."
        ),
        "chat.help.switch": (
            "Send /switch to pick another cellar. /household shows which one you're on and who is in it."
        ),
        "chat.help.photo": (
            "Just send a photo of the label, or of the barcode. No command needed. "
            "I'll read it and you can add it."
        ),
        "chat.help.take_out": (
            "Scan the bottle out, or ask me what to open, tap <i>That's the one</i>, then "
            "<i>Take it out now</i>. After a shuffle, /fridge then Catch up walks a drawer."
        ),
        "chat.help.commands": (
            "Ask me what to open, what's in a fridge, or send a label photo.\n"
            "/invite — add someone to this cellar\n"
            "/household — who's in it\n"
            "/switch — another cellar\n"
            "/help — the full list"
        ),
        "chat.looking": "🍷 Looking through your cellar…",
        "chat.progress.matching": "🔎 Checking what's drinking well right now…",
        "chat.progress.matching_phrase": "🔎 Matching \"{phrase}\" against your tasting notes…",
        "chat.progress.narrowing": "⚖️ Down to a few — picking the best one…",
        "chat.progress.slow": "⏳ Taking a little longer than usual — nearly there.",
        "chat.pick.none_notes": "<i>None of your notes mention {words} — this is the closest.</i>\n",
        "chat.pick.tie": "🍷 Two fit equally well:\n{first}\n{second}\n\nEither one — or shall I suggest something else?",
        "chat.pick.ask": "{evidence}{bottle}\n{reason}\n\nDoes that look right, or shall I suggest something else?",
        "chat.enjoy": "🍷 Enjoy the <b>{name}</b>{loc}.",
        "chat.no_problem": "No problem.",
        "chat.pick.exhausted": "That's everything that fits — ask me another way and I'll look again.",
        "chat.write.no_price": "I didn't catch the price.",
        "chat.write.no_change": "I didn't catch what to change.",
        "chat.write.which": "Which bottle?",
        "chat.write.which_mean": "Which bottle do you mean?",
        "chat.write.set_price": "💷 Set <b>{name}</b> to {shown}.{note}",
        "chat.write.currency_note": " (stored as {currency})",
        "chat.write.updated": "✏️ Updated <b>{name}</b>: {bits}.",
        "chat.write.updated_card": "✏️ Updated <b>{name}</b>: {bits}. Add will use this.",
        "chat.write.not_found": "I couldn't find '{name}' in your cellar.",
        "chat.checkout.fail": "Couldn't take that out just now, and nothing changed. Tap it again in a moment.",
        "chat.checkout.gone": "That bottle's already out of stock.",
        "chat.checkout.taken": "📤 <b>Taken out:</b> {name}",
        "chat.checkout.from": "📍 from {storage}",
        "chat.checkout.stock": "📊 In stock: {n}",
        "chat.link": (
            "🔗 I can't open links yet.\n\n"
            "If that's a wine list, take a <b>screenshot</b> or photo of it "
            "and send me that — I'll read it and tell you what to order."
        ),
        "chat.hint": (
            "💡 Type a wine name to search, send a <b>photo</b> of a bottle to add one, "
            "or try /help."
        ),
        "chat.err.generic": "Something went wrong just now. Try again in a minute.",
        "chat.err.search": "I couldn't search the cellar just now. Try again in a minute.",
        "chat.search.none": (
            "🔍 Nothing matching <b>{query}</b> in your cellar.\n\n"
            "📸 If you've just bought it, send me a photo of the label."
        ),
        "chat.search.header_one": "🔍 <b>1 match for \"{query}\"</b>\n",
        "chat.search.header_many": "🔍 <b>{n} matches for \"{query}\"</b>\n",
        "chat.search.more": "\n<i>... and {n} more — try a narrower search.</i>",
        "chat.search.none_left": "none left",
        "chat.drink.confirm": "Remove 1× <b>{name}</b>{extra} and mark it as drunk?",
        "chat.drink.stock_bit": " ({stock} in stock)",
        "chat.drink.which": "Which one did you drink?",
        "chat.drink.none": "Nothing matching <b>{query}</b>{where} in stock.",
        "chat.drink.in_storage": " in <b>{storage}</b>",
        "chat.drink.closest": "\n\nClosest in stock:",
        "chat.drink.browse_hint": (
            "\n\nBrowse the fridge, or send a label photo if it wasn't in the cellar."
        ),
        "chat.drink.which_fridge": "Which fridge did you take it from?",
        "chat.drink.waiting": "Nothing waiting to mark as drunk — send a photo or name the wine.",
        "chat.add.waiting": "Nothing waiting to add, send me a photo of the label.",
        "chat.list.added_named": "📝 {name} is on your To-drink list.",
        "chat.list.already_named": "📝 {name} was already on your To-drink list.",
        "chat.fact.which_storage": "Which one? {names}",
        "chat.fact.missing_storage": "I can't find a storage called '{name}'. You have: {names}.",
        "chat.fact.empty_drawer": "D{n} is empty.",
        "chat.fact.empty_drawer_in": "D{n} in the {storage} is empty.",
        "chat.fact.storage_empty": "{storage} is empty.",
        "chat.fact.country_none": "Nothing from {name} in the cellar.",
        "chat.fact.country_hit": "From {name}",
        "chat.fact.grape_none": "You don't have a {grape}.",
        "chat.fact.grape_hit": "{grape} in your cellar:",
        "chat.fact.grape_alias": "No bottle labelled {asked}, but these are the same grape ({shown}):",
        "chat.fact.blend": "(blend with {grape})",
        "chat.fact.none_matching": "Nothing matching {query} in the cellar.",
        "chat.quick.note": "The tasting note matches what you asked for.",
        "chat.quick.attribute": "It's marked with what you asked for.",
        "chat.quick.grape": "The grape matches what you asked for.",
        "chat.quick.solid": "A solid bottle from what's open in the cellar.",
        "chat.drank": "🍷 <b>Drank:</b> {name}\n📊 In stock: {stock}",
    },
    "af": {
        "invite.join.share": (
            "Sluit by {name} aan op Whiney.\n\n"
            "Tik hier, dan Start:\n"
            "{link}\n\n"
            "Stuur aan, of tik Deel.\n\n"
            "Werk tot {date}, een keer."
        ),
        "invite.share.btn": "Deel uitnodiging",
        "invite.expiry.fallback": "Een keer, hou 30 dae.",
        "invite.mint.fail": (
            "Kon nie daai uitnodiging maak nie. Probeer oor 'n minuut."
        ),
        "invite.mint.bad_hh": "Kon nie daai uitnodiging maak nie.",
        "invite.fresh": (
            "Daai uitnodiging het verval of is al gebruik. Vra vir 'n nuwe skakel."
        ),
        "invite.unreachable": (
            "Kon nie die kelder nou bereik nie — tik die skakel oor 'n minuut weer."
        ),
        "list.btn.add": "To-drink-lys",
        "list.note.added": "Op jou To-drink-lys.",
        "list.note.already": "Reeds op jou To-drink-lys.",
        "list.note.in_cellar": (
            "Reeds in die kelder — voeg nog 'n bottel hier by, of maak /fridge oop."
        ),
        "list.need_name": "Ek het eers 'n naam op die etiket nodig.",
        "list.empty": (
            "Niks hier nie.\n\n"
            "Neem 'n foto van 'n bottel wat jy nie besit nie — dan tik To-drink-lys."
        ),
        "list.moved": "{name} is in die kelder.",
        "list.removed": "Van To-drink afgehaal.",
        "list.off": "To-drink-lyste is nog nie aan nie.",
        "list.err": "Kon nie die lys bywerk nie. Probeer weer.",
        "list.title": "Om te drink",
        "list.btn.move": "Skuif na kelder",
        "list.btn.remove": "Haal af",
        "list.card.gone": "Daai kaart is klaar — stuur die foto weer.",
        "list.status.ready": "Lyste: gereed",
        "list.status.resting": "Lyste: af",
        "list.older": "Ouer 5",
        "list.newer": "Nuwer 5",
        "list.where": "Waar sit jy dit?",
        "list.drawer": "Watter laai?",
        "list.dup.body": (
            "Reeds {name} 'n oomblik gelede bygevoeg.\n\nNog 'n bottel byvoeg?"
        ),
        "list.dup.add_another": "Nog 'n bottel?",
        "list.dup.thats_the_one": "Dis die een",
        "club.off": "Wynklubs is nog nie aan nie.",
        "club.err": "Kon nie die klub nou bereik nie.",
        "club.not_member": "Jy is nie in daai klub nie.",
        "club.forbidden": "Net die eienaar kan dit verander.",
        "club.name_taken": "Jy het al 'n klub met daai naam.",
        "club.fresh": (
            "Daai uitnodiging het verval of is al gebruik. Vra vir 'n nuwe skakel."
        ),
        "club.unreachable": (
            "Kon nie die kelder nou bereik nie — tik die skakel oor 'n minuut weer."
        ),
        "club.pitch": (
            "Wynklubs is om te drink met mense wat hul eie kelders het.\n\n"
            "Nie 'n gedeelde yskas nie — elkeen hou sy eie."
        ),
        "club.found.btn": "Begin 'n klub",
        "club.found.name": "Wat noem ons dit?",
        "club.share.ask": (
            "Wat deel {name}?\n\nAlmal wat aansluit deel dit met die klub."
        ),
        "club.share.summary": "Tipe en aantal",
        "club.share.full": "Vol kelder",
        "club.share.none": "Net die drink-lys",
        "club.share.badges_on": "Kentekens: aan",
        "club.share.badges_off": "Kentekens: af",
        "club.create.btn": "Skep klub",
        "club.invite.body": (
            "Sluit by {name} aan op Whiney.\n\n{policy}\n\nTik hier, dan Start:\n{link}\n\n"
            "Stuur aan, of tik Deel.\n\nWerk tot {date}, tot {n} keer."
        ),
        "club.policy.summary": (
            "Aansluit deel 'n tipe-en-aantal opsomming van jou kelder en jou kentekens "
            "met die ander lede. Drink-oops word outomaties geplaas. Nie 'n gedeelde yskas nie."
        ),
        "club.policy.full": (
            "Aansluit deel die name en oesjare in jou kelder (geen pryse of laaie nie) "
            "en jou kentekens. Drink-oops word outomaties geplaas. Nie 'n gedeelde yskas nie."
        ),
        "club.policy.none": (
            "Aansluit plaas drink-oops outomaties. Jou kelder bly privaat."
        ),
        "club.policy_of.summary": "'n tipe-en-aantal opsomming van",
        "club.policy_of.full": "die name en oesjare in",
        "club.policy_of.none": "net die drink-lys van",
        "club.join.known": (
            "{inviter} het jou na {club} genooi.\n\n"
            "Aansluit deel {policy_of} {household}, insluitend bottels wat enigiemand "
            "in daai kelder bygevoeg het. Drink-oops gaan na die klub. Nie 'n gedeelde yskas nie."
        ),
        "club.join.btn": "Sluit by {name} aan",
        "club.join.not_now": "Nou nie",
        "club.join.pick_hh": "Watter kelder moet {club} sien?",
        "club.not_now.ok": "Geen probleem. Daai skakel werk tot {date}.",
        "club.feed.opened": "{name} het 'n {vintage} {wine} oopgemaak",
        "club.feed.put_back": "{name} het die {vintage} {wine} teruggesit.",
        "club.feed.joined": "{name} het aangesluit",
        "club.muted": (
            "{name} is stil. Oops gaan nog na die klub; jy kry nie pings nie. /club dan Unmute."
        ),
        "club.unmuted": "{name} is nie meer stil nie.",
        "club.left": "Jy het {name} verlaat.",
        "club.handover.need": "Gee eers die klub aan iemand anders.",
        "club.handover.btn": "Gee oor",
        "club.hh.notice": (
            "Net gou — hierdie kelder word nou binne 'n\n"
            "wynklub gedeel.\n\n"
            "Net mense in die klub kan dit sien, en net\n"
            "wat die klub toelaat. Whiney deel nooit 'n\n"
            "kelder op 'n ander manier nie."
        ),
        "club.widen.body": (
            "{club} deel nou die {level}.\n\n"
            "Jy deel al 'n {old}. Bly om die nuwe vlak te deel, of loop."
        ),
        "club.widen.stay": "Bly",
        "club.widen.leave": "Loop",
        "club.switch.ask": "Gebruik {household} vir {club}?",
        "club.switch.yes": "Ja",
        "club.switch.keep": "Hou {household}",
        "club.home.invite": "Nooi",
        "club.home.members": "Lede",
        "club.home.feed": "Aktiwiteit",
        "club.home.mute": "Stil",
        "club.home.unmute": "Praat",
        "club.home.settings": "Instellings",
        "club.home.leave": "Loop",
        "club.peek.btn": "Wys kelder",
        "club.badges.btn": "Kentekens",
        "club.peek.summary_title": "{household} · opsomming",
        "club.peek.full_title": "{household} · vol kelder",
        "club.peek.more": "en nog {n}",
        "club.help": "Wynklubs",
        "club.status.ready": "Klubs: gereed",
        "club.status.resting": "Klubs: af",
        "club.end.btn": "Beëindig hierdie klub",
        "club.end.confirm": (
            "Beëindig {name}?\n\nElke lid moet saamstem. As iemand nee sê, "
            "of nie binne 48 uur antwoord nie, bly die klub.\n\nDit kan nie ontdoen word nie."
        ),
        "club.end.ask": "Vra almal",
        "club.end.cancel": "Kanselleer",
        "club.end.ballot": (
            "{name} wil {club} beëindig.\n\n"
            "As almal ja sê, verdwyn die klub en sy aktiwiteit. "
            "As iemand nee sê, of nie betyds antwoord nie, verander niks."
        ),
        "club.end.yes": "Ja, beëindig dit",
        "club.end.no": "Nee, hou dit",
        "club.end.status": (
            "Beëindig {club} — wag op {n} stemme.\n"
            "Ja: {yes_n} · Nee: {no_n}\n"
            "Wag: {waiting}\n\n"
            "Sluit {date} as nie almal gestem het nie."
        ),
        "club.end.rejected": "{name} het gestem om {club} te hou. Die klub bly.",
        "club.end.passed": "{club} is beëindig.",
        "club.end.expired": "Nie almal het betyds gestem nie. {club} bly.",
        "club.end.cancelled": "Gekanselleer. {club} bly.",
        "club.end.in_progress": (
            "Hierdie klub besluit of dit toemaak. Vra later vir 'n nuwe skakel."
        ),
        "club.end.over": "Daai stemming is verby.",
        "club.picker.title": "Jou wynklubs",
        "club.leave.ask": "Verlaat {name}?",
        "club.leave.yes": "Loop",
        "club.leave.keep": "Bly",
        "club.feed.empty": "Niemand het nog 'n bottel oopgemaak nie. Oops verskyn hier.",
        "club.members.empty": "Net jy tot nou. Nooi iemand.",
        "club.peek.none": "Hierdie klub deel net die drink-lys.",
        "club.badges.off": "Kentekens word nie hier gedeel nie.",
        "club.name.retry": "Wat noem ons dit? 2–40 karakters.",
        "club.created": "{name} is live.",
        "club.back": "Terug",
        "club.feed.newer": "Nuwer",
        "club.feed.older": "Ouer",
        "club.home.n_members": "{n} lede",
        "club.invite.cancelled": "Uitnodiging {code} gekanselleer.",
        "club.invite.share": "Sluit by {name} aan op Whiney.",
        "club.type.RE": "Rooi",
        "club.type.WH": "Wit",
        "club.type.RO": "Rosé",
        "club.type.SP": "Vonkelwyn",
        "club.type.DE": "Nagereg",
        "club.type.FO": "Versterk",
        "club.type.OR": "Oranje",
        "club.named.ok": "Reg — hier's wat aansluit deel.",
        "club.hh.taken": "Daai naam is al geneem — kies 'n ander",
        "club.comment.btn": "Sê iets",
        "club.comment.prompt": "Antwoord op hierdie boodskap met 'n kort nota.",
        "club.comment.saved": "Genoteer.",
        "club.comment.too_long": "Hou dit onder 280 karakters.",
        "club.comment.again": "Tik Sê iets weer.",
        "club.comment.more": "en nog {n}",
        "club.td.btn": "To-drink-lys",
        "club.td.added": "Op jou To-drink-lys.",
        "club.td.off": "To-drink-lyste is nog nie aan nie.",
        "club.list.title": "Klublys",
        "club.list.empty": "Niks op die klublys nie.",
        "club.list.add_btn": "Voeg by",
        "club.list.add_prompt": "Noem 'n wyn om by te voeg.",
        "club.list.added": "Bygevoeg.",
        "club.list.removed": "Afgehaal.",
        "club.list.remove_btn": "Haal af",
        "club.to.title": "Wat noem ons die proe?",
        "club.to.score": "Hoe was dit? 1 (swak) – 5 (sterk)",
        "club.to.closed": "Proe is afgesluit.",
        "club.to.need_two": "Voeg ten minste twee wyne by.",
        "club.to.open": "Begin telling",
        "club.to.close": "Sluit die proe",
        "club.to.running": "{host} hou 'n proe: {title}. Tel 1–5.",
        "club.to.summary_only": (
            "Hierdie klub deel net 'n opsomming — tik liewer 'n naam."
        ),
        "club.to.already": "Daar is al 'n proe oop.",
        "club.to.add": "Voeg 'n wyn by",
        "club.to.from_list": "Van die klublys",
        "club.to.type_btn": "Tik 'n naam",
        "club.to.added_bottle": "{label} bygevoeg.",
        "club.to.scored": "Jy het {label} {n} gegee.",
        "club.to.host_open": "Telling is oop. Sluit as julle klaar is.",
        "club.to.setup": "Bottels ({n}). Voeg ten minste twee by, dan begin telling.",
        "club.to.cancel_setup": "Kanselleer die proe",
        "club.to.n_scores": "{n} tellings",
        "club.home.tasteoff": "Proe",
        "club.to.from_cellar": "Uit 'n kelder",
        "club.to.off": "Proeë is nog nie aan nie.",
        "club.comment.off": "Opmerkings is nog nie aan nie.",
        "club.discover.title": "Klubs vir almal",
        "club.discover.btn": "Ontdek",
        "club.discover.empty": "Nog geen klubs vir almal nie.",
        "club.global.consent": (
            "{name} is 'n Whiney-klub vir almal.\n\n"
            "Aansluit beteken drink-oops (as jy 'n bottel oopmaak, kan die klub dit sien) "
            "en nou en dan 'n nota van ons. Jou kelder bly privaat."
        ),
        "club.global.private_cellar": "Jou kelder bly privaat.",
        "club.global.join": "Sluit aan",
        "club.global.not_now": "Nou nie",
        "club.global.frozen": "Hierdie klub is gepouseer.",
        "club.global.kicked": "Jy is uit {name} verwyder.",
        "club.global.joined": "Jy is in {name}.",
        "club.global.post.prompt": "Wat moet {name} dié week hoor?",
        "club.global.posted": "Op {name} geplaas.",
        "club.global.staff_only": "Dit is 'n personeel-hulpmiddel.",
        "club.global.off": "Klubs vir almal is nog nie aan nie.",
        "chat.club.no_clubs": "Jy is nog nie in 'n wynklub nie. Stuur /club om een te begin of aan te sluit.",
        "chat.club.pick_club": "Watter klub?",
        "chat.club.pick_member": "Wie in {club}?",
        "chat.club.this_club": "hierdie klub",
        "chat.club.not_found_club": "Ek sien nie 'n klub genaamd {name} nie. Kies een van joune.",
        "chat.club.not_found_member": "Ek sien niemand genaamd {name} in {club} nie.",
        "chat.club.private_list": "{name} se To-drink-lys is privaat.",
        "chat.club.global_cellar": "{club} deel nie kelders nie — net wat oopgemaak word.",
        "chat.club.badges_hidden": "Kentekens word nie in {club} gedeel nie.",
        "chat.club.summary_only": "{club} deel tellings, nie bottelname nie.",
        "chat.club.private_cellar_member": "{name} se kelder word nie met {club} gedeel nie.",
        "chat.club.private_cellar": "Kelders word nie in {club} gedeel nie.",
        "chat.club.nothing": "Ek kon dit nie nou in die klub opsoek nie.",
        "chat.club.down": "Kon nie die klub nou bereik nie. Probeer oor 'n minuut weer.",
        "chat.club.member_owner": "{name} (eienaar)",
        "chat.club.members_empty": "Net jy tot dusver.",
        "chat.club.members": "In {club}:\n{list}",
        "chat.club.my_clubs": "Jou klubs:\n{list}",
        "chat.club.cellar_summary_grape": "{name} het {n} {grape} in die kelder wat {club} kan sien.",
        "chat.club.cellar_summary": "{name} het {n} bottels in die kelder wat {club} kan sien.",
        "chat.club.cellar_none": "Niks wat daarby pas in {name} se kelder vir {club} nie.",
        "chat.club.cellar_full": "{name} ({club}): {n}\n{list}",
        "chat.club.who_has_summary_note": "Party lede deel net tellings, nie name nie.",
        "chat.club.who_has_none": "Niemand in {club} het {query} wat ek kan sien nie.",
        "chat.club.who_has_summary_line": "• {name}: {n} (net tellings)",
        "chat.club.who_has": "In {club}, {query}:\n{list}",
        "chat.club.oldest_none": "Geen oesjare wat ek in {club} kan sien nie.",
        "chat.club.oldest": "Oudste in {club} is {year}:\n{list}",
        "chat.club.feed_none_member": "{name} het nog niks onlangs in {club} oopgemaak nie.",
        "chat.club.feed_none": "Niks onlangs in {club} oopgemaak nie.",
        "chat.club.feed": "Oopgemaak in {club}:\n{list}",
        "chat.club.who_drinks_none": "Nog geen oops in {club} aangeteken nie.",
        "chat.club.who_drinks": "Oops in {club}:\n{list}",
        "chat.club.avg_tasteoff": "{name} se proe-gemiddeld is {n}/5.",
        "chat.club.avg_rating_none": "Nog geen klubstemme van {name} nie.",
        "chat.club.avg_rating": "{name} se klubstemme: 👍{up} —{meh} 👎{down}.",
        "chat.club.top_rated_none": "Nog geen stemme in {club} nie.",
        "chat.club.top_rated": "Top-gestem in {club}:\n{list}",
        "chat.club.tasteoff_none": "Geen proe in {club} tans nie.",
        "chat.club.tasteoff_next": "Volgende in {club}: {title} ({status}).",
        "chat.club.tasteoff_won": "{title}: {names} ({n}).",
        "chat.club.tasteoff_scores": "{title}:\n{list}",
        "chat.club.badges_none": "{name} het geen kentekens om te wys nie.",
        "chat.club.badges": "{name} se kentekens:\n{list}",
        "chat.club.badge_progress_none": "Niemand in hierdie klub deel kenteken-vordering nie.",
        "chat.club.badge_progress": "{name} is die naaste: {title} {have}/{nxt}.",
        "chat.club.list_empty_personal": "Jou To-drink-lys is leeg.",
        "chat.club.list_empty": "Niks op die {club}-lys nog nie.",
        "chat.club.list_personal": "Op jou To-drink-lys:\n{list}",
        "chat.club.list": "{club}-lys:\n{list}",
        "chat.club.comments_none": "Niemand het iets oor {query} in die klub gesê nie.",
        "chat.club.comments": "Oor {query}:\n{list}",
        "chat.offtopic": (
            "Ek is jammer, ek is net goed met wyn. Vra my wat om vanaand oop te maak, "
            "wat by aandete pas, wat om te bestel as jy uit is, of wat in jou yskas is. "
            "Of vra hoe om iemand by te voeg, 'n kelder te begin, of stuur /help."
        ),
        "chat.flag_off": "Ek kan nog nie daarmee help nie, maar ek kan jou van jou eie kelder vertel.",
        "chat.clarify": "Ek is nie heeltemal seker nie. Wat moet ek opsoek?",
        "chat.unknown": (
            "Ek is nie seker wat jy nodig het nie. Ek kan jou kelder deursoek, "
            "'n bottel voorstel, of verduidelik hoe Whiney werk."
        ),
        "chat.add_photo": "Stuur 'n foto van die etiket en ek sal dit byvoeg.",
        "chat.add_photo_named": "Stuur 'n foto van die {name}-etiket en ek sal dit byvoeg.",
        "chat.empty.title": "Jou kelder is leeg",
        "chat.empty.body": (
            "📷 <b>Stuur my 'n foto van 'n bottel</b> en ek voeg dit by.\n\n"
            "Mik jou kamera na die voorkant-etiket — ek lees die produsent, druiwe, "
            "streek en oesjaar, skryf proe-notas, en soek graderings en prys op."
        ),
        "chat.empty.unconfigured": (
            "\n\n<i>{where} het nog geen rakke nie. As jy 'n yskas of rak het, "
            "voeg dit by op die webwerf en sê hoeveel laaie — "
            "of stuur net bottels en ek hou hulle hier tot jy dit doen.</i>"
        ),
        "chat.empty.storage": "{where} is leeg",
        "chat.empty.no_storage": "Nog geen stoorplekke nie",
        "chat.list.removed_named": "📝 Het {name} van jou To-drink-lys afgehaal.",
        "chat.list.not_on_list": (
            "{name} is nie op jou To-drink-lys nie. Stuur /list om dit te sien, of noem 'n ander bottel."
        ),
        "chat.list.which": (
            "A paar bottels pas. Watter een moet van die lys af?"
        ),
        "chat.fact.named_hit": "{query} in die kelder:",
        "chat.dont_know": "Ek weet nie — ek kan net gaan op wat in jou kelder is.",
        "chat.list_add_need_wine": "Watter bottel moet op die lys? Stuur 'n etiketfoto, of noem dit.",
        "chat.btn.stock": "What's in stock",
        "chat.btn.drink": "What should I drink",
        "chat.btn.help": "How Whiney works",
        "chat.resting": (
            "💤 My kantlyn rus 'n oomblik. Alles anders werk nog: "
            "/stock, /fridge, of tik 'n wynnaam om te soek. Probeer my oor 'n minuut weer."
        ),
        "chat.rec_slow": "⏳ Dit het langer gevat as wat dit moet. Moet ek weer probeer?",
        "chat.rec_quick": "🍷 My sommelier-brein is af, so hier's 'n vinnige keuse uit jou kelder:",
        "chat.cellar_down": (
            "🍷 Ek kan jou kelder nou nie bereik nie, so ek kan nie die rakke nagaan nie. "
            "Wag 'n minuut en tik Try again."
        ),
        "chat.write_failed": "Kon dit nie nou stoor nie, en niks is verander nie. Tik Try again.",
        "chat.wine_gone": "Daai wyn is nie meer in jou kelder nie.",
        "chat.write_rejected": "Daai waarde het nie reg gelyk nie.",
        "chat.ask_lost": "Ek het daai voorstel verloor. Vra my net weer.",
        "chat.followup_footer": "(Ek onthou nie opvolge nou nie, so vra net weer vir 'n ander een.)",
        "chat.finished": "Daai een is klaar — vra net weer.",
        "chat.place.ask_location": (
            "📍 Tik die knoppie om te deel waar jy is, dan soek ek iewers naby "
            "met 'n goeie wynlys vir jou smaak. Of tik net 'n voorstad of dorp."
        ),
        "chat.place.down": "Kon nie nou plekke soek nie. Probeer oor 'n minuut weer.",
        "chat.place.none": (
            "Ek kon nêrens daar vind wat ek sou aanbeveel nie. Probeer 'n nabygeleë voorstad of dorp."
        ),
        "chat.place.searching": "🔎 Soek op die web vir plekke naby jou…",
        "chat.place.reading": "🍷 Lees wynlyste…",
        "chat.place.reviews": "⭐ Kyk na resensies…",
        "chat.place.matching": "⚖️ Pas hulle by jou smaak…",
        "chat.place.narrowing": "🗺️ Smal dit af tot die beste paar…",
        "chat.place.still": "⏳ Soek nog — goeie wynlyste vat 'n oomblik…",
        "chat.place.got_location": "📍 Het jou ligging.",
        "chat.place.got_area": "📍 Het dit — {area}.",
        "chat.place.thanks": "📍 Dankie — ek het waar jy is.",
        "chat.place.header": "📍 <b>Goeie wyn naby jou</b>",
        "chat.place.header_area": "📍 <b>Goeie wyn in {area}</b>",
        "chat.place.footnote": "<i>Gevind met 'n lewendige websoektog — kyk of hulle oop is.</i>",
        "chat.help.invite": (
            "Om iemand by hierdie kelder te voeg, stuur /invite. Ek gee jou 'n skakel om te deel. "
            "Hulle maak dit oop, kies 'n naam, en hulle is in."
        ),
        "chat.help.new_cellar_staff": (
            "Om 'n nuwe kelder te begin, stuur /newcellar. Ek gee jou 'n skakel; hulle kies 'n naam "
            "en kry hul eie kelder. Om iemand by <i>hierdie</i> een te voeg, stuur /invite."
        ),
        "chat.help.new_cellar": (
            "'n Nuwe kelder het 'n personeel-uitnodiging nodig. As /newcellar in jou / kieslys is, stuur dit. "
            "Anders vra wie Whiney opgestel het. Om iemand by <i>hierdie</i> kelder te voeg, stuur /invite."
        ),
        "chat.help.switch": (
            "Stuur /switch om 'n ander kelder te kies. /household wys watter een jy op is en wie daarin is."
        ),
        "chat.help.photo": (
            "Stuur net 'n foto van die etiket, of van die strepieskode. Geen opdrag nodig nie. "
            "Ek lees dit en jy kan dit byvoeg."
        ),
        "chat.help.take_out": (
            "Skandeer die bottel uit, of vra my wat om oop te maak, tik <i>That's the one</i>, dan "
            "<i>Take it out now</i>. Ná 'n skuif, /fridge dan Catch up stap 'n laai."
        ),
        "chat.help.commands": (
            "Vra my wat om oop te maak, wat in 'n yskas is, of stuur 'n etiketfoto.\n"
            "/invite — voeg iemand by hierdie kelder\n"
            "/household — wie is daarin\n"
            "/switch — 'n ander kelder\n"
            "/help — die volle lys"
        ),
        "chat.looking": "🍷 Ek kyk deur jou kelder…",
        "chat.progress.matching": "🔎 Kyk wat nou lekker drink…",
        "chat.progress.matching_phrase": "🔎 Pas \"{phrase}\" by jou proe-notas…",
        "chat.progress.narrowing": "⚖️ Af tot 'n paar — kies die beste een…",
        "chat.progress.slow": "⏳ Vat 'n bietjie langer as gewoonlik — amper daar.",
        "chat.pick.none_notes": "<i>Geen van jou notas noem {words} nie — dit is die naaste.</i>\n",
        "chat.pick.tie": "🍷 Twee pas ewe goed:\n{first}\n{second}\n\nEnigeen — of moet ek iets anders voorstel?",
        "chat.pick.ask": "{evidence}{bottle}\n{reason}\n\nLyk dit reg, of moet ek iets anders voorstel?",
        "chat.enjoy": "🍷 Geniet die <b>{name}</b>{loc}.",
        "chat.no_problem": "Geen probleem.",
        "chat.pick.exhausted": "Dis alles wat pas — vra my op 'n ander manier en ek kyk weer.",
        "chat.write.no_price": "Ek het nie die prys gehoor nie.",
        "chat.write.no_change": "Ek het nie gehoor wat om te verander nie.",
        "chat.write.which": "Watter bottel?",
        "chat.write.which_mean": "Watter bottel bedoel jy?",
        "chat.write.set_price": "💷 Het <b>{name}</b> op {shown} gesit.{note}",
        "chat.write.currency_note": " (gestoor as {currency})",
        "chat.write.updated": "✏️ Het <b>{name}</b> bygewerk: {bits}.",
        "chat.write.updated_card": "✏️ Het <b>{name}</b> bygewerk: {bits}. Add sal dit gebruik.",
        "chat.write.not_found": "Ek kon '{name}' nie in jou kelder kry nie.",
        "chat.checkout.fail": "Kon dit nie nou uithaal nie, en niks het verander nie. Tik dit oor 'n oomblik weer.",
        "chat.checkout.gone": "Daai bottel is al uit voorraad.",
        "chat.checkout.taken": "📤 <b>Uitgehaal:</b> {name}",
        "chat.checkout.from": "📍 uit {storage}",
        "chat.checkout.stock": "📊 In voorraad: {n}",
        "chat.link": (
            "🔗 Ek kan nog nie skakels oopmaak nie.\n\n"
            "As dit 'n wynlys is, neem 'n <b>skermkiekie</b> of foto daarvan "
            "en stuur dit — ek lees dit en sê wat om te bestel."
        ),
        "chat.hint": (
            "💡 Tik 'n wynnaam om te soek, stuur 'n <b>foto</b> van 'n bottel om een by te voeg, "
            "of probeer /help."
        ),
        "chat.err.generic": "Iets het nou skeefgeloop. Probeer oor 'n minuut weer.",
        "chat.err.search": "Ek kon die kelder nou nie deursoek nie. Probeer oor 'n minuut weer.",
        "chat.search.none": (
            "🔍 Niks wat by <b>{query}</b> in jou kelder pas nie.\n\n"
            "📸 As jy dit net gekoop het, stuur my 'n foto van die etiket."
        ),
        "chat.search.header_one": "🔍 <b>1 treffer vir \"{query}\"</b>\n",
        "chat.search.header_many": "🔍 <b>{n} treffers vir \"{query}\"</b>\n",
        "chat.search.more": "\n<i>... en nog {n} — probeer 'n nouer soektog.</i>",
        "chat.search.none_left": "geen oor",
        "chat.drink.confirm": "Haal 1× <b>{name}</b>{extra} uit en merk as gedrink?",
        "chat.drink.stock_bit": " ({stock} in voorraad)",
        "chat.drink.which": "Watter een het jy gedrink?",
        "chat.drink.none": "Niks wat by <b>{query}</b>{where} in voorraad pas nie.",
        "chat.drink.in_storage": " in <b>{storage}</b>",
        "chat.drink.closest": "\n\nNaaste in voorraad:",
        "chat.drink.browse_hint": (
            "\n\nBlaai die yskas, of stuur 'n etiketfoto as dit nie in die kelder was nie."
        ),
        "chat.drink.which_fridge": "Uit watter yskas het jy dit gehaal?",
        "chat.drink.waiting": "Niks wag om as gedrink te merk nie — stuur 'n foto of noem die wyn.",
        "chat.add.waiting": "Niks wag om by te voeg nie, stuur my 'n foto van die etiket.",
        "chat.list.added_named": "📝 {name} is op jou To-drink-lys.",
        "chat.list.already_named": "📝 {name} was al op jou To-drink-lys.",
        "chat.fact.which_storage": "Watter een? {names}",
        "chat.fact.missing_storage": "Ek kry nie 'n stoorplek genaamd '{name}' nie. Jy het: {names}.",
        "chat.fact.empty_drawer": "D{n} is leeg.",
        "chat.fact.empty_drawer_in": "D{n} in die {storage} is leeg.",
        "chat.fact.storage_empty": "{storage} is leeg.",
        "chat.fact.country_none": "Niks van {name} in die kelder nie.",
        "chat.fact.country_hit": "Van {name}",
        "chat.fact.grape_none": "Jy het nie 'n {grape} nie.",
        "chat.fact.grape_hit": "{grape} in jou kelder:",
        "chat.fact.grape_alias": "Geen bottel gemerk {asked} nie, maar dit is dieselfde druiwe ({shown}):",
        "chat.fact.blend": "(mengsel met {grape})",
        "chat.fact.none_matching": "Niks wat by {query} in die kelder pas nie.",
        "chat.quick.note": "Die proe-nota pas by wat jy gevra het.",
        "chat.quick.attribute": "Dit is gemerk met wat jy gevra het.",
        "chat.quick.grape": "Die druiwe pas by wat jy gevra het.",
        "chat.quick.solid": "'n Soliede bottel uit wat oop is in die kelder.",
        "chat.drank": "🍷 <b>Gedrink:</b> {name}\n📊 In voorraad: {stock}",
    },
}


def normalize_locale(value: str | None) -> str:
    """Map UserSettings.language / Telegram language_code onto a catalog locale.

    en* → en, af* → af. Anything else (de-DE, fr-FR, unset) → en until that
    locale is added to MESSAGES.
    """
    if not isinstance(value, str):
        return "en"
    raw = value.strip().lower().replace("_", "-")
    if raw.startswith("af"):
        return "af"
    if raw.startswith("en"):
        return "en"
    return "en"


def locale_from_telegram(language_code) -> str:
    return normalize_locale(language_code if isinstance(language_code, str) else None)


_AF_MARKS = (
    " ek ", " het ", " wat ", " watter ", " hoe ", " iets ", " vanaand",
    " yskas", " kelder", " gedrink", " wyn ", " myne ", " nie ",
    " asb ", " asseblief", " waar ", " wie ", " hoeveel", " enige ",
)


def sniff_locale(text: str, fallback: str = "en") -> str:
    """Upgrade to Afrikaans when the typed line clearly is. Never override AF down to EN."""
    fallback = normalize_locale(fallback)
    if fallback == "af":
        return "af"
    if not isinstance(text, str):
        return fallback
    padded = f" {text.casefold()} "
    hits = sum(1 for mark in _AF_MARKS if mark in padded)
    if hits >= 2:
        return "af"
    return fallback


def t(locale: str, key: str, **kwargs) -> str:
    loc = normalize_locale(locale)
    catalog = MESSAGES.get(loc) or MESSAGES["en"]
    template = catalog.get(key) or MESSAGES["en"].get(key) or key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


def catalog_keys(locale: str = "en") -> set[str]:
    return set(MESSAGES.get(normalize_locale(locale), {}))


def missing_af_keys() -> set[str]:
    return catalog_keys("en") - catalog_keys("af")
