const THEME_KEY = 'nairobi-flood-dashboard-theme';
const NAIROBI_BOUNDS = [[36.65, -1.45], [37.11, -1.15]];
let map;
let metrics;
let locationsData;
let dataVersion;
let inspectionData;
let riversData;
let selectedArea = 'all';
let mapLoadError = false;
let preparedness;
let lookoutPoints = [];
let lookoutMarkers = [];
let lookoutPopup;
let vectorTiles;
let measurementControl;

function measurementActive() {
  const draw = measurementControl?.getTerraDrawInstance();
  return draw && ['linestring','polygon','select'].includes(draw.getMode());
}

function initMeasurement() {
  const status = document.querySelector('#measurement-status');
  if (typeof MaplibreTerradrawControl === 'undefined' || !MaplibreTerradrawControl.MaplibreMeasureControl) {
    status.textContent = 'Measurement tools could not load. Check your connection and refresh.';
    return;
  }
  measurementControl = new MaplibreTerradrawControl.MaplibreMeasureControl({
    modes:['render','linestring','polygon','select','delete'], open:true,
    distanceUnit:'kilometers', distancePrecision:3, areaUnit:'metric', areaPrecision:2,
  });
  map.addControl(measurementControl, 'top-right');
  status.textContent = 'Line: distance · Polygon: area';
  const controls = document.querySelector('.maplibregl-ctrl-top-right');
  const names = {'linestring':'Measure distance', 'polygon':'Measure area', 'select':'Edit measurement'};
  for (const [mode, label] of Object.entries(names)) {
    const button = controls.querySelector(`.maplibregl-terradraw-add-${mode}-button`);
    button.title = label;
    button.setAttribute('aria-label', label);
  }
  const sync = () => document.querySelector('#map').classList.toggle('measurement-active', Boolean(measurementActive()));
  controls.addEventListener('click', () => queueMicrotask(sync));
  document.addEventListener('keyup', sync);
  measurementControl.getTerraDrawInstance().on('change', sync);
}

const layerIds = {
  hazard: ['hazard-layer'], priority: ['priority-layer'], candidate: ['candidate-layer'],
  buildings: ['building-fill'],
  drainage: ['drainage-lines'], catchment: ['catchment-fill', 'catchment-line'],
  analysis_rivers: ['analysis-river-lines'],
  buffer_intervals: ['buffer-interval-fill', 'buffer-interval-line'],
  dams: ['dam-fill', 'dam-line'],
  river_corridor: ['river-corridor-fill', 'river-corridor-line'],
  inspection: ['inspection-fill', 'inspection-line'],
  locations: ['neighborhood-points', 'neighborhood-label'],
};
const styles = {
  standard: 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json',
  dark: 'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
  satellite: {
    version: 8,
    glyphs: 'https://tiles.basemaps.cartocdn.com/fonts/{fontstack}/{range}.pbf',
    sources: {
      'esri-satellite': {
        type: 'raster',
        tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
        tileSize: 256,
        attribution: 'Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community',
      },
    },
    layers: [{ id: 'esri-satellite-layer', type: 'raster', source: 'esri-satellite' }],
  },
  outdoors: 'https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json',
};

const currentTheme = () => document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
const selectedMapStyle = key => key === 'standard' && currentTheme() === 'dark'
  ? styles.dark
  : (styles[key] || styles.standard);

function preserveAnalysisStyle(previous, next) {
  const ids = new Set(Object.values(layerIds).flat());
  const layers = (previous?.layers || []).filter(layer => ids.has(layer.id) || /^(td-|terradraw)/.test(layer.id));
  const sources = { ...next.sources };
  for (const layer of layers) {
    if (previous.sources[layer.source]) sources[layer.source] = previous.sources[layer.source];
  }
  return {
    ...next,
    sources,
    layers: [...next.layers.filter(layer => !ids.has(layer.id)), ...layers],
  };
}

function changeBasemap(key) {
  if (!map) return;
  mapLoadError = false;
  // A full style load has a reliable completion event. Carry overlays forward
  // as well, keeping their filters, visibility and draw order above the basemap.
  map.setStyle(selectedMapStyle(key), {
    diff: false,
    transformStyle: preserveAnalysisStyle,
  });
}

function applyTheme(theme, save = true) {
  const selected = theme === 'light' ? 'light' : 'dark';
  document.documentElement.dataset.theme = selected;
  const dark = selected === 'dark';
  document.querySelector('#theme-icon').textContent = dark ? '☀' : '☾';
  document.querySelector('#theme-label').textContent = dark ? 'Light' : 'Dark';
  document.querySelector('#theme-toggle').setAttribute('aria-label', `Switch to ${dark ? 'light' : 'dark'} theme`);
  if (save) {
    try { localStorage.setItem(THEME_KEY, selected); } catch { /* Browser storage may be disabled. */ }
  }
  if (map && document.querySelector('#basemap-select').value === 'standard') {
    changeBasemap('standard');
  }
}

const number = (value, digits = 1) => Number(value).toLocaleString(undefined, {
  minimumFractionDigits: digits, maximumFractionDigits: digits
});

async function loadDashboard() {
  [metrics, locationsData, inspectionData, riversData, preparedness, vectorTiles] = await Promise.all([
    fetch('data/dashboard_metrics.json', { cache: 'no-store' }).then(response => {
      if (!response.ok) throw new Error('Dashboard data could not be loaded. Run build_maplibre_dashboard.py.');
      return response.json();
    }),
    fetch('data/neighborhoods.geojson', { cache: 'no-store' }).then(response => response.json()),
    fetch('data/inspection_zones.geojson', { cache: 'no-store' }).then(response => {
      if (!response.ok) throw new Error('Study buffers could not be loaded. Rebuild the dashboard assets.');
      return response.json();
    }),
    fetch('data/river_inventory.json', { cache: 'no-store' }).then(response => {
      if (!response.ok) throw new Error('Analysis rivers could not be loaded. Rebuild the dashboard assets.');
      return response.json();
    }),
    fetch('data/preparedness_context.json', { cache: 'no-store' }).then(response => {
      if (!response.ok) throw new Error('Preparedness history could not be loaded. Rebuild the dashboard assets.');
      return response.json();
    }),
    fetch('data/vector_tiles.json', {cache:'no-store'}).then(response => {
      if (!response.ok) throw new Error('Map tiles are missing. Rebuild the dashboard assets.');
      return response.json();
    }),
  ]);
  dataVersion = metrics.generated_at;
  renderStats();
  renderFindings();
  renderForecastNarrative();
  renderPreparedness();
  renderBufferBuildings();
  renderLayerControls();
  showAreaStory('all');
  renderStudyAreas();
  if (!document.querySelector('#dashboard-view').hidden && !map) initMap();
}

