"""Build browser-ready assets for the MapLibre flood dashboard."""

from __future__ import annotations

import json
import shutil
import os
import importlib.util
from pathlib import Path
from datetime import datetime, timezone

# The asset builder only needs local exports, not the STAC download stack.
# Select the same bundled PROJ database used by the analysis before imports.
for package, relative in (("rasterio", "proj_data"), ("pyproj", "proj_dir/share/proj")):
    spec = importlib.util.find_spec(package)
    if spec and spec.origin:
        bundled = Path(spec.origin).parent / relative
        if (bundled / "proj.db").exists():
            os.environ["PROJ_DATA"] = os.environ["PROJ_LIB"] = str(bundled)
            import pyproj
            pyproj.datadir.set_data_dir(str(bundled))
            break
import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from rasterio.warp import transform_bounds
from rasterio.features import geometry_mask


ROOT = Path(__file__).resolve().parent
EXPORTS = ROOT / "exports"
DASHBOARD = ROOT / "maplibre_dashboard"
DATA = DASHBOARD / "data"


def _write_geojson(frame, destination):
    frame = frame.to_crs("EPSG:4326").copy()
    # Projection can introduce tiny self-intersections in narrow buffer rings.
    # Repair polygon topology before browser serialization.
    invalid = ~frame.geometry.is_valid & frame.geom_type.isin(["Polygon", "MultiPolygon"])
    if invalid.any():
        frame.loc[invalid, "geometry"] = frame.loc[invalid, "geometry"].buffer(0)
    if not frame.geometry.is_valid.all():
        raise ValueError(f"Invalid dashboard geometry: {destination}")
    frame.to_file(destination, driver="GeoJSON")


def _reviewed_rivers_and_bands(aoi, zones):
    """Match Stage 1's reviewed line selection, AOI clip and exclusive bands."""
    rivers = gpd.read_file(ROOT / "raw_data/vectors/river.geojson")
    rivers = rivers[
        rivers.geom_type.isin(["LineString", "MultiLineString"])
        & rivers["waterway"].fillna("").str.casefold().isin(["river", "stream", "canal", "drain", "ditch"])
    ].copy()
    rivers = gpd.clip(rivers.to_crs(aoi.crs), aoi.geometry.union_all()).explode(index_parts=False).reset_index(drop=True)
    rivers = rivers[rivers.geom_type.isin(["LineString", "MultiLineString"]) & ~rivers.geometry.is_empty].copy()
    names = rivers["name"]
    if "name_en" in rivers:
        names = names.fillna(rivers["name_en"])
    rivers["display_name"] = names.fillna("(unnamed waterway)")
    rivers["model_source"] = "river.geojson"
    if "id" in rivers and "osm_id" not in rivers:
        rivers["osm_id"] = rivers["id"].astype(str)
    projected = rivers.to_crs(zones.crs)
    records = []
    for _, zone in zones.iterrows():
        parts = projected[projected.geometry.intersects(zone.geometry)]
        if parts.empty:
            continue
        lines = parts.geometry.intersection(zone.geometry).union_all()
        previous, lower = None, 0.0
        for upper in (31.0, 62.5, 125.0):
            cumulative = lines.buffer(upper).intersection(zone.geometry)
            ring = cumulative if previous is None else cumulative.difference(previous)
            if not ring.is_empty:
                records.append({"inspection_area": zone["name"], "distance_from_m": lower,
                                "distance_to_m": upper, "interval_label": f"{lower:g}-{upper:g} m",
                                "area_ha": ring.area / 10_000, "geometry": ring})
            previous, lower = cumulative, upper
    return rivers, gpd.GeoDataFrame(records, geometry="geometry", crs=zones.crs)


