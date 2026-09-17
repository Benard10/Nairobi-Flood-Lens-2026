# Dashboard vector tiles

The map uses static XYZ `.pbf` tiles for buildings, the full river network and
river buffer bands. Tiles are generated with the already-installed GDAL MVT
driver through pyogrio. MVT is an open tile format supported by MapLibre; this
does not require a Mapbox account, token or SDK.

Analytical GeoJSON and screening counts remain the originals. Display tiles
carry the classification, study area and popup fields. Coordinates are
quantised for display; never use tile fragments to calculate analytical totals.

From the project directory, rebuild only tiles after changing display GeoJSON:

The full `buildings.geojson`, `analysis_rivers.geojson` and
`river_buffer_intervals.geojson` tile inputs are local build products excluded
from Git. Regenerate them in the analysis workspace before rebuilding tiles.
The published dashboard serves the active tile bundles without these inputs.

```powershell
python build_dashboard_tiles.py
```

The normal asset builder also builds tiles:

```powershell
python build_maplibre_dashboard.py
```

The final dashboard cell in Stage 8 of `Flood_Staged_Analysis.ipynb` already calls
that builder. No notebook analysis rerun is needed for these presentation
changes. When analysis changes, complete the affected analysis stages and run
the final export/dashboard cell afterward.

Serve the dashboard through HTTP using the existing local server. Plain static
servers work because tiles are uncompressed PBF and missing tiles inside the
tileset bounds are materialised as empty tiles. Zooms 10–15 are generated;
MapLibre reuses the zoom-15 tiles for closer views. Higher zoom has full display
precision; lower zoom is generalised. Content hashes in directory names prevent
stale tiles after an export changes. Source attribution remains on the map.

Validation:

```powershell
python tests/dashboard_tiles_qa.py
python tests/dashboard_data_qa.py
python tests/dashboard_browser_qa.py
```

Tiles follow [GDAL's vector-tile documentation](https://gdal.org/en/stable/drivers/vector/mvt.html)
and [MapLibre vector source specifications](https://maplibre.org/maplibre-style-spec/sources/).
