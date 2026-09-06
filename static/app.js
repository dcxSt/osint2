const $ = (id) => document.getElementById(id);
let activeJob = null;
let polling = null;
let activeTab = 'summary';
let lastReport = null;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function link(text, url, className) {
  const node = el('a', className, text);
  try {
    const parsed = new URL(url);
    if (!['https:', 'http:'].includes(parsed.protocol)) return node;
    node.href = parsed.href;
    node.target = '_blank';
    node.rel = 'noopener noreferrer';
  } catch (_) { /* Untrusted URLs remain plain text. */ }
  return node;
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Request failed');
  return data;
}
function showError(message) {
  $('form-error').textContent = message;
  $('form-error').hidden = !message;
}
function setTab(name, focus = false) {
  activeTab = name;
  for (const button of document.querySelectorAll('[data-tab]')) {
    const selected = button.dataset.tab === name;
    button.classList.toggle('active', selected);
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
    if (selected && focus) button.focus();
    $(`panel-${button.dataset.tab}`).hidden = !selected;
  }
}
document.querySelectorAll('[data-tab]').forEach((button) => {
  button.addEventListener('click', () => setTab(button.dataset.tab));
  button.addEventListener('keydown', (event) => {
    const names = ['summary', 'sources', 'activity'];
    let index = names.indexOf(activeTab);
    if (event.key === 'ArrowRight') index = (index + 1) % names.length;
    else if (event.key === 'ArrowLeft') index = (index + names.length - 1) % names.length;
    else if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = names.length - 1;
    else return;
    event.preventDefault();
    setTab(names[index], true);
  });
});

function render(report) {
  lastReport = report;
  const running = ['running', 'queued'].includes(report.status);
  $('empty-state').hidden = true;
  $('results').hidden = false;
  $('profile-name').textContent = report.name;
  $('seed-link').textContent = report.seed;
  $('seed-link').href = report.seed;
  const status = running ? 'RESEARCH IN PROGRESS' : report.status === 'cancelled' ? 'STOPPED · PARTIAL REPORT' : report.status === 'error' ? 'ERROR · PARTIAL REPORT' : report.sources.length ? 'RESEARCH COMPLETE' : 'COMPLETE · NO READABLE SOURCES';
  if ($('report-status').textContent !== status) $('report-status').textContent = status;
  $('report-status').classList.toggle('running', running);
  $('cancel').hidden = !running;
  $('cancel').textContent = report.events.some((e) => e.message === 'Cancellation requested') ? 'Stopping…' : 'Stop research';
  $('submit').disabled = running;
  $('submit').textContent = running ? 'Researching…' : 'Research profile ↗';
  $('source-count').textContent = report.sources.length;
  $('finding-count').textContent = report.summary.reduce((sum, group) => sum + group.items.length, 0);
  $('project-count').textContent = report.work.length;
  $('gap-count').textContent = report.gaps.length;
  $('export-md').href = `/api/jobs/${report.id}/report.md`;
  $('export-json').href = `/api/jobs/${report.id}/report.json`;

  const summary = document.createDocumentFragment();
  if (!report.summary.length) {
    summary.append(el('div', 'pending', running ? 'Following public links. Sourced findings will appear here as they arrive.' : 'No biographical excerpts could be established from the accessible pages. Check Activity & gaps for details, or start with the person’s own public website.'));
  }
  for (const group of report.summary) {
    const section = el('section', 'finding-group');
    section.append(el('h3', '', group.title));
    for (const item of group.items) {
      const card = el('article', 'finding');
      card.append(el('p', '', item.text));
      const meta = el('div', 'finding-meta');
      meta.append(link(item.source_id + ' ↗', item.source_url, 'source-tag'), el('span', '', item.kind));
      card.append(meta);
      section.append(card);
    }
    summary.append(section);
  }
  if (report.sources.length && !report.summary.some((g) => g.title === 'Background')) {
    summary.append(el('p', 'method-note', 'Background: no explicit statement about where this person grew up was found. Location is not inferred from photos, names, or connections.'));
  }
  $('summary-content').replaceChildren(summary);

  const work = document.createDocumentFragment();
  if (report.work.length) {
    work.append(el('h3', 'work-heading', 'Public repositories'));
    work.append(el('p', 'method-note', 'Up to 12 recently updated owned repositories per GitHub profile, excluding forks. Repository ownership does not establish sole authorship.'));
    const grid = el('div', 'work-grid');
    for (const repo of report.work) {
      const card = el('article', 'repo');
      const heading = el('h4');
      heading.append(link(repo.title + ' ↗', repo.url));
      card.append(heading, el('p', '', repo.description || 'No public description provided.'), el('div', 'repo-meta', [repo.language, '☆ ' + repo.stars, repo.updated && 'Updated ' + repo.updated].filter(Boolean).join(' · ')));
      grid.append(card);
    }
    work.append(grid);
  }
  $('work-content').replaceChildren(work);

  const sources = document.createDocumentFragment();
  for (const source of report.sources) {
    const card = el('article', 'source-card');
    const heading = el('h3');
    heading.append(el('span', 'source-tag', source.id), link(source.title + ' ↗', source.url));
    card.append(heading, link(source.url, source.url, 'source-url'));
    if (source.description) card.append(el('p', '', source.description));
    card.append(el('div', 'source-details', `${source.parent ? source.parent + ' → ' + source.id + ' · ' : ''}${source.relationship} · Depth ${source.depth} · ${source.adapter} · Retrieved ${new Date(source.retrieved).toLocaleString()}`));
    if (source.data_url) card.append(link('View underlying public API ↗', source.data_url, 'source-url'));
    if (source.image_captions.length) {
      const details = el('details');
      details.append(el('summary', '', 'Publisher-provided image captions (' + source.image_captions.length + ')'));
      const list = el('ul');
      source.image_captions.forEach((caption) => list.append(el('li', '', caption)));
      details.append(el('p', '', 'Text from image alt attributes. No image recognition or face matching is performed.'), list);
      card.append(details);
    }
    sources.append(card);
  }
  if (!report.sources.length) sources.append(el('div', 'pending', running ? 'Waiting for the first readable source…' : 'No sources collected. See the coverage gaps for the reason.'));
  $('sources-content').replaceChildren(sources);
  const candidates = document.createDocumentFragment();
  if (report.candidates.length) {
    candidates.append(el('h3', 'subheading', 'Unconfirmed profile links'), el('p', 'method-note', 'These links were found on a source, but lack an explicit identity relationship. They were not crawled or merged into the summary.'));
    for (const candidate of report.candidates) {
      const item = el('div', 'candidate');
      item.append(link(candidate.url + ' ↗', candidate.url), el('small', '', 'Found on ' + candidate.from));
      candidates.append(item);
    }
  }
  $('candidates-content').replaceChildren(candidates);
  const gaps = document.createDocumentFragment();
  if (report.gaps.length) gaps.append(el('h3', 'subheading', 'Coverage gaps'));
  for (const gap of report.gaps) {
    const card = el('div', 'gap');
    card.append(link(gap.url, gap.url), el('p', '', gap.reason));
    gaps.append(card);
  }
  $('gaps-content').replaceChildren(gaps);
  const activity = document.createDocumentFragment();
  for (const event of report.events) {
    const row = el('div', 'log-line');
    row.append(el('time', '', event.time + ' UTC'), document.createTextNode(event.message));
    activity.append(row);
  }
  $('activity-content').replaceChildren(activity);
  return running;
}

