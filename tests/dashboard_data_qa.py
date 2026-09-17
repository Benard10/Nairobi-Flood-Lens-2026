"""Validate generated dashboard coverage and consistency; no model rerun.

Run: python tests/dashboard_data_qa.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_maplibre_dashboard as builder
import json
import numpy as np
import geopandas as gpd
import rasterio
from rasterio.features import geometry_mask
from PIL import Image

metrics = json.loads((builder.DATA / "dashboard_metrics.json").read_text())
names = {row["name"] for row in metrics["neighborhoods"]}
zones = gpd.read_file(builder.DATA / "inspection_zones.geojson")
assert len(zones) == 6 and set(zones["name"]) == names
for path in builder.DATA.glob("*.geojson"):
    frame = gpd.read_file(path)
    assert frame.crs.to_epsg() == 4326, path
    assert frame.geometry.is_valid.all(), f"Invalid browser geometry: {path}"
    x0, y0, x1, y1 = frame.total_bounds
    assert -180 <= x0 <= x1 <= 180 and -90 <= y0 <= y1 <= 90, path

with rasterio.open(builder.EXPORTS / "nairobi_flood_screening.tif") as dataset:
    inside = geometry_mask(zones.to_crs(dataset.crs).geometry,
        out_shape=(dataset.height, dataset.width), transform=dataset.transform, invert=True)
    for file, band in [("hazard_score.png", "hazard_score"), ("planning_priority.png", "planning_priority_score")]:
        values = dataset.read(dataset.descriptions.index(band) + 1, masked=True)
        valid = ~np.ma.getmaskarray(values) & np.isfinite(values.filled(np.nan))
        alpha = np.asarray(Image.open(builder.DATA / file))[:, :, 3]
        assert np.array_equal(alpha > 0, valid), f"Incomplete analysis coverage: {file}"
        assert np.any(alpha[~inside]), f"Analysis is incorrectly limited to six buffers: {file}"
    alpha = np.asarray(Image.open(builder.DATA / "candidate_corridor.png"))[:, :, 3]
    assert not np.any(alpha[~inside]), "Candidate river raster extends outside the study buffers"

projected_zones = zones.to_crs("EPSG:32737")
for filename in ["river_buffer_intervals.geojson", "river_corridor.geojson"]:
    frame = gpd.read_file(builder.DATA / filename).to_crs(projected_zones.crs)
    frame.geometry = frame.geometry.make_valid()
    outside = frame.geometry.union_all().difference(projected_zones.geometry.union_all().buffer(.01))
    assert outside.area < .1, f"River buffers extend outside six study areas: {filename}"

buildings = gpd.read_file(builder.DATA / "buildings.geojson")
for area in names:
    rows = [row for row in metrics["buffer_buildings"] if row["inspection_area"] == area]
    selected = buildings[buildings["inspection_area"] == area]
    assert len(selected) == sum(row["osm_buildings_in_buffer"] for row in rows), area
    assert int(selected["potentially_affected"].sum()) == sum(row["osm_buildings_potentially_affected"] for row in rows), area
assert {item["key"] for item in metrics["layer_manifest"]} == {
    "hazard", "priority", "candidate", "buildings", "analysis_rivers", "drainage",
    "buffer_intervals", "dams", "river_corridor", "catchment", "inspection", "locations",
}
print("PASS full valid raster coverage, river buffers confined to six areas, GeoJSON coordinates, building counts and layer manifest")
