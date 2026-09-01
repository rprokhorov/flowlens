'use strict';

// ============================================================================
// Константы
// ============================================================================

// Цвет закреплён за фазой навсегда: фильтрация не должна перекрашивать
// оставшиеся серии — читатель, запомнивший «блокировка красная», не должен
// переучиваться.
const PHASE_ORDER = ['backlog', 'in_progress', 'blocked', 'verify', 'done_pending', 'done'];
const PHASE_SLOT = {
  backlog: 4, in_progress: 1, blocked: 8, verify: 7, done_pending: 3, done: 6,
};
const PHASE_LABEL = {
  backlog: 'В очереди', in_progress: 'В работе', blocked: 'Заблокировано',
  verify: 'Проверка', done_pending: 'Ждёт релиза', done: 'Готово',
  triage: 'Разбор', review: 'Ревью', cancelled: 'Отменено',
};
const SOURCE_LABEL = {
  declared: 'заявленные даты', system: 'история статусов',
  reconciled: 'согласовано', inferred: 'выведено', none: 'нет данных',
};
const CONFIDENCE_LABEL = { high: 'надёжно', medium: 'средне', low: 'ненадёжно' };

const charts = {};
const loaded = {};      // какие вкладки уже загружены
let unit = 'business';
let currentTab = 'overview';
let ticketPage = 0;
const PAGE_SIZE = 50;

// ============================================================================
// Утилиты
// ============================================================================

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function phaseColor(phase) {
  return css(`--series-${PHASE_SLOT[phase] || 5}`);
}
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text == null ? '' : String(text);
  return div.innerHTML;
}
function hours(seconds) {
  if (seconds == null) return '—';
  const h = seconds / 3600;
  if (h < 1) return `${Math.round(seconds / 60)} мин`;
  if (h < 24) return `${h.toFixed(1)} ч`;
  const workdays = h / 9;
  return workdays < 10 ? `${workdays.toFixed(1)} дн` : `${Math.round(workdays)} дн`;
}
function percent(value) {
  return value == null ? '—' : `${(value * 100).toFixed(0)}%`;
}
function plural(n, one, few, many) {
  const mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few;
  return many;
}
function formatDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString('ru-RU', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
}
function formatDay(iso) {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('ru-RU',
    { day: '2-digit', month: '2-digit', year: 'numeric' });
}

// ============================================================================
// Запросы
// ============================================================================

function filterParams() {
  const params = new URLSearchParams();
  const get = id => document.getElementById(id).value;

  if (get('f-from')) params.set('date_from', get('f-from'));
  if (get('f-to')) params.set('date_to', get('f-to'));
  if (get('f-type')) params.set('issue_type', get('f-type'));
  if (get('f-priority')) params.set('priority', get('f-priority'));
  if (get('f-component')) params.set('component', get('f-component'));
  if (get('f-confidence') !== 'low') params.set('min_confidence', get('f-confidence'));
  params.set('unit', unit);
  return params;
}

async function fetchJson(path, extra) {
  const params = filterParams();
  if (extra) for (const [k, v] of Object.entries(extra)) {
    if (v !== null && v !== undefined && v !== '') params.set(k, v);
  }
  const response = await fetch(`${path}?${params}`);
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

// ============================================================================
// Общие настройки графиков
// ============================================================================

function baseOption() {
  return {
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'inherit', color: css('--text-secondary') },
    grid: { left: 8, right: 14, top: 30, bottom: 8, containLabel: true },
    tooltip: {
      backgroundColor: css('--surface-1'),
      borderColor: css('--border'),
      textStyle: { color: css('--text-primary'), fontSize: 12 },
      extraCssText: 'box-shadow: 0 2px 12px rgba(0,0,0,.12); border-radius: 8px;',
    },
    legend: {
      textStyle: { color: css('--text-secondary'), fontSize: 12 },
      icon: 'roundRect', itemWidth: 10, itemHeight: 10, top: 0,
    },
  };
}
function axisStyle() {
  return {
    axisLine: { lineStyle: { color: css('--border') } },
    axisTick: { show: false },
    axisLabel: { color: css('--text-muted'), fontSize: 11 },
    splitLine: { lineStyle: { color: css('--grid'), type: 'dashed' } },
  };
}
function ensureChart(id) {
  const element = document.getElementById(id);
  if (!element) return null;
  if (!charts[id]) charts[id] = echarts.init(element, null, { renderer: 'canvas' });
  return charts[id];
}
function emptyChart(chart, message) {
  chart.setOption({
    ...baseOption(),
    title: {
      text: message, left: 'center', top: 'middle',
      textStyle: { color: css('--text-muted'), fontSize: 13, fontWeight: 'normal' },
    },
  }, true);
}

// ============================================================================
// Вкладка: обзор
// ============================================================================

function renderTiles(summary, quality) {
  const trust = quality.trustworthy_pct;
  const trustClass = trust >= 80 ? 'good' : trust >= 50 ? 'warning' : 'critical';
  const efficiency = summary.avg_flow_efficiency;
  const effClass = efficiency == null ? '' : efficiency >= 0.4 ? 'good'
    : efficiency >= 0.25 ? 'warning' : 'critical';

  const tiles = [
    { label: 'Задач в периоде', value: summary.total_tickets,
      note: `${summary.completed} завершено` },
    { label: 'Не завершено', value: summary.open_tickets, note: 'на текущий момент' },
    { label: 'Время цикла, медиана', value: hours(summary.p50_cycle_s),
      note: `85-й перцентиль: ${hours(summary.p85_cycle_s)}` },
    { label: 'Эффективность потока', value: percent(efficiency),
      note: 'доля времени в работе', cls: effClass },
    { label: 'Задач с блокировками', value: summary.ever_blocked,
      note: summary.total_tickets
        ? `${Math.round(100 * summary.ever_blocked / summary.total_tickets)}% от всех` : '' },
    { label: 'Надёжных метрик', value: `${trust}%`,
      note: 'остальные — с оговорками', cls: trustClass },
  ];

  document.getElementById('tiles').innerHTML = tiles.map(t => `
    <div class="tile ${t.cls || ''}">
      <div class="label">${t.label}</div>
      <div class="value">${t.value}</div>
      <div class="note">${t.note || ''}</div>
    </div>`).join('');
}

function renderAdvice(data) {
  const container = document.getElementById('advice');
  if (!data.findings.length) {
    container.innerHTML =
      '<div class="no-findings">Отклонений по заданным порогам не обнаружено.</div>';
    return;
  }
  container.innerHTML = data.findings.map(f => `
    <div class="finding ${f.severity}">
      <div class="finding-mark"></div>
      <div class="finding-body">
        <div class="finding-title">${escapeHtml(f.title)}</div>
        <div class="finding-detail">${escapeHtml(f.detail)}</div>
        <div class="finding-suggestion">${escapeHtml(f.suggestion)}</div>
        ${f.tickets.length ? `<div class="finding-tickets">${
          f.tickets.map(k =>
            `<span class="pill key-link" data-ticket="${escapeHtml(k)}">${escapeHtml(k)}</span>`
          ).join('')
        }</div>` : ''}
      </div>
    </div>`).join('');
}

async function loadOverview() {
  const [summary, quality, advice] = await Promise.all([
    fetchJson('/api/summary'),
    fetch('/api/quality').then(r => r.json()),
    fetchJson('/api/advice'),
  ]);
  renderTiles(summary, quality);
  renderAdvice(advice);
}

// ============================================================================
// Вкладка: поток
// ============================================================================

