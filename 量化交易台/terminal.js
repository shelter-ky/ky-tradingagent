"use strict";
const $ = id => document.getElementById(id);
const money = value => value.toLocaleString("en-US",{style:"currency",currency:"USD"});
const pct = value => (value*100).toFixed(2)+"%";
const signed = (v,fmt) => (v>0?"+":"")+fmt(v);
const notes = ["首日以可用整股买入，后续持有，现金分红留在账户。",
 "TQQQ 和 QQQ 的均价、均线及五分钟动量同时走弱时观察卖出，同时转强时买回。",
 "早盘部分按前日 VXN 相对中位数调仓，突破部分等待 QQQ 同向突破并放量。两部分独立使用现金。",
 "前日 VXN 与此前20日中位数之比达到90%时目标半仓，超过100%时目标空仓；减仓同时检查盘前条件。",
 "沿用早盘规则。收盘前10分钟若 QQQ 含息跌幅达到1%且低于日内均价，减掉约一半现有股数。"];
const data = window.REPLAY;
const state = {day:0,metric:"nav",range:"all",page:0,hover:null,view:"replay"};
const signalState = {manual:false,payload:null,note:"",sequence:0};
const hosted = location.protocol==="http:" && location.hostname==="127.0.0.1";
const pageSize=8;
let playback=null;