def _write_overlay(dataset: rasterio.DatasetReader, band_name: str, filename: str, zones: gpd.GeoDataFrame | None = None) -> dict:
    band_index = dataset.descriptions.index(band_name) + 1
    values = dataset.read(band_index, masked=True).astype("float32")
    if zones is not None:
        inside = geometry_mask(
            zones.to_crs(dataset.crs).geometry, out_shape=values.shape,
            transform=dataset.transform, invert=True,
        )
        values = np.ma.masked_where(~inside, values)
    output = DATA / filename
    if band_name == "candidate_affected_corridor":
        rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
        active = (~values.mask) & (values.filled(0) > 0)
        rgba[active] = [239, 68, 68, 190]
        plt.imsave(output, rgba)
    else:
        cmap_name = "RdYlBu_r" if band_name == "hazard_score" else "magma"
        normalized = np.clip(values.filled(np.nan), 0, 1)
        rgba = plt.get_cmap(cmap_name)(np.nan_to_num(normalized), bytes=True)
        rgba[..., 3] = np.where(np.isfinite(normalized), 190, 0).astype(np.uint8)
        plt.imsave(output, rgba)
    west, south, east, north = transform_bounds(
        dataset.crs, "EPSG:4326", *dataset.bounds, densify_pts=21
    )
    return {
        "file": f"data/{filename}",
        "coordinates": [[west, north], [east, north], [east, south], [west, south]],
    }


def _records(frame: pd.DataFrame) -> list[dict]:
    return json.loads(frame.replace({np.nan: None}).to_json(orient="records"))


