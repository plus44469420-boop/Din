const STORAGE = {
  profile: "claimdesk.profile",
  shopped: "claimdesk.shopped",
  qualified: "claimdesk.qualified",
  submitted: "claimdesk.submitted",
  seen: "claimdesk.seen",
  notices: "claimdesk.notices"
};

const state = {
  catalog: null,
  view: "open",
  selectedId: null,
  attest: false,
  continued: false
};

const $ = (sel) => document.querySelector(sel);

function load(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function save(key, value) {
  localStorage.setItem(key, JSON.stringify(value));
}

function todayISO() {
  return new Date().toISOString().slice(0, 10);
}

function isOpen(item) {
  return item.deadline >= todayISO();
}

function profile() {
  return load(STORAGE.profile, {
    legalName: "",
    email: "",
    phone: "",
    address: "",
    city: "",
    state: "",
    zip: ""
  });
}

function submitted() {
  return load(STORAGE.submitted, []);
}

function shopped() {
  return load(STORAGE.shopped, {});
}

function notices() {
  return load(STORAGE.notices, []);
}

function formatDate(iso) {
  const [y, m, d] = iso.slice(0, 10).split("-");
  return `${m}/${d}/${y}`;
}

function formatWhen(iso) {
  return new Date(iso).toLocaleString();
}

async function init() {
  const res = await fetch("settlements.json");
  state.catalog = await res.json();
  bindNav();
  $("#check-new").addEventListener("click", checkForNew);
  render();
}

function bindNav() {
  document.querySelectorAll("nav button").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.view = btn.dataset.view;
      state.selectedId = null;
      state.attest = false;
      state.continued = false;
      render();
    });
  });
}

function checkForNew() {
  const seen = new Set(load(STORAGE.seen, []));
  const fresh = [];
  const items = notices();
  for (const settlement of state.catalog.settlements) {
    if (seen.has(settlement.id) || !isOpen(settlement)) continue;
    fresh.push({
      id: settlement.id,
      name: settlement.name,
      foundAt: new Date().toISOString(),
      proofRequired: settlement.proofRequired,
      read: false
    });
    seen.add(settlement.id);
  }
  save(STORAGE.seen, [...seen]);
  save(STORAGE.notices, [...fresh, ...items].slice(0, 40));
  state.view = "inbox";
  render();
}

function render() {
  document.querySelectorAll("nav button").forEach((btn) => {
    btn.setAttribute("aria-current", btn.dataset.view === state.view ? "page" : "false");
  });
  const root = $("#view");
  const done = submitted();
  $("#done-count").textContent = String(done.length);
  $("#open-count").textContent = String(state.catalog.settlements.filter(isOpen).length);
  $("#notice-count").textContent = String(notices().filter((n) => !n.read).length);

  if (state.view === "open") root.innerHTML = listView(false);
  else if (state.view === "proof") root.innerHTML = listView(true);
  else if (state.view === "done") root.innerHTML = doneView(done);
  else if (state.view === "inbox") root.innerHTML = inboxView();
  else if (state.view === "sources") root.innerHTML = sourcesView();
  else root.innerHTML = profileView();

  wire(root);
}

function listView(proofRequired) {
  const items = state.catalog.settlements
    .filter((item) => item.proofRequired === proofRequired)
    .sort((a, b) => a.deadline.localeCompare(b.deadline));
  const selected = items.find((item) => item.id === state.selectedId) || null;
  const heading = proofRequired
    ? "Needs proof"
    : "No proof required";
  const blurb = proofRequired
    ? "These only move forward if you have shopped, owned, or used the product and can show it. “I’ve shopped there” is a note for you. It does not file anything."
    : "No receipt does not mean anyone may file. Continue only after you confirm you were actually a customer or user in the class.";
  return `
    <div class="layout">
      <section>
        <div class="notice"><strong>${heading}</strong>${blurb}</div>
        <div class="list">
          ${items.map((item) => card(item)).join("")}
        </div>
      </section>
      <aside>${selected ? detail(selected) : `<div class="card"><h3>Pick a settlement</h3><p class="meta">The class definition stays on screen before any continue button.</p></div>`}</aside>
    </div>`;
}

