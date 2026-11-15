# tokenharbor-reg

Registers accounts on tokenharbor.ai with disposable email, enables free
models, verifies the email, creates an API key through the dashboard UI and
checks the key with a real chat request. Results go to `tokens.txt` /
`accounts.txt` (verified keys only) and `accounts.db` (all attempts).

Browser automation runs on [Camoufox](https://camoufox.com) (Firefox-based).
Email comes from [mail.tm](https://mail.tm). Each account is registered from a
different ExpressVPN region.

## Requirements

- Python 3.10+
- [ExpressVPN](https://www.expressvpn.com) installed, logged in, with the
  `expressvpnctl` CLI available (default path
  `C:\Program Files\ExpressVPN\expressvpnctl.exe`, override with the
  `EXPRESSVPNCTL` env var)
- Windows / macOS / Linux

```bash
pip install -r requirements.txt
python -m camoufox fetch      # download the browser once
```

## Usage

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

## Files

| File           | Purpose                                              |
| -------------- | ---------------------------------------------------- |
| `main.py`      | registration flow, key creation, DB, exports         |
| `chromedrv.py` | Camoufox browser/context factory                     |
| `fastfox.py`   | Camoufox launch helpers and options                  |
| `mailtm.py`    | mail.tm client                                       |
| `vpn.py`       | ExpressVPN CLI wrapper                               |

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
