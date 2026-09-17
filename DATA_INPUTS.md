# Optional data contract for the Nairobi flood model

The core Planetary Computer inputs are discovered automatically. The following
local inputs are optional because they require scientific or licensing review.
Place large files under `data/` or `validation/`; both directories are ignored
by Git.

## Reusable raw-data cache

`raw_data/manifest.json` is the completeness marker for the local input bundle.
The bundle contains the exact scenario configuration, source inventory, aligned
elevation, WorldCover, surface-water-occurrence and rainfall rasters, plus the
AOI, neighborhood points, inspection zones, OSM buildings, river reaches,
125 m corridor, and exclusive distance bands. Stage 1 restores this bundle
without remote calls, then deliberately reloads `river.geojson` and
`dams.geojson` so reviewed hydrography updates override older cached vectors.

Treat the cache as a dated source snapshot. Retain `source_inventory.csv` and
`manifest.json` with any archived copy, and rebuild deliberately when newer
source data or a different configuration is required.

| Notebook variable | Accepted data | Required checks |
|---|---|---|
| `MODEL_CONFIG_JSON` | JSON matching `model_config.example.json` | Weights sum to 1; projected CRS uses metres |
| `config.aoi_path` | GeoJSON/GPKG polygon | Nairobi boundary source, date, CRS, valid geometry |
| `EVENT_OR_IDF_RAINFALL_RASTER` | GeoTIFF in millimetres | Duration, event dates or return period, native resolution, no negative values |
| `RIVER_FILE` | `raw_data/vectors/river.geojson`; line geometry | River/stream/canal/drain/ditch tags, topology, completeness, source date, licence, CRS |
| `DAMS_FILE` | `raw_data/vectors/dams.geojson`; polygon geometry | Water-body classification, valid polygons, completeness, source date, licence, CRS |
| `CURVE_NUMBER_RASTER` | GeoTIFF with values 30–100 | Antecedent moisture condition, soil source, land-cover source, CRS |
| `POPULATION_DENSITY_RASTER` | GeoTIFF in people/km² | Density rather than cell counts, reference year, licence, CRS |
| `CRITICAL_FACILITIES_FILE` | GeoJSON/GPKG points or polygons | Deduplication, facility type, operating status, CRS |
| `VULNERABILITY_INDEX_RASTER` | GeoTIFF normalized to 0–1 | Indicator rationale, direction, date, missing-data policy, CRS |
| `OBSERVED_FLOOD_MASK` | GeoTIFF: 1 flood, 0 confirmed dry, NaN unknown | Independent source, event date, observation limits, CRS |
| `INCIDENT_POINTS_FILE` | GeoJSON/GPKG points | Independent geocoding, event date, positional uncertainty, duplicates |

Do not encode unreported areas as dry. Absence of an incident report is not a
negative flood observation. Keep each event in a separate labelled raster and
run validation separately before pooling results.

The three corridor settings in `model_config.example.json` control a screening
mask: distance from a mapped centre-line, maximum HAND, and minimum relative
hazard score. This mask is not a hydraulic floodplain. Depth, velocity, and
return-period corridors require channel geometry, capacity, structures,
discharge/boundary conditions, and a calibrated SWMM/HEC-RAS-class model.

Planetary Computer's historical GPM IMERG store is half-hourly but is chunked
across the global spatial grid. Long local time-series extraction can therefore
transfer far more data than the Nairobi subset suggests. Prepare a documented
event/IDF raster separately, or use local gauge-derived IDF data, then pass that
raster through `EVENT_OR_IDF_RAINFALL_RASTER`.

The public `sentinel-1-rtc` metadata can be searched anonymously, but retrieval
of its RTC assets requires a Planetary Computer account. It is therefore not an
automatic dependency of this no-credential workflow.

The optional river-corridor stage uses a -16 dB median-VV threshold as an
uncalibrated starting point. Validate it against known wet/dry observations and
review radar shadow, permanent water, vegetation, and acquisition coverage
before interpreting the resulting area as flood extent. OpenStreetMap building
footprints are incomplete in some places and are exposure objects, not people.
The 2026 regression layer extrapolates only three historical seasonal
composites. Treat it as a transparent scenario baseline, not an operational
forecast; add event rainfall forecasts, upstream gauge/discharge observations,
and independent flood labels before using it for decisions.

River-distance statistics use mutually exclusive bands. A building footprint
that crosses more than one band is assigned to the nearest intersected band so
it is not double-counted within an inspection area. The same footprint can
still appear in two overlapping inspection areas; aggregate charts state this
explicitly.
