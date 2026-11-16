import {api} from './client.js';
export const datasets={list:()=>api('/datasets'),get:id=>api('/datasets/'+id),create:data=>api('/datasets',{method:'POST',body:JSON.stringify(data)})};
export const jobs={list:()=>api('/jobs'),get:id=>api('/jobs/'+id),create:data=>api('/jobs',{method:'POST',body:JSON.stringify(data)}),approve:id=>api('/jobs/'+id+'/approve',{method:'POST'})};
export const reports={list:()=>api('/reports'),create:data=>api('/reports',{method:'POST',body:JSON.stringify(data)}),toggle:id=>api('/reports/'+id+'/toggle',{method:'POST'}),deliveries:()=>api('/deliveries')};
export const ledger={
 entries:(limit=200,offset=0)=>api('/ledger/entries?limit='+limit+'&offset='+offset),
 verify:(start,end)=>{let q='/ledger/verify?';if(Number.isFinite(+start))q+='start='+encodeURIComponent(start)+'&';if(Number.isFinite(+end))q+='end='+encodeURIComponent(end);return api(q);},
 pending:()=>api('/ledger/pending'),
 relay:()=>api('/ledger/relay',{method:'POST'})
};