function renderCfd(data) {
  const chart = ensureChart('chart-cfd');
  if (!data.series.length) return emptyChart(chart, 'Нет данных за период');

  const ordered = PHASE_ORDER
    .map(phase => data.series.find(s => s.phase === phase))
    .filter(Boolean)
    .concat(data.series.filter(s => !PHASE_ORDER.includes(s.phase)));

  // «Готово» накапливается за весь период и по величине давит остальные фазы,
  // из-за чего незавершённая работа — то, ради чего смотрят CFD — не читается.
  const legendSelected = {};
  for (const s of ordered) {
    legendSelected[PHASE_LABEL[s.phase] || s.phase] = s.phase !== 'done';
  }

  chart.setOption({
    ...baseOption(),
    tooltip: { ...baseOption().tooltip, trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: css('--text-muted') } } },
    legend: { ...baseOption().legend,
      data: ordered.map(s => PHASE_LABEL[s.phase] || s.phase), selected: legendSelected },
    xAxis: { type: 'category', data: data.days, boundaryGap: false, ...axisStyle() },
    yAxis: { type: 'value', ...axisStyle(), name: 'задач', nameTextStyle: { fontSize: 11 } },
    dataZoom: [
      { type: 'inside', start: 55, end: 100 },
      { type: 'slider', start: 55, end: 100, height: 18, bottom: 4,
        borderColor: css('--border'), fillerColor: css('--surface-2'),
        textStyle: { color: css('--text-muted'), fontSize: 10 } },
    ],
    grid: { left: 8, right: 14, top: 30, bottom: 42, containLabel: true },
    series: ordered.map(s => ({
      name: PHASE_LABEL[s.phase] || s.phase,
      type: 'line', stack: 'total', areaStyle: { opacity: 0.85 },
      lineStyle: { width: 0 }, showSymbol: false,
      emphasis: { focus: 'series' },
      itemStyle: { color: phaseColor(s.phase) },
      data: s.values,
    })),
  }, true);
}

function renderArrival(data) {
  const chart = ensureChart('chart-arrival');
  if (!data.periods.length) return emptyChart(chart, 'Нет данных за период');

  chart.setOption({
    ...baseOption(),
    legend: { ...baseOption().legend, data: ['Поступило', 'Закрыто'] },
    tooltip: { ...baseOption().tooltip, trigger: 'axis' },
    xAxis: { type: 'category', data: data.periods, ...axisStyle() },
    yAxis: { type: 'value', ...axisStyle(), name: 'задач', nameTextStyle: { fontSize: 11 } },
    series: [
      { name: 'Поступило', type: 'line', smooth: 0.3, symbolSize: 8,
        lineStyle: { width: 2, color: css('--series-2') },
        itemStyle: { color: css('--series-2') }, data: data.arrived },
      { name: 'Закрыто', type: 'line', smooth: 0.3, symbolSize: 8,
        lineStyle: { width: 2, color: css('--series-3') },
        itemStyle: { color: css('--series-3') }, data: data.completed },
    ],
  }, true);
}

async function loadFlow() {
  const [cfd, arrival] = await Promise.all([
    fetchJson('/api/cfd'),
    fetchJson('/api/arrival-throughput', { granularity: 'week' }),
  ]);
  renderCfd(cfd);
  renderArrival(arrival);
}

// ============================================================================
// Вкладка: время цикла
// ============================================================================

function bucketIndex(histogram, value) {
  for (let i = 0; i < histogram.length; i++) {
    if (histogram[i].to == null || value < histogram[i].to) return i;
  }
  return histogram.length - 1;
}

function renderCycleTime(data) {
  const chart = ensureChart('chart-cycle');
  if (!data.count) {
    emptyChart(chart, 'Нет завершённых задач за период');
    document.getElementById('cycle-stats').innerHTML = '';
    document.getElementById('cycle-slowest').innerHTML =
      '<div class="muted">Нет данных.</div>';
    return;
  }

  const p = data.percentiles;
  chart.setOption({
    ...baseOption(),
    legend: { show: false },
    tooltip: { ...baseOption().tooltip, trigger: 'axis',
      formatter: params => {
        const bucket = data.histogram[params[0].dataIndex];
        const count = params[0].value;
        return `${hours(bucket.from)} — ${bucket.to ? hours(bucket.to) : '∞'}<br>` +
               `<b>${count}</b> ${plural(count, 'задача', 'задачи', 'задач')}` +
               `<br><span style="color:${css('--text-muted')}">нажмите, чтобы посмотреть</span>`;
      } },
    xAxis: { type: 'category', data: data.histogram.map(b => hours(b.from)), ...axisStyle() },
    yAxis: { type: 'value', ...axisStyle(), name: 'задач', nameTextStyle: { fontSize: 11 } },
    series: [{
      type: 'bar',
      data: data.histogram.map(b => b.count),
      itemStyle: { color: css('--series-1'), borderRadius: [4, 4, 0, 0] },
      barCategoryGap: '18%',
      markLine: {
        symbol: 'none',
        label: { formatter: item => item.name, color: css('--text-secondary'), fontSize: 11 },
        lineStyle: { color: css('--text-muted'), type: 'dashed', width: 1 },
        data: [
          { name: `медиана ${hours(p.p50)}`, xAxis: bucketIndex(data.histogram, p.p50) },
          { name: `85% ${hours(p.p85)}`, xAxis: bucketIndex(data.histogram, p.p85) },
        ],
      },
    }],
  }, true);

  // клик по столбцу открывает задачи из этого диапазона
  chart.off('click');
  chart.on('click', params => {
    const bucket = data.histogram[params.dataIndex];
    if (bucket) openBucketTickets(bucket);
  });

  document.getElementById('cycle-stats').innerHTML = [
    { label: 'Задач', value: data.count },
    { label: 'Медиана', value: hours(p.p50) },
    { label: '70-й перцентиль', value: hours(p.p70) },
    { label: '85-й перцентиль', value: hours(p.p85) },
    { label: '95-й перцентиль', value: hours(p.p95) },
    { label: 'Максимум', value: hours(data.max) },
  ].map(s => `
    <div class="stat-item">
      <div class="stat-label">${s.label}</div>
      <div class="stat-value">${s.value}</div>
    </div>`).join('');

  document.getElementById('cycle-slowest').innerHTML = `
    <table>
      <thead><tr><th>Задача</th><th>Тип</th><th class="num">Время цикла</th></tr></thead>
      <tbody>${data.slowest.map(t => `
        <tr>
          <td><span class="key-link" data-ticket="${escapeHtml(t.key)}">${escapeHtml(t.key)}</span></td>
          <td>${escapeHtml(t.type)}</td>
          <td class="num">${hours(t.value)}</td>
        </tr>`).join('')}</tbody>
    </table>`;
}

async function loadCycle() {
  renderCycleTime(await fetchJson('/api/cycle-time'));
}

// ============================================================================
// Вкладка: где время
// ============================================================================

