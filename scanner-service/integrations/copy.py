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
