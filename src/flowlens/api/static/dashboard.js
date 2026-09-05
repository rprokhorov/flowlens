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
// Возраст в очереди идёт по календарю: задача ждёт и в выходные. Пропускать его
// через hours() нельзя — там рабочий день в 9 часов, и 28 суток превращаются в 75.
function calendarDays(seconds) {
  if (seconds == null) return '—';
  const days = seconds / 86400;
  if (days < 1) return `${Math.round(seconds / 3600)} ч`;
  return days < 10 ? `${days.toFixed(1)} дн` : `${Math.round(days)} дн`;
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
  if (get('f-completion') !== 'terminal') params.set('completion', get('f-completion'));
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
      note: `85% — ${hours(summary.p85_cycle_s)}, 95% — ${hours(summary.p95_cycle_s)}` },
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

function renderNetFlow(data) {
  const chart = ensureChart('chart-net-flow');
  if (!chart) return;
  if (!data.periods.length) return emptyChart(chart, 'Нет данных за период');

  const cumulative = data.backlog_delta || [];
  chart.setOption({
    ...baseOption(),
    tooltip: {
      ...baseOption().tooltip, trigger: 'axis',
      formatter: params => {
        const index = params[0].dataIndex;
        const net = data.net_per_period[index];
        return `${formatDay(data.periods[index])}<br>`
          + `за период: ${net > 0 ? '+' : ''}${net}<br>`
          + `накопленно: ${params[0].value > 0 ? '+' : ''}${params[0].value}`;
      },
    },
    xAxis: { type: 'category', data: data.periods.map(formatDay), ...axisStyle() },
    yAxis: { type: 'value', ...axisStyle() },
    series: [{
      type: 'line', smooth: false, symbolSize: 4,
      data: cumulative,
      lineStyle: { color: css('--series-8'), width: 2 },
      itemStyle: { color: css('--series-8') },
      areaStyle: { opacity: 0.1, color: css('--series-8') },
      markLine: {
        silent: true, symbol: 'none',
        data: [{ yAxis: 0, lineStyle: { color: css('--border') } }],
      },
    }],
  }, true);
}

function renderBacklog(data) {
  const chart = ensureChart('chart-backlog');
  const hint = document.getElementById('backlog-hint');
  if (!chart) return;

  if (hint) {
    const stale = data.histogram.slice(2).reduce((sum, bucket) => sum + bucket.count, 0);
    hint.textContent = `В очереди ${data.size} `
      + `${plural(data.size, 'задача', 'задачи', 'задач')}, половина ждёт дольше `
      + `${calendarDays(data.p50_age_s)}.`
      + (stale ? ` Старше трёх месяцев — ${stale}: такие задачи обычно не делаются `
        + 'никогда и только зашумляют приоритизацию.' : '');
  }
  if (!data.size) return emptyChart(chart, 'Очередь пуста');

  chart.setOption({
    ...baseOption(),
    tooltip: {
      ...baseOption().tooltip, trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: params => `${params[0].name}: ${params[0].value} `
        + `${plural(params[0].value, 'задача', 'задачи', 'задач')}`,
    },
    xAxis: { type: 'category', data: data.histogram.map(bucket => bucket.label), ...axisStyle() },
    yAxis: { type: 'value', ...axisStyle() },
    series: [{
      type: 'bar', barMaxWidth: 64,
      data: data.histogram.map((bucket, index) => ({
        value: bucket.count,
        itemStyle: { color: index >= 2 ? css('--status-warning') : css('--series-1') },
      })),
    }],
  }, true);

  const oldest = document.getElementById('backlog-oldest');
  if (oldest) {
    oldest.innerHTML = data.oldest.length ? `<table>
      <thead><tr><th>Задача</th><th>Тип</th><th class="num">Ждёт</th></tr></thead>
      <tbody>${data.oldest.map(item => `
        <tr>
          <td><span class="key-link" data-ticket="${escapeHtml(item.key)}">${escapeHtml(item.key)}</span></td>
          <td>${escapeHtml(item.type)}</td>
          <td class="num">${calendarDays(item.age_s)}</td>
        </tr>`).join('')}</tbody></table>` : '';
  }
}