function contextSource(key) {
  const source = preparedness.sources[key];
  return `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.name)}${source.author ? ` · ${escapeHtml(source.author)}` : ''} ↗</a>`;
}

function renderPreparedness() {
  document.querySelector('#study-selection').textContent =
    'We selected six study areas to compare how risk builds up in different parts of Nairobi: Mathare Valley, Korogocho, Gikomba Market, South C, South B and Kibera. Each one sits inside a 1 km review buffer, which helps us compare patterns without treating the point as the exact place where flooding occurred.';
  document.querySelector('#study-drivers').innerHTML = `${escapeHtml('The pattern is not the same in every neighbourhood. Low ground, river and drain proximity, surface-water history, rainfall patterns and building density together explain why some places need more attention than others.') } <span class="source-links">${contextSource('wri')}</span>`;
  document.querySelector('#study-context-areas').innerHTML = preparedness.areas.map(area => `
    <section class="context-area"><h3>${escapeHtml(area.name)}</h3><p>${escapeHtml(area.reason)}</p>
    <p>${escapeHtml(area.history)}</p><div class="source-links">${contextSource(area.source)}</div>
    <button type="button" class="text-button" data-context-area="${escapeHtml(area.name)}">Highlight study buffer →</button></section>`).join('');
  document.querySelectorAll('[data-context-area]').forEach(button => button.addEventListener('click', () => {
    switchView('dashboard'); selectArea(button.dataset.contextArea);
  }));
  document.querySelector('#impact-history').innerHTML = preparedness.history.map(item => `
    <section class="context-area"><div class="eyebrow">${escapeHtml(item.period)}</div><h3>${escapeHtml(item.title)}</h3>
    <p>${escapeHtml(item.detail)}</p><div class="source-links">${contextSource(item.source)}</div></section>`).join('');
  document.querySelector('#rainfall-amount-title').textContent = preparedness.rainfall.title;
  document.querySelector('#rainfall-amount-detail').textContent = preparedness.rainfall.detail;
  document.querySelector('#rainfall-preparation').textContent = preparedness.rainfall.action;
  document.querySelector('#rainfall-amount-source').innerHTML = contextSource(preparedness.rainfall.source);
  lookoutPoints = preparedness.watch_points;
  document.querySelector('#story-road-watch').innerHTML = preparedness.watch_points.map(point => `
    <section class="context-area"><div class="eyebrow">${escapeHtml(point.date)}</div><h3>${escapeHtml(point.name)}</h3>
    <p>${escapeHtml(point.detail)}</p><p class="table-note">${escapeHtml(point.precision)}</p>
    <div class="source-links">${contextSource(point.source)}${point.additional_source ? contextSource(point.additional_source) : ''}</div>
    <button type="button" class="text-button" data-lookout="${escapeHtml(point.id)}">Show lookout on map →</button></section>`).join('');
  document.querySelector('#map-lookout-list').innerHTML = lookoutPoints.map(point => `
    <button type="button" class="lookout-list-button" data-lookout="${escapeHtml(point.id)}">${escapeHtml(point.name)}</button>`).join('');
  document.querySelectorAll('[data-lookout]').forEach(button => button.addEventListener('click', () => showLookout(button.dataset.lookout)));
}

function lookoutDetails(point) {
  return `<strong>${escapeHtml(point.name)}</strong><p>${escapeHtml(point.kind)} · ${escapeHtml(point.date)}</p>
    <p>${escapeHtml(point.detail)}</p><p>${escapeHtml(point.precision)}</p>
    <div class="source-links">${contextSource(point.source)}${point.additional_source ? contextSource(point.additional_source) : ''}
    ${point.coordinate_source ? `<a href="${escapeHtml(point.coordinate_source)}" target="_blank" rel="noopener noreferrer">Landmark location source ↗</a>` : ''}</div>
    <p class="lookout-note">Historical reports and inspection priorities. No live alerts.</p>`;
}

function syncLookouts() {
  for (const marker of lookoutMarkers) {
    marker.getElement().classList.add('animate-lookout');
  }
}

function addLookouts() {
  for (const marker of lookoutMarkers) marker.remove();
  lookoutMarkers = lookoutPoints.map(point => {
    const element = document.createElement('button');
    element.type = 'button';
    element.className = 'lookout-marker reported-lookout';
    element.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2 23 21H1Z" fill="#b91c1c" stroke="white" stroke-width="1.5" stroke-linejoin="round"/><path d="M12 8v6" stroke="white" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="17" r="1" fill="white"/></svg>';
    element.setAttribute('aria-label', `${point.name}: ${point.kind}`);
    element.title = `${point.name} · ${point.kind} · not a live alert`;
    element.addEventListener('click', event => { event.stopPropagation(); if (!measurementActive()) showLookout(point.id); });
    return new maplibregl.Marker({element}).setLngLat(point.coordinates).addTo(map);
  });
  syncLookouts();
}

function showLookout(id) {
  if (id.startsWith('study-')) {
    switchView('dashboard');
    if (map) showStudyPopup(id.slice(6));
    return;
  }
  const point = lookoutPoints.find(item => item.id === id);
  if (!point) return;
  switchView('dashboard');
  if (!map) return;
  syncLookouts();
  // Historical access points remain visible independently of area filtering.
  map.resize();
  map.flyTo({center: point.coordinates, zoom: 14, duration: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 800});
  lookoutPopup?.remove();
  lookoutPopup = new maplibregl.Popup({maxWidth:'340px'}).setLngLat(point.coordinates).setHTML(lookoutDetails(point)).addTo(map);
}

function showStudyPopup(name) {
  const location = locationsData.features.find(feature => feature.properties.name === name);
  if (!location) return;
  selectArea(name);
  const context = preparedness.areas.find(area => area.name === name);
  const content = document.createElement('div');
  content.className = 'study-popup';
  content.innerHTML = `<div class="study-popup-tabs" role="tablist" aria-label="${escapeHtml(name)} information">
    <button type="button" role="tab" id="study-analysis-tab" aria-controls="study-analysis-panel" aria-selected="true">Analysis</button>
    <button type="button" role="tab" id="study-history-tab" aria-controls="study-history-panel" aria-selected="false" tabindex="-1">History &amp; checks</button></div>
    <div role="tabpanel" id="study-analysis-panel" aria-labelledby="study-analysis-tab">${areaDetails(name)}</div>
    <div role="tabpanel" id="study-history-panel" aria-labelledby="study-history-tab" hidden><strong>${escapeHtml(name)}</strong>
    <p>${escapeHtml(context?.history || 'No local history is available.')}</p><p>${escapeHtml(context?.reason || '')}</p>
    <div class="source-links">${context ? contextSource(context.source) : ''}</div></div>`;
  const tabs = [...content.querySelectorAll('[role=tab]')];
  const activate = tab => {
    tabs.forEach(item => {
      const active = item === tab;
      item.setAttribute('aria-selected', String(active));
      item.tabIndex = active ? 0 : -1;
      content.querySelector(`#${item.getAttribute('aria-controls')}`).hidden = !active;
    });
  };
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => activate(tab));
    tab.addEventListener('keydown', event => {
      if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
      event.preventDefault();
      const next = tabs[event.key === 'Home' ? 0 : event.key === 'End' ? 1 : 1-index];
      activate(next); next.focus();
    });
  });
  lookoutPopup?.remove();
  lookoutPopup = new maplibregl.Popup({maxWidth:'340px'}).setLngLat(location.geometry.coordinates).setDOMContent(content).addTo(map);
}

