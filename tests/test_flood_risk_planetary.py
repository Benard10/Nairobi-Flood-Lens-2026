import numpy as np
import geopandas as gpd
import json
import pandas as pd
import pytest
import xarray as xr
from affine import Affine
from shapely.geometry import LineString, Point, box
from urllib.error import HTTPError

import flood_risk_planetary as frp


def _layer(values, name="test"):
    data = xr.DataArray(
        np.asarray(values, dtype="float32"),
        dims=("y", "x"),
        coords={"y": [45.0, 15.0, -15.0], "x": [15.0, 45.0, 75.0]},
        name=name,
    )
    return data.rio.write_crs("EPSG:32737").rio.write_transform(
        Affine(30, 0, 0, 0, -30, 60)
    )


def test_odd_window_is_positive_and_odd():
    assert frp._odd_window(450, 30) == 15
    assert frp._odd_window(900, 90) == 11
    assert frp._odd_window(1, 30) == 1


def test_robust_scale_masks_missing_and_reverses():
    layer = _layer([[0, 1, 2], [3, np.nan, 5], [6, 7, 8]])
    config = frp.PCModelConfig(percentile_low=0, percentile_high=100)
    scaled = frp._robust_scale(layer, config)
    reversed_layer = frp._robust_scale(layer, config, reverse=True)
    assert np.isnan(scaled.values[1, 1])
    assert scaled.values[0, 0] == 0
    assert scaled.values[2, 2] == 1
    np.testing.assert_allclose(reversed_layer.values[np.isfinite(scaled.values)],
                               1 - scaled.values[np.isfinite(scaled.values)])


def test_worldcover_lookup_and_built_mask():
    landcover = _layer([[10, 20, 30], [40, 50, 60], [70, 80, 100]], "worldcover")
    runoff, built = frp._worldcover_runoff(landcover)
    assert runoff.values[1, 1] == np.float32(0.95)
    assert built.values.sum() == 1


def test_priority_flood_fills_enclosed_pit():
    elevation = np.array([[5, 5, 5], [5, 1, 5], [5, 5, 5]], dtype="float64")
    valid = np.ones_like(elevation, dtype=bool)
    conditioned, parent, order = frp._priority_flood_route(elevation, valid)
    assert conditioned[1, 1] == 5
    assert parent[4] >= 0
    receiver = frp._d8_receivers(conditioned, parent, valid, 30)
    accumulation = frp._flow_accumulation(receiver, order, valid)
    assert np.isfinite(accumulation).all()


def test_terrain_catchment_uses_dem_outlet_without_mapped_river():
    elevation = _layer(
        [[90, 80, 70], [80, 60, 40], [70, 40, 10]], "elevation"
    )
    raw = xr.Dataset({"elevation": elevation})
    locations = gpd.GeoDataFrame(
        [{"name": "South C"}], geometry=[Point(45, 15)], crs="EPSG:32737"
    )
    scored = xr.Dataset({
        "hazard_score": _layer(np.full((3, 3), 0.8), "hazard_score")
    })

    result = frp.delineate_terrain_catchment(
        raw,
        locations,
        frp.PCModelConfig(resolution_m=30),
        location_name="South C",
        snap_radius_m=1,
        scored=scored,
    )

    assert result.layers.catchment_mask.values[1, 1] == 1
    assert result.pour_point.loc[0, "snap_distance_m"] == 0
    assert result.summary.loc[0, "mean_hazard_score"] == pytest.approx(0.8)
    assert result.summary.loc[0, "high_hazard_area_pct"] == 100
    assert result.summary.loc[0, "interpretation"].startswith("terrain consistency")


def test_score_model_masks_any_incomplete_predictor():
    config = frp.PCModelConfig()
    base = np.full((3, 3), 0.5, dtype="float32")
    normalized = xr.Dataset({name: _layer(base.copy(), name) for name in config.weights})
    normalized["low_hand"].values[1, 1] = np.nan
    derived = xr.Dataset({"built_density": _layer(np.ones((3, 3)), "built_density")})
    scored, _ = frp.score_model(normalized, derived, config)
    assert np.isnan(scored.hazard_score.values[1, 1])
    assert scored.data_coverage.values[1, 1] == np.float32(5 / 6)
    assert scored.complete_data_mask.values[1, 1] == 0


