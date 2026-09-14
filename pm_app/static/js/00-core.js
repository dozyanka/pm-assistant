/* Local-only UI. Untrusted messages are rendered with textContent, never HTML. */
"use strict";
const $ = (id) => document.getElementById(id);
let state = {csrf_token:"", projects:[], imports:[], user:null}, selected = "", busy = false, allMessages = [], registryEntries = [], meetingSessions = [], participants = [];
let postMeetingResult=null;
let pendingImport=null, pendingConfirm=null;
let noticeTimer, refreshEpoch=0, currentTab="answer";
const SOURCE_NAMES = {client_chat:"Клиентский чат", internal_chat:"Внутренний чат",
  mixed_project_stress_fixture:"Смешанный стресс-тест", email:"Письмо", meeting_transcript:"Транскрипт встречи", unspecified:"Источник"};
const STATUS_NAMES = {supported:"Есть основания", partial:"Частичный ответ",
  conflict:"Есть противоречие", not_found:"Недостаточно данных", not_verified:"Не прошёл проверку", evidence_only:"Цитаты без вывода"};
const REGISTRY_KIND = {TASK:"Задача",DECISION:"Решение",PROMISE:"Обещание",REQUIREMENT:"Требование",OPEN_QUESTION:"Открытый вопрос",DEPENDENCY:"Зависимость"};
const REGISTRY_LIFECYCLE = {proposed:"Предложено",agreed:"Согласовано",open:"Открыто",in_progress:"В работе",completed:"Выполнено",cancelled:"Отменено",superseded:"Заменено",blocked:"Заблокировано",unknown:"Не определён"};
const REGISTRY_REVIEW = {candidate:"Нужно проверить",confirmed:"Подтверждено менеджером",edited:"Исправлено менеджером",excluded:"Исключено"};
const TASK_KINDS = new Set(["TASK","PROMISE","OPEN_QUESTION","DEPENDENCY"]);
const KANBAN_COLUMNS = [
  {id:"todo",label:"К выполнению",lifecycle:"open",states:new Set(["proposed","agreed","open","unknown"])},
  {id:"progress",label:"В работе",lifecycle:"in_progress",states:new Set(["in_progress"])},
  {id:"blocked",label:"Заблокировано",lifecycle:"blocked",states:new Set(["blocked"])},
  {id:"done",label:"Готово",lifecycle:"completed",states:new Set(["completed"])}
];
const POST_MEETING_TITLES = {
  key_results:"Ключевые итоги", client_actions:"С вашей стороны", our_actions:"С нашей стороны",
  fixed:"Зафиксировали", clarify:"Требует уточнения"
};
const PARTICIPANT_SIDE = {our_team:"Наша команда",client:"Клиент",contractor:"Подрядчик",other:"Другая сторона",unknown:"Не определено"};
function isAdmin(){return state.user?.role==="admin";}