function renderForecastNarrative() {
  const forecast = metrics.forecast_2026;
  if (!forecast) return;
  document.querySelector('#forecast-summary').textContent = forecast.plain_summary;
  document.querySelector('#regional-context').textContent = forecast.regional_context;
  document.querySelector('#history-timeline').innerHTML = forecast.history_breakdown.map(item => `
    <div><strong>${item.period}</strong><h3>${item.title}</h3><p>${item.detail}</p></div>
  `).join('');
  document.querySelector('#outlook-facts').innerHTML = forecast.outlook_facts.map(fact => `
    <div><span>${fact.label}</span><strong>${fact.value}</strong><p>${fact.detail}</p></div>
  `).join('');

  const buffer = forecast.buffer_method;
  document.querySelector('#buffer-warning').textContent = buffer.warning;
  const model = forecast.selected_model;
  document.querySelector('#dashboard-model-status').textContent = model.current_status;
  document.querySelector('#forecast-sources').innerHTML = forecast.sources.map(source => `
    <a href="${source.url}" target="_blank" rel="noreferrer"><b>${source.name}</b><span>${source.issued}</span></a>
  `).join('');
}

function renderStats() {
  const h = metrics.headline;
  const s = metrics.south_c;
  document.querySelector('#catchment-area').textContent = number(s.catchment_area_km2, 2);
  document.querySelector('#catchment-high').textContent = `${number(s.high_hazard_area_pct, 1)}%`;
  document.querySelector('#catchment-progress').style.width = `${s.high_hazard_area_pct}%`;
  document.querySelector('#airport-upstream').textContent = s.wilson_airport_upstream ? 'Yes' : 'No';
  document.querySelector('#outlet-notice').hidden = !s.outlet_needs_review;
  const sorted = [...metrics.neighborhoods].sort((a, b) => b.hazard_score_mean - a.hazard_score_mean);
  document.querySelector('#area-select').insertAdjacentHTML('beforeend', sorted.map(row =>
    `<option value="${row.name}">${row.name}</option>`).join(''));
  const reviewed = (metrics.buffer_buildings || []).reduce(
    (sum, row) => sum + Number(row.osm_buildings_in_buffer || 0), 0
  );
  document.querySelector('#intro-area-count').textContent = h.inspection_areas;
  document.querySelector('#intro-building-count').textContent = number(reviewed, 0);
  renderAreaStatistics('all');
}

function renderFindings() {
  const buildingRows = metrics.buffer_buildings || [];
  const areaBuildings = new Map();
  buildingRows.forEach(row => {
    const current = areaBuildings.get(row.inspection_area) || {screened: 0, review: 0};
    current.screened += Number(row.osm_buildings_in_buffer || 0);
    current.review += Number(row.osm_buildings_potentially_affected || 0);
    areaBuildings.set(row.inspection_area, current);
  });
  const buildingAreas = [...areaBuildings].map(([name, values]) => ({
    name, ...values, share: values.screened ? 100 * values.review / values.screened : 0,
  }));
  const screened = buildingAreas.reduce((sum, row) => sum + row.screened, 0);
  const review = buildingAreas.reduce((sum, row) => sum + row.review, 0);
  const largestCount = [...buildingAreas].sort((a, b) => b.review - a.review)[0];
  const largestShare = [...buildingAreas].sort((a, b) => b.share - a.share)[0];
  const highestAverage = [...metrics.neighborhoods].sort(
    (a, b) => Number(b.hazard_score_mean) - Number(a.hazard_score_mean)
  )[0];
  const market = buildingAreas.find(row => row.name === 'Gikomba Market');
  const highArea = Number(metrics.headline.high_hazard_area_ha || 0);
  const corridorArea = Number(metrics.headline.candidate_corridor_area_ha || 0);
  const dams = Number(metrics.headline.mapped_dams || 0);
  renderDiscoveryStory(buildingAreas);

  document.querySelector('#finding-area-badge').textContent =
    `${metrics.headline.inspection_areas} selected areas · ${number(dams, 0)} mapped water bodies`;
  document.querySelector('#finding-environment-number').textContent = `${number(highArea, 0)} ha`;
  document.querySelector('#finding-environment-text').textContent =
    `${number(highArea, 0)} hectares in the six study areas have higher flood-concern scores. Checks near rivers identify ${number(corridorArea, 0)} hectares needing closer review. The analysis also uses ${number(dams, 0)} mapped dams and water bodies to understand nearby drainage.`;
  document.querySelector('#finding-disaster-number').textContent = `${number(review, 0)} building records`;
  document.querySelector('#finding-disaster-text').textContent =
    `${number(review, 0)} of ${number(screened, 0)} checked buildings overlap land needing review. ${largestCount?.name || 'The leading area'} has the most (${number(largestCount?.review || 0, 0)}). ${largestShare?.name || 'Another area'} has the largest proportion (${number(largestShare?.share || 0, 0)}%). ${highestAverage?.name || 'The leading area'} has the highest average flood-concern score across its study area.`;
  document.querySelector('#finding-business-number').textContent = market
    ? `${number(market.review, 0)} of ${number(market.screened, 0)}`
    : `${number(review, 0)} footprints`;
  document.querySelector('#finding-business-text').textContent = market
    ? `In Gikomba Market, ${number(market.review, 0)} of ${number(market.screened, 0)} checked buildings need closer review. Flooding or blocked roads here could make it harder to trade, reach customers and deliver goods, even for businesses that stay dry.`
    : `Flooding or blocked roads could make it harder to trade, reach customers and deliver goods, even for businesses that stay dry.`;
  document.querySelectorAll('.finding-visual img').forEach(img => {
    img.src = versioned(img.getAttribute('src').split('?')[0]);
  });
  document.querySelectorAll('.finding-visual a').forEach(link => {
    link.href = versioned(link.getAttribute('href').split('?')[0]);
  });
}

