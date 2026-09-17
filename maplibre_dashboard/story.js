/* Chapter navigation and screenshots of the actual screening dashboard. */
const storyChapters = [
  {id:'places', label:'Six places', image:'areas', area:'all', layers:['analysis_rivers','drainage','dams','inspection','locations'], caption:'Six study centres and their 1 km review boundaries, with the mapped drainage network. Boundaries define the comparison areas; they do not show flood reach.'},
  {id:'findings', label:'Key findings', image:'concern', area:'South B', layers:['hazard','buildings','drainage','inspection','locations'], caption:'South B: screened buildings over the flood-concern layer. Red buildings overlap land selected for closer review; nearby orange buildings were checked but are not classified as safe.'},
  {id:'comparison', label:'Compare areas', image:'comparison', area:'Kibera', layers:['buildings','drainage','inspection','locations'], caption:'The dashboard’s six-area comparison. Kibera has the largest flagged count; South B has the highest flagged share. Mean concern scores describe neighbourhoods, not flood probabilities.'},
  {id:'reading', label:'Read the map', image:'buildings', area:'Gikomba Market', layers:['buildings','drainage','locations'], caption:'A closer view around Gikomba Market shows red and orange building footprints beside drainage. A small overlap with the review mask qualifies a building as red.'},
  {id:'prepare', label:'Prepare', image:'priorities', area:'Kibera', layers:['priority','buildings','drainage','inspection','locations'], caption:'Kibera: inspection priority combines concern with building density. Use it to plan visits, then check drains, crossings and evacuation routes on the ground.'},
  {id:'history', label:'Past floods', image:'history', caption:'Dated reports used in the dashboard provide historical context. They describe reported impacts; they do not establish surveyed flood boundaries.'},
  {id:'rainfall', label:'Short rains', image:'outlook', caption:'The dashboard’s seasonal-outlook cards. Weather-agency forecasts provide preparation context separately from the building-screening results.'},
  {id:'roads', label:'Roads & access', image:'roads', area:'South B', layers:['analysis_rivers','drainage','inspection','locations'], caption:'Road-watch markers around South B and neighbouring access routes. Warning triangles link to dated reports; positions are approximate and are not live alerts.'},
  {id:'methods', label:'How we checked', image:'methods', area:'South C', layers:['catchment','buildings','analysis_rivers','inspection','locations'], caption:'South C: the terrain-derived drainage catchment and screened buildings. This method follows land draining to a selected outlet, rather than a mapped river corridor.'},
  {id:'sources', label:'Evidence', image:'sources', caption:'The dashboard’s input inventory records each source, purpose and data period. Historical water records and terrain indicators support screening, rather than a completed hydraulic forecast.'},
];
let activeStoryChapter = 0;
let storyReturnChapter = null;

function openStoryMap(index) {
  const chapter = storyChapters[index];
  if (!chapter?.area || !metrics) return;
  storyReturnChapter = index;
  document.querySelector('#return-to-story').hidden = false;
  // Set the controls first so a newly created map also starts with this preset.
  document.querySelectorAll('[data-layer-key]').forEach(input => {
    input.checked = chapter.layers.includes(input.dataset.layerKey);
    input.dispatchEvent(new Event('change', {bubbles:true}));
  });
  switchView('dashboard');
  selectArea(chapter.area);
  const frame = () => {
    if (chapter.id==='places') map.fitBounds([[36.75,-1.335],[36.925,-1.235]],{padding:45,duration:0});
    if (chapter.id==='reading') map.jumpTo({center:[36.839,-1.287],zoom:16});
  };
  if (map?.loaded()) requestAnimationFrame(frame);
  else map?.once('load',frame);
}

