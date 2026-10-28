"""Shared config, journal I/O and indicator math for the Socrates trading desk (rules v1.0)."""
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
ROOT = Path(os.environ.get("DESK_ROOT", Path(__file__).resolve().parent.parent))
JOURNAL = ROOT / "journal"
REPORTS = ROOT / "reports"
CANDIDATES = JOURNAL / "candidates.jsonl"
TRADES = JOURNAL / "trades.jsonl"
CONTROL = JOURNAL / "control.jsonl"
WEEKLY = JOURNAL / "weekly.md"
START_FILE = ROOT / "start_date.txt"
LOCK_FILE = JOURNAL / ".lock"
CORRUPT_FILE = JOURNAL / "corrupt_lines.txt"

# ---------------- rules v1.0 constants ----------------
ETFS = ["SPY", "QQQ", "IWM", "DIA", "XLE", "XLK", "XLF", "XLV", "XLI", "XLU", "XLY", "XLP", "XLB",
        "SMH", "TLT", "IEF", "GLD", "SLV", "GDX", "USO", "UUP"]
STOCKS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM", "XOM", "AVGO"]
UNIVERSE = ETFS + STOCKS

THEMES = {
    "broad": ["SPY", "DIA", "IWM"],
    "tech": ["QQQ", "XLK", "SMH", "NVDA", "AAPL", "MSFT", "AVGO", "GOOGL", "META"],
    "consumer": ["XLY", "AMZN", "TSLA"],
    "energy": ["XLE", "XOM", "USO"],
    "financials": ["XLF", "JPM"],
    "defensive": ["XLV", "XLU", "XLP"],
    "cyclicals": ["XLI", "XLB"],
    "bonds": ["TLT", "IEF"],
    "metals": ["GLD", "SLV", "GDX"],
    "dollar": ["UUP"],
}
THEME_OF = {s: t for t, syms in THEMES.items() for s in syms}

RISK_PCT = {"A": 0.0050, "B": 0.0025}
DAILY_HALT_PCT = -0.015
MAX_POSITIONS = 5
MAX_OPEN_RISK_PCT = 0.025
MAX_PER_THEME = 2
MAX_NOTIONAL_PCT = 0.20
QUOTA_DAYS = 5
QUOTA_MIN_SCORE = 4
TIME_STOP_DAYS = 10
ENTRY_SLIP = 0.0005
MIN_RR = 1.0
MIN_BARS = 61            # 60-day levels + 1
BARS_DELAY_MIN = 16      # free data plan: SIP history must be older than 15 minutes
TEXT_MAX = 600           # longest thesis / invalidation / dissent accepted

# The rules version is pinned in code. rules_version.txt must match it, so editing the text file alone can never
# reset the scorecard; a real version bump changes both (and is reviewed by Chris).
RULES_VERSION = "v1.0"


class DeskError(SystemExit):
    """A refusal or failure the agent must report verbatim. Exits non-zero."""


def rules_version():
    f = ROOT / "rules_version.txt"
    v = f.read_text().strip() if f.exists() else RULES_VERSION
    if v != RULES_VERSION:
        raise DeskError(f"RULES VERSION MISMATCH: rules_version.txt says {v!r} but the scripts are {RULES_VERSION!r}. "
                        "Rule changes go through Chris; do not edit rules_version.txt. Stop and tell Chris.")
    return v


_INVISIBLE = re.compile("[\u0000-\u0008\u000b-\u001f\u007f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")


def plain_text(val, limit=TEXT_MAX):
    """Free text for the journal: no control, zero-width or right-to-left override characters, single-spaced, capped."""
    val = " ".join(_INVISIBLE.sub(" ", str(val)).split())
    return val[:limit]


def md_safe(val, limit=300):
    """Text from the agent or the web, made inert for markdown/HTML (GitHub, Obsidian)."""
    val = plain_text(val, limit).replace("<", "\u2039").replace(">", "\u203a")
    return re.sub(r"([\\`*_\[\]()<>!|#])", r"\\\1", val)


