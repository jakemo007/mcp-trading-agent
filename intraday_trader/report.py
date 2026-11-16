"""HTML reports: self-contained, dark theme, inline CSS and inline SVG, no external assets.

Input shapes are in models.py. Every number shown comes from the dicts passed
in; nothing is recomputed from market data here (only sums, ratios and counts
of the values already in those dicts). All inserted text is HTML-escaped. Money
uses a rupee sign and ordinary thousands separators, e.g. "₹102,340.50" (Indian
lakh grouping is not required), with a leading + or - on every P&L figure.

Rendering is defensive: a missing optional key, a None or an empty list shows
a dash or a plain sentence and never raises.

Colours (dark surface #1a1a19) were checked with the dataviz palette validator:
  up / down candles  #0ca30c / #e66767  colour-blind distance in the 6-8 band, so
                     a second cue is always present: up bodies are hollow, down
                     bodies are filled, and every candle carries a hover title
  5m gap / 1h gap    #c98500 / #3987e5  pass every check
Wins, losses, stops and targets never rely on colour alone: each has a word, a
sign or an arrow next to it.
"""
from __future__ import annotations

import html
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from intraday_trader import config, portfolio

# ── Palette (dark only) ───────────────────────────────────────────────────────
PAGE = "#0d0d0d"
SURFACE = "#1a1a19"
INK = "#f0efec"
INK2 = "#c3c2b7"
MUTED = "#898781"
GRID = "#2c2c2a"
AXIS = "#383835"
UP = "#0ca30c"          # up candle, win, target
DOWN = "#e66767"        # down candle, loss, stop
BLUE = "#3987e5"        # equity line, 1h gap
YELLOW = "#c98500"      # 5m gap (the entry zone)
WARN = "#fab219"
FONT = "system-ui,-apple-system,'Segoe UI',Roboto,sans-serif"
DASH = "-"              # shown for a missing value

_IST = timezone(timedelta(hours=5, minutes=30))

LEVEL_WORDS = {
    "PDH": "previous day high", "PDL": "previous day low",
    "PWH": "previous week high", "PWL": "previous week low",
    "ORH": "opening range high", "ORL": "opening range low",
    "HOD": "high of day", "LOD": "low of day",
    "H1_SH": "1h swing high", "H1_SL": "1h swing low",
    "D_SH": "daily swing high", "D_SL": "daily swing low",
    "M5_SH": "5m swing high", "M5_SL": "5m swing low",
}

STATUS_WORDS = {
    "TRADED": "Traded",
    "SIGNAL_NOT_FILLED": "Signal, not filled",
    "NO_SETUP": "No setup",
    "NOT_ALIGNED": "Not aligned",
    "FILLED": "Filled",
    "EXPIRED": "Expired",
    "INVALIDATED": "Invalidated",
    "SKIPPED": "Skipped",
}

EXIT_WORDS = {"TARGET": "Target hit", "STOP": "Stop hit", "EOD": "Square-off at end of day"}

CONFIG_LABELS = [
    ("initial_capital", "Starting capital", "money0"),
    ("max_margin_per_trade", "Own money per trade", "money0"),
    ("leverage", "Intraday leverage", "x"),
    ("charge_per_trade", "Brokerage per completed trade", "money"),
    ("max_open_positions", "Positions open at once", "int"),
    ("max_trades_per_day", "Trades per day", "int"),
    ("daily_loss_limit_pct", "Daily loss limit", "frac"),
    ("min_rvol", "Relative volume to be in play", "x"),
    ("min_rr", "Minimum reward to risk", "r"),
    ("max_rr", "Reward to risk cap", "r"),
    ("min_stop_pct", "Closest stop allowed", "frac"),
    ("max_stop_pct", "Widest stop allowed", "frac"),
    ("entry_window", "Entry window (IST)", "range"),
    ("square_off", "Square-off (IST)", "text"),
    ("scan_checkpoints", "Volume scan checkpoints (IST)", "list"),
    ("allow_short", "Short trades allowed", "bool"),
]


# ── Small formatting helpers ──────────────────────────────────────────────────

def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _f(value: Any) -> Optional[float]:
    """A finite float, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _i(value: Any) -> Optional[int]:
    v = _f(value)
    return None if v is None else int(round(v))


def _price(value: Any) -> str:
    v = _f(value)
    return DASH if v is None else "{:,.2f}".format(v)


def _money(value: Any, signed: bool = False, decimals: int = 2) -> str:
    v = _f(value)
    if v is None:
        return DASH
    body = "₹{:,.{d}f}".format(abs(v), d=decimals)
    if v < 0 and round(abs(v), decimals) > 0:
        return "-" + body
    if signed and round(v, decimals) > 0:
        return "+" + body
    return body


def _pct(value: Any, signed: bool = True) -> str:
    v = _f(value)
    if v is None:
        return DASH
    return ("{:+.2f}%" if signed else "{:.2f}%").format(v)


def _rvol(value: Any) -> str:
    v = _f(value)
    return DASH if v is None else "{:.2f}x".format(v)


def _rmult(value: Any) -> str:
    v = _f(value)
    return DASH if v is None else "{:+.2f}R".format(v)


def _count(value: Any) -> str:
    v = _i(value)
    return DASH if v is None else "{:,d}".format(v)


def _sign_class(value: Any) -> str:
    v = _f(value)
    if v is None or v == 0:
        return "zero"
    return "pos" if v > 0 else "neg"


def _parse_iso(value: Any) -> Optional[datetime]:
    """ISO string -> naive datetime on the IST wall clock (naive input is taken as IST)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_IST).replace(tzinfo=None)
    return dt


def _hhmm(value: Any) -> str:
    dt = _parse_iso(value)
    if dt is not None:
        return dt.strftime("%H:%M")
    if isinstance(value, str) and len(value) == 5 and value[2] == ":":
        return value
    return DASH


def _date_long(value: Any) -> str:
    dt = _parse_iso(value)
    return dt.strftime("%A, %d %b %Y") if dt is not None else (str(value) if value else DASH)


def _date_short(value: Any) -> str:
    dt = _parse_iso(value)
    return dt.strftime("%d %b") if dt is not None else (str(value) if value else DASH)


def _stamp(value: Any) -> str:
    dt = _parse_iso(value)
    return dt.strftime("%Y-%m-%d %H:%M") + " IST" if dt is not None else (str(value) if value else DASH)


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    return value if isinstance(value, list) else []


def _n(value: float) -> str:
    """SVG coordinate: one decimal, never NaN."""
    return "{:.1f}".format(value if math.isfinite(value) else 0.0)


_NARROW = set("ijl.,:;'|!")
_SLIM = set("ftr()[] -")
_WIDE = set("mwMW")


def _tw(text: str, size: float) -> float:
    """Rough rendered width of a string in the system sans (per-character classes)."""
    em = 0.0
    for ch in text:
        if ch in _NARROW:
            em += 0.27
        elif ch in _SLIM:
            em += 0.35
        elif ch in _WIDE:
            em += 0.84
        elif ch.isupper():
            em += 0.66
        elif ch.isdigit():
            em += 0.56
        else:
            em += 0.54
    return em * size


def _nice_ticks(lo: float, hi: float, target: int = 5) -> List[float]:
    span = hi - lo
    if not math.isfinite(span) or span <= 0:
        return [lo]
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = mag
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * mag
        if span / step <= target:
            break
    first = math.ceil(lo / step - 1e-9) * step
    ticks = []
    v = first
    while v <= hi + step * 1e-6 and len(ticks) < 20:
        ticks.append(round(v, 10))
        v += step
    return ticks or [lo]


def _spread(ys: Sequence[float], lo: float, hi: float, gap: float) -> List[float]:
    """Move label positions apart so neighbours are at least `gap` px away, inside [lo, hi]."""
    order = sorted(range(len(ys)), key=lambda k: ys[k])
    out = [0.0] * len(ys)
    prev: Optional[float] = None
    for k in order:
        y = min(max(ys[k], lo), hi)
        if prev is not None:
            y = max(y, prev + gap)
        out[k] = y
        prev = y
    if prev is not None and prev > hi:
        nxt: Optional[float] = None
        for k in reversed(order):
            y = min(out[k], hi) if nxt is None else min(out[k], nxt - gap)
            out[k] = y
            nxt = y
    return out


# ── Words for tags ────────────────────────────────────────────────────────────

def _context_words(context: Any) -> str:
    text = str(context or "")
    if text.startswith("SWEEP"):
        name = text.split(":", 1)[1] if ":" in text else ""
        words = LEVEL_WORDS.get(name)
        if name and words:
            return "Swept {} ({})".format(name, words)
        return "Swept {}".format(name) if name else "Liquidity sweep"
    if text.startswith("H1_FVG"):
        return "Tapped a 1h fair value gap"
    return text or DASH


def _context_kind(contexts: Any) -> str:
    items = [str(c) for c in _l(contexts)]
    sweep = any(c.startswith("SWEEP") for c in items)
    tap = any(c.startswith("H1_FVG") for c in items)
    if sweep and tap:
        return "Sweep and 1h gap"
    if sweep:
        return "Sweep only"
    if tap:
        return "1h gap tap only"
    return "Other"


def _target_words(kind: Any) -> str:
    text = str(kind or "")
    if not text:
        return ""
    if text.startswith("CAP_"):
        return "capped at {}".format(text[4:])
    if text.startswith("FIXED_"):
        return "fixed {}, no level above".format(text[6:])
    return "{}, {}".format(text, LEVEL_WORDS[text]) if text in LEVEL_WORDS else text


def _dir_html(direction: Any) -> str:
    d = str(direction or "").upper()
    if d == "LONG":
        return '<span class="pill dir-long"><span class="g" aria-hidden="true">&#9650;</span> LONG</span>'
    if d == "SHORT":
        return '<span class="pill dir-short"><span class="g" aria-hidden="true">&#9660;</span> SHORT</span>'
    return '<span class="pill">{}</span>'.format(_esc(d or DASH))


def _dir_text(direction: Any) -> str:
    d = str(direction or "").upper()
    if d == "LONG":
        return '<span class="g up" aria-hidden="true">&#9650;</span> Long'
    if d == "SHORT":
        return '<span class="g dn" aria-hidden="true">&#9660;</span> Short'
    return _esc(d or DASH)


def _bias_html(read: Any, with_title: bool = True) -> str:
    r = _d(read)
    bias = str(r.get("bias") or "").upper()
    title = ' title="{}"'.format(_esc(r.get("reason"))) if with_title and r.get("reason") else ""
    if bias == "BULL":
        return '<span class="chip bull"{}><span class="g" aria-hidden="true">&#9650;</span> Bull</span>'.format(title)
    if bias == "BEAR":
        return '<span class="chip bear"{}><span class="g" aria-hidden="true">&#9660;</span> Bear</span>'.format(title)
    if bias == "NEUTRAL":
        return '<span class="chip"{}>Neutral</span>'.format(title)
    return DASH


