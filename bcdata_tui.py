#!/usr/bin/env python
"""Terminal UI for exploring and downloading BC open data.

Three sections: pick one on the splash screen (0 there quits; Esc returns to it), or cycle with `s`:
  layers     ~900 DataBC WFS vector layers  -> <output_dir>/layers/<TABLE>.gpkg + <TABLE>.info.json
  catalogue  BC Data Catalogue datasets     -> <output_dir>/catalogue/<dataset>/<files> + dataset.json
             (CSV/XLSX/PDF/ZIP/GPKG... files; service links such as WMS/KML are listed but skipped)
  news       The Tyee (RSS feed) - a read-only reader with the lead image in green; nothing is saved to disk

Run inside the `geo` env:   python bcdata_tui.py [output_dir]      (default output_dir: ./bcdata)

Keys:  /  search     m  mark/unmark     d  download (marked, else highlighted)     c  clear marks
       Esc  back to the main menu (splash)     s  cycle section (layers > catalogue > news)
       f  cycle topic filter (catalogue)     a  show/hide datasets with no downloadable files
       x  check downloaded layers for changes (layers)
       Enter  read the highlighted article (news)     o  open it in your browser     r  refresh the feed
       j / k  scroll the details pane down / up (any section)     q  quit
Status icons: green check = downloaded and untouched, grey ? = changed/missing/incomplete.

How the pieces fit together
  * Every selectable item has a string KEY. A WFS layer's key is its table name ("WHSE_X.TABLE_NAME");
    a catalogue dataset's key is "cat:" + its catalogue name. The same key is used for the tree node,
    the marked set, the download queue and the manifest, so layers and datasets can be mixed freely.
  * Downloads run in background threads (Textual "workers") so the UI never freezes. Worker threads
    must not touch widgets directly: they call `self.call_from_thread(...)` (aliased `ui` below).
  * The MANIFEST file records the size + modification time of every file this tool wrote. It is what
    lets the tree show a green check (untouched) or a grey ? (changed, missing or incomplete).
  * All catalogue logic (index, filtering, file downloads) lives in bccatalogue.py and all news logic (feed,
    article text) in thetyee.py. Neither has any UI code.
  * News keys are "news:" + the article URL. News is read-only: it can't be marked or downloaded.
  * A layer's description/columns are cached on disk for 24 h (layerinfo.py). Downloads always fetch them fresh,
    and the saved <TABLE>.info.json is later compared with the current definition to show what has changed.
"""
# --- standard library
import json
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

# --- third-party
import pandas as pd
import textual.renderables.bar as _textual_bar  # only used to patch the progress-bar gradient (see below)
from bcdata.wfs import BCWFS  # bcdata's WFS client; used directly so we can report progress page by page
from rich.markup import escape  # always escape server/user text before putting it in Rich markup
from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Gradient
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Footer, Header, Input, Label, ProgressBar, RichLog, Static, Tree

# --- this project
import bcdata  # layer list, table definitions (columns) and feature counts
import bccatalogue as bc  # BC Data Catalogue support: index, filters, file downloads
import halfblock  # draws the news lead image as green half-block text
import layerinfo  # 24-hour cache of layer definitions + comparison with the copy saved at download time
import thetyee  # The Tyee news support: RSS feed and article text


def _fixed_rainbow(text, gradient, width):
    """Textual anchors a bar's gradient to its moving head and flips it, so a half-full bar shows a
    squashed, reversed slice. Colour by fixed position instead: red at the left edge, violet at the
    right end of the full bar, revealed as the bar fills."""
    if not width:
        return
    span = max(width - 1, 1)
    for i in range(len(text)):
        text.stylize(Style.from_color(gradient.get_rich_color(min(i / span, 1.0))), i, i + 1)


# Monkey-patch: swap in our version of Textual's private gradient helper. This is the one place the
# tool depends on Textual internals (written against textual 8.x); if a Textual update breaks the
# rainbow bars, this line is the likely cause.
_textual_bar._apply_gradient = _fixed_rainbow

# Red -> orange -> yellow -> green -> blue -> indigo -> violet, evenly spaced along the bar.
RAINBOW = Gradient(
    (0.0, "#ff0000"), (1 / 6, "#ff8800"), (2 / 6, "#ffee00"), (3 / 6, "#22dd22"),
    (4 / 6, "#2299ff"), (5 / 6, "#5533ff"), (1.0, "#cc22ff"),
)

# Where everything is saved. Read from the command line when the module loads: `python bcdata_tui.py [dir]`.
OUT_DIR = Path(sys.argv[1] if len(sys.argv) > 1 else "bcdata")
# WFS layers above this many features are held entirely in memory while downloading, so they need a
# second press of `d` to confirm (the first press only warns). Catalogue datasets have their own
# byte-size threshold, bc.BIG_BYTES.
BIG_LAYER = 250_000
LAYERS_DIR = OUT_DIR / "layers"  # WFS map layers (.gpkg); catalogue datasets live in OUT_DIR/catalogue/
LAYER_INFO_CACHE = OUT_DIR / ".layer_info_cache.json"  # layer descriptions/columns, refreshed after 24 h
MANIFEST = OUT_DIR / ".bcdata_tui_manifest.json"  # remembers size+mtime of each file we wrote


def _sig(path: Path) -> list[int]:
    """A file's fingerprint: [size in bytes, modification time in ns]. Cheap, but not a content hash, so an
    edit that leaves both unchanged would go unnoticed (documented in the README). Same as bc._sig."""
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def load_manifest() -> dict:
    """Read the manifest ({key: fingerprints}); a missing or corrupt file just means "nothing recorded"."""
    try:
        return json.loads(MANIFEST.read_text())
    except (OSError, ValueError):
        return {}


def saved_snapshot(table: str) -> dict | None:
    """The definition saved next to a downloaded layer (<TABLE>.info.json), or None if there isn't a readable one."""
    try:
        return json.loads((LAYERS_DIR / f"{table}.info.json").read_text())
    except (OSError, ValueError):
        return None


def layer_status(table: str, manifest: dict) -> str | None:
    """'ok' = both files present and untouched since we wrote them; 'modified' = anything else
    (changed, one file missing, or files we have no record of); None = not downloaded."""
    gpkg, info = LAYERS_DIR / f"{table}.gpkg", LAYERS_DIR / f"{table}.info.json"
    if not gpkg.exists() and not info.exists():
        return None
    rec = manifest.get(table)
    if gpkg.exists() and info.exists() and rec and rec.get("gpkg") == _sig(gpkg) and rec.get("info") == _sig(info):
        return "ok"
    return "modified"