def warn(msg):
    """Loud warning on stderr (keeps --json output on stdout valid)."""
    print(f"WARNING: {msg}", file=sys.stderr)


SECRET_NAMES = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "FINNHUB_API_KEY")
KEYCHAIN_SERVICE = "socrates-desk"


def keychain_get(name):
    """Read one secret from the macOS Keychain (service 'socrates-desk'). None if absent or not on a Mac."""
    import subprocess
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def load_env():
    """Secrets come from the macOS Keychain first; socrates-desk/.env is only a fallback, and only for the three
    secret names. Nothing else in .env is read, so a planted .env can't set a proxy or CA bundle and siphon the keys."""
    for name in SECRET_NAMES:
        if not os.environ.get(name):
            v = keychain_get(name)
            if v:
                os.environ[name] = v
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k in SECRET_NAMES:
                    os.environ.setdefault(k, v.strip().strip('"').strip("'"))


_BROKER = None


def broker():
    global _BROKER
    if _BROKER is None:
        load_env()
        from broker import RestBroker
        _BROKER = RestBroker()
    return _BROKER


def set_broker(b):   # used by tests
    global _BROKER
    _BROKER = b


def now_et():
    return dt.datetime.now(ET)


def today_str():
    return now_et().date().isoformat()


def bars_end():
    """End timestamp for bar requests: now minus the free plan's 15-minute SIP delay."""
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=BARS_DELAY_MIN)).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16] if path and Path(path).exists() else None


# ---------------- market calendar ----------------
def session(b, day=None):
    """Today's trading session from Alpaca's calendar: {"date","open","close"} or None on weekends/holidays."""
    day = day or today_str()
    rows = b.calendar_days(day, day)
    return next((r for r in rows if r.get("date") == day), None)


def market_status(b):
    """(is_trading_day, is_open_now, session_close_hhmm). Raises if Alpaca can't be reached: never guess."""
    sess = session(b)
    clk = b.clock()
    return sess is not None, bool(clk.get("is_open")), (sess or {}).get("close")


def trading_days_between(b, start, end):
    """Trading days strictly after `start` up to and including `end`. Raises on API failure (never silently 0)."""
    if start >= end:
        return 0
    return max(0, len(b.calendar(start, end)) - 1)


def trading_days_ahead(b, start, n):
    """The date n trading days after `start` (inclusive window end). Raises on API failure."""
    end = (dt.date.fromisoformat(start) + dt.timedelta(days=n * 2 + 10)).isoformat()
    days = [d for d in b.calendar(start, end) if d > start]
    if len(days) < n:
        raise DeskError(f"calendar returned only {len(days)} trading days after {start}")
    return days[n - 1]


def previous_trading_day(b, day=None):
    day = day or today_str()
    start = (dt.date.fromisoformat(day) - dt.timedelta(days=10)).isoformat()
    days = [d for d in b.calendar(start, day) if d < day]
    if not days:
        raise DeskError(f"calendar returned no trading day before {day}")
    return days[-1]


# ---------------- journal I/O ----------------
# All scripts take one exclusive lock on journal/.lock for their whole run, so two runs (or a run plus a manual
# command) can never interleave writes. Appends start on a fresh line and are fsynced; rewrites are atomic.
_lock_depth = 0
_lock_fh = None


@contextlib.contextmanager
def journal_lock(timeout_s=120):
    global _lock_depth, _lock_fh
    if _lock_depth:
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return
    JOURNAL.mkdir(parents=True, exist_ok=True)
    fh = os.fdopen(os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "r+")
    deadline = dt.datetime.now().timestamp() + timeout_s
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if dt.datetime.now().timestamp() > deadline:
                fh.close()
                raise DeskError("JOURNAL BUSY: another desk script has held the journal lock for 2 minutes. "
                                "Do not retry in a loop; tell Chris.")
            import time
            time.sleep(0.5)
    _lock_fh, _lock_depth = fh, 1
    try:
        yield
    finally:
        _lock_depth = 0
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
        _lock_fh = None


