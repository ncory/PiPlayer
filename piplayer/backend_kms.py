"""KMS renderer: decoded frames go straight onto hardware display planes.

This is the renderer for the Raspberry Pi. There is no GPU compositing:

    layer bins (decodebin, V4L2 hardware decode) ─ fakevideosink (sync to clock)
         │ handoff: the decoder's own dmabuf
         ▼
    presenter thread ─ one atomic KMS commit per display frame:
         primary plane  = background color
         overlay planes = one per visible layer (zero-copy YUV scanout),
                          positioned, scaled, alpha-blended and z-ordered
                          by the display controller (HVS)
         top overlay    = dip color (alpha animated)

    audio ─ audiomixer ─ volume ─ alsasink (HDMI), as in the GL renderer

Why: on the Pi 3 the GPU can't import the decoder's YUV buffers (it gets
empty textures) and uploading 1080p frames through the CPU is far too slow,
while the display controller scans the same buffers out directly for free.
Its per-frame pixel budget allows two full 1080p video planes at 30 Hz but
not at 60 Hz, hence the 30 Hz preference in "auto" mode on the Pi 3.

Timing, preroll, pause and audio reuse the GStreamer layer machinery from
GstBackend; only where video ends up differs.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstAllocators", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstAllocators, GstVideo  # noqa: E402

from . import kms  # noqa: E402
from .backend import Keyframes, Layer, Source, covers, eval_keyframes, fit_rect, hex_to_rgb  # noqa: E402
from .backend_gst import GstBackend, _LayerGst, _structure  # noqa: E402

log = logging.getLogger(__name__)
SEC = Gst.SECOND
PREVIEW_WIDTH = 480
LOW_REFRESH_FAMILIES = {"pi3"}  # display controllers too slow for 2x1080p at 60 Hz


@dataclass
class _KLayer:
    """Presenter-side state of one layer (guarded by KmsBackend.lock)."""

    rect: tuple[int, int, int, int] | None = None  # placed rect incl. offset
    alpha_kfs: Keyframes = field(default_factory=list)
    alpha_base: float = 0.0
    pending: Gst.Buffer | None = None  # newest decoded frame, not yet shown
    current: Gst.Buffer | None = None  # frame on (or going to) screen
    fmt: tuple[int, int] = (kms.YU12, 0)  # drm fourcc, modifier
    size: tuple[int, int] = (0, 0)  # source frame size
    image: kms.DumbBuffer | None = None
    image_path: Any = None
    fbs: dict = field(default_factory=dict)  # (fd, offsets) -> (fb_id, handle)
    plane: int | None = None
    frames_in: int = 0
    frames_shown: int = 0
    removed: bool = False


class KmsBackend(GstBackend):
    name = "kms"

    def __init__(self, settings: dict, hw: dict, emit, device: str | None = None):
        super().__init__(settings, hw, emit, sink="fake")  # no GL/GBM setup
        self.sink_mode = "planes"  # (not "fake": audio goes to the real HDMI device)
        self.device = device
        self.card: kms.Card | None = None
        self.out: kms.Output | None = None
        self.lock = threading.RLock()
        self.klayers: dict[int, _KLayer] = {}
        self.bg: kms.DumbBuffer | None = None
        self.dipbuf: kms.DumbBuffer | None = None
        self.bg_color = settings["background_color"]
        self.dip_color = "#000000"
        self.dip_kfs: Keyframes = []
        self.primary: int | None = None
        self.dip_plane: int | None = None
        self.free_planes: list[int] = []
        self.presenter: _Presenter | None = None
        self.modes: list[str] = []
        self.forced_mode = False
        self.commit_failures = 0
        self.frames_presented = 0
        self.last_commit_error: str | None = None

    # ------------------------------------------------------------- setup
    def _open_display(self) -> None:
        path = self.device or next((o["card"] for o in _connected_cards()), "/dev/dri/card0")
        self.card = card = kms.Card(path)
        want = self.settings["output"].get("mode", "auto")
        # An explicitly chosen mode may be forced even if the display doesn't
        # list it (standard CEA timing); "auto" only ever uses listed modes.
        out = card.pick_output(mode=want, allow_custom=True)
        self.forced_mode = bool(out.mode.type & kms.DRM_MODE_TYPE_USERDEF)
        if self.forced_mode:
            log.warning("forcing %s, which the display does not list", out.mode_name())
        if want in (None, "", "auto") and self.hw.get("family") in LOW_REFRESH_FAMILIES:
            out = _prefer_low_refresh(card, out)
        self.out = out
        self.render_size = out.size
        self.fps = out.refresh
        self.modes = _mode_list(card, out.name)
        planes = card.planes(out.crtc_index)
        self.primary = next(p["id"] for p in planes if p["type"] == kms.PLANE_TYPE_PRIMARY)
        overlays = [p["id"] for p in planes if p["type"] == kms.PLANE_TYPE_OVERLAY
                    and kms.YU12 in p["formats"] and p["has_alpha"] and p["has_zpos"]]
        if len(overlays) < 3:
            raise kms.DrmError(0, "display has too few overlay planes with alpha/zpos")
        self.dip_plane = overlays[0]
        self.free_planes = overlays[1:]
        w, h = out.size
        self.bg = card.dumb_buffer(w, h)
        kms.fill_xrgb(self.bg, hex_to_rgb(self.bg_color))
        self.dipbuf = card.dumb_buffer(w, h)
        kms.fill_xrgb(self.dipbuf, hex_to_rgb(self.dip_color))
        changes: dict[int, dict[str, int]] = {
            out.connector_id: {"CRTC_ID": out.crtc_id},
            out.crtc_id: {"MODE_ID": card.mode_blob(out.mode), "ACTIVE": 1},
            self.primary: _plane_props(self.bg.fb_id, out.crtc_id, (0, 0, w << 16, h << 16),
                                       (0, 0, w, h)),
        }
        for pid in overlays:  # clear anything left on the planes we'll use
            changes[pid] = {"FB_ID": 0, "CRTC_ID": 0}
        ret = card.commit(changes, kms.DRM_MODE_ATOMIC_ALLOW_MODESET)
        if ret:
            raise kms.DrmError(-ret, f"modeset {out.mode_name()} failed: {ret}")
        log.info("display %s %s (%d overlay planes)", out.name, out.mode_name(), len(overlays))

    def _build(self) -> Gst.Pipeline:
        # Audio (or a silent live source) keeps the pipeline live; video
        # sinks are added per layer.
        audio = self._audio_desc()
        desc = audio or "audiotestsrc is-live=true wave=silence ! fakesink sync=true"
        log.info("pipeline: audio %s", self.audio_device if audio else "off")
        pipe = Gst.parse_launch(desc)
        if not isinstance(pipe, Gst.Pipeline):  # a single chain parses to a bin
            p = Gst.Pipeline.new()
            p.add(pipe)
            pipe = p
        self.mixer = None
        self.amixer = pipe.get_by_name("amix")
        return pipe

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._open_display)
        await super().start()
        self.presenter = _Presenter(self)
        self.presenter.start()

    async def stop(self) -> None:
        if self.presenter:
            self.presenter.stop()
            await asyncio.to_thread(self.presenter.join, 2)
        await super().stop()
        with self.lock:
            for k in self.klayers.values():
                self._free_klayer(k)
            self.klayers.clear()
        if self.card:
            for buf in (self.bg, self.dipbuf):
                if buf:
                    self.card.destroy_dumb(buf)
            self.card.close()  # drops DRM master; the console comes back
            self.card = None

    # ------------------------------------------------------------ layers
    def _k(self, layer: Layer) -> _KLayer:
        with self.lock:
            k = self.klayers.get(layer.id)
            if k is None:
                k = self.klayers[layer.id] = _KLayer()
            return k

    def create_layer(self, source: Source) -> Layer:
        if source.kind != "image":
            layer = super().create_layer(source)
            self._k(layer)
            return layer
        layer = Layer(source)
        layer.g = _LayerGst()  # type: ignore[attr-defined]
        self.z += 1
        layer.z = self.z  # type: ignore[attr-defined]
        self.layers[layer.id] = layer
        k = self._k(layer)
        w, h = self.render_size
        k.size = (w, h)
        k.fmt = (kms.XR24, 0)
        self._place(layer, (0, 0, w, h))
        self.teardown.submit(self._load_image, layer, k)
        return layer

    def _load_image(self, layer: Layer, k: _KLayer) -> None:
        """Copy the pre-rendered image into a scanout buffer (worker thread)."""
        try:
            from PIL import Image

            w, h = self.render_size
            with Image.open(layer.source.path) as im:
                im = im.convert("RGB")
                if im.size != (w, h):
                    im = im.resize((w, h))
                data = im.tobytes("raw", "BGRX")
            buf = self.card.dumb_buffer(w, h)
            row = w * 4
            if buf.pitch == row:
                buf.map[: len(data)] = data
            else:
                for y in range(h):
                    buf.map[y * buf.pitch: y * buf.pitch + row] = data[y * row: (y + 1) * row]
            with self.lock:
                if k.removed:
                    self.card.destroy_dumb(buf)
                    return
                k.image = buf
                k.image_path = layer.source.path
            self._post(self._layer_ready, layer)
        except Exception as e:  # noqa: BLE001
            log.exception("image load failed")
            self._post(self.emit, "error", layer, f"image: {e}")

    def _video_elements(self) -> list[tuple[str, dict]]:
        # A small queue decouples the decoder thread from clock waits.
        return [("queue", {"max-size-buffers": 1, "max-size-time": 0, "max-size-bytes": 0})]

    def _attach_video(self, layer: Layer, ghost: Gst.Pad) -> None:
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        sink = Gst.ElementFactory.make("fakevideosink", f"layer{layer.id}_sink")
        for prop, val in (("sync", True), ("async", False), ("qos", False),
                          ("signal-handoffs", True), ("enable-last-sample", False),
                          ("max-lateness", -1)):
            sink.set_property(prop, val)
        sink.connect("handoff", self._on_frame, layer)
        self.pipeline.add(sink)
        g.extra_elements.append(sink)
        sink.sync_state_with_parent()
        ghost.link(sink.get_static_pad("sink"))

    def _apply_geometry(self, layer: Layer, caps: Gst.Caps) -> None:
        with _structure(caps) as s:
            ok_w, vw = s.get_int("width")
            ok_h, vh = s.get_int("height")
            ok, pn, pd = s.get_fraction("pixel-aspect-ratio")
            drm_format = s.get_string("drm-format") if s.has_field("drm-format") else None
        if not (ok_w and ok_h):
            return
        k = self._k(layer)
        with self.lock:
            k.size = (vw, vh)
            if drm_format:
                k.fmt = _parse_drm_format(drm_format)
        dw = vw * pn / pd if ok and pd else vw
        w, h = self.render_size
        self._place(layer, fit_rect(dw, vh, w, h, layer.source.fit))

    def _place(self, layer: Layer, rect: tuple[int, int, int, int]) -> None:
        g: _LayerGst = layer.g  # type: ignore[attr-defined]
        g.rect = rect
        x, y, rw, rh = rect
        dx, dy = layer.offset
        placed = (x + dx, y + dy, rw, rh)
        with self.lock:
            self._k(layer).rect = placed
        layer.full_frame = covers(placed, *self.render_size)
        self._wake()

    def _on_frame(self, sink, buf: Gst.Buffer, pad, layer: Layer) -> None:
        k = self.klayers.get(layer.id)
        if k is None:
            return
        with self.lock:
            k.pending = buf
            k.frames_in += 1
        self._wake()

    def start_layer(self, layer: Layer, at: float) -> None:
        if layer.source.kind == "image":
            layer.start_time = at
            layer.state = "playing"
            self._wake()
            return
        super().start_layer(layer, at)

    def remove_layer(self, layer: Layer) -> None:
        with self.lock:
            k = self.klayers.get(layer.id)
            if k:
                k.removed = True  # presenter drops it and frees its buffers after the flip
        self._wake()
        super().remove_layer(layer)

    def release_freed(self, freed: list) -> None:
        with self.lock:
            for lid, k in freed:
                if self.klayers.get(lid) is k:
                    del self.klayers[lid]
                self._free_klayer(k)

    def _free_klayer(self, k: _KLayer) -> None:
        """Release a removed layer's KMS resources (not on screen any more)."""
        card = self.card
        if card:
            for fb, handle in k.fbs.values():
                card.rm_fb(fb)
                card.release_handle(handle)
            if k.image:
                card.destroy_dumb(k.image)
        k.fbs.clear()
        k.image = None
        k.pending = k.current = None
        if k.plane is not None:
            self.free_planes.append(k.plane)
            k.plane = None

    # ---------------------------------------------------------- animation
    def set_alpha(self, layer: Layer, kfs: Keyframes) -> None:
        k = self._k(layer)
        with self.lock:
            k.alpha_base = eval_keyframes(k.alpha_kfs, self.now(), k.alpha_base)
            k.alpha_kfs = sorted(kfs)
        self._wake()

    def dip(self, color: str, kfs: Keyframes) -> None:
        with self.lock:
            if color != self.dip_color and self.dipbuf:
                kms.fill_xrgb(self.dipbuf, hex_to_rgb(color))  # plane is off (alpha 0) now
                self.dip_color = color
            self.dip_kfs = sorted(kfs)
        self._wake()

    def set_background(self, color: str) -> None:
        self.bg_color = color
        if self.bg:
            kms.fill_xrgb(self.bg, hex_to_rgb(color))

    def _wake(self) -> None:
        if self.presenter:
            self.presenter.event.set()

    # ------------------------------------------------------------ frame
    def compose(self, t: float) -> tuple[dict[int, dict[str, int]], list, list]:
        """Plane state for display time `t`: (changes, buffers on screen, freed layers)."""
        card, out = self.card, self.out
        W, H = self.render_size
        planes: dict[int, dict[str, int]] = {}
        held: list = []
        visible = []
        freed = []
        with self.lock:
            for lid, k in list(self.klayers.items()):
                if k.removed:  # freed once the commit hiding it has flipped
                    freed.append((lid, k))
                    continue
                layer = self.layers.get(lid)
                if layer is None or layer.state != "playing" or layer.start_time is None:
                    continue
                if k.pending is not None:
                    k.current, k.pending = k.pending, None
                    k.frames_shown += 1
                alpha = eval_keyframes(k.alpha_kfs, t, k.alpha_base)
                if alpha <= 0.002 or k.rect is None or t < layer.start_time - 0.001:
                    continue
                if k.image is not None:
                    fb = k.image.fb_id
                elif k.current is not None:
                    fb = self._fb_for(k, k.current)
                    if not fb:
                        continue
                    held.append(k.current)
                else:
                    continue
                clip = _clip(k.rect, k.size, W, H, yuv=k.fmt[0] != kms.XR24)
                if clip is None:
                    continue
                visible.append((layer.z, k, fb, clip, alpha))  # type: ignore[attr-defined]
            visible.sort(key=lambda v: v[0])
            used = set()
            for zpos, (_, k, fb, (src, dst), alpha) in enumerate(visible, start=1):
                if k.plane is None:
                    if not self.free_planes:
                        continue
                    k.plane = self.free_planes.pop(0)
                used.add(k.plane)
                props = _plane_props(fb, out.crtc_id, src, dst)
                props["alpha"] = min(65535, int(alpha * 65535 + 0.5))
                props["zpos"] = zpos
                planes[k.plane] = props
            dip_alpha = eval_keyframes(self.dip_kfs, t, 0.0)
            if dip_alpha > 0.002 and self.dipbuf:
                props = _plane_props(self.dipbuf.fb_id, out.crtc_id, (0, 0, W << 16, H << 16),
                                     (0, 0, W, H))
                props["alpha"] = min(65535, int(dip_alpha * 65535 + 0.5))
                props["zpos"] = len(visible) + 1
                planes[self.dip_plane] = props
            # Layers that have a plane but aren't visible this frame keep it
            # (assignment is sticky) but it's switched off.
            for k in self.klayers.values():
                if k.plane is not None and k.plane not in used:
                    planes[k.plane] = {"FB_ID": 0, "CRTC_ID": 0}
            if self.dip_plane not in planes:
                planes[self.dip_plane] = {"FB_ID": 0, "CRTC_ID": 0}
        return planes, held, freed

    def primary_props(self) -> dict[str, int]:
        W, H = self.render_size
        return _plane_props(self.bg.fb_id, self.out.crtc_id, (0, 0, W << 16, H << 16), (0, 0, W, H))

    def _fb_for(self, k: _KLayer, buf: Gst.Buffer) -> int:
        """Framebuffer for a decoder dmabuf (cached per underlying buffer)."""
        mem = buf.peek_memory(0)
        if not GstAllocators.is_dmabuf_memory(mem):
            return 0
        fd = GstAllocators.dmabuf_memory_get_fd(mem)
        meta = GstVideo.buffer_get_video_meta(buf)
        if meta is None:
            return 0
        n = meta.n_planes
        key = (fd, tuple(meta.offset[:n]), tuple(meta.stride[:n]))
        hit = k.fbs.get(key)
        if hit:
            return hit[0]
        try:
            handle = self.card.import_dmabuf(fd)
            pad = (0,) * (4 - n)
            fb = self.card.add_fb(meta.width, meta.height, k.fmt[0], (handle,) * n + pad,
                                  tuple(meta.stride[:n]) + pad, tuple(meta.offset[:n]) + pad,
                                  modifier=k.fmt[1])
        except kms.DrmError as e:
            log.error("could not import video frame: %s", e)
            return 0
        k.fbs[key] = (fb, handle)
        return fb

    # ------------------------------------------------------------ preview
    async def snapshot(self) -> bytes | None:
        with self.lock:
            now = self.now()
            items = []
            for lid, k in self.klayers.items():
                layer = self.layers.get(lid)
                if k.removed or layer is None or layer.state != "playing" or k.rect is None:
                    continue
                alpha = eval_keyframes(k.alpha_kfs, now, k.alpha_base)
                if alpha <= 0.002:
                    continue
                src = k.image_path if k.image is not None else k.current
                if src is None:
                    continue
                items.append((layer.z, src, k.fmt[0], k.rect, alpha))  # type: ignore[attr-defined]
            dip = (self.dip_color, eval_keyframes(self.dip_kfs, now, 0.0))
            bg = self.bg_color
        items.sort(key=lambda i: i[0])
        return await asyncio.to_thread(self._render_preview, bg, items, dip)

    def _render_preview(self, bg: str, items: list, dip: tuple[str, float]) -> bytes:
        from PIL import Image

        W, H = self.render_size
        pw = PREVIEW_WIDTH
        ph = max(1, round(pw * H / W))
        sx = pw / W
        frame = Image.new("RGB", (pw, ph), hex_to_rgb(bg))
        for _, src, fmt, (x, y, w, h), alpha in items:
            size = (max(1, round(w * sx)), max(1, round(h * sx)))
            try:
                pic = _preview_image(src, fmt, size)
            except Exception:  # noqa: BLE001
                log.exception("preview frame failed")
                continue
            layer_img = frame.copy()
            layer_img.paste(pic, (round(x * sx), round(y * sx)))
            frame = Image.blend(frame, layer_img, alpha)
        if dip[1] > 0.002:
            frame = Image.blend(frame, Image.new("RGB", frame.size, hex_to_rgb(dip[0])), dip[1])
        out = io.BytesIO()
        frame.save(out, "JPEG", quality=80)
        return out.getvalue()

    # --------------------------------------------------------------- info
    def info(self) -> dict[str, Any]:
        shown = sum(k.frames_shown for k in self.klayers.values())
        got = sum(k.frames_in for k in self.klayers.values())
        return {
            "backend": self.name,
            "render_size": list(self.render_size),
            "fps": self.fps,
            "display": {"connector": self.out.name, "mode": self.out.mode_name(),
                        "connected": True, "modes": self.modes, "forced": self.forced_mode,
                        "forceable": [f"{w}x{h}@{hz}" for (w, h, hz) in kms.CEA_MODES
                                      if f"{w}x{h}@{hz}" not in self.modes]} if self.out else None,
            "audio_device": self.audio_device,
            "frames_presented": self.frames_presented,
            "frames_rendered": shown,
            "frames_dropped": max(0, got - shown),
            "commit_failures": self.commit_failures,
            "last_commit_error": self.last_commit_error,
            "layers": len(self.layers),
        }