def _allowed_html(bias: Any) -> str:
    direction = str(_d(bias).get("direction") or "").upper()
    if direction == "LONG":
        return _dir_text("LONG") + "s only"
    if direction == "SHORT":
        return _dir_text("SHORT") + "s only"
    return "Stand aside"


def _outcome_html(trade: dict) -> str:
    outcome = str(trade.get("outcome") or "").upper()
    if not outcome:
        net = _f(trade.get("net_pnl"))
        outcome = "" if net is None else ("WIN" if net > 0 else "LOSS")
    if outcome == "WIN":
        return '<span class="pill win"><span class="g" aria-hidden="true">&#9650;</span> WIN</span>'
    if outcome == "LOSS":
        return '<span class="pill loss"><span class="g" aria-hidden="true">&#9660;</span> LOSS</span>'
    return '<span class="pill">{}</span>'.format(_esc(outcome or DASH))


def _status_html(status: Any) -> str:
    s = str(status or "").upper()
    cls = {"TRADED": "st-traded", "FILLED": "st-traded", "SIGNAL_NOT_FILLED": "st-warn",
           "SKIPPED": "st-warn", "EXPIRED": "", "INVALIDATED": "", "NO_SETUP": "",
           "NOT_ALIGNED": ""}.get(s, "")
    return '<span class="pill {}">{}</span>'.format(cls, _esc(STATUS_WORDS.get(s, s or DASH)))


def _pnl_html(value: Any) -> str:
    return '<span class="{}">{}</span>'.format(_sign_class(value), _esc(_money(value, signed=True)))


# ── Chart: one trade on 5-minute candles ──────────────────────────────────────

def _chart_rows(trade: dict) -> List[Tuple[Optional[datetime], str, float, float, float, float]]:
    rows = []
    for raw in _l(_d(trade.get("chart")).get("bars")):
        if not isinstance(raw, (list, tuple)) or len(raw) < 5:
            continue
        o, h, l, c = _f(raw[1]), _f(raw[2]), _f(raw[3]), _f(raw[4])
        if None in (o, h, l, c):
            continue
        rows.append((_parse_iso(raw[0]), _hhmm(raw[0]), o, max(h, o, c), min(l, o, c), c))
    return rows


def _bar_index(times: Sequence[Optional[datetime]], value: Any) -> Optional[int]:
    """Position of the bar stamped `value`; else the last bar starting before it; else None."""
    t = _parse_iso(value)
    if t is None:
        return None
    best = None
    for k, bt in enumerate(times):
        if bt is None:
            continue
        if bt == t:
            return k
        if bt < t:
            best = k
    return best


def _badge(x: float, y: float, text: str, filled: bool) -> str:
    fill, stroke, ink = (INK, INK, "#0b0b0b") if filled else (SURFACE, INK2, INK)
    return (
        '<circle cx="{x}" cy="{y}" r="10" fill="{s}"/>'
        '<circle cx="{x}" cy="{y}" r="8" fill="{f}" stroke="{st}" stroke-width="1.2"/>'
        '<text x="{x}" y="{ty}" text-anchor="middle" font-size="10" font-weight="700" fill="{ink}">{t}</text>'
    ).format(x=_n(x), y=_n(y), ty=_n(y + 3.5), s=SURFACE, f=fill, st=stroke, ink=ink, t=_esc(text))


