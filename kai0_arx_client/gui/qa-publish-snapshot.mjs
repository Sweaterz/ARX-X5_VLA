// UI-only fixtures against a --demo backend; no hardware or real data actions.
import {chromium,expect} from '@playwright/test';
import fs from 'node:fs';
const browser=await chromium.launch({headless:true,...(process.env.CHROME_PATH?{executablePath:process.env.CHROME_PATH}:{})});
const page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];let connected=false,checkpoint='/test/fold',prompt='fold the paper boxes.';
page.on('pageerror',e=>errors.push(e.message));
const episode='ep_1_aaaaaaaa';const parts=[{id:'1:0:29',segment:1,intervention:1,start_frame:0,end_frame:29,frames:30,duration_s:1}];
await page.route('**/api/state',async route=>{const response=await route.fetch();const s=await response.json();if(!s.demo)throw Error('Requires --demo backend');s.connected=connected;s.mode=connected?'holding':'disconnected';s.local_server.checkpoint=checkpoint;s.local_server.default_prompt=prompt;s.dagger.episodes=[{episode,created:1,status:'saved',frames:30,human_frames:30,prompt:'gripper correction',result:'success'}];s.dagger.task={episode,operation:'inspect',status:'complete',segments:parts};await route.fulfill({json:s})});
await page.route('**/api/action',route=>route.fulfill({json:{ok:true}}));
await page.route('**/api/dagger/episode/'+episode,route=>route.fulfill({json:{status:'saved',result:'success',frames:30,human_frames:30}}));
await page.goto((process.env.ARX_GUI_TEST_URL||'http://127.0.0.1:18093')+'/#control');await expect(page.getByText('页面服务在线',{exact:true})).toBeVisible();
await expect(page.getByLabel('任务描述（Prompt）')).toHaveValue(prompt);await expect(page.getByLabel('运动总步数（0 = 持续运行）')).toHaveValue('0');
await expect(page.getByRole('button',{name:'一键修复连接冲突',exact:true})).toBeEnabled();connected=true;await expect(page.getByRole('button',{name:'一键修复连接冲突',exact:true})).toBeDisabled();connected=false;
await page.getByLabel('任务描述（Prompt）').fill('manual');await page.waitForTimeout(1000);await expect(page.getByLabel('任务描述（Prompt）')).toHaveValue('manual');checkpoint='/test/plate';prompt='pick up the plate';await expect(page.getByLabel('任务描述（Prompt）')).toHaveValue(prompt);
await page.getByRole('button',{name:'DAgger 采集',exact:true}).click();await page.getByRole('button',{name:'选择导出',exact:true}).click();await expect(page.getByRole('checkbox',{name:'自动去除 human 开头静止帧',exact:false})).not.toBeChecked();
await page.getByRole('checkbox',{name:'自动去除 human 开头静止帧',exact:false}).check();await expect(page.getByRole('checkbox',{name:'自动去除 human 开头静止帧',exact:false})).toBeChecked();
await page.setViewportSize({width:1280,height:800});if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Horizontal overflow');
if(process.env.ARX_QA_ARTIFACT_DIR){fs.mkdirSync(process.env.ARX_QA_ARTIFACT_DIR,{recursive:true});await page.screenshot({path:process.env.ARX_QA_ARTIFACT_DIR+'/publish-gui.png',fullPage:true})}
if(errors.length)throw Error(errors.join('\n'));console.log('PASS continuous default, prompt association/manual edits, disconnected-only repair, trim opt-in, 1280 layout');await browser.close();
