const $=s=>document.querySelector(s);
let events=[];
let config={};
let toastTimer;
async function api(path,options={}){
  const headers={...(options.headers||{})};
  if(options.body&&!headers['Content-Type'])headers['Content-Type']='application/json';
  let response,data;
  try{response=await fetch(path,{...options,headers,cache:'no-store',credentials:'same-origin'});data=await response.json();}
  catch(e){throw new Error('Impossible de joindre le serveur. Vérifiez la connexion.');}
  if(!response.ok){const error=new Error(data.error||`Erreur serveur (${response.status}).`);error.status=response.status;throw error;}
  return data;
}
function showError(selector,text){const el=$(selector);el.textContent=text;el.hidden=false;}
function clearError(selector){const el=$(selector);el.textContent='';el.hidden=true;}
function setBusy(button,busy,label){button.disabled=busy;if(label)button.dataset.originalLabel=label;else if(button.dataset.originalLabel)button.textContent=button.dataset.originalLabel;}
function showToast(text){const el=$('#toast');el.textContent=text;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,3600);}
function showLogin(){ $('#login-panel').hidden=false; $('#settings-panel').hidden=true; }
function showSettings(username){$('#login-panel').hidden=true;$('#settings-panel').hidden=false;loadSettings(username);}
function activeEventList(){return events.filter(e=>e.status!=='archived');}
async function loadSettings(username){
  try{
    const [eventData, kioskData]=await Promise.all([api('/api/admin/events'),api('/api/config')]);
    events=eventData.events||[];config=kioskData;
    const select=$('#event-select');
    select.innerHTML='';
    const active=activeEventList();
    if(!active.length){select.innerHTML='<option value="">Aucun événement actif trouvé</option>';throw new Error('Créez ou réactivez un événement dans Imagine Desk avant de configurer la borne.');}
    active.forEach(event=>{const option=document.createElement('option');option.value=event.id;option.textContent=event.name+(event.date?` · ${event.date}`:'');select.append(option);});
    const selected=config.event?.id&&active.some(e=>String(e.id)===String(config.event.id))?String(config.event.id):String(active[0].id);
    select.value=selected;
    const current=active.find(e=>String(e.id)===selected);
    populate(current);
    $('#registrations-enabled').checked=config.registrationsEnabled!==false;
    $('#save-status').textContent=`Connecté en tant que ${username||'administrateur'}`;
    clearError('#settings-error');
  }catch(error){showError('#settings-error',error.message);}
}
function populate(event){if(!event)return;$('#event-name').value=event.name||'';$('#event-date').value=event.date||'';$('#event-venue').value=event.venue||'';}
async function startAdmin(){
  try{const data=await api('/api/admin/session');if(data.authenticated){showSettings(data.username);return;}}
  catch(e){}
  showLogin();
}
$('#login-form').addEventListener('submit',async e=>{
  e.preventDefault();clearError('#login-error');
  const button=e.currentTarget.querySelector('button[type=submit]');
  const username=$('#username').value.trim();const password=$('#password').value;
  setBusy(button,true);
  try{
    const data=await api('/api/admin/login',{method:'POST',body:JSON.stringify({username,password})});
    $('#password').value='';e.currentTarget.reset();showSettings(data.username);showToast('Connexion administrateur réussie.');
  }catch(error){showError('#login-error',error.message);}
  finally{setBusy(button,false);}
});
$('#event-select').addEventListener('change',e=>populate(events.find(event=>String(event.id)===String(e.target.value))));
$('#settings-form').addEventListener('submit',async e=>{
  e.preventDefault();clearError('#settings-error');
  const button=e.currentTarget.querySelector('button[type=submit]');setBusy(button,true);
  $('#save-status').textContent='Enregistrement en cours…';
  const selected=events.find(event=>String(event.id)===String($('#event-select').value));
  try{
    const data=await api('/api/admin/settings',{method:'POST',body:JSON.stringify({eventId:selected?.id,name:$('#event-name').value.trim(),date:$('#event-date').value,venue:$('#event-venue').value.trim(),registrationsEnabled:$('#registrations-enabled').checked})});
    if(selected){selected.name=data.event.name;selected.date=data.event.date;selected.venue=data.event.venue;const option=$('#event-select').selectedOptions[0];option.textContent=selected.name+(selected.date?` · ${selected.date}`:'');}
    $('#save-status').textContent='Enregistré dans Imagine Desk';showToast('Les réglages de la borne ont été enregistrés.');
  }catch(error){showError('#settings-error',error.message);$('#save-status').textContent='';if(error.status===401){showLogin();showError('#login-error','Votre session a expiré. Reconnectez-vous.');}}
  finally{setBusy(button,false);}
});
$('#logout-button').addEventListener('click',async()=>{try{await api('/api/admin/logout',{method:'POST',body:JSON.stringify({})});}catch(e){}showLogin();showToast('Vous êtes déconnecté·e.');});
startAdmin();