def test_drainage_corridor_requires_proximity_low_hand_and_hazard():
    config = frp.PCModelConfig(
        drainage_corridor_m=20,
        corridor_hand_max_m=5,
        corridor_hazard_threshold=0.6,
    )
    drainage = gpd.GeoDataFrame(
        [{"name": "Test River", "display_name": "Test River", "waterway": "river"}],
        geometry=[LineString([(45, -30), (45, 60)])],
        crs="EPSG:32737",
    )
    zones = gpd.GeoDataFrame(
        [{"name": "Test Area", "zone": "Test Basin"}],
        geometry=[box(0, -30, 90, 60)],
        crs="EPSG:32737",
    )
    hazard = _layer(np.full((3, 3), 0.8), "hazard_score")
    hand = _layer(np.full((3, 3), 2.0), "hand_m")
    built = _layer(np.ones((3, 3)), "built_density")
    hand.values[1, 1] = 8.0

    result = frp.assess_drainage_corridors(
        drainage,
        zones,
        xr.Dataset({"hand_m": hand, "built_density": built}),
        xr.Dataset({"hazard_score": hazard}),
        config,
    )

    assert result.summary.loc[0, "river_names"] == "Test River"
    assert result.summary.loc[0, "mapped_reach_length_km"] == 0.09
    assert result.layers.mapped_drainage_corridor.values.sum() == 3
    assert result.layers.candidate_affected_corridor.values.sum() == 2


def test_river_reaches_are_clipped_to_zones_and_buffered_500m_each_side():
    config = frp.PCModelConfig(crs="EPSG:32737", drainage_corridor_m=500)
    drainage = gpd.GeoDataFrame(
        [{"name": "Test River", "display_name": "Test River", "waterway": "river"}],
        geometry=[LineString([(-1000, 500), (2000, 500)])],
        crs=config.crs,
    )
    zones = gpd.GeoDataFrame(
        [{"name": "Inspection A", "zone": "Test Basin"}],
        geometry=[box(0, 0, 1000, 1000)],
        crs=config.crs,
    )

    reaches, corridor = frp.river_reaches_in_zones(drainage, zones, config)

    assert reaches.loc[0, "inspection_area"] == "Inspection A"
    assert reaches.geometry.iloc[0].length == 1000
    assert corridor.loc[0, "buffer_m_each_side"] == 500
    assert corridor.geometry.iloc[0].within(zones.geometry.iloc[0])
    assert corridor.geometry.iloc[0].area == 1_000_000


def test_linear_trend_predicts_2026_from_historical_db_not_future_search():
    years = [2015, 2019, 2023]
    composites = [
        _layer(np.full((3, 3), value), f"vv_{year}")
        for year, value in zip(years, [-12.0, -14.0, -16.0])
    ]

    prediction = frp.linear_trend_water_prediction(
        composites, years, 2026, water_threshold_db=-16.0
    )

    np.testing.assert_allclose(prediction.predicted_vv_db.values, -17.5)
    np.testing.assert_allclose(prediction.regression_slope_db_per_year.values, -0.5)
    assert np.all(prediction.predicted_water.values == 1)
    assert prediction.attrs["target_year"] == 2026
    assert "not a rainfall-driven" in prediction.attrs["warning"]