function card(item) {
  const open = isOpen(item);
  const been = shopped()[item.id];
  const filed = submitted().some((row) => row.id === item.id);
  return `
    <article class="card">
      <div class="tags">
        <span class="tag ${item.proofRequired ? "yes" : "no"}">${item.proofRequired ? "Proof required" : "No proof"}</span>
        <span class="tag ${open ? "" : "closed"}">${open ? "Open" : "Deadline passed"}</span>
        ${filed ? `<span class="tag no">Submitted ${formatDate(submitted().find((row) => row.id === item.id).at)}</span>` : ""}
        ${been ? `<span class="tag">Shopped there</span>` : ""}
      </div>
      <h2>${item.name}</h2>
      <p class="meta">${item.category} · ${item.payout} · deadline ${formatDate(item.deadline)}</p>
      <p>${item.classDefinition}</p>
      <div class="row">
        <button class="btn" data-select="${item.id}">Review</button>
      </div>
    </article>`;
}

function detail(item) {
  const open = isOpen(item);
  const p = profile();
  const ready = p.legalName && p.email;
  const been = Boolean(shopped()[item.id]);
  if (item.proofRequired) {
    return `
      <div class="card">
        <h3>${item.name}</h3>
        <p class="meta">Proof required · deadline ${formatDate(item.deadline)}</p>
        <p>${item.classDefinition}</p>
        <div class="row">
          <button class="btn" data-shop="${item.id}">${been ? "Clear “I’ve shopped there”" : "I’ve shopped there"}</button>
          <a class="btn" href="${item.listingUrl}" target="_blank" rel="noreferrer">Open listing</a>
        </div>
        <p class="meta">Gather receipts or records, then file on the official administrator site linked from the listing. This desk does not submit the form for you.</p>
      </div>`;
  }

  const packet = state.continued && state.selectedId === item.id ? packetHtml(item, p) : "";
  return `
    <div class="card">
      <h3>${item.name}</h3>
      <p class="meta">${open ? "Open" : "Closed"} · no proof listed · deadline ${formatDate(item.deadline)}</p>
      <p><strong>Who can file:</strong> ${item.classDefinition}</p>
      <label class="attest">
        <input type="checkbox" id="attest" ${state.attest ? "checked" : ""} ${open ? "" : "disabled"} />
        <span>I’ve been there. I meet this class definition, and the statement I send will be true.</span>
      </label>
      ${ready ? "" : `<p class="meta">Add your settlement-only name and email on the Profile tab before continuing.</p>`}
      <div class="row">
        <button class="btn primary" id="continue" ${open && state.attest && ready ? "" : "disabled"}>I’ve been there — continue</button>
      </div>
      ${packet}
    </div>`;
}

function packetHtml(item, p) {
  const block = [
    `Legal name: ${p.legalName}`,
    `Settlement email: ${p.email}`,
    `Phone: ${p.phone || "—"}`,
    `Address: ${[p.address, p.city, p.state, p.zip].filter(Boolean).join(", ") || "—"}`,
    `Settlement: ${item.name}`,
    `Class: ${item.classDefinition}`
  ].join("\n");
  return `
    <div class="packet">
      <h3>Your details, ready to copy</h3>
      <p class="meta">Paste these into the official claim form. Submit that form yourself. A listing site is only a pointer.</p>
      <pre id="packet">${block}</pre>
      <div class="row">
        <button class="btn" id="copy">Copy details</button>
        <a class="btn" href="${item.listingUrl}" target="_blank" rel="noreferrer">Open listing</a>
        <button class="btn primary" id="mark-done">I submitted the official form</button>
      </div>
    </div>`;
}

function doneView(rows) {
  if (!rows.length) {
    return `<div class="card"><h2>Completed claims</h2><p class="empty">None yet. A claim lands here only after you confirm you qualify and mark the official form as submitted.</p></div>`;
  }
  const body = rows
    .slice()
    .sort((a, b) => b.at.localeCompare(a.at))
    .map((row) => `<tr><td>${row.name}</td><td>${row.proofRequired ? "Proof" : "No proof"}</td><td>${formatWhen(row.at)}</td></tr>`)
    .join("");
  return `
    <div class="card">
      <h2>${rows.length} completed</h2>
      <p class="meta">Each time is when you marked the official form submitted from this desk.</p>
      <table>
        <thead><tr><th>Settlement</th><th>Type</th><th>Marked submitted</th></tr></thead>
        <tbody>${body}</tbody>
      </table>
    </div>`;
}

