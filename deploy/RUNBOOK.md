# RUNBOOK — running the stack as a service on your own server

End state: the Streamlit UI runs 24/7 **loopback-only** behind `tailscale serve`
(HTTPS on your tailnet, never the public internet), and a systemd user timer runs
the daily data job — fetch → categorize (deterministic rules, final) → Drive push →
GitHub push. The ~30-day overfetch safety net self-triggers inside the daily
fetch, so there is no separate monthly schedule. Deep review is the occasional
desktop Claude audit ritual (§12) — the local LLM is off by default.

Everything here is run **by you, on the server**, in order. §§1–6 are setup-only
(no live calls); §7 is the first live run.

## 1. Prerequisites

- Linux server with systemd, Python 3.10+, git, and an SSH login for one
  (non-root) user — everything below is that user's, no root except where noted.
- Tailscale installed and logged in (`tailscale up`) on the server, plus on each
  device that will view the UI.

## 2. Clone the repos

```bash
cd ~
git clone <spend_visualizer remote> spend_visualizer
git clone <finance_data remote> finance_data       # or scp the repo over for now
chmod 700 ~/finance_data
```

Both go in `~` — the units address `%h/spend_visualizer`, and `~/finance_data`
is the default data root (no `data_root` file or env var needed).

## 3. Build the venvs

Copy the exact block from the root README ("Clone + per-component venvs") — five
venvs, all hash-locked installs. Then run every component's offline test suite
(root README "Tests" section) to prove the build before anything live.

## 4. Migrate `.secrets/` (complete sets, exact perms)

From the desktop:

```bash
scp -r ~/spend_visualizer/transactions/.secrets             server:spend_visualizer/transactions/
scp -r ~/spend_visualizer/plaid_category_transformer/.secrets server:spend_visualizer/plaid_category_transformer/
ssh server 'chmod 700 ~/spend_visualizer/*/.secrets && chmod 600 ~/spend_visualizer/*/.secrets/*'
```

Required per component:

- `transactions/.secrets/`: `.env`, `tokens.json`, `client_secret.json`,
  `token.json`, `drive_state.json`
- `plaid_category_transformer/.secrets/`: `client_secret.json`, `token.json`,
  `drive_state.json`, `merchant_memory.json`

> **⚠ `drive_state.json` is load-bearing.** It is the file-id memory mapping
> store names to the Drive files that already exist. A server without it doesn't
> error — it quietly **creates new Drive files**, permanently forked from the
> ones your desktop (and your history) point at. Migrate it, both components.

## 5. GitHub remote for finance_data (deploy key)

One key, write access to that single private repo — leaking it exposes nothing
else (why a deploy key beats a personal token here):

```bash
# on the server
ssh-keygen -t ed25519 -f ~/.ssh/finance_data_deploy -N '' -C finance-data-deploy
cat ~/.ssh/finance_data_deploy.pub
```

GitHub → the private `finance_data` repo → Settings → Deploy keys → add the
public key, **check "Allow write access"**. Then:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com-finance-data
    HostName github.com
    IdentityFile ~/.ssh/finance_data_deploy
    IdentitiesOnly yes
EOF
cd ~/finance_data
git remote add origin git@github.com-finance-data:<you>/finance_data.git
```

The first `--push-data` run pushes with `-u` and sets the upstream; no manual
first push needed. (Passphrase-less is deliberate: an unattended timer can't
type one, and the key's blast radius is this one repo.)

## 6. Google OAuth app → Production (one-time, on the desktop is fine)

If your OAuth consent screen is still **Testing**, refresh tokens die every
7 days and the unattended server breaks weekly. Google Cloud console → APIs &
Services → OAuth consent screen → **Publish app**. `drive.file` is a
non-sensitive scope; no verification review is required.

Day-to-day the server needs no OAuth (the migrated `token.json` refreshes
itself). If you ever must re-auth **on the server**: the console flow prints a
URL whose redirect lands on `localhost:<random-port>` *on the server* — read the
port from the printed URL, forward it (`ssh -L <port>:127.0.0.1:<port> server`),
then open the URL in your laptop browser.

## 7. First live run (by hand, watch it)

```bash
cd ~/spend_visualizer/finance_pipeline
python3 run.py --no-ui --no-llm --push-data
```

Expect: fetch reconciles cleanly against Drive (proof `drive_state.json` came
across), categorize adopts the Drive head and applies the deterministic rules
(new rows stamped final — the local LLM is off; deep review is §12), and the
data repo lands on GitHub.
**On the server, always pass `--no-ui` for manual runs** — once the UI service
owns port 8501, a second Streamlit can't bind, and the pipeline's readiness
probe would false-positive against the service's socket.

## 8. Install the units

```bash
~/spend_visualizer/deploy/install.sh
```

Installs user-level units (UI service + daily timer + failure hook) and enables
linger so they run without an open SSH session. Check:

```bash
systemctl --user list-timers finance-daily.timer
systemctl --user status spend-analyzer.service
journalctl --user -u finance-daily -f        # watch the next 06:30 run
```

## 9. Tailscale serve (HTTPS on the tailnet)

```bash
sudo tailscale set --operator=$USER     # once: lets your user manage serve
tailscale serve --bg 8501
tailscale serve status
```

In the Tailscale admin console enable **MagicDNS** and **HTTPS certificates**
(Settings → DNS). The UI is then at `https://<server>.<tailnet>.ts.net`. Serve
config persists in tailscaled across reboots — it's host-level state, which is
why it isn't part of the systemd units.