# ---------- Apple ][ look: splash-screen art and colour theme ----------
GREEN = "#33ff33"  # Apple II phosphor
# Hand-drawn ASCII sketch of British Columbia (NW panhandle, Rockies diagonal, Vancouver Island,
# Haida Gwaii as the two "o"). Not to scale. Raw string: it is full of backslashes.
BC_ART = r"""
 ______________________
  \                    |
    \                  |
   o   \               |
    o    \             |
           \           |
         /  \          |
          /  \          \
           /   \          \
            /_  \          \
             /    \          \
                   \__________\
""".strip("\n").splitlines()
# Text printed to the left of the art, keyed by the art row it sits beside.
TITLE_LINES = {
    2: "B C D A T A R E S C U E",
    3: "=======================",
    5: "RESCUE BC OPEN GEODATA",
    6: "ARCHIVE LAYERS LOCALLY",
}

# Textual theme: pure black background, phosphor-green text, amber warnings. Registered in on_mount.
APPLE2 = Theme(
    name="apple2",
    primary=GREEN, secondary="#1fa81f", accent="#99ff99",
    foreground=GREEN, background="#000000", surface="#000a00", panel="#021402",
    success="#99ff99", warning="#ffb000", error="#ff5533",
    dark=True,
    variables={"block-cursor-foreground": "#000000", "block-cursor-background": GREEN,
               "input-cursor-foreground": "#000000", "input-cursor-background": GREEN,
               "scrollbar": "#1fa81f", "scrollbar-background": "#000000"},
)


class Splash(ModalScreen[str]):
    """Apple ][ style splash and main menu: green phosphor on black, centred, 50% of the screen. Asks which
    section to open: 1/L = layers, 2/C = catalogue, 3/N = news (Enter = layers), or 0 to quit the program.
    Dismisses with "layers", "catalogue", "news" or "quit". Esc in the main screen brings it back.

    A ModalScreen sits on top of the main screen while the layer list loads in the background, so the
    app is ready by the time a choice is made."""

    DEFAULT_CSS = f"""
    Splash {{ align: center middle; background: black 60%; }}
    #splash {{
        /* 50% of the terminal, but never smaller than the art + menu need (68x22), or it would be clipped */
        width: 50%; height: 50%; min-width: 68; min-height: 22;
        background: black; color: {GREEN}; border: heavy {GREEN};
        align: center middle;
    }}
    #splash Static {{ width: auto; height: auto; background: black; color: {GREEN}; }}
    """
    # key pressed -> source chosen. Any other key is ignored on purpose (see on_key).
    CHOICES = {"1": "layers", "l": "layers", "enter": "layers", "2": "catalogue", "c": "catalogue",
               "3": "news", "n": "news", "0": "quit"}

    def __init__(self):
        super().__init__()
        self.cursor_on = True  # toggled by blink() to make the prompt cursor flash

    def compose(self) -> ComposeResult:
        with Vertical(id="splash"):
            yield Static(self.render_text(), id="splash_text")

    def render_text(self) -> Text:
        """Build the whole splash as one block of text: header, title beside the map, then the menu."""
        t = Text(style=GREEN)
        t.append("APPLE ][\n", style=f"bold {GREEN}")
        for i, art in enumerate(BC_ART):
            t.append(f"{TITLE_LINES.get(i, ''):<26}", style=f"bold {GREEN}" if i in (2, 3) else GREEN)
            t.append(art + "\n")
        t.append("\n")
        t.append("]RUN BCDATARESCUE\n")
        t.append("]1 LAYERS     MAP LAYERS (GPKG)\n")
        t.append("]2 CATALOGUE  CSV, XLSX, PDF, ZIP\n")
        t.append("]3 NEWS       THE TYEE (RSS)\n")
        t.append("]0 QUIT\n")
        t.append("]SOURCE (0-3)? ")
        t.append("█" if self.cursor_on else " ")
        return t

    def on_mount(self) -> None:
        self.set_interval(0.5, self.blink)  # flash the cursor twice a second

    def blink(self) -> None:
        """Flip the cursor on/off and redraw the text."""
        self.cursor_on = not self.cursor_on
        self.query_one("#splash_text", Static).update(self.render_text())

    def on_key(self, event) -> None:
        """Swallow every key (so e.g. `q` can't quit the app from behind the splash); only a valid
        choice closes the screen."""
        event.stop()
        event.prevent_default()  # keys never leak through to the app's own bindings
        choice = self.CHOICES.get(event.key.lower())
        if choice:
            self.dismiss(choice)