function pairs(container,items){
  container.replaceChildren();
  for(const [label,value] of items){
    const row=document.createElement("div"), term=document.createElement("dt"), detail=document.createElement("dd");
    term.textContent=label;detail.textContent=value;row.append(term,detail);container.append(row);
  }
}
function tableRow(body,values){
  const row=body.insertRow();
  for(const value of values) row.insertCell().textContent=value;
  return row;
}
function emptyRow(body,count,message){
  const cell=body.insertRow().insertCell();cell.colSpan=count;cell.className="empty";cell.textContent=message;
}
function colored(node,value,formatted){
  node.textContent=formatted;node.classList.toggle("up",value>0);node.classList.toggle("down",value<0);
}
function activeAccount(){return data.symbols[$("symbol").value][$("strategy").value];}
function riskSeries(rows){
  let peak=data.initial;
  return rows.map(row=>{peak=Math.max(peak,row[0]);return row[0]/peak-1;});
}
function dayIndex(value){
  let lo=0,hi=data.dates.length;
  while(lo<hi){const mid=(lo+hi)>>1;if(data.dates[mid]<=value)lo=mid+1;else hi=mid;}
  return Math.max(0,lo-1);
}
function drawOrders(){
  const side=$("side").value;
  const eligible=activeAccount().orders.filter(o=>o[0]<=state.day&&(side==="all"||(side==="buy"?o[2]>0:o[2]<0))).reverse();
  const pages=Math.max(1,Math.ceil(eligible.length/pageSize));
  state.page=Math.min(state.page,pages-1);
  const body=$("orders");body.replaceChildren();
  for(const [day,time,qty,price,fee,pnl] of eligible.slice(state.page*pageSize,(state.page+1)*pageSize)){
    const rule=Number($("strategy").value);
    let trigger="早盘 VXN 调仓";
    if(day===0&&time==="09:30")trigger="初始建仓";
    else if(rule===1)trigger=qty>0?"双标的转强":"双标的走弱";
    else if(rule===2&&time!=="09:35")trigger=qty>0?"放量向上突破":"放量向下突破";
    else if(rule===4&&time!=="09:35")trigger="尾盘风险减半";
    const row=tableRow(body,[data.dates[day]+" "+time,qty>0?"买入":"卖出",trigger,Math.abs(qty),money(price),
                             money(Math.abs(qty)*price),money(fee),pnl===null?"—":signed(pnl,money)]);
    if(pnl!==null)colored(row.cells[7],pnl,signed(pnl,money));
  }
  if(!eligible.length)emptyRow(body,8,"截至所选日期没有符合条件的模拟成交");
  $("orderCount").textContent=eligible.length+" 笔 · 第 "+(state.page+1)+" / "+pages+" 页";
  $("newer").disabled=state.page===0;$("older").disabled=state.page+1>=pages;
}
function drawChart(){
  if(!data||state.view!=="replay")return;
  const canvas=$("chart"),rect=canvas.getBoundingClientRect(),ctx=canvas.getContext("2d");
  if(rect.width<20)return;
  const w=rect.width,h=rect.height,scale=window.devicePixelRatio||1;
  canvas.width=Math.round(w*scale);canvas.height=Math.round(h*scale);ctx.scale(scale,scale);
  const first=state.range==="all"?0:Math.max(0,state.day-Number(state.range)+1);
  const selected=activeAccount().daily.slice(0,state.day+1),hold=data.symbols[$("symbol").value][0].daily.slice(0,state.day+1);
  const series=[selected,hold].map(rows=>(state.metric==="nav"?rows.map(r=>r[0]):riskSeries(rows)).slice(first));
  const format=state.metric==="nav"?money:pct;
  let min=Math.min(...series[0],...series[1]),max=Math.max(...series[0],...series[1]);
  const pad=(max-min)*.08||(state.metric==="nav"?1:.01);min-=pad;max+=pad;
  if(state.metric==="dd")max=0;
  const left=w<480?54:66,right=w-10,top=11,bottom=h-28,span=state.day-first;
  const x=i=>left+(right-left)*(span?i/span:0);
  const y=v=>bottom-(bottom-top)*(v-min)/(max-min||1);
  ctx.font="12px 'Microsoft YaHei',sans-serif";ctx.textAlign="right";
  for(let i=0;i<5;i++){
    const value=min+(max-min)*i/4,py=y(value);
    ctx.strokeStyle="#e1e9eb";ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(right,py);ctx.stroke();
    ctx.fillStyle="#526872";ctx.fillText(state.metric==="nav"?"$"+Math.round(value).toLocaleString("en-US"):(value*100).toFixed(1)+"%",left-6,py+4);
  }
  for(const k of [1,0]){
    ctx.strokeStyle=k?"#778ca2":"#007966";ctx.fillStyle=ctx.strokeStyle;ctx.lineWidth=k?1.6:2.2;ctx.setLineDash(k?[5,3]:[]);
    ctx.beginPath();series[k].forEach((v,i)=>{if(i===0)ctx.moveTo(x(i),y(v));else ctx.lineTo(x(i),y(v));});ctx.stroke();
    if(!span){ctx.beginPath();ctx.arc(left,y(series[k][0]),3,0,Math.PI*2);ctx.fill();}
  }
  ctx.setLineDash([]);ctx.fillStyle="#50656e";ctx.textAlign="left";ctx.fillText(data.dates[first],left,h-5);
  if(span){ctx.textAlign="right";ctx.fillText(data.dates[state.day],right,h-5);}
  const focus=state.hover===null?state.day:Math.min(state.day,Math.max(first,state.hover));
  if(state.hover!==null){
    const px=x(focus-first);ctx.strokeStyle="#57737e";ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(px,top);ctx.lineTo(px,bottom);ctx.stroke();
  }
  $("chartReadout").textContent=data.dates[focus]+"　策略 "+format(series[0][focus-first])+"　持有 "+format(series[1][focus-first]);
  canvas.dataset.first=first;canvas.dataset.left=left;canvas.dataset.right=right;
  canvas.setAttribute("aria-label","所选策略和直接持有的"+(state.metric==="nav"?"收盘净值":"收盘回撤")+"，截至 "+data.dates[state.day]);
}
function draw(){
  const rule=Number($("strategy").value),account=activeAccount(),i=state.day;
  const [equity,cash,qty,close,owed,unrealized]=account.daily[i];
  const prev=i?account.daily[i-1][0]:data.initial;
  const dd=riskSeries(account.daily.slice(0,i+1));
  const [vxn,median,pre,prefix,minutes]=data.market[i],targets=account.targets[i];
  $("equity").textContent=money(equity);
  colored($("net"),equity-data.initial,"累计 "+signed(equity/data.initial-1,pct));
  colored($("dayPnl"),equity-prev,signed(equity-prev,money));$("dayPct").textContent="当日 "+signed(equity/prev-1,pct);
  $("exposure").textContent=pct(qty*close/equity);$("shares").textContent=qty+" 股 · "+$("symbol").value;
  $("maxdd").textContent=pct(-Math.min(...dd));$("draw").textContent="当前回撤 "+pct(Math.abs(dd[i]));
  $("logic").textContent=notes[rule];
  const target=rule===2?"早盘 "+pct(targets[0])+" ／ 突破 "+pct(targets[1]):pct(targets[0]);
  const fields=[["规则目标仓位",target],["可用模拟现金",money(cash)],["收盘价 ／ 持仓市值",money(close)+" ／ "+money(qty*close)],["持仓浮动盈亏（含息）",signed(unrealized,money)]];
  if(owed>0)fields.push(["应收分红",money(owed)]);
  if(rule>=2)fields.push(["前日 VXN ／ 中位数",vxn.toFixed(2)+" ／ "+median.toFixed(2)]);
  if(rule>0)fields.push(["盘前筛选",pre==="MISSING"?"缺数据，用常规盘规则":pre==="WEAK"?"双标的同跌":"已核验，非双跌"]);
  if(prefix<minutes)fields.push(["常规盘连续分钟",prefix+" / "+minutes]);
  pairs($("position"),fields);
  $("date").value=data.dates[i];$("prev").disabled=i===0;$("next").disabled=i===data.dates.length-1;$("last").disabled=$("next").disabled;
  drawOrders();drawChart();
}
function stopPlayback(){
  clearInterval(playback);playback=null;$("play").textContent="播放";$("play").setAttribute("aria-pressed","false");
}
function setDay(i,manual=true){
  if(manual||i>=data.dates.length-1)stopPlayback();
  state.day=Math.min(data.dates.length-1,Math.max(0,i));state.page=0;state.hover=null;draw();
}
function groupSwitch(id,key){
  for(const button of $(id).querySelectorAll("button"))button.onclick=()=>{
    state[key]=button.dataset.value;state.hover=null;
    for(const item of $(id).querySelectorAll("button"))item.setAttribute("aria-pressed",String(item===button));
    drawChart();
  };
}

