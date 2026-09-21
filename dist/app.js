(() => {
  'use strict';
  const PAIRS = [
    { id: 'EUR/USD', name: '欧元 / 美元', initial: 1.08742, digits: 5 },
    { id: 'GBP/USD', name: '英镑 / 美元', initial: 1.27486, digits: 5 },
    { id: 'USD/JPY', name: '美元 / 日元', initial: 149.382, digits: 3 },
    { id: 'USD/CHF', name: '美元 / 瑞郎', initial: 0.88521, digits: 5 },
    { id: 'AUD/USD', name: '澳元 / 美元', initial: 0.65432, digits: 5 },
    { id: 'USD/CAD', name: '美元 / 加元', initial: 1.37185, digits: 5 }
  ];
  const $ = (id) => document.getElementById(id);
  const money = (n) => `${n < 0 ? '-' : ''}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const signedMoney = (n) => `${n >= 0 ? '+' : '-'}$${Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const pct = (n) => `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
  const priceText = (pair, n) => Number.isFinite(n) ? n.toFixed(PAIRS.find(p => p.id === pair).digits) : '—';
  const initialState = () => ({ cash: 10000, debt: 0, positions: [], history: [], pair: 'EUR/USD', mode: 'sim', leverage: 20, margin: 500 });
  let state = initialState();
  let savedSim = null;
  try {
    const saved = JSON.parse(localStorage.getItem('fx-heartbeat-save') || 'null');
    if (saved && Number.isFinite(saved.cash) && Array.isArray(saved.positions)) {
      state = { ...state, ...saved, mode: saved.mode === 'real' ? 'real' : 'sim' };
      if (PAIRS.every(p => Array.isArray(saved.sim?.[p.id]?.candles) && Number.isFinite(saved.sim[p.id].price))) savedSim = saved.sim;
    }
  } catch (_) {}
  if (!PAIRS.some(p => p.id === state.pair)) state.pair = 'EUR/USD';
  state.leverage = Math.max(1, Math.min(100, Number(state.leverage) || 20));
  state.margin = Math.max(10, Number(state.margin) || 500);
  let sim = {};
  let real = { items: [], cursor: 0, loading: false, error: '' };
  let autoPlay = false;
  let toastTimer;
  let modalAction = null;
  let modalQueue = [];
  let modalOpen = false;
  let historyExpanded = false;
  let requestCounter = 0;

  function save() {
    try { localStorage.setItem('fx-heartbeat-save', JSON.stringify({ cash: state.cash, debt: state.debt, positions: state.positions, history: state.history.slice(0, 25), pair: state.pair, mode: state.mode, leverage: state.leverage, margin: state.margin, lastRealDate: state.lastRealDate, sim })); } catch (_) {}
  }
  function seedSim() {
    if (savedSim) { sim = savedSim; savedSim = null; return; }
    PAIRS.forEach((pair, index) => {
      let value = pair.initial;
      const candles = [];
      for (let i = 0; i < 76; i++) {
        const open = value;
        const wave = Math.sin(i * .43 + index * 1.9) * .0008 + Math.cos(i * .19 + index) * .00055;
        value = open * (1 + wave + (Math.random() - .5) * .0019);
        const spread = open * (.0005 + Math.random() * .00065);
        candles.push({ open, high: Math.max(open, value) + spread, low: Math.min(open, value) - spread, close: value });
      }
      sim[pair.id] = { candles, price: value };
    });
  }
  seedSim();
  function currentPrice(pair) {
    if (state.mode === 'real') return real.items[real.cursor]?.prices[pair] ?? NaN;
    return sim[pair]?.price ?? NaN;
  }
  function previousPrice(pair) {
    if (state.mode === 'real') return real.items[Math.max(0, real.cursor - 1)]?.prices[pair] ?? currentPrice(pair);
    const candles = sim[pair]?.candles || [];
    return candles.at(-2)?.close ?? currentPrice(pair);
  }
  function positionPnl(pos) {
    const price = currentPrice(pos.pair);
    if (!Number.isFinite(price)) return 0;
    return pos.margin * pos.leverage * pos.side * (price / pos.entry - 1);
  }
  function account() {
    const used = state.positions.reduce((sum, p) => sum + p.margin, 0);
    const floating = state.positions.reduce((sum, p) => sum + positionPnl(p), 0);
    return { used, floating, equity: state.cash + used + floating - state.debt };
  }
  function notify(message) {
    const toast = $('toast'); toast.textContent = message; toast.classList.add('show');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.classList.remove('show'), 3300);
  }
  function openModal(options) {
    if (modalOpen) { modalQueue.push(options); return; }
    modalOpen = true;
    modalAction = options.action || null;
    $('modalIcon').textContent = options.icon || '✦';
    $('modalIcon').classList.toggle('loss', !!options.loss);
    $('modalTitle').textContent = options.title;
    $('modalMessage').textContent = options.message || '';
    $('modalDetail').textContent = options.detail || '';
    $('modalInputWrap').classList.toggle('hidden', !options.input);
    $('modalInputLabel').textContent = options.input?.label || '';
    $('modalInput').value = options.input?.value ?? '';
    $('modalInput').max = options.input?.max ?? '';
    $('modalConfirm').textContent = options.confirm || '知道了';
    $('modalCancel').classList.toggle('hidden', !options.action);
    $('modal').classList.remove('hidden');
    if (options.input) setTimeout(() => $('modalInput').focus(), 30);
    else setTimeout(() => $('modalConfirm').focus(), 30);
  }
  function closeModal() {
    $('modal').classList.add('hidden'); modalOpen = false; modalAction = null;
    if (modalQueue.length) setTimeout(() => openModal(modalQueue.shift()), 70);
  }
  function renderPairs() {
    const list = $('pairList'); list.replaceChildren();
    PAIRS.forEach(pair => {
      const rate = currentPrice(pair.id);
      const change = (rate / previousPrice(pair.id) - 1) * 100;
      const row = document.createElement('button'); row.type = 'button'; row.className = 'pair-row' + (state.pair === pair.id ? ' selected' : '');
      row.setAttribute('role', 'option'); row.setAttribute('aria-selected', String(state.pair === pair.id));
      row.innerHTML = `<span><strong>${pair.id}</strong><small>${pair.name}</small></span><span class="pair-numbers"><strong>${priceText(pair.id, rate)}</strong><small class="${change >= 0 ? 'positive' : 'negative'}">${pct(Number.isFinite(change) ? change : 0)}</small></span>`;
      row.addEventListener('click', () => { state.pair = pair.id; save(); render(); });
      list.append(row);
    });
  }
  function renderAccount() {
    const a = account();
    $('equity').textContent = money(a.equity);
    $('cash').textContent = money(state.cash);
    $('floating').textContent = signedMoney(a.floating);
    $('floating').className = a.floating >= 0 ? 'positive' : 'negative';
    $('usedMargin').textContent = money(a.used);
    $('debt').textContent = money(state.debt);
  }
  function renderOrder() {
    $('orderPair').textContent = state.pair;
    $('marginInput').value = state.margin;
    $('leverageRange').value = state.leverage;
    $('leverageRange').style.setProperty('--progress', `${(state.leverage - 1) / 99 * 100}%`);
    $('leverageValue').textContent = `${state.leverage}×`;
    $('notional').textContent = money(state.margin * state.leverage);
    $('riskPercent').textContent = `${(80 / state.leverage).toFixed(2)}%`;
  }
  function renderPositions() {
    $('positionCount').textContent = state.positions.length;
    $('riskHint').textContent = state.positions.length ? `合计 ${money(account().used)} 保证金` : '暂无持仓';
    const root = $('positions'); root.replaceChildren();
    if (!state.positions.length) { root.innerHTML = '<div class="empty-state"><div class="empty-icon">♡</div>还没有持仓，选一个货币对开始吧</div>'; return; }
    state.positions.forEach(pos => {
      const pnl = positionPnl(pos);
      const row = document.createElement('div'); row.className = 'position-row';
      row.innerHTML = `<div class="pos-name"><strong>${pos.pair}<span class="direction ${pos.side === 1 ? 'long' : 'short'}">${pos.side === 1 ? '做多' : '做空'}</span></strong><small>${money(pos.margin)} 保证金 · ${pos.leverage}×</small></div><div class="pos-cell pos-entry"><strong>${priceText(pos.pair, pos.entry)}</strong><small>开仓价格</small></div><div class="pos-cell"><strong>${priceText(pos.pair, currentPrice(pos.pair))}</strong><small>当前价格</small></div><div class="pos-cell"><strong class="${pnl >= 0 ? 'positive' : 'negative'}">${signedMoney(pnl)}</strong><small>浮动盈亏</small></div><button class="close-btn" type="button">平仓</button>`;
      row.querySelector('button').addEventListener('click', () => closePosition(pos.id, false)); root.append(row);
    });
  }
  function renderHistory() {
    $('history').classList.toggle('hidden', !historyExpanded);
    $('historyToggle').textContent = historyExpanded ? '收起记录' : '展开记录';
    if (!historyExpanded) return;
    const root = $('history'); root.replaceChildren();
    if (!state.history.length) { root.innerHTML = '<div class="empty-state">交易结算后会显示在这里</div>'; return; }
    state.history.slice(0, 8).forEach(item => {
      const row = document.createElement('div'); row.className = 'history-row';
      row.innerHTML = `<span>${item.pair} · ${item.side === 1 ? '做多' : '做空'} · ${item.liquidated ? '爆仓' : '平仓'} · ${item.time}</span><strong class="${item.pnl >= 0 ? 'positive' : 'negative'}">${signedMoney(item.pnl)}</strong>`;
      root.append(row);
    });
  }
  function renderChartHeader() {
    const pair = PAIRS.find(p => p.id === state.pair);
    const rate = currentPrice(pair.id); const change = (rate / previousPrice(pair.id) - 1) * 100;
    $('pairTitle').textContent = pair.id.replace('/', ' / ');
    $('pairName').textContent = pair.name;
    $('currentPrice').textContent = priceText(pair.id, rate);
    $('priceChange').textContent = pct(Number.isFinite(change) ? change : 0);
    $('priceChange').className = change >= 0 ? 'positive' : 'negative';
    $('modeBadge').textContent = state.mode === 'real' ? '真实历史数据' : '模拟行情';
    $('modeBadge').classList.toggle('real', state.mode === 'real');
    $('simMode').classList.toggle('active', state.mode === 'sim');
    $('realMode').classList.toggle('active', state.mode === 'real');
    $('nextDayBtn').classList.toggle('hidden', state.mode !== 'real');
    $('playBtn').classList.toggle('hidden', state.mode !== 'real');
    $('playBtn').textContent = autoPlay ? '暂停播放' : '自动播放';
    $('nextDayBtn').disabled = state.mode !== 'real' || real.loading || real.cursor >= real.items.length - 1;
    $('playBtn').disabled = state.mode !== 'real' || real.loading || real.cursor >= real.items.length - 1;
    $('dataStatus').textContent = state.mode === 'real' ? (real.loading ? '读取中…' : real.items[real.cursor] ? `历史回放 · ${real.items[real.cursor].date}` : '尚未载入数据') : '每 1.5 秒更新';
    $('chartLegend').textContent = state.mode === 'real' ? 'Frankfurter · 历史日度参考汇率' : '电脑生成 · 模拟 K 线';
    $('chartDate').textContent = state.mode === 'real' ? `${real.cursor + 1} / ${real.items.length} 个交易日` : '实时模拟';
    $('chartLoading').classList.toggle('hidden', !real.loading);
  }
  function render() { renderPairs(); renderAccount(); renderOrder(); renderPositions(); renderHistory(); renderChartHeader(); drawChart(); }

  function openPosition(side) {
    const margin = Number($('marginInput').value);
    if (!Number.isFinite(margin) || margin < 10) { notify('保证金至少为 $10。'); return null; }
    if (margin > state.cash + .001) { notify('可用余额不足，请减少保证金或申请贷款。'); return null; }
    const entry = currentPrice(state.pair);
    if (!Number.isFinite(entry)) { notify('行情尚未准备好，请稍后再试。'); return null; }
    state.margin = margin;
    state.cash = Math.max(0, state.cash - margin);
    const position = { id: `${Date.now()}-${Math.random()}`, pair: state.pair, side, entry, margin, leverage: state.leverage, opened: new Date().toLocaleString('zh-CN') };
    state.positions.unshift(position);
    save(); render(); notify(`${state.pair} ${side === 1 ? '做多' : '做空'}开仓成功。`);
    return position;
  }
  function closePosition(id, liquidated) {
    const index = state.positions.findIndex(p => p.id === id);
    if (index < 0) return;
    const pos = state.positions[index];
    const rawPnl = positionPnl(pos);
    const pnl = Math.max(-pos.margin, rawPnl);
    state.cash += Math.max(0, pos.margin + pnl);
    state.positions.splice(index, 1);
    state.history.unshift({ pair: pos.pair, side: pos.side, pnl, liquidated, time: new Date().toLocaleDateString('zh-CN') });
    state.history = state.history.slice(0, 25);
    save(); render();
    openModal({ icon: liquidated ? '✕' : pnl >= 0 ? '✦' : '♡', loss: liquidated || pnl < 0,
      title: liquidated ? '哎呀，仓位爆掉了' : pnl >= 0 ? '平仓成功，漂亮！' : '已经平仓，休息一下',
      message: liquidated ? `${pos.pair} 的亏损触及保证金的 80%，系统已自动平仓。` : `${pos.pair} ${pos.side === 1 ? '做多' : '做空'}仓位已结算，盈亏计入可用余额。`,
      detail: `${liquidated ? '本次爆仓盈亏' : '本次交易盈亏'}  ${signedMoney(pnl)}` });
  }
  function checkLiquidations() {
    const hit = state.positions.filter(p => positionPnl(p) <= -p.margin * .8);
    hit.forEach(p => closePosition(p.id, true));
  }
  function tickSim() {
    PAIRS.forEach((pair, index) => {
      const series = sim[pair.id]; const open = series.price;
      const drift = Math.sin(Date.now() / 13000 + index * 2) * .00036;
      const shock = Math.random() < .018 ? (Math.random() - .5) * .019 : 0;
      const close = Math.max(open * .5, open * (1 + drift + (Math.random() - .5) * .0042 + shock));
      const wick = open * (Math.random() * .0007 + .00015);
      series.candles.push({ open, high: Math.max(open, close) + wick, low: Math.min(open, close) - wick, close });
      if (series.candles.length > 150) series.candles.shift();
      series.price = close;
    });
    if (state.mode === 'sim') { checkLiquidations(); render(); }
    save();
  }
  async function loadReal() {
    if (real.items.length) return true;
    real.loading = true; renderChartHeader();
    const request = ++requestCounter;
    try {
      const since = new Date(); since.setUTCDate(since.getUTCDate() - 190);
      const start = since.toISOString().slice(0, 10);
      const response = await fetch(`https://api.frankfurter.dev/v1/${start}..?base=EUR&symbols=USD,GBP,JPY,CHF,AUD,CAD`, { signal: AbortSignal.timeout(12000) });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (request !== requestCounter) return false;
      const items = Object.entries(data.rates || {}).sort(([a], [b]) => a.localeCompare(b)).map(([date, r]) => {
        if (!['USD', 'GBP', 'JPY', 'CHF', 'AUD', 'CAD'].every(key => Number.isFinite(r[key]) && r[key] > 0)) return null;
        return { date, prices: { 'EUR/USD': r.USD, 'GBP/USD': r.USD / r.GBP, 'USD/JPY': r.JPY / r.USD, 'USD/CHF': r.CHF / r.USD, 'AUD/USD': r.USD / r.AUD, 'USD/CAD': r.CAD / r.USD } };
      }).filter(Boolean);
      if (items.length < 30) throw new Error('历史数据不足');
      const savedCursor = state.lastRealDate ? items.findIndex(item => item.date === state.lastRealDate) : -1;
      real = { items, cursor: savedCursor >= 0 ? savedCursor : Math.max(29, items.length - 25), loading: false, error: '' };
      return true;
    } catch (error) {
      if (request !== requestCounter) return false;
      real.loading = false; real.error = error.message || '网络错误';
      notify('真实历史数据读取失败，请检查网络后重试。'); return false;
    }
  }
  async function setMode(mode) {
    if (state.mode === mode) {
      if (mode === 'real' && !real.items.length && !real.loading) { await loadReal(); render(); }
      return;
    }
    if (state.positions.length) return notify('请先平掉当前持仓，再切换行情模式。');
    autoPlay = false;
    if (mode === 'real') { const ok = await loadReal(); if (!ok) { render(); return; } }
    state.mode = mode;
    if (mode === 'real') state.lastRealDate = real.items[real.cursor].date;
    save(); render(); notify(mode === 'real' ? '已切换为真实历史汇率回放。' : '已切换为电脑生成行情。');
  }
  function nextDay() {
    if (state.mode !== 'real' || real.cursor >= real.items.length - 1) { autoPlay = false; renderChartHeader(); return; }
    real.cursor++; state.lastRealDate = real.items[real.cursor].date; checkLiquidations(); save(); render();
    if (real.cursor >= real.items.length - 1) { autoPlay = false; renderChartHeader(); notify('已经到达最新可用交易日。'); }
  }

  function drawChart() {
    const canvas = $('chart'); const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(rect.width * dpr); canvas.height = Math.round(rect.height * dpr);
    const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
    const w = rect.width, h = rect.height;
    ctx.fillStyle = '#24383c'; ctx.fillRect(0, 0, w, h);
    const left = 16, right = w - 58, top = 23, bottom = h - 28;
    const realRows = state.mode === 'real' ? real.items.slice(Math.max(0, real.cursor - 54), real.cursor + 1) : [];
    const candles = state.mode === 'sim' ? sim[state.pair].candles.slice(-54) : [];
    const values = state.mode === 'real' ? realRows.map(r => r.prices[state.pair]) : candles.flatMap(c => [c.high, c.low]);
    if (values.length < 2) return;
    let min = Math.min(...values), max = Math.max(...values);
    const pad = Math.max((max - min) * .18, min * .0006); min -= pad; max += pad;
    const yOf = value => top + (max - value) / (max - min) * (bottom - top);
    ctx.strokeStyle = '#3c5254'; ctx.lineWidth = 1;
    ctx.fillStyle = '#91a9a8'; ctx.font = '10px DM Mono, monospace'; ctx.textAlign = 'left';
    for (let i = 0; i < 5; i++) {
      const y = top + i * (bottom - top) / 4;
      ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
      ctx.fillText(priceText(state.pair, max - i * (max - min) / 4), right + 7, y + 3);
    }
    for (let i = 0; i < 6; i++) {
      const x = left + i * (right - left) / 5;
      ctx.strokeStyle = '#32494b'; ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke();
    }
    if (state.mode === 'real') {
      const gradient = ctx.createLinearGradient(0, top, 0, bottom); gradient.addColorStop(0, '#94dcc188'); gradient.addColorStop(1, '#94dcc100');
      ctx.beginPath(); realRows.forEach((row, i) => { const x = left + i / (realRows.length - 1) * (right - left); const y = yOf(row.prices[state.pair]); if (!i) ctx.moveTo(x, y); else ctx.lineTo(x, y); });
      ctx.lineTo(right, bottom); ctx.lineTo(left, bottom); ctx.closePath(); ctx.fillStyle = gradient; ctx.fill();
      ctx.beginPath(); realRows.forEach((row, i) => { const x = left + i / (realRows.length - 1) * (right - left); const y = yOf(row.prices[state.pair]); if (!i) ctx.moveTo(x, y); else ctx.lineTo(x, y); });
      ctx.strokeStyle = '#9fe1bb'; ctx.lineWidth = 2.5; ctx.lineJoin = 'round'; ctx.stroke();
      ctx.fillStyle = '#b6f3cc'; ctx.beginPath(); ctx.arc(right, yOf(realRows.at(-1).prices[state.pair]), 4, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = '#7f9999'; ctx.font = '10px DM Mono, monospace'; ctx.fillText(realRows[0].date.slice(5), left, h - 9); ctx.textAlign = 'right'; ctx.fillText(realRows.at(-1).date.slice(5), right, h - 9);
    } else {
      const step = (right - left) / candles.length;
      candles.forEach((c, i) => {
        const x = left + step * (i + .5); const rising = c.close >= c.open;
        ctx.strokeStyle = rising ? '#8fddb1' : '#f19aa8'; ctx.fillStyle = rising ? '#8fddb1' : '#f19aa8';
        ctx.lineWidth = 1.3; ctx.beginPath(); ctx.moveTo(x, yOf(c.high)); ctx.lineTo(x, yOf(c.low)); ctx.stroke();
        const yTop = yOf(Math.max(c.open, c.close)), yBottom = yOf(Math.min(c.open, c.close));
        ctx.fillRect(x - Math.max(2, step * .3), yTop, Math.max(4, step * .6), Math.max(2, yBottom - yTop));
      });
      ctx.fillStyle = '#7f9999'; ctx.font = '10px DM Mono, monospace'; ctx.fillText('← 过去', left, h - 9); ctx.textAlign = 'right'; ctx.fillText('现在 →', right, h - 9);
    }
    const p = currentPrice(state.pair); const y = yOf(p);
    ctx.setLineDash([4, 5]); ctx.strokeStyle = '#d5e1b3'; ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke(); ctx.setLineDash([]);
  }

  $('longBtn').addEventListener('click', () => openPosition(1));
  $('shortBtn').addEventListener('click', () => openPosition(-1));
  $('simMode').addEventListener('click', () => setMode('sim'));
  $('realMode').addEventListener('click', () => setMode('real'));
  $('nextDayBtn').addEventListener('click', nextDay);
  $('playBtn').addEventListener('click', () => { autoPlay = !autoPlay; renderChartHeader(); });
  $('leverageRange').addEventListener('input', e => { state.leverage = Number(e.target.value); save(); renderOrder(); });
  $('marginInput').addEventListener('input', e => { state.margin = Number(e.target.value) || 0; save(); $('notional').textContent = money(state.margin * state.leverage); });
  $('maxBtn').addEventListener('click', () => { state.margin = Math.floor(state.cash * 100) / 100; save(); renderOrder(); });
  $('historyToggle').addEventListener('click', () => { historyExpanded = !historyExpanded; renderHistory(); });
  $('borrowBtn').addEventListener('click', () => {
    if (state.debt > 0) return notify('请先偿还当前贷款，再申请新贷款。');
    openModal({ icon: '$', title: '申请周转贷款', message: '最多可借 $5,000，借入时计入 1% 一次性费用。', detail: '借款会增加可用余额与待还贷款', input: { label: '借款金额（最多 $5,000）', value: 1000, max: 5000 }, confirm: '确认借入', action: amount => { if (!Number.isFinite(amount) || amount < 1 || amount > 5000) return notify('请输入 $1–$5,000 的借款金额。'); state.cash += amount; state.debt += amount * 1.01; save(); render(); closeModal(); notify(`已借入 ${money(amount)}。`); } });
  });
  $('repayBtn').addEventListener('click', () => {
    if (state.debt <= 0) return notify('现在没有待还贷款。');
    if (state.cash < 1) return notify('可用余额不足，暂时无法还款。');
    const max = Math.min(state.debt, state.cash);
    openModal({ icon: '♡', title: '偿还贷款', message: `当前待还 ${money(state.debt)}，可用余额 ${money(state.cash)}。`, input: { label: '还款金额', value: Math.floor(max * 100) / 100, max }, confirm: '确认还款', action: amount => { if (!Number.isFinite(amount) || amount < 1 || amount > max + .001) return notify('还款金额超出可用范围。'); state.cash = Math.max(0, state.cash - amount); state.debt = Math.max(0, state.debt - amount); save(); render(); closeModal(); notify(`已偿还 ${money(amount)}。`); } });
  });
  $('resetBtn').addEventListener('click', () => openModal({ icon: '↺', title: '重新开始？', message: '账户余额、持仓、贷款和交易记录都会重置。', confirm: '确认重置', action: () => { state = initialState(); real = { items: [], cursor: 0, loading: false, error: '' }; autoPlay = false; savedSim = null; seedSim(); save(); closeModal(); render(); notify('新的一局开始了。'); } }));
  $('modalConfirm').addEventListener('click', () => { if (modalAction) modalAction(Number($('modalInput').value)); else closeModal(); });
  $('modalCancel').addEventListener('click', closeModal);
  $('modalClose').addEventListener('click', closeModal);
  $('modal').addEventListener('click', e => { if (e.target === $('modal')) closeModal(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && modalOpen) closeModal(); if (e.key === 'Enter' && modalOpen && e.target === $('modalInput')) $('modalConfirm').click(); });
  window.addEventListener('resize', drawChart);
  setInterval(() => { tickSim(); if (autoPlay && state.mode === 'real') nextDay(); }, 1500);
  if (document.modelContext?.registerTool) {
    const tools = [
      { name: 'read_fx_game_state', title: '读取游戏状态', description: '查看账户余额、模式、货币对价格和持仓，供后续游戏操作使用。', inputSchema: { type: 'object', properties: {}, additionalProperties: false }, annotations: { readOnlyHint: true }, execute() { return { mode: state.mode, date: state.mode === 'real' ? real.items[real.cursor]?.date : null, cash: state.cash, debt: state.debt, equity: account().equity, prices: Object.fromEntries(PAIRS.map(p => [p.id, currentPrice(p.id)])), positions: state.positions.map(p => ({ id: p.id, pair: p.pair, side: p.side === 1 ? 'long' : 'short', margin: p.margin, leverage: p.leverage, entry: p.entry, pnl: positionPnl(p) })) }; } },
      { name: 'place_fx_trade', title: '开仓', description: '按当前游戏价格做多或做空，扣除保证金并更新可见持仓。', inputSchema: { type: 'object', properties: { pair: { type: 'string', enum: PAIRS.map(p => p.id) }, side: { type: 'string', enum: ['long', 'short'] }, margin: { type: 'number', minimum: 10 }, leverage: { type: 'integer', minimum: 1, maximum: 100 } }, required: ['pair', 'side', 'margin', 'leverage'], additionalProperties: false }, annotations: { readOnlyHint: false }, execute(input) { if (!PAIRS.some(p => p.id === input.pair) || !['long', 'short'].includes(input.side) || !Number.isFinite(input.margin) || input.margin < 10 || input.margin > state.cash || !Number.isInteger(input.leverage) || input.leverage < 1 || input.leverage > 100) throw new Error('开仓参数无效或可用余额不足'); state.pair = input.pair; state.margin = input.margin; state.leverage = input.leverage; render(); const pos = openPosition(input.side === 'long' ? 1 : -1); if (!pos) throw new Error('行情尚未准备好'); return { id: pos.id, pair: pos.pair, side: input.side, entry: pos.entry, remainingCash: state.cash }; } },
      { name: 'close_fx_trade', title: '平仓', description: '按当前游戏价格结算指定持仓，并显示平仓弹窗。', inputSchema: { type: 'object', properties: { id: { type: 'string' } }, required: ['id'], additionalProperties: false }, annotations: { readOnlyHint: false }, execute(input) { const pos = state.positions.find(p => p.id === input.id); if (!pos) throw new Error('找不到该持仓'); const pnl = Math.max(-pos.margin, positionPnl(pos)); closePosition(pos.id, false); return { id: pos.id, pnl, cash: state.cash }; } },
      { name: 'advance_real_fx_day', title: '推进交易日', description: '在真实历史数据模式中回放下一个交易日，并结算触发的爆仓。', inputSchema: { type: 'object', properties: {}, additionalProperties: false }, annotations: { readOnlyHint: false }, execute() { if (state.mode !== 'real' || real.cursor >= real.items.length - 1) throw new Error('当前无法推进交易日'); nextDay(); return { date: real.items[real.cursor].date, prices: real.items[real.cursor].prices, equity: account().equity }; } }
    ];
    tools.forEach(tool => { try { Promise.resolve(document.modelContext.registerTool(tool)).catch(() => {}); } catch (_) {} });
  }
  render();
  if (state.mode === 'real') loadReal().then(ok => { if (ok) render(); });
})();