class BCDataTUI(App):
    """The main application: a tree of layers/datasets on the left, details on the right, two progress
    bars and a log along the bottom. See the module docstring for the key/threading conventions."""

    # Layout and colours. The rainbow bars are deliberately left out of the green recolouring: only their
    # empty track and the percentage text are made green.
    CSS = """
    #left { width: 45%; }
    #search { dock: top; }
    #right { width: 55%; border-left: solid $primary; }
    #info { padding: 0 1; }
    #log { height: 10; border-top: solid $primary; }
    #bars { height: auto; border-top: solid $primary; padding: 0 1; }
    Screen { background: #000000; color: #33ff33; }
    Header { background: #33ff33; color: #000000; text-style: bold; }
    HeaderTitle { color: #000000; }
    Footer { background: #33ff33; color: #000000; }
    Footer .footer-key--key { background: #000000; color: #33ff33; text-style: bold; }
    Footer .footer-key--description { background: #33ff33; color: #000000; }
    Input { background: #000000; color: #33ff33; border: tall #1fa81f; }
    Input:focus { border: tall #33ff33; }
    Input > .input--placeholder { color: #1fa81f; }
    Tree { background: #000000; color: #33ff33; }
    Tree > .tree--guides { color: #1a7a1a; }
    Tree > .tree--cursor { background: #33ff33; color: #000000; text-style: bold; }
    Tree:focus > .tree--cursor { background: #33ff33; color: #000000; }
    Tree > .tree--highlight { background: #0a3d0a; }
    #info, RichLog { background: #000000; color: #33ff33; }
    /* progress bars: keep the rainbow fill, make the track and text phosphor green */
    Bar > .bar--bar { color: #0a3d0a; background: #000000; }
    Bar > .bar--complete { color: #33ff33; }
    PercentageStatus { color: #33ff33; }
    Label { color: #33ff33; }
    .barrow { height: 1; }
    .barrow Label { width: 28; }
    .barrow ProgressBar { width: 1fr; }
    .barrow Bar { width: 1fr; }
    """
    # (key, action method name without the "action_" prefix, label shown in the footer)
    BINDINGS = [
        Binding("escape", "main_menu", "Menu"),
        Binding("slash", "focus_search", "Search"),
        Binding("m", "mark", "Mark"),
        Binding("d", "download", "Download"),
        Binding("c", "clear_marks", "Clear marks", show=False),
        Binding("s", "switch_source", "Section"),
        Binding("f", "next_preset", "Topic"),
        Binding("a", "toggle_files", "Files-only", show=False),
        Binding("x", "check_changes", "Changes"),
        Binding("o", "open_link", "Open"),
        Binding("r", "refresh_news", "Refresh"),
        Binding("j", "scroll_info(1)", "Scroll down", show=False),
        Binding("k", "scroll_info(-1)", "Scroll up", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Show (and allow) only the keys that make sense in the current section. Besides being tidier, this
        keeps the footer short enough to fit a narrow terminal. Returning False hides and disables a key."""
        only = {"next_preset": "catalogue", "toggle_files": "catalogue", "open_link": "news",
                "refresh_news": "news", "check_changes": "layers"}
        if action in only:
            return self.mode == only[action]
        if action in ("mark", "download", "clear_marks"):
            return self.mode != "news"  # news is read-only
        return True

    def __init__(self):
        super().__init__()
        self.tables: list[str] = []                  # all WFS layer names, sorted (loaded in the background)
        self.marked: set[str] = set()                # keys queued for download (layers and datasets mixed)
        self.info_cache: dict[str, dict] = {}        # layer key -> its description/columns/count, fetched once
        self.pending_big: set[str] = set()           # big items already warned about: the next `d` confirms
        self.current: str | None = None              # key of the highlighted item (default download target)
        self.filter_text = ""                        # contents of the search box
        self.manifest: dict = load_manifest()        # fingerprints of files we wrote; drives the check/? icons
        self.nodes: dict[str, object] = {}          # item key -> tree leaf
        self.branch_schema: dict[int, str] = {}      # branch node id -> group name (schema or publisher)
        self.expanded_schemas: set[str] = set()      # branches the user has opened
        self.mode = "layers"                         # "layers" (WFS) or "catalogue"
        self.catalogue: list[dict] = []              # slim catalogue index; empty until first needed
        self.cat_by_name: dict[str, dict] = {}       # catalogue name -> dataset record (fast lookup)
        self.preset: str | None = None               # catalogue topic filter
        self.only_files = True                       # catalogue: hide datasets with nothing downloadable
        self.news: list[dict] = []                   # The Tyee articles, newest first (from the RSS feed)
        self.news_by_key: dict[str, dict] = {}       # "news:<url>" -> article record
        self.news_loaded_at: float | None = None     # time.monotonic() of the last feed fetch
        self.article_cache: dict[str, list] = {}     # key -> article text blocks; memory only, never saved
        self.image_cache: dict[str, bytes] = {}      # key -> lead image bytes; memory only, never saved
        self.layer_cache = layerinfo.LayerInfoCache(LAYER_INFO_CACHE)   # 24 h on-disk cache of layer definitions
        self.drift: dict[str, dict] = {}             # layer key -> diff vs the saved copy (set by `x`)

    # ---------- layout ----------
    def compose(self) -> ComposeResult:
        """Declare the widget tree: header, [tree | details], two progress bars, log, footer."""
        yield Header()
        with Horizontal():
            with VerticalScroll(id="left"):
                yield Input(placeholder="/ to search layers (e.g. glacier)", id="search")
                yield Tree("BC Data", id="tree")
            with VerticalScroll(id="right"):
                yield Static("Select a layer to see its details.", id="info")
        with Vertical(id="bars"):
            with Horizontal(classes="barrow"):
                yield Label("All items: idle", id="total_label")
                yield ProgressBar(id="total_bar", total=1, show_eta=False, gradient=RAINBOW)
            with Horizontal(classes="barrow"):
                yield Label("Current: idle", id="cur_label")
                yield ProgressBar(id="cur_bar", total=1, show_eta=False, gradient=RAINBOW)
        yield RichLog(id="log", markup=True, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        """Start-up: apply the theme, show the splash (its answer calls set_source) and begin loading
        the layer list in the background."""
        self.register_theme(APPLE2)
        self.theme = "apple2"
        # The log is output only. If it is focusable it ends up with keyboard focus after the splash closes,
        # and then the arrow keys / Enter don't reach the tree.
        self.query_one("#log", RichLog).can_focus = False
        self.title = "DataBC explorer"
        self.update_subtitle()
        self.push_screen(Splash(), self.set_source)
        self.log_msg("Loading layer list from DataBC...")
        self.load_tables()

    def log_msg(self, msg: str) -> None:
        """Append a line (Rich markup allowed) to the log pane. UI thread only; workers use `ui(self.log_msg, ...)`."""
        self.query_one("#log", RichLog).write(msg)

    # ---------- layer list / tree ----------
    @work(thread=True, exclusive=True, group="list")
    def load_tables(self) -> None:
        """Worker thread: fetch the list of WFS layer names (one network call) and build the tree."""
        try:
            tables = sorted(bcdata.list_tables())
        except Exception as e:
            self.call_from_thread(self.log_msg, f"[red]Could not list layers: {escape(str(e))}[/]")
            return
        self.tables = tables
        self.call_from_thread(self.rebuild_tree)
        self.call_from_thread(self.log_msg, f"{len(tables)} layers loaded.")

    def label_for(self, key: str) -> str:
        """Rich-markup text for one tree leaf: status icon, [x] mark if queued, then the name.
        Catalogue entries also show how many downloadable files they have, in brackets."""
        if key.startswith("news:"):  # news has no download status, so no icon: just the headline
            return "  " + escape(self.news_by_key[key]["title"])
        if key.startswith("cat:"):
            ds = self.cat_by_name[key[4:]]
            st = bc.status(ds["name"], self.manifest, OUT_DIR)
            name = f"{escape(ds['title'])} [dim]({len(bc.files_of(ds))})[/]"
        else:
            st = layer_status(key, self.manifest)
            name = escape(key.split(".", 1)[1])
        # Rich markup treats "[x]" as a tag, so the literal brackets are escaped with a backslash.
        icon = {"ok": "[green]✓[/]", "modified": "[grey62]?[/]"}.get(st, " ")
        mark = "[bold #ccffcc]\\[x][/] " if key in self.marked else ""
        # amber delta: the layer's documentation/columns changed upstream since download (found by `x`)
        d = self.drift.get(key)
        delta = "[#ffb000]Δ[/] " if d and (d.get("gone") or layerinfo.has_changes(d)) else ""
        return f"{icon} {mark}{delta}{name}"

    def update_subtitle(self) -> None:
        """Header subtitle: current source, topic filter, number marked and the output folder."""
        parts = [self.mode]
        if self.mode == "catalogue":
            parts.append(f"topic: {self.preset or 'all'}")
            if not self.only_files:
                parts.append("incl. datasets without files")
        if self.mode == "news":
            parts.append("The Tyee (thetyee.ca/rss2.xml)")
        if self.marked:
            parts.append(f"{len(self.marked)} marked")
        parts.append(f"downloads -> {OUT_DIR.resolve()}")
        self.sub_title = "  |  ".join(parts)

    def refresh_label(self, key: str) -> None:
        """Re-render one leaf in place, so branches stay open and the cursor doesn't move."""
        node = self.nodes.get(key)
        if node is not None:
            node.set_label(self.label_for(key))
        self.update_subtitle()

    def auto_expand(self) -> bool:
        """Branches open by themselves while searching, while a topic filter is on, and always in news (a short
        list, grouped by day). Those automatic
        expansions must not be remembered as the user's own choices (see on_tree_node_expanded)."""
        return bool(self.filter_text) or self.mode == "news" or (self.mode == "catalogue" and bool(self.preset))

    def rebuild_tree(self) -> None:
        """Throw the tree away and rebuild it for the current source, search text and filters.

        Used when the *set* of visible items changes. For a change to a single item (mark, status icon)
        use refresh_label instead: it leaves branches open and the cursor where it is.
        Layers are grouped by schema, catalogue datasets by publishing organisation, news by day.
        """
        tree = self.query_one("#tree", Tree)
        self.manifest = load_manifest()  # re-read so icons reflect downloads made since the last rebuild
        tree.clear()
        self.nodes.clear()
        self.branch_schema.clear()
        tree.root.expand()
        needle = self.filter_text.lower()
        groups: dict[str, list[str]] = {}
        if self.mode == "news":
            for a in self.news:  # already newest first, so groups and items come out in that order
                text = " ".join([a["title"], a["author"], a["summary"], *a["topics"]]).lower()
                if needle and needle not in text:
                    continue
                day = a["published"].strftime("%a %Y-%m-%d") if a["published"] else "(undated)"
                groups.setdefault(day, []).append(a["key"])
        elif self.mode == "catalogue":
            for ds in self.catalogue:
                if bc.matches(ds, needle, self.preset, self.only_files):
                    groups.setdefault(ds["org"], []).append("cat:" + ds["name"])
            for items in groups.values():
                items.sort(key=lambda k: self.cat_by_name[k[4:]]["title"].lower())
            groups = dict(sorted(groups.items(), key=lambda kv: kv[0].lower()))
        else:
            for t in self.tables:
                if needle and needle not in t.lower():
                    continue
                groups.setdefault(t.split(".", 1)[0], []).append(t)
        for group, items in groups.items():
            # open a branch if searching/filtering, or if the user had opened it earlier
            expand = self.auto_expand() or group in self.expanded_schemas
            branch = tree.root.add(f"{escape(group)} ({len(items)})", expand=expand)
            self.branch_schema[branch.id] = group
            for k in items:
                self.nodes[k] = branch.add_leaf(self.label_for(k), data=k)

    # ---------- source switching / catalogue ----------
    def action_switch_source(self) -> None:
        """`s`: cycle layers -> catalogue -> news -> layers."""
        order = ["layers", "catalogue", "news"]
        self.set_source(order[(order.index(self.mode) + 1) % len(order)])

    def action_main_menu(self) -> None:
        """Esc: go back to the splash screen / main menu to pick a section."""
        if not isinstance(self.screen, Splash):  # already showing it? then do nothing
            self.push_screen(Splash(), self.set_source)

    def set_source(self, mode: str | None) -> None:
        """Open the chosen section (also the splash screen's callback). "quit" ends the program."""
        if mode == "quit":
            self.exit()
            return
        if mode not in ("layers", "catalogue", "news"):  # the splash can be dismissed without a value
            return
        self.mode = mode
        self.current = None
        hints = {  # per section: (hint shown in the details pane, search-box placeholder)
            "layers": ("Select a layer to see its details.", "/ to search layers (e.g. glacier)"),
            "catalogue": ("Select a dataset to see its details.", "/ to search datasets (e.g. emissions)"),
            "news": ("Select an article. Enter reads it, o opens it in your browser.", "/ to search articles"),
        }
        self.query_one("#info", Static).update(hints[self.mode][0])
        self.query_one("#search", Input).placeholder = hints[self.mode][1]
        if self.mode == "news" and (self.news_loaded_at is None or time.monotonic() - self.news_loaded_at > 600):
            self.log_msg("Loading The Tyee feed...")  # first visit, or the list is over 10 minutes old
            self.load_news()
        if self.mode == "catalogue" and not self.catalogue:  # first visit: fetch (or load the cached) index
            self.log_msg("Loading the catalogue index (first time takes a few seconds)...")
            self.load_catalogue()
        self.update_subtitle()
        self.rebuild_tree()
        self.refresh_bindings()  # the footer shows different keys in different sections
        self.query_one("#tree", Tree).focus()  # so the arrow keys and Enter work straight away

    @work(thread=True, exclusive=True, group="catalogue")
    def load_catalogue(self) -> None:
        """Worker thread: load the catalogue index (from the 24-hour disk cache if fresh, else the API)."""
        try:
            idx = bc.fetch_index(OUT_DIR / ".catalogue_index.json")
        except Exception as e:
            self.call_from_thread(self.log_msg, f"[red]Could not load the catalogue: {escape(str(e))}[/]")
            return
        self.catalogue = idx
        self.cat_by_name = {d["name"]: d for d in idx}
        self.call_from_thread(self.rebuild_tree)
        n_files = sum(1 for d in idx if bc.files_of(d))
        self.call_from_thread(self.log_msg, f"{len(idx)} catalogue datasets loaded ({n_files} with downloadable files).")

    # ---------- change check (layers) ----------
    def action_check_changes(self) -> None:
        """`x`: check every downloaded layer against the current definition (layers only)."""
        if self.mode != "layers":
            self.log_msg("[#ffb000]The change check applies to layers: press s or Esc to reach them.[/]")
            return
        self.log_msg("Checking downloaded layers for changes...")
        self.check_changes()

    @work(thread=True, exclusive=True, group="drift")
    def check_changes(self) -> None:
        """Worker thread: for each downloaded layer, fetch its current definition (bypassing the cache) and
        compare it with the saved <TABLE>.info.json. Flags changed layers in the tree with an amber delta."""
        ui = self.call_from_thread
        log = lambda m: ui(self.log_msg, m)  # noqa: E731
        saved = sorted(p.name[: -len(".info.json")] for p in LAYERS_DIR.glob("*.info.json")) if LAYERS_DIR.exists() else []
        if not saved:
            log("[#ffb000]No downloaded layers to check yet.[/]")
            return
        listed = set(self.tables)
        results: dict[str, dict] = {}
        unchanged = failed = 0
        for i, table in enumerate(saved):
            ui(self.set_bars, i, len(saved), 0.0, table.split(".", 1)[-1], "checking")
            snap = saved_snapshot(table)
            if snap is None:  # unreadable JSON: the file-status icon already shows a "?"
                failed += 1
                continue
            if listed and table not in listed:  # DataBC no longer offers this layer: important for a rescue tool
                gone = layerinfo.diff_definitions({}, {})
                gone["gone"] = True
                results[table] = gone
                continue
            try:
                definition, _, stale = self.layer_cache.get(table, force=True)
                if stale:
                    raise ConnectionError("catalogue unreachable")  # an old copy can't tell us what changed
                diff = layerinfo.diff_definitions(snap, definition, snap.get("count"), bcdata.get_count(table))
            except Exception as e:
                failed += 1
                log(f"[red]{table}: couldn't check ({escape(str(e))})[/]")
                continue
            results[table] = diff
            if not (layerinfo.has_changes(diff) or diff["count"]):
                unchanged += 1
        changed = {t: d for t, d in results.items() if d.get("gone") or layerinfo.has_changes(d)}
        count_only = [t for t, d in results.items() if t not in changed and d["count"]]
        old = set(self.drift)
        self.drift = changed
        for table in old | set(changed):
            ui(self.refresh_label, table)
        ui(self.set_bars, len(saved), len(saved), 0.0, "", "idle")
        ui(self.query_one("#cur_label", Label).update, "Current: idle")
        ui(self.query_one("#total_label", Label).update, f"All items: checked ({len(saved)})")
        log(f"Checked {len(saved)} layers: [#ffb000]{len(changed)} changed[/], {len(count_only)} with a different "
            f"feature count only, {unchanged} unchanged" + (f", {failed} couldn't be checked." if failed else "."))
        for table, d in list(changed.items())[:15]:
            first = "no longer listed by DataBC" if d.get("gone") else layerinfo.describe_diff(d)[0]
            log(f"  [#ffb000]Δ[/] {escape(table)}: {escape(first)}")
        if self.current in self.info_cache:  # refresh the open details pane too
            ui(self.show_info, self.current)

    # ---------- news (The Tyee) ----------
    @work(thread=True, exclusive=True, group="news")
    def load_news(self) -> None:
        """Worker thread: fetch the RSS feed and rebuild the tree with the articles."""
        try:
            items = thetyee.fetch_feed()
        except Exception as e:
            self.call_from_thread(self.log_msg, f"[red]Could not load the news feed: {escape(str(e))}[/]")
            return
        self.news = items
        self.news_by_key = {a["key"]: a for a in items}
        self.news_loaded_at = time.monotonic()
        self.call_from_thread(self.rebuild_tree)
        self.call_from_thread(self.log_msg, f"{len(items)} articles loaded from The Tyee.")

    def action_refresh_news(self) -> None:
        """`r`: fetch the feed again (news only)."""
        if self.mode != "news":
            self.log_msg("[#ffb000]Refresh applies to the news section.[/]")
            return
        self.log_msg("Refreshing The Tyee feed...")
        self.load_news()

    def action_open_link(self) -> None:
        """`o`: open the highlighted article in the default web browser (news only)."""
        if not (self.current or "").startswith("news:"):
            self.log_msg("[#ffb000]Highlight a news article first (press s or Esc to reach the news section).[/]")
            return
        url = self.news_by_key[self.current]["link"]
        ok = thetyee.is_allowed_url(url) and webbrowser.open(url)
        self.log_msg(f"Opened {escape(url)}" if ok else f"[#ffb000]Couldn't open a browser; the link is {escape(url)}[/]")

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        """Enter/click on a leaf. In news this reads the article; elsewhere it does nothing extra."""
        key = event.node.data
        if key and key.startswith("news:"):
            self.read_article(key)

    @work(thread=True, exclusive=True, group="article")
    def read_article(self, key: str) -> None:
        """Worker thread: fetch the full article text on demand (nothing is saved to disk)."""
        ui = self.call_from_thread
        if key not in self.article_cache:
            ui(self.query_one("#info", Static).update, "Loading article ...")
            try:
                self.article_cache[key] = thetyee.fetch_article(self.news_by_key[key]["link"])
            except Exception as e:
                ui(self.query_one("#info", Static).update, f"[red]Couldn't load the article: {escape(str(e))}[/]\n"
                   "Press o to open it in your browser instead.")
                return
        a = self.news_by_key[key]
        if a["image"] and key not in self.image_cache:  # the lead picture is a bonus: never let it block the text
            try:
                data = thetyee.fetch_image(a["image"])
                halfblock.render_green(data, 4, 2)  # decode a tiny version now, so a bad file is caught here
                self.image_cache[key] = data
            except Exception as e:
                ui(self.log_msg, f"[#ffb000]Couldn't load the article image: {escape(str(e))}[/]")
        if self.current == key:  # the cursor may have moved on while we were fetching
            ui(self.show_article, key)

    def news_header(self, a: dict) -> Text:
        """Headline, byline and date: the top of both the summary view and the full article."""
        t = Text()
        t.append(f"{a['title']}\n", style="bold")
        when = a["published"].strftime("%A %d %B %Y, %H:%M") if a["published"] else "date unknown"
        t.append(f"{a['author'] or 'The Tyee'}  -  {when}\n", style="#99ff99")
        return t

    def show_news_summary(self, a: dict) -> None:
        """Details pane for a highlighted article: header, topics, the feed's one-line summary, how to read on."""
        t = self.news_header(a)
        if a["topics"]:
            t.append("Topics: " + ", ".join(a["topics"][:10]) + "\n", style="dim")
        t.append("\n" + (a["summary"] or "(no summary)") + "\n\n")
        t.append("Enter: read the full article    o: open in browser    j/k: scroll", style="dim")
        self.query_one("#info", Static).update(t)

    def show_article(self, key: str) -> None:
        """Details pane for an opened article: header, then the text blocks, then a source line."""
        a = self.news_by_key[key]
        t = self.news_header(a)
        # Reminder: the pane scrolls with j/k (the arrow keys stay on the list). Kept above the picture so
        # it is always visible, even when the picture fills most of the pane.
        t.append("j / k  scroll down / up    o  open in browser    Esc  main menu\n\n", style="dim")
        if key in self.image_cache:
            width = self.query_one("#right").size.width  # fit the pane, up to 60 columns
            t.append_text(halfblock.render_green(self.image_cache[key], max_cols=max(10, min(60, width - 4))))
            t.append("\n")
        for kind, text in self.article_cache[key]:
            if kind == "h":
                t.append(f"{text}\n\n", style="bold #99ff99")
            elif kind == "quote":
                t.append(f"  | {text}\n\n", style="italic")
            elif kind == "li":
                t.append(f"  - {text}\n")
            else:
                t.append(f"{text}\n\n")
        t.append(f"Source: The Tyee, {a['link']}\nShown for reading only; not saved. (c) The Tyee.", style="dim")
        self.query_one("#info", Static).update(t)
        self.query_one("#right").scroll_home(animate=False)  # start at the top of the new article

    def action_scroll_info(self, direction: int) -> None:
        """`j`/`k`: scroll the details pane a page down/up. The tree keeps the arrow keys and Page keys."""
        right = self.query_one("#right")
        right.scroll_page_down(animate=False) if direction > 0 else right.scroll_page_up(animate=False)

    def count_visible(self) -> int:
        """How many catalogue datasets pass the current search / topic / files-only filters."""
        return sum(1 for ds in self.catalogue if bc.matches(ds, self.filter_text, self.preset, self.only_files))

    def action_next_preset(self) -> None:
        """`f`: step through the topic filters (None = all), wrapping around at the end."""
        if self.mode != "catalogue":
            self.log_msg("[#ffb000]Topic filters apply to the catalogue: press s to switch source.[/]")
            return
        names = [None, *bc.PRESETS]
        self.preset = names[(names.index(self.preset) + 1) % len(names)]
        self.update_subtitle()
        self.rebuild_tree()
        self.log_msg(f"Topic: {self.preset or 'all'}  ({self.count_visible()} datasets)")

    def action_toggle_files(self) -> None:
        """`a`: hide/show datasets that have no downloadable files (only service links)."""
        if self.mode != "catalogue":
            return
        self.only_files = not self.only_files
        self.update_subtitle()
        self.rebuild_tree()
        self.log_msg(f"{'Only datasets with downloadable files' if self.only_files else 'All datasets'} "
                     f"({self.count_visible()} shown)")

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        """Remember branches the user opens so they stay open across tree rebuilds."""
        schema = self.branch_schema.get(event.node.id)
        if schema and not self.auto_expand():  # ignore the auto-expansion done while searching
            self.expanded_schemas.add(schema)

    def on_tree_node_collapsed(self, event: Tree.NodeCollapsed) -> None:
        """...and forget branches the user closes."""
        schema = self.branch_schema.get(event.node.id)
        if schema and not self.auto_expand():
            self.expanded_schemas.discard(schema)

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter as you type: every keystroke in the search box rebuilds the tree."""
        if event.input.id == "search":
            self.filter_text = event.value.strip()
            self.rebuild_tree()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the search box hands focus back to the tree (so m/d/s work again)."""
        self.query_one("#tree", Tree).focus()

    def action_focus_search(self) -> None:
        """`/`: jump to the search box."""
        self.query_one("#search", Input).focus()

    # ---------- details panel ----------
    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted) -> None:
        """The cursor moved: show the item's details. Branch rows carry no data, so they are ignored.
        Catalogue records are already in memory; a layer's columns/count need a network call (cached)."""
        table = event.node.data
        if not table:
            return
        self.current = table
        if table.startswith("news:"):
            # an article already read this session reopens at once; otherwise show the summary
            if table in self.article_cache:
                self.show_article(table)
            else:
                self.show_news_summary(self.news_by_key[table])
            return
        if table.startswith("cat:"):
            self.show_dataset(self.cat_by_name[table[4:]])
            return
        if table in self.info_cache:
            self.show_info(table)
        else:
            self.query_one("#info", Static).update(f"Loading {escape(table)} ...")
            self.fetch_info(table)

    @work(thread=True, exclusive=True, group="info")
    def fetch_info(self, table: str) -> None:
        """Worker thread: fetch a layer's description, columns and feature count, then show them.
        `exclusive` cancels a stale lookup when the cursor moves on quickly."""
        try:
            # description + columns come from the 24 h cache (or the catalogue); the count is always live
            definition, fetched, stale = self.layer_cache.get(table)
            info = dict(definition)
            info["count"] = bcdata.get_count(table)
            info["table"] = table
            info["_fetched"], info["_stale"] = fetched, stale  # shown in the pane; never written to disk
            self.info_cache[table] = info
        except Exception as e:
            self.call_from_thread(
                self.query_one("#info", Static).update, f"[red]Failed to load info: {escape(str(e))}[/]"
            )
            return
        if self.current == table:  # the user may have moved on while we were waiting
            self.call_from_thread(self.show_info, table)

    def show_info(self, table: str) -> None:
        """Render a WFS layer's details (from info_cache) into the right-hand pane."""
        info = self.info_cache[table]
        t = Text()
        t.append(f"{table}\n", style="bold")
        t.append(f"Features: {info.get('count', '?'):,}" if isinstance(info.get("count"), int) else "Features: ?")
        if info["count"] and info["count"] > BIG_LAYER:
            t.append("   (large: press d twice to confirm)", style="#ffb000")
        t.append("\n")
        # how old is the description/columns text we are showing?
        fetched = info.get("_fetched")
        if fetched and info.get("_stale"):
            t.append(f"Definition from your cache ({time.strftime('%Y-%m-%d %H:%M', time.localtime(fetched))}): "
                     "the catalogue couldn't be reached.\n", style="#ffb000")
        elif fetched and time.time() - fetched > 90:
            t.append(f"Definition as of {time.strftime('%Y-%m-%d %H:%M', time.localtime(fetched))} "
                     "(cached; refreshed after 24 h)\n", style="dim")
        t.append("\n")
        self.append_changes(t, table, info)
        t.append((info.get("description") or "(no description)") + "\n\n")
        cols = info.get("schema") or []
        t.append(f"Columns ({len(cols)})\n", style="bold")
        for c in cols:
            t.append(f"  {c.get('column_name')}", style="bold #99ff99")
            t.append(f"  {c.get('data_type')}\n", style="dim")
        self.query_one("#info", Static).update(t)

    def append_changes(self, t: Text, table: str, info: dict) -> None:
        """If this layer is downloaded, add a block saying whether its documentation/columns have changed
        since the saved <TABLE>.info.json was written. Nothing is added for layers you haven't downloaded."""
        snap = saved_snapshot(table)
        if snap is None:
            return
        # when was the snapshot taken? newer files record it; older ones fall back to the file's date
        when = ""
        try:  # stored in UTC; show the date in the user's own time zone
            when = datetime.fromisoformat(snap["_archive"]["downloaded"]).astimezone().strftime("%Y-%m-%d")
        except (KeyError, TypeError, ValueError):
            pass
        if not when:
            try:
                when = time.strftime("%Y-%m-%d", time.localtime((LAYERS_DIR / f"{table}.info.json").stat().st_mtime))
            except OSError:
                when = "an earlier date"
        if (self.drift.get(table) or {}).get("gone"):
            t.append("No longer listed by DataBC's WFS service (found by the last check).\n\n", style="bold #ffb000")
        lines = layerinfo.describe_diff(
            layerinfo.diff_definitions(snap, info, snap.get("count"), info.get("count")))
        if not lines:
            t.append(f"Unchanged since you downloaded it ({when}).\n\n", style="dim")
            return
        t.append(f"Changed since you downloaded it ({when})\n", style="bold #ffb000")
        for line in lines:
            t.append(f"  - {line}\n", style="#ffb000")
        t.append("\n")

    def show_dataset(self, ds: dict) -> None:
        """Render a catalogue dataset: publisher, licence, size, then the file list, then the description.
        The files come before the (often very long) description so they are visible without scrolling."""
        files = bc.files_of(ds)
        size = bc.total_size(ds)
        t = Text()
        t.append(f"{ds['title']}\n", style="bold")
        t.append(f"{ds['org']}\n", style="#99ff99")
        t.append(f"Licence: {ds['license'] or '?'}   Updated: {ds['modified'][:10] or '?'}\n")
        if ds["groups"]:
            t.append(f"Groups: {', '.join(ds['groups'])}\n", style="dim")
        t.append(f"\nDownloadable: {len(files)} file(s), {bc.human_size(size)}", style="bold")
        if size > bc.BIG_BYTES:
            t.append("   (large: press d twice to confirm)", style="#ffb000")
        t.append("\n\n")
        t.append(f"Resources ({len(ds['resources'])})\n", style="bold")
        for r in ds["resources"]:
            ok = bc.is_downloadable(r)  # "get" = a real file we will download; "---" = a service link
            t.append("  get " if ok else "  --- ", style="bold #99ff99" if ok else "dim")
            t.append(f"{r['format'] or '?':<11}", style="" if ok else "dim")
            t.append(f"{bc.human_size(r['size']):>9}  " if ok else " " * 11, style="" if ok else "dim")
            t.append(f"{r['name'][:60]}\n", style="" if ok else "dim")
        t.append("\n'---' rows are service links (WMS, KML, ArcGIS REST, ...) and are not downloaded.", style="dim")
        t.append("\n\nDescription\n", style="bold")
        notes = ds["notes"].strip() or "(no description)"
        t.append(notes[:1500] + (" ..." if len(notes) > 1500 else "") + "\n")
        self.query_one("#info", Static).update(t)

    # ---------- marking ----------
    def action_mark(self) -> None:
        """`m`: queue/unqueue the highlighted item. Branch rows have no data and are ignored."""
        node = self.query_one("#tree", Tree).cursor_node
        if node is None or not node.data:
            return
        table = node.data
        if table.startswith("news:"):
            self.log_msg("[#ffb000]News is read-only: Enter reads an article, o opens it in your browser.[/]")
            return
        self.marked.symmetric_difference_update({table})
        self.refresh_label(table)

    def action_clear_marks(self) -> None:
        """`c`: unmark everything (refreshing labels in place, so the tree stays as it is)."""
        old = list(self.marked)
        self.marked.clear()
        for table in old:
            self.refresh_label(table)

    def finish_layer(self, table: str, manifest: dict) -> None:
        """Called on the UI thread after a layer was saved: unmark it and show its new status."""
        self.manifest = manifest
        self.marked.discard(table)
        self.drift.pop(table, None)  # a fresh download is, by definition, up to date
        self.refresh_label(table)

    # ---------- downloading ----------
    def action_download(self) -> None:
        """`d`: download every marked item, or just the highlighted one if nothing is marked."""
        targets = sorted(self.marked) or ([self.current] if self.current else [])
        if any(t.startswith("news:") for t in targets):  # only reachable when nothing is marked
            self.log_msg("[#ffb000]News is read-only: Enter reads an article, o opens it in your browser.[/]")
            return
        if not targets:
            self.log_msg("[#ffb000]Nothing selected.[/]")
            return
        self.download(targets)

    def set_bars(self, n_done: int, n_total: int, frac: float, name: str, step: str) -> None:
        """Update both bars (called on the UI thread). frac = progress of the current item, 0..1."""
        self.query_one("#total_bar", ProgressBar).update(total=n_total, progress=n_done + frac)
        self.query_one("#cur_bar", ProgressBar).update(total=1, progress=frac)
        self.query_one("#total_label", Label).update(f"All items: {min(n_done + 1, n_total)}/{n_total}")
        self.query_one("#cur_label", Label).update(f"{name[:18]} {step}"[:27])

    @staticmethod
    def short_name(key: str) -> str:
        """Human-readable name for a key, used in the progress label."""
        return key[4:] if key.startswith("cat:") else key.split(".", 1)[1]

    @work(thread=True, exclusive=True, group="download")
    def download(self, targets: list[str]) -> None:
        """Worker thread: download each target in turn (layers and datasets can be mixed).

        One item failing never stops the rest: errors are logged and the loop moves on. `exclusive`
        means starting a second download cancels the first, so wait for "Done." before pressing d again.
        """
        ui = self.call_from_thread  # the only safe way for a thread to touch widgets
        log = lambda m: ui(self.log_msg, m)  # noqa: E731
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        n = len(targets)
        done = 0
        for key in targets:
            try:
                if key.startswith("cat:"):
                    self._download_dataset(key, done, n, ui, log)
                else:
                    self._download_layer(key, done, n, ui, log)
            except Exception as e:
                log(f"[red]{key} failed: {escape(str(e))}[/]")
            done += 1
            ui(self.set_bars, done, n, 0.0, self.short_name(key), "finished")
        ui(self.set_bars, n, n, 0.0, "", "idle")
        ui(self.query_one("#cur_label", Label).update, "Current: idle")
        ui(self.query_one("#total_label", Label).update, f"All items: done ({n})")
        log("Done.")

    def _record(self, key: str, entry: dict) -> dict:
        """Save fingerprints for a finished item into the manifest and return the updated manifest.
        Re-read from disk each time so a manifest written by an earlier run is never clobbered."""
        manifest = load_manifest()
        manifest[key] = entry
        MANIFEST.write_text(json.dumps(manifest, indent=2))
        return manifest

    def _download_dataset(self, key: str, done: int, n: int, ui, log) -> None:
        """Download one catalogue dataset (runs in the worker thread).

        done/n = items finished / total in this batch, used for the overall bar. Success clears the mark
        and records the files; any failed file leaves the dataset marked and flagged "?" so it is retried.
        """
        ds = self.cat_by_name[key[4:]]
        files = bc.files_of(ds)
        size = bc.total_size(ds)
        if not files:
            log(f"[#ffb000]{ds['title']}: no downloadable files (only service links), skipped.[/]")
            return
        # Two-press confirmation: the first `d` on a big dataset only warns and remembers it; the next
        # `d` finds it in pending_big and goes ahead.
        if size > bc.BIG_BYTES and key not in self.pending_big:
            self.pending_big.add(key)
            log(f"[#ffb000]{ds['title']} is {bc.human_size(size)}. Press d again to confirm.[/]")
            return
        log(f"Downloading {ds['title']} ({len(files)} file(s), {bc.human_size(size)})...")
        last = [0.0]

        def progress(frac: float, text: str) -> None:
            """Called from bc.download_dataset for every chunk received."""
            now = time.monotonic()
            if frac < 1 and now - last[0] < 0.1:  # don't flood the UI thread
                return
            last[0] = now
            ui(self.set_bars, done, n, frac, ds["name"], text)

        ui(self.set_bars, done, n, 0.0, ds["name"], "starting")
        result = bc.download_dataset(ds, OUT_DIR, progress)
        if result["errors"]:
            for err in result["errors"]:
                log(f"[red]{ds['name']}: {escape(err)}[/]")
            log(f"[#ffb000]{ds['title']} is incomplete: kept marked and shown with ?.[/]")
            ui(self.refresh_label, key)
            return
        self.pending_big.discard(key)
        manifest = self._record(key, {"files": result["files"]})
        ui(self.finish_layer, key, manifest)
        log(f"[green]Saved {bc.dataset_dir(OUT_DIR, ds['name'])} ({len(result['files']) - 1} file(s))[/]")

    def _download_layer(self, table: str, done: int, n: int, ui, log) -> None:
        """Download one WFS layer into layers/<TABLE>.gpkg plus <TABLE>.info.json (worker thread).

        The service returns features in pages, so we request each page ourselves (instead of calling
        bcdata.get_data) to advance the progress bars as pages arrive. Same logic as get_data otherwise.
        """
        short = table.split(".", 1)[1]
        wfs = BCWFS()
        urls = wfs.define_requests(table)  # one URL per page of features; also asks the server for the count
        count = wfs.get_count(table)
        if count > BIG_LAYER and table not in self.pending_big:  # same two-press confirmation as datasets
            self.pending_big.add(table)
            log(f"[#ffb000]{table} has {count:,} features and will be held in memory. "
                f"Press d again to confirm.[/]")
            return
        log(f"Downloading {table} ({count:,} features, {len(urls)} request(s))...")
        steps = len(urls) + 1  # one per page, plus writing the file
        ui(self.set_bars, done, n, 0.0, short, "starting")
        pages = []
        for i, url in enumerate(urls, 1):
            pages.append(wfs.request_features(url, as_gdf=True))
            ui(self.set_bars, done, n, i / steps, short, f"page {i}/{len(urls)}")
        gdf = pd.concat(pages) if len(pages) > 1 else pages[0]  # stitch the pages into one GeoDataFrame
        if len(gdf) == 0:
            log(f"[#ffb000]{table}: no features returned, skipped.[/]")
            return
        ui(self.set_bars, done, n, len(urls) / steps, short, "writing")
        LAYERS_DIR.mkdir(parents=True, exist_ok=True)
        dest = LAYERS_DIR / f"{table}.gpkg"
        gdf.to_file(dest, layer=short, driver="GPKG")
        # Keep the layer's documentation (description + column meanings) next to the data: the .gpkg keeps
        # column names and types, but not the long explanations, which are the part most easily lost.
        # force=True: the archive copy must be fetched now, never served from the 24 h cache.
        definition, fetched, stale = self.layer_cache.get(table, force=True)
        info = dict(definition)
        info["count"] = count
        info["table"] = table
        info["_archive"] = {"downloaded": datetime.now(timezone.utc).isoformat(),
                            "definition_fetched": datetime.fromtimestamp(fetched, timezone.utc).isoformat(),
                            "definition_source": "older cached copy (catalogue unreachable)" if stale else "live"}
        (LAYERS_DIR / f"{table}.info.json").write_text(json.dumps(info, indent=2, default=str))
        self.pending_big.discard(table)
        manifest = self._record(table, {"gpkg": _sig(dest), "info": _sig(LAYERS_DIR / f"{table}.info.json")})
        ui(self.finish_layer, table, manifest)
        log(f"[green]Saved {dest} ({len(gdf):,} features)[/]")


# Only start the UI when run as a script (not when imported, e.g. by a test).
if __name__ == "__main__":
    BCDataTUI().run()
