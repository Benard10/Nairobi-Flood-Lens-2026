"""Build static XYZ vector tiles from dashboard display exports using installed GDAL."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import geopandas as gpd
import mercantile
import pyogrio

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'maplibre_dashboard/data'
LAYERS = {'buildings': 'buildings.geojson', 'analysis-rivers': 'analysis_rivers.geojson',
          'buffer-intervals': 'river_buffer_intervals.geojson'}


def build_tiles():
    """Keep analytical GeoJSON intact; version tile directories by input content."""
    manifest = {}
    pyogrio.set_gdal_config_options({'GDAL_NUM_THREADS': '2'})
    for name, filename in LAYERS.items():
        source = DATA / filename
        digest = hashlib.sha256(source.read_bytes() + b'xyz-v1-10-15-16384' +
            (b'exploded-bands-v2' if name == 'buffer-intervals' else b'')).hexdigest()[:16]
        destination = DATA / 'tiles' / f'{name}-{digest}'
        destination.parent.mkdir(parents=True, exist_ok=True)
        frame = gpd.read_file(source)
        original_count = len(frame)
        if name == 'analysis-rivers':
            counts = frame['display_name'].value_counts()
            (DATA / 'river_inventory.json').write_text(json.dumps({'count':len(frame),
                'names':[{'name':str(key),'count':int(value)} for key,value in counts.items()]}), encoding='utf-8')
        # Stable identifiers allow checking features split across tile boundaries.
        frame['tile_feature_id'] = range(len(frame))
        if name == 'buffer-intervals':
            # Encode narrow disconnected band polygons separately. Quantisation
            # of a complex multipart ring can otherwise discard the whole record.
            frame = frame.explode(index_parts=False, ignore_index=True)
        bounds = list(map(float, frame.total_bounds))
        if not (destination / 'complete.json').exists():
            if destination.exists():
                # Retain an interrupted output for inspection and start clean.
                destination.rename(destination.with_name(f'{destination.name}-incomplete-{uuid.uuid4().hex[:8]}'))
            pyogrio.write_dataframe(frame, destination, driver='MVT', layer=name,
                dataset_options={'MINZOOM':'10', 'MAXZOOM':'15', 'EXTENT':'16384',
                    'SIMPLIFICATION':'1', 'SIMPLIFICATION_MAX_ZOOM':'0',
                    'COMPRESS':'NO', 'MAX_SIZE':'50000000', 'MAX_FEATURES':'1000000'})
            # Static servers cannot return empty PBF for a missing tile. Materialise
            # holes inside source bounds so panning never produces local 404 errors.
            for zoom in range(10, 16):
                for tile in mercantile.tiles(*bounds, zooms=zoom):
                    target = destination / str(tile.z) / str(tile.x) / f'{tile.y}.pbf'
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(b'')
            (destination / 'complete.json').write_text(json.dumps({'features':original_count}))
        sizes = [p.stat().st_size for p in destination.rglob('*.pbf')]
        manifest[name] = {'tiles':f'data/tiles/{destination.name}/{{z}}/{{x}}/{{y}}.pbf',
            'source_layer':name, 'minzoom':10, 'maxzoom':15, 'bounds':bounds,
            'feature_count':original_count, 'source_bytes':source.stat().st_size,
            'tile_count':len(sizes), 'largest_tile_bytes':max(sizes), 'total_tile_bytes':sum(sizes)}
        print(f'{name}: {original_count:,} features, {len(sizes):,} tiles, largest {max(sizes):,} bytes', flush=True)
    (DATA / 'vector_tiles.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest


if __name__ == '__main__':
    # Select the same PROJ database as the main export builder.
    import build_maplibre_dashboard
    build_tiles()