function renderAreaStatistics(area = 'all') {
  const allAreas = area === 'all';
  const neighborhoods = allAreas
    ? [...metrics.neighborhoods]
    : metrics.neighborhoods.filter(row => row.name === area);
  const sorted = neighborhoods.sort((a, b) => b.hazard_score_mean - a.hazard_score_mean);
  const highestMean = sorted.length ? Math.max(...sorted.map(row => Number(row.hazard_score_mean))) : 0;
  const highArea = sorted.reduce((sum, row) => sum + Number(row.high_hazard_area_ha || 0), 0);
  document.querySelector('#inspection-count').textContent = sorted.length;
  document.querySelector('#highest-hazard').textContent = number(highestMean, 2);
  document.querySelector('#high-area').textContent = `${number(highArea, 0)} ha`;
  document.querySelector('#neighborhood-bars').innerHTML = sorted.map(row => `
    <div class="bar-row">
      <span class="label" title="${row.name}">${row.name}</span>
      <span class="bar-track"><i style="width:${row.hazard_score_mean * 100}%"></i></span>
      <span class="bar-value">${number(row.hazard_score_mean, 2)}</span>
    </div>`).join('');

  const buildingRows = allAreas
    ? (metrics.buffer_buildings || [])
    : (metrics.buffer_buildings || []).filter(row => row.inspection_area === area);
  const reviewed = buildingRows.reduce(
    (sum, row) => sum + Number(row.osm_buildings_in_buffer || 0), 0
  );
  const affected = buildingRows.reduce(
    (sum, row) => sum + Number(row.osm_buildings_potentially_affected || 0), 0
  );
  const affectedShare = reviewed ? (affected / reviewed) * 100 : 0;
  document.querySelector('#donut-share').textContent = `${number(affectedShare, 0)}%`;
  document.querySelector('#overview-affected').textContent = number(affected, 0);
  document.querySelector('#overview-other').textContent = number(reviewed - affected, 0);
  document.querySelector('#building-donut').style.setProperty('--review-share', `${affectedShare}%`);
  document.querySelector('#building-scope').textContent = allAreas ? 'All areas' : area;

  const corridorRows = (allAreas
    ? [...metrics.corridors]
    : metrics.corridors.filter(row => row.name === area)
  ).sort((a, b) => b.candidate_affected_area_ha - a.candidate_affected_area_ha);
  const includeSouthC = allAreas || area === 'South C';
  const southCReviewHa = includeSouthC
    ? Number(metrics.south_c.catchment_area_km2) * Number(metrics.south_c.high_hazard_area_pct)
    : 0;
  const reviewArea = corridorRows.reduce(
    (sum, row) => sum + Number(row.candidate_affected_area_ha || 0), 0
  ) + southCReviewHa;
  document.querySelector('#corridor-total').textContent = number(reviewArea, 1);
  document.querySelector('#review-area-title').textContent = area === 'South C'
    ? 'South C land needing review'
    : allAreas ? 'Areas needing closer review' : 'River corridor needing review';
  document.querySelector('#review-area-method').textContent = area === 'South C'
    ? 'Terrain' : allAreas ? 'All methods' : 'River';
  const reviewItems = corridorRows.map(row => ({
    name: row.name,
    area: Number(row.candidate_affected_area_ha || 0),
  }));
  if (includeSouthC) reviewItems.push({name: 'South C terrain area', area: southCReviewHa});
  document.querySelector('#corridor-list').innerHTML = reviewItems.length
    ? reviewItems.map(row => `<div class="compact-row"><span>${row.name}</span><strong>${number(row.area, 1)} ha</strong></div>`).join('')
    : '<div class="compact-row"><span>No review area calculated</span><strong>—</strong></div>';
  document.querySelector('#south-c-card').hidden = !(allAreas || area === 'South C');
}

function showAreaStory(area) {
  const title = document.querySelector('#area-story-title');
  const story = document.querySelector('#area-story');
  if (area === 'all') {
    title.textContent = 'What the selected areas show';
    story.textContent = 'Across these six areas, the pattern is consistent: water gathers where low ground, nearby drainage and building density line up. In five areas we check buildings near mapped rivers and drains; in South C we follow the land that drains toward the selected outlet because there is no dedicated river mapped there.';
    return;
  }
  const rows = (metrics.buffer_buildings || []).filter(row => row.inspection_area === area);
  const total = rows.reduce((sum, row) => sum + Number(row.osm_buildings_in_buffer || 0), 0);
  const affected = rows.reduce((sum, row) => sum + Number(row.osm_buildings_potentially_affected || 0), 0);
  title.textContent = area;
  const method = area === 'South C'
    ? 'South C is checked by terrain drainage rather than a mapped river. We follow the land that drains toward the selected outlet and include the ground near Wilson Airport, but the outlet and nearby drains still need a field check.'
    : 'We assess buildings in three distance bands around mapped rivers and drains, then compare them with low ground and nearby water features. This shows which parts of the area need closer review.';
  story.textContent = `${method} ${number(total, 0)} mapped buildings were reviewed and ${number(affected, 0)} overlap places needing closer review.`;
}

function renderBufferBuildings(area = 'all') {
  const rows = metrics.buffer_buildings || [];
  const select = document.querySelector('#buffer-area-select');
  if (select.options.length === 1) {
    [...new Set(rows.map(row => row.inspection_area))].sort().forEach(name => {
      select.insertAdjacentHTML('beforeend', `<option value="${name}">${name}</option>`);
    });
  }
  const filtered = area === 'all' ? rows : rows.filter(row => row.inspection_area === area);
  const total = filtered.reduce((sum, row) => sum + Number(row.osm_buildings_in_buffer || 0), 0);
  const affected = filtered.reduce((sum, row) => sum + Number(row.osm_buildings_potentially_affected || 0), 0);
  document.querySelector('#buffer-total').textContent = number(total, 0);
  document.querySelector('#buffer-affected').textContent = number(affected, 0);
  document.querySelector('#buffer-building-rows').innerHTML = filtered.length ? filtered.map(row => `
    <tr title="${row.inspection_area}">
      <td>${area === 'all' ? `${row.inspection_area}<br>` : ''}<span class="muted">${row.interval_label}</span></td>
      <td>${number(row.osm_buildings_in_buffer, 0)}</td>
      <td>${number(row.osm_buildings_potentially_affected, 0)}</td>
      <td>${number(row.potentially_affected_pct, 1)}%</td>
    </tr>`).join('') : '<tr><td colspan="4">Run Stage 8 to create the building counts.</td></tr>';
}

const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));

