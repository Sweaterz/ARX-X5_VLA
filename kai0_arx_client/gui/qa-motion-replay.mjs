import {chromium} from '@playwright/test';
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const page=await browser.newPage({viewport:{width:1440,height:1050}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
await page.goto('http://127.0.0.1:8093/#replay');
async function state(){return page.evaluate(async()=>await(await fetch('/api/state')).json())}
async function wait(fn){for(let i=0;i<150;i++){let s=await state();if(fn(s))return s;await page.waitForTimeout(100)}throw Error('state timeout '+JSON.stringify(await state()))}
await page.getByRole('button',{name:'加载轨迹（不运动）'}).click();await wait(s=>s.replay.phase==='ready');
if((await state()).connected)throw Error('loading connected hardware');
await page.getByRole('button',{name:'控制台',exact:true}).click();await page.getByRole('button',{name:'连接机械臂',exact:true}).click();await page.getByRole('button',{name:'确认执行'}).click();await wait(s=>s.connected&&!s.busy);
await page.getByRole('button',{name:'双臂归位',exact:true}).click();await wait(s=>s.mode==='holding'&&!s.busy);
await page.getByRole('button',{name:'Replay 回放'}).click();
await page.getByRole('button',{name:'前往起点',exact:true}).click();await page.getByRole('button',{name:'确认执行'}).click();await wait(s=>s.replay.phase==='ready'&&!s.busy);
await page.getByRole('button',{name:'开始回放',exact:true}).click();await page.getByRole('button',{name:'确认执行'}).click();await wait(s=>s.replay.phase==='playing');
await page.getByRole('button',{name:'DAgger 采集',exact:true}).click();await page.getByRole('button',{name:'Replay 回放'}).click();
await page.getByRole('button',{name:'暂停并保持',exact:true}).click();await wait(s=>s.replay.phase==='paused'&&s.mode==='holding'&&!s.busy);
await page.getByRole('button',{name:'继续当前片段',exact:true}).click();await page.getByRole('button',{name:'确认执行'}).click();await wait(s=>s.replay.phase==='boundary'&&!s.busy);
await page.screenshot({path:'/tmp/replay-motion-desktop.png',fullPage:true});
await page.setViewportSize({width:1280,height:800});await page.screenshot({path:'/tmp/replay-motion-compact.png',fullPage:true});
if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('horizontal overflow');
await page.getByRole('button',{name:'控制台',exact:true}).click();await page.getByRole('button',{name:'断开机械臂',exact:true}).click();await page.getByRole('button',{name:'确认执行'}).click();await wait(s=>!s.connected&&!s.busy);
if(errors.length)throw Error(errors.join('\n'));console.log('PASS load, align, replay, tab persistence, pause, resume, boundary hold, desktop layouts; mock only');await browser.close();
