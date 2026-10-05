// Pure presentation checks: no browser, services, or measurement files changed.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../dashboard/dashboard.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source.split('const METRICS =')[0], context);
const present = reports => context.benchmarkStages(reports);
const fixture = (id, value = 1, group = 'same') => ({
  id, group, label: id, history_reason: null,
  benchmarks: { workload: { median: value, mean: value + 1, min: value / 2,
    max: value + 2, rounds: 2, iterations: 1, data: [value, value + 1] } }
});
const finalIds = [1, 2, 3].map(n => `frozen-final-b18ff49d14ea/000${n}_frozen-final.json`);
const parserIds = [2, 3, 6].map(n => `incremental-post-20261004-153f2b97e2c0/000${n}_streaming-post-after.json`);
const singles = ['baseline', 'encoder', 'session', 'response', 'copy'].map(id => fixture(id));

test('six points; all six supporting repeats preserved; no input mutation', () => {
  const reports = [...singles, ...parserIds.map(id => fixture(id)),
    ...finalIds.map((id, i) => fixture(id, [3, 1, 2][i]))];
  const original = JSON.stringify(reports);
  const result = present(reports);
  assert.equal(result.warnings.length, 0);
  assert.equal(result.reports.filter(report => !report.history_reason).length, 6);
  assert.equal(result.reports.filter(report => report.history_reason).length, 6);
  const final = result.reports.at(-1);
  assert.equal(final.label, 'Incremental POST parsing + tuning');
  assert.equal(final.benchmarks.workload.median, 2);
  assert.equal(final.benchmarks.workload.mean, 3);
  assert.equal(final.benchmarks.workload.min, 1);
  assert.equal(final.benchmarks.workload.max, 4);
  assert.deepEqual(Array.from(final.benchmarks.workload.data), [3, 4, 1, 2, 2, 3]);
  assert.equal(final.source_reports.length, 3);
  assert.equal(JSON.stringify(reports), original);
});

test('missing or incompatible final runs do not produce a misleading summary', () => {
  for (const sources of [finalIds.slice(0, 2).map(id => fixture(id)),
    finalIds.map((id, i) => fixture(id, 1, i === 2 ? 'different' : 'same'))]) {
    const result = present([...singles, ...sources]);
    assert.equal(result.reports.filter(report => !report.history_reason).length, 5);
    assert.equal(result.warnings.length, 1);
  }
});

test('absent raw rounds stay missing rather than being invented', () => {
  const sources = finalIds.map(id => fixture(id));
  delete sources[1].benchmarks.workload.data;
  const final = present(sources).reports.at(-1);
  assert.equal(final.benchmarks.workload.data, undefined);
});

test('existing archived experiments stay archived and one-S3-read stays omitted', () => {
  const archived = { ...fixture('rejected'), history_reason: 'Rejected' };
  const reports = context.displayedEntries([archived,
    fixture('Darwin-CPython-3.11-64bit/0006_baseline.json'),
    ...finalIds.map(id => fixture(id))], 'benchmarks');
  const result = present(reports);
  assert.equal(result.reports.find(report => report.id === 'rejected').history_reason, 'Rejected');
  assert.equal(result.reports.some(report => report.id.includes('0006_baseline')), false);
});