function buildingExplanation(properties) {
  const review = properties.potentially_affected === true || properties.potentially_affected === 'true';
  const terrain = properties.inspection_area === 'South C' || String(properties.screening_method || '').startsWith('Terrain catchment');
  if (terrain) {
    return review
      ? 'Red: part of this building overlaps higher-concern land in the South C drainage area.'
      : 'Orange: this building was checked in the South C drainage area and does not overlap the higher-concern land. Orange does not mean safe.';
  }
  return review
    ? 'Red: part of this building overlaps land needing review near a river. The check combines river distance, low height above nearby drainage and a higher flood-concern score.'
    : 'Orange: this building was checked near a river but does not overlap land marked for review. Distance from a river alone does not make a building red. Orange does not mean safe.';
}

function renderDiscoveryStory(buildingAreas) {
  const totals = buildingAreas.reduce((sum, row) => ({screened:sum.screened + row.screened, review:sum.review + row.review}), {screened:0, review:0});
  const share = totals.screened ? 100 * totals.review / totals.screened : 0;
  const mostFlagged = [...buildingAreas].sort((a,b) => b.review - a.review)[0];
  const highestShare = [...buildingAreas].sort((a,b) => b.share - a.share)[0];
  const highestConcern = [...metrics.neighborhoods].sort((a,b) => b.hazard_score_mean - a.hazard_score_mean)[0];
  const count = metrics.neighborhoods.length;
  document.querySelector('#intro-discovery-summary').textContent =
    `Our checks across ${count} study areas identify ${number(totals.review,0)} of ${number(totals.screened,0)} mapped buildings (${number(share,1)}%) for closer review. The map shows where these buildings sit and how the nearby drainage helps explain the result.`;
  document.querySelector('#intro-key-finding').textContent =
    `Kibera has the most buildings needing review, South B shows the highest share of checked buildings marked red, and South C is assessed through its drainage catchment because no mapped river defines that study area. Each result points to a place where field checks are worth doing before the rains.`;
  document.querySelector('#story-main-finding').textContent =
    `Across the six study areas, we checked ${number(totals.screened,0)} mapped buildings. ${number(totals.review,0)} overlap land that needs a closer look and appear red on the map; the rest were screened but fall outside the higher-concern mask. The result is a clear pattern: nearby buildings can carry different risk signals because water does not gather evenly across a neighbourhood.`;
  document.querySelector('#story-land-finding').textContent =
    `The analysis also highlights ${number(metrics.headline.high_hazard_area_ha,1)} hectares with higher concern scores and ${number(metrics.headline.candidate_corridor_area_ha,1)} hectares near rivers that stand out for closer review. These land areas overlap, so they should be read together rather than added up as separate totals.`;
  document.querySelector('#story-comparison-finding').textContent =
    `${mostFlagged.name} has the highest number of buildings flagged for review. ${highestShare.name} shows the largest share of checked buildings in that category, while ${highestConcern.name} shows the strongest average concern score. That combination tells us where field checks are likely to be most useful.`;
  const southC = buildingAreas.find(row => row.name === 'South C');
  document.querySelector('#story-south-c-method').textContent =
    `For South C, the method is different. We follow the land draining toward the selected outlet rather than using a mapped river corridor. The catchment covers ${number(metrics.south_c.catchment_area_km2,2)} km², ${number(metrics.south_c.high_hazard_area_pct,1)}% of which scores as higher concern, and ${number(southC?.review || 0,0)} of ${number(southC?.screened || 0,0)} checked buildings fall in that higher-risk drainage area.`;
  const container = document.querySelector('#story-area-findings');
  container.innerHTML = `<table class="analysis-source-table summary-area-table">
    <thead><tr><th scope="col">Study area</th><th scope="col">Flagged / screened</th><th scope="col">Flagged share</th><th scope="col">Mean concern score</th><th scope="col">Drainage context</th></tr></thead>
    <tbody>${[...buildingAreas].sort((a,b) => b.review-a.review).map(area => {
      const row = metrics.neighborhoods.find(item => item.name === area.name);
      const corridor = metrics.corridors.find(item => item.name === area.name);
      const names = [...new Set(String(corridor?.river_names || '').split(';').map(name => name.trim()).filter(Boolean))];
      const drainage = area.name === 'South C' ? 'Terrain drainage catchment' : names.join(', ') || 'Mapped rivers and drains';
      return `<tr><th scope="row"><button class="text-button" type="button" data-story-area="${escapeHtml(area.name)}" aria-label="Explore ${escapeHtml(area.name)} on the map">${escapeHtml(area.name)} →</button></th>` +
        `<td>${number(area.review,0)} / ${number(area.screened,0)}</td><td>${number(area.share,1)}%</td>` +
        `<td>${number(row.hazard_score_mean,2)}</td><td>${escapeHtml(drainage)}</td></tr>`;
    }).join('')}</tbody></table>
    <p class="table-note">Mean concern scores describe the 1 km study areas on a 0–1 scale; they are not flood probabilities. South C building screening uses a terrain catchment. Click an area name to explore its results in Part B.</p>`;
  container.querySelectorAll('[data-story-area]').forEach(button => button.addEventListener('click', () => {
    switchView('dashboard');
    selectArea(button.dataset.storyArea);
  }));
}

function areaDetails(area) {
  const row = metrics.neighborhoods.find(item => item.name === area);
  if (!row) return escapeHtml(area);
  const buildings = (metrics.buffer_buildings || []).filter(item => item.inspection_area === area);
  const total = buildings.reduce((sum, item) => sum + Number(item.osm_buildings_in_buffer || 0), 0);
  const review = buildings.reduce((sum, item) => sum + Number(item.osm_buildings_potentially_affected || 0), 0);
  return `<strong>${escapeHtml(area)}</strong><br>` +
    `Average concern score: ${number(row.hazard_score_mean, 2)}<br>` +
    `Average inspection priority: ${number(row.planning_priority_score_mean, 2)}<br>` +
    `Higher-concern land in study buffer: ${number(row.high_hazard_area_ha, 1)} ha<br>` +
    `Screened buildings: ${number(total, 0)}; needing review: ${number(review, 0)}<br>` +
    `${area === 'South C' ? 'Building counts use terrain drainage.' : 'Building counts use 31 / 62.5 / 125 m river bands.'}`;
}

