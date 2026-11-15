"use strict";

const $ = id => document.getElementById(id);
const labels = {MATCH:"Confirmed match", NEEDS_INSPECTION:"Needs inspection", NO_MATCH:"No match"};
const views = {matches:"Incidents", inventory:"Inventory", notifications:"New incidents", past:"Past incidents", resolved:"Resolved"};
const weights = {MATCH:2, NEEDS_INSPECTION:1, NO_MATCH:0};
const noticeFields = {reason:"Recall reason", scope_raw:"Affected scope", review_reason:"Review note", action:"Source instruction"};
const names = {
  inventory_id:"Stock row", product_name:"Product", name:"Product", ean:"EAN", gtin:"GTIN", lot:"Lot",
  pack_size:"Pack size", best_before:"Best before", use_by:"Use by", quantity:"Quantity", location:"Location",
  status:"Status", barcode_unspecified:"Barcode", date_mark:"Date mark", label_text:"Label text"
};
const operators = {
  equals:"", one_of:"One of ", between_inclusive:"Between ", on_or_before:"On or before ",
  before:"Before ", on_or_after:"On or after ", after:"After ", contains:"Contains ", described_by:""
};
const storageKey = "lotwise-incident-desk-v1";
const collator = new Intl.Collator(undefined, {numeric:true, sensitivity:"base"});
let inventory = null, previous = null, events = [], resolved = {}, nextId = 1;
let state = {}, results = [], revision = null, view = "matches", generation = 0, running = null;
let draftRunning = false, checkError = "", toastTimer, openRecord = null;
let uploadBusy = false, pendingImport = null, storageWarning = false;
const sorting = {matches:{key:"status",direction:-1}, inventory:{key:null,direction:1}};

try {
  const current = localStorage.getItem(storageKey);
  const saved = JSON.parse(current ?? localStorage.getItem("shelfsignal-incident-desk-v1") ?? "null");
  if (Array.isArray(saved?.inventory?.rows)) {
    ({inventory, previous, events, resolved, nextId} = saved);
    if (current === null && previous) {
      // Preserve the active simulation's match baseline across the URL rename.
      const legacyUrl = "https://demo.shelfsignal.invalid/harbor-lemon-oat-biscuits";
      const demoUrl = "https://demo.lotwise.invalid/harbor-lemon-oat-biscuits";
      previous = Object.fromEntries(Object.entries(previous).map(([key, rank]) => [key.replace(legacyUrl, demoUrl), rank]));
    }
  }
  if (!inventory) {
    const prior = JSON.parse(sessionStorage.getItem("shelfsignal-inventory-v1") || "null");
    if (Array.isArray(prior?.inventory?.rows)) inventory = prior.inventory;
  }
} catch { /* The tab can still be used when browser storage is disabled. */ }

function save() {
  try { localStorage.setItem(storageKey, JSON.stringify({inventory, previous, events, resolved, nextId})); }
  catch {
    if (!storageWarning) toast("Browser storage is unavailable. Keep this tab open to retain your incident history.");
    storageWarning = true;
  }
}

function node(tag, text, css) {
  const item = document.createElement(tag);
  if (text !== undefined && text !== null) item.textContent = text;
  if (css) item.className = css;
  return item;
}

function button(text, action, style = "secondary") {
  const element = node("button", text, style);
  element.type = "button";
  element.addEventListener("click", action);
  return element;
}

function empty(title, message) {
  const item = node("div", null, "empty");
  item.append(icon("incidents"), node("strong", title), node("span", message));
  return item;
}

function icon(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  const use = document.createElementNS(ns, "use");
  svg.classList.add("icon");
  svg.setAttribute("aria-hidden", "true");
  use.setAttribute("href", "#icon-" + name);
  svg.append(use);
  return svg;
}
function badge(text, css, glyph) {
  const item = node("span", null, "badge " + css);
  if (glyph) item.append(icon(glyph));
  item.append(node("span", text));
  return item;
}
function chip(status) {
  const icons = {MATCH:"alert", NEEDS_INSPECTION:"inspect", NO_MATCH:"minus"};
  return badge(labels[status], status, icons[status]);
}
function product(row) { return row.product_name || row.name || row.ean || row.gtin || row.product_code || "Inventory row"; }
function label(key) {
  return names[key] || key.replaceAll("_", " ").replace(/^./, char => char.toUpperCase());
}

function timeLabel(value) {
  return value ? new Date(value).toLocaleString([], {dateStyle:"medium", timeStyle:"short"}) : "—";
}

function feedback(message, error = false) {
  const item = $("feedback");
  item.textContent = message;
  item.hidden = !message;
  item.classList.toggle("error", error);
}

function toast(message, newIncident = false, action = null) {
  const item = $("toast");
  item.classList.toggle("incident-update", newIncident);
  item.replaceChildren(...(newIncident ? [icon("bell"), node("span", message)] : [node("span", message)]));
  if (action) item.append(button(action.label, () => { action.run(); item.hidden = true; }, "toast-action"));
  item.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { if (!item.contains(document.activeElement)) item.hidden = true; }, action ? 10000 : 6500);
}

