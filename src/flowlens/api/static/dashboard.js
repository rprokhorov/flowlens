'use strict';

// Цвет закреплён за фазой навсегда: фильтрация не должна перекрашивать
// оставшиеся серии — читатель, запомнивший «блокировка красная», не должен
// переучиваться.
const PHASE_ORDER = ['backlog', 'in_progress', 'blocked', 'verify', 'done_pending', 'done'];
const PHASE_SLOT = {
  backlog: 4,       // yellow
  in_progress: 1,   // blue
  blocked: 8,       // red
  verify: 7,        // violet
  done_pending: 3,  // aqua
  done: 6,          // green
};
const PHASE_LABEL = {
  backlog: 'В очереди',
  in_progress: 'В работе',
  blocked: 'Заблокировано',
  verify: 'Проверка',
  done_pending: 'Ждёт релиза',
  done: 'Готово',
  triage: 'Разбор',
  review: 'Ревью',
  cancelled: 'Отменено',
};

const charts = {};
let unit = 'business';

function css(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
function phaseColor(phase) {
  return css(`--series-${PHASE_SLOT[phase] || 5}`);
}

// --- форматирование ---------------------------------------------------------

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

// --- запросы ----------------------------------------------------------------

function filterParams() {
  const params = new URLSearchParams();
  const from = document.getElementById('f-from').value;
  const to = document.getElementById('f-to').value;
  const type = document.getElementById('f-type').value;
  const priority = document.getElementById('f-priority').value;
  const component = document.getElementById('f-component').value;
  const confidence = document.getElementById('f-confidence').value;

  if (from) params.set('date_from', from);
  if (to) params.set('date_to', to);
  if (type) params.set('issue_type', type);
  if (priority) params.set('priority', priority);
  if (component) params.set('component', component);
  if (confidence !== 'low') params.set('min_confidence', confidence);
  params.set('unit', unit);
  return params;
}

async function fetchJson(path, extra) {
  const params = filterParams();
  if (extra) for (const [k, v] of Object.entries(extra)) params.set(k, v);
  const response = await fetch(`${path}?${params}`);
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

// --- общие настройки графиков ----------------------------------------------

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
  if (!charts[id]) {
    charts[id] = echarts.init(document.getElementById(id), null, { renderer: 'canvas' });
  }
  return charts[id];
}

// --- плитки -----------------------------------------------------------------

function renderTiles(summary, quality) {
  const trust = quality.trustworthy_pct;
  const trustClass = trust >= 80 ? 'good' : trust >= 50 ? 'warning' : 'critical';
  const efficiency = summary.avg_flow_efficiency;
  const effClass = efficiency == null ? '' : efficiency >= 0.4 ? 'good'
    : efficiency >= 0.25 ? 'warning' : 'critical';

  const tiles = [
    { label: 'Задач в периоде', value: summary.total_tickets,
      note: `${summary.completed} завершено` },
    { label: 'Не завершено', value: summary.open_tickets,
      note: 'на текущий момент' },
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

// --- накопительная диаграмма потока ----------------------------------------

function renderCfd(data) {
  const chart = ensureChart('chart-cfd');
  const ordered = PHASE_ORDER
    .map(phase => data.series.find(s => s.phase === phase))
    .filter(Boolean)
    .concat(data.series.filter(s => !PHASE_ORDER.includes(s.phase)));

  // «Готово» накапливается за весь период и по величине давит остальные фазы,
  // из-за чего незавершённая работа — то, ради чего смотрят CFD — не читается.
  // Серия остаётся в легенде: её можно включить одним щелчком.
  const legendSelected = {};
  for (const s of ordered) {
    legendSelected[PHASE_LABEL[s.phase] || s.phase] = s.phase !== 'done';
  }

  chart.setOption({
    ...baseOption(),
    tooltip: { ...baseOption().tooltip, trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: css('--text-muted') } } },
    legend: { ...baseOption().legend,
      data: ordered.map(s => PHASE_LABEL[s.phase] || s.phase),
      selected: legendSelected },
    xAxis: { type: 'category', data: data.days, boundaryGap: false, ...axisStyle() },
    yAxis: { type: 'value', ...axisStyle(), name: 'задач', nameTextStyle: { fontSize: 11 } },
    dataZoom: [
      { type: 'inside', start: 60, end: 100 },
      { type: 'slider', start: 60, end: 100, height: 18, bottom: 4,
        borderColor: css('--border'), fillerColor: css('--surface-2'),
        textStyle: { color: css('--text-muted'), fontSize: 10 } },
    ],
    grid: { left: 8, right: 14, top: 30, bottom: 42, containLabel: true },
    series: ordered.map(s => ({
      name: PHASE_LABEL[s.phase] || s.phase,
      type: 'line', stack: 'total', areaStyle: { opacity: 0.85 },
      lineStyle: { width: 0 },
      showSymbol: false,
      emphasis: { focus: 'series' },
      itemStyle: { color: phaseColor(s.phase) },
      data: s.values,
    })),
  }, true);
}

// --- время цикла ------------------------------------------------------------

function renderCycleTime(data) {
  const chart = ensureChart('chart-cycle');
  if (!data.count) {
    chart.setOption({ ...baseOption(), title: {
      text: 'Нет завершённых задач', left: 'center', top: 'middle',
      textStyle: { color: css('--text-muted'), fontSize: 13, fontWeight: 'normal' } } }, true);
    return;
  }

  const labels = data.histogram.map(b => hours(b.from));
  const p = data.percentiles;
  chart.setOption({
    ...baseOption(),
    legend: { show: false },
    tooltip: { ...baseOption().tooltip, trigger: 'axis',
      formatter: params => {
        const bucket = data.histogram[params[0].dataIndex];
        const count = params[0].value;
        return `${hours(bucket.from)} — ${bucket.to ? hours(bucket.to) : '∞'}<br>` +
               `<b>${count}</b> ${plural(count, 'задача', 'задачи', 'задач')}`;
      } },
    xAxis: { type: 'category', data: labels, ...axisStyle() },
    yAxis: { type: 'value', ...axisStyle(), name: 'задач', nameTextStyle: { fontSize: 11 } },
    series: [{
      type: 'bar',
      data: data.histogram.map(b => b.count),
      itemStyle: { color: css('--series-1'), borderRadius: [4, 4, 0, 0] },
      barCategoryGap: '18%',
      markLine: {
        symbol: 'none',
        label: { formatter: p => p.name, color: css('--text-secondary'), fontSize: 11 },
        lineStyle: { color: css('--text-muted'), type: 'dashed', width: 1 },
        data: [
          { name: `медиана ${hours(p.p50)}`,
            xAxis: bucketIndex(data.histogram, p.p50) },
          { name: `85% ${hours(p.p85)}`,
            xAxis: bucketIndex(data.histogram, p.p85) },
        ],
      },
    }],
  }, true);
}
function bucketIndex(histogram, value) {
  for (let i = 0; i < histogram.length; i++) {
    if (histogram[i].to == null || value < histogram[i].to) return i;
  }
  return histogram.length - 1;
}

// --- поступление и закрытие -------------------------------------------------

function renderArrival(data) {
  const chart = ensureChart('chart-arrival');
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

// --- распределение времени по фазам ----------------------------------------

function renderPhases(data) {
  const chart = ensureChart('chart-phases');
  // медиана на задачу читается осмысленно; сумма по всем задачам за год —
  // число вроде «10030 дней», из которого ничего не следует
  const phases = data.by_phase
    .filter(p => p.phase !== 'done' && p.total_s > 0)
    .sort((a, b) => b.p50_s - a.p50_s);
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
        value: p.p50_s, itemStyle: { color: phaseColor(p.phase) },
      })).reverse(),
      itemStyle: { borderRadius: [0, 4, 4, 0] },
      barCategoryGap: '30%',
      label: { show: true, position: 'right', color: css('--text-secondary'),
        fontSize: 11, formatter: item => hours(item.value) },
    }],
  }, true);
}

