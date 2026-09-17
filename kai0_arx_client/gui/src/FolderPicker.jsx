import React,{useState,useEffect} from 'react';
import {Folder,ChevronRight,ArrowUp,X} from 'lucide-react';
export default function FolderPicker({initial,onChoose,onClose}){
 const [path,setPath]=useState(initial),[data,setData]=useState(null),[error,setError]=useState(''),[loading,setLoading]=useState(false);
 useEffect(()=>{const controller=new AbortController();setLoading(true);setError('');
 (async()=>{try{const session=await(await fetch('/api/session',{signal:controller.signal})).json();const r=await fetch('/api/dagger/folders?path='+encodeURIComponent(path),{headers:{'X-Control-Token':session.token},signal:controller.signal});const result=await r.json();if(!r.ok)throw Error(result.error);setData(result)}catch(e){if(e.name!=='AbortError')setError(e.message)}finally{if(!controller.signal.aborted)setLoading(false)}})();return()=>controller.abort();},[path]);
 return <div className="modal-backdrop"><div className="modal folder-picker" role="dialog" aria-modal="true" aria-label="选择保存文件夹"><button className="close" aria-label="关闭文件夹选择" onClick={onClose}><X/></button><h2>选择保存文件夹</h2><p className="folder-help">浏览 qijun 电脑上的文件夹，选择后作为保存根目录。</p>
 <div className="folder-shortcuts">{data?.shortcuts.map(p=><button key={p.name} onClick={()=>setPath(p.path)}>{p.name}</button>)}</div>
 <div className="folder-location"><button aria-label="上一级文件夹" disabled={loading||!data||data.path===data.parent} onClick={()=>setPath(data.parent)}><ArrowUp size={17}/></button><code>{loading?path:data?.path||path}</code></div>
 {error&&<div className="dagger-error" role="alert">{error}</div>}
 <div className="folder-list" aria-busy={loading}>{loading?<p>正在读取文件夹…</p>:data?.folders.length?data.folders.map(p=><button key={p.name} onClick={()=>setPath(p.path)}><Folder size={18}/><span>{p.name}</span><ChevronRight size={15}/></button>):!error&&<p>此文件夹下没有可见的子文件夹，可直接选择当前位置。</p>}</div>
 {data?.truncated&&<p className="hint">仅显示前 500 个子文件夹。</p>}
 <div className="folder-actions"><button onClick={onClose}>取消</button><button className="primary" disabled={loading||!!error||!data?.writable} onClick={()=>onChoose(data.path)}>使用此文件夹</button></div>{data&&!data.writable&&<p className="hint">当前文件夹没有写入权限，请选择其他位置。</p>}
 </div></div>
}
