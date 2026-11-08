# parent-school-skills

Two read-only [Agent Skills](https://agentskills.io) that let an AI agent answer a parent's
questions about their child's school records, such as "What's missing this week?", "How
many tardies so far?" or "Who teaches 3rd period?".

| Skill | System | Reads |
|---|---|---|
| [`canvas-grades`](canvas-grades/SKILL.md) | Canvas LMS (parent/observer account) | Grades, assignment scores, missing/late work, due dates, teacher comments |
| [`parentvue-grades`](parentvue-grades/SKILL.md) | Edupoint Synergy ParentVUE (parent login) | Attendance, schedule and teachers, plus gradebook and report cards where the district enables them |

Each skill is a folder with a `SKILL.md` (instructions for the agent) and a single Python
script that does all the network access. The scripts use only the Python standard library
and can't write anything: they enforce a fixed allowlist of read-only API calls.

They work with any agent that supports `SKILL.md` skill folders and can run shell commands,
including **Claude Code**, **OpenAI Codex** and **Hermes Agent**.

> **Unofficial.** This project isn't affiliated with or endorsed by Instructure (Canvas) or
> Edupoint (Synergy/ParentVUE). ParentVUE has no public API, so `parentvue-grades` uses the
> same JSON API as the official mobile app, and it can break if Edupoint changes it. Use it
> only with your own parent account, and check your district's acceptable-use terms.

## Requirements

- Python 3.8 or newer (`python3`), macOS or Linux
- A parent account: a Canvas **observer** account linked to your child, and/or a ParentVUE
  parent login
- An agent that can load skills and run shell commands, with outbound HTTPS access

## Install

```bash
git clone https://github.com/rpg36/parent-school-skills.git
cd parent-school-skills
```

### Claude Code

```bash
./install.sh --claude
```

This copies both skills to `~/.claude/skills/`. For a single project, use
`./install.sh --dir <project>/.claude/skills` instead.

### OpenAI Codex

```bash
./install.sh --codex
```

This copies both skills to `~/.agents/skills/`. For a single repository, use
`./install.sh --dir <repo>/.agents/skills` instead. Codex's default sandbox blocks network
access, so approve the network request when Codex asks to run a skill's script, or enable
network access for your sandbox in `~/.codex/config.toml`.

### Other agents (Hermes, etc.)

Copy the skill folders to wherever your agent loads skills from:

```bash
./install.sh --dir ~/path/to/agent/skills
```

Or build tarballs with `./package.sh` (written to `dist/`) and extract them on another
machine. Hermes reads the `required_environment_variables` in the frontmatter and prompts
for the credentials itself, so you can skip the next step there if you prefer.

Pass `--skill canvas-grades` or `--skill parentvue-grades` to install just one.

## Set up credentials

Do this yourself in a terminal, not through the agent. Each script has an interactive
`configure` command that reads secrets with hidden input and saves them to
`~/.config/school-skills/<name>.env` with mode `0600`. The agent never needs to see them.
`configure` refuses to run without an interactive terminal.

### Canvas

1. Log in to Canvas as **yourself (the parent)**, not as your child.
2. Go to **Account → Settings → Approved Integrations → + New Access Token**.
3. Give it a purpose like `AI agent grades (read-only)`. Set an expiry date to limit the
   damage if the token ever leaks.
4. Copy the token. Canvas shows it only once.
5. Note your school's Canvas address from the browser bar, e.g.
   `https://yourdistrict.instructure.com`.

```bash
python3 ~/.claude/skills/canvas-grades/scripts/canvas_read.py configure
```

(Use the path you installed to.) It checks the connection when it's done. You should see
your name and `"linked_students": 1` or more.

### ParentVUE

You need the ParentVUE web address (the `https://…` host of your district's ParentVUE login
page, often `https://<something>.edupoint.com`) and your parent username and password.
**Use a password you don't use anywhere else.**

```bash
python3 ~/.claude/skills/parentvue-grades/scripts/parentvue_read.py configure
```

It signs in once to check. A rejected login is never retried, so a typo can't lock your
account.

### Alternatives

- **Environment variables** (`CANVAS_BASE_URL`, `CANVAS_TOKEN`, `PARENTVUE_BASE_URL`,
  `PARENTVUE_USERNAME`, `PARENTVUE_PASSWORD`) work too and take priority over the file.
- **Several children:** set `CANVAS_STUDENT` / `PARENTVUE_STUDENT` (or answer the
  "default student" prompt), or the agent passes `--student <name>`.
- **Config location:** set `SCHOOL_SKILLS_CONFIG_DIR` to keep the files somewhere other
  than `~/.config/school-skills/` (`$XDG_CONFIG_HOME` is respected).

## Use it

Ask your agent things like:

- "How is my kid doing in school this week?"
- "Is anything missing or overdue in Canvas?"
- "What's due in the next two weeks?"
- "Any absences or tardies this month?"
- "What's the email for the science teacher?"

The agent runs the scripts, which print JSON, and summarizes the results. You can also run
them yourself, e.g. `python3 …/canvas_read.py summary` or `python3 …/parentvue_read.py
attendance --days 30`. Run any script with `--help` to see every command.

## District-specific notes

Districts set these systems up differently. For example, some keep grades only in Canvas and
turn off the ParentVUE gradebook. The skills handle this generically, but you can give your
agent notes about your own school without changing the skill: put them in
`<skill>/references/local-notes.md` in the installed copy. The agent reads that file first.
To manage notes outside the install, keep them as `<dir>/canvas-grades.md` and
`<dir>/parentvue-grades.md` and pass `--notes-dir <dir>` to `install.sh` or `package.sh`.
`local-notes.md` is git-ignored here so personal details don't end up in a fork.

## Security model

- **Read-only by construction.** The Canvas script only sends `GET` requests, to 7
  allowlisted API paths on your school's host. The ParentVUE script only calls 6 read
  methods plus login/refresh, with fixed, validated parameters. Neither has a "raw request"
  command, and neither follows redirects, so credentials can't be forwarded to another host.
- **Sensitive areas are excluded.** ParentVUE student info (address, medical, contacts),
  health, discipline, fees, mail and every update method can't be called.
- **The agent is told never to touch the credentials** or to call the APIs any other way,
  and to treat text from school systems as data, not instructions.
- **Remaining risk:** an agent with shell access could still read the config file or
  environment and misuse the credentials directly. Canvas tokens can't be limited to
  read-only, and the ParentVUE credential is your real password. Limit who can talk to the
  agent, give the Canvas token an expiry, and use a unique ParentVUE password.
- **Revoke anytime:** delete the token in Canvas (Account → Settings → Approved
  Integrations), change your ParentVUE password, and delete `~/.config/school-skills/`.

Details: [canvas-api-notes.md](canvas-grades/references/canvas-api-notes.md),
[parentvue-api-notes.md](parentvue-grades/references/parentvue-api-notes.md).

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests run each CLI against a local mock server, so they need no network access or real
credentials. `./install.sh --claude --link` symlinks the skills into your agent for live
editing. `./package.sh` runs the tests and builds `dist/*.tar.gz`.

ParentVUE field names partly come from community documentation
([StudentVue/docs](https://github.com/StudentVue/docs)). If a command returns empty or odd
results for your district, `parentvue_read.py debug-shape --method <Method>` prints the
response structure (keys and types, never values), which you can safely share in an issue.

## License

[MIT](LICENSE)