def trade_chart_svg(trade: dict, width: int = 760, height: int = 320) -> str:
    """A 5-minute candlestick chart of one trade, as an <svg> string.

    Draws trade["chart"]["bars"] ([[iso, o, h, l, c], ...]); shades the 5m FVG
    (trade["fvg"] top/bottom) from the first of its three candles to the entry
    bar; shades the tapped 1h FVG when trade["h1_fvg"] is set; horizontal lines
    for entry, stop and target with the price as a label; a dashed line for the
    swept level when trade["sweep_level"] is set; numbered badges on the sweep
    or tap bar (1), the displacement candle (2) and the signal bar (3), and
    lettered badges at the entry (E) and the exit (X); a time axis in HH:MM;
    a legend row that names every mark. Up candles are hollow, down candles
    filled. The string is a complete standalone SVG (own background, own
    fonts), so it can also be saved as a .svg file. Returns "" when there are
    no bars.
    """
    trade = _d(trade)
    rows = _chart_rows(trade)
    if not rows:
        return ""
    n = len(rows)
    times = [r[0] for r in rows]
    highs = [r[3] for r in rows]
    lows = [r[4] for r in rows]

    is_short = str(trade.get("direction") or "").upper() == "SHORT"
    entry, stop, target = _f(trade.get("entry")), _f(trade.get("stop")), _f(trade.get("target"))
    exit_price = _f(trade.get("exit_price"))
    fvg, h1, sweep = _d(trade.get("fvg")), _d(trade.get("h1_fvg")), _d(trade.get("sweep_level"))
    fvg_top, fvg_bot = _f(fvg.get("top")), _f(fvg.get("bottom"))
    h1_top, h1_bot = _f(h1.get("top")), _f(h1.get("bottom"))
    sweep_price = _f(sweep.get("price"))
    contexts = [str(c) for c in _l(trade.get("contexts"))]
    has_sweep = any(c.startswith("SWEEP") for c in contexts) or sweep_price is not None
    has_tap = any(c.startswith("H1_FVG") for c in contexts) or (h1_top is not None and h1_bot is not None)

    # Bars the story happens on.
    i_signal = _bar_index(times, trade.get("signal_time"))
    if i_signal is None:
        i_signal = _bar_index(times, fvg.get("created_time"))
    i_entry = _bar_index(times, trade.get("entry_time"))
    i_exit = _bar_index(times, trade.get("exit_time"))
    i_disp = i_signal - 1 if i_signal is not None and i_signal >= 1 else None
    i_ctx = _bar_index(times, trade.get("context_time"))
    if i_ctx is None and i_signal is not None and i_signal >= 1:
        # Not recorded on the trade, so find it: the most extreme bar before the signal that
        # traded through the swept level or into the 1h gap (any bar in the lookback if none did).
        start = max(0, i_signal - config.CONTEXT_LOOKBACK_BARS - 1)
        window = list(range(start, i_signal))
        if is_short:
            edge = min([v for v in (sweep_price, h1_bot) if v is not None], default=None)
            hits = [k for k in window if edge is not None and highs[k] >= edge]
            i_ctx = max(hits or window, key=lambda k: highs[k])
        else:
            edge = max([v for v in (sweep_price, h1_top) if v is not None], default=None)
            hits = [k for k in window if edge is not None and lows[k] <= edge]
            i_ctx = min(hits or window, key=lambda k: lows[k])

    # Legend first: its row count decides the bottom margin.
    legend: List[Tuple[str, str]] = []
    if i_ctx is not None:
        word = "sweep and tap" if (has_sweep and has_tap) else ("tap of 1h gap" if has_tap and not has_sweep else "sweep")
        legend.append(("badge:1", word))
    if i_disp is not None:
        legend.append(("badge:2", "displacement"))
    if i_signal is not None:
        legend.append(("badge:3", "gap complete"))
    if i_entry is not None and entry is not None:
        legend.append(("fbadge:E", "entry"))
    if i_exit is not None and exit_price is not None:
        legend.append(("fbadge:X", "exit"))
    if fvg_top is not None and fvg_bot is not None:
        legend.append(("zone:" + YELLOW, "5m gap"))
    if h1_top is not None and h1_bot is not None:
        legend.append(("zone:" + BLUE, "1h gap"))
    if sweep_price is not None:
        legend.append(("dash", "swept level"))
    legend.append(("up", "up candle"))
    legend.append(("down", "down candle"))

    pad_x = 12.0
    key_w, gap_w, fs = 20.0, 14.0, 10.5
    placed: List[Tuple[int, float, str, str]] = []
    row, cursor = 0, pad_x
    for kind, label in legend:
        w = key_w + _tw(label, fs)
        if cursor + w > width - pad_x and cursor > pad_x:
            row += 1
            cursor = pad_x
        placed.append((row, cursor, kind, label))
        cursor += w + gap_w
    legend_rows = row + 1

    ml, mr, mt = 60.0, 134.0, 16.0
    mb = 24.0 + legend_rows * 17.0 + 8.0
    pw, ph = width - ml - mr, height - mt - mb
    if pw <= 20 or ph <= 20:
        return ""
    x0, x1, y0, y1 = ml, ml + pw, mt, mt + ph

    # Price scale: everything that must be visible, then padding.
    core = highs + lows + [v for v in (entry, stop, target, exit_price, fvg_top, fvg_bot, sweep_price) if v is not None]
    lo, hi = min(core), max(core)
    span = hi - lo
    if span <= 0:
        span = max(abs(hi) * 0.005, 0.01)
    for v in (h1_top, h1_bot):
        if v is not None and lo - 0.5 * span <= v <= hi + 0.5 * span:
            lo, hi = min(lo, v), max(hi, v)
    span = max(hi - lo, 1e-9)
    lo, hi = lo - span * 0.10, hi + span * 0.10
    span = hi - lo

    def py(v: float) -> float:
        return y0 + (hi - v) / span * ph

    slot = pw / n

    def px(k: float) -> float:
        return x0 + slot * (k + 0.5)

    body_w = min(max(slot * 0.62, 1.5), 12.0)

    symbol = str(trade.get("symbol") or "trade")
    direction_word = "short" if is_short else "long"
    title = "{} {} trade on 5-minute candles".format(symbol, direction_word)
    desc = "Entry {}, stop {}, target {}, exit {} ({}).".format(
        _price(entry), _price(stop), _price(target), _price(exit_price),
        EXIT_WORDS.get(str(trade.get("exit_reason") or "").upper(), "exit reason not recorded"))

    out: List[str] = []
    out.append(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="100%" role="img" '
        'aria-label="{t}" font-family="{font}" style="display:block;max-width:100%;height:auto">'
        .format(w=width, h=height, t=_esc(title), font=FONT))
    out.append("<title>{}</title><desc>{}</desc>".format(_esc(title), _esc(desc)))
    out.append('<rect x="0" y="0" width="{}" height="{}" rx="10" fill="{}"/>'.format(width, height, SURFACE))

    # Grid and price axis.
    for tick in _nice_ticks(lo + span * 0.04, hi - span * 0.04, 5):
        ty = py(tick)
        out.append('<line x1="{}" y1="{y}" x2="{}" y2="{y}" stroke="{}" stroke-width="1"/>'.format(
            _n(x0), _n(x1), GRID, y=_n(ty)))
        out.append('<text x="{}" y="{}" text-anchor="end" font-size="10.5" fill="{}">{}</text>'.format(
            _n(x0 - 8), _n(ty + 3.5), MUTED, _esc(_price(tick))))
    out.append('<line x1="{}" y1="{y}" x2="{}" y2="{y}" stroke="{}" stroke-width="1"/>'.format(
        _n(x0), _n(x1), AXIS, y=_n(y1)))

    # Time axis: a label about every 6 bars, more sparsely when bars are narrow.
    step = 6
    while slot * step < 46 and step < n:
        step += 6
    first_t = next((t for t in times if t is not None), None)
    for k in range(n):
        t = times[k]
        if t is not None and first_t is not None:
            bars_from_open = ((t.hour * 60 + t.minute) - (9 * 60 + 15)) // 5
            on_tick = bars_from_open % step == 0
        else:
            on_tick = k % step == 0
        if not on_tick or rows[k][1] == DASH:
            continue
        tx = px(k)
        if tx < x0 + 14 or tx > x1 - 14:
            continue
        out.append('<line x1="{x}" y1="{}" x2="{x}" y2="{}" stroke="{}" stroke-width="1"/>'.format(
            _n(y1), _n(y1 + 4), AXIS, x=_n(tx)))
        out.append('<text x="{}" y="{}" text-anchor="middle" font-size="10.5" fill="{}">{}</text>'.format(
            _n(tx), _n(y1 + 16), MUTED, _esc(rows[k][1])))

    def clamp_y(v: float) -> float:
        return min(max(v, y0), y1)

    # 1h gap: a standing zone across the whole plot.
    if h1_top is not None and h1_bot is not None:
        za, zb = clamp_y(py(max(h1_top, h1_bot))), clamp_y(py(min(h1_top, h1_bot)))
        if zb - za >= 0.5:
            out.append(
                '<g><title>1h fair value gap {} to {}</title>'
                '<rect x="{}" y="{}" width="{}" height="{}" fill="{}" fill-opacity="0.16"/>'
                '<line x1="{x0}" y1="{ya}" x2="{x1}" y2="{ya}" stroke="{c}" stroke-opacity="0.55" stroke-width="1"/>'
                '<line x1="{x0}" y1="{yb}" x2="{x1}" y2="{yb}" stroke="{c}" stroke-opacity="0.55" stroke-width="1"/></g>'
                .format(_esc(_price(min(h1_top, h1_bot))), _esc(_price(max(h1_top, h1_bot))),
                        _n(x0), _n(za), _n(pw), _n(zb - za), BLUE,
                        x0=_n(x0), x1=_n(x1), ya=_n(za), yb=_n(zb), c=BLUE))

    # 5m gap: from the first of its three candles to the entry bar.
    if fvg_top is not None and fvg_bot is not None:
        i_created = _bar_index(times, fvg.get("created_time"))
        if i_created is None:
            i_created = i_signal
        if i_created is not None:
            left = px(max(i_created - 2, 0))
            last = i_entry if (i_entry is not None and i_entry >= i_created) else min(i_created + 3, n - 1)
            right = min(x0 + slot * (last + 1), x1)
            right = max(right, min(left + slot * 3, x1))
            za, zb = clamp_y(py(max(fvg_top, fvg_bot))), clamp_y(py(min(fvg_top, fvg_bot)))
            zh = max(zb - za, 1.5)
            out.append(
                '<g><title>5m fair value gap {} to {} (the entry zone)</title>'
                '<rect x="{}" y="{}" width="{}" height="{}" fill="{}" fill-opacity="0.30"/>'
                '<rect x="{}" y="{}" width="{}" height="{}" fill="none" stroke="{}" stroke-opacity="0.9" stroke-width="1"/></g>'
                .format(_esc(_price(min(fvg_top, fvg_bot))), _esc(_price(max(fvg_top, fvg_bot))),
                        _n(left), _n(za), _n(right - left), _n(zh), YELLOW,
                        _n(left), _n(za), _n(right - left), _n(zh), YELLOW))

    # Swept level: dashed, labelled on the side away from price.
    if sweep_price is not None and lo <= sweep_price <= hi:
        sy = py(sweep_price)
        name = str(sweep.get("name") or "level")
        label = "{} {} swept".format(name, _price(sweep_price))
        ly = sy - 5 if is_short else sy + 12
        ly = min(max(ly, y0 + 10), y1 - 4)
        out.append('<line x1="{}" y1="{y}" x2="{}" y2="{y}" stroke="{}" stroke-width="1.2" stroke-dasharray="5 4"/>'.format(
            _n(x0), _n(x1), INK2, y=_n(sy)))
        out.append('<text x="{}" y="{}" font-size="10.5" fill="{}">{}</text>'.format(
            _n(x0 + 6), _n(ly), INK2, _esc(label)))

    # Entry, stop and target lines, from the signal bar to the right edge.
    line_from = px(i_signal) if i_signal is not None else x0
    marks: List[Tuple[str, float, str]] = []
    if target is not None:
        marks.append(("Target", target, UP))
    if entry is not None:
        marks.append(("Entry", entry, INK))
    if stop is not None:
        marks.append(("Stop", stop, DOWN))
    if exit_price is not None and all(abs(exit_price - m[1]) > span * 0.004 for m in marks if m[0] != "Entry"):
        marks.append(("Exit", exit_price, MUTED))
    for name, price, colour in marks:
        if name == "Exit" or not (lo <= price <= hi):
            continue
        ly = py(price)
        out.append('<line x1="{}" y1="{y}" x2="{}" y2="{y}" stroke="{}" stroke-width="1.5"/>'.format(
            _n(line_from), _n(x1), colour, y=_n(ly)))

    # Candles: hollow body = up, filled body = down. Each has a hover title.
    for k, (_, label, o, h, l, c) in enumerate(rows):
        cx = px(k)
        up = c > o
        flat = c == o
        colour = MUTED if flat else (UP if up else DOWN)
        top, bot = py(max(o, c)), py(min(o, c))
        bh = max(bot - top, 1.0)
        word = "flat" if flat else ("up" if up else "down")
        tip = "{}  O {}  H {}  L {}  C {}  ({})".format(label, _price(o), _price(h), _price(l), _price(c), word)
        out.append('<g><title>{}</title>'.format(_esc(tip)))
        out.append('<rect x="{}" y="{}" width="{}" height="{}" fill="{}" fill-opacity="0"/>'.format(
            _n(cx - slot / 2), _n(y0), _n(slot), _n(ph), SURFACE))
        out.append('<line x1="{x}" y1="{}" x2="{x}" y2="{}" stroke="{}" stroke-width="1"/>'.format(
            _n(py(h)), _n(top), colour, x=_n(cx)))
        out.append('<line x1="{x}" y1="{}" x2="{x}" y2="{}" stroke="{}" stroke-width="1"/>'.format(
            _n(top + bh), _n(py(l)), colour, x=_n(cx)))
        if up and body_w >= 3:
            out.append('<rect x="{}" y="{}" width="{}" height="{}" fill="{}" stroke="{}" stroke-width="1.2"/>'.format(
                _n(cx - body_w / 2 + 0.6), _n(top + 0.6), _n(max(body_w - 1.2, 0.6)), _n(max(bh - 1.2, 0.4)),
                SURFACE, colour))
        else:
            out.append('<rect x="{}" y="{}" width="{}" height="{}" fill="{}"/>'.format(
                _n(cx - body_w / 2), _n(top), _n(body_w), _n(bh), colour))
        out.append("</g>")

    # Badges. 1-3 sit outside the candles on the side the move came from.
    def clamp_badge(v: float) -> float:
        return min(max(v, y0 + 10), y1 - 10)

    def leader(xa: float, ya: float, yb: float) -> str:
        return '<line x1="{x}" y1="{}" x2="{x}" y2="{}" stroke="{}" stroke-width="1"/>'.format(
            _n(ya), _n(yb), MUTED, x=_n(xa))

    badges: List[str] = []
    if i_ctx is not None:
        edge = py(highs[i_ctx]) if is_short else py(lows[i_ctx])
        by = clamp_badge(edge - 14 if is_short else edge + 14)
        badges.append(_badge(px(i_ctx), by, "1", False))
    y_two: Optional[float] = None
    if i_disp is not None:
        edge = py(lows[i_disp]) if is_short else py(highs[i_disp])
        y_two = clamp_badge(edge + 14 if is_short else edge - 14)   # opposite side to badge 1
        badges.append(_badge(px(i_disp), y_two, "2", False))
    if i_signal is not None:
        edge = py(lows[i_signal]) if is_short else py(highs[i_signal])
        by = edge + 14 if is_short else edge - 14
        if y_two is not None and abs(by - y_two) < 19 and slot < 21:
            by = y_two + 19 if is_short else y_two - 19
        by = clamp_badge(by)
        if y_two is not None and abs(by - y_two) < 19 and slot < 21:
            # No room further out: go the other way from badge 2.
            by = clamp_badge(y_two - 19 if is_short else y_two + 19)
        badges.append(leader(px(i_signal), edge, by))
        badges.append(_badge(px(i_signal), by, "3", False))
    ex, ey = (px(i_entry), py(entry)) if (i_entry is not None and entry is not None) else (None, None)
    xx, xy = (px(i_exit), py(exit_price)) if (i_exit is not None and exit_price is not None) else (None, None)
    if ex is not None and xx is not None and abs(ex - xx) < 18 and abs(ey - xy) < 18:
        ex, xx = ex - 10, xx + 10
    if ex is not None:
        badges.append(_badge(ex, clamp_badge(ey), "E", True))
    if xx is not None:
        badges.append(_badge(xx, clamp_badge(xy), "X", True))
    out.extend(badges)

    # Right-hand labels, nudged apart when prices are close.
    visible = [m for m in marks if lo <= m[1] <= hi]
    if visible:
        true_y = [py(m[1]) for m in visible]
        label_y = _spread(true_y, y0 + 8, y1 - 4, 15.0)
        for (name, price, colour), ty, ly in zip(visible, true_y, label_y):
            start_x = px(i_exit) + 9 if (name == "Exit" and i_exit is not None) else x1
            start_x = min(start_x, x1)
            out.append('<polyline points="{},{} {},{} {},{}" fill="none" stroke="{}" stroke-width="1.5"/>'.format(
                _n(start_x), _n(ty), _n(x1 + 10), _n(ly), _n(x1 + 18), _n(ly), colour))
            out.append('<text x="{}" y="{}" font-size="11.5" fill="{}"><tspan font-weight="600">{}</tspan> {}</text>'.format(
                _n(x1 + 22), _n(ly + 4), INK, _esc(name), _esc(_price(price))))

    # Legend row(s): the caption that names every mark.
    base = y1 + 24.0
    for row_k, lx, kind, label in placed:
        cy = base + row_k * 17.0 + 9.0
        kx = lx + 8.0
        if kind.startswith("badge:") or kind.startswith("fbadge:"):
            filled = kind.startswith("fbadge:")
            fill, stroke, ink = (INK, INK, "#0b0b0b") if filled else (SURFACE, INK2, INK)
            out.append('<circle cx="{}" cy="{}" r="7" fill="{}" stroke="{}" stroke-width="1.1"/>'.format(
                _n(kx), _n(cy), fill, stroke))
            out.append('<text x="{}" y="{}" text-anchor="middle" font-size="9" font-weight="700" fill="{}">{}</text>'.format(
                _n(kx), _n(cy + 3.2), ink, _esc(kind.split(":", 1)[1])))
        elif kind.startswith("zone:"):
            colour = kind.split(":", 1)[1]
            out.append('<rect x="{}" y="{}" width="14" height="9" fill="{c}" fill-opacity="0.30" stroke="{c}" stroke-width="1"/>'.format(
                _n(kx - 7), _n(cy - 4.5), c=colour))
        elif kind == "dash":
            out.append('<line x1="{}" y1="{y}" x2="{}" y2="{y}" stroke="{}" stroke-width="1.2" stroke-dasharray="4 3"/>'.format(
                _n(kx - 8), _n(kx + 8), INK2, y=_n(cy)))
        elif kind == "up":
            out.append('<line x1="{x}" y1="{}" x2="{x}" y2="{}" stroke="{}" stroke-width="1"/>'.format(
                _n(cy - 7), _n(cy + 7), UP, x=_n(kx)))
            out.append('<rect x="{}" y="{}" width="6" height="8" fill="{}" stroke="{}" stroke-width="1.2"/>'.format(
                _n(kx - 3), _n(cy - 4), SURFACE, UP))
        elif kind == "down":
            out.append('<line x1="{x}" y1="{}" x2="{x}" y2="{}" stroke="{}" stroke-width="1"/>'.format(
                _n(cy - 7), _n(cy + 7), DOWN, x=_n(kx)))
            out.append('<rect x="{}" y="{}" width="7" height="9" fill="{}"/>'.format(_n(kx - 3.5), _n(cy - 4.5), DOWN))
        out.append('<text x="{}" y="{}" font-size="{}" fill="{}">{}</text>'.format(
            _n(lx + key_w), _n(cy + 3.6), fs, INK2, _esc(label)))

    out.append("</svg>")
    return "".join(out)


