// Shared local / static-site chart. No network requests, dependencies or ledger writes.
(() => {
  const esc=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
  const units=value=>Number(value.toFixed(6)).toLocaleString('zh-CN',{maximumFractionDigits:3});
  const signed=value=>`${value>0?'+':''}${units(value)}`;
  function validDay(value) {
    if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(value)||value.startsWith('0000-'))return false;
    const date=new Date(value+'T00:00:00Z');
    return Number.isFinite(date.getTime())&&date.toISOString().slice(0,10)===value;
  }
  function historyFromRecords(records) {
    const days=new Map();let undatedCount=0,undatedProfit=0;
    for(const row of records) {
      if(row.status!=='settled')continue;
      const micro=Math.round(row.profit*1e6);
      // Use the saved game day (Beijing noon), never confirmation/settlement date.
      if(!validDay(row.gameDay)){undatedCount++;undatedProfit+=micro;continue;}
      const day=days.get(row.gameDay)||{micro:0,settled:0};
      day.micro+=micro;day.settled++;days.set(row.gameDay,day);
    }
    return {days:Array.from(days).sort(([a],[b])=>a.localeCompare(b)).map(([date,value])=>({date,profit:value.micro/1e6,settled:value.settled})),
      undatedCount,undatedProfit:undatedProfit/1e6};
  }
  function series(history,period='day') {
    const grouped=new Map();
    for(const row of history?.days||[]) {
      if(!validDay(row.date)||!Number.isFinite(row.profit))continue;
      const date=period==='month'?row.date.slice(0,7):row.date;
      const value=grouped.get(date)||{micro:0,settled:0};
      value.micro+=Math.round(row.profit*1e6);value.settled+=row.settled;
      grouped.set(date,value);
    }
    const dates=Array.from(grouped.keys()).sort();
    if(!dates.length)return [];
    // Categorical dates: omit gaps, but keep real zero-profit days (e.g. pushes).
    const points=[];let cumulative=0;
    for(const date of dates) {
      const value=grouped.get(date);
      cumulative+=value.micro;
      points.push({date,profit:value.micro/1e6,cumulative:cumulative/1e6,settled:value.settled});
    }
    return points;
  }
  function description(point,period) {
    return `${point.date} · ${period==='month'?'当月':'当日'}盈亏 ${signed(point.profit)} 单位 · 累计净收益 ${signed(point.cumulative)} 单位 · 已结算 ${point.settled} 笔`;
  }
  function svg(points,period,viewportWidth=640) {
    const width=Math.max(640,viewportWidth,points.length*32+88),height=300,left=64,right=24,top=26,bottom=54;
    let min=0,max=0;
    for(const point of points){min=Math.min(min,point.profit,point.cumulative);max=Math.max(max,point.profit,point.cumulative);}
    const rawStep=(max-min||2)/5,base=10**Math.floor(Math.log10(rawStep));
    const step=([1,2,5,10].find(n=>n*base>=rawStep)||10)*base;
    min=Math.floor(min/step)*step;max=Math.ceil(max/step)*step;
    if(min===max){min=-1;max=1;}
    const y=value=>top+(max-value)/(max-min)*(height-top-bottom);
    const spacing=(width-left-right)/points.length,x=i=>left+(i+.5)*spacing;
    const zero=y(0),barWidth=Math.min(32,spacing*.58),labelEvery=Math.max(1,Math.ceil(88/spacing));
    let grid='';
    for(let i=0;i<=Math.round((max-min)/step);i++) {
      const value=min+i*step,py=y(value);
      grid+=`<line class="${Math.abs(value)<step/100?'paper-chart-zero':'paper-chart-grid'}" x1="${left}" x2="${width-right}" y1="${py}" y2="${py}"/><text x="${left-10}" y="${py+4}" text-anchor="end">${esc(units(value))}</text>`;
    }
    const marks=points.map((point,i)=>{
      const px=x(i),py=y(point.profit),desc=description(point,period);
      const label=(i%labelEvery===0||i===points.length-1&&i%labelEvery>=labelEvery/2)?`<text x="${px}" y="${height-bottom+24}" text-anchor="middle">${esc(point.date)}</text>`:'';
      return `<g class="paper-chart-point" data-profit-index="${i}" tabindex="0" role="img" aria-label="${esc(desc)}"><title>${esc(desc)}</title><rect class="paper-chart-hit" x="${px-spacing/2}" y="${top}" width="${spacing}" height="${height-top-bottom}"/><rect class="paper-chart-bar ${point.profit>0?'gain':point.profit<0?'loss':'flat'}" x="${px-barWidth/2}" y="${point.profit===0?zero-.75:Math.min(zero,py)}" width="${barWidth}" height="${Math.max(1.5,Math.abs(py-zero))}"/><circle class="paper-chart-dot" cx="${px}" cy="${y(point.cumulative)}" r="3"/>${label}</g>`;
    }).join('');
    // The left edge is the zero-unit opening balance, including for a single day.
    const line=`M${left},${zero} `+points.map((p,i)=>`L${x(i)},${y(p.cumulative)}`).join(' ');
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${height}" style="min-width:${width}px" aria-label="累计净收益折线及${period==='month'?'每月':'每日'}盈亏柱状图（单位）"><title>累计净收益与各期盈亏</title><desc>蓝线为累计净收益，绿柱盈利，红柱亏损。日期等距排列，跳过无记录日期；折线与柱状图共用单位刻度；点击或聚焦日期查看数值。</desc>${grid}${marks}<path class="paper-chart-line" d="${line}"/><text x="${width-right}" y="${height-8}" text-anchor="end">${period==='month'?'月份（按赛日）':'赛日（北京时间12点切日）'}</text></svg>`;
  }
  function mount(root) {
    if(!root)return {update(){}};
    const plot=root.querySelector('#paperProfitPlot'),detail=root.querySelector('#paperProfitDetail');
    const buttons=root.querySelectorAll('[data-profit-period]');
    let history,period='day',points=[],signature='',selectedDate='';
    const warning=()=>history?.undatedCount?`；另有 ${history.undatedCount} 笔缺失有效赛日，净收益 ${signed(history.undatedProfit)} 单位未绘入曲线。`:'';
    function show(point) {
      if(!point)return;
      selectedDate=point.date;detail.textContent=description(point,period)+warning();
    }
    function render() {
      const previousScroll=plot.scrollLeft;
      points=series(history,period);
      for(const button of buttons)button.setAttribute('aria-pressed',String(button.dataset.profitPeriod===period));
      if(!points.length) {
        plot.innerHTML=`<p class="paper-chart-empty">${history?'暂无可绘制的已结算收益记录':'收益图数据暂不可用，请稍后刷新。'}</p>`;
        detail.textContent=warning().replace(/^；/,'');return;
      }
      plot.innerHTML=svg(points,period,plot.clientWidth);plot.scrollLeft=previousScroll;
      show(points.find(p=>p.date===selectedDate)||points.at(-1));
    }
    root.addEventListener('click',event=>{
      const button=event.target.closest('[data-profit-period]');
      if(button&&['day','month'].includes(button.dataset.profitPeriod)) {
        if(period!==button.dataset.profitPeriod){period=button.dataset.profitPeriod;selectedDate='';plot.scrollLeft=0;render();}
      }
    });
    const inspect=event=>{
      const point=event.target.closest('[data-profit-index]');
      if(point)show(points[Number(point.dataset.profitIndex)]);
    };
    for(const event of ['pointerover','focusin','click'])plot.addEventListener(event,inspect);
    if(typeof ResizeObserver!=='undefined') {
      let width=plot.clientWidth;
      new ResizeObserver(()=>{if(width!==plot.clientWidth){width=plot.clientWidth;render();}}).observe(plot);
    }
    return {update(value) {
      const next=JSON.stringify(value??null);
      if(next===signature)return; // Date filtering / polling must not reset the chart.
      signature=next;history=value;render();
    }};
  }
  const api={historyFromRecords,series,svg,mount};
  globalThis.PaperProfitChart=api;
  if(typeof module!=='undefined')module.exports=api;
})();
