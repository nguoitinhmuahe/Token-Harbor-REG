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
Email comes from [mail.tm](https://mail.tm). Each account is registered from a
different ExpressVPN region.

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
| `REG_PER_IP`     | accounts per VPN region before switching | `10`    |
| `REG_HEADLESS`   | `1` = headless, `0` = show window        | `1`     |
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
**Gateway / IP** (throttle + IP rotation).

| Var             | Meaning                                            | Default                  |
| --------------- | -------------------------------------------------- | ------------------------ |
| `WEBUI_PORT`    | dashboard port                                     | `8787`                   |
| `WEBUI_GATE`    | `1` = start the throttle gateway with the panel    | `1`                      |
| `WEBUI_VPN`     | `1` = start the IP rotator with the panel          | `0`                      |
| `WEBUI_PREWARM` | `1` = warm the browser before the first account    | `1`                      |
| `ROUTER_URL`    | the router the panel talks to                      | `http://127.0.0.1:20128` |
| `ROUTER_HOME`   | the router's data dir (its SQLite `db/data.sqlite`)| per-OS default           |
| `GATE_PORT`     | throttle gateway port                              | `20130`                  |
| `GATE_RPM`      | gateway rate, requests/minute (`0` = auto)         | `0`                      |

## Why the gateway

tokenharbor's free tier blocks by **network (IP)**, not by key. One IP firing
too fast starts answering `429 "many free accounts are sharing this network
address"`, and for the next few minutes almost every request fails. The router
does round-robin over keys but knows nothing about the IP's budget.

`gate.py` fixes that: it holds a token bucket at `GATE_RPM`, reads
`Retry-After` / `"next request in N s"` on a `429` and pauses everything, then
retries a few times before answering. Point your client at
`http://127.0.0.1:20130/v1` instead of `...:20128/v1`.

## Files

| File            | Purpose                                                   |
| --------------- | --------------------------------------------------------- |
| `main.py`       | registration flow, key creation, DB, exports              |
| `chromedrv.py`  | Camoufox browser/context factory                          |
| `fastfox.py`    | Camoufox launch helpers and options                       |
| `mailtm.py`     | mail.tm client                                            |
| `vpn.py`        | ExpressVPN CLI wrapper                                    |
| `webui.py`      | dashboard server (stdlib HTTP) + backend jobs             |
| `webui.html`    | dashboard UI (single file, no build step)                 |
| `gate.py`       | throttle/retry gateway in front of the router             |
| `pool.py`       | key-pool strategy + weekly-quota scan                     |
| `usage_watch.py`| per-key usage probe, groups accounts, auto-disables       |
| `rotator.py`    | IP rotation loop driven by the gateway                    |

## Notes

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
