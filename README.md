# PiPlayer

Fullscreen playlist player for the Raspberry Pi, controlled over the network.

- Boots straight into playback of a **default playlist**, fullscreen on HDMI, with no desktop.
- **Playlists** of videos and still images. Images have per-item display durations. Each playlist can **loop**, **stop**, **hold** the last frame, or **go to another playlist** when it ends.
- **Transitions** between items and playlists: **cut**, **dissolve**, or **dip through a color**. You can set them per playlist, per item, per "go to" link, or per cue.
- **Per-item position offsets** (X/Y). Adjust them live: the ⌖ button on a playlist item puts it on screen in loop-item mode. Drag it in the live preview, nudge it with the arrow keys, or pause and fine-tune.
- **GPI triggers:** contact closures on the Pi's GPIO pins fire playlists or transport commands (next, stop, pause, and so on). Set them up on the Triggers tab.
- **Web UI** for playlists, media uploads, settings, transport controls (including loop-item) and a live preview of the output.
- **REST + WebSocket API** for show controllers and scripts. See [docs/API.md](docs/API.md). The same document is shown on the UI's API tab.
- Output uses the display's own resolution. On a Pi 3 it runs at 30 Hz (1080p30) when the display supports it, or you can pick any mode the display offers.
- No authentication. Meant for a trusted LAN.

## Hardware

| | Pi 3 Model B / B+ | Pi 4 |
|---|---|---|
| HDMI | full-size | micro-HDMI ×2 |
| Hardware video decode | H.264 up to 1080p | H.264 up to 1080p, HEVC up to 4K |
| Recommended content | H.264, ≤1920×1080, ≤30 fps | H.264 or HEVC, 1080p |

