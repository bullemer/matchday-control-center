# Matchday Control Center

A Linux dashboard for USB internet tethering, an application-owned Wi-Fi hotspot,
three camera inputs, a live scoreboard, and a supervised YouTube encoder.

## Requirements and first launch

- Python **3.10+** on Linux, x86-64 or ARM64.
- `ffmpeg` and `ffprobe`, with libx264, AAC, drawtext, RTSP, and RTMPS support.
- NetworkManager / `nmcli`, an AP-capable Wi-Fi adapter, `iproute2`, and `ping`.
- Optional: `pactl` / PipeWire-Pulse or ALSA `arecord` for microphone discovery.
- Internet is needed for the initial Python dependency and MediaMTX installation.
  The checked-in dashboard CSS and subsequent launches work without a CDN.

On Ubuntu, install the system prerequisites:

```bash
sudo apt install python3-venv ffmpeg network-manager iproute2 iputils-ping alsa-utils pulseaudio-utils
./start.sh
```

Open `http://localhost:5000`. Log in as **operator**. On first launch, a random
password is saved in `data/operator-password` with owner-only permissions. Read it
locally, or supply your own password through `MCC_OPERATOR_PASSWORD` before launch.
To change the login, edit `data/operator-user` and `data/operator-password` (one line
each) and restart, or set `MCC_OPERATOR_USER` / `MCC_OPERATOR_PASSWORD`.
Keep the password private; camera operators only need their camera publishing URL.
The application never stores a YouTube stream key on disk or intentionally logs it.

HTTP Basic authentication is intended for a trusted local network. Use a TLS
reverse proxy before exposing the control panel beyond it. To restrict the control
panel to the laptop, set `MCC_BIND=127.0.0.1`. Do not run the entire app as root;
NetworkManager operations require the user's normal NetworkManager/Polkit permission.

The launcher creates a venv and installs pinned Python dependencies when needed.
It uses Waitress with a single Python process. Do not deploy multiple independent
workers: the hardware process manager and live match state are process-local.

## Match-day workflow

1. Connect the phone by USB and enable internet tethering. USB devices are detected
   through sysfs. USB ethernet adapters may also appear; confirm the selected device.
2. Start the hotspot. The app records the UUID of its own NetworkManager AP profile.
   Stop Hotspot only stops this profile; it never disconnects arbitrary Wi-Fi.
   Activating AP mode on an adapter currently used for Wi-Fi replaces that connection.
3. Connect camera phones to the hotspot and use the camera URLs shown in the UI.
   URLs use the detected hotspot address; previews use the host serving the dashboard.
   Use H.264 video and AAC audio for RTMP ingest. Preview audio support depends on
   the browser/codec; verify commentary at the broadcast output.
4. Select a camera and choose Cam 1 commentary, Follow camera, or an available mic.
   A silent camera in Follow mode receives generated silence. If the chosen commentary
   source (Cam 1 or the USB mic) is missing or drops out, the picture stays on the
   selected camera with that camera's own audio (or silence) and the dashboard shows an
   audio warning; the chosen source is restored automatically when it returns.
5. Enter the YouTube stream key, choose 1080p, 720p or 480p, and click GO LIVE.
   1080p needs about 7 Mbps of reliable upload; use 720p on a weak mobile connection. The app
   validates a camera relay before launching the output encoder. “Starting” is not
   “Live”: frame progress is required for live status.
6. Tap **Match mode** in the header for a one-screen sideline view: program preview
   (can be hidden), cut buttons with tally, audio source, score, clock and stream
   status. Keys 1–3 cut cameras on a laptop keyboard. The mode is remembered per device,
   and hidden previews stop playing to save CPU, battery and Wi-Fi.
   Update scores and clock. Use **Scoreboard on video** in Setup or Match mode to hide
   or show the overlay without interrupting the stream. Scores and the clock continue
   while hidden, and the visibility setting is saved with the match.
   The overlay refreshes every half-second. Match state is
   saved after changes and every five seconds, and restored with the clock **paused**.
7. STOP LIVE ends both encoder and camera relay. Ctrl+C / SIGTERM also cleans up the
   managed media processes. The hotspot remains under its explicit start/stop controls.

## Media pipeline and recovery

Camera phones publish RTMP to `cam1`, `cam2`, or `cam3`. A relay normalizes the
selected camera to H.264 at the stream resolution and stereo 48 kHz AAC on `program`. MediaMTX 1.21.2's
always-available program path supplies fallback video and silence when that publisher
is absent. A separate encoder reads the continuous program over loopback RTSP,
adds the scoreboard, and publishes RTMPS to YouTube.

STOP cancels an in-flight GO LIVE operation, including camera readiness checks; a cancelled
start cannot launch an encoder afterward. Duplicate starts are rejected.
Audio changes are serialized with cuts so Follow camera applies to the newly selected
camera, and meter highlights report the actual unmuted relay inputs. Compact meters
show all active mixed inputs rather than guessing an input.

The program preview is before the scoreboard overlay; its audio meter measures the
local program, not YouTube reception. Clipboard denial offers a manual copy fallback.