function renderPhases(data) {
  const chart = ensureChart('chart-phases');
  const phases = data.by_phase
    .filter(p => p.phase !== 'done' && p.total_s > 0)
    .sort((a, b) => b.p50_s - a.p50_s);

  if (!phases.length) return emptyChart(chart, 'Нет данных за период');

  chart.setOption({
    ...baseOption(),
    legend: { show: false },
    tooltip: { ...baseOption().tooltip, trigger: 'item',
      formatter: item => {
        const phase = phases[item.dataIndex];
        return `<b>${PHASE_LABEL[phase.phase] || phase.phase}</b><br>` +
               `медиана: ${hours(phase.p50_s)}<br>` +
               `85-й перцентиль: ${hours(phase.p85_s)}<br>` +
               `всего за период: ${hours(phase.total_s)}`;
      } },
    grid: { left: 8, right: 70, top: 10, bottom: 8, containLabel: true },
    xAxis: { type: 'value', ...axisStyle(), name: 'часов на задачу (медиана)',
      nameLocation: 'middle', nameGap: 26, nameTextStyle: { fontSize: 11 },
      axisLabel: { ...axisStyle().axisLabel, formatter: v => Math.round(v / 3600) } },
    yAxis: { type: 'category', ...axisStyle(), splitLine: { show: false },
      data: phases.map(p => PHASE_LABEL[p.phase] || p.phase).reverse() },
    series: [{
      type: 'bar',
      data: phases.map(p => ({
        value: p.p50_s, itemStyle: { color: phaseColor(p.phase) }, phase: p.phase,
      })).reverse(),
      itemStyle: { borderRadius: [0, 4, 4, 0] },
      barCategoryGap: '30%',
      label: { show: true, position: 'right', color: css('--text-secondary'),
        fontSize: 11, formatter: item => hours(item.value) },
    }],
  }, true);

  chart.off('click');
  chart.on('click', params => {
    const phase = params.data && params.data.phase;
    if (phase) openPhaseIntervals(phase);
  });
}

function renderEfficiency(data) {
  const chart = ensureChart('chart-efficiency');
  const touch = data.touch_s || 0;
  const blocked = data.blocked_s || 0;
  const waiting = Math.max(0, (data.queue_s || 0) - blocked);

  if (!touch && !waiting && !blocked) return emptyChart(chart, 'Нет данных за период');

  chart.setOption({
    ...baseOption(),
    legend: { ...baseOption().legend, data: ['В работе', 'Ожидание', 'Заблокировано'] },
    tooltip: { ...baseOption().tooltip, trigger: 'item',
      formatter: item => `${item.name}: ${hours(item.value)} (${item.percent}%)` },
    series: [{
      type: 'pie', radius: ['45%', '72%'], center: ['50%', '55%'],
      itemStyle: { borderColor: css('--surface-1'), borderWidth: 2 },
      label: { color: css('--text-secondary'), fontSize: 12,
        formatter: item => `${item.name}\n${item.percent}%` },
      data: [
        { name: 'В работе', value: touch, itemStyle: { color: css('--series-1') } },
        { name: 'Ожидание', value: waiting, itemStyle: { color: css('--series-4') } },
        { name: 'Заблокировано', value: blocked, itemStyle: { color: css('--series-8') } },
      ],
    }],
  }, true);
}

async function loadPhases() {
  const data = await fetchJson('/api/flow-efficiency');
  renderPhases(data);
  renderEfficiency(data);
}

// ============================================================================
// Вкладка: незавершённое
// ============================================================================

function renderAging(data) {
  const chart = ensureChart('chart-aging');
  const hint = document.getElementById('aging-hint');
  const body = document.querySelector('#table-aging tbody');

  hint.textContent =
    `${data.total} ${plural(data.total, 'задача', 'задачи', 'задач')} не завершено, ` +
    `из них ${data.over_p85} висят дольше 85-го перцентиля (${hours(data.reference.p85)}).`;

  if (!data.items.length) {
    emptyChart(chart, 'Незавершённых задач нет');
    body.innerHTML = '<tr><td colspan="5" class="muted">Нет незавершённых задач</td></tr>';
    return;
  }

  // точечная диаграмма: возраст против позиции на доске
  const byPhase = {};
  for (const item of data.items) {
    (byPhase[item.phase] ||= []).push(item);
  }
  const phases = PHASE_ORDER.filter(p => byPhase[p]);

  chart.setOption({
    ...baseOption(),
    legend: { show: false },
    tooltip: { ...baseOption().tooltip, trigger: 'item',
      formatter: item => {
        const t = item.data.ticket;
        return `<b>${escapeHtml(t.key)}</b><br>` +
               `${escapeHtml(t.summary || '')}<br>` +
               `${escapeHtml(t.status)} · ${escapeHtml(t.assignee || 'без исполнителя')}<br>` +
               `возраст: ${hours(t.age_s)}`;
      } },
    grid: { left: 8, right: 20, top: 20, bottom: 8, containLabel: true },
    xAxis: { type: 'value', ...axisStyle(), name: 'возраст',
      nameLocation: 'middle', nameGap: 26, nameTextStyle: { fontSize: 11 },
      axisLabel: { ...axisStyle().axisLabel, formatter: v => hours(v) } },
    yAxis: { type: 'category', ...axisStyle(), splitLine: { show: false },
      data: phases.map(p => PHASE_LABEL[p] || p) },
    series: phases.map((phase, index) => ({
      type: 'scatter', name: PHASE_LABEL[phase] || phase,
      symbolSize: 9,
      itemStyle: { color: phaseColor(phase), opacity: 0.75 },
      data: byPhase[phase].map(t => ({ value: [t.age_s, index], ticket: t })),
      markLine: index === 0 && data.reference.p85 ? {
        symbol: 'none', silent: true,
        label: { formatter: '85-й перцентиль', color: css('--text-muted'), fontSize: 11 },
        lineStyle: { color: css('--status-warning'), type: 'dashed', width: 1 },
        data: [{ xAxis: data.reference.p85 }],
      } : undefined,
    })),
  }, true);

  chart.off('click');
  chart.on('click', params => {
    const ticket = params.data && params.data.ticket;
    if (ticket) openTicket(ticket.key);
  });

  body.innerHTML = data.items.slice(0, 100).map(item => `
    <tr>
      <td><span class="key-link" data-ticket="${escapeHtml(item.key)}">${escapeHtml(item.key)}</span></td>
      <td>${escapeHtml(item.status)}</td>
      <td>${escapeHtml(item.assignee || '—')}</td>
      <td class="num">${hours(item.age_s)}</td>
      <td>${item.is_blocked ? '<span class="pill blocked">блок</span>' : ''}
          ${item.over_p85 ? '<span class="pill over">долго</span>' : ''}</td>
    </tr>`).join('');
}

async function loadWip() {
  renderAging(await fetchJson('/api/aging-wip'));
}

// ============================================================================
// Вкладка: нагрузка
// ============================================================================

function renderPeople(data) {
  const chart = ensureChart('chart-people');
  const people = data.people.slice(0, 12);
  if (!people.length) {
    emptyChart(chart, 'Нет данных за период');
    document.getElementById('people-table').innerHTML = '<div class="muted">Нет данных.</div>';
    return;
  }

  chart.setOption({
    ...baseOption(),
    legend: { ...baseOption().legend, data: ['В работе', 'Ожидание', 'Заблокировано'] },
    tooltip: { ...baseOption().tooltip, trigger: 'axis', axisPointer: { type: 'shadow' },
      valueFormatter: value => hours(value) },
    grid: { left: 8, right: 14, top: 30, bottom: 8, containLabel: true },
    xAxis: { type: 'value', ...axisStyle(),
      axisLabel: { ...axisStyle().axisLabel,
        formatter: v => `${Math.round(v / 3600 / 9)} дн` },
      name: 'рабочих дней владения', nameLocation: 'middle', nameGap: 26,
      nameTextStyle: { fontSize: 11 } },
    yAxis: { type: 'category', ...axisStyle(), splitLine: { show: false },
      data: people.map(p => p.person).reverse() },
    series: [
      { name: 'В работе', type: 'bar', stack: 'load',
        itemStyle: { color: css('--series-1') },
        data: people.map(p => p.touch_s).reverse() },
      { name: 'Ожидание', type: 'bar', stack: 'load',
        itemStyle: { color: css('--series-4') },
        data: people.map(p => Math.max(0, p.owned_s - p.touch_s - p.blocked_s)).reverse() },
      { name: 'Заблокировано', type: 'bar', stack: 'load',
        itemStyle: { color: css('--series-8') },
        data: people.map(p => p.blocked_s).reverse() },
    ],
  }, true);

  document.getElementById('people-table').innerHTML = `
    <table>
      <thead><tr>
        <th>Человек</th>
        <th class="num">Владел</th><th class="num">В работе</th>
        <th class="num">Блокировки</th><th class="num">Закрыл</th>
        <th class="num">Задач в день</th><th class="num">Дней активности</th>
      </tr></thead>
      <tbody>${data.people.map(p => `
        <tr>
          <td>${escapeHtml(p.person)}</td>
          <td class="num">${hours(p.owned_s)}</td>
          <td class="num">${hours(p.touch_s)}</td>
          <td class="num">${hours(p.blocked_s)}</td>
          <td class="num">${p.completed}</td>
          <td class="num">${p.avg_active_tickets.toFixed(1)}</td>
          <td class="num">${p.active_days}</td>
        </tr>`).join('')}</tbody>
    </table>`;
}