function renderStudyAreas() {
  const list = document.querySelector('#study-area-list');
  list.innerHTML = metrics.neighborhoods.map(row => {
    const buildings = (metrics.buffer_buildings || []).filter(item => item.inspection_area === row.name);
    const total = buildings.reduce((sum, item) => sum + Number(item.osm_buildings_in_buffer || 0), 0);
    const review = buildings.reduce((sum, item) => sum + Number(item.osm_buildings_potentially_affected || 0), 0);
    return `<button class="study-area-button" type="button" data-study-area="${escapeHtml(row.name)}" aria-pressed="${row.name === selectedArea}">` +
      `<strong>${escapeHtml(row.name)}</strong><span>Concern ${number(row.hazard_score_mean, 2)} · priority ${number(row.planning_priority_score_mean, 2)}</span>` +
      `<span>${number(row.high_hazard_area_ha, 1)} ha higher concern</span>` +
      `<span>${number(review, 0)} / ${number(total, 0)} screened buildings need review</span></button>`;
  }).join('');
  list.querySelectorAll('[data-study-area]').forEach(button => button.addEventListener('click', () => selectArea(button.dataset.studyArea)));
  document.querySelector('#analysis-river-summary').textContent =
    `${number(riversData.count, 0)} mapped river, stream and drain sections are used in the analysis. Bright blue lines show the parts inside our study areas. For South C, we follow the shape of the land to understand drainage.`;
  const names = new Map();
  riversData.names.forEach(item => names.set(item.name, item.count));
  document.querySelector('#analysis-river-list').innerHTML = [...names].sort(([a], [b]) => a.localeCompare(b)).map(([name, count]) =>
    `<div class="compact-row"><span>${escapeHtml(name)}</span><strong>${number(count, 0)}</strong></div>`
  ).join('');
}

function syncAreaLayers() {
  if (!map) return;
  for (const id of ['building-fill', 'drainage-lines', 'buffer-interval-fill', 'buffer-interval-line']) {
    if (map.getLayer(id)) map.setFilter(id, selectedArea === 'all' ? null : ['==', ['get', 'inspection_area'], selectedArea]);
  }
  if (map.getLayer('inspection-line')) map.setPaintProperty('inspection-line', 'line-color',
    selectedArea === 'all' ? '#fbbf24' : ['case', ['==', ['get', 'name'], selectedArea], '#fff176', '#94a3b8']);
}

function featureBounds(feature) {
  const bounds = [[Infinity, Infinity], [-Infinity, -Infinity]];
  function visit(coords) {
    if (typeof coords[0] === 'number') {
      for (let axis = 0; axis < 2; axis++) {
        bounds[0][axis] = Math.min(bounds[0][axis], coords[axis]);
        bounds[1][axis] = Math.max(bounds[1][axis], coords[axis]);
      }
    } else coords.forEach(visit);
  }
  visit(feature.geometry.coordinates);
  return bounds;
}

function renderLayerControls() {
  const descriptions = {
    hazard: 'A score from 0 to 1 combining low ground, closeness to water, land use and past rainfall and water records. Blue shows lower concern and red higher concern across the mapped analysis area. It does not show flood depth or probability.',
    priority: 'Combines flood concern with building density to help decide where to inspect first. Dark purple shows lower priority and pale yellow higher priority. It appears over flood concern when both are switched on.',
    candidate: 'Red patches near rivers that meet three checks: within 125 metres of a mapped waterway, no more than 5 metres above nearby drainage, and a concern score of at least 0.60. These patches stay within the study boundaries and need checks on the ground.',
    buildings: 'The buildings checked in the analysis. Red means part of a building overlaps land marked for review; orange means it does not. Orange does not mean safe. Choosing an area filters this layer.',
    analysis_rivers: 'The full mapped network of rivers, streams, canals and drains used in the analysis, shown in blue. It stays visible across the analysis area when you choose a study area.',
    drainage: 'Brighter, thicker blue lines highlight the parts of mapped rivers and drains inside the study boundaries. Choosing an area shows its sections.',
    buffer_intervals: 'Three distance bands beside mapped rivers and drains: red 0–31 metres, orange 31–62.5 metres and yellow 62.5–125 metres. They stay within the study boundaries and follow your selected area. These are planning distances, not flood boundaries or legal river reserves.',
    dams: 'Blue shapes show mapped dams and water bodies used to understand nearby drainage and closeness to water. They do not show current water levels or dam condition.',
    river_corridor: 'Light blue land extending up to 125 metres on either side of mapped rivers and drains, clipped to the study boundaries. This shows distance from water, including land that is not marked for closer review.',
    catchment: 'The pink outline and shading show land that drains towards the selected South C outlet, based on the shape of the ground. This area is used for South C building checks. The outlet and local drains still need a visit.',
    inspection: 'Dashed boundaries extend 1 kilometre from each of the six study points. They define the areas compared in the study and do not predict where flooding will reach.',
    locations: 'The six study points and their names. Click a point or name to select the area and open its analysis or history tab. These points are study centres, not exact locations of past flood incidents.',
  };
  document.querySelector('#layer-guide-content').innerHTML = metrics.layer_manifest.map(layer =>
    `<dt>${escapeHtml(layer.label)}</dt><dd>${escapeHtml(descriptions[layer.key] || '')}</dd>`).join('');
  document.querySelector('#layer-controls').innerHTML = metrics.layer_manifest.map(layer => `
    <label class="layer-toggle"><span>${layer.label}</span>
      <input type="checkbox" data-layer-key="${layer.key}" ${layer.default_visible ? 'checked' : ''}>
    </label>`).join('');
  document.querySelector('#layer-controls').addEventListener('change', event => {
    const key = event.target.dataset.layerKey;
    (layerIds[key] || []).forEach(id => {
      if (map?.getLayer(id)) map.setLayoutProperty(id, 'visibility', event.target.checked ? 'visible' : 'none');
    });
  });
}

const versioned = file => `${file}?v=${encodeURIComponent(dataVersion)}`;
const visibilityFor = key => {
  const checkbox = document.querySelector(`[data-layer-key="${key}"]`);
  if (checkbox) return checkbox.checked ? 'visible' : 'none';

  // Handle case where metrics isn't loaded yet
  if (!metrics || !metrics.layer_manifest) return 'visible';

  const layer = metrics.layer_manifest.find(item => item.key === key);
  return layer?.default_visible === false ? 'none' : 'visible';
};

function addImageLayer(id, key, overlay) {
  if (!map.getSource(`${id}-source`)) map.addSource(`${id}-source`, { type: 'image', url: versioned(overlay.file), coordinates: overlay.coordinates });
  const visibility = visibilityFor(key);
  if (!map.getLayer(id)) map.addLayer({ id, type: 'raster', source: `${id}-source`, layout: { visibility }, paint: { 'raster-fade-duration': 0, 'raster-opacity': .72 } });
}

function addAnalysisLayers() {
  if (!metrics || !map) return;
  addImageLayer('hazard-layer', 'hazard', metrics.overlays.hazard);
  addImageLayer('priority-layer', 'priority', metrics.overlays.priority);
  addImageLayer('candidate-layer', 'candidate', metrics.overlays.corridor);
  addGeoJsonLayersOnTop();
}

