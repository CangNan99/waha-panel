"""Per-session reply epochs; archiving history never makes it a new message."""
import math
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import datetime, timedelta


def message_timestamp(value, now):
    if value is None or isinstance(value, bool):
        return None
    try:
        stamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(stamp) or stamp <= 0:
        return None
    if stamp >= 100000000000:
        stamp /= 1000
    # A small clock skew is accepted, but never arbitrarily future-dated data.
    return stamp if stamp <= now + 5 else None


def active_window_start(windows, current):
    weekdays = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    spans = []
    for offset in range(-8, 1):
        day = current.date() + timedelta(days=offset)
        for item in windows.get(weekdays[day.weekday()], []):
            if item["start"] == item["end"]:
                continue
            start = datetime.fromisoformat(str(day) + "T" + item["start"]).replace(tzinfo=current.tzinfo)
            end = datetime.fromisoformat(str(day) + "T" + item["end"]).replace(tzinfo=current.tzinfo)
            if end < start:
                end += timedelta(days=1)
            spans.append((start.timestamp(), end.timestamp()))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return next((start for start, end in merged if start <= current.timestamp() < end), None)


class AutoReplyPolicy:
    def __init__(self, database_path, clock):
        self.database_path = database_path
        self.clock = clock
        self.started_at = clock()
        self.lock = threading.RLock()
        self.sessions = {}
        self.chat_epochs = {}

    def _session(self, name):
        if name not in self.sessions:
            with closing(sqlite3.connect(self.database_path)) as connection:
                row = connection.execute(
                    "SELECT value FROM session_settings WHERE session_name=? AND key='_auto_reply_since'", (name,)
                ).fetchone()
            try:
                previous = float(row[0]) if row else 0
                previous = previous if math.isfinite(previous) else 0
            except (TypeError, ValueError):
                previous = 0
            self.sessions[name] = {"connected": False, "since": max(self.started_at, previous),
                                   "epoch": uuid.uuid4().hex, "observed_at": 0, "event_at": 0}
        return self.sessions[name]

    def invalidate(self, name):
        with self.lock:
            state = self._session(name)
            state["since"] = max(state["since"], self.clock())
            state["epoch"] = uuid.uuid4().hex
            with closing(sqlite3.connect(self.database_path)) as connection, connection:
                connection.execute(
                    "INSERT INTO session_settings(session_name,key,value,updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(session_name,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (name, "_auto_reply_since", str(state["since"]), int(self.clock())),
                )

    def observe(self, name, connected, observed_at=None, event_timestamp=None):
        with self.lock:
            state = self._session(name)
            if event_timestamp is not None:
                # Event clocks can have only second precision. Order events by
                # their own clock; snapshots use a separate local read barrier.
                if event_timestamp < state["event_at"]:
                    return
                state["event_at"] = event_timestamp
                observed = max(state["observed_at"], self.clock())
            else:
                observed = self.clock() if observed_at is None else observed_at
                if observed < state["observed_at"]:
                    return
            state["observed_at"] = observed
            if state["connected"] != bool(connected):
                state["connected"] = bool(connected)
                self.invalidate(name)

    def chat_changed(self, name, chat_id):
        with self.lock:
            self.chat_epochs[(name, chat_id)] = (uuid.uuid4().hex, self.clock())

    def ticket(self, name, timestamp, window_start=0, chat_id=""):
        with self.lock:
            state = self._session(name)
            if not state["connected"] or timestamp is None:
                return None
            chat_epoch, chat_since = self.chat_epochs.get((name, chat_id), (None, 0))
            if timestamp <= max(state["since"], window_start, chat_since):
                return None
            return state["epoch"], chat_epoch
