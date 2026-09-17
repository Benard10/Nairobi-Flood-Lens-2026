"""Check tiled feature retention, attributes, bounds and static-server coverage."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_maplibre_dashboard as builder
import build_dashboard_tiles as tiles
import geopandas as gpd
import mercantile
import pyogrio

manifest = json.loads((builder.DATA / 'vector_tiles.json').read_text())
for name, info in manifest.items():
    directory = builder.DATA / Path(info['tiles']).parts[1] / Path(info['tiles']).parts[2]
    reference = gpd.read_file(builder.DATA / tiles.LAYERS[name])
    observed = {}
    for path in (directory / str(info['maxzoom'])).rglob('*.pbf'):
        if not path.stat().st_size:
            continue
        frame = pyogrio.read_dataframe(path, layer=name)
        for record in frame.drop(columns='geometry').to_dict('records'):
            feature_id = int(record['tile_feature_id'])
            if name == 'buildings':
                original = reference.iloc[feature_id]
                assert bool(record['potentially_affected']) == bool(original['potentially_affected'])
                assert record['inspection_area'] == original['inspection_area']
                assert record['screening_method'] == original['screening_method']
            observed[feature_id] = record
    assert set(observed) == set(range(len(reference))), f'{name}: lost features {set(range(len(reference))) - set(observed)}'
    for zoom in range(info['minzoom'], info['maxzoom']+1):
        for tile in mercantile.tiles(*info['bounds'], zooms=zoom):
            assert (directory / str(tile.z) / str(tile.x) / f'{tile.y}.pbf').exists()
    print(f'PASS {name}: all {len(observed):,} feature IDs retained; tile coverage complete', flush=True)
print('PASS vector tiles and building classifications')
