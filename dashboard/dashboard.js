'use strict';

// Presentation exclusions only: preserve the stored reports and application code.
const DASHBOARD_OMISSIONS = {
  benchmarks: ['Darwin-CPython-3.11-64bit/0006_baseline.json'],
  profiles: ['one-s3-read']
};

function displayedEntries(entries, section) {
  return entries.filter(entry => !DASHBOARD_OMISSIONS[section].includes(entry.id));
}

const FINAL_BENCHMARK_IDS = [1, 2, 3].map(repeat =>
  `frozen-final-b18ff49d14ea/000${repeat}_frozen-final.json`);
const PARSER_BENCHMARK_IDS = [2, 3, 6].map(repeat =>
  `incremental-post-20261004-153f2b97e2c0/000${repeat}_streaming-post-after.json`);

function medianOf(values) {
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

function benchmarkStages(reports) {
  const sources = FINAL_BENCHMARK_IDS.map(id => reports.find(report => report.id === id));
  const supportingIds = [...FINAL_BENCHMARK_IDS, ...PARSER_BENCHMARK_IDS];
  const result = reports.map(report => supportingIds.includes(report.id) ? {
    ...report,
    history_reason: FINAL_BENCHMARK_IDS.includes(report.id)
      ? 'Supporting final repeat; included in the incremental-parser summary, not a separate optimization.'
      : 'Earlier parser experiment; final retained parser and tuning are represented by the final summary.'
  } : report);
  // Never silently use a subset or combine incompatible collection settings.
  if (sources.some(report => !report) || new Set(sources.map(report => report.group)).size !== 1) {
    return { reports: result, warnings: ['Final parser summary requires all three final suites with matching collection settings.'] };
  }
  const benchmarks = {};
  for (const name of Object.keys(sources[0].benchmarks)) {
    const suites = sources.map(report => report.benchmarks[name]);
    const stats = {};
    for (const key of Object.keys(suites[0]).filter(key => key !== 'data')) {
      const values = suites.map(suite => suite[key]);
      if (values.every(value => typeof value === 'number' && Number.isFinite(value))) stats[key] = medianOf(values);
    }
    // Raw rounds remain raw; we do not claim the summary is a pooled distribution.
    if (suites.every(suite => Array.isArray(suite.data))) stats.data = suites.flatMap(suite => suite.data);
    benchmarks[name] = stats;
  }
  result.push({
    ...sources[0], id: 'summary:incremental-post-parsing',
    label: 'Incremental POST parsing + tuning', history_reason: null,
    benchmarks, source_reports: sources
  });
  return { reports: result, warnings: [] };
}

const METRICS = [
  ['median', 'Median latency', 'performance', 'ms', true],
  ['mean', 'Mean latency', 'performance', 'ms', true]
].map(([key, title, section, unit, milliseconds]) => ({ key, title, section, unit, milliseconds }));

const DETAIL_METRICS = [
  ['ops', 'Operation rate', false],
  ['min', 'Minimum (ms)', true],
  ['max', 'Maximum (ms)', true],
  ['stddev', 'Standard deviation (ms)', true],
  ['iqr', 'IQR (ms)', true],
  ['rounds', 'Rounds', false],
  ['iterations', 'Iterations / round', false],
  ['iqr_outliers', 'IQR outliers', false],
  ['stddev_outliers', 'Std. dev. outliers', false]
].map(([key, title, milliseconds]) => ({ key, title, milliseconds }));

const state = { data: null, signature: '', group: '', visible: new Set(), charts: [], pending: false };
const byId = id => document.getElementById(id);
const ns = 'http://www.w3.org/2000/svg';
const tooltip = byId('tooltip');

function html(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text;
  return node;
}

function svgNode(tag, attributes = {}, text = '') {
  const node = document.createElementNS(ns, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  node.textContent = text;
  return node;
}

function selectedReports() {
  return state.data ? state.data.reports.filter(report => report.group === state.group && !report.history_reason) : [];
}

function selectedWorkloads() {
  return Object.keys(state.data?.workloads || {}).filter(name => state.visible.has(name));
}

function seriesIndex(name) {
  return Object.keys(state.data.workloads).indexOf(name);
}

function number(value, digits = 3) {
  return value == null ? 'Not recorded' : value.toLocaleString('en-US', { maximumFractionDigits: digits });
}

function metricValue(report, name, metric) {
  const value = report.benchmarks[name]?.[metric.key];
  return typeof value === 'number' ? value * (metric.milliseconds ? 1000 : 1) : null;
}

function delta(value, reference) {
  if (value == null || reference == null || reference === 0) return 'N/A';
  const percent = (value / reference - 1) * 100;
  return `${percent > 0 ? '+' : ''}${number(percent, 1)}%`;
}

function showError(message) {
  byId('error').textContent = message;
  byId('error').hidden = !message;
}

function setLiveStatus(text, detail = 'Checks for saved reports every 3 seconds') {
  byId('status').textContent = text;
  byId('status').title = detail;
}

function createControls() {
  const groupSelect = byId('group');
  const previous = state.group;
  const groups = state.data.groups.filter(group => state.data.reports.some(report => report.group === group.id && !report.history_reason));
  groupSelect.replaceChildren();
  groups.forEach((group, index) => {
    const count = state.data.reports.filter(report => report.group === group.id && !report.history_reason).length;
    const option = html('option', '', `Set ${index + 1}: ${group.label} · ${count} stage${count === 1 ? '' : 's'}`);
    option.value = group.id;
    groupSelect.append(option);
  });
  state.group = groups.some(group => group.id === previous) ? previous : groups[0]?.id || '';
  groupSelect.value = state.group;
  groupSelect.disabled = groups.length < 2;
  byId('comparison-set-control').hidden = groups.length < 2;
  if (!byId('workloads').children.length) {
    Object.entries(state.data.workloads).forEach(([name, label], index) => {
      state.visible.add(name);
      const wrapper = html('label', 'workload');
      wrapper.style.setProperty('--color', `var(--series-${index})`);
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = true;
      input.value = name;
      input.addEventListener('change', () => {
        if (input.checked) state.visible.add(name); else state.visible.delete(name);
        renderCharts();
      });
      wrapper.append(input, html('span', 'swatch'), html('span', '', label));
      byId('workloads').append(wrapper);
    });
  }
}

function renderStages(reports) {
  const container = byId('stages');
  container.replaceChildren();
  reports.forEach((report, index) => {
    const row = html('div', 'stage-row');
    if (report.source_reports) {
      const provenance = html('div', 'provenance');
      provenance.append(html('strong', '', 'Median of each statistic across 3 final suites · not pooled statistics'));
      report.source_reports.forEach(source => provenance.append(html('div', '', source.id)));
      row.append(html('span', 'stage-number', String(index + 1)), html('strong', '', report.label), provenance);
      container.append(row);
      return;
    }
    const form = html('form', 'stage-name');
    const input = document.createElement('input');
    input.type = 'text';
    input.value = report.label;
    input.maxLength = 80;
    input.required = true;
    input.setAttribute('aria-label', `Stage ${index + 1} name`);
    const save = html('button', '', 'Save');
    save.type = 'submit';
    form.append(input, save);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      save.disabled = true;
      try {
        const response = await fetch('/api/stage-name', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ report_id: report.id, label: input.value })
        });
        if (!response.ok) throw new Error((await response.json()).detail || 'Could not save stage name');
        state.signature = '';
        await refresh();
      } catch (error) { showError(String(error.message)); }
      finally { save.disabled = false; }
    });
    const provenance = html('div', 'provenance');
    const date = new Date(report.datetime);
    const time = Number.isNaN(date.getTime()) ? 'Unknown date' : date.toLocaleString();
    const commit = String(report.commit.id || 'unknown').slice(0, 7);
    provenance.append(html('strong', '', `${commit}${report.commit.dirty ? ' · uncommitted changes' : ''} · ${time}`));
    provenance.append(html('div', '', report.id));
    row.append(html('span', 'stage-number', String(index + 1)), form, provenance);
    container.append(row);
  });
}

