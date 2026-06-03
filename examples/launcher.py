"""Presto Home Screen Launcher
iPhone-style 3×3 app grid with a time + weather status bar.
Swipe left/right to paginate; tap an icon to launch the app.

Note: PicoVector vector.text(text, x, y) uses y as BASELINE, not top.
"""
import gc
import os
import sys
import time
import ujson

from picovector import ANTIALIAS_BEST, PicoVector, Polygon, Transform
from presto import Presto

try:
    import urequests
except ImportError:
    urequests = None

try:
    import ntptime
except ImportError:
    ntptime = None

# Latitude/longitude for weather (open-meteo, free, no key needed).
# These default to London — update if your secrets.py doesn't have LAT/LNG.
try:
    import secrets as _s
    LAT = getattr(_s, "LAT", 51.5)
    LNG = getattr(_s, "LNG", -0.1)
    del _s
except Exception:
    LAT = 51.5
    LNG = -0.1

# ── Display ────────────────────────────────────────────────────────────────
presto = Presto(full_res=True, ambient_light=True)
display = presto.display
W, H = display.get_bounds()        # 480 × 480
CX = W // 2
touch = presto.touch

# ── PicoVector ─────────────────────────────────────────────────────────────
vector = PicoVector(display)
vector.set_antialiasing(ANTIALIAS_BEST)
_t = Transform()
vector.set_font("Roboto-Medium.af", 32)
vector.set_font_letter_spacing(100)
vector.set_font_word_spacing(100)
vector.set_transform(_t)

# ── Colour palette ─────────────────────────────────────────────────────────
BG        = display.create_pen(14, 14, 18)
BAR_BG    = display.create_pen(26, 26, 32)
ORANGE    = display.create_pen(205, 127, 106)    # Claude orange
WHITE     = display.create_pen(255, 255, 255)
DIM       = display.create_pen(155, 155, 165)
WARM      = display.create_pen(255, 185, 80)

_ICON_PENS = [
    display.create_pen(59,  130, 246),  # blue
    display.create_pen(16,  185, 129),  # emerald
    display.create_pen(245, 158, 11),   # amber
    display.create_pen(239, 68,  68),   # red
    display.create_pen(168, 85,  247),  # purple
    display.create_pen(236, 72,  153),  # pink
    display.create_pen(20,  184, 166),  # teal
    display.create_pen(249, 115, 22),   # orange
]

# ── Layout constants ───────────────────────────────────────────────────────
BAR_H     = 96      # status bar height (px)
GRID_TOP  = 104     # first icon row top
COLS      = 3
ROWS      = 3
PER_PAGE  = COLS * ROWS
CELL_W    = W // COLS                         # 160
CELL_H    = (H - GRID_TOP) // ROWS            # ≈ 125
ICON_W    = 76
ICON_H    = 76
ICON_R    = 16

# ── Pixel-art icon library (8×8 bitmaps, one byte = one row, MSB = left) ──
# Each pixel is drawn at scale 5 → 40×40 px inside the 76×76 icon tile.
_ART = {
    "vector_clock_full":  (0x7E, 0x81, 0x99, 0x89, 0x81, 0x81, 0x7E, 0x00),
    "stop_watch":         (0x18, 0x7E, 0x81, 0x99, 0x89, 0x81, 0x7E, 0x00),
    "tomato":             (0x08, 0x1C, 0x7C, 0xFE, 0xFE, 0x7C, 0x38, 0x00),
    "awesome_game":       (0x7E, 0x89, 0xFB, 0x89, 0x7E, 0x24, 0x00, 0x00),
    "random_maze":        (0xFF, 0xA9, 0xAE, 0xA8, 0xFB, 0x82, 0xBE, 0xFF),
    "cubes":              (0x3C, 0x7E, 0xFF, 0xFF, 0x7E, 0x3C, 0x00, 0x00),
    "image_gallery":      (0x7E, 0xA5, 0xB5, 0xFF, 0x81, 0x7E, 0x00, 0x00),
    "cheerlights_bulb":   (0x38, 0x7C, 0xFE, 0xFE, 0x7C, 0x38, 0x38, 0x00),
    "attitude_indicator": (0x3C, 0x42, 0xFF, 0x42, 0x3C, 0x00, 0x10, 0x00),
    "sensor-stick-temperature": (0x38, 0x28, 0x28, 0x38, 0x7C, 0x7C, 0x38, 0x00),
    "word_clock":         (0x7E, 0xAB, 0xAB, 0xAB, 0xAB, 0x7E, 0x20, 0x00),
    "clawdmeter":         (0x00, 0x44, 0x7C, 0xDA, 0xFE, 0x7C, 0x44, 0x00),
}
# Diamond fallback for unknown apps
_ART_DEFAULT = (0x18, 0x3C, 0x7E, 0xFF, 0xFF, 0x7E, 0x3C, 0x18)

