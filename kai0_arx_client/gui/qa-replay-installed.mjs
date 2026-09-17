import {chromium} from '@playwright/test';
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});const page=await browser.newPage({viewport:{width:1440,height:1000}});
await page.goto('http://127.0.0.1:8092/#replay');await page.getByRole('button',{name:'加载轨迹（不运动）'}).click();
for(let i=0;i<60;i++){let s=await page.evaluate(async()=>await(await fetch('/api/state')).json());if(s.replay.phase==='ready'){if(s.connected||s.cameras||s.replay.total!==1834)throw Error('unexpected real state');console.log('PASS deployed GUI read-only load: 1834 frames, 4 parts; disconnected');break}if(i===59)throw Error(JSON.stringify(s));await page.waitForTimeout(100)}
await page.waitForTimeout(600);await page.screenshot({path:'/tmp/replay-installed.png',fullPage:true});await browser.close();
