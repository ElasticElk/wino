# Copy catalog (EN source + AF)

Planning only. Implementation lives in `scanner-service/integrations/copy.py`.

**Rules (open-q #20 decided):** slash commands and the `/` menu stay English. All dialogue / message copy is multilingual. English is the source of truth. Every new key in a phase PR must have an `af` value (CI fixture). Missing locale or key → English. Interpolated names go through `esc()` when `parse_mode=HTML`. The bot must be designed so a third locale is a catalog addition, not a handler rewrite.

Resolve locale: `UserSettings.language` if set → else Telegram `language_code` for unprovisioned chats → else `en`. Map `en* → en`, `af* → af`.

`{name}`, `{club}`, `{household}`, `{wine}`, `{vintage}`, `{link}`, `{date}`, `{n}`, `{inviter}`, `{policy}`, `{level}`, `{old}`, `{host}`, `{title}` are interpolations. Do not put real people’s names in fixtures; use Riley / Sam / Lee / Pat, Hillside, Thursday reds.

---

## Phase 0 — household invites

| key | EN | AF |
| --- | --- | --- |
| `invite.join.share` | Join {name} on Whiney.\n\nTap this, then Start:\n{link}\n\nForward this, or tap Share.\n\nWorks until {date}, one use. | Sluit by {name} aan op Whiney.\n\nTik hier, dan Start:\n{link}\n\nStuur aan, of tik Deel.\n\nWerk tot {date}, een keer. |
| `invite.share.btn` | Share invite | Deel uitnodiging |
| `invite.expiry.fallback` | One use, lasts 30 days. | Een keer, hou 30 dae. |
| `invite.mint.fail` | Couldn't make that invite. Try again in a minute. | Kon nie daai uitnodiging maak nie. Probeer oor 'n minuut. |
| `invite.mint.bad_hh` | Couldn't make that invite. | Kon nie daai uitnodiging maak nie. |
| `invite.fresh` | That invite has expired or was already used. Ask them for a fresh link. | Daai uitnodiging het verval of is al gebruik. Vra vir 'n nuwe skakel. |
| `invite.unreachable` | Couldn't reach the cellar just now — tap the link again in a minute. | Kon nie die kelder nou bereik nie — tik die skakel oor 'n minuut weer. |

Existing `JOIN_CARD`, `I1_COPY`, `I2_COPY`, `FRESH_LINK`, `UNREACHABLE`, cancel copy stay as today until they move into the catalog. Do not rewrite the whole invite module in phase 0; only TTL + new error keys.

---

## Phase 1 — To-drink lists

| key | EN | AF |
| --- | --- | --- |
| `list.btn.add` | To-drink list | To-drink-lys |
| `list.note.added` | On your To-drink list. | Op jou To-drink-lys. |
| `list.note.already` | Already on your To-drink list. | Reeds op jou To-drink-lys. |
| `list.note.in_cellar` | Already in the cellar — add another bottle from here, or open /fridge. | Reeds in die kelder — voeg nog 'n bottel hier by, of maak /fridge oop. |
| `list.need_name` | I need a name on the label first. | Ek het eers 'n naam op die etiket nodig. |
| `list.empty` | Nothing here yet.\n\nPhotograph a bottle you don't own — then tap To-drink list. | Niks hier nie.\n\nNeem 'n foto van 'n bottel wat jy nie besit nie — dan tik To-drink-lys. |
| `list.moved` | {name} is in the cellar. | {name} is in die kelder. |
| `list.removed` | Removed from To-drink. | Van To-drink afgehaal. |
| `list.off` | To-drink lists aren't on yet. | To-drink-lyste is nog nie aan nie. |
| `list.err` | Couldn't update that list. Try again. | Kon nie die lys bywerk nie. Probeer weer. |
| `list.title` | To drink | Om te drink |
| `list.btn.move` | Move to cellar | Skuif na kelder |
| `list.btn.remove` | Remove | Haal af |
| `list.card.gone` | That card's finished — send the photo again. | Daai kaart is klaar — stuur die foto weer. |

Existing label buttons (`Add to cellar`, `Drank this`, `Just looking`) stay English until the i18n PR. New button `list.btn.add` is catalogued from day one.

---

## Phase 2 — clubs

| key | EN | AF |
| --- | --- | --- |
| `club.off` | Wine clubs aren't on yet. | Wynklubs is nog nie aan nie. |
| `club.err` | Couldn't reach the club just now. | Kon nie die klub nou bereik nie. |
| `club.not_member` | You're not in that club. | Jy is nie in daai klub nie. |
| `club.forbidden` | Only the owner can change that. | Net die eienaar kan dit verander. |
| `club.name_taken` | You already have a club with that name. | Jy het al 'n klub met daai naam. |
| `club.fresh` | That invite has expired or was already used. Ask them for a fresh link. | Daai uitnodiging het verval of is al gebruik. Vra vir 'n nuwe skakel. |
| `club.unreachable` | Couldn't reach the cellar just now — tap the link again in a minute. | Kon nie die kelder nou bereik nie — tik die skakel oor 'n minuut weer. |
| `club.pitch` | Wine clubs are for drinking with people who have their own cellars.\n\nNot a shared fridge — you each keep your own. | Wynklubs is om te drink met mense wat hul eie kelders het.\n\nNie 'n gedeelde yskas nie — elkeen hou sy eie. |
| `club.found.btn` | Found a club | Begin 'n klub |
| `club.found.name` | What should we call it? | Wat noem ons dit? |
| `club.share.ask` | What does {name} share?\n\nEveryone who joins shares this with the club. | Wat deel {name}?\n\nAlmal wat aansluit deel dit met die klub. |
| `club.share.summary` | Type and count | Tipe en aantal |
| `club.share.full` | Full cellar | Vol kelder |
| `club.share.none` | Drink feed only | Net die drink-lys |
| `club.share.badges_on` | Badges: on | Kentekens: aan |
| `club.share.badges_off` | Badges: off | Kentekens: af |
| `club.create.btn` | Create club | Skep klub |
| `club.invite.body` | Join {name} on Whiney.\n\n{policy}\n\nTap this, then Start:\n{link}\n\nForward this, or tap Share.\n\nWorks until {date}, up to {n} uses. | Sluit by {name} aan op Whiney.\n\n{policy}\n\nTik hier, dan Start:\n{link}\n\nStuur aan, of tik Deel.\n\nWerk tot {date}, tot {n} keer. |
| `club.policy.summary` | Joining shares a type-and-count summary of your cellar and your badges with the other members. Drink-opens are posted automatically. Not a shared fridge. | Aansluit deel 'n tipe-en-aantal opsomming van jou kelder en jou kentekens met die ander lede. Drink-oops word outomaties geplaas. Nie 'n gedeelde yskas nie. |
| `club.policy.full` | Joining shares the names and vintages in your cellar (no prices or drawers) and your badges. Drink-opens are posted automatically. Not a shared fridge. | Aansluit deel die name en oesjare in jou kelder (geen pryse of laaie nie) en jou kentekens. Drink-oops word outomaties geplaas. Nie 'n gedeelde yskas nie. |
| `club.policy.none` | Joining posts drink-opens automatically. Your cellar stays private. | Aansluit plaas drink-oops outomaties. Jou kelder bly privaat. |
| `club.join.known` | {inviter} invited you to {club}.\n\nJoining shares {policy_of} {household}, including bottles anyone in that cellar added. Drink-opens are posted to the club. Not a shared fridge. | {inviter} het jou na {club} genooi.\n\nAansluit deel {policy_of} {household}, insluitend bottels wat enigiemand in daai kelder bygevoeg het. Drink-oops gaan na die klub. Nie 'n gedeelde yskas nie. |
| `club.join.btn` | Join {name} | Sluit by {name} aan |
| `club.join.not_now` | Not now | Nou nie |
| `club.join.pick_hh` | Which cellar should {club} see? | Watter kelder moet {club} sien? |
| `club.not_now.ok` | No problem. That link still works until {date}. | Geen probleem. Daai skakel werk tot {date}. |
| `club.feed.opened` | {name} opened a {vintage} {wine} | {name} het 'n {vintage} {wine} oopgemaak |
| `club.feed.put_back` | {name} put the {vintage} {wine} back. | {name} het die {vintage} {wine} teruggesit. |
| `club.feed.joined` | {name} joined | {name} het aangesluit |
| `club.muted` | Muted {name}. Opens still go to the club; you won't get pings. /club then Unmute. | {name} is stil. Oops gaan nog na die klub; jy kry nie pings nie. /club dan Unmute. |
| `club.unmuted` | Unmuted {name}. | {name} is nie meer stil nie. |
| `club.left` | You've left {name}. | Jy het {name} verlaat. |
| `club.handover.need` | Hand the club to someone else first. | Gee eers die klub aan iemand anders. |
| `club.handover.btn` | Hand over | Gee oor |
| `club.hh.notice` | Just so you know — this cellar is now shared inside a wine club.\n\nOnly people in the club can see it, and only what the club allows. Whiney never shares a cellar any other way. | Net gou — hierdie kelder word nou binne 'n wynklub gedeel.\n\nNet mense in die klub kan dit sien, en net wat die klub toelaat. Whiney deel nooit 'n kelder op 'n ander manier nie. |
| `club.widen.body` | {club} now shares the {level}.\n\nYou already share a {old}. Stay to share the new level, or leave. | {club} deel nou die {level}.\n\nJy deel al 'n {old}. Bly om die nuwe vlak te deel, of loop. |
| `club.widen.stay` | Stay | Bly |
| `club.widen.leave` | Leave | Loop |
| `club.switch.ask` | Use {household} for {club}? | Gebruik {household} vir {club}? |
| `club.switch.yes` | Yes | Ja |
| `club.switch.keep` | Keep {household} | Hou {household} |
| `club.home.invite` | Invite | Nooi |
| `club.home.members` | Members | Lede |
| `club.home.feed` | Feed | Lys |
| `club.home.mute` | Mute | Stil |
| `club.home.unmute` | Unmute | Praat |
| `club.home.settings` | Settings | Instellings |
| `club.peek.btn` | Show cellar | Wys kelder |
| `club.badges.btn` | Badges | Kentekens |
| `club.peek.summary_title` | {household} · summary | {household} · opsomming |
| `club.peek.more` | and {n} more | en nog {n} |
| `club.help` | Wine clubs | Wynklubs |
| `club.status.ready` | Clubs: ready | Klubs: gereed |
| `club.status.resting` | Clubs: off | Klubs: af |
| `club.end.btn` | End this club | Beëindig hierdie klub |
| `club.end.confirm` | End {name}?\n\nEvery member has to agree. If anyone says no, or doesn't reply within 48 hours, the club stays.\n\nThis can't be undone. | Beëindig {name}?\n\nElke lid moet saamstem. As iemand nee sê, of nie binne 48 uur antwoord nie, bly die klub.\n\nDit kan nie ontdoen word nie. |
| `club.end.ask` | Ask everyone | Vra almal |
| `club.end.cancel` | Cancel | Kanselleer |
| `club.end.ballot` | {name} wants to end {club}.\n\nIf everyone says yes, the club and its feed go away. If anyone says no, or doesn't reply in time, nothing changes. | {name} wil {club} beëindig.\n\nAs almal ja sê, verdwyn die klub en sy lys. As iemand nee sê, of nie betyds antwoord nie, verander niks. |
| `club.end.yes` | Yes, end it | Ja, beëindig dit |
| `club.end.no` | No, keep it | Nee, hou dit |
| `club.end.status` | Ending {club} — waiting on {n} votes.\nYes: {yes_n} · No: {no_n}\nWaiting: {waiting}\n\nCloses {date} if not everyone has voted. | Beëindig {club} — wag op {n} stemme.\nJa: {yes_n} · Nee: {no_n}\nWag: {waiting}\n\nSluit {date} as nie almal gestem het nie. |
| `club.end.rejected` | {name} voted to keep {club}. The club stays. | {name} het gestem om {club} te hou. Die klub bly. |
| `club.end.passed` | {club} has ended. | {club} is beëindig. |
| `club.end.expired` | Not everyone voted in time. {club} stays. | Nie almal het betyds gestem nie. {club} bly. |
| `club.end.cancelled` | Cancelled. {club} stays. | Gekanselleer. {club} bly. |
| `club.end.in_progress` | This club is deciding whether to close. Ask for a new link later. | Hierdie klub besluit of dit toemaak. Vra later vir 'n nuwe skakel. |
| `club.end.over` | That vote is over. | Daai stemming is verby. |

`club.hh.notice` must never interpolate a club name, member name, or household name. Tests lock that.

Policy-of fragments for `club.join.known`: `a type-and-count summary of` / `'n tipe-en-aantal opsomming van`; `the names and vintages in` / `die name en oesjare in`; `only the drink feed from` / `net die drink-lys van`.

---

## Phase 3 — comments, taste-offs, club lists

| key | EN | AF |
| --- | --- | --- |
| `club.comment.btn` | Say something | Sê iets |
| `club.comment.prompt` | Reply to this message with a short note. | Antwoord op hierdie boodskap met 'n kort nota. |
| `club.comment.saved` | Noted. | Genoteer. |
| `club.comment.too_long` | Keep it under 280 characters. | Hou dit onder 280 karakters. |
| `club.comment.again` | Tap Say something again. | Tik Sê iets weer. |
| `club.comment.more` | and {n} more | en nog {n} |
| `club.td.btn` | To-drink list | To-drink-lys |
| `club.td.added` | On your To-drink list. | Op jou To-drink-lys. |
| `club.list.title` | Club list | Klublys |
| `club.list.empty` | Nothing on the club list yet. | Niks op die klublys nie. |
| `club.to.title` | Name this tasting? | Wat noem ons die proe? |
| `club.to.score` | How was it? 1 (low) – 5 (high) | Hoe was dit? 1 (swak) – 5 (sterk) |
| `club.to.closed` | Tasting closed. | Proe is afgesluit. |
| `club.to.need_two` | Add at least two wines. | Voeg ten minste twee wyne by. |
| `club.to.open` | Open scoring | Begin telling |
| `club.to.close` | Close tasting | Sluit die proe |
| `club.to.running` | {host} is running a taste-off: {title}. Score 1–5. | {host} hou 'n proe: {title}. Tel 1–5. |
| `club.to.summary_only` | This club only shares a summary — type a name instead. | Hierdie klub deel net 'n opsomming — tik liewer 'n naam. |
| `club.to.already` | There's already a tasting open. | Daar is al 'n proe oop. |

---

## Phase 4 — global clubs

| key | EN | AF |
| --- | --- | --- |
| `club.discover.title` | Clubs for everyone | Klubs vir almal |
| `club.discover.btn` | Discover | Ontdek |
| `club.global.consent` | {name} is a Whiney club for everyone.\n\nJoining means drink-opens (if you drink a bottle, the club can see that you opened it) and a note from us now and then. Your cellar stays private. | {name} is 'n Whiney-klub vir almal.\n\nAansluit beteken drink-oops (as jy 'n bottel oopmaak, kan die klub dit sien) en nou en dan 'n nota van ons. Jou kelder bly privaat. |
| `club.global.private_cellar` | Your cellar stays private. | Jou kelder bly privaat. |
| `club.global.join` | Join | Sluit aan |
| `club.global.not_now` | Not now | Nou nie |
| `club.global.frozen` | This club is paused. | Hierdie klub is gepouseer. |
| `club.global.kicked` | You've been removed from {name}. | Jy is uit {name} verwyder. |

---

## Command menu (English — decided, open-q #20)

Slash commands and `set_bot_presence` descriptions stay English. Dialogue on messages uses this catalog.

| command | EN description |
| --- | --- |
| `/list` | Things to drink or buy |
| `/club` | Wine clubs |
| `/clubpost` (staff) | Post to a global club |
