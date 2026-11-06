"""WFS layer definitions: a 24-hour on-disk cache, and a comparison against what you saved.

No UI code here, so it can be tested on its own.

A layer's "definition" is its description, table comments and column list (names, types, explanations).
bcdata fetches it from the BC Data Catalogue's API by layer name (about 0.15 s per layer). This module adds:

  * LayerInfoCache - remembers definitions on disk for `max_age_h` hours (default 24), so browsing is
    repeatable offline and doesn't re-ask the government API for layers you've already looked at. If a
    refresh fails, the old copy is served and flagged as stale instead of raising an error.
  * diff_definitions / describe_diff - compare the definition saved next to a downloaded layer
    (<TABLE>.info.json) with the current one, to show whether the layer's documentation or columns have
    changed since you downloaded it, and so whether a re-download is worthwhile.

The feature count is deliberately NOT cached: it is the value that changes fastest and is a cheap call.
"""
import json
import os
import threading
import time
from pathlib import Path


def _fetch_live(table: str) -> dict:
    """The default fetcher: ask the catalogue (through bcdata) for the layer's definition."""
    import bcdata  # imported here so this module (and its tests) don't need bcdata unless fetching for real
    return dict(bcdata.get_table_definition(table))


def _is_empty(definition: dict) -> bool:
    """True when the catalogue found nothing for the layer (no description and no columns)."""
    return not definition.get("schema") and not definition.get("description")


class LayerInfoCache:
    """Layer definitions remembered on disk, refreshed when older than `max_age_h` hours.

    Safe to use from several threads (the TUI's workers do). The file is one JSON object:
    {table: {"fetched": <epoch seconds>, "definition": {...}}}.
    """

    def __init__(self, path: Path, max_age_h: float = 24.0, fetch=_fetch_live):
        self.path = Path(path)
        self.max_age = max_age_h * 3600
        self._fetch = fetch  # injectable so tests can run without the network
        self._lock = threading.Lock()
        self._data: dict = self._load()

    def _load(self) -> dict:
        """Read the cache file; a missing or corrupt file just means an empty cache."""
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        """Write the cache atomically (temp file + rename) so a crash can't leave half a file.
        The cache is a convenience, so a failure to write it is ignored."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._data))
            os.replace(tmp, self.path)
        except OSError:
            pass

    def get(self, table: str, force: bool = False) -> tuple[dict, float, bool]:
        """Return (definition, fetched_at, stale).

        fetched_at is when the definition was fetched (epoch seconds). `stale` is True only when a refresh
        was needed but failed and an older copy is being served. force=True always asks the catalogue
        (used when saving a download, so the archive record is never a day-old copy). Raises if the
        catalogue can't be reached and nothing is cached.
        """
        with self._lock:
            rec = self._data.get(table)
        if rec and not force and time.time() - rec["fetched"] < self.max_age:
            return rec["definition"], rec["fetched"], False
        try:
            definition = self._fetch(table)
        except Exception:
            if rec:  # offline or the API is down: better an old definition than none
                return rec["definition"], rec["fetched"], True
            raise
        now = time.time()
        if not _is_empty(definition):  # "found nothing" may be a hiccup: don't keep it for a day
            with self._lock:
                self._data[table] = {"fetched": now, "definition": definition}
                self._save()
        return definition, now, False


# ---------------------------------------------------------------- comparing against a saved copy
COLUMN_FIELDS = ("data_type", "column_comments", "data_precision")  # what counts as a column having changed


def _norm(text) -> str:
    """Collapse whitespace so trailing spaces and line wrapping never count as an edit."""
    return " ".join(str(text or "").split())


def diff_definitions(saved: dict, live: dict, saved_count=None, live_count=None) -> dict:
    """Compare a saved definition (from <TABLE>.info.json) with the current one.

    Returns {"description": bool, "comments": bool, "added": [names], "removed": [names],
    "changed": {column: [fields]}, "count": (old, new) or None, "live_incomplete": bool}.
    If the live definition came back empty (the catalogue had nothing) the comparison can't be trusted, so
    `live_incomplete` is set and no changes are reported, rather than reporting every column as removed.
    """
    diff = {"description": False, "comments": False, "added": [], "removed": [], "changed": {},
            "count": None, "live_incomplete": False}
    if isinstance(saved_count, int) and isinstance(live_count, int) and saved_count != live_count:
        diff["count"] = (saved_count, live_count)
    if _is_empty(live) and not _is_empty(saved):
        diff["live_incomplete"] = True
        return diff
    diff["description"] = _norm(saved.get("description")) != _norm(live.get("description"))
    diff["comments"] = _norm(saved.get("comments")) != _norm(live.get("comments"))
    old = {c.get("column_name"): c for c in saved.get("schema") or []}
    new = {c.get("column_name"): c for c in live.get("schema") or []}
    diff["added"] = sorted(n for n in new if n not in old)
    diff["removed"] = sorted(n for n in old if n not in new)
    for name in sorted(set(old) & set(new)):
        fields = [f for f in COLUMN_FIELDS if _norm(old[name].get(f)) != _norm(new[name].get(f))]
        if fields:
            diff["changed"][name] = fields
    return diff


def has_changes(diff: dict) -> bool:
    """Did the layer's documentation or columns change? (A different feature count alone doesn't count:
    it is normal for live layers and is reported separately.)"""
    return bool(diff["description"] or diff["comments"] or diff["added"] or diff["removed"] or diff["changed"])


def describe_diff(diff: dict) -> list[str]:
    """Human-readable lines for a diff; an empty list means nothing to report."""
    lines = []
    if diff["live_incomplete"]:
        lines.append("The catalogue returned no definition just now, so no comparison is possible.")
        return lines
    if diff["description"]:
        lines.append("The description has changed.")
    if diff["comments"]:
        lines.append("The table comments have changed.")
    if diff["added"]:
        lines.append("Columns added: " + ", ".join(diff["added"]))
    if diff["removed"]:
        lines.append("Columns removed: " + ", ".join(diff["removed"]))
    for name, fields in diff["changed"].items():
        lines.append(f"Column {name}: {', '.join(f.replace('_', ' ') for f in fields)} changed")
    if diff["count"]:
        lines.append(f"Feature count: {diff['count'][0]:,} when you downloaded it, {diff['count'][1]:,} now.")
    return lines
