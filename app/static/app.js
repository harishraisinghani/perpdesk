const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const AUTO_REFRESH_MS = 5_000;
const CANDLE_RANGES = {
  "1D": { interval: "15m", limit: 96, seconds: 900 },
  "7D": { interval: "1h", limit: 168, seconds: 3_600 },
  "30D": { interval: "4h", limit: 180, seconds: 14_400 },
};
let dashboard;
let dashboardRequestId = 0;
let candleRange = "1D";
let candleRequestId = 0;
let candleCacheKey = "";
let candleLoadedAt = 0;
let priceChart;
let priceCandleSeries;
let priceVolumeSeries;
let latestPriceCandle;
let hoveredPriceCandle;
let firstPriceOpen;
let priceCandleCount = 0;

const money = (value, compact = false) => {
  if (value == null) return "—";
  if (compact) return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", notation: "compact", maximumFractionDigits: 1 }).format(value);
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: value > 1000 ? 0 : 2 }).format(value);
};
const pct = (value, digits = 1) => value == null ? "—" : `${(value * 100).toFixed(digits)}%`;
const ago = (iso) => {
  if (!iso) return "unknown";
  const seconds = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  return `${Math.round(seconds / 3600)}h ago`;
};
const safe = (value) => String(value).replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));

async function getJSON(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || `Request failed (${response.status})`);
  return response.json();
}

function path(points) { return points.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" "); }

function setPriceOhlc(row) {
  if (!row) return;
  $("#price-ohlc").textContent=`O ${money(row.open)}  H ${money(row.high)}  L ${money(row.low)}  C ${money(row.close)}`;
}

function initPriceChart() {
  if (priceChart) return true;
  const container=$("#price-chart");
  if (!window.LightweightCharts || !container) {
    $("#price-chart-status").textContent="Price chart library unavailable.";
    return false;
  }
  priceChart=LightweightCharts.createChart(container,{
    autoSize:true,
    layout:{background:{type:LightweightCharts.ColorType.Solid,color:"#ffffff"},textColor:"#69766f",fontFamily:'Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'},
    grid:{vertLines:{color:"#f0f2f0"},horzLines:{color:"#edf0ee"}},
    rightPriceScale:{borderColor:"#dfe5e1",scaleMargins:{top:.08,bottom:.24}},
    timeScale:{borderColor:"#dfe5e1",timeVisible:true,secondsVisible:false,rightOffset:3,barSpacing:7,minBarSpacing:3},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal,vertLine:{color:"#98a39d",width:1,labelBackgroundColor:"#142421"},horzLine:{color:"#98a39d",width:1,labelBackgroundColor:"#142421"}},
    handleScale:true,
    handleScroll:true,
  });
  priceCandleSeries=priceChart.addSeries(LightweightCharts.CandlestickSeries,{
    upColor:"#23705a",downColor:"#f0723c",borderVisible:false,wickUpColor:"#23705a",wickDownColor:"#f0723c",priceLineColor:"#154f3c",
  });
  priceVolumeSeries=priceChart.addSeries(LightweightCharts.HistogramSeries,{
    priceFormat:{type:"volume"},priceScaleId:"volume",lastValueVisible:false,priceLineVisible:false,
  });
  priceChart.priceScale("volume").applyOptions({scaleMargins:{top:.82,bottom:0}});
  priceChart.subscribeCrosshairMove(param=>{
    hoveredPriceCandle=param?.seriesData?.get(priceCandleSeries) || null;
    setPriceOhlc(hoveredPriceCandle || latestPriceCandle);
  });
  return true;
}

function renderPriceChange() {
  const change=firstPriceOpen && latestPriceCandle ? latestPriceCandle.close/firstPriceOpen-1 : null;
  const changeEl=$("#price-change");
  changeEl.textContent=change==null?"—":`${change>=0?"+":""}${pct(change,2)}`;
  changeEl.className=change==null?"":change>=0?"positive":"negative";
}