# ── Chart: capital over time ──────────────────────────────────────────────────

def _axis_money(value: float, step: float) -> str:
    if step >= 1000 and abs(value) >= 10000:
        return "₹{:,.0f}K".format(value / 1000.0)
    if step >= 100 and abs(value) >= 10000:
        return "₹{:,.1f}K".format(value / 1000.0)
    return "₹{:,.0f}".format(value)


def equity_curve_svg(days: List[dict], initial_capital: float, width: int = 900, height: int = 280) -> str:
    """End-of-day capital as a line, in INR.

    A dashed baseline at initial_capital, one mark per day (an up triangle
    when that day's net_pnl > 0, a down triangle when < 0, a circle when 0;
    green, red and grey as a second cue), the last value labelled. `days` are
    PortfolioState["days"] entries. Beyond 60 sessions the per-day marks are
    dropped and only the line and the last value remain. Returns "" when empty.
    """
    pts: List[Tuple[dict, float]] = []
    for day in _l(days):
        d = _d(day)
        cap = _f(d.get("end_capital"))
        if cap is not None:
            pts.append((d, cap))
    if not pts:
        return ""
    initial = _f(initial_capital)
    if initial is None:
        initial = _f(pts[0][0].get("start_capital"))
    if initial is None:
        initial = pts[0][1]
    n = len(pts)
    show_marks = n <= 60

    ml, mr, mt, mb = 66.0, 108.0, 34.0 if show_marks else 18.0, 34.0
    pw, ph = width - ml - mr, height - mt - mb
    if pw <= 20 or ph <= 20:
        return ""
    x0, x1, y0, y1 = ml, ml + pw, mt, mt + ph

    values = [initial] + [p[1] for p in pts]
    lo, hi = min(values), max(values)
    span = hi - lo
    if span <= 0:
        span = max(abs(hi) * 0.01, 1.0)
        lo, hi = lo - span / 2, hi + span / 2
    lo, hi = lo - span * 0.12, hi + span * 0.12
    span = hi - lo

    def py(v: float) -> float:
        return y0 + (hi - v) / span * ph

    def px(k: int) -> float:  # k = 0 is the starting capital, k = 1..n the sessions
        return x0 + pw * k / n

    first, last = pts[0][0].get("date"), pts[-1][0].get("date")
    title = "Paper capital at the close of each session"
    desc = "{} sessions from {} to {}. Started at {}, now {}.".format(
        n, first or DASH, last or DASH, _money(initial), _money(pts[-1][1]))

    out: List[str] = []
    out.append(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="100%" role="img" '
        'aria-label="{t}" font-family="{font}" style="display:block;max-width:100%;height:auto">'
        .format(w=width, h=height, t=_esc(title), font=FONT))
    out.append("<title>{}</title><desc>{}</desc>".format(_esc(title), _esc(desc)))
    out.append('<rect x="0" y="0" width="{}" height="{}" rx="10" fill="{}"/>'.format(width, height, SURFACE))

    ticks = _nice_ticks(lo + span * 0.04, hi - span * 0.04, 4)
    tick_step = (ticks[1] - ticks[0]) if len(ticks) > 1 else span
    for tick in ticks:
        ty = py(tick)
        out.append('<line x1="{}" y1="{y}" x2="{}" y2="{y}" stroke="{}" stroke-width="1"/>'.format(
            _n(x0), _n(x1), GRID, y=_n(ty)))
        out.append('<text x="{}" y="{}" text-anchor="end" font-size="10.5" fill="{}">{}</text>'.format(
            _n(x0 - 8), _n(ty + 3.5), MUTED, _esc(_axis_money(tick, tick_step))))
    out.append('<line x1="{}" y1="{y}" x2="{}" y2="{y}" stroke="{}" stroke-width="1"/>'.format(
        _n(x0), _n(x1), AXIS, y=_n(y1)))

    # Date labels: at most 8, never closer than about 60px.
    every = max(1, int(math.ceil(n / 8.0)))
    while pw / n * every < 60 and every < n:
        every += 1
    for k in range(1, n + 1):
        if (k - 1) % every and k != n:
            continue
        if k == n and (n - 1) % every and pw / n * ((n - 1) % every) < 46:
            continue  # the last date would sit on top of the previous label
        tx = px(k)
        out.append('<text x="{}" y="{}" text-anchor="{}" font-size="10.5" fill="{}">{}</text>'.format(
            _n(tx), _n(y1 + 17), "end" if k == n and n > 1 else "middle", MUTED,
            _esc(_date_short(pts[k - 1][0].get("date")))))

    # Baseline at the starting capital (a reference line, so dashed).
    by = py(initial)
    out.append('<line x1="{}" y1="{y}" x2="{}" y2="{y}" stroke="{}" stroke-width="1.2" stroke-dasharray="5 4"/>'.format(
        _n(x0), _n(x1), MUTED, y=_n(by)))
    # The line leaves the baseline at the left edge, so the label goes on the other side of it.
    label_above = pts[0][1] < initial
    if label_above and by - 5 < y0 + 8:
        label_above = False
    if not label_above and by + 13 > y1 - 2:
        label_above = True
    out.append('<text x="{}" y="{}" font-size="10.5" fill="{}">Start {}</text>'.format(
        _n(x0 + 6), _n(by - 5 if label_above else by + 13), INK2, _esc(_money(initial, decimals=0))))

    coords = [(px(0), by)] + [(px(k), py(pts[k - 1][1])) for k in range(1, n + 1)]
    out.append('<polyline points="{}" fill="none" stroke="{}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'.format(
        " ".join("{},{}".format(_n(cx), _n(cy)) for cx, cy in coords), BLUE))

    if show_marks:
        size = 5.5
        for k in range(1, n + 1):
            day, cap = pts[k - 1]
            cx, cy = coords[k]
            net = _f(day.get("net_pnl"))
            trades = _i(day.get("n_trades"))
            tip = "{}: capital {}, day {}{}".format(
                _date_long(day.get("date")), _money(cap), _money(net, signed=True),
                "" if trades is None else ", {} trade{}".format(trades, "" if trades == 1 else "s"))
            out.append("<g><title>{}</title>".format(_esc(tip)))
            out.append('<circle cx="{}" cy="{}" r="12" fill="{}" fill-opacity="0"/>'.format(_n(cx), _n(cy), SURFACE))
            if net is not None and net > 0:
                pts_s = "{},{} {},{} {},{}".format(_n(cx), _n(cy - size - 1), _n(cx + size + 0.5), _n(cy + size - 1),
                                                   _n(cx - size - 0.5), _n(cy + size - 1))
                out.append('<polygon points="{}" fill="{}" stroke="{}" stroke-width="2" stroke-linejoin="round"/>'.format(
                    pts_s, UP, SURFACE))
            elif net is not None and net < 0:
                pts_s = "{},{} {},{} {},{}".format(_n(cx), _n(cy + size + 1), _n(cx + size + 0.5), _n(cy - size + 1),
                                                   _n(cx - size - 0.5), _n(cy - size + 1))
                out.append('<polygon points="{}" fill="{}" stroke="{}" stroke-width="2" stroke-linejoin="round"/>'.format(
                    pts_s, DOWN, SURFACE))
            else:
                out.append('<circle cx="{}" cy="{}" r="4.5" fill="{}" stroke="{}" stroke-width="2"/>'.format(
                    _n(cx), _n(cy), MUTED, SURFACE))
            out.append("</g>")
        # Legend for the mark shapes.
        lx, ly = x0, 15.0
        for shape, colour, label in (("up", UP, "day closed up"), ("down", DOWN, "day closed down"), ("flat", MUTED, "flat day")):
            if shape == "up":
                out.append('<polygon points="{},{} {},{} {},{}" fill="{}"/>'.format(
                    _n(lx + 5), _n(ly - 5), _n(lx + 10), _n(ly + 4), _n(lx), _n(ly + 4), colour))
            elif shape == "down":
                out.append('<polygon points="{},{} {},{} {},{}" fill="{}"/>'.format(
                    _n(lx + 5), _n(ly + 5), _n(lx + 10), _n(ly - 4), _n(lx), _n(ly - 4), colour))
            else:
                out.append('<circle cx="{}" cy="{}" r="4.5" fill="{}"/>'.format(_n(lx + 5), _n(ly), colour))
            out.append('<text x="{}" y="{}" font-size="10.5" fill="{}">{}</text>'.format(
                _n(lx + 16), _n(ly + 3.6), INK2, _esc(label)))
            lx += 16 + _tw(label, 10.5) + 18
    else:
        cx, cy = coords[-1]
        out.append('<circle cx="{}" cy="{}" r="4.5" fill="{}" stroke="{}" stroke-width="2"/>'.format(
            _n(cx), _n(cy), BLUE, SURFACE))

    lx, ly = coords[-1]
    out.append('<text x="{}" y="{}" font-size="12" font-weight="600" fill="{}">{}</text>'.format(
        _n(lx + 12), _n(min(max(ly + 4, y0 + 10), y1 - 2)), INK, _esc(_money(pts[-1][1]))))
    out.append("</svg>")
    return "".join(out)


