# Token Harbor REG

Registers free accounts on [tokenharbor.ai](https://tokenharbor.ai) with a
disposable email, verifies the address, enables the free models, creates an API
key through the dashboard UI, and checks the key with a real chat request. The
verified keys are loaded into a local OpenAI-compatible **router** so many
accounts share one endpoint.

It also ships a small **web dashboard** (single-file HTML + a stdlib Python
server) to run and watch registration, scan per-key quota, enable/disable or
prune accounts, and sit a **throttle gateway** in front of the router that keeps
one IP from tripping the site's per-network rate limit.

Browser automation runs on [Camoufox](https://camoufox.com) (Firefox-based).
Email comes from a disposable-mail backend — [mail.tm](https://mail.tm) by
default, or duckmail.sbs / guerrillamail / temp-mail.io / inboxes.com / a
random pick per attempt (`MAIL_PROVIDER`). When the chosen service is
rate-limited or the mail never lands, the engine falls back through the other
providers. Each account is registered from a different ExpressVPN region.

## Requirements

- Python 3.10+
- [ExpressVPN](https://www.expressvpn.com) installed and logged in, with the
  `expressvpnctl` CLI available (default path
  `C:\Program Files\ExpressVPN\expressvpnctl.exe`, override with the
  `EXPRESSVPNCTL` env var)
- A local OpenAI-compatible router the dashboard can read and write (default
  `http://127.0.0.1:20128`)
- Windows / macOS / Linux

```bash
pip install -r requirements.txt
python -m camoufox fetch      # download the browser once
```

## CLI

Interactive (menu, prompts for count / concurrency / accounts per region):

```bash
python main.py
```

Non-interactive via environment variables:

| Var              | Meaning                                  | Default |
| ---------------- | ---------------------------------------- | ------- |
| `REG_COUNT`      | how many accounts to register            | `30`    |
| `REG_CONCURRENT` | parallel tasks                           | `5`     |
| `REG_PER_IP`     | accounts per VPN region before switching | `4`     |
| `REG_NET`        | `direct` (current VPN) or `express`      | `direct`|
| `REG_HEADLESS`   | `1` = headless, `0` = show window        | `1`     |
| `REG_WEB`        | `1` = verify / create key / chat over HTTP instead of the UI | `0` |
| `REG_ROUTER`     | `1` = auto-add each live key to the router | `1`   |
| `REG_ROUTER_PROVIDER` | provider node (name or id) to add new keys to | `a` |
| `ROUTER_URL`     | router base URL                            | `http://127.0.0.1:20128` |
| `ROUTER_TOKEN`   | bearer token for the router API (if it needs one) | unset |
| `REG_PASSWORD`   | fixed account password (else a random one per run) | random |
| `MAIL_PROVIDER`  | `mailtm`, `duckmail`, `guerrilla`, `tempmailio`, `inboxes` or `random` | `mailtm` |
| `GATE_KEYPOOL`   | `1` = the gateway keeps its own key pool and rotates it (no router hop) | `1` in `web.bat` |
| `GATE_POOL_UP`   | upstream for pool mode                                   | `https://tokenharbor.ai` |
| `GATE_POOL_PREFIX` | only models starting with `<prefix>/` use the pool     | `a` |
| `GATE_TOKEN_FILE` | key files, `;` separated                                | `alive_tokens.txt;tokens.txt` |
| `GATE_POOL_TRIES` | keys tried for one request                              | `12` |
| `GATE_POOL_TIMEOUT` | per-try timeout in seconds (streaming-friendly)       | `300` |
| `GATE_POOL_WAIT` | when all keys are parked, wait this long for the soonest one | `60` |
| `REG_VERBOSE`    | `1` = full log instead of one status line | `0`  |
| `REG_APPEND`     | `1` = append to existing exports          | unset   |

```bash
REG_COUNT=10 REG_CONCURRENT=5 REG_PER_IP=5 REG_HEADLESS=1 python main.py
```

Set `VPN_REGIONS` in `main.py` to the regions available on your ExpressVPN plan.

## Web dashboard

Start it with the launcher (finds `webui.py`, opens the browser, restarts on
crash):

```bat
web.bat
```

or directly:

```bash
python webui.py            # http://127.0.0.1:8787
```

Tabs: **Overview**, **Register** (runs the CLI flow with a live log), **Keys**
(exported keys vs. keys in the router), **Usage** (per-key quota scan),
**Providers** (the router's account list — enable/disable/delete/add),
**Gateway / IP** (throttle + IP rotation). The Register tab has a **mail**
dropdown that picks the disposable-mail backend for the run.

On first load after an update the dashboard shows a **"What's new"** notice:
the changes for the version you are running plus an invite to the community
Discord. **Don't show again** silences that version only - the next update
brings the notice back.

| Var             | Meaning                                            | Default                  |
| --------------- | -------------------------------------------------- | ------------------------ |
| `WEBUI_PORT`    | dashboard port                                     | `8787`                   |
| `WEBUI_GATE`    | `1` = start the throttle gateway with the panel    | `1`                      |
| `WEBUI_VPN`     | `1` = start the IP rotator with the panel          | `0`                      |
| `WEBUI_PREWARM` | `1` = warm the browser before the first account    | `1`                      |
| `ROUTER_URL`    | the router the panel talks to                      | `http://127.0.0.1:20128` |
| `ROUTER_TOKEN`  | bearer token for the router API (if it needs one)  | unset                    |
| `ROUTER_HOME`   | the router's data dir (its SQLite `db/data.sqlite`)| per-OS default           |
| `GATE_PORT`     | throttle gateway port                              | `20130`                  |
| `GATE_RPM`      | gateway rate, requests/minute (`0` = auto)         | `0`                      |
| `GATE_UP_TIMEOUT` | seconds to wait for the upstream per attempt     | `90`                     |
| `GATE_MAXWAIT`  | total seconds a single request may keep retrying   | `300`                    |

A hung upstream used to burn the whole retry budget in one attempt (one 180s
stall → give up). Now each attempt is capped at `GATE_UP_TIMEOUT` and the total
budget (`GATE_MAXWAIT`) is large enough for all `GATE_RETRY` retries, with a
1.5s→3s→6s back-off between 5xx retries.

## Updates

Every checkout carries a `VERSION` file. The dashboard checks GitHub for a newer
version (comparing the version file and, in a git checkout, the commit sha) and
shows a banner with **Update now** / **Later** when one exists. *Update now* runs
`git pull --ff-only` in a checkout, or, for a plain download with no `.git`,
downloads the repo zip and writes it over the code (your `tokens.txt`,
`accounts.db`, `router.json`, `logs/` and `debug/` are never touched). Either way,
restart the panel after updating. Point `UPDATE_REPO` at a fork if you use one.

## Why the gateway

tokenharbor's free tier blocks by **network (IP)**, not by key. One IP firing
too fast starts answering `429 "many free accounts are sharing this network
address"`, and for the next few minutes almost every request fails. The router
does round-robin over keys but knows nothing about the IP's budget.

`gate.py` fixes that: it holds a token bucket at `GATE_RPM`, reads
`Retry-After` / `"next request in N s"` on a `429` and pauses everything, then
retries a few times before answering. Point your client at
`http://127.0.0.1:20130/v1` instead of `...:20128/v1`.

### Front door and two backends

Clients only ever talk to `:20130`. That port is the **front door**
(`gate_front.py`); the gateway itself runs twice, on `:20140` and `:20141`
(`gate.py`). The front forwards to the active backend, retries the other one when
a backend is not answering, and streams the answer back unchanged, so a backend
being restarted is invisible to a chat client.

**Editing the gateway while clients are connected** (the safe way):

```
python -m py_compile gate.py
python gate_swap.py            # --restart forces the idle backend to reload
```

`gate_swap.py` starts a fresh backend on the idle port, health-checks it (its
stats plus a real chat request), tells the front to switch to it, verifies a few
requests through the front, and only then stops the old backend. If any step
fails it aborts and the live backend keeps serving - traffic is never
interrupted. `web.bat` and the dashboard both start through `gate_boot.py`, so
the topology is set up automatically and `web.bat` no longer frees the gateway
port on launch.

### Three safety layers

1. **Front door + two backends** - editing the gateway never drops the port
   clients use (`gate_swap.py`).
2. **`gate_boot.py`** - starts only what is missing (front, backends).
3. **`guard.py`** - a detached supervisor that keeps the panel *and* the gateway
   alive on its own, so no window has to stay open. `web.bat` and the panel both
   start it; `web.bat` never kills it. Stop it with a `.guard_stop` file.

### Split Tunnel (only the gateway + reg use the VPN)

A system-wide VPN slows the whole machine down. ExpressVPN's CLI only supports a
*bypass* list, so instead we bypass the heavy apps and keep `python` / `camoufox`
tunnelled:

```
split_tunnel.bat            # double-click: runs as admin (the VPN CLI needs root)
python split_tunnel.py status
python split_tunnel.py add "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

`auto` enables Split Tunnel and bypasses the browsers/IDE/chat already running,
and never bypasses `python`/`camoufox`.

## Files

| File            | Purpose                                                   |
| --------------- | --------------------------------------------------------- |
| `main.py`       | registration flow, key creation, DB, exports              |
| `chromedrv.py`  | Camoufox browser/context factory                          |
| `fastfox.py`    | Camoufox launch helpers and options                       |
| `foxserve.py`   | shared Camoufox browser server (one browser stays hot)    |
| `mailtm.py`     | mail.tm client                                            |
| `tempmail.py`   | disposable-mail backends (mail.tm / duckmail / guerrilla / temp-mail.io / inboxes) |
| `vpn.py`        | ExpressVPN CLI wrapper                                    |
| `webui.py`      | dashboard server (stdlib HTTP) + backend jobs             |
| `webui.html`    | dashboard UI (single file, no build step)                 |
| `gate.py`       | throttle/retry gateway in front of the router             |
| `gate_front.py` | front door on `:20130` - failover + streaming passthrough |
| `gate_boot.py`  | start/ensure the front door + two gateway backends        |
| `gate_swap.py`  | swap a backend to new code without dropping traffic       |
| `handover.py`   | restart the front door itself (after editing `gate_front.py`) |
| `guard.py`      | detached supervisor: keeps the panel + gateway alive      |
| `guard.bat`     | start `guard.py` detached (survives closing the window)   |
| `split_tunnel.py` | keep the machine direct, only gateway + reg on the VPN  |
| `split_tunnel.bat`| run `split_tunnel.py auto` as admin (VPN CLI needs root) |
| `prune_scan.py` | sweep every key once and remove content-policy-flagged accounts |
| `NOTICE.json`   | what's-new text + Discord invite shown in the dashboard   |
| `restart_stack.py` | restart panel/gate with three fallback paths           |
| `stress_gate.py`   | hammer the gateway the way a chat client does          |
| `pool.py`       | key-pool strategy + weekly-quota scan                     |
| `usage_watch.py`| per-key usage probe, groups accounts, auto-disables       |
| `rotator.py`    | IP rotation loop driven by the gateway                    |
| `VERSION`       | current version (used by the dashboard update check)      |

## Notes

- Each live key is POSTed to a provider node on the router right after it is
  verified. Choose **which** router and node on the dashboard's **Register**
  tab, under *"where new keys go"*: router URL, optional API token, and the
  provider node (picked from the router's own node list). It is saved to
  `router.json` (git-ignored) and handed to the engine. Equivalent env vars:
  `ROUTER_URL`, `ROUTER_TOKEN`, `REG_ROUTER_PROVIDER` (a node name **or** id).
  Set `REG_ROUTER=0` to leave the router alone. `main.py` prints
  `[DONE] <name> | <key>` for each live account.
- If the router's admin API answers `401`: when it is **9router running on the
  same machine** the panel derives the token itself (it sends the `x-9r-cli-token`
  header, computed as `sha256(machine-id + "9r-cli-auth" + cli-secret)[:16]` from
  `%APPDATA%\9router` / `~/.config/9router`; set `ROUTER_HOME` if it lives
  elsewhere) - no manual setup. For any other router, paste its API key in **API
  token** and press **Load nodes** (sent as `Authorization: Bearer` and
  `x-api-key`). The provider field is a free text box, so you can also just type
  the provider node's name or id and press **Save** without loading the list.
- A shared Camoufox browser server (`foxserve.py`) is started once per run, so
  every account only opens a new context instead of launching Firefox again
  (roughly 0s vs. 2-6s). `CAMOUFOX_WS` connects to an external server and
  `REG_FOX_KEEP=0` kills the one this run started.
- The site limits how many accounts one IP can register; keep `REG_PER_IP` low
  (4-5) and expect `network_blocked` when a region is exhausted.
- Signup uses a Cloudflare Turnstile token that is single-use: a failed submit
  reloads the page for a fresh token before retrying.
- The API key must be created through the dashboard UI; creating it via a raw
  `POST /api/keys` call gets rate-limited.
- Cloudflare also rate-limits signups per IP and answers with `429` plus its
  `Just a moment...` interstitial. Clicking through the interstitial clears the
  challenge but the per-IP quota stays spent, so the flow retries once on the
  same IP and then reports `cf_blocked` to make the loop switch region/IP.

## License

MIT, see [LICENSE](LICENSE).
