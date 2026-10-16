# ⚽ Matchday Control Center (MCC)

A complete, production-ready, mobile-responsive web application for grassroots football live broadcasting — built for a Linux laptop on the sidelines.

![Python](https://img.shields.io/badge/Python-3.8+-blue?logo=python)
![Flask](https://img.shields.io/badge/Flask-Web_UI-green?logo=flask)
![License](https://img.shields.io/badge/License-MIT-yellow)

## What It Does

Turn any Linux laptop into a **sideline broadcast station**:

1. 📡 **Network Router** — Receives internet via USB-tethered phone, broadcasts a Wi-Fi hotspot for camera phones
2. 🎥 **RTMP Ingest Server** — Accepts up to 3 camera feeds via MediaMTX (RTMP + WebRTC)
3. 🎬 **Live Video Switcher** — Browser-based multi-camera switching with zero-glitch YouTube encoding (no OBS needed)
4. 📊 **Live Scoreboard** — Real-time team names, scores, and match clock burned into the stream

## Architecture

```
Phone (USB Tether) ──→ Laptop (Internet) ──→ Wi-Fi Hotspot (10.42.0.1)
                                                    │
                            ┌───────────────────────┤
                            ▼                       ▼
                     Camera Phones            Laptop Browser
                     stream RTMP to:          http://10.42.0.1:5000
                     rtmp://10.42.0.1:1935/   ┌─────────────────┐
                       ├── /cam1              │  Control Center  │
                       ├── /cam2              │  • Multi-view    │
                       └── /cam3              │  • Switch cams   │
                            │                 │  • Scoreboard    │
                            ▼                 │  • YouTube ctrl  │
                     MediaMTX Server          └─────────────────┘
                       ├── WebRTC preview
                       └── /program ──→ FFmpeg ──→ YouTube Live
                              ▲           │
                              │           └── drawtext overlay
                         Camera Relay         (scoreboard.txt)
                         (seamless switch)
```

## Key Features

| Feature | Details |
|---|---|
| **Zero-glitch switching** | Two-stage FFmpeg pipeline: Stage A (persistent encoder → YouTube) + Stage B (camera relay → `/program`). Switching only restarts Stage B — YouTube never drops. |
| **Audio lock** | Lock commentary audio to Cam 1 while switching video between cameras |
| **Live scoreboard overlay** | Atomic file write → FFmpeg `drawtext reload=1` updates every frame |
| **Mobile-first UI** | Dark-mode dashboard works on laptops and sideline phones |
| **One-tap camera setup** | Camera operators copy RTMP URLs with a single tap (HTTP-safe clipboard) |
| **Sideline checklist** | Step-by-step setup guide persisted in localStorage |
| **Auto-provisioning** | MediaMTX binary auto-downloaded on first run |
| **Graceful degradation** | Missing `ffmpeg`/`nmcli` → clear warnings, app still starts |

## Quick Start

### Prerequisites

- Linux laptop with Wi-Fi adapter
- Python 3.8+
- `ffmpeg` (for streaming)
- `nmcli` / NetworkManager (for hotspot)
- A phone for USB tethering (internet source)
- Camera phones with an RTMP streaming app (e.g., Larix Broadcaster)

### Launch

```bash
git clone https://github.com/bullemer/matchday-control-center.git
cd matchday-control-center
chmod +x start.sh
./start.sh
```

The script will:
1. Create a Python virtual environment
2. Install dependencies (`flask`, `requests`)
3. Launch the server on `http://0.0.0.0:5000`

### On the Pitch

1. Plug your internet phone into the laptop via USB → enable USB tethering
2. Open `http://localhost:5000` on the laptop
3. Click **USB Connect** → **Start Hotspot**
4. Connect camera phones to the `MatchdayLive` Wi-Fi
5. On each camera phone, stream to `rtmp://10.42.0.1:1935/cam1` (or cam2/cam3)
6. Enter your YouTube stream key → **GO LIVE**
7. Use the **CUT TO CAM** buttons to switch angles live!

## Project Structure

```
matchday-streamer/
├── app.py                 # Flask server + 12 REST API endpoints + match clock
├── network_manager.py     # USB tethering, Wi-Fi hotspot, firewall checks
├── media_engine.py        # MediaMTX + seamless 2-stage FFmpeg pipeline
├── templates/
│   └── index.html         # Mobile-first dark-mode broadcast dashboard
├── static/
│   └── scoreboard.txt     # Live overlay text for FFmpeg drawtext
├── requirements.txt       # flask, requests
└── start.sh               # One-command launcher with venv setup
```

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Dashboard UI |
| `GET` | `/api/status` | Combined system status |
| `POST` | `/api/network/usb-connect` | Connect USB tether |
| `POST` | `/api/network/hotspot-start` | Start Wi-Fi hotspot |
| `POST` | `/api/network/hotspot-stop` | Stop hotspot |
| `POST` | `/api/stream/start` | Start YouTube stream |
| `POST` | `/api/stream/stop` | Stop YouTube stream |
| `POST` | `/api/switch/<cam_id>` | Switch camera (cam1/cam2/cam3) |
| `POST` | `/api/audio-source` | Set audio mode |
| `POST` | `/api/scoreboard/update` | Update team names & scores |
| `POST` | `/api/scoreboard/clock` | Control match clock |
| `GET` | `/api/scoreboard` | Get current scoreboard state |

## License

MIT