async function loadPeople() {
  renderPeople(await fetchJson('/api/people'));
}

// ============================================================================
// Вкладка: прогноз
// ============================================================================

function renderForecast(data) {
  const container = document.getElementById('forecast');
  const long = data.how_long;
  const many = data.how_many;

  if (!long.percentiles || !Object.keys(long.percentiles).length) {
    container.innerHTML = `<div class="forecast-warning">${
      escapeHtml(long.warning || 'Недостаточно истории для прогноза.')}</div>`;
  } else {
    const levels = [50, 70, 85, 95];
    const longRows = levels.map(level => `
      <div class="forecast-row">
        <span class="forecast-prob">${level}%</span>
        <span class="forecast-value">${long.percentiles[level]} нед</span>
        <span class="forecast-when">${escapeHtml(long.dates[level] || '')}</span>
      </div>`).join('');

    const manyRows = levels.map(level => `
      <div class="forecast-row">
        <span class="forecast-prob">${level}%</span>
        <span class="forecast-value">${many.percentiles[level]}</span>
        <span class="forecast-when">задач и более</span>
      </div>`).join('');

    const health = data.wip_health || {};
    const season = data.seasonality || {};

    container.innerHTML = `
      <div class="forecast-grid">
        <div class="forecast-block">
          <h3>Когда закроем текущий объём (${data.backlog} задач)</h3>
          ${longRows}
          ${long.warning ? `<div class="forecast-warning">${escapeHtml(long.warning)}</div>` : ''}
        </div>
        <div class="forecast-block">
          <h3>Сколько успеем за ${many.periods} нед</h3>
          ${manyRows}
        </div>
        <div class="forecast-block">
          <h3>Незавершённая работа</h3>
          ${health.verdict ? `
            <div class="forecast-row">
              <span class="forecast-value">${health.measured_days} дн</span>
              <span class="forecast-when">измеренное время цикла</span>
            </div>
            <div class="forecast-row">
              <span class="forecast-value">${health.implied_days} дн</span>
              <span class="forecast-when">следует из объёма работы</span>
            </div>
            <div class="forecast-note">${escapeHtml(health.verdict)}</div>
          ` : '<div class="forecast-note">Недостаточно данных.</div>'}
          ${season.busiest ? `
            <div class="forecast-note">
              Больше всего задач приходит в ${escapeHtml(season.busiest)},
              меньше всего — в ${escapeHtml(season.quietest)}${
                season.ratio ? ` (разница в ${season.ratio} раза)` : ''}.
            </div>` : ''}
        </div>
      </div>`;
  }

  // выборка, на которой строится прогноз
  const chart = ensureChart('chart-throughput');
  const history = data.throughput_history || [];
  if (!history.length) return emptyChart(chart, 'Недостаточно истории');

  const mean = history.reduce((a, b) => a + b, 0) / history.length;
  chart.setOption({
    ...baseOption(),
    legend: { show: false },
    tooltip: { ...baseOption().tooltip, trigger: 'axis',
      formatter: params => `Неделя ${params[0].dataIndex + 1}: <b>${params[0].value}</b> задач` },
    xAxis: { type: 'category', ...axisStyle(),
      data: history.map((_, i) => `н${i + 1}`) },
    yAxis: { type: 'value', ...axisStyle(), name: 'закрыто задач',
      nameTextStyle: { fontSize: 11 } },
    series: [{
      type: 'bar', data: history,
      itemStyle: { color: css('--series-3'), borderRadius: [4, 4, 0, 0] },
      barCategoryGap: '30%',
      markLine: {
        symbol: 'none', silent: true,
        label: { formatter: `среднее ${mean.toFixed(0)}`,
          color: css('--text-muted'), fontSize: 11 },
        lineStyle: { color: css('--text-muted'), type: 'dashed', width: 1 },
        data: [{ yAxis: mean }],
      },
    }],
  }, true);
}

async function loadForecast() {
  renderForecast(await fetchJson('/api/forecast', { horizon_periods: 4 }));
}

// ============================================================================
// Вкладка: качество данных
// ============================================================================

function renderQuality(data) {
  const container = document.getElementById('quality');
  const items = data.anomalies.map(a => `
    <div class="quality-item">
      <div class="quality-count" data-anomaly="${escapeHtml(a.code)}">${a.count}</div>
      <div class="quality-text">
        ${escapeHtml(a.label)}
        <div class="quality-hint">${escapeHtml(a.hint)}</div>
      </div>
    </div>`).join('');

  container.innerHTML = `
    <div class="verdict">${escapeHtml(data.verdict)}</div>
    <div class="stat-row" style="border-top:none; padding-top:0; margin-bottom:12px;">
      <div class="stat-item">
        <div class="stat-label">Надёжных метрик</div>
        <div class="stat-value">${data.trustworthy_pct}%</div>
      </div>
      <div class="stat-item">
        <div class="stat-label">Даты заполнены</div>
        <div class="stat-value">${data.declared_coverage_pct}%</div>
      </div>
      <div class="stat-item">
        <div class="stat-label">Всего задач</div>
        <div class="stat-value">${data.total_tickets}</div>
      </div>
    </div>
    <div class="section-title">Обнаруженные проблемы</div>
    <p class="section-note">Нажмите на число, чтобы посмотреть затронутые задачи.</p>
    ${items || '<div class="quality-hint">Проблем не обнаружено.</div>'}`;

  // источник итогового значения времени работы
  const chart = ensureChart('chart-sources');
  const sources = Object.entries(data.by_source || {});
  if (!sources.length) return emptyChart(chart, 'Нет данных');

  const slots = { declared: 3, system: 1, reconciled: 7, inferred: 4, none: 8 };
  chart.setOption({
    ...baseOption(),
    legend: { show: false },
    tooltip: { ...baseOption().tooltip, trigger: 'item',
      formatter: item => `${item.name}: <b>${item.value}</b> задач (${item.percent}%)` },
    series: [{
      type: 'pie', radius: ['45%', '72%'], center: ['50%', '55%'],
      itemStyle: { borderColor: css('--surface-1'), borderWidth: 2 },
      label: { color: css('--text-secondary'), fontSize: 12,
        formatter: item => `${item.name}\n${item.value}` },
      data: sources.map(([source, count]) => ({
        name: SOURCE_LABEL[source] || source,
        value: count,
        itemStyle: { color: css(`--series-${slots[source] || 5}`) },
      })),
    }],
  }, true);
}

async function loadQuality() {
  const data = await fetch('/api/quality').then(r => r.json());
  renderQuality(data);
  fillAnomalyFilter(data.anomalies);
}

// ============================================================================
// Вкладка: задачи
// ============================================================================

