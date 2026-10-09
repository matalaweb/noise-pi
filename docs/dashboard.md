# Local live dashboard

An optional, read-only page on the Pi that graphs the collector's one-second levels as they are
committed, with candidate events, baseline and trigger lines, and health. It complements the web
app (which polls every 15 s); it is not a replacement for it.

## What it is and is not

* **Read-only by default.** A separate process (`noise-collector dashboard`) reads SQLite through a
  read-only connection and the runtime status files. It cannot change, block, or slow capture,
  storage, or delivery, and it rejects any write method, except the one optional owner control
  below.
* **Never audio.** No route serves recordings, spool chunks, or raw samples (spec section 17: no LAN
  microphone endpoint).
* **Resolution is the instrument's.** One point per UTC second as committed, pushed to the browser
  with Server-Sent Events about 1 to 2 s after the second ends. Long windows (6 h, 24 h) are
  max-preserving downsampled, so short loud events stay visible.
* **Self-contained.** No CDN or external assets (strict CSP), so it works on a LAN without internet.
* **Honest units.** It shows dB(A)/dB(C)/dB only for channels with an absolute scale; otherwise it
  shows dBFS, labeled as not a sound pressure level.

## Enable

```toml
[dashboard]
enabled = true          # the supervisor starts it as an independent child process
bind = "127.0.0.1"      # default: loopback only
port = 8765
```

Open it from your computer through an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 pi@noise-pi.local    # then browse http://127.0.0.1:8765/
```

### LAN access (optional)

Binding a non-loopback address requires a token. The dashboard refuses to start without one:

```bash
sudo -u noise-collector /opt/noise-collector/venv/bin/noise-collector dashboard --new-token /etc/noise-collector/dashboard-token
```

```toml
[dashboard]
enabled = true
bind = "0.0.0.0"
access_token_file = "/etc/noise-collector/dashboard-token"
```

Open `http://noise-pi.local:8765/?token=<token>` once. The token becomes an HttpOnly,
SameSite=Strict cookie and is removed from the address bar. API clients can send
`Authorization: Bearer <token>` instead. Traffic is plain HTTP on your LAN: use it only on a trusted
network, or keep loopback plus SSH.

### LAN access without a token (owner opt-in)

On a trusted private network the token can be switched off explicitly. Anyone who can reach the
Pi can then view the page (still read-only, still no audio):

```toml
[dashboard]
enabled = true
bind = "0.0.0.0"
allow_unauthenticated_lan = true   # leave access_token_file unset
```

### Stop event button (owner control, optional)

```toml
[dashboard]
allow_stop_event = true
```

While an event is open, the page shows **Stop event**. It sends `POST /api/events/<id>/stop`
(header `X-Noise-Collector: stop-event`, which other web pages cannot send cross-site). The
dashboard only drops a request file in the runtime directory; within a second the acquisition
process checks that the id is still the open event and ends it at the current second:

* **Active event:** ended now, no post-roll, recording stopped, flag `ended_by_operator` (the web
  app shows the duration as a lower bound, "stopped manually"), and the baselines are re-learnt at
  the current level (about 2 minutes of warmup).
* **In post-roll:** the detection end is already known and kept; only the post-roll recording is
  cut short.

Requests older than 60 s are ignored. With a token configured, the button needs the same token as
the page.

## Endpoints

| Path | Returns |
|---|---|
| `/` | the page |
| `/api/status` | microphone/capture/clock/detector/upload/storage state, current baselines and thresholds |
| `/api/measurements?seconds=N` | per-second points for the last N seconds (max 7 days) |
| `/api/events?seconds=N` | recent candidate events (latest revision snapshot, upload state) |
| `/api/stream?since=T` | Server-Sent Events: `points` (new seconds after T) and `status` (each second) |

In the optional Docker/Compose mode no port is published, per the deployment rules, so the
dashboard is not reachable. Use the native systemd install if you want it.
