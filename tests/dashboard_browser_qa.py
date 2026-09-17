"""Exercise the actual dashboard in installed headless Chrome, without installs.

Run manually: python tests/dashboard_browser_qa.py
Uses a temporary localhost server and closes Chrome and the server on exit.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import websocket

ROOT = Path(__file__).resolve().parents[1]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "maplibre_dashboard"), **kwargs)

    def log_message(self, *args):
        pass


def run():
    chrome = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe"
    if not chrome.exists():
        raise RuntimeError("Chrome is required for this optional browser check")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = tempfile.TemporaryDirectory(prefix="flood-dashboard-qa-")
    assert Path(profile.name).resolve().parent == Path(tempfile.gettempdir()).resolve()
    process = subprocess.Popen([
        str(chrome), "--headless=new", "--no-first-run", "--no-default-browser-check",
        "--enable-unsafe-swiftshader", "--use-angle=swiftshader", "--window-size=1600,1100",
        "--remote-debugging-port=0", f"--user-data-dir={profile.name}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    ws = None
    errors = []
    sequence = 0
    try:
        port_file = Path(profile.name) / "DevToolsActivePort"
        deadline = time.monotonic() + 20
        while not port_file.exists():
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("Headless Chrome did not start")
            time.sleep(.1)
        port = int(port_file.read_text().splitlines()[0])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json") as response:
            target = next(t for t in json.load(response) if t["type"] == "page")
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], suppress_origin=True, timeout=30)

        def call(method, params=None):
            nonlocal sequence
            sequence += 1
            request_id = sequence
            ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
            while True:
                event = json.loads(ws.recv())
                if event.get("method") == "Runtime.exceptionThrown":
                    errors.append(event["params"]["exceptionDetails"])
                if event.get("method") == "Runtime.consoleAPICalled" and event["params"]["type"] == "error":
                    descriptions = " ".join(arg.get("description", arg.get("value", "")) for arg in event["params"]["args"])
                    # Replaced styles deliberately abort requests for old tiles.
                    if not ("AbortError" in descriptions and "._remove" in descriptions and "._updateStyle" in descriptions):
                        errors.append(event["params"])
                if event.get("id") == request_id:
                    if "error" in event:
                        raise RuntimeError(event["error"])
                    return event.get("result", {})

        def evaluate(expression):
            result = call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
            if "exceptionDetails" in result:
                raise RuntimeError(result["exceptionDetails"])
            return result["result"].get("value")

        def wait_for(expression, timeout=60):
            deadline = time.monotonic() + timeout
            while not evaluate(expression):
                if time.monotonic() > deadline:
                    state = evaluate("typeof map === 'undefined' || !map ? null : ({missing:Object.values(layerIds).flat().filter(id=>!map.getLayer(id)),styleLoaded:map.isStyleLoaded(),sources:Object.fromEntries(Object.keys(map.getStyle().sources).map(id=>[id,map.isSourceLoaded(id)]))})")
                    raise AssertionError(f"Timed out: {expression}; state: {state}; errors: {errors[-3:]}")
                time.sleep(.2)

        call("Runtime.enable")
        call("Page.enable")
        call("Page.navigate", {"url": f"http://127.0.0.1:{server.server_port}/"})
        wait_for("typeof metrics !== 'undefined' && !!metrics")
        assert evaluate("document.querySelector('h1').textContent") == 'Understanding flood risk in Nairobi'
        if "--story-only" in sys.argv:
            wait_for("document.querySelectorAll('[data-story-area]').length === 6")
            assert evaluate("document.querySelector('#intro-discovery-summary').textContent.includes('10,641')")
            assert evaluate("document.querySelectorAll('[data-context-area]').length") == 6
            assert evaluate("document.querySelector('#study-selection').textContent.includes('1 km')")
            assert evaluate("document.querySelector('#rainfall-amount-detail').textContent.includes('125%')")
            assert evaluate("document.querySelector('#impact-history').textContent.includes('1997')")
            assert evaluate("document.querySelector('#story-road-watch').textContent.includes('Mombasa Road')")
            assert evaluate("document.querySelector('#intro-key-finding').textContent.includes('Kibera')")
            assert evaluate("document.querySelector('#intro-key-finding').textContent.includes('South B')")
            assert evaluate("document.querySelector('#intro-key-finding').textContent.includes('South C')")
            evaluate("document.querySelectorAll('.finding-visual img').forEach(img=>img.loading='eager'); true")
            wait_for("[...document.querySelectorAll('.finding-visual img')].every(img=>img.complete && img.naturalWidth>0)")
            dimensions = call("Page.getLayoutMetrics")["cssContentSize"]
            screenshot = call("Page.captureScreenshot", {"format":"png", "captureBeyondViewport":True,
                "clip":{"x":0,"y":0,"width":dimensions["width"],"height":dimensions["height"],"scale":1}})
            (ROOT / "exports/dashboard_story_qa.png").write_bytes(base64.b64decode(screenshot["data"]))
            evaluate("document.querySelector('[data-story-area=Kibera]').click()")
            wait_for("!!map && !!map.getLayer('building-fill')")
            assert evaluate("selectedArea") == "Kibera"
            assert evaluate("document.querySelector('#intro-view').hidden")
            assert evaluate("map.getFilter('building-fill')[2]") == "Kibera"
            if errors:
                raise AssertionError(f"Story browser errors: {errors}")
            print("PASS analysis-led story, six area discoveries, evidence images and story-to-map link; screenshot: exports/dashboard_story_qa.png", flush=True)
            return
        evaluate("document.querySelector('#open-dashboard').click()")
        ready = "!!map && Object.values(layerIds).flat().every(id => !!map.getLayer(id)) && ['hazard-layer-source','inspection','locations','buildings'].every(id=>map.isSourceLoaded(id))"
        wait_for(ready)
        if '--buildings-only' in sys.argv:
            colors = evaluate("map.getPaintProperty('building-fill','fill-color')")
            for theme in ['light', 'dark']:
                evaluate(f"applyTheme({json.dumps(theme)})")
                for area in ['all'] + evaluate("metrics.neighborhoods.map(row=>row.name)"):
                    evaluate(f"selectArea({json.dumps(area)})")
                    actual = evaluate("({review:document.querySelector('#overview-affected').textContent,other:document.querySelector('#overview-other').textContent,share:parseFloat(document.querySelector('#building-donut').style.getPropertyValue('--review-share')),gradient:getComputedStyle(document.querySelector('#building-donut')).backgroundImage,reviewColor:getComputedStyle(document.querySelector('.review-swatch')).backgroundColor,otherColor:getComputedStyle(document.querySelector('.screened-swatch')).backgroundColor})")
                    rows = evaluate("metrics.buffer_buildings.filter(row=>selectedArea==='all'||row.inspection_area===selectedArea)")
                    total = sum(row['osm_buildings_in_buffer'] for row in rows)
                    review = sum(row['osm_buildings_potentially_affected'] for row in rows)
                    assert int(actual['review'].replace(',', '')) == review
                    assert int(actual['other'].replace(',', '')) == total - review
                    assert abs(actual['share'] - (100 * review / total if total else 0)) < 1e-8
                    for swatch, color in [('reviewColor', colors[2]), ('otherColor', colors[3])]:
                        expected = 'rgb({}, {}, {})'.format(*(int(color[i:i+2], 16) for i in (1, 3, 5)))
                        assert actual[swatch] == expected
                        assert expected in actual['gradient']
            evaluate("selectArea('all')")
            (ROOT / 'exports/dashboard_building_chart_qa.png').write_bytes(base64.b64decode(call('Page.captureScreenshot', {'format':'png'})['data']))
            print('PASS building chart counts, proportions and map-matched colours for all six areas and both themes', flush=True)
            return
        if '--measure-only' in sys.argv:
            wait_for('!!measurementControl')
            assert evaluate('!!measurementActive()') is False
            evaluate("document.querySelector('.maplibregl-terradraw-add-linestring-button').click(); map.jumpTo({center:[36.85,-1.27],zoom:14}); true")
            assert evaluate('!!measurementActive()') is True
            box = evaluate("(()=>{const r=map.getCanvas().getBoundingClientRect();return {x:r.x,y:r.y}})()")
            def click_coordinate(coordinate):
                point = evaluate(f'map.project({json.dumps(coordinate)})')
                call('Input.dispatchMouseEvent',{'type':'mouseMoved','x':box['x']+point['x'],'y':box['y']+point['y']})
                for event_type in ['mousePressed','mouseReleased']:
                    call('Input.dispatchMouseEvent',{'type':event_type,'x':box['x']+point['x'],
                        'y':box['y']+point['y'],'button':'left','clickCount':1})
            click_coordinate([36.847,-1.27])
            click_coordinate([36.853,-1.27])
            for key_type in ['keyDown','keyUp']:
                call('Input.dispatchKeyEvent',{'type':key_type,'key':'Enter','code':'Enter','windowsVirtualKeyCode':13})
            wait_for("measurementControl.getFeatures().features.some(f=>f.geometry.type==='LineString' && f.properties.distance>0)")
            line = evaluate("measurementControl.getFeatures().features.find(f=>f.geometry.type==='LineString')")
            assert abs(line['properties']['distance']-.667)<.01, line
            assert evaluate("document.querySelectorAll('.maplibregl-popup-content').length") == 0
            evaluate("document.querySelector('.maplibregl-terradraw-add-polygon-button').click()")
            for coordinate in [[36.847,-1.269],[36.851,-1.269],[36.851,-1.273],[36.847,-1.273],[36.847,-1.269]]:
                click_coordinate(coordinate)
            wait_for("measurementControl.getFeatures().features.some(f=>f.geometry.type==='Polygon' && f.properties.area>0)")
            polygon = evaluate("measurementControl.getFeatures().features.find(f=>f.geometry.type==='Polygon')")
            assert polygon['properties']['unit']=='ha' and 19<polygon['properties']['area']<21, polygon
            drawn_layers = evaluate("map.getStyle().layers.filter(l=>l.source && ['geojson'].includes(map.getSource(l.source).type) && !Object.values(layerIds).flat().includes(l.id) && !l.id.startsWith('terradraw-measure')).map(l=>l.id)")
            assert drawn_layers, 'Drawing geometry has no map layers'
            before = evaluate('measurementControl.getTerraDrawInstance().getSnapshot().length')
            for style in ['satellite','outdoors']:
                evaluate(f'changeBasemap({json.dumps(style)})')
                wait_for(ready)
                assert evaluate('measurementControl.getTerraDrawInstance().getSnapshot().length') == before
                wait_for("!!map.getLayer('terradraw-measure-polygon-label')")
                assert evaluate("map.queryRenderedFeatures({layers:['terradraw-measure-line-node']}).length > 0")
                assert evaluate(f"map.queryRenderedFeatures({{layers:{json.dumps(drawn_layers)}}}).some(f=>f.geometry.type==='Polygon')")
            evaluate("document.querySelector('.maplibregl-terradraw-delete-button').click()")
            wait_for('measurementControl.getTerraDrawInstance().getSnapshot().length === 0')
            if errors:
                raise AssertionError(f'Measurement errors: {errors}')
            print('PASS real line/area drawing, metric values, popup suppression, basemap persistence and clear',flush=True)
            return
        if '--tiles-only' in sys.argv:
            records = json.loads((ROOT / 'maplibre_dashboard/data/buildings.geojson').read_text())['features']
            sample = next(f for f in records if f['properties']['inspection_area']=='South B' and f['properties']['potentially_affected'])
            ring = sample['geometry']['coordinates'][0] if sample['geometry']['type']=='Polygon' else sample['geometry']['coordinates'][0][0]
            centre = [sum(c[i] for c in ring)/len(ring) for i in range(2)]
            evaluate(f"selectArea('South B'); map.jumpTo({{center:{json.dumps(centre)},zoom:15}}); true")
            wait_for("map.isSourceLoaded('buildings') && map.queryRenderedFeatures({layers:['building-fill']}).length > 0")
            point = evaluate("(()=>{for(const f of map.queryRenderedFeatures({layers:['building-fill']})){if(!f.properties.potentially_affected)continue;const c=f.geometry.type==='Polygon'?f.geometry.coordinates[0]:f.geometry.coordinates[0][0];const lngLat={lng:(c[0][0]+c[1][0]+c[2][0])/3,lat:(c[0][1]+c[1][1]+c[2][1])/3};const point=map.project(lngLat);if(point.x<20||point.y<60||point.x>map.getCanvas().clientWidth-20||point.y>map.getCanvas().clientHeight-60)continue;if(map.queryRenderedFeatures(point,{layers:['building-fill']}).some(f=>f.properties.potentially_affected))return point;}throw Error('No visible flagged tile footprint to click');})()")
            box = evaluate("(()=>{const r=map.getCanvas().getBoundingClientRect();return {x:r.x,y:r.y}})()")
            for event_type in ['mousePressed','mouseReleased']:
                call('Input.dispatchMouseEvent',{'type':event_type,'x':box['x']+point['x'],
                    'y':box['y']+point['y'],'button':'left','clickCount':1})
            wait_for("!!document.querySelector('.maplibregl-popup-content')")
            assert evaluate("document.querySelector('.maplibregl-popup-content').textContent.includes('Building needing closer review')")
            assert evaluate("document.querySelector('.maplibregl-popup-content').textContent.includes('Mapped river corridor')")
            evaluate("showLookout('tmall')")
            assert evaluate("getComputedStyle(document.querySelector('.lookout-note')).fontSize") == '10px'
            assert evaluate("lookoutMarkers.every(m=>!m.getElement().textContent.includes('?'))")
            if errors:
                raise AssertionError(f'Tiled map errors: {errors}')
            print('PASS tiled building rendering, classification and clicked popup; heading, marker symbols and small context note',flush=True)
            return
        assert evaluate("lookoutMarkers.length") == 6
        assert evaluate("lookoutMarkers.every(m=>!m.getElement().textContent.includes('?'))")
        assert evaluate("map.getSource('buildings').type") == 'vector'
        assert evaluate("map.getLayer('building-fill').sourceLayer") == 'buildings'
        assert evaluate("lookoutMarkers.every(m=>m.getElement().classList.contains('animate-lookout') && !!m.getElement().querySelector('svg'))")
        assert evaluate("getComputedStyle(lookoutMarkers[0].getElement()).width") == '21px'
        assert evaluate("!document.querySelector('#show-lookouts') && !document.querySelector('#animate-lookouts')")
        for area in evaluate("metrics.neighborhoods.map(row=>row.name)"):
            evaluate(f"showStudyPopup({json.dumps(area)})")
            assert evaluate("!document.querySelector('#study-analysis-panel').hidden && document.querySelector('#study-history-panel').hidden")
            evaluate("document.querySelector('#study-history-tab').click()")
            assert evaluate("document.querySelector('#study-analysis-panel').hidden && !document.querySelector('#study-history-panel').hidden")
            assert evaluate("document.querySelector('#study-history-panel').textContent.includes(preparedness.areas.find(a=>a.name===selectedArea).history)")
            assert evaluate("!!document.querySelector('#study-history-panel a[href]')")
            evaluate("document.querySelector('#study-history-tab').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}))")
            assert evaluate("!document.querySelector('#study-analysis-panel').hidden && document.querySelector('#study-history-panel').hidden")
        print('PASS six smaller animated road warning symbols and both study-popup tabs, sources and keyboard navigation', flush=True)
        evaluate("showLookout('cabanas')")
        assert evaluate("document.querySelector('.maplibregl-popup-content').textContent.includes('2026-05-01')")
        assert evaluate("document.querySelector('.maplibregl-popup-content').textContent.includes('Outside the six study buffers')")
        evaluate("lookoutPopup.remove(); selectArea('all')")
        assert evaluate("document.querySelectorAll('[data-study-area]').length") == 6
        evaluate("for (const input of document.querySelectorAll('[data-layer-key]')) { const before=input.checked; input.click(); for(const id of layerIds[input.dataset.layerKey]) {if(map.getLayoutProperty(id,'visibility') !== (input.checked ? 'visible' : 'none')) throw Error('Unwired control: '+id)} input.click(); if(input.checked!==before)throw Error('Toggle state lost') }")
        print("PASS all 12 layer controls", flush=True)
        evaluate("selectArea('Mathare Valley'); const c=document.querySelector('[data-layer-key=buffer_intervals]'); c.checked=true; c.dispatchEvent(new Event('change',{bubbles:true}))")
        for basemap in ["satellite", "outdoors", "standard", "satellite", "standard"]:
            evaluate(f"document.querySelector('#basemap-select').value={json.dumps(basemap)}; document.querySelector('#basemap-select').dispatchEvent(new Event('change'))")
            wait_for(ready)
            assert evaluate("map.getFilter('building-fill')[2]") == "Mathare Valley"
            assert evaluate("map.getLayoutProperty('buffer-interval-fill','visibility')") == "visible"
            assert evaluate("map.getLayoutProperty('hazard-layer','visibility')") == "visible"
            assert evaluate("document.querySelectorAll('.lookout-marker').length") == 6
            assert evaluate("map.getStyle().layers.findIndex(l=>l.id==='hazard-layer') > map.getStyle().layers.findIndex(l=>l.id==='background' || l.id==='esri-satellite-layer')")
            print(f"PASS basemap {basemap}: overlays, selection, visibility and order", flush=True)
        evaluate("document.querySelector('#theme-toggle').click()")
        wait_for(ready)
        assert evaluate("selectedArea") == "Mathare Valley"
        evaluate("document.querySelector('[data-layer-key=buildings]').click()")
        assert evaluate("map.getLayoutProperty('building-fill','visibility')") == "none"
        evaluate("changeBasemap('outdoors')")
        wait_for(ready)
        assert evaluate("map.getLayoutProperty('building-fill','visibility')") == "none"
        # Multiple choices while a style loads must eventually show the last choice.
        evaluate("for (const key of ['satellite','outdoors','satellite']) {document.querySelector('#basemap-select').value=key;document.querySelector('#basemap-select').dispatchEvent(new Event('change'))}")
        wait_for(ready + " && !!map.getSource('esri-satellite')")
        assert evaluate("map.getLayoutProperty('building-fill','visibility')") == "none"
        evaluate("document.querySelector('#reset-filters').click()")
        wait_for(ready)
        assert evaluate("selectedArea") == "all"
        assert evaluate("map.getFilter('building-fill') == null")
        assert evaluate("map.getLayoutProperty('building-fill','visibility')") == "visible"
        assert evaluate("map.getLayoutProperty('buffer-interval-fill','visibility')") == "none"
        print("PASS theme, hidden-layer persistence, rapid basemap changes and reset", flush=True)
        evaluate("document.querySelector('[data-view-target=intro]').click(); document.querySelector('[data-view-target=dashboard]').click()")
        wait_for(ready)
        wait_for("!map.isMoving()")
        (ROOT / "exports/dashboard_qa_overview.png").write_bytes(base64.b64decode(call("Page.captureScreenshot", {"format": "png"})["data"]))
        # Exercise real point hit detection and the linked popup.
        coordinate = evaluate("locationsData.features.find(f=>f.properties.name==='South B').geometry.coordinates")
        evaluate(f"selectArea('South B'); map.jumpTo({{center:{json.dumps(coordinate)},zoom:14}}); true")
        wait_for(ready)
        wait_for(f"map.queryRenderedFeatures(map.project({json.dumps(coordinate)}), {{layers:['neighborhood-points']}}).some(f=>f.properties.name==='South B')")
        point = evaluate(f"map.project({json.dumps(coordinate)})")
        box = evaluate("(()=>{const r=map.getCanvas().getBoundingClientRect();return {x:r.x,y:r.y}})()")
        for event_type in ["mousePressed", "mouseReleased"]:
            call("Input.dispatchMouseEvent", {"type": event_type, "x": box["x"] + point["x"],
                "y": box["y"] + point["y"], "button": "left", "clickCount": 1})
        wait_for("!!document.querySelector('.maplibregl-popup-content')")
        assert evaluate("document.querySelector('#study-analysis-panel').textContent.includes('Average inspection priority')")
        assert evaluate("document.querySelector('#study-analysis-panel').textContent.includes('South B')")
        artifact = ROOT / "exports/dashboard_qa.png"
        artifact.write_bytes(base64.b64decode(call("Page.captureScreenshot", {"format": "png"})["data"]))
        if errors:
            raise AssertionError(f"Browser errors: {json.dumps(errors, default=str)[:3000]}")
        print("PASS theme, layer toggle, reset, view navigation and real map popup; screenshot:", artifact, flush=True)
    finally:
        if ws:
            try:
                ws.send(json.dumps({"id": sequence + 1, "method": "Browser.close"}))
            except websocket.WebSocketException:
                pass
            ws.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        try:
            profile.cleanup()
        except PermissionError:
            pass  # Chrome child processes may briefly retain profile handles.


if __name__ == "__main__":
    run()
