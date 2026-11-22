'use strict';
const $ = (selector) => document.querySelector(selector);
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const escapeHTML = (value) => String(value).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state = {source:'link', length:30, count:3, captions:true, jobs:[], activeId:null, view:'create', file:null, submitting:false};
let polling = null;
let account = null;
let config = {};
let toastTimer;
function toast(message) { $('#toast').textContent = message; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 5500); }
async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    if (response.status === 402) $('#pricing-dialog').showModal();
    let message = `Request failed (${response.status}). Please try again.`;
    try { const data = await response.json(); message = typeof data.detail === 'string' ? data.detail : data.detail?.[0]?.msg?.replace(/^Value error, /, '') || message; } catch { /* Non-JSON server response. */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}
function setSource(source) {
  state.source = source;
  ['link','upload'].forEach((type) => {
    $(`#tab-${type}`).classList.toggle('selected', source === type);
    $(`#tab-${type}`).setAttribute('aria-selected', String(source === type));
    $(`#${type}-panel`).hidden = source !== type;
  });
  $('#form-error').hidden = true;
}
$('#tab-link').onclick = () => setSource('link');
$('#tab-upload').onclick = () => setSource('upload');
document.querySelectorAll('[role=tab]').forEach((tab) => tab.addEventListener('keydown', (event) => {
  if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') { event.preventDefault(); setSource(state.source === 'link' ? 'upload' : 'link'); $(`#tab-${state.source}`).focus(); }
}));
$('#length-options').onclick = (event) => {
  const button = event.target.closest('[data-length]');
  if (!button) return;
  state.length = Number(button.dataset.length);
  document.querySelectorAll('[data-length]').forEach((item) => { const selected = item === button; item.classList.toggle('chosen', selected); item.setAttribute('aria-pressed', String(selected)); });
};
function updateCount() { $('#clip-count').value = state.count; $('#count-less').disabled = state.count <= 1; $('#count-more').disabled = state.count >= 5; }
$('#count-less').onclick = () => { state.count = Math.max(1, state.count - 1); updateCount(); };
$('#count-more').onclick = () => { state.count = Math.min(5, state.count + 1); updateCount(); };
$('#captions').onclick = () => { state.captions = !state.captions; $('#captions').setAttribute('aria-checked', String(state.captions)); };
function setFile(file) {
  state.file = file || null;
  $('#file-label').textContent = file ? file.name : 'Choose a video or drop it here';
  $('#form-error').hidden = true;
}
$('#video-file').onchange = (event) => setFile(event.target.files[0]);
const dropZone = $('#drop-zone');
['dragenter','dragover'].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.add('dragging'); }));
['dragleave','drop'].forEach((name) => dropZone.addEventListener(name, (event) => { event.preventDefault(); dropZone.classList.remove('dragging'); }));
dropZone.addEventListener('drop', (event) => { if (event.dataTransfer.files[0]) setFile(event.dataTransfer.files[0]); });
function showView(view) {
  state.view = view;
  $('#create-view').hidden = view !== 'create'; $('#library-view').hidden = view !== 'library';
  $('#nav-create').classList.toggle('active', view === 'create'); $('#nav-library').classList.toggle('active', view === 'library');
  $('#page-label').textContent = view === 'create' ? 'Create clips' : 'My clips';
  $('#view-all').hidden = view === 'library';
  renderJobs(); window.scrollTo({top:0, behavior:'smooth'});
}
$('#nav-create').onclick = () => showView('create');
$('#nav-library').onclick = () => showView('library');
$('#library-create').onclick = () => showView('create');
$('#view-all').onclick = () => showView('library');
const help = $('#help-dialog');
['#nav-help','#top-help','#learn-more'].forEach((id) => $(id).onclick = () => help.showModal());
document.querySelectorAll('.close-dialog,.close-help').forEach((button) => button.onclick = () => help.close());
help.addEventListener('click', (event) => { if (event.target === help) { const box = help.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) help.close(); } });
function time(seconds) { const s = Math.floor(seconds); return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`; }
function fileURL(jobId, file) { return `/api/jobs/${encodeURIComponent(jobId)}/files/${encodeURIComponent(file)}`; }
function sourceCard(job) {
  const id = job.video_id || (job.options.url || '').split('v=')[1];
  if (!/^[A-Za-z0-9_-]{11}$/.test(id || '')) return '';
  return `<div class="source-preview"><a href="https://www.youtube.com/watch?v=${id}" target="_blank" rel="noopener noreferrer" aria-label="Watch original video"><img src="https://i.ytimg.com/vi/${id}/hqdefault.jpg" alt="Original YouTube video thumbnail" loading="lazy"></a><div><span class="source-eyebrow">SOURCE VIDEO</span><h4>${escapeHTML(job.title)}</h4><p>${escapeHTML(job.channel || 'YouTube')}${job.source_duration ? ' · '+time(job.source_duration) : ''}</p><a href="https://www.youtube.com/watch?v=${id}" target="_blank" rel="noopener noreferrer">Watch original on YouTube ↗</a></div></div>`;
}
function renderJobs() {
  const videoStates = new Map();
  document.querySelectorAll('#jobs-list video').forEach((video) => videoStates.set(video.dataset.key, {time:video.currentTime, paused:video.paused}));
  const jobs = state.view === 'create' ? state.jobs.slice(0,3) : state.jobs;
  $('#project-count').textContent = state.jobs.length;
  $('#nav-count').textContent = state.jobs.reduce((sum, job) => sum + job.clips.length, 0);
  $('#empty-state').hidden = jobs.length > 0;
  $('#jobs-list').innerHTML = jobs.map((job) => `<article class="job-card" id="job-${job.id}">
    <div class="job-header"><div><h3>${escapeHTML(job.title)}</h3><span class="job-meta">${new Date(job.created).toLocaleDateString(undefined,{month:'short',day:'numeric'})} · ${job.options.length}s max · ${job.options.framing === 'fit' ? 'Blurred background' : 'Center crop'}${job.status === 'completed' ? ` · ${job.clips.length} clip${job.clips.length === 1 ? '' : 's'}` : ''}</span></div><div class="job-actions"><span class="status-pill ${job.status}">${escapeHTML(({completed:'Ready to share',failed:'Needs attention',queued:'In queue',uploading:'Uploading',processing:'Creating clips'})[job.status] || job.status)}</span>${['completed','failed'].includes(job.status) ? `<button class="delete-button" data-delete="${job.id}" aria-label="Delete ${escapeHTML(job.title)}">${icon('trash')}</button>` : ''}</div></div>
    ${sourceCard(job)}
    ${job.analyzed_duration && job.source_duration && job.analyzed_duration < job.source_duration ? `<p class="job-note">Analyzed first ${time(job.analyzed_duration)} of ${time(job.source_duration)}. Choose Entire video to search the full source.</p>` : ''}
    ${job.status === 'completed' ? `<p class="job-note">${job.options.speed === 'fast' ? 'Fast' : 'Balanced'} mode · ${job.options.resolution || 1080}p ${job.transcript_available ? ` · <a href="${fileURL(job.id,'transcript.txt')}" download>Download transcript</a>` : ''}</p>` : ''}
    ${job.status === 'failed' ? `<p class="job-error">${escapeHTML(job.message)}</p>` : ''}
    ${job.status === 'completed' && !job.speech_detected ? '<p class="job-note">No speech was detected. Here’s an opening excerpt without captions.</p>' : ''}
    ${['processing','queued','uploading'].includes(job.status) ? `<p class="job-note">${escapeHTML(job.message)} · ${job.progress}%</p>` : ''}
    ${job.clips.length ? `<div class="clip-grid">${job.clips.map((clip,index) => `<div class="clip-item"><video controls preload="none" playsinline poster="${fileURL(job.id,clip.poster)}" data-key="${job.id}-${index}" src="${fileURL(job.id,clip.file)}" aria-label="Clip ${index+1}: ${escapeHTML(clip.title)}"></video><h4>${escapeHTML(clip.title)}</h4><p>${time(clip.start)}–${time(clip.end)} · ${clip.duration}s<br>${escapeHTML(clip.reason)}</p><a class="download-link" href="${fileURL(job.id,clip.file)}?download=true" download>${icon('download')}Download MP4</a></div>`).join('')}</div>` : ''}
  </article>`).join('');
  document.querySelectorAll('#jobs-list video').forEach((video) => {
    const previous = videoStates.get(video.dataset.key);
    if (previous && previous.time > 0) { video.currentTime = previous.time; if (!previous.paused) video.play().catch(() => {}); }
  });
  $('#empty-state h3').textContent = state.view === 'create' ? 'Your next great clip is waiting.' : 'A blank canvas, for now.';
  $('#empty-state p').textContent = state.view === 'create' ? 'Add a video above and let’s make something worth watching.' : 'Create your first project and your clips will appear here.';
}
$('#jobs-list').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-delete]'); if (!button) return;
  button.disabled = true;
  try { await api(`/api/jobs/${button.dataset.delete}`,{method:'DELETE'}); state.jobs = state.jobs.filter((job) => job.id !== button.dataset.delete); renderJobs(); toast('Project and its files deleted.'); }
  catch (error) { button.disabled = false; toast(error.message); }
});
function updateProgress(job) {
  $('#progress-panel').hidden = !job || ['completed','failed'].includes(job.status);
  if (!job) return;
  $('#progress-message').textContent = job.message; $('#progress-percent').textContent = `${job.progress}%`;
  $('#progress-bar').style.width = `${job.progress}%`;
}
async function refresh() {
  try {
    const jobs = await api('/api/jobs');
    await updateAccount();
    if (JSON.stringify(jobs) !== JSON.stringify(state.jobs)) { state.jobs = jobs; renderJobs(); }
    const active = jobs.find((job) => ['processing','queued','uploading'].includes(job.status));
    const previous = jobs.find((job) => job.id === state.activeId);
    if (previous && ['completed','failed'].includes(previous.status)) {
      toast(previous.status === 'completed' ? 'Your clips are ready! Preview and download them below.' : 'This video needs attention. See the project for details.');
      state.activeId = null;
    }
    if (active) { state.activeId = active.id; updateProgress(active); }
    else { updateProgress(null); }
    $('#generate-button').disabled = state.submitting;
  } catch (error) { toast(`Studio connection lost. ${error.message}`); }
  finally { clearTimeout(polling); polling = setTimeout(refresh, state.activeId ? 1800 : 12000); }
}
$('#create-form').addEventListener('submit', async (event) => {
  event.preventDefault(); if (state.submitting) return;
  const errorBox = $('#form-error'); errorBox.hidden = true;
  try {
    const options = {length:state.length, count:state.count, framing:$('#framing').value, captions:state.captions, speed:$('#speed').value, analysis_minutes:Number($('#analysis-minutes').value), resolution:Number($('#resolution').value)};
    let path = '/api/jobs'; let request;
    if (state.source === 'link') {
      const url = $('#youtube-url').value.trim(); if (!url) throw new Error('Add a YouTube link to get started.');
      request = {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...options,url})};
    } else {
      if (!state.file) throw new Error('Choose a video from your computer first.');
      if (config.max_bytes && state.file.size > config.max_bytes) throw new Error('This video exceeds this server’s upload limit.');
      path = '/api/uploads/stream?' + new URLSearchParams(options);
      request = {method:'PUT',headers:{'Content-Type':'application/octet-stream','X-File-Name':encodeURIComponent(state.file.name)},body:state.file};
    }
    state.submitting = true; $('#generate-button').disabled = true;
    $('#generate-button').innerHTML = `${icon('clock')}Sending your video…`;
    const job = await api(path,request); state.activeId = job.id;
    state.jobs.unshift(job); renderJobs(); updateProgress(job);
    toast('Your video is in the studio. We’ll take it from here.');
    await refresh();
  } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
  finally { state.submitting = false; $('#generate-button').disabled = false; $('#generate-button').innerHTML = `${icon('spark')}Generate clips${icon('arrow')}`; }
});
async function init() {
  updateCount();
  config = await api('/api/config');
  account = await api('/api/me');
  if (!account) {
    $('#auth-gate').hidden = false;
    $('#auth-note').textContent = config.google_ready ? 'One Google account. No separate password needed.' : 'Google sign-in is being set up. Please check back soon.';
    if (!config.google_ready) {
      ['#google-signin','#google-signup'].forEach(id=> { $(id).removeAttribute('href'); $(id).setAttribute('aria-disabled','true'); });
    }
    return;
  }
  await updateAccount();
  try { const saved=JSON.parse(sessionStorage.getItem('clipstudio-qr')); if (saved?.id) openQR(saved); } catch { sessionStorage.removeItem('clipstudio-qr'); }
  await refresh();
  try {
    const health = await api('/api/health');
    const ready = health.requirements.ffmpeg && health.requirements.ffprobe;
    $('#system-status').innerHTML = `<span></span>${ready ? 'Your studio is ready' : 'FFmpeg setup needed'}`;
    if (!ready) toast('Install FFmpeg to start creating clips. See the README for setup steps.');
  } catch { $('#system-status').textContent = 'Studio is offline'; }
}
async function updateAccount() {
  account = await api('/api/me');
  if (!account) { location.reload(); return; }
  $('#account-label').textContent = account.local ? 'PRIVATE LOCAL MODE' : account.name;
  $('#access-status').textContent = account.owner ? 'Owner account · always free' : account.local ? 'Private local mode · no billing' : account.unlimited ? 'Premium active · keep creating' : account.trial_available ? 'Your first video is on us.' : 'Trial complete. Choose your plan.';
  $('#signout-button').hidden = !!account.local;
}
function showPlans() {
  $('#billing-note').textContent = account?.owner ? 'Your verified owner account is free, with no subscription needed.' : config.payments_ready ? 'Scan a UPI QR to pay. Premium activates after payment is confirmed.' : 'Payments are not activated yet. Please check back soon.';
  document.querySelectorAll('[data-plan]').forEach(button=>button.disabled = !config.payments_ready || !!account?.unlimited || !!account?.local);
  $('#manage-subscription').hidden = !account?.has_subscription;
  $('#pricing-dialog').showModal();
}
['#plans-button','#sidebar-plans','#auth-plans'].forEach(id=>$(id).onclick=showPlans);
$('#manage-subscription').onclick=async()=>{try{const r=await api('/api/billing/portal',{method:'POST'});location.href=r.url;}catch(e){toast(e.message);}};
$('#close-pricing').onclick=()=>$('#pricing-dialog').close();
$('#signout-button').onclick=async()=>{await api('/api/logout',{method:'POST'});location.reload();};
let qrCurrent = null;
let qrTimer = null;
let qrPolling = false;
async function updateQR() {
  if (!qrCurrent || qrPolling) return;
  const remaining = Math.max(0, Math.ceil(qrCurrent.expires - Date.now()/1000));
  $('#qr-countdown').textContent = remaining ? `Expires in ${Math.floor(remaining/60)}:${String(remaining%60).padStart(2,'0')}` : 'QR expired';
  if (!remaining) { $('#qr-image').hidden = true; $('.qr-image-wrap').hidden = true; $('#new-qr').hidden = false; $('#qr-status').textContent = 'This QR has expired. Choose a new one to pay.'; }
  if (qrCurrent.nextPoll > Date.now()) return;
  qrPolling = true; qrCurrent.nextPoll = Date.now() + 3000;
  try {
    const current = await api(`/api/billing/qr/${encodeURIComponent(qrCurrent.id)}`);
    if (current.status === 'paid') {
      clearInterval(qrTimer); qrCurrent = null; sessionStorage.removeItem('clipstudio-qr');
      $('#qr-image').hidden = true; $('.qr-image-wrap').hidden = true; $('#qr-countdown').textContent = '';
      $('#qr-status').textContent = 'Premium activated!'; $('#new-qr').hidden = true;
      await updateAccount();
    }
  } catch (error) { $('#qr-status').textContent = `Waiting for confirmation. ${error.message}`; }
  finally { qrPolling = false; }
}
function openQR(result) {
  qrCurrent = {...result, nextPoll:0};
  sessionStorage.setItem('clipstudio-qr', JSON.stringify(result));
  $('#qr-title').textContent = `Pay ₹${(result.amount/100).toLocaleString('en-IN')} · ${result.plan === 'monthly' ? 'one month' : 'one year'}`;
  $('#qr-image').src = result.image_url; $('#qr-image').hidden = false; $('.qr-image-wrap').hidden = false;
  $('#new-qr').hidden = true; $('#qr-status').textContent = 'Waiting for payment confirmation…';
  if ($('#pricing-dialog').open) $('#pricing-dialog').close();
  if (!$('#qr-dialog').open) $('#qr-dialog').showModal();
  clearInterval(qrTimer); updateQR(); qrTimer = setInterval(updateQR, 1000);
}
$('#close-qr').onclick=()=>$('#qr-dialog').close();
$('#new-qr').onclick=()=>{ clearInterval(qrTimer); qrCurrent=null; sessionStorage.removeItem('clipstudio-qr'); $('#qr-dialog').close(); showPlans(); };
document.querySelectorAll('[data-plan]').forEach(button=>button.onclick=async()=>{
  if (!account) { location.href='/auth/google'; return; }
  button.disabled=true;
  try { const result=await api('/api/billing/qr',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plan:button.dataset.plan})}); openQR(result); }
  catch(error) { toast(error.message);button.disabled=false; }
});
if (new URLSearchParams(location.search).get('login') === 'failed') toast('Google sign-in could not finish. Please try again.');
if (new URLSearchParams(location.search).get('payment') === 'received') toast('Payment received by checkout. Access updates after server confirmation.');
init().catch(error=>toast(error.message));