// Candles are fetched at most once a minute while the mark refreshes every few
// seconds, so the newest bar is re-closed on the same mark the rest of the page
// quotes. Without this the headline mark and the chart drift apart intraminute.
function syncChartToLiveMark() {
  if (!priceCandleSeries || !latestPriceCandle || !dashboard) return;
  const mark=Number(dashboard.mark_px);
  if (!Number.isFinite(mark) || candleCacheKey!==`${dashboard.coin}:${candleRange}`) return;
  const step=CANDLE_RANGES[candleRange].seconds;
  const bucket=Math.floor(Date.now()/1000/step)*step;
  latestPriceCandle = bucket>latestPriceCandle.time
    ? {time:bucket,open:mark,high:mark,low:mark,close:mark}
    : {...latestPriceCandle,close:mark,high:Math.max(latestPriceCandle.high,mark),low:Math.min(latestPriceCandle.low,mark)};
  priceCandleSeries.update(latestPriceCandle);
  if (!hoveredPriceCandle) setPriceOhlc(latestPriceCandle);
  renderPriceChange();
  renderPriceChartStatus();
}

function renderPriceChartStatus() {
  if (!candleCacheKey) return;
  const interval=CANDLE_RANGES[candleRange].interval;
  const age=dashboard?.mark_as_of?` · mark ${ago(dashboard.mark_as_of)}`:"";
  $("#price-chart-status").textContent=`${priceCandleCount} ${interval} candles · Hyperliquid · newest bar closed on the live mark${age}`;
}

function renderPriceCandles(payload) {
  const rows=payload.candles || [];
  if (!rows.length || !initPriceChart()) {
    $("#price-chart-status").textContent=rows.length?"Price chart library unavailable.":"No candles available for this range.";
    return;
  }
  const series=rows.map(({time,open,high,low,close})=>({time,open,high,low,close}));
  priceCandleSeries.setData(series);
  priceVolumeSeries.setData(rows.map(row=>({time:row.time,value:row.volume,color:row.close>=row.open?"rgba(35,112,90,.28)":"rgba(240,114,60,.24)"})));
  priceChart.timeScale().fitContent();
  firstPriceOpen=series[0].open;
  latestPriceCandle=series.at(-1);
  priceCandleCount=series.length;
  setPriceOhlc(latestPriceCandle);
  renderPriceChange();
  renderPriceChartStatus();
  syncChartToLiveMark();
}

async function loadPriceCandles(coin,{force=false}={}) {
  const config=CANDLE_RANGES[candleRange];
  const key=`${coin}:${candleRange}`;
  if (!force && key===candleCacheKey && Date.now()-candleLoadedAt<60_000) return;
  const requestId=++candleRequestId;
  $("#price-chart-coin").textContent=coin;
  $("#price-chart-status").textContent="Loading Hyperliquid candles…";
  try {
    const payload=await getJSON(`/api/candles?coin=${encodeURIComponent(coin)}&interval=${config.interval}&limit=${config.limit}`);
    if (requestId!==candleRequestId) return;
    candleCacheKey=key;
    candleLoadedAt=Date.now();
    renderPriceCandles(payload);
  } catch(error) {
    if (requestId===candleRequestId) $("#price-chart-status").textContent=error.message;
  }
}

