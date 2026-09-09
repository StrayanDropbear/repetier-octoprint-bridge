# repetier-octoprint-bridge

Lets you send prints to [Repetier-Server](https://www.repetier-server.com/) from
apps that only support OctoPrint.

Prusa's EasyPrint, Cura and others can send a sliced file straight to a printer, but
Repetier isn't in their list of options. This sits in the middle and translates:

```
EasyPrint / Cura ──► repetier-octoprint-bridge ──► Repetier-Server ──► printer
```

Point the app at this instead and it behaves as if it were talking to OctoPrint.
Uploads, starting prints, queueing and live temperatures all work.

## Setup

You need two things from Repetier first: its address (e.g. `http://192.168.0.50:3344`)
and an API key, from **Global settings → API keys**.

Then get your printer's slug — the internal name, which often isn't the display name:

```bash
curl "http://192.168.0.50:3344/printer/api?a=listPrinter&apikey=YOUR_KEY"
```

### Run with Docker

```bash
docker run -d --name repetier-octoprint-bridge \
  --restart unless-stopped \
  -p 5000:5000 \
  -e REPETIER_URL="http://192.168.0.50:3344" \
  -e REPETIER_APIKEY="your-repetier-api-key" \
  -e BRIDGE_KEYMAP='{"token-a":"Printer_One"}' \
  ghcr.io/strayandropbear/repetier-octoprint-bridge:latest
```

To build it yourself instead of pulling:

```bash
git clone https://github.com/StrayanDropbear/repetier-octoprint-bridge.git
cd repetier-octoprint-bridge
docker build -t repetier-octoprint-bridge .
```

Then use `repetier-octoprint-bridge` in place of the `ghcr.io/...` line above.

### Run with Docker Compose

```bash
git clone https://github.com/StrayanDropbear/repetier-octoprint-bridge.git
cd repetier-octoprint-bridge
cp .env.example .env      # edit this
docker compose up -d --build
```

### Run without Docker

Python 3.10 or newer, which the pinned dependencies require. Debian 11 and Ubuntu
20.04 ship older Python — use Docker there, or install a newer Python.

On Debian/Ubuntu the venv module is a separate package:

```bash
sudo apt install python3 python3-venv
```

(Fedora: `sudo dnf install python3`. Arch: `sudo pacman -S python`.)

```bash
git clone https://github.com/StrayanDropbear/repetier-octoprint-bridge.git
cd repetier-octoprint-bridge

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export REPETIER_URL=http://192.168.0.50:3344
export REPETIER_APIKEY=your-repetier-api-key
export BRIDGE_KEYMAP='{"token-a":"Printer_One"}'

uvicorn repetier_octoprint_bridge:app --host 0.0.0.0 --port 5000
```

### Running as a service

To keep it going after you log out, copy [`repetier-octoprint-bridge.service`](repetier-octoprint-bridge.service) to
`/etc/systemd/system/` and adjust the paths:

```ini
[Unit]
Description=repetier-octoprint-bridge
After=network-online.target

[Service]
User=repetier
WorkingDirectory=/opt/repetier-octoprint-bridge
EnvironmentFile=/opt/repetier-octoprint-bridge/.env
ExecStart=/opt/repetier-octoprint-bridge/.venv/bin/uvicorn repetier_octoprint_bridge:app --host 0.0.0.0 --port 5000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now repetier-octoprint-bridge
journalctl -u repetier-octoprint-bridge -f
```

## The .env file

Both Docker Compose and the systemd unit read settings from a file called `.env`.
The repo ships an example you copy and edit:

```bash
cp .env.example .env
nano .env
```

Copying rather than renaming leaves `.env.example` in place as a reference. `.env` is
listed in `.gitignore`, so your API key won't get committed if you fork the repo.

Each line is `NAME=value`, one per line. A few rules that trip people up:

```bash
REPETIER_URL=http://192.168.0.50:3344     # no spaces around the =
BRIDGE_KEYMAP={"token-a":"Printer_One"}   # no quotes around the whole value
# lines starting with # are ignored
```

After editing, restart to pick up changes:

```bash
sudo systemctl restart repetier-octoprint-bridge   # systemd
docker compose up -d                               # compose
```

## Settings

All configuration is environment variables.

| Variable | Default | What it does |
| --- | --- | --- |
| `REPETIER_URL` | `http://192.168.0.50:3344` | Address of Repetier-Server |
| `REPETIER_APIKEY` | — | Your Repetier API key |
| `BRIDGE_KEYMAP` | `{}` | Maps slicer token → printer slug |
| `REPETIER_SLUG` | — | Printer to use if the token isn't in the map |
| `BRIDGE_NAME_TEMPLATE` | `{name}_{printer}_{material}_{nozzle}mm_{time}` | How to rename uploads; empty = don't rename |
| `BRIDGE_NAME_CASE` | `lower` | `lower`, `upper`, `keep`, `lower-all`, `upper-all` |
| `BRIDGE_PORT` | `5000` | Port to listen on |

## Connect your slicer

In EasyPrint (or Cura, etc.) choose **OctoPrint** as the connection type, then:

- **Host / URL:** `http://<machine-running-the-bridge>:5000`
- **API token:** `token-a`

Check it works before slicing anything:

```bash
curl -H "x-api-key: token-a" http://localhost:5000/api/printer
```

Real temperatures in the output means everything is connected.

## About that token

Your slicer gives you one API token field and no way to pick a printer, so the token
doubles as the printer selector. **You invent the tokens** — they can be anything.
`BRIDGE_KEYMAP` maps each one to a real Repetier printer:

```
BRIDGE_KEYMAP={"token-a":"Printer_One","token-b":"Printer_Two"}
```

Set up one printer in your slicer per printer, all pointing at the same address, each
with its own token. Your actual Repetier API key stays on the server; the slicer
never sees it.

## File renaming

Some apps upload using just the model name. The bridge reads the G-code's comments and
builds a better filename:

```
whistle.gcode  ->  whistle_MK3S_PLA_0.4mm_1h23m.gcode
```

Available: `{name}` `{printer}` `{material}` `{nozzle}` `{layer}` `{time}` `{weight}`
`{slug}` `{date}`. Anything the G-code doesn't mention is left out. Works with
PrusaSlicer, SuperSlicer, Orca and Cura. The print time is the slicer's estimate,
not a measurement.

## If something doesn't work

Check the logs first — every request is logged:

```bash
docker logs -f repetier-octoprint-bridge
```

**Nothing in the log when the slicer connects.** With browser-based apps like
EasyPrint, the browser may be blocking a plain-HTTP request from an HTTPS page. Check
the browser console (F12) for a mixed-content error; the fix is a reverse proxy with a
certificate.

**Printer offline, temperatures zero.** Can't reach Repetier — look for
`listPrinter failed`. In Docker, `127.0.0.1` is the container itself, so `REPETIER_URL`
needs a LAN address even if Repetier is on the same machine.

**502 on upload.** Wrong API key or printer slug. The error includes Repetier's reply.

**415 on upload.** Binary G-code (`.bgcode`), which Repetier can't read. Turn it off
in your print profile.

**404 on something.** An endpoint that isn't implemented yet. The log shows it as
`UNHANDLED` — open an issue and paste that line.

## Licence

MIT — see [LICENSE](LICENSE).

Not affiliated with Repetier, Prusa Research or the OctoPrint project.
