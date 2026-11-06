# BCDataRescue

A terminal app for browsing and downloading British Columbia's open data, so you can keep your own local copy. It also has a small news reader for The Tyee.

It has three sections:

| Source | What it is | What you get |
|---|---|---|
| **Layers** | ~900 map layers published through DataBC's WFS service | one GeoPackage (`.gpkg`) per layer, plus a JSON file with the layer's description and column documentation |
| **Catalogue** | ~3,400 datasets in the [BC Data Catalogue](https://catalogue.data.gov.bc.ca) | the dataset's own files (CSV, XLSX, PDF, ZIP, GPKG, ...), plus a `dataset.json` with the full metadata |
| **News** | [The Tyee](https://thetyee.ca)'s public [RSS feed](https://thetyee.ca/rss2.xml) | a read-only reader: headlines by day, full article text and the lead image (drawn in green) on demand. **Nothing is saved.** |

Nothing is downloaded until you ask. The data itself is **not** part of this repository: you download what you want, onto your own disk.

> **Not affiliated with the Province of British Columbia or The Tyee.** This is an independent tool. The data belongs to its publishers and comes under their licences, and The Tyee's articles remain The Tyee's (see [Data licences](#data-licences)).

## Why

Public datasets sometimes change, move or disappear. The catalogue's descriptions and column documentation are easy to lose too. This tool keeps the data and the metadata together, in ordinary open formats, with a record of where each file came from.

## Setup

You need [micromamba](https://mamba.readthedocs.io/en/latest/installation/micromamba-installation.html) (or conda). GDAL and the other geospatial libraries are installed from conda-forge, which avoids building them from source.

```bash
micromamba create -f environment.yml    # creates an environment named "geo"
micromamba activate geo
python bcdata_tui.py                    # downloads go to ./bcdata
python bcdata_tui.py ~/my-bc-data       # or choose another folder
```

`environment.yml` pins Python 3.12 and `bcdata==0.17.2`. Textual is not pinned (see [Known limitations](#known-limitations)).

## Using it

On start-up a splash screen (the main menu) asks which section to open: `1` for layers, `2` for the catalogue, `3` for news, or `0` to quit. Press `Esc` at any time to come back to this menu, or `s` to cycle straight to the next section.

| Key | Action |
|---|---|
| `/` | Search (then Enter to return to the list) |
| `m` | Mark or unmark the highlighted item |
| `d` | Download the marked items, or the highlighted one if none are marked |
| `c` | Clear all marks |
| `Esc` | Back to the main menu (the splash screen); `0` there quits the program |
| `s` | Cycle through the sections: layers, catalogue, news |
| `f` | Cycle the topic filter (catalogue only) |
| `a` | Show or hide catalogue datasets that have nothing downloadable (catalogue only) |
| `x` | Layers: check every downloaded layer for changes (see below) |
| `Enter` | News: read the highlighted article in full, with its lead image |
| `o` | News: open the highlighted article in your web browser |
| `r` | News: fetch the feed again |
| `j` / `k` | Scroll the details pane down / up a page (the arrow keys move through the list). A reminder is shown at the top of every opened article. |
| `q` | Quit |

The footer shows only the keys that apply to the section you are in, and keys that don't apply are switched off (for example `m` and `d` do nothing in news, which is read-only).

The right-hand pane shows details for the highlighted item: for layers the description, feature count and columns (and, if you have downloaded the layer, whether those have changed since); for catalogue datasets the licence, last-updated date, file list with sizes, and description; for news the headline, author, date, topics and the feed's one-line summary (press `Enter` for the full text). Two progress bars show overall and current download progress.

**Topic filters** (catalogue): Climate, Gender & sex, Census, Indigenous, Species at risk, Water, Wildfire & air. They match on title, tags, groups and publisher, so they are a convenient starting point, not a complete or authoritative list. Use search for anything else.

### Status icons

| Icon | Meaning |
|---|---|
| green ✓ | Downloaded, and the files are unchanged since the tool wrote them |
| grey ? | Files are changed, missing, incomplete, or the tool has no record of writing them |
| (none) | Not downloaded |

### Have the layers changed since I downloaded them?

A layer's description and column list can change when the publisher releases a new version. Each downloaded layer's `.info.json` is a snapshot of that documentation, so it can be compared with today's:

- **Highlight a downloaded layer** and the details pane says either *Unchanged since you downloaded it* or lists what differs: the description or table comments, columns added or removed, a column's type or explanation, and the feature count.
- **Press `x`** to check every downloaded layer in one go (about a quarter of a second each). Layers whose description or columns changed get an amber **Δ** in the tree, and the log lists them. A layer that DataBC no longer offers is flagged too.
- A different **feature count on its own** is reported but not flagged: it is normal for live layers (current wildfires, monitoring stations) and says nothing about the documentation.
- If the catalogue returns nothing for a layer at that moment, the check says it can't compare, and does not report every column as removed.
- Downloading a layer again resets its Δ.

Descriptions and columns come from the BC Data Catalogue and are cached on disk for **24 hours**, so repeat browsing is instant, works offline for layers you've already viewed, and doesn't re-ask the government API. The pane says when a definition is from the cache, and warns if a refresh failed and an older copy is being shown. The feature count is never cached. Downloads and `x` always fetch fresh, so a saved `.info.json` is never a day-old copy.

### Where files go

```
bcdata/                       (or the folder you passed)
├── layers/                   <TABLE>.gpkg  and  <TABLE>.info.json
├── catalogue/<dataset>/      the dataset's files  and  dataset.json
├── .bcdata_tui_manifest.json  size + timestamp of every file written (drives the icons)
├── .catalogue_index.json      cached copy of the catalogue listing (refreshed after 24 h)
└── .layer_info_cache.json     cached layer descriptions and columns (refreshed after 24 h)
```

The whole `bcdata/` folder is git-ignored. The news section writes nothing to disk.

### Safeguards

- A layer with more than 250,000 features, or a catalogue dataset over 500 MB, is not downloaded on the first `d`. The log says so and a second `d` confirms. Large layers are held in memory while downloading.
- Files are written as `.part` and renamed when complete, so an interrupted download never looks finished.
- The catalogue answers a dead link with a normal web page. The downloader rejects HTML for data files instead of saving it under a `.csv` name.
- Items that only partly download stay marked and show `?`; the failures are logged and recorded in that dataset's `dataset.json`.
- Catalogue service links (WMS, KML, ArcGIS REST and similar) are listed but not downloaded. They are not files.
- News is treated as untrusted input: the feed is parsed with entity expansion and network access off, and article pages are only fetched when the URL is `https://thetyee.ca/...` (a redirect elsewhere is refused). Full articles are fetched only when you press `Enter`, one at a time, and are kept in memory for the session.

## Known limitations

- **Sizes are often unknown.** Many catalogue files list no size, so a dataset can be far larger than advertised (one hydrometric dataset listed 125 MB and was about 1 GB). Check the folder size as you go.
- **Some links are dead or blocked.** Several catalogue entries point at pages that no longer exist, and some hosts (for example Statistics Canada) refuse automated downloads with a 403. Those datasets end up partial and show `?`.
- **The ✓/? check uses file size and modification time, not a hash.** An edit that keeps both unchanged would not be noticed.
- **The change check covers layers only.** Catalogue datasets also save their metadata (`dataset.json`), but there is no comparison for them yet. The layer check compares the catalogue's documentation of a layer; it does not re-download the data, so it can't tell you whether the rows themselves changed (apart from the feature count).
- **No resume.** An interrupted dataset starts again.
- **It relies on library internals.** Layer downloads use bcdata's `BCWFS` class, and the rainbow progress bars replace one private function in Textual (`textual.renderables.bar._apply_gradient`). A bcdata or Textual update could break either. If that happens, pin the versions that worked.
- **Tested on one machine, headlessly.** It was developed on a Linux (Fedora-based) system and exercised through Textual's test tools and rendered screenshots, not by a person using it in many real terminals. Other platforms and terminals are untested, Windows in particular.
- **The news image is a chunky thumbnail.** Each text cell shows two stacked pixels in shades of green, so the picture is about 60 columns by 22 rows: enough to recognise a scene, not to read detail. It works in any terminal with 24-bit colour and needs no graphics support. Only the lead image is shown, not images inside the article, and the image is fetched only when you open the article.
- **The news reader depends on The Tyee's page layout.** The feed only has a one-line summary and the 20 or so latest articles, so full text is read from the article page itself. If the site changes its HTML, the reader says it couldn't find the text, and `o` still opens the article in your browser. Text only: no images, captions or embedded media.
- **Not every dataset type is covered.** Rasters and other formats that bcdata's WFS does not serve are only available through the catalogue's files, where they exist.

## How this was made

This project was written by an AI coding assistant, **Claude Code** (Anthropic's command-line agent, running the Claude Sonnet 5.5 model), working in a single session under the direction of the project owner. The owner described what they wanted in plain language, step by step (check for GDAL, set up an isolated conda environment, install `bcdata`, build a browser, add the catalogue, retheme it, add a news reader, and so on), and the assistant wrote the code, ran the commands and tested the results.

What that means in practice:

- **The code has not had an independent human review or a security audit.** Read it before relying on it for anything that matters. It is short: `bcdata_tui.py` (the interface) and `bccatalogue.py` (catalogue index, filtering and downloads).
- **Testing was ad hoc.** The assistant drove the interface headlessly with Textual's test tools, rendered screenshots to check the layout and colours, and ran real downloads against the live services. Those test scripts were throwaway and are **not** included, so there is no automated test suite.
- **The visual design is a homage, not a recreation.** The splash screen and green-on-black theme imitate an Apple ][ monitor. The map of BC on the splash is a hand-drawn ASCII sketch, not a survey.
- **Real downloads happened during development.** The assistant downloaded a set of climate, census, Indigenous, species-at-risk, water and wildfire datasets to try the tool on real data, and found and fixed several bugs along the way (for example the dead-link problem above).

## Data licences

The tool's code is public domain (see below). **The data you download is not, and neither are the news articles.** Most BC datasets are under the [Open Government Licence – British Columbia](https://www2.gov.bc.ca/gov/content/data/policy-standards/open-data/open-government-licence-bc), but licences vary by dataset. Each catalogue download records its licence in `dataset.json`; check it before redistributing anything. This repository does not contain any downloaded data.

The Tyee's articles and pictures are its copyrighted work. The reader shows them on screen for personal reading, like a browser would, and saves nothing; it is not a way to copy or redistribute them. Read The Tyee's own terms and please [support it](https://thetyee.ca).

## Licence

Public domain, under the [Unlicense](LICENSE). Do what you like with the code.
