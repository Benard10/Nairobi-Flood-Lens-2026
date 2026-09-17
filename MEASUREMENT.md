# Line and area measurement

The dashboard uses `@watergis/maplibre-gl-terradraw` 1.0.1 and its
`MaplibreMeasureControl`, following MapLibre's Terra Draw plugin integration.
The UMD script and stylesheet load from jsDelivr; no package installation or
notebook analysis changes are required. The version is pinned and tested with
the dashboard's MapLibre GL JS 4.7.1.

Controls appear at the map's upper right: expand/collapse, distance, area,
selection/edit and clear. Click points and press Enter to finish a line; click
the first corner to close a polygon. Distances use kilometres with three
decimal places; polygon units switch between square metres, hectares and square
kilometres. Measurements are geodesic surface estimates, not surveyed or
terrain-adjusted distances. They last for the current page session.

Drawing and editing suppress analysis popups and marker navigation. Returning
to the collapsed/static mode enables normal inspection. Measurement sources
and labels are carried across basemap styles alongside the analytical layers;
Terra Draw restores its drawing geometry through its adapter.

Focused browser validation:

```powershell
python tests/dashboard_browser_qa.py --measure-only
```

This exercises actual mouse clicks, a roughly 667 m line and 20 ha polygon,
label and shape persistence across satellite and street styles, and clearing.

References: [MapLibre plugin catalogue](https://maplibre.org/maplibre-gl-js/docs/plugins/),
[MapLibre Terra Draw example](https://maplibre.org/maplibre-gl-js/docs/examples/draw-geometries-with-terra-draw/),
[WaterGIS measurement example](https://terradraw.water-gis.com/examples/measure-control).
