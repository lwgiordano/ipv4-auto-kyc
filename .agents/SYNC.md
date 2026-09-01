# Keeping Claude Code and Codex in sync

Two agents work this repo: **Claude Code** runs in an ephemeral cloud container;
**Codex** runs in a clone on your machine. They share **no filesystem** — the
only place they meet is **GitHub**. So "sync" means git, and it works in both
directions as long as each side pushes what it does and pulls what the other did.

```
   Claude (cloud) ──push──►  GitHub branch  ◄──push── Codex (your laptop)
                  ◄─pull───  (shared)        ──pull─►
```

The shared branch is **`claude/project-setup-verify-kpfgjs`** (both clones are on
it by default).

## The rules (both agents follow these)

1. **Pull before you work:** `./.agents/sync.sh pull`
2. **Push after you commit:** `./.agents/sync.sh push` (or `sync` = pull+push)
3. **Respect claims.** `/AGENT_BUS.md` (repo root) holds CLAIM/RELEASE file
   lanes — never edit a file the other agent has CLAIMed and not RELEASEd.
   That's what prevents two agents colliding on the same branch.
4. **Talk in `/AGENT_BUS.md`.** It's the bus: claims, findings, questions,
   "your turn". Append to the Log (newest on top), commit, push. The other side
   reads it on pull. (`.agents/HANDOFF.md` is the archived earlier mailbox.)

## Making your side auto-update (the closest thing to a live sync)

A hook *here* can't reach your laptop, and Codex's clone won't pull on its own.
So run this tiny loop in a **spare terminal on your machine**, next to Codex —
it pulls Claude's pushes every 30s so your folder stays current:

```sh
cd ipv4-auto-kyc        # your local clone
./.agents/sync.sh watch
```

Leave it running. When Codex commits, push its work back with:

```sh
./.agents/sync.sh sync  # pull anything new, then push Codex's commits
```

(If Codex's environment lets it run shell commands, you can have it call
`./.agents/sync.sh sync` itself after each change — then it's hands-off.)

## What each side can and can't do

| | Claude (cloud) | Codex (your laptop) |
|---|---|---|
| Push its work to GitHub | yes (does it each PR) | yes (`sync.sh push`) |
| Receive the other's work | on `sync.sh pull` at session start / on request | continuous via `sync.sh watch` |
| Reach into the other's folder directly | **no** | **no** |

There is no daemon in the cloud container (it's ephemeral), so Claude pulls when
a session starts or when you ask it to — it can't poll your pushes 24/7. Push a
bus entry and Claude will pick it up next time it runs.

## If a push is rejected (both changed the branch)

```sh
./.agents/sync.sh pull   # rebases your commits on top of theirs
# resolve any conflict it reports, then:
./.agents/sync.sh push
```

Turn-taking (rule 3) keeps this rare.