def build() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    forecast_scenario = json.loads(
        (ROOT / "forecast_scenarios" / "ond_2026.json").read_text(encoding="utf-8")
    )
    neighborhoods = pd.read_csv(EXPORTS / "nairobi_neighborhood_summary.csv")
    catchment = pd.read_csv(EXPORTS / "south_c_catchment_summary.csv")
    corridors = pd.read_csv(EXPORTS / "drainage_corridor_summary.csv")
    dams = gpd.read_file(EXPORTS / "mapped_dams.geojson")
    buffer_summary_path = EXPORTS / "building_screening_summary.csv"
    buffer_buildings = (
        pd.read_csv(buffer_summary_path) if buffer_summary_path.exists() else pd.DataFrame()
    )
    zones = gpd.read_file(ROOT / "raw_data/vectors/inspection_zones.geojson")
    if len(zones) != 6 or set(zones["name"]) != set(neighborhoods["name"]):
        raise ValueError("The six study buffers must match the analysis summary areas")
    aoi = gpd.read_file(ROOT / "raw_data/vectors/aoi.geojson")
    drainage, intervals = _reviewed_rivers_and_bands(aoi, zones)

    with rasterio.open(EXPORTS / "nairobi_flood_screening.tif") as dataset:
        overlays = {
            "hazard": _write_overlay(dataset, "hazard_score", "hazard_score.png"),
            "priority": _write_overlay(dataset, "planning_priority_score", "planning_priority.png"),
            "corridor": _write_overlay(
                dataset, "candidate_affected_corridor", "candidate_corridor.png", zones
            ),
        }

    copies = {
        ROOT / "forecast_scenarios/nairobi_preparedness_context.json": DATA / "preparedness_context.json",
        EXPORTS / "report_assets/figure_02_hydrology.png": DATA / "story_environment.png",
        EXPORTS / "report_assets/figure_06_candidate_corridors.png": DATA / "story_disaster_management.png",
        EXPORTS / "report_assets/figure_05_hazard_and_priority.png": DATA / "story_business.png",
        EXPORTS / "south_c_terrain_catchment.geojson": DATA / "south_c_catchment.geojson",
        EXPORTS / "mapped_drainage_reaches.geojson": DATA / "drainage.geojson",
        EXPORTS / "mapped_dams.geojson": DATA / "dams.geojson",
        EXPORTS / "river_corridor_125m.geojson": DATA / "river_corridor.geojson",
        ROOT / "raw_data/vectors/inspection_zones.geojson": DATA / "inspection_zones.geojson",
        ROOT / "raw_data/vectors/neighborhoods.geojson": DATA / "neighborhoods.geojson",
    }
    for source, destination in copies.items():
        if source.suffix == ".geojson":
            frame = gpd.read_file(source)
            if source.name == "inspection_zones.geojson":
                frame = frame.merge(neighborhoods, on="name", how="left", suffixes=("", "_summary"))
            _write_geojson(frame, destination)
        else:
            shutil.copyfile(source, destination)
    _write_geojson(drainage, DATA / "analysis_rivers.geojson")
    _write_geojson(intervals, DATA / "river_buffer_intervals.geojson")

    screened_buildings_path = EXPORTS / "drainage_area_buildings.geojson"
    if screened_buildings_path.exists():
        buildings = gpd.read_file(screened_buildings_path)
    else:
        buildings = gpd.read_file(ROOT / "raw_data/vectors/osm_buildings.geojson")
        buildings = buildings[["geometry"]]
        buildings["potentially_affected"] = False
        buildings["inspection_area"] = "Not yet screened"
        buildings["interval_label"] = "Not yet screened"
    buildings = buildings.to_crs("EPSG:4326")
    buildings.geometry = buildings.geometry.simplify(0.000005, preserve_topology=True)
    _write_geojson(buildings, DATA / "buildings.geojson")

    catchment_row = catchment.iloc[0]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_from": "Nairobi flood screening outputs using reviewed river and dam layers",
        "headline": {
            "inspection_areas": int(len(neighborhoods)),
            "highest_mean_hazard": float(neighborhoods["hazard_score_mean"].max()),
            "high_hazard_area_ha": float(neighborhoods["high_hazard_area_ha"].sum()),
            "candidate_corridor_area_ha": float(corridors["candidate_affected_area_ha"].sum()),
            "mapped_dams": int(len(dams)),
            "analysis_rivers": int(len(drainage)),
        },
        "south_c": {
            "catchment_area_km2": float(catchment_row["catchment_area_km2"]),
            "mean_hazard_score": float(catchment_row["mean_hazard_score"]),
            "high_hazard_area_pct": float(catchment_row["high_hazard_area_pct"]),
            "wilson_airport_upstream": bool(
                catchment_row["wilson_airport_reference_in_catchment"]
            ),
            "outlet_needs_review": bool(catchment_row["snap_near_search_limit"]),
        },
        "neighborhoods": _records(neighborhoods),
        "corridors": _records(corridors),
        "buffer_buildings": _records(buffer_buildings),
        "forecast_2026": forecast_scenario,
        "overlays": overlays,
        "layer_manifest": [
            {"key": "hazard", "label": "Flood concern", "default_visible": True},
            {"key": "priority", "label": "Inspection priority", "default_visible": False},
            {"key": "candidate", "label": "River flood concern areas", "default_visible": False},
            {"key": "buildings", "label": "Screened buildings", "default_visible": True},
            {"key": "analysis_rivers", "label": "All rivers and drains", "default_visible": True},
            {"key": "drainage", "label": "Rivers and drains in study areas", "default_visible": True},
            {"key": "buffer_intervals", "label": "Distance from rivers (3 bands)", "default_visible": False},
            {"key": "dams", "label": "Dams and water bodies", "default_visible": True},
            {"key": "river_corridor", "label": "Land within 125 m of rivers", "default_visible": True},
            {"key": "catchment", "label": "South C drainage area", "default_visible": True},
            {"key": "inspection", "label": "Study boundaries (6)", "default_visible": True},
            {"key": "locations", "label": "Study locations and names", "default_visible": True},
        ],
        "warning": "The current map is the screening baseline for the OND 2026 scenario—not observed flooding, predicted depth, or a completed hydraulic forecast.",
    }
    from build_dashboard_tiles import build_tiles
    build_tiles()
    (DATA / "dashboard_metrics.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )
    print(f"Dashboard assets written to {DATA}")


if __name__ == "__main__":
    build()