async function poll(id) {
  try {
    const report = await api(`/api/jobs/${id}`);
    if (activeJob !== id) return;
    showError('');
    if (render(report)) polling = setTimeout(() => poll(id), 1200);
  } catch (error) {
    if (activeJob !== id) return;
    showError('Could not refresh the report: ' + error.message);
    $('submit').disabled = false;
    $('submit').textContent = 'Research profile ↗';
    // A transient disconnect should not discard an active session.
    polling = setTimeout(() => poll(id), 5000);
  }
}
$('research-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  showError('');
  $('submit').disabled = true;
  try {
    const data = await api('/api/jobs', {url: $('profile-url').value.trim(), pages: Number($('pages').value), depth: Number($('depth').value)});
    clearTimeout(polling);
    activeJob = data.id;
    sessionStorage.setItem('fieldnotes-job', activeJob);
    setTab('summary');
    await poll(activeJob);
  } catch (error) {
    showError(error.message);
    $('submit').disabled = false;
  }
});
$('cancel').addEventListener('click', async () => {
  if (!activeJob) return;
  $('cancel').disabled = true;
  try {
    await api(`/api/jobs/${activeJob}/cancel`, {});
    $('cancel').textContent = 'Stopping…';
  } catch (error) { showError(error.message); }
  finally { $('cancel').disabled = false; }
});
$('new-research').addEventListener('click', () => {
  $('profile-url').focus();
  $('profile-url').select();
  window.scrollTo({top: 0, behavior: 'smooth'});
});
const saved = sessionStorage.getItem('fieldnotes-job');
if (saved && /^[a-f0-9]{32}$/.test(saved)) {
  api(`/api/jobs/${saved}`).then((report) => {
    activeJob = saved;
    $('profile-url').value = report.seed;
    $('pages').value = report.limits.pages;
    $('depth').value = report.limits.depth;
    if (render(report)) polling = setTimeout(() => poll(saved), 1200);
  }).catch(() => sessionStorage.removeItem('fieldnotes-job'));
}
