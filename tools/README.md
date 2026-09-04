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
git pull
open "tools/KYC Full Stack.app"
```

The bundles ship inside the repo, so each one finds the checkout by walking up from itself. Drag
a copy to /Applications if you prefer and it will remember where the repo was; move the repo and
it searches your home folder, then asks.

macOS will refuse an unsigned app downloaded from the internet. These arrive by `git clone`, not
by download, so Gatekeeper leaves them alone. If it does complain, right-click → Open once.

Full Stack needs `python3` and a Postgres it can start; `scripts/dev.sh` looks for one across
apt, Homebrew and Postgres.app and tells you what is missing. Preview needs only `python3`.

Two environment variables, on either app:

- `KYC_CONSOLE_BRANCH` pins a branch instead of following the one you have checked out.
- `KYC_CONSOLE_INTERVAL` sets the seconds between pulls (default 20).

Logs go to `~/Library/Application Support/KYC Console/` — `full-stack.log` and `preview.log`.

## The same, from a terminal

The apps are wrappers. If you would rather watch the output:

```
bash scripts/dev.sh                 # the stack: API on :8080
python3 scripts/devproxy.py --seed  # the console, read from disk, on :8099/ui
python3 scripts/preview.py          # the preview, no stack
```