async function loadFlow() {
  const [cfd, arrival, backlog] = await Promise.all([
    fetchJson('/api/cfd'),
    fetchJson('/api/arrival-throughput', { granularity: 'week' }),
    fetchJson('/api/backlog'),
  ]);
  renderCfd(cfd);
  renderArrival(arrival);
  renderNetFlow(arrival);
  renderBacklog(backlog);
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
          { name: `95% ${hours(p.p95)}`, xAxis: bucketIndex(data.histogram, p.p95) },
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
               `95-й перцентиль: ${hours(phase.p95_s)}<br>` +
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

function renderHiddenQueue(data) {
  const chart = ensureChart('chart-hidden');
  const hint = document.getElementById('hidden-hint');
  if (!chart) return;

  const phases = (data.by_phase || []).filter(item => item.total_s > 0);
  if (!phases.length) return emptyChart(chart, 'Нет завершённых интервалов за период');

  const worst = phases.reduce((a, b) => (a.waiting_s > b.waiting_s ? a : b));
  if (hint) {
    hint.textContent = worst.share >= 0.2
      ? `В статусе «${PHASE_LABEL[worst.phase] || worst.phase}» ${percent(worst.share)} времени — `
        + 'ожидание: задача уже там, но её ещё никто не взял. Это время считается работой '
        + 'и завышает эффективность потока.'
      : 'Задачи почти сразу попадают к исполнителю. Ожидание внутри активных статусов невелико.';
  }

  chart.setOption({
    ...baseOption(),
    legend: { ...baseOption().legend, data: ['Работали', 'Ждали исполнителя'] },
    tooltip: {
      ...baseOption().tooltip, trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: params => {
        const item = phases[params[0].dataIndex];
        return `<b>${PHASE_LABEL[item.phase] || item.phase}</b><br>`
          + `работали: ${hours(item.total_s - item.waiting_s)}<br>`
          + `ждали: ${hours(item.waiting_s)} (${percent(item.share)})`;
      },
    },
    xAxis: { type: 'value', ...axisStyle(),
      axisLabel: { ...axisStyle().axisLabel, formatter: value => hours(value) } },
    yAxis: { type: 'category', ...axisStyle(),
      data: phases.map(item => PHASE_LABEL[item.phase] || item.phase) },
    series: [
      { name: 'Работали', type: 'bar', stack: 'time', barMaxWidth: 34,
        data: phases.map(item => item.total_s - item.waiting_s),
        itemStyle: { color: css('--series-1') } },
      { name: 'Ждали исполнителя', type: 'bar', stack: 'time', barMaxWidth: 34,
        data: phases.map(item => item.waiting_s),
        itemStyle: { color: css('--status-warning') } },
    ],
  }, true);
}

function renderTransitions(data) {
  const chart = ensureChart('chart-transitions');
  const hint = document.getElementById('transitions-hint');
  if (!chart) return;

  if (!data.cells.length) return emptyChart(chart, 'Нет переходов за период');
  if (hint) {
    hint.textContent = `Возвраты — ${percent(data.backflow_rate)} всех переходов. `
      + 'Задача, дважды вернувшаяся из проверки, проходит ожидание трижды; '
      + 'в отчётах это выглядит как «долгое ревью», хотя причина — качество на входе.';
  }

  const statuses = [...new Set(data.cells.flatMap(cell => [cell.from, cell.to]))];
  const index = new Map(statuses.map((name, i) => [name, i]));
  const points = data.cells.map(cell => ({
    value: [index.get(cell.to), index.get(cell.from), cell.moves],
    is_backflow: cell.is_backflow,
  }));
  const maxMoves = Math.max(...data.cells.map(cell => cell.moves));

  chart.setOption({
    ...baseOption(),
    grid: { left: 8, right: 24, top: 40, bottom: 42, containLabel: true },
    tooltip: {
      ...baseOption().tooltip, trigger: 'item',
      formatter: params => `${escapeHtml(statuses[params.value[1]])} → `
        + `${escapeHtml(statuses[params.value[0]])}<br>переходов: ${params.value[2]}`
        + (params.data.is_backflow ? '<br><b>возврат</b>' : ''),
    },
    xAxis: { type: 'category', data: statuses, name: 'куда',
      nameLocation: 'middle', nameGap: 58,
      nameTextStyle: { color: css('--text-muted'), fontSize: 11 }, ...axisStyle(),
      splitLine: { show: true, lineStyle: { color: css('--grid'), type: 'dashed' } },
      axisLabel: { ...axisStyle().axisLabel, interval: 0, rotate: 20 } },
    yAxis: { type: 'category', data: statuses, name: 'откуда',
      nameTextStyle: { color: css('--text-muted'), fontSize: 11, align: 'right' }, ...axisStyle(),
      splitLine: { show: true, lineStyle: { color: css('--grid'), type: 'dashed' } } },
    series: [{
      type: 'scatter',
      symbolSize: point => 12 + 34 * Math.sqrt(point[2] / maxMoves),
      data: points,
      itemStyle: {
        color: params => params.data.is_backflow ? css('--status-critical') : css('--series-1'),
        opacity: 0.82,
      },
      label: {
        show: true, formatter: params => params.value[2],
        color: css('--text-primary'), fontSize: 10,
      },
    }],
  }, true);
}

async function loadPhases() {
  const [efficiency, hidden, transitions] = await Promise.all([
    fetchJson('/api/flow-efficiency'),
    fetchJson('/api/hidden-queue'),
    fetchJson('/api/transitions'),
  ]);
  renderPhases(efficiency);
  renderEfficiency(efficiency);
  renderHiddenQueue(hidden);
  renderTransitions(transitions);
}

// ============================================================================
// Вкладка: незавершённое
// ============================================================================

function renderAging(data) {
  const chart = ensureChart('chart-aging');
  const hint = document.getElementById('aging-hint');
  const body = document.querySelector('#table-aging tbody');

  const over95 = data.over_p95 != null ? data.over_p95 : null;
  hint.textContent =
    `${data.total} ${plural(data.total, 'задача', 'задачи', 'задач')} не завершено. ` +
    `Дольше 85-го перцентиля (${hours(data.reference.p85)}) висят ${data.over_p85}` +
    (over95 !== null
      ? `, дольше 95-го (${hours(data.reference.p95)}) — ${over95}.`
      : '.');

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
        lineStyle: { type: 'dashed', width: 1 },
        data: [
          // подписи разведены по высоте: на фоне длинного хвоста
          // линии перцентилей стоят вплотную и накладываются
          { name: '85%', xAxis: data.reference.p85,
            lineStyle: { color: css('--status-warning') },
            label: { formatter: '85%', position: 'insideEndTop',
              color: css('--status-warning'), fontSize: 11 } },
          ...(data.reference.p95 ? [{ name: '95%', xAxis: data.reference.p95,
            lineStyle: { color: css('--status-critical') },
            label: { formatter: '95%', position: 'insideEndBottom',
              color: css('--status-critical'), fontSize: 11 } }] : []),
        ],
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
          ${item.over_p95 ? '<span class="pill low">очень долго</span>'
            : item.over_p85 ? '<span class="pill over">долго</span>' : ''}</td>
    </tr>`).join('');
}

async function loadWip() {
  renderAging(await fetchJson('/api/aging-wip'));
}

// ============================================================================
// Вкладка: блокировки
// ============================================================================

function renderBlockerTiles(data) {
  const unknown = data.unknown_share || 0;
  const tiles = [
    { label: 'Задач блокировалось', value: percent(data.blocked_rate),
      note: `${data.blocked_tickets} из ${data.tickets}` },
    { label: 'Потеряно времени', value: hours(data.lost_business_s),
      note: `${data.episodes} ${plural(data.episodes, 'эпизод', 'эпизода', 'эпизодов')}` },
    { label: 'Заблокировано сейчас', value: data.current.length,
      note: data.current.length ? 'требует внимания' : 'ничего не висит',
      cls: data.current.length ? 'warning' : 'good' },
    { label: 'Без указанной причины', value: percent(unknown),
      note: unknown > 0.3 ? 'разбор опирается на догадки' : 'причины заполняются',
      cls: unknown > 0.3 ? 'warning' : 'good' },
  ];
  document.getElementById('blocker-tiles').innerHTML = tiles.map(t => `
    <div class="tile ${t.cls || ''}">
      <div class="label">${t.label}</div>
      <div class="value">${t.value}</div>
      <div class="note">${t.note || ''}</div>
    </div>`).join('');
}

function renderBlockerPareto(data) {
  const chart = ensureChart('chart-blockers');
  if (!chart) return;
  if (!data.pareto.length) return emptyChart(chart, 'Блокировок за период не было');

  const labels = data.pareto.map(r => r.label);
  const losses = data.pareto.map(r => r.business_s);
  const cumulative = data.pareto.map(r => +(r.cumulative_share * 100).toFixed(1));
  // граница 80%: причины левее неё и есть то, чем стоит заняться
  const headCount = data.pareto.findIndex(r => r.cumulative_share >= 0.8) + 1;

  chart.setOption({
    ...baseOption(),
    grid: { left: 8, right: 52, top: 30, bottom: 34, containLabel: true },
    legend: { ...baseOption().legend, data: ['Потери', 'Накопленная доля'] },
    tooltip: {
      ...baseOption().tooltip, trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: params => {
        const index = params[0].dataIndex;
        const row = data.pareto[index];
        return `<b>${escapeHtml(row.label)}</b><br>`
          + `потери: ${hours(row.business_s)} (${percent(row.share)})<br>`
          + `эпизодов: ${row.episodes}<br>`
          + `накопленно: ${percent(row.cumulative_share)}`;
      },
    },
    xAxis: { type: 'category', data: labels, ...axisStyle(),
      axisLabel: { ...axisStyle().axisLabel, interval: 0, rotate: 30,
        width: 150, overflow: 'break', lineHeight: 13 } },
    yAxis: [
      { type: 'value', name: 'потери', ...axisStyle(),
        axisLabel: { ...axisStyle().axisLabel, formatter: value => hours(value) } },
      { type: 'value', name: '%', min: 0, max: 100, ...axisStyle(),
        splitLine: { show: false },
        axisLabel: { ...axisStyle().axisLabel, formatter: '{value}%' } },
    ],
    series: [
      {
        name: 'Потери', type: 'bar', data: losses.map((value, index) => ({
          value,
          itemStyle: { color: index < headCount ? css('--series-8') : css('--series-4') },
        })),
        barMaxWidth: 46,
      },
      {
        name: 'Накопленная доля', type: 'line', yAxisIndex: 1, data: cumulative,
        smooth: false, symbolSize: 6,
        lineStyle: { color: css('--text-secondary'), width: 2 },
        itemStyle: { color: css('--text-secondary') },
        markLine: {
          silent: true, symbol: 'none',
          data: [{ yAxis: 80, lineStyle: { color: css('--status-warning'), type: 'dashed' },
            label: { formatter: '80% потерь', position: 'insideEndTop',
              color: css('--status-warning'), fontSize: 11 } }],
        },
      },
    ],
  }, true);
}

function renderBlockedNow(data) {
  const body = document.querySelector('#table-blocked tbody');
  if (!body) return;
  if (!data.current.length) {
    body.innerHTML = '<tr><td colspan="5" class="muted">Сейчас ничего не заблокировано</td></tr>';
    return;
  }
  body.innerHTML = data.current.map(item => `
    <tr>
      <td><span class="key-link" data-ticket="${escapeHtml(item.key)}">${escapeHtml(item.key)}</span></td>
      <td>${escapeHtml(item.label || '—')}</td>
      <td>${escapeHtml(item.assignee || '—')}</td>
      <td class="num">${hours(item.age_s)}</td>
      <td><span class="pill blocked">блок</span></td>
    </tr>`).join('');
}

async function loadBlockers() {
  const data = await fetchJson('/api/blockers');
  renderBlockerTiles(data);
  renderBlockerPareto(data);
  renderBlockedNow(data);
}

// ============================================================================
// Вкладка: обещания (SLE)
// ============================================================================

function renderPromises(data) {
  const container = document.getElementById('sle-promises');
  if (!container) return;
  if (!data.promises.length) {
    container.innerHTML = '<div class="no-findings">Обещаний пока нет. '
      + 'Зафиксируйте текущий 85-й перцентиль — дальше по нему можно проверять себя.</div>';
    return;
  }
  container.innerHTML = `<div class="scroll"><table>
    <thead><tr>
      <th>Класс</th><th class="num">Перцентиль</th><th class="num">Обещание</th>
      <th class="num">Выборка</th><th>Зафиксировано</th>
    </tr></thead>
    <tbody>${data.promises.map(promise => `
      <tr>
        <td>${escapeHtml(promise.issue_type || 'все типы')}${
          promise.priority ? ' · ' + escapeHtml(promise.priority) : ''}</td>
        <td class="num">${promise.percentile}%</td>
        <td class="num">${hours(promise.target_business_s)}</td>
        <td class="num">${promise.sample_size}</td>
        <td>${formatDay(promise.fixed_at)}</td>
      </tr>`).join('')}</tbody></table></div>`;
}

function renderSleAttainment(data) {
  const chart = ensureChart('chart-sle');
  const hint = document.getElementById('sle-hint');
  if (!chart) return;

  if (!data.periods.length) {
    if (hint) hint.textContent = 'Зафиксируйте обещание, чтобы отслеживать попадание в него.';
    return emptyChart(chart, 'Обещание не зафиксировано');
  }

  const target = data.target;
  const values = data.attainment.map(value => value == null ? null : +(value * 100).toFixed(1));
  const recent = data.attainment.filter(value => value != null).slice(-3);
  const holding = recent.length && recent.every(value => value >= target - 0.05);
  if (hint) {
    hint.textContent = holding
      ? `Обещание держится: попадание около цели в ${percent(target)}.`
      : `Попадание ниже цели в ${percent(target)}. Либо система замедлилась, `
        + 'либо обещание устарело — сравните с датой фиксации.';
  }

  chart.setOption({
    ...baseOption(),
    tooltip: {
      ...baseOption().tooltip, trigger: 'axis',
      formatter: params => {
        const index = params[0].dataIndex;
        return `${formatDay(data.periods[index])}<br>`
          + `попадание: ${params[0].value}%<br>`
          + `уложились ${data.met[index]} из ${data.counts[index]}`
          + (data.counts[index] < 10 ? '<br><i>выборка мала, доля недостоверна</i>' : '');
      },
    },
    xAxis: { type: 'category', data: data.periods.map(formatDay), ...axisStyle() },
    yAxis: { type: 'value', min: 0, max: 100, ...axisStyle(),
      axisLabel: { ...axisStyle().axisLabel, formatter: '{value}%' } },
    series: [{
      type: 'bar', data: values.map((value, index) => ({
        value,
        // на выборке меньше десяти задач доля пляшет от одной задачи:
        // показываем приглушённо, чтобы её не читали как тренд
        itemStyle: {
          color: value == null ? css('--series-4')
            : value >= target * 100 - 5 ? css('--series-1') : css('--status-warning'),
          opacity: data.counts[index] < 10 ? 0.35 : 1,
        },
      })),
      barMaxWidth: 52,
      markLine: {
        silent: true, symbol: 'none',
        data: [{ yAxis: +(target * 100).toFixed(0),
          lineStyle: { color: css('--status-critical'), type: 'dashed' },
          label: { formatter: `цель ${percent(target)}`, position: 'insideStartTop',
            color: css('--status-critical'), fontSize: 11 } }],
      },
    }],
  }, true);
}

function renderExpedite(data) {
  const chart = ensureChart('chart-expedite');
  const hint = document.getElementById('classes-hint');
  if (!chart) return;

  if (hint) {
    hint.textContent = data.priority_devalued
      ? `Срочных задач ${percent(data.overall_share)} — выше десятой части. `
        + 'Когда срочно всё, не срочно ничто: приоритет перестал нести информацию.'
      : `Срочных задач ${percent(data.overall_share)}. Устойчивый рост означает, `
        + 'что система теряет управление приоритетами.';
  }

  const mix = document.getElementById('classes-mix');
  if (mix) {
    const total = data.by_class.reduce((sum, item) => sum + item.count, 0) || 1;
    mix.innerHTML = data.by_class.map(item => `
      <div class="stat">
        <div class="stat-value">${item.count}</div>
        <div class="stat-label">${escapeHtml(item.name)} · ${percent(item.count / total)}</div>
      </div>`).join('');
  }

  if (!data.periods.length) return emptyChart(chart, 'Нет данных за период');

  chart.setOption({
    ...baseOption(),
    tooltip: {
      ...baseOption().tooltip, trigger: 'axis',
      formatter: params => `${formatDay(data.periods[params[0].dataIndex])}<br>`
        + `срочных: ${params[0].value}%<br>`
        + `всего задач: ${data.counts[params[0].dataIndex]}`,
    },
    xAxis: { type: 'category', data: data.periods.map(formatDay), ...axisStyle() },
    yAxis: { type: 'value', ...axisStyle(),
      axisLabel: { ...axisStyle().axisLabel, formatter: '{value}%' } },
    series: [{
      type: 'line', smooth: false, symbolSize: 5,
      data: data.share.map(value => +(value * 100).toFixed(1)),
      lineStyle: { color: css('--series-8'), width: 2 },
      itemStyle: { color: css('--series-8') },
      areaStyle: { opacity: 0.12, color: css('--series-8') },
      markLine: {
        silent: true, symbol: 'none',
        data: [{ yAxis: 10, lineStyle: { color: css('--status-warning'), type: 'dashed' },
          label: { formatter: 'граница 10%', position: 'insideStartTop',
            color: css('--status-warning'), fontSize: 11 } }],
      },
    }],
  }, true);
}

async function fixSle() {
  const button = document.getElementById('sle-fix-btn');
  const container = document.getElementById('sle-promises');
  const previous = button.textContent;
  button.disabled = true;
  button.textContent = 'Фиксирую…';

  try {
    const response = await fetch(`/api/sle?${filterParams()}&percentile=85`, { method: 'POST' });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      container.insertAdjacentHTML('afterbegin',
        `<div class="error">${escapeHtml(error.detail || 'Не удалось зафиксировать обещание')}</div>`);
      return;
    }
    loaded.sle = false;
    await showTab('sle', { force: true });
  } finally {
    button.disabled = false;
    button.textContent = previous;
  }
}

function renderPredictability(data) {
  const chart = ensureChart('chart-predictability');
  const hint = document.getElementById('predictability-hint');
  if (!chart) return;

  const known = data.index.filter(value => value != null);
  if (hint) {
    if (!known.length) {
      hint.textContent = `Нужно хотя бы ${data.min_sample} завершённых задач за период: `
        + 'на меньшей выборке верхние 2% — это одна-две задачи, а не свойство системы.';
    } else {
      const latest = known[known.length - 1];
      const verdict = latest < 3 ? 'Разброс рабочий, по медиане можно ориентироваться.'
        : latest < 6 ? 'Разброс заметный: обещать стоит по 85-му перцентилю, не по медиане.'
        : 'Разброс очень велик — фраза «в среднем столько-то» здесь ничего не значит.';
      hint.textContent = `Во сколько раз самые долгие задачи (верхние 2%) идут дольше `
        + `типичных. Сейчас — в ${latest.toFixed(1)} раза. ${verdict}`;
    }
  }
  if (!data.periods.length) return emptyChart(chart, 'Нет завершённых задач за период');

  chart.setOption({
    ...baseOption(),
    tooltip: {
      ...baseOption().tooltip, trigger: 'axis',
      formatter: params => {
        const detail = data.details[params[0].dataIndex];
        if (!detail.reliable) {
          return `${formatDay(detail.period)}<br>задач: ${detail.count}`
            + `<br><i>мало данных для оценки</i>`;
        }
        return `${formatDay(detail.period)}<br>`
          + `индекс: ${detail.index}<br>`
          + `медиана: ${hours(detail.p50_s)}<br>`
          + `верхние 2%: ${hours(detail.p98_s)}<br>`
          + `задач: ${detail.count}`;
      },
    },
    xAxis: { type: 'category', data: data.periods.map(formatDay), ...axisStyle() },
    yAxis: { type: 'value', min: 1, ...axisStyle(),
      axisLabel: { ...axisStyle().axisLabel, formatter: '×{value}' } },
    series: [{
      type: 'line', smooth: false, symbolSize: 7, connectNulls: false,
      data: data.index,
      lineStyle: { color: css('--series-1'), width: 2 },
      itemStyle: {
        color: params => params.value == null ? css('--series-4')
          : params.value < 3 ? css('--status-good')
          : params.value < 6 ? css('--status-warning') : css('--status-critical'),
      },
      markLine: {
        silent: true, symbol: 'none',
        data: [
          { yAxis: 3, lineStyle: { color: css('--status-good'), type: 'dashed' },
            label: { formatter: 'предсказуемо', position: 'insideStartTop',
              color: css('--status-good'), fontSize: 11 } },
          { yAxis: 6, lineStyle: { color: css('--status-critical'), type: 'dashed' },
            label: { formatter: 'медиана не значит ничего', position: 'insideStartTop',
              color: css('--status-critical'), fontSize: 11 } },
        ],
      },
    }],
  }, true);
}

const FIELD_PURPOSES = {
  work_start: 'Дата начала работы',
  work_end: 'Дата завершения',
  story_points: 'Оценка (story points)',
  epic_link: 'Связь с эпиком',
};

let jiraGuesses = [];

function switchSource(name) {
  for (const tab of document.querySelectorAll('.source-tab')) {
    tab.classList.toggle('active', tab.dataset.source === name);
  }
  for (const pane of document.querySelectorAll('.source-pane')) {
    pane.hidden = pane.dataset.sourcePane !== name;
  }
}

function renderFieldGuesses(guesses) {
  jiraGuesses = guesses;
  const container = document.getElementById('jira-fields');
  container.innerHTML = guesses.map(guess => {
    // выбранное поле всегда среди вариантов, даже если совпадение частичное
    const options = [{ id: '', name: '— не использовать —' }, ...guess.candidates];
    if (guess.field_id && !options.some(o => o.id === guess.field_id)) {
      options.push({ id: guess.field_id, name: guess.field_name });
    }
    const mark = guess.confidence === 'exact' ? 'найдено точно'
      : guess.confidence === 'partial' ? 'похоже — проверьте'
      : 'не найдено';
    return `
      <div class="field-guess">
        <label for="guess-${guess.purpose}">${FIELD_PURPOSES[guess.purpose] || guess.purpose}</label>
        <div>
          <select id="guess-${guess.purpose}" data-purpose="${guess.purpose}">
            ${options.map(option => `
              <option value="${escapeHtml(option.id)}"
                ${option.id === (guess.field_id || '') ? 'selected' : ''}>
                ${escapeHtml(option.name)}${option.id ? ` (${option.id})` : ''}
              </option>`).join('')}
          </select>
          <span class="guess-mark">${mark}</span>
        </div>
      </div>`;
  }).join('');
}

function jiraCredentials() {
  return {
    base_url: document.getElementById('jira-url').value.trim(),
    token: document.getElementById('jira-token').value.trim() || null,
  };
}

async function checkJira() {
  const button = document.getElementById('jira-check');
  const result = document.getElementById('jira-check-result');
  const credentials = jiraCredentials();

  if (!credentials.base_url) {
    result.textContent = 'Укажите адрес Jira.';
    return;
  }

  const previous = button.textContent;
  button.disabled = true;
  button.textContent = 'Проверяю…';
  result.textContent = 'Подключаюсь и читаю список полей…';

  try {
    const response = await fetch('/api/jira/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(credentials),
    });
    const data = await response.json();
    if (!response.ok) {
      result.textContent = `Не получилось: ${data.detail || response.statusText}`;
      document.getElementById('jira-config').hidden = true;
      return;
    }

    result.innerHTML = `Подключились как <b>${escapeHtml(data.user || 'неизвестно')}</b>. `
      + `Статусов на доске: ${data.statuses.length}.`;
    renderFieldGuesses(data.guesses);
    document.getElementById('jira-config').hidden = false;
  } catch (error) {
    result.textContent = `Не получилось: ${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = previous;
  }
}

async function syncJira() {
  const button = document.getElementById('jira-sync');
  const result = document.getElementById('jira-sync-result');
  const replace = document.getElementById('jira-replace').checked;

  if (replace && !window.confirm('Текущие данные будут заменены. Продолжить?')) return;

  const mapping = {};
  for (const select of document.querySelectorAll('#jira-fields select')) {
    if (select.value) mapping[select.dataset.purpose] = select.value;
  }

  const previous = button.textContent;
  button.disabled = true;
  button.textContent = 'Выгружаю…';
  result.textContent = 'Забираю задачи и историю изменений. На больших проектах это небыстро.';

  try {
    const response = await fetch('/api/jira/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...jiraCredentials(),
        jql: document.getElementById('jira-jql').value.trim(),
        mapping,
        replace_existing: replace,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      result.textContent = `Не получилось: ${data.detail || response.statusText}`;
      return;
    }

    result.innerHTML = `Загружено ${data.tickets} `
      + `${plural(data.tickets, 'задача', 'задачи', 'задач')} и ${data.events} `
      + `${plural(data.events, 'событие', 'события', 'событий')}.`
      + '<br><br>Чтобы обновлять по расписанию, сохраните это в <code>jira.yml</code> '
      + 'и запускайте <code>flowlens sync --config jira.yml</code> — токен '
      + 'подставится из переменной окружения:'
      + `<pre class="sample">${escapeHtml(data.config_yaml)}</pre>`;
    invalidateAll();
  } catch (error) {
    result.textContent = `Не получилось: ${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = previous;
  }
}

function openImport() {
  document.getElementById('import-result').innerHTML = '';
  document.getElementById('import-modal').hidden = false;
}

function closeImport() {
  document.getElementById('import-modal').hidden = true;
}

async function submitImport() {
  const input = document.getElementById('import-file');
  const result = document.getElementById('import-result');
  const button = document.getElementById('import-submit');
  const file = input.files && input.files[0];

  if (!file) {
    result.innerHTML = 'Выберите файл.';
    return;
  }

  const replace = document.getElementById('import-replace').checked;
  if (replace && !window.confirm(
    'Текущие данные будут удалены и заменены содержимым файла. Продолжить?'
  )) return;

  const previous = button.textContent;
  button.disabled = true;
  button.textContent = 'Загружаю…';
  result.innerHTML = 'Разбираю файл и пересчитываю метрики…';

  try {
    const body = new FormData();
    body.append('file', file);
    body.append('replace_existing', String(replace));

    const response = await fetch('/api/import', { method: 'POST', body });
    const data = await response.json();
    if (!response.ok) {
      result.innerHTML = `Не получилось: ${escapeHtml(data.detail || response.statusText)}`;
      return;
    }

    result.innerHTML = `Загружено ${data.tickets} `
      + `${plural(data.tickets, 'задача', 'задачи', 'задач')} и ${data.events} `
      + `${plural(data.events, 'событие', 'события', 'событий')} из «${escapeHtml(data.source)}». `
      + 'Обновляю графики…';

    // данные сменились целиком — все вкладки надо пересчитать
    invalidateAll();
    setTimeout(closeImport, 1200);
  } catch (error) {
    console.error(error);
    result.innerHTML = `Не получилось: ${escapeHtml(error.message)}`;
  } finally {
    button.disabled = false;
    button.textContent = previous;
  }
}

async function exportSlice() {
  const button = document.getElementById('export-btn');
  const full = window.confirm(
    'Выгрузить как есть — с ключами задач, заголовками и именами людей?\n\n'
    + 'OK — полный файл: он уносит эти данные за пределы вашего контура.\n'
    + 'Отмена — обезличенный: ключи и имена заменяются псевдонимами, '
    + 'заголовки убираются. Метрики одинаковые в обоих случаях.'
  );

  const previous = button.textContent;
  button.disabled = true;
  button.textContent = 'Готовлю файл…';
  try {
    const params = filterParams();
    if (full) params.set('full', 'true');
    const response = await fetch(`/api/export?${params}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);

    const blob = await response.blob();
    const name = (response.headers.get('Content-Disposition') || '')
      .match(/filename="([^"]+)"/)?.[1] || 'flowlens-export.ndjson';
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = name;
    link.click();
    URL.revokeObjectURL(url);

    const tickets = response.headers.get('X-Flowlens-Tickets');
    button.textContent = `Готово: ${tickets} задач`;
    setTimeout(() => { button.textContent = previous; }, 2500);
  } catch (error) {
    console.error(error);
    button.textContent = 'Не получилось';
    setTimeout(() => { button.textContent = previous; }, 2500);
  } finally {
    button.disabled = false;
  }
}

