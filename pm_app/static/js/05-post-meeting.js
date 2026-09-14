"use strict";
async function loadMeetings() {
  if(!selected)return;
  const select=$("meeting-filter"),previous=select.value;
  const result=await api(`/api/meetings?project=${encodeURIComponent(selected)}`);
  meetingSessions=result.meetings||[];
  select.replaceChildren(new Option("Выберите встречу",""));
  meetingSessions.forEach(session=>{
    const value=JSON.stringify({source_id:session.source_id,date_key:session.date_key});
    const channel=session.channel||"Созвон";
    select.add(new Option(`${session.date_label} · ${channel} · ${session.message_count} сообщ.`,value));
  });
  if([...select.options].some(option=>option.value===previous))select.value=previous;
  else if(meetingSessions.length===1)select.selectedIndex=1;
  $("postmeeting-generate").disabled=busy||!meetingSessions.length;
  if(!meetingSessions.length&&!postMeetingResult){
    $("postmeeting-output").replaceChildren(el("p","В проекте пока нет источников, распознанных как созвон или транскрипт встречи.","empty"));
  }
}
function postMeetingPlainText(result) {
  const date=result.meeting_date_label&&result.meeting_date_label!=="Дата не указана"?` ${result.meeting_date_label}`:"";
  const lines=[`Коллеги, фиксирую итоги и договоренности по встрече${date}:`,""];
  Object.keys(POST_MEETING_TITLES).forEach(key=>{
    lines.push(POST_MEETING_TITLES[key]);
    (result.sections[key]||[]).forEach(item=>lines.push(`• ${item.text}`));
    lines.push("");
  });
  return lines.join("\n").trim();
}
function renderPostMeeting(result) {
  postMeetingResult=result;
  const parent=$("postmeeting-output");parent.replaceChildren();
  const panel=el("article",null,"panel postmeeting-result");
  const date=result.meeting_date_label&&result.meeting_date_label!=="Дата не указана"?` ${result.meeting_date_label}`:"";
  panel.append(el("p",`Коллеги, фиксирую итоги и договоренности по встрече${date}:`,"postmeeting-intro"));
  Object.keys(POST_MEETING_TITLES).forEach(key=>{
    const section=el("section",null,"postmeeting-section");section.append(el("h3",POST_MEETING_TITLES[key]));
    const items=result.sections[key]||[];
    if(items.length){const list=document.createElement("ul");items.forEach(item=>list.append(el("li",item.text)));section.append(list);}
    else section.append(el("p","Нет подтверждённых пунктов по этой встрече.","muted"));
    panel.append(section);
  });
  const actions=el("div",null,"postmeeting-actions"),copy=el("button","Скопировать текст","btn primary");
  copy.type="button";copy.addEventListener("click",async()=>{try{await navigator.clipboard.writeText(postMeetingPlainText(result));notify("Post-meeting скопирован.");}catch{notify("Не удалось скопировать автоматически.",true);}});
  actions.append(copy);panel.append(actions);
  if(result.unconfirmed_participants)panel.append(el("p",`Не подтверждены роли участников: ${result.unconfirmed_participants}.`,"notice warning"));
  parent.append(panel);
}
async function generatePostMeeting() {
  if(busy||!selected)return;
  const raw=$("meeting-filter").value;
  if(!raw){notify("Выберите встречу.",true);return;}
  let choice;try{choice=JSON.parse(raw);}catch{notify("Не удалось прочитать выбранную встречу.",true);return;}
  try {
    setBusy(true,"models");$("postmeeting-output").replaceChildren();
    const started=await api("/api/post-meeting",{project_id:selected,source_id:choice.source_id,meeting_date:choice.date_key});
    let job;
    do{await new Promise(resolve=>setTimeout(resolve,800));job=await api(`/api/job?id=${encodeURIComponent(started.job_id)}`);if(job.stage)setStage(job.stage);}while(job.state==="running");
    if(job.state==="error")throw Error(job.error);
    renderPostMeeting(job.result);notify("Post-meeting сформирован.");
  }catch(err){notify("Post-meeting не сформирован.\n"+err.message,true);}finally{setBusy(false);$("postmeeting-generate").disabled=!meetingSessions.length;}
}

