# Nairobi Flood Lens dashboard

A static MapLibre application with a preparation story and an interactive map for six Nairobi study areas.

![Interactive map and building screening](../docs/screenshots/dashboard.png)

## Run

From the repository root:

```sh
python -m http.server 8000 --directory maplibre_dashboard
```

Open **http://localhost:8000**. No build or analysis rerun is required. External libraries and basemaps need internet access.

## Read, explore, inspect

- **Part A · The story:** ten chapters with a sticky chapter menu, reading progress, dashboard screenshots and captions. Enlarge screenshots, compare flood concern with inspection priority using the on-map swipe handle (mouse, touch or arrow keys), including in the enlarged view, and open a chapter’s area and layer preset in Part B. Use **Return to story** to resume the chapter. Supporting reports expand alongside the visuals; the six-area table uses current dashboard metrics.
- **Part B · Dashboard:** select a study area, inspect building and study-point popups, switch layers or basemaps, and measure distances or areas.
- **Light / Dark:** switch the interface theme using the header control.

![Full story view](../docs/screenshots/story.png)

Red buildings overlap land needing closer review. Orange buildings were screened but fall outside the higher-concern mask; they are not classified as safe. The Wilson Airport reference and South C drainage area describe the terrain-based catchment check.

## Files needed to serve the site

Keep `index.html`, `styles.css`, `app.js`, `story.js` and the Git-included `data/` assets together. `data/vector_tiles.json` identifies the three active tile bundles. Metrics, contextual JSON, small display vectors and raster overlays also support the map and controls. Raw analysis data and duplicate full GeoJSON tile inputs are excluded from publication.

Serve this folder as the static site root. GitHub Pages uses the `gh-pages` branch, which contains this folder at its root. In repository Settings → Pages, choose **Deploy from a branch**, **gh-pages**, and **/(root)**, then save.

The dashboard address is https://benard10.github.io/Nairobi-Flood-Lens-2026/ once Pages is enabled and deployment finishes.

After committing dashboard changes on `main`, update the published branch with:

```sh
git subtree split --prefix=maplibre_dashboard -b gh-pages-update
git push origin gh-pages-update:gh-pages
git branch -d gh-pages-update
```

The `.nojekyll` marker lets Pages serve the static assets directly.

## Update the results

In the full analysis workspace, review the notebook and export its results, then run `python build_maplibre_dashboard.py` from the repository root. Review changed runtime assets and update the Git tile allowlist if the builder generates new versioned directories. Do not overwrite story HTML with the archived one-off editing scripts.

See the [project README](../README.md) for notebook setup, sources and verification, [TILING.md](TILING.md) for tile generation, and [MEASUREMENT.md](MEASUREMENT.md) for measurement controls.

These maps support field review and preparation. They do not predict flood depth, timing or probability.

## Refresh story screenshots

Run `python tests/dashboard_browser_qa.py --capture-story-assets` after changing the analysis or dashboard. The 11 screenshots in `data/story/` are captured from the real dashboard at twice the pixel density, with landscape map framing and separate readable legends; they are dated snapshots, while the comparison table and map use the runtime metrics. Run `python tests/dashboard_browser_qa.py --interactive-story` to check chapter navigation, enlargement, comparison, map presets, return navigation and the mobile layout.
