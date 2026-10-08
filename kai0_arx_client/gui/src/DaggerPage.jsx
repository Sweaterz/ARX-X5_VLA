import FolderPicker from './FolderPicker';
import ExportPanel from './ExportPanel';
import React,{useState,useRef,useEffect} from 'react';
import {Play,Pause,Hand,Save,Trash2,Download,CameraOff,Check,Clock,FolderOpen,RotateCcw,ArrowRight,AlertTriangle} from 'lucide-react';
const phases={idle:'准备开始',policy:'策略控制',human:'人工纠正',paused:'位置保持 · 待恢复',transition:'切换中',saving:'正在保存',saved:'已保存',discarded:'已回收',error:'需要处理'};
const roles=[['head','顶部视角'],['left','左腕视角'],['right','右腕视角']];
export default function DaggerPage({s,online,pending,form,setForm,send,ask,onReplay}){
 const d=s?.dagger||{},phase=d.phase||'idle',active=!!d.active;
 const [focus,setFocus]=useState('head'),[result,setResult]=useState('unfinished'),[selected,setSelected]=useState(null),[detail,setDetail]=useState(null),[folderPicker,setFolderPicker]=useState(false),[replayError,setReplayError]=useState('');
 useEffect(()=>{setSelected(null)},[d.data_root]);
 const videos=useRef({}),lastSync=useRef(0),replayEpoch=useRef(0),lastCorrection=useRef({});
 useEffect(()=>{let cancelled=false;setDetail(null);setReplayError('');replayEpoch.current++;lastSync.current=0;lastCorrection.current={};if(selected)fetch('/api/dagger/episode/'+selected).then(r=>r.json()).then(v=>{if(!cancelled)setDetail(v)});return()=>{cancelled=true}},[selected]);
 const job=['running','exporting'].includes(d.task?.status),locked=!online||pending||s?.latched||s?.detached;
 const canStart=!locked&&!s?.busy&&!active&&!job&&s?.connected&&s?.cameras&&s?.mode==='holding'&&(s?.demo||s?.local_server?.status==='ready')&&!['starting','saving','recording'].includes(d.writer?.status);
 const hint=!online?'页面连接中，暂不可操作':s?.latched?'停止已锁定，请在控制台处理':s?.detached?'先在控制台接管后台保持':pending||s?.busy?'等待当前操作完成':job?'等待数据处理完成':!s?.connected?'先在控制台连接双臂':!s?.cameras?'先在控制台连接相机':s?.mode!=='holding'&&!active?'先完成归位或暂停并保持':!s?.demo&&s?.local_server?.status!=='ready'?'等待模型服务就绪':['starting','saving','recording'].includes(d.writer?.status)?'等待录制进程就绪':'准备就绪，可开始完整试验';
 // Only the head video emits transport commands. Follower media events never feed back.
 function sync(type){
  const master=videos.current.head;if(!master)return;
  const now=performance.now();
  if(type==='time'){
   if(master.paused||master.seeking||now-lastSync.current<1000)return;
   lastSync.current=now;
  }
  const pause=type==='pause'||type==='seeking'||type==='waiting'||master.paused||master.ended;
  if(type!=='time')replayEpoch.current++;
  const epoch=replayEpoch.current;
  for(const role of ['left','right']){
   const v=videos.current[role];if(!v)continue;
   if(pause)v.pause();
   v.playbackRate=master.playbackRate;
   const delta=Math.abs(v.currentTime-master.currentTime);
   const explicit=['seek','play','ready','rate'].includes(type);
   if(v.readyState>=1&&!v.seeking&&((explicit&&delta>.08)||(type==='time'&&v.readyState>=2&&delta>.35&&now-(lastCorrection.current[role]||0)>2000))){
    v.currentTime=master.currentTime;lastCorrection.current[role]=now;
   }
   if(!pause&&v.paused)v.play().then(()=>{if(epoch!==replayEpoch.current&&master.paused)v.pause()}).catch(e=>{if(e.name!=='AbortError')setReplayError('腕部视频未能播放：'+e.message)});
  }
 }
 function playReplay(){setReplayError('');videos.current.head?.play().catch(e=>{if(e.name!=='AbortError')setReplayError('播放失败：'+e.message)});}
 function locateFrame(frame){
  const master=videos.current.head;if(!master)return;
  setReplayError('');master.pause();sync('pause');
  const target=frame/30;
  if(master.readyState<1){setReplayError('视频正在加载，请稍后定位');return;}
  master.currentTime=target;playReplay();
 }
 function primary(){if(!active)return <button className="primary dagger-main" disabled={!canStart} onClick={()=>send('dagger_start')}><Play size={20}/>开始试验并录制</button>;
 if(phase==='human')return <button className="primary dagger-main" disabled={locked} onClick={()=>send('dagger_end_correction')}><Check size={20}/>结束纠正并保持</button>;
 if(phase==='policy')return <button className="human-action dagger-main" disabled={locked} onClick={()=>send('dagger_takeover')}><Hand size={20}/>人工接管 · 开始拖动</button>;
 return <div className="dagger-pair"><button className="primary" disabled={locked||phase!=='paused'||s?.busy||(!s?.demo&&s?.local_server?.status!=='ready')} onClick={()=>send('dagger_resume')}><Play size={17}/>恢复 VLA</button><button className="human-action" disabled={locked||phase!=='paused'} onClick={()=>send('dagger_takeover')}><Hand size={17}/>拖动纠正</button></div>}
 return <div className="dagger-page">
 {folderPicker&&<FolderPicker initial={d.data_root} onClose={()=>setFolderPicker(false)} onChoose={path=>{setFolderPicker(false);send('dagger_storage',false,{data_root:path})}}/>}
 <div className="dagger-intro"><div><span className="eyebrow">POLICY → HUMAN → POLICY</span><h2>观察、接管与记录</h2><p>策略与人工段写入同一次试验，结束后再导出训练数据。</p></div><div className={'phase-pill '+phase}><span className="pulse-dot"/>{phases[phase]}</div></div>
 <div className="dagger-stats">{[['当前控制者',phase==='human'?'操作者':phase==='policy'?'VLA 策略':'位置保持 / 未运行'],['试验时长',`${Math.floor((d.elapsed||0)/60)}:${String(Math.floor((d.elapsed||0)%60)).padStart(2,'0')}`],['接受样本',d.samples||0],['人工纠正',`${d.interventions||0} 次 · ${d.human_ratio||0}%`]].map(([k,v])=><div key={k}><span>{k}</span><strong>{v}</strong></div>)}</div>
 <div className="dagger-grid"><div className="dagger-observe"><section><div className="section-head"><h2>现场观察</h2><small>最近采样：图像 {d.quality?.image_age_ms??'—'} ms · 反馈 {d.quality?.feedback_age_ms??'—'} ms</small></div>
 <div className="dagger-camera-grid">{[roles.find(r=>r[0]===focus),...roles.filter(r=>r[0]!==focus)].map(([role,label],i)=><div key={role} className={'dagger-camera '+(i===0?'featured':'')} onClick={()=>setFocus(role)}><span>{label}{i===0?' · 主视角':''}</span>{s?.cameras&&online?<img src={'/api/camera/'+role+'?t='+Math.floor(Date.now()/600)} alt={label}/>:<div className="dagger-no-camera"><CameraOff/><p>等待相机连接</p></div>}</div>)}</div>
 <details className="joint-details"><summary>关节与夹爪反馈 · rad</summary><div className="joint-strip">{['左臂','右臂'].map((side,index)=><div key={side}><b>{side}</b>{Array.from({length:7},(_,i)=><span key={i}><small>{i===6?'夹爪':`J${i+1}`}</small>{s?.state?.[index*7+i]?.toFixed(2)??'—'}</span>)}</div>)}</div></details></section>
 <section className="dagger-timeline"><div className="section-head"><h2>控制时间线</h2><small>蓝：策略 · 橙：人工 · 灰：保持</small></div><div className="timeline-track">{(d.events||[]).filter(e=>['policy','human','paused','transition'].includes(e.phase)).map((e,i)=><div key={i} className={'timeline-segment '+e.phase} title={e.reason}>{e.phase==='human'?'人工':e.phase==='policy'?'策略':e.phase==='paused'?'保持':'切换'}</div>)}</div><p className="hint">{d.episode||'开始试验后生成 episode'} · 采样目标 30 Hz，数据质量异常时自动暂停</p></section></div>
 <div className="dagger-actions"><section><div className="section-head"><h2>试验控制</h2><span className={'server-dot '+(s?.local_server?.status==='ready'?'green':['starting','checking','stopping'].includes(s?.local_server?.status)?'yellow':'red')}/></div><div className="checkpoint-card"><span>当前 CHECKPOINT</span><strong>{(active?d.metadata?.checkpoint_name:s?.local_server?.checkpoint_name)||'等待服务信息'}</strong><small>{s?.local_server?.message||'检查服务中'}</small></div>

 {primary()}<p className="hint">{active?phase==='human'?'双臂重力补偿中。手动拖动关节与夹爪；结束后先保持，再手动恢复策略。':'接管前请扶稳双臂；进入人工模式后，夹爪可手动拨动。':hint}</p>
 <button className="wide" disabled={locked||!active||phase==='saving'} onClick={()=>send('dagger_pause')}><Pause size={17}/>暂停并保持</button>
 <div className="dagger-save"><label>试验结果<select value={result} onChange={e=>setResult(e.target.value)}><option value="unfinished">未完成 / 待评估</option><option value="success">成功</option><option value="failure">失败</option></select></label><button className="primary wide" disabled={!online||pending||!active} onClick={()=>send('dagger_finish',false,{result})}><Save size={17}/>结束试验并保存</button><button className="text-button" disabled={!online||pending||!active} onClick={()=>ask('dagger_discard','丢弃当前试验','停止任务并保持后，将本次数据移入回收区。')}><Trash2 size={15}/>丢弃本次试验</button></div>
 <div className="writer-status"><span className={'dot '+(d.writer?.status==='saved'?'live':'')}/>{({starting:'录制进程启动中',recording:'独立进程写入中',saving:'正在排空缓冲并校验',saved:'原始数据已校验保存',discarded:'已移入回收区',error:'写入失败，数据保留在未完成区'})[d.writer?.status]||'等待录制'}<small>已写入 {d.writer?.frames||0} 帧</small></div>
 <details className="task-settings"><summary>任务与策略参数 · {Number(active?d.metadata?.steps:form.steps)===0?'持续运行':`${active?d.metadata?.steps:form.steps} 步`} · 每块 {Number(active?(d.metadata?.chunk_steps??0):form.chunk_steps)===0?'Checkpoint 默认':`${active?(d.metadata?.chunk_steps??0):form.chunk_steps} 步`}</summary>
 <label>任务描述<textarea value={active?d.metadata?.prompt||form.prompt:form.prompt} disabled={active||pending} onChange={e=>setForm({...form,prompt:e.target.value})}/></label><label>运动总步数（0 = 持续运行）<input type="number" min="0" step="1" value={active?d.metadata?.steps??form.steps:form.steps} disabled={active||pending} onChange={e=>setForm({...form,steps:e.target.value})}/></label><label>每块执行步数（0 = Checkpoint 默认）<input type="number" min="0" step="1" value={active?d.metadata?.chunk_steps??form.chunk_steps:form.chunk_steps} disabled={active||pending} onChange={e=>setForm({...form,chunk_steps:e.target.value})}/></label></details>
 <details className="storage-settings"><summary>保存路径与数据设置</summary><label>当前保存根目录<code className="storage-current-path">{d.data_root||'读取中…'}</code></label><button className="wide" disabled={!online||pending||active||job||s?.connected||!d.data_root} onClick={()=>setFolderPicker(true)}><FolderOpen size={16}/>选择文件夹</button><p className="hint">选择 qijun 电脑上的文件夹。断开机械臂后可修改；旧数据不移动。</p><div className="storage-formats"><b>episodes/</b><span>原始试验 · HDF5 + 三路视频</span><b>exports/</b><span>可选训练片段 · LeRobot v3.0</span></div></details>
 {d.error&&<div className="dagger-error"><AlertTriangle size={17}/>{d.error}</div>}</section></div></div>
 <section className="episode-library"><div className="section-head"><div><h2><FolderOpen size={18}/>试验记录</h2><p className="hint">原始试验保留完整过程；训练导出默认只使用连续人工纠正段。</p></div><small>{(d.episodes||[]).length} 条记录</small></div>
 {job&&<div className="export-progress"><span>{d.task?.message||'数据任务处理中…'}</span><progress max="100" value={d.task?.progress||0}/></div>}{d.task?.status==='error'&&<p className="dagger-error">{d.task.error}</p>}{d.task?.status==='complete'&&<p className="export-success"><Check size={16}/>{d.task.message||'处理完成'}{d.task.path&&<small>{d.task.path}</small>}</p>}
 <div className="episode-list">{!(d.episodes||[]).length?<div className="episode-empty"><FolderOpen size={30}/><h3>从第一段纠正开始</h3><p>保存后的记录会出现在这里，可回看三路视频并导出训练数据。</p></div>:(d.episodes||[]).map(ep=><div className="episode-row" key={ep.episode}><button className="episode-name" onClick={()=>setSelected(selected===ep.episode?null:ep.episode)}><span>{new Date(ep.created*1000).toLocaleString()}</span><strong>{ep.prompt}</strong><small>{ep.episode}</small></button><div><b>{ep.frames} 帧</b><small>人工 {ep.human_frames||0} 帧 · {ep.status==='writing'&&active&&ep.episode===d.episode?'正在录制':ep.recoverable?'未完成 / 可恢复':({success:'成功',failure:'失败',unfinished:'未完成'})[ep.result]||'已保存'}</small></div><div className="episode-buttons">{ep.status==='saved'&&<button disabled={active||s?.busy} onClick={()=>onReplay(ep.episode)}><Play size={16}/>动作回放</button>}<button disabled={job||s?.connected||active} onClick={()=>{if(ep.recoverable)send('dagger_recover',false,{episode:ep.episode});else{setSelected(ep.episode);send('dagger_inspect',false,{episode:ep.episode})}}}>{ep.recoverable?<RotateCcw size={16}/>:<Download size={16}/>} {ep.recoverable?'恢复数据':'选择导出'}</button><button aria-label="回收记录" disabled={job||s?.connected||active} onClick={()=>{if(window.confirm('将该记录移入回收区？'))send('dagger_trash',true,{episode:ep.episode})}}><Trash2 size={16}/></button></div></div>)}</div>
 <p className="hint">导出和数据恢复前需断开机械臂，避免处理负载干扰控制。原始数据不会因导出而改变。加载验证通过不代表已核实原训练集的时间对齐，不应直接混入原数据训练。</p>
 {selected&&detail&&<div className="episode-detail"><div className="section-head"><h3>同步回看 · {detail.result==='success'?'成功':'试验记录'}</h3><div className="replay-buttons"><button onClick={playReplay}>播放三路视频</button><button onClick={()=>{videos.current.head?.pause();sync('pause')}}>暂停回看</button></div><button className="text-button" onClick={()=>setSelected(null)}>收起</button></div><div className="replay-grid">{roles.map(([r,label])=><div key={r}><span>{label}</span><video key={selected+'/'+r} ref={v=>videos.current[r]=v} controls={r==='head'} muted playsInline preload="auto" src={'/api/dagger/episode/'+selected+'/'+r+'.mp4'} onPlaying={r==='head'?()=>sync('play'):undefined} onPause={r==='head'?()=>sync('pause'):undefined} onSeeking={r==='head'?()=>sync('seeking'):undefined} onSeeked={r==='head'?()=>sync('seek'):undefined} onTimeUpdate={r==='head'?()=>sync('time'):undefined} onWaiting={r==='head'?()=>sync('waiting'):undefined} onEnded={r==='head'?()=>sync('pause'):undefined} onRateChange={r==='head'?()=>sync('rate'):undefined} onLoadedMetadata={()=>sync('ready')} onError={()=>setReplayError(label+'加载失败，请重新打开记录')}/></div>)}</div><p className="hint">顶部视频进度条控制三路画面；定位回看会跳转并播放。</p>{replayError&&<p className="dagger-error" role="alert">{replayError}</p>}<p className="hint">{detail.checkpoint_name} · {detail.frames} 帧 · 人工纠正 {detail.human_frames||0} 帧</p>{detail.status==='saved'&&<ExportPanel key={selected} episode={selected} frames={detail.frames} s={s} send={send} pending={pending} videos={videos} onLocate={locateFrame}/>}</div>}
 </section></div>
}