class _Presenter(threading.Thread):
    """Commits plane updates once per display frame (or when idle, on change)."""

    def __init__(self, be: KmsBackend):
        super().__init__(name="piplayer-present", daemon=True)
        self.be = be
        self.event = threading.Event()
        self._stop = False
        self.last: dict[int, dict[str, int]] = {}
        self.onscreen: list = []
        self.inflight: tuple[list, list] | None = None
        self.warned = False
        self._flip_sent = 0.0

    def stop(self) -> None:
        self._stop = True
        self.event.set()

    def run(self) -> None:
        be = self.be
        period = 1.0 / max(1, be.fps)
        while not self._stop:
            try:
                if self.inflight is not None:
                    if be.card.read_events(0.25) or self._stale():
                        held, freed = self.inflight
                        self.inflight = None
                        self.onscreen = held  # previous frame's buffers are released here
                        be.release_freed(freed)
                    continue
                planes, held, freed = be.compose(be.now() + period * 0.5)
                changes = {pid: p for pid, p in planes.items() if self.last.get(pid) != p}
                if not changes and not freed:
                    self.event.wait(period / 2)
                    self.event.clear()
                    continue
                if changes:
                    # A flip event needs the CRTC in the commit; the (unchanged)
                    # background plane brings it in.
                    changes[be.primary] = be.primary_props()
                ret = be.card.commit(changes, kms.DRM_MODE_ATOMIC_NONBLOCK
                                     | kms.DRM_MODE_PAGE_FLIP_EVENT) if changes else 0
                if ret == 0:
                    self.last.update(changes)
                    be.frames_presented += 1
                    self._flip_sent = time.monotonic()
                    self.inflight = (held, freed) if changes else None
                    if not changes:
                        be.release_freed(freed)
                else:
                    be.commit_failures += 1
                    be.last_commit_error = f"atomic commit failed: {ret}"
                    if not self.warned:
                        log.warning("atomic commit failed (%d)%s", ret,
                                    "; over the display budget, hiding the oldest layer"
                                    if ret == -28 else "")
                        self.warned = True
                    if ret == -28:  # ENOSPC: too many pixels for the display controller
                        self._shed_load(planes)  # removed layers stay pending; retried next frame
                    self.event.wait(period / 2)
                    self.event.clear()
            except Exception:  # noqa: BLE001
                log.exception("presenter error")
                time.sleep(0.1)

    def _stale(self) -> bool:
        # Never wait forever on a flip event (e.g. display unplugged).
        return time.monotonic() - self._flip_sent > 1.0

    def _shed_load(self, planes: dict[int, dict[str, int]]) -> None:
        """Over the display controller's budget: hide the lowest visible layer."""
        be = self.be
        with be.lock:
            shown = [(p.get("zpos", 0), pid) for pid, p in planes.items()
                     if p.get("FB_ID") and pid != be.dip_plane]
            if len(shown) < 2:
                return
            _, pid = min(shown)
            for k in be.klayers.values():
                if k.plane == pid:
                    k.alpha_kfs, k.alpha_base = [], 0.0


