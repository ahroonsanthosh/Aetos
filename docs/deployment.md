# Deploying Aetos to a site

Aetos does not terminate TLS and holds no private key. A reverse proxy in front of it does
that, and `--behind-proxy` is how you tell Aetos you have arranged one. Without that flag it
refuses to bind to anything but loopback (ADR 0016).

Read `docs/failure_modes.md` before deploying. It is the honest list of what this build does
not do, and several entries matter more once the system is on a network than they did on a
laptop.

## 1. Create the first account

```bash
.venv/bin/python -m backend.auth.admin add --username alice --role admin
```

On Windows in Git Bash the interpreter is `.venv/Scripts/python.exe` instead. Use the one
inside `.venv` either way: the dependencies are installed there, and the bare `python` on PATH
fails with `ModuleNotFoundError: No module named 'pydantic'` before argparse ever runs — which
reads like a broken install rather than the wrong interpreter. `./start.sh` prints the right
command for your machine when no account exists.

Prompts for the password twice, without echo. There is no default credential and no generated
first-run password: the first account is a deliberate act by someone who already has access to
the machine.

Roles are `viewer` (reads the picture), `operator` (also acknowledges alerts) and `admin` (also
manages accounts). Give people the lowest one that lets them do their job — an observer or an
auditor should hold `viewer`, and the difference shows up in the event log.

Other commands:

```bash
PY=.venv/bin/python          # .venv/Scripts/python.exe on Windows

$PY -m backend.auth.admin list
$PY -m backend.auth.admin passwd  --username alice
$PY -m backend.auth.admin role    --username bob --role viewer
$PY -m backend.auth.admin disable --username bob      # also ends every session it holds
$PY -m backend.auth.admin unlock  --username alice    # clears a lockout early
```

## 2. Run Aetos on loopback

```bash
.venv/bin/python -m backend.main --host 127.0.0.1 --port 8000 --behind-proxy
```

Bind loopback even with a proxy in front. Anything else leaves the port reachable directly,
bypassing whatever the proxy is doing.

`--behind-proxy` does two things: it satisfies the startup guard, and it makes uvicorn trust
`X-Forwarded-Proto` from the proxy — which is what marks the session cookie `Secure`. Do not
pass it unless a proxy really is terminating TLS, because the flag is the only assertion Aetos
has to go on.

## 3. Terminate TLS in front of it

### Caddy

```
aetos.example.org {
    reverse_proxy 127.0.0.1:8000

    # The WebSocket carries the live track picture and must survive a quiet period.
    # Caddy proxies upgrades automatically; the timeout is what needs saying.
    transport http {
        read_timeout 300s
    }
}
```

Caddy obtains and renews a certificate on its own, which is why it is first here — certificate
expiry is a predictable way for a console to go dark.

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name aetos.example.org;

    ssl_certificate     /etc/letsencrypt/live/aetos.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/aetos.example.org/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;

        # Required. Without X-Forwarded-Proto, Aetos cannot tell the request arrived over TLS
        # and will not mark the session cookie Secure.
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # The event stream. Without these two the WebSocket upgrade fails and the picture
        # falls back to nothing — the console loads and then never updates.
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Long enough that a quiet sky does not look like a dead connection.
        proxy_read_timeout 300s;
    }
}

server {
    listen 80;
    server_name aetos.example.org;
    return 301 https://$host$request_uri;
}
```

## 4. Check what the guard refuses

Worth doing once, so the failure modes are familiar before they happen at an awkward moment.
Each of these refuses to start and says why:

```bash
$PY -m backend.main --host 0.0.0.0 --no-auth        # unauthenticated on a network
$PY -m backend.main --host 0.0.0.0                  # no --behind-proxy: cookies in clear
$PY -m backend.main --host 0.0.0.0 --behind-proxy   # before any account exists
```

## 5. Verify after deploying

```bash
# Unauthenticated: liveness only, and no data.
curl https://aetos.example.org/api/v1/system/health
curl -i https://aetos.example.org/api/v1/tracks          # expect 401

# Authenticated.
curl -c jar -X POST https://aetos.example.org/api/v1/auth/login \
     -H 'Content-Type: application/json' \
     -d '{"username":"alice","password":"..."}'
curl -b jar https://aetos.example.org/api/v1/tracks       # expect 200
```

Then open the site in a browser: you should get the sign-in screen, not the console.

The health endpoint is deliberately readable without a session so a monitoring probe works, but
it answers liveness only until you authenticate.

Once signed in, the component named `access_control` is the one to watch. It reads `ok` when
operators authenticate, and `not_connected` with *"NO authentication is configured. Anyone who
can reach this port has full operator access and the complete event log"* when the process is
running with `--no-auth`. Worth alerting on: it is the difference between a guarded console and
an open one, stated by the backend rather than assumed from how it was launched.

## What is still true after all this

- **`data/` holds the event log and the account table.** Back it up, and treat the backup as
  sensitive: it contains password hashes and the complete operational record. It does *not*
  contain usable session tokens — only hashes of them are stored.
- **No password reset flow, no multi-factor.** An administrator resets a password with
  `passwd`, then `disable` and `enable` to end that account's existing sessions.
- **No rate limiting on anything except login.** A reverse proxy is the right place to add it,
  and both examples above are a natural home for that.
- **The failure modes in `docs/failure_modes.md` that are still open** — sensor silence
  (FM-010), backend restart recovery (FM-030), event store unavailability (FM-032), saturation
  (FM-014) — are unaddressed. `tests/chaos/` is empty. None of them is a reason not to deploy,
  but all of them are things to know before an operator relies on the display.
