# Two apps, and which one you want

Both open a window that looks like the ops console. Only one of them is the console.

### KYC Full Stack.app — the real thing

Double-click it and it stands up the whole application on your machine: a temporary Postgres,
the migrations, the dev worker, the fake platform receiver, and the API. Send Message runs the
pipeline for real. Approve Manually writes a decision, under your name, to a row you can go and
look at. The database is created when you open the app and thrown away when you quit, so you can
break whatever you like.

It keeps itself current. It pulls on startup, and every 20 seconds after. A change to the console
repaints the open window a couple of seconds later without disturbing the database; a change to
the engine restarts the stack; a change to the launcher restarts the launcher. Leave it open while
someone is pushing and you watch the thing change under you.

You can tell it apart without reading anything twice: the window is titled `Full stack · …`,
and the sidebar footer says **Full stack — temporary database**.

### KYC Preview.app — the screens, and nothing behind them

One HTML file and a captured sample. No database, no API, no network. It is for looking at the
design — the layout, the type, a case in each of its states — without waiting for anything to
start. Every button that would write refuses: the page answers its own requests from the fixture.

If you press Send Message and nothing happens, this is the app you opened.

Same two places: the window is titled `Preview · …`, and the sidebar footer says
**Preview — sample data, nothing sends**.

## Running them

Double-click **KYC Full Stack.app**. That is the whole procedure.

It pulls before it starts anything, so what you get is what was last pushed, and it keeps pulling
every 20 seconds while it runs. If the pull replaces the launcher itself, it restarts into the new
one rather than carrying on as the old one. A checkout with local changes is left alone — your
edits are not the app's to discard, and it says so instead of pulling over them.

The one thing it cannot do for you is switch to a branch it isn't on. These apps live on
`claude/project-setup-standing-rules-5w1fwh`; if `tools/` doesn't contain them, that is why:

```
cd ~/ipv4-auto-kyc && git checkout claude/project-setup-standing-rules-5w1fwh
```

The bundles ship inside the repo, so each one finds the checkout by walking up from itself. Drag
a copy to /Applications if you prefer and it will remember where the repo was; move the repo and
it searches your home folder, then asks.

macOS will refuse an unsigned app downloaded from the internet. These arrive by `git clone`, not
by download, so Gatekeeper leaves them alone. If it does complain, right-click → Open once.

### What the first run needs

Preview needs `python3` and nothing else.

Full Stack runs the real application, so it needs what the application needs:

- **`python3`** — `xcode-select --install` if you don't have it.
- **The project's Python environment.** If `.venv/` is not in your checkout, the app offers to
  build it by running `./manage.sh setup`. Say yes once; it downloads dependencies, takes a few
  minutes, and after that opening the app just works.
- **Postgres binaries** (`initdb`, `pg_ctl`) — `brew install postgresql@16`, or Postgres.app.
  The app does not install this for you. It does not need a running server or a database you
  have made: it starts its own on a spare port and deletes it when you quit.

Two environment variables, on either app:

- `KYC_CONSOLE_BRANCH` pins a branch instead of following the one you have checked out.
- `KYC_CONSOLE_INTERVAL` sets the seconds between pulls (default 20).

### It opens a Terminal window, on purpose

Double-clicking either app opens Terminal and runs it there. Finder starts a process with no
terminal attached, so anything that went wrong had nowhere to appear and the app looked like it
did nothing at all — while the same script run from a shell worked first time. The window is the
run: it shows the stack coming up, every callback as it lands, and any error in full. Ctrl-C, or
closing the window, stops everything and drops the temporary database.

Failures also raise a dialog naming the cause, with the last ten lines of the log and a button
that opens it. Logs are at `~/Library/Application Support/KYC Console/` — `full-stack.log` and
`preview.log`.

If nothing happens at all, the app is not what ran: check that `tools/KYC Full Stack.app` is in
your checkout, which means checking the branch.

## The same, from a terminal

The apps are wrappers. If you would rather watch the output:

```
bash scripts/dev.sh                 # the stack: API on :8080
python3 scripts/devproxy.py --seed  # the console, read from disk, on :8099/ui
python3 scripts/preview.py          # the preview, no stack
```