function renderRiskChart(rows) {
  const svg = $("#risk-chart"), width = 720, height = 245, pad = {l:52,r:18,t:25,b:35};
  const max = Math.max(...rows.map(r => r.liquidatable_notional_tracked), 1);
  const x = i => pad.l + i * (width - pad.l - pad.r) / (rows.length - 1);
  const y = v => height - pad.b - v / max * (height - pad.t - pad.b);
  const points = rows.map((r, i) => [x(i), y(r.liquidatable_notional_tracked)]);
  const area = `${path(points)} L${x(rows.length-1)},${height-pad.b} L${x(0)},${height-pad.b} Z`;
  const yTicks = [0,.25,.5,.75,1].map(f => `<line class="grid-line" x1="${pad.l}" x2="${width-pad.r}" y1="${y(max*f)}" y2="${y(max*f)}"/><text class="axis-label" x="${pad.l-8}" y="${y(max*f)+3}" text-anchor="end">${money(max*f,true)}</text>`).join("");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = `<defs><linearGradient id="riskGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#f0723c" stop-opacity=".24"/><stop offset="1" stop-color="#f0723c" stop-opacity=".015"/></linearGradient></defs>${yTicks}<line class="axis-line" x1="${pad.l}" x2="${width-pad.r}" y1="${height-pad.b}" y2="${height-pad.b}"/><path class="curve-area" d="${area}"/><path class="curve-line" d="${path(points)}"/>${points.map((p,i)=>`<circle class="curve-dot ${rows[i].shock_pct===5?'active':''}" cx="${p[0]}" cy="${p[1]}" r="4"/><text class="axis-label" x="${p[0]}" y="${height-12}" text-anchor="middle">−${rows[i].shock_pct}%</text>${rows[i].shock_pct===5?`<text class="chart-value" x="${p[0]}" y="${p[1]-12}" text-anchor="middle">${money(rows[i].liquidatable_notional_tracked,true)}</text>`:""}`).join("")}`;
}

function renderHistory(points) {
  const svg = $("#history-chart"), width = 1000, height = 180, pad={l:66,r:20,t:18,b:34};
  $("#history-snapshots").textContent=points.length.toLocaleString();
  if (!points.length) {
    $("#history-latest").textContent="—"; $("#history-change").textContent="—";
    svg.setAttribute("viewBox",`0 0 ${width} ${height}`);
    svg.innerHTML=`<text class="history-empty" x="${width/2}" y="${height/2}" text-anchor="middle">No historical snapshots for this asset yet</text>`;
    return;
  }
  const normalized=points.map(p=>({...p,time:new Date(p.captured_at).getTime()})).filter(p=>Number.isFinite(p.time)&&Number.isFinite(p.notional));
  if (!normalized.length) { renderHistory([]); return; }
  const values=normalized.map(p=>p.notional), low=Math.min(...values), high=Math.max(...values), spread=high-low;
  const yMin=Math.max(0,low-(spread||high*.1||1)*.2), yMax=high+(spread||high*.1||1)*.2;
  const tMin=normalized[0].time, tMax=normalized.at(-1).time;
  const x=t=>pad.l+(t-tMin)/(tMax-tMin||1)*(width-pad.l-pad.r);
  const y=v=>pad.t+(yMax-v)/(yMax-yMin||1)*(height-pad.t-pad.b);
  const pts=normalized.map(p=>[normalized.length===1?(pad.l+width-pad.r)/2:x(p.time),y(p.notional)]);
  const area=`${path(pts)} L${pts.at(-1)[0]},${height-pad.b} L${pts[0][0]},${height-pad.b} Z`;
  const yTicks=[0,.5,1].map(f=>{const value=yMin+(yMax-yMin)*f;return `<line class="grid-line" x1="${pad.l}" x2="${width-pad.r}" y1="${y(value)}" y2="${y(value)}"/><text class="axis-label" x="${pad.l-9}" y="${y(value)+3}" text-anchor="end">${money(value,true)}</text>`}).join("");
  const tickIndexes=[0,normalized.length-1].filter((value,index,array)=>array.indexOf(value)===index);
  const duration=tMax-tMin;
  const timeLabel=value=>new Date(value).toLocaleString([],duration>86_400_000?{month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}:{hour:"2-digit",minute:"2-digit"});
  const xTicks=tickIndexes.map(i=>`<line class="history-tick" x1="${pts[i][0]}" x2="${pts[i][0]}" y1="${height-pad.b}" y2="${height-pad.b+5}"/><text class="axis-label" x="${pts[i][0]}" y="${height-10}" text-anchor="${i===0?'start':i===normalized.length-1?'end':'middle'}">${timeLabel(normalized[i].time)}</text>`).join("");
  const first=normalized[0].notional, latest=normalized.at(-1).notional, change=first?latest/first-1:null;
  $("#history-latest").textContent=money(latest,true);
  $("#history-change").textContent=change==null?"—":`${change>=0?"+":""}${pct(change,1)}`;
  $("#history-change").className=change==null?"":change>0?"risk-up":change<0?"risk-down":"";
  svg.setAttribute("viewBox",`0 0 ${width} ${height}`);
  svg.innerHTML=`<defs><linearGradient id="historyGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#23705a" stop-opacity=".2"/><stop offset="1" stop-color="#23705a" stop-opacity="0"/></linearGradient></defs>${yTicks}<line class="axis-line" x1="${pad.l}" x2="${width-pad.r}" y1="${height-pad.b}" y2="${height-pad.b}"/>${xTicks}<path class="history-area" d="${area}"/><path class="history-line" d="${path(pts)}"/>${pts.map((p,i)=>`<circle class="history-point ${i===pts.length-1?'history-live':''}" cx="${p[0]}" cy="${p[1]}" r="${i===pts.length-1?5:2.5}"/>`).join("")}<text class="history-latest-label" x="${pts.at(-1)[0]-8}" y="${pts.at(-1)[1]-10}" text-anchor="end">${money(latest,true)}</text>`;
}

