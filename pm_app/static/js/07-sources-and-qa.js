"use strict";
function renderMessages() {
  const parent=$("messages");parent.replaceChildren();
  const filtered=allMessages.filter(m=>(!$("source-filter").value||m.source_id===$("source-filter").value)&&(!$("topic-filter").value||m.raw.topic_id===$("topic-filter").value));
  let previous="";
  filtered.forEach(m=>{
    if(previous!==m.source_id){previous=m.source_id;parent.append(el("h3",sourceHumanLabel(m),"source-group"));}
    parent.append(messageCard(m,true));
  });
  if(!filtered.length)parent.append(el("p","Сообщений пока нет. Добавьте материалы в проект.","empty"));
}
async function loadHistory() {
  const pid=selected;
  const parent=$("history");parent.replaceChildren();
  const result=await api(`/api/history?project=${encodeURIComponent(selected)}`);
  if(selected!==pid)return;
  result.answers.forEach(answer=>{
    const d=el("details",null,"panel history-item");
    d.append(el("summary",answer.question),el("small",showDate(answer.created_at)));
    let rendered=false;
    d.addEventListener("toggle",()=>{if(d.open&&!rendered){renderAnswer(answer.response,null,d);rendered=true;}});
    parent.append(d);
  });
  if(!result.answers.length)parent.append(el("p","Сохранённых ответов пока нет.","empty"));
}
function renderSearch(result) {
  const parent=$("search-output");parent.replaceChildren();
  parent.append(el("h2","Найденные обсуждения","search-title"));
  if(!result.hits.length)parent.append(el("p","В выбранной области нет подходящих фрагментов.","empty"));
  result.hits.forEach((hit,i)=>{
    const details=el("details",null,"panel history-item");
    details.open=i===0;
    details.append(el("summary",`Фрагмент ${i+1} · ${hit.sources[0]?sourceHumanLabel(hit.sources[0]):""}`));
    hit.sources.forEach(s=>details.append(messageCard(s)));
    parent.append(details);
  });
}
async function operation(mode) {
  if(busy||!selected)return;
  const question=mode==="summary"?"Краткая сводка":$("question").value.trim();
  if(!question){notify("Введите вопрос или поисковый запрос.",true);$("question").focus();return;}
  $("notification").classList.add("hidden");
  setBusy(true,"models");
  const output=$("answer-output"), isSearch=mode==="search";
  if(isSearch)$("search-output").replaceChildren();else output.replaceChildren();
  try {
    const started=await api(isSearch?"/api/search":"/api/ask",{
      project_id:selected,source_id:$("source-filter").value||null,topic_id:$("topic-filter").value||null,question,summary:mode==="summary"
    });
    let job;
    do {
      await new Promise(resolve=>setTimeout(resolve,800));
      job=await api(`/api/job?id=${encodeURIComponent(started.job_id)}`);
      if(job.stage)setStage(job.stage);
    }while(job.state==="running");
    if(job.state==="error")throw Error(job.error);
    if(isSearch)renderSearch(job.result);else renderAnswer(job.result,question,output);
    await refresh(selected);
  }catch(e){notify("Операция не завершена. Это не означает, что информации нет.\n"+e.message,true);}
  finally{setBusy(false);}
}
