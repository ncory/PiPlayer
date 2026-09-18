# PiPlayer

Fullscreen playlist player for the Raspberry Pi, controlled over the network.

- Boots straight into playback of a **default playlist**, fullscreen on HDMI, with no desktop.
- **Playlists** of videos and still images. Images have per-item display durations. Each playlist can **loop**, **stop**, **hold** the last frame, or **go to another playlist** when it ends.
- **Transitions** between items and playlists: **cut**, **dissolve**, or **dip through a color**. You can set them per playlist, per item, per "go to" link, or per cue.
- **Per-item position offsets** (X/Y). Adjust them live: the ⌖ button on a playlist item puts it on screen in loop-item mode. Drag it in the live preview, nudge it with the arrow keys, or pause and fine-tune.
- **Web UI** for playlists, media uploads, settings, transport controls (including loop-item) and a live preview of the output.
- **REST + WebSocket API** for show controllers and scripts. See [docs/API.md](docs/API.md). The same document is shown on the UI's API tab.
- Output follows the display's preferred HDMI mode. Video is composited at up to 1080p (30 fps by default) and scaled to fit. Rotation is supported for portrait screens.
- No authentication. Meant for a trusted LAN.

## Hardware

| | Pi 3 Model B / B+ | Pi 4 |
|---|---|---|
| HDMI | full-size | micro-HDMI ×2 |
| Hardware video decode | H.264 up to 1080p | H.264 up to 1080p, HEVC up to 4K |
| Recommended content | H.264, ≤1920×1080, ≤30 fps | H.264 or HEVC, 1080p |