function renderCliffs(rows) {
  const max=Math.max(...rows.map(r=>r.notional),1);
  $("#cliff-list").innerHTML=rows.length ? rows.map(r=>`<div class="cliff-row"><div class="cliff-level ${r.notional===max?'urgent':''}">−${r.drop_pct.toFixed(1)}%</div><div class="cliff-track"><i class="${r.notional===max?'urgent':''}" style="width:${r.notional/max*100}%"></i></div><div class="cliff-data"><strong>${money(r.notional,true)}</strong><small>${r.accounts} account${r.accounts===1?'':'s'}</small></div></div>`).join("") : `<p class="table-note">No downside roots in the tracked interval.</p>`;
}

function renderMarketScanner(rows) {
  const container=$("#market-risk-rows");
  if (!rows?.length) {
    container.innerHTML=`<tr><td colspan="8" class="table-note">No markets available.</td></tr>`;
    return;
  }
  container.innerHTML=rows.map((row,index)=>{
    const skew=row.dominant==="downside"?"Downside cascade":row.dominant==="upside"?"Short squeeze":"Balanced";
    const funding=`${row.funding>=0?"+":""}${(row.funding*100).toFixed(4)}%`;
    const trend=row.trend_fraction==null?"—":`${row.trend_fraction>=0?"+":""}${pct(row.trend_fraction,1)}`;
    return `<tr data-market-coin="${safe(row.coin)}" class="market-row ${index===0?'top-risk':''} ${row.coin===dashboard?.coin?'selected':''}" tabindex="0"><td><button class="asset-link" type="button">${safe(row.coin)}</button>${index===0?'<span class="rank-tag">Highest</span>':''}<small>${money(row.mark_px)}</small></td><td><span class="action-badge ${row.action}">${safe(row.action)}</span><small class="action-reason">${safe(row.action_reason)}</small>${row.funding_aligned?'<small class="carry-note">Funding supports carry</small>':''}</td><td class="trend ${row.trend_fraction>0?'positive':row.trend_fraction<0?'negative':''}"><strong>${trend}</strong><small>vs prior day</small></td><td><span class="risk-badge ${row.dominant}">${skew}</span></td><td><strong>${money(row.downside_5.liquidatable_notional_tracked,true)}</strong><small>${pct(row.downside_5.share_of_tracked)} tracked</small></td><td><strong>${money(row.upside_5.liquidatable_notional_tracked,true)}</strong><small>${pct(row.upside_5.share_of_tracked)} tracked</small></td><td>${pct(row.coverage_fraction_open_interest,1)}</td><td class="funding ${row.funding>0?'positive':row.funding<0?'negative':''}">${funding}</td></tr>`;
  }).join("");
}

