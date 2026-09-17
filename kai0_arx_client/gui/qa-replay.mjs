import {chromium,expect} from '@playwright/test';import assert from 'node:assert/strict';
const b=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'}),p=await b.newPage(),errors=[];p.on('pageerror',e=>errors.push(String(e)));
try{
await p.goto((process.env.REPLAY_GUI_URL||'http://127.0.0.1:8093')+'/#dagger');await p.getByRole('button',{name:'选择导出',exact:true}).click();await p.locator('video').first().waitFor();await p.waitForFunction(()=>[...document.querySelectorAll('video')].every(v=>v.readyState>=2));
await p.evaluate(()=>{window.counts={seeking:0,seeked:0};for(const v of document.querySelectorAll('video'))for(const k of Object.keys(window.counts))v.addEventListener(k,()=>window.counts[k]++)});
await p.getByRole('button',{name:'定位回看',exact:true}).nth(2).click();await p.waitForTimeout(3500);
const status=()=>p.evaluate(()=>({counts:window.counts,videos:[...document.querySelectorAll('video')].map(v=>({time:v.currentTime,paused:v.paused,seeking:v.seeking,dropped:v.getVideoPlaybackQuality().droppedVideoFrames}))}));
const playing=await status();assert(playing.videos.every(v=>v.time>28&&!v.paused),JSON.stringify(playing));assert(playing.counts.seeking<20,JSON.stringify(playing));assert(Math.max(...playing.videos.map(v=>v.time))-Math.min(...playing.videos.map(v=>v.time))<.5);
await p.getByRole('button',{name:'暂停回看',exact:true}).click();await p.waitForTimeout(300);assert((await status()).videos.every(v=>v.paused));
const paused=(await status()).videos[0].time;await p.waitForTimeout(400);assert.equal((await status()).videos[0].time,paused);
await p.getByRole('button',{name:'播放三路视频',exact:true}).click();await p.waitForTimeout(1000);assert((await status()).videos.every(v=>!v.paused&&v.time>paused+.6));
await p.locator('video').first().evaluate(v=>{v.currentTime=5});await p.waitForTimeout(1400);assert((await status()).videos.every(v=>v.time>5.7&&v.time<8));
await p.getByRole('button',{name:'定位回看',exact:true}).last().click();await p.getByRole('button',{name:'暂停回看',exact:true}).click();await p.waitForTimeout(400);assert((await status()).videos.every(v=>v.paused));
assert.deepEqual(errors,[]);console.log(JSON.stringify({pass:true,playing,checks:'locate autoplay, pause/resume, manual seek, rapid pause, no seek feedback loop'}));
}catch(e){console.error(await p.locator('.dagger-error').allTextContents());throw e}finally{await b.close()}