const actions={WAIT:"等待",WATCH_LONG:"观察买入条件",WATCH_RISK:"观察减仓／退出条件"};
const formatTime=v=>new Date(v).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false});
const riskFields={max_position_weight:"单标的仓位上限",max_daily_exposure_weight:"每日杠杆敞口上限",
  max_stop_scenario_loss_fraction:"单笔止损风险预算",min_cash_weight:"最低现金比例",
  slippage_bps:"预估滑点",fee_per_order:"每单手续费",quantity_step:"交易股数步长"};

function showSellRisk(report,active){
  const body=$("sellRows"),history=$("sellHistory"),status=$("sellStatus"),note=$("sellNote"),preNote=$("preNote");
  body.replaceChildren();history.hidden=true;preNote.hidden=true;
  if(!report){status.textContent="等待报告";note.textContent="读取盘中报告后展示本轮风险观察。";return;}
  const archived=active?"":"存档 · ";
  if(report.status!=="READY"){
    status.textContent="等待判断";note.textContent=report.reason||"等待完整且同步的分钟行情。";return;
  }
  if(report.action!=="WATCH_RISK"){
    status.textContent=archived+"未触发";note.textContent="这轮报告没有确认卖出风险观察。";return;
  }
  const pre=report.premarket_context?.premarket;
  if(pre){
    const text=pre.status==="AVAILABLE"?({WEAK:"两只 ETF 同跌",STRONG:"两只 ETF 同涨",MIXED:"两只 ETF 方向不一致"}[pre.direction]):pre.reason;
    if(text){preNote.textContent="盘前背景："+text;preNote.hidden=false;}
  }
  status.textContent=archived+"风险观察";
  if(report.ticker!=="TQQQ"){note.textContent="本轮已触发风险观察。卖出避损的历史测算仅覆盖 TQQQ。";return;}
  const impact=report.analysis?.sell_impact;
  const rows=Array.isArray(impact?.rows)?impact.rows.filter(row=>row&&row.n>0&&row.horizon!=null):[];
  if(impact?.status!=="HISTORICAL"||!rows.length||!Number.isFinite(impact.unit_usd)){
    note.textContent=impact?.reason||"本轮报告没有附带可比历史样本。";return;
  }
  const fee=Number.isFinite(impact.sale_fee_usd)?"每单费用 "+money(impact.sale_fee_usd):
    Number.isFinite(impact.cost_bps)?"存档卖出费率 "+pct(impact.cost_bps/10000):"存档未列费用";
  const cohort=impact.anchor?"按美东 "+impact.anchor+" 触发时段对照。":"触发起点未知，参考全部时段。";
  const period=impact.sample_start&&impact.sample_end?"历史样本 "+impact.sample_start+" 至 "+impact.sample_end+"。":"";
  note.textContent=cohort+period+"模拟卖出 "+money(impact.unit_usd)+"，"+fee+"。";
  const dollars=value=>Number.isFinite(value)?signed(value,money):"—";
  for(const row of rows){
    const horizon=row.horizon==="收盘"?"收盘":row.horizon+" 分钟";
    tableRow(body,[horizon,row.n,dollars(row.down_saved),dollars(row.rebound_missed),dollars(row.average_net),
                   row.n>0&&Number.isFinite(row.profitable)?pct(row.profitable/row.n):"—"]);
  }
  history.hidden=false;
}

