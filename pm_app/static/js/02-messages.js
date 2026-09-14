"use strict";
function participantForSpeaker(speaker){return participants.find(p=>p.speaker===speaker);}
function participantRoleLabel(person){
  if(!person)return "";
  if(person.source==="ai"&&!person.confirmed&&person.side!=="unknown")return `AI: ${PARTICIPANT_SIDE[person.side]||person.side}`;
  return PARTICIPANT_SIDE[person.side]||person.side;
}
function messageCard(source, includeHistory=false) {
  const raw=source.raw, card=el("article",null,"message-card");
  const meta=el("div",null,"message-meta");
  const who=el("span",null,"message-speaker");
  who.append(el("span",raw.speaker||"Автор не указан"));
  const person=participantForSpeaker(raw.speaker||"");
  if(person&&person.side!=="unknown")who.append(el("span",participantRoleLabel(person),"participant-inline-role"+(person.confirmed?" confirmed":" suggested")));
  meta.append(who,el("span",messageTime(raw)));
  if(raw.topic_name)card.append(el("div",raw.topic_name,"topic-chip"));
  card.append(meta,el("p",raw.text));
  if(isAdmin()) {
    const details=el("details",null,"admin-only");
    details.append(el("summary",`${source.id || source.label} · ID и расположение`));
    details.append(el("p",`Источник: ${source.source_id}\nID: ${source.message_id}\nВерсия: ${source.revision}\nЗагружено: ${showDate(source.imported_at)}`));
    if(raw.source_locator)details.append(el("p",JSON.stringify(raw.source_locator)));
    card.append(details);
  }
  if(includeHistory) {
    const actions=el("div",null,"message-actions");
    const edit=el("button","Редактировать","btn ghost compact");
    edit.type="button";
    edit.addEventListener("click",()=>openMessageEditor(source,card));
    actions.append(edit);card.append(actions);
  }
  if(includeHistory && source.revision>1) {
    const history=el("details");
    history.append(el("summary",`Предыдущие версии (${source.revision-1})`));
    let loaded=false;
    history.addEventListener("toggle",async()=>{
      if(!history.open||loaded)return;
      try {
        const r=await api(`/api/revisions?project=${encodeURIComponent(selected)}&message=${source.message_fk}`);
        r.revisions.forEach(v=>{const label=isAdmin()?`Версия ${v.revision} · импорт ${showDate(v.imported_at)}`:`Изменено ${showDate(v.imported_at)}`;history.append(el("p",`${label}\n${v.raw.text}`));});
        loaded=true;
      }catch(e){notify(e.message,true);}
    });
    card.append(history);
  }
  return card;
}
