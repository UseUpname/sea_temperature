const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const root = path.resolve(__dirname, '..');
const detail = fs.readFileSync(path.join(root, 'farm_detail.html'), 'utf8');

function source(name) {
  const start = detail.search(new RegExp(`(?:async )?function ${name}\\(`));
  assert.ok(start >= 0);
  return detail.slice(start, detail.indexOf('\n}', start) + 2);
}

function setup(search = '') {
  const nodes = new Map();
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, { value: '', innerHTML: '', hidden: false });
    return nodes.get(id);
  };
  const context = vm.createContext({
    URLSearchParams, console, location: { search, hash: '' },
    Shell: { mode: 'heat', reloadNoti() {} },
    apiOk: false, apiList: [], apiSel: null, apiData: null, apiErr: null,
    apiRequestId: 0, detailMode: 'mock', page: 'detail', farms: [], selected: null,
    apiListLoading: false, apiPending: null, homeRegion: 'test-region', fcRegion: 'test-region',
    notices: [], toasts: [], boxes: [],
    document: { getElementById: element, querySelectorAll: () => [] },
    esc: value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
    renderBrief() {},
    getOffice: () => 'test-region', putOffice() {}, hasOffice: () => false,
    loadFarms: async () => {}, fillOffice: current => current, sorted: () => [], renderHome() {},
  });
  context.showPage = page => { context.page = page; if (page === 'detail') context.renderBrief(); };
  context.addNotice = message => context.notices.push(message);
  context.toast = message => context.toasts.push(message);
  context.briefBoxes = (...html) => context.boxes.push(html);
  vm.runInContext(detail.match(/^const regionHouseholds = .+$/m)[0], context);
  vm.runInContext(['errCode', 'initApi', 'loadApiDetail', 'renderPicker', 'renderApiDetail', 'saveApiLog', 'startDetailPage']
    .map(source).join('\n'), context);
  return { context, element };
}

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test('direct links select the requested household; missing IDs do not select another', async () => {
  for (const [search, expected] of [['?id=2', 2], ['?id=999', null], ['?id=bad', null], ['', 1]]) {
    const { context } = setup(search);
    context.apiGet = async () => [1, 2].map(household_id => ({
      household_id, region: 'test-region', species: [], owner_name: '', household_name: '',
    }));
    await context.initApi();
    assert.equal(context.apiSel, expected);
    assert.equal(context.detailMode, 'api');
    if (expected === null) assert.ok(context.apiErr);
  }
});

test('empty lists and connection errors show an explicit state', async () => {
  const { context } = setup();
  context.apiGet = async () => [];
  await context.initApi();
  await context.renderApiDetail();
  assert.ok(context.boxes.at(-1)[0].includes('등록된 어가 없음'));
  context.apiGet = async () => { throw Error('HTTP 503'); };
  await context.initApi();
  await context.renderApiDetail();
  assert.equal(context.apiOk, false);
  assert.equal(context.apiSel, null);
  assert.ok(context.notices[0].includes('오류코드: 503'));
  assert.ok(context.boxes.at(-1)[0].includes('오류코드: 503'));
});

test('direct detail starts before region and household lists finish', async () => {
  const { context } = setup('?id=2');
  const regions = deferred(), households = deferred();
  const calls = [];
  context.loadFarms = () => regions.promise;
  context.apiGet = path => {
    calls.push(path);
    return path.includes('/2/detail')
      ? Promise.resolve({ household: { household_id: 2 } })
      : households.promise;
  };
  context.renderBrief = () => { if (context.apiSel != null) context.loadApiDetail(context.apiSel); };
  const start = context.startDetailPage();
  await new Promise(setImmediate);
  assert.equal(context.apiData.household.household_id, 2);
  assert.equal(context.apiListLoading, true);
  assert.deepEqual(calls, ['/api/households?limit=1000', '/api/households/2/detail']);
  households.resolve([{ household_id: 2, region: 'other-region', species: [] }]);
  await new Promise(setImmediate);
  regions.resolve();
  await start;
  assert.equal(context.homeRegion, 'other-region');
  assert.equal(calls.filter(path => path.includes('/2/detail')).length, 1);
});