# ── HTML building blocks ──────────────────────────────────────────────────────

_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:#0d0d0d;color:#f0efec;font:15px/1.55 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}
.page{max-width:1100px;margin:0 auto;padding:28px 16px 56px}
h1{font-size:1.55rem;line-height:1.25;margin:0 0 4px;font-weight:650;letter-spacing:-.01em}
h2{font-size:1.1rem;margin:40px 0 6px;font-weight:650}
h3{font-size:1rem;margin:0;font-weight:650}
h4{font-size:.8rem;margin:18px 0 8px;font-weight:600;color:#c3c2b7;text-transform:uppercase;letter-spacing:.06em}
p{margin:0 0 10px}
.sub{color:#c3c2b7;font-size:.9rem}
.lead{color:#c3c2b7;font-size:.92rem;margin:0 0 14px;max-width:72ch}
.banner{margin-top:14px;padding:10px 14px;border:1px solid #fab219;border-left-width:4px;border-radius:8px;background:rgba(250,178,25,.08)}
.banner b{font-weight:650}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:22px}
h4.grp{margin:22px 0 8px}
h4.grp+.tiles{margin-top:0}
.tile{background:#1a1a19;border:1px solid rgba(255,255,255,.10);border-radius:10px;padding:14px 16px;min-width:0}
.tile .label{color:#c3c2b7;font-size:.8rem}
.tile .value{font-size:1.4rem;font-weight:650;line-height:1.25;margin-top:2px;overflow-wrap:anywhere}
.tile .delta{color:#c3c2b7;font-size:.82rem;margin-top:2px}
.pos{color:#3fc23f}.neg{color:#ee8282}.zero{color:#f0efec}
.card{background:#1a1a19;border:1px solid rgba(255,255,255,.10);border-radius:10px;padding:18px;margin:14px 0}
.muted{color:#898781}.dim{color:#c3c2b7}
.steps{list-style:none;margin:0;padding:0;display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.steps li{background:#1a1a19;border:1px solid rgba(255,255,255,.10);border-radius:10px;padding:14px 16px}
.steps .n{display:inline-block;width:22px;height:22px;line-height:20px;text-align:center;border:1px solid #c3c2b7;border-radius:50%;font-size:.78rem;font-weight:700;margin-right:8px}
.steps .t{font-weight:650}
.steps p{color:#c3c2b7;font-size:.9rem;margin:8px 0 0}
.steps b{color:#f0efec;font-weight:650}
.notes{margin:14px 0 0;padding-left:20px;color:#c3c2b7;font-size:.9rem}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch;background:#1a1a19;border:1px solid rgba(255,255,255,.10);border-radius:10px;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:.86rem;font-variant-numeric:tabular-nums}
th{text-align:left;color:#c3c2b7;font-weight:600;padding:10px 12px;border-bottom:1px solid #383835;vertical-align:bottom;line-height:1.3}
td{padding:9px 12px;border-bottom:1px solid #2c2c2a;white-space:nowrap;vertical-align:top}
tr:last-child td{border-bottom:none}
th.num,td.num{text-align:right}
td.wrap{white-space:normal;min-width:170px;color:#c3c2b7}
.tight th,.tight td{padding:8px 9px}
.compact th,.compact td{padding:8px 10px}
.compact td:first-child{white-space:normal}
td .small{display:block;color:#898781;font-size:.76rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:150px}
td.num .small{margin-left:auto}
.pill{display:inline-block;padding:1px 9px;border-radius:999px;border:1px solid #55544f;font-size:.74rem;font-weight:650;letter-spacing:.02em;white-space:nowrap;color:#f0efec}
.pill .g,.chip .g{font-size:.62rem;vertical-align:1px}
.pill.win,.pill.st-traded{border-color:#0ca30c;background:rgba(12,163,12,.16)}
.pill.win .g,.pill.dir-long .g,.chip.bull .g,.g.up{color:#3fc23f}
.pill.loss{border-color:#e66767;background:rgba(230,103,103,.16)}
.pill.loss .g,.pill.dir-short .g,.chip.bear .g,.g.dn{color:#ee8282}
.pill.st-warn{border-color:#fab219;background:rgba(250,178,25,.10)}
.chip{display:inline-block;font-size:.8rem;font-weight:600;white-space:nowrap}
.trade-head{display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center;justify-content:space-between}
.trade-head .who{display:flex;flex-wrap:wrap;gap:8px;align-items:center;min-width:0}
.trade-head .sym{font-size:1.15rem;font-weight:700}
.trade-head .name{color:#c3c2b7;font-size:.88rem}
.trade-head .res{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;font-weight:650}
.trade-head .res .big{font-size:1.15rem}
.why{color:#c3c2b7;font-size:.9rem;margin:10px 0 0}
.reads{display:grid;gap:6px;margin:0}
.read{display:grid;grid-template-columns:64px 78px 1fr;gap:8px;align-items:baseline;font-size:.9rem}
.read .tf{color:#c3c2b7}
.read .txt{color:#c3c2b7;min-width:0;overflow-wrap:anywhere}
.verdict{margin-top:8px;font-size:.9rem}
.story{margin:0;padding-left:22px}
.story li{margin:0 0 5px;padding-left:2px;overflow-wrap:anywhere}
.chart{margin:0;overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:10px}
.chart svg{min-width:700px}
.chart.wide svg{min-width:720px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;margin:0;background:#1a1a19;border:1px solid #2c2c2a;border-radius:8px;overflow:hidden;font-variant-numeric:tabular-nums}
.kv div{background:#1a1a19;padding:9px 12px;min-width:0;box-shadow:1px 0 0 #2c2c2a,0 1px 0 #2c2c2a}
.kv dt{color:#c3c2b7;font-size:.76rem}
.kv dd{margin:1px 0 0;font-weight:600;overflow-wrap:anywhere}
.kv dd .small{display:block;color:#898781;font-size:.76rem;font-weight:400}
.rules{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:1px;margin:10px 0 0;background:#1a1a19;border:1px solid #2c2c2a;border-radius:8px;overflow:hidden}
.rules div{background:#1a1a19;padding:9px 12px;box-shadow:1px 0 0 #2c2c2a,0 1px 0 #2c2c2a}
.rules dt{color:#c3c2b7;font-size:.78rem}
.rules dd{margin:1px 0 0;font-weight:600;overflow-wrap:anywhere}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,430px),1fr));gap:0 16px}
.grid2 h3{margin:18px 0 0;font-size:.92rem}
.disclaimer{margin-top:36px;padding-top:14px;border-top:1px solid #2c2c2a;color:#898781;font-size:.82rem}
@media (max-width:520px){
  .page{padding:20px 12px 44px}
  h1{font-size:1.3rem}
  .card{padding:14px}
  .read{grid-template-columns:58px 1fr}
  .read .txt{grid-column:1 / -1}
  .tile .value{font-size:1.2rem}
}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        "<title>{}</title>\n<style>{}</style>\n</head>\n<body>\n<div class=\"page\">\n{}\n</div>\n</body>\n</html>\n"
    ).format(_esc(title), _CSS, body)


def _tile(label: str, value_html: str, delta_html: str = "") -> str:
    delta = '<div class="delta">{}</div>'.format(delta_html) if delta_html else ""
    return '<div class="tile"><div class="label">{}</div><div class="value">{}</div>{}</div>'.format(
        _esc(label), value_html, delta)


def _table(headers: Sequence[Tuple[str, str]], rows: Sequence[Sequence[Tuple[str, str]]], empty: str,
           css: str = "") -> str:
    """headers: (text, css class). rows: cells as (html, css class). Scrolls sideways on its own."""
    head = "".join('<th scope="col"{}>{}</th>'.format(' class="{}"'.format(c) if c else "", _esc(t)) for t, c in headers)
    if rows:
        body = "".join(
            "<tr>{}</tr>".format("".join('<td{}>{}</td>'.format(' class="{}"'.format(c) if c else "", h) for h, c in row))
            for row in rows)
    else:
        body = '<tr><td colspan="{}" class="wrap">{}</td></tr>'.format(len(headers), _esc(empty))
    return '<div class="tablewrap{}"><table><thead><tr>{}</tr></thead><tbody>{}</tbody></table></div>'.format(
        " " + css if css else "", head, body)


def _plural(count: Any, word: str, plural: Optional[str] = None) -> str:
    c = _i(count) or 0
    return "{:,d} {}".format(c, word if c == 1 else (plural or word + "s"))


def _summary(state: dict) -> dict:
    return _d(portfolio.summarize(_d(state)))


def _win_rate(wins: Any, total: Any) -> Optional[float]:
    w, t = _f(wins), _f(total)
    if w is None or not t:
        return None
    return w / t * 100.0


def _config_html(cfg: Any) -> str:
    cfg = _d(cfg)
    if not cfg:
        return '<p class="muted">The settings were not recorded with this result.</p>'
    cells = []
    known = set()
    for key, label, kind in CONFIG_LABELS:
        if key not in cfg:
            continue
        known.add(key)
        raw = cfg.get(key)
        v = _f(raw)
        if kind == "money0":
            text = _money(raw, decimals=0)
        elif kind == "money":
            text = _money(raw)
        elif kind == "x":
            text = DASH if v is None else "{:g}x".format(v)
        elif kind == "r":
            text = DASH if v is None else "{:g}R".format(v)
        elif kind == "frac":
            text = DASH if v is None else "{:.2f}%".format(v * 100.0)
        elif kind == "int":
            text = _count(raw)
        elif kind == "range":
            items = [str(x) for x in _l(raw)]
            text = " to ".join(items) if items else DASH
        elif kind == "list":
            items = [str(x) for x in _l(raw)]
            text = ", ".join(items) if items else DASH
        elif kind == "bool":
            text = "Yes" if raw is True else ("No" if raw is False else DASH)
        else:
            text = str(raw) if raw not in (None, "") else DASH
        cells.append("<div><dt>{}</dt><dd>{}</dd></div>".format(_esc(label), _esc(text)))
    for key in sorted(k for k in cfg if k not in known):
        raw = cfg.get(key)
        text = ", ".join(str(x) for x in raw) if isinstance(raw, (list, tuple)) else str(raw)
        cells.append("<div><dt>{}</dt><dd>{}</dd></div>".format(_esc(str(key).replace("_", " ")), _esc(text)))
    return '<dl class="rules">{}</dl>'.format("".join(cells))


def _trade_card(trade: dict, heading: str = "h3") -> str:
    """One trade as a short lesson: header, top-down reads, story, chart, money."""
    t = _d(trade)
    bias = _d(t.get("bias"))
    name = t.get("name")
    name_html = '<span class="name">{}</span>'.format(_esc(name)) if name and name != t.get("symbol") else ""
    date_html = '<span class="name">{}</span>'.format(_esc(_date_short(t.get("date")))) if heading != "h3" and t.get("date") else ""
    head = (
        '<header class="trade-head"><div class="who"><{h} class="sym">{sym}</{h}>{name}{date}{dir}</div>'
        '<div class="res">{out}<span class="big {cls}">{net}</span><span class="dim">{r}</span></div></header>'
    ).format(h=heading, sym=_esc(t.get("symbol") or DASH), name=name_html, date=date_html,
             dir=_dir_html(t.get("direction")), out=_outcome_html(t), cls=_sign_class(t.get("net_pnl")),
             net=_esc(_money(t.get("net_pnl"), signed=True)), r=_esc(_rmult(t.get("r_multiple"))))

    # Why here: the context tags in words, with the level or zone prices.
    why_bits = []
    sweep, h1 = _d(t.get("sweep_level")), _d(t.get("h1_fvg"))
    for ctx in _l(t.get("contexts")):
        words = _context_words(ctx)
        if str(ctx).startswith("SWEEP") and _f(sweep.get("price")) is not None:
            words += " at {}".format(_price(sweep.get("price")))
        if str(ctx).startswith("H1_FVG") and _f(h1.get("top")) is not None and _f(h1.get("bottom")) is not None:
            words += " ({} to {})".format(_price(h1.get("bottom")), _price(h1.get("top")))
        why_bits.append(words)
    rvol = t.get("rvol")
    why = ""
    if why_bits or _f(rvol) is not None:
        parts = []
        if why_bits:
            parts.append("Reason to be here: " + "; ".join(why_bits) + ".")
        if _f(rvol) is not None:
            parts.append("Relative volume {}.".format(_rvol(rvol)))
        why = '<p class="why">{}</p>'.format(_esc(" ".join(parts)))

    reads = []
    for label, key in (("Weekly", "weekly"), ("Daily", "daily"), ("1-hour", "hourly")):
        read = _d(bias.get(key))
        if not read:
            continue
        reads.append('<div class="read"><span class="tf">{}</span><span>{}</span><span class="txt">{}</span></div>'.format(
            label, _bias_html(read, with_title=False), _esc(read.get("reason") or DASH)))
    if reads:
        aligned = _i(bias.get("aligned"))
        verdict = _esc(bias.get("reason") or "")
        if aligned:
            verdict = "<b>{}/3 aligned.</b> {}".format(aligned, verdict)
        reads_html = '<h4>Top-down read</h4><div class="reads">{}</div>{}'.format(
            "".join(reads), '<p class="verdict">{}</p>'.format(verdict) if verdict else "")
    else:
        reads_html = '<h4>Top-down read</h4><p class="muted">The weekly, daily and 1-hour reads were not recorded for this trade.</p>'

    story = [s for s in _l(t.get("story")) if s]
    if story:
        story_html = '<h4>What happened, in order</h4><ol class="story">{}</ol>'.format(
            "".join("<li>{}</li>".format(_esc(s)) for s in story))
    else:
        story_html = '<h4>What happened, in order</h4><p class="muted">No step-by-step story was recorded for this trade.</p>'

    svg = trade_chart_svg(t)
    if svg:
        chart_html = '<h4>The 5-minute chart</h4><figure class="chart">{}</figure>'.format(svg)
    else:
        chart_html = '<h4>The 5-minute chart</h4><p class="muted">No chart candles were recorded for this trade.</p>'

    exit_reason = str(t.get("exit_reason") or "").upper()
    target_note = _target_words(t.get("target_kind"))
    cells = [
        ("Entry", _esc(_price(t.get("entry"))), _esc(_hhmm(t.get("entry_time")))),
        ("Stop", _esc(_price(t.get("stop"))), ""),
        ("Target", _esc(_price(t.get("target"))), _esc(target_note)),
        ("Exit", _esc(_price(t.get("exit_price"))),
         _esc("{} at {}".format(EXIT_WORDS.get(exit_reason, exit_reason or DASH), _hhmm(t.get("exit_time"))))),
        ("Quantity", _esc(_count(t.get("qty"))), "shares"),
        ("Position value", _esc(_money(t.get("exposure"))), ""),
        ("Margin blocked", _esc(_money(t.get("margin"))), ""),
        ("Risk (1R)", _esc(_money(t.get("risk_inr"))), ""),
        ("Planned reward to risk", _esc(DASH if _f(t.get("rr_planned")) is None else "{:.2f}".format(_f(t.get("rr_planned")))), ""),
        ("Gross P&L", _pnl_html(t.get("gross_pnl")), ""),
        ("Charges", _esc(_money(t.get("charges"))), ""),
        ("Net P&L", _pnl_html(t.get("net_pnl")), _esc(_rmult(t.get("r_multiple")))),
    ]
    kv = "".join(
        "<div><dt>{}</dt><dd>{}{}</dd></div>".format(
            _esc(label), value, '<span class="small">{}</span>'.format(note) if note else "")
        for label, value, note in cells)
    money_html = '<h4>The money</h4><dl class="kv">{}</dl>'.format(kv)

    tid = t.get("id")
    id_html = '<p class="muted" style="margin:10px 0 0;font-size:.76rem">Trade id {}</p>'.format(_esc(tid)) if tid else ""
    return '<article class="card trade">{}{}{}{}{}{}{}</article>'.format(
        head, why, reads_html, story_html, chart_html, money_html, id_html)


def _watchlist_table(watchlist: Sequence[dict]) -> str:
    headers = [("Stock", ""), ("First seen", ""), ("RVOL", "num"), ("Traded value", "num"), ("Day change", "num"),
               ("Weekly", ""), ("Daily", ""), ("1-hour", ""), ("Allowed", ""), ("Status", ""), ("Note", "")]
    rows = []
    for item in watchlist:
        w = _d(item)
        bias = _d(w.get("bias"))
        name = w.get("name")
        stock = "<b>{}</b>{}".format(
            _esc(w.get("symbol") or DASH),
            '<span class="small" title="{0}">{0}</span>'.format(_esc(name)) if name and name != w.get("symbol") else "")
        turnover = _f(w.get("turnover_cr"))
        rows.append([
            (stock, ""),
            (_esc(_hhmm(w.get("eligible_from")) if w.get("eligible_from") else DASH), ""),
            (_esc(_rvol(w.get("rvol"))), "num"),
            (_esc(DASH if turnover is None else "₹{:,.1f} cr".format(turnover)), "num"),
            ('<span class="{}">{}</span>'.format(_sign_class(w.get("day_change_pct")), _esc(_pct(w.get("day_change_pct")))), "num"),
            (_bias_html(bias.get("weekly")), ""),
            (_bias_html(bias.get("daily")), ""),
            (_bias_html(bias.get("hourly")), ""),
            (_allowed_html(bias) if bias else DASH, ""),
            (_status_html(w.get("status")), ""),
            (_esc(w.get("note") or DASH), "wrap"),
        ])
    return _table(headers, rows, "No stock passed the volume scan today.", "tight")


def _signals_table(signals: Sequence[dict]) -> str:
    headers = [("Time", ""), ("Stock", ""), ("Direction", ""), ("Context", ""), ("Entry", "num"), ("Stop", "num"),
               ("Target", "num"), ("RR", "num"), ("Status", ""), ("Reason", "")]
    rows = []
    for sig in signals:
        s = _d(sig)
        rr = _f(s.get("rr"))
        contexts = ", ".join(str(c) for c in _l(s.get("contexts"))) or DASH
        rows.append([
            (_esc(_hhmm(s.get("signal_time"))), ""),
            ("<b>{}</b>".format(_esc(s.get("symbol") or DASH)), ""),
            (_dir_text(s.get("direction")), ""),
            (_esc(contexts), ""),
            (_esc(_price(s.get("entry"))), "num"),
            (_esc(_price(s.get("stop"))), "num"),
            (_esc(_price(s.get("target"))), "num"),
            (_esc(DASH if rr is None else "{:.2f}".format(rr)), "num"),
            (_status_html(s.get("status")), ""),
            (_esc(_sentence(s.get("skip_reason")) or _signal_reason(s)), "wrap"),
        ])
    return _table(headers, rows, "Every signal was traded.")


def _sentence(text: Any) -> str:
    """First letter upper-cased; the rest untouched."""
    t = str(text or "").strip()
    return t[:1].upper() + t[1:] if t else ""


def _signal_reason(sig: dict) -> str:
    status = str(sig.get("status") or "").upper()
    if status == "EXPIRED":
        return "Price did not come back to the entry in time."
    if status == "INVALIDATED":
        return "The gap failed before the order filled."
    return DASH


def _sessions_table(days: Sequence[dict], limit: Optional[int] = 20) -> str:
    headers = [("Date", ""), ("Trades", "num"), ("Wins", "num"), ("Losses", "num"), ("Net P&L", "num"), ("Capital at close", "num")]
    ordered = [_d(d) for d in days]
    ordered = list(reversed(ordered))
    if limit:
        ordered = ordered[:limit]
    rows = []
    for d in ordered:
        label = _esc(_date_long(d.get("date")))
        if d.get("partial"):
            label += ' <span class="pill st-warn">partial</span>'
        rows.append([
            (label, ""),
            (_esc(_count(d.get("n_trades"))), "num"),
            (_esc(_count(d.get("wins"))), "num"),
            (_esc(_count(d.get("losses"))), "num"),
            (_pnl_html(d.get("net_pnl")), "num"),
            (_esc(_money(d.get("end_capital"))), "num"),
        ])
    return _table(headers, rows, "No sessions recorded yet.")


def _equity_block(state: dict, lead: str) -> str:
    st = _d(state)
    svg = equity_curve_svg(_l(st.get("days")), _f(st.get("initial_capital")) or config.INITIAL_CAPITAL)
    if not svg:
        return '<div class="card"><p class="muted" style="margin:0">No sessions recorded yet, so there is no equity curve to draw.</p></div>'
    return '<p class="lead">{}</p><figure class="chart wide">{}</figure>'.format(_esc(lead), svg)


# ── Daily report ──────────────────────────────────────────────────────────────

def build_daily_html(result: dict, state: dict) -> str:
    """The daily report for one DayResult; `state` is the portfolio after that day.

    Sections, in this order:
      1. Header: "Intraday Paper Trader", the date, generated time, a banner
         when result["partial"] is true.
      2. Stat cards: capital at close, day net P&L (INR and % of start capital),
         trades (wins / losses), charges, running net P&L since inception,
         sessions traded, overall win rate.
      3. "How today was traded": the four steps in two or three lines each, with
         the day's real counts (universe size, stocks in play, signals, trades).
      4. Trades: one card per trade with direction, contexts, entry / stop /
         target / exit, qty, exposure, margin, planned RR, exit reason, gross,
         charges, net, R multiple, the weekly / daily / 1h reads, the `story`
         as a numbered list, and trade_chart_svg. A plain "No trades today" card
         with the reason when there are none.
      5. Stocks in play: table of the watchlist (symbol, first seen, RVOL,
         turnover, day change, W / D / 1h bias, direction allowed, status, note).
      6. Signals not traded: table (time, symbol, direction, contexts, entry,
         stop, target, RR, status, reason). Omitted when empty.
      7. Equity curve since inception (equity_curve_svg) and a table of the last
         20 sessions (date, trades, net P&L, capital).
      8. Rules in force (result["config"]) and config.DISCLAIMER.
    """
    res, st = _d(result), _d(state)
    summ = _summary(st)
    cfg = _d(res.get("config"))
    trades = [_d(t) for t in _l(res.get("trades"))]
    watch = [_d(w) for w in _l(res.get("watchlist"))]
    signals = [_d(s) for s in _l(res.get("signals"))]
    notes = [str(x) for x in _l(res.get("notes")) if x]
    date = res.get("date")

    parts: List[str] = []

    # 1. Header
    sub = "{} &middot; NSE &middot; paper trades replayed after the close".format(_esc(_date_long(date)))
    if res.get("generated_at"):
        sub += " &middot; generated {}".format(_esc(_stamp(res.get("generated_at"))))
    parts.append("<header><h1>Intraday Paper Trader</h1><div class=\"sub\">{}</div>".format(sub))
    if res.get("partial"):
        parts.append(
            '<div class="banner" role="note"><b>Partial session.</b> Fewer 5-minute candles than a full day were '
            "available when this ran, so the result below covers only part of the session. Replaying the date "
            "later replaces it.</div>")
    parts.append("</header>")

    # 2. Stat cards
    start_cap, end_cap = _f(res.get("start_capital")), _f(res.get("end_capital"))
    net = _f(res.get("net_pnl"))
    day_pct = (net / start_cap * 100.0) if (net is not None and start_cap) else None
    n_trades = _i(res.get("n_trades"))
    if n_trades is None:
        n_trades = len(trades)
    wins, losses = _i(res.get("wins")), _i(res.get("losses"))
    charge_each = _f(cfg.get("charge_per_trade"))
    today_tiles = [
        _tile("Capital at close", _esc(_money(end_cap)),
              "started the day at {}".format(_esc(_money(start_cap))) if start_cap is not None else ""),
        _tile("Day net P&L", '<span class="{}">{}</span>'.format(_sign_class(net), _esc(_money(net, signed=True))),
              "{} of start capital".format(_esc(_pct(day_pct))) if day_pct is not None else ""),
        _tile("Trades today", _esc(_count(n_trades)),
              "{} / {}".format(_esc(_plural(wins, "win")), _esc(_plural(losses, "loss", "losses")))
              if (wins is not None or losses is not None) else ""),
        _tile("Charges today", _esc(_money(res.get("charges"))),
              "{} per completed trade".format(_esc(_money(charge_each))) if charge_each is not None else ""),
    ]
    since_tiles = [
        _tile("Net P&L since start", '<span class="{}">{}</span>'.format(
            _sign_class(summ.get("net_pnl")), _esc(_money(summ.get("net_pnl"), signed=True))),
            "{} on {}".format(_esc(_pct(summ.get("net_pnl_pct"))), _esc(_money(st.get("initial_capital"), decimals=0)))
            if (_f(summ.get("net_pnl_pct")) is not None and _f(st.get("initial_capital")) is not None) else ""),
        _tile("Sessions traded", _esc(_count(summ.get("sessions"))), ""),
        _tile("Overall win rate", _esc(_pct(summ.get("win_rate_pct"), signed=False) if _i(summ.get("n_trades")) else DASH),
              "{} of {}".format(_esc(_count(summ.get("wins"))), _esc(_plural(summ.get("n_trades"), "trade")))
              if _i(summ.get("n_trades")) else "no closed trades yet"),
    ]
    parts.append(
        '<section aria-label="Headline numbers"><h4 class="grp">Today</h4><div class="tiles">{}</div>'
        '<h4 class="grp">Since the first session</h4><div class="tiles">{}</div></section>'.format(
            "".join(today_tiles), "".join(since_tiles)))

    # 3. How today was traded
    n_watch = len(watch)
    longs = sum(1 for w in watch if str(_d(w.get("bias")).get("direction") or "").upper() == "LONG")
    shorts = sum(1 for w in watch if str(_d(w.get("bias")).get("direction") or "").upper() == "SHORT")
    aside = n_watch - longs - shorts
    n_signals = len(signals)
    by_status: Dict[str, int] = {}
    for s in signals:
        key = str(s.get("status") or "").upper()
        by_status[key] = by_status.get(key, 0) + 1
    exits: Dict[str, int] = {}
    for t in trades:
        key = str(t.get("exit_reason") or "").upper()
        exits[key] = exits.get(key, 0) + 1
    universe, scanned = _i(res.get("universe_size")), _i(res.get("scanned"))
    min_rvol = _f(cfg.get("min_rvol"))
    checkpoints = ", ".join(str(x) for x in _l(cfg.get("scan_checkpoints")))

    step1 = "<b>{}</b> of {} stocks had usable data. <b>{}</b> traded unusually heavy volume{} and went on the watchlist{}.".format(
        _esc(_count(scanned)), _esc(_count(universe)), _esc(_plural(n_watch, "stock")),
        " (relative volume of {:g}x or more)".format(min_rvol) if min_rvol is not None else "",
        ", checked at {}".format(_esc(checkpoints)) if checkpoints else "")
    step2 = ("Weekly, daily and 1-hour structure was read for each. <b>{}</b> allowed longs only, <b>{}</b> allowed "
             "shorts only and <b>{}</b> had timeframes that disagreed, so they were left alone.").format(longs, shorts, aside)
    not_filled = n_signals - by_status.get("FILLED", 0)
    step3 = ("On the 5-minute chart a setup needs a sweep or a 1-hour gap tap, a displacement candle, a structure "
             "shift and a fair value gap. <b>{}</b> appeared; <b>{}</b> filled and <b>{}</b> did not.").format(
        _esc(_plural(n_signals, "setup")), by_status.get("FILLED", 0), not_filled)
    exit_bits = []
    for key, word in (("TARGET", "at the target"), ("STOP", "at the stop"), ("EOD", "at the square-off")):
        if exits.get(key):
            exit_bits.append("{} {}".format(exits[key], word))
    step4 = "<b>{}</b> taken{}. Net result after charges: <b class=\"{}\">{}</b>.".format(
        _esc(_plural(n_trades, "trade")),
        ": " + _esc(", ".join(exit_bits)) if exit_bits else "",
        _sign_class(net), _esc(_money(net, signed=True)))
    steps = "".join(
        '<li><span class="n">{}</span><span class="t">{}</span><p>{}</p></li>'.format(k, _esc(t), body)
        for k, (t, body) in enumerate((("Scan for stocks in play", step1), ("Read the trend top-down", step2),
                                       ("Wait for the 5-minute entry", step3), ("Manage and square off", step4)), 1))
    notes_html = ""
    if notes:
        notes_html = '<ul class="notes">{}</ul>'.format("".join("<li>{}</li>".format(_esc(x)) for x in notes))
    parts.append('<section><h2>How today was traded</h2><ol class="steps">{}</ol>{}</section>'.format(steps, notes_html))

    # 4. Trades
    parts.append("<section><h2>Trades</h2>")
    if trades:
        parts.append('<p class="lead">Each card reads top to bottom the way the trade was found: the bigger timeframes, '
                     "the events on the 5-minute chart, then the money.</p>")
        parts.extend(_trade_card(t) for t in trades)
    else:
        if not watch:
            reason = "No stock passed the volume scan, so there was nothing to trade."
        elif not signals:
            reason = "{} in play, but none completed a valid 5-minute setup in the direction its trend allowed.".format(
                _plural(n_watch, "stock was", "stocks were"))
        else:
            reason = "{} appeared but none became a trade. The table of signals below gives the reason for each.".format(
                _plural(n_signals, "setup"))
        parts.append('<div class="card"><h3>No trades today</h3><p class="why">{}</p></div>'.format(_esc(reason)))
    parts.append("</section>")

    # 5. Stocks in play
    parts.append('<section><h2>Stocks in play</h2><p class="lead">Ranked by relative volume at the checkpoint where each '
                 "first qualified. Hover a bias to see the reason.</p>{}</section>".format(_watchlist_table(watch)))

    # 6. Signals not traded
    untaken = [s for s in signals if str(s.get("status") or "").upper() != "FILLED"]
    if untaken:
        parts.append('<section><h2>Signals not traded</h2><p class="lead">Setups that passed every check but did not '
                     "become a trade.</p>{}</section>".format(_signals_table(untaken)))

    # 7. Equity curve and recent sessions
    parts.append("<section><h2>Equity curve since inception</h2>{}<h4>Last 20 sessions</h4>{}</section>".format(
        _equity_block(st, "Paper capital at the close of each session. The dashed line is the starting capital."),
        _sessions_table(_l(st.get("days")), 20)))

    # 8. Rules and disclaimer
    parts.append("<section><h2>Rules in force</h2>{}</section>".format(_config_html(cfg)))
    parts.append('<p class="disclaimer">{}</p>'.format(_esc(config.DISCLAIMER)))

    return _page("Intraday Paper Trader {}".format(date or ""), "\n".join(parts))


# ── Backtest report ───────────────────────────────────────────────────────────

def _breakdown(trades: Sequence[dict], key_fn, order: Optional[Sequence[str]] = None) -> List[Tuple[str, int, int, float]]:
    groups: Dict[str, List[dict]] = {}
    for t in trades:
        groups.setdefault(str(key_fn(t)), []).append(t)
    keys = list(groups)
    if order:
        keys = [k for k in order if k in groups] + sorted(k for k in groups if k not in order)
    else:
        keys.sort()
    out = []
    for k in keys:
        items = groups[k]
        wins = sum(1 for t in items if (_f(t.get("net_pnl")) or 0) > 0)
        net = sum(_f(t.get("net_pnl")) or 0.0 for t in items)
        out.append((k, len(items), wins, net))
    return out


def _breakdown_table(title: str, first_col: str, rows: List[Tuple[str, int, int, float]]) -> str:
    headers = [(first_col, ""), ("Trades", "num"), ("Win rate", "num"), ("Net P&L", "num")]
    body = [[(_esc(k), ""), (_esc(_count(n)), "num"),
             (_esc(_pct(_win_rate(w, n), signed=False)), "num"), (_pnl_html(net), "num")]
            for k, n, w, net in rows]
    return "<div><h3>{}</h3>{}</div>".format(_esc(title), _table(headers, body, "No trades.", "compact"))


def _entry_hour(trade: dict) -> str:
    dt = _parse_iso(trade.get("entry_time"))
    return "{:02d}:00 to {:02d}:59".format(dt.hour, dt.hour) if dt is not None else "Unknown"


def build_backtest_html(results: List[dict], state: dict) -> str:
    """The backtest report; `results` are DayResults in date order, `state` the
    in-memory portfolio they were recorded into.

    Header with the period and a line stating this is a replay of past sessions
    on the current universe with a flat charge and no slippage. Stat cards from
    portfolio.summarize(state). Equity curve. Breakdown tables: by context kind
    (SWEEP / H1_FVG / both), by direction, by exit reason, by entry hour, each
    with trades, win rate and net P&L. Per-day table. Full trade log. The three
    best and three worst trades as cards with charts. Rules in force and
    config.DISCLAIMER.
    """
    results = [_d(r) for r in _l(results)]
    st = _d(state)
    summ = _summary(st)
    trades = [_d(t) for r in results for t in _l(r.get("trades"))]
    if not trades:
        trades = [_d(t) for t in _l(st.get("trades"))]
    days = _l(st.get("days")) or [
        {"date": r.get("date"), "n_trades": r.get("n_trades"), "wins": r.get("wins"), "losses": r.get("losses"),
         "net_pnl": r.get("net_pnl"), "end_capital": r.get("end_capital"), "partial": r.get("partial")} for r in results]
    cfg = _d(results[0].get("config")) if results else {}
    if not cfg:
        cfg = config.snapshot()
    dates = [str(_d(d).get("date")) for d in days if _d(d).get("date")]
    first, last = (dates[0], dates[-1]) if dates else (None, None)
    generated = next((r.get("generated_at") for r in reversed(results) if r.get("generated_at")), None)

    parts: List[str] = []
    period = "{} to {}".format(_date_long(first), _date_long(last)) if first else "no sessions"
    sub = "{} &middot; {}".format(_esc(period), _esc(_plural(len(days), "session")))
    if generated:
        sub += " &middot; generated {}".format(_esc(_stamp(generated)))
    charge = _f(cfg.get("charge_per_trade"))
    parts.append(
        '<header><h1>Intraday Paper Trader: backtest</h1><div class="sub">{}</div>'
        '<div class="banner" role="note"><b>Read this first.</b> This is a replay of past sessions on today\'s stock '
        "universe, with a flat charge of {} per trade and no slippage. Real trading costs more and fills worse. "
        "A good result here is a reason to keep testing, not a promise.</div></header>"
        .format(sub, _esc(_money(charge)) if charge is not None else "a fixed amount"))

    pf = _f(summ.get("profit_factor"))
    best, worst = _d(summ.get("best_day")), _d(summ.get("worst_day"))
    tiles = [
        _tile("Final capital", _esc(_money(summ.get("capital") if summ.get("capital") is not None else st.get("capital"))),
              "from {}".format(_esc(_money(st.get("initial_capital"), decimals=0)))
              if _f(st.get("initial_capital")) is not None else ""),
        _tile("Net P&L", '<span class="{}">{}</span>'.format(
            _sign_class(summ.get("net_pnl")), _esc(_money(summ.get("net_pnl"), signed=True))),
            "{} of starting capital".format(_esc(_pct(summ.get("net_pnl_pct"))))),
        _tile("Trades", _esc(_count(summ.get("n_trades"))),
              "{} / {}".format(_esc(_plural(summ.get("wins"), "win")), _esc(_plural(summ.get("losses"), "loss", "losses")))),
        _tile("Win rate", _esc(_pct(summ.get("win_rate_pct"), signed=False) if _i(summ.get("n_trades")) else DASH), ""),
        _tile("Profit factor", _esc(DASH if pf is None else "{:.2f}".format(pf)), "gross wins divided by gross losses"),
        _tile("Expectancy per trade", '<span class="{}">{}</span>'.format(
            _sign_class(summ.get("expectancy_inr")), _esc(_money(summ.get("expectancy_inr"), signed=True))),
            "average {}".format(_esc(_rmult(summ.get("avg_r"))))),
        _tile("Largest drawdown", _esc(_money(summ.get("max_drawdown_inr"))),
              "{} from a peak, end-of-day capital".format(_esc(_pct(summ.get("max_drawdown_pct"), signed=False)))),
        _tile("Charges paid", _esc(_money(summ.get("charges"))),
              "gross P&L {}".format(_esc(_money(summ.get("gross_pnl"), signed=True)))),
        _tile("Best day", '<span class="{}">{}</span>'.format(
            _sign_class(best.get("net_pnl")), _esc(_money(best.get("net_pnl"), signed=True))),
            _esc(_date_short(best.get("date"))) if best else ""),
        _tile("Worst day", '<span class="{}">{}</span>'.format(
            _sign_class(worst.get("net_pnl")), _esc(_money(worst.get("net_pnl"), signed=True))),
            _esc(_date_short(worst.get("date"))) if worst else ""),
    ]
    parts.append('<section class="tiles" aria-label="Headline numbers">{}</section>'.format("".join(tiles)))

    parts.append("<section><h2>Equity curve</h2>{}</section>".format(
        _equity_block(st, "Paper capital at the close of each replayed session. The dashed line is the starting capital.")))

    if trades:
        parts.append(
            '<section><h2>Where the results came from</h2><p class="lead">Small groups prove little. Treat a row with '
            'fewer than about 20 trades as a hint, not a finding.</p><div class="grid2">{}{}{}{}</div></section>'.format(
                _breakdown_table("By reason for the trade", "Context", _breakdown(
                    trades, lambda t: _context_kind(t.get("contexts")),
                    ["Sweep only", "1h gap tap only", "Sweep and 1h gap"])),
                _breakdown_table("By direction", "Direction", _breakdown(
                    trades, lambda t: {"LONG": "Long", "SHORT": "Short"}.get(str(t.get("direction") or "").upper(), "Unknown"),
                    ["Long", "Short"])),
                _breakdown_table("By exit", "Exit", _breakdown(
                    trades, lambda t: EXIT_WORDS.get(str(t.get("exit_reason") or "").upper(), "Unknown"),
                    [EXIT_WORDS["TARGET"], EXIT_WORDS["STOP"], EXIT_WORDS["EOD"]])),
                _breakdown_table("By hour of entry (IST)", "Entry hour", _breakdown(trades, _entry_hour))))

    parts.append("<section><h2>Day by day</h2>{}</section>".format(_sessions_table(days, None)))

    # One row per trade. Times and the exit reason sit under the prices so the table fits a desktop page.
    headers = [("Date", ""), ("Stock", ""), ("Direction", ""), ("Context", ""), ("Entry", "num"), ("Stop", "num"),
               ("Target", "num"), ("Exit", "num"), ("Qty", "num"), ("Gross", "num"), ("Charges", "num"),
               ("Net", "num"), ("R", "num")]
    short_exit = {"TARGET": "Target", "STOP": "Stop", "EOD": "EOD"}
    rows = []
    for t in trades:
        reason = str(t.get("exit_reason") or "").upper()
        exit_note = " ".join(x for x in (short_exit.get(reason, reason.capitalize()), _hhmm(t.get("exit_time"))) if x and x != DASH)
        rows.append([
            (_esc(t.get("date") or DASH), ""),
            ("<b>{}</b>".format(_esc(t.get("symbol") or DASH)), ""),
            (_dir_text(t.get("direction")), ""),
            (_esc(", ".join(str(c) for c in _l(t.get("contexts"))) or DASH), ""),
            ('{}<span class="small">{}</span>'.format(_esc(_price(t.get("entry"))), _esc(_hhmm(t.get("entry_time")))), "num"),
            (_esc(_price(t.get("stop"))), "num"),
            (_esc(_price(t.get("target"))), "num"),
            ('{}<span class="small">{}</span>'.format(_esc(_price(t.get("exit_price"))), _esc(exit_note or DASH)), "num"),
            (_esc(_count(t.get("qty"))), "num"),
            (_pnl_html(t.get("gross_pnl")), "num"),
            (_esc(_money(t.get("charges"))), "num"),
            (_pnl_html(t.get("net_pnl")), "num"),
            (_esc(_rmult(t.get("r_multiple"))), "num"),
        ])
    parts.append('<section><h2>Every trade</h2><p class="lead">Under each entry is the time it filled; under each exit, '
                 "why and when the trade closed.</p>{}</section>".format(
                     _table(headers, rows, "The replay took no trades.", "tight")))

    ranked = sorted((t for t in trades if _f(t.get("net_pnl")) is not None), key=lambda t: _f(t.get("net_pnl")) or 0.0)
    best_trades = [t for t in reversed(ranked) if (_f(t.get("net_pnl")) or 0) > 0][:3]
    worst_trades = [t for t in ranked if (_f(t.get("net_pnl")) or 0) < 0][:3]
    if best_trades:
        parts.append('<section><h2>The three best trades</h2><p class="lead">What the method looks like when it works.</p>{}</section>'.format(
            "".join(_trade_card(t, "h4") for t in best_trades)))
    if worst_trades:
        parts.append('<section><h2>The three worst trades</h2><p class="lead">Worth more study than the winners: every '
                     "check passed and the trade still lost.</p>{}</section>".format(
                         "".join(_trade_card(t, "h4") for t in worst_trades)))

    parts.append("<section><h2>Rules in force</h2>{}</section>".format(_config_html(cfg)))
    parts.append('<p class="disclaimer">{}</p>'.format(_esc(config.DISCLAIMER)))
    return _page("Intraday Paper Trader backtest {}".format(last or ""), "\n".join(parts))


def save_report(html: str, filename: str, reports_dir: Path = config.REPORTS_DIR) -> Path:
    """Write the HTML (UTF-8) into reports_dir, creating it, and return the path."""
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    out = reports_dir / filename
    out.write_text(html, encoding="utf-8")
    return out
