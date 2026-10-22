(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const RMB = n => `${Number(n) < 0 ? '-' : ''}¥${Math.abs(Number(n) || 0).toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
  const plain = n => Math.round(Number(n) || 0).toLocaleString('zh-CN');
  const esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const BACKEND = new URL(window.A_SHARE_BACKEND_URL || './', location.href);
  const API = new URL('api/', BACKEND);
  const WS = new URL('ws', BACKEND);
  WS.protocol = BACKEND.protocol === 'https:' ? 'wss:' : 'ws:';
  const CLOUD_KEY = 'a_share_online_session_v1';
  const LOCAL_KEY = 'a-share-local-session-v1';
  const SETTINGS_KEY = 'a-share-settings-v1';
  const WELCOME_KEY = 'a-share-welcome-v1';
  const CHART_LABELS = {sec:'1 秒', hour:'1 小时', fiveHour:'5 小时', day:'1 天'};
  const isToy = location.hostname.endsWith('bilibilitoy.com');
  let sessionToken = '', unsavedToken = '', cloudMode = false, loginRequired = false;
  let snapshot = null, selectedStock = '300059', orderType = 'market';
  let socket = null, reconnectTimer = null, retryTimer = null, toastTimer = null;
  let firstSnapshot = true, lastTradeAt = 0, actionPending = false, closingPositionId = null;
  let audioContext = null, lastPortrait = null;
  let cloudFailureCount = 0, cloudRetryAt = 0;
  let chartFrame = 'sec', chartCandles = [], chartEnd = 0, chartLive = true;
  let chartHasMore = false, chartKey = '', chartRequestId = 0;
  let chartLoading = false, chartOlderLoading = false, lastChartFetchAt = 0;
  const settings = Object.assign({soundOn:true, volume:70, theme:'day', colors:'cn'}, (() => {
    try { return JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}; } catch (_) { return {}; }
  })());

  function saveSettings() {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    applySettings();
  }
  function applySettings() {
    settings.theme = settings.theme === 'night' ? 'night' : 'day';
    settings.colors = settings.colors === 'global' ? 'global' : 'cn';
    settings.volume = Math.max(0, Math.min(100, Number(settings.volume) || 0));
    document.documentElement.dataset.theme = settings.theme;
    document.documentElement.dataset.colors = settings.colors;
    $('themeColor').content = settings.theme === 'night' ? '#142023' : '#f2f5f2';
    $('soundOn').checked = !!settings.soundOn;
    $('volume').value = settings.volume;
    $('volumeLabel').textContent = `${settings.volume}%`;
    document.querySelectorAll('[data-theme]').forEach(el => el.classList.toggle('active', el.dataset.theme === settings.theme));
    document.querySelectorAll('[data-colors]').forEach(el => el.classList.toggle('active', el.dataset.colors === settings.colors));
    if (snapshot) render(snapshot);
  }
  function sound(kind='tap') {
    if (!settings.soundOn || !settings.volume) return;
    try {
      audioContext ||= new (window.AudioContext || window.webkitAudioContext)();
      if (audioContext.state === 'suspended') audioContext.resume();
      const t = audioContext.currentTime, osc = audioContext.createOscillator();
      const gain = audioContext.createGain();
      osc.type = kind === 'loss' ? 'triangle' : 'sine';
      osc.frequency.setValueAtTime(kind === 'loss' ? 240 : kind === 'trade' ? 660 : 440, t);
      osc.frequency.exponentialRampToValueAtTime(kind === 'loss' ? 120 : kind === 'trade' ? 880 : 530, t + .13);
      gain.gain.setValueAtTime(.0001, t);
      gain.gain.exponentialRampToValueAtTime(.07 * settings.volume / 100, t + .015);
      gain.gain.exponentialRampToValueAtTime(.0001, t + .18);
      osc.connect(gain).connect(audioContext.destination);
      osc.start(t); osc.stop(t + .19);
    } catch (_) {}
  }
  function toast(message) {
    $('toast').textContent = message;
    $('toast').classList.add('visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $('toast').classList.remove('visible'), 4200);
  }
  function connection(connected) {
    $('connection').textContent = connected ? '● 已联机' : '○ 重连中';
    $('connection').classList.toggle('offline', !connected);
    $('longBtn').disabled = !connected || actionPending;
    $('shortBtn').disabled = !connected || actionPending;
    document.querySelectorAll('.close-button,.limit-close-button,.cancel-button').forEach(
      b => b.disabled = !connected || actionPending);
  }
  async function post(path, body={}) {
    const headers = {'Content-Type':'application/json', 'X-A-Share-Action':'1'};
    if (sessionToken) headers.Authorization = `Bearer ${sessionToken}`;
    const response = await fetch(new URL(path, API), {
      method:'POST', credentials:'omit', headers, body:JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || '操作失败，请稍后再试。');
    return data;
  }
  async function loadCredential() {
    if (window.toy && typeof window.toy.getCloudStorage === 'function') {
      try {
        const data = await window.toy.getCloudStorage([CLOUD_KEY]);
        cloudMode = true;
        sessionToken = data[CLOUD_KEY] || unsavedToken || '';
        $('bindStatus').textContent = '已关联云存档';
        return;
      } catch (error) {
        if (isToy) {
          loginRequired = true;
          if (error?.code === 307044) {
            cloudRetryAt = Date.now() + Math.min(300000, 30000 * 2 ** cloudFailureCount++);
            throw new Error('云存档请求频繁，请稍后重试。');
          }
          throw new Error('请先登录 B站以关联联机账户。');
        }
      }
    }
    if (isToy) { loginRequired = true; throw new Error('B站云存档暂不可用，请稍后重试。'); }
    sessionToken = localStorage.getItem(LOCAL_KEY) || '';
    $('bindStatus').textContent = '本地调试';
  }
  async function saveCredential() {
    if (cloudMode) await window.toy.setCloudStorage({[CLOUD_KEY]:sessionToken});
    else localStorage.setItem(LOCAL_KEY, sessionToken);
  }
  async function init() {
    clearTimeout(retryTimer);
    try {
      loginRequired = false;
      if (cloudRetryAt > Date.now()) {
        loginRequired = true;
        throw new Error('云存档请求频繁，请稍后重试。');
      }
      await loadCredential();
      const initial = await post('session');
      const newToken = initial.sessionToken;
      if (newToken !== sessionToken || (cloudMode && unsavedToken)) {
        sessionToken = newToken;
        try {
          await saveCredential();
          unsavedToken = '';
          cloudFailureCount = 0;
        } catch (error) {
          unsavedToken = sessionToken;
          loginRequired = cloudMode;
          if (error?.code === 307044) {
            cloudRetryAt = Date.now() + Math.min(300000, 30000 * 2 ** cloudFailureCount++);
            throw new Error('云存档请求频繁，请稍后重试。');
          }
          throw new Error(cloudMode ? '云存档暂时无法保存联机账户，请稍后重试。' : '本地账号保存失败。');
        }
      }
      if ($('loginDialog').open) $('loginDialog').close();
      render(initial);
      if (!localStorage.getItem(WELCOME_KEY) && !$('welcomeDialog').open)
        $('welcomeDialog').showModal();
      connect();
    } catch (error) {
      connection(false);
      toast(error.message);
      if (loginRequired) {
        $('loginDialog').querySelector('h2').textContent = error.message.includes('频繁') ? '云存档繁忙' : '请先登录 B站';
        $('loginDialog').querySelector('p').textContent = error.message;
        if (!$('loginDialog').open) $('loginDialog').showModal();
      } else retryTimer = setTimeout(init, 4500);
    }
  }
  function connect() {
    clearTimeout(reconnectTimer);
    if (socket) socket.close();
    socket = new WebSocket(WS);
    socket.onopen = () => socket.send(JSON.stringify({sessionToken}));
    socket.onmessage = event => {
      try { connection(true); render(JSON.parse(event.data)); }
      catch (error) { console.error(error); }
    };
    socket.onclose = () => {
      connection(false);
      if (sessionToken) reconnectTimer = setTimeout(connect, 2500);
    };
    socket.onerror = () => socket.close();
  }
  function stockInfo(stock) { return snapshot?.stocks.find(s => s.id === stock); }
  function percentChange(quote) {
    const price = Number(quote?.price), previous = Number(quote?.previous);
    return Number.isFinite(price) && price > 0 && Number.isFinite(previous) && previous > 0
      ? (price / previous - 1) * 100 : null;
  }
  function selectStock(stock) {
    if (!snapshot?.markets[stock]) return;
    selectedStock = stock;
    if (orderType === 'limit') $('limitPrice').value = snapshot.markets[stock].price.toFixed(2);
    render(snapshot);
  }
  function render(data) {
    if (!data || data.type !== 'snapshot') return;
    snapshot = data;
    if (!data.markets[selectedStock]) selectedStock = data.stocks[0].id;
    const account = data.account, stock = stockInfo(selectedStock);
    $('equity').textContent = RMB(account.equity);
    $('cash').textContent = RMB(account.available);
    $('cash').title = `现金 ${RMB(account.cash)} · 委托占用 ${RMB(account.reserved)}`;
    $('floating').textContent = RMB(account.floating);
    $('floating').className = account.floating >= 0 ? 'up' : 'down';
    $('onlineCount').textContent = data.online;
    $('botCount').textContent = data.bots;
    $('blowCount').textContent = data.liquidations.toLocaleString('zh-CN');
    $('nickname').textContent = `${account.nickname} ✎`;
    $('resetCount').textContent = account.resetRemaining;
    $('resetBtn').title = account.resetRemaining > 0 ? `今天还可重启 ${account.resetRemaining} 次` : '今天的机会已用完，明天 00:00（北京时间）恢复';
    $('positionCount').textContent = account.positions.length;
    $('marginUsed').textContent = `保证金 ${RMB(account.margin)}`;
    $('stockTabs').innerHTML = data.stocks.map(s => {
      const change = percentChange(data.markets[s.id]);
      return `<button class="stock-tab ${s.id === selectedStock ? 'active' : ''}" data-stock="${s.id}" type="button"><span><b>${s.name}</b><small>${s.id}</small></span><span class="${change === null ? '' : change >= 0 ? 'up' : 'down'}">${change === null ? '—' : `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`}</span></button>`;
    }).join('');
    const quote = data.markets[selectedStock], book = data.orderBooks[selectedStock];
    const change = percentChange(quote);
    $('stockTitle').innerHTML = `${esc(stock.name)} <small>${stock.id}</small>`;
    $('orderStock').textContent = `${stock.name} · ${stock.id}`;
    $('price').textContent = RMB(quote.price);
    $('change').textContent = change === null ? '—' : `${change >= 0 ? '+' : ''}${change.toFixed(3)}%`;
    $('change').className = change === null ? '' : change >= 0 ? 'up' : 'down';
    $('bestBid').textContent = book.bids[0] ? RMB(book.bids[0].price) : '—';
    $('bestAsk').textContent = book.asks[0] ? RMB(book.asks[0].price) : '—';
    renderBook(book);
    renderPositions(account.positions, data.markets);
    renderOrders(account.orders);
    renderRanking(data.leaderboard);
    renderLog(account.trades);
    const nextChartKey = selectedStock + ':' + chartFrame;
    if (nextChartKey !== chartKey) {
      chartKey = nextChartKey;
      chartRequestId++;
      chartCandles = []; chartEnd = 0; chartLive = true;
      chartHasMore = false; chartLoading = false; chartOlderLoading = false;
      lastChartFetchAt = 0;
      refreshChart();
    } else if (chartLive && Date.now() - lastChartFetchAt >= 1900) {
      refreshChart();
    }
    drawChart();
    previewOrder();
    const newest = account.trades[0];
    if (firstSnapshot) { lastTradeAt = newest?.at || 0; firstSnapshot = false; }
    else if (newest && newest.at > lastTradeAt) {
      lastTradeAt = newest.at;
      $('eventIcon').textContent = newest.reason === '爆仓' ? '💥' : newest.pnl >= 0 ? '✿' : '♡';
      $('eventTitle').textContent = newest.reason === '爆仓' ? '仓位爆仓了' : '平仓成交';
      $('eventText').textContent = `${stockInfo(newest.stock)?.name || newest.stock} · ${newest.qty} 股 · 成交价 ${RMB(newest.price)} · 盈亏 ${newest.pnl >= 0 ? '+' : ''}${RMB(newest.pnl)}`;
      sound(newest.pnl >= 0 ? 'trade' : 'loss');
      if (!$('eventDialog').open) $('eventDialog').showModal();
    }
  }
  function renderBook(book) {
    $('bookCount').textContent = `${book.bids.length} 买 / ${book.asks.length} 卖`;
    for (const [id, rows] of [['bids',book.bids],['asks',book.asks]]) {
      $(id).innerHTML = rows.length ? rows.map((r, i) =>
        `<div class="book-row"><span>${i + 1}</span><strong class="${id === 'bids' ? 'up' : 'down'}">${RMB(r.price)}</strong><b>${plain(r.qty)} 股</b><small>${r.owner}</small></div>`
      ).join('') : '<div class="empty">暂无委托</div>';
    }
  }
  function renderPositions(positions, markets) {
    $('positions').innerHTML = positions.length ? positions.map(p => {
      const name = stockInfo(p.stock)?.name || p.stock;
      const risk = -p.pnl / p.margin;
      return `<div class="position"><div class="position-main"><strong>${esc(name)} <small>${p.stock}</small> <i class="${p.side === 1 ? 'long-tag' : 'short-tag'}">${p.side === 1 ? '做多' : '做空'}</i></strong><span>${p.qty} 股 · ${p.leverage}× · 保证金 ${RMB(p.margin)}</span></div><div class="position-pnl"><b class="${p.pnl >= 0 ? 'up' : 'down'}">${p.pnl >= 0 ? '+' : ''}${RMB(p.pnl)}</b><small>开 ${RMB(p.entry)} · 现 ${RMB(markets[p.stock].price)}</small></div><div class="position-actions"><button class="close-button" data-id="${p.id}" type="button">市价平仓</button><button class="limit-close-button" data-id="${p.id}" type="button">限价</button></div>${risk >= .6 ? '<div class="risk-note">接近爆仓，请留意风险</div>' : ''}</div>`;
    }).join('') : '<div class="empty">暂无持仓。选一只股票下单试试 ♡</div>';
    connection(socket?.readyState === WebSocket.OPEN);
  }
  function renderOrders(orders) {
    const pending = orders.filter(o => o.status === 'open').length;
    $('myOrderCount').textContent = `待成交 ${pending}`;
    $('myOrders').innerHTML = orders.length ? orders.map(o => {
      const state = o.status === 'open' ? '待成交' : o.status === 'filled' ? '已成交' : '已撤销';
      const action = o.action === 'close' ? '平仓' : o.direction === 1 ? '买入' : '卖出';
      return `<div class="my-order"><span><b>${esc(stockInfo(o.stock)?.name || o.stock)} · ${action}</b><small>${o.orderType === 'market' ? '市价' : RMB(o.price)} · 已成交 ${o.quantity - o.remaining}/${o.quantity} 股</small></span><span class="order-state">${state}</span>${o.status === 'open' ? `<button class="cancel-button" data-id="${o.id}" type="button">撤单</button>` : ''}</div>`;
    }).join('') : '<div class="empty">还没有委托。</div>';
    connection(socket?.readyState === WebSocket.OPEN);
  }
  function renderRanking(rows) {
    $('leaderboard').innerHTML = rows.map((r, i) =>
      `<div class="rank-row ${r.mine ? 'mine' : ''}"><span>${i + 1}</span><b>${esc(r.nickname)}${r.mine ? ' · 我' : ''}</b><strong>${RMB(r.equity)}</strong></div>`
    ).join('') || '<div class="empty">暂无玩家上榜。</div>';
  }
  function renderLog(rows) {
    $('tradeLog').innerHTML = rows.map(r =>
      `<div class="log-row"><span>${esc(stockInfo(r.stock)?.name || r.stock)} · ${r.reason} · ${r.qty} 股</span><strong class="${r.pnl >= 0 ? 'up' : 'down'}">${r.pnl >= 0 ? '+' : ''}${RMB(r.pnl)}</strong></div>`
    ).join('') || '<div class="empty">本轮还没有结算记录。</div>';
  }
  function candleUrl(before, limit=240) {
    const url = new URL('candles', API);
    url.searchParams.set('stock', selectedStock);
    url.searchParams.set('timeframe', chartFrame);
    url.searchParams.set('limit', String(limit));
    if (before != null) url.searchParams.set('before', String(before));
    return url;
  }
  async function readCandles(before, limit=240) {
    const response = await fetch(candleUrl(before, limit), {credentials:'omit'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || 'K线读取失败。');
    return data;
  }
  async function refreshChart() {
    if (chartLoading || !chartLive) return;
    chartLoading = true;
    lastChartFetchAt = Date.now();
    const key = chartKey, requestId = chartRequestId;
    try {
      const data = await readCandles(null, 240);
      if (key !== chartKey || requestId !== chartRequestId) return;
      const wasEmpty = !chartCandles.length;
      const merged = new Map(chartCandles.map(bar => [bar.t, bar]));
      data.candles.forEach(bar => merged.set(bar.t, bar));
      chartCandles = [...merged.values()].sort((a, b) => a.t - b.t);
      if (chartCandles.length > 1200) {
        chartCandles = chartCandles.slice(-1200);
        chartHasMore = true;
      }
      if (wasEmpty) chartHasMore = data.hasMore;
      chartEnd = chartCandles.length;
      drawChart();
    } catch (error) {
      if (key === chartKey) $('chartStatus').textContent = 'K线读取失败';
    } finally {
      if (key === chartKey && requestId === chartRequestId) chartLoading = false;
    }
  }
  async function loadOlderCandles() {
    if (chartOlderLoading || !chartHasMore || !chartCandles.length) return;
    chartOlderLoading = true;
    const key = chartKey, requestId = chartRequestId;
    const before = chartCandles[0].t;
    try {
      const data = await readCandles(before);
      if (key !== chartKey || requestId !== chartRequestId) return;
      const seen = new Set(chartCandles.map(bar => bar.t));
      const older = data.candles.filter(bar => !seen.has(bar.t));
      chartCandles = older.concat(chartCandles);
      chartEnd += older.length;
      chartHasMore = data.hasMore && older.length > 0;
      drawChart();
    } catch (error) {
      if (key === chartKey) toast(error.message);
    } finally {
      if (key === chartKey && requestId === chartRequestId) chartOlderLoading = false;
    }
  }
  function chartDate(timestamp) {
    if (!timestamp) return '—';
    const date = new Date(timestamp * 1000);
    if (chartFrame === 'sec')
      return date.toLocaleTimeString('zh-CN', {hour12:false});
    if (chartFrame === 'day')
      return date.toLocaleDateString('zh-CN', {month:'2-digit', day:'2-digit'});
    return `${date.getMonth()+1}/${date.getDate()} ${String(date.getHours()).padStart(2,'0')}:00`;
  }
  function drawChart() {
    const canvas = $('chart'), box = canvas.parentElement;
    const width = box.clientWidth, height = box.clientHeight;
    if (!width || !height) return;
    const dpr = Math.min(devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr);
    const style = getComputedStyle(document.documentElement);
    const up = style.getPropertyValue('--up').trim(), down = style.getPropertyValue('--down').trim();
    ctx.fillStyle = '#203236'; ctx.fillRect(0, 0, width, height);
    const count = chartCandles.length;
    chartEnd = Math.max(0, Math.min(chartEnd, count));
    const slider = $('timeSlider');
    slider.max = String(Math.max(1, count));
    slider.value = String(Math.max(1, chartEnd));
    slider.disabled = count < 2;
    $('goLiveBtn').hidden = chartLive;
    $('chartFootLabel').textContent = `${CHART_LABELS[chartFrame]} K 线 · 成交才改变价格`;
    $('chartStatus').textContent = chartLoading && !count ? '加载中' : chartLive ? '实时' : '历史';
    const windowSize = Math.max(18, Math.min(60, Math.floor(width / 10)));
    const data = chartCandles.slice(Math.max(0, chartEnd - windowSize), chartEnd);
    $('rangeStart').textContent = chartDate(data[0]?.t);
    $('rangeEnd').textContent = chartDate(data[data.length - 1]?.t);
    $('chartTime').textContent = chartDate(data[data.length - 1]?.t);
    if (!data.length) {
      ctx.fillStyle = '#91aaa9';
      ctx.font = '12px sans-serif';
      ctx.fillText('该周期的 K 线历史正在积累', 16, height / 2);
      return;
    }
    const current = data[data.length - 1].c, pad = {l:9,r:56,t:13,b:11};
    let low = Math.min(...data.map(c => c.l)), high = Math.max(...data.map(c => c.h));
    const gap = Math.max((high-low)*.15,current*.001,.01); low -= gap; high += gap;
    const y = p => pad.t + (high-p)/(high-low)*(height-pad.t-pad.b);
    const plotW = width-pad.l-pad.r, plotH = height-pad.t-pad.b;
    ctx.font = '9px monospace'; ctx.lineWidth = 1;
    for (let i=0;i<=3;i++) {
      const py = pad.t + i*plotH/3;
      ctx.strokeStyle = '#385156'; ctx.beginPath(); ctx.moveTo(pad.l,py); ctx.lineTo(width-pad.r+3,py); ctx.stroke();
      ctx.fillStyle = '#91aaa9'; ctx.fillText((high-(high-low)*i/3).toFixed(2),width-pad.r+6,py+3);
    }
    const step=plotW/data.length, bar=Math.max(2,Math.min(8,step*.62));
    data.forEach((c,i) => {
      const x=pad.l+step*(i+.5), color=c.c>=c.o?up:down;
      ctx.strokeStyle=color; ctx.fillStyle=color;
      ctx.beginPath(); ctx.moveTo(x,y(c.h)); ctx.lineTo(x,y(c.l)); ctx.stroke();
      ctx.fillRect(x-bar/2,Math.min(y(c.o),y(c.c)),bar,Math.max(2,Math.abs(y(c.o)-y(c.c))));
    });
    ctx.strokeStyle='#e6dda6'; ctx.setLineDash([4,4]); ctx.beginPath();
    ctx.moveTo(pad.l,y(current)); ctx.lineTo(width-pad.r+3,y(current)); ctx.stroke(); ctx.setLineDash([]);
  }
  function previewOrder() {
    const margin=Number($('margin').value), lev=Number($('leverage').value);
    const basis = orderType === 'limit' ? Number($('limitPrice').value) : snapshot?.markets[selectedStock]?.price || 25;
    const qty = basis > 0 ? Math.floor(margin*lev/basis) : 0;
    $('leverageLabel').textContent = `${lev}×`;
    $('shareCount').textContent = plain(qty);
    $('notional').textContent = RMB(qty*basis);
    $('fee').textContent = RMB(qty*basis*(.00008+lev*.000002));
  }
  async function doAction(path, body={}) {
    if (actionPending) return;
    actionPending=true; connection(socket?.readyState === WebSocket.OPEN);
    try {
      const result = await post(path,body);
      sound('trade'); toast(result.message);
      if (path === 'reset') { firstSnapshot=true; lastTradeAt=0; }
      return result;
    } catch (error) { sound('loss'); toast(error.message); return null; }
    finally { actionPending=false; connection(socket?.readyState === WebSocket.OPEN); }
  }
  function updateOrientation() {
    const portrait = innerHeight > innerWidth;
    $('orientationHint').hidden = !portrait || sessionStorage.getItem('a-share-orientation-dismissed') === '1';
    if (portrait !== lastPortrait) {
      for (const id of ['orderBookPanel','myOrdersPanel','rankingPanel','logPanel']) $(id).open = !portrait;
      lastPortrait = portrait;
    }
    if (snapshot) drawChart();
  }
  document.addEventListener('click', e => { if (e.target.closest('button')) sound('tap'); });
  $('stockTabs').addEventListener('click', e => {
    const button=e.target.closest('[data-stock]'); if (button) selectStock(button.dataset.stock);
  });
  document.querySelectorAll('[data-chart-frame]').forEach(button => button.addEventListener('click', () => {
    chartFrame = button.dataset.chartFrame;
    document.querySelectorAll('[data-chart-frame]').forEach(
      item => item.classList.toggle('active', item === button));
    if (snapshot) render(snapshot);
  }));
  $('timeSlider').addEventListener('input', e => {
    chartEnd = Number(e.target.value);
    chartLive = chartEnd >= chartCandles.length;
    drawChart();
    if (!chartLive && chartEnd <= 30) loadOlderCandles();
  });
  $('goLiveBtn').addEventListener('click', () => {
    chartLive = true;
    chartEnd = chartCandles.length;
    lastChartFetchAt = 0;
    refreshChart();
    drawChart();
  });
  document.querySelectorAll('[data-order-type]').forEach(button => button.addEventListener('click', () => {
    orderType=button.dataset.orderType;
    document.querySelectorAll('[data-order-type]').forEach(b => b.classList.toggle('active',b===button));
    $('limitField').hidden = orderType !== 'limit';
    if (orderType === 'limit' && snapshot) $('limitPrice').value = snapshot.markets[selectedStock].price.toFixed(2);
    previewOrder();
  }));
  for (const id of ['margin','leverage','limitPrice']) $(id).addEventListener('input',previewOrder);
  $('longBtn').addEventListener('click', () => doAction('order', {
    stock:selectedStock, side:1, margin:Number($('margin').value),
    leverage:Number($('leverage').value), orderType,
    limitPrice:orderType==='limit'?Number($('limitPrice').value):null
  }));
  $('shortBtn').addEventListener('click', () => doAction('order', {
    stock:selectedStock, side:-1, margin:Number($('margin').value),
    leverage:Number($('leverage').value), orderType,
    limitPrice:orderType==='limit'?Number($('limitPrice').value):null
  }));
  $('positions').addEventListener('click', e => {
    const market=e.target.closest('.close-button'), limit=e.target.closest('.limit-close-button');
    if (market) doAction('close',{id:market.dataset.id});
    if (limit) {
      closingPositionId=limit.dataset.id;
      const pos=snapshot.account.positions.find(p => p.id===closingPositionId);
      $('closeLimitStock').textContent = `${stockInfo(pos.stock).name} · ${pos.qty} 股`;
      $('closeLimitPrice').value = snapshot.markets[pos.stock].price.toFixed(2);
      $('closeLimitDialog').showModal();
    }
  });
  $('submitCloseLimit').addEventListener('click', async () => {
    if (!closingPositionId) return;
    const result=await doAction('close',{id:closingPositionId,orderType:'limit',
      limitPrice:Number($('closeLimitPrice').value)});
    if (result) $('closeLimitDialog').close();
  });
  $('myOrders').addEventListener('click', e => {
    const button=e.target.closest('.cancel-button');
    if (button) doAction('cancel',{id:button.dataset.id});
  });
  $('resetBtn').addEventListener('click', () => {
    if (!snapshot) { toast('账户正在连接，请稍后再试。'); return; }
    if (snapshot.account.resetRemaining <= 0) {
      toast('今天的重启人生机会已用完，明天 00:00（北京时间）恢复。');
      return;
    }
    $('resetDialogText').textContent = `确定使用今天剩余 ${snapshot.account.resetRemaining} 次机会中的一次吗？当前持仓和未成交委托会清除，账户资金恢复为 ¥10,000。`;
    if (!$('resetDialog').open) $('resetDialog').showModal();
  });
  $('confirmResetBtn').addEventListener('click', async () => {
    $('confirmResetBtn').disabled = true;
    try {
      const result = await doAction('reset');
      if (result) $('resetDialog').close();
    } finally { $('confirmResetBtn').disabled = false; }
  });
  $('nickname').addEventListener('click', async () => {
    try {
      if (window.toy?.getUserProfile) {
        const profile=await window.toy.getUserProfile();
        if (profile.nickname) { await doAction('rename',{nickname:profile.nickname.slice(0,16)}); return; }
      }
      const name=prompt('设置排行榜昵称（2–16字）',snapshot?.account.nickname||'');
      if (name?.trim()) doAction('rename',{nickname:name.trim()});
    } catch (error) { toast('未能读取B站昵称，可稍后再试。'); }
  });
  $('authorHomeBtn').addEventListener('click', async () => {
    try {
      if (window.toy?.navigate) { await window.toy.navigate({type:'space',id:'443211651'}); return; }
    } catch (_) {}
    window.open('https://space.bilibili.com/443211651','_blank','noopener');
  });
  $('settingsBtn').addEventListener('click', () => $('settingsDialog').showModal());
  $('helpBtn').addEventListener('click', () => $('helpDialog').showModal());
  $('newbieBtn').addEventListener('click', () => {
    localStorage.setItem(WELCOME_KEY, '1');
    $('welcomeDialog').close();
    $('helpDialog').showModal();
  });
  $('veteranBtn').addEventListener('click', () => {
    localStorage.setItem(WELCOME_KEY, '1');
    $('welcomeDialog').close();
  });
  document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', () => b.closest('dialog').close()));
  $('soundOn').addEventListener('change', e => {settings.soundOn=e.target.checked;saveSettings();});
  $('volume').addEventListener('input', e => {settings.volume=Number(e.target.value);saveSettings();});
  document.querySelectorAll('[data-theme]').forEach(b => b.addEventListener('click', () => {settings.theme=b.dataset.theme;saveSettings();}));
  document.querySelectorAll('[data-colors]').forEach(b => b.addEventListener('click', () => {settings.colors=b.dataset.colors;saveSettings();}));
  $('dismissOrientation').addEventListener('click', () => {sessionStorage.setItem('a-share-orientation-dismissed','1');$('orientationHint').hidden=true;});
  $('retryLogin').addEventListener('click', () => {
    if (cloudRetryAt > Date.now()) {
      toast(`请 ${Math.ceil((cloudRetryAt - Date.now()) / 1000)} 秒后重试。`);
      return;
    }
    if ($('loginDialog').open) $('loginDialog').close();
    init();
  });
  window.addEventListener('resize',updateOrientation);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && sessionToken && socket?.readyState !== WebSocket.OPEN) connect();
  });
  applySettings(); updateOrientation(); previewOrder(); init();
})();
