const $=selector=>document.querySelector(selector);
let authenticated=false;
let polling=false;
let pollTimer=null;
let knownCallIds=new Set();
let soundEnabled=localStorage.getItem('imagine-help-sound')==='on';
let audioContext=null;

async function api(path,options={}){
  const headers={...(options.headers||{})};
  if(options.body&&!headers['Content-Type'])headers['Content-Type']='application/json';
  let response,data;
  try{
    response=await fetch(path,{...options,headers,cache:'no-store',credentials:'same-origin'});
    data=await response.json();
  }catch(error){throw new Error('Impossible de joindre le serveur. Vérifiez la connexion.');}
  if(!response.ok){const error=new Error(data.error||`Erreur serveur (${response.status}).`);error.status=response.status;throw error;}
  return data;
}
function showLogin(error=''){
  authenticated=false;$('#login-panel').hidden=false;$('#dashboard').hidden=true;
  if(error){$('#login-error').textContent=error;$('#login-error').hidden=false;}
  clearInterval(pollTimer);
}
function showDashboard(){
  authenticated=true;$('#login-panel').hidden=true;$('#dashboard').hidden=false;
  $('#login-error').hidden=true;$('#dashboard-error').hidden=true;
  updateSoundButton();pollRequests();clearInterval(pollTimer);pollTimer=setInterval(pollRequests,4000);
}
function updateSoundButton(){
  const button=$('#sound-toggle');button.setAttribute('aria-pressed',String(soundEnabled));
  button.textContent=soundEnabled?'Son activé':'Activer le son';
}
function playAlert(){
  if(!soundEnabled)return;
  try{
    audioContext=audioContext||new (window.AudioContext||window.webkitAudioContext)();
    if(audioContext.state==='suspended')audioContext.resume();
    const now=audioContext.currentTime;
    [0,0.24,0.48].forEach(delay=>{
      const oscillator=audioContext.createOscillator(),gain=audioContext.createGain();
      oscillator.type='sine';oscillator.frequency.value=880;
      gain.gain.setValueAtTime(.0001,now+delay);gain.gain.exponentialRampToValueAtTime(.18,now+delay+.02);gain.gain.exponentialRampToValueAtTime(.0001,now+delay+.18);
      oscillator.connect(gain);gain.connect(audioContext.destination);oscillator.start(now+delay);oscillator.stop(now+delay+.2);
    });
  }catch(error){}
}
function formatTime(value){
  const date=new Date(value);if(Number.isNaN(date.getTime()))return 'À l’instant';
  return new Intl.DateTimeFormat('fr-TN',{hour:'2-digit',minute:'2-digit',day:'2-digit',month:'short'}).format(date);
}
function createCallCard(request){
  const card=document.createElement('article');card.className=`call-card ${request.status==='acknowledged'?'acknowledged':''}`;
  const main=document.createElement('div');main.className='call-main';
  const titleRow=document.createElement('div');titleRow.className='call-title-row';
  const title=document.createElement('strong');title.className='call-title';title.textContent=request.stationLabel||`Borne ${request.stationId}`;
  const status=document.createElement('span');status.className='call-status';status.textContent=request.status==='acknowledged'?'Pris en charge':'Nouvel appel';
  titleRow.append(title,status);main.append(titleRow);
  const time=document.createElement('div');time.className='call-time';time.textContent=`Appel reçu à ${formatTime(request.createdAt)}`;main.append(time);
  if(request.handledBy){const handled=document.createElement('div');handled.className='call-handled';handled.textContent=`Pris en charge par ${request.handledBy}`;main.append(handled);}
  const actions=document.createElement('div');actions.className='call-actions';
  if(request.status==='pending'){
    const ack=document.createElement('button');ack.type='button';ack.textContent='Je m’en occupe';ack.dataset.status='acknowledged';ack.dataset.id=request.id;actions.append(ack);
  }
  const done=document.createElement('button');done.type='button';done.className='finish-button';done.textContent='Terminer l’appel';done.dataset.status='resolved';done.dataset.id=request.id;actions.append(done);
  card.append(main,actions);return card;
}
function renderRequests(requests){
  const host=$('#calls-list');host.replaceChildren();
  if(!requests.length){const empty=document.createElement('div');empty.className='empty-state';empty.innerHTML='<span class="empty-check" aria-hidden="true">✓</span><strong>Aucun appel en attente</strong><div>Les nouvelles demandes apparaîtront ici.</div>';host.append(empty);return;}
  requests.forEach(request=>host.append(createCallCard(request)));
}
async function pollRequests(){
  if(!authenticated||polling)return;polling=true;
  try{
    const data=await api('/api/help-requests');const requests=data.requests||[];
    const newRequests=requests.filter(request=>request.status==='pending'&&!knownCallIds.has(request.id));
    if(newRequests.length)playAlert();
    knownCallIds=new Set(requests.map(request=>request.id));
    renderRequests(requests);$('#dashboard-error').hidden=true;
    $('#refresh-status').textContent=`Mis à jour à ${new Intl.DateTimeFormat('fr-TN',{hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date())}`;
  }catch(error){
    if(error.status===401){showLogin('Votre session a expiré. Reconnectez-vous.');}
    else{$('#dashboard-error').textContent=error.message;$('#dashboard-error').hidden=false;}
  }finally{polling=false;}
}
$('#login-form').addEventListener('submit',async event=>{
  event.preventDefault();const button=event.currentTarget.querySelector('button[type=submit]');button.disabled=true;$('#login-error').hidden=true;
  try{
    const username=$('#username').value.trim(),password=$('#password').value;
    await api('/api/admin/login',{method:'POST',body:JSON.stringify({username,password})});
    $('#login-form').reset();knownCallIds=new Set();showDashboard();
  }catch(error){$('#login-error').textContent=error.message;$('#login-error').hidden=false;}
  finally{button.disabled=false;}
});
$('#calls-list').addEventListener('click',async event=>{
  const button=event.target.closest('button[data-id]');if(!button)return;button.disabled=true;
  try{await api(`/api/help-requests/${encodeURIComponent(button.dataset.id)}`,{method:'POST',body:JSON.stringify({status:button.dataset.status})});await pollRequests();}
  catch(error){$('#dashboard-error').textContent=error.message;$('#dashboard-error').hidden=false;button.disabled=false;}
});
$('#refresh-button').addEventListener('click',pollRequests);
$('#sound-toggle').addEventListener('click',async()=>{
  soundEnabled=!soundEnabled;localStorage.setItem('imagine-help-sound',soundEnabled?'on':'off');
  if(soundEnabled){try{audioContext=audioContext||new (window.AudioContext||window.webkitAudioContext)();await audioContext.resume();playAlert();}catch(error){}}
  updateSoundButton();
});
$('#logout-button').addEventListener('click',async()=>{
  try{await api('/api/admin/logout',{method:'POST',body:JSON.stringify({})});}catch(error){}
  showLogin();$('#password').value='';
});
async function start(){
  updateSoundButton();
  try{const session=await api('/api/admin/session');if(session.authenticated){showDashboard();return;}}
  catch(error){}
  showLogin();
}
start();