The Pi 3 has 1 GB of RAM (there's no 2 GB Pi 3), which is plenty for this. Start with the Pi 3. The software is identical on both, so switching to a Pi 4 later is just moving the SD card.

A dissolve decodes two videos at once for its duration. The Pi 3 decoder handles two 1080p30 H.264 streams, but that's the part to watch when testing on real hardware.

## Preparing the Pi

Use **Raspberry Pi OS Lite (64-bit), Trixie**. You don't need the desktop: PiPlayer drives the HDMI output directly through DRM/KMS and never uses X11 or Wayland. On the full desktop image, the compositor would own the display, so PiPlayer would have to run inside a desktop session. That costs RAM and GPU time the Pi 3 can't spare, and adds boot time and moving parts.

1. In **Raspberry Pi Imager**, choose *Raspberry Pi OS (other) → Raspberry Pi OS Lite (64-bit)*. In the OS customization settings:
   - hostname: `piplayer` (the UI will be at `http://piplayer.local/`)
   - a username and password
   - enable SSH (public-key auth recommended) and add your key
   - Wi-Fi if needed (wired Ethernet is recommended for uploading large videos)
2. Boot it with the HDMI display connected, then SSH in.
3. Install:
   ```bash
   sudo apt install -y git
   git clone https://github.com/ncory/PiPlayer.git
   cd PiPlayer
   sudo ./scripts/install.sh
   sudo reboot
   ```
   The installer adds the GStreamer/Mesa/Python packages. It then creates a `piplayer` service user, installs the app to `/opt/piplayer` with data in `/var/lib/piplayer`, and enables the `piplayer` systemd service on port 80. It also hides boot messages and the tty1 login prompt; pass `--no-quiet-boot` to keep them.
   Since the repo is private, clone it over SSH (with a deploy key) or with a GitHub token.
4. Open `http://piplayer.local/`, upload media, and build the `default` playlist.

To update, run `git pull && sudo ./scripts/install.sh` in the checkout. Playlists, settings and media are kept.

To check the hardware (display, GL, decoders, audio), run `sudo ./scripts/diag.sh /var/lib/piplayer/media/some-video.mp4`.

## Preparing media

The Pi 3 only plays H.264 smoothly. To convert anything else, run this on your Mac/PC:

```bash
ffmpeg -i input.mov -c:v libx264 -profile:v high -level 4.1 -pix_fmt yuv420p \
  -vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30" \
  -preset slow -crf 20 -g 60 -movflags +faststart -c:a aac -b:a 192k -ar 48000 output.mp4
```

The Media tab warns about files the hardware can't decode well. Images (JPEG/PNG/etc., any size) are rendered once at upload or first use to exactly the output resolution, with letterboxing baked in. Big photos are fine.

## Display modes

PiPlayer uses whatever mode the display reports as preferred, which covers most monitors and TVs. To force a mode, or to have HDMI come up even when the display is off or connected later, add a `video=` option to the end of the single line in `/boot/firmware/cmdline.txt`, for example:

```
video=HDMI-A-1:1920x1080@60D
```

The trailing `D` forces the output on. Settings → *Compositing resolution* and *Frame rate* control the internal render size and rate (30 fps at 1080p is recommended on a Pi 3).

If the display isn't connected at boot, PiPlayer retries the output every 30 s. It resumes the default playlist once a display appears.

## Development (any machine)

```bash
python3 -m venv .venv && .venv/bin/pip install aiohttp pillow pytest pytest-asyncio
.venv/bin/python -m piplayer --backend sim --data dev-data      # http://localhost:8080
.venv/bin/python -m pytest
```

The `sim` backend models the compositor's timing, layers, fades and pauses exactly. It renders the preview image with Pillow, so the UI, API and playlist logic can be developed without a Pi. On a Linux desktop with GStreamer, `--backend gst --sink window` plays real video in a window.

## How it works

```
piplayer/
  web.py          aiohttp REST API, WebSocket status, static UI
  engine.py       playlists, scheduling, transitions, error recovery (backend-agnostic)
  backend_gst.py  GStreamer renderer (the real one)
  backend_sim.py  simulated renderer for development and tests
  config.py       settings + playlists, validation, JSON persistence
  media.py        media library, probing, image pre-rendering
  hw.py           Pi model / HDMI / audio detection, health
  static/         web UI (plain HTML/CSS/JS, no build step)
```

The renderer is a single long-lived GStreamer pipeline:

```
[layer: file ─ decodebin (V4L2 HW decode) ─ glupload ─ glcolorconvert] ─┐
[layer ...]                                                             ├─ glvideomixer ─ glimagesink (GBM → KMS → HDMI)
[background color] [dip color]                                         ─┘         └─ preview JPEG (on demand)
[layer audio ...] ─ audiomixer ─ volume ─ alsasink (HDMI)
```

- Each playlist item becomes a *layer* that is added to the running pipeline about 4 s before it's needed. It decodes its first frame and holds it with a blocking pad probe. Starting the layer sets that pad's time offset so the held frame lands on an exact running time, then releases it.
- Transitions are keyframes on the mixer pads' `alpha` and `volume`, evaluated by the mixers every output frame. Timing is frame-accurate and doesn't depend on Python's scheduling.
- A dissolve fades the incoming layer in over the outgoing one; if the incoming picture is letterboxed, the outgoing one also fades out. A dip fades a solid-color layer in and out and cuts between items underneath it at the midpoint.
- Compositing happens on the GPU (the Pi 3's VC4 over GLES2), so the CPU mostly shuffles buffers.

## Troubleshooting

- **Logs:** `journalctl -u piplayer -f`
- **Black screen, UI works:** check Now Playing → Player for the display and dropped-frame count. Run `scripts/diag.sh`.
- **Stutter:** check the file is H.264 ≤1080p30 and the Media tab shows no warnings. Look for under-voltage in the Player panel; the Pi 3 needs a proper 2.5 A supply. Try Settings → Frame rate 25/30.
- **No audio:** Settings → Audio. `auto` picks the HDMI ALSA device (`vc4hdmi`). If the display has no speakers, audio is disabled automatically after a failure.
- **Restart video without rebooting:** Settings → *Restart renderer*, or `POST /api/system/restart-renderer`.

## Status

The web UI, API and playlist engine are covered by tests against the simulated renderer. The GStreamer renderer was written against the GStreamer 1.26 sources and still needs to be brought up and tuned on real Pi 3 hardware.
