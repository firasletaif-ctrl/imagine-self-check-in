const $=s=>document.querySelector(s);
const adminStationMatch=window.location.pathname.match(/^\/admin\/([A-Za-z0-9_-]{1,32})\/?$/);
const stationId=adminStationMatch?adminStationMatch[1]:'default';
let events=[];
let settings={};
let smtpReady=false;
let toastTimer;
async function api(path,options={}){
  const headers={...(options.headers||{}),'X-Kiosk-Station':stationId};
  const isFormData=typeof FormData!=='undefined'&&options.body instanceof FormData;
  if(options.body&&!headers['Content-Type']&&!isFormData)headers['Content-Type']='application/json';
  let response,data;
  try{response=await fetch(path,{...options,headers,cache:'no-store',credentials:'same-origin'});data=await response.json();}
  catch(e){throw new Error('Impossible de joindre le serveur. Vérifiez la connexion.');}
  if(!response.ok){const error=new Error(data.error||`Erreur serveur (${response.status}).`);error.status=response.status;throw error;}
  return data;
}
function showError(selector,text){const el=$(selector);el.textContent=text;el.hidden=false;}
function clearError(selector){const el=$(selector);el.textContent='';el.hidden=true;}
function setBusy(button,busy){button.disabled=busy;}
function showToast(text){const el=$('#toast');el.textContent=text;el.hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>el.hidden=true,3600);}
function formatBytes(bytes){if(bytes<1024)return `${bytes} o`;if(bytes<1024*1024)return `${(bytes/1024).toFixed(0)} Ko`;return `${(bytes/(1024*1024)).toFixed(1)} Mo`;}
async function loadEmailAttachments(eventId){
  const list=$('#email-attachments-list'),status=$('#email-attachment-status');list.replaceChildren();status.textContent='Chargement des pièces jointes…';
  if(!eventId){status.textContent='Sélectionne et enregistre un événement avant d’ajouter son programme.';return;}
  try{
    const data=await api(`/api/admin/events/${encodeURIComponent(eventId)}/email-attachments`);const attachments=data.attachments||[];
    if(!attachments.length){const empty=document.createElement('li');empty.className='attachment-empty';empty.textContent='Aucun programme importé pour cet événement.';list.append(empty);}
    for(const file of attachments){
      const item=document.createElement('li');const details=document.createElement('span');details.className='attachment-file';
      const name=document.createElement('strong');name.textContent=file.filename;const size=document.createElement('small');size.textContent=formatBytes(file.sizeBytes||0);details.append(name,size);
      const remove=document.createElement('button');remove.type='button';remove.className='attachment-remove';remove.dataset.deleteAttachment=file.id;remove.textContent='Retirer';remove.setAttribute('aria-label',`Retirer ${file.filename}`);
      item.append(details,remove);list.append(item);
    }
    status.textContent=`${attachments.length}/${data.maxFiles||5} pièce(s) jointe(s) · ${(data.maxTotalBytes||12*1024*1024)/(1024*1024)} Mo maximum au total.`;
  }catch(error){status.textContent=error.message;const item=document.createElement('li');item.className='attachment-empty';item.textContent=error.message;list.append(item);}
}
function showLogin(){ $('#login-panel').hidden=false; $('#settings-panel').hidden=true; }
function showSettings(username){$('#login-panel').hidden=true;$('#settings-panel').hidden=false;loadSettings(username);}
function activeEventList(){return events.filter(e=>e.status!=='archived');}
function fillForm(saved){
  for(const field of document.querySelectorAll('[data-setting]'))field.value=saved[field.dataset.setting]??'';
  $('#registrations-enabled').checked=saved.registrationsEnabled!==false;
  $('#payment-required').checked=saved.paymentRequired===true;
  $('#payment-email-enabled').checked=saved.paymentEmailEnabled===true;
  $('#max-participants').value=Number(saved.maxParticipants||0);
}
async function loadPrices(eventId){
  const host=$('#price-list');host.innerHTML='<span class="muted">Chargement des tarifs…</span>';
  try{
    const data=await api(`/api/admin/events/${encodeURIComponent(eventId)}/prices`);
    const prices=(data.prices||[]).filter(price=>price.active);const currency=events.find(event=>String(event.id)===String(eventId))?.currency||'TND';
    if(!prices.length){host.innerHTML='<span class="muted">Aucun tarif actif : ajoutez-les dans Imagine Desk si l’événement est payant.</span>';return;}
    host.replaceChildren(...prices.map(price=>{const chip=document.createElement('span');chip.className='price-chip';const label=document.createElement('span');label.textContent=`${price.label} · ${price.category||'Participant'}`;const amount=document.createElement('strong');amount.textContent=`${Number(price.amount||0).toFixed(3).replace(/0+$/,'').replace(/\.$/,'')} ${currency}`;chip.append(label,amount);return chip;}));
  }catch(error){host.textContent=error.message;}
}
async function loadSettings(username){
  try{
    const [eventData,settingsData]=await Promise.all([api('/api/admin/events'),api('/api/admin/settings')]);
    events=eventData.events||[];settings=settingsData.settings||{};smtpReady=Boolean(settingsData.smtpConfigured);
    const active=activeEventList();const select=$('#event-select');select.innerHTML='';
    if(!active.length){select.innerHTML='<option value="">Aucun événement actif trouvé</option>';throw new Error('Créez ou réactivez un événement dans Imagine Desk avant de configurer la borne.');}
    active.forEach(event=>{const option=document.createElement('option');option.value=event.id;option.textContent=event.name+(event.date?` · ${event.date}`:'');select.append(option);});
    const selected=settings.eventId&&active.some(e=>String(e.id)===String(settings.eventId))?String(settings.eventId):String(active[0].id);
    select.value=selected;fillForm(settings);
    const current=active.find(e=>String(e.id)===selected);populateEvent(current);loadPrices(selected);loadEmailAttachments(selected);
    const smtp=$('#smtp-status');smtp.className=`smtp-status ${smtpReady?'ready':'missing'}`;smtp.textContent=smtpReady?'Serveur e-mail configuré. Les reçus partiront après confirmation du paiement dans Imagine Desk.':'E-mail non configuré : ajoute les variables SMTP dans Render avant d’activer l’envoi.';
    $('#save-status').textContent=`Connecté en tant que ${username||'administrateur'}`;clearError('#settings-error');
  }catch(error){showError('#settings-error',error.message);}
}
function populateEvent(event){if(!event)return;$('#event-name').value=event.name||'';$('#event-date').value=event.date||'';$('#event-venue').value=event.venue||'';}
async function startAdmin(){try{const data=await api('/api/admin/session');if(data.authenticated){showSettings(data.username);return;}}catch(e){}showLogin();}
$('#login-form').addEventListener('submit',async e=>{
  e.preventDefault();clearError('#login-error');const button=e.currentTarget.querySelector('button[type=submit]');
  const username=$('#username').value.trim(),password=$('#password').value;setBusy(button,true);
  try{const data=await api('/api/admin/login',{method:'POST',body:JSON.stringify({username,password})});$('#login-form').reset();showSettings(data.username);showToast('Connexion administrateur réussie.');}
  catch(error){showError('#login-error',error.message);}finally{setBusy(button,false);}
});
$('#event-select').addEventListener('change',e=>{const id=String(e.target.value);populateEvent(events.find(event=>String(event.id)===id));loadPrices(id);loadEmailAttachments(id);});
$('#settings-form').addEventListener('submit',async e=>{
  e.preventDefault();clearError('#settings-error');const button=e.currentTarget.querySelector('button[type=submit]');setBusy(button,true);$('#save-status').textContent='Enregistrement en cours…';
  const selected=events.find(event=>String(event.id)===String($('#event-select').value));
  const payload={eventId:selected?.id,name:$('#event-name').value.trim(),date:$('#event-date').value,venue:$('#event-venue').value.trim(),
    registrationsEnabled:$('#registrations-enabled').checked,paymentRequired:$('#payment-required').checked,maxParticipants:$('#max-participants').value,
    paymentEmailEnabled:$('#payment-email-enabled').checked};
  for(const field of document.querySelectorAll('[data-setting]'))payload[field.dataset.setting]=field.value;
  try{
    const data=await api('/api/admin/settings',{method:'POST',body:JSON.stringify(payload)});settings=data.settings||settings;
    if(selected){selected.name=data.event.name;selected.date=data.event.date;selected.venue=data.event.venue;const option=$('#event-select').selectedOptions[0];option.textContent=selected.name+(selected.date?` · ${selected.date}`:'');}
    $('#save-status').textContent='Réglages enregistrés';showToast('Les réglages de la borne sont enregistrés.');
  }catch(error){showError('#settings-error',error.message);$('#save-status').textContent='';if(error.status===401){showLogin();showError('#login-error','Votre session a expiré. Reconnectez-vous.');}}
  finally{setBusy(button,false);}
});
$('#upload-email-program').addEventListener('click',async()=>{
  const input=$('#email-program-files'),files=Array.from(input.files||[]),status=$('#email-attachment-status');
  const eventId=$('#event-select').value;const button=$('#upload-email-program');
  if(!eventId){status.textContent='Sélectionne d’abord un événement.';return;}
  if(!files.length){status.textContent='Choisis au moins un fichier PDF ou une photo.';return;}
  setBusy(button,true);let added=0;
  try{
    for(const file of files){
      const form=new FormData();form.append('file',file,file.name);
      await api(`/api/admin/events/${encodeURIComponent(eventId)}/email-attachments`,{method:'POST',body:form});added++;
    }
    status.textContent=`${added} fichier(s) ajouté(s) au programme de cet événement.`;input.value='';
  }catch(error){status.textContent=added?`${added} fichier(s) ajouté(s); arrêt sur erreur : ${error.message}`:error.message;}
  finally{setBusy(button,false);await loadEmailAttachments(eventId);}
});
$('#email-attachments-list').addEventListener('click',async e=>{
  const button=e.target.closest('[data-delete-attachment]');if(!button)return;
  const eventId=$('#event-select').value,id=button.dataset.deleteAttachment,status=$('#email-attachment-status');
  button.disabled=true;status.textContent='Suppression…';
  try{await api(`/api/admin/events/${encodeURIComponent(eventId)}/email-attachments/${encodeURIComponent(id)}/delete`,{method:'POST',body:JSON.stringify({})});status.textContent='Pièce jointe retirée.';}
  catch(error){status.textContent=error.message;}
  finally{await loadEmailAttachments(eventId);}
});
$('#test-email-button').addEventListener('click',async()=>{
  const to=$('#test-email-to').value.trim();const status=$('#email-test-status');status.textContent='';
  const button=$('#test-email-button');if(!to){status.textContent='Indique une adresse e-mail pour le test.';return;}
  setBusy(button,true);status.textContent='Envoi du test…';
  try{await api('/api/admin/test-email',{method:'POST',body:JSON.stringify({to,subjectTemplate:$('#email-subject').value,bodyTemplate:$('#email-body').value})});status.textContent='E-mail de test envoyé. Vérifie aussi les courriers indésirables.';}
  catch(error){status.textContent=error.message;}
  finally{setBusy(button,false);}
});
$('#logout-button').addEventListener('click',async()=>{try{await api('/api/admin/logout',{method:'POST',body:JSON.stringify({})});}catch(e){}showLogin();showToast('Vous êtes déconnecté·e.');});
if(stationId!=='default'){
  $('#station-admin-title').textContent=`Réglages de la borne ${stationId}`;
  document.title=`Administration borne ${stationId} · Imagine Self Check-in`;
  document.querySelectorAll('a[href="/"]').forEach(link=>{link.href=`/${encodeURIComponent(stationId)}`;});
  $('#station-link-hint').hidden=false;
  $('#station-link-hint').textContent=`Lien de cette borne : /${stationId} · Réglages : /admin/${stationId}`;
}
startAdmin();