ART_SCALE   = 5       # each pixel → 5×5 px
ART_PX      = 8 * ART_SCALE    # 40 px
# Art is drawn in the upper portion of the icon; label text below it.
# cy = icon centre.  Art top = cy - 30 (8px margin from icon top at cy-38)
ART_Y_OFF   = -30     # offset of art top from cy

# ── Launcher state ─────────────────────────────────────────────────────────
_temp        = None
_weather_ms  = -999_999
_page        = 0

# ── Utilities ──────────────────────────────────────────────────────────────

def _vtext_c(text, cx, baseline_y, size):
    """Draw text, horizontally centred at cx, baseline at baseline_y."""
    vector.set_font_size(size)
    tw = int(vector.measure_text(text)[2])
    vector.text(text, cx - tw // 2, baseline_y)


def _msg(text):
    display.set_pen(BG)
    display.clear()
    display.set_pen(ORANGE)
    # Baseline ~midscreen; for size 24, caps appear above baseline by ~17px
    _vtext_c(text, CX, H // 2 + 8, 24)
    presto.update()


# ── Network ────────────────────────────────────────────────────────────────

def _sync_time():
    if ntptime:
        try:
            ntptime.settime()
        except Exception as e:
            print("ntp:", e)


def _fetch_weather():
    global _temp, _weather_ms
    if not urequests:
        _weather_ms = time.ticks_ms()
        return
    url = (
        "http://api.open-meteo.com/v1/forecast"
        "?latitude={}&longitude={}&current_weather=true&timezone=auto"
    ).format(LAT, LNG)
    try:
        r = urequests.get(url, timeout=8)
        _temp = ujson.loads(r.text)["current_weather"]["temperature"]
        r.close()
    except Exception as e:
        print("weather:", e)
    _weather_ms = time.ticks_ms()


# ── App discovery ──────────────────────────────────────────────────────────
_SKIP = {"main", "secrets"}


def _discover_apps():
    out = []
    try:
        files = sorted(f for f in os.listdir("/") if f.endswith(".py"))
    except OSError:
        return out
    for fname in files:
        mod = fname[:-3]
        if mod in _SKIP or mod.startswith("_"):
            continue
        name = None
        try:
            with open("/" + fname) as f:
                for _ in range(8):
                    ln = f.readline()
                    if ln.startswith("# NAME "):
                        name = ln[7:].strip()
                        break
        except Exception:
            pass
        if not name:
            raw = mod.replace("_", " ").replace("-", " ")
            name = " ".join(w[0].upper() + w[1:] for w in raw.split() if w)
        out.append((name, mod))
    return out


# ── Drawing ────────────────────────────────────────────────────────────────

def _rrect(x, y, w, h, r):
    p = Polygon()
    p.rectangle(x, y, w, h, (r, r, r, r))
    vector.draw(p)


def _draw_pixel_art(icon_x, art_y, mod_name):
    """Draw 8×8 pixel art inside the icon at (icon_x, art_y) top-left corner.
    icon_x is the left edge of the art bounding box."""
    rows = _ART.get(mod_name, _ART_DEFAULT)
    display.set_pen(WHITE)
    for ri, row_byte in enumerate(rows):
        for ci in range(8):
            if row_byte & (0x80 >> ci):
                display.rectangle(
                    icon_x + ci * ART_SCALE,
                    art_y + ri * ART_SCALE,
                    ART_SCALE, ART_SCALE
                )


def _draw_icon(cx, cy, name, mod_name, color_idx):
    """Draw the icon tile: coloured rounded rect + pixel art + label."""
    ix = cx - ICON_W // 2
    iy = cy - ICON_H // 2
    display.set_pen(_ICON_PENS[color_idx % len(_ICON_PENS)])
    _rrect(ix, iy, ICON_W, ICON_H, ICON_R)

    # Pixel art — centred horizontally, in the upper portion of the tile
    art_x = cx - ART_PX // 2
    art_y = cy + ART_Y_OFF
    _draw_pixel_art(art_x, art_y, mod_name)

    # App name — small text inside the icon below the art.
    # Baseline = art_bottom + gap + cap_height; cap_height ≈ 9 for size 13.
    label = name if len(name) <= 11 else name[:10] + "…"
    display.set_pen(WHITE)
    vector.set_font_size(13)
    tw = int(vector.measure_text(label)[2])
    label_baseline = cy + ART_Y_OFF + ART_PX + 14    # art_bottom + 5px gap + 9px cap
    vector.text(label, cx - tw // 2, label_baseline)


def _draw_status():
    """Top status bar: large time centred, date + temperature below."""
    now = time.localtime()
    display.set_pen(BAR_BG)
    display.rectangle(0, 0, W, BAR_H)
    display.set_pen(ORANGE)
    display.rectangle(0, BAR_H - 2, W, 2)

    # Time — font_size 48, baseline at ~65 so caps sit y≈30..65, centred in bar.
    h_str = "{:02d}:{:02d}".format(now[3], now[4])
    display.set_pen(WHITE)
    _vtext_c(h_str, CX, 65, 48)

    # Date + temperature on one line — font_size 19, baseline ~88.
    DAYS   = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    t_str = "{:.0f}°C".format(_temp) if _temp is not None else "--°C"
    info = "{} {}  ·  {}".format(
        DAYS[now[6]],
        MONTHS[now[1] - 1],
        t_str,
    )
    display.set_pen(DIM)
    _vtext_c(info, CX, 88, 19)


def _draw_grid(apps):
    display.set_pen(BG)
    display.rectangle(0, GRID_TOP, W, H - GRID_TOP)

    start = _page * PER_PAGE
    visible = apps[start: start + PER_PAGE]

    for i, (name, mod) in enumerate(visible):
        col = i % COLS
        row = i // COLS
        cx = col * CELL_W + CELL_W // 2
        cy = GRID_TOP + row * CELL_H + (CELL_H - 20) // 2
        _draw_icon(cx, cy, name, mod, start + i)

    # Page dots
    total = max(1, (len(apps) + PER_PAGE - 1) // PER_PAGE)
    if total > 1:
        dot_x0 = CX - (total * 14) // 2 + 7
        for p in range(total):
            display.set_pen(WHITE if p == _page else DIM)
            display.circle(dot_x0 + p * 14, H - 14, 5 if p == _page else 3)


def _full_redraw(apps):
    display.set_pen(BG)
    display.clear()
    _draw_status()
    _draw_grid(apps)
    presto.update()


# ── Hit testing ────────────────────────────────────────────────────────────

def _hit(tx, ty, apps):
    start = _page * PER_PAGE
    for i, _ in enumerate(apps[start: start + PER_PAGE]):
        col = i % COLS
        row = i // COLS
        cx = col * CELL_W + CELL_W // 2
        cy = GRID_TOP + row * CELL_H + (CELL_H - 20) // 2
        if abs(tx - cx) <= ICON_W // 2 + 12 and abs(ty - cy) <= ICON_H // 2 + 12:
            return start + i
    return None


# ── App launcher ───────────────────────────────────────────────────────────

def _launch(mod_name):
    display.set_pen(BG)
    display.clear()
    display.set_pen(ORANGE)
    _vtext_c("Loading…", CX, H // 2 + 8, 28)
    presto.update()
    gc.collect()
    try:
        sys.modules.pop(mod_name, None)
        __import__(mod_name)
    except Exception as e:
        display.set_pen(BG)
        display.clear()
        display.set_pen(display.create_pen(220, 60, 60))
        _vtext_c(str(e)[:32], CX, H // 2 + 8, 18)
        display.set_pen(DIM)
        _vtext_c("Tap to return", CX, H - 36, 16)
        presto.update()
        time.sleep_ms(400)
        while True:
            touch.poll()
            if touch.state:
                while touch.state:
                    touch.poll()
                break
            time.sleep_ms(30)


# ── Main loop ──────────────────────────────────────────────────────────────

def main():
    global _page

    _msg("Connecting…")
    try:
        presto.connect()
    except Exception as e:
        _msg("WiFi: {}".format(str(e)[:28]))
        time.sleep_ms(2000)

    _msg("Syncing time…")
    _sync_time()

    _msg("Getting weather…")
    _fetch_weather()

    apps = _discover_apps()
    total_pages = max(1, (len(apps) + PER_PAGE - 1) // PER_PAGE)

    _full_redraw(apps)

    last_clock_ms = time.ticks_ms()

    while True:
        # Clock refresh every 30 s (checked while idle)
        now_ms = time.ticks_ms()
        if time.ticks_diff(now_ms, last_clock_ms) >= 30_000:
            _draw_status()
            presto.update()
            last_clock_ms = now_ms

        # Weather refresh every 5 min
        if time.ticks_diff(now_ms, _weather_ms) >= 300_000:
            _fetch_weather()
            _draw_status()
            presto.update()

        touch.poll()
        if not touch.state:
            time.sleep_ms(15)
            continue

        # ── Touch started ────────────────────────────────────────────────
        tx_start, ty_start = touch.x, touch.y
        tx, ty = tx_start, ty_start

        # Track finger until it lifts
        while touch.state:
            touch.poll()
            tx, ty = touch.x, touch.y
            time.sleep_ms(15)

        # ── Touch ended — classify gesture ───────────────────────────────
        dx = tx - tx_start
        dy = ty - ty_start

        if abs(dx) > 40 and abs(dx) > abs(dy):
            # Horizontal swipe → page turn
            if dx < 0 and _page < total_pages - 1:
                _page += 1
                _full_redraw(apps)
            elif dx > 0 and _page > 0:
                _page -= 1
                _full_redraw(apps)
        else:
            # Tap → launch app
            idx = _hit(tx_start, ty_start, apps)
            if idx is not None:
                _, mod = apps[idx]
                _launch(mod)
                _fetch_weather()
                _full_redraw(apps)


main()
