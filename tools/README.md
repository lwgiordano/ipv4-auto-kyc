# Two apps, and which one you want

Both open a window that looks like the ops console. Only one of them is the console.

### KYC Full Stack.app — the real thing

Double-click it and it stands up the whole application on your machine: a temporary Postgres,
the migrations, the dev worker, the fake platform receiver, and the API. Send Message runs the
pipeline for real. Approve Manually writes a decision, under your name, to a row you can go and
look at. The database is created when you open the app and thrown away when you quit, so you can
break whatever you like.

It keeps itself current. Every 20 seconds it pulls the branch you have checked out. A change to
the console repaints the open window a couple of seconds later without disturbing the database;
a change to the engine restarts the stack. Leave it open while someone is pushing and you watch
the thing change under you.

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

```
cd ~/ipv4-auto-kyc
git checkout claude/project-setup-standing-rules-5w1fwh
git pull
open "tools/KYC Full Stack.app"
```

The `git checkout` matters. These apps live on that branch, so a `git pull` on any other branch
brings nothing and the tools folder stays as it was.

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

### When it doesn't work

Every failure ends in a dialog naming the cause, with the last ten lines of the log and a button
that opens it. If you get no dialog at all, the app is not what ran — check that
`tools/KYC Full Stack.app` exists in your checkout, which means checking the branch.

Logs are at `~/Library/Application Support/KYC Console/` — `full-stack.log` and `preview.log`.
The same run, from a terminal, prints the same thing without the dialogs:

```
bash "tools/KYC Full Stack.app/Contents/MacOS/kyc-full-stack"
```

## The same, from a terminal

The apps are wrappers. If you would rather watch the output:

```
bash scripts/dev.sh                 # the stack: API on :8080
python3 scripts/devproxy.py --seed  # the console, read from disk, on :8099/ui
python3 scripts/preview.py          # the preview, no stack
```