function addGeoJsonLayersOnTop() {
   const addSource = (id, source) => { if (!map.getSource(id)) map.addSource(id, source); };
   const addLayer = layer => {
     if (vectorTiles[layer.source]) layer['source-layer'] = vectorTiles[layer.source].source_layer;
     if (!map.getLayer(layer.id)) map.addLayer(layer);
   };
   const tiledSource = id => ({type:'vector', tiles:[new URL(vectorTiles[id].tiles, document.baseURI).href.replace(/%7B/g, '{').replace(/%7D/g, '}')],
     minzoom:vectorTiles[id].minzoom, maxzoom:vectorTiles[id].maxzoom, bounds:vectorTiles[id].bounds,
     attribution:'© OpenStreetMap contributors · Project flood screening'});
   try {
     addSource('inspection', { type: 'geojson', data: inspectionData });
     addLayer({ id:'inspection-fill', type:'fill', source:'inspection', layout:{visibility:visibilityFor('inspection')}, paint:{'fill-color':'#f59e0b', 'fill-opacity':.04} });

     addSource('analysis-rivers', tiledSource('analysis-rivers'));
     addLayer({id:'analysis-river-lines', type:'line', source:'analysis-rivers', layout:{visibility:visibilityFor('analysis_rivers'),'line-cap':'round','line-join':'round'}, paint:{'line-color':'#38bdf8','line-width':2,'line-opacity':.8}});
     addSource('buffer-intervals', tiledSource('buffer-intervals'));
     const bandColor = ['case', ['==', ['get','distance_to_m'], 31], '#ef4444', ['==', ['get','distance_to_m'], 62.5], '#fb923c', '#facc15'];
     addLayer({id:'buffer-interval-fill', type:'fill', source:'buffer-intervals', layout:{visibility:visibilityFor('buffer_intervals')}, paint:{'fill-color':bandColor,'fill-opacity':.22}});
     addLayer({id:'buffer-interval-line', type:'line', source:'buffer-intervals', layout:{visibility:visibilityFor('buffer_intervals')}, paint:{'line-color':bandColor,'line-width':1}});

     addSource('dams', { type:'geojson', data:versioned('data/dams.geojson') });
     addLayer({ id:'dam-fill', type:'fill', source:'dams', layout:{visibility:visibilityFor('dams')}, paint:{'fill-color':'#38bdf8','fill-opacity':.70} });
     addLayer({ id:'dam-line', type:'line', source:'dams', layout:{visibility:visibilityFor('dams')}, paint:{'line-color':'#0652a3','line-width':2.5,'line-opacity':1.0} });

     addSource('river-corridor', { type:'geojson', data:versioned('data/river_corridor.geojson') });
     addLayer({ id:'river-corridor-fill', type:'fill', source:'river-corridor', layout:{visibility:visibilityFor('river_corridor')}, paint:{'fill-color':'#0ea5e9','fill-opacity':.25} });
     addLayer({ id:'river-corridor-line', type:'line', source:'river-corridor', layout:{visibility:visibilityFor('river_corridor')}, paint:{'line-color':'#0284c7','line-width':2.5,'line-opacity':1.0} });

     addSource('drainage', { type:'geojson', data:versioned('data/drainage.geojson') });
     addLayer({ id:'drainage-lines', type:'line', source:'drainage', layout:{visibility:visibilityFor('drainage'),'line-cap':'round','line-join':'round'}, paint:{'line-color':'#00bfff','line-width':4,'line-opacity':1.0} });

     addSource('catchment', { type:'geojson', data:versioned('data/south_c_catchment.geojson') });
     addLayer({ id:'catchment-fill', type:'fill', source:'catchment', layout:{visibility:visibilityFor('catchment')}, paint:{'fill-color':'#e11d48','fill-opacity':.20,'fill-outline-color':'#be123c'} });
     addLayer({ id:'catchment-line', type:'line', source:'catchment', layout:{visibility:visibilityFor('catchment')}, paint:{'line-color':'#be123c','line-width':2.5,'line-opacity':1.0} });

     addLayer({ id:'inspection-line', type:'line', source:'inspection', layout:{visibility:visibilityFor('inspection')}, paint:{'line-color':'#fbbf24','line-width':3,'line-opacity':1.0,'line-dasharray':[5,3]} });

     addSource('locations', { type:'geojson', data:locationsData });
     addLayer({ id:'neighborhood-points', type:'circle', source:'locations', layout:{visibility:visibilityFor('locations')}, paint:{'circle-radius':7,'circle-color':'#f59e0b','circle-stroke-color':'#fff','circle-stroke-width':2.5} });
     addLayer({ id:'neighborhood-label', type:'symbol', source:'locations', layout:{visibility:visibilityFor('locations'),'text-field':['get','name'],'text-size':12,'text-offset':[0,1.3],'text-anchor':'top','text-allow-overlap':false,'text-font':['Open Sans Bold']}, paint:{'text-color':'#1f2937','text-halo-color':'#fff','text-halo-width':2} });

     addSource('buildings', tiledSource('buildings'));
     addLayer({ id:'building-fill', type:'fill', source:'buildings', layout:{visibility:visibilityFor('buildings')}, paint:{
       'fill-color':['case',['boolean',['get','potentially_affected'],false],'#dc2626','#f59e0b'],
       'fill-opacity':['case',['boolean',['get','potentially_affected'],false],.80,.35],
       'fill-outline-color':['case',['boolean',['get','potentially_affected'],false],'#7f1d1d','#92400e']
     } });
     for (const id of ['analysis-river-lines', 'drainage-lines', 'inspection-line', 'neighborhood-points', 'neighborhood-label']) map.moveLayer(id);
     syncAreaLayers();

   } catch (error) {
     mapLoadError = true;
     console.error('Error adding GeoJSON layers:', error);
     const message = document.querySelector('#map-message');
     message.textContent = `Analysis layers could not be restored: ${error.message}`;
     message.classList.remove('hidden');
   }
}

