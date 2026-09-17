# Publication cleanup

The repository packages the static dashboard and its supporting analysis source together. The browser entry point is `maplibre_dashboard/index.html`; the notebook remains at the project root alongside its Python module and builders.

## Included

- Dashboard HTML, CSS and JavaScript.
- Three active vector tile bundles, runtime metrics, context, small display GeoJSON and raster overlays.
- Output-cleared analysis notebook, implementation, configuration example and dependencies.
- Maintained verification scripts and browser screenshots.

## Excluded

Raw inputs, source caches, full exports, QGIS project files, old reports/planning notes, logs, editor files, Python caches and the local cleanup archive are excluded by the root Git allowlist. Full GeoJSON inputs duplicated by vector tiles remain available locally but are excluded from publication.

One-off dashboard editing scripts, unused story raster images, the removed source-inventory download and the obsolete river-band tile bundle were moved to `.local_archive/publication_cleanup/`. The original notebook with its saved outputs is also backed up there. Nothing in this archive is published.

## Verification

The story browser check passed. The complete browser check exercises study popups, historical markers, layer controls, basemap switching, themes, area selection and reset. Fresh browser screenshots are in `docs/screenshots/`. The publication asset audit confirms all direct runtime data references and raster overlays exist and the notebook contains no saved code outputs.

The analysis test suite currently cannot collect in the local Python environment: installed `odc.stac` imports missing `odc.loader._rio`. This is an environment dependency failure; analysis tests have not been reported as passing. Install the requirements in a fresh virtual environment before rerunning them. The full data/tile comparison checks need local inputs deliberately excluded from Git.

## Publish

The local Git repository is initialized, with no remote or push yet. The intended new repository name is `nairobi-flood-lens`. GitHub CLI is not installed in this environment and no GitHub connector is available. Connect an authenticated GitHub publishing route or create the repository and provide its remote URL before pushing.

When publishing a refreshed tile snapshot, update the explicit directory allowlist in `.gitignore` to match `maplibre_dashboard/data/vector_tiles.json`. Keep raw data and full exports excluded.
