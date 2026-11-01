# Matchday Control Center

A Linux dashboard for USB internet tethering, an application-owned Wi-Fi hotspot,
three camera inputs, a live scoreboard, and a supervised encoder that streams to YouTube,
Facebook, Twitch and any other RTMP server at the same time, records and replays.

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
The application never stores a stream key on disk or intentionally logs it.

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
   **Mute** (Main tab audio panel, Match mode, or key M in Match mode) silences every
   source on the broadcast at once — for a stoppage, an injury or a private word on the
   bench. Picture, scoreboard and the streaming connections keep running; unmuting restores
   the previous mix. Audio-source and level changes made while muted take effect on
   unmute. Mute lasts until it is switched off, including across camera cuts, and resets
   to off when the app restarts.
5. Switch on the platforms (YouTube, Facebook, Twitch, Custom RTMP), enter their stream
   keys, choose 1080p, 720p or 480p, and click GO LIVE. See *Streaming destinations*
   below. Each platform needs its own full upload (720p about 4.1 Mbps each); the panel
   shows the total. Use 720p or fewer platforms on a weak mobile connection. The app
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
7. **Recording.** With "Also record to this laptop" on (the default), GO LIVE also
   records; **Record only** records without YouTube (no internet needed), and **Start
   recording** adds a recording to a running stream. Mark moments with **⭐ Mark
   highlight** (Main tab, Match mode, or key H in Match mode); a goal (+1) is marked
   automatically and withdrawn again by a −1 within two minutes. See *Recordings and
   highlights* below.
8. **Instant replay** (needs the recording on): **⏪ Replay** plays the last 6–20 s
   (choose in the Multiview panel), **🐢 Slow-mo** the same at half speed; in Match mode
   ⏪ / 🐢 or keys R / S. Any highlight of the running match can be replayed from the
   🎬 Recordings tab. See *Instant replay* below.
9. STOP ALL ends the broadcast on every platform (⏹ Stop on a platform's row ends only
   that one); a running recording continues until **Stop
   recording**. Stopping whichever runs last also stops the encoder and camera relay. Ctrl+C / SIGTERM also cleans up the
   managed media processes. The hotspot remains under its explicit start/stop controls.

## Media pipeline and recovery

Camera phones publish RTMP to `cam1`, `cam2`, or `cam3`. A relay normalizes the
selected camera to H.264 at the stream resolution and stereo 48 kHz AAC on `program`. MediaMTX 1.21.2's
always-available program path supplies fallback video and silence when that publisher
is absent. A separate encoder reads the continuous program over loopback RTSP,
adds the scoreboard, and publishes the finished picture locally; each streaming
destination and the recorder copy it from there.

## Streaming destinations

| Platform | Default server (editable under **Server**) | Key |
|---|---|---|
| YouTube | `rtmps://a.rtmps.youtube.com:443/live2` | YouTube Studio stream key |
| Facebook | `rtmps://live-api-s.facebook.com:443/rtmp` | Live Producer stream key (may contain `?`, `=`, `&`) |
| Twitch | `rtmps://ingest.global-contribute.live-video.net/app` (Twitch "Default"; regional servers from ingest.twitch.tv) | Twitch stream key |
| Custom RTMP | none: enter the `rtmp://` or `rtmps://` server | optional |

Every platform gets its own copy of the same stream (no extra encoding, so no extra CPU),
its own status (LIVE / CONNECTING / RECONNECTING / FAILED with the reason) and its own
reconnect timing: one platform failing never interrupts the others or the recording.
The header shows ON AIR while any platform is live. Platforms can be added (▶ Start on
the row) or stopped (⏹ Stop) while live. On/off choices and servers are remembered per
device; stream keys are never stored.

The stream meets the common requirements of all three platforms: H.264 up to 1080p30,
AAC 48 kHz 128 kbps, a keyframe every 2 s, constant bitrate. 720p uses 4 Mbps, the top
of Facebook's and YouTube's recommended 720p range. With Facebook, depending on how the
key was created, the broadcast may only become public after **Go live** in Live Producer.
Upload is the limit: three platforms at 720p need about 12.4 Mbps of steady upload.

The final encoder uses two filter threads, six H.264 encoder threads, and explicit
YUV420 overlay composition to reduce CPU overhead. Resolution, bitrate, the
`veryfast` preset and the 30 fps target are unchanged. Short synthetic 1080p PiP
benchmarks on an Intel Core Ultra 7 155H measured about 35% less CPU across the
sampled media processes; actual savings depend on footage and hardware. This does
not enable hardware encoding or remove the separate relay stage.

STOP cancels an in-flight GO LIVE operation, including camera readiness checks; a cancelled
start cannot launch an encoder afterward. Duplicate starts are rejected.
Audio changes are serialized with cuts so Follow camera applies to the newly selected
camera, and meter highlights report the actual unmuted relay inputs. Compact meters
show all active mixed inputs rather than guessing an input.

The program preview is before the scoreboard overlay; its audio meter measures the
local program, not YouTube reception. Clipboard denial offers a manual copy fallback.