async function loadSle() {
  const [sle, classes, predictable] = await Promise.all([
    fetchJson('/api/sle'),
    fetchJson('/api/service-classes', { granularity: 'month' }),
    fetchJson('/api/predictability', { granularity: 'month' }),
  ]);
  renderPredictability(predictable);
  renderPromises(sle);
  renderSleAttainment(sle);
  renderExpedite(classes);
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
              (i.over_p95 ? '<span class="pill low">очень долго</span>'
                : i.over_p85 ? '<span class="pill over">долго</span>' : '') },
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
    blockers: async () => {
      const data = await fetchJson('/api/blockers');
      return {
        title: `Блокировки: ${data.episodes} ${
          plural(data.episodes, 'эпизод', 'эпизода', 'эпизодов')}`,
        html: `
          <p class="section-note">Потери по причинам, отсортированные по убыванию.
             Накопленная доля показывает, сколько причин набирают 80% простоя.</p>
          <table>
            <thead><tr><th>Причина</th><th class="num">Потери</th>
              <th class="num">Эпизодов</th><th class="num">Доля</th>
              <th class="num">Накопленно</th></tr></thead>
            <tbody>${data.pareto.map(row => `
              <tr>
                <td>${escapeHtml(row.label)}</td>
                <td class="num">${hours(row.business_s)}</td>
                <td class="num">${row.episodes}</td>
                <td class="num">${percent(row.share)}</td>
                <td class="num">${percent(row.cumulative_share)}</td>
              </tr>`).join('')}</tbody>
          </table>`,
      };
    },
    hidden: async () => {
      const data = await fetchJson('/api/hidden-queue');
      return {
        title: 'Ожидание внутри активных статусов',
        html: `
          <p class="section-note">Задача сменила фазу, но осталась на прежнем
             исполнителе — значит её ещё никто не взял. Это оценка снизу:
             часть интервалов действительно активна с первой минуты.</p>
          <table>
            <thead><tr><th>Фаза</th><th class="num">Всего</th>
              <th class="num">Из них ждали</th><th class="num">Доля</th></tr></thead>
            <tbody>${data.by_phase.map(row => `
              <tr>
                <td>${escapeHtml(PHASE_LABEL[row.phase] || row.phase)}</td>
                <td class="num">${hours(row.total_s)}</td>
                <td class="num">${hours(row.waiting_s)}</td>
                <td class="num">${percent(row.share)}</td>
              </tr>`).join('')}</tbody>
          </table>`,
      };
    },
    transitions: async () => {
      const data = await fetchJson('/api/transitions');
      return {
        title: `Переходы: ${data.total_moves}, из них возвратов ${data.backflow_moves}`,
        html: `
          <p class="section-note">Возврат — переход назад по доске. Выход из
             блокировки возвратом не считается: это возобновление работы.</p>
          <table>
            <thead><tr><th>Откуда</th><th>Куда</th>
              <th class="num">Переходов</th><th></th></tr></thead>
            <tbody>${data.cells.map(cell => `
              <tr>
                <td>${escapeHtml(cell.from)}</td>
                <td>${escapeHtml(cell.to)}</td>
                <td class="num">${cell.moves}</td>
                <td>${cell.is_backflow ? '<span class="pill over">возврат</span>' : ''}</td>
              </tr>`).join('')}</tbody>
          </table>`,
      };
    },
    backlog: async () => {
      const data = await fetchJson('/api/backlog');
      return {
        title: `Очередь: ${data.size} ${plural(data.size, 'задача', 'задачи', 'задач')}`,
        html: `
          <p class="section-note">Задачи, которые ещё не начинали. Половина ждёт
             дольше ${calendarDays(data.p50_age_s)}, 15% — дольше
             ${calendarDays(data.p85_age_s)}. Время календарное: очередь идёт
             и в выходные.</p>
          <table>
            <thead><tr><th>Возраст</th><th class="num">Задач</th></tr></thead>
            <tbody>${data.histogram.map(bucket => `
              <tr><td>${escapeHtml(bucket.label)}</td>
                  <td class="num">${bucket.count}</td></tr>`).join('')}</tbody>
          </table>`,
      };
    },
    predictability: async () => {
      const data = await fetchJson('/api/predictability', { granularity: 'month' });
      return {
        title: `Предсказуемость: индекс ${data.latest ?? '—'}`,
        html: `
          <p class="section-note">Отношение верхних 2% времени цикла к медиане.
             Периоды с выборкой меньше ${data.min_sample} задач не оцениваются:
             там этот показатель описывает один выброс, а не систему.</p>
          <table>
            <thead><tr><th>Период</th><th class="num">Индекс</th>
              <th class="num">Медиана</th><th class="num">Верхние 2%</th>
              <th class="num">Задач</th></tr></thead>
            <tbody>${data.details.map(d => `
              <tr>
                <td>${formatDay(d.period)}</td>
                <td class="num">${d.index != null ? '×' + d.index : '—'}</td>
                <td class="num">${hours(d.p50_s)}</td>
                <td class="num">${hours(d.p98_s)}</td>
                <td class="num">${d.count}</td>
              </tr>`).reverse().join('')}</tbody>
          </table>`,
      };
    },
    sle: async () => {
      const data = await fetchJson('/api/sle');
      if (!data.promises.length) {
        return { title: 'Обещания', html: '<p class="section-note">Обещание не зафиксировано.</p>' };
      }
      return {
        title: `Попадание при цели ${percent(data.target)}`,
        html: `
          <p class="section-note">Доля задач, уложившихся в зафиксированное обещание.
             Устойчивое падение означает, что либо система замедлилась,
             либо обещание устарело.</p>
          <table>
            <thead><tr><th>Период</th><th class="num">Попадание</th>
              <th class="num">Уложились</th><th class="num">Всего</th></tr></thead>
            <tbody>${data.periods.map((period, i) => `
              <tr>
                <td>${formatDay(period)}</td>
                <td class="num">${percent(data.attainment[i])}</td>
                <td class="num">${data.met[i]}</td>
                <td class="num">${data.counts[i]}</td>
              </tr>`).reverse().join('')}</tbody>
          </table>`,
      };
    },
    classes: async () => {
      const data = await fetchJson('/api/service-classes', { granularity: 'month' });
      const total = data.by_class.reduce((sum, item) => sum + item.count, 0) || 1;
      return {
        title: `Классы обслуживания: срочных ${percent(data.overall_share)}`,
        html: `
          <p class="section-note">Класс задаётся правилом issue_type × priority.
             ${data.priority_devalued
               ? 'Срочных больше десятой части — приоритет перестал нести информацию.'
               : 'Доля срочных в пределах нормы.'}</p>
          <table>
            <thead><tr><th>Класс</th><th class="num">Задач</th>
              <th class="num">Доля</th></tr></thead>
            <tbody>${data.by_class.map(item => `
              <tr><td>${escapeHtml(item.name)}</td>
                  <td class="num">${item.count}</td>
                  <td class="num">${percent(item.count / total)}</td></tr>`).join('')}</tbody>
          </table>`,
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
  blockers: loadBlockers,
  sle: loadSle,
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
  for (const id of ['f-from', 'f-to', 'f-type', 'f-priority', 'f-component', 'f-confidence',
                    'f-completion']) {
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
  document.getElementById('sle-fix-btn').addEventListener('click', fixSle);
  document.getElementById('export-btn').addEventListener('click', exportSlice);
  document.getElementById('import-btn').addEventListener('click', openImport);
  document.getElementById('import-submit').addEventListener('click', submitImport);
  document.getElementById('jira-check').addEventListener('click', checkJira);
  document.getElementById('jira-sync').addEventListener('click', syncJira);
  for (const tab of document.querySelectorAll('.source-tab')) {
    tab.addEventListener('click', () => switchSource(tab.dataset.source));
  }
  for (const element of document.querySelectorAll('[data-close-import]')) {
    element.addEventListener('click', closeImport);
  }

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
    if (event.key !== 'Escape') return;
    if (!document.getElementById('modal').hidden) closeModal();
    if (!document.getElementById('import-modal').hidden) closeImport();
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
