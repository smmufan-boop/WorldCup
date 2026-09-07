// Static GitHub Pages data adapter: no localhost calls, credentials or write operations.
(() => {
  globalThis.paperStatsReadOnly=true;
  function kickoffTime(value) {
    if(typeof value!=='string'||!value.trim())return NaN;
    let text=value.trim().replace(' ','T');
    if(!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$/i.test(text))return NaN;
    const [year,month,day,hour,minute]=[text.slice(0,4),text.slice(5,7),text.slice(8,10),text.slice(11,13),text.slice(14,16)].map(Number);
    if(!year||month<1||month>12||day<1||day>new Date(Date.UTC(year,month,0)).getUTCDate()||hour>23||minute>59)return NaN;
    if(!/(Z|[+-]\d{2}:?\d{2})$/i.test(text))text+='+08:00';
    return Date.parse(text);
  }
  function maxDrawdown(settled) {
    if(!settled.length)return null;
    const ordered=settled.map(row=>({row,time:kickoffTime(row.kickoff)}));
    if(ordered.some(item=>!Number.isFinite(item.time)))return null;
    ordered.sort((a,b)=>a.time-b.time||a.row.id-b.row.id);
    // Stored profits have six-decimal precision; integer micro-units avoid float drift.
    let equity=0,peak=0,drawdown=0;
    for(const {row} of ordered) {
      equity+=Math.round(row.profit*1e6);peak=Math.max(peak,equity);
      drawdown=Math.max(drawdown,peak-equity);
    }
    return drawdown/1e6;
  }
  function summary(rows) {
    const settled=rows.filter(r=>r.status==='settled');
    const outcomes={win:0,half_win:0,push:0,half_loss:0,loss:0};
    for(const row of settled)outcomes[row.result]++;
    const settledUnits=settled.reduce((sum,r)=>sum+r.units,0);
    const profit=Math.round(settled.reduce((sum,r)=>sum+r.profit,0)*1e6)/1e6;
    const decisive=settled.length-outcomes.push;
    return {total:rows.length,pending:rows.length-settled.length,settled:settled.length,
      placedUnits:rows.reduce((sum,r)=>sum+r.units,0),settledUnits,profit,
      roi:settledUnits?profit/settledUnits:null,winRate:decisive?(outcomes.win+outcomes.half_win)/decisive:null,
      maxDrawdown:maxDrawdown(settled),outcomes};
  }
  globalThis.paperStatsSource=async params=>{
    const response=await fetch('./data.json',{cache:'no-store'});
    if(!response.ok)throw new Error('未能读取已同步的投注数据，请稍后刷新');
    const snapshot=await response.json();
    if(snapshot.schemaVersion!==1||!Array.isArray(snapshot.records))throw new Error('投注快照格式不兼容');
    // Beijing noon boundary, independent of the visitor's device time zone.
    const today=new Date(Date.now()-4*3600000).toISOString().slice(0,10);
    const scope=params.get('scope')||'today';
    const from=scope==='today'?today:scope==='all'?'':params.get('from');
    const to=scope==='today'?today:scope==='all'?'':params.get('to');
    const rows=snapshot.records.filter(r=>(!from||r.gameDay>=from)&&(!to||r.gameDay<=to));
    const offset=Math.max(0,Number(params.get('offset'))||0),limit=50;
    // Export is already fully sorted by Beijing kickoff before any pagination.
    const data={...snapshot,today,scope,filters:{from,to},summary:summary(rows),
      records:rows.slice(offset,offset+limit),total:rows.length,limit,offset};
    return {ok:true,json:async()=>data};
  };
})();