function el(tag, text, cls) {
  const n=document.createElement(tag);
  if(text !== undefined && text !== null) n.textContent=String(text);
  if(cls) n.className=cls;
  return n;
}
function notify(text, error=false) {
  clearTimeout(noticeTimer);
  $("notification").textContent=text;
  $("notification").className=error?"error":"";
  noticeTimer=setTimeout(()=>$("notification").classList.add("hidden"),error?18000:8000);
}
async function api(path, data) {
  const opts={cache:"no-store",credentials:"same-origin"};
  if(data!==undefined) {
    opts.method="POST";
    opts.headers={"Content-Type":"application/json"};
    if(state.csrf_token)opts.headers["X-PM-Token"]=state.csrf_token;
    opts.body=JSON.stringify(data);
  }
  const response=await fetch(path,opts);
  let value;
  try {value=await response.json();} catch {throw Error("Сервер вернул нечитаемый ответ. Проверьте окно приложения.");}
  if(!response.ok) {
    if(response.status===401)showAuth(false);
    throw Error(value.error || `HTTP ${response.status}`);
  }
  return value;
}
function showDate(value) {
  if(!value) return "не указана";
  const d=new Date(value);
  return Number.isNaN(d.getTime())?String(value):d.toLocaleString("ru-RU");
}
function messageTime(raw) {
  if(raw.occurred_at) return raw.occurred_at;
  if(raw.date_text&&raw.clock_time) return `${raw.date_text} · ${raw.clock_time}`;
  if(raw.offset_text!==undefined && raw.offset_text!==null) return `смещение записи ${raw.offset_text} · дата неизвестна`;
  if(raw.clock_time) return `${raw.clock_time} · дата неизвестна`;
  return "дата и время не указаны";
}
function setBusy(value, stage) {
  busy=value;
  $("progress").classList.toggle("hidden",!value);
  document.querySelectorAll("button").forEach(b=>{b.disabled=value;});
  $("source-filter").disabled=value; $("topic-filter").disabled=value;
  if($("meeting-filter")) $("meeting-filter").disabled=value;
  if(stage) setStage(stage);
}
function setStage(stage) {
  if(stage.startsWith("topic:")) {
    const parts=stage.split(":");
    setStage(parts.slice(3).join(":"));
    $("progress-title").textContent=`Тема ${parts[1]} из ${parts[2]}: `+$("progress-title").textContent;
    return;
  }
  if(stage.startsWith("source:")) {
    const parts=stage.split(":");
    setStage(parts.slice(3).join(":"));
    $("progress-title").textContent=`Источник ${parts[1]} из ${parts[2]}: `+$("progress-title").textContent;
    return;
  }
  const labels={models:"Проверяем локальные модели",draft:"Читаем выбранные источники",
    repair:"Уточняем обещания, статусы и привязку сроков", verify_unknowns:"Проверяем неопределённости",
    verify:"Проверяем утверждения по источникам",save:"Сохраняем ответ",
    retry:"Повторно составляем ответ по найденным основаниям",
    search:"Ищем сообщения по смыслу и словам", registry_extract:"Извлекаем записи реестра",
    registry_merge:"Объединяем повторы и продолжения",
    registry_save:"Сохраняем проверяемый реестр",
    postmeeting_merge:"Собираем короткий post-meeting"};
  const pieces=stage.split(":");
  $("progress-title").textContent=pieces[0]==="index"?
    `Строим локальный поисковый индекс: ${pieces[1]} из ${pieces[2]}`:
    pieces[0]==="recall"?
    `Перепроверяем пустой ответ: фрагмент ${pieces[1]} из ${pieces[2]}`:
    pieces[0]==="registry_verify"?
    `Проверяем записи реестра: пакет ${pieces[1]} из ${pieces[2]}`:
    pieces[0]==="registry_chunk"&&pieces[3]==="verify"?
    `Проверяем реестр: часть ${pieces[1]} из ${pieces[2]}, пакет ${pieces[4]} из ${pieces[5]}`:
    pieces[0]==="registry_chunk"?
    `Анализируем сообщения для реестра: часть ${pieces[1]} из ${pieces[2]}`:
    pieces[0]==="postmeeting_chunk"&&pieces[3]==="verify"?
    `Проверяем post-meeting: часть ${pieces[1]} из ${pieces[2]}, пакет ${pieces[4]} из ${pieces[5]}`:
    pieces[0]==="postmeeting_chunk"?
    `Разбираем встречу: часть ${pieces[1]} из ${pieces[2]}`:
    pieces[0]==="participants"?
    `Определяем роли участников: группа ${pieces[1]} из ${pieces[2]}`:
    (labels[stage]||"Обработка");
}
async function refresh(prefer) {
  const epoch=++refreshEpoch;
  const updated=await api("/api/state");
  if(epoch!==refreshEpoch)return;
  state=updated;
  $("app-version").textContent=state.version;
  renderAccount();
  renderImportBatches();
  const visibleProjects=state.projects;
  $("project-count").textContent=visibleProjects.length;
  $("import-count").textContent=(state.imports||[]).length;
  const nav=$("projects");nav.replaceChildren();
  if(prefer && state.projects.some(p=>p.id===prefer)) selected=prefer;
  if(!state.projects.some(p=>p.id===selected)) selected=visibleProjects[0]?.id || state.projects[0]?.id || "";
  visibleProjects.forEach(p=>{
    const b=el("button",null,"project-button"+(p.id===selected?" active":""));
    b.dataset.project=p.id;
    b.append(el("span",p.name.slice(0,1).toUpperCase(),"project-letter"),el("span",p.name,"project-caption"),el("small",p.message_count));
    b.addEventListener("click",()=>{if(!busy){selectProject(p.id).catch(e=>notify(e.message,true));}});
    nav.append(b);
  });
  $("welcome").classList.toggle("hidden",Boolean(selected));
  $("project-view").classList.toggle("hidden",!selected);
  if(selected) await loadProject(epoch);
}
async function selectProject(id) {
  selected=id;
  $("question").value="";
  $("answer-output").replaceChildren();$("search-output").replaceChildren(); registryEntries=[]; meetingSessions=[]; participants=[]; postMeetingResult=null;
  $("postmeeting-output").replaceChildren();
  $("source-filter").value=""; $("topic-filter").value="";
  $("import-file").value=""; $("paste-text").value="";
  $("import-panel").classList.add("hidden");
  tab("answer");
  await refresh(id);
}
async function loadProject(epoch) {
  const pid=selected;
  const p=state.projects.find(p=>p.id===selected);
  $("project-title").textContent=p.name;
  $("project-meta").textContent=`${p.message_count} сообщений · ${p.source_count} источников · последний импорт: ${showDate(p.last_import)}`;
  const result=await api(`/api/messages?project=${encodeURIComponent(selected)}`);
  if(epoch!==refreshEpoch || selected!==pid)return;
  allMessages=result.messages;
  const topicFilter=$("topic-filter"), previousTopic=topicFilter.value;
  const topics=new Map();
  allMessages.forEach(m=>{if(m.raw.topic_id)topics.set(m.raw.topic_id,m.raw.topic_name);});
  topicFilter.replaceChildren(new Option("Все темы отдельно",""));
  topics.forEach((name,id)=>topicFilter.add(new Option(name,id)));
  if(topics.has(previousTopic))topicFilter.value=previousTopic;
  $("topic-bar").classList.toggle("hidden",topics.size===0);
  rebuildSourceFilter();
  renderMessages();
  if(currentTab==="registry"||currentTab==="tasks") await loadRegistry();
  if(currentTab==="postmeeting") await loadMeetings();
  if(currentTab==="sources") await loadParticipants();
}
function sourceHumanLabel(source) {
  const raw=source.raw||{};
  const channel=(raw.channel||"").trim();
  const base=channel||SOURCE_NAMES[source.source_type]||source.source_type||"Источник";
  return isAdmin()&&source.source_id?`${base} · ${source.source_id}`:base;
}
function rebuildSourceFilter() {
  const filter=$("source-filter"), previous=filter.value, topic=$("topic-filter").value;
  filter.replaceChildren(new Option("Все источники выбранной темы",""));
  const sources=new Map();
  allMessages.filter(m=>!topic||m.raw.topic_id===topic).forEach(m=>{if(!sources.has(m.source_id))sources.set(m.source_id,m);});
  sources.forEach((source,id)=>filter.add(new Option(sourceHumanLabel(source),id)));
  if(sources.has(previous))filter.value=previous;
}
function tab(which) {
  currentTab=which;
  ["answer","registry","tasks","postmeeting","sources","history"].forEach(name=>{
    $(`${name}-view`).classList.toggle("hidden",name!==which);
    $(`tab-${name}`).classList.toggle("active",name===which);
    $(`tab-${name}`).setAttribute("aria-selected",String(name===which));
  });
}