function initMap() {
  if (!metrics) return;
  if (typeof maplibregl === 'undefined') {
    const message = document.querySelector('#map-message');
    message.textContent = 'The map library could not load. Check your internet connection and reload.';
    message.classList.remove('hidden');
    return;
  }
  document.querySelector('#map-message').classList.add('hidden');
  map = new maplibregl.Map({
    container: 'map',
    style: selectedMapStyle(document.querySelector('#basemap-select').value),
    bounds: NAIROBI_BOUNDS,
    fitBoundsOptions: { padding: 35 },
  });
  map.addControl(new maplibregl.NavigationControl(), 'bottom-right');
  map.addControl(new maplibregl.FullscreenControl(), 'bottom-right');
  map.once('load', initMeasurement);
  addLookouts();
  map.on('style.load', () => {
    try {
      addAnalysisLayers();
      for (const [key, ids] of Object.entries(layerIds)) {
        for (const id of ids) {
          if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', visibilityFor(key));
        }
      }
      syncAreaLayers();
    } catch (error) {
      mapLoadError = true;
      const message = document.querySelector('#map-message');
      message.textContent = `Analysis layers could not be restored: ${error.message}`;
      message.classList.remove('hidden');
    }
  });
  map.on('error', event => {
    mapLoadError = true;
    console.error('Map resource could not load:', event.error);
    const message = document.querySelector('#map-message');
    message.textContent = 'A map resource could not load. Check your connection or try another basemap.';
    message.classList.remove('hidden');
  });
  map.on('idle', () => {
    if (!mapLoadError && Object.values(layerIds).flat().every(id => map.getLayer(id))) {
      document.querySelector('#map-message').classList.add('hidden');
    }
  });
  map.on('click', event => {
    if (measurementActive()) return;
    const layers = ['neighborhood-points', 'neighborhood-label', 'building-fill', 'dam-fill', 'drainage-lines', 'analysis-river-lines', 'catchment-fill', 'inspection-fill'];
    let feature;
    for (const id of layers) {
      if (!map.getLayer(id)) continue;
      feature = map.queryRenderedFeatures(event.point, {layers:[id]})[0];
      if (feature) break;
    }
    if (!feature) return;
    const p = feature.properties || {};
    let details;
    const id = feature.layer.id;
    if (id === 'inspection-fill' || id === 'neighborhood-points' || id === 'neighborhood-label') {
      showStudyPopup(p.name);
      return;
    } else if (id === 'building-fill') {
      if (p.inspection_area) selectArea(p.inspection_area);
      const review = p.potentially_affected === true || p.potentially_affected === 'true';
      details = `<strong>${review ? 'Building needing closer review' : 'Other screened building'}</strong><br>` +
        `${escapeHtml(p.inspection_area)}<br>${escapeHtml(p.interval_label)}<br>${escapeHtml(p.screening_method || '')}` +
        `<p>${escapeHtml(buildingExplanation(p))}</p>` +
        (p.screening_level ? `<p>Recorded screening level: ${escapeHtml(p.screening_level)}</p>` : '') +
        '<p>Even a small overlap with land marked for review can make a building red. This does not show confirmed damage. Orange does not mean safe.</p>';
    } else if (id === 'dam-fill') {
      details = `<strong>${escapeHtml(p.display_name || 'Mapped dam or water body')}</strong><br>${escapeHtml(p.waterbody_type || 'water body')}`;
    } else if (id === 'catchment-fill') {
      selectArea('South C');
      details = areaDetails('South C') + `<br>${number(metrics.south_c.catchment_area_km2, 2)} km² contributing drainage area`;
    } else {
      details = `<strong>${escapeHtml(p.display_name || p.name || 'Unnamed river or drain')}</strong><br>` +
        `${escapeHtml(p.waterway || 'waterway')}<br>${escapeHtml(p.inspection_area || 'Full analysis network')}<br>` +
        `Source: ${escapeHtml(p.model_source || 'Reviewed river layer')}`;
    }
    new maplibregl.Popup().setLngLat(event.lngLat).setHTML(details).addTo(map);
  });
  selectArea(selectedArea);
}

function switchView(view) {
  const dashboard = view === 'dashboard';
  document.querySelector('#intro-view').hidden = dashboard;
  document.querySelector('#dashboard-view').hidden = !dashboard;
  document.querySelectorAll('[data-view-target]').forEach(button => {
    button.classList.toggle('nav-active', button.dataset.viewTarget === view);
  });
  if (dashboard) {
    if (!map) initMap();
    else setTimeout(() => map.resize(), 0);
  }
}

function selectArea(area) {
  if (!metrics || (area !== 'all' && !metrics.neighborhoods.some(row => row.name === area))) return;
  selectedArea = area;
  document.querySelector('#area-select').value = area;
  document.querySelector('#buffer-area-select').value = area;
  renderAreaStatistics(area);
  renderBufferBuildings(area);
  showAreaStory(area);
  renderStudyAreas();
  syncAreaLayers();
  if (!map) return;
  if (area === 'all') {
    map.fitBounds(NAIROBI_BOUNDS, {padding:45});
  } else {
    const feature = inspectionData.features.find(item => item.properties.name === area);
    if (feature) map.fitBounds(featureBounds(feature), {padding:55, maxZoom:14});
  }
}

document.querySelector('#open-dashboard').addEventListener('click', () => switchView('dashboard'));
document.querySelectorAll('[data-view-target]').forEach(button => button.addEventListener('click', () => {
  switchView(button.dataset.viewTarget);
}));
document.querySelector('#buffer-area-select').addEventListener('change', event => {
  selectArea(event.target.value);
});
document.querySelector('#basemap-select').addEventListener('change', event => {
  changeBasemap(event.target.value);
});
document.querySelector('#area-select').addEventListener('change', event => {
  selectArea(event.target.value);
});
document.querySelector('#reset-filters').addEventListener('click', () => {
  if (!metrics) return;
  document.querySelector('#basemap-select').value = 'standard';
  document.querySelectorAll('[data-layer-key]').forEach(input => {
    const layer = metrics.layer_manifest.find(item => item.key === input.dataset.layerKey);
    input.checked = Boolean(layer?.default_visible);
    (layerIds[input.dataset.layerKey] || []).forEach(id => {
      if (map?.getLayer(id)) map.setLayoutProperty(id, 'visibility', input.checked ? 'visible' : 'none');
    });
  });
  changeBasemap('standard');
  selectArea('all');
});
document.querySelector('#theme-toggle').addEventListener('click', () => {
  applyTheme(currentTheme() === 'dark' ? 'light' : 'dark');
});
applyTheme(currentTheme(), false);
loadDashboard().catch(error => {
  const message = document.querySelector('#map-message');
  message.textContent = error.message;
  message.classList.remove('hidden');
});

setInterval(async () => {
  if (!dataVersion) return;
  const dot = document.querySelector('#refresh-dot');
  const status = document.querySelector('#refresh-status');
  dot.classList.add('checking');
  status.textContent = 'Checking for updates';
  try {
    const latest = await fetch(`data/dashboard_metrics.json?t=${Date.now()}`, {cache:'no-store'}).then(response => response.json());
    if (latest.generated_at !== dataVersion) {
      status.textContent = 'New notebook results found';
      setTimeout(() => location.reload(), 700);
      return;
    }
    status.textContent = 'Dashboard is current';
  } catch {
    status.textContent = 'Update check paused';
  } finally {
    dot.classList.remove('checking');
  }
}, 15000);
