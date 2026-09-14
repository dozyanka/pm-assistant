"use strict";
function registryEvidence(item) {
  const source=item.source;
  return evidenceDetails({id:source.id,quote:item.quote},source);
}
function registryBadge(text, cls="") { return el("span",text,"registry-badge "+cls); }
function registryEditForm(entry, card) {
  const form=el("form",null,"registry-edit");
  const addField=(label,node)=>{const wrap=el("label",label);wrap.append(node);form.append(wrap);};
  const kind=document.createElement("select");
  Object.entries(REGISTRY_KIND).forEach(([value,label])=>kind.add(new Option(label,value)));
  kind.value=entry.kind; addField("Тип",kind);
  const lifecycle=document.createElement("select");
  Object.entries(REGISTRY_LIFECYCLE).forEach(([value,label])=>lifecycle.add(new Option(label,value)));
  lifecycle.value=entry.lifecycle; addField("Статус договорённости",lifecycle);
  const subject=document.createElement("input"); subject.maxLength=160; subject.value=entry.subject; addField("Предмет",subject);
  const statement=document.createElement("textarea"); statement.maxLength=700; statement.rows=3; statement.value=entry.statement; addField("Формулировка",statement);
  const responsible=document.createElement("input"); responsible.maxLength=160; responsible.value=entry.responsible||""; addField("Ответственный (если явно известен)",responsible);
  const deadline=document.createElement("input"); deadline.maxLength=200; deadline.value=entry.deadline_text||""; addField("Срок как в источнике",deadline);
  const actions=el("div",null,"registry-edit-actions");
  const save=el("button","Сохранить исправление","btn primary");save.type="submit";
  const cancel=el("button","Отмена","btn ghost");cancel.type="button";
  cancel.addEventListener("click",()=>form.remove()); actions.append(save,cancel);form.append(actions);
  form.addEventListener("submit",async e=>{
    e.preventDefault(); if(busy)return;
    try {
      setBusy(true,"save");
      await api("/api/registry/action",{project_id:selected,entry_id:entry.id,action:"edit",patch:{
        kind:kind.value,lifecycle:lifecycle.value,subject:subject.value.trim(),statement:statement.value.trim(),
        responsible:responsible.value.trim(),deadline_text:deadline.value.trim()
      }});
      await loadRegistry(); await refresh(selected); notify("Запись исправлена менеджером. История сохранена.");
    } catch(err){notify(err.message,true);} finally{setBusy(false);}
  });
  card.append(form);
}
async function registryAction(entry, action) {
  if(busy)return;
  const labels={confirm:"Запись подтверждена менеджером.",exclude:"Запись исключена, но осталась в истории.",complete:"Статус отмечен менеджером как выполненный."};
  try {
    setBusy(true,"save");
    await api("/api/registry/action",{project_id:selected,entry_id:entry.id,action});
    await loadRegistry(); await refresh(selected); notify(labels[action]||"Реестр обновлён.");
  } catch(err){notify(err.message,true);} finally{setBusy(false);}
}
function renderRegistry() {
  const parent=$("registry-list"); parent.replaceChildren();
  const review=$("registry-review-filter").value,kind=$("registry-kind-filter").value;
  const rows=registryEntries.filter(e=>(!review||e.review_status===review)&&(!kind||e.kind===kind));
  rows.forEach(entry=>{
    const card=el("article",null,"registry-card"+(entry.review_status==="excluded"?" excluded":"")+(entry.stale?" stale":""));
    const top=el("div",null,"registry-card-top");
    const labels=el("div",null,"registry-badges");
    labels.append(registryBadge(REGISTRY_KIND[entry.kind]||entry.kind,"kind"),registryBadge(REGISTRY_LIFECYCLE[entry.lifecycle]||entry.lifecycle,"lifecycle"),registryBadge(REGISTRY_REVIEW[entry.review_status]||entry.review_status,"review"));
    if(entry.stale)labels.append(registryBadge("Не найдено при последнем обновлении","stale"));
    if(entry.superseded_by?.length)labels.append(registryBadge(isAdmin()?`Заменено записью #${entry.superseded_by.join(", #")}`:"Заменено новой записью","stale"));
    top.append(labels); if(isAdmin())top.append(el("span",`#${entry.id}`,"registry-id admin-only")); card.append(top);
    if(entry.topic_id)card.append(el("div",allMessages.find(m=>m.raw.topic_id===entry.topic_id)?.raw.topic_name||entry.topic_id,"topic-chip"));
    card.append(el("h3",entry.subject),el("p",entry.statement,"registry-statement"));
    const meta=el("div",null,"registry-fields");
    meta.append(el("div",`Ответственный\n${entry.responsible||"не указан"}`),el("div",`Срок\n${entry.deadline_text||"не указан"}`));
    if(entry.deadline_iso)meta.append(el("div",`Нормализованная дата\n${entry.deadline_iso}`));
    card.append(meta);
    if(entry.supersedes?.length)card.append(el("p",isAdmin()?`Явно заменяет записи: #${entry.supersedes.join(", #")}.`:`Заменяет предыдущую договорённость.`,`notice`));
    const evidence=el("details",null,"registry-evidence"); evidence.append(el("summary",`Основания (${entry.evidence.length})`));
    entry.evidence.forEach(ev=>evidence.append(registryEvidence(ev))); card.append(evidence);
    const buttons=el("div",null,"registry-actions");
    if(entry.review_status!=="confirmed"&&entry.review_status!=="excluded"){
      const confirm=el("button","Подтвердить","btn primary");confirm.addEventListener("click",()=>registryAction(entry,"confirm"));buttons.append(confirm);
    }
    if(entry.review_status!=="excluded"){
      const edit=el("button","Исправить","btn secondary");edit.addEventListener("click",()=>{if(!card.querySelector(".registry-edit"))registryEditForm(entry,card);});buttons.append(edit);
      if(entry.lifecycle!=="completed"){
        const done=el("button","Выполнено","btn secondary");done.addEventListener("click",()=>registryAction(entry,"complete"));buttons.append(done);
      }
      const exclude=el("button","Исключить","btn ghost");exclude.addEventListener("click",()=>registryAction(entry,"exclude"));buttons.append(exclude);
    }
    card.append(buttons);
    if(isAdmin()) {
      const history=el("details",null,"registry-history admin-only");history.append(el("summary","История записи"));let loaded=false;
      history.addEventListener("toggle",async()=>{if(!history.open||loaded)return;try{const r=await api(`/api/registry-history?project=${encodeURIComponent(selected)}&entry=${entry.id}`);r.history.forEach(h=>history.append(el("pre",`${showDate(h.changed_at)} · ${h.action}\n${JSON.stringify(h.snapshot,null,2)}`,"registry-history-row")));loaded=true;}catch(err){notify(err.message,true);}});
      card.append(history);
    }
    parent.append(card);
  });
  if(!rows.length)parent.append(el("p",registryEntries.length?"По выбранным фильтрам записей нет.":"Реестр пока пуст. Нажмите «Обновить из сообщений». ","empty"));
}
async function loadRegistry() {
  if(!selected)return;
  const topic=$("topic-filter").value;
  const suffix=topic?`&topic=${encodeURIComponent(topic)}`:"";
  const result=await api(`/api/registry?project=${encodeURIComponent(selected)}${suffix}`);
  registryEntries=result.entries;
  const c=result.counts;
  $("registry-meta").textContent=`Всего ${c.total} · на проверке ${c.candidate} · подтверждено ${c.confirmed} · исправлено ${c.edited} · исключено ${c.excluded}${c.stale?` · устаревших кандидатов ${c.stale}`:""}`;
  renderRegistry();
  renderTaskTracker();
}
async function syncRegistry() {
  if(busy||!selected)return;
  setBusy(true,"models");
  try {
    const started=await api("/api/registry/sync",{project_id:selected,topic_id:$("topic-filter").value||null});
    let job;
    do {await new Promise(resolve=>setTimeout(resolve,800));job=await api(`/api/job?id=${encodeURIComponent(started.job_id)}`);if(job.stage)setStage(job.stage);} while(job.state==="running");
    if(job.state==="error")throw Error(job.error);
    await loadRegistry(); await refresh(selected);
    notify("Реестр обновлён.");
  } catch(err){notify("Реестр не обновлён. Исходные сообщения не изменены.\n"+err.message,true);} finally{setBusy(false);}
}