A cut probes the new source before stopping the old relay. Probing and waiting for
frames never block STOP, status, or other controls; a newer cut supersedes one still in
progress, and STOP aborts it. During relay replacement,
the fallback may be visible: this is **not a promise of frame-perfect switching**.
An invalid source leaves the existing relay intact. A failed replacement preserves
the previous selection for recovery. Audio changes also restart the relay.

Media processes run under a small watchdog, so they exit if the control app dies
(even with SIGKILL) instead of continuing to stream. MediaMTX is restarted
automatically if it exits, whether or not the stream is live.
A separate encoder watchdog checks output every half-second independently of slower
camera recovery and microphone probes. The supervisor monitors advancing frames, reports stalled output, and retries failed
encoders with bounded backoff. Active-camera loss leaves program fallback available;
the selected source is retried when it returns. The dashboard reports fallback and
connection loss explicitly. “Live” indicates the local encoder is producing output;
check YouTube Studio to confirm platform-side reception and audience availability.

MediaMTX's control API and RTSP transport are loopback-only. Program publishing is
loopback-only; camera publishing and preview reading are available on the LAN.
Camera paths reject replacement publishers. Ports: TCP 5000 (control), 1935 (camera
RTMP), 8888 (HLS), 8889 (WebRTC signaling), and UDP 8189 (WebRTC media).
Firewall diagnostics distinguish listening sockets from verified remote reachability;
they do not claim that a matching rule or a listening socket guarantees access.

MediaMTX is downloaded for the host architecture, checked against the release's
SHA-256 manifest, and extracted as a single regular executable. An incompatible
existing binary is rejected; move it aside to provision the supported version.
Generated configuration, certificates, credentials, and match state are under `data/`.
Never commit this directory. MediaMTX is not automatically upgraded on launch.

## Video layouts

Use **Layout** above the program preview in Match mode, or beside Multiview in Setup.
Choose **Full-screen**, **Picture-in-picture**, or **Side-by-side (50 / 50)**, select
Main camera and Second camera, and apply. PiP uses half the output width and height
(one-quarter of its area) in a selectable corner. Side-by-side puts the main camera
on the left and the second on the right; both pictures fit without stretching or cropping.
Audio remains under the existing audio controls; Follow camera follows the main camera.
The scoreboard is applied over the composed output, so top corners are preferable for PiP.

If the second camera disappears, recovery rebuilds a main-only view and restores the
chosen composition when it returns. Main-camera loss uses the existing program fallback.
Layout changes replace the relay while keeping the YouTube encoder alive; fallback may
briefly appear during replacement. Layout selections are kept for the current app session.

## API

All routes require the operator login. POST requests require JSON objects and reject
cross-origin browser requests. Successful actions return `{ "ok": true, "message": ... }`;
failed actions use an error HTTP status and `{ "ok": false, ... }`.

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/status` | Nested `network`, `cameras`, `mediamtx`, `stream`, `scoreboard`, audio devices, firewall diagnostics |
| POST | `/api/network/usb-connect` | Connect detected USB ethernet |
| POST | `/api/network/hotspot-start` | Start/update owned hotspot with `ssid`, `password` |
| POST | `/api/network/hotspot-stop` | Stop owned hotspot only |
| POST | `/api/stream/start` | `stream_key`, optional `profile`: `1080p`, `720p` or `480p` |
| POST | `/api/stream/stop` | Stop streaming |
| POST | `/api/switch/<cam_id>` | Select cam1/cam2/cam3 |
| POST | `/api/layout` | `mode`: single/pip/split, `main`, `secondary`, `position`: top-left/top-right/bottom-left/bottom-right |
| POST | `/api/audio-source` | `mode`: `cam1`, `active`, or `usb:<device id>` |
| GET | `/api/audio/devices` | Audio devices |
| POST | `/api/scoreboard/update` | Names/scores, or atomic `team` + `delta` adjustment |
| POST | `/api/scoreboard/clock` | `action`: start/pause/reset/set_half; `half`: 1 or 2 |
| GET | `/api/scoreboard` | Match state |

Network/audio/firewall diagnostics are collected in a shared background worker;
opening additional dashboards does not start additional probe subprocesses.

## Development and validation

```bash
venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
npm ci
npm run build:css
npx playwright install chromium
npx playwright test
PATH="$PATH" venv/bin/python tests/media_smoke.py
```

The media smoke test requires a provisioned `mediamtx` binary plus ffmpeg/ffprobe.
It uses synthetic cameras and private loopback test ports (21935, 28554, 29997).
The simulated YouTube destination is another local MediaMTX path: it never contacts
YouTube or changes networking. It checks startup, repeated cuts with the same output
encoder PID, active-camera disconnect/fallback, recovery to another camera, and automatic restart after encoder failure.
Browser tests run a separate fixture on port 25000 with mocked hardware.

A real venue rehearsal is still required for physical USB tethering, AP driver/Polkit
behavior, microphone capture, phone WebRTC compatibility, and YouTube reception.
Local recording, audio meters, QR onboarding, and adaptive bandwidth control remain
possible future features; they are not implemented by this reliability repair.