def test_exclusive_buffer_intervals_and_affected_building_summary():
    config = frp.PCModelConfig(crs="EPSG:32737", drainage_corridor_m=20)
    drainage = gpd.GeoDataFrame(
        [{"display_name": "Test River", "waterway": "river"}],
        geometry=[LineString([(45, -30), (45, 90)])],
        crs=config.crs,
    )
    zones = gpd.GeoDataFrame(
        [{"name": "Inspection A", "zone": "Test Basin"}],
        geometry=[box(0, -30, 90, 60)],
        crs=config.crs,
    )
    intervals = frp.make_river_buffer_intervals(
        drainage, zones, config, distances_m=(10, 20)
    )
    assert list(intervals.interval_label) == ["0-10 m", "10-20 m"]
    assert not intervals.geometry.iloc[0].intersects(intervals.geometry.iloc[1].buffer(-0.01))

    buildings = gpd.GeoDataFrame(
        [{"osm_id": 1}, {"osm_id": 2}],
        geometry=[box(40, 10, 50, 20), box(58, 10, 62, 20)],
        crs=config.crs,
    )
    water = _layer([[0, 1, 0], [0, 1, 0], [0, 1, 0]], "2015_Event")
    exposure, summary = frp.assess_buildings_by_buffer_interval(
        buildings, intervals, xr.Dataset({"2015_Event": water})
    )

    assert len(exposure) == 2
    inner = summary.loc[summary.interval_label == "0-10 m"].iloc[0]
    outer = summary.loc[summary.interval_label == "10-20 m"].iloc[0]
    assert inner.osm_buildings_total == 1
    assert inner.osm_buildings_affected == 1
    assert outer.osm_buildings_total == 1
    assert outer.osm_buildings_affected == 0


def test_candidate_building_counts_are_reported_for_each_river_band():
    config = frp.PCModelConfig(crs="EPSG:32737", drainage_corridor_m=20)
    drainage = gpd.GeoDataFrame(
        [{"display_name": "Test River", "waterway": "river"}],
        geometry=[LineString([(45, -30), (45, 90)])], crs=config.crs,
    )
    zones = gpd.GeoDataFrame(
        [{"name": "Inspection A", "zone": "Test Basin"}],
        geometry=[box(0, -30, 90, 60)], crs=config.crs,
    )
    intervals = frp.make_river_buffer_intervals(drainage, zones, config, (10, 20))
    buildings = gpd.GeoDataFrame(
        [{"osm_id": 1}, {"osm_id": 2}],
        geometry=[box(40, 10, 50, 20), box(61, 10, 64, 20)], crs=config.crs,
    )
    candidate = _layer(
        [[0, 0, 0], [0, 1, 0], [0, 0, 0]], "candidate_affected_corridor"
    )

    exposure, summary = frp.assess_candidate_buildings_by_buffer_interval(
        buildings, intervals, candidate
    )

    assert len(exposure) == 2
    inner = summary.loc[summary.interval_label == "0-10 m"].iloc[0]
    outer = summary.loc[summary.interval_label == "10-20 m"].iloc[0]
    assert inner.osm_buildings_in_buffer == 1
    assert inner.osm_buildings_potentially_affected == 1
    assert inner.potentially_affected_pct == 100
    assert outer.osm_buildings_in_buffer == 1
    assert outer.osm_buildings_potentially_affected == 0


def test_south_c_buildings_use_terrain_catchment_when_no_river_exists():
    buildings = gpd.GeoDataFrame(
        [{"osm_id": 1}, {"osm_id": 2}],
        geometry=[box(40, 10, 50, 20), box(65, 10, 70, 20)], crs="EPSG:32737",
    )
    catchment = gpd.GeoDataFrame(
        [{"name": "South C"}], geometry=[box(0, -30, 90, 60)], crs="EPSG:32737"
    )
    hazard = _layer(
        [[0.2, 0.2, 0.2], [0.2, 0.8, 0.2], [0.2, 0.2, 0.2]], "hazard_score"
    )

    exposure, summary = frp.assess_buildings_in_terrain_catchment(
        buildings, catchment, hazard, location_name="South C", hazard_threshold=0.6
    )

    assert len(exposure) == 2
    assert summary.loc[0, "interval_label"] == "Terrain catchment"
    assert summary.loc[0, "osm_buildings_in_buffer"] == 2
    assert summary.loc[0, "osm_buildings_potentially_affected"] == 1
    assert summary.loc[0, "potentially_affected_pct"] == 50
    assert set(exposure["screening_level"]) == {"Higher concern", "Lower concern"}
    assert summary.loc[0, "osm_buildings_higher_concern"] == 1
    assert summary.loc[0, "osm_buildings_moderate_concern"] == 0
    assert summary.loc[0, "osm_buildings_lower_concern"] == 1