async function request(url, body, multipart = false) {
  const response = await fetch(url, {
    method:"POST", body:multipart ? body : JSON.stringify(body),
    headers:multipart ? {} : {"Content-Type":"application/json"}
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request could not be completed.");
  return data;
}

function firstDate(row) {
  const keys = ["best_before", "use_by", "date_mark", "production_date", "packing_date", "delivery_date"];
  return keys.find(key => row[key]);
}

function sortDate(text) {
  if (!text) return "";
  const iso = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/.exec(text);
  if (iso) return iso[1] + "-" + iso[2] + "-" + (iso[3] || "00");
  const fi = /^(?:(\d{1,2})\.)?(\d{1,2})\.(\d{4})$/.exec(text);
  return fi ? fi[3] + "-" + fi[2].padStart(2,"0") + "-" + (fi[1] || "0").padStart(2,"0") : text;
}

function ordered(entries, table, value) {
  const {key, direction} = sorting[table];
  if (!key) return entries;
  return entries.sort((a, b) => {
    const first = value(a,key), second = value(b,key);
    if (first === "" || second === "") return first === second ? a.index - b.index : first === "" ? 1 : -1;
    return direction * collator.compare(first, second) || a.index - b.index;
  });
}

function rowTable(headers, lines, table) {
  const grid = node("table", null, table === "inventory" ? "inventory-grid" : "incident-table");
  const head = grid.createTHead().insertRow();
  for (const [key, title] of headers) {
    const th = node("th");
    th.scope = "col";
    th.dataset.field = key || "details";
    if (key) {
      const selected = sorting[table].key === key;
      const direction = sorting[table].direction;
      if (selected) th.setAttribute("aria-sort", direction === 1 ? "ascending" : "descending");
      const control = button(title, () => {
        sorting[table] = {key, direction:selected ? -direction : 1};
        if (table === "inventory") renderInventory(); else renderMatches();
        const id = table === "inventory" ? "inventory-table" : "match-list";
        [...$(id).querySelectorAll("[data-sort-key]")].find(item => item.dataset.sortKey === key)?.focus();
      }, "sort-button");
      control.dataset.sortKey = key;
      const arrow = node("span", selected ? (direction === 1 ? "▲" : "▼") : "↕", "sort-arrow");
      arrow.setAttribute("aria-hidden", "true");
      control.append(arrow);
      th.append(control);
    } else th.textContent = title;
    head.append(th);
  }
  for (const cells of lines) {
    const tr = (grid.tBodies[0] || grid.createTBody()).insertRow();
    cells.forEach((value, index) => {
      const cell = tr.insertCell();
      cell.dataset.field = headers[index][0] || "details";
      cell.append(value);
    });
  }
  return grid;
}

function appendFields(container, data) {
  const dl = node("dl");
  for (const [key, value] of Object.entries(data)) {
    if (value) dl.append(node("dt", key), node("dd", value));
  }
  if (dl.children.length) container.append(dl);
}

function textFields(data) {
  return Object.fromEntries(Object.entries(noticeFields).map(([key, title]) => [title, data[key]]));
}

function outcome(value) {
  return value === true ? "Matches" : value === false ? "Differs" : "Unverified";
}

function miniChip(value) {
  return node("span", outcome(value), "compare " + (value === true ? "yes" : value === false ? "no" : "unknown"));
}

function dateValue(value) {
  return [value.year ?? "Year unknown", value.month && String(value.month).padStart(2,"0"),
    value.day && String(value.day).padStart(2,"0")].filter(Boolean).join("-");
}

function noticeValue(condition) {
  const dates = condition.date_values.map(dateValue);
  const values = dates.length ? dates : condition.values;
  if (!values.length) return "Missing or unspecified";
  return (operators[condition.operator] || "") + values.join(condition.operator === "between_inclusive" ? " and " : " / ");
}

function relevant(result) {
  return result.evidence.filter(item => weights[item.status])
    .sort((a, b) => weights[b.status] - weights[a.status]);
}

function keyDetails(evidence) {
  const {identity,scope} = evidence.decision;
  const words = [];
  if (identity.outcome === true) {
    const kinds = [...new Set(identity.matching_ids.map(item => label(item.type)))];
    words.push(kinds.length ? kinds.join(" / ") + " verified" : "Product verified");
  } else if (identity.detail.includes("no product identifier")) {
    words.push(identity.notice_ids.length ? "Inventory identifier missing" : "Source identifier missing");
  } else {
    words.push("Product identity unverified");
  }
  if (scope.outcome === true) {
    const matched = scope.alternatives.find(option => option.outcome === true);
    words.push(matched ? matched.conditions.map(c => label(c.field)).join(" + ") + " matched" : "All batches");
  } else if (scope.outcome === false) {
    words.push("Listed scope differs");
  } else {
    const missing = [...new Set(scope.alternatives.filter(o => o.outcome === null)
      .flatMap(o => o.conditions.filter(c => c.outcome === null && !c.inventory).map(c => label(c.field))))];
    words.push(missing.length ? missing.join(" + ") + " missing" : "Batch scope unverified");
  }
  if (evidence.decision.review_reason) words.push("Notice review required");
  return words.join(" · ");
}

function comparison(evidence) {
  const box = node("section", null, "decision-card");
  const {identity, scope, review_reason} = evidence.decision;
  box.append(node("h3", evidence.product_name || "Notice product"), chip(evidence.status));
  if (evidence.simulated) box.append(node("span", "Simulated", "badge sim"));
  const line = node("div", null, "decision-heading");
  line.append(node("strong", "Product identity"), miniChip(identity.outcome), node("span", identity.detail));
  box.append(line);
  const currentIds = Object.entries(identity.inventory_ids).map(([k,v]) => label(k) + " " + v);
  const sourceIds = identity.notice_ids.map(i => label(i.type) + " " + i.value_raw +
    (i.role === "incorrect_label" ? " (wrong label)" : i.role === "unknown" ? " (role unknown)" : ""));
  appendFields(box, {
    "Inventory identity":currentIds.join(" · ") || identity.inventory_name || "Missing",
    "Notice identity":sourceIds.join(" · ") || identity.notice_name || "Missing"
  });
  const scopeLine = node("div", null, "decision-heading");
  scopeLine.append(node("strong", "Batch scope"), miniChip(scope.outcome));
  box.append(scopeLine);
  if (!scope.alternatives.length || scope.basis !== "specific_conditions") box.append(node("p", scope.detail.replaceAll("_", " ")));
  scope.alternatives.forEach((option, index) => {
    const group = node("div", null, "scope-option");
    const heading = node("div", null, "decision-heading");
    heading.append(node("strong", scope.alternatives.length > 1 ? "Batch alternative " + (index + 1) : "Required conditions"), miniChip(option.outcome));
    group.append(heading);
    for (const condition of option.conditions) {
      const record = node("div", null, "compare-row");
      record.append(node("strong", label(condition.field)), miniChip(condition.outcome),
        node("span", "Inventory: " + (condition.inventory || "Missing")),
        node("span", "Notice: " + noticeValue(condition)));
      group.append(record);
    }
    box.append(group);
  });
  if (review_reason) box.append(node("p", "Needs review: " + (evidence.english.review_reason || review_reason), "review-note"));
  if (evidence.notice_refresh_error) box.append(node("p", "Source refresh pending. Showing the last valid extraction."));
  const more = node("details", null, "notice-more");
  more.append(node("summary", "Notice text and instructions"));
  appendFields(more, textFields({...evidence.original, ...evidence.english}));
  if (Object.keys(evidence.english).length && !evidence.simulated) {
    const original = node("details");
    original.append(node("summary", "Original text"));
    appendFields(original, textFields(evidence.original));
    more.append(original);
  }
  box.append(more);
  return box;
}

function sourceLink(href, title = "View original recall") {
  if (!/^https?:\/\//i.test(href || "")) return node("span", "Source link unavailable", "detail-stock");
  const link = node("a", title, "source-link");
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.append(icon("arrow"));
  return link;
}

function nextStep(result) {
  return result.status === "MATCH" ? "Locate affected stock and follow the publisher’s recall instructions."
    : "Check the unverified details against the product label and source notice.";
}

function decisionSummary(evidence) {
  const box = node("div", null, "decision-summary");
  const parts = keyDetails(evidence).split(" · ");
  const outcomes = [evidence.decision.identity.outcome, evidence.decision.scope.outcome, null];
  parts.forEach((text, index) => {
    const line = node("span", null, outcomes[index] === true ? "verified-fact" : "unknown-fact");
    line.append(icon(outcomes[index] === true ? "check" : "inspect"), node("span", text));
    box.append(line);
  });
  return box;
}

function overviewPanel(record) {
  const area = node("section", null, "detail-overview");
  const callout = node("div", null, "next-step " + record.result.status);
  callout.append(icon(record.result.status === "MATCH" ? "alert" : "inspect"));
  const copy = node("div");
  copy.append(node("strong", record.result.status === "MATCH" ? "Affected stock needs action" : "A few details need verification"), node("p", nextStep(record.result)));
  callout.append(copy);
  area.append(callout, node("h3", "How this was decided", "section-heading"));
  const evidence = relevant(record.result);
  if (!evidence.length) area.append(node("p", record.result.reason));
  for (const item of evidence) {
    const card = node("section", null, "recall-overview");
    card.append(node("h4", item.product_name || "Recall notice"), decisionSummary(item));
    const text = {...item.original, ...item.english};
    appendFields(card, {"Recall reason":text.reason, "Source instruction":text.action});
    if (item.decision.review_reason) card.append(node("p", text.review_reason || item.decision.review_reason, "review-note"));
    if (item.notice_refresh_error) card.append(node("p", "Source refresh pending. This is the last valid extraction.", "review-note"));
    if (item.simulated) card.append(badge("Simulated recall", "sim", "incidents"));
    else card.append(sourceLink(item.notice_url));
    area.append(card);
  }
  return area;
}

function emailText(draft) {
  const stock = Object.entries(draft.stock).map(([key,value]) => key + ": " + value).join("\n");
  return ["Subject: " + draft.subject, "Hi stock team,", draft.body, stock,
    draft.sources.filter(url => /^https?:\/\//i.test(url)).join("\n"), "Thanks,\nLotWise recall desk"].filter(Boolean).join("\n\n");
}

function emailPanel(draft) {
  const panel = node("section", null, "email-draft");
  panel.id = "shown-email";
  if (!draft) {
    panel.append(empty("No email draft for this row", "Drafts are prepared for confirmed matches and inspection cases."));
    return panel;
  }
  const origin = {pending:"Preparing AI draft…", gemini:"Gemini draft", template:"Template draft",
    fallback:"Template draft", expired:"Template draft"};
  const heading = node("div", null, "email-heading");
  heading.append(node("h3", "Email to the stock lead"), node("small", origin[draft.state] || "Draft email", "draft-state"));
  const tools = node("div", null, "email-tools");
  const status = node("span", "Review before sharing. Nothing is sent automatically.", "copy-status");
  status.setAttribute("role", "status");
  const copy = button("Copy email", async () => {
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(emailText(draft));
      else {
        const field = node("textarea");
        field.value = emailText(draft);
        field.className = "clipboard-field";
        panel.append(field);
        field.select();
        const copied = document.execCommand("copy");
        field.remove();
        copy.focus();
        if (!copied) throw new Error("Clipboard unavailable");
      }
      status.textContent = "Email copied, including stock details and source links.";
    } catch { status.textContent = "Clipboard unavailable. Select and copy the email text below."; }
  }, "secondary");
  copy.id = "copy-email";
  copy.prepend(icon("copy"));
  tools.append(copy, status);
  panel.append(heading, tools);
  if (draft.state !== "gemini") panel.append(node("p", draft.state === "pending"
    ? "A complete template is ready while Gemini prepares the wording."
    : "Using the evidence-based template. Gemini is unavailable or not configured.", "draft-explanation"));
  panel.append(node("small", "SUBJECT", "eyebrow"), node("strong", draft.subject, "email-subject"));
  panel.append(node("p", "Hi stock team,"), node("p", draft.body, "email-body"));
  appendFields(panel, draft.stock);
  if (draft.sources.length) {
    const list = node("ul", null, "source-links");
    for (const [index, href] of draft.sources.entries()) {
      if (!/^https?:\/\//i.test(href)) continue;
      const item = node("li");
      item.append(sourceLink(href, draft.sources.length === 1 ? "Open source recall notice" : "Open source recall notice " + (index + 1)));
      list.append(item);
    }
    panel.append(list);
  }
  panel.append(node("p", "Thanks,\nLotWise recall desk", "email-body"));
  return panel;
}

function detailTabs(panels) {
  const wrapper = node("div", null, "detail-sections");
  const tabs = node("div", null, "detail-tabs");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "Incident details");
  function select(index, focus = false) {
    [...tabs.children].forEach((tab, i) => {
      tab.setAttribute("aria-selected", String(i === index));
      tab.tabIndex = i === index ? 0 : -1;
      panels[i].content.hidden = i !== index;
    });
    if (focus) tabs.children[index].focus();
  }
  panels.forEach(({title, content}, index) => {
    const tab = button(title, () => select(index), "detail-tab");
    tab.id = "detail-tab-" + index;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-controls", "detail-panel-" + index);
    tab.addEventListener("keydown", event => {
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % panels.length;
      if (event.key === "ArrowLeft") next = (index + panels.length - 1) % panels.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = panels.length - 1;
      if (next !== undefined) { event.preventDefault(); select(next, true); }
    });
    content.id = "detail-panel-" + index;
    content.setAttribute("role", "tabpanel");
    content.setAttribute("aria-labelledby", tab.id);
    content.tabIndex = 0;
    tabs.append(tab);
  });
  wrapper.append(tabs, ...panels.map(p => p.content));
  select(0);
  return wrapper;
}

function recordFor(index) {
  return {row:index, stock:inventory.rows[index], result:results[index]};
}

function openDetails(record, mode, event = null) {
  if (!record?.result) return;
  openRecord = record;
  document.querySelector(".drawer-kicker > span").textContent = "INCIDENT REVIEW";
  const dialog = $("details"), main = $("details-content"), actions = $("detail-actions");
  main.replaceChildren(node("h2", product(record.stock)), chip(record.result.status));
  if (mode === "resolved") main.append(node("p", "Resolved " + timeLabel(record.at), "detail-stock"));
  if (event?.ackAt) main.append(node("p", "Acknowledged " + timeLabel(event.ackAt), "detail-stock"));
  const stock = node("div", null, "stock-facts");
  const dateKey = firstDate(record.stock);
  const facts = {"Location":record.stock.location, "Quantity":record.stock.quantity,
    "Lot":record.stock.lot, [dateKey ? label(dateKey) : "Date"]:dateKey && record.stock[dateKey]};
  for (const [key, value] of Object.entries(facts)) {
    const item = node("div");
    item.append(node("span", key), node("strong", value || "Not provided"));
    stock.append(item);
  }
  main.append(node("p", [record.stock.inventory_id, record.stock.pack_size].filter(Boolean).join(" · "), "detail-stock"), stock);
  const evidencePanel = node("section");
  evidencePanel.append(node("h3", "Inventory compared with the recall", "section-heading"), node("p", "Each batch alternative is evaluated separately. All conditions within an alternative must match.", "detail-stock"));
  const evidence = relevant(record.result);
  if (evidence.length) for (const item of evidence) evidencePanel.append(comparison(item));
  else evidencePanel.append(node("p", record.result.reason));
  const email = node("section");
  email.append(emailPanel(record.result.draft));
  main.append(detailTabs([
    {title:"Overview", content:overviewPanel(record)},
    {title:"Matching evidence", content:evidencePanel},
    {title:"Email draft", content:email}
  ]));
  actions.replaceChildren();
  const pendingEvent = event || events.find(i => i.row === record.row && !i.ackAt);
  main.append(node("p", mode === "resolved"
    ? "This incident was marked as handled. Reopen it if further action is needed."
    : pendingEvent && !pendingEvent.ackAt
      ? "Acknowledge to record that you reviewed this update. Resolve only after the stock response is complete."
      : "Mark resolved after the stock response is complete. You can reopen the incident later.", "workflow-help"));
  if (pendingEvent && !pendingEvent.ackAt) {
    if (!pendingEvent.viewedAt) pendingEvent.viewedAt = new Date().toISOString();
    save();
    actions.append(button("Acknowledge incident", () => {
      pendingEvent.ackAt = new Date().toISOString();
      save();
      closeDetails();
      renderWorkflow();
      focusViewHeading();
      toast("Incident moved to Past incidents.");
    }, "secondary"));
  }
  const current = results[record.row];
  if (mode === "resolved") {
    actions.append(button("Reopen incident", () => {
      delete resolved[record.row];
      save();
      closeDetails();
      renderAll();
      focusViewHeading();
      toast("Resolution removed.");
    }));
  } else if (!resolved[record.row] && current && weights[current.status]) {
    actions.append(button("Mark as resolved", () => {
      const ongoing = recordFor(record.row);
      resolved[record.row] = {...ongoing, at:new Date().toISOString()};
      save();
      closeDetails();
      renderAll();
      focusViewHeading();
      const token = generation;
      const snapshot = resolved[record.row];
      toast("Incident moved to Resolved.", false, {label:"Undo", run:() => {
        if (token !== generation || resolved[record.row] !== snapshot) return;
        delete resolved[record.row];
        save();
        renderAll();
      }});
    }, "primary"));
  }
  if (!dialog.open) dialog.showModal();
  dialog.scrollTop = 0;
}

function closeDetails() {
  openRecord = null;
  $("details").close();
}

function focusViewHeading() {
  const heading = $(view).querySelector("h1");
  heading.tabIndex = -1;
  heading.focus({preventScroll:true});
}

function renderMatches() {
  const target = $("match-list");
  const filter = $("filter").value;
  for (const control of document.querySelectorAll("[data-filter]")) control.setAttribute("aria-pressed", String(control.dataset.filter === filter));
  for (const id of ["stat-matches", "stat-inspection", "stat-no-match"]) $(id).disabled = !results.length;
  $("filter-summary").hidden = true;
  $("review-next").hidden = true;
  const sort = sorting.matches;
  $("incident-sort").value = sort.key;
  $("incident-direction").textContent = sort.direction === 1 ? "Ascending ↑" : "Descending ↓";
  $("incident-sort").disabled = !results.length;
  $("incident-direction").disabled = !results.length;
  if (!inventory) {
    for (const id of ["matched-count", "inspect-count", "clear-count"]) $(id).textContent = "—";
    $("match-subtitle").textContent = "Upload a CSV to start checking stock.";
    target.replaceChildren(empty("Your incidents will appear here", "Upload a CSV or load the sample inventory above."));
    return;
  }
  $("match-subtitle").textContent = "See what matches, what is missing, and what needs review.";
  if (!results.length) {
    for (const id of ["matched-count", "inspect-count", "clear-count"]) $(id).textContent = "—";
    target.replaceChildren(empty("Checking inventory", "LotWise is comparing your rows with the recall notices."));
    return;
  }
  const query = $("search").value.trim().toLowerCase(), location = $("location-filter").value;
  const entries = results.map((result, index) => ({result, index, row:inventory.rows[index]}));
  const open = entries.filter(({result,index}) => weights[result.status] && !resolved[index]);
  $("matched-count").textContent = open.filter(entry => entry.result.status === "MATCH").length;
  $("inspect-count").textContent = open.filter(entry => entry.result.status === "NEEDS_INSPECTION").length;
  $("clear-count").textContent = entries.filter(entry => entry.result.status === "NO_MATCH").length;
  $("open-total").hidden = false;
  $("open-total").textContent = open.length;
  $("filter-all-count").textContent = open.length;
  $("filter-match-count").textContent = $("matched-count").textContent;
  $("filter-inspect-count").textContent = $("inspect-count").textContent;
  const chosen = open.filter(({result,row}) =>
    (filter === "attention" || filter === result.status) && (!location || row.location === location) && matchesSearch(row, query, relevant(result).map(keyDetails)));
  $("filter-summary").hidden = !(query || location || filter !== "attention");
  $("visible-count").textContent = chosen.length + " of " + open.length + " open incidents";
  ordered(chosen, "matches", ({result,row}, key) => {
    if (key === "status") return weights[result.status];
    if (key === "product_name") return product(row);
    if (key === "date") return sortDate(row[firstDate(row)]);
    return row[key] || "";
  });
  if (!chosen.length) {
    target.replaceChildren(open.length
      ? empty("No incidents match these filters", "Clear the filters or try another product, lot, or location.")
      : empty("No open incidents", "Your current inventory has no outstanding matches or inspections in the tracked notices."));
    return;
  }
  $("review-next").hidden = false;
  $("review-next").onclick = () => openDetails(recordFor(chosen[0].index), "current");
  const lines = chosen.map(({result, index, row}) => {
    const name = node("div");
    name.append(node("strong", product(row)), node("small", [row.inventory_id,row.pack_size].filter(Boolean).join(" · ")));
    const status = node("div", null, "status-cell");
    status.append(chip(result.status));
    const best = relevant(result)[0];
    status.append(best ? decisionSummary(best) : node("small", result.reason));
    if (events.some(event => event.row === index && !event.ackAt)) status.append(badge("New incident", "new-incident", "bell"));
    const evidence = relevant(result);
    if (evidence.some(item => item.simulated)) status.append(node("span", "Simulated", "badge sim"));
    const date = node("div");
    const field = firstDate(row);
    date.append(node("span", field ? row[field] : "—"));
    if (field) date.append(node("small", label(field)));
    const compactDetails = [row.lot && "Lot " + row.lot, field && row[field], row.location].filter(Boolean);
    const open = () => openDetails(recordFor(index), "current");
    if (compactDetails.length) name.append(node("small", compactDetails.join(" · "), "mobile-row"));
    const mobileDetails = button("View details", open, "detail-link mobile-row");
    mobileDetails.append(icon("arrow"));
    name.append(mobileDetails);
    const details = button("View details", open, "detail-link");
    details.append(icon("chevron"));
    return [name, row.lot || "—", date, row.location || "—", row.quantity || "—", status, details];
  });
  const scroller = node("div", null, "table-scroll");
  scroller.append(rowTable([
    ["product_name","Product"],["lot","Lot"],["date","Date"],["location","Location"],["quantity","Qty"],
    ["status","Decision"],[null,"Details"]
  ],lines,"matches"));
  target.replaceChildren(scroller);
}

function matchesSearch(row, query, extra = []) {
  return !query || [...Object.values(row), ...extra].some(value => String(value ?? "").toLowerCase().includes(query));
}

function renderInventory() {
  const target = $("inventory-table");
  if (!inventory) {
    $("inventory-subtitle").textContent = "Upload a CSV to see your stock here.";
    target.replaceChildren(empty("No inventory uploaded", "Use Upload CSV to add stock rows to this tab."));
    return;
  }
  const query = $("inventory-search").value.trim().toLowerCase();
  const filter = $("inventory-filter").value, location = $("inventory-location").value;
  const entries = inventory.rows.map((row, index) => ({row,index})).filter(({row,index}) =>
    (!filter || (resolved[index] ? "resolved" : results[index]?.status) === filter) && (!location || row.location === location) &&
    matchesSearch(row,query,[resolved[index] ? "Resolved" : labels[results[index]?.status] || ""]));
  $("inventory-subtitle").textContent = inventory.name + " · " + entries.length + " of " + inventory.rows.length + " stock rows";
  ordered(entries,"inventory",({row,index},key)=>{
    if (key === "status") return resolved[index] ? 3 : weights[results[index]?.status] ?? "";
    const field = key.slice("csv:".length);
    return ["best_before","use_by","date_mark","production_date","packing_date","delivery_date"].includes(field)
      ? sortDate(row[field]) : row[field] || "";
  });
  if (!entries.length) {
    const message = empty("No inventory rows found", "Try another product, lot, location, or status.");
    message.append(button("Clear filters", () => {
      $("inventory-search").value = $("inventory-filter").value = $("inventory-location").value = "";
      renderInventory();
      $("inventory-search").focus();
    }));
    target.replaceChildren(message);
    return;
  }
  const lines = entries.map(({row,index}) => {
    const status = node("div", null, "inventory-status");
    status.append(resolved[index] ? badge("Resolved", "done", "check") : results[index] ? chip(results[index].status) : "Checking…");
    if (results[index] && (weights[results[index].status] || resolved[index])) status.append(button("View details", () => {
      openDetails(resolved[index] || recordFor(index), resolved[index] ? "resolved" : "current");
    }, "detail-link"));
    return [...inventory.columns.map(key => row[key] || "—"), status];
  });
  const headers = inventory.columns.map(key => ["csv:" + key,label(key)]);
  const originalKey = sorting.inventory.key;
  if (originalKey && !headers.some(([key]) => key === originalKey) && originalKey !== "status") sorting.inventory.key = null;
  target.replaceChildren(rowTable([...headers,["status","Recall status"]], lines,"inventory"));
}

function historyRow(record, kind) {
  const row = node("div", null, "notification");
  const text = node("div");
  text.append(node("strong", product(record.stock)));
  const note = [record.stock.inventory_id,record.stock.location,record.simulated && "Simulated",
    kind === "resolved" && "Resolved"].filter(Boolean);
  text.append(node("p", note.join(" · ")));
  const details = button("View details", () => openDetails(record,kind,kind === "resolved" ? null : record), "detail-link");
  details.append(icon("arrow"));
  text.append(details);
  const status = kind === "resolved" ? badge("Resolved", "done", "check")
    : kind === "new" ? badge("New incident", "new-incident", "bell")
    : badge("Acknowledged", "acknowledged", "history");
  const marks = node("div", null, "incident-marks");
  marks.append(status, chip(record.result.status));
  row.append(marks, text, node("time", timeLabel(kind === "past" ? record.ackAt : record.at)));
  return row;
}

function renderWorkflow() {
  const fresh = events.filter(event => !event.ackAt);
  const past = events.filter(event => event.ackAt);
  const done = Object.values(resolved).sort((a,b) => b.at.localeCompare(a.at));
  for (const [id,count] of [["nav-open",results.filter((result,index) => weights[result.status] && !resolved[index]).length],
    ["nav-inventory",inventory?.rows.length || 0],["nav-resolved",done.length]]) {
    $(id).hidden = !count;
    $(id).textContent = count;
  }
  for (const id of ["unread","top-unread"]) {
    $(id).hidden = !fresh.length;
    $(id).textContent = fresh.length;
  }
  for (const [id,rows,kind,title,description] of [
    ["notification-list",fresh,"new","No new incidents","New or upgraded matches appear here after the first check."],
    ["past-list",past,"past","No acknowledged incidents","Open a new incident and acknowledge it to move it here."],
    ["resolved-list",done,"resolved","No resolved incidents","Mark an incident as resolved after handling it."]
  ]) {
    $(id).replaceChildren(...(rows.length ? rows.map(record => historyRow(record,kind)) : [empty(title,description)]));
  }
}

function renderScreening() {
  const bar = $("screening-bar"), legend = $("screening-legend");
  bar.replaceChildren();
  legend.replaceChildren();
  legend.hidden = !results.length;
  bar.hidden = !results.length;
  $("screening-title").textContent = inventory ? "Inventory screening" : "Recall coverage";
  if (!results.length) {
    $("screened-count").textContent = inventory ? "—" : state.notice_count ?? "—";
    $("screened-caption").textContent = inventory ? "Checking stock rows" : "Stored notices available for matching";
    bar.setAttribute("aria-label", "No inventory screened yet");
    return;
  }
  const counts = {MATCH:0, NEEDS_INSPECTION:0, NO_MATCH:0, done:0};
  results.forEach((result, index) => {
    counts[resolved[index] ? "done" : result.status]++;
  });
  const sections = [
    ["MATCH", "Affected"], ["NEEDS_INSPECTION", "Inspect"],
    ["NO_MATCH", "No match"], ["done", "Resolved"]
  ];
  const spoken = [];
  for (const [key, title] of sections) {
    if (!counts[key] && key === "done") continue;
    if (counts[key]) {
      const part = node("span", null, "screening-segment " + key);
      part.style.flexGrow = counts[key];
      bar.append(part);
    }
    const detail = node("span");
    detail.append(node("i", null, key), document.createTextNode(title + " " + counts[key]));
    legend.append(detail);
    spoken.push(title + " " + counts[key]);
  }
  $("screened-count").textContent = results.length;
  $("screened-caption").textContent = results.length === 1 ? "Inventory row checked" : "Inventory rows checked";
  bar.setAttribute("aria-label", "Screening results. " + spoken.join(", "));
}

function renderOverview() {
  document.body.classList.toggle("has-inventory", !!inventory);
  $("onboarding").hidden = !!inventory;
  $("loaded-inventory").hidden = !inventory;
  $("matches-title").textContent = inventory ? "Your recall desk." : "Know what needs attention.";
  $("overview-description").textContent = inventory
    ? "Review affected stock. Verify uncertain batches. Keep your team informed."
    : "Connect your stock to recall notices. See the affected batches, understand the evidence, and act with clarity.";
  if (inventory) {
    const locations = [...new Set(inventory.rows.map(row => row.location).filter(Boolean))].sort(collator.compare);
    $("loaded-name").textContent = inventory.name;
    $("loaded-locations").textContent = locations.length + (locations.length === 1 ? " location" : " locations");
    for (const id of ["location-filter", "inventory-location"]) {
      const control = $(id), selected = control.value;
      const values = ["", ...locations];
      if (JSON.stringify([...control.options].map(o => o.value)) !== JSON.stringify(values)) {
        control.replaceChildren(...values.map(value => {
          const option = node("option", value || "All locations");
          option.value = value;
          return option;
        }));
        control.value = locations.includes(selected) ? selected : "";
      }
    }
  }
}

function renderAll() {
  renderOverview();
  renderScreening();
  renderMatches();
  renderInventory();
  renderWorkflow();
}

function renderStatus() {
  const monitor = state.monitor;
  if (monitor) {
    $("source-state").textContent = {snapshot:"Notice snapshot",running:"Live monitoring",stopped:"Monitor unavailable"}[monitor.state];
    $("source-dot").className = "source-dot " + monitor.state;
    const count = state.notice_count + " notices";
    $("source-description").textContent = count + (state.issues.length ? " · " + state.issues.length + " refresh issues" : "") + " · " + monitor.detail;
  }
  $("simulation").hidden = !state.simulation_active;
  if (state.checked_at) $("checked").textContent = "Last checked " + timeLabel(state.checked_at);
}

function updateIncidents() {
  const current = {}, additions = [];
  results.forEach((result, row) => {
    const changes = [];
    for (const item of relevant(result)) {
      const key = JSON.stringify([row,item.notice_url,item.notice_product_key]);
      const rank = weights[item.status];
      current[key] = Math.max(current[key] || 0,rank);
      if (previous && rank > (previous[key] || 0)) changes.push(item);
    }
    if (changes.length) {
      delete resolved[row];
      additions.push({...recordFor(row), at:state.checked_at, id:nextId++,
        simulated:changes.every(item => item.simulated), viewedAt:null, ackAt:null});
    }
  });
  previous = current;
  if (additions.length) {
    events = [...additions.reverse(), ...events];
    const first = product(additions[0].stock);
    toast(additions.length === 1 ? "New incident · " + first : additions.length + " new incidents", true);
  }
  save();
}

async function refreshDrafts() {
  if (draftRunning) return;
  const list = [...results,...events.map(x => x.result),...Object.values(resolved).map(x => x.result)];
  if (openRecord) list.push(openRecord.result);
  const pending = new Map();
  for (const {draft} of list) {
    if (draft?.state !== "pending") continue;
    if (!pending.has(draft.id)) pending.set(draft.id,new Set());
    pending.get(draft.id).add(draft);
  }
  if (!pending.size) return;
  const token = generation;
  const shownDraft = openRecord ? JSON.stringify(openRecord.result.draft) : null;
  draftRunning = true;
  try {
    const ids = [...pending.keys()];
    for (let index = 0; index < ids.length; index += 1000) {
      const data = await request("/api/drafts",{ids:ids.slice(index,index+1000)});
      if (token !== generation) return;
      for (const [key,value] of Object.entries(data.drafts)) {
        for (const draft of pending.get(key)) Object.assign(draft,value);
      }
    }
    if (openRecord && $("shown-email") && JSON.stringify(openRecord.result.draft) !== shownDraft) {
      const focus = document.activeElement?.id === "copy-email";
      $("shown-email").replaceWith(emailPanel(openRecord.result.draft));
      if (focus) $("copy-email")?.focus();
    }
    save();
  } catch { /* The next draft poll can retry while the template remains visible. */ }
  finally { draftRunning = false; }
}

async function refresh() {
  const token = generation;
  if (running === token) return;
  running = token;
  $("refresh-now").disabled = true;
  $("refresh-now").classList.add("refreshing");
  try {
    const result = await request("/api/check", {rows:inventory ? inventory.rows : [], revision});
    if (token !== generation) return;
    if (checkError && $("feedback").textContent === checkError) feedback("");
    checkError = "";
    state = {...state,...result};
    revision = result.revision;
    renderStatus();
    if (result.results) {
      results = result.results;
      if (inventory) updateIncidents();
      renderAll();
      refreshDrafts();
    }
  } catch (error) {
    if (token === generation) {
      checkError = "Recall check unavailable. " + error.message;
      feedback(checkError,true);
      $("source-state").textContent = "Connection interrupted";
      $("source-dot").className = "source-dot stopped";
      $("source-description").textContent = "Showing the last available results. Checks will retry automatically.";
    }
  } finally {
    if (running === token) {
      running = null;
      $("refresh-now").disabled = false;
      $("refresh-now").classList.remove("refreshing");
    }
  }
}

async function upload(file) {
  if (uploadBusy || pendingImport) return;
  uploadBusy = true;
  $("upload").disabled = $("choose-file").disabled = $("load-sample").disabled = true;
  $("onboarding").setAttribute("aria-busy", "true");
  feedback("Reading " + file.name + "…");
  try {
    const form = new FormData();
    form.append("inventory",file);
    const result = await request("/api/upload",form,true);
    const next = {...result,name:file.name};
    if (inventory) {
      pendingImport = next;
      $("import-description").textContent = "Ready to import " + next.rows.length + " stock rows from " + next.name + ". Current inventory: " + inventory.name + ".";
      $("import-review").showModal();
      feedback("");
    } else await commitInventory(next);
  } catch (error) { feedback(error.message,true); }
  finally {
    uploadBusy = false;
    $("upload").disabled = $("choose-file").disabled = $("load-sample").disabled = false;
    $("onboarding").setAttribute("aria-busy", "false");
  }
}

async function commitInventory(next) {
  inventory = next;
  generation++;
  previous = null;
  events = [];
  resolved = {};
  revision = null;
  results = [];
  $("search").value = $("inventory-search").value = $("inventory-filter").value = "";
  $("location-filter").value = $("inventory-location").value = "";
  $("filter").value = "attention";
  for (const id of ["open-total", "filter-all-count", "filter-match-count", "filter-inspect-count"]) $(id).textContent = "0";
  save();
  selectView("matches");
  renderAll();
  feedback("");
  await refresh();
  if (!checkError) toast(inventory.rows.length + " inventory rows checked against " + state.notice_count + " recall notices.");
}

function selectView(next) {
  view = next;
  for (const key of Object.keys(views)) $(key).hidden = key !== next;
  for (const control of document.querySelectorAll("[data-view]")) {
    if (control.dataset.view === view) control.setAttribute("aria-current","page");
    else control.removeAttribute("aria-current");
  }
  $("breadcrumb").textContent = "Workspace / " + views[view];
  document.title = "LotWise | " + views[view];
}

for (const control of document.querySelectorAll("[data-view]")) control.addEventListener("click", () => selectView(control.dataset.view));
$("top-notifications").addEventListener("click", () => selectView("notifications"));
$("refresh-now").addEventListener("click", async () => {
  await refresh();
  if (!checkError) toast("Recall matches checked against the current notice database.");
});
function filterIncidents(value) {
  $("filter").value = value;
  selectView("matches");
  renderMatches();
}
for (const control of document.querySelectorAll("[data-filter]")) control.addEventListener("click", () => filterIncidents(control.dataset.filter));
$("stat-matches").addEventListener("click", () => { filterIncidents("MATCH"); $("search").focus({preventScroll:true}); });
$("stat-inspection").addEventListener("click", () => { filterIncidents("NEEDS_INSPECTION"); $("search").focus({preventScroll:true}); });
$("stat-no-match").addEventListener("click", () => {
  $("inventory-search").value = $("inventory-location").value = "";
  $("inventory-filter").value = "NO_MATCH";
  selectView("inventory");
  renderInventory();
  $("inventory-search").focus({preventScroll:true});
});
$("reset-filters").addEventListener("click", () => {
  $("search").value = $("location-filter").value = "";
  filterIncidents("attention");
  $("search").focus();
});
$("search").addEventListener("input", renderMatches);
$("filter").addEventListener("change", renderMatches);
$("location-filter").addEventListener("change", renderMatches);
$("incident-sort").addEventListener("change", event => {
  sorting.matches = {key:event.target.value, direction:1};
  renderMatches();
});
$("incident-direction").addEventListener("click", () => {
  sorting.matches.direction *= -1;
  renderMatches();
});
$("inventory-search").addEventListener("input", renderInventory);
$("inventory-filter").addEventListener("change", renderInventory);
$("inventory-location").addEventListener("change", renderInventory);
$("details").addEventListener("close", () => {openRecord = null;});
$("close-details").addEventListener("click", closeDetails);
$("source-details").addEventListener("click", () => {
  openRecord = null;
  document.querySelector(".drawer-kicker > span").textContent = "SOURCE COVERAGE";
  $("detail-actions").replaceChildren();
  const detail = $("details-content");
  detail.replaceChildren(node("h2","Recall sources"),node("p",state.monitor?.detail || "Source check pending."));
  detail.append(node("p", "Inventory checks run every five seconds while this page is open. Live retrieval of new notices depends on the background monitor.", "coverage-note"));
  for (const source of state.sources || []) {
    const card = node("div", null, "recall-overview");
    card.append(sourceLink(source.url, source.url), node("p", "Source last checked " + timeLabel(source.last_checked)));
    detail.append(card);
  }
  for (const issue of state.issues || []) detail.append(node("p",issue.url + " · " + issue.error));
  $("details").showModal();
  $("details").scrollTop = 0;
});
$("choose-file").addEventListener("click", () => $("upload").click());
$("dropzone").addEventListener("dragover", event => { event.preventDefault(); $("dropzone").classList.add("drag-over"); });
$("dropzone").addEventListener("dragleave", event => {
  if (!$("dropzone").contains(event.relatedTarget)) $("dropzone").classList.remove("drag-over");
});
$("dropzone").addEventListener("drop", event => {
  event.preventDefault();
  $("dropzone").classList.remove("drag-over");
  const files = [...event.dataTransfer.files];
  if (files.length !== 1) { feedback("Choose one inventory CSV at a time.", true); return; }
  upload(files[0]);
});
$("cancel-import").addEventListener("click", () => $("import-review").close());
$("import-review").addEventListener("close", () => { pendingImport = null; });
$("import-review").addEventListener("cancel", () => { pendingImport = null; });
$("confirm-import").addEventListener("click", async () => {
  if (!pendingImport) return;
  const next = pendingImport;
  pendingImport = null;
  $("import-review").close();
  await commitInventory(next);
});
$("upload").addEventListener("change", async event => {
  const [file] = event.target.files;
  if (!file) return;
  try { await upload(file); } catch (error) { feedback(error.message,true); }
  finally { event.target.value = ""; }
});
$("load-sample").addEventListener("click", async () => {
  $("load-sample").disabled = true;
  try {
    const response = await fetch("/examples/market_inventory.csv");
    if (!response.ok) throw new Error("Sample CSV unavailable.");
    await upload(new File([await response.blob()],"market_inventory.csv",{type:"text/csv"}));
  } catch (error) { feedback(error.message,true); }
  finally { $("load-sample").disabled = false; }
});

renderAll();
refresh();
setInterval(refresh,5000);
setInterval(refreshDrafts,1250);