# ---------------------------------------------------------------- helpers

def _plane_props(fb: int, crtc: int, src: tuple[int, int, int, int],
                 dst: tuple[int, int, int, int]) -> dict[str, int]:
    return {"FB_ID": fb, "CRTC_ID": crtc,
            "SRC_X": src[0], "SRC_Y": src[1], "SRC_W": src[2], "SRC_H": src[3],
            "CRTC_X": dst[0], "CRTC_Y": dst[1], "CRTC_W": dst[2], "CRTC_H": dst[3]}


def _clip(rect: tuple[int, int, int, int], size: tuple[int, int], W: int, H: int,
          yuv: bool) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]] | None:
    """Clip a placed rect to the screen; returns (src 16.16, dst) or None."""
    x, y, w, h = rect
    sw, sh = size
    if w <= 0 or h <= 0 or sw <= 0 or sh <= 0:
        return None
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    fx, fy = sw / w, sh / h
    step = 2 if yuv else 1  # keep chroma-subsampled sources on even pixels

    def snap(v: float) -> int:
        return int(v) // step * step

    src_x, src_y = snap((x0 - x) * fx), snap((y0 - y) * fy)
    src_w = min(sw - src_x, snap((x1 - x0) * fx + step - 1) or step)
    src_h = min(sh - src_y, snap((y1 - y0) * fy + step - 1) or step)
    return (src_x << 16, src_y << 16, src_w << 16, src_h << 16), (x0, y0, x1 - x0, y1 - y0)