A cut probes the new source and starts the new relay while the old one stays on air
(relays alternate between two local control ports, so both can run briefly). Probing
and waiting for frames never block STOP, status, or other controls; a newer cut
supersedes one still in progress, and STOP aborts it. The handover itself is not
frame-accurate, but the fallback picture is no longer shown during a cut.
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
check YouTube Studio, Live Producer or the Twitch dashboard to confirm platform-side reception.

MediaMTX's control API and RTSP transport are loopback-only. Program publishing is
loopback-only; camera publishing and preview reading are available on the LAN.
Camera paths reject replacement publishers. Publishing to the output encoder's `final` path is loopback-only too. Ports: TCP 5000 (control), 1935 (camera
RTMP), 8888 (HLS), 8889 (WebRTC signaling), and UDP 8189 (WebRTC media).
Firewall diagnostics distinguish listening sockets from verified remote reachability;
they do not claim that a matching rule or a listening socket guarantees access.

MediaMTX is downloaded for the host architecture, checked against the release's
SHA-256 manifest, and extracted as a single regular executable. An incompatible
existing binary is rejected; move it aside to provision the supported version.
Generated configuration, certificates, credentials, and match state are under `data/`.
Never commit this directory. MediaMTX is not automatically upgraded on launch.

## Recordings and highlights

The recording is the finished broadcast picture, scoreboard included, at the selected
quality (about 2 GB per hour at 720p, 2.7 GB at 1080p). Files are under `recordings/`
next to the app; set `MCC_RECORDINGS_DIR` to use another folder or drive.
Recording refuses to start with less than 3 GB free and stops (and saves) by itself
below 1 GB.

Pipeline: the output encoder publishes its finished picture to the local `final` path.
The streaming connections and the recorder only copy that stream (no second encode), so a
platform outage reconnects that platform alone and never interrupts the recording.
The recorder writes crash-safe MPEG-TS parts; if the encoder or recorder restarts, the
recording continues in a new part and the gap is short. Stopping joins the parts into
one MP4 (`<date>_<Home>-vs-<Away>.mp4`). If the app stops mid-recording, the parts are
joined automatically at the next start, or with **Finish saving**.

The **🎬 Recordings** tab lists every recording: download or watch the MP4, delete it,
and work with its highlights. Each highlight shows its time in the recording, the match
clock and the score; change its type (Goal, Chance, Save, Card, Foul, Other) or remove
it. **🎬 Clip** cuts 20 s before to 10 s after the mark without re-encoding (the cut
starts on the nearest keyframe, at most 2 s earlier); clips also work while the match is
still being recorded. **Build highlight reel** joins all clips in match order into
`highlights.mp4`. **YouTube chapters** gives a ready-to-paste chapter list for the
description of the uploaded recording (it matches this file, not YouTube's own
live-stream archive).

## Instant replay and CAM REPLAY

**CAM REPLAY** is a fourth source next to the cameras (🎬 CAM REPLAY cut button; REPLAY
and key 4 in Match mode). In the Multiview panel, choose the moment on its timeline: drag
the slider over the running recording, step with ◀ 10 s · ◀ 1 s · 1 s ▶ · 10 s ▶, jump
with ⏭ Near live, or tap a ⭐ highlight mark (5 s before it). A still preview shows the
chosen moment. **▶ Play** or **🐢 Play slow** puts CAM REPLAY on air from there; it
plays on (holding its last frame once it reaches what has been recorded) until you cut
to a camera. The quick **⏪ Replay / 🐢 Slow-mo** buttons (keys R / S) play the last
6–20 s and return to live by themselves.

A replay plays part of the running recording into the program: the picture as it was
on air (layout and the scoreboard of that moment included) with a red **REPLAY** label,
while the live commentary mix (and mute) continues over it. The live scoreboard is
hidden during the replay so the two never overlap. The replay starts 1.5 s behind the
live recording edge; a highlight replay runs from 8 s before to 4 s after the mark.
When a quick replay's clip ends, the program returns to the live camera by itself: the
live relay starts as long before the end as a cut takes on this laptop (measured), and
holds the replay's last frame if it is not ready yet. **⏹ Back to live**, any camera cut, a layout change or
STOP ends a replay at once. Camera tallies do not show ON AIR during a replay.
Replays show the angle that was on air; other angles would need per-camera recording.

For cuts and replays a new relay starts while the current one stays on air, and
MediaMTX hands the program over the moment the new relay publishes. A cut that fails
leaves the current picture on air. At the handover itself MediaMTX shows its offline
picture for 1–2 frames (33–66 ms); the app replaces MediaMTX's "STREAM IS OFFLINE"
picture with a plain dark "Back in a moment" card, which is also what viewers see while
no camera at all is available. (A switcher inside the encoder that would avoid even
those frames was tried; it was not reliable under load and is not used.)

Highlight marks, replays and the CAM REPLAY timeline use what has actually been
written to the recording, not the wall clock, so they stay exact even if the stream
falls behind real time (for example while no camera is available).

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
Layout changes replace the relay while keeping the output encoder alive; the current
picture stays on air until the new layout is ready. Layout selections are kept for the current app session.

## Scoreboard sources