def test_local_waterways_are_line_filtered_and_clipped_to_aoi(tmp_path):
    source = gpd.GeoDataFrame(
        [
            {"id": "river-1", "name": "Test River", "waterway": "river",
             "geometry": LineString([(-1, 5), (11, 5)])},
            {"id": "pond-1", "name": "Pond", "waterway": None, "water": "pond",
             "geometry": box(2, 2, 3, 3)},
            {"id": "road-1", "name": "Not water", "waterway": "road",
             "geometry": LineString([(1, 1), (9, 1)])},
        ],
        crs="EPSG:32737",
    )
    path = tmp_path / "waterways.geojson"
    source.to_file(path, driver="GeoJSON")
    aoi = gpd.GeoDataFrame(
        [{"name": "AOI"}], geometry=[box(0, 0, 10, 10)], crs="EPSG:32737"
    )

    clipped = frp.load_and_clip_local_waterways(path, aoi)

    assert len(clipped) == 1
    assert clipped.iloc[0].display_name == "Test River"
    assert clipped.iloc[0].model_source == "waterways.geojson"
    clipped_projected = clipped.to_crs(aoi.crs)
    assert clipped_projected.total_bounds[0] >= 0
    assert clipped_projected.total_bounds[2] <= 10

    context = frp.load_and_clip_local_water_context(path, aoi)
    assert len(context) == 1
    assert context.iloc[0].display_name == "Pond"
    assert context.iloc[0].context_type == "pond"


def test_reviewed_dams_are_loaded_and_used_by_hydrology(tmp_path):
    source = gpd.GeoDataFrame(
        [{"id": "dam-1", "name": "Test Dam", "water": "reservoir"}],
        geometry=[box(30, 0, 60, 30)],
        crs="EPSG:32737",
    )
    path = tmp_path / "dams.geojson"
    source.to_file(path, driver="GeoJSON")
    aoi = gpd.GeoDataFrame(
        [{"name": "AOI"}], geometry=[box(0, -30, 90, 60)], crs="EPSG:32737"
    )
    dams = frp.load_and_clip_local_dams(path, aoi)

    assert len(dams) == 1
    assert dams.iloc[0].display_name == "Test Dam"
    assert dams.iloc[0].waterbody_type == "reservoir"

    elevation = _layer([[90, 80, 70], [80, 60, 40], [70, 40, 10]], "elevation")
    raw = xr.Dataset({
        "elevation": elevation,
        "water_occurrence": _layer(np.zeros((3, 3)), "water_occurrence"),
        "worldcover": _layer(np.full((3, 3), 10), "worldcover"),
    })
    derived = frp.derive_predictors(
        raw,
        frp.PCModelConfig(crs="EPSG:32737", stream_threshold_km2=999),
        mapped_dams=dams,
    )

    assert derived.mapped_dams.values[1, 1] == 1
    assert derived.distance_to_drainage_m.values[1, 1] == 0
    assert derived.drainage_proximity.values[1, 1] == 1


def test_river_buffer_exports_csv_excel_and_geopackage(tmp_path):
    config = frp.PCModelConfig(crs="EPSG:32737", drainage_corridor_m=20)
    reaches = gpd.GeoDataFrame(
        [{"display_name": "Test River", "waterway": "river"}],
        geometry=[LineString([(45, -30), (45, 60)])],
        crs=config.crs,
    )
    zones = gpd.GeoDataFrame(
        [{"name": "Inspection A", "zone": "Test Basin"}],
        geometry=[box(0, -30, 90, 60)],
        crs=config.crs,
    )
    intervals = frp.make_river_buffer_intervals(reaches, zones, config, (10, 20))
    buildings = gpd.GeoDataFrame(
        [{"osm_id": 1}], geometry=[box(40, 10, 50, 20)], crs=config.crs
    )
    water = _layer([[0, 1, 0], [0, 1, 0], [0, 1, 0]], "2015_Event")
    masks = xr.Dataset({"2015_Event": water})
    exposure, summary = frp.assess_buildings_by_buffer_interval(buildings, intervals, masks)
    extent = frp.RiverFloodExtentOutputs(masks=masks, summary=pd.DataFrame())

    paths = frp.export_river_buffer_analysis(
        extent, reaches, intervals, exposure, summary, tmp_path
    )

    assert all(path.exists() for path in paths)
    assert set(pd.ExcelFile(paths[1]).sheet_names) == {
        "interval_summary", "building_exposure", "metadata"
    }
    assert set(gpd.list_layers(paths[2]).name) == {
        "river_reaches", "buffer_intervals", "osm_building_exposure", "water_extents"
    }


