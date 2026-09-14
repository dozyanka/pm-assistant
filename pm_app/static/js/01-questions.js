"use strict";
function evidenceDetails(ev, source) {
  const d=el("details",null,"evidence");
  const author=(source.raw.topic_name?source.raw.topic_name+" · ":"")+(source.raw.speaker||"Автор не указан");
  const summary=isAdmin()?`${ev.id} · ${sourceHumanLabel(source)} · ${author}`:`${sourceHumanLabel(source)} · ${author}`;
  d.append(el("summary",summary));
  d.append(el("blockquote",ev.quote,"quote"));
  d.append(el("div",`Полное сообщение: ${source.raw.text}`,"source-full"));
  d.append(el("div",messageTime(source.raw),"source-full"));
  if(isAdmin()) {
    d.append(el("div",`Источник: ${source.source_id}\nID: ${source.message_id} · версия ${source.revision}`,"source-full admin-only"));
    const loc=source.raw.source_locator;
    if(loc) d.append(el("div","Расположение в оригинале: "+JSON.stringify(loc),"source-full admin-only"));
  }
  return d;
}
function renderAnswer(result, question, parent) {
  if(result.groups?.length) {
    const grouping=result.grouping||"topic";
    const isSourceGrouping=grouping==="source";
    const header=el("article",null,"panel");
    header.append(el("h2",isSourceGrouping?"Результаты по источникам":"Результаты по исходным проектам"));
    if(question)header.append(el("p",question,"muted"));
    if(result.failed_source_count)header.append(el("p",`Не удалось проверить источников: ${result.failed_source_count}. Остальные результаты сохранены.`,"notice warning"));
    parent.append(header);
    result.groups.forEach(group=>{
      const section=el("section",null,"topic-result");
      const title=group.label||group.topic_name||group.source_id||"Без темы";
      section.append(el("h2",title,"topic-result-title"));
      renderAnswer(group.answer,null,section);parent.append(section);
    });
    return;
  }
  const panel=el("article",null,"panel");
  const heading=el("div",null,"answer-heading");
  heading.append(el("h2","Ответ по источникам"));
  heading.append(el("span",STATUS_NAMES[result.status]||result.status,"status"+(result.status==="supported"?"":" warning")));
  panel.append(heading);
  if(question)panel.append(el("p",question,"muted"));
  const lookup=new Map(result.sources.map(s=>[s.id,s]));
  if(result.message)panel.append(el("p",result.message,"claim-text"));
  result.claims.forEach(claim=>{
    const block=el("div",null,"claim");
    block.append(el("p",claim.text,"claim-text"));
    claim.evidence.forEach(ev=>{const src=lookup.get(ev.id);if(src)block.append(evidenceDetails(ev,src));});
    panel.append(block);
  });
  if(result.evidence_candidates?.length) {
    panel.append(el("h3","Выбранные фрагменты и соседние сообщения"));
    result.evidence_candidates.forEach(source=>{
      const card=messageCard(source);
      card.prepend(el("div",source.selected_by_lookup?"Выбрано моделью поиска":"Соседнее сообщение","muted"));
      panel.append(card);
    });
  }
  if(result.uncertainties?.length) {
    panel.append(el("h3","Что требует уточнения"));
    result.uncertainties.forEach(t=>panel.append(el("p",t,"muted")));
  }
  if(result.date_notice)panel.append(el("p",result.date_notice,"notice warning"));
  if(result.diagnostics && isAdmin()) {
    const details=el("details",null,"diagnostics");
    details.append(el("summary",`Диагностика ответа · v${result.diagnostics.pipeline_version}`));
    details.append(el("p","Технические этапы и счётчики. В этом блоке нет текстов сообщений, вопроса и названия проекта.","muted"));
    const text=JSON.stringify(result.diagnostics,null,2);
    const copy=el("button","Скопировать диагностику","btn secondary");
    copy.addEventListener("click",async()=>{
      try {
        if(!navigator.clipboard)throw Error("clipboard unavailable");
        await navigator.clipboard.writeText(text);
        notify("Диагностика скопирована.");
      } catch {
        notify("Не удалось скопировать автоматически. Выделите текст диагностики ниже и скопируйте вручную.",true);
      }
    });
    details.append(copy,el("pre",text,"diagnostic-json"));
    panel.append(details);
  }
  parent.append(panel);
}
