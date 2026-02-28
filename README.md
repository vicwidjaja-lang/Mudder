# Mudder — MUD Client

A Python MUD client for **dsl-mud.org port 4000** (and any Diku/Circle/ROM MUD).

## Features

| Feature | Details |
|---------|---------|
| **Aliases** | Expand short commands to full ones (`kk` → `kill`) |
| **Triggers** | Regex patterns on game output that auto-send commands |
| **Auto-Skills** | Priority queue of skills fired automatically in combat |
| **Rate Limiting** | Min 0.5 s between commands — prevents IP bans |
| **ANSI Colours** | Game colour passes through to your terminal |
| **Input History** | Arrow-key history via `prompt_toolkit` |

## Quick Start

```bash
pip install prompt_toolkit        # optional but recommended
python main.py                    # connects to dsl-mud.org:4000
```

## Solo SharePlay GUI (non-terminal)

Use the bridge plus a local GUI where both "You" and "Agent" can send
commands into one shared session.

```bash
# terminal/tab 1
.venv/bin/python bridge.py --log INFO

# terminal/tab 2
./shareplay-solo
```

## In-game Client Commands

All client commands start with `#`:

```
#help                          show all commands
#aliases                       list aliases
#alias kk kill                 add alias  kk -> kill
#alias kk                      remove alias kk
#triggers                      list triggers
#trigger myname pattern cmd    add trigger
#trigger myname on|off         enable/disable trigger
#trigdel myname                delete trigger
#skills                        show skill list + auto-skill status
#skill bash on                 enable bash auto-use
#autoskill on                  start auto-skill loop
#state                         show parsed HP/mana/combat state
#save                          save all configs to configs/
#quit                          disconnect
```

## Rate Limiting / IP Safety

- Default: **0.5 s minimum** between outgoing commands (≤ 120/min)
- Auto-skill loop: runs every **2 s** by default
- Auto-skill and all triggers are **disabled by default** — you must opt in
- Use `--rate-limit 1.0` for an even more conservative setting

## Skill Setup Example

```
# In-game, once you know your character's skills:
#skill bash on
#autoskill on
```

The skills loop will then fire `bash` automatically while you are in combat,
once every 4 seconds (configurable in `configs/skills.json`).

## Directory Layout

```
Mudder/
├── main.py              entry point
├── requirements.txt
├── configs/
│   ├── aliases.json     persisted aliases
│   ├── triggers.json    persisted triggers
│   └── skills.json      persisted skills + auto_skill flag
└── mud/
    ├── app.py           MudApp orchestrator
    ├── client.py        TelnetClient (IAC handling, rate limit)
    ├── game_state.py    HP/mana/combat parser
    ├── aliases.py       AliasManager
    ├── triggers.py      TriggerManager
    └── skills.py        SkillsManager
```
