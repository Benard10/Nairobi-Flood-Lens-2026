"""Staged, authentication-free Nairobi flood screening with Planetary Computer."""

from __future__ import annotations

import heapq
import json
import os
import importlib.util
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _isolate_proj_database() -> None:
    """Prevent a PostgreSQL/PostGIS PROJ database from overriding Python wheels."""
    rasterio_spec = importlib.util.find_spec("rasterio")
    pyproj_spec = importlib.util.find_spec("pyproj")
    candidates = []
    if rasterio_spec and rasterio_spec.origin:
        candidates.append(Path(rasterio_spec.origin).parent / "proj_data")
    if pyproj_spec and pyproj_spec.origin:
        candidates.append(Path(pyproj_spec.origin).parent / "proj_dir" / "share" / "proj")
    bundled = next((path for path in candidates if (path / "proj.db").exists()), None)
    if bundled is None:
        return
    # Use one known-compatible database for pyproj, rasterio, and GDAL. This is
    # intentionally set before importing the rest of the geospatial stack.
    os.environ["PROJ_DATA"] = str(bundled)
    os.environ["PROJ_LIB"] = str(bundled)
    import pyproj

    pyproj.datadir.set_data_dir(str(bundled))


_isolate_proj_database()

import geopandas as gpd  # noqa: E402
import folium  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import odc.stac  # noqa: E402
import pandas as pd  # noqa: E402
import planetary_computer  # noqa: E402
import pystac_client  # noqa: E402
import rasterio  # noqa: E402
import rioxarray  # noqa: E402,F401 - registers the .rio accessor
import xarray as xr  # noqa: E402
from rasterio.enums import Resampling  # noqa: E402
from rasterio.features import geometry_mask, rasterize, shapes  # noqa: E402
from scipy.ndimage import distance_transform_edt, uniform_filter  # noqa: E402
from shapely.geometry import LineString, Point, Polygon, box, shape  # noqa: E402
from branca.element import Element  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402


STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
PIPELINE_VERSION = "2026-09-15-reviewed-rivers-dams-v5.3"
RIVER_BUFFER_COLORS = {
    31.0: "#08306b",
    62.5: "#2171b5",
    125.0: "#6baed6",
}
MAX_RIVER_CORRIDOR_M = 125.0
NEIGHBORHOODS = (
    {"name": "Mathare Valley", "lat": -1.2588, "lon": 36.8572, "zone": "Nairobi North Corridor"},
    {"name": "Korogocho", "lat": -1.2515, "lon": 36.8925, "zone": "Nairobi North Corridor"},
    {"name": "Gikomba Market", "lat": -1.2854, "lon": 36.8378, "zone": "Nairobi Central Basin"},
    {"name": "South C", "lat": -1.3211, "lon": 36.8286, "zone": "Nairobi South Plains"},
    {"name": "South B", "lat": -1.3145, "lon": 36.8452, "zone": "Nairobi South Plains"},
    {"name": "Kibera", "lat": -1.3133, "lon": 36.7885, "zone": "Nairobi South Plains"},
)


@dataclass(frozen=True)
class PCModelConfig:
    bbox: tuple[float, float, float, float] = (36.65, -1.45, 37.11, -1.15)
    crs: str = "EPSG:32737"
    # Terrain and land-cover processing remains at the native DEM scale. Coarse
    # rainfall is never described as having 30 m information content.
    resolution_m: int = 30
    reporting_resolution_m: int = 90
    hydrology_buffer_m: int = 5000
    aoi_path: str | None = None
    neighborhood_radius_m: int = 1000
    terrain_window_m: int = 900
    built_density_window_m: int = 450
    water_influence_m: int = 1500
    stream_threshold_km2: float = 1.0
    # Buffer distance on each side of a mapped river/drainage centre-line.
    drainage_corridor_m: int = 125
    corridor_hand_max_m: float = 5.0
    corridor_hazard_threshold: float = 0.60
    rainfall_start: str = "2001-01-01"
    rainfall_end: str = "2020-12-31"
    percentile_low: float = 2.0
    percentile_high: float = 98.0
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "extreme_rainfall": 0.10,
            "topographic_wetness": 0.20,
            "low_hand": 0.20,
            "depression_storage": 0.15,
            "drainage_proximity": 0.15,
            "runoff_potential": 0.20,
        }
    )

    @classmethod
    def from_json(cls, path: str | Path) -> "PCModelConfig":
        """Create a reproducible scenario from a version-controlled JSON file."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Model configuration JSON must contain an object")
        config = cls(**payload)
        config.validate()
        return config

    def validate(self) -> None:
        west, south, east, north = self.bbox
        if not (west < east and south < north):
            raise ValueError("bbox must be (west, south, east, north)")
        if self.resolution_m <= 0:
            raise ValueError("resolution_m must be positive")
        if self.reporting_resolution_m < self.resolution_m:
            raise ValueError("reporting_resolution_m cannot be finer than resolution_m")
        if self.hydrology_buffer_m < 0 or self.stream_threshold_km2 <= 0:
            raise ValueError("hydrology buffer and stream threshold must be positive")
        if self.drainage_corridor_m <= 0 or self.corridor_hand_max_m < 0:
            raise ValueError("drainage corridor width must be positive and HAND limit non-negative")
        if not 0 <= self.corridor_hazard_threshold <= 1:
            raise ValueError("corridor_hazard_threshold must be between 0 and 1")
        expected = {"extreme_rainfall", "topographic_wetness", "low_hand",
                    "depression_storage", "drainage_proximity", "runoff_potential"}
        if set(self.weights) != expected:
            raise ValueError(f"weights must contain exactly {sorted(expected)}")
        if abs(sum(self.weights.values()) - 1.0) > 1e-9:
            raise ValueError("weights must sum to 1.0")


@dataclass
class PCSourceBundle:
    catalog: pystac_client.Client | None
    dem_items: list[Any]
    worldcover_items: list[Any]
    water_items: list[Any]
    inventory: pd.DataFrame

@dataclass
class PCModelOutputs:
    aoi: gpd.GeoDataFrame
    layers: xr.Dataset
    neighborhoods: gpd.GeoDataFrame
    neighborhood_zones: gpd.GeoDataFrame
    summary: pd.DataFrame
    inventory: pd.DataFrame
    drainage_reaches: gpd.GeoDataFrame | None = None
    drainage_corridor_summary: pd.DataFrame | None = None
    terrain_catchments: gpd.GeoDataFrame | None = None
    terrain_pour_points: gpd.GeoDataFrame | None = None
    reference_points: gpd.GeoDataFrame | None = None
    catchment_summary: pd.DataFrame | None = None
    buffer_building_exposure: gpd.GeoDataFrame | None = None
    buffer_building_summary: pd.DataFrame | None = None
    mapped_dams: gpd.GeoDataFrame | None = None


@dataclass
class DrainageCorridorOutputs:
    """Named mapped reaches, screening masks, and per-area corridor statistics."""

    reaches: gpd.GeoDataFrame
    layers: xr.Dataset
    summary: pd.DataFrame


@dataclass
class TerrainCatchmentOutputs:
    """DEM-delineated contributing area and its snapped terrain outlet."""

    catchment: gpd.GeoDataFrame
    pour_point: gpd.GeoDataFrame
    layers: xr.Dataset
    summary: pd.DataFrame


@dataclass
class RiverFloodExtentOutputs:
    """Event water masks, anomaly layer, and corridor/building statistics."""

    masks: xr.Dataset
    summary: pd.DataFrame
    historical_max: xr.DataArray | None = None
    predicted_expansion: xr.DataArray | None = None
    regression: xr.Dataset | None = None


@dataclass
class AnalysisInputCache:
    """Local, reloadable copy of every core input used by the staged workflow."""

    config: PCModelConfig
    sources: PCSourceBundle
    raw: xr.Dataset
    aoi: gpd.GeoDataFrame
    neighborhoods: gpd.GeoDataFrame
    zones: gpd.GeoDataFrame
    mapped_drainage: gpd.GeoDataFrame
    buildings: gpd.GeoDataFrame
    river_reaches: gpd.GeoDataFrame
    river_corridor: gpd.GeoDataFrame
    buffer_intervals: gpd.GeoDataFrame
    manifest_path: Path


INPUT_CACHE_SCHEMA_VERSION = 2


def analysis_input_cache_ready(output_dir: str | Path = "raw_data") -> bool:
    """Return whether a complete, readable staged-input cache is available locally."""
    destination = Path(output_dir)
    manifest_path = destination / "manifest.json"
    if not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if manifest.get("schema_version") != INPUT_CACHE_SCHEMA_VERSION:
        return False
    raster_files = manifest.get("rasters", {})
    required = ["config.json", "source_inventory.csv"]
    required.extend(raster_files.values())
    required.extend(manifest.get("vectors", {}).values())
    if not required or not all((destination / relative).exists() for relative in required):
        return False
    # A previous rainfall-reprojection bug could write a correctly shaped
    # GeoTIFF containing only NaNs. File existence alone therefore cannot
    # establish that the cache is usable.
    try:
        for relative in raster_files.values():
            with rasterio.open(destination / relative) as dataset:
                values = dataset.read(masked=True)
                if values.size == 0 or not np.isfinite(values.filled(np.nan)).any():
                    return False
    except (OSError, rasterio.errors.RasterioError, ValueError):
        return False
    return True


def _cache_json_value(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _cache_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_cache_json_value(item) for item in value]
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _inventory_with_osm_vectors(
    inventory: pd.DataFrame,
    mapped_drainage: gpd.GeoDataFrame | None,
    buildings: gpd.GeoDataFrame | None,
    mapped_dams: gpd.GeoDataFrame | None = None,
) -> pd.DataFrame:
    """Add non-duplicated reviewed hydrography/building provenance rows."""
    result = inventory.copy()
    existing = set(result.get("collection", pd.Series(dtype=str)).astype(str))
    rows: list[dict[str, Any]] = []
    definitions = (
        (
            "openstreetmap-waterways", mapped_drainage,
            "reviewed river/stream/canal/drain/ditch centre-lines",
            "river.geojson; feature IDs retained in osm_id",
        ),
        (
            "openstreetmap-waterbodies", mapped_dams,
            "reviewed dam and mapped water-body polygons",
            "dams.geojson; feature IDs retained in osm_id",
        ),
        (
            "openstreetmap-buildings", buildings,
            "building footprint polygons within reviewed river corridors",
            "building cache; feature IDs retained in osm_id",
        ),
    )
    for collection, frame, asset, item_note in definitions:
        if frame is None or frame.empty or collection in existing:
            continue
        retrieval = frame.get("retrieved_utc", pd.Series(dtype=object)).dropna()
        rows.append({
            "collection": collection,
            "asset": asset,
            "period": "cached OpenStreetMap snapshot",
            "items": len(frame),
            "item_ids": item_note,
            "collection_version": "OpenStreetMap live database at retrieval time",
            "license": "ODbL 1.0",
            "providers": "OpenStreetMap contributors",
            "retrieved_utc": str(retrieval.iloc[0]) if len(retrieval) else "not recorded",
            "search_bbox_wgs84": tuple(float(value) for value in frame.to_crs("EPSG:4326").total_bounds),
        })
    if rows:
        result = pd.concat([result, pd.DataFrame(rows)], ignore_index=True)
    return result


def export_analysis_input_cache(
    raw: xr.Dataset,
    sources: PCSourceBundle,
    config: PCModelConfig,
    aoi: gpd.GeoDataFrame,
    neighborhoods: gpd.GeoDataFrame,
    zones: gpd.GeoDataFrame,
    mapped_drainage: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    river_reaches: gpd.GeoDataFrame,
    river_corridor: gpd.GeoDataFrame,
    buffer_intervals: gpd.GeoDataFrame,
    output_dir: str | Path = "raw_data",
) -> tuple[Path, ...]:
    """Persist every core input needed to rerun the notebook without downloads."""
    destination = Path(output_dir)
    raster_dir = destination / "rasters"
    vector_dir = destination / "vectors"
    raster_dir.mkdir(parents=True, exist_ok=True)
    vector_dir.mkdir(parents=True, exist_ok=True)

    config_path = destination / "config.json"
    inventory_path = destination / "source_inventory.csv"
    config_path.write_text(
        json.dumps(_cache_json_value(dict(config.__dict__)), indent=2), encoding="utf-8"
    )
    complete_inventory = _inventory_with_osm_vectors(
        sources.inventory, mapped_drainage, buildings
    )
    complete_inventory.to_csv(inventory_path, index=False)
    paths: list[Path] = [config_path, inventory_path]

    raster_files: dict[str, str] = {}
    raster_attrs: dict[str, dict[str, Any]] = {}
    for name, layer in raw.data_vars.items():
        raster_path = raster_dir / f"{name}.tif"
        # A layer reopened from this cache may contain CF serialization fields
        # in both attrs and encoding. Xarray rejects that duplication during a
        # second write, so write a shallow copy with encoding-owned keys removed
        # from attrs while preserving the original metadata in the manifest.
        writable = layer.copy(deep=False)
        for key in set(writable.attrs).intersection(writable.encoding):
            writable.attrs.pop(key, None)
        writable.rio.to_raster(raster_path, compress="deflate", tiled=True)
        raster_files[name] = raster_path.relative_to(destination).as_posix()
        raster_attrs[name] = _cache_json_value(dict(layer.attrs))
        paths.append(raster_path)

    vector_frames = {
        "aoi": aoi,
        "neighborhoods": neighborhoods,
        "inspection_zones": zones,
        "mapped_drainage": mapped_drainage,
        "osm_buildings": buildings,
        "river_reaches": river_reaches,
        "river_corridor_125m": river_corridor,
        "river_buffer_intervals": buffer_intervals,
    }
    vector_files: dict[str, str] = {}
    vector_crs: dict[str, str | None] = {}
    for name, frame in vector_frames.items():
        vector_path = vector_dir / f"{name}.geojson"
        vector_crs[name] = frame.crs.to_string() if frame.crs is not None else None
        if frame.empty:
            vector_path.write_text(
                '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
            )
        else:
            properties = tuple(column for column in frame.columns if column != frame.geometry.name)
            folium_safe_geodataframe(frame, properties).to_file(vector_path, driver="GeoJSON")
        vector_files[name] = vector_path.relative_to(destination).as_posix()
        paths.append(vector_path)

    manifest_path = destination / "manifest.json"
    manifest = {
        "schema_version": INPUT_CACHE_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "purpose": "Complete local input cache for the staged Nairobi flood-screening notebook",
        "rasters": raster_files,
        "raster_attrs": raster_attrs,
        "dataset_attrs": _cache_json_value(dict(raw.attrs)),
        "vectors": vector_files,
        "vector_crs": vector_crs,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    paths.append(manifest_path)
    return tuple(paths)


def load_analysis_input_cache(output_dir: str | Path = "raw_data") -> AnalysisInputCache:
    """Load the complete local input cache written by ``export_analysis_input_cache``."""
    destination = Path(output_dir)
    if not analysis_input_cache_ready(destination):
        raise FileNotFoundError(f"Incomplete or unsupported analysis input cache: {destination}")
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    config = PCModelConfig.from_json(destination / "config.json")
    inventory = pd.read_csv(destination / "source_inventory.csv")
    sources = PCSourceBundle(None, [], [], [], inventory)

    layers = []
    for name, relative in manifest["rasters"].items():
        opened = rioxarray.open_rasterio(destination / relative, masked=True)
        layer = opened.squeeze(drop=True).rename(name).load()
        opened.close()
        layer.attrs.update(manifest.get("raster_attrs", {}).get(name, {}))
        layers.append(layer)
    raw = xr.merge(layers, compat="override", join="exact")
    raw.attrs.update(manifest.get("dataset_attrs", {}))

    vectors: dict[str, gpd.GeoDataFrame] = {}
    for name, relative in manifest["vectors"].items():
        frame = gpd.read_file(destination / relative)
        cached_crs = manifest.get("vector_crs", {}).get(name)
        if frame.crs is None and cached_crs:
            frame = frame.set_crs(cached_crs)
        vectors[name] = frame
    return AnalysisInputCache(
        config=config,
        sources=sources,
        raw=raw,
        aoi=vectors["aoi"],
        neighborhoods=vectors["neighborhoods"],
        zones=vectors["inspection_zones"],
        mapped_drainage=vectors["mapped_drainage"],
        buildings=vectors["osm_buildings"],
        river_reaches=vectors["river_reaches"],
        river_corridor=vectors["river_corridor_125m"],
        buffer_intervals=vectors["river_buffer_intervals"],
        manifest_path=manifest_path,
    )


def open_catalog() -> pystac_client.Client:
    """Open public STAC with automatically generated temporary SAS signatures."""
    return pystac_client.Client.open(STAC_URL, modifier=planetary_computer.sign_inplace)


def _aoi_wgs84(config: PCModelConfig) -> gpd.GeoDataFrame:
    """Read an approved AOI polygon, falling back to the documented rectangle."""
    if config.aoi_path:
        aoi = gpd.read_file(config.aoi_path)
        if aoi.empty:
            raise ValueError(f"AOI file contains no features: {config.aoi_path}")
        if aoi.crs is None:
            raise ValueError("AOI file must declare a CRS")
        geometry = aoi.to_crs("EPSG:4326").geometry.union_all()
        return gpd.GeoDataFrame([{"name": "Approved Nairobi AOI"}], geometry=[geometry], crs="EPSG:4326")
    return gpd.GeoDataFrame(
        [{"name": "Nairobi analysis extent (provisional rectangle)"}],
        geometry=[box(*config.bbox)],
        crs="EPSG:4326",
    )


def processing_bbox(config: PCModelConfig) -> tuple[float, float, float, float]:
    """Return an upstream-context search extent around the reporting AOI."""
    aoi = _aoi_wgs84(config).to_crs(config.crs)
    buffered = aoi.geometry.buffer(config.hydrology_buffer_m).union_all()
    return tuple(float(value) for value in gpd.GeoSeries([buffered], crs=config.crs).to_crs("EPSG:4326").total_bounds)


def make_review_geometries(
    config: PCModelConfig,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Create the approved/provisional AOI, target points, and assessment buffers."""
    aoi_wgs84 = _aoi_wgs84(config)
    neighborhoods = gpd.GeoDataFrame(
        list(NEIGHBORHOODS),
        geometry=[Point(item["lon"], item["lat"]) for item in NEIGHBORHOODS],
        crs="EPSG:4326",
    )
    zones = neighborhoods.to_crs(config.crs).copy()
    zones.geometry = zones.geometry.buffer(config.neighborhood_radius_m)
    return aoi_wgs84.to_crs(config.crs), neighborhoods, zones


