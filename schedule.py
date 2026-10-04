"""Schedules for the Kiwix manager: validation, cron parsing, next-run times.

Standard library only. Every structured mode (daily, weekly, monthly) is
turned into a cron expression, so one engine handles all calendar schedules.
"""
import calendar
import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MODES = ("manual", "interval", "daily", "weekly", "monthly", "cron")
UNITS = {"minutes": 60, "hours": 3600, "days": 86400, "weeks": 604800}
MIN_INTERVAL = 10 * 60

DEFAULTS = {
    "mode": "weekly",
    "every": 7,
    "unit": "days",
    "time": "04:00",
    "days": [0],             # cron numbering: 0 = Sunday ... 6 = Saturday
    "day_of_month": "1",     # "1".."31" or "L" for the last day
    "monthly_by": "date",    # "date" (day_of_month) or "weekday" (nth + weekday)
    "nth": 1,                # 1..5, or -1 for the last one in the month
    "weekday": 0,            # 0 = Sunday ... 6 = Saturday
    "cron": "0 4 * * 0",
    "tz": "UTC",
    "catch_up": True,
}

DOW = {n: i for i, n in enumerate(["sun", "mon", "tue", "wed", "thu", "fri", "sat"])}
MON = {n: i + 1 for i, n in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
MACROS = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _field(text, lo, hi, names, label, allow_last=False):
    def num(s):
        if s in names:
            return names[s]
        if not s.isdigit():
            raise ValueError(f"'{s}' isn't a valid {label}")
        return int(s)

    values, last = set(), False
    for part in text.lower().split(","):
        if allow_last and part == "l":
            last = True
            continue
        rng, slash, step_s = part.partition("/")
        step = 1
        if slash:
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError(f"The step '/{step_s}' in the {label} field must be a positive number")
            step = int(step_s)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            x, y = rng.split("-", 1)
            a, b = num(x), num(y)
        else:
            a = num(rng)
            b = hi if slash else a
        if not (lo <= a <= hi and lo <= b <= hi):
            raise ValueError(f"The {label} field only accepts {lo} to {hi}")
        if a > b:
            raise ValueError(f"The range {a}-{b} in the {label} field runs backwards")
        values.update(range(a, b + 1, step))
    return values, last


class Cron:
    def __init__(self, expr):
        expr = MACROS.get(expr.strip().lower(), expr.strip())
        f = expr.split()
        if len(f) != 5:
            raise ValueError("A cron expression needs 5 fields: minute, hour, day of month, month, day of week")
        self.minutes, _ = _field(f[0], 0, 59, {}, "minute")
        self.hours, _ = _field(f[1], 0, 23, {}, "hour")
        self.doms, self.last_dom = _field(f[2], 1, 31, {}, "day of month", allow_last=True)
        self.months, _ = _field(f[3], 1, 12, MON, "month")
        # Extensions in the day-of-week field: "sun#2" = 2nd Sunday, "5L" = last Friday.
        self.nth, plain = [], []
        for part in f[4].lower().split(","):
            m = re.fullmatch(r"([a-z]{3}|[0-7])(?:#([1-5])|(l))", part)
            if m:
                d = DOW[m.group(1)] if m.group(1) in DOW else int(m.group(1)) % 7
                self.nth.append((d, int(m.group(2)) if m.group(2) else -1))
            else:
                plain.append(part)
        dows = _field(",".join(plain), 0, 7, DOW, "day of week")[0] if plain else set()
        self.dows = {0 if d == 7 else d for d in dows}
        # Standard cron: if both day fields are restricted, either may match.
        self.dom_any = f[2].startswith("*")
        self.dow_any = f[4].startswith("*")

    def _day_ok(self, t):
        last = calendar.monthrange(t.year, t.month)[1]
        dom = t.day in self.doms or (self.last_dom and t.day == last)
        wd = t.isoweekday() % 7
        dow = wd in self.dows or any(
            d == wd and ((t.day - 1) // 7 + 1 == n if n > 0 else t.day + 7 > last)
            for d, n in self.nth)
        if not self.dom_any and not self.dow_any:
            return dom or dow
        if not self.dom_any:
            return dom
        if not self.dow_any:
            return dow
        return True

    def next_after(self, after):
        """First matching wall-clock minute strictly after `after` (aware datetime)."""
        t = after.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        for _ in range(200_000):
            if t.month not in self.months:
                y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
                t = t.replace(year=y, month=m, day=1, hour=0, minute=0)
            elif not self._day_ok(t):
                t = (t + dt.timedelta(days=1)).replace(hour=0, minute=0)
            elif t.hour not in self.hours:
                t = (t + dt.timedelta(hours=1)).replace(minute=0)
            elif t.minute not in self.minutes:
                t = t + dt.timedelta(minutes=1)
            else:
                return t
        raise ValueError("This schedule never comes around (for example, February 30)")


def to_cron(s):
    if s["mode"] == "cron":
        return s["cron"]
    h, m = (int(x) for x in s["time"].split(":"))
    if s["mode"] == "daily":
        return f"{m} {h} * * *"
    if s["mode"] == "weekly":
        return f"{m} {h} * * {','.join(str(d) for d in s['days'])}"
    if s["mode"] == "monthly":
        if s["monthly_by"] == "weekday":
            suffix = "L" if s["nth"] == -1 else f"#{s['nth']}"
            return f"{m} {h} * * {s['weekday']}{suffix}"
        return f"{m} {h} {s['day_of_month']} * *"
    return None


def normalize(raw):
    """Validate a schedule from the portal. Raises ValueError with a readable message."""
    if not isinstance(raw, dict):
        raise ValueError("Schedule must be an object")
    s = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}
    mode = s["mode"]
    if mode not in MODES:
        raise ValueError("Choose how often to check")

    try:
        ZoneInfo(str(s["tz"]))
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"'{s['tz']}' isn't a time zone this server knows")
    s["tz"] = str(s["tz"])
    s["catch_up"] = bool(s["catch_up"])

    try:
        s["every"] = int(s["every"])
    except (TypeError, ValueError):
        s["every"] = DEFAULTS["every"]
    if s["unit"] not in UNITS:
        s["unit"] = DEFAULTS["unit"]
    if mode == "interval":
        if s["every"] < 1:
            raise ValueError("The interval must be at least 1")
        if s["every"] * UNITS[s["unit"]] < MIN_INTERVAL:
            raise ValueError("Checks can run at most every 10 minutes")
        if s["every"] * UNITS[s["unit"]] > 366 * 86400:
            raise ValueError("The interval can be at most a year")

    if not TIME_RE.match(str(s["time"])):
        if mode in ("daily", "weekly", "monthly"):
            raise ValueError("Enter a time like 04:30")
        s["time"] = DEFAULTS["time"]
    h, m = s["time"].split(":")
    s["time"] = f"{int(h):02d}:{m}"

    try:
        days = sorted({int(d) for d in s["days"]})
        assert all(0 <= d <= 6 for d in days)
    except Exception:
        days = []
    if mode == "weekly" and not days:
        raise ValueError("Pick at least one day of the week")
    s["days"] = days or DEFAULTS["days"]

    dom = str(s["day_of_month"]).upper()
    if dom != "L" and not (dom.isdigit() and 1 <= int(dom) <= 31):
        if mode == "monthly":
            raise ValueError("Pick a day of the month")
        dom = DEFAULTS["day_of_month"]
    s["day_of_month"] = dom

    if s["monthly_by"] not in ("date", "weekday"):
        s["monthly_by"] = "date"
    try:
        s["nth"] = int(s["nth"])
        assert s["nth"] in (-1, 1, 2, 3, 4, 5)
    except Exception:
        s["nth"] = DEFAULTS["nth"]
    try:
        s["weekday"] = int(s["weekday"])
        assert 0 <= s["weekday"] <= 6
    except Exception:
        s["weekday"] = DEFAULTS["weekday"]

    s["cron"] = str(s["cron"]).strip()
    if mode == "cron":
        Cron(s["cron"]).next_after(dt.datetime.now(ZoneInfo(s["tz"])))  # raises if invalid or never fires
    elif mode in ("daily", "weekly", "monthly"):
        Cron(to_cron(s))
    return s


def upcoming(s, base_ts, now_ts, count=5):
    """Next `count` run times (epoch seconds) after base_ts. A first run already
    in the past means 'due now' and is reported as now_ts."""
    mode = s["mode"]
    if mode == "manual":
        return []
    out = []
    if mode == "interval":
        period = s["every"] * UNITS[s["unit"]]
        t = base_ts + period
        if t < now_ts:
            out.append(now_ts)
            t = now_ts + period
        while len(out) < count:
            out.append(t)
            t += period
        return out
    tz = ZoneInfo(s["tz"])
    cron = Cron(to_cron(s))
    t = dt.datetime.fromtimestamp(base_ts, tz)
    while len(out) < count:
        t = cron.next_after(t)
        ts = t.timestamp()
        if ts < now_ts:
            if not out:
                out.append(now_ts)  # missed run: due now
            t = cron.next_after(dt.datetime.fromtimestamp(now_ts, tz)) - dt.timedelta(minutes=1)
            continue
        out.append(ts)
    return out
