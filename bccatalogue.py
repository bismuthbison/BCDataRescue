"""BC Data Catalogue (CKAN) support for bcdata_tui: index, topic filters, file downloads.

No UI code here, so it can be tested on its own. Only plain-file resources (csv, xlsx, pdf, zip, gpkg ...)
are downloaded. Service links (WMS, KML, ArcGIS REST, oracle_sde, 'multiple') are listed but skipped:
they point at the same layers the WFS browser already covers, or at web services, not files.

The catalogue is a CKAN site. Its public API (`package_search`, `package_show`) needs no login. In CKAN
vocabulary a "dataset" (called a "package") holds several "resources", which are the individual files or
service links. This module works with a slimmed-down copy of each dataset (see _slim) to keep the cached
index small (about 5 MB for ~3,400 datasets, instead of ~70 MB of raw API output).

Typical use:
    index = fetch_index(cache_path)                       # list of slim dataset dicts
    hits = [d for d in index if matches(d, preset="Climate")]
    result = download_dataset(hits[0], out_root)          # {"files": {...}, "errors": [...]}
"""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

# Base URL of the catalogue's CKAN action API.
API = "https://catalogue.data.gov.bc.ca/api/3/action/"
# Resource formats (as the catalogue labels them) that are real downloadable files. Everything else
# (wms, kml, arcgis_rest, oracle_sde, multiple, html, ...) is a service or a web page.
FILE_FORMATS = {"csv", "xlsx", "xls", "pdf", "txt", "zip", "gpkg", "geojson", "fgdb", "shp", "kmz", "json", "xml", "gdb"}
# Some files are labelled just "other"; for those we fall back to the URL's file extension.
FILE_EXTS = {".csv", ".xlsx", ".xls", ".pdf", ".txt", ".zip", ".gpkg", ".geojson", ".json", ".xml", ".kmz", ".gdb",
             ".docx", ".pptx", ".tif", ".tiff", ".nc"}
# Some government hosts (www2.gov.bc.ca) drop connections from the default python-requests User-Agent.
HEADERS = {"User-Agent": "BCDataRescue/1.0 (open-data archiver)"}
# A dataset whose listed files total more than this needs a second press of `d` to confirm. Only
# files with a known size count, so the real total can be larger (many resources list no size).
BIG_BYTES = 500 * 1024 ** 2

# Ready-made topic filters: name -> regex, matched (case-insensitively) against title + tags + groups +
# publishing organisation. \b marks a word boundary so "sex" does not match "Essex". These are a
# convenience for finding likely datasets, not an authoritative classification.
PRESETS = {
    "Climate": r"climate|greenhouse|emission|carbon|sea level|drought|glacier|snow|permafrost|\bghg\b",
    "Gender & sex": r"\bgender\b|\bsex\b|\bwomen\b|\bfemale\b|\bmale\b|2slgbtq|\blgbt",
    "Census": r"\bcensus\b",
    "Indigenous": r"indigenous|first nation|treaty|aboriginal|\bm[eé]tis\b",
    "Species at risk": r"species at risk|species and ecosystems at risk|critical habitat|endangered|\bcdc\b",
    "Water": r"\bwater\b|aquifer|flood|watershed|hydromet|\bstream",
    "Wildfire & air": r"wildfire|air quality|smoke|\bfire\b",
}


def human_size(n) -> str:
    """Bytes as a short string ("21.4 KB", "1.0 GB"); "?" when the size is unknown (None)."""
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _slim(d: dict) -> dict:
    """Reduce one raw CKAN dataset to the fields this tool uses (title, publisher, tags, licence, dates,
    description and each resource's name/format/url/size). The full record is still saved at download
    time, via package_show, into dataset.json."""
    org = d.get("organization") or {}
    res = []
    for r in d.get("resources", []):
        size = r.get("size")
        try:  # sizes arrive as ints, numeric strings, "null", or missing; normalise to int-or-None
            size = int(size) if size not in (None, "", "null") else None
        except (TypeError, ValueError):
            size = None
        res.append({"id": r.get("id"), "name": r.get("name") or "", "format": (r.get("format") or "").lower(),
                    "url": r.get("url") or "", "size": size, "last_modified": r.get("last_modified")})
    return {
        "name": d["name"], "title": d.get("title") or d["name"], "org": org.get("title") or "(no organisation)",
        "notes": d.get("notes") or "", "tags": [t["name"] for t in d.get("tags", [])],
        "groups": [g["title"] for g in d.get("groups", [])], "license": d.get("license_title") or "",
        "modified": d.get("metadata_modified") or "", "resources": res,
    }