function renderAccounts(rows) {
  $("#account-rows").innerHTML=rows.map(r=>`<tr><td class="mono" title="${safe(r.account)}">${safe(r.account.slice(0,8))}…${safe(r.account.slice(-4))}</td><td><span class="side-pill ${r.side}">${r.side}</span></td><td>${money(r.notional,true)}</td><td>${money(r.joint_liq_px)}</td><td class="distance">${r.direction==='down'?'−':'+'}${Math.abs((1-r.root)*100).toFixed(2)}%</td><td>${r.other_positions}</td><td>${ago(r.observed_at)}</td></tr>`).join("");
}

async function load(coin, { silent = false } = {}) {
  const requestId = ++dashboardRequestId;
  const requestedCoin = coin || $("#coin-select").value || dashboard?.coin || "BTC";
  if (!silent) document.body.classList.add("loading");
  try {
    let nextDashboard=await getJSON(`/api/dashboard?coin=${encodeURIComponent(requestedCoin)}`);
    if (requestId !== dashboardRequestId) return;
    if (!nextDashboard.coins.includes(nextDashboard.coin) && nextDashboard.coins.length) {
      nextDashboard=await getJSON(`/api/dashboard?coin=${encodeURIComponent(nextDashboard.coins[0])}`);
      if (requestId !== dashboardRequestId) return;
    }
    dashboard=nextDashboard;
    $("#open-promote").hidden=!!dashboard.read_only;
    $("#as-of").textContent=`${ago(dashboard.as_of)} · ${new Date(dashboard.as_of).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`;
    const listedCoins=[...$("#coin-select").options].map(option=>option.value);
    if (listedCoins.join("\0") !== dashboard.coins.join("\0")) {
      $("#coin-select").innerHTML=dashboard.coins.map(c=>`<option>${safe(c)}</option>`).join("");
    }
    $("#coin-select").value=dashboard.coin; $("#coin-name").textContent=dashboard.coin; $("#mark-price").textContent=money(dashboard.mark_px);
    $("#tracked-notional").textContent=money(dashboard.tracked_notional,true); $("#tracked-accounts").textContent=dashboard.tracked_accounts.toLocaleString(); $("#live-share").textContent=pct(dashboard.share_of_tracked_notional_live);
    const five=dashboard.scenarios.find(r=>r.shock_pct===5); $("#risk-five").textContent=money(five.liquidatable_notional_tracked,true); $("#risk-five-accounts").textContent=`${five.accounts} accounts`; $("#risk-five-share").textContent=`${pct(five.share_of_tracked)} tracked`;
    const fiveUp=dashboard.upside_5; $("#risk-five-up").textContent=money(fiveUp.liquidatable_notional_tracked,true); $("#risk-five-up-accounts").textContent=`${fiveUp.accounts} accounts`; $("#risk-five-up-share").textContent=`${pct(fiveUp.share_of_tracked)} tracked`;
    $("#coverage").textContent=pct(dashboard.coverage_fraction_open_interest,1); $("#coverage-bar").style.width=`${Math.min(100,(dashboard.coverage_fraction_open_interest||0)*100)}%`;
    const cliff=dashboard.cliffs.reduce((best,row)=>!best||row.notional>best.notional?row:best,null); $("#largest-cliff").textContent=cliff?money(cliff.notional,true):"None"; $("#largest-cliff-level").textContent=cliff?`at −${cliff.drop_pct.toFixed(1)}%`:"—"; $("#largest-cliff-accounts").textContent=cliff?`${cliff.accounts} account${cliff.accounts===1?'':'s'}`:"—";
    renderMarketScanner(dashboard.markets); renderRiskChart(dashboard.scenarios); renderCliffs(dashboard.cliffs); renderAccounts(dashboard.account_roots); $("#limitations").innerHTML=dashboard.limitations.map(x=>`<li>${safe(x)}</li>`).join("");
    loadPriceCandles(dashboard.coin,{force:!silent});
    syncChartToLiveMark();
    const history=await getJSON(`/api/history?coin=${encodeURIComponent(dashboard.coin)}&shock_pct=5`);
    if (requestId !== dashboardRequestId) return;
    renderHistory(history.points);
  } catch (error) {
    if (requestId === dashboardRequestId && !silent) toast(error.message, true);
  } finally {
    if (requestId === dashboardRequestId && !silent) document.body.classList.remove("loading");
  }
}

