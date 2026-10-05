'use strict';

// Curated results are loaded from files, not generated or profiled in the browser.
const profileState = { data: null, stage: '', report: 'cpu', signature: '', pending: false, charts: [] };
const profileChartObserver = new ResizeObserver(entries => {
  entries.forEach(entry => entry.target.drawProfileChart?.());
});

function selectDashboardView(view) {
  const profiles = view === 'profiles';
  const changed = byId('profile-panel').hidden === profiles;
  byId('benchmark-panel').hidden = profiles;
  byId('profile-panel').hidden = !profiles;
  if (changed) window.scrollTo(0, 0);
  updateProfileStageNav();
  document.querySelectorAll('[data-view]').forEach(button => {
    const selected = button.dataset.view === view;
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  tooltip.hidden = true;
  history.replaceState(null, '', profiles ? '#profiles' : '#benchmarks');
  if (profiles) {
    byId('status').textContent = 'Saved profile comparisons · checks every 3s';
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
const profileStageNav = byId('profile-stage-nav');

function updateProfileStageNav() {
  profileStageNav.hidden = sidebarLayout.classList.contains('sidebar-collapsed') ||
    byId('profile-panel').hidden || !profileState.data?.stages.length;
}

function setSidebarCollapsed(collapsed) {
  sidebarLayout.classList.toggle('sidebar-collapsed', collapsed);
  sidebarViews.hidden = collapsed;
  sidebarToggle.setAttribute('aria-expanded', String(!collapsed));
  sidebarToggle.setAttribute('aria-label', collapsed ? 'Expand navigation' : 'Collapse navigation');
  sidebarToggle.querySelector('.sidebar-toggle-icon').textContent = collapsed ? '›' : '‹';
  sidebarToggle.title = collapsed ? 'Expand navigation' : 'Collapse navigation';
  updateProfileStageNav();
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

const profileReportTabs = [...document.querySelectorAll('[data-profile-report]')];

function selectProfileReport(report) {
  profileState.report = report;
  profileReportTabs.forEach(button => {
    const selected = button.dataset.profileReport === report;
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  if (profileState.data) renderProfileInterpretation();
}

profileReportTabs.forEach((button, index) => {
  button.addEventListener('click', () => selectProfileReport(button.dataset.profileReport));
  button.addEventListener('keydown', event => {
    let next;
    if (event.key === 'ArrowRight') next = (index + 1) % profileReportTabs.length;
    if (event.key === 'ArrowLeft') next = (index - 1 + profileReportTabs.length) % profileReportTabs.length;
    if (event.key === 'Home') next = 0;
    if (event.key === 'End') next = profileReportTabs.length - 1;
    if (next == null) return;
    event.preventDefault();
    profileReportTabs[next].focus();
    selectProfileReport(profileReportTabs[next].dataset.profileReport);
  });
});

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
    const reason = source?.status === 'changed' ? 'changed since capture' : 'missing';
    return html('span', 'profile-source source-missing', `${label} · ${reason}`);
  }
  const link = html('a', 'profile-source', label);
  link.href = source.url;
  link.target = '_blank';
  link.rel = 'noopener';
  return link;
}

function profileSection(title, description, id) {
  const section = html('section', 'profile-section');
  section.id = id;
  section.append(html('h2', '', title), html('p', '', description));
  return section;
}

function cpuSeconds(value) {
  return value > 0 && value < 0.001 ? '<0.001' : number(value, 3);
}

function percentChange(before, after) {
  if (!Number.isFinite(before) || !Number.isFinite(after) || before === 0) return null;
  return (after / before - 1) * 100;
}

function improvementText(before, after) {
  const change = percentChange(before, after);
  if (change == null) return 'No comparison';
  if (before > 0 && after === 0) return 'eliminated';
  if (Math.abs(change) < 5) return 'unchanged';
  return `${Math.abs(change).toLocaleString('en-US', { maximumFractionDigits: 1 })}% ${change < 0 ? 'lower' : 'higher'}`;
}

function improvementClass(before, after) {
  const change = percentChange(before, after);
  if (change == null || Math.abs(change) < 5) return 'neutral';
  return change < 0 ? 'improved' : 'regressed';
}

function measuredValue(value, digits) {
  const threshold = 10 ** -digits;
  return value > 0 && value < threshold ? `<${number(threshold, digits)}` : number(value, digits);
}

function stageColor(index) {
  return index === 0 ? 'var(--baseline)' : `var(--series-${(index - 1) % 4})`;
}

function stageLegend(stages) {
  const legend = html('div', 'profile-stage-legend');
  stages.forEach((stage, index) => {
    const item = html('span', 'profile-stage-key');
    const swatch = html('span', 'profile-stage-swatch');
    swatch.style.setProperty('--stage-color', stageColor(index));
    item.append(swatch, html('span', '', stage.title));
    legend.append(item);
  });
  return legend;
}

function workload(section, id) {
  return section?.workloads?.find(item => item.id === id);
}

function phase(stage, id) {
  return stage.cpu.phase_medians?.find(item => item.id === id);
}

function functionResult(stage, workloadId, label) {
  return workload(stage.cpu, workloadId)?.functions?.find(item => item.label === label);
}

function allocation(stage, workloadId, labels) {
  const accepted = Array.isArray(labels) ? labels : [labels];
  const groups = workload(stage.memory, workloadId)?.allocations;
  if (!groups) return undefined;
  const matches = groups.filter(item => accepted.includes(item.label));
  if (!matches.length && !stage.memory.grouping) return undefined;
  return matches.reduce((total, item) => total + item.mib, 0);
}

function axisScale(maximum) {
  const rawStep = Math.max(maximum / 4, Number.EPSILON);
  const magnitude = 10 ** Math.floor(Math.log10(rawStep));
  const normalized = rawStep / magnitude;
  const factor = [1, 2, 2.5, 5, 10].find(item => item >= normalized) || 10;
  const step = factor * magnitude;
  const top = Math.ceil(maximum / step) * step;
  return { step, top, ticks: Array.from({ length: Math.round(top / step) + 1 }, (_, index) => index * step) };
}

function appendWrappedSvgLabel(parent, x, y, label, availableWidth) {
  const words = label.split(' ');
  const lines = [];
  const approximateCharacters = Math.max(Math.floor(availableWidth / 7), 10);
  words.forEach(word => {
    const current = lines.at(-1);
    if (!current || `${current} ${word}`.length > approximateCharacters) lines.push(word);
    else lines[lines.length - 1] = `${current} ${word}`;
  });
  while (lines.length > 2) lines[1] = `${lines[1]} ${lines.pop()}`;
  const text = svgNode('text', { class: 'profile-svg-x-label', x, y, 'text-anchor': 'middle' });
  lines.forEach((line, index) => text.append(svgNode('tspan', { x, dy: index ? 15 : 0 }, line)));
  parent.append(text);
}

function drawProfileComparisonChart(plot, stages, rows, unit, digits, axes, title) {
  const svg = plot.querySelector('svg');
  svg.replaceChildren();
  const width = Math.max(plot.clientWidth || 0, rows.length * 135, 620);
  const height = 390;
  const margin = { top: 38, right: 22, bottom: 92, left: 78 };
  const innerWidth = width - margin.left - margin.right;
  const innerHeight = height - margin.top - margin.bottom;
  const values = rows.flatMap(row => row.values).filter(Number.isFinite);
  const maximum = Math.max(...values, Number.EPSILON);
  const scale = axisScale(maximum);
  const xStep = innerWidth / rows.length;
  const groupWidth = Math.min(xStep * 0.76, 118);
  const barGap = Math.min(8, groupWidth * 0.08);
  const barWidth = Math.max(Math.min((groupWidth - barGap * (stages.length - 1)) / stages.length, 42), 12);
  const actualGroupWidth = barWidth * stages.length + barGap * (stages.length - 1);
  const baselineY = margin.top + innerHeight;
  const y = value => margin.top + innerHeight - value / scale.top * innerHeight;
  const tickDigits = scale.top < 1 ? Math.min(digits, 3) : scale.top < 10 ? Math.min(digits, 2) : scale.top < 100 ? 1 : 0;

  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('width', width);
  svg.setAttribute('height', height);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `${title}. ${axes.y} by ${axes.x}, comparing ${stages.map(stage => stage.title).join(', ')}.`);
  svg.append(svgNode('title', {}, title));

  scale.ticks.forEach(tick => {
    const tickY = y(tick);
    svg.append(svgNode('line', { class: 'profile-svg-grid', x1: margin.left, x2: width - margin.right, y1: tickY, y2: tickY }));
    svg.append(svgNode('text', { class: 'profile-svg-tick', x: margin.left - 10, y: tickY + 4, 'text-anchor': 'end' }, number(tick, tickDigits)));
  });
  svg.append(svgNode('line', { class: 'profile-svg-axis', x1: margin.left, x2: margin.left, y1: margin.top, y2: baselineY }));
  svg.append(svgNode('line', { class: 'profile-svg-axis', x1: margin.left, x2: width - margin.right, y1: baselineY, y2: baselineY }));
  svg.append(svgNode('text', { class: 'profile-svg-axis-title', x: 18, y: margin.top + innerHeight / 2, transform: `rotate(-90 18 ${margin.top + innerHeight / 2})`, 'text-anchor': 'middle' }, axes.y));
  svg.append(svgNode('text', { class: 'profile-svg-axis-title', x: margin.left + innerWidth / 2, y: height - 12, 'text-anchor': 'middle' }, axes.x));

  rows.forEach((row, rowIndex) => {
    const center = margin.left + xStep * rowIndex + xStep / 2;
    const startX = center - actualGroupWidth / 2;
    row.values.forEach((value, stageIndex) => {
      const barX = startX + stageIndex * (barWidth + barGap);
      const barY = y(value);
      const barHeight = Math.max(baselineY - barY, value > 0 ? 1 : 0);
      const bar = svgNode('rect', { class: 'profile-svg-bar', x: barX, y: baselineY - barHeight, width: barWidth, height: barHeight, rx: 3 });
      bar.style.setProperty('--bar-color', stageColor(stageIndex));
      bar.append(svgNode('title', {}, `${row.label} · ${stages[stageIndex].title}: ${measuredValue(value, digits)} ${unit}${stageIndex ? ` · ${improvementText(row.values[0], value)}` : ''}`));
      svg.append(bar);
      const labelY = Math.max(baselineY - barHeight - 7, margin.top + 11);
      svg.append(svgNode('text', { class: 'profile-svg-value', x: barX + barWidth / 2, y: labelY, 'text-anchor': 'middle' }, measuredValue(value, digits)));
    });
    appendWrappedSvgLabel(svg, center, baselineY + 22, row.label, xStep - 10);
  });
}

function comparisonChart(title, description, stages, rows, unit, digits = 3, axes = { x: 'Category', y: unit }) {
  const chart = html('article', 'profile-card profile-comparison-chart');
  chart.append(html('h3', '', title), html('p', 'profile-meta', description), stageLegend(stages));
  const plot = html('div', 'profile-svg-wrap');
  const svg = svgNode('svg', { class: 'profile-svg-chart' });
  plot.append(svg);
  plot.drawProfileChart = () => drawProfileComparisonChart(plot, stages, rows, unit, digits, axes, title);
  profileState.charts.push(plot);
  profileChartObserver.observe(plot);
  chart.append(plot);
  return chart;
}

function metricCard(label, before, after, unit, detail, digits = 3) {
  const card = html('article', 'profile-kpi');
  card.append(html('p', 'profile-kpi-label', label));
  const values = html('div', 'profile-kpi-values');
  values.append(html('span', '', `${number(before, digits)} ${unit}`), html('span', 'profile-kpi-arrow', '→'), html('strong', '', `${number(after, digits)} ${unit}`));
  card.append(values, html('p', `profile-kpi-change ${improvementClass(before, after)}`, improvementText(before, after)), html('p', 'profile-kpi-detail', detail));
  return card;
}

function baselineMetricCard(label, value, unit, detail, digits = 3) {
  const card = html('article', 'profile-kpi');
  card.append(html('p', 'profile-kpi-label', label));
  card.append(html('div', 'profile-kpi-values', `${measuredValue(value, digits)} ${unit}`));
  card.append(html('p', 'profile-kpi-detail', detail));
  return card;
}

function nextExperiment(stages) {
  if (stages.at(-1)?.optimization_frozen) return undefined;
  const current = stages.at(-1);
  if (current.parser_followup_complete) return null;
  const topCurrent = workload(current.cpu, 'post-500-10kb')?.functions?.reduce((top, item) => !top || item.inclusive_s > top.inclusive_s ? item : top, null);
  if (!topCurrent) return null;
  const label = topCurrent.label.toLowerCase();
  const finding = stages.flatMap(stage => stage.findings || []).find(item =>
    item.title?.toLowerCase().includes(label) || item.evidence?.toLowerCase().includes(label)
  );
  return {
    optimization: finding?.optimization || `Investigate ${topCurrent.label}`,
    evidence: `${topCurrent.label} remains ${cpuSeconds(topCurrent.inclusive_s)} s inclusive CPU in the listed POST 500 functions.`,
    validation: finding?.validation || 'Measure the same CPU, memory and benchmark workloads after the next change.'
  };
}

function renderBaselineOverview(stage) {
  const memoryReport = profileState.report === 'memory';
  const postPhase = phase(stage, 'post-500-10kb');
  const postMemory = workload(stage.memory, 'post-500-10kb');
  const search = functionResult(stage, 'post-500-10kb', 'Batch search');
  const section = profileSection(memoryReport ? 'Baseline memory profile' : 'Baseline CPU profile', 'Starting measurements before performance optimization. These numbers establish what work was expensive and what should be tested next.', 'profile-overview');
  const heading = html('div', 'profile-heading');
  const verification = stage.source_verification;
  heading.append(html('span', 'profile-badge', memoryReport ? 'Tracked memory' : `${stage.cpu.condition} CPU`), html('span', `profile-badge ${verification.verified === verification.total ? '' : 'source-lock-warning'}`, `${verification.verified}/${verification.total} source files verified`));
  section.append(heading);

  const kpis = html('div', 'profile-kpis');
  if (memoryReport) {
    kpis.append(baselineMetricCard('POST 500 · peak tracked memory', postMemory?.peak_mib, 'MiB', 'Original whole-test Memray scope.', 1));
  } else {
    kpis.append(
      baselineMetricCard('POST 500 · first-request CPU', postPhase?.first_request_s, 's', 'Median of 3 fresh Python processes.'),
      baselineMetricCard('POST 500 · batch search hotspot', search?.calls, 'calls', `${number(search?.self_s, 3)} self CPU seconds.`, 0)
    );
  }
  section.append(kpis);

  const story = html('div', 'profile-story');
  const cpu = html('article');
  cpu.append(html('h3', '', 'CPU observation'), html('p', '', `${number(search?.calls, 0)} full-batch searches consume ${cpuSeconds(search?.self_s)} CPU s in POST 500.`));
  const memory = html('article');
  const batch = allocation(stage, 'post-500-10kb', 'Serialize full batch');
  const dictionaries = allocation(stage, 'post-500-10kb', 'Run dictionary construction');
  memory.append(html('h3', '', 'Memory observation'), html('p', '', `${number(batch, 1)} MiB serialized batch and ${number(dictionaries, 1)} MiB run dictionaries coexist at peak.`));
  const candidate = stage.findings?.find(item => item.optimization === 'One-pass encoder');
  const next = html('article');
  next.append(html('h3', '', 'First optimization candidate'), html('p', '', candidate ? `${candidate.optimization} — ${candidate.validation}` : 'Remove repeated scans and duplicate payload representations, then measure again.'));
  story.append(memoryReport ? memory : cpu, next);
  section.append(story);
  return section;
}

function renderBaselineCpu(stage) {
  const section = profileSection('Baseline CPU', 'Cold phase medians and function details, with their interpretations in the same tables.', 'profile-cpu');
  const details = detailedCpuTables(stage);
  details.classList.add('profile-detail-group');
  section.append(details);
  return section;
}

function renderBaselineMemory(stage) {
  const section = profileSection('Baseline memory numbers', 'Starting measurements only. These are whole benchmark-test peaks, not server RSS.', 'profile-memory');
  const workloadNotes = {
    'post-500-10kb': 'Largest peak; primary POST memory target.',
    'post-50-100kb': 'Confirms the issue across the second POST shape.',
    'get-10kb': 'GET reference; not targeted by the encoder change.',
    'get-100kb': 'GET reference; not targeted by the encoder change.'
  };
  const peaks = html('article', 'profile-card baseline-number-card');
  peaks.append(html('h3', '', 'Peak tracked memory'));
  peaks.append(profileTable('Baseline peak tracked memory', ['Workload', 'Peak MiB', 'Interpretation'], stage.memory.workloads.map(item => [item.label, number(item.peak_mib, 1), workloadNotes[item.id]]), [1]));
  const targetLabels = ['Serialize full batch', 'Run dictionary construction', 'HTTP request body', 'Decoded request JSON'];
  const driverNotes = {
    'Serialize full batch': 'Full extra batch representation.',
    'Run dictionary construction': 'Run dictionaries coexist with the batch.',
    'HTTP request body': 'Input representation that remains necessary.',
    'Decoded request JSON': 'Decoded payload that remains necessary.'
  };
  const allocations = workload(stage.memory, 'post-500-10kb').allocations.filter(item => targetLabels.includes(item.label));
  const drivers = html('article', 'profile-card baseline-number-card');
  drivers.append(html('h3', '', 'POST 500 · payload allocations relevant to encoding'));
  drivers.append(profileTable('Baseline POST 500 encoding allocations', ['Allocation', 'MiB at peak', 'Interpretation'], allocations.map(item => [item.label, number(item.mib, 1), driverNotes[item.label]]), [1]));
  section.append(peaks, drivers);
  const details = html('section', 'profile-detail-group');
  details.append(html('h3', '', 'Exact baseline memory tables and source recordings'), detailedMemoryTables(stage));
  section.append(details);
  return section;
}

function renderBaselineFindings(stage) {
  const section = profileSection('Baseline observations and potential optimizations', 'Each candidate starts from a measured number. Expected improvements remain hypotheses until the next stage is measured.', 'profile-findings');
  const list = html('div', 'profile-finding-list');
  stage.findings.forEach(finding => {
    const item = html('article', 'profile-finding');
    item.append(html('span', 'profile-finding-status', finding.status));
    item.append(html('strong', '', finding.title || finding.evidence), html('p', '', finding.evidence), html('p', '', finding.interpretation), html('p', 'profile-meta', `Candidate: ${finding.optimization} · Test: ${finding.validation}`));
    list.append(item);
  });
  section.append(list);
  return section;
}

function renderBaseline(stage) {
  return [renderBaselineOverview(stage), profileState.report === 'memory' ? renderBaselineMemory(stage) : renderBaselineCpu(stage), renderBaselineFindings(stage)];
}

function renderProfileOverview(stages) {
  const memoryReport = profileState.report === 'memory';
  const baseline = stages[0];
  const current = stages.at(-1);
  const cpuBefore = phase(baseline, 'post-500-10kb')?.first_request_s;
  const cpuAfter = phase(current, 'post-500-10kb')?.first_request_s;
  const memoryBefore = workload(baseline.memory, 'post-500-10kb')?.peak_mib;
  const memoryAfter = workload(current.memory, 'post-500-10kb')?.peak_mib;
  const searchBefore = functionResult(baseline, 'post-500-10kb', 'Batch search');
  const searchAfter = functionResult(current, 'post-500-10kb', 'Batch search');

  const section = profileSection(`${baseline.title} → ${current.title}`, `${stages.length} saved profile stages compared in one view. Lower ${memoryReport ? 'memory' : 'CPU'} values are better.`, 'profile-overview');
  const heading = html('div', 'profile-heading');
  const verified = stages.reduce((sum, stage) => sum + stage.source_verification.verified, 0);
  const total = stages.reduce((sum, stage) => sum + stage.source_verification.total, 0);
  heading.append(html('span', 'profile-badge', memoryReport ? 'Tracked memory' : `${current.cpu.condition} CPU`), html('span', `profile-badge ${verified === total ? '' : 'source-lock-warning'}`, `${verified}/${total} source files verified`));
  section.append(heading);

  if (stages.length > 1) {
    const kpis = html('div', 'profile-kpis');
    if (memoryReport) {
      kpis.append(metricCard('POST 500 · peak tracked memory', memoryBefore, memoryAfter, 'MiB', 'Original whole-test Memray scope.', 1));
    } else {
      kpis.append(metricCard('POST 500 · first-request CPU', cpuBefore, cpuAfter, 's', 'Median of 3 fresh Python processes.'));
      if (searchBefore?.calls > 0) kpis.append(metricCard('POST 500 · batch searches', searchBefore.calls, searchAfter?.calls, 'calls', `${number(searchBefore.self_s, 3)} CPU s hotspot removed.`, 0));
    }
    section.append(kpis);
  }

  const story = html('div', 'profile-story');
  const changed = html('article');
  changed.append(html('h3', '', 'What changed'), html('p', '', current.findings?.[0]?.optimization || current.title));
  const evidence = html('article');
  evidence.append(html('h3', '', 'What the measurements show'), html('p', '', current.findings?.[0]?.interpretation || current.summary));
  const candidate = nextExperiment(stages);
  const next = html('article');
  next.append(html('h3', '', current.optimization_frozen ? 'Optimization work frozen' : current.parser_followup_complete ? 'Parser decision' : 'Next optimization candidate'), html('p', '', current.parser_followup_complete ? current.decision_summary || 'Retained with the measured latency trade-off; no further parser change selected.' : candidate ? `${candidate.optimization} — ${candidate.evidence}` : 'Use the detailed evidence below to select the next experiment.'));
  story.append(changed, evidence, next);
  section.append(story);
  return section;
}

function detailedCpuTables(stage) {
  const baseline = stage.id === 'baseline';
  const workloadNotes = {
    'post-500-10kb': 'Highest request CPU; primary POST target.',
    'post-50-100kb': 'Second POST payload shape.',
    'get-10kb': 'GET reference; unchanged by a POST-only optimization.',
    'get-100kb': 'GET reference; unchanged by a POST-only optimization.'
  };
  const hotspotNotes = {
    'Batch search': 'Dominant request hotspot; first optimization candidate.',
    'S3 client dependency': 'Inclusive CPU includes session and client work; profile internals before choosing which to reuse.',
    'JSON decode': 'Smaller than batch searching.',
    'Response encoding': 'Not a priority from this profile.'
  };
  const details = html('section', 'profile-exact-tables');
  details.setAttribute('aria-label', `${stage.title} exact CPU tables`);
  if (stage.cpu.phase_medians?.length) {
    const headers = ['Workload', 'Imports', 'Startup', 'First request', 'Total'];
    if (baseline) headers.push('Interpretation');
    details.append(profileTable(`${stage.title} cold CPU phase medians`, headers, stage.cpu.phase_medians.map(row => [row.label, cpuSeconds(row.imports_s), cpuSeconds(row.startup_s), cpuSeconds(row.first_request_s), cpuSeconds(row.total_s), ...(baseline ? [workloadNotes[row.id] || 'Recorded for reference.'] : [])]), [1, 2, 3, 4]));
    details.append(profileSource(stage, 'cold-cpu-summary', 'Open 12-capture summary'));
  }
  const grid = html('div', 'profile-table-grid');
  stage.cpu.workloads.forEach(item => {
    const card = html('article', 'profile-table-card');
    card.append(html('h4', '', item.label));
    const annotate = baseline && item.id === 'post-500-10kb';
    const headers = ['Function', 'Calls', 'Self (s)', 'Inclusive (s)'];
    if (annotate) headers.push('Interpretation');
    card.append(profileTable(`${stage.title} ${item.label} CPU functions`, headers, item.functions.map(result => [result.label, number(result.calls, 0), cpuSeconds(result.self_s), cpuSeconds(result.inclusive_s), ...(annotate ? [hotspotNotes[result.label] || 'Recorded for reference.'] : [])]), [1, 2, 3]));
    card.append(profileSource(stage, item.source_id, `Open ${item.label} CPU text`));
    grid.append(card);
  });
  details.append(grid);
  return details;
}

function renderCpuComparison(stages) {
  const section = profileSection('CPU comparison', 'The main graph compares the profiled CPU used by the first complete request. Imports and startup are deliberately excluded from this graph.', 'profile-cpu');
  const workloadIds = stages[0].cpu.phase_medians.map(item => item.id);
  const rows = workloadIds.map(id => ({
    label: phase(stages[0], id).label,
    values: stages.map(stage => phase(stage, id)?.first_request_s)
  }));
  section.append(comparisonChart('First-request CPU by workload', 'Median of 3 fresh processes per stage · lower is better', stages, rows, 's', 3, { x: 'Workload', y: 'First-request CPU (seconds)' }));

  const comparableFunctions = ['Batch search', 'S3 client dependency'];
  const hotspotRows = comparableFunctions.map(label => ({
    label,
    values: stages.map(stage => functionResult(stage, 'post-500-10kb', label)?.inclusive_s)
  })).filter(row => row.values.every(Number.isFinite));
  if (hotspotRows.length) {
    section.append(comparisonChart('POST 500 · comparable function hotspots', 'Session 1 from each stage · lower is better', stages, hotspotRows, 's', 3, { x: 'Function', y: 'Inclusive CPU (seconds)' }));
  }

  const current = stages.at(-1);
  const replacement = ['JSON serialization', 'Buffer writes'].map(label => functionResult(current, 'post-500-10kb', label)).filter(Boolean);
  if (replacement.length) {
    const note = html('div', 'profile-explanation');
    note.append(html('strong', '', 'Replacement work is small:'));
    note.append(html('span', '', replacement.map(item => `${item.label} ${number(item.calls, 0)} calls / ${cpuSeconds(item.inclusive_s)} s`).join(' · ')));
    section.append(note);
  }

  const details = html('section', 'profile-detail-group');
  details.append(html('h3', '', 'Exact CPU tables and source reports'));
  details.append(detailedCpuTables(current));
  section.append(details);
  return section;
}

function detailedMemoryTables(stage) {
  const details = html('section', 'profile-exact-tables');
  details.setAttribute('aria-label', `${stage.title} exact memory tables`);
  details.append(html('p', 'profile-meta', 'Five largest groups at the recorded peak. Test data and GET seeding are included. No Python stack means attribution is unavailable.'));
  const grid = html('div', 'profile-table-grid');
  stage.memory.workloads.forEach(item => {
    const card = html('article', 'profile-table-card');
    card.append(html('h4', '', `${item.label} · ${number(item.peak_mib)} MiB peak`));
    const groups = [...item.allocations].sort((a, b) => b.mib - a.mib || a.label.localeCompare(b.label));
    const visible = groups.slice(0, 5);
    const smaller = groups.slice(5);
    if (smaller.length) visible.push({ label: `Remaining smaller groups (${smaller.length})`, mib: smaller.reduce((total, result) => total + result.mib, 0) });
    const rows = results => results.map(result => [result.label, number(result.mib), `${number(item.peak_mib ? result.mib / item.peak_mib * 100 : 0, 1)}%`]);
    card.append(profileTable(`${stage.title} ${item.label} allocations`, ['Allocation group', 'MiB at peak', '% of peak'], rows(visible), [1, 2]));
    if (smaller.length) {
      const remainder = html('details', 'profile-notes');
      remainder.append(html('summary', '', 'Show remaining smaller groups'), profileTable(`${stage.title} ${item.label} smaller allocations`, ['Allocation group', 'MiB at peak', '% of peak'], rows(smaller), [1, 2]));
      card.append(remainder);
    }
    card.append(profileSource(stage, item.source_id, 'Download Memray recording'));
    grid.append(card);
  });
  details.append(grid);
  return details;
}

function renderMemoryComparison(stages) {
  const section = profileSection('Memory comparison', 'Peak tracked allocation for the original whole benchmark test. This is comparable between stages, but it is not server RSS or per-request memory.', 'profile-memory');
  const workloadIds = stages[0].memory.workloads.map(item => item.id);
  const rows = workloadIds.map(id => ({
    label: workload(stages[0].memory, id).label,
    values: stages.map(stage => workload(stage.memory, id)?.peak_mib)
  }));
  section.append(comparisonChart('Peak tracked memory by workload', 'Same original pytest-memray workflow · lower is better', stages, rows, 'MiB', 1, { x: 'Workload', y: 'Peak tracked memory (MiB)' }));

  const drivers = [
    ['Batch representation', ['Serialize full batch', 'Batch buffer', 'Encoded run JSON', 'Per-run buffer']],
    ['Run dictionaries', 'Run dictionary construction'],
    ['S3 SDK / data', 'S3 SDK / data'],
    ['HTTP request body', 'HTTP request body'],
    ['Request JSON parsing + validation', ['Decoded request JSON', 'Model validation', 'Request JSON parsing + validation']]
  ].map(([label, aliases]) => ({
    label,
    values: stages.map(stage => allocation(stage, 'post-500-10kb', aliases))
  })).filter(row => row.values.every(Number.isFinite));
  section.append(comparisonChart('POST 500 · allocation drivers at peak', 'Recorded stack attribution · absent groups are zero at that peak, not necessarily absent throughout execution', stages, drivers, 'MiB', 1, { x: 'Allocation group', y: 'Allocation at peak (MiB)' }));

  const details = html('section', 'profile-detail-group');
  details.append(html('h3', '', 'Exact memory tables and source recordings'));
  details.append(detailedMemoryTables(stages.at(-1)));
  section.append(details);
  return section;
}

function renderServerMemory(stage) {
  const memory = stage.server_memory;
  if (memory.mode === 'get-decoder') {
    const section = profileSection('GET decoder · actual API-worker RAM', memory.scope, 'profile-server-memory');
    const versions = [{ title: 'orjson GET' }, { title: 'Replacement GET decoder' }];
    const rows = memory.workloads.map(item => ({
      label: `${item.label} · concurrency ${item.concurrency}`,
      values: [item.before.warm_peak_mib, item.after.warm_peak_mib]
    }));
    section.append(comparisonChart('GET worker peak RSS', 'Median of 3 fresh workers · includes runtime and startup · not Memray', versions, rows, 'MiB', 1, { x: 'GET workload / concurrency', y: 'API-worker peak RSS (MiB)' }));
    section.append(profileTable('GET decoder · Linux RSS comparison', ['Workload', 'Concurrency', 'First GET peak MiB', 'Warm peak MiB', 'Warm RAM saving', 'Warm latency ms'], memory.workloads.map(item => [
      item.label, item.concurrency,
      `${number(item.before.first_peak_mib, 2)} → ${number(item.after.first_peak_mib, 2)}`,
      `${number(item.before.warm_peak_mib, 2)} → ${number(item.after.warm_peak_mib, 2)}`,
      `${number(item.warm_saving_mib, 2)} MiB (${number(item.warm_reduction_pct, 1)}%)`,
      `${number(item.before.warm_median_ms, 1)} → ${number(item.after.warm_median_ms, 1)}`
    ]), [1, 2, 3, 4, 5]));
    section.append(html('p', 'profile-meta', memory.limitation));
    section.append(html('p', 'profile-meta', memory.decision));
    section.append(profileSource(stage, memory.source_id, 'Open RAM measurements and all repeats'));
    return section;
  }
  const section = profileSection('API-worker RAM · assumed Linux reference', memory.scope, 'profile-server-memory');
  const versions = [{ title: 'Original baseline' }, { title: 'Pre-parser control' }, { title: 'Retained parser' }];
  const rows = memory.workloads.map(item => ({
    label: `${item.label} · concurrency ${item.concurrency}`,
    values: ['original', 'before', 'after'].map(key => item[key].peak_mib)
  }));
  section.append(comparisonChart('Peak worker RSS', 'Median of 3 fresh workers per configuration · separate from Memray', versions, rows, 'MiB', 1, { x: 'POST workload / concurrency', y: 'Peak API-worker RSS (MiB)' }));
  section.append(profileTable('Assumed Linux peak API-worker RSS', ['Workload', 'Concurrency', 'Original MiB', 'Pre-parser MiB', 'Retained MiB', 'Reduction vs original'], memory.workloads.map(item => [
    item.label, item.concurrency, number(item.original.peak_mib, 1), number(item.before.peak_mib, 1), number(item.after.peak_mib, 1), `${number(item.reduction_vs_original_pct, 1)}%`
  ]), [1, 2, 3, 4, 5]));
  section.append(profileSource(stage, memory.source_id, 'Open RAM measurements and repeats'));
  return section;
}

function renderEvidence(stages) {
  const current = stages.at(-1);
  const section = profileSection('Evidence and conclusion', 'Short interpretation of the latest saved stage. Exact tables, limitations and pinned source files remain available below.', 'profile-findings');
  if ((current.retention_decision === 'retained' || current.show_comparison_results) && current.comparison_results) {
    section.append(html('h3', '', current.comparison_title || 'Paired POST results · versus pre-parser control'));
    section.append(profileTable('Paired benchmark and tracked memory', ['Workload', 'Latency ms', 'Latency change', 'Tracked peak MiB', 'Peak reduction'], current.comparison_results.filter(item => current.comparison_focus_ids ? current.comparison_focus_ids.includes(item.id) : item.id.startsWith('post')).map(item => [
      item.label,
      `${number(item.before_median_ms, 1)} → ${number(item.after_median_ms, 1)}`,
      `${percentChange(item.before_median_ms, item.after_median_ms) > 0 ? '+' : ''}${number(percentChange(item.before_median_ms, item.after_median_ms), 1)}%`,
      `${number(item.before_peak_mib, 1)} → ${number(item.after_peak_mib, 1)}`,
      `${number(-percentChange(item.before_peak_mib, item.after_peak_mib), 1)}%`
    ]), [1, 2, 3, 4]));
    section.append(html('p', 'profile-meta', current.comparison_results_scope || 'Median of 3 suites/version. Tracked allocation peaks are not server RAM.'));
  }
  const list = html('div', 'profile-finding-list');
  current.findings.forEach(finding => {
    const item = html('article', 'profile-finding');
    item.append(html('span', `profile-finding-status ${finding.status === 'Improved' ? 'improved' : ''}`, finding.status));
    item.append(html('strong', '', finding.evidence), html('p', '', finding.interpretation), html('p', 'profile-meta', `${finding.optimization} · ${finding.validation}`));
    list.append(item);
  });
  section.append(list);

  const candidate = nextExperiment(stages);
  if (candidate) {
    const next = html('article', 'profile-next-experiment');
    next.append(html('p', 'eyebrow', 'INTRODUCTION TO THE NEXT EXPERIMENT'), html('h3', '', candidate.optimization), html('p', '', candidate.evidence), html('p', 'profile-meta', candidate.validation));
    section.append(next);
  }


  const sources = html('details', 'profile-sources');
  sources.append(html('summary', '', 'Pinned reports, source hashes and calculation rules'));
  sources.append(html('h3', '', current.title));
  sources.append(html('p', 'profile-meta', `Commit ${current.commit.slice(0, 7)} · ${current.review_status}`));
  sources.append(profileTable(`${current.title} profile metrics and pinned reports`, ['Displayed metric', 'Pinned report IDs', 'Calculation'], current.metric_sources.map(item => [item.metric, item.source_ids.join(', '), item.rule])));
  const links = html('ul');
  Object.entries(current.sources).forEach(([id, source]) => {
    const row = html('li');
    const fingerprint = html('code', '', `${source.path} · SHA-256 ${source.sha256.slice(0, 12)}… · ${source.size_bytes} bytes`);
    fingerprint.title = source.sha256;
    row.append(profileSource(current, id, source.label), fingerprint);
    links.append(row);
  });
  sources.append(links);
  section.append(sources);
  return section;
}

function renderProfileInterpretation() {
  const stages = profileState.data.stages;
  byId('profile-empty').hidden = Boolean(stages.length);
  byId('profile-print').disabled = !stages.length;
  byId('profile-report-tabs').hidden = !stages.length;
  if (!stages.some(stage => stage.id === profileState.stage)) profileState.stage = stages.find(stage => stage.optimization_frozen)?.id || stages[0]?.id || '';
  const activeIndex = Math.max(stages.findIndex(stage => stage.id === profileState.stage), 0);
  const active = stages[activeIndex];
  // Historical side experiments use their own measured control, rather than
  // crediting earlier accepted or rejected changes to the latest candidate.
  const comparisonStages = active?.comparison_stage_ids
    ? active.comparison_stage_ids.map(id => stages.find(stage => stage.id === id)).filter(Boolean)
    : stages.slice(0, activeIndex + 1);
  profileStageNav.replaceChildren();
  stages.forEach((stage, index) => {
    const button = html('button', '', `${index + 1}. ${stage.title}`);
    button.type = 'button';
    button.id = `profile-stage-tab-${stage.id}`;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-controls', 'profile-content');
    button.setAttribute('aria-selected', String(stage.id === profileState.stage));
    button.tabIndex = stage.id === profileState.stage ? 0 : -1;
    button.addEventListener('click', () => {
      profileState.stage = stage.id;
      renderProfileInterpretation();
      byId('profile-panel').scrollIntoView({ block: 'start' });
    });
    button.addEventListener('keydown', event => {
      let next;
      if (event.key === 'ArrowDown') next = (index + 1) % stages.length;
      if (event.key === 'ArrowUp') next = (index - 1 + stages.length) % stages.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = stages.length - 1;
      if (next == null) return;
      event.preventDefault();
      profileState.stage = stages[next].id;
      renderProfileInterpretation();
      profileStageNav.querySelector('[aria-selected="true"]')?.focus();
    });
    profileStageNav.append(button);
  });
  updateProfileStageNav();
  byId('profile-view-title').textContent = active ? (activeIndex === 0 ? 'Baseline profile' : `${active.title} comparison`) : 'Profile interpretation';
  byId('profile-stage-summary').textContent = active ? (activeIndex === 0 ? 'Starting measurements and optimization hypotheses' : `${comparisonStages.length} stages on each graph · ${comparisonStages[0].title} through ${active.title}`) : 'No saved stages';
  const downloads = byId('profile-downloads');
  downloads.replaceChildren();
  if (active) {
    const link = html('a', 'button-link', `Download ${active.title}`);
    link.href = active.download_url;
    downloads.append(link);
  }
  const content = byId('profile-content');
  profileState.charts.forEach(plot => profileChartObserver.unobserve(plot));
  profileState.charts = [];
  content.replaceChildren();
  if (!stages.length) return;
  const reportPanels = {};
  profileReportTabs.forEach(button => {
    const report = button.dataset.profileReport;
    const panel = html('div', 'profile-report-panel');
    panel.id = `profile-report-${report}`;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', button.id);
    panel.tabIndex = 0;
    panel.hidden = report !== profileState.report;
    reportPanels[report] = panel;
    content.append(panel);
  });
  const reportPanel = reportPanels[profileState.report];
  if (activeIndex === 0) {
    reportPanel.append(...renderBaseline(active));
  } else {
    reportPanel.append(renderProfileOverview(comparisonStages), profileState.report === 'memory' ? renderMemoryComparison(comparisonStages) : renderCpuComparison(comparisonStages), renderEvidence(comparisonStages));
    if (profileState.report === 'memory' && active.server_memory) {
      reportPanel.insertBefore(renderServerMemory(active), reportPanel.lastElementChild);
    }
  }
  requestAnimationFrame(() => profileState.charts.forEach(plot => plot.drawProfileChart()));
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
    if (!byId('profile-panel').hidden) byId('status').textContent = `Profile comparison · ${data.stages.length} stage${data.stages.length === 1 ? '' : 's'} · checks every 3s`;
  } catch (error) {
    byId('profile-warnings').textContent = `Could not load saved interpretations. Existing results are retained. ${error.message}`;
    byId('profile-warnings').hidden = false;
  } finally { profileState.pending = false; }
}

byId('profile-print').addEventListener('click', () => window.print());
selectDashboardView(location.hash === '#profiles' ? 'profiles' : 'benchmarks');
refreshProfiles();
setInterval(refreshProfiles, 3000);