function showSizing(report,active){
  const sizing=report?.sizing;
  const labels={NEEDS_INPUT:"待补资料",UNSUPPORTED:"暂不支持",BLOCKED:"未通过"};
  $("sizingStatus").textContent=!report?"等待报告":(active?"":"存档 · ")+(labels[sizing?.status]||"无可用校验结果");
  $("sizingNote").textContent=report?(sizing?.reason||"这份报告未提供盘中仓位校验说明。"):"读取盘中报告后展示账户及风险参数的校验状态。";
  const missing=Array.isArray(sizing?.missing)?sizing.missing.filter(v=>typeof v==="string").map(v=>riskFields[v]||v):[];
  pairs($("sizingRows"),missing.length?[["待补项目",missing.join("、")]]:[]);
}

function renderSignal(){
  const report=signalState.payload;
  $("signalSource").textContent=signalState.note;
  $("signalStatus").textContent="等待信号";$("signalReason").textContent="";$("signalDetails").replaceChildren();
  const body=$("signalsTable");body.replaceChildren();
  if(!report){emptyRow(body,6,"暂无可读取的分钟行情");showSellRisk(null,false);showSizing(null,false);return;}
  const stamp=Date.parse(report.generated_at),until=Date.parse(report.valid_until),now=Date.now();
  signalState.nextBoundary=stamp>now?stamp:until>now?until:Infinity;
  const active=report.status==="READY"&&report.session==="REGULAR"&&stamp<=now&&until>now;
  const expired=Number.isFinite(until)&&until<=now;
  showSellRisk(report,active);showSizing(report,active);
  $("signalStatus").textContent=active?actions[report.action]:expired?"已过期 · 等待新信号":"等待";
  $("signalReason").textContent=active||report.action==="WAIT"?report.reason:"当时的观察已结束。";
  pairs($("signalDetails"),[["报告标的",report.ticker],["生成时间（北京时间）",formatTime(stamp)],
      ["有效至（北京时间）",Number.isFinite(until)?formatTime(until):"无有效信号"],["分钟数据",active?"报告有效期内":"存档快照"]]);
  const num=v=>typeof v==="number"&&Number.isFinite(v)?v.toFixed(2):"—";
  for(const s of report.snapshots)tableRow(body,[s.ticker,num(s.price),num(s.vwap),num(s.ema9),num(s.ema20),num(s.momentum5_pct)+(Number.isFinite(s.momentum5_pct)?"%":"")]);
  if(!report.snapshots.length)emptyRow(body,6,"本轮信号没有完整行情");
}
function acceptSignal(r){
  if(!r||r.version!=="intraday-v1"||r.ticker!==$("signalSymbol").value||!Object.hasOwn(actions,r.action)||
     !Array.isArray(r.snapshots)||!r.snapshots.every(s=>s&&typeof s.ticker==="string")||
     !Number.isFinite(Date.parse(r.generated_at)))throw Error();
  signalState.payload=r;
}
async function refreshSignal(){
  if(state.view!=="signals"||signalState.manual)return;
  const serial=++signalState.sequence,symbol=$("signalSymbol").value;
  if(!hosted){signalState.note="直接打开模式 · 可在下方选择 Agent 的 latest.json 文件";renderSignal();return;}
  try{
    const response=await fetch("/signal/"+symbol,{cache:"no-store"});
    if(!response.ok)throw Error();
    const result=await response.json();if(serial!==signalState.sequence)return;
    signalState.payload=null;
    if(result.report)acceptSignal(result.report);
    signalState.note=result.report?"本地自动读取 · 每 10 秒检查":"本地自动读取 · 未找到该标的已完成的盘中报告";
  }catch{if(serial!==signalState.sequence)return;signalState.payload=null;signalState.note="本轮读取失败，等待下一次刷新。";}
  renderSignal();
}
function autoSignal(){
  signalState.sequence++;signalState.manual=false;signalState.payload=null;signalState.note="正在检查本地报告";
  $("signalFile").value="";renderSignal();refreshSignal();
}
for(const button of document.querySelectorAll("nav button"))button.onclick=()=>{
  stopPlayback();
  state.view=button.dataset.view;
  for(const item of document.querySelectorAll("nav button")){
    const selected=item===button;item.setAttribute("aria-pressed",String(selected));$(item.dataset.view).hidden=!selected;
  }
  if(state.view==="signals"){renderSignal();refreshSignal();}else if(state.view==="replay")drawChart();
};
$("signalSymbol").onchange=autoSignal;$("resume").onclick=autoSignal;
$("signalFile").onchange=async event=>{
  const file=event.target.files[0];if(!file)return;
  const serial=++signalState.sequence;signalState.manual=true;signalState.payload=null;
  signalState.note="正在读取本地报告";renderSignal();
  try{
    if(file.size>2_000_000)throw Error();
    const value=JSON.parse(await file.text());if(serial!==signalState.sequence)return;
    acceptSignal(value);signalState.note="手动读取 · "+file.name;
  }catch{if(serial!==signalState.sequence)return;signalState.note="请选择与标的一致的 Agent 盘中 JSON 报告。";}
  renderSignal();
};
setInterval(refreshSignal,10000);
setInterval(()=>{if(state.view==="signals"&&signalState.payload&&Date.now()>=signalState.nextBoundary)renderSignal();},1000);
if(data){
  state.day=data.dates.length-1;
  $("coverage").textContent=data.dates[0]+" 至 "+data.dates.at(-1)+"　·　"+data.dates.length.toLocaleString()+" 个交易日";
  if(data.demo){
    document.querySelector("#replay .heading .badge").textContent="合成演示";
    $("coverage").textContent+="　·　虚构行情，用于演示界面";
  }
  $("date").min=data.dates[0];$("date").max=data.dates.at(-1);
  $("date").onchange=()=>{if($("date").value)setDay(dayIndex($("date").value));else draw();};
  $("prev").onclick=()=>setDay(state.day-1);$("next").onclick=()=>setDay(state.day+1);$("last").onclick=()=>setDay(data.dates.length-1);
  $("play").onclick=()=>{
    if(playback){stopPlayback();return;}
    if(state.day===data.dates.length-1)setDay(0);
    $("play").textContent="暂停";$("play").setAttribute("aria-pressed","true");
    playback=setInterval(()=>setDay(state.day+1,false),750);
  };
  for(const id of ["symbol","strategy"])$(id).onchange=()=>setDay(state.day);
  $("side").onchange=()=>{state.page=0;drawOrders();};$("newer").onclick=()=>{state.page--;drawOrders();};$("older").onclick=()=>{state.page++;drawOrders();};
  groupSwitch("range","range");groupSwitch("metric","metric");
  $("chart").onpointermove=e=>{
    const c=$("chart"),a=Number(c.dataset.first),left=Number(c.dataset.left),right=Number(c.dataset.right);
    state.hover=a+Math.round((e.clientX-c.getBoundingClientRect().left-left)/(right-left)*(state.day-a));drawChart();
  };
  $("chart").onpointerleave=()=>{state.hover=null;drawChart();};
  new ResizeObserver(drawChart).observe($("chart"));draw();
}else{$("error").hidden=false;$("error").textContent="回放数据未载入，请从量化交易台目录重新打开面板。";}