def fetch_index(cache: Path, refresh: bool = False, max_age_h: float = 24.0) -> list[dict]:
    """All catalogue datasets in slim form (see _slim).

    The result is cached at `cache` and reused for `max_age_h` hours, so after the first run the browser
    opens instantly. refresh=True forces a new download. The API returns at most 1000 datasets per
    request, so the listing is fetched page by page, sorted by name so the paging is stable.
    """
    if not refresh and cache.exists() and (time.time() - cache.stat().st_mtime) < max_age_h * 3600:
        try:
            return json.loads(cache.read_text())
        except (OSError, ValueError):
            pass
    out, start, rows = [], 0, 1000
    with requests.Session() as s:
        s.headers.update(HEADERS)
        while True:
            r = s.get(API + "package_search", params={"rows": rows, "start": start, "sort": "name asc"}, timeout=120)
            r.raise_for_status()
            result = r.json()["result"]
            out.extend(_slim(d) for d in result["results"])
            start += rows
            if start >= result["count"] or not result["results"]:
                break
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out))
    return out


def is_downloadable(res: dict) -> bool:
    """True if a resource is a real file we can fetch: an http(s) URL whose format (or, for "other",
    file extension) is one we know. Service links and web pages return False."""
    url = res.get("url", "")
    if not url.lower().startswith(("http://", "https://")):
        return False
    ext = Path(urlparse(url).path).suffix.lower()
    fmt = res.get("format", "")
    return fmt in FILE_FORMATS or (fmt == "other" and ext in FILE_EXTS)


def files_of(ds: dict) -> list[dict]:
    """The downloadable resources of a dataset."""
    return [r for r in ds["resources"] if is_downloadable(r)]


def total_size(ds: dict) -> int:
    """Combined listed size of the downloadable files, in bytes. Files with no listed size count as 0,
    so this is a lower bound."""
    return sum(r["size"] or 0 for r in files_of(ds))


def blob(ds: dict) -> str:
    """The lower-cased text that searches and topic filters look at: title, publisher, tags, groups.
    The long description is left out on purpose, since it makes filters match far too much."""
    return " ".join([ds["title"], ds["org"], *ds["tags"], *ds["groups"]]).lower()


def matches(ds: dict, needle: str = "", preset: str | None = None, only_files: bool = True) -> bool:
    """Does a dataset pass the current filters? `needle` is a plain substring (the search box), `preset`
    a key of PRESETS, and only_files hides datasets with nothing downloadable."""
    if only_files and not files_of(ds):
        return False
    b = blob(ds)
    if needle and needle.lower() not in b:
        return False
    if preset and not re.search(PRESETS[preset], b):
        return False
    return True


def _safe_name(res: dict, used: set[str]) -> str:
    """A safe local filename for a resource.

    The name comes from the remote URL, which we don't control, so it is sanitised: anything outside
    letters, digits, dot, dash, underscore and space becomes "_" (this removes "/" and "\\", so a name
    can never point outside the dataset folder) and leading dots are stripped (no "." / ".." / hidden
    files). `used` holds names already taken in this dataset; a clash gets the resource id as a prefix.
    """
    name = unquote(Path(urlparse(res["url"]).path).name) or res["name"] or "file"
    name = re.sub(r"[^\w.\- ]", "_", name).lstrip(".") or "file"
    if not Path(name).suffix and res.get("format"):
        name += "." + res["format"]
    if name in used:
        name = f"{(res.get('id') or 'dup')[:8]}_{name}"
    used.add(name)
    return name


def _sig(path: Path) -> list[int]:
    """File fingerprint: [size in bytes, modification time in ns]. Duplicated in bcdata_tui.py."""
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def dataset_dir(out_root: Path, name: str) -> Path:
    """Folder holding one dataset's files: <out_root>/catalogue/<dataset name>/."""
    return out_root / "catalogue" / name