function ticketQueryParams() {
  return {
    limit: PAGE_SIZE,
    offset: ticketPage * PAGE_SIZE,
    sort: document.getElementById('t-sort').value,
    order: document.getElementById('t-order').value,
    search: document.getElementById('t-search').value.trim(),
    anomaly: document.getElementById('t-anomaly').value,
    only_open: document.getElementById('t-open').checked ? 'true' : '',
  };
}

function confidencePill(level) {
  if (!level) return '';
  return `<span class="pill ${level}">${CONFIDENCE_LABEL[level] || level}</span>`;
}

function renderTickets(data) {
  const container = document.getElementById('tickets-table');
  document.getElementById('tickets-count').textContent =
    `${data.total} ${plural(data.total, 'задача', 'задачи', 'задач')}`;

  if (!data.items.length) {
    container.innerHTML = '<div class="muted">Ничего не найдено.</div>';
  } else {
    container.innerHTML = `
      <table>
        <thead><tr>
          <th>Задача</th><th>Название</th><th>Тип</th><th>Статус</th><th>Исполнитель</th>
          <th class="num">Цикл</th><th class="num">Всего</th><th class="num">Блок</th>
          <th class="num">Эфф.</th><th>Данные</th>
        </tr></thead>
        <tbody>${data.items.map(t => `
          <tr>
            <td><span class="key-link" data-ticket="${escapeHtml(t.key)}">${escapeHtml(t.key)}</span></td>
            <td title="${escapeHtml(t.summary || '')}">${
              escapeHtml((t.summary || '').slice(0, 46))}${(t.summary || '').length > 46 ? '…' : ''}</td>
            <td>${escapeHtml(t.type)}</td>
            <td>${escapeHtml(t.status || '—')}</td>
            <td>${escapeHtml(t.assignee || '—')}</td>
            <td class="num">${hours(t.cycle_s)}</td>
            <td class="num">${hours(t.lead_s)}</td>
            <td class="num">${t.blocked_s ? hours(t.blocked_s) : '—'}</td>
            <td class="num">${percent(t.flow_efficiency)}</td>
            <td>${confidencePill(t.confidence)}${
              t.anomalies.length ? ` <span class="pill over">${t.anomalies.length}</span>` : ''}</td>
          </tr>`).join('')}</tbody>
      </table>`;
  }

  const from = data.total ? data.offset + 1 : 0;
  const to = Math.min(data.offset + data.limit, data.total);
  document.getElementById('t-page').textContent = `${from}–${to} из ${data.total}`;
  document.getElementById('t-prev').disabled = ticketPage === 0;
  document.getElementById('t-next').disabled = to >= data.total;
}

async function loadTickets() {
  renderTickets(await fetchJson('/api/tickets', ticketQueryParams()));
}

function fillAnomalyFilter(anomalies) {
  const select = document.getElementById('t-anomaly');
  const current = select.value;
  select.innerHTML = '<option value="">Все задачи</option>' +
    anomalies.map(a =>
      `<option value="${escapeHtml(a.code)}">${escapeHtml(a.label)} (${a.count})</option>`
    ).join('');
  select.value = current;
}

// ============================================================================
// Модальные окна: исходные данные
// ============================================================================

function openModal(title, html) {
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-body').innerHTML = html;
  document.getElementById('modal').hidden = false;
  document.body.style.overflow = 'hidden';
}
function closeModal() {
  document.getElementById('modal').hidden = true;
  document.body.style.overflow = '';
}
function modalLoading(title) {
  openModal(title, '<div class="loading">загрузка…</div>');
}

/** Таблица задач внутри модального окна. */
function ticketTableHtml(items, columns) {
  if (!items.length) return '<div class="muted">Нет записей.</div>';
  return `
    <table>
      <thead><tr>${columns.map(c =>
        `<th class="${c.num ? 'num' : ''}">${escapeHtml(c.title)}</th>`).join('')}</tr></thead>
      <tbody>${items.map(item => `
        <tr>${columns.map(c =>
          `<td class="${c.num ? 'num' : ''}">${c.render(item)}</td>`).join('')}</tr>`).join('')}
      </tbody>
    </table>`;
}

const KEY_COLUMN = {
  title: 'Задача',
  render: item => `<span class="key-link" data-ticket="${escapeHtml(item.key)}">${
    escapeHtml(item.key)}</span>`,
};

/** Задачи из выбранного столбца гистограммы времени цикла. */
async function openBucketTickets(bucket) {
  const range = `${hours(bucket.from)} — ${bucket.to ? hours(bucket.to) : '∞'}`;
  modalLoading(`Задачи со временем цикла ${range}`);
  try {
    const data = await fetchJson('/api/tickets', {
      limit: 200, sort: 'cycle_time', order: 'asc',
      min_cycle_s: bucket.from,
      max_cycle_s: bucket.to == null ? '' : bucket.to,
    });
    openModal(`Задачи со временем цикла ${range} — ${data.total} шт.`,
      ticketTableHtml(data.items, [
        KEY_COLUMN,
        { title: 'Название', render: i => escapeHtml((i.summary || '').slice(0, 60)) },
        { title: 'Тип', render: i => escapeHtml(i.type) },
        { title: 'Цикл', num: true, render: i => hours(i.cycle_s) },
        { title: 'В работе', num: true, render: i => hours(i.touch_s) },
        { title: 'Ожидание', num: true, render: i => hours(i.queue_s) },
      ]));
  } catch (error) {
    openModal('Ошибка', `<div class="error">${escapeHtml(error.message)}</div>`);
  }
}

/** Интервалы конкретной фазы. */
async function openPhaseIntervals(phase) {
  const label = PHASE_LABEL[phase] || phase;
  modalLoading(`Интервалы: ${label}`);
  try {
    const data = await fetchJson('/api/phase-intervals', { phase });
    openModal(`Интервалы в фазе «${label}» — ${data.items.length} шт. (самые длительные)`,
      ticketTableHtml(data.items, [
        KEY_COLUMN,
        { title: 'Статус', render: i => escapeHtml(i.status) },
        { title: 'Исполнитель', render: i => escapeHtml(i.assignee || '—') },
        { title: 'Начало', render: i => formatDate(i.started_at) },
        { title: 'Конец', render: i => formatDate(i.ended_at) },
        { title: 'Рабочее', num: true, render: i => hours(i.business_s) },
        { title: 'Календарное', num: true, render: i => hours(i.calendar_s) },
      ]));
  } catch (error) {
    openModal('Ошибка', `<div class="error">${escapeHtml(error.message)}</div>`);
  }
}

/** Задачи с конкретной аномалией. */
async function openAnomalyTickets(code, label) {
  modalLoading(`Задачи: ${label}`);
  try {
    const data = await fetchJson('/api/tickets', { limit: 200, anomaly: code });
    openModal(`${label} — ${data.total} ${plural(data.total, 'задача', 'задачи', 'задач')}`,
      ticketTableHtml(data.items, [
        KEY_COLUMN,
        { title: 'Название', render: i => escapeHtml((i.summary || '').slice(0, 50)) },
        { title: 'Статус', render: i => escapeHtml(i.status || '—') },
        { title: 'Цикл', num: true, render: i => hours(i.cycle_s) },
        { title: 'Достоверность', render: i => confidencePill(i.confidence) },
      ]));
  } catch (error) {
    openModal('Ошибка', `<div class="error">${escapeHtml(error.message)}</div>`);
  }
}

