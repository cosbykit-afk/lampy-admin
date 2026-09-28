# Lampy Console — how to run it

No command prompt. No WSL. Just a browser (or an SSH client).

## Current status (2026-09-27, verified)

The console is **fully running inside the `lampy` system** — web UI and
SSH admin both answer there. But Windows itself **cannot reach it yet**:
a WSL networking problem on Toetop is blocking all distro ports from
Windows (browsers and SSH alike), and fixing that is a separate step
(mirrored networking or a WSL relay reset — your call, nothing to
change in the console). Until then, the addresses below work from
inside the `lampy` system only.

## Open it (once Windows can reach it)

On Toetop, open a browser and go to:

**http://localhost:8090**

(That address only works on Toetop itself — the console listens on
localhost only, so nobody else on the network can reach it. It runs
inside the `lampy` system on Toetop and starts automatically with it.)

## First login

Log in with a **forum account** — the same username/password you would
use on the forum itself. If you don't have one yet, register at
http://localhost/app/register first, then log in here.

The **Admin** tab and the service-control buttons only appear for
accounts with the forum's admin flag.

## The tabs

- **Stack** — seven cards, one per service (database, web server, forum
  app, AI search worker, embeddings, mail, workspace IDE). Green means
  the console really reached it; red means it really failed (the error
  is shown). Each card also shows the service's supervisor state.
  Admins get per-service **restart / stop / start** buttons, a **logs**
  viewer (the real service log), and whole-stack **Start all /
  Restart all / Stop all** — everything is wired, nothing is a stub.
- **Forum** — browse categories, threads, and posts, live from the
  database.
- **Write** — start a new thread or reply, with the same rules as the
  forum site (title ≤ 200 chars, message ≤ 20,000 chars, locked threads
  stay locked).
- **Search** — keyword search over posts and thread titles. Semantic
  search is labeled "keyword mode": the forum app's own semantic path
  is an unwired stub, and the console matches the app honestly.
- **Docs** — the docs table in the forum database, bodies and diagram
  images.
- **Metrics** — events per day from the forum's metrics table.
- **Mail** — connect with a James mailbox name and password (mailboxes
  live on the `localhost` mail domain, e.g. `kit@localhost`), read the
  inbox, send mail. If no mailbox exists yet, the tab says so instead
  of making anything up. Your mailbox password is kept in the
  console's memory only — never in a cookie, never on disk.
- **Admin** (admins only) — user list, lock/unlock threads, **download
  a full database backup** (one click, no terminal), and a button that
  opens the code-server IDE in a new tab.

## If something is red

A red card shows the exact error (connection refused, timeout, …).
The supervisor state under each card tells you whether the service's
process is actually running. If a service is down, an admin can
restart it right from its card — no terminal needed.

The console's own log lives at
`/var/log/supervisor/console.log` inside the `lampy` system
(visible to Muse for troubleshooting).

## SSH admin (no browser needed)

In addition to the web UI, the console runs its own SSH server on
**port 8022** (localhost only, same reachability note as above).
No password, no shell — key login only, using the **same SSH key you
already use on Toetop** (the console checks it against your existing
Windows authorized_keys, so there is nothing new to set up).

From a terminal / PowerShell on Toetop (once Windows can reach it):

```
ssh -p 8022 <any-username>@localhost "status"
```

The username is ignored — your key is your identity. Every command is
a single quoted string; there is no interactive shell. Available
commands (also listed by `ssh -p 8022 <u>@localhost "help"`):

- `status` — the seven service probes + supervisor state
- `restart <svc>` / `start <svc>` / `stop <svc>` — e.g. `restart forum`
- `restart-all` / `start-all` / `stop-all` — the whole stack
  (the console itself is excluded)
- `logs <svc> [lines]` — tail a service's log
- `users` — forum user list
- `threads [limit]` — recent threads (for lock/unlock)
- `lock <thread_id>` / `unlock <thread_id>`
- `backup` — streams a full database dump to your terminal
  (save it with `> lampy-backup.sql`)
- `metrics [days]` — forum events per day

`<svc>` is one of: postgres apache2 forum ollama james pgai-worker
codeserver bible console.

### From off-machine

Once the WSL networking is fixed, the same SSH port is reachable
through Toetop's built-in Windows OpenSSH as a jump host — no Remote
Desktop, no WSL terminal:

```
ssh -J kitco@<toetop-address> -p 8022 <any-username>@localhost "status"
```

(From your other machines, `<toetop-address>` is Toetop's Tailscale
address — the same one already used for SSH to Toetop.)
