import {ledger} from '../api/entities.js';
import {heading} from '../components/common/shell.js';
import {WitnessState,LedgerCategory} from '../constants/enums.js';
import {session} from '../stores/session.js';
import {esc,date,notify} from '../utils/dom.js';

const stateBadge=s=>'<span class="ledger-state state-'+esc(s)+'">'+esc(WitnessState[s]||s)+'</span>';
const short=h=>h?esc(String(h).slice(0,16))+'…':'—';

export async function ledgerPage(){
 const [list,backlog]=await Promise.all([ledger.entries(200),ledger.pending()]);
 return heading('06 / VERIFIABLE LEDGER','可核验账本与独立见证',
   '导入、试验状态、批准与分发的每个动作都与审计同事务写入哈希链；独立见证端另存凭证。可固定起止范围核验、定位首处差异并查看待确认积压。',
   '<button class="button" id="verify-all">核验全账 ↻</button>')
 + '<section class="panel"><div class="section-title"><h2>固定范围核验</h2><span>核验期间新操作照常进行，不锁定账本</span></div>'
 + '<form id="verify-form" class="verify-form"><label>起始序号<input name="start" type="number" min="0" placeholder="留空 = 从创世锚点"></label>'
 + '<label>终止序号<input name="end" type="number" min="0" placeholder="留空 = 到当前链尾"></label>'
 + '<button class="primary" type="submit">核验该范围</button></form><div id="verify-result" class="verify-result"></div></section>'
 + backlogPanel(backlog)
 + '<section class="panel"><div class="section-title"><h2>账本条目（最新在前）</h2><span>共 '+list.total+' 条 · 仅展示前 200 条</span></div>'
 + '<div class="table-wrap"><table><thead><tr><th>序号</th><th>类别</th><th>动作</th><th>操作者</th><th>时间</th><th>见证状态</th><th>本条哈希</th><th>前序哈希</th></tr></thead><tbody>'
 + list.items.map(r=>'<tr><td>'+r.seq+'</td><td>'+esc(LedgerCategory[r.category]||r.category)+'</td><td>'+esc(r.action)+'</td><td>'+esc(r.actor)+'</td><td>'+date(r.at)+'</td><td>'+stateBadge(r.witness_state)+'</td><td><code>'+short(r.entry_hash)+'</code></td><td><code>'+short(r.prev_hash)+'</code></td></tr>').join('')
 + '</tbody></table></div></section>';
}

function backlogPanel(backlog){
 const pending=backlog.pending||[];const conflicts=backlog.conflicts||[];
 return '<section class="panel"><div class="section-title"><h2>待确认积压与冲突</h2><span>待确认 '+backlog.pending_count+' 条 · 冲突 '+backlog.conflict_count+' 条</span></div>'
 + (session.user.role==='admin'?'<button class="button small" id="relay-btn">立即按序补送 ↗</button><p class="hint">见证端断线时业务不阻塞；恢复后按原序补送，重试不新增凭证。</p>':'')
 + (conflicts.length?'<div class="ledger-alert conflict"><strong>见证冲突（同序异内容已被拒绝）</strong><ul>'+conflicts.map(c=>'<li>序号 '+c.seq+' · '+esc(c.action)+'：'+esc(c.reason)+'</li>').join('')+'</ul></div>':'')
 + (pending.length?'<div class="ledger-alert pending"><strong>待确认尾段（尚未取得见证回执）</strong><ul>'+pending.map(p=>'<li>序号 '+p.seq+' · '+esc(LedgerCategory[p.category]||p.category)+' · '+esc(p.action)+' · '+date(p.at)+'</li>').join('')+'</ul></div>':'<p class="empty">无待确认积压，所有可见证条目均已取得独立凭证。</p>')
 + '</section>';
}

function renderReport(r){
 const ok=r.ok&&r.chain_ok;
 const diff=r.first_difference;
 const errs=r.witness_errors||[];
 return '<div class="verify-card '+(ok?'ok':'bad')+'">'
 + '<h3>'+(ok?'✔ 核验通过：范围 '+esc(r.range.start)+' ~ '+esc(r.range.end)+' 链完整且凭证一致':'✘ 核验未通过，已定位首处差异')+'</h3>'
 + '<p class="hint">本库链尾序号 '+r.local_max_seq+' · 范围条目 '+r.total+' 条 · 见证端 '+(r.witness_online?'在线，已见证至序号 '+esc(r.witness_tip?.seq??'—'):'不可达（仅完成链内自检）')+'</p>'
 + '<div class="verify-counts">'+Object.entries(r.counts||{}).map(([k,v])=>'<span><strong>'+v+'</strong>'+esc(WitnessState[k]||k)+'</span>').join('')+'</div>'
 + (diff?'<div class="ledger-alert conflict"><strong>首处差异：序号 '+esc(diff.seq)+' · '+esc(diff.kind)+'</strong><p>'+esc(diff.reason)+'</p>'+(diff.actual_hash?'<p><code>实际：'+esc(diff.actual_hash)+'<br>应为：'+esc(diff.expected_hash)+'</code></p>':'')+'</div>':'<p class="hint">未发现断链、改写或截尾。</p>')
 + (errs.length?'<div class="ledger-alert '+(r.witness_online?'conflict':'pending')+'"><strong>见证端交叉比对结果（'+errs.length+' 项）</strong><ul>'+errs.map(e=>'<li>序号 '+esc(e.seq)+' · '+esc(e.kind)+'：'+esc(e.reason)+'</li>').join('')+'</ul></div>':'')
 + (r.pending_tail&&r.pending_tail.length?'<p class="hint">未见证尾段（待确认）序号：'+r.pending_tail.join(', ')+'</p>':'')
 + '</div>';
}

export function bindLedger(refresh){
 document.querySelector('#verify-all').onclick=async()=>{
  try{document.querySelector('#verify-result').innerHTML='<p class="hint">正在向独立见证端核验全账…</p>';
   const r=await ledger.verify();document.querySelector('#verify-result').innerHTML=renderReport(r);
  }catch(e){notify(e.message,true);}
 };
 const form=document.querySelector('#verify-form');
 form.onsubmit=async e=>{e.preventDefault();const f=new FormData(form);
  try{const r=await ledger.verify(f.get('start'),f.get('end'));document.querySelector('#verify-result').innerHTML=renderReport(r);}
  catch(err){notify(err.message,true);}
 };
 const relayBtn=document.querySelector('#relay-btn');
 if(relayBtn)relayBtn.onclick=async()=>{relayBtn.disabled=true;try{const s=await ledger.relay();
   notify(s.unavailable?'见证端暂不可达，条目保持待确认，恢复后自动补送':('已补送 '+s.sent+' 条，剩余待确认 '+s.pending+' 条'));await refresh();}
  catch(e){notify(e.message,true);}finally{relayBtn.disabled=false;}};
}