def _parse_drm_format(s: str) -> tuple[int, int]:
    code, _, mod = s.partition(":")
    return kms.fourcc(code[:4].ljust(4)), int(mod, 16) if mod else 0


def _mode_list(card: kms.Card, connector: str) -> list[str]:
    for name, connected, modes, _ in card.outputs():
        if name == connector:
            seen = []
            for m in modes:
                if m.flags & 0x10:  # interlaced
                    continue
                s = f"{m.hdisplay}x{m.vdisplay}@{m.vrefresh}"
                if s not in seen:
                    seen.append(s)
            return seen
    return []


def _prefer_low_refresh(card: kms.Card, out: kms.Output) -> kms.Output:
    """Same resolution at 30 (or 25) Hz, if the display offers it."""
    w, h = out.size
    for hz in (30, 25):
        try:
            alt = card.pick_output(connector=out.name, mode=f"{w}x{h}@{hz}")
        except kms.DrmError:
            continue
        if alt.refresh == hz and alt.size == (w, h):
            return alt
    return out


def _connected_cards() -> list[dict]:
    from . import hw

    return [o for o in hw.hdmi_outputs() if o["connected"]]


def _preview_image(src, fmt: int, size: tuple[int, int]):
    from PIL import Image

    if not isinstance(src, Gst.Buffer):  # an image file
        with Image.open(src) as im:
            return im.convert("RGB").resize(size)
    meta = GstVideo.buffer_get_video_meta(src)
    ok, info = src.map(Gst.MapFlags.READ)
    if not ok:
        raise RuntimeError("cannot map frame")
    try:
        data = bytes(info.data)
    finally:
        src.unmap(info)
    w, h = meta.width, meta.height
    off, st = list(meta.offset), list(meta.stride)
    y = Image.frombuffer("L", (w, h), data[off[0]:off[0] + st[0] * h], "raw", "L", st[0], 1)
    if fmt == kms.YU12 and meta.n_planes >= 3:
        cw, ch = (w + 1) // 2, (h + 1) // 2
        u = Image.frombuffer("L", (cw, ch), data[off[1]:off[1] + st[1] * ch], "raw", "L", st[1], 1)
        v = Image.frombuffer("L", (cw, ch), data[off[2]:off[2] + st[2] * ch], "raw", "L", st[2], 1)
        return Image.merge("YCbCr", (y.resize(size), u.resize(size), v.resize(size))).convert("RGB")
    return y.resize(size).convert("RGB")  # other layouts: luma only