“Scoreboard on video” in Match mode or Setup hides/shows the selected scoreboard
without stopping the stream. Scores and clocks keep running while hidden.
In Setup → Scoreboard & Match Clock, choose **Built-in scoreboard** or
**Score67 OBS overlay**, then click **Apply scoreboard source**.

For Score67, paste its “Copy OBS overlay link”, for example
`https://score67.de/s/your-board/overlay`. Keep the `k` access key on private links.
The viewer link is also accepted and converted to an overlay link. Position, view,
language and delay options are preserved; `preview=1` is removed for transparency.
Control the external scores, clock and design in Score67. The local match controls
are disabled while that source is selected. The master visibility switch works
with both sources, and the choice/link are saved locally with the match.

A headless Chromium browser renders Score67 at its OBS viewport (1920 × 1080).
Transparent frames refresh about twice per second and are scaled onto the final
stream, including PiP/split layouts. This suits a scoreboard clock; it is not a
full-frame-rate browser animation source. The authenticated preview in Setup shows
the rendered overlay; the camera/program previews remain before the scoreboard.
Chromium closes when Score67 is hidden or deselected. A failed page is cleared and
retried automatically; lost state updates show a connection warning while Score67
keeps the last received scores. Score67's own hidden/no-match state also produces
an empty overlay.

`start.sh` installs the pinned Playwright runtime and Chromium when dependencies
change. Manual setup: `venv/bin/python -m playwright install chromium`.
If Chromium reports missing system libraries, install its platform prerequisites.
Built-in scoring and video streaming still work if the browser cannot start.
The private link is stored in `data/match.json` (mode 600); do not share that file.

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
| POST | `/api/stream/start` | `destinations`: `{youtube\|facebook\|twitch\|custom: {key, server?}}` (or legacy `stream_key` = YouTube), optional `profile`: `1080p`, `720p` or `480p`, `record`: also record. While live, adds platforms not yet running |
| POST | `/api/stream/stop` | Stop all platforms, or one with `destination` (a recording continues) |
| POST | `/api/recording/start` | Record to this laptop, optional `profile`; adds to a running stream |
| POST | `/api/recording/stop` | Stop recording and save the MP4 (streaming continues) |
| POST | `/api/replay` | Instant replay: `seconds` (3–30) and `speed` (1 or 0.5), or `marker` (highlight id); CAM REPLAY: `start` (timeline seconds), `speed`, `hold: true` |
| GET | `/api/replay/timeline` | CAM REPLAY timeline: replayable length (`edge`) and highlight marks |
| GET | `/api/replay/frame?t=` | JPEG still of the recording at a timeline position |
| POST | `/api/replay/stop` | Back to live now |
| POST | `/api/markers` | Mark a highlight now on the running recording, optional `label` |
| GET | `/api/recordings` | Recordings with highlights, chapters and free disk space |
| POST | `/api/recordings/<id>/markers/<marker>` | `label`, or `delete: true` |
| POST | `/api/recordings/<id>/markers/<marker>/clip` | Cut the highlight clip |
| POST | `/api/recordings/<id>/reel` | Join all highlight clips into `highlights.mp4` |
| POST | `/api/recordings/<id>/finalize` | Join an unfinished recording's parts |
| POST | `/api/recordings/<id>/delete` | Delete a recording with its clips |
| GET | `/recordings/<id>/<file>` | Watch a recorded file; `?download=1` to download |
| POST | `/api/switch/<cam_id>` | Select cam1/cam2/cam3 |
| POST | `/api/layout` | `mode`: single/pip/split, `main`, `secondary`, `position`: top-left/top-right/bottom-left/bottom-right |
| POST | `/api/audio/mute` | `muted`: true/false — master mute of every source, mixer kept |
| POST | `/api/audio-source` | `mode`: `cam1`, `active`, or `usb:<device id>` |
| GET | `/api/audio/devices` | Audio devices |
| POST | `/api/scoreboard/update` | Names/scores, atomic `team` + `delta`, `visible`, `source` (builtin/score67), `overlay_url` |
| POST | `/api/scoreboard/clock` | `action`: start/pause/reset/set_half; `half`: 1 or 2 |
| GET | `/api/scoreboard` | Match state and overlay render status |
| GET | `/api/scoreboard/preview` | Authenticated transparent Score67 PNG |

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
The simulated YouTube destination is another local MediaMTX path and a local ffmpeg RTMP
server stands in for a second platform: it never contacts a platform or changes networking. It checks startup, repeated cuts with the same output
encoder PID, active-camera disconnect/fallback, recovery to another camera, and automatic restart after encoder failure.
Browser tests run a separate fixture on port 25000 with mocked hardware.

A real venue rehearsal is still required for physical USB tethering, AP driver/Polkit
behavior, microphone capture, phone WebRTC compatibility, and reception on YouTube,
Facebook and Twitch (keys, upload bandwidth).
Local recording, audio meters, QR onboarding, and adaptive bandwidth control remain
possible future features; they are not implemented by this reliability repair.

Optional browser/encoder integration check: `venv/bin/python tests/overlay_smoke.py`.
Pass a local `score67-de/backend` path to exercise its actual OBS template and scripts.
