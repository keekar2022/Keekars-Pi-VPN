// Concept: Mukesh Kesharwani
// Contact: mukesh.kesharwani@adobe.com

function setStatus(msg, ok) {
  const el = document.getElementById("network-status");
  el.textContent = msg;
  el.className = ok ? "ok" : "error";
}

async function loadInterfaces() {
  const res = await fetch("/api/network/interfaces");
  const rows = await res.json();
  const tbody = document.querySelector("#iface-table tbody");
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.name}</td><td>${r.is_up ? "yes" : "no"}</td><td>${r.addresses.join(", ")}</td>`;
    tbody.appendChild(tr);
  }

  const select = document.getElementById("config-iface-select");
  const previous = select.value;
  select.innerHTML = "";
  for (const r of rows) {
    if (r.name === "lo") continue;
    const opt = document.createElement("option");
    opt.value = r.name;
    opt.textContent = r.name;
    select.appendChild(opt);
  }
  if ([...select.options].some((o) => o.value === previous)) select.value = previous;
}
loadInterfaces();

async function loadKnownWifi() {
  const res = await fetch("/api/network/wifi/known");
  const networks = await res.json();
  const select = document.getElementById("wifi-network-select");
  select.innerHTML = '<option value="__new__">-- New network --</option>';
  for (const n of networks) {
    const opt = document.createElement("option");
    opt.value = n.name;
    opt.dataset.ssid = n.ssid;
    opt.textContent = n.ssid === n.name ? n.ssid : `${n.ssid} (${n.name})`;
    select.appendChild(opt);
  }
  updateWifiFormMode();
}
loadKnownWifi();

function updateWifiFormMode() {
  const select = document.getElementById("wifi-network-select");
  const isNew = select.value === "__new__";
  document.getElementById("new-wifi-wrap").style.display = isNew ? "block" : "none";
  document.getElementById("psk-hint").textContent = isNew
    ? "(required for a new network)"
    : "(leave blank to just reconnect, fill in to update it)";
}
document.getElementById("wifi-network-select").addEventListener("change", updateWifiFormMode);

document.getElementById("scan-wifi-btn").addEventListener("click", async () => {
  const list = document.getElementById("scan-results");
  list.innerHTML = "<li>Scanning...</li>";
  try {
    const res = await fetch("/api/network/wifi/scan");
    const networks = await res.json();
    list.innerHTML = "";
    for (const n of networks) {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = `${n.ssid} (${n.security}, signal ${n.signal})`;
      btn.addEventListener("click", () => {
        document.getElementById("wifi-ssid-input").value = n.ssid;
      });
      li.appendChild(btn);
      list.appendChild(li);
    }
    if (!networks.length) list.innerHTML = "<li>No networks found.</li>";
  } catch {
    list.innerHTML = "<li>Scan failed.</li>";
  }
});

document.getElementById("config-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const payload = { interface: form.get("interface"), method: form.get("method") };
  if (form.get("address")) payload.address = form.get("address");
  if (form.get("gateway")) payload.gateway = form.get("gateway");
  const res = await fetch("/api/network/configure", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.ok) { setStatus("Applied", true); loadInterfaces(); }
  else { const body = await res.json(); setStatus(body.detail || "Failed", false); }
});

document.getElementById("wifi-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const select = document.getElementById("wifi-network-select");
  const isNew = select.value === "__new__";
  const selectedOption = select.options[select.selectedIndex];
  const ssid = isNew ? document.getElementById("wifi-ssid-input").value.trim() : selectedOption.dataset.ssid;
  if (!ssid) { setStatus("Enter or select an SSID", false); return; }

  const form = new FormData(e.target);
  const psk = form.get("psk");
  const payload = { ssid };
  if (psk) payload.psk = psk;
  if (!isNew) payload.connection_name = select.value;

  const res = await fetch("/api/network/wifi/connect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.ok) { setStatus("Connected", true); loadKnownWifi(); }
  else { const body = await res.json(); setStatus(body.detail || "Failed", false); }
  e.target.reset();
  updateWifiFormMode();
  document.getElementById("scan-results").innerHTML = "";
});
