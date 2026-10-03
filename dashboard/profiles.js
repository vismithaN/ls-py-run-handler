'use strict';

// Curated results are loaded from files, not generated or profiled in the browser.
const profileState = { data: null, stage: '', signature: '', pending: false };

function selectDashboardView(view) {
  const profiles = view === 'profiles';
  const changed = byId('profile-panel').hidden === profiles;
  byId('benchmark-panel').hidden = profiles;
  byId('profile-panel').hidden = !profiles;
  if (changed) window.scrollTo(0, 0);
  updateProfileSectionNav();
  document.querySelectorAll('[data-view]').forEach(button => {
    const selected = button.dataset.view === view;
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  tooltip.hidden = true;
  history.replaceState(null, '', profiles ? '#profiles' : '#benchmarks');
  if (profiles) {
    byId('status').textContent = 'Saved profile findings · checks every 3s';
    refreshProfiles();
  } else {
    if (state.data) renderCharts();
    refresh();
  }
}

const dashboardTabs = [...document.querySelectorAll('[data-view]')];
const sidebarToggle = byId('sidebar-toggle');
const sidebarLayout = byId('dashboard-layout');
const sidebarViews = byId('dashboard-views');
const profileSectionNav = byId('profile-section-nav');

function updateProfileSectionNav() {
  profileSectionNav.hidden = sidebarLayout.classList.contains('sidebar-collapsed') ||
    byId('profile-panel').hidden || !profileState.data?.stages.length;
}

function setSidebarCollapsed(collapsed) {
  sidebarLayout.classList.toggle('sidebar-collapsed', collapsed);
  sidebarViews.hidden = collapsed;
  sidebarToggle.setAttribute('aria-expanded', String(!collapsed));
  sidebarToggle.setAttribute('aria-label', collapsed ? 'Expand navigation' : 'Collapse navigation');
  sidebarToggle.querySelector('.sidebar-toggle-icon').textContent = collapsed ? '›' : '‹';
  sidebarToggle.querySelector('.sidebar-toggle-label').textContent = collapsed ? 'Expand menu' : 'Collapse menu';
  updateProfileSectionNav();
}

try {
  setSidebarCollapsed(localStorage.getItem('benchmark-dashboard-sidebar') === 'collapsed');
} catch {
  setSidebarCollapsed(false);
}
sidebarToggle.addEventListener('click', () => {
  const collapsed = !sidebarLayout.classList.contains('sidebar-collapsed');
  setSidebarCollapsed(collapsed);
  try { localStorage.setItem('benchmark-dashboard-sidebar', collapsed ? 'collapsed' : 'expanded'); } catch { /* Storage is optional. */ }
});

document.querySelectorAll('[data-profile-section]').forEach(button => {
  button.addEventListener('click', () => {
    byId(button.dataset.profileSection)?.scrollIntoView({ block: 'start' });
  });
});

dashboardTabs.forEach((button, index) => {
  button.addEventListener('click', () => selectDashboardView(button.dataset.view));
  button.addEventListener('keydown', event => {
    let next;
    if (event.key === 'ArrowDown') next = (index + 1) % dashboardTabs.length;
    if (event.key === 'ArrowUp') next = (index - 1 + dashboardTabs.length) % dashboardTabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = dashboardTabs.length - 1;
    if (next == null) return;
    event.preventDefault();
    dashboardTabs[next].focus();
    selectDashboardView(dashboardTabs[next].dataset.view);
  });
});
window.addEventListener('hashchange', () => selectDashboardView(location.hash === '#profiles' ? 'profiles' : 'benchmarks'));

function profileTable(title, headers, rows, numeric = []) {
  const wrapper = html('div', 'value-table-wrap');
  const table = html('table', 'profile-table');
  table.setAttribute('aria-label', title);
  const head = html('thead');
  const labels = html('tr');
  headers.forEach((label, index) => {
    const cell = html('th', numeric.includes(index) ? 'numeric' : '', label);
    cell.scope = 'col';
    labels.append(cell);
  });
  head.append(labels);
  const body = html('tbody');
  rows.forEach(values => {
    const row = html('tr');
    values.forEach((value, index) => {
      const cell = html('td', numeric.includes(index) ? 'numeric' : '');
      if (value instanceof Node) cell.append(value); else cell.textContent = value;
      row.append(cell);
    });
    body.append(row);
  });
  table.append(head, body);
  wrapper.append(table);
  return wrapper;
}

function profileSource(stage, id, label = 'Open source report') {
  const source = stage.sources[id];
  if (!source?.available) {
    const reason = source?.status === 'changed' ? 'changed since baseline' : 'missing';
    return html('span', 'profile-source source-missing', `${label} · ${reason}`);
  }
  const link = html('a', 'profile-source', label);
  link.href = source.url;
  link.target = '_blank';
  link.rel = 'noopener';
  return link;
}

function profileSection(title, description) {
  const section = html('section', 'profile-section');
  section.append(html('h2', '', title), html('p', '', description));
  return section;
}

function profileNotes(title, paragraphs) {
  const notes = html('details', 'profile-notes');
  notes.append(html('summary', '', title));
  paragraphs.forEach(text => notes.append(html('p', 'profile-scope', text)));
  return notes;
}

function cpuSeconds(value) {
  return value > 0 && value < 0.001 ? '<0.001' : number(value, 3);
}

function renderCpuProfiles(stage) {
  const cpu = stage.cpu;
  const section = profileSection('CPU profiling results', cpu.procedure);
  section.id = 'profile-cpu';
  section.append(html('p', 'profile-meta', cpu.profiler), html('p', '', cpu.reading_guide));
  if (cpu.phase_medians?.length) {
    const phases = html('article', 'profile-card cpu-card');
    phases.append(html('h3', '', 'Cold CPU by phase · median of 3 processes'));
    phases.append(profileTable('Cold CPU phase medians in seconds', ['Workload', 'Imports', 'Startup', 'First request', 'Total'], cpu.phase_medians.map(row => [row.label, cpuSeconds(row.imports_s), cpuSeconds(row.startup_s), cpuSeconds(row.first_request_s), cpuSeconds(row.total_s)]), [1, 2, 3, 4]));
    phases.append(profileSource(stage, 'cold-cpu-summary', 'Download 12-capture summary'));
    section.append(phases);
  }
  const grid = html('div', 'profile-grid');
  cpu.workloads.forEach(workload => {
    const card = html('article', 'profile-card cpu-card');
    card.append(html('h3', '', workload.label), html('p', 'profile-meta', 'Function calls · session 1 · one cold capture'));
    const rows = workload.functions.map(result => {
      const functionName = html('div', '', result.label);
      functionName.title = result.function;
      return [functionName, number(result.calls, 0), cpuSeconds(result.self_s), cpuSeconds(result.inclusive_s)];
    });
    card.append(profileTable(`${workload.label} CPU results`, ['Function', 'Calls', 'Self CPU (s)', 'Inclusive CPU (s)'], rows, [1, 2, 3]));
    card.append(profileSource(stage, workload.source_id, `Open ${workload.label} CPU text`));
    grid.append(card);
  });
  section.append(grid, profileNotes('CPU scope and limitations', [cpu.scope, cpu.limitation]));
  return section;
}

function renderMemoryProfiles(stage) {
  const memory = stage.memory;
  const section = profileSection('Memory profiling results', 'Peak tracked allocation · whole benchmark test · not server-only RAM or RSS.');
  section.id = 'profile-memory';
  section.append(html('p', 'profile-meta', memory.profiler));
  const chart = html('article', 'profile-card memory-card');
  chart.append(html('h3', '', 'Peak tracked allocation by workload'), html('p', 'profile-meta', 'Whole benchmark test · MiB · not server-only RAM'));
  const bars = html('div', 'memory-bars');
  const maximum = Math.max(...memory.workloads.map(workload => workload.peak_mib), 1);
  memory.workloads.forEach((workload, index) => {
    const row = html('div');
    row.style.setProperty('--color', `var(--series-${index % 4})`);
    const heading = html('div', 'memory-bar-heading');
    heading.append(html('span', '', workload.label), html('strong', '', `${number(workload.peak_mib)} MiB`));
    const track = html('div', 'memory-bar-track');
    const fill = html('div', 'memory-bar-fill');
    fill.style.width = `${workload.peak_mib / maximum * 100}%`;
    track.append(fill);
    track.setAttribute('aria-hidden', 'true');
    row.append(heading, track);
    bars.append(row);
  });
  chart.append(bars);
  section.append(chart, html('p', '', memory.reading_guide));
  const grid = html('div', 'profile-grid');
  memory.workloads.forEach(workload => {
    const card = html('article', 'profile-card memory-card');
    const peak = html('div', 'profile-peak', number(workload.peak_mib));
    peak.append(html('small', '', ' MiB peak'));
    card.append(html('h3', '', workload.label), peak);
    const allocations = html('details', 'profile-notes');
    allocations.append(html('summary', '', 'Allocation breakdown'), profileTable(`${workload.label} allocations at peak`, ['Allocation group', 'MiB at peak'], workload.allocations.map(result => [result.label, number(result.mib)]), [1]));
    card.append(allocations, profileSource(stage, workload.source_id, 'Download Memray recording'));
    grid.append(card);
  });
  section.append(grid, profileNotes('Memory scope and limitations', [memory.scope, memory.reading_guide, memory.limitation]));
  return section;
}

function renderProfileFindings(stage) {
  const results = stage.finding_mode === 'results';
  const section = profileSection(results ? 'Results of this optimization' : 'Findings → next experiments', results ? 'Profile evidence for this stage. Benchmark speed is shown in Benchmark graphs.' : 'Targets below are hypotheses, not measured improvements. ↓ = expected decrease.');
  section.id = 'profile-findings';
  const card = html('article', 'profile-card');
  const rows = stage.findings.map(finding => {
    const evidence = html('div', '', finding.evidence);
    evidence.append(html('div', 'profile-meta', finding.evidence_type));
    return [evidence, finding.interpretation, finding.optimization, finding.validation, finding.status];
  });
  card.append(profileTable('Evidence and optimization hypotheses', results ? ['Observed number', 'Meaning', 'Change', 'Check', 'Status'] : ['Observed number', 'Problem', 'Next change', 'Check / expected effect', 'Status'], rows));
  section.append(card);
  return section;
}

function renderProfileInterpretation() {
  const data = profileState.data;
  const select = byId('profile-stage');
  select.replaceChildren();
  data.stages.forEach(stage => {
    const option = html('option', '', `${stage.title} · ${stage.cpu.condition} CPU`);
    option.value = stage.id;
    select.append(option);
  });
  const stage = data.stages.find(item => item.id === profileState.stage) || data.stages[0];
  profileState.stage = stage?.id || '';
  select.value = profileState.stage;
  select.disabled = data.stages.length < 2;
  byId('profile-empty').hidden = Boolean(stage);
  updateProfileSectionNav();
  byId('profile-download').hidden = !stage;
  byId('profile-print').disabled = !stage;
  const content = byId('profile-content');
  content.replaceChildren();
  if (!stage) return;
  byId('profile-download').href = stage.download_url;
  const heading = html('div', 'profile-heading');
  const verification = stage.source_verification;
  const complete = verification.verified === verification.total;
  heading.append(html('h2', '', `${stage.title} · profile interpretation`), html('span', 'profile-badge', `${stage.cpu.condition} CPU`), html('span', `profile-badge ${complete ? '' : 'source-lock-warning'}`, `${verification.verified}/${verification.total} source files verified`), html('p', '', `Commit ${stage.commit.slice(0, 7)} · ${stage.review_status}`));
  const overview = html('div', 'profile-overview');
  overview.append(html('p', '', stage.summary));
  const limitations = html('details', 'profile-notes');
  limitations.append(html('summary', '', 'Reporting limitations'));
  const list = html('ul', 'profile-limitations');
  stage.limitations.forEach(text => list.append(html('li', '', text)));
  limitations.append(list);
  const sources = html('details', 'profile-sources');
  sources.append(html('summary', '', 'Pinned reports and calculation rules'));
  sources.append(html('p', 'profile-meta', `Saved interpretation: reports/interpretations/${stage.id}.json · SHA-256 checked against every raw file. Changed or missing files are flagged; displayed numbers remain the saved interpretation.`));
  sources.append(profileTable('Profile metrics and pinned input reports', ['Displayed metric', 'Pinned report IDs', 'Calculation'], stage.metric_sources.map(item => [item.metric, item.source_ids.join(', '), item.rule])));
  sources.append(html('h3', '', 'Exact raw files'));
  const links = html('ul');
  Object.entries(stage.sources).forEach(([id, source]) => {
    const row = html('li');
    const fingerprint = html('code', '', `${source.path} · SHA-256 ${source.sha256.slice(0, 12)}… · ${source.size_bytes} bytes`);
    fingerprint.title = source.sha256;
    row.append(profileSource(stage, id, source.label), fingerprint);
    links.append(row);
  });
  sources.append(links);
  content.append(heading, overview, renderCpuProfiles(stage), renderMemoryProfiles(stage), renderProfileFindings(stage), limitations, sources);
}

async function refreshProfiles() {
  if (profileState.pending) return;
  profileState.pending = true;
  try {
    const response = await fetch('/api/interpretations', { cache: 'no-store' });
    if (!response.ok) throw new Error(`Profile server returned ${response.status}`);
    const data = await response.json();
    const signature = JSON.stringify(data);
    if (signature !== profileState.signature) {
      profileState.data = data;
      renderProfileInterpretation();
      profileState.signature = signature;
    }
    byId('profile-warnings').textContent = data.warnings.join(' · ');
    byId('profile-warnings').hidden = !data.warnings.length;
    if (!byId('profile-panel').hidden) byId('status').textContent = `Saved findings · ${data.stages.length} stage${data.stages.length === 1 ? '' : 's'} · checks every 3s`;
  } catch (error) {
    byId('profile-warnings').textContent = `Could not load saved interpretations. Existing results are retained. ${error.message}`;
    byId('profile-warnings').hidden = false;
  } finally { profileState.pending = false; }
}

byId('profile-stage').addEventListener('change', () => {
  profileState.stage = byId('profile-stage').value;
  renderProfileInterpretation();
});
byId('profile-print').addEventListener('click', () => window.print());
selectDashboardView(location.hash === '#profiles' ? 'profiles' : 'benchmarks');
refreshProfiles();
setInterval(refreshProfiles, 3000);