**If the page sticks on "Please wait…"** (websocket blocked behind the proxy),
set **both** of these in `spend_analyzer/.streamlit/config.toml` and restart the
service — Streamlit silently re-enables CORS while XSRF is on, so flipping one
alone does nothing:

```toml
[server]
enableCORS = false
enableXsrfProtection = false
```

Acceptable here: the app is loopback-bound and reachable only inside the tailnet.

## 10. Partner access

Tailscale admin console → invite partner → their devices join the tailnet; the
same `https://…ts.net` URL works. Optionally pin them down with an ACL that
allows only this host's `:443`.

## 11. When a bank forces re-link

Plaid Items occasionally require re-auth (bank password change, consent expiry —
the fetch will fail for that Item). The Link app binds `127.0.0.1:5000`, so
tunnel it:

```bash
ssh -L 5000:127.0.0.1:5000 server
# on the server:
cd ~/spend_visualizer/transactions && ./venv/bin/python app.py --user <owner>
# then open http://localhost:5000 in the laptop browser and re-link
```

## 12. The Claude audit ritual (occasional)

The server's daily run categorizes with the deterministic rules and stamps new
rows **final** (no local LLM). When you want a deep review, run the **Claude audit
ritual** from the **desktop** — see the root `README.md` "Everyday workflow" §2–3
or `plaid_category_transformer/README.md` "Claude audit ritual" (the
`/audit-transactions` skill): export the unreviewed rows, let Claude flag
suspicious ones + propose rules, then adjudicate and push:

```bash
cd ~/spend_visualizer/plaid_category_transformer
./.venv/bin/python categorize.py --claude-export       # then run /audit-transactions in Claude Code
./.venv/bin/python categorize.py --claude-apply --no-drive   # apply Claude's flags locally
./.venv/bin/python categorize.py --review              # adjudicate → re-persist → push Drive
```

This is safe by construction: every Drive-enabled categorize run starts by
**adopting the Drive head** — the other machine's audited rows, corrections, and
manual-edit intents are pulled in before anything is audited or pushed. Where
both machines touched the same row, the **more recently audited side wins**
(per-row `category_audited_at` stamps), so work that only exists locally — an
offline session, a crash before the push — is never lost; both versions of every
conflict are also appended to
`~/finance_data/plaid_category_transformer/data/adopt_conflicts.jsonl`, which
the daily git push preserves. The rules that keep it that way:

- **Don't run both machines at once.** The desktop ritual and the server's
  06:30 ± 10m timer window shouldn't overlap. The stores re-converge on the
  following runs (and the conflict log keeps every version), but an overlap can
  waste review work. Run the ritual any other time, or
  `systemctl --user stop finance-daily.timer` first.
- **The server is the sole GitHub pusher.** Desktop never pushes or commits
  `finance_data`. Don't bother `git pull`ing into the desktop's live data root
  either — its working tree is always dirty from local runs and the contents
  converge via Drive anyway. To browse history, clone the GitHub repo to a
  separate directory.
- Corrections and review can happen on **either** machine now (the intent log
  union-merges), but the served UI is the natural place for corrections, and
  the review queue (`categorize.py --review`) runs wherever you are —
  over SSH on the server, or on the desktop during a ritual.
- Always run the ritual through `./run.py` (fetch first), not a standalone
  `categorize.py` against a stale raw store. This is also enforced: the prune
  gate stops any categorize run whose input is missing posted rows the store
  already has, so a stale machine cannot shrink the shared store.

## 13. Day-to-day ops

| What | How |
|---|---|
| Did last night run? | `journalctl --user -u finance-daily -n 50` |
| Anything failing? | `cat ~/finance_data/logs/failures.log` (written by the OnFailure hook) |
| Did a two-machine conflict resolve? | `cat ~/finance_data/plaid_category_transformer/data/adopt_conflicts.jsonl` (both versions of every resolved conflict) |
| UI health | `systemctl --user status spend-analyzer` (auto-restarts on crash) |
| Manual run | `cd ~/spend_visualizer/finance_pipeline && python3 run.py --no-ui --no-llm --push-data` |
| Pause the schedule | `systemctl --user stop finance-daily.timer` (start to resume) |
| Remove everything | `~/spend_visualizer/deploy/uninstall.sh` |

A run that overlaps a manual one exits immediately with a lock message
(`<data_root>/.pipeline.lock`) — that's the guard working, just re-run.
If journald on the server doesn't persist across reboots, `mkdir -p
/var/log/journal && sudo systemctl restart systemd-journald` makes it durable.
