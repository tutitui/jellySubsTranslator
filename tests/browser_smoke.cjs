// Run only against the disposable Jellyfin fixture on localhost:18096.
const {chromium}=require('playwright-core');const fs=require('fs');const path=require('path');const root=path.resolve(__dirname,'..');
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}});page.setDefaultTimeout(15000);const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://127.0.0.1:18096/web/');await page.locator('#txtManualName').waitFor();await page.fill('#txtManualName','translator-integration');
  await page.fill('#txtManualPassword',JSON.parse(fs.readFileSync(path.join(root,'integration/test-auth.json'))).password);await page.getByRole('button',{name:'Sign In',exact:true}).click();await page.waitForTimeout(1000);
  const data=await page.evaluate(()=>ApiClient.getItems(ApiClient.getCurrentUserId(),{Recursive:true,IncludeItemTypes:'Movie',Limit:1}));
  await page.goto('http://127.0.0.1:18096/web/#/details?id='+data.Items[0].Id);await page.locator('.btnMoreCommands').first().click();await page.getByText('Edit subtitles',{exact:true}).click();
  await page.locator('.jst-open-translate').click();const panel=page.locator('.jst-translation-panel');
  const row=panel.locator('.jst-subtitle-row').filter({hasText:'UnindexedSpanish.es.srt'});await row.waitFor();
  if(!await row.locator('.secondaryText').filter({hasText:'Spanish'}).count())throw new Error('Detected source language missing');
  if(await panel.locator('a,progress').count())throw new Error('Extra movie-level controls found');
  if(await panel.locator('button').count()!==await panel.locator('.jst-subtitle-row').count())throw new Error('Extra movie-level button');
  if((await panel.locator('button').allTextContents()).some(s=>!['Translate','Translated','Translating…'].includes(s)))throw new Error('Unexpected movie-level button');
  if(!await panel.locator('.jst-subtitle-row').filter({hasText:'Bitmap.idx'}).locator('button').isDisabled())throw new Error('Image subtitle should not be translatable');
  await row.locator('select').selectOption('en');let posted;let fakeJob;
  await page.route('**/SubtitleTranslator/Jobs',async route=>{
   if(route.request().method()==='POST'){
    posted=route.request().postDataJSON();fakeJob={id:'a1111111-1111-1111-1111-111111111111',state:'queued',auto_save:1,saved:false,target:posted.target,total:1,completed:0,error:''};
    return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(fakeJob)});
   }
   return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(fakeJob?[{...fakeJob,state:'completed',saved:true,completed:1}]:[])});
  });
  const submission=page.waitForRequest(r=>r.url().endsWith('/SubtitleTranslator/Jobs')&&r.method()==='POST');
  await row.getByRole('button',{name:'Translate',exact:true}).click();
  posted=(await submission).postDataJSON();
  if(!posted||posted.target!=='en'||!posted.source_file_id||!posted.srt.includes('Hola, amigo.')||!posted.auto_save)throw new Error('Wrong file, language, or automatic-save setting submitted');
  if(!await row.locator('button').isDisabled())throw new Error('Active job must prevent duplicate clicks');
  await row.getByRole('button',{name:'Translated',exact:true}).waitFor();
  if(errors.length)throw new Error(errors.join('\n'));
  fs.mkdirSync(path.join(root,'artifacts'),{recursive:true});await page.screenshot({path:path.join(root,'artifacts/movie-editor.png'),fullPage:true});
  console.log('Simplified movie panel passed: filename/language + target dropdown + Translate only; real folder source; automatic-save job; completion state.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e.message);process.exit(1)});