function inboxView() {
  const items = notices();
  if (items.some((item) => !item.read)) {
    save(STORAGE.notices, items.map((item) => ({ ...item, read: true })));
  }
  if (!items.length) {
    return `<div class="card"><h2>New settlements</h2><p class="empty">Nothing queued. Use “Check for new” to compare the catalog with what you have already seen.</p></div>`;
  }
  const rows = items.map((item) => `
    <article class="card">
      <div class="tags"><span class="tag new">New</span><span class="tag ${item.proofRequired ? "yes" : "no"}">${item.proofRequired ? "Proof required" : "No proof"}</span></div>
      <h3>${item.name}</h3>
      <p class="meta">Found ${formatWhen(item.foundAt)}</p>
    </article>`).join("");
  return `<div class="list">${rows}</div>`;
}

function sourcesView() {
  const cards = state.catalog.sources.map((source) => `
    <article class="card">
      <h3><a href="${source.url}" target="_blank" rel="noreferrer">${source.name}</a></h3>
      <p>${source.role}</p>
    </article>`).join("");
  return `
    <div class="notice"><strong>Where this list comes from</strong>
    r/classactions is the main pointer. The settlements in this desk were cross-checked against Settlement Pulse on ${state.catalog.updated}, then linked back to the listing sites the subreddit recommends. Deadlines change. Read the official administrator page before you file. Never pay a company to submit a claim.</div>
    <div class="list">${cards}</div>`;
}

function profileView() {
  const p = profile();
  return `
    <div class="card">
      <h2>Settlement-only contact</h2>
      <p class="meta">Use an email you open only for settlement mail. This stays in this browser. It is not sent anywhere until you paste it into an official form.</p>
      <form class="profile" id="profile-form">
        <label>Legal name<input name="legalName" required value="${escapeAttr(p.legalName)}" /></label>
        <label>Settlement email<input name="email" type="email" required value="${escapeAttr(p.email)}" placeholder="settlements@your-domain" /></label>
        <label>Phone<input name="phone" value="${escapeAttr(p.phone)}" /></label>
        <label>Street<input name="address" value="${escapeAttr(p.address)}" /></label>
        <label>City<input name="city" value="${escapeAttr(p.city)}" /></label>
        <label>State<input name="state" value="${escapeAttr(p.state)}" /></label>
        <label>ZIP<input name="zip" value="${escapeAttr(p.zip)}" /></label>
        <button class="btn primary" type="submit">Save profile</button>
      </form>
    </div>`;
}

function escapeAttr(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;");
}

function wire(root) {
  root.querySelectorAll("[data-select]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.selectedId = btn.dataset.select;
      state.attest = false;
      state.continued = false;
      render();
    });
  });
  root.querySelectorAll("[data-shop]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const map = shopped();
      map[btn.dataset.shop] = !map[btn.dataset.shop];
      save(STORAGE.shopped, map);
      render();
    });
  });
  const attest = root.querySelector("#attest");
  if (attest) {
    attest.addEventListener("change", () => {
      state.attest = attest.checked;
      if (!state.attest) state.continued = false;
      render();
    });
  }
  const cont = root.querySelector("#continue");
  if (cont) {
    cont.addEventListener("click", () => {
      state.continued = true;
      render();
    });
  }
  const copy = root.querySelector("#copy");
  if (copy) {
    copy.addEventListener("click", async () => {
      await navigator.clipboard.writeText($("#packet").textContent);
      copy.textContent = "Copied";
    });
  }
  const mark = root.querySelector("#mark-done");
  if (mark) {
    mark.addEventListener("click", () => {
      const item = state.catalog.settlements.find((row) => row.id === state.selectedId);
      const rows = submitted().filter((row) => row.id !== item.id);
      rows.push({
        id: item.id,
        name: item.name,
        proofRequired: item.proofRequired,
        at: new Date().toISOString()
      });
      save(STORAGE.submitted, rows);
      state.view = "done";
      state.selectedId = null;
      state.attest = false;
      state.continued = false;
      render();
    });
  }
  const form = root.querySelector("#profile-form");
  if (form) {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const data = Object.fromEntries(new FormData(form).entries());
      save(STORAGE.profile, data);
      state.view = "open";
      render();
    });
  }
}

init();