test('default detail does not wait for regional forecasts', async () => {
  const { context } = setup();
  const regions = deferred();
  const calls = [];
  context.loadFarms = () => regions.promise;
  context.apiGet = async path => {
    calls.push(path);
    return path.includes('/detail')
      ? { household: { household_id: 1 } }
      : [{ household_id: 1, region: 'test-region', species: [] }];
  };
  context.renderBrief = () => { if (context.apiSel != null) context.loadApiDetail(context.apiSel); };
  const start = context.startDetailPage();
  await new Promise(setImmediate);
  assert.equal(context.apiData.household.household_id, 1);
  assert.ok(calls.includes('/api/households/1/detail'));
  regions.resolve();
  await start;
});

test('rendering the same pending household shares one detail request', async () => {
  const { context } = setup();
  const response = deferred();
  let calls = 0;
  context.detailMode = 'api';
  context.apiSel = 1;
  context.apiGet = () => { ++calls; return response.promise; };
  const first = context.loadApiDetail(1);
  const second = context.loadApiDetail(1);
  assert.equal(calls, 1);
  response.resolve({ household: { household_id: 1 } });
  assert.deepEqual(await Promise.all([first, second]), [true, true]);
  assert.equal(context.apiPending, null);
});

test('household-list failure preserves successfully loaded direct detail', async () => {
  const { context } = setup('?id=2');
  const households = deferred();
  context.apiGet = path => path.includes('/detail')
    ? Promise.resolve({ household: { household_id: 2 } })
    : households.promise;
  const list = context.initApi();
  await context.loadApiDetail(2);
  households.reject(Error('HTTP 503'));
  await list;
  assert.equal(context.apiSel, 2);
  assert.equal(context.apiData.household.household_id, 2);
  assert.equal(context.apiErr, null);
  assert.ok(context.notices[0].includes('오류코드: 503'));
});

test('late household list cannot overwrite a newer household selection', async () => {
  const { context } = setup('?id=2');
  const households = deferred();
  context.apiGet = path => path.includes('/detail')
    ? Promise.resolve({ household: { household_id: 3 } })
    : households.promise;
  const list = context.initApi();
  context.apiSel = 3;
  await context.loadApiDetail(3);
  households.resolve([2, 3].map(household_id => ({ household_id, region: 'test-region', species: [] })));
  await list;
  assert.equal(context.apiSel, 3);
  assert.equal(context.apiData.household.household_id, 3);
});

test('invalid direct IDs never request a detail or fall back to another household', async () => {
  for (const id of ['bad', '0', '-1', '1.2']) {
    const { context } = setup(`?id=${id}`);
    const calls = [];
    context.apiGet = async path => {
      calls.push(path);
      return [{ household_id: 1, region: 'test-region', species: [] }];
    };
    context.renderBrief = () => { if (context.apiSel != null) context.loadApiDetail(context.apiSel); };
    await context.startDetailPage();
    assert.equal(context.apiSel, null);
    assert.ok(context.apiErr);
    assert.deepEqual(calls, ['/api/households?limit=1000']);
  }
});

test('a missing direct household invalidates its older pending detail', async () => {
  const { context } = setup('?id=999');
  const households = deferred(), response = deferred();
  context.apiGet = path => path.includes('/detail') ? response.promise : households.promise;
  const list = context.initApi();
  const pending = context.loadApiDetail(999);
  households.resolve([{ household_id: 1, region: 'test-region', species: [] }]);
  await list;
  response.resolve({ household: { household_id: 999 } });
  assert.equal(await pending, false);
  assert.equal(context.apiSel, null);
  assert.equal(context.apiData, null);
  assert.ok(context.apiErr);
});

test('an older success or failure cannot overwrite the latest selection', async () => {
  for (const fail of [false, true]) {
    const { context } = setup();
    const old = deferred();
    context.detailMode = 'api';
    context.apiSel = 1;
    context.apiGet = url => url.includes('/1/') ? old.promise : Promise.resolve({ household: { household_id: 2 } });
    const pending = context.loadApiDetail(1);
    context.apiSel = 2;
    assert.equal(await context.loadApiDetail(2), true);
    if (fail) old.reject(Error('Old request failed'));
    else old.resolve({ household: { household_id: 1 } });
    assert.equal(await pending, false);
    assert.equal(context.apiData.household.household_id, 2);
    assert.equal(context.apiErr, null);
  }
});