async function loadAlerts() {
  try { const data=await getJSON("/api/alerts"); $("#alerts-list").innerHTML=data.alerts.map(a=>`<div class="alert-row ${a.acknowledged?'ack':''}"><span class="alert-state"></span><span class="alert-rule">${safe(a.rule)} · ${safe(a.subject)}</span><span class="alert-detail">${safe(typeof a.detail==='string'?a.detail:JSON.stringify(a.detail))}</span><button data-alert="${a.id}" ${a.acknowledged||data.read_only?'disabled':''}>${a.acknowledged?'Acknowledged':'Acknowledge'}</button></div>`).join(""); } catch(e) { toast(e.message,true); }
}
function toast(message,error=false){const el=$("#toast");el.textContent=message;el.style.borderLeft=`3px solid ${error?'#f0723c':'#c9f25e'}`;el.classList.add("show");setTimeout(()=>el.classList.remove("show"),3200)}

$$('.nav-item').forEach(button=>button.addEventListener('click',()=>{$$('.nav-item').forEach(x=>x.classList.remove('active'));button.classList.add('active');$$('.view').forEach(x=>x.classList.remove('active'));$(`#view-${button.dataset.view}`).classList.add('active');if(button.dataset.view==='quality')loadAlerts();}));
$("#coin-select").addEventListener("change", e=>load(e.target.value)); $("#refresh").addEventListener("click",()=>load());
$$('[data-candle-range]').forEach(button=>button.addEventListener('click',()=>{
  candleRange=button.dataset.candleRange;
  $$('[data-candle-range]').forEach(item=>item.classList.toggle('active',item===button));
  loadPriceCandles(dashboard?.coin || $("#coin-select").value || "BTC",{force:true});
}));
function selectMarket(coin) { $("#coin-select").value=coin; load(coin); }
$("#market-risk-rows").addEventListener("click",e=>{const row=e.target.closest("[data-market-coin]");if(row)selectMarket(row.dataset.marketCoin);});
$("#market-risk-rows").addEventListener("keydown",e=>{if((e.key==="Enter"||e.key===" ")&&e.target.matches("[data-market-coin]")){e.preventDefault();selectMarket(e.target.dataset.marketCoin);}});
const dialog=$("#promote-dialog"); $("#open-promote").addEventListener("click",()=>dialog.showModal());
$("#close-dialog").addEventListener("click",()=>dialog.close());
$("#promote-form").addEventListener("submit",async e=>{e.preventDefault();const address=$("#address").value;try{await getJSON('/api/watchlist/promote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({address})});dialog.close();$("#address").value='';toast('Wallet added to the tracked set.');}catch(error){$("#dialog-message").textContent=error.message;}});
$("#alerts-list").addEventListener("click",async e=>{const id=e.target.dataset.alert;if(!id)return;await getJSON(`/api/alerts/${id}/acknowledge`,{method:'PATCH'});loadAlerts();});
load();
setInterval(() => {
  if (!document.hidden) load(undefined, { silent: true });
}, AUTO_REFRESH_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) load(undefined, { silent: true });
});
