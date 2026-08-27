// Concept: Mukesh Kesharwani
// Contact: mukesh.kesharwani@adobe.com

function setStatus(elId, msg, ok) {
  const el = document.getElementById(elId);
  el.textContent = msg;
  el.className = ok ? "ok" : "error";
}

function formatHandshake(ts) {
  if (!ts) return "never";
  return new Date(ts * 1000).toLocaleString();
}

let selectedTunnel = "";

async function loadTunnels() {
  const res = await fetch("/api/wireguard/tunnels");
  const rows = await res.json();
  const clientRows = rows.filter((t) => t.mode === "client");
  const serverRows = rows.filter((t) => t.mode !== "client");

  renderClientTable(clientRows);
  renderServerTable(serverRows);
}

function renderClientTable(rows) {
  const tbody = document.querySelector("#client-table tbody");
  tbody.innerHTML = "";
  for (const t of rows) {
    const tr = document.createElement("tr");
    const badge = t.active ? '<span class="ok">connected</span>' : '<span class="error">disconnected</span>';
    const action = t.active ? "disconnect" : "activate";
    const label = t.active ? "Disconnect" : "Connect";
    tr.innerHTML = `<td>${t.name}</td><td>${badge}</td><td>${t.remote_endpoint || "-"}</td>
      <td>${formatHandshake(t.latest_handshake)}</td>
      <td><button data-name="${t.name}" data-action="${action}" class="toggle-client">${label}</button></td>`;
    tbody.appendChild(tr);
  }
  document.querySelectorAll(".toggle-client").forEach((btn) => {
    btn.addEventListener("click", () => setTunnelState(btn.dataset.name, btn.dataset.action, false));
  });
}

function renderServerTable(rows) {
  const tbody = document.querySelector("#tunnel-table tbody");
  tbody.innerHTML = "";
  const select = document.getElementById("peer-tunnel-select");
  const previousSelection = select.value;
  select.innerHTML = '<option value="">-- select a tunnel --</option>';

  for (const t of rows) {
    const tr = document.createElement("tr");
    const badge = t.active ? '<span class="ok">active</span>' : '<span class="error">inactive</span>';
    const toggleLabel = t.active ? "Deactivate" : "Activate";
    tr.innerHTML = `<td>${t.name}</td><td>${badge}</td><td>${t.address || "-"}</td><td>${t.listen_port || "-"}</td><td>${t.peer_count}</td>
      <td><button data-name="${t.name}" data-action="${t.active ? "deactivate" : "activate"}" class="toggle-tunnel">${toggleLabel}</button>
      <button data-name="${t.name}" class="del-tunnel">Delete</button></td>`;
    tbody.appendChild(tr);

    const opt = document.createElement("option");
    opt.value = t.name;
    opt.textContent = t.name;
    select.appendChild(opt);
  }

  if (rows.some((t) => t.name === previousSelection)) {
    select.value = previousSelection;
    selectedTunnel = previousSelection;
  }

  document.querySelectorAll(".toggle-tunnel").forEach((btn) => {
    btn.addEventListener("click", () => setTunnelState(btn.dataset.name, btn.dataset.action, false));
  });
  document.querySelectorAll(".del-tunnel").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const res = await fetch(`/api/wireguard/tunnels/${btn.dataset.name}`, { method: "DELETE" });
      if (res.ok) { setStatus("tunnel-status", "Deleted", true); loadTunnels(); }
      else { const body = await res.json(); setStatus("tunnel-status", body.detail || "Failed", false); }
    });
  });
}

async function setTunnelState(name, action, force) {
  const res = await fetch(`/api/wireguard/tunnels/${name}/state`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, force }),
  });
  if (res.ok) {
    setStatus("tunnel-status", `${action === "activate" ? "Activated" : "Deactivated"} ${name}`, true);
    loadTunnels();
    return;
  }
  const body = await res.json();
  if (res.status === 409 && action === "activate" && !force) {
    const confirmed = confirm(`${body.detail}\n\nActivate anyway?`);
    if (confirmed) return setTunnelState(name, action, true);
  }
  setStatus("tunnel-status", body.detail || "Failed", false);
}

document.getElementById("tunnel-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const payload = {
    name: form.get("name"),
    address: form.get("address"),
    listen_port: parseInt(form.get("listen_port"), 10),
    force: document.getElementById("tunnel-force").checked,
  };
  if (form.get("endpoint")) payload.endpoint = form.get("endpoint");

  const res = await fetch("/api/wireguard/tunnels", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.ok) {
    setStatus("tunnel-status", "Created", true);
    document.getElementById("tunnel-force-wrap").classList.add("hidden");
    document.getElementById("tunnel-force").checked = false;
    e.target.reset();
    loadTunnels();
    return;
  }
  const body = await res.json();
  if (res.status === 409) document.getElementById("tunnel-force-wrap").classList.remove("hidden");
  setStatus("tunnel-status", body.detail || "Failed", false);
});

async function loadPeers() {
  const tbody = document.querySelector("#peer-table tbody");
  tbody.innerHTML = "";
  if (!selectedTunnel) return;

  const res = await fetch(`/api/wireguard/tunnels/${selectedTunnel}/peers`);
  if (!res.ok) return;
  const rows = await res.json();
  for (const p of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${p.description}</td><td>${p.tunnel_address}</td><td>${p.allowed_ips.join(", ")}</td>
      <td>${formatHandshake(p.latest_handshake)}</td>
      <td><button data-id="${p.id}" class="del-peer">Delete</button></td>`;
    tbody.appendChild(tr);
  }
  document.querySelectorAll(".del-peer").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const res = await fetch(`/api/wireguard/tunnels/${selectedTunnel}/peers/${btn.dataset.id}`, { method: "DELETE" });
      if (res.ok) { setStatus("peer-status", "Removed", true); loadPeers(); }
      else { const body = await res.json(); setStatus("peer-status", body.detail || "Failed", false); }
    });
  });
}

document.getElementById("peer-tunnel-select").addEventListener("change", (e) => {
  selectedTunnel = e.target.value;
  loadPeers();
});

document.getElementById("peer-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!selectedTunnel) { setStatus("peer-status", "Select a tunnel first", false); return; }
  const form = new FormData(e.target);
  const payload = {
    description: form.get("description"),
    allowed_ips: form.get("allowed_ips").split(",").map((s) => s.trim()).filter(Boolean),
    preshared_key: form.get("preshared_key") === "on",
    keepalive: form.get("keepalive") ? parseInt(form.get("keepalive"), 10) : null,
  };

  const res = await fetch(`/api/wireguard/tunnels/${selectedTunnel}/peers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const body = await res.json();
    setStatus("peer-status", body.detail || "Failed", false);
    return;
  }

  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition") || "";
  const match = disposition.match(/filename="([^"]+)"/);
  const filename = match ? match[1] : "peer.conf";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  setStatus("peer-status", "Peer added — config downloaded", true);
  e.target.reset();
  loadTunnels();
  loadPeers();
});

loadTunnels();
setInterval(() => { loadTunnels(); loadPeers(); }, 5000);