/** Общие данные графика — открывается кнопкой «Данные». */
async function openChartData(kind) {
  const handlers = {
    cfd: async () => {
      const data = await fetchJson('/api/cfd');
      const rows = data.days.map((day, index) => ({
        day,
        values: data.series.map(s => ({ phase: s.phase, value: s.values[index] })),
      })).reverse();
      return {
        title: `Накопительная диаграмма: ${data.days.length} дней`,
        html: `
          <p class="section-note">Сколько задач находилось в каждой фазе на конец дня.</p>
          <table>
            <thead><tr><th>Дата</th>${data.series.map(s =>
              `<th class="num">${escapeHtml(PHASE_LABEL[s.phase] || s.phase)}</th>`).join('')}</tr></thead>
            <tbody>${rows.map(r => `
              <tr><td>${formatDay(r.day)}</td>${r.values.map(v =>
                `<td class="num">${v.value}</td>`).join('')}</tr>`).join('')}</tbody>
          </table>`,
      };
    },
    arrival: async () => {
      const data = await fetchJson('/api/arrival-throughput', { granularity: 'week' });
      const rows = data.periods.map((period, i) => ({
        period, arrived: data.arrived[i], completed: data.completed[i],
        net: data.net_per_period[i], backlog: data.backlog_delta[i],
      })).reverse();
      return {
        title: `Поступление и закрытие: ${data.periods.length} недель`,
        html: `
          <p class="section-note">Разница между поступлением и закрытием накапливается
             в столбце «очередь».</p>
          <table>
            <thead><tr>
              <th>Неделя</th><th class="num">Поступило</th><th class="num">Закрыто</th>
              <th class="num">Разница</th><th class="num">Очередь</th>
            </tr></thead>
            <tbody>${rows.map(r => `
              <tr>
                <td>${formatDay(r.period)}</td>
                <td class="num">${r.arrived}</td>
                <td class="num">${r.completed}</td>
                <td class="num" style="color:${r.net > 0 ? css('--status-critical') : css('--status-good')}">
                  ${r.net > 0 ? '+' : ''}${r.net}</td>
                <td class="num">${r.backlog > 0 ? '+' : ''}${r.backlog}</td>
              </tr>`).join('')}</tbody>
          </table>`,
      };
    },
    cycle: async () => {
      const data = await fetchJson('/api/tickets',
        { limit: 300, sort: 'cycle_time', order: 'desc' });
      return {
        title: `Завершённые задачи: ${data.total}`,
        html: `
          <p class="section-note">Каждая строка — задача, из которых сложилось
             распределение. Нажмите на ключ, чтобы увидеть её историю.</p>` +
          ticketTableHtml(data.items, [
            KEY_COLUMN,
            { title: 'Название', render: i => escapeHtml((i.summary || '').slice(0, 45)) },
            { title: 'Тип', render: i => escapeHtml(i.type) },
            { title: 'Цикл', num: true, render: i => hours(i.cycle_s) },
            { title: 'Всего', num: true, render: i => hours(i.lead_s) },
            { title: 'В работе', num: true, render: i => hours(i.touch_s) },
            { title: 'Ожидание', num: true, render: i => hours(i.queue_s) },
            { title: 'Эфф.', num: true, render: i => percent(i.flow_efficiency) },
          ]),
      };
    },
    phases: async () => {
      const data = await fetchJson('/api/phase-intervals');
      return {
        title: `Интервалы по фазам: ${data.items.length} самых длительных`,
        html: `
          <p class="section-note">Каждая строка — период, в течение которого задача
             находилась в одном статусе у одного исполнителя.</p>` +
          ticketTableHtml(data.items, [
            KEY_COLUMN,
            { title: 'Фаза', render: i => escapeHtml(PHASE_LABEL[i.phase] || i.phase) },
            { title: 'Статус', render: i => escapeHtml(i.status) },
            { title: 'Исполнитель', render: i => escapeHtml(i.assignee || '—') },
            { title: 'Начало', render: i => formatDate(i.started_at) },
            { title: 'Рабочее', num: true, render: i => hours(i.business_s) },
            { title: 'Календарное', num: true, render: i => hours(i.calendar_s) },
          ]),
      };
    },
    wip: async () => {
      const data = await fetchJson('/api/aging-wip');
      return {
        title: `Незавершённые задачи: ${data.total}`,
        html: `
          <p class="section-note">Возраст — сколько задача находится в текущем статусе.</p>` +
          ticketTableHtml(data.items, [
            KEY_COLUMN,
            { title: 'Название', render: i => escapeHtml((i.summary || '').slice(0, 45)) },
            { title: 'Статус', render: i => escapeHtml(i.status) },
            { title: 'Исполнитель', render: i => escapeHtml(i.assignee || '—') },
            { title: 'Возраст', num: true, render: i => hours(i.age_s) },
            { title: '', render: i =>
              (i.is_blocked ? '<span class="pill blocked">блок</span> ' : '') +
              (i.over_p85 ? '<span class="pill over">долго</span>' : '') },
          ]),
      };
    },
    people: async () => {
      const data = await fetchJson('/api/people');
      return {
        title: 'Нагрузка по людям',
        html: `
          <p class="section-note">Время владения задачами за выбранный период.
             Это не мера усилий: задача может стоять заблокированной.</p>` +
          ticketTableHtml(data.people, [
            { title: 'Человек', render: p => escapeHtml(p.person) },
            { title: 'Владел', num: true, render: p => hours(p.owned_s) },
            { title: 'В работе', num: true, render: p => hours(p.touch_s) },
            { title: 'Блокировки', num: true, render: p => hours(p.blocked_s) },
            { title: 'Закрыл', num: true, render: p => p.completed },
            { title: 'Задач/день', num: true, render: p => p.avg_active_tickets.toFixed(1) },
            { title: 'Максимум', num: true, render: p => p.max_active_tickets },
            { title: 'Дней', num: true, render: p => p.active_days },
          ]),
      };
    },
    throughput: async () => {
      const data = await fetchJson('/api/forecast', { horizon_periods: 4 });
      const history = data.throughput_history || [];
      const mean = history.length
        ? history.reduce((a, b) => a + b, 0) / history.length : 0;
      return {
        title: 'Выборка для прогноза',
        html: `
          <p class="section-note">Прогноз строится случайным выбором недель из этой
             выборки (10 000 прогонов). Чем ровнее темп, тем уже разброс.</p>
          <table>
            <thead><tr><th>Неделя</th><th class="num">Закрыто задач</th>
              <th class="num">Отклонение от среднего</th></tr></thead>
            <tbody>${history.map((value, i) => `
              <tr>
                <td>${i + 1}</td>
                <td class="num">${value}</td>
                <td class="num">${value - mean > 0 ? '+' : ''}${(value - mean).toFixed(1)}</td>
              </tr>`).join('')}</tbody>
          </table>
          <div class="section-title">Итог</div>
          <div class="reconcile">
            <div class="reconcile-row">
              <span class="reconcile-label">Среднее за неделю</span>
              <span class="reconcile-value">${mean.toFixed(1)} задач</span>
            </div>
            <div class="reconcile-row">
              <span class="reconcile-label">Незавершённых сейчас</span>
              <span class="reconcile-value">${data.backlog}</span>
            </div>
          </div>`,
      };
    },
  };

  const handler = handlers[kind];
  if (!handler) return;
  modalLoading('Данные');
  try {
    const { title, html } = await handler();
    openModal(title, html);
  } catch (error) {
    openModal('Ошибка', `<div class="error">${escapeHtml(error.message)}</div>`);
  }
}

// ============================================================================
// Карточка задачи: полная история
// ============================================================================

