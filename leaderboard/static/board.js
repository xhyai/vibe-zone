const messages = JSON.parse(document.querySelector('#translations').dataset.messages);
const badgeLabels = JSON.parse(document.querySelector('#translations').dataset.badges);
const t = text => messages[text] || text;
const locale = document.documentElement.lang === 'pl' ? 'pl-PL' : 'en-GB';
const board = document.querySelector('[data-live-board]');
const formatTime = value => value ? new Date(value * 1000).toLocaleString(locale, {timeZone:'Europe/Warsaw', day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit'}) : t('jeszcze brak');
const metric = (value, suffix = '', digits = 1) => value == null ? '-' : value.toFixed(digits) + suffix;
document.querySelectorAll('[data-time]').forEach(node => {node.textContent = formatTime(Number(node.dataset.time));});
// Old home-page bookmarks still lead to their original task. Keep the language query.
function followLegacyBookmark() {
  if (location.pathname !== '/') return;
  const destination = {'#submit-title':'/submit', '#submit':'/submit', '#submissions-title':'/submit', '#ranking-title':'/leaderboard', '#ranking':'/leaderboard'}[location.hash];
  if (destination) location.replace(destination + location.search + location.hash);
}
followLegacyBookmark();
window.addEventListener('hashchange', followLegacyBookmark);
function cell(text, className = '') { const td = document.createElement('td'); td.textContent = text; td.className = className; return td; }
function render(data) {
  const banner = document.querySelector('.test-banner');
  if (banner && data.runner) banner.hidden = !data.runner.test_banner;
  const rows = document.querySelector('#ranking-rows'); rows.replaceChildren();
  const compact = board.dataset.compact === '1';
  const limit = Number(board.dataset.limit);
  const ranking = limit ? data.ranking.slice(0, limit) : data.ranking;
  ranking.forEach(row => {
    const tr = document.createElement('tr'); const name = cell(''); const place = document.createElement('span');
    place.className = 'place'; place.textContent = row.rank;
    const link = document.createElement('a'); link.href = '/team/' + row.participant_id;
    const strong = document.createElement('strong'); strong.textContent = row.nickname; link.append(strong);
    const source = document.createElement('small'); source.textContent = (row.result.profile?.name || '-') + ' · ' + row.source + (row.result.profile?.name === 'smoke' ? ' ' + t('· test lokalny') : '');
    name.append(place, link, source);
    const badges = document.createElement('div'); badges.className = 'badges';
    for (const key of row.badges || []) {const badge=document.createElement('span');badge.className='badge';badge.textContent=t(badgeLabels[key]);badges.append(badge);}
    name.append(badges);
    if (row.extras?.hidden) {const checks=document.createElement('small');checks.className='checks';const hidden=row.extras.hidden;checks.textContent=t('Ukryte testy')+': '+(hidden.total ? `${hidden.passed}/${hidden.total}` : t('brak wyniku'));name.append(checks);}
    tr.append(name, cell(row.result.max_vus.toLocaleString(locale),'number score'), cell(metric(row.result.image_bytes == null ? null : row.result.image_bytes / 1e6, ' MB', 2),'number'), cell(metric(row.result.cold_start_ms, ' ms', 0),'number'), cell(metric(row.result.p99_ms, ' ms'),'number'), cell(metric(row.result.throughput_rps),'number'));
    if (!compact) {
      const samples = cell(row.stats.measurements || '-', 'number');
      const range = document.createElement('small'); range.className = 'sample-range';
      range.textContent = `${row.stats.vus_min ?? '-'}-${row.stats.vus_max ?? '-'} VU`; samples.append(range);
      tr.append(cell(metric(row.result.p95_ms, ' ms'), 'number'), cell(metric(row.result.error_percent, '%', 2), 'number'), samples);
    }
    rows.append(tr);
  });
  if (!ranking.length) {const tr = document.createElement('tr'); const td = cell(t('Czekamy na pierwszy wynik.'),'empty');td.colSpan=compact?6:9;tr.append(td);rows.append(tr);}
  document.querySelectorAll('[data-summary]').forEach(node => {node.textContent = data.summary[node.dataset.summary] ?? '-';});
  document.querySelector('#last-update').textContent = formatTime(data.last_update);
  document.querySelector('#next-update').textContent = formatTime(data.next_update);
  document.querySelector('#batch-status').textContent = data.running ? t('Trwa pomiar') : data.requested ? t('Batch w kolejce') : '';
  const announcement = document.querySelector('#bonus-announcement');
  announcement.hidden = !data.extras.bonus;
  document.querySelector('#bonus-spec').textContent = data.extras.bonus?.[document.documentElement.lang] || '';
  document.querySelector('#vote-link').hidden = !data.extras.features.voting;
  document.querySelector('#pilot-ranking').hidden = !data.extras.features.ai_pilot;
  const pilots = document.querySelector('#pilot-results'); pilots.replaceChildren();
  for (const entry of data.extras.pilot) {const li=document.createElement('li');const a=document.createElement('a');a.href='/team/'+entry.id;a.textContent=entry.nickname;li.append(a, ': '+entry.elapsed_seconds.toFixed(1)+' s');pilots.append(li);}
  if (!data.extras.pilot.length) {const li=document.createElement('li');li.textContent=t('Czekamy na pierwszy wynik.');pilots.append(li);}
  const rejected = document.querySelector('#rejected-list'); rejected.replaceChildren();
  document.querySelector('#rejected-panel').hidden = !data.rejected.length;
  data.rejected.forEach(row => {const li=document.createElement('li');li.textContent=`${row.nickname}: ${row.result.error || t('Testy niezaliczone. Szczegóły u organizatora.')}`;rejected.append(li);});
}
async function refresh() {
  try {
    const response = await fetch('/api/leaderboard?lang=' + document.documentElement.lang + (new URLSearchParams(location.search).has('profile') ? '&profile=' + encodeURIComponent(new URLSearchParams(location.search).get('profile')) : ''), {cache:'no-store', signal:AbortSignal.timeout(8000)});
    if (!response.ok) throw new Error('fetch');
    render(await response.json());
    document.querySelector('#refresh-status').textContent = t('Odświeżanie co 10 s');
  } catch (_) { document.querySelector('#refresh-status').textContent = t('Brak połączenia. Ponawiamy za 10 s.'); }
  setTimeout(refresh,10000);
}
if (board) setTimeout(refresh,10000);
