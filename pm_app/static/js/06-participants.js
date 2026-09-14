"use strict";
function participantStatus(person){
  if(person.source==="manager")return "Подтверждено PM";
  if(person.source==="explicit")return "Определено по явной подписи";
  if(person.source==="structural")return "Определено по типу источника";
  if(person.source==="ai")return person.side==="unknown"?`AI: недостаточно данных · ${person.confidence}%`:`Предположение AI · ${person.confidence}%`;
  return "Не определено";
}
function participantKindLabel(person){
  if(person.identity_kind==="role_alias")return "Роль без имени";
  if(person.identity_kind==="service_account")return "Служебный адрес";
  return "Участник";
}
function participantChannels(person){
  const values=(person.channels||[]).map(value=>SOURCE_NAMES[value]||value).filter(Boolean);
  return [...new Set(values)].join(", ")||"канал не указан";
}
function participantExampleTime(example){
  if(example.occurred_at)return showDate(example.occurred_at);
  if(example.date_text&&example.clock_time)return `${example.date_text} · ${example.clock_time}`;
  if(example.date_text)return example.date_text;
  if(example.clock_time)return example.clock_time;
  return "дата не указана";
}
function participantExamples(person){
  const details=el("details",null,"participant-examples");
  const shown=(person.examples||[]).length;
  details.append(el("summary",shown?`Показать сообщения (${shown} из ${person.message_count})`:"Сообщения не найдены"));
  (person.examples||[]).forEach(example=>{
    const card=el("div",null,"participant-example");
    const channel=example.channel||SOURCE_NAMES[example.source_type]||example.source_type||"Источник";
    card.append(el("div",`${channel} · ${participantExampleTime(example)}`,"participant-example-meta"));
    card.append(el("blockquote",example.text,"participant-example-text"));
    details.append(card);
  });
  return details;
}
function participantCard(person){
  const row=el("article",null,"participant-row participant-kind-"+(person.identity_kind||"person"));
  const main=el("div",null,"participant-main");
  const identity=el("div",null,"participant-identity");
  const title=el("div",null,"participant-title-line");
  title.append(el("strong",person.speaker),el("span",participantKindLabel(person),"participant-kind-badge"));
  identity.append(title);
  const identityHint=person.identity_kind==="role_alias"?"Имя человека в источнике не указано":person.identity_kind==="service_account"?"Адрес отправителя/служебная учётная запись, не отдельный человек":"Имя из исходных сообщений";
  identity.append(el("small",`${identityHint} · ${person.message_count} сообщ. · ${participantChannels(person)}`));
  main.append(identity,participantExamples(person));
  const status=el("span",participantStatus(person),"participant-status "+(person.confirmed?"confirmed":person.source==="ai"?"suggested":"unknown"));
  const controls=el("div",null,"participant-controls");
  const select=document.createElement("select");select.className="participant-side";
  Object.entries(PARTICIPANT_SIDE).forEach(([value,label])=>select.add(new Option(label,value)));
  select.value=person.side||"unknown";
  const save=el("button",person.confirmed?"Изменить":"Подтвердить","btn secondary compact");save.type="button";
  save.addEventListener("click",async()=>{
    if(busy)return;
    try{setBusy(true,"save");await api("/api/participants/role",{project_id:selected,speaker:person.speaker,side:select.value});await loadParticipants();renderMessages();notify(`Роль «${person.speaker}» сохранена.`);}catch(err){notify(err.message,true);}finally{setBusy(false);}
  });
  controls.append(select,save);
  const right=el("div",null,"participant-role-side");right.append(status,controls);
  row.append(main,right);
  return row;
}
function participantGroup(title,rows,cls=""){
  if(!rows.length)return null;
  const section=el("section",null,"participant-group "+cls);
  const head=el("div",null,"participant-group-head");head.append(el("h3",title),el("span",rows.length,"participant-group-count"));
  section.append(head);
  rows.forEach(person=>section.append(participantCard(person)));
  return section;
}
function renderParticipants(){
  const parent=$("participants-list"),summary=$("participants-summary");
  if(!parent||!summary)return;
  parent.replaceChildren();summary.replaceChildren();
  const people=participants.filter(p=>(p.identity_kind||"person")==="person");
  const aliases=participants.filter(p=>p.identity_kind==="role_alias");
  const services=participants.filter(p=>p.identity_kind==="service_account");
  const confirmed=participants.filter(p=>p.confirmed&&p.side!=="unknown").length;
  const suggested=participants.filter(p=>p.source==="ai"&&!p.confirmed&&p.side!=="unknown").length;
  const unknown=participants.filter(p=>p.side==="unknown").length;
  summary.append(el("span",`людей ${people.length}`),el("span",`ролевых подписей ${aliases.length}`),el("span",`служебных адресов ${services.length}`),el("span",`подтверждено ${confirmed}`),el("span",`предположений ${suggested}`),el("span",`не определено ${unknown}`));
  const groups=[
    participantGroup("Люди",people,"people"),
    participantGroup("Роли без имени",aliases,"aliases"),
    participantGroup("Служебные адреса",services,"services")
  ].filter(Boolean);
  groups.forEach(group=>parent.append(group));
  if(!participants.length)parent.append(el("p","В проекте пока нет сообщений с указанными участниками.","empty"));
}
async function loadParticipants(){
  if(!selected)return;
  const result=await api(`/api/participants?project=${encodeURIComponent(selected)}`);
  participants=result.participants||[];renderParticipants();renderMessages();
}
async function inferParticipants(){
  if(busy||!selected)return;
  try{
    setBusy(true,"models");
    const started=await api("/api/participants/infer",{project_id:selected});
    let job;do{await new Promise(resolve=>setTimeout(resolve,700));job=await api(`/api/job?id=${encodeURIComponent(started.job_id)}`);if(job.stage)setStage(job.stage);}while(job.state==="running");
    if(job.state==="error")throw Error(job.error);
    participants=job.result.participants||[];renderParticipants();renderMessages();
    notify(job.result.unresolved?`Роли проанализированы. Не удалось уверенно определить: ${job.result.unresolved}.`:`Роли участников проанализированы. Проверьте AI-предположения и подтвердите их.`);
  }catch(err){notify("Не удалось определить роли участников.\n"+err.message,true);}finally{setBusy(false);}
}