function timelineHtml(intervals) {
  const durations = intervals.map(i => i.business_s || 0);
  const maxDuration = Math.max(...durations, 1);
  const instant = intervals.filter(i => !i.business_s).length;

  const rows = intervals.map(interval => {
    const seconds = interval.business_s || 0;
    // мгновенные переходы показываем засечкой, а не полосой:
    // иначе минимальная ширина выглядит как настоящая длительность
    const width = seconds ? Math.max(3, 100 * seconds / maxDuration) : 0;
    const color = interval.is_blocked
      ? css('--status-critical') : phaseColor(interval.phase);
    const bar = seconds
      ? `<span class="timeline-bar" style="width:${width}%; background:${color}"></span>`
      : `<span class="timeline-tick" style="background:${color}"></span>`;
    return `
      <div class="timeline-row" title="${escapeHtml(formatDate(interval.started_at))} — ${
        escapeHtml(formatDate(interval.ended_at))}">
        <span class="timeline-status">${escapeHtml(interval.status)}${
          interval.assignee ? ` · ${escapeHtml(interval.assignee)}` : ''}</span>
        <span class="timeline-track">${bar}</span>
        <span class="timeline-dur">${seconds ? hours(seconds) : 'мгновенно'}</span>
      </div>`;
  }).join('');

  const note = instant
    ? `<p class="section-note">${instant} ${
        plural(instant, 'переход прошёл', 'перехода прошли', 'переходов прошли')
      } мгновенно — задачу двигали по доске задним числом.</p>`
    : '';

  return `<div class="timeline">${rows}</div>${note}`;
}

function reconcileHtml(facts) {
  if (!facts.length) return '<div class="muted">Нет данных о согласовании.</div>';

  return facts.map(fact => {
    const label = fact.boundary === 'work_start' ? 'Начало работы' : 'Окончание работы';
    const chosen = SOURCE_LABEL[fact.chosen_source] || fact.chosen_source;
    const rows = [
      { label: 'По истории статусов', value: formatDate(fact.system_at),
        chosen: fact.chosen_source === 'system' },
      { label: 'Заявлено вручную', value: formatDate(fact.declared_at),
        chosen: fact.chosen_source === 'declared' },
      { label: 'Принято за истину', value: formatDate(fact.effective_at), result: true },
    ];
    return `
      <div class="reconcile">
        <div style="font-weight:600; margin-bottom:6px">${label}</div>
        ${rows.map(r => `
          <div class="reconcile-row">
            <span class="reconcile-label">${r.label}</span>
            <span class="reconcile-value ${r.chosen || r.result ? 'reconcile-chosen' : ''}">${
              escapeHtml(r.value)}</span>
            ${r.chosen ? '<span class="pill">выбрано</span>' : ''}
          </div>`).join('')}
        <div class="reconcile-row">
          <span class="reconcile-label">Источник и достоверность</span>
          <span class="reconcile-value">${escapeHtml(chosen)} ·
            ${confidencePill(fact.confidence)}</span>
        </div>
        ${fact.discrepancy_business_s ? `
          <div class="reconcile-row">
            <span class="reconcile-label">Расхождение</span>
            <span class="reconcile-value">${hours(fact.discrepancy_business_s)}</span>
          </div>` : ''}
        ${fact.anomalies.length ? `
          <div class="reconcile-row">
            <span class="reconcile-label">Замечания</span>
            <span>${fact.anomalies.map(a =>
              `<span class="pill over">${escapeHtml(a)}</span>`).join(' ')}</span>
          </div>` : ''}
      </div>`;
  }).join('');
}

function eventsHtml(events) {
  const KIND_LABEL = {
    created: 'создана', status_change: 'смена статуса',
    assignee_change: 'смена исполнителя', field_change: 'изменение поля',
    comment: 'комментарий', resolved: 'решена', reopened: 'переоткрыта',
    link_change: 'связь', flag_change: 'флаг',
  };
  return `
    <table>
      <thead><tr>
        <th>Когда</th><th>Что</th><th>Было</th><th>Стало</th><th>Кто</th>
      </tr></thead>
      <tbody>${events.map(e => `
        <tr>
          <td>${formatDate(e.occurred_at)}</td>
          <td>${escapeHtml(KIND_LABEL[e.kind] || e.kind)}</td>
          <td class="muted">${escapeHtml(e.old_value || '—')}</td>
          <td>${escapeHtml(e.new_value || '—')}</td>
          <td>${escapeHtml(e.actor || '—')}</td>
        </tr>`).join('')}</tbody>
    </table>`;
}

async function openTicket(key) {
  modalLoading(key);
  try {
    const t = await fetch(`/api/tickets/${encodeURIComponent(key)}`).then(r => {
      if (!r.ok) throw new Error(`задача ${key} не найдена`);
      return r.json();
    });

    const m = t.metrics || {};
    const facts = t.timeline_facts || [];

    openModal(`${t.key} — ${t.summary || ''}`, `
      <dl class="detail-grid">
        <dt>Тип</dt><dd>${escapeHtml(t.type)}${t.is_subtask ? ' (подзадача)' : ''}</dd>
        <dt>Приоритет</dt><dd>${escapeHtml(t.priority || '—')}</dd>
        <dt>Статус</dt><dd>${escapeHtml(t.status || '—')}</dd>
        <dt>Исполнитель</dt><dd>${escapeHtml(t.assignee || '—')}</dd>
        <dt>Автор</dt><dd>${escapeHtml(t.reporter || '—')}</dd>
        <dt>Создана</dt><dd>${formatDate(t.created_at)}</dd>
        <dt>Закрыта</dt><dd>${formatDate(t.closed_at)}</dd>
        ${t.components.length ? `<dt>Компоненты</dt><dd>${
          t.components.map(c => escapeHtml(c)).join(', ')}</dd>` : ''}
      </dl>

      <div class="section-title">Метрики</div>
      <div class="stat-row" style="border-top:none; padding-top:0">
        <div class="stat-item">
          <div class="stat-label">Время цикла</div>
          <div class="stat-value">${hours(m.cycle_business_s)}</div>
        </div>
        <div class="stat-item">
          <div class="stat-label">Общее время</div>
          <div class="stat-value">${hours(m.lead_business_s)}</div>
        </div>
        <div class="stat-item">
          <div class="stat-label">В работе</div>
          <div class="stat-value">${hours(m.touch_s)}</div>
        </div>
        <div class="stat-item">
          <div class="stat-label">Ожидание</div>
          <div class="stat-value">${hours(m.queue_s)}</div>
        </div>
        <div class="stat-item">
          <div class="stat-label">Блокировки</div>
          <div class="stat-value">${hours(m.blocked_s)}</div>
        </div>
        <div class="stat-item">
          <div class="stat-label">Эффективность</div>
          <div class="stat-value">${percent(m.flow_efficiency)}</div>
        </div>
      </div>

      <div class="section-title">Как ядро определило время работы</div>
      <p class="section-note">История статусов и даты, проставленные вручную,
         хранятся отдельно. Ядро выбирает по политике и помечает расхождения.</p>
      ${reconcileHtml(facts)}

      <div class="section-title">Путь по доске</div>
      <p class="section-note">Длина полосы — рабочее время в статусе.
         ${t.intervals.length} ${plural(t.intervals.length, 'интервал', 'интервала', 'интервалов')}.</p>
      ${timelineHtml(t.intervals)}

      <div class="section-title">История изменений</div>
      <p class="section-note">Сырой журнал событий, как его отдал источник —
         ${t.events.length} ${plural(t.events.length, 'запись', 'записи', 'записей')}.</p>
      <div class="scroll">${eventsHtml(t.events)}</div>

      ${t.declared_dates.length ? `
        <div class="section-title">Заявленные вручную даты</div>
        <table>
          <thead><tr><th>Граница</th><th>Значение</th><th>Точность</th><th>Поле</th></tr></thead>
          <tbody>${t.declared_dates.map(d => `
            <tr>
              <td>${d.boundary === 'work_start' ? 'начало' : 'окончание'}</td>
              <td>${formatDate(d.value_at)}</td>
              <td>${d.precision === 'day' ? 'только дата' : 'дата и время'}</td>
              <td class="muted">${escapeHtml(d.source_field || '—')}</td>
            </tr>`).join('')}</tbody>
        </table>` : ''}

      ${t.comments.length ? `
        <div class="section-title">Комментарии (${t.comments.length})</div>
        <div class="scroll">${t.comments.map(c => `
          <div style="padding:8px 0; border-bottom:1px solid var(--border)">
            <div class="muted" style="font-size:12px">${
              escapeHtml(c.author || '—')} · ${formatDate(c.created_at)}</div>
            <div>${escapeHtml(c.body)}</div>
          </div>`).join('')}</div>` : ''}

      ${t.links.length ? `
        <div class="section-title">Связи</div>
        <div>${t.links.map(l =>
          `<span class="pill">${escapeHtml(l.type)}: <span class="key-link" data-ticket="${
            escapeHtml(l.to)}">${escapeHtml(l.to)}</span></span>`).join(' ')}</div>` : ''}
    `);
  } catch (error) {
    openModal('Ошибка', `<div class="error">${escapeHtml(error.message)}</div>`);
  }
}