function render() {
  createControls();
  const reports = selectedReports();
  const archived = state.data.reports.filter(report => report.history_reason);
  renderStages(reports);
  byId('benchmark-history-summary').textContent = `Supporting and archived benchmark reports (${archived.length})`;
  const history = byId('archived-reports');
  history.replaceChildren();
  archived.forEach(report => {
    const item = html('article', 'history-report');
    item.append(html('strong', '', report.label), html('p', '', report.history_reason), html('code', '', report.id));
    history.append(item);
  });
  const warnings = [...state.data.warnings];
  if (state.data.groups.length > 1) warnings.push('Recorded environment or collection settings differ; comparison sets are kept separate.');
  if (reports.some(report => Object.values(report.benchmarks).some(stats => !Array.isArray(stats.data)))) {
    warnings.push('Some reports have no raw data. Their summary graphs are retained; raw distributions are missing, not zero. Use --benchmark-save-data for future runs.');
  }
  byId('warnings').replaceChildren();
  if (warnings.length) {
    const list = html('ul');
    warnings.forEach(warning => list.append(html('li', '', warning)));
    byId('warnings').append(list);
  }
  byId('warnings').hidden = !warnings.length;
  byId('empty').hidden = Boolean(reports.length);
  renderCharts();
}

function valueTable(metric, sampleWorkload = null) {
  const details = html('details');
  details.append(html('summary', '', 'Exact values'));
  const wrapper = html('div', 'value-table-wrap');
  const table = html('table');
  const head = html('tr');
  head.append(html('th', '', 'Stage'));
  const workloads = sampleWorkload ? [sampleWorkload] : selectedWorkloads();
  workloads.forEach(name => head.append(html('th', '', state.data.workloads[name])));
  const thead = html('thead');
  thead.append(head);
  const tbody = html('tbody');
  selectedReports().forEach(report => {
    const row = html('tr');
    row.append(html('th', '', report.label));
    workloads.forEach(name => {
      const text = sampleWorkload ? report.benchmarks[name].data?.map(value => number(value * 1000)).join(', ') || 'Not recorded' :
        number(metricValue(report, name, metric), metric.milliseconds ? 3 : 4);
      row.append(html('td', '', text));
    });
    tbody.append(row);
  });
  table.append(thead, tbody);
  wrapper.append(table);
  details.append(wrapper);
  return details;
}

