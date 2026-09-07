(() => {
  const form=document.querySelector('#paperStatsFilters'),message=document.querySelector('#paperStatsMessage');
  const from=form.elements.namedItem('from'),to=form.elements.namedItem('to');
  let offset=0,busy=false,scope='today',today='',requestSeq=0,appliedFilters={from:'',to:''};
  const esc=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
  const percent=v=>v==null?'—':`${(v*100).toFixed(1)}%`;
  const units=v=>v==null?'—':`${Number(v).toFixed(3).replace(/0+$/,'').replace(/\.$/,'')}`;
  const names={competition:'联赛偏好',role:'投注方向偏好',units:'注码选择',leadTime:'下注时机',signalType:'信号分类（同方向）',signalAlignment:'信号关联',modelAlignment:'万象方向关联'};
  const companyNames={'1':'澳彩','3':'皇家','8':'Bet365'};
  function selectedTeamHandicap(record) {
    if(!['home','away'].includes(record.side)||!Number.isFinite(record.line)||Math.abs(record.line)>20||!Number.isInteger(record.line*4))return '盘口待确认';
    // Source line > 0 means home gives goals. Display the signed handicap
    // added to the SELECTED team's score, not the source's home-giving sign.
    const handicap=record.side==='home'?-record.line:record.line;
    return handicap===0?'0':`${handicap>0?'+':''}${handicap}`;
  }
  const kickoffFormatter=new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'});
  function beijingKickoff(value) {
    if(typeof value!=='string'||!value.trim())return '时间待确认';
    let text=value.trim().replace(' ','T');
    // Stored kickoff strings without an offset are already Beijing local time.
    if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$/i.test(text))return '时间待确认';
    if(!/(Z|[+-]\d{2}:?\d{2})$/i.test(text))text+='+08:00';
    const time=Date.parse(text);
    if(!Number.isFinite(time))return '时间待确认';
    const parts=Object.fromEntries(kickoffFormatter.formatToParts(time).map(p=>[p.type,p.value]));
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
  }
  function shiftDate(value,days) {
    if(!/^\d{4}-\d{2}-\d{2}$/.test(value))return '';
    const time=Date.parse(value+'T00:00:00Z');
    return Number.isFinite(time)?new Date(time+days*86400000).toISOString().slice(0,10):'';
  }
  function selectedRange() {
    if(!from.value||!to.value||from.value>to.value) {
      message.textContent='请选择有效的起止赛日，结束日期不能早于开始日期。';return false;
    }
    return true;
  }
  async function load() {
    const seq=++requestSeq,requestedScope=scope;
    if(scope==='range'&&!selectedRange()){busy=false;return;}
    busy=true;
    const params=new URLSearchParams({scope:requestedScope,offset:String(offset)});
    if(requestedScope==='range'){params.set('from',from.value);params.set('to',to.value);}
    message.textContent='正在读取本地模拟投注…';
    try {
      const response=await (globalThis.paperStatsSource?globalThis.paperStatsSource(params):fetch(`/api/paper-bets?${params}`,{cache:'no-store'})),data=await response.json();
      if(seq!==requestSeq)return; // A slower previous date must not replace the current selection.
      if (!response.ok) throw new Error(data.error||'统计读取失败');
      today=data.today||today;
      appliedFilters=data.filters||{from:from.value,to:to.value};
      from.value=appliedFilters.from;to.value=appliedFilters.to;
      document.querySelector('#paperStatsPeriodTitle').textContent=requestedScope==='today'?'今日统计':requestedScope==='all'?'全部日期统计':'所选赛日统计';
      document.querySelector('#paperStatsPeriod').textContent=requestedScope==='all'?'全部赛日（北京时间）':`${appliedFilters.from} 12:00 至 ${shiftDate(appliedFilters.to,1)} 12:00（北京时间，截止时间不含）`;
      const s=data.summary;
      document.querySelector('#paperStatsSummary').innerHTML=[['投注笔数',s.total],['待结算',s.pending],['已结算',s.settled],['胜率（走盘剔除）',percent(s.winRate)],['半赢半输折算胜率',percent(s.halfWeightedWinRate)],['ROI',percent(s.roi)],['净收益（单位）',units(s.profit)],['已结算投入',units(s.settledUnits)]]
        .map(([label,value])=>`<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join('');
      const all=data.allTimeSummary;
      document.querySelector('#paperStatsAllTime').innerHTML=all?[['总笔数',all.total],['胜率',percent(all.winRate)],['ROI',percent(all.roi)],['累计净收益（单位）',units(all.profit)]]
        .map(([label,value])=>`<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join(''):'<p class="paper-rules">历史总览暂不可用，请刷新页面。</p>';
      message.textContent=`全赢 ${s.outcomes.win} · 赢半 ${s.outcomes.half_win} · 走盘 ${s.outcomes.push} · 输半 ${s.outcomes.half_loss} · 全输 ${s.outcomes.loss}；`+(data.exportedAt?`只读快照，数据截至 ${beijingKickoff(data.exportedAt)}（北京时间）；最新结算需从本地再次同步。`:'服务运行时自动结算，证据不足保留待结算。');
      if(data.snapshotMode==='page-only')message.textContent='网页已发布，个人投注数据尚未上传。';
      document.querySelector('#paperRecordCount').textContent=`共 ${data.total} 笔`;
      document.querySelector('#paperStatsRecords').innerHTML=data.records.map(r=>`<tr><td>${esc(beijingKickoff(r.kickoff))}<br><strong>${esc(r.home)} vs ${esc(r.away)}</strong><br>${esc(r.competition)} · 赛日 ${esc(r.gameDay)}</td><td>${r.marketTiming==='historical'?'旧盘口（已锁定） · ':''}${esc(companyNames[r.companyId]||r.companyName)}<br><strong>${esc(r.team)} ${esc(selectedTeamHandicap(r))}</strong> · 水位 ${Number(r.water).toFixed(2)}<br><small>${esc(r.quoteTs)}</small></td><td>${r.units}单位</td><td>${r.status==='settled'?`${r.scoreHome}-${r.scoreAway} · `:''}${esc(r.resultLabel)}${r.settlementRevisions>1?`<br>比分修订 ${r.settlementRevisions-1} 次`:''}</td><td class="${r.profit>0?'paper-profit':r.profit<0?'paper-loss':''}">${units(r.profit)}</td>${globalThis.paperStatsReadOnly?'':`<td>${esc(r.note||'—')}</td>`}</tr>`).join('')||`<tr><td colspan="${globalThis.paperStatsReadOnly?5:6}">${globalThis.paperStatsReadOnly?'所选赛日没有已同步的模拟投注记录。':'还没有模拟投注记录，请到比赛看板选择赛前让球盘。'}</td></tr>`;
      document.querySelector('#paperStatsPager').innerHTML=`<button data-offset="${Math.max(0,offset-data.limit)}" ${offset===0?'disabled':''}>上一页</button><span>第 ${Math.floor(offset/data.limit)+1} / ${Math.max(1,Math.ceil(data.total/data.limit))} 页</span><button data-offset="${offset+data.limit}" ${offset+data.limit>=data.total?'disabled':''}>下一页</button>`;
      const habits=document.querySelector('#paperStatsHabits');
      const habitsHtml=Object.entries(data.habits).filter(([key])=>Object.hasOwn(names,key)).map(([key,items])=>`<section class="board paper-habit-card"><div class="board-head"><h3>${esc(names[key])}</h3></div>
        <div class="paper-habit-scroll" data-habit="${esc(key)}" tabindex="0" role="region" aria-label="${esc(names[key])}，可上下左右滚动">
        ${key==='signalType'?`<p class="paper-rules">${esc(data.rules?.signalType||'按确认时同方向信号分类；同笔同类去重，多个类型可重叠；待结算不参与胜率和ROI。')}</p>`:''}
        ${key==='signalAlignment'?`<p class="paper-rules">${esc(data.rules?.signalAlignment||'仅同向、仅反向、双方均有、无有效方向信号互斥分组；胜率和ROI按你的投注结果计算。')}</p>`:''}
        <table class="paper-table"><thead><tr><th>类别</th><th>笔数 / 单位</th><th>已结算</th><th>胜率</th><th>ROI</th></tr></thead><tbody>${items.map(r=>`<tr><td>${esc(r.label)}</td><td>${r.total} / ${r.placedUnits}</td><td>${r.settled}</td><td>${percent(r.winRate)}</td><td>${percent(r.roi)}</td></tr>`).join('')||'<tr><td colspan="5">暂无记录</td></tr>'}</tbody></table></div></section>`).join('');
      // Keep untouched cards in place; changed totals must not reset the reader's scroll positions.
      if(habits.innerHTML!==habitsHtml) {
        const positions=new Map(Array.from(habits.querySelectorAll('.paper-habit-scroll'),el=>[el.dataset.habit,{top:el.scrollTop,left:el.scrollLeft}]));
        habits.innerHTML=habitsHtml;
        for(const el of habits.querySelectorAll('.paper-habit-scroll')) {
          const position=positions.get(el.dataset.habit);
          if(position){el.scrollTop=position.top;el.scrollLeft=position.left;}
        }
      }
    } catch (error) {if(seq===requestSeq)message.textContent=error.message;} finally {if(seq===requestSeq)busy=false;}
  }
  form.addEventListener('submit',event=>{event.preventDefault();
    if(!(scope==='all'&&!from.value&&!to.value)&&!(scope==='today'&&from.value===today&&to.value===today))scope='range';
    offset=0;return load();});
  form.addEventListener('change',event=>{
    if(!['from','to'].includes(event.target.name))return;
    if(event.target.name==='from'&&(!to.value||appliedFilters.from===appliedFilters.to))to.value=from.value;
    scope='range';offset=0;return load();
  });
  function moveDay(delta) {
    const first=from.value||today,last=to.value||first;
    if(!first)return;
    from.value=shiftDate(first,delta);to.value=shiftDate(last,delta);
    scope='range';offset=0;return load();
  }
  document.querySelector('#paperStatsPrev').addEventListener('click',()=>moveDay(-1));
  document.querySelector('#paperStatsNext').addEventListener('click',()=>moveDay(1));
  document.querySelector('#paperStatsToday').addEventListener('click',()=>{scope='today';offset=0;return load();});
  document.querySelector('#paperStatsAll').addEventListener('click',()=>{scope='all';from.value='';to.value='';offset=0;return load();});
  document.querySelector('#paperStatsPager').addEventListener('click',event=>{const b=event.target.closest('button[data-offset]');if(b&&!b.disabled&&!busy){offset=Number(b.dataset.offset);load();}});
  setInterval(()=>{if(!document.hidden&&!busy)return load();},30000);load();
})();
