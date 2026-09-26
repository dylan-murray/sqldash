export function initStudioTour() {
 const key='sqldash-ai-studio-tour-v1';
 let step=-1,returnFocus=null,ticker=null,paused=false,remaining=10000,lastTick=0;
 const steps=[
  {target:'#studio-pick',title:'Pin a change',text:'Select <strong>Annotate dashboard</strong>, then click a tile, title, or chart. A comment box opens right where you point.'},
  {target:'#studio-notes',title:'Describe the change',text:'Describe what you want, then choose <strong>Save request</strong>. Each saved pin appears in this list, ready to send.',example:'Try “Show weekly totals instead of daily.”'},
  {target:'.studio-message-area',title:'Send it together',text:'Choose your coding agent below and add any overall direction. <strong>Send to agent</strong> submits your pinned requests together.'}
 ];
 const tip=document.createElement('aside');tip.className='studio-tour';tip.hidden=true;tip.setAttribute('role','dialog');tip.setAttribute('aria-label','AI Studio walkthrough');document.body.append(tip);
 function clearTarget(){document.querySelectorAll('.tour-target').forEach(el=>el.classList.remove('tour-target'))}
 function place(){
  if(step<0)return;const target=document.querySelector(steps[step].target);if(!target)return;
  clearTarget();target.classList.add('tour-target');
  const anchor=step<2?demo.querySelector(step===0?'.tour-live-pin':demo.dataset.phase==='arrived'?'.tour-queued-card':'.tour-live-form')||target:demo.dataset.phase==='arrived'?demo.querySelector('.tour-agent-processing')||target:target;
  const r=anchor.getBoundingClientRect(),w=tip.offsetWidth,h=tip.offsetHeight;
  let x=step<2?r.right+18:r.left-w-14;
  let y=step<2?r.top:r.top+Math.min(r.height/2,60)-h/2;
  if(x+w>innerWidth-12){x=r.left;y=r.bottom+14;if(y+h>innerHeight-12)y=r.top-h-14;}
  tip.style.left=Math.max(12,Math.min(x,innerWidth-w-12))+'px';
  tip.style.top=Math.max(80,Math.min(y,innerHeight-h-72))+'px';
  tip.dataset.side=step===0?'bottom':'right';
 }
 const demo=document.createElement('div');demo.className='tour-live';demo.hidden=true;demo.inert=true;demo.setAttribute('aria-hidden','true');document.body.append(demo);
 function queueCard(){
  const r=document.getElementById('studio-notes').getBoundingClientRect();
  const card=document.createElement('div');card.className='studio-note-card tour-queued-card';
  card.style.cssText=`left:${r.left+10}px;top:${r.top+6}px;width:${r.width-20}px`;
  card.innerHTML='<span class="studio-note-number">1</span><span class="studio-note-copy"><strong>Example request</strong><span>Show weekly totals instead of daily.</span></span>';
  return card;
 }
 function example(){
  demo.replaceChildren();demo.hidden=false;demo.dataset.step=step;
  const tile=[...document.querySelectorAll('main.container .tile')].find(el=>{const r=el.getBoundingClientRect();return r.bottom>100&&r.top<innerHeight-160});
  if(step<2){
   if(!tile)return;
   const r=tile.getBoundingClientRect();
   const x=Math.max(30,Math.min(r.left+r.width*.55,innerWidth-330)),y=Math.max(110,Math.min(r.top+55,innerHeight-330));
   demo.style.setProperty('--pin-x',x+'px');demo.style.setProperty('--pin-y',y+'px');
   demo.innerHTML='<span class="tour-live-cursor">↖</span><span class="tour-live-pin">1</span>';
   if(step===1){
    const form=document.getElementById('studio-note-form').cloneNode(true);form.hidden=false;form.className='studio-composer tour-live-form';
    form.style.cssText=`left:${Math.max(12,Math.min(x+20,innerWidth-320))}px;top:${Math.max(90,Math.min(y+20,innerHeight-310))}px;width:290px`;
    form.querySelector('textarea').value='Show weekly totals instead of daily.';
    form.querySelector('strong').textContent='Example request';
    form.querySelector('select').value=tile.dataset.tileId;
    demo.append(form);
    const queue=document.getElementById('studio-notes').getBoundingClientRect();
    const card=queueCard();
    const flight=document.createElement('span');flight.className='tour-request-flight';flight.textContent='1';
    flight.style.cssText=`left:${x}px;top:${y}px;--travel-x:${queue.left+24-x}px;--travel-y:${queue.top+32-y}px`;
    demo.append(flight,card);
   }
  }else{
   const original=document.querySelector('.studio-message-box'),r=original.getBoundingClientRect();
   const box=original.cloneNode(true);box.classList.add('tour-live-compose');
   box.style.cssText=`left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px`;
   const field=box.querySelector('textarea'),text=document.createElement('span');text.className='send-demo-typing';text.textContent='Keep the current colors.';field.replaceWith(text);
   const label=document.createElement('small');label.className='tour-live-label';label.textContent='Example';box.prepend(label);demo.append(box);
   const conversation=document.querySelector('.studio-conversation').getBoundingClientRect();
   const processing=document.createElement('div');processing.className='tour-agent-processing';
   processing.style.cssText=`left:${conversation.left+18}px;top:${conversation.top+42}px;width:${conversation.width-36}px;max-height:${Math.max(80,conversation.height-50)}px`;
   processing.innerHTML='<small>Example run</small><strong></strong><div class="tour-agent-line"><span class="tour-agent-spinner"></span><span class="tour-agent-status"></span></div><p>1 requested change included</p>';
   processing.querySelector('strong').textContent=document.getElementById('studio-entrypoint').selectedOptions[0]?.textContent||'Your coding agent';
   demo.append(processing,queueCard());
  }
  for(const el of [demo,...demo.querySelectorAll('*')]){el.removeAttribute('id');el.removeAttribute('for');if(el.matches('button,input,select,textarea'))el.tabIndex=-1;}
  frame();
 }
 function frame(){
  if(step<0)return;
  const elapsed=matchMedia('(prefers-reduced-motion: reduce)').matches?8500:(10000-remaining)*2;
  const old=demo.dataset.phase;
  demo.dataset.phase=elapsed<4000?'writing':elapsed<5200?'saving':elapsed<6400?'moving':'arrived';
  if(step===1){
   const field=demo.querySelector('textarea');if(field)field.value='Show weekly totals instead of daily.'.slice(0,Math.floor(Math.max(0,elapsed-500)/75));
   const flight=demo.querySelector('.tour-request-flight');
   if(flight){const progress=Math.max(0,Math.min(1,(elapsed-5200)/1200)),ease=progress*progress*(3-2*progress);flight.style.transform=`translate(calc(var(--travel-x) * ${ease}),calc(var(--travel-y) * ${ease}))`;}
  }
  if(step===2){
   demo.querySelector('.send-demo-typing').textContent='Keep the current colors.'.slice(0,Math.floor(Math.max(0,elapsed-300)/110));
   demo.querySelector('.tour-agent-status').textContent=elapsed<7300?'Reading your request…':elapsed<8800?'Updating the dashboard…':'Changes ready to review';
   demo.querySelector('.tour-agent-processing').dataset.complete=String(elapsed>=8800);
  }
  if(old!==demo.dataset.phase&&demo.dataset.phase==='arrived')place();
 }
 function show(){remaining=10000;lastTick=performance.now();const s=steps[step];tip.hidden=false;tip.innerHTML=`<div class="tour-heading"><h2>${s.title}</h2></div><p>${s.text}</p><div class="tour-progress" aria-label="Step ${step+1} of 3">${steps.map((_,i)=>`<i class="${i===step?'current':''}"></i>`).join('')}</div><footer><button data-tour="skip">Skip tour</button><div><button class="tour-timer" data-tour="pause" aria-label="Pause automatic tour"><span class="tour-seconds">10s</span> <span class="tour-pause-label">Ⅱ</span></button>${step?'<button data-tour="back">Back</button>':''}<button class="tour-next" data-tour="next">${step===2?'Done':'Next →'}</button></div></footer>`;example();place();updateTimer();}
 function finish(){clearInterval(ticker);ticker=null;step=-1;tip.hidden=true;demo.hidden=true;demo.replaceChildren();clearTarget();if(tip.contains(document.activeElement)&&returnFocus?.isConnected)returnFocus.focus({preventScroll:true});}
 function start(){if(document.getElementById('studio-panel').hidden)return;returnFocus=document.activeElement;try{localStorage.setItem(key,'seen')}catch{}step=0;paused=matchMedia('(prefers-reduced-motion: reduce)').matches;show();clearInterval(ticker);ticker=setInterval(tick,16);}
 tip.addEventListener('click',e=>{const action=e.target.closest('[data-tour]')?.dataset.tour;if(action==='pause'){paused=!paused;updateTimer()}if(action==='skip')finish();if(action==='back'){step--;show()}if(action==='next'){if(step===2)finish();else{step++;show()}}});
 function updateTimer(){if(step<0)return;const held=paused||tip.matches(':hover')||document.hidden;frame();tip.classList.toggle('tour-paused',held);demo.classList.toggle('tour-paused',held);const button=tip.querySelector('[data-tour="pause"]');button.setAttribute('aria-label',paused?'Resume automatic tour':'Pause automatic tour');tip.querySelector('.tour-seconds').textContent=held?'Paused':Math.ceil(remaining/1000)+'s';tip.querySelector('.tour-pause-label').textContent=paused?'▷':'Ⅱ';}
 function tick(){const now=performance.now(),elapsed=now-lastTick;lastTick=now;if(step<0)return;if(!paused&&!tip.matches(':hover')&&!document.hidden)remaining-=elapsed;if(remaining<=0){if(step===2)finish();else{step++;show()}}else updateTimer();}
 document.addEventListener('visibilitychange',()=>{lastTick=performance.now();updateTimer()});
 document.addEventListener('keydown',()=>{if(step>=0){paused=true;updateTimer()}},true);

 const panel = document.getElementById('studio-panel');
 document.getElementById('studio-tour-replay').addEventListener('click', start);
 document.getElementById('studio-close').addEventListener('click', finish);
 document.addEventListener('keydown', event => {
   if (event.key === 'Escape' && step >= 0) { event.preventDefault(); event.stopImmediatePropagation(); finish(); }
 }, true);
 window.addEventListener('resize', () => {if(step>=0){example();place();}});
 window.addEventListener('scroll', () => {if(step>=0){example();place();}}, true);
 let openingTimer;
 window.addEventListener('sqldash:studio-open', () => {
   clearTimeout(openingTimer);
   openingTimer = setTimeout(() => {
     let seen = false;
     try { seen = localStorage.getItem(key); } catch {}
     if (!seen && !panel.hidden && !document.getElementById('studio-message').disabled) start();
   }, 1000);
 });
 document.addEventListener('pointerdown', event => {
   if (step >= 0 && !tip.contains(event.target)) { paused = true; updateTimer(); }
 }, true);
}