def _quarantine(path, lineno, line):
    if len(line) > 2000:
        line = line[:2000] + f"...[{len(line) - 2000} more characters]"
    with open(CORRUPT_FILE, "a") as f:
        f.write(json.dumps({"file": Path(path).name, "line": lineno, "text": line,
                            "found": dt.datetime.now(ET).isoformat(timespec="seconds")}) + "\n")
        f.flush()
        os.fsync(f.fileno())
    warn(f"{Path(path).name} line {lineno} is not valid JSON (a crash mid-write?). Copied it to "
         f"journal/{CORRUPT_FILE.name} so nothing is lost. Tell Chris.")


def read_jsonl(path):
    """Every valid row. A corrupted line is never silently dropped: it is copied to journal/corrupt_lines.txt
    (once) with a loud warning, so a later rewrite of the file can't erase it without a trace."""
    if not Path(path).exists():
        return []
    rows, seen = [], None
    for i, line in enumerate(Path(path).read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if seen is None:
                seen = CORRUPT_FILE.read_text() if CORRUPT_FILE.exists() else ""
            if json.dumps(line[:2000])[:-1] not in seen:
                _quarantine(path, i, line)
    return rows


def _fsync_dir(path):
    try:
        fd = os.open(str(Path(path).parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def append_jsonl(path, rows):
    if not rows:
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with journal_lock():
        with open(path, "ab+") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":       # last write was torn: never glue a good row onto it
                    f.write(b"\n")
            f.write("".join(json.dumps(r) + "\n" for r in rows).encode())
            f.flush()
            os.fsync(f.fileno())


def atomic_write(path, text):
    """Write a file so a crash leaves either the old or the new version. The temp file has an unpredictable name
    (no symlink games) and lives next to the target so the rename is atomic."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path)


def write_jsonl(path, rows):
    with journal_lock():
        atomic_write(path, "".join(json.dumps(r) + "\n" for r in rows))


ACTIVE = ("submitting", "pending", "open", "closing")


def open_trades():
    return [t for t in read_jsonl(TRADES) if t.get("status") in ACTIVE]


# ---------------- regime file ----------------
REGIME_EXTRA_OK = {"date", "catalysts", "notes", "sources", "exclude"}


def validate_regime(reg):
    """Return (errors, warnings). Errors make the scores wrong, so score.py refuses to run until they're fixed."""
    errors, warns = [], []
    if not isinstance(reg, dict):
        return ["the file must be a JSON object"], []
    for k in reg:
        if k not in ("themes", "symbols") and k not in REGIME_EXTRA_OK:
            warns.append(f"unknown top-level key {k!r} ignored")
    for scope, allowed in (("themes", THEMES), ("symbols", UNIVERSE)):
        block = reg.get(scope) or {}
        if not isinstance(block, dict):
            errors.append(f"{scope} must be an object")
            continue
        for key, node in block.items():
            if key not in allowed:
                errors.append(f"{scope}.{key}: unknown {scope[:-1]} (must be one of: {', '.join(sorted(allowed))})")
                continue
            if not isinstance(node, dict):
                errors.append(f"{scope}.{key}: must be an object like {{\"long\": 1, \"short\": 1, \"why\": \"...\"}}")
                continue
            for nk, nv in node.items():
                if nk in ("long", "short"):
                    if isinstance(nv, bool) or not isinstance(nv, int) or not 0 <= nv <= 2:
                        errors.append(f"{scope}.{key}.{nk}: must be the whole number 0, 1 or 2 (got {nv!r})")
                elif nk == "why":
                    if not isinstance(nv, str):
                        errors.append(f"{scope}.{key}.why: must be text")
                else:
                    errors.append(f"{scope}.{key}: unknown key {nk!r} (only long, short, why; keys are lowercase)")
    ex = reg.get("exclude")
    if ex is not None:
        if not isinstance(ex, dict):
            errors.append('exclude must be an object like {"META": "earnings 2026-10-29 (nasdaq.com 10/28)"}')
        else:
            for sym, why in ex.items():
                if sym not in STOCKS:
                    errors.append(f"exclude.{sym}: only single stocks can be excluded (earnings); "
                                  "ETFs have no earnings, so excluding one would be a veto")
                elif not isinstance(why, str) or len(why.strip()) < 10 or not re.search(r"\d{4}-\d{2}-\d{2}", why):
                    errors.append(f"exclude.{sym}: give the earnings date (YYYY-MM-DD) and source, e.g. \"earnings 2026-10-29 (nasdaq.com)\"")
                elif len(why) > 200:
                    errors.append(f"exclude.{sym}: keep the reason under 200 characters")
    return errors, warns


# ---------------- indicators ----------------
def bars_to_df(bars):
    df = pd.DataFrame(bars)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["t"], utc=True).dt.tz_convert(ET).dt.date
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return df[["date", "open", "high", "low", "close", "volume"]].drop_duplicates("date").reset_index(drop=True)


def completed(df, today):
    """Drop today's still-forming bar."""
    return df[df["date"] < today].reset_index(drop=True)


def atr(df, n=14):
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)
    return float(tr.tail(n).mean())


def ret(df, n):
    return float(df["close"].iloc[-1] / df["close"].iloc[-1 - n] - 1)


def factors(df, spy_ret20):
    """Direction-independent facts about one symbol (completed bars only)."""
    c = df["close"]
    return {
        "close": float(c.iloc[-1]),
        "sma20": float(c.tail(20).mean()),
        "sma50": float(c.tail(50).mean()),
        "ret20": ret(df, 20),
        "ret5": ret(df, 5),
        "rs": ret(df, 20) - spy_ret20,
        "vol_ok": bool(df["volume"].iloc[-1] >= df["volume"].tail(20).mean()),
        "atr": atr(df),
        "low10": float(df["low"].tail(10).min()),
        "high10": float(df["high"].tail(10).max()),
        "high20": float(df["high"].tail(20).max()),
        "low20": float(df["low"].tail(20).min()),
        "high60": float(df["high"].tail(60).max()),
        "low60": float(df["low"].tail(60).min()),
    }


def levels(f, entry, direction):
    """Stop: at the 10-day swing low/high, clamped to 1-2 ATR (ATR = simple 14-day average of true range).
    Target: nearest 20/60-day level at least 1R away (closer levels are ignored); none -> 2R; capped at 3R."""
    a = f["atr"]
    if direction == "long":
        dist = min(max(entry - f["low10"], a), 2 * a)
        stop = entry - dist
        above = [x for x in (f["high20"], f["high60"]) if x >= entry + dist]
        target = min(above) if above else entry + 2 * dist
        target = min(target, entry + 3 * dist)
    else:
        dist = min(max(f["high10"] - entry, a), 2 * a)
        stop = entry + dist
        below = [x for x in (f["low20"], f["low60"]) if x <= entry - dist]
        target = max(below) if below else entry - 2 * dist
        target = max(target, entry - 3 * dist)
    rr = abs(target - entry) / dist if dist > 0 else 0.0
    return round(stop, 2), round(target, 2), round(rr, 2)


def score(f, direction, regime_pts, rr, is_spy=False):
    if direction == "long":
        trend = 2 if f["close"] > f["sma20"] > f["sma50"] else (1 if f["close"] > f["sma50"] else 0)
        edge = f["ret20"] if is_spy else f["rs"]
        mom = 1 if f["ret5"] > 0 else 0
    else:
        trend = 2 if f["close"] < f["sma20"] < f["sma50"] else (1 if f["close"] < f["sma50"] else 0)
        edge = -(f["ret20"] if is_spy else f["rs"])
        mom = 1 if f["ret5"] < 0 else 0
    rs_pts = 2 if edge > 0.02 else (1 if edge > 0 else 0)
    rr_pts = 2 if rr >= 2.0 else (1 if rr >= 1.5 else 0)
    vol = 1 if f["vol_ok"] else 0
    b = {"trend": trend, "rs": rs_pts, "regime": regime_pts, "rr": rr_pts, "momentum": mom, "volume": vol}
    return sum(b.values()), b


def tier_for(total, rr=None):
    if rr is not None and rr < MIN_RR:
        return "shadow"          # hard gate: never risk more than you can make
    return "A" if total >= 7 else ("B" if total >= 5 else "shadow")


def size(equity, risk_pct, entry, stop):
    per_share = abs(entry - stop)
    if per_share <= 0:
        return 0
    shares = math.floor(equity * risk_pct / per_share)
    cap = math.floor(equity * MAX_NOTIONAL_PCT / entry)
    return max(0, min(shares, cap))


def r_multiple(direction, entry, stop, exit_price):
    risk = abs(entry - stop)
    if risk == 0:
        return 0.0
    move = (exit_price - entry) if direction == "long" else (entry - exit_price)
    return round(move / risk, 3)


def simulate_path(df, direction, entry, stop, target, start_date, horizon=TIME_STOP_DAYS):
    """Walk completed daily bars AFTER start_date. Returns (R, reason) or None if still unresolved.
    Same-day target+stop counts as a stop; a gap through the stop fills at the open (can be worse than -1R);
    no target/stop within `horizon` bars exits at the last close (time stop)."""
    path = df[df["date"] > start_date].head(horizon)
    for _, bar in path.iterrows():
        if direction == "long":
            hit_stop, hit_tgt = bar["low"] <= stop, bar["high"] >= target
            stop_fill = float(bar["open"]) if bar["open"] <= stop else stop
        else:
            hit_stop, hit_tgt = bar["high"] >= stop, bar["low"] <= target
            stop_fill = float(bar["open"]) if bar["open"] >= stop else stop
        if hit_stop:
            return r_multiple(direction, entry, stop, stop_fill), "stop"
        if hit_tgt:
            return r_multiple(direction, entry, stop, target), "target"
    if len(path) < horizon:
        return None
    return r_multiple(direction, entry, stop, float(path["close"].iloc[-1])), "time"


# ---------------- simulating logged levels forward ----------------
def simulation_bars(b, syms, first_date, today):
    """Bars for walking LOGGED price levels forward. Logged entries/stops/targets are real (unadjusted) prices, so
    simulate on split-only bars (dividends never move history) and keep raw bars to undo a split that happened
    after the logged date. Completed bars only. Returns {symbol: (split_df, raw_df)}."""
    if not syms:
        return {}
    start = (dt.date.fromisoformat(first_date) - dt.timedelta(days=7)).isoformat() + "T00:00:00Z"
    end = bars_end()
    today_d = dt.date.fromisoformat(today)
    sp = b.daily_bars(syms, start, end, adjustment="split")
    rw = b.daily_bars(syms, start, end, adjustment="raw")
    return {s: (completed(bars_to_df(sp.get(s, [])), today_d), completed(bars_to_df(rw.get(s, [])), today_d))
            for s in syms}


def split_factor(df_split, df_raw, d0):
    """raw / split-adjusted close on the last bar at or before d0: 1.0 unless the symbol split after d0."""
    a, r = df_split[df_split["date"] <= d0], df_raw[df_raw["date"] <= d0]
    if a.empty or r.empty:
        return 1.0
    sa, ra = float(a["close"].iloc[-1]), float(r["close"].iloc[-1])
    if sa <= 0:
        return 1.0
    f = ra / sa
    return 1.0 if abs(f - 1.0) < 0.02 else f


def simulate_logged(frames, direction, entry, stop, target, d0, horizon=TIME_STOP_DAYS):
    """simulate_path for levels logged in real prices on day d0. R is unchanged by the split scaling."""
    if not frames:
        return None
    df_split, df_raw = frames
    if df_split is None or df_split.empty:
        return None
    f = split_factor(df_split, df_raw, d0)
    return simulate_path(df_split, direction, entry / f, stop / f, target / f, d0, horizon)


def matured(df, d0, horizon=TIME_STOP_DAYS):
    """True once d0 has `horizon` completed bars after it, so every outcome is known."""
    return df is not None and len(df[df["date"] > d0]) >= horizon