// ============================================================================
// Разбор
// ============================================================================

async function requestExplanation() {
  const button = document.getElementById('explain-btn');
  const container = document.getElementById('explain');
  button.disabled = true;
  button.textContent = 'Готовлю разбор…';
  container.innerHTML = '<div class="loading">модель анализирует метрики…</div>';

  try {
    const response = await fetch(`/api/explain?${filterParams()}`, { method: 'POST' });
    const data = await response.json();
    if (!data.available) {
      container.innerHTML = `<div class="explain-unavailable">${
        escapeHtml(data.reason || 'Разбор недоступен.')}</div>`;
    } else {
      container.innerHTML = `<div class="explain-text">${
        data.text.split(/\n\n+/).map(p => `<p>${escapeHtml(p)}</p>`).join('')}</div>`;
    }
  } catch (error) {
    container.innerHTML = `<div class="explain-unavailable">Не удалось получить разбор: ${
      escapeHtml(error.message)}</div>`;
  } finally {
    button.disabled = false;
    button.textContent = 'Обновить разбор';
  }
}

// ============================================================================
// Вкладки и загрузка
// ============================================================================

const TAB_LOADERS = {
  overview: loadOverview,
  flow: loadFlow,
  cycle: loadCycle,
  phases: loadPhases,
  wip: loadWip,
  people: loadPeople,
  forecast: loadForecast,
  quality: loadQuality,
  tickets: loadTickets,
};

async function showTab(name, { force = false } = {}) {
  if (!TAB_LOADERS[name]) name = 'overview';
  currentTab = name;

  for (const tab of document.querySelectorAll('.tab')) {
    tab.classList.toggle('active', tab.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll('.panel')) {
    panel.classList.toggle('active', panel.dataset.panel === name);
  }
  if (location.hash.slice(1) !== name) location.hash = name;

  if (loaded[name] && !force) {
    // графики скрытой вкладки не знают своего размера — пересчитываем
    requestAnimationFrame(() => {
      for (const chart of Object.values(charts)) chart.resize();
    });
    return;
  }

  try {
    await TAB_LOADERS[name]();
    loaded[name] = true;
    requestAnimationFrame(() => {
      for (const chart of Object.values(charts)) chart.resize();
    });
  } catch (error) {
    console.error(error);
    const panel = document.querySelector(`.panel[data-panel="${name}"]`);
    if (panel) {
      const holder = panel.querySelector('.card');
      if (holder) {
        holder.insertAdjacentHTML('beforeend',
          `<div class="error">Не удалось загрузить: ${escapeHtml(error.message)}</div>`);
      }
    }
  }
}

/** Смена фильтров обесценивает все загруженные вкладки. */
function invalidateAll() {
  for (const key of Object.keys(loaded)) delete loaded[key];
  ticketPage = 0;
  showTab(currentTab, { force: true });
}

async function loadFilterOptions() {
  const options = await fetch('/api/filters').then(r => r.json());
  const fill = (id, values) => {
    const select = document.getElementById(id);
    for (const value of values) {
      const option = document.createElement('option');
      option.value = option.textContent = value;
      select.appendChild(option);
    }
  };
  fill('f-type', options.issue_types);
  fill('f-priority', options.priorities);
  fill('f-component', options.components);
  if (options.period.earliest) {
    document.getElementById('period').textContent =
      `${formatDay(options.period.earliest)} — ${formatDay(options.period.latest)}`;
  }
}

function setUnit(next) {
  unit = next;
  document.getElementById('u-business').classList.toggle('active', next === 'business');
  document.getElementById('u-calendar').classList.toggle('active', next === 'calendar');
  invalidateAll();
}

// ============================================================================
// События
// ============================================================================

document.addEventListener('DOMContentLoaded', async () => {
  // вкладки
  for (const tab of document.querySelectorAll('.tab')) {
    tab.addEventListener('click', () => showTab(tab.dataset.tab));
  }
  window.addEventListener('hashchange', () => showTab(location.hash.slice(1)));

  // фильтры
  for (const id of ['f-from', 'f-to', 'f-type', 'f-priority', 'f-component', 'f-confidence']) {
    document.getElementById(id).addEventListener('change', invalidateAll);
  }
  document.getElementById('u-business').addEventListener('click', () => setUnit('business'));
  document.getElementById('u-calendar').addEventListener('click', () => setUnit('calendar'));

  // тема
  document.getElementById('theme-toggle').addEventListener('click', () => {
    const current = document.documentElement.getAttribute('data-theme');
    const isDark = current === 'dark' ||
      (!current && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark');
    invalidateAll();
  });

  // разбор
  document.getElementById('explain-btn').addEventListener('click', requestExplanation);

  // таблица задач
  let searchTimer;
  document.getElementById('t-search').addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { ticketPage = 0; loadTickets(); }, 300);
  });
  for (const id of ['t-sort', 't-order', 't-anomaly', 't-open']) {
    document.getElementById(id).addEventListener('change', () => {
      ticketPage = 0;
      loadTickets();
    });
  }
  document.getElementById('t-prev').addEventListener('click', () => {
    if (ticketPage > 0) { ticketPage--; loadTickets(); }
  });
  document.getElementById('t-next').addEventListener('click', () => {
    ticketPage++; loadTickets();
  });

  // модальное окно
  document.getElementById('modal-close').addEventListener('click', closeModal);
  document.getElementById('modal').addEventListener('click', event => {
    if (event.target.id === 'modal') closeModal();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !document.getElementById('modal').hidden) closeModal();
  });

  // делегирование: ключи задач, кнопки «Данные», числа аномалий
  document.addEventListener('click', event => {
    const ticket = event.target.closest('[data-ticket]');
    if (ticket) return openTicket(ticket.dataset.ticket);

    const dataButton = event.target.closest('[data-data-for]');
    if (dataButton) return openChartData(dataButton.dataset.dataFor);

    const anomaly = event.target.closest('[data-anomaly]');
    if (anomaly) {
      const label = anomaly.parentElement.querySelector('.quality-text');
      return openAnomalyTickets(anomaly.dataset.anomaly,
        label ? label.firstChild.textContent.trim() : 'Задачи');
    }
  });

  window.addEventListener('resize', () => {
    for (const chart of Object.values(charts)) chart.resize();
  });

  await loadFilterOptions();
  await showTab(location.hash.slice(1) || 'overview');

  // список аномалий нужен для фильтра на вкладке задач
  fetch('/api/quality').then(r => r.json())
    .then(data => fillAnomalyFilter(data.anomalies))
    .catch(() => {});
});
