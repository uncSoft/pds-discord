# 📚 Research Library for Discord

**Ask the post-drug syndromes research library from your own Discord server.** Every answer cites
the records it came from.

[**➕ Add to Discord**](https://discord.com/oauth2/authorize?client_id=1573604090225758208&scope=bot+applications.commands&permissions=19456)
· [The library](https://postdrugsyndrome.com/research)
· [The assistant](https://ask.postdrugsyndrome.com/)
· [Every command](https://pds-discord-site.onrender.com/commands.html)
· [Privacy](https://pds-discord-site.onrender.com/privacy.html)

The library is a curated reading list on post-finasteride syndrome, post-SSRI sexual dysfunction and
post-retinoid sexual dysfunction, with an assistant that answers from its reviewed records. This bot
brings that assistant into Discord, where most of the people reading it already talk to each other.

- **`/ask`** puts a question to the assistant and shows the answer with its sources, each one a link.
- **Follow-up questions** work for ten minutes in the same channel, so "and in women?" reads as a follow-up.
- **`/search`, `/record`, `/glossary`, `/news`** read the library directly, without asking a question.
- **Right-click any message → Apps → Ask the library** looks up a question someone already typed.

## What it is not

It carries no corpus and no model. Every question goes to the library's own assistant, which holds

- the guard that declines requests for dosing, diagnosis or what to take,
- the fixed care-first reply, with crisis lines, for someone who says they are at risk,
- Dr Powers' attribution on his material, and the retracted, unverifiable and tentative flags,
- the links to each record, which are generated and link-checked against the live site.

None of that is reimplemented here, and this bot never writes an answer of its own. One copy of the
rules, one place to fix them. A reply that is a decline, or the care-first reply, is passed through
word for word, and the care-first reply gets no "not medical advice" footer under it.

## How it's built

| | |
|---|---|
| Language | Python 3.12, [discord.py](https://github.com/Rapptz/discord.py) 2.x (slash commands, a message context menu, link buttons) |
| Talks to | `POST https://ask.postdrugsyndrome.com/api/chat`, answers streamed as Server-Sent Events. The contract is in the library's `bot/HANDOFF.md`. |
| Storage | None. No database, no disk. Questions are never written down; the few turns kept for follow-ups are in memory for ten minutes |
| Intents | None privileged. It cannot read what people type in channels: slash commands and the context menu arrive as interactions |
| Hosting | Render background worker, auto-deploying on push. The site in `site/` is a Render static site. `deploy/` has a systemd unit for a plain VPS |
| Tests | 80 pytest tests, including recorded answer streams and end-to-end handler tests with stand-in Discord objects |

```
pds_discord/
  api.py      question + history -> the assistant's streamed answer. No discord.py in it
  library.py  the library's public search index and news feed, cached for an hour
  render.py   an answer -> Discord embeds: text, sources with their flags, background, further reading
  limits.py   who may ask how often, and the short conversation each reader has going
  bot.py      the commands, and the reply that is edited as the answer streams
```

`api.py`, `library.py` and the text shaping in `render.py` have no Discord connection, so every
awkward part is tested on a recorded stream rather than against the live assistant. That is the
pattern [PandaBot](https://github.com/uncSoft/pandabot) uses for music-bot messages.

## Commands

| Command | What it shows |
|---|---|
| `/ask question [private]` | The assistant's answer, with its sources. `private: true` shows it only to you |
| `/search words` | Up to five records matching words, a record ID like `PFS-001`, or a catalog number like `214` |
| `/record id` | One record: what it is, who wrote it, where it appeared, and any warnings. Autocompletes |
| `/glossary term` | What a term means in the library. Autocompletes |
| `/news` | The most recent news items the curator has approved |
| `/support` | Crisis lines and the library's support page |
| `/forget` | Drops the conversation held in memory for your follow-up questions |
| `/about` | What the library is, what the bot will not do, and how to add it elsewhere |
| Right-click a message → Apps → **Ask the library** | Looks up the question in that message |

Answers are shortened to fit Discord's limits (6,000 characters for one message), and a long answer
links to the rest on the assistant's own page. Only the sources an answer actually cites are listed.

Server admins can limit the commands to particular channels in Server Settings, under Integrations.
There is nothing to configure in the bot, and so nothing for it to store.

## The server side

The assistant keeps a rate limit of 6 questions a minute and 40 an hour **per IP address**. Every
question from Discord arrives from this one process, so without a shared key the whole of Discord
looks to it like a single reader.

The bot is safe to run that way, and ships that way: the budgets in `pds_discord/limits.py` stay
inside one reader's allowance (2 a minute per reader, 4 per server, 5 for the bot as a whole) and
one question in flight leaves the assistant a slot for readers on the library's own site. Busy
servers wait rather than spending the allowance the site's readers need.

Set `PDS_CLIENT_KEY` to the same value as the assistant's `DISCORD_CLIENT_KEY` and the bot sends two
headers with each question:

| Header | What it is |
|---|---|
| `X-PDS-Client` | the shared key, compared in constant time |
| `X-PDS-Client-User` | SHA-256 of the key and the Discord user ID, so who is asking never reaches the assistant |

The assistant then holds its limit per reader rather than per address, and holds this client as a
whole to 20 questions a minute and 200 an hour, so a key that leaked cannot ask without limit. The
budgets here widen to match on their own (`Budgets.for_client`), and the bot takes two questions in
flight instead of one. There is nothing else to change on this side.

## Setup (about ten minutes)

1. **Make the application.** [Developer Portal](https://discord.com/developers/applications) → New
   Application. Under **Bot**, press Reset Token and copy it. Leave every privileged intent off:
   this bot needs none of them.
2. **Fill in `.env`.** `cp .env.example .env`, paste the token in. The library's addresses are
   already right; leave `PDS_CLIENT_KEY` empty until the change above is live.
3. **Install and run.**

   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
   .venv/bin/python -m pds_discord
   ```

   The commands register globally on startup, which takes Discord a minute to spread around.
4. **Invite it.** Developer Portal → OAuth2 → URL Generator → scopes `bot` and
   `applications.commands`, permissions **View Channels**, **Send Messages** and **Embed Links**.
   Or take the link at the top of this file, which carries the application ID already.

## Tests

```bash
.venv/bin/python -m pytest -q
```

`tests/data/*.sse` are recorded answer streams: a cited answer with a retracted source and a comment
of Dr Powers', a declined question, and the care-first reply. Capture a new one with
`curl -N -H 'Content-Type: application/json' -d '{"question":"..."}' https://ask.postdrugsyndrome.com/api/chat`
and drop it in beside them.

## Hosting

**Render**, the way PandaBot is hosted: Dashboard → New → Blueprint → pick this repo, and
`render.yaml` sets up the worker and the static site. It asks for `DISCORD_TOKEN` and
`PDS_CLIENT_KEY`. A worker is about $7 a month; there is no disk, because nothing is stored. Pushes
to `main` deploy themselves.

**A plain VPS**, if you would rather: `deploy/push.sh root@SERVER_IP --first` copies the bot up,
installs it under systemd and starts it. Afterwards, `deploy/push.sh root@SERVER_IP` for code
changes and `journalctl -u pds-discord -f` for the log.

Only one copy may run at a time, or every question gets answered twice.

## Privacy

No database, no disk, no question log. The operational log records which command was used and in
which server, never the text of a question or an answer. The assistant is told a scrambled version
of the asker's Discord ID so it can keep its limits fair, never the ID itself. The full statement is
`site/privacy.html`.

## Licence

**GNU AGPL-3.0.** You can run and modify this, but if you run a modified version for other people,
you must share your changes under the same licence. The library's own content is CC BY 4.0 and
citable by DOI; see [the library](https://postdrugsyndrome.com/research).