The Pi 3 has 1 GB of RAM (there's no 2 GB Pi 3), which is plenty for this. Start with the Pi 3. The software is identical on both, so switching to a Pi 4 later is just moving the SD card.

A dissolve between two videos decodes both at once and shows both on screen. Measured on a Pi 3 B+: two 1080p30 H.264 streams decode and display together at full rate. PiPlayer averages about 23% of one CPU core while looping a mixed playlist with dissolves.

## Preparing the Pi

Use **Raspberry Pi OS Lite (64-bit), Trixie**. You don't need the desktop: PiPlayer drives the HDMI output directly through DRM/KMS and never uses X11 or Wayland. On the full desktop image, the compositor would own the display, so PiPlayer would have to run inside a desktop session. That costs RAM and GPU time the Pi 3 can't spare, and adds boot time and moving parts.

1. In **Raspberry Pi Imager**, choose *Raspberry Pi OS (other) → Raspberry Pi OS Lite (64-bit)*. In the OS customization settings:
   - hostname: `piplayer` (the UI will be at `http://piplayer.local/`)
   - a username and password
   - enable SSH (public-key auth recommended) and add your key
   - Wi-Fi if needed (wired Ethernet is recommended for uploading large videos)
2. Boot it with the HDMI display connected, then SSH in.
3. Install with one command, run as your normal user (not with `sudo`):
   ```bash
   curl -sL "https://github.com/ncory/PiPlayer/raw/refs/heads/main/install.sh" | bash
   ```
   It asks for your password once. Raspberry Pi OS Trixie doesn't give the first user passwordless `sudo`. The installer then:
   - updates the system (`PIPLAYER_SKIP_UPGRADE=1` skips this)
   - clones PiPlayer into `~/piplayer`
   - installs the GStreamer, Mesa and Python packages
   - creates a `piplayer` service user
   - installs the app to `/opt/piplayer`, with data in `/var/lib/piplayer`
   - enables the `piplayer` systemd service on port 80
   - hides boot messages and the tty1 login prompt
   - reserves 128 MB of video memory (`gpu_mem`) on Pi 4 and earlier

   It also lets your user update PiPlayer later without a password: you own `/opt/piplayer` and may start, stop and restart the `piplayer` service and reboot the Pi, and nothing else. The service itself runs as the unprivileged `piplayer` user.

   Options go in `PIPLAYER_OPTS`, for example:
   ```bash
   curl -sL "https://github.com/ncory/PiPlayer/raw/refs/heads/main/install.sh" | PIPLAYER_OPTS="--port 8080 --no-quiet-boot" bash
   ```
4. Reboot once (`sudo reboot`), then open `http://<hostname>.local/`, upload media, and build the `default` playlist.

To update, run the same `curl` command again. Playlists, settings and media are kept.

From a checkout you can also run the system installer directly: `sudo ./scripts/install.sh [--deploy-user "$USER"] [--port N] [--no-quiet-boot] [--gpu-mem N | --no-gpu-mem]`.

### Video memory (`gpu_mem`)

The hardware H.264 decoder allocates from the firmware's video memory pool, which `gpu_mem` sizes. At 1080p one clip costs about 26 MB, and a dissolve needs roughly 27 MB more, because two clips and the freeze buffer are alive at once. Raspberry Pi OS ships with `gpu_mem=76`, which leaves about 2 MB spare: the first dissolve scrapes through and a later one fails with `Failed to allocate required memory`, taking the renderer down until the Pi is rebooted.

The installer therefore appends `gpu_mem=128` to `/boot/firmware/config.txt` (backing the file up first), which roughly doubles the pool and leaves around 27 MB of headroom. It applies at the next reboot. The installer never overrides a `gpu_mem` you have set yourself; it warns instead if yours is lower. It is skipped on the Pi 5, which has no firmware decoder, and on boards with less than 1 GB of RAM. Use `--no-gpu-mem` to leave `config.txt` untouched.

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

Settings → *Display mode* is `auto` or any mode the display offers (for example `1920x1080@60`).

- `auto` uses the display's preferred resolution. On a Pi 3 it picks the 30 Hz (or else 25 Hz) version of that resolution when the display supports it. The Pi 3's display controller has a fixed pixel budget: at 60 Hz it can show only one 1080p video at a time, so a dissolve between two videos would turn into a cut. At 30 Hz two fit comfortably, and 30 fps content plays without judder.
- Where that isn't possible (a display that only accepts 50/60 Hz, or a 60 Hz mode you chose), a Pi 3 can't show two 1080p videos at once. Video-to-video dissolves then **freeze the outgoing clip**: it holds its current frame (converted to a still by the Pi's image processor) while the new clip fades in over it with full motion, about ¼ second after the freeze. Cuts, loops with cuts, dips to color, and dissolves to or from images aren't affected. The Playlists page shows a notice when this applies. If the display doesn't list 30 Hz, try forcing `1920x1080@30` in Settings → Display mode: many projectors and TVs accept it anyway.

If the display isn't connected at boot, PiPlayer retries every 30 s and resumes the default playlist once a display appears. To make HDMI come up even when the display is off, add `video=HDMI-A-1:1920x1080@30D` to the end of the single line in `/boot/firmware/cmdline.txt`. The trailing `D` forces the output on.

## GPI triggers (contact closures)

Set these up on the web UI's Triggers tab, or with `PUT /api/gpi`. Each input watches one GPIO pin and runs an action when its contact **closes**, **opens**, or either. The actions are: play a playlist (optionally from a given item, with a transition), next, previous, stop, pause, resume, pause/resume, and loop item.

Wiring: connect a switch, button or dry relay contact between a GPIO pin and a GND pin. The Pi's internal pull-up holds the pin high, and closing the contact pulls it low. No other parts are needed.

| Good GPIO choices | Header pin |  | GND header pins |
|---|---|---|---|
| GPIO 17, 27, 22 | 11, 13, 15 | | 6, 9, 14, 20, 25, 30, 34, 39 |
| GPIO 23, 24, 25 | 16, 18, 22 | | |
| GPIO 5, 6, 16, 26 | 29, 31, 36, 37 | | |

- The pins are 3.3 V only. **Never connect 5 V or 12 V to a GPIO pin.** For a device that outputs a voltage (such as a show controller's 12 V GPO), use a relay or an optocoupler, or set the input to pull-down / active high for a 3.3 V logic signal.
- For long cable runs, twisted pair and the kernel debounce (20 ms by default) keep noise out. The hold-off (300 ms by default) ignores re-triggers, such as a double-pressed button.
- One pin can carry several inputs, for example *closes* plays one playlist and *opens* plays another.
- The Triggers tab shows each input's live open/closed state, so you can check the wiring. **Test** runs an input's action without touching the hardware.

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
  backend_kms.py  hardware-plane renderer (the Pi default)
  kms.py          libdrm atomic modesetting bindings (ctypes)
  backend_gst.py  GStreamer layer machinery + optional GL-compositing renderer
  backend_sim.py  simulated renderer for development and tests
  config.py       settings + playlists, validation, JSON persistence
  media.py        media library, probing, image pre-rendering
  gpi.py          GPIO contact-closure triggers (libgpiod)
  hw.py           Pi model / HDMI / audio detection, health
  static/         web UI (plain HTML/CSS/JS, no build step)
```

There is no GPU compositing. Decoded frames go straight onto hardware display planes, and the display controller (the HVS) positions, scales, alpha-blends and stacks them while it scans out the picture:

```
[layer: file ─ decodebin (V4L2 HW decode) ─ fakevideosink] ─┐ decoder's own dmabufs
[layer ...]                                                 │ (zero copy)
                                                            ▼
            presenter thread: one atomic KMS commit per display frame
              primary plane  = background color
              overlay planes = one per visible layer (YUV scanout), with alpha + zpos
              top overlay    = dip color
[layer audio ...] ─ audiomixer ─ volume ─ alsasink (HDMI)
```

- Each playlist item becomes a *layer* that is added to the running GStreamer pipeline about 4 s before it's needed. It decodes its first frame and holds it with a blocking pad probe. Starting the layer sets that pad's time offset so the held frame lands on an exact running time, then releases it.
- Transitions are opacity keyframes. The presenter evaluates them for the next display refresh and applies every plane's frame, position and opacity in a single atomic update, so each refresh shows one consistent frame. Audio fades are keyframes on the audio mixer's pads.
- A dissolve is a true crossfade: the incoming layer fades in over the outgoing one, which stays fully opaque until the incoming layer is fully opaque, and is then hidden. If the incoming picture has letterbox bars (or is offset), the outgoing picture shows in those areas until the dissolve completes. A dip fades a solid-color plane in and out and cuts between items underneath it at the midpoint.
- Images are rendered once, at the display resolution, into scanout buffers.
- Why not the GPU: on the Pi 3 the GPU gets empty textures when it imports the decoder's YUV buffers, and uploading 1080p frames through the CPU runs at about 1 fps. The display controller scans the same buffers out for free. (`--backend gl` keeps a GStreamer GL-compositing renderer for other hardware.)

## Troubleshooting

- **Logs:** `journalctl -u piplayer -f`
- **Black screen, UI works:** check Now Playing → Player for the display and dropped-frame count. Run `scripts/diag.sh`.
- **Stutter:** check the file is H.264 ≤1080p30 and the Media tab shows no warnings. Look for under-voltage in the Player panel; the Pi 3 needs a proper 2.5 A supply. Try Settings → Frame rate 25/30.
- **No audio:** Settings → Audio. `auto` picks the HDMI ALSA device (`vc4hdmi`). If the display has no speakers, audio is disabled automatically after a failure.
- **Restart video without rebooting:** Settings → *Restart renderer*, or `POST /api/system/restart-renderer`.

## Status

Brought up on a Raspberry Pi 3 Model B+ (Raspberry Pi OS Lite Trixie, kernel 6.18, GStreamer 1.26) driving a 1080p display. Verified there:

- H.264 1080p30 playback with HDMI audio
- dissolves between two 1080p videos, and between images and videos
- dip to color
- letterboxed 4:3 content
- pause, loop-item, and live repositioning of the on-screen item while paused

The web UI, API and playlist engine are also covered by tests against the simulated renderer.

## License

MIT. See [LICENSE](LICENSE).