def _request_overpass_json(
    query: str,
    endpoints: tuple[str, ...],
    *,
    timeout_s: int,
    retries_per_endpoint: int = 2,
) -> dict[str, Any]:
    """Run one Overpass query with bounded 429 retries and endpoint failover."""
    errors: list[str] = []
    for endpoint in dict.fromkeys(endpoints):
        for attempt in range(retries_per_endpoint):
            request = urllib.request.Request(
                endpoint,
                data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
                headers={"User-Agent": "NairobiFloodScreening/5.1 (research workflow)"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout_s + 30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                errors.append(f"{endpoint}: HTTP {error.code} {error.reason}")
                retryable = error.code in {429, 502, 503, 504}
                if not retryable or attempt + 1 >= retries_per_endpoint:
                    break
                retry_after = error.headers.get("Retry-After") if error.headers else None
                try:
                    delay_s = float(retry_after) if retry_after else 30.0
                except ValueError:
                    delay_s = 30.0
                time.sleep(min(max(delay_s, 1.0), 30.0))
            except (URLError, TimeoutError) as error:
                errors.append(f"{endpoint}: {error}")
                if attempt + 1 >= retries_per_endpoint:
                    break
                time.sleep(5.0)
    detail = "; ".join(errors[-6:])
    raise RuntimeError(
        "Every configured Overpass endpoint failed. Wait and rerun the cell, "
        "or supply a reviewed local building GeoJSON/GPKG as the cache. "
        f"Attempts: {detail}"
    )


def fetch_osm_drainage(
    config: PCModelConfig,
    *,
    target_areas: gpd.GeoDataFrame | None = None,
    endpoint: str = "https://overpass-api.de/api/interpreter",
    timeout_s: int = 180,
) -> gpd.GeoDataFrame:
    """Download mapped river, stream, canal, drain, and ditch centre-lines.

    OpenStreetMap is a useful mapping input, not an authoritative inventory of
    channel capacity or condition. By default the query covers the target-area
    envelopes plus the configured corridor margin. DEM-derived drainage still
    covers the full buffered hydrology processing extent.
    """
    if target_areas is None:
        _, _, target_areas = make_review_geometries(config)
    if target_areas.empty or target_areas.crs is None:
        raise ValueError("Target areas must be non-empty and declare a CRS")
    query_extent = target_areas.to_crs(config.crs).geometry.union_all().buffer(
        config.drainage_corridor_m
    )
    west, south, east, north = gpd.GeoSeries(
        [query_extent], crs=config.crs
    ).to_crs("EPSG:4326").total_bounds
    query = f"""
    [out:json][timeout:{timeout_s}];
    way["waterway"~"^(river|stream|canal|drain|ditch)$"]({south},{west},{north},{east});
    out tags geom;
    """
    request = urllib.request.Request(
        endpoint,
        data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
        headers={"User-Agent": "NairobiFloodScreening/4.0 (research workflow)"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout_s + 30) as response:
        payload = json.loads(response.read().decode("utf-8"))

    retrieved = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    for element in payload.get("elements", []):
        coordinates = [
            (point["lon"], point["lat"])
            for point in element.get("geometry", [])
            if "lon" in point and "lat" in point
        ]
        if len(coordinates) < 2:
            continue
        tags = element.get("tags", {})
        osm_id = int(element["id"])
        records.append(
            {
                "osm_id": osm_id,
                "name": tags.get("name"),
                "waterway": tags.get("waterway"),
                "intermittent": tags.get("intermittent"),
                "tunnel": tags.get("tunnel"),
                "source": "OpenStreetMap contributors",
                "source_url": f"https://www.openstreetmap.org/way/{osm_id}",
                "retrieved_utc": retrieved,
                "geometry": LineString(coordinates),
            }
        )
    columns = ["osm_id", "name", "waterway", "intermittent", "tunnel",
               "source", "source_url", "retrieved_utc", "geometry"]
    drainage = gpd.GeoDataFrame(records, columns=columns, geometry="geometry", crs="EPSG:4326")
    if drainage.empty:
        raise RuntimeError("OpenStreetMap returned no mapped waterways for the processing extent")
    query_bounds = gpd.GeoDataFrame(
        geometry=[box(west, south, east, north)], crs="EPSG:4326"
    )
    drainage = gpd.clip(drainage, query_bounds).explode(index_parts=False).reset_index(drop=True)
    drainage = drainage[~drainage.geometry.is_empty].copy()
    drainage["display_name"] = drainage["name"].fillna("(unnamed waterway)")
    return drainage


def load_and_clip_local_waterways(
    source_path: str | Path,
    aoi: gpd.GeoDataFrame,
    *,
    waterway_types: tuple[str, ...] = ("river", "stream", "canal", "drain", "ditch"),
) -> gpd.GeoDataFrame:
    """Load a broad local waterway layer and keep line drainage inside the AOI.

    Polygon water bodies and point features are intentionally excluded because
    the river-corridor model expects centre-lines. The unmodified source file is
    retained; callers can save the returned AOI clip as the active model input.
    """
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(f"Local waterway layer not found: {path}")
    if aoi.empty or aoi.crs is None:
        raise ValueError("AOI must be non-empty and declare a CRS")
    allowed = {str(value).casefold() for value in waterway_types}
    if not allowed:
        raise ValueError("At least one waterway type is required")

    source_preview = gpd.read_file(path, rows=1)
    if source_preview.crs is None:
        raise ValueError("Local waterway layer must declare a CRS")
    aoi_in_source_crs = aoi.to_crs(source_preview.crs)
    waterways = gpd.read_file(path, bbox=tuple(aoi_in_source_crs.total_bounds))
    if waterways.crs is None:
        raise ValueError("Local waterway layer must declare a CRS")
    if "waterway" not in waterways:
        raise ValueError("Local waterway layer must contain a 'waterway' field")
    waterways = waterways[
        waterways.geom_type.isin(["LineString", "MultiLineString"])
        & waterways["waterway"].fillna("").astype(str).str.casefold().isin(allowed)
    ].copy()
    if waterways.empty:
        raise ValueError("No supported line waterways intersect the AOI bounds")
    waterways = gpd.clip(waterways.to_crs(aoi.crs), aoi.geometry.union_all())
    waterways = waterways.explode(index_parts=False).reset_index(drop=True)
    waterways = waterways[
        waterways.geom_type.isin(["LineString", "MultiLineString"])
        & ~waterways.geometry.is_empty
    ].copy()
    if waterways.empty:
        raise ValueError("No supported line waterways remain after exact AOI clipping")

    names = waterways.get("name", pd.Series(index=waterways.index, dtype=object))
    if "name_en" in waterways:
        names = names.fillna(waterways["name_en"])
    waterways["display_name"] = names.fillna("(unnamed waterway)")
    if "id" in waterways and "osm_id" not in waterways:
        waterways["osm_id"] = waterways["id"].astype(str)
    waterways["model_source"] = path.name
    return waterways.to_crs("EPSG:4326")


def load_and_clip_local_dams(
    source_path: str | Path,
    aoi: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Load reviewed dam/water-body polygons and clip them to the analysis AOI."""
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(f"Local dam layer not found: {path}")
    if aoi.empty or aoi.crs is None:
        raise ValueError("AOI must be non-empty and declare a CRS")

    source_preview = gpd.read_file(path, rows=1)
    if source_preview.crs is None:
        raise ValueError("Local dam layer must declare a CRS")
    aoi_in_source_crs = aoi.to_crs(source_preview.crs)
    dams = gpd.read_file(path, bbox=tuple(aoi_in_source_crs.total_bounds))
    if dams.crs is None:
        raise ValueError("Local dam layer must declare a CRS")
    dams = dams[dams.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    if dams.empty:
        raise ValueError("No polygon dams or water bodies intersect the AOI bounds")
    dams = gpd.clip(dams.to_crs(aoi.crs), aoi.geometry.union_all())
    dams = dams[
        dams.geom_type.isin(["Polygon", "MultiPolygon"])
        & ~dams.geometry.is_empty
        & dams.geometry.is_valid
    ].copy().reset_index(drop=True)
    if dams.empty:
        raise ValueError("No valid dam polygons remain after exact AOI clipping")

    names = dams.get("name", pd.Series(index=dams.index, dtype="string")).astype("string")
    names = names.str.strip().replace("", pd.NA)
    if "name_en" in dams:
        names = names.fillna(dams["name_en"].astype("string").str.strip().replace("", pd.NA))
    dams["display_name"] = names.fillna("(unnamed dam or water body)")
    water = dams.get("water", pd.Series(index=dams.index, dtype="string")).astype("string").str.strip().replace("", pd.NA)
    waterway = dams.get("waterway", pd.Series(index=dams.index, dtype="string")).astype("string").str.strip().replace("", pd.NA)
    natural = dams.get(
        "natural_cl",
        dams.get("natural_class", pd.Series(index=dams.index, dtype=object)),
    ).astype("string").str.strip().replace("", pd.NA)
    dams["waterbody_type"] = water.fillna(waterway).fillna(natural).fillna("dam/water body")
    if "id" in dams and "osm_id" not in dams:
        dams["osm_id"] = dams["id"].astype(str)
    dams["model_source"] = path.name
    return dams.to_crs("EPSG:4326")


def load_and_clip_local_water_context(
    source_path: str | Path,
    aoi: gpd.GeoDataFrame,
    *,
    modeled_waterway_types: tuple[str, ...] = ("river", "stream", "canal", "drain", "ditch"),
) -> gpd.GeoDataFrame:
    """Return non-modelled water features for map context inside the AOI.

    Dams, reservoirs, ponds, weirs, waterfalls and water polygons/points remain
    visible, while line features used by the corridor model are excluded.
    """
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(f"Local waterway layer not found: {path}")
    if aoi.empty or aoi.crs is None:
        raise ValueError("AOI must be non-empty and declare a CRS")
    allowed = {str(value).casefold() for value in modeled_waterway_types}
    source_preview = gpd.read_file(path, rows=1)
    if source_preview.crs is None:
        raise ValueError("Local waterway layer must declare a CRS")
    aoi_in_source_crs = aoi.to_crs(source_preview.crs)
    features = gpd.read_file(path, bbox=tuple(aoi_in_source_crs.total_bounds))
    if features.crs is None:
        raise ValueError("Local waterway layer must declare a CRS")
    waterway = features.get("waterway", pd.Series(index=features.index, dtype=object))
    water = features.get("water", pd.Series(index=features.index, dtype=object))
    natural = features.get("natural_class", pd.Series(index=features.index, dtype=object))
    context_waterway_types = {
        "dam", "weir", "waterfall", "rapids", "lock_gate", "sluice_gate",
        "riverbank", "dock", "boatyard",
    }
    relevant = (
        waterway.fillna("").astype(str).str.casefold().isin(context_waterway_types)
        | water.notna()
        | natural.astype(str).str.casefold().eq("water")
    )
    modeled_lines = (
        features.geom_type.isin(["LineString", "MultiLineString"])
        & waterway.fillna("").astype(str).str.casefold().isin(allowed)
    )
    context = features[relevant & ~modeled_lines].copy()
    context = gpd.clip(context.to_crs(aoi.crs), aoi.geometry.union_all())
    context = context[~context.geometry.is_empty].copy().reset_index(drop=True)
    names = context.get("name", pd.Series(index=context.index, dtype=object))
    if "name_en" in context:
        names = names.fillna(context["name_en"])
    context["display_name"] = names.fillna("(unnamed water feature)")
    context["context_type"] = (
        context.get("waterway", pd.Series(index=context.index, dtype=object))
        .fillna(context.get("water", pd.Series(index=context.index, dtype=object)))
        .fillna(context.get("natural_class", pd.Series(index=context.index, dtype=object)))
        .fillna("water feature")
    )
    context["model_source"] = path.name
    return context.to_crs("EPSG:4326")


def fetch_osm_buildings(
    target_area: gpd.GeoDataFrame,
    *,
    endpoint: str = "https://overpass-api.de/api/interpreter",
    fallback_endpoints: tuple[str, ...] = (
        "https://overpass.private.coffee/api/interpreter",
        "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    ),
    timeout_s: int = 180,
) -> gpd.GeoDataFrame:
    """Download OSM building footprints whose polygons intersect ``target_area``.

    The query requests closed building ways. Multipolygon relations are not
    reconstructed, so the returned layer is a useful exposure inventory rather
    than an authoritative building register.
    """
    if target_area.empty or target_area.crs is None:
        raise ValueError("Building target area must be non-empty and declare a CRS")
    retrieved = datetime.now(timezone.utc).isoformat()
    records: list[dict[str, Any]] = []
    # Submit disconnected corridor bounding boxes as one Overpass union. The
    # server deduplicates ways, and one request avoids exhausting per-user slots.
    query_parts = target_area.to_crs("EPSG:4326").explode(index_parts=False)
    clauses: list[str] = []
    for geometry in query_parts.geometry:
        west, south, east, north = geometry.bounds
        clauses.append(f'way["building"]({south},{west},{north},{east});')
    query = (
        f"[out:json][timeout:{timeout_s}];\n(\n"
        + "\n".join(clauses)
        + "\n);\nout tags geom;"
    )
    payload = _request_overpass_json(
        query, (endpoint, *fallback_endpoints), timeout_s=timeout_s
    )
    elements = {int(element["id"]): element for element in payload.get("elements", [])}

    for element in elements.values():
        coordinates = [
            (point["lon"], point["lat"])
            for point in element.get("geometry", [])
            if "lon" in point and "lat" in point
        ]
        if len(coordinates) < 4 or coordinates[0] != coordinates[-1]:
            continue
        geometry = Polygon(coordinates)
        if geometry.is_empty or not geometry.is_valid:
            geometry = geometry.buffer(0)
        if geometry.is_empty:
            continue
        tags = element.get("tags", {})
        osm_id = int(element["id"])
        records.append(
            {
                "osm_id": osm_id,
                "name": tags.get("name"),
                "building": tags.get("building"),
                "building_levels": tags.get("building:levels"),
                "source": "OpenStreetMap contributors",
                "source_url": f"https://www.openstreetmap.org/way/{osm_id}",
                "retrieved_utc": retrieved,
                "geometry": geometry,
            }
        )
    columns = ["osm_id", "name", "building", "building_levels", "source",
               "source_url", "retrieved_utc", "geometry"]
    buildings = gpd.GeoDataFrame(records, columns=columns, geometry="geometry", crs="EPSG:4326")
    if buildings.empty:
        return buildings
    target_wgs84 = target_area.to_crs("EPSG:4326").geometry.union_all()
    buildings = buildings[buildings.geometry.intersects(target_wgs84)].copy()
    return buildings.reset_index(drop=True)


def folium_safe_geodataframe(
    frame: gpd.GeoDataFrame,
    property_columns: tuple[str, ...] = (),
) -> gpd.GeoDataFrame:
    """Return a lightweight frame whose properties are JSON serializable."""
    if frame.crs is None:
        raise ValueError("Map layer must declare a CRS")
    columns = [column for column in property_columns if column in frame.columns]
    result = frame[columns + [frame.geometry.name]].copy()

    def json_safe(value: Any) -> Any:
        if value is None or value is pd.NA:
            return None
        if isinstance(value, (pd.Timestamp, datetime)):
            return value.isoformat()
        if isinstance(value, np.generic):
            value = value.item()
        if isinstance(value, float) and not np.isfinite(value):
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    for column in columns:
        result[column] = result[column].map(json_safe)
    return result


def add_review_basemaps(map_object: folium.Map) -> folium.Map:
    """Add durable keyless satellite/hybrid and terrain choices to a Folium map."""
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri, Maxar, Earthstar Geographics, and contributors",
        name="Satellite imagery",
        overlay=False,
        control=True,
        show=True,
    ).add_to(map_object)
    folium.TileLayer(
        tiles="https://services.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
        attr="Esri and contributors",
        name="Street and place labels",
        overlay=True,
        control=True,
        show=True,
    ).add_to(map_object)
    folium.TileLayer(
        tiles="https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
        attr="Map data © OpenStreetMap contributors; map style © OpenTopoMap (CC-BY-SA)",
        name="Topographic terrain",
        overlay=False,
        control=True,
        show=False,
        max_zoom=17,
    ).add_to(map_object)
    folium.TileLayer(
        tiles="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        attr="© OpenStreetMap contributors",
        name="OpenStreetMap",
        overlay=False,
        control=True,
        show=False,
    ).add_to(map_object)
    return map_object


def add_folium_raster_layer(
    map_object: folium.Map,
    layer: xr.DataArray,
    *,
    name: str,
    cmap: str = "viridis",
    opacity: float = 0.65,
    show: bool = False,
    vmin: float | None = None,
    vmax: float | None = None,
    max_size: int = 700,
    nearest: bool = False,
) -> folium.raster_layers.ImageOverlay:
    """Add a bounded, downsampled raster overlay without recalculating the model."""
    data = layer.squeeze(drop=True)
    if set(data.dims) != {"x", "y"} or data.rio.crs is None:
        raise ValueError(f"{name} must be a two-dimensional georeferenced x/y layer")
    data = data.transpose("y", "x").rio.reproject(
        "EPSG:4326", resampling=Resampling.nearest if nearest else Resampling.bilinear
    )
    stride = max(1, int(np.ceil(max(data.sizes.values()) / max_size)))
    if stride > 1:
        data = data.isel(y=slice(None, None, stride), x=slice(None, None, stride))
    values = data.values.astype("float64")
    finite = np.isfinite(values)
    if not finite.any():
        raise ValueError(f"{name} contains no finite pixels")
    finite_values = values[finite]
    lower = float(np.nanquantile(finite_values, 0.02)) if vmin is None else float(vmin)
    upper = float(np.nanquantile(finite_values, 0.98)) if vmax is None else float(vmax)
    if not np.isfinite(lower) or not np.isfinite(upper) or upper <= lower:
        lower, upper = float(np.nanmin(finite_values)), float(np.nanmax(finite_values))
    if upper <= lower:
        upper = lower + 1.0
    scaled = np.clip((values - lower) / (upper - lower), 0, 1)
    rgba = plt.get_cmap(cmap)(scaled)
    rgba[..., 3] = np.where(finite, 1.0, 0.0)
    bounds = [
        [float(data.y.min()), float(data.x.min())],
        [float(data.y.max()), float(data.x.max())],
    ]
    overlay = folium.raster_layers.ImageOverlay(
        image=rgba,
        bounds=bounds,
        name=name,
        opacity=opacity,
        interactive=True,
        cross_origin=False,
        zindex=2,
        show=show,
    )
    overlay.add_to(map_object)
    return overlay


def add_layer_legends(
        map_object: folium.Map,
        legends: dict[str, dict[str, Any]],
) -> folium.Map:
        """Add overlay keys whose visibility follows Folium layer-control events."""
        if not legends:
                return map_object
        map_name = map_object.get_name()
        legend_markup = []
        for index, (name, legend) in enumerate(legends.items()):
                legend_id = f"{map_name}_legend_{index}"
                display = "block" if legend.get("show", False) else "none"
                item_markup = "".join(
                        f'<div class="frp-legend-item"><span class="frp-legend-swatch" '
                        f'style="background:{swatch};"></span><span>{label}</span></div>'
                        for label, swatch in legend.get("items", [])
                )
                legend_markup.append(
                        f'<div id="{legend_id}" class="frp-layer-legend" style="display:{display}">'
                        f'<div class="frp-legend-title">{name}</div>{item_markup}</div>'
                )
        legend_names = json.dumps({
                name: f"{map_name}_legend_{index}" for index, name in enumerate(legends)
        })
        html = f"""
        <style>
            .frp-legends {{ position: fixed; right: 12px; bottom: 24px; z-index: 9999;
                max-width: 260px; max-height: 48vh; overflow-y: auto; font: 12px/1.35 sans-serif; }}
            .frp-layer-legend {{ background: rgba(255,255,255,.94); border: 1px solid #777;
                border-radius: 3px; box-shadow: 0 1px 5px rgba(0,0,0,.25); margin-top: 6px; padding: 8px; }}
            .frp-legend-title {{ font-weight: 700; margin-bottom: 5px; }}
            .frp-legend-item {{ align-items: center; display: flex; gap: 6px; margin: 3px 0; }}
            .frp-legend-swatch {{ border: 1px solid #555; display: inline-block; flex: 0 0 18px;
                height: 12px; opacity: .85; }}
        </style>
        <div id="{map_name}_legends" class="frp-legends">{''.join(legend_markup)}</div>
        <script>
            (function() {{
                var legendIds = {legend_names};
                var map = {map_name};
                function setLegend(name, visible) {{
                    var id = legendIds[name];
                    if (id) {{ document.getElementById(id).style.display = visible ? 'block' : 'none'; }}
                }}
                map.on('overlayadd', function(event) {{ setLegend(event.name, true); }});
                map.on('overlayremove', function(event) {{ setLegend(event.name, false); }});
            }})();
        </script>
        """
        map_object.get_root().html.add_child(Element(html))
        return map_object


def add_inspection_markers(
    map_object: folium.Map, neighborhoods: gpd.GeoDataFrame
) -> folium.Map:
    """Add target labels plus browser links to Google satellite and Street View."""
    points = neighborhoods.to_crs("EPSG:4326")
    for _, row in points.iterrows():
        point = row.geometry if row.geometry.geom_type == "Point" else row.geometry.representative_point()
        lat, lon = float(point.y), float(point.x)
        google_map = (
            "https://www.google.com/maps/@?api=1&map_action=map"
            f"&center={lat:.7f},{lon:.7f}&zoom=17&basemap=satellite"
        )
        street_view = (
            "https://www.google.com/maps/@?api=1&map_action=pano"
            f"&viewpoint={lat:.7f},{lon:.7f}"
        )
        label = str(row.get("name", "Inspection location"))
        zone = str(row.get("zone", ""))
        popup = (
            f"<b>{label}</b><br>{zone}<br>"
            f"<a href='{google_map}' target='_blank'>Google satellite/hybrid</a><br>"
            f"<a href='{street_view}' target='_blank'>Google Street View</a>"
        )
        folium.Marker([lat, lon], tooltip=label, popup=folium.Popup(popup, max_width=320)).add_to(map_object)
    return map_object


def plot_reviewed_hydrography_screening(
    hazard: xr.DataArray,
    candidate_corridor: xr.DataArray,
    rivers: gpd.GeoDataFrame,
    dams: gpd.GeoDataFrame,
    zones: gpd.GeoDataFrame,
) -> plt.Figure:
    """Show the reviewed hydrography and a legible zoom of screening results."""
    if rivers.empty or dams.empty or zones.empty:
        raise ValueError("Rivers, dams, and study zones must be non-empty")
    if any(frame.crs is None for frame in (rivers, dams, zones)):
        raise ValueError("Rivers, dams, and study zones must declare a CRS")
    map_crs = hazard.rio.crs
    if map_crs is None:
        raise ValueError("Hazard layer must declare a CRS")
    rivers_map = rivers.to_crs(map_crs)
    dams_map = dams.to_crs(map_crs)
    zones_map = zones.to_crs(map_crs)

    figure, axes = plt.subplots(1, 2, figsize=(18, 8), constrained_layout=True)
    hazard.plot(ax=axes[0], cmap="Greys", vmin=0, vmax=1, alpha=0.45, add_colorbar=False)
    dams_map.plot(ax=axes[0], color="#38bdf8", edgecolor="#075985", linewidth=0.5, alpha=0.65)
    rivers_map.plot(ax=axes[0], color="#1685d1", linewidth=1.0, alpha=0.90)
    zones_map.boundary.plot(ax=axes[0], color="#20252a", linewidth=0.7, linestyle="--")
    axes[0].set_title(f"Reviewed hydrography used by the model\n{len(rivers_map):,} river/stream lines · {len(dams_map):,} dam/water-body polygons")

    hazard.plot(ax=axes[1], cmap="Greys", vmin=0, vmax=1, alpha=0.45, add_colorbar=False)
    candidate_corridor.where(candidate_corridor == 1).plot(
        ax=axes[1], cmap="Reds", vmin=0, vmax=1, alpha=0.75, add_colorbar=False
    )
    dams_map.boundary.plot(ax=axes[1], color="#075985", linewidth=1.0, alpha=0.80)
    rivers_map.plot(ax=axes[1], color="#1685d1", linewidth=1.2, alpha=0.95)
    zones_map.boundary.plot(ax=axes[1], color="#20252a", linewidth=1.0)
    west, south, east, north = zones_map.total_bounds
    padding = 2500
    axes[1].set_xlim(west - padding, east + padding)
    axes[1].set_ylim(south - padding, north + padding)
    for _, row in zones_map.iterrows():
        label_point = row.geometry.representative_point()
        axes[1].annotate(
            str(row.get("name", "")), (label_point.x, label_point.y),
            xytext=(3, 3), textcoords="offset points", fontsize=7,
            color="#111827", bbox={"facecolor": "white", "alpha": 0.75, "edgecolor": "none", "pad": 1},
        )
    axes[1].set_title("Six study areas: river corridors needing closer review")

    legend = [
        Line2D([0], [0], color="#1685d1", lw=2, label="Reviewed river or stream"),
        Patch(facecolor="#38bdf8", edgecolor="#075985", alpha=0.65, label="Reviewed dam or water body"),
        Patch(facecolor="#d73027", alpha=0.75, label="Candidate corridor"),
        Line2D([0], [0], color="#20252a", lw=1, label="Study-area boundary"),
    ]
    for axis in axes:
        axis.legend(handles=legend, loc="lower left", fontsize=8, framealpha=0.90)
        axis.set_axis_off()
    return figure


def build_final_interactive_map(
    outputs: PCModelOutputs,
    *,
    buildings: gpd.GeoDataFrame | None = None,
    buffer_intervals: gpd.GeoDataFrame | None = None,
    river_corridor: gpd.GeoDataFrame | None = None,
    show_point_layers: bool = True,
) -> folium.Map:
    """Build the final layer-controlled hazard map from already computed outputs."""
    center = outputs.aoi.to_crs("EPSG:4326").geometry.union_all().centroid
    result = folium.Map(location=[center.y, center.x], zoom_start=11, tiles=None)
    add_review_basemaps(result)
    legends: dict[str, dict[str, Any]] = {}
    if "hazard_score" in outputs.layers:
        hazard_name = "Relative flood hazard"
        add_folium_raster_layer(
            result, outputs.layers["hazard_score"], name=hazard_name, cmap="RdYlBu_r",
            opacity=0.72, show=True, vmin=0, vmax=1,
        )
        legends[hazard_name] = {"show": True, "items": [("Low", "#313695"), ("Medium", "#ffffbf"), ("High", "#a50026")]}
    if "planning_priority_score" in outputs.layers:
        priority_name = "Planning priority"
        add_folium_raster_layer(
            result, outputs.layers["planning_priority_score"], name=priority_name,
            cmap="magma", opacity=0.68, show=False, vmin=0, vmax=1,
        )
        legends[priority_name] = {"items": [("Low", "#000004"), ("Medium", "#b73779"), ("High", "#fcffa4")]}
    if "candidate_affected_corridor" in outputs.layers:
        corridor_name = "Candidate affected corridor"
        add_folium_raster_layer(
            result, outputs.layers["candidate_affected_corridor"].where(
                outputs.layers["candidate_affected_corridor"] > 0
            ), name=corridor_name,
            cmap="Reds", opacity=0.70, show=False, vmin=0, vmax=1, nearest=True,
        )
        legends[corridor_name] = {"items": [("Candidate corridor", "#d73027")]}
    if buffer_intervals is not None and not buffer_intervals.empty:
        fields = tuple(column for column in ("inspection_area", "interval_label", "distance_to_m", "area_ha")
                       if column in buffer_intervals.columns)
        intervals = folium_safe_geodataframe(buffer_intervals, fields).to_crs("EPSG:4326")
        interval_name = "River-distance intervals"
        folium.GeoJson(
            intervals, name=interval_name, show=False,
            style_function=lambda feature: {
                "color": RIVER_BUFFER_COLORS.get(float(feature["properties"].get("distance_to_m", 125)), "#6baed6"),
                "weight": 1, "fillOpacity": 0.18,
            },
            tooltip=folium.GeoJsonTooltip(fields=list(fields)) if fields else None,
        ).add_to(result)
        legends[interval_name] = {
            "items": [
                (f"{lower:g}-{upper:g} m", color)
                for lower, (upper, color) in zip((0.0, *tuple(RIVER_BUFFER_COLORS)[:-1]), RIVER_BUFFER_COLORS.items())
            ]
        }
    if buildings is not None and not buildings.empty:
        buildings_layer = folium_safe_geodataframe(buildings).to_crs("EPSG:4326")
        buildings_name = f"OSM buildings ({len(buildings):,})"
        folium.GeoJson(
            buildings_layer, name=buildings_name, show=False,
            style_function=lambda _: {"color": "#ff0000", "weight": 1.0, "fillColor": "#ffb3b3", "fillOpacity": 0.20},
        ).add_to(result)
        legends[buildings_name] = {"items": [("Building footprint", "#ff0000")]}
    if outputs.mapped_dams is not None and not outputs.mapped_dams.empty:
        fields = tuple(
            column for column in ("display_name", "waterbody_type")
            if column in outputs.mapped_dams.columns
        )
        dams = folium_safe_geodataframe(outputs.mapped_dams, fields).to_crs("EPSG:4326")
        dams_name = "Reviewed dams and water bodies"
        folium.GeoJson(
            dams, name=dams_name, show=True,
            style_function=lambda _: {
                "color": "#075985", "weight": 1.5,
                "fillColor": "#38bdf8", "fillOpacity": 0.40,
            },
            tooltip=folium.GeoJsonTooltip(fields=list(fields)) if fields else None,
        ).add_to(result)
        legends[dams_name] = {
            "show": True, "items": [("Dam or mapped water body", "#38bdf8")]
        }
    if outputs.drainage_reaches is not None and not outputs.drainage_reaches.empty:
        fields = tuple(column for column in ("display_name", "waterway")
                       if column in outputs.drainage_reaches.columns)
        reaches = folium_safe_geodataframe(outputs.drainage_reaches, fields).to_crs("EPSG:4326")
        drainage_name = "Mapped drainage"
        folium.GeoJson(
            reaches, name=drainage_name, show=True,
            style_function=lambda _: {"color": "#1685d1", "weight": 4.0, "opacity": 0.95},
            tooltip=folium.GeoJsonTooltip(fields=list(fields)) if fields else None,
        ).add_to(result)
        legends[drainage_name] = {"show": True, "items": [("Mapped drainage", "#1685d1")]}
    if river_corridor is not None and not river_corridor.empty:
        corridor = folium_safe_geodataframe(
            river_corridor,
            tuple(column for column in ("buffer_m_each_side",) if column in river_corridor.columns),
        ).to_crs("EPSG:4326")
        corridor_name = "River/stream corridor polygon"
        folium.GeoJson(
            corridor, name=corridor_name, show=True,
            style_function=lambda _: {
                "color": "#1685d1", "weight": 2.0,
                "fillColor": "#1685d1", "fillOpacity": 0.10,
            },
        ).add_to(result)
        legends[corridor_name] = {
            "show": True, "items": [("River/stream corridor", "#1685d1")]
        }
    if outputs.terrain_catchments is not None and not outputs.terrain_catchments.empty:
        fields = tuple(column for column in ("name", "area_km2", "boundary_truncated")
                       if column in outputs.terrain_catchments.columns)
        catchments = folium_safe_geodataframe(outputs.terrain_catchments, fields).to_crs("EPSG:4326")
        catchment_name = "South C contributing catchment"
        folium.GeoJson(
            catchments, name=catchment_name, show=True,
            style_function=lambda _: {
                "color": "#d100d1", "weight": 3.0,
                "fillColor": "#d100d1", "fillOpacity": 0.08,
            },
            tooltip=folium.GeoJsonTooltip(fields=list(fields)) if fields else None,
        ).add_to(result)
        legends[catchment_name] = {
            "show": True, "items": [("Area draining through South C", "#d100d1")]
        }
    if show_point_layers and outputs.terrain_pour_points is not None and not outputs.terrain_pour_points.empty:
        for _, row in outputs.terrain_pour_points.to_crs("EPSG:4326").iterrows():
            point = row.geometry
            details = (
                f"<b>South C water outlet</b><br>"
                f"Upstream area: {float(row.get('upstream_area_km2', float('nan'))):.2f} km²<br>"
                f"Distance from South C marker: {float(row.get('snap_distance_m', float('nan'))):.0f} m"
            )
            folium.CircleMarker(
                [point.y, point.x], radius=7, color="#003f5c", fill=True,
                fill_color="#00ffff", fill_opacity=1, weight=2,
                tooltip="South C water outlet", popup=folium.Popup(details, max_width=300),
            ).add_to(result)
    if show_point_layers and outputs.reference_points is not None and not outputs.reference_points.empty:
        for _, row in outputs.reference_points.to_crs("EPSG:4326").iterrows():
            point = row.geometry
            label = str(row.get("name", "Reference point"))
            folium.CircleMarker(
                [point.y, point.x], radius=7, color="#4d3b00", fill=True,
                fill_color="#ffd700", fill_opacity=1, weight=2,
                tooltip=label, popup=label,
            ).add_to(result)
    zones = folium_safe_geodataframe(
        outputs.neighborhood_zones,
        tuple(column for column in ("name", "zone") if column in outputs.neighborhood_zones.columns),
    ).to_crs("EPSG:4326")
    zones_name = "Inspection zones"
    folium.GeoJson(
        zones, name=zones_name, show=True,
        style_function=lambda _: {"color": "black", "weight": 1.5, "fillOpacity": 0.01},
    ).add_to(result)
    legends[zones_name] = {"show": True, "items": [("Inspection zone", "#000000")]}
    if show_point_layers:
        add_inspection_markers(result, outputs.neighborhoods)
    bounds = outputs.aoi.to_crs("EPSG:4326").total_bounds
    result.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])
    folium.LayerControl(collapsed=False).add_to(result)
    add_layer_legends(result, legends)
    return result


def export_interactive_map(
    map_object: folium.Map,
    output_path: str | Path = "exports/nairobi_flood_interactive_map.html",
) -> Path:
    """Save a standalone interactive result map and return its path."""
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    map_object.save(str(destination))
    return destination


def _markdown_table(frame: pd.DataFrame, max_rows: int = 30) -> str:
    """Render a compact Markdown table without requiring the tabulate package."""
    table = frame.copy()
    if not isinstance(table.index, pd.RangeIndex):
        index_name = table.index.name or "index"
        table = table.reset_index(names=index_name)
    if len(table) > max_rows:
        table = table.head(max_rows)

    def render(value: Any) -> str:
        if value is None or (not isinstance(value, (list, tuple, dict)) and pd.isna(value)):
            return "—"
        if isinstance(value, (float, np.floating)):
            return f"{float(value):.3f}"
        text = str(value).replace("|", "\\|").replace("\n", " ")
        return text[:160] + ("…" if len(text) > 160 else "")

    columns = [str(column) for column in table.columns]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    lines.extend(
        "| " + " | ".join(render(value) for value in row) + " |"
        for row in table.itertuples(index=False, name=None)
    )
    if len(frame) > max_rows:
        lines.append(f"\n*Preview limited to {max_rows} of {len(frame):,} rows; use the CSV attachment for the full table.*")
    return "\n".join(lines)


def export_analysis_report(
    tables: dict[str, pd.DataFrame],
    config: PCModelConfig,
    *,
    template_path: str | Path = "Nairobi_Flood_Analysis_Report.md",
    output_path: str | Path = "exports/Nairobi_Flood_Analysis_Report.md",
) -> Path:
    """Generate a run-specific Markdown report with live tables and asset status."""
    template = Path(template_path).read_text(encoding="utf-8")
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    table_titles = {
        "table_01_source_inventory.csv": "Source inventory",
        "table_02_raw_input_statistics.csv": "Raw-input statistics",
        "table_03_derived_layer_statistics.csv": "Derived-layer statistics",
        "table_04_normalized_predictor_statistics.csv": "Normalized-predictor statistics",
        "table_05_predictor_correlation.csv": "Predictor correlation",
        "table_06_model_weights.csv": "Model weights",
        "table_07_drainage_corridor_summary.csv": "Drainage-corridor summary",
        "table_08_weight_sensitivity.csv": "Weight sensitivity",
        "table_09_neighborhood_summary.csv": "Neighborhood summary",
        "table_10_validation_metrics.csv": "Independent validation metrics",
        "table_11_incident_scores.csv": "Incident-point scores",
        "table_12_river_interval_summary.csv": "River-distance interval summary",
    }
    generated = [
        "## Automated results from this run",
        "",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}  ",
        f"**Pipeline:** `{PIPELINE_VERSION}`  ",
        f"**CRS:** `{config.crs}`  ",
        f"**Analysis/reporting resolution:** {config.resolution_m} m / {config.reporting_resolution_m} m",
        "",
        "The tables below were inserted automatically from the same in-memory objects used for export. Full CSV attachments retain all rows and columns.",
    ]
    for filename, frame in tables.items():
        generated.extend([
            "",
            f"### {table_titles.get(filename, Path(filename).stem.replace('_', ' ').title())}",
            "",
            _markdown_table(frame),
            "",
            f"[Download the complete table](report_assets/{filename})",
        ])

    expected_assets = (
        ("Stage 1 input-review map", "report_assets/stage_01_input_review_map.html"),
        ("Final interactive map", "nairobi_flood_interactive_map.html"),
        ("GeoTIFF model raster", "nairobi_flood_screening.tif"),
        ("Neighborhood summary CSV", "nairobi_neighborhood_summary.csv"),
        ("Source inventory CSV", "source_inventory.csv"),
        ("Run metadata JSON", "run_metadata.json"),
        ("Reviewed river/stream GeoJSON", "mapped_drainage_reaches.geojson"),
        ("Reviewed dams/water bodies GeoJSON", "mapped_dams.geojson"),
        ("Drainage corridor summary CSV", "drainage_corridor_summary.csv"),
        ("Raw-input figure", "report_assets/figure_01_raw_inputs.png"),
        ("Hydrology figure", "report_assets/figure_02_hydrology.png"),
        ("Predictor-correlation figure", "report_assets/figure_03_predictor_correlation.png"),
        ("Normalized-predictor figure", "report_assets/figure_04_normalized_predictors.png"),
        ("Hazard and priority figure", "report_assets/figure_05_hazard_and_priority.png"),
        ("Candidate-corridor figure", "report_assets/figure_06_candidate_corridors.png"),
        ("Weight-sensitivity figure", "report_assets/figure_07_weight_sensitivity.png"),
        ("Optional Sentinel-1 event maps", "river_corridor/river_buffer_event_maps.png"),
        ("Optional affected-building charts", "river_corridor/river_buffer_building_charts.png"),
    )
    generated.extend(["", "### Attachment availability", "", "| Attachment | Status | Link |", "|---|---|---|"])
    for label, relative in expected_assets:
        exists = (destination.parent / relative).exists()
        status = "Available" if exists else "Not generated in this run"
        generated.append(f"| {label} | {status} | [Open]({relative}) |")

    marker = "{{AUTOMATED_RESULTS}}"
    if marker not in template:
        raise ValueError(f"Report template is missing required marker: {marker}")
    report = template.replace(marker, "\n".join(generated), 1)
    # The template lives one directory above the generated report. Convert its
    # root-relative project links to paths relative to exports/.
    report = report.replace("(exports/report_assets/", "(report_assets/")
    report = report.replace("(exports/river_corridor/", "(river_corridor/")
    report = report.replace("(exports/nairobi_", "(nairobi_")
    report = report.replace("(raw_data/", "(../raw_data/")
    destination.write_text(report, encoding="utf-8")
    return destination


def aoi_review_table(aoi: gpd.GeoDataFrame, config: PCModelConfig) -> pd.DataFrame:
    west, south, east, north = _aoi_wgs84(config).total_bounds
    pwest, psouth, peast, pnorth = processing_bbox(config)
    return pd.DataFrame(
        [
            {
                "name": aoi.iloc[0]["name"],
                "west": west,
                "south": south,
                "east": east,
                "north": north,
                "crs": config.crs,
                "grid_resolution_m": config.resolution_m,
                "reporting_resolution_m": config.reporting_resolution_m,
                "hydrology_buffer_m": config.hydrology_buffer_m,
                "processing_bbox": (pwest, psouth, peast, pnorth),
                "area_km2": float(aoi.geometry.area.iloc[0] / 1_000_000),
            }
        ]
    )


def _search(
    catalog: pystac_client.Client,
    collection: str,
    bbox: tuple[float, float, float, float],
    datetime: str | None = None,
) -> list[Any]:
    items = list(catalog.search(collections=[collection], bbox=bbox, datetime=datetime).item_collection())
    if not items:
        raise RuntimeError(f"No {collection!r} items intersect bbox={bbox}, datetime={datetime!r}")
    return items


def discover_sources(
    config: PCModelConfig,
    catalog: pystac_client.Client | None = None,
) -> PCSourceBundle:
    """Stage 2: find source items and return a reviewable inventory table."""
    config.validate()
    catalog = catalog or open_catalog()
    search_bbox = processing_bbox(config)
    retrieved = datetime.now(timezone.utc).isoformat()
    dem_items = _search(catalog, "cop-dem-glo-30", search_bbox)
    worldcover_items = _search(catalog, "esa-worldcover", search_bbox, "2021-01-01/2021-12-31")
    water_items = _search(catalog, "jrc-gsw", search_bbox)
    rows = [
        {"collection": "cop-dem-glo-30", "asset": "data", "period": "2021", "items": len(dem_items),
         "item_ids": ";".join(item.id for item in dem_items)},
        {"collection": "esa-worldcover", "asset": "map", "period": "2021", "items": len(worldcover_items),
         "item_ids": ";".join(item.id for item in worldcover_items)},
        {"collection": "jrc-gsw", "asset": "occurrence", "period": "1984-2020", "items": len(water_items),
         "item_ids": ";".join(item.id for item in water_items)},
        {"collection": "terraclimate", "asset": "ppt", "period": f"{config.rainfall_start}/{config.rainfall_end}",
         "items": "Zarr", "item_ids": "collection-level Zarr"},
    ]
    for row in rows:
        collection = catalog.get_collection(row["collection"])
        row["collection_version"] = collection.extra_fields.get("version", "not declared")
        row["license"] = collection.license
        row["providers"] = ";".join(
            provider.name for provider in (collection.providers or [])
        )
    inventory = pd.DataFrame(rows)
    inventory["retrieved_utc"] = retrieved
    inventory["search_bbox_wgs84"] = str(search_bbox)
    return PCSourceBundle(catalog, dem_items, worldcover_items, water_items, inventory)


def _mosaic(
    items: list[Any],
    band: str,
    config: PCModelConfig,
    *,
    geobox: Any | None = None,
    resampling: str = "bilinear",
) -> xr.DataArray:
    kwargs: dict[str, Any] = {
        "bands": [band], "resampling": resampling, "chunks": {}, "fail_on_error": True,
    }
    if geobox is None:
        kwargs.update(bbox=processing_bbox(config), crs=config.crs, resolution=config.resolution_m)
    else:
        kwargs["geobox"] = geobox
    loaded = odc.stac.load(items, **kwargs)[band]
    if "time" in loaded.dims:
        loaded = loaded.max("time", skipna=True)
    return loaded.astype("float32")


def _coordinate_slice(coordinate: xr.DataArray, low: float, high: float) -> slice:
    first = float(coordinate.isel({coordinate.dims[0]: 0}))
    last = float(coordinate.isel({coordinate.dims[0]: -1}))
    return slice(low, high) if first < last else slice(high, low)


def _load_rainfall(
    catalog: pystac_client.Client,
    template: xr.DataArray,
    config: PCModelConfig,
) -> xr.DataArray:
    asset = catalog.get_collection("terraclimate").assets["zarr-abfs"]
    open_kwargs = dict(asset.extra_fields.get("xarray:open_kwargs", {}))
    open_kwargs.pop("engine", None)
    storage_options = open_kwargs.pop("storage_options", {})
    ds = xr.open_zarr(asset.href, storage_options=storage_options, **open_kwargs)
    search_bbox = processing_bbox(config)
    lon_bounds = (search_bbox[0] - 0.1, search_bbox[2] + 0.1)
    lat_bounds = (search_bbox[1] - 0.1, search_bbox[3] + 0.1)
    rain = ds["ppt"].sel(
        time=slice(config.rainfall_start, config.rainfall_end),
        lon=_coordinate_slice(ds.lon, *lon_bounds),
        lat=_coordinate_slice(ds.lat, *lat_bounds),
    )
    if rain.sizes.get("lat", 0) == 0 or rain.sizes.get("lon", 0) == 0:
        raise RuntimeError("TerraClimate spatial selection is empty; review bbox and coordinate order")
    annual_max = rain.resample(time="YS").max().mean("time").compute()
    annual_max = annual_max.rio.set_spatial_dims(x_dim="lon", y_dim="lat").rio.write_crs("EPSG:4326")
    return annual_max.rio.reproject_match(template, resampling=Resampling.bilinear).rename(
        "extreme_rainfall_raw"
    ).astype("float32").assign_attrs(
        source="TerraClimate ppt",
        statistic="mean of annual maximum monthly totals",
        native_resolution="approximately 4.6 km; reprojection does not add 30 m detail",
        role="temporary climatology; replace with event rainfall or local IDF data",
    )


def load_event_rainfall_raster(path: str | Path, template: xr.DataArray) -> xr.DataArray:
    """Load a prepared event/IDF rainfall raster and align it to the model grid."""
    source = rioxarray.open_rasterio(path, masked=True).squeeze(drop=True)
    if source.rio.crs is None:
        raise ValueError("Event rainfall raster must declare a CRS")
    aligned = source.rio.reproject_match(template, resampling=Resampling.bilinear)
    return aligned.where(aligned >= 0).rename("extreme_rainfall_raw").astype("float32").assign_attrs(
        source_path=str(path),
        expected_units="millimetres for one documented duration/return period",
        warning="Record the event duration, dates, product version, and native resolution",
    )


def load_raw_layers(sources: PCSourceBundle, config: PCModelConfig) -> xr.Dataset:
    """Stage 3: load and align the four raw source layers."""
    elevation = _mosaic(sources.dem_items, "data", config, resampling="bilinear").rename("elevation")
    geobox = elevation.odc.geobox
    worldcover = _mosaic(
        sources.worldcover_items, "map", config, geobox=geobox, resampling="nearest"
    ).rename("worldcover")
    water = _mosaic(
        sources.water_items, "occurrence", config, geobox=geobox, resampling="nearest"
    ).rename("water_occurrence")
    rainfall = _load_rainfall(sources.catalog, elevation, config)
    # All layers were created with the DEM geobox or reproject_match. Their
    # scalar spatial_ref coordinates can still carry different-but-equivalent
    # WKT/metadata attributes, which Dataset(...) treats as a conflict.
    # join='exact' protects the actual x/y grid while compat='override' accepts
    # the DEM's canonical spatial_ref metadata.
    raw = xr.merge(
        [elevation, worldcover, water, rainfall],
        join="exact",
        compat="override",
    )
    expected_shape = elevation.shape
    if any(raw[name].shape != expected_shape for name in raw.data_vars):
        raise RuntimeError(
            f"Raw layers are not aligned to the DEM grid {expected_shape}: "
            + ", ".join(f"{name}={raw[name].shape}" for name in raw.data_vars)
        )
    raw.attrs.update(stage="raw aligned inputs", crs=config.crs, resolution_m=config.resolution_m)
    return raw


def _as_layer(values: np.ndarray, template: xr.DataArray, name: str) -> xr.DataArray:
    result = xr.DataArray(values.astype("float32"), coords=template.coords, dims=template.dims, name=name)
    result = result.rio.write_crs(template.rio.crs)
    result.rio.write_transform(template.rio.transform(), inplace=True)
    return result


def _robust_scale(layer: xr.DataArray, config: PCModelConfig, reverse: bool = False) -> xr.DataArray:
    values = layer.values.astype("float64")
    finite = np.isfinite(values)
    if not finite.any():
        raise ValueError(f"Layer {layer.name!r} contains no finite data")
    low, high = np.nanpercentile(values, [config.percentile_low, config.percentile_high])
    scaled = np.zeros_like(values, dtype="float32") if high <= low else np.clip((values - low) / (high - low), 0, 1)
    if reverse:
        scaled = 1 - scaled
    scaled[~finite] = np.nan
    return _as_layer(scaled, layer, layer.name or "scaled")


def _odd_window(distance_m: int, resolution_m: int) -> int:
    cells = max(1, round(distance_m / resolution_m))
    return cells if cells % 2 else cells + 1


def _worldcover_runoff(worldcover: xr.DataArray) -> tuple[xr.DataArray, xr.DataArray]:
    values = worldcover.values
    lookup = {10: 0.30, 20: 0.40, 30: 0.35, 40: 0.50, 50: 0.95, 60: 0.70,
              70: 0.20, 80: 1.00, 90: 0.80, 95: 0.80, 100: 0.30}
    runoff = np.full(values.shape, np.nan, dtype="float32")
    for code, coefficient in lookup.items():
        runoff[values == code] = coefficient
    known = np.isin(values, list(lookup))
    built = np.where(known, values == 50, np.nan).astype("float32")
    return _as_layer(runoff, worldcover, "runoff_potential"), _as_layer(built, worldcover, "built_up")


def load_curve_number_raster(path: str | Path, template: xr.DataArray) -> tuple[xr.DataArray, xr.DataArray]:
    """Load a reviewed SCS Curve Number raster and return CN plus 0..1 runoff potential."""
    source = rioxarray.open_rasterio(path, masked=True).squeeze(drop=True)
    if source.rio.crs is None:
        raise ValueError("Curve Number raster must declare a CRS")
    curve_number = source.rio.reproject_match(template, resampling=Resampling.nearest).where(
        lambda layer: (layer >= 30) & (layer <= 100)
    ).rename("curve_number")
    runoff = ((curve_number - 30) / 70).clip(0, 1).rename("runoff_potential")
    curve_number.attrs.update(source_path=str(path), units="dimensionless SCS Curve Number")
    runoff.attrs.update(source="reviewed Curve Number raster", source_path=str(path))
    return curve_number.astype("float32"), runoff.astype("float32")


_D8 = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def _priority_flood_route(values: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """Condition a DEM and create an acyclic eight-neighbour drainage tree.

    The routing parent recorded by Priority-Flood resolves flats created by
    filling, avoiding loops without adding an arbitrary epsilon gradient.
    """
    rows, cols = values.shape
    conditioned = values.astype("float64", copy=True)
    receiver = np.full(values.size, -1, dtype="int64")
    visited = ~valid.copy()
    heap: list[tuple[float, int]] = []
    order: list[int] = []

    boundary = set()
    boundary.update((0, col) for col in range(cols))
    boundary.update((rows - 1, col) for col in range(cols))
    boundary.update((row, 0) for row in range(rows))
    boundary.update((row, cols - 1) for row in range(rows))
    for row, col in boundary:
        if valid[row, col] and not visited[row, col]:
            visited[row, col] = True
            heapq.heappush(heap, (conditioned[row, col], row * cols + col))

    if not heap:
        raise ValueError("DEM has no valid cell connected to the raster boundary")

    while heap:
        spill, flat_index = heapq.heappop(heap)
        order.append(flat_index)
        row, col = divmod(flat_index, cols)
        for drow, dcol in _D8:
            nrow, ncol = row + drow, col + dcol
            if not (0 <= nrow < rows and 0 <= ncol < cols) or visited[nrow, ncol]:
                continue
            visited[nrow, ncol] = True
            next_index = nrow * cols + ncol
            receiver[next_index] = flat_index
            conditioned[nrow, ncol] = max(conditioned[nrow, ncol], spill)
            heapq.heappush(heap, (conditioned[nrow, ncol], next_index))

    if np.any(valid & ~visited):
        raise ValueError("DEM contains valid cells disconnected from its boundary")
    conditioned[~valid] = np.nan
    return conditioned, receiver, order


def _d8_receivers(
    conditioned: np.ndarray,
    priority_parent: np.ndarray,
    valid: np.ndarray,
    resolution_m: float,
) -> np.ndarray:
    """Route to steepest D8 downslope neighbor, using Priority-Flood parents on flats."""
    rows, cols = conditioned.shape
    indices = np.arange(rows * cols, dtype="int64").reshape(rows, cols)
    receiver = priority_parent.reshape(rows, cols).copy()
    best_gradient = np.zeros((rows, cols), dtype="float64")
    for drow, dcol in _D8:
        source_rows = slice(max(0, -drow), min(rows, rows - drow))
        source_cols = slice(max(0, -dcol), min(cols, cols - dcol))
        neighbor_rows = slice(max(0, drow), min(rows, rows + drow))
        neighbor_cols = slice(max(0, dcol), min(cols, cols + dcol))
        source = conditioned[source_rows, source_cols]
        neighbor = conditioned[neighbor_rows, neighbor_cols]
        distance = resolution_m * (2 ** 0.5 if drow and dcol else 1.0)
        gradient = (source - neighbor) / distance
        source_valid = valid[source_rows, source_cols]
        neighbor_valid = valid[neighbor_rows, neighbor_cols]
        current_best = best_gradient[source_rows, source_cols]
        improve = source_valid & neighbor_valid & (gradient > current_best)
        current_best[improve] = gradient[improve]
        receiver_view = receiver[source_rows, source_cols]
        neighbor_indices = indices[neighbor_rows, neighbor_cols]
        receiver_view[improve] = neighbor_indices[improve]
    receiver[~valid] = -1
    return receiver.ravel()


def _flow_accumulation(receiver: np.ndarray, order: list[int], valid: np.ndarray) -> np.ndarray:
    accumulation = np.where(valid.ravel(), 1.0, 0.0)
    for index in reversed(order):
        downstream = receiver[index]
        if downstream >= 0:
            accumulation[downstream] += accumulation[index]
    result = accumulation.reshape(valid.shape)
    result[~valid] = np.nan
    return result


def _height_above_drainage(
    elevation: np.ndarray,
    conditioned: np.ndarray,
    receiver: np.ndarray,
    order: list[int],
    stream: np.ndarray,
    valid: np.ndarray,
) -> np.ndarray:
    drainage_elevation = np.full(elevation.size, np.nan, dtype="float64")
    flat_conditioned = conditioned.ravel()
    flat_stream = stream.ravel()
    for index in order:
        downstream = receiver[index]
        if flat_stream[index] or downstream < 0 or not np.isfinite(drainage_elevation[downstream]):
            drainage_elevation[index] = flat_conditioned[index]
        else:
            drainage_elevation[index] = drainage_elevation[downstream]
    hand = np.maximum(elevation.ravel() - drainage_elevation, 0).reshape(elevation.shape)
    hand[~valid] = np.nan
    return hand


def derive_predictors(
    raw: xr.Dataset,
    config: PCModelConfig,
    *,
    mapped_drainage: gpd.GeoDataFrame | None = None,
    mapped_dams: gpd.GeoDataFrame | None = None,
) -> xr.Dataset:
    """Condition terrain and derive predictors using reviewed rivers and dams."""
    dem = raw["elevation"]
    values = dem.values.astype("float64")
    valid = np.isfinite(values)
    if not valid.any():
        raise ValueError("Elevation contains no finite data")
    conditioned, priority_parent, order = _priority_flood_route(values, valid)
    receiver = _d8_receivers(conditioned, priority_parent, valid, config.resolution_m)
    calculation_dem = np.where(valid, conditioned, float(np.nanmedian(conditioned)))
    grad_y, grad_x = np.gradient(calculation_dem, config.resolution_m, config.resolution_m)
    slope_values = np.degrees(np.arctan(np.hypot(grad_x, grad_y)))
    slope_values[~valid] = np.nan
    slope = _as_layer(slope_values, dem, "slope")

    depression_values = np.maximum(conditioned - values, 0)
    depression_values[~valid] = np.nan
    depression = _as_layer(depression_values, dem, "depression_storage_raw")

    accumulation_cells = _flow_accumulation(receiver, order, valid)
    accumulation_km2 = accumulation_cells * (config.resolution_m ** 2) / 1_000_000
    stream = valid & (accumulation_km2 >= config.stream_threshold_km2)

    def rasterized_mask(features: gpd.GeoDataFrame | None, label: str) -> np.ndarray:
        mask = np.zeros_like(valid)
        if features is None or features.empty:
            return mask
        if features.crs is None:
            raise ValueError(f"{label} must declare a CRS")
        projected = features.to_crs(dem.rio.crs)
        feature_shapes = [
            (geometry, 1) for geometry in projected.geometry
            if geometry is not None and not geometry.is_empty
        ]
        if feature_shapes:
            mask = rasterize(
                feature_shapes, out_shape=dem.shape, transform=dem.rio.transform(),
                fill=0, dtype="uint8",
            ).astype(bool)
        return mask

    mapped = rasterized_mask(mapped_drainage, "Mapped rivers")
    dam_mask = rasterized_mask(mapped_dams, "Mapped dams")
    reference_drainage = stream | mapped | dam_mask
    hand_values = _height_above_drainage(
        values, conditioned, receiver, order, reference_drainage, valid
    )
    slope_tangent = np.tan(np.deg2rad(np.maximum(slope_values, 0.1)))
    specific_area_m = accumulation_cells * config.resolution_m
    twi_values = np.log(np.maximum(specific_area_m, 1.0) / slope_tangent)
    twi_values[~valid] = np.nan

    observed_water = np.isfinite(raw.water_occurrence.values) & (raw.water_occurrence.values > 0)
    drainage = observed_water | reference_drainage
    distance_values = (distance_transform_edt(~drainage, sampling=config.resolution_m)
                       if drainage.any() else np.full_like(values, np.nan))
    proximity_values = np.clip(1 - distance_values / config.water_influence_m, 0, 1)
    proximity_values[~valid] = np.nan

    runoff, built_up = _worldcover_runoff(raw.worldcover)
    built_window = _odd_window(config.built_density_window_m, config.resolution_m)
    density_values = uniform_filter(np.nan_to_num(built_up.values), size=built_window, mode="nearest")
    density_values[~valid] = np.nan
    built_density = _as_layer(density_values, dem, "built_density")

    derived = xr.Dataset(
        {
            "conditioned_elevation": _as_layer(conditioned, dem, "conditioned_elevation"),
            "slope": slope,
            "depression_storage_raw": depression,
            "flow_accumulation_cells": _as_layer(accumulation_cells, dem, "flow_accumulation_cells"),
            "flow_accumulation_km2": _as_layer(accumulation_km2, dem, "flow_accumulation_km2"),
            "modeled_stream": _as_layer(stream.astype("float32"), dem, "modeled_stream"),
            "mapped_drainage": _as_layer(mapped.astype("float32"), dem, "mapped_drainage"),
            "mapped_dams": _as_layer(dam_mask.astype("float32"), dem, "mapped_dams"),
            "distance_to_drainage_m": _as_layer(distance_values, dem, "distance_to_drainage_m"),
            "drainage_proximity": _as_layer(proximity_values, dem, "drainage_proximity"),
            "topographic_wetness_raw": _as_layer(twi_values, dem, "topographic_wetness_raw"),
            "hand_m": _as_layer(hand_values, dem, "hand_m"),
            "runoff_potential": runoff,
            "built_up": built_up,
            "built_density": built_density,
            "terrain_valid": _as_layer(valid.astype("float32"), dem, "terrain_valid"),
        }
    )
    derived.attrs.update(
        stage="conditioned terrain, routed hydrology, and exposure proxies",
        routing="Priority-Flood conditioning with D8 steepest descent and flat resolution",
        stream_threshold_km2=config.stream_threshold_km2,
        reviewed_river_features=0 if mapped_drainage is None else len(mapped_drainage),
        reviewed_dam_features=0 if mapped_dams is None else len(mapped_dams),
        drainage_proximity_basis="observed water, modelled streams, reviewed rivers, and reviewed dam/water-body polygons",
    )
    return derived


def delineate_terrain_catchment(
    raw: xr.Dataset,
    locations: gpd.GeoDataFrame,
    config: PCModelConfig,
    *,
    location_name: str = "South C",
    snap_radius_m: float | None = None,
    scored: xr.Dataset | None = None,
) -> TerrainCatchmentOutputs:
    """Delineate the terrain catchment draining to a location's local outlet.

    The named point is snapped to the largest DEM-derived flow-accumulation cell
    within ``snap_radius_m``. This is useful where no mapped river is present:
    the outlet is selected from routed terrain, not from a hydrography layer.
    The result is a structural hydrology check, not independent flood validation.
    """
    if "elevation" not in raw:
        raise ValueError("raw must contain an elevation layer")
    if locations.empty or "name" not in locations:
        raise ValueError("locations must contain named point features")
    if locations.crs is None:
        raise ValueError("locations must declare a CRS")
    selected = locations.loc[locations["name"].astype(str).str.casefold()
                             == location_name.casefold()]
    if len(selected) != 1:
        raise ValueError(f"Expected one location named {location_name!r}, found {len(selected)}")

    dem = raw["elevation"]
    if dem.rio.crs is None:
        raise ValueError("elevation must declare a CRS")
    point = selected.to_crs(dem.rio.crs).geometry.iloc[0]
    if point is None or point.is_empty or point.geom_type != "Point":
        raise ValueError(f"{location_name!r} must have a point geometry")

    values = dem.values.astype("float64")
    valid = np.isfinite(values)
    conditioned, priority_parent, order = _priority_flood_route(values, valid)
    receiver = _d8_receivers(conditioned, priority_parent, valid, config.resolution_m)
    accumulation_cells = _flow_accumulation(receiver, order, valid)

    radius = float(config.neighborhood_radius_m if snap_radius_m is None else snap_radius_m)
    if radius <= 0:
        raise ValueError("snap_radius_m must be positive")
    x_coords, y_coords = np.meshgrid(dem.x.values, dem.y.values)
    distance_sq = (x_coords - point.x) ** 2 + (y_coords - point.y) ** 2
    candidates = valid & (distance_sq <= radius ** 2)
    # A boundary outlet would imply an unknown upstream area outside the DEM.
    candidates[[0, -1], :] = False
    candidates[:, [0, -1]] = False
    if not candidates.any():
        raise ValueError(f"No valid DEM cell lies within {radius:g} m of {location_name}")
    best_accumulation = np.nanmax(np.where(candidates, accumulation_cells, np.nan))
    tied = candidates & np.isclose(accumulation_cells, best_accumulation)
    outlet_flat = int(np.nanargmin(np.where(tied, distance_sq, np.nan)))
    outlet_row, outlet_col = np.unravel_index(outlet_flat, valid.shape)

    catchment_flat = np.zeros(valid.size, dtype=bool)
    catchment_flat[outlet_flat] = True
    # Priority-Flood ordering places every receiver before its contributors.
    for index in order:
        downstream = receiver[index]
        if downstream >= 0 and catchment_flat[downstream]:
            catchment_flat[index] = True
    catchment_mask = catchment_flat.reshape(valid.shape) & valid
    if not catchment_mask[outlet_row, outlet_col]:
        raise RuntimeError("Catchment delineation did not retain its outlet")

    transform = dem.rio.transform()
    polygons = [shape(geometry) for geometry, value in shapes(
        catchment_mask.astype("uint8"), mask=catchment_mask, transform=transform
    ) if value == 1]
    catchment_geometry = gpd.GeoSeries(polygons, crs=dem.rio.crs).union_all()
    outlet_x, outlet_y = transform * (outlet_col + 0.5, outlet_row + 0.5)
    outlet_point = Point(outlet_x, outlet_y)
    snap_distance = float(point.distance(outlet_point))
    cell_area_m2 = abs(transform.a * transform.e - transform.b * transform.d)
    catchment_area_km2 = float(catchment_mask.sum() * cell_area_m2 / 1_000_000)
    touches_boundary = bool(
        catchment_mask[0, :].any() or catchment_mask[-1, :].any()
        or catchment_mask[:, 0].any() or catchment_mask[:, -1].any()
    )

    catchment = gpd.GeoDataFrame([{
        "name": location_name,
        "method": "Priority-Flood D8; outlet snapped to maximum accumulation",
        "area_km2": catchment_area_km2,
        "boundary_truncated": touches_boundary,
    }], geometry=[catchment_geometry], crs=dem.rio.crs)
    pour_point = gpd.GeoDataFrame([{
        "name": location_name,
        "snap_distance_m": snap_distance,
        "snap_radius_m": radius,
        "upstream_area_km2": float(best_accumulation * cell_area_m2 / 1_000_000),
    }], geometry=[outlet_point], crs=dem.rio.crs)

    record: dict[str, Any] = {
        "name": location_name,
        "catchment_area_km2": catchment_area_km2,
        "pour_point_upstream_area_km2": float(best_accumulation * cell_area_m2 / 1_000_000),
        "snap_distance_m": snap_distance,
        "snap_radius_m": radius,
        "snap_near_search_limit": bool(snap_distance >= 0.95 * radius),
        "min_elevation_m": float(np.nanmin(values[catchment_mask])),
        "mean_elevation_m": float(np.nanmean(values[catchment_mask])),
        "max_elevation_m": float(np.nanmax(values[catchment_mask])),
        "boundary_truncated": touches_boundary,
        "interpretation": "terrain consistency check; not independent flood validation",
    }
    layers = xr.Dataset({
        "catchment_mask": _as_layer(catchment_mask.astype("float32"), dem, "catchment_mask"),
        "catchment_flow_accumulation_km2": _as_layer(
            np.where(catchment_mask, accumulation_cells * cell_area_m2 / 1_000_000, np.nan),
            dem,
            "catchment_flow_accumulation_km2",
        ),
    })
    if scored is not None and "hazard_score" in scored:
        hazard = scored["hazard_score"].values
        usable = catchment_mask & np.isfinite(hazard)
        if usable.any():
            record["mean_hazard_score"] = float(np.mean(hazard[usable]))
            record["high_hazard_area_pct"] = float(100 * np.mean(hazard[usable] >= 0.60))
            record["hazard_cells_evaluated"] = int(usable.sum())
    summary = pd.DataFrame([record])
    layers.attrs.update(
        method="Priority-Flood conditioned D8 catchment",
        location=location_name,
        caveat="Structural terrain check only; observed flood labels remain necessary for validation.",
    )
    return TerrainCatchmentOutputs(catchment, pour_point, layers, summary)


def normalize_predictors(raw: xr.Dataset, derived: xr.Dataset, config: PCModelConfig) -> xr.Dataset:
    """Stage 5: convert each hazard predictor to a comparable 0..1 scale."""
    normalized = xr.Dataset(
        {
            "extreme_rainfall": _robust_scale(raw.extreme_rainfall_raw.rename("extreme_rainfall"), config),
            "topographic_wetness": _robust_scale(
                derived.topographic_wetness_raw.rename("topographic_wetness"), config
            ),
            "low_hand": _robust_scale(derived.hand_m.rename("low_hand"), config, reverse=True),
            "depression_storage": _robust_scale(
                derived.depression_storage_raw.rename("depression_storage"), config
            ),
            "drainage_proximity": derived.drainage_proximity,
            "runoff_potential": derived.runoff_potential,
        }
    )
    normalized.attrs.update(stage="normalized hazard predictors", range="0 lower to 1 higher")
    return normalized


def score_model(
    normalized: xr.Dataset,
    derived: xr.Dataset,
    config: PCModelConfig,
) -> tuple[xr.Dataset, pd.DataFrame]:
    """Stage 6: calculate contributions, hazard, priority, and classes."""
    template = normalized.extreme_rainfall
    predictor_names = list(config.weights)
    finite_stack = np.stack([np.isfinite(normalized[name].values) for name in predictor_names])
    coverage_values = finite_stack.mean(axis=0).astype("float32")
    # Incomplete cells are unknown, not low hazard. Keep them masked rather than
    # silently turning missing predictor contributions into zero.
    valid = finite_stack.all(axis=0)
    contributions: dict[str, xr.DataArray] = {}
    hazard_values = np.zeros(template.shape, dtype="float32")
    for name, weight in config.weights.items():
        contribution = normalized[name].values * weight
        contributions[f"contribution_{name}"] = _as_layer(contribution, template, f"contribution_{name}")
        hazard_values += np.where(valid, contribution, 0.0)
    hazard_values[~valid] = np.nan
    hazard = _as_layer(np.clip(hazard_values, 0, 1), template, "hazard_score")
    planning_priority = _as_layer(
        np.clip(hazard.values * (0.5 + 0.5 * derived.built_density.values), 0, 1),
        template,
        "planning_priority_score",
    )
    finite_hazard = hazard.values[np.isfinite(hazard.values)]
    breaks = np.unique(np.nanquantile(finite_hazard, [0.2, 0.4, 0.6, 0.8])) if finite_hazard.size else np.array([])
    classes = np.digitize(hazard.values, breaks, right=False) + 1
    hazard_class = _as_layer(np.where(np.isfinite(hazard.values), classes, 0), template, "hazard_class").astype(
        "uint8"
    )
    scored = xr.Dataset(
        {
            **contributions,
            "data_coverage": _as_layer(coverage_values, template, "data_coverage"),
            "complete_data_mask": _as_layer(valid.astype("float32"), template, "complete_data_mask"),
            "hazard_score": hazard,
            "planning_priority_score": planning_priority,
            "hazard_class": hazard_class,
        }
    )
    scored.attrs.update(
        stage="relative hazard and built-environment planning priority",
        classification="relative quintiles; not probabilities or calibrated flood depths",
        class_breaks=",".join(f"{value:.6g}" for value in breaks),
        missing_data_policy="strict intersection of all hazard predictors",
    )
    weights = pd.DataFrame(
        [{"predictor": name, "weight": weight, "weighted_mean": float(contributions[f"contribution_{name}"].mean())}
         for name, weight in config.weights.items()]
    )
    return scored, weights


def assess_drainage_corridors(
    mapped_drainage: gpd.GeoDataFrame,
    zones: gpd.GeoDataFrame,
    derived: xr.Dataset,
    scored: xr.Dataset,
    config: PCModelConfig,
) -> DrainageCorridorOutputs:
    """Screen mapped waterway corridors intersecting the target areas.

    A candidate affected cell must be inside ``drainage_corridor_m`` of a
    mapped centre-line, have HAND no higher than ``corridor_hand_max_m``, and
    meet ``corridor_hazard_threshold``. This is deliberately not described as
    an inundation extent: no discharge, channel capacity, structures, depth,
    or two-dimensional hydraulics are represented.
    """
    if mapped_drainage.empty:
        raise ValueError("Mapped drainage is empty")
    if mapped_drainage.crs is None or zones.crs is None:
        raise ValueError("Mapped drainage and target areas must declare a CRS")
    required_derived = {"hand_m", "built_density"}
    if not required_derived.issubset(derived.data_vars) or "hazard_score" not in scored:
        raise ValueError("Corridor assessment needs HAND, built density, and hazard score")

    template = scored.hazard_score
    drainage = mapped_drainage.to_crs(template.rio.crs).copy()
    target_zones = zones.to_crs(template.rio.crs).copy()
    zone_union = target_zones.geometry.union_all()
    drainage = drainage[drainage.geometry.intersects(zone_union)].copy()
    if drainage.empty:
        raise ValueError("No mapped drainage reaches intersect the target areas")

    drainage["target_areas"] = [
        "; ".join(target_zones.loc[target_zones.geometry.intersects(geometry), "name"].astype(str))
        for geometry in drainage.geometry
    ]
    drainage["length_km_in_target_areas"] = [
        float(geometry.intersection(zone_union).length / 1000) for geometry in drainage.geometry
    ]

    corridor_geometry = drainage.geometry.buffer(config.drainage_corridor_m).union_all().intersection(zone_union)
    corridor_values = rasterize(
        [(corridor_geometry, 1)],
        out_shape=template.shape,
        transform=template.rio.transform(),
        fill=0,
        dtype="uint8",
    ).astype(bool)
    valid = np.isfinite(template.values) & np.isfinite(derived.hand_m.values)
    candidate_values = (
        corridor_values
        & valid
        & (derived.hand_m.values <= config.corridor_hand_max_m)
        & (template.values >= config.corridor_hazard_threshold)
    )
    built_candidate_values = candidate_values & (derived.built_density.values >= 0.20)

    corridor_layer = _as_layer(corridor_values.astype("float32"), template, "mapped_drainage_corridor")
    candidate_layer = _as_layer(candidate_values.astype("float32"), template, "candidate_affected_corridor")
    built_candidate_layer = _as_layer(
        built_candidate_values.astype("float32"), template, "candidate_affected_built_corridor"
    )
    for layer in (corridor_layer, candidate_layer, built_candidate_layer):
        layer.values[~valid] = np.nan
    layers = xr.Dataset(
        {
            "mapped_drainage_corridor": corridor_layer,
            "candidate_affected_corridor": candidate_layer,
            "candidate_affected_built_corridor": built_candidate_layer,
        }
    )
    layers.attrs.update(
        stage="mapped drainage corridor screening",
        drainage_corridor_m=config.drainage_corridor_m,
        corridor_hand_max_m=config.corridor_hand_max_m,
        corridor_hazard_threshold=config.corridor_hazard_threshold,
        warning="Candidate screening corridor; not modelled inundation, depth, velocity, or probability",
    )

    transform = template.rio.transform()
    shape = template.shape
    cell_area_ha = abs(transform.a * transform.e) / 10_000
    rows: list[dict[str, Any]] = []
    for _, zone in target_zones.iterrows():
        inside = geometry_mask([zone.geometry], out_shape=shape, transform=transform, invert=True)
        in_corridor = inside & corridor_values & valid
        affected = inside & candidate_values
        affected_built = inside & built_candidate_values
        intersecting = drainage[drainage.geometry.intersects(zone.geometry)]
        clipped_length = sum(
            geometry.intersection(zone.geometry).length for geometry in intersecting.geometry
        ) / 1000
        corridor_scores = template.values[in_corridor]
        names = sorted(set(intersecting.get("display_name", intersecting.get("name", pd.Series(dtype=str)))
                           .fillna("(unnamed waterway)").astype(str)))
        types = sorted(set(intersecting["waterway"].dropna().astype(str))) if "waterway" in intersecting else []
        rows.append(
            {
                "name": zone["name"],
                "zone": zone.get("zone", ""),
                "river_names": "; ".join(names),
                "waterway_types": "; ".join(types),
                "mapped_reach_length_km": float(clipped_length),
                "corridor_area_ha": float(in_corridor.sum() * cell_area_ha),
                "candidate_affected_area_ha": float(affected.sum() * cell_area_ha),
                "candidate_affected_built_area_ha": float(affected_built.sum() * cell_area_ha),
                "corridor_hazard_mean": float(np.nanmean(corridor_scores)) if corridor_scores.size else np.nan,
                "corridor_hazard_max": float(np.nanmax(corridor_scores)) if corridor_scores.size else np.nan,
            }
        )
    summary = pd.DataFrame(rows).sort_values(
        "candidate_affected_built_area_ha", ascending=False
    ).reset_index(drop=True)
    return DrainageCorridorOutputs(drainage, layers, summary)


def river_reaches_in_zones(
    mapped_drainage: gpd.GeoDataFrame,
    zones: gpd.GeoDataFrame,
    config: PCModelConfig,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Return red-overlay reaches and their 125 m-per-side analysis corridor."""
    if mapped_drainage.empty or zones.empty:
        raise ValueError("Mapped drainage and inspection zones must be non-empty")
    if mapped_drainage.crs is None or zones.crs is None:
        raise ValueError("Mapped drainage and inspection zones must declare a CRS")
    drainage = mapped_drainage.to_crs(config.crs)
    target_zones = zones.to_crs(config.crs)
    rows: list[dict[str, Any]] = []
    for _, zone in target_zones.iterrows():
        for _, reach in drainage[drainage.geometry.intersects(zone.geometry)].iterrows():
            clipped = reach.geometry.intersection(zone.geometry)
            if clipped.is_empty:
                continue
            record = reach.drop(labels="geometry").to_dict()
            record["inspection_area"] = zone.get("name", "")
            record["geometry"] = clipped
            rows.append(record)
    reaches = gpd.GeoDataFrame(rows, geometry="geometry", crs=config.crs)
    if reaches.empty:
        raise ValueError("No mapped drainage reaches intersect the inspection zones")
    corridor_geometry = reaches.geometry.buffer(config.drainage_corridor_m).union_all()
    corridor_geometry = corridor_geometry.intersection(target_zones.geometry.union_all())
    corridor = gpd.GeoDataFrame(
        [{"buffer_m_each_side": config.drainage_corridor_m}],
        geometry=[corridor_geometry],
        crs=config.crs,
    )
    return reaches, corridor


def make_river_buffer_intervals(
    mapped_drainage: gpd.GeoDataFrame,
    zones: gpd.GeoDataFrame,
    config: PCModelConfig,
    distances_m: tuple[float, ...] = (31.0, 62.5, 125.0),
) -> gpd.GeoDataFrame:
    """Create exclusive river-distance rings within every inspection zone."""
    distances = tuple(float(value) for value in distances_m)
    if not distances or any(value <= 0 for value in distances):
        raise ValueError("Buffer distances must be positive")
    if tuple(sorted(set(distances))) != distances:
        raise ValueError("Buffer distances must be unique and strictly increasing")
    distances = tuple(value for value in distances if value <= MAX_RIVER_CORRIDOR_M)
    if not distances:
        raise ValueError("No buffer distances are within the supported 125 m corridor")
    if mapped_drainage.empty or zones.empty:
        raise ValueError("Mapped drainage and inspection zones must be non-empty")
    drainage = mapped_drainage.to_crs(config.crs)
    target_zones = zones.to_crs(config.crs)
    rows: list[dict[str, Any]] = []
    for _, zone in target_zones.iterrows():
        intersecting = drainage[drainage.geometry.intersects(zone.geometry)]
        if intersecting.empty:
            continue
        centre_lines = intersecting.geometry.intersection(zone.geometry).union_all()
        previous = None
        lower = 0.0
        for upper in distances:
            cumulative = centre_lines.buffer(upper).intersection(zone.geometry)
            ring = cumulative if previous is None else cumulative.difference(previous)
            if not ring.is_empty:
                rows.append(
                    {
                        "inspection_area": zone.get("name", ""),
                        "zone": zone.get("zone", ""),
                        "distance_from_m": lower,
                        "distance_to_m": upper,
                        "interval_label": f"{lower:g}-{upper:g} m",
                        "geometry": ring,
                    }
                )
            previous = cumulative
            lower = upper
    intervals = gpd.GeoDataFrame(rows, geometry="geometry", crs=config.crs)
    if intervals.empty:
        raise ValueError("No river buffer intervals could be constructed inside the zones")
    intervals["area_ha"] = intervals.geometry.area / 10_000
    return intervals


def assess_buildings_by_buffer_interval(
    buildings: gpd.GeoDataFrame,
    intervals: gpd.GeoDataFrame,
    water_masks: xr.Dataset,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Assign buildings to their nearest exclusive band and summarize each event."""
    if buildings.crs is None or intervals.crs is None:
        raise ValueError("Buildings and buffer intervals must declare a CRS")
    if not water_masks.data_vars:
        raise ValueError("At least one observed or predicted water mask is required")
    building_layer = buildings.to_crs(intervals.crs)
    assigned_rows: list[dict[str, Any]] = []
    for area_name, area_intervals in intervals.groupby("inspection_area", sort=False):
        ordered = area_intervals.sort_values("distance_to_m")
        outer_geometry = ordered.geometry.union_all()
        candidates = building_layer[building_layer.geometry.intersects(outer_geometry)]
        for _, building in candidates.iterrows():
            matching = ordered[ordered.geometry.intersects(building.geometry)]
            if matching.empty:
                continue
            band = matching.iloc[0]
            record = building.drop(labels="geometry").to_dict()
            record.update(
                {
                    "inspection_area": area_name,
                    "zone": band.get("zone", ""),
                    "distance_from_m": float(band["distance_from_m"]),
                    "distance_to_m": float(band["distance_to_m"]),
                    "interval_label": band["interval_label"],
                    "geometry": building.geometry,
                }
            )
            assigned_rows.append(record)
    exposure = gpd.GeoDataFrame(assigned_rows, geometry="geometry", crs=intervals.crs)
    if exposure.empty:
        base_columns = list(buildings.columns) + [
            "inspection_area", "zone", "distance_from_m", "distance_to_m", "interval_label"
        ]
        exposure = gpd.GeoDataFrame(columns=list(dict.fromkeys(base_columns)), geometry="geometry", crs=intervals.crs)

    summaries: list[dict[str, Any]] = []
    for event, layer in water_masks.data_vars.items():
        event_column = "affected_" + "".join(character if character.isalnum() else "_" for character in event)
        affected_flags: list[bool] = []
        water = layer.values == 1
        transform = layer.rio.transform()
        for geometry in exposure.geometry:
            footprint = geometry_mask(
                [geometry], out_shape=layer.shape, transform=transform, invert=True
            )
            affected_flags.append(bool(np.any(footprint & water)))
        exposure[event_column] = affected_flags

        cell_area_km2 = abs(transform.a * transform.e) / 1_000_000
        for _, band in intervals.iterrows():
            selected = exposure[
                (exposure["inspection_area"] == band["inspection_area"])
                & (exposure["interval_label"] == band["interval_label"])
            ]
            in_band = geometry_mask(
                [band.geometry], out_shape=layer.shape, transform=transform, invert=True
            )
            valid = np.isfinite(layer.values)
            summaries.append(
                {
                    "event": event,
                    "inspection_area": band["inspection_area"],
                    "zone": band.get("zone", ""),
                    "distance_from_m": float(band["distance_from_m"]),
                    "distance_to_m": float(band["distance_to_m"]),
                    "interval_label": band["interval_label"],
                    "interval_area_ha": float(band["area_ha"]),
                    "mapped_water_area_km2": float(np.sum(in_band & water & valid) * cell_area_km2),
                    "osm_buildings_total": int(len(selected)),
                    "osm_buildings_affected": int(selected[event_column].sum()),
                }
            )
    summary = pd.DataFrame(summaries).sort_values(
        ["event", "inspection_area", "distance_to_m"]
    ).reset_index(drop=True)
    return exposure, summary


def assess_candidate_buildings_by_buffer_interval(
    buildings: gpd.GeoDataFrame,
    intervals: gpd.GeoDataFrame,
    candidate_mask: xr.DataArray,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Count buildings in each river band and flag candidate-affected footprints.

    Buildings are assigned to the nearest exclusive distance band they touch.
    ``potentially_affected`` means that a footprint intersects the current
    terrain/HAND/hazard screening mask; it does not mean observed flood damage.
    """
    if buildings.crs is None or intervals.crs is None or candidate_mask.rio.crs is None:
        raise ValueError("Buildings, intervals, and candidate mask must declare a CRS")
    building_layer = buildings.to_crs(intervals.crs)
    assigned_rows: list[dict[str, Any]] = []
    for area_name, area_intervals in intervals.groupby("inspection_area", sort=False):
        ordered = area_intervals.sort_values("distance_to_m")
        candidates = building_layer[
            building_layer.geometry.intersects(ordered.geometry.union_all())
        ]
        for _, building in candidates.iterrows():
            matching = ordered[ordered.geometry.intersects(building.geometry)]
            if matching.empty:
                continue
            band = matching.iloc[0]
            record = building.drop(labels="geometry").to_dict()
            record.update({
                "inspection_area": area_name,
                "zone": band.get("zone", ""),
                "screening_method": "Mapped river corridor",
                "distance_from_m": float(band["distance_from_m"]),
                "distance_to_m": float(band["distance_to_m"]),
                "interval_label": band["interval_label"],
                "geometry": building.geometry,
            })
            assigned_rows.append(record)
    exposure = gpd.GeoDataFrame(assigned_rows, geometry="geometry", crs=intervals.crs)
    if exposure.empty:
        base_columns = list(buildings.columns) + [
            "inspection_area", "zone", "distance_from_m", "distance_to_m",
            "interval_label", "potentially_affected",
        ]
        exposure = gpd.GeoDataFrame(
            columns=list(dict.fromkeys(base_columns)), geometry="geometry", crs=intervals.crs
        )
    else:
        active = np.isfinite(candidate_mask.values) & (candidate_mask.values > 0)
        candidate_polygons = [shape(geometry) for geometry, value in shapes(
            active.astype("uint8"), mask=active, transform=candidate_mask.rio.transform()
        ) if value == 1]
        if candidate_polygons:
            candidate_geometry = gpd.GeoSeries(
                candidate_polygons, crs=candidate_mask.rio.crs
            ).union_all()
            test_geometry = exposure.to_crs(candidate_mask.rio.crs).geometry
            exposure["potentially_affected"] = test_geometry.intersects(candidate_geometry).values
        else:
            exposure["potentially_affected"] = False

    rows: list[dict[str, Any]] = []
    for _, band in intervals.sort_values(["inspection_area", "distance_to_m"]).iterrows():
        selected = exposure[
            (exposure["inspection_area"] == band["inspection_area"])
            & (exposure["interval_label"] == band["interval_label"])
        ]
        total = int(len(selected))
        affected = int(selected["potentially_affected"].sum()) if total else 0
        rows.append({
            "inspection_area": band["inspection_area"],
            "zone": band.get("zone", ""),
            "screening_method": "Mapped river corridor",
            "distance_from_m": float(band["distance_from_m"]),
            "distance_to_m": float(band["distance_to_m"]),
            "interval_label": band["interval_label"],
            "interval_area_ha": float(band["area_ha"]),
            "osm_buildings_in_buffer": total,
            "osm_buildings_potentially_affected": affected,
            "potentially_affected_pct": float(100 * affected / total) if total else 0.0,
        })
    summary = pd.DataFrame(rows).sort_values(
        ["inspection_area", "distance_to_m"]
    ).reset_index(drop=True)
    return exposure, summary


def assess_buildings_in_terrain_catchment(
    buildings: gpd.GeoDataFrame,
    catchment: gpd.GeoDataFrame,
    hazard_score: xr.DataArray,
    *,
    location_name: str = "South C",
    hazard_threshold: float = 0.60,
    moderate_threshold: float = 0.40,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Screen buildings in a terrain catchment where no mapped river exists.

    A building is potentially affected when its footprint intersects a
    catchment cell at or above ``hazard_threshold``. Buildings below that
    threshold are separated into moderate and lower concern so the South C
    field-review list is not reduced to a single yes/no result. This is a
    screening count, not observed damage or a hydraulic inundation result.
    """
    if buildings.crs is None or catchment.crs is None or hazard_score.rio.crs is None:
        raise ValueError("Buildings, catchment, and hazard score must declare a CRS")
    if not 0 <= hazard_threshold <= 1:
        raise ValueError("hazard_threshold must be between 0 and 1")
    if not 0 <= moderate_threshold < hazard_threshold:
        raise ValueError("moderate_threshold must be below hazard_threshold")
    selected_catchment = catchment[
        catchment["name"].astype(str).str.casefold() == location_name.casefold()
    ] if "name" in catchment else catchment
    if selected_catchment.empty:
        raise ValueError(f"No terrain catchment found for {location_name!r}")

    catchment_crs = selected_catchment.crs
    catchment_geometry = selected_catchment.geometry.union_all()
    exposure = buildings.to_crs(catchment_crs)
    exposure = exposure[exposure.geometry.intersects(catchment_geometry)].copy()
    exposure["inspection_area"] = location_name
    exposure["zone"] = "Terrain drainage area"
    exposure["screening_method"] = "Terrain catchment (no mapped river)"
    exposure["distance_from_m"] = np.nan
    exposure["distance_to_m"] = np.nan
    exposure["interval_label"] = "Terrain catchment"

    catchment_on_grid = selected_catchment.to_crs(hazard_score.rio.crs).geometry.union_all()
    inside_catchment = geometry_mask(
        [catchment_on_grid], out_shape=hazard_score.shape,
        transform=hazard_score.rio.transform(), invert=True,
    )
    high = (
        inside_catchment & np.isfinite(hazard_score.values)
        & (hazard_score.values >= hazard_threshold)
    )
    moderate = (
        inside_catchment & np.isfinite(hazard_score.values)
        & (hazard_score.values >= moderate_threshold)
        & (hazard_score.values < hazard_threshold)
    )

    def mask_union(mask: np.ndarray):
        polygons = [shape(geometry) for geometry, value in shapes(
            mask.astype("uint8"), mask=mask, transform=hazard_score.rio.transform()
        ) if value == 1]
        return gpd.GeoSeries(polygons, crs=hazard_score.rio.crs).union_all() if polygons else None

    high_geometry = mask_union(high)
    moderate_geometry = mask_union(moderate)
    if not exposure.empty and high_geometry is not None:
        test_geometry = exposure.to_crs(hazard_score.rio.crs).geometry
        exposure["potentially_affected"] = test_geometry.intersects(high_geometry).values
    else:
        exposure["potentially_affected"] = False
    if not exposure.empty and moderate_geometry is not None:
        moderate_flags = exposure.to_crs(hazard_score.rio.crs).geometry.intersects(
            moderate_geometry
        ).values
    else:
        moderate_flags = np.zeros(len(exposure), dtype=bool)
    exposure["screening_level"] = np.select(
        [exposure["potentially_affected"].to_numpy(), moderate_flags],
        ["Higher concern", "Moderate concern"],
        default="Lower concern",
    )

    total = int(len(exposure))
    affected = int(exposure["potentially_affected"].sum()) if total else 0
    level_counts = exposure["screening_level"].value_counts()
    summary = pd.DataFrame([{
        "inspection_area": location_name,
        "zone": "Terrain drainage area",
        "screening_method": "Terrain catchment (no mapped river)",
        "distance_from_m": np.nan,
        "distance_to_m": np.nan,
        "interval_label": "Terrain catchment",
        "interval_area_ha": float(selected_catchment.to_crs(hazard_score.rio.crs).geometry.area.sum() / 10_000),
        "osm_buildings_in_buffer": total,
        "osm_buildings_potentially_affected": affected,
        "potentially_affected_pct": float(100 * affected / total) if total else 0.0,
        "osm_buildings_higher_concern": int(level_counts.get("Higher concern", 0)),
        "osm_buildings_moderate_concern": int(level_counts.get("Moderate concern", 0)),
        "osm_buildings_lower_concern": int(level_counts.get("Lower concern", 0)),
        "moderate_concern_threshold": moderate_threshold,
        "higher_concern_threshold": hazard_threshold,
    }])
    return exposure, summary


def _water_masks_to_polygons(water_masks: xr.Dataset) -> gpd.GeoDataFrame:
    """Vectorize classified water cells for GeoPackage delivery."""
    records: list[dict[str, Any]] = []
    for event, layer in water_masks.data_vars.items():
        water = layer.values == 1
        if not np.any(water):
            continue
        for geometry, value in shapes(
            water.astype("uint8"), mask=water, transform=layer.rio.transform()
        ):
            if value == 1:
                records.append({"event": event, "geometry": shape(geometry)})
    return gpd.GeoDataFrame(records, geometry="geometry", crs=water_masks.rio.crs)


def plot_river_buffer_analysis(
    extent: RiverFloodExtentOutputs,
    reaches: gpd.GeoDataFrame,
    intervals: gpd.GeoDataFrame,
    building_exposure: gpd.GeoDataFrame,
    interval_summary: pd.DataFrame,
) -> dict[str, Any]:
    """Create event maps and distance-band building-exposure charts."""
    events = list(extent.masks.data_vars)
    if not events:
        raise ValueError("No water masks are available to plot")
    map_crs = extent.masks.rio.crs
    reaches_map = reaches.to_crs(map_crs)
    intervals_map = intervals.to_crs(map_crs)
    buildings_map = building_exposure.to_crs(map_crs) if not building_exposure.empty else building_exposure
    interval_colors = [
        RIVER_BUFFER_COLORS.get(float(value), "#eff3ff")
        for value in intervals_map["distance_to_m"]
    ]

    figure_width = max(7, 6 * len(events))
    event_figure, axes = plt.subplots(
        1, len(events), figsize=(figure_width, 7), squeeze=False, constrained_layout=True
    )
    for ax, event in zip(axes.flat, events):
        intervals_map.plot(ax=ax, color=interval_colors, edgecolor="#666666", linewidth=0.25, alpha=0.72)
        layer = extent.masks[event]
        layer.where(layer == 1).plot(
            ax=ax,
            cmap="Reds" if "Prediction" in event else "Blues",
            add_colorbar=False,
            alpha=0.70,
        )
        if not buildings_map.empty:
            affected_column = "affected_" + "".join(
                character if character.isalnum() else "_" for character in event
            )
            buildings_map.plot(ax=ax, facecolor="none", edgecolor="#555555", linewidth=0.25)
            if affected_column in buildings_map:
                affected = buildings_map[buildings_map[affected_column]]
                if not affected.empty:
                    affected.plot(ax=ax, color="#ffcc00", edgecolor="#7f0000", linewidth=0.5)
        reaches_map.plot(ax=ax, color="red", linewidth=1.5)
        ax.set_title(f"{event}\nWater and affected OSM buildings")
        ax.set_axis_off()
    event_figure.suptitle(
        "River-buffer exposure maps: dark bands are nearest the river; yellow buildings intersect water",
        fontsize=14,
    )

    ordered = interval_summary.sort_values("distance_to_m")
    count_table = ordered.pivot_table(
        index="interval_label", columns="event", values="osm_buildings_affected",
        aggfunc="sum", fill_value=0,
    )
    total_table = ordered.pivot_table(
        index="interval_label", columns="event", values="osm_buildings_total",
        aggfunc="sum", fill_value=0,
    )
    label_order = (
        ordered[["interval_label", "distance_to_m"]]
        .drop_duplicates()
        .sort_values("distance_to_m")["interval_label"]
        .tolist()
    )
    count_table = count_table.reindex(label_order)
    total_table = total_table.reindex(label_order)
    rate_table = count_table.divide(total_table.where(total_table > 0)) * 100
    chart_figure, chart_axes = plt.subplots(1, 2, figsize=(16, 6), constrained_layout=True)
    count_table.plot(kind="bar", ax=chart_axes[0])
    chart_axes[0].set_title("Affected OSM buildings by river-distance interval")
    chart_axes[0].set_xlabel("Exclusive distance interval")
    chart_axes[0].set_ylabel("Affected building footprints")
    chart_axes[0].tick_params(axis="x", rotation=30)
    rate_table.plot(marker="o", ax=chart_axes[1])
    chart_axes[1].set_title("Share of mapped buildings affected")
    chart_axes[1].set_xlabel("Exclusive distance interval")
    chart_axes[1].set_ylabel("Affected buildings (%)")
    chart_axes[1].set_ylim(bottom=0)
    chart_axes[1].tick_params(axis="x", rotation=30)
    chart_axes[1].grid(axis="y", alpha=0.25)
    chart_figure.suptitle(
        "Counts are summed across inspection areas; overlapping inspection areas may repeat a building",
        fontsize=11,
    )
    return {"event_maps": event_figure, "building_charts": chart_figure}


def export_river_buffer_analysis(
    extent: RiverFloodExtentOutputs,
    reaches: gpd.GeoDataFrame,
    intervals: gpd.GeoDataFrame,
    building_exposure: gpd.GeoDataFrame,
    interval_summary: pd.DataFrame,
    output_dir: str | Path = "exports/river_corridor",
) -> tuple[Path, ...]:
    """Write tables, vectors, event maps, and exposure charts."""
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    csv_path = destination / "river_buffer_building_summary.csv"
    excel_path = destination / "river_buffer_building_analysis.xlsx"
    gpkg_path = destination / "river_buffer_analysis.gpkg"
    map_path = destination / "river_buffer_event_maps.png"
    chart_path = destination / "river_buffer_building_charts.png"
    interval_summary.to_csv(csv_path, index=False)
    building_table = pd.DataFrame(building_exposure.drop(columns="geometry"))
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        interval_summary.to_excel(writer, sheet_name="interval_summary", index=False)
        building_table.to_excel(writer, sheet_name="building_exposure", index=False)
        pd.DataFrame(
            [{"key": key, "value": value} for key, value in extent.masks.attrs.items()]
        ).to_excel(writer, sheet_name="metadata", index=False)

    if gpkg_path.exists():
        gpkg_path.unlink()
    layers: list[tuple[str, gpd.GeoDataFrame]] = [
        ("river_reaches", reaches),
        ("buffer_intervals", intervals),
    ]
    if not building_exposure.empty:
        layers.append(("osm_building_exposure", building_exposure))
    water_polygons = _water_masks_to_polygons(extent.masks)
    if not water_polygons.empty:
        layers.append(("water_extents", water_polygons))
    for index, (layer_name, frame) in enumerate(layers):
        frame.to_file(gpkg_path, layer=layer_name, driver="GPKG", mode="w" if index == 0 else "a")
    figures = plot_river_buffer_analysis(
        extent, reaches, intervals, building_exposure, interval_summary
    )
    figures["event_maps"].savefig(map_path, dpi=200, bbox_inches="tight")
    figures["building_charts"].savefig(chart_path, dpi=200, bbox_inches="tight")
    for figure in figures.values():
        plt.close(figure)
    return csv_path, excel_path, gpkg_path, map_path, chart_path


def linear_trend_water_prediction(
    composites: list[xr.DataArray],
    years: list[int],
    target_year: int,
    *,
    water_threshold_db: float = -16.0,
) -> xr.Dataset:
    """Extrapolate per-pixel median VV dB with ordinary least squares.

    This is a transparent scenario baseline, not a weather-driven forecast.
    At least three same-season historical composites are required. RMSE and R²
    are returned so unstable pixels are visible instead of silently accepted.
    """
    if len(composites) != len(years) or len(composites) < 3:
        raise ValueError("Prediction needs at least three composites with matching years")
    if len(set(years)) != len(years):
        raise ValueError("Historical composite years must be unique")
    stack = xr.concat(composites, dim="event").astype("float64")
    x = np.asarray(years, dtype="float64")
    x_centered = x - x.mean()
    denominator = float(np.sum(x_centered**2))
    if denominator == 0:
        raise ValueError("Historical years must span time")
    values = stack.values
    valid = np.all(np.isfinite(values), axis=0)
    mean_y = np.nanmean(values, axis=0)
    slope = np.nansum(x_centered[:, None, None] * values, axis=0) / denominator
    predicted = mean_y + slope * (float(target_year) - x.mean())
    fitted = mean_y[None, :, :] + slope[None, :, :] * x_centered[:, None, None]
    residual_sum_squares = np.nansum((values - fitted) ** 2, axis=0)
    total_sum_squares = np.nansum((values - mean_y[None, :, :]) ** 2, axis=0)
    rmse = np.sqrt(residual_sum_squares / len(years))
    r2 = np.full_like(predicted, np.nan, dtype="float64")
    varying = valid & (total_sum_squares > 0)
    r2[varying] = 1 - residual_sum_squares[varying] / total_sum_squares[varying]
    predicted_water = predicted < water_threshold_db

    template = composites[0]
    layers = xr.Dataset(
        {
            "predicted_vv_db": template.copy(data=np.where(valid, predicted, np.nan).astype("float32")),
            "regression_slope_db_per_year": template.copy(data=np.where(valid, slope, np.nan).astype("float32")),
            "regression_rmse_db": template.copy(data=np.where(valid, rmse, np.nan).astype("float32")),
            "regression_r2": template.copy(data=np.where(valid, r2, np.nan).astype("float32")),
            "predicted_water": template.copy(
                data=np.where(valid, predicted_water.astype("float32"), np.nan)
            ),
        }
    )
    layers.attrs.update(
        model="per-pixel ordinary least-squares trend in seasonal median Sentinel-1 VV dB",
        training_years=",".join(str(year) for year in years),
        target_year=target_year,
        water_threshold_db=water_threshold_db,
        warning="Three-event trend projection; not a rainfall-driven or operational flood forecast",
    )
    return layers


def analyze_sentinel1_river_extents(
    mapped_drainage: gpd.GeoDataFrame,
    zones: gpd.GeoDataFrame,
    config: PCModelConfig,
    time_windows: dict[str, str],
    *,
    buildings: gpd.GeoDataFrame | None = None,
    catalog: pystac_client.Client | None = None,
    water_threshold_db: float = -16.0,
    resolution_m: int = 20,
    historical_labels: tuple[str, ...] = (),
    prediction_year: int | None = None,
    prediction_label: str | None = None,
) -> RiverFloodExtentOutputs:
    """Map median Sentinel-1 low-backscatter water inside river corridors.

    Results are screening masks, not validated flood boundaries. Permanent
    water, radar shadow, smooth wet ground, and threshold sensitivity can all
    affect the classification; event windows with no catalogued scenes are
    retained in the summary with ``status='no_scenes'``.
    """
    if not time_windows:
        raise ValueError("At least one labelled time window is required")
    if resolution_m <= 0:
        raise ValueError("Sentinel-1 resolution must be positive")
    reaches, corridor = river_reaches_in_zones(mapped_drainage, zones, config)
    del reaches  # the caller can obtain the display layer with river_reaches_in_zones
    catalog = catalog or open_catalog()
    west, south, east, north = corridor.to_crs("EPSG:4326").total_bounds
    target_zones = zones.to_crs(config.crs)
    building_layer = None
    if buildings is not None and not buildings.empty:
        if buildings.crs is None:
            raise ValueError("Buildings must declare a CRS")
        building_layer = buildings.to_crs(config.crs)

    mask_layers: dict[str, xr.DataArray] = {}
    composite_layers: dict[str, xr.DataArray] = {}
    event_metadata: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for label, time_window in time_windows.items():
        items = list(
            catalog.search(
                collections=["sentinel-1-rtc"],
                bbox=(west, south, east, north),
                datetime=time_window,
            ).item_collection()
        )
        items = [item for item in items if "vv" in item.assets]
        if not items:
            for _, zone in target_zones.iterrows():
                rows.append(
                    {
                        "event": label,
                        "time_window": time_window,
                        "name": zone.get("name", ""),
                        "zone": zone.get("zone", ""),
                        "status": "no_scenes",
                        "scene_count": 0,
                        "water_area_km2": np.nan,
                        "corridor_area_km2": np.nan,
                        "water_percent_of_corridor": np.nan,
                        "osm_buildings_in_corridor": np.nan,
                        "osm_buildings_intersecting_water": np.nan,
                    }
                )
            continue

        loaded = odc.stac.load(
            items,
            bands=["vv"],
            bbox=(west, south, east, north),
            crs=config.crs,
            resolution=resolution_m,
            chunks={},
            fail_on_error=True,
        ).vv
        vv_db = 10 * np.log10(loaded.where(loaded > 0))
        median_db = vv_db.median(dim="time", skipna=True).compute().astype("float32")
        inside_corridor = geometry_mask(
            corridor.geometry,
            out_shape=median_db.shape,
            transform=median_db.rio.transform(),
            invert=True,
        )
        valid = np.isfinite(median_db.values)
        water = inside_corridor & valid & (median_db.values < water_threshold_db)
        mask = median_db.copy(data=np.where(inside_corridor & valid, water.astype("float32"), np.nan))
        mask.name = label
        mask.attrs.update(
            threshold_db=water_threshold_db,
            corridor_buffer_m_each_side=config.drainage_corridor_m,
            classification="median VV low-backscatter screening mask; 1 water, 0 non-water, NaN outside corridor",
        )
        mask_layers[label] = mask
        composite = median_db.where(inside_corridor & valid)
        composite.name = label
        composite_layers[label] = composite
        event_metadata[label] = {"time_window": time_window, "scene_count": len(items)}

        transform = median_db.rio.transform()
        cell_area_km2 = abs(transform.a * transform.e) / 1_000_000
        for _, zone in target_zones.iterrows():
            in_zone = geometry_mask(
                [zone.geometry], out_shape=median_db.shape, transform=transform, invert=True
            )
            corridor_pixels = in_zone & inside_corridor & valid
            water_pixels = in_zone & water
            zone_corridor = zone.geometry.intersection(corridor.geometry.iloc[0])
            buildings_in_corridor = np.nan
            affected_buildings = np.nan
            if building_layer is not None:
                candidates = building_layer[building_layer.geometry.intersects(zone_corridor)]
                buildings_in_corridor = int(len(candidates))
                affected_buildings = 0
                for geometry in candidates.geometry:
                    footprint = geometry_mask(
                        [geometry], out_shape=median_db.shape, transform=transform, invert=True
                    )
                    affected_buildings += int(np.any(footprint & water_pixels))
            corridor_area = float(corridor_pixels.sum() * cell_area_km2)
            water_area = float(water_pixels.sum() * cell_area_km2)
            rows.append(
                {
                    "event": label,
                    "time_window": time_window,
                    "name": zone.get("name", ""),
                    "zone": zone.get("zone", ""),
                    "status": "ok",
                    "scene_count": len(items),
                    "water_area_km2": water_area,
                    "corridor_area_km2": corridor_area,
                    "water_percent_of_corridor": 100 * water_area / corridor_area if corridor_area else np.nan,
                    "osm_buildings_in_corridor": buildings_in_corridor,
                    "osm_buildings_intersecting_water": affected_buildings,
                }
            )

    masks = xr.Dataset(mask_layers)
    masks.attrs.update(
        source="Microsoft Planetary Computer sentinel-1-rtc, VV polarization",
        water_threshold_db=water_threshold_db,
            corridor_buffer_m_each_side=config.drainage_corridor_m,
        warning="Screening classification; not validated inundation, depth, or forecast",
        events=json.dumps(event_metadata),
    )
    available_historical = [label for label in historical_labels if label in masks]
    historical_max = None
    predicted_expansion = None
    regression = None
    if available_historical:
        historical_max = xr.concat(
            [masks[label].fillna(0).astype(bool) for label in available_historical], dim="event"
        ).max("event")
        historical_max = historical_max.where(masks[available_historical[0]].notnull())
        historical_max.name = "historical_max_water"
    if prediction_year is not None:
        if len(available_historical) < 3:
            raise ValueError("A 2026 regression prediction requires at least three available historical events")
        training_years = [int(time_windows[label].split("-", 1)[0]) for label in available_historical]
        regression = linear_trend_water_prediction(
            [composite_layers[label] for label in available_historical],
            training_years,
            prediction_year,
            water_threshold_db=water_threshold_db,
        )
        label = prediction_label or f"{prediction_year}_Prediction"
        prediction_mask = regression.predicted_water.rename(label)
        mask_layers[label] = prediction_mask
        masks[label] = prediction_mask
        if historical_max is not None:
            predicted_expansion = (prediction_mask == 1) & (historical_max == 0)
            predicted_expansion = predicted_expansion.where(prediction_mask.notnull())
            predicted_expansion.name = "predicted_water_outside_historical_max"

        transform = prediction_mask.rio.transform()
        cell_area_km2 = abs(transform.a * transform.e) / 1_000_000
        water = prediction_mask.values == 1
        valid = np.isfinite(prediction_mask.values)
        inside_corridor = valid
        for _, zone in target_zones.iterrows():
            in_zone = geometry_mask(
                [zone.geometry], out_shape=prediction_mask.shape, transform=transform, invert=True
            )
            corridor_pixels = in_zone & inside_corridor
            water_pixels = in_zone & water
            zone_corridor = zone.geometry.intersection(corridor.geometry.iloc[0])
            buildings_in_corridor = np.nan
            affected_buildings = np.nan
            if building_layer is not None:
                candidates = building_layer[building_layer.geometry.intersects(zone_corridor)]
                buildings_in_corridor = int(len(candidates))
                affected_buildings = 0
                for geometry in candidates.geometry:
                    footprint = geometry_mask(
                        [geometry], out_shape=prediction_mask.shape, transform=transform, invert=True
                    )
                    affected_buildings += int(np.any(footprint & water_pixels))
            corridor_area = float(corridor_pixels.sum() * cell_area_km2)
            water_area = float(water_pixels.sum() * cell_area_km2)
            rows.append(
                {
                    "event": label,
                    "time_window": f"OLS projection from {','.join(map(str, training_years))}",
                    "name": zone.get("name", ""),
                    "zone": zone.get("zone", ""),
                    "status": "model_prediction",
                    "scene_count": sum(event_metadata[item]["scene_count"] for item in available_historical),
                    "water_area_km2": water_area,
                    "corridor_area_km2": corridor_area,
                    "water_percent_of_corridor": 100 * water_area / corridor_area if corridor_area else np.nan,
                    "osm_buildings_in_corridor": buildings_in_corridor,
                    "osm_buildings_intersecting_water": affected_buildings,
                }
            )
    summary = pd.DataFrame(rows)
    return RiverFloodExtentOutputs(masks, summary, historical_max, predicted_expansion, regression)


def predictor_correlation(
    normalized: xr.Dataset,
    config: PCModelConfig,
    *,
    max_pixels: int = 200_000,
    seed: int = 42,
) -> pd.DataFrame:
    """Spearman correlation table used to detect double-counted predictors."""
    frame = pd.DataFrame({name: normalized[name].values.ravel() for name in config.weights}).dropna()
    if frame.empty:
        raise ValueError("No complete pixels are available for predictor correlation")
    if len(frame) > max_pixels:
        frame = frame.sample(max_pixels, random_state=seed)
    return frame.corr(method="spearman")


def weight_sensitivity(
    normalized: xr.Dataset,
    zones: gpd.GeoDataFrame,
    config: PCModelConfig,
    *,
    simulations: int = 500,
    concentration: float = 100.0,
    seed: int = 42,
) -> pd.DataFrame:
    """Monte Carlo neighborhood-rank stability under plausible weight changes."""
    if simulations < 2 or concentration <= 0:
        raise ValueError("simulations must be >= 2 and concentration must be positive")
    names = list(config.weights)
    layers = np.stack([normalized[name].values for name in names])
    valid = np.isfinite(layers).all(axis=0)
    transform = normalized.rio.transform()
    shape = (normalized.sizes["y"], normalized.sizes["x"])
    masks = [geometry_mask([feature.geometry], out_shape=shape, transform=transform, invert=True) & valid
             for _, feature in zones.iterrows()]
    if any(not mask.any() for mask in masks):
        raise ValueError("At least one reporting zone has no complete model cells")

    baseline = np.array([config.weights[name] for name in names], dtype="float64")
    samples = np.random.default_rng(seed).dirichlet(baseline * concentration, size=simulations)
    zone_predictor_means = np.array(
        [[float(layers[predictor_index][mask].mean()) for predictor_index in range(len(names))]
         for mask in masks]
    )
    simulated_scores = samples @ zone_predictor_means.T
    records: list[dict[str, Any]] = []
    for run, means in enumerate(simulated_scores):
        ranks = pd.Series(-means).rank(method="average").to_numpy()
        for zone_index, (_, feature) in enumerate(zones.iterrows()):
            records.append({"simulation": run, "name": feature["name"], "score": means[zone_index],
                            "rank": ranks[zone_index]})
    runs = pd.DataFrame(records)
    return runs.groupby("name").agg(
        score_median=("score", "median"), score_p05=("score", lambda x: x.quantile(0.05)),
        score_p95=("score", lambda x: x.quantile(0.95)), rank_median=("rank", "median"),
        rank_best=("rank", "min"), rank_worst=("rank", "max"), rank_sd=("rank", "std"),
    ).sort_values("rank_median").reset_index()


def load_population_density(
    path: str | Path,
    template: xr.DataArray,
    config: PCModelConfig,
) -> xr.DataArray:
    """Load a local population-density raster without inventing finer detail."""
    source = rioxarray.open_rasterio(path, masked=True).squeeze(drop=True)
    if source.rio.crs is None:
        raise ValueError("Population raster must declare a CRS")
    aligned = source.rio.reproject_match(template, resampling=Resampling.bilinear)
    aligned = aligned.where(aligned >= 0).rename("population_density_raw")
    aligned.attrs.update(source_path=str(path), expected_units="people per square kilometre")
    return aligned.astype("float32")


def load_normalized_index(path: str | Path, template: xr.DataArray, name: str) -> xr.DataArray:
    """Load a reviewed 0..1 vulnerability or exposure index."""
    source = rioxarray.open_rasterio(path, masked=True).squeeze(drop=True)
    if source.rio.crs is None:
        raise ValueError(f"{name} raster must declare a CRS")
    aligned = source.rio.reproject_match(template, resampling=Resampling.bilinear)
    finite = aligned.values[np.isfinite(aligned.values)]
    if finite.size and (finite.min() < 0 or finite.max() > 1):
        raise ValueError(f"{name} must be normalized to the 0..1 range")
    return aligned.rename(name).astype("float32").assign_attrs(source_path=str(path))


def build_exposure_risk(
    hazard: xr.DataArray,
    built_density: xr.DataArray,
    config: PCModelConfig,
    *,
    population_density: xr.DataArray | None = None,
    critical_facilities: gpd.GeoDataFrame | None = None,
    vulnerability: xr.DataArray | None = None,
) -> xr.Dataset:
    """Build explicit exposure, vulnerability, and relative risk components.

    Population and vulnerability inputs must already represent densities or
    indices; raw population counts must first be converted to density.
    """
    components: dict[str, xr.DataArray] = {
        "built_exposure": built_density.clip(0, 1).rename("built_exposure")
    }
    if population_density is not None:
        aligned = population_density.rio.reproject_match(hazard, resampling=Resampling.bilinear)
        components["population_exposure"] = _robust_scale(
            aligned.rename("population_exposure"), config
        )
    if critical_facilities is not None and not critical_facilities.empty:
        facilities = critical_facilities.to_crs(hazard.rio.crs)
        shapes = [(geometry, 1.0) for geometry in facilities.geometry
                  if geometry is not None and not geometry.is_empty]
        if not shapes:
            raise ValueError("Critical-facility dataset contains no usable geometries")
        counts = rasterize(
            shapes,
            out_shape=hazard.shape,
            transform=hazard.rio.transform(),
            fill=0,
            merge_alg=rasterio.enums.MergeAlg.add,
            dtype="float32",
        )
        window = _odd_window(1000, config.resolution_m)
        facility_density = uniform_filter(counts, size=window, mode="constant") * window * window
        components["critical_facility_exposure"] = _robust_scale(
            _as_layer(facility_density, hazard, "critical_facility_exposure"), config
        )

    exposure_values = np.nanmean(np.stack([layer.values for layer in components.values()]), axis=0)
    exposure = _as_layer(exposure_values, hazard, "exposure_score")
    if vulnerability is None:
        vulnerability_layer = _as_layer(np.ones(hazard.shape), hazard, "vulnerability_score")
        vulnerability_note = "neutral placeholder (1.0); supply an evidence-based local layer"
    else:
        vulnerability_layer = vulnerability.rio.reproject_match(hazard, resampling=Resampling.bilinear)
        vulnerability_layer = vulnerability_layer.clip(0, 1).rename("vulnerability_score")
        vulnerability_note = "user supplied"
    risk = _as_layer(hazard.values * exposure.values * vulnerability_layer.values, hazard, "risk_score")
    result = xr.Dataset({**components, "exposure_score": exposure,
                         "vulnerability_score": vulnerability_layer, "risk_score": risk})
    result.attrs.update(
        stage="explicit relative exposure and risk",
        vulnerability=vulnerability_note,
        warning="Relative screening index; not expected loss, depth, velocity, or probability",
    )
    return result


def sample_incident_points(hazard: xr.DataArray, incidents: gpd.GeoDataFrame) -> pd.DataFrame:
    """Sample hazard at independently geocoded flood incidents (positive cases)."""
    if incidents.empty:
        raise ValueError("Incident dataset is empty")
    points = incidents.to_crs(hazard.rio.crs)
    rows = []
    for index, feature in points.iterrows():
        geometry = feature.geometry
        if geometry is None or geometry.is_empty:
            continue
        point = geometry if geometry.geom_type == "Point" else geometry.representative_point()
        score = hazard.sel(x=point.x, y=point.y, method="nearest")
        rows.append({"incident_index": index, "hazard_score": float(score.values)})
    return pd.DataFrame(rows)


def validate_flood_mask(
    hazard: xr.DataArray,
    observed: xr.DataArray,
    *,
    threshold: float | None = None,
) -> pd.DataFrame:
    """Compare hazard with independent labelled flood/dry pixels.

    `observed` must use 1=flooded, 0=confirmed dry, and NaN=unknown. Unknown
    pixels are excluded so unreported flooding is never treated as dry.
    """
    truth = observed.rio.reproject_match(hazard, resampling=Resampling.nearest)
    truth_values = truth.values.astype("float64")
    if truth_values.ndim > 2:
        truth_values = np.squeeze(truth_values)
    valid = np.isfinite(hazard.values) & np.isin(truth_values, [0, 1])
    if not valid.any() or not np.any(truth_values[valid] == 1) or not np.any(truth_values[valid] == 0):
        raise ValueError("Validation needs both flooded (1) and confirmed-dry (0) labelled pixels")
    scores = hazard.values[valid]
    actual = truth_values[valid].astype(bool)
    threshold = float(np.nanquantile(hazard.values, 0.8)) if threshold is None else threshold
    predicted = scores >= threshold
    tp = int(np.sum(predicted & actual))
    fp = int(np.sum(predicted & ~actual))
    fn = int(np.sum(~predicted & actual))
    tn = int(np.sum(~predicted & ~actual))
    precision = tp / (tp + fp) if tp + fp else np.nan
    recall = tp / (tp + fn) if tp + fn else np.nan
    order = np.argsort(-scores)
    ordered_actual = actual[order]
    cumulative_tp = np.cumsum(ordered_actual)
    cumulative_fp = np.cumsum(~ordered_actual)
    tpr = np.r_[0.0, cumulative_tp / actual.sum(), 1.0]
    fpr = np.r_[0.0, cumulative_fp / (~actual).sum(), 1.0]
    roc_auc = float(np.trapezoid(tpr, fpr))
    pr_precision = cumulative_tp / np.arange(1, len(scores) + 1)
    pr_recall = cumulative_tp / actual.sum()
    pr_auc = float(np.trapezoid(np.r_[1.0, pr_precision], np.r_[0.0, pr_recall]))
    return pd.DataFrame([{
        "threshold": threshold, "labelled_pixels": int(valid.sum()), "tp": tp, "fp": fp,
        "fn": fn, "tn": tn, "precision": precision, "recall": recall,
        "specificity": tn / (tn + fp) if tn + fp else np.nan,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else np.nan,
        "intersection_over_union": tp / (tp + fp + fn) if tp + fp + fn else np.nan,
        "roc_auc": roc_auc, "precision_recall_auc": pr_auc,
        "flooded_score_mean": float(scores[actual].mean()),
        "dry_score_mean": float(scores[~actual].mean()),
    }])


def summarize_zones(layers: xr.Dataset, zones: gpd.GeoDataFrame) -> pd.DataFrame:
    """Stage 7: summarize score and exposure-proxy metrics for every target zone."""
    transform = layers.rio.transform()
    shape = (layers.sizes["y"], layers.sizes["x"])
    cell_area_ha = abs(transform.a * transform.e) / 10_000
    rows: list[dict[str, Any]] = []
    for _, feature in zones.iterrows():
        inside = geometry_mask([feature.geometry], out_shape=shape, transform=transform, invert=True)
        row: dict[str, Any] = {"name": feature["name"], "zone": feature["zone"]}
        metrics = [name for name in (
            "hazard_score", "planning_priority_score", "exposure_score", "risk_score", "built_density"
        ) if name in layers]
        for name in metrics:
            values = layers[name].values[inside]
            row[f"{name}_mean"] = float(np.nanmean(values))
            row[f"{name}_max"] = float(np.nanmax(values))
        high = inside & (layers.hazard_score.values >= 0.60)
        high_built = high & (layers.built_density.values >= 0.20)
        row["high_hazard_area_ha"] = float(high.sum() * cell_area_ha)
        row["high_hazard_built_area_ha"] = float(high_built.sum() * cell_area_ha)
        rows.append(row)
    sort_column = "risk_score_mean" if "risk_score_mean" in rows[0] else "planning_priority_score_mean"
    return pd.DataFrame(rows).sort_values(sort_column, ascending=False).reset_index(drop=True)


def assemble_outputs(
    config: PCModelConfig,
    sources: PCSourceBundle,
    raw: xr.Dataset,
    derived: xr.Dataset,
    normalized: xr.Dataset,
    scored: xr.Dataset,
    exposure: xr.Dataset | None = None,
    drainage_corridors: xr.Dataset | None = None,
    drainage_reaches: gpd.GeoDataFrame | None = None,
    drainage_corridor_summary: pd.DataFrame | None = None,
    buildings: gpd.GeoDataFrame | None = None,
    terrain_catchment_layers: xr.Dataset | None = None,
    terrain_catchments: gpd.GeoDataFrame | None = None,
    terrain_pour_points: gpd.GeoDataFrame | None = None,
    reference_points: gpd.GeoDataFrame | None = None,
    catchment_summary: pd.DataFrame | None = None,
    buffer_building_exposure: gpd.GeoDataFrame | None = None,
    buffer_building_summary: pd.DataFrame | None = None,
    mapped_dams: gpd.GeoDataFrame | None = None,
) -> PCModelOutputs:
    """Combine reviewed stages without recalculating or downloading data."""
    aoi, neighborhoods, zones = make_review_geometries(config)
    parts = [raw, derived, normalized, scored]
    if exposure is not None:
        parts.append(exposure)
    if drainage_corridors is not None:
        parts.append(drainage_corridors)
    if terrain_catchment_layers is not None:
        parts.append(terrain_catchment_layers)
    layers = xr.merge(parts, join="exact", compat="override")
    layers = layers.rio.clip(aoi.geometry, aoi.crs, drop=True)
    layers.attrs.update(
        model="Nairobi flood screening model",
        pipeline_version=PIPELINE_VERSION,
        model_type="relative susceptibility, explicit exposure, and optional relative risk",
        stac_url=STAC_URL,
        analysis_crs=config.crs,
        analysis_resolution_m=config.resolution_m,
        reporting_resolution_m=config.reporting_resolution_m,
        warning="Not observed inundation, a forecast, or a hydraulic model",
        rainfall_scale_warning="TerraClimate is ~4.6 km; aligned 30 m pixels do not add rainfall detail",
    )
    inventory = _inventory_with_osm_vectors(
        sources.inventory, drainage_reaches, buildings, mapped_dams
    )
    if drainage_reaches is not None and not drainage_reaches.empty:
        layers.attrs.update(
            mapped_drainage_source="OpenStreetMap contributors, ODbL 1.0",
            mapped_drainage_reaches=len(drainage_reaches),
            corridor_warning="Screening corridor only; no channel capacity or hydraulic simulation",
        )
    summary = summarize_zones(layers, zones)
    return PCModelOutputs(
        aoi=aoi, layers=layers, neighborhoods=neighborhoods,
        neighborhood_zones=zones, summary=summary, inventory=inventory,
        drainage_reaches=drainage_reaches,
        drainage_corridor_summary=drainage_corridor_summary,
        terrain_catchments=terrain_catchments,
        terrain_pour_points=terrain_pour_points,
        reference_points=reference_points,
        catchment_summary=catchment_summary,
        buffer_building_exposure=buffer_building_exposure,
        buffer_building_summary=buffer_building_summary,
        mapped_dams=mapped_dams,
    )


def aggregate_for_reporting(layers: xr.Dataset, resolution_m: int) -> xr.Dataset:
    """Aggregate continuous layers and nearest-sample categorical layers."""
    current = abs(float(layers.rio.resolution()[0]))
    if resolution_m <= current:
        return layers
    categorical = {"worldcover", "modeled_stream", "mapped_drainage", "mapped_dams", "terrain_valid",
                   "complete_data_mask", "hazard_class", "mapped_drainage_corridor",
                   "candidate_affected_corridor", "candidate_affected_built_corridor",
                   "catchment_mask"}
    aggregated = []
    for name, layer in layers.data_vars.items():
        method = Resampling.nearest if name in categorical else Resampling.average
        aggregated.append(layer.rio.reproject(layers.rio.crs, resolution=resolution_m,
                                               resampling=method).rename(name))
    result = xr.merge(aggregated, compat="override", join="exact")
    result.attrs.update(layers.attrs)
    result.attrs.update(reporting_aggregation=f"{current:g} m to {resolution_m} m",
                        categorical_resampling="nearest", continuous_resampling="average")
    return result


def build_model(config: PCModelConfig | None = None) -> PCModelOutputs:
    """Convenience wrapper; notebooks should prefer the explicit review stages."""
    config = config or PCModelConfig()
    sources = discover_sources(config)
    raw = load_raw_layers(sources, config)
    derived = derive_predictors(raw, config)
    normalized = normalize_predictors(raw, derived, config)
    scored, _ = score_model(normalized, derived, config)
    return assemble_outputs(config, sources, raw, derived, normalized, scored)


def export_outputs(outputs: PCModelOutputs, output_dir: str | Path = "exports") -> tuple[Path, ...]:
    """Write rasters, summaries, source inventory, and run metadata."""
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    raster_path = destination / "nairobi_flood_screening.tif"
    csv_path = destination / "nairobi_neighborhood_summary.csv"
    inventory_path = destination / "source_inventory.csv"
    metadata_path = destination / "run_metadata.json"
    preferred = [
        "extreme_rainfall", "topographic_wetness", "low_hand", "depression_storage",
        "drainage_proximity", "runoff_potential", "flow_accumulation_km2", "hand_m",
        "mapped_dams", "mapped_drainage_corridor", "candidate_affected_corridor",
        "candidate_affected_built_corridor",
        "catchment_mask", "catchment_flow_accumulation_km2",
        "built_density", "data_coverage", "hazard_score", "hazard_class",
        "planning_priority_score", "population_exposure", "critical_facility_exposure",
        "exposure_score", "vulnerability_score", "risk_score",
    ]
    bands = [name for name in preferred if name in outputs.layers]
    reporting_resolution = int(outputs.layers.attrs.get(
        "reporting_resolution_m", outputs.layers.attrs.get("analysis_resolution_m", 30)
    ))
    reporting_layers = aggregate_for_reporting(outputs.layers[bands], reporting_resolution)
    export_bands = []
    for name, layer in reporting_layers.data_vars.items():
        export_layer = layer.astype("float32")
        nodata = layer.rio.nodata
        if nodata is not None and not np.isnan(nodata):
            export_layer = export_layer.where(export_layer != nodata)
        export_bands.append(export_layer.rio.write_nodata(np.nan).rename(name))
    reporting_layers = xr.merge(export_bands, compat="override", join="exact")
    reporting_layers.attrs.update(outputs.layers.attrs)
    reporting_layers.rio.to_raster(raster_path, compress="deflate", tiled=True)
    outputs.summary.to_csv(csv_path, index=False)
    outputs.inventory.to_csv(inventory_path, index=False)
    metadata_path.write_text(json.dumps(dict(outputs.layers.attrs), indent=2, default=str), encoding="utf-8")
    paths = [raster_path, csv_path, inventory_path, metadata_path]
    if outputs.drainage_reaches is not None and not outputs.drainage_reaches.empty:
        reaches_path = destination / "mapped_drainage_reaches.geojson"
        outputs.drainage_reaches.to_crs("EPSG:4326").to_file(reaches_path, driver="GeoJSON")
        paths.append(reaches_path)
    if outputs.mapped_dams is not None and not outputs.mapped_dams.empty:
        dams_path = destination / "mapped_dams.geojson"
        outputs.mapped_dams.to_crs("EPSG:4326").to_file(dams_path, driver="GeoJSON")
        paths.append(dams_path)
    if outputs.drainage_corridor_summary is not None:
        corridor_summary_path = destination / "drainage_corridor_summary.csv"
        outputs.drainage_corridor_summary.to_csv(corridor_summary_path, index=False)
        paths.append(corridor_summary_path)
    if outputs.terrain_catchments is not None and not outputs.terrain_catchments.empty:
        catchment_path = destination / "south_c_terrain_catchment.geojson"
        outputs.terrain_catchments.to_crs("EPSG:4326").to_file(catchment_path, driver="GeoJSON")
        paths.append(catchment_path)
    if outputs.terrain_pour_points is not None and not outputs.terrain_pour_points.empty:
        pour_point_path = destination / "south_c_terrain_pour_point.geojson"
        outputs.terrain_pour_points.to_crs("EPSG:4326").to_file(pour_point_path, driver="GeoJSON")
        paths.append(pour_point_path)
    if outputs.reference_points is not None and not outputs.reference_points.empty:
        reference_path = destination / "wilson_airport_reference.geojson"
        outputs.reference_points.to_crs("EPSG:4326").to_file(reference_path, driver="GeoJSON")
        paths.append(reference_path)
    if outputs.catchment_summary is not None:
        catchment_summary_path = destination / "south_c_catchment_summary.csv"
        outputs.catchment_summary.to_csv(catchment_summary_path, index=False)
        paths.append(catchment_summary_path)
    if outputs.buffer_building_exposure is not None and not outputs.buffer_building_exposure.empty:
        building_exposure_path = destination / "drainage_area_buildings.geojson"
        outputs.buffer_building_exposure.to_crs("EPSG:4326").to_file(
            building_exposure_path, driver="GeoJSON"
        )
        paths.append(building_exposure_path)
    if outputs.buffer_building_summary is not None:
        building_summary_path = destination / "building_screening_summary.csv"
        outputs.buffer_building_summary.to_csv(building_summary_path, index=False)
        paths.append(building_summary_path)
    return tuple(paths)