def download_dataset(ds: dict, out_root: Path, progress=None, max_file_bytes: int | None = None) -> dict:
    """Download every file of a dataset into <out_root>/catalogue/<name>/ plus dataset.json (full metadata).

    progress(frac, text) is called as bytes arrive. Returns {"files": {relpath: [size, mtime_ns]},
    "errors": [str]}. Files are written as .part and renamed, so a half-finished file is never mistaken
    for a finished one.
    """
    folder = dataset_dir(out_root, ds["name"])
    folder.mkdir(parents=True, exist_ok=True)
    files = files_of(ds)
    known_total = sum(f["size"] or 0 for f in files)  # bytes we expect; 0 if the catalogue lists no sizes
    done_bytes, errors, written, used = 0, [], {}, set()
    report = progress or (lambda frac, text: None)  # a do-nothing callback when none is given

    with requests.Session() as s:
        s.headers.update(HEADERS)
        for i, res in enumerate(files, 1):
            # Download to <name>.part and rename on success, so a crash can't leave a truncated file
            # that looks complete.
            fname = _safe_name(res, used)
            dest, part = folder / fname, folder / (fname + ".part")
            try:
                with s.get(res["url"], stream=True, timeout=(15, 120)) as r:
                    r.raise_for_status()
                    # The catalogue answers 200 + its own web page for paths that don't exist, so a dead
                    # link would otherwise be saved as a bogus "file". Real data files are never HTML.
                    dead = "server returned a web page, not a file (dead link?)"
                    if "text/html" in r.headers.get("content-type", "").lower():
                        raise ValueError(dead)
                    declared = r.headers.get("content-length")
                    if max_file_bytes and declared and int(declared) > max_file_bytes:
                        raise ValueError(f"file is {human_size(int(declared))}, over the {human_size(max_file_bytes)} limit")
                    first, this_file = True, 0
                    with open(part, "wb") as fh:
                        for chunk in r.iter_content(1 << 16):
                            this_file += len(chunk)
                            if max_file_bytes and this_file > max_file_bytes:
                                raise ValueError(f"file passed the {human_size(max_file_bytes)} limit")
                            if first:
                                first = False
                                if chunk[:300].lstrip().lower().startswith((b"<!doctype html", b"<html")):
                                    raise ValueError(dead)
                            fh.write(chunk)
                            done_bytes += len(chunk)
                            # Progress by bytes when sizes are known; otherwise by whole files finished.
                            # Capped at 0.99 because listed sizes can be wrong; 1.0 is reported only at the end.
                            frac = done_bytes / known_total if known_total else (i - 1) / len(files)
                            report(min(frac, 0.99), f"file {i}/{len(files)}")
                part.replace(dest)
                written[fname] = _sig(dest)
            except Exception as e:  # keep going: one dead link shouldn't lose the whole dataset
                errors.append(f"{fname}: {e}")
                part.unlink(missing_ok=True)

        # Full metadata (description, contacts, licence, dates) is the part most worth archiving, and it
        # can change or vanish with the live site. package_show returns more than the slim index kept. If
        # that call fails we fall back to the slim record rather than lose the metadata altogether.
        try:
            r = s.get(API + "package_show", params={"id": ds["name"]}, timeout=60)
            r.raise_for_status()
            meta = r.json()["result"]
        except Exception:
            meta = dict(ds)
        # Add our own bookkeeping under "_archive": when we downloaded, which files we got, what failed,
        # and which resources were skipped as service links (so the archive says what it does NOT contain).
        meta["_archive"] = {"downloaded": datetime.now(timezone.utc).isoformat(), "files": list(written),
                            "errors": errors, "skipped_resources": [
                                {"name": x["name"], "format": x["format"], "url": x["url"]}
                                for x in ds["resources"] if not is_downloadable(x)]}
        meta_path = folder / "dataset.json"
        meta_path.write_text(json.dumps(meta, indent=2, default=str))
        written["dataset.json"] = _sig(meta_path)
    report(1.0, "done")
    return {"files": written, "errors": errors}


def status(name: str, manifest: dict, out_root: Path) -> str | None:
    """Is a dataset downloaded, and is it still intact? Compares the files on disk to the fingerprints
    in the manifest (key "cat:<name>").

    'ok' = every file we wrote is still there, untouched; 'modified' = changed/missing/partial/unrecorded;
    None = never downloaded."""
    folder = dataset_dir(out_root, name)
    if not folder.exists():
        return None
    rec = (manifest.get("cat:" + name) or {}).get("files")
    if not rec:
        return "modified"
    for rel, sig in rec.items():
        p = folder / rel
        if not p.exists() or _sig(p) != sig:
            return "modified"
    return "ok"