function renderBenchmarkDetails() {
  const table = html('table');
  table.append(html('caption', '', 'Supporting statistics by stage and selected workload'));
  const head = html('tr');
  head.append(html('th', '', 'Stage'), html('th', '', 'Workload'));
  DETAIL_METRICS.forEach(metric => head.append(html('th', '', metric.title)));
  [...head.children].forEach(cell => { cell.scope = 'col'; });
  const thead = html('thead');
  thead.append(head);
  const tbody = html('tbody');
  selectedReports().forEach(report => {
    selectedWorkloads().forEach(name => {
      const row = html('tr');
      const stage = html('th', '', report.label);
      stage.scope = 'row';
      row.append(stage, html('td', '', state.data.workloads[name]));
      DETAIL_METRICS.forEach(metric => {
        const value = metricValue(report, name, metric);
        const unit = metric.key === 'ops' ? (name.includes('create') ? ' batches/s' : ' requests/s') : '';
        row.append(html('td', '', number(value, metric.milliseconds ? 3 : 4) + (value == null ? '' : unit)));
      });
      tbody.append(row);
    });
  });
  table.append(thead, tbody);
  byId('benchmark-details').replaceChildren(table);
}

function downloadSvg(svg, name) {
  const panel = svg.closest('.chart');
  const clone = svg.cloneNode(true);
  const originals = [svg, ...svg.querySelectorAll('*')];
  const copies = [clone, ...clone.querySelectorAll('*')];
  originals.forEach((original, index) => {
    const style = getComputedStyle(original);
    for (const property of ['fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'font-family', 'font-size', 'opacity']) {
      copies[index].style.setProperty(property, style.getPropertyValue(property));
    }
  });
  clone.querySelectorAll('.hover-line').forEach(node => node.remove());
  const [, , width, height] = svg.getAttribute('viewBox').split(' ').map(Number);
  const names = name.startsWith('data-') ? [name.slice(5)] : selectedWorkloads();
  const columns = width >= 360 ? 2 : 1;
  const headerHeight = 45 + Math.ceil(names.length / columns) * 19;
  const exported = svgNode('svg', {
    xmlns: ns, width, height: height + headerHeight,
    viewBox: `0 0 ${width} ${height + headerHeight}`,
    role: 'img', 'aria-label': svg.getAttribute('aria-label')
  });
  clone.setAttribute('width', width);
  clone.setAttribute('height', height);
  clone.setAttribute('y', headerHeight);
  const colors = getComputedStyle(document.documentElement);
  const foreground = colors.getPropertyValue('--text').trim();
  exported.append(svgNode('rect', { x: 0, y: 0, width, height: height + headerHeight, fill: getComputedStyle(panel).backgroundColor }));
  exported.append(svgNode('text', { x: 14, y: 23, fill: foreground, 'font-size': 16, 'font-family': 'system-ui, sans-serif' }, panel.querySelector('h3').textContent));
  names.forEach((workload, index) => {
    const x = 14 + index % columns * (width / columns);
    const y = 44 + Math.floor(index / columns) * 19;
    exported.append(svgNode('circle', { cx: x + 4, cy: y - 4, r: 4, fill: colors.getPropertyValue(`--series-${seriesIndex(workload)}`).trim() }));
    exported.append(svgNode('text', { x: x + 14, y, fill: foreground, 'font-size': 11, 'font-family': 'system-ui, sans-serif' }, state.data.workloads[workload]));
  });
  exported.append(clone);
  const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(exported)], { type: 'image/svg+xml' }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `${name}.svg`;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function chart(metric, sampleWorkload = null) {
  const panel = html('article', 'chart');
  panel.dataset.metric = sampleWorkload ? `data-${sampleWorkload}` : metric.key;
  const header = html('div', 'chart-header');
  const heading = html('div');
  heading.append(html('h3', '', sampleWorkload ? state.data.workloads[sampleWorkload] : metric.title));
  heading.append(html('div', 'unit', sampleWorkload ? 'Round timings · ms' : metric.unit));
  const svg = svgNode('svg', { class: 'plot', role: 'img' });
  const download = html('button', '', 'Save SVG');
  download.type = 'button';
  download.setAttribute('aria-label', `Save ${sampleWorkload || metric.title} graph as SVG`);
  download.addEventListener('click', () => downloadSvg(svg, panel.dataset.metric));
  header.append(heading, download);
  panel.append(header, svg, valueTable(metric, sampleWorkload));
  if (sampleWorkload && selectedReports().some(report => (report.benchmarks[sampleWorkload].data?.length || 0) > 300)) {
    panel.append(html('p', 'unit', 'Plot shows up to 300 evenly spaced samples per stage. Exact values retains every sample.'));
  }
  const redraw = () => drawChart(svg, metric, sampleWorkload);
  state.charts.push({ panel, redraw });
  return panel;
}

function renderCharts() {
  tooltip.hidden = true;
  state.charts.forEach(({ panel }) => observer.unobserve(panel));
  state.charts = [];
  ['performance', 'samples', 'benchmark-details'].forEach(id => byId(id).replaceChildren());
  const hasReports = selectedReports().length > 0;
  ['performance', 'samples', 'benchmark-details'].forEach(id => { byId(`${id}-section`).hidden = !hasReports; });
  if (!hasReports) return;
  renderBenchmarkDetails();
  METRICS.forEach(metric => byId(metric.section).append(chart(metric)));
  selectedWorkloads().forEach(name => byId('samples').append(chart({ key: 'data', unit: 'ms', milliseconds: true }, name)));
  state.charts.forEach(({ panel, redraw }) => { observer.observe(panel); redraw(); });
}

function drawChart(svg, metric, sampleWorkload) {
  const reports = selectedReports();
  const names = sampleWorkload ? [sampleWorkload] : selectedWorkloads();
  const width = Math.round(svg.closest('.chart').getBoundingClientRect().width - (innerWidth < 600 ? 26 : 34));
  if (!reports.length || width <= 0) return;
  const height = 260;
  const allValues = reports.flatMap(report => names.flatMap(name => sampleWorkload ?
    (report.benchmarks[name].data || []).map(value => value * 1000) : [metricValue(report, name, metric)]
  )).filter(value => value !== null);
  const maximum = allValues.reduce((highest, value) => Math.max(highest, value), 0);
  const upper = maximum > 0 ? maximum * 1.12 : 1;
  const tickDigits = upper < 2 ? 3 : 1;
  const bounds = { left: Math.max(70, 26 + number(upper, tickDigits).length * 7), right: width - 16, top: 15, bottom: 194 };
  const x = index => reports.length === 1 ? (bounds.left + bounds.right) / 2 :
    bounds.left + 16 + index / (reports.length - 1) * (bounds.right - bounds.left - 32);
  const y = value => bounds.bottom - value / upper * (bounds.bottom - bounds.top);
  svg.replaceChildren();
  svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
  svg.setAttribute('aria-label', `${sampleWorkload ? state.data.workloads[sampleWorkload] + ' raw timing samples' : metric.title}, across ${reports.length} stages. Exact values are available below.`);
  svg.append(svgNode('title', {}, svg.getAttribute('aria-label')));
  for (let index = 0; index <= 4; index++) {
    const value = upper * index / 4;
    svg.append(svgNode('line', { class: 'grid', x1: bounds.left, x2: bounds.right, y1: y(value), y2: y(value) }));
    svg.append(svgNode('text', { x: bounds.left - 9, y: y(value) + 4, 'text-anchor': 'end' }, number(value, upper < 2 ? 3 : 1)));
  }
  const stride = Math.max(1, Math.ceil(reports.length / Math.max(2, (width - bounds.left) / 88)));
  reports.forEach((report, index) => {
    if (index % stride && index !== reports.length - 1) return;
    const text = svgNode('text', { class: 'stage-tick', x: x(index), y: 218, 'text-anchor': index === 0 && reports.length > 1 ? 'start' : index === reports.length - 1 && reports.length > 1 ? 'end' : 'middle' },
      report.label.length > 15 ? report.label.slice(0, 13) + '…' : report.label);
    text.append(svgNode('title', {}, report.label));
    svg.append(text);
  });
  svg.append(svgNode('text', { class: 'axis-title', x: (bounds.left + bounds.right) / 2, y: 245, 'text-anchor': 'middle' }, 'Benchmark stage'));
  svg.append(svgNode('text', { class: 'axis-title', x: 13, y: 102, transform: 'rotate(-90 13 102)', 'text-anchor': 'middle' }, metric.milliseconds ? 'Milliseconds' : metric.key === 'ops' ? 'Operations / s' : 'Count'));
  names.forEach(name => {
    const color = `var(--series-${seriesIndex(name)})`;
    if (sampleWorkload) {
      reports.forEach((report, stage) => {
        const samples = report.benchmarks[name].data || [];
        const limit = Math.min(samples.length, 300);
        for (let index = 0; index < limit; index++) {
          const source = limit === samples.length ? index : Math.round(index * (samples.length - 1) / (limit - 1));
          const jitter = limit === 1 ? 0 : (index / (limit - 1) - .5) * 24;
          svg.append(svgNode('circle', { class: 'sample', cx: x(stage) + jitter, cy: y(samples[source] * 1000), r: 3, style: `--color:${color}` }));
        }
        const median = report.benchmarks[name].median;
        if (samples.length && typeof median === 'number') {
          svg.append(svgNode('line', { x1: x(stage) - 16, x2: x(stage) + 16, y1: y(median * 1000), y2: y(median * 1000), stroke: color, 'stroke-width': 2 }));
        }
      });
    } else {
      let path = '';
      let connected = false;
      reports.forEach((report, index) => {
        const value = metricValue(report, name, metric);
        if (value === null) { connected = false; return; }
        path += `${connected ? 'L' : 'M'}${x(index)},${y(value)} `;
        connected = true;
      });
      svg.append(svgNode('path', { class: 'series-line', d: path, style: `--color:${color}` }));
      reports.forEach((report, index) => {
        const value = metricValue(report, name, metric);
        if (value === null) return;
        const marker = svgNode('circle', { class: 'marker', cx: x(index), cy: y(value), r: 4, style: `--color:${color}` });
        marker.append(svgNode('title', {}, `${report.label} · ${state.data.workloads[name]}: ${number(value, 4)}`));
        svg.append(marker);
      });
    }
  });
  if (!names.length || !allValues.length) svg.append(svgNode('text', { x: (bounds.left + bounds.right) / 2, y: 100, 'text-anchor': 'middle' }, names.length ? 'Raw samples not recorded' : 'Select a workload'));
  const guide = svgNode('line', { class: 'hover-line', x1: 0, x2: 0, y1: bounds.top, y2: bounds.bottom, visibility: 'hidden' });
  svg.append(guide);
  let previousTick = null;
  const stageTicks = [...svg.querySelectorAll('.stage-tick')];
  stageTicks.forEach((tick, index) => {
    if (previousTick) {
      const before = previousTick.getBBox();
      const current = tick.getBBox();
      if (before.x + before.width + 4 > current.x) {
        if (index === stageTicks.length - 1) previousTick.remove();
        else { tick.remove(); return; }
      }
    }
    previousTick = tick;
  });
  svg.onpointermove = event => {
    const rect = svg.getBoundingClientRect();
    const cursor = (event.clientX - rect.left) * width / rect.width;
    const stage = reports.length === 1 ? 0 : Math.max(0, Math.min(reports.length - 1,
      Math.round((cursor - x(0)) / (x(reports.length - 1) - x(0)) * (reports.length - 1))));
    guide.setAttribute('x1', x(stage));
    guide.setAttribute('x2', x(stage));
    guide.setAttribute('visibility', 'visible');
    showTooltip(event, reports, stage, names, metric, sampleWorkload);
  };
  svg.onpointerleave = () => { tooltip.hidden = true; guide.setAttribute('visibility', 'hidden'); };
}

function showTooltip(event, reports, index, names, metric, samples) {
  tooltip.replaceChildren(html('div', 'tip-title', reports[index].label));
  if (reports[index].source_reports) tooltip.append(html('small', '', 'Median of 3 suite statistics · all raw rounds retained'));
  names.forEach(name => {
    const row = html('div', 'tip-row');
    row.style.setProperty('--color', `var(--series-${seriesIndex(name)})`);
    const content = html('div');
    if (samples) {
      const data = reports[index].benchmarks[name].data;
      content.append(html('div', '', `${state.data.workloads[name]} · ${data ? data.length + ' rounds' : 'Raw samples not recorded'}`));
      if (data) content.append(html('small', '', `Median ${number(reports[index].benchmarks[name].median * 1000)} ms · horizontal mark`));
    } else {
      const value = metricValue(reports[index], name, metric);
      const base = metricValue(reports[0], name, metric);
      const previous = index > 0 ? metricValue(reports[index - 1], name, metric) : null;
      content.append(html('div', '', `${state.data.workloads[name]}: ${number(value, 4)} ${metric.key === 'ops' ? (name.includes('create') ? 'batches/s' : 'requests/s') : metric.milliseconds ? 'ms' : ''}`));
      content.append(html('small', '', `Change vs baseline: ${delta(value, base)} · vs previous: ${delta(value, previous)}`));
    }
    row.append(html('span', 'tip-dot'), content);
    tooltip.append(row);
  });
  tooltip.hidden = !names.length;
  const rect = tooltip.getBoundingClientRect();
  tooltip.style.left = `${Math.max(10, Math.min(innerWidth - rect.width - 10, event.clientX + 14))}px`;
  tooltip.style.top = `${Math.max(10, Math.min(innerHeight - rect.height - 10, event.clientY + 14))}px`;
}

const observer = new ResizeObserver(() => state.charts.forEach(({ redraw }) => redraw()));
byId('group').addEventListener('change', () => { state.group = byId('group').value; render(); });

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document.querySelectorAll('[data-theme-choice]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.themeChoice === theme));
  });
}