// --- нагрузка ---------------------------------------------------------------

function renderPeople(data) {
  const chart = ensureChart('chart-people');
  const people = data.people.slice(0, 10);
  chart.setOption({
    ...baseOption(),
    legend: { ...baseOption().legend, data: ['В работе', 'Ожидание', 'Заблокировано'] },
    tooltip: { ...baseOption().tooltip, trigger: 'axis',
      axisPointer: { type: 'shadow' } },
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
}

// --- висящие задачи ---------------------------------------------------------

function renderAging(data) {
  const body = document.querySelector('#table-aging tbody');
  const items = data.items.slice(0, 60);
  document.getElementById('aging-hint').textContent =
    `${data.total} ${plural(data.total, 'задача', 'задачи', 'задач')} не завершено, ` +
    `из них ${data.over_p85} висят дольше 85-го перцентиля ` +
    `(${hours(data.reference.p85)}).`;

  body.innerHTML = items.map(item => `
    <tr>
      <td title="${escapeHtml(item.summary || '')}">${escapeHtml(item.key)}</td>
      <td>${escapeHtml(item.status)}</td>
      <td>${escapeHtml(item.assignee || '—')}</td>
      <td class="num">${hours(item.age_s)}</td>
      <td>${item.is_blocked ? '<span class="pill blocked">блок</span>' : ''}
          ${item.over_p85 ? '<span class="pill over">долго</span>' : ''}</td>
    </tr>`).join('') || '<tr><td colspan="5">Нет незавершённых задач</td></tr>';
}
function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

// --- качество данных --------------------------------------------------------

function renderQuality(data) {
  const container = document.getElementById('quality');
  const items = data.anomalies.map(a => `
    <div class="quality-item">
      <div class="quality-count">${a.count}</div>
      <div class="quality-text">
        ${escapeHtml(a.label)}
        <div class="quality-hint">${escapeHtml(a.hint)}</div>
      </div>
    </div>`).join('');

  container.innerHTML = `
    <div class="verdict">${escapeHtml(data.verdict)}</div>
    <div style="color: var(--text-secondary); font-size: 12px; margin-bottom: 8px;">
      Даты заполнены у ${data.declared_coverage_pct}% задач
    </div>
    ${items || '<div class="quality-hint">Проблем не обнаружено.</div>'}`;
}

// --- наблюдения -------------------------------------------------------------

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
          f.tickets.map(k => `<span class="pill">${escapeHtml(k)}</span>`).join('')
        }</div>` : ''}
      </div>
    </div>`).join('');
}