test('a newer refresh of the same household supersedes an older request', async () => {
  const { context } = setup();
  const old = deferred();
  context.detailMode = 'api'; context.apiSel = 1;
  context.apiGet = () => old.promise;
  const pending = context.loadApiDetail(1, true);
  context.apiGet = async () => ({ household: { household_id: 1 }, version: 2 });
  await context.loadApiDetail(1, true);
  old.resolve({ household: { household_id: 1 }, version: 1 });
  assert.equal(await pending, false);
  assert.equal(context.apiData.version, 2);
});

test('saving is blocked when the displayed household differs from the selection', async () => {
  const { context } = setup();
  context.apiSel = 2; context.apiData = { household: { household_id: 1 } };
  let calls = 0;
  context.apiPost = async () => { calls++; };
  await context.saveApiLog();
  assert.equal(calls, 0);
  assert.equal(context.toasts.length, 1);
});

test('next contact defaults to current Korean time and preserves saved appointments', async () => {
  for (const [now, saved, expected] of [
    ['2026-10-29T05:27:45Z', null, '2026-10-29T14:27'],
    ['2026-10-29T18:30:00Z', null, '2026-10-30T03:30'],
    ['2026-10-29T05:27:45Z', '2026-11-02T01:30:00Z', '2026-11-02T10:30'],
    ['2026-10-29T05:27:45Z', '2026-11-02T10:30:00+09:00', '2026-11-02T10:30'],
  ]) {
    const { context } = setup();
    context.Date = class extends Date {
      static now() { return Date.parse(now); }
    };
    Object.assign(context, {
      detailMode: 'api', apiSel: 1,
      API_GAP: [], API_STATUS: [], STAGE: [''],
      acc: (_key, _title, _summary, body) => body,
      govAcc: () => '', helpAcc: () => '', scriptBox: () => '', mdStr: () => '',
      loadGov() {}, updateApiCalc() {},
      apiData: {
        household: { household_id: 1 }, cards: [], checks: [], script: '', guide: '',
        sea: { today: {}, stage: {}, roms: [], om: [] }, stage: { level: 0 },
        consult: { status: '', season: '', logs: [], next_contact_at: saved },
      },
    });
    await context.renderApiDetail();
    const html = context.boxes.at(-1)[1];
    const value = html.match(/id="r-next"[^>]*value="([^"]*)"/);
    assert.ok(value, 'next-contact field must have a default value');
    assert.equal(value[1], expected);
  }
});

test('a save response cannot overwrite a newly selected household', async () => {
  const { context } = setup();
  const pending = deferred();
  context.detailMode = 'api'; context.apiSel = 1;
  context.apiData = { household: { household_id: 1 } };
  let target;
  context.apiPost = url => { target = url; return pending.promise; };
  const saving = context.saveApiLog();
  context.apiSel = 2;
  context.apiData = { household: { household_id: 2 } };
  pending.resolve({ consult: { status: 'saved' } });
  await saving;
  assert.equal(target, '/api/households/1/consult-logs');
  assert.equal(context.apiData.consult, undefined);
  assert.equal(context.toasts.length, 0);
});

test('consultation detail buttons navigate with an encoded household ID', () => {
  const html = fs.readFileSync(path.join(root, 'consult_status.html'), 'utf8');
  const start = html.indexOf("consultEl.addEventListener('click',");
  const handler = html.slice(start, html.indexOf('\n});', start) + 4);
  let click;
  const context = vm.createContext({
    encodeURIComponent, location: { href: '' },
    consultEl: { addEventListener: (event, callback) => { click = callback; } },
  });
  vm.runInContext(handler, context);
  click({ target: { closest: selector => selector === '[data-open]' ? { dataset: { open: '42&next=x' } } : null } });
  assert.equal(context.location.href, 'farm_detail.html?id=42%26next%3Dx');
});