def test_osm_buildings_batches_query_and_fails_over_after_429(monkeypatch):
    target = gpd.GeoDataFrame(
        geometry=[box(36.80, -1.30, 36.81, -1.29), box(36.82, -1.28, 36.83, -1.27)],
        crs="EPSG:4326",
    )
    payload = {
        "elements": [{
            "id": 123,
            "tags": {"building": "yes"},
            "geometry": [
                {"lon": 36.805, "lat": -1.295},
                {"lon": 36.806, "lat": -1.295},
                {"lon": 36.806, "lat": -1.294},
                {"lon": 36.805, "lat": -1.295},
            ],
        }]
    }
    attempts = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return __import__("json").dumps(payload).encode("utf-8")

    def fake_urlopen(request, timeout):
        attempts.append(request.full_url)
        if "overpass-api.de" in request.full_url:
            raise HTTPError(request.full_url, 429, "Too Many Requests", {}, None)
        return FakeResponse()

    monkeypatch.setattr(frp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(frp.time, "sleep", lambda _: None)
    buildings = frp.fetch_osm_buildings(
        target,
        fallback_endpoints=("https://overpass.private.coffee/api/interpreter",),
    )

    assert attempts == [
        "https://overpass-api.de/api/interpreter",
        "https://overpass-api.de/api/interpreter",
        "https://overpass.private.coffee/api/interpreter",
    ]
    assert list(buildings.osm_id) == [123]


def test_folium_frame_converts_timestamp_and_numpy_properties_to_json():
    buildings = gpd.GeoDataFrame(
        [{
            "osm_id": np.int64(123),
            "building": "yes",
            "retrieved_utc": pd.Timestamp("2026-09-14T10:00:00Z"),
        }],
        geometry=[box(36.8, -1.3, 36.81, -1.29)],
        crs="EPSG:4326",
    )
    safe = frp.folium_safe_geodataframe(
        buildings, ("osm_id", "building", "retrieved_utc")
    )

    encoded = json.dumps(safe.__geo_interface__)
    assert "2026-09-14T10:00:00+00:00" in encoded
    assert '"osm_id": 123' in encoded


def test_final_interactive_map_exports_expected_layers_and_links(tmp_path):
    transform = Affine(500, 0, 250000, 0, -500, 9851500)

    def map_layer(name, values):
        data = xr.DataArray(
            np.asarray(values, dtype="float32"), dims=("y", "x"),
            coords={"y": [9851250.0, 9850750.0, 9850250.0],
                    "x": [250250.0, 250750.0, 251250.0]}, name=name,
        )
        return data.rio.write_crs("EPSG:32737").rio.write_transform(transform)

    layers = xr.Dataset({
        "hazard_score": map_layer("hazard_score", np.arange(9).reshape(3, 3) / 8),
        "planning_priority_score": map_layer(
            "planning_priority_score", np.arange(9).reshape(3, 3) / 8
        ),
        "candidate_affected_corridor": map_layer(
            "candidate_affected_corridor", [[0, 0, 0], [0, 1, 0], [0, 0, 0]]
        ),
    })
    aoi = gpd.GeoDataFrame(
        [{"name": "AOI", "zone": "Test"}],
        geometry=[box(249500, 9849500, 251500, 9851500)], crs="EPSG:32737",
    )
    points = gpd.GeoDataFrame(
        [{"name": "Target", "zone": "Test zone"}],
        geometry=[Point(250500, 9850500)], crs="EPSG:32737",
    )
    reaches = gpd.GeoDataFrame(
        [{"display_name": "River", "waterway": "river"}],
        geometry=[LineString([(249500, 9850500), (251500, 9850500)])],
        crs="EPSG:32737",
    )
    buildings = gpd.GeoDataFrame(
        [{"retrieved_utc": pd.Timestamp("2026-09-14T08:00:00Z")}],
        geometry=[box(250300, 9850300, 250400, 9850400)], crs="EPSG:32737",
    )
    intervals = gpd.GeoDataFrame(
        [{"inspection_area": "Target", "interval_label": "0-31 m",
          "distance_to_m": 31.0, "area_ha": 1.0}],
        geometry=[box(250000, 9850000, 251000, 9851000)], crs="EPSG:32737",
    )
    catchment = gpd.GeoDataFrame(
        [{"name": "South C", "area_km2": 1.0, "boundary_truncated": False}],
        geometry=[box(250000, 9850000, 251000, 9851000)], crs="EPSG:32737",
    )
    pour_point = gpd.GeoDataFrame(
        [{"name": "South C", "upstream_area_km2": 1.0, "snap_distance_m": 100}],
        geometry=[Point(250900, 9850500)], crs="EPSG:32737",
    )
    airport = gpd.GeoDataFrame(
        [{"name": "Wilson Airport reference"}],
        geometry=[Point(250100, 9850500)], crs="EPSG:32737",
    )
    outputs = frp.PCModelOutputs(
        aoi, layers, points, aoi, pd.DataFrame(), pd.DataFrame(), reaches, pd.DataFrame(),
        terrain_catchments=catchment, terrain_pour_points=pour_point,
        reference_points=airport,
    )

    result = frp.build_final_interactive_map(
        outputs, buildings=buildings, buffer_intervals=intervals
    )
    path = frp.export_interactive_map(result, tmp_path / "interactive.html")
    html = path.read_text(encoding="utf-8")

    assert path.exists()
    for label in (
        "Relative flood hazard", "Planning priority", "Candidate affected corridor",
        "OSM buildings", "Mapped drainage", "Topographic terrain", "Google Street View",
        "South C contributing catchment", "South C water outlet", "Wilson Airport reference",
    ):
        assert label in html


def test_complete_analysis_input_cache_round_trip(tmp_path):
    raw = xr.Dataset({
        "elevation": _layer(np.arange(9).reshape(3, 3), "elevation"),
        "worldcover": _layer(np.full((3, 3), 50), "worldcover"),
        "water_occurrence": _layer(np.zeros((3, 3)), "water_occurrence"),
        "extreme_rainfall_raw": _layer(np.ones((3, 3)), "extreme_rainfall_raw"),
    })
    config = frp.PCModelConfig()
    aoi = gpd.GeoDataFrame(
        [{"name": "AOI"}], geometry=[box(0, -30, 90, 60)], crs=config.crs
    )
    points = gpd.GeoDataFrame(
        [{"name": "Target", "zone": "Zone"}],
        geometry=[Point(45, 15)], crs=config.crs,
    )
    zones = gpd.GeoDataFrame(
        [{"name": "Target", "zone": "Zone"}],
        geometry=[box(0, -30, 90, 60)], crs=config.crs,
    )
    drainage = gpd.GeoDataFrame(
        [{"display_name": "River", "waterway": "river",
          "retrieved_utc": pd.Timestamp("2026-09-14T08:00:00Z")}],
        geometry=[LineString([(45, -30), (45, 60)])], crs=config.crs,
    )
    buildings = gpd.GeoDataFrame(
        [{"osm_id": 1}], geometry=[box(40, 10, 50, 20)], crs=config.crs
    )
    reaches = drainage.assign(inspection_area="Target")
    corridor = gpd.GeoDataFrame(
        [{"inspection_area": "Target", "buffer_m_each_side": 500}],
        geometry=[zones.geometry.iloc[0]], crs=config.crs,
    )
    intervals = gpd.GeoDataFrame(
        [{"inspection_area": "Target", "interval_label": "0-31 m",
          "distance_to_m": 31.0, "area_ha": 1.0}],
        geometry=[zones.geometry.iloc[0]], crs=config.crs,
    )
    sources = frp.PCSourceBundle(
        None, [], [], [], pd.DataFrame([{"collection": "test"}])
    )
    destination = tmp_path / "raw_data"

    paths = frp.export_analysis_input_cache(
        raw, sources, config, aoi, points, zones, drainage, buildings,
        reaches, corridor, intervals, destination,
    )
    restored = frp.load_analysis_input_cache(destination)

    assert len(paths) == 15
    assert frp.analysis_input_cache_ready(destination)
    assert set(restored.raw.data_vars) == set(raw.data_vars)
    assert len(restored.mapped_drainage) == 1
    assert len(restored.buildings) == 1
    assert restored.config.resolution_m == config.resolution_m

    # Re-exporting layers reopened from GeoTIFF must not fail when CF fields
    # such as _FillValue exist in both attrs and encoding.
    repacked = tmp_path / "raw_data_repacked"
    second_paths = frp.export_analysis_input_cache(
        restored.raw, restored.sources, restored.config,
        restored.aoi, restored.neighborhoods, restored.zones,
        restored.mapped_drainage, restored.buildings, restored.river_reaches,
        restored.river_corridor, restored.buffer_intervals, repacked,
    )
    assert len(second_paths) == 15
    assert frp.analysis_input_cache_ready(repacked)


def test_analysis_input_cache_rejects_all_nan_raster(tmp_path):
    raw = xr.Dataset({
        "elevation": _layer(np.arange(9).reshape(3, 3), "elevation"),
        "extreme_rainfall_raw": _layer(
            np.full((3, 3), np.nan), "extreme_rainfall_raw"
        ),
    })
    config = frp.PCModelConfig()
    area = gpd.GeoDataFrame(
        [{"name": "AOI", "zone": "Zone"}],
        geometry=[box(0, -30, 90, 60)], crs=config.crs,
    )
    points = gpd.GeoDataFrame(
        [{"name": "Target", "zone": "Zone"}],
        geometry=[Point(45, 15)], crs=config.crs,
    )
    lines = gpd.GeoDataFrame(
        [{"display_name": "River"}],
        geometry=[LineString([(45, -30), (45, 60)])], crs=config.crs,
    )
    sources = frp.PCSourceBundle(
        None, [], [], [], pd.DataFrame([{"collection": "test"}])
    )
    destination = tmp_path / "invalid_raw_data"

    frp.export_analysis_input_cache(
        raw, sources, config, area, points, area, lines, area.iloc[0:0],
        lines, area, area, destination,
    )

    assert not frp.analysis_input_cache_ready(destination)


def test_analysis_report_refreshes_tables_and_attachment_status(tmp_path):
    template = tmp_path / "report_template.md"
    template.write_text(
        "# Report\n\n{{AUTOMATED_RESULTS}}\n\n"
        "[Figure](exports/report_assets/figure_01_raw_inputs.png)\n"
        "[Manifest](raw_data/manifest.json)\n",
        encoding="utf-8",
    )
    export_dir = tmp_path / "exports"
    assets = export_dir / "report_assets"
    assets.mkdir(parents=True)
    (assets / "figure_01_raw_inputs.png").write_bytes(b"test-image")
    tables = {
        "table_09_neighborhood_summary.csv": pd.DataFrame([
            {"name": "Test area", "hazard_score_mean": 0.625}
        ])
    }

    report_path = frp.export_analysis_report(
        tables, frp.PCModelConfig(), template_path=template,
        output_path=export_dir / "Nairobi_Flood_Analysis_Report.md",
    )
    report = report_path.read_text(encoding="utf-8")

    assert "Test area" in report
    assert "0.625" in report
    assert "[Download the complete table](report_assets/table_09_neighborhood_summary.csv)" in report
    assert "| Raw-input figure | Available |" in report
    assert "(report_assets/figure_01_raw_inputs.png)" in report
    assert "(../raw_data/manifest.json)" in report
    assert "{{AUTOMATED_RESULTS}}" not in report
