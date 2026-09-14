"use strict";
function renderAccount(){
  if(!state.user)return;
  $("account-name").textContent=state.user.display_name||state.user.username;
  $("account-role").textContent=state.user.role==="admin"?"Администратор":"PM";
  $("account-users").classList.toggle("hidden",state.user.role!=="admin");
  document.body.classList.toggle("pm-mode",state.user.role==="pm");
}
function switchSide(which){
  const projects=which==="projects";
  $("project-side").classList.toggle("hidden",!projects);$("import-side").classList.toggle("hidden",projects);
  $("side-projects").classList.toggle("active",projects);$("side-imports").classList.toggle("active",!projects);
}
function renderImportBatches(){
  const parent=$("import-batches");if(!parent)return;parent.replaceChildren();
  (state.imports||[]).forEach(batch=>{
    const card=el("section",null,"dataset-card");
    card.append(el("strong",batch.name),el("small",`${batch.project_count} проектов · ${batch.message_count} сообщений`));
    const list=el("div",null,"dataset-projects");
    (batch.projects||[]).forEach(p=>{
      const b=el("button",null,"dataset-project");
      b.append(el("span",p.name),el("small",p.message_count));
      b.addEventListener("click",()=>selectProject(p.id).catch(e=>notify(e.message,true)));
      list.append(b);
    });
    card.append(list);parent.append(card);
  });
  if(!(state.imports||[]).length)parent.append(el("p","Импортированных наборов пока нет.","empty"));
}
function showAuth(setup){
  $("app-shell").classList.add("hidden");$("auth-screen").classList.remove("hidden");
  $("setup-box").classList.toggle("hidden",!setup);$("login-box").classList.toggle("hidden",setup);
  $("auth-error").classList.add("hidden");
}
function authError(message){$("auth-error").textContent=message;$("auth-error").classList.remove("hidden");}
async function boot(){
  try{
    const auth=await api("/api/auth/status");
    if(auth.setup_required){showAuth(true);return;}
    if(!auth.authenticated){showAuth(false);return;}
    $("auth-screen").classList.add("hidden");$("app-shell").classList.remove("hidden");
    await refresh();
  }catch(e){showAuth(false);authError(e.message);}
}
function closeModal(){
  $("modal-backdrop").classList.add("hidden");
  ["confirm-modal","import-modal","users-modal"].forEach(id=>$(id).classList.add("hidden"));
}
function confirmDialog(title,text,okText="Подтвердить"){
  return new Promise(resolve=>{
    pendingConfirm=resolve;$("confirm-title").textContent=title;$("confirm-text").textContent=text;$("confirm-ok").textContent=okText;
    $("modal-backdrop").classList.remove("hidden");$("confirm-modal").classList.remove("hidden");
  });
}
function bytesToBase64(bytes){let binary="";const step=0x8000;for(let i=0;i<bytes.length;i+=step)binary+=String.fromCharCode(...bytes.subarray(i,i+step));return btoa(binary);}
async function filePayload(file){return {file_name:file.name,data_base64:bytesToBase64(new Uint8Array(await file.arrayBuffer()))};}
function showImportPreview(preview){
  const parent=$("import-preview");parent.replaceChildren();
  if(preview.is_dataset)parent.append(el("p",`${preview.batch_name}: ${preview.projects.length} проектов, ${preview.message_count} сообщений.`,"import-summary"));
  parent.append(el("div",`Новых сообщений: ${preview.added}\nБез изменений: ${preview.unchanged}\nИзменённых сообщений: ${preview.revised}\nНовых проектов: ${preview.new_projects.length}`,"import-stats"));
  if(preview.is_dataset){const list=el("div",null,"preview-projects");preview.projects.forEach(p=>list.append(el("span",`${p.name} · ${p.message_count}`)));parent.append(list);}
  if(preview.revised)parent.append(el("p","Изменённые сообщения будут сохранены как новые версии. Предыдущий текст останется в истории.","notice"));
  $("modal-backdrop").classList.remove("hidden");$("import-modal").classList.remove("hidden");
}
async function previewFile(file,projectId){
  if(!file)return;
  if(file.size>8*1024*1024){notify("Файл должен быть не больше 8 МиБ.",true);return;}
  try{
    setBusy(true,"save");const payload=await filePayload(file);payload.project_id=projectId||null;
    const preview=await api("/api/import/preview",payload);pendingImport={payload,preview};showImportPreview(preview);
  }catch(e){notify(e.message,true);}finally{setBusy(false);}
}
async function applyPendingImport(){
  if(!pendingImport)return;
  try{
    closeModal();setBusy(true,"save");const result=await api("/api/import/apply",pendingImport.payload);const wasDataset=result.is_dataset;
    const target=wasDataset?(result.projects[0]||selected):selected;pendingImport=null;
    await refresh(target);
    if(wasDataset)switchSide("imports");
    notify(`Импорт завершён. Добавлено: ${result.added}; без изменений: ${result.unchanged}; новых версий: ${result.revised}.`);
  }catch(e){notify(e.message,true);}finally{setBusy(false);}
}
function openMessageEditor(source,card){
  if(card.querySelector(".message-edit"))return;
  const form=el("form",null,"message-edit"),area=document.createElement("textarea");area.rows=4;area.maxLength=20000;area.value=source.raw.text;
  const actions=el("div",null,"message-edit-actions"),save=el("button","Сохранить новую версию","btn primary compact"),cancel=el("button","Отмена","btn ghost compact");
  save.type="submit";cancel.type="button";actions.append(save,cancel);form.append(area,actions);card.append(form);cancel.addEventListener("click",()=>form.remove());
  form.addEventListener("submit",async e=>{e.preventDefault();if(!area.value.trim())return;try{setBusy(true,"save");await api("/api/messages/edit",{project_id:selected,message_id:source.message_fk,text:area.value.trim()});await refresh(selected);notify("Сообщение обновлено. Предыдущая версия сохранена.");}catch(err){notify(err.message,true);}finally{setBusy(false);}});
}
async function openUsers(){
  try{
    const r=await api("/api/users"),parent=$("users-list");parent.replaceChildren();
    r.users.forEach(u=>{const row=el("div",null,"user-row");row.append(el("strong",u.display_name),el("span",`@${u.username}`),el("small",u.role==="admin"?"Администратор":"PM"));parent.append(row);});
    $("modal-backdrop").classList.remove("hidden");$("users-modal").classList.remove("hidden");$("account-menu").classList.add("hidden");
  }catch(e){notify(e.message,true);}
}

