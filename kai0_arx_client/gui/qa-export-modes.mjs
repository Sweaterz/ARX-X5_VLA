import {chromium,expect} from '@playwright/test';import assert from 'node:assert/strict';
const b=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});const p=await b.newPage({viewport:{width:1440,height:1000}}),errors=[];p.on('pageerror',e=>errors.push(String(e)));
const state=()=>p.evaluate(async()=>await(await fetch('/api/state')).json());
try{
await p.goto('http://127.0.0.1:8093/#dagger');await p.getByText('页面服务在线',{exact:true}).waitFor();assert((await state()).demo);
await p.getByRole('button',{name:'选择导出',exact:true}).first().click();await p.getByRole('button',{name:'完整有效轨迹',exact:false}).waitFor();
const parts=(await state()).dagger.task.segments;assert(parts.some(s=>!s.intervention)&&parts.some(s=>s.intervention));
await expect(p.getByRole('checkbox',{name:'自动去除 human 开头静止帧',exact:false})).not.toBeChecked();
await p.getByRole('button',{name:'完整有效轨迹',exact:false}).click();const split=parts.find(s=>s.frames>=12);
await p.getByText('排除错误／保持画面',{exact:true}).click();await p.getByLabel('排除起始帧',{exact:true}).fill(String(split.start_frame+4));await p.getByLabel('排除结束帧',{exact:true}).fill(String(split.start_frame+5));await p.getByRole('button',{name:'添加排除',exact:true}).click();await p.waitForTimeout(1100);await expect(p.getByRole('button',{name:'撤销排除 1',exact:true})).toBeVisible();
await p.screenshot({path:'../design/export-modes-full.png',fullPage:true});await p.getByRole('button',{name:'导出 LeRobot v3.0',exact:true}).click();await expect.poll(async()=>{let t=(await state()).dagger.task;return t.operation==='export'&&t.status==='complete'},{timeout:60000}).toBe(true);
let task=(await state()).dagger.task;assert.equal(task.report.selection.mode,'full');assert.equal(task.report.selection.human_motion_trim,false);assert(!task.report.human_motion_trim||task.report.human_motion_trim.config===null);assert.equal(task.report.source_files_modified,false);assert.equal(task.report.segments,parts.length+1);assert.equal(task.report.human_only,false);
await p.getByRole('button',{name:'指定片段',exact:false}).click();await p.getByRole('button',{name:'撤销排除 1',exact:true}).click();for(const part of parts)await p.getByRole('checkbox',{name:'选择片段 '+part.id,exact:true}).setChecked(part.intervention===1);
await p.waitForTimeout(1000);assert.equal(await p.locator('.clip-row input:checked').count(),parts.filter(x=>x.intervention===1).length);
await p.getByRole('button',{name:'定位回看',exact:true}).last().click();assert(await p.locator('video').first().evaluate(v=>v.currentTime>0));
await p.getByRole('button',{name:'导出 LeRobot v3.0',exact:true}).click();await expect.poll(async()=>(await state()).dagger.task.report?.selection?.mode,{timeout:60000}).toBe('selected');await expect.poll(async()=>(await state()).dagger.task.status,{timeout:60000}).toBe('complete');assert((await state()).dagger.task.report.human_only);
for(const width of [1280,390]){await p.setViewportSize({width,height:900});assert(await p.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));}assert.deepEqual(errors,[]);console.log('PASS: modes, stable selections, cut boundaries, mixed labels, selected export, video seeking and responsive layout');
}catch(e){console.log(JSON.stringify((await state()).dagger.task));throw e}finally{await b.close()}