document.querySelectorAll('[data-theme-choice]').forEach(button => {
  button.addEventListener('click', () => {
    const theme = button.dataset.themeChoice;
    applyTheme(theme);
    try { localStorage.setItem('benchmark-dashboard-theme', theme); }
    catch { /* A blocked storage setting must not prevent theme switching. */ }
  });
});
applyTheme(document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light');

async function refresh() {
  if (state.pending) return;
  state.pending = true;
  try {
    const response = await fetch('/api/reports', { cache: 'no-store' });
    if (!response.ok) throw new Error(`Report server returned ${response.status}`);
    const data = await response.json();
    data.reports = displayedEntries(data.reports, 'benchmarks');
    const presentation = benchmarkStages(data.reports);
    data.reports = presentation.reports;
    data.warnings.push(...presentation.warnings);
    const signature = JSON.stringify(data);
    const editing = byId('stages').contains(document.activeElement);
    if (signature !== state.signature && (!editing || !state.signature)) {
      state.data = data;
      state.signature = signature;
      render();
    }
    if (!byId('benchmark-panel').hidden) {
      const retained = data.reports.filter(report => !report.history_reason).length;
      setLiveStatus(`Live · ${retained} stages`, `${retained} plotted · ${data.reports.length - retained} supporting/archived reports · checks every 3 seconds`);
    }
    showError('');
  } catch (error) {
    byId('status').textContent = 'Reconnecting…';
    showError(`Dashboard connection interrupted. Existing graphs are retained. ${error.message}`);
  } finally { state.pending = false; }
}

refresh();
setInterval(refresh, 3000);