function initStory() {
  const shell = document.querySelector('#intro-view');
  const grid = shell.querySelector('.story-grid');
  const cards = [...grid.children];
  const nav = document.createElement('nav');
  nav.className = 'story-chapter-nav';
  nav.setAttribute('aria-label','Story chapters');
  nav.innerHTML = `<div class="story-chapter-links">${storyChapters.map((chapter,index) => `<button type="button" data-chapter="${index}" aria-controls="chapter-${chapter.id}"><span>${String(index+1).padStart(2,'0')}</span>${chapter.label}</button>`).join('')}</div><div class="story-progress" aria-hidden="true"><span></span></div>`;
  grid.before(nav);
  const jump = index => {
    cards[index]?.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth', block:'start'});
  };
  nav.querySelectorAll('[data-chapter]').forEach(button => button.addEventListener('click', () => jump(Number(button.dataset.chapter))));

  const dialog = document.createElement('dialog');
  dialog.className = 'story-image-dialog';
  dialog.id = 'story-image-dialog';
  dialog.setAttribute('aria-label','Enlarged dashboard screenshot');
  dialog.innerHTML = '<button class="story-dialog-close" type="button" aria-label="Close screenshot">Close ×</button><img alt=""><p></p>';
  document.body.append(dialog);
  dialog.querySelector('button').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => {if (event.target===dialog) dialog.close();});

  cards.forEach((card,index) => {
    const chapter = storyChapters[index];
    card.id = `chapter-${chapter.id}`;
    card.classList.add('story-chapter');
    const copy = document.createElement('div');
    copy.className = 'story-chapter-copy';
    while(card.firstChild) copy.append(card.firstChild);
    card.append(copy);
    const visual = document.createElement('figure');
    visual.className = 'story-chapter-visual';
    visual.innerHTML = `<div class="story-visual-heading"><span>Dashboard view · ${String(index+1).padStart(2,'0')}</span><button type="button" class="text-button" data-enlarge>Enlarge ↗</button></div><button type="button" class="story-image-button" data-enlarge aria-label="Enlarge screenshot: ${chapter.label}"><img src="data/story/${chapter.image}.png" alt="${chapter.caption}" loading="lazy" width="960" height="700"></button><figcaption>${chapter.caption}</figcaption><div class="story-visual-actions">${chapter.area ? `<button type="button" class="primary-button" data-explore-chapter="${index}">Explore this view on the map →</button>` : '<span class="story-evidence-note">Expand the evidence alongside this screenshot to read the reports and sources.</span>'}</div>`;
    visual.querySelectorAll('[data-enlarge]').forEach(button => button.addEventListener('click', () => {
      dialog.querySelector('.story-swipe')?.remove();
      dialog.querySelector('img').hidden = false;
      const img = dialog.querySelector('img');
      img.src = `data/story/${chapter.image}.png`;
      img.alt = chapter.caption;
      dialog.querySelector('p').textContent = chapter.caption;
      dialog.showModal();
    }));
    visual.querySelector('[data-explore-chapter]')?.addEventListener('click', () => openStoryMap(index));
    const legend = document.createElement('div');
    legend.className='story-context-legend';
    if(chapter.layers?.includes('buildings')) legend.innerHTML='<span><i class="building-review-swatch"></i>Needs review</span><span><i class="building-other-swatch"></i>Screened, not flagged</span>';
    if(chapter.layers?.includes('drainage') || chapter.layers?.includes('analysis_rivers')) legend.innerHTML+='<span><i class="key-river"></i>Mapped drainage</span>';
    if(chapter.layers?.includes('catchment')) legend.innerHTML+='<span><i class="key-catchment"></i>Drainage catchment</span>';
    if(legend.innerHTML) visual.querySelector('.story-image-button').after(legend);
    card.append(visual);
    const footer = document.createElement('div');
    footer.className = 'story-chapter-footer';
    footer.innerHTML = `<span>${String(index+1).padStart(2,'0')} / ${storyChapters.length}</span>${index<cards.length-1 ? `<button type="button" class="text-button">Next: ${storyChapters[index+1].label} ↓</button>` : '<button type="button" class="text-button">Back to the beginning ↑</button>'}`;
    footer.querySelector('button').addEventListener('click', () => index<cards.length-1 ? jump(index+1) : shell.scrollTo({top:0,behavior:'smooth'}));
    copy.append(footer);
  });

  // Comparing the same map extent makes the different score scales visible.
  const comparison = document.createElement('div');
  comparison.className = 'story-swipe';
  comparison.innerHTML = `<h3>Compare concern and inspection priority</h3><p>The same South B map extent. Drag the handle across the map, or use the arrow keys.</p><div class="story-swipe-images"><img src="data/story/priority.png" alt="South B inspection-priority layer" loading="lazy" draggable="false"><img class="story-swipe-overlay" src="data/story/concern.png" alt="South B flood-concern layer" loading="lazy" draggable="false"><span class="story-swipe-line" aria-hidden="true"><span class="story-swipe-handle">‹ ›</span></span><span class="story-swipe-label left">Flood concern</span><span class="story-swipe-label right">Inspection priority</span><input id="story-swipe-range" type="range" min="0" max="100" value="50" aria-label="Reveal flood concern versus inspection priority" aria-valuetext="50% flood concern, 50% inspection priority"></div><div class="story-score-legends"><div><span>Flood concern</span><i class="concern-scale"></i><small>Lower concern <b>Higher concern</b></small></div><div><span>Inspection priority</span><i class="priority-scale"></i><small>Lower priority <b>Higher priority</b></small></div></div><p class="table-note">Concern uses a 0–1 screening score. Priority also accounts for building density. Neither is a predicted flood depth.</p>`;
  const comparisonVisual = cards[1].querySelector('.story-chapter-visual');
  comparisonVisual.querySelector('.story-image-button').replaceWith(comparison);
  comparisonVisual.querySelector('.story-context-legend')?.remove();
  comparisonVisual.querySelector('.story-visual-heading span').textContent='South B · Interactive comparison';
  comparisonVisual.querySelector('figcaption').textContent='One neighbourhood, two screening perspectives. Compare the land needing review with the places to prioritise for field visits.';
  setupStorySwipe(comparison);
  // The enlarged view keeps the comparison interactive.
  const enlarge = comparisonVisual.querySelector('[data-enlarge]');
  const replacement = enlarge.cloneNode(true);
  enlarge.replaceWith(replacement);
  replacement.addEventListener('click', () => {
    dialog.querySelector('.story-swipe')?.remove();
    dialog.querySelector('img').hidden=true;
    const full = comparison.cloneNode(true);
    full.querySelector('input').id='story-swipe-fullscreen';
    full.querySelector('input').value=comparison.querySelector('input').value;
    full.style.setProperty('--reveal',`${full.querySelector('input').value}%`);
    dialog.querySelector('img').after(full);
    setupStorySwipe(full);
    dialog.lastElementChild.textContent='South B: screening scores for the same map extent.';
    dialog.showModal();
  });

  const returnButton = document.createElement('button');
  returnButton.id = 'return-to-story';
  returnButton.type = 'button';
  returnButton.className = 'text-button story-return';
  returnButton.textContent = '← Return to story';
  returnButton.hidden = true;
  document.querySelector('.top-actions').prepend(returnButton);
  returnButton.addEventListener('click', () => {
    switchView('intro');
    jump(storyReturnChapter ?? activeStoryChapter);
    returnButton.hidden = true;
  });
  const update = () => {
    const top = shell.getBoundingClientRect().top + nav.offsetHeight + 40;
    let current = 0;
    cards.forEach((card,index) => {if(card.getBoundingClientRect().top<=top) current=index;});
    const changed = activeStoryChapter!==current;
    activeStoryChapter=current;
    nav.querySelectorAll('[data-chapter]').forEach((button,index) => {
      button.classList.toggle('chapter-active',index===current);
      if(index===current) button.setAttribute('aria-current','step');
      else button.removeAttribute('aria-current');
    });
    if (changed) {
      const links=nav.querySelector('.story-chapter-links');
      const button=links.querySelector(`[data-chapter="${current}"]`);
      links.scrollTo({left:Math.max(0,button.offsetLeft-links.clientWidth/2+button.offsetWidth/2),behavior:'instant'});
    }
    nav.querySelector('.story-progress span').style.width=`${100 * shell.scrollTop / Math.max(1,shell.scrollHeight-shell.clientHeight)}%`;
  };
  let queued = false;
  shell.addEventListener('scroll', () => {if(!queued) {queued=true;requestAnimationFrame(()=>{update();queued=false;});}}, {passive:true});
  window.addEventListener('resize',update);
  update();
}
function setupStorySwipe(comparison) {
  const input=comparison.querySelector('input');
  const surface=comparison.querySelector('.story-swipe-images');
  const update=() => {
    comparison.style.setProperty('--reveal',`${input.value}%`);
    input.setAttribute('aria-valuetext',`${input.value}% flood concern, ${100-input.value}% inspection priority`);
  };
  input.addEventListener('input',update);
  const move=event => {
    const bounds=surface.getBoundingClientRect();
    input.value=Math.round(Math.max(0,Math.min(100,(event.clientX-bounds.left)/bounds.width*100)));
    input.dispatchEvent(new Event('input',{bubbles:true}));
  };
  surface.addEventListener('pointerdown',event => {
    if(event.button!==0) return;
    event.preventDefault();
    input.focus({preventScroll:true});
    surface.setPointerCapture(event.pointerId);
    move(event);
  });
  surface.addEventListener('pointermove',event => {if(surface.hasPointerCapture(event.pointerId)) move(event);});
  surface.addEventListener('pointerup',event => {if(surface.hasPointerCapture(event.pointerId)) surface.releasePointerCapture(event.pointerId);});
  update();
}
initStory();
