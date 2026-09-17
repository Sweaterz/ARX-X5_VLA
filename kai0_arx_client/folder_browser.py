"""Read-only server folder picker; never reads file contents."""
import os
from pathlib import Path

def browse_folders(value,default):
    if value is not None and (not isinstance(value,str) or len(value)>4096):raise ValueError('文件夹路径无效')
    path=Path(value or default).expanduser()
    if not path.is_absolute():raise ValueError('请选择绝对路径')
    path=path.resolve(strict=True)
    if not path.is_dir():raise ValueError('所选位置不是文件夹')
    entries=[];truncated=False
    with os.scandir(path) as stream:
        for item in stream:
            if item.name.startswith('.'):continue
            try:
                if item.is_dir():entries.append({'name':item.name,'path':str(Path(item.path).resolve())})
            except OSError:continue
            if len(entries)>=500:truncated=True;break
    shortcuts=[]
    for label,p in [('主目录',Path.home()),('Documents',Path.home()/'Documents'),('移动磁盘',Path('/media')),('挂载目录',Path('/mnt')),('当前保存目录',Path(default))]:
        if p.is_dir():shortcuts.append({'name':label,'path':str(p.resolve())})
    return {'path':str(path),'parent':str(path.parent),'folders':sorted(entries,key=lambda x:x['name'].casefold()),
            'shortcuts':shortcuts,'writable':os.access(path,os.W_OK|os.X_OK),'truncated':truncated}