// --- прогноз ----------------------------------------------------------------

function renderForecast(data) {
  const container = document.getElementById('forecast');
  const long = data.how_long;
  const many = data.how_many;

  if (!long.percentiles || !Object.keys(long.percentiles).length) {
    container.innerHTML =
      `<div class="forecast-warning">${escapeHtml(long.warning || 'Недостаточно истории для прогноза.')}</div>`;
    return;
  }

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

// --- загрузка ---------------------------------------------------------------

async function loadAll() {
  try {
    const [summary, quality, cfd, cycle, arrival, phases, people, aging, advice,
           forecast] =
      await Promise.all([
        fetchJson('/api/summary'),
        fetch('/api/quality').then(r => r.json()),
        fetchJson('/api/cfd'),
        fetchJson('/api/cycle-time'),
        fetchJson('/api/arrival-throughput', { granularity: 'week' }),
        fetchJson('/api/flow-efficiency'),
        fetchJson('/api/people'),
        fetchJson('/api/aging-wip'),
        fetchJson('/api/advice'),
        fetchJson('/api/forecast', { horizon_periods: 4 }),
      ]);
    renderAdvice(advice);
    renderForecast(forecast);
    renderTiles(summary, quality);
    renderQuality(quality);
    renderCfd(cfd);
    renderCycleTime(cycle);
    renderArrival(arrival);
    renderPhases(phases);
    renderPeople(people);
    renderAging(aging);
  } catch (error) {
    document.getElementById('tiles').innerHTML =
      `<div class="error">Не удалось загрузить данные: ${escapeHtml(error.message)}</div>`;
  }
}

async function loadFilters() {
  const options = await fetch('/api/filters').then(r => r.json());
  fill('f-type', options.issue_types);
  fill('f-priority', options.priorities);
  fill('f-component', options.components);
  if (options.period.earliest) {
    document.getElementById('period').textContent =
      `${options.period.earliest} — ${options.period.latest}`;
  }
}
function fill(id, values) {
  const select = document.getElementById(id);
  for (const value of values) {
    const option = document.createElement('option');
    option.value = option.textContent = value;
    select.appendChild(option);
  }
}

// --- разбор -----------------------------------------------------------------

async function requestExplanation() {
  const button = document.getElementById('explain-btn');
  const container = document.getElementById('explain');
  button.disabled = true;
  button.textContent = 'Готовлю разбор…';
  container.innerHTML = '<div class="loading">модель анализирует метрики…</div>';

  try {
    const params = filterParams();
    const response = await fetch(`/api/explain?${params}`, { method: 'POST' });
    const data = await response.json();

    if (!data.available) {
      container.innerHTML =
        `<div class="explain-unavailable">${escapeHtml(data.reason || 'Разбор недоступен.')}</div>`;
    } else {
      const paragraphs = data.text.split(/\n\n+/)
        .map(p => `<p>${escapeHtml(p)}</p>`).join('');
      container.innerHTML = `<div class="explain-text">${paragraphs}</div>`;
    }
  } catch (error) {
    container.innerHTML =
      `<div class="explain-unavailable">Не удалось получить разбор: ${escapeHtml(error.message)}</div>`;
  } finally {
    button.disabled = false;
    button.textContent = 'Обновить разбор';
  }
}

// --- события ----------------------------------------------------------------

function setUnit(next) {
  unit = next;
  document.getElementById('u-business').classList.toggle('active', next === 'business');
  document.getElementById('u-calendar').classList.toggle('active', next === 'calendar');
  loadAll();
}

document.addEventListener('DOMContentLoaded', async () => {
  for (const id of ['f-from', 'f-to', 'f-type', 'f-priority', 'f-component', 'f-confidence']) {
    document.getElementById(id).addEventListener('change', loadAll);
  }
  document.getElementById('explain-btn').addEventListener('click', requestExplanation);
  document.getElementById('u-business').addEventListener('click', () => setUnit('business'));
  document.getElementById('u-calendar').addEventListener('click', () => setUnit('calendar'));
  document.getElementById('theme-toggle').addEventListener('click', () => {
    const current = document.documentElement.getAttribute('data-theme');
    const isDark = current === 'dark' ||
      (!current && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.setAttribute('data-theme', isDark ? 'light' : 'dark');
    loadAll();  // перерисовать графики под новую тему
  });
  window.addEventListener('resize', () => {
    for (const chart of Object.values(charts)) chart.resize();
  });

  await loadFilters();
  await loadAll();
});
