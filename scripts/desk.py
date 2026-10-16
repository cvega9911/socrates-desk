"""Shared config, journal I/O and indicator math for the Socrates trading desk (rules v1.0)."""
import datetime as dt
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
ROOT = Path(os.environ.get("DESK_ROOT", Path(__file__).resolve().parent.parent))
JOURNAL = ROOT / "journal"
REPORTS = ROOT / "reports"
CANDIDATES = JOURNAL / "candidates.jsonl"
TRADES = JOURNAL / "trades.jsonl"
WEEKLY = JOURNAL / "weekly.md"
START_FILE = ROOT / "start_date.txt"

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


def rules_version():
    f = ROOT / "rules_version.txt"
    return f.read_text().strip() if f.exists() else "v1.0"


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
    """Secrets come from the macOS Keychain first; socrates-desk/.env is only a fallback."""
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
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


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


# ---------------- journal I/O ----------------
def read_jsonl(path):
    if not Path(path).exists():
        return []
    rows = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass   # a corrupted line never kills a run
    return rows


def append_jsonl(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def write_jsonl(path, rows):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(path)


def open_trades():
    return [t for t in read_jsonl(TRADES) if t.get("status") in ("pending", "open", "closing")]


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
    """Stop: beyond the 10-day swing, clamped to 1-2 ATR.
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
