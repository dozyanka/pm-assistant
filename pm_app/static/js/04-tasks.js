"use strict";
function trackerEntries() {
  return registryEntries.filter(entry=>TASK_KINDS.has(entry.kind)&&entry.review_status!=="excluded"&&!entry.stale&&!["cancelled","superseded"].includes(entry.lifecycle));
}
function taskColumn(entry) {
  if(entry.kind==="DEPENDENCY"&&entry.lifecycle!=="completed")return KANBAN_COLUMNS.find(column=>column.id==="blocked");
  return KANBAN_COLUMNS.find(column=>column.states.has(entry.lifecycle))||KANBAN_COLUMNS[0];
}
async function moveTask(entry,lifecycle) {
  if(busy||entry.lifecycle===lifecycle)return;
  try {
    setBusy(true,"save");
    await api("/api/registry/action",{project_id:selected,entry_id:entry.id,action:"move",patch:{lifecycle}});
    await loadRegistry();
    notify(`«${entry.subject}» перемещено: ${REGISTRY_LIFECYCLE[lifecycle]||lifecycle}.`);
  } catch(err){notify(err.message,true);} finally{setBusy(false);}
}
function renderTaskTracker() {
  const summary=$("task-summary"),board=$("kanban");
  if(!summary||!board)return;
  summary.replaceChildren();board.replaceChildren();
  const rows=trackerEntries();
  const active=rows.filter(e=>e.lifecycle!=="completed");
  const stats=[
    ["Активные",active.length],
    ["Со сроком",active.filter(e=>e.deadline_text||e.deadline_iso).length],
    ["Блокеры",active.filter(e=>e.lifecycle==="blocked"||e.kind==="DEPENDENCY").length],
    ["Нужно проверить",active.filter(e=>e.review_status==="candidate").length]
  ];
  stats.forEach(([label,value])=>{const card=el("div",null,"task-stat");card.append(el("strong",value),el("span",label));summary.append(card);});
  KANBAN_COLUMNS.forEach(column=>{
    const wrap=el("section",null,"kanban-column"),head=el("div",null,"kanban-head"),body=el("div",null,"kanban-cards");
    const columnRows=rows.filter(entry=>taskColumn(entry).id===column.id);
    head.append(el("h3",column.label),el("span",columnRows.length,"kanban-count"));wrap.append(head,body);
    body.dataset.lifecycle=column.lifecycle;
    body.addEventListener("dragover",e=>{e.preventDefault();body.classList.add("drag-over");});
    body.addEventListener("dragleave",()=>body.classList.remove("drag-over"));
    body.addEventListener("drop",e=>{e.preventDefault();body.classList.remove("drag-over");const id=Number(e.dataTransfer?.getData("text/plain"));const entry=rows.find(x=>x.id===id);if(entry)moveTask(entry,column.lifecycle);});
    columnRows.forEach(entry=>{
      const card=el("article",null,"kanban-card");card.draggable=true;
      card.addEventListener("dragstart",e=>{e.dataTransfer.effectAllowed="move";e.dataTransfer.setData("text/plain",String(entry.id));});
      const badges=el("div",null,"kanban-badges");
      badges.append(registryBadge(REGISTRY_KIND[entry.kind]||entry.kind,"kind"));
      if(entry.review_status==="candidate")badges.append(registryBadge("Нужно проверить","review"));
      card.append(badges,el("h4",entry.subject),el("p",entry.statement,"kanban-statement"));
      const meta=el("div",null,"kanban-meta");
      if(entry.responsible){const person=participantForSpeaker(entry.responsible);const side=person&&person.side!=="unknown"?` · ${PARTICIPANT_SIDE[person.side]||person.side}`:"";meta.append(el("span",`Ответственный: ${entry.responsible}${side}`));}
      if(entry.deadline_text||entry.deadline_iso)meta.append(el("span",`Срок: ${entry.deadline_text||entry.deadline_iso}`));
      if(meta.childNodes.length)card.append(meta);
      const move=document.createElement("select");move.className="kanban-move";move.setAttribute("aria-label","Переместить карточку");
      KANBAN_COLUMNS.forEach(target=>move.add(new Option(target.label,target.lifecycle)));
      move.value=column.lifecycle;
      move.addEventListener("change",()=>moveTask(entry,move.value));card.append(move);body.append(card);
    });
    if(!columnRows.length)body.append(el("p","Нет карточек","kanban-empty"));
    board.append(wrap);
  });
  if(!rows.length)board.prepend(el("p","В таск-трекере пока нет рабочих карточек. Обновите реестр из сообщений — сюда попадут задачи, обещания, открытые вопросы и зависимости.","empty kanban-wide"));
}
