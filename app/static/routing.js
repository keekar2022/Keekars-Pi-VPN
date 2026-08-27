// Concept: Mukesh Kesharwani
// Contact: mukesh.kesharwani@adobe.com

function setStatus(msg, ok) {
  const el = document.getElementById("routing-status");
  el.textContent = msg;
  el.className = ok ? "ok" : "error";
}

async function loadRoutes() {
  const res = await fetch("/api/routing");
  const rows = await res.json();
  const tbody = document.querySelector("#route-table tbody");
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    const dest = r.dst_len != null ? `${r.destination}/${r.dst_len}` : "default";
    tr.innerHTML = `<td>${dest}</td><td>${r.gateway || "-"}</td><td>${r.interface || "-"}</td>
      <td><button data-dest="${dest}" data-iface="${r.interface || ""}" class="del-route">Delete</button></td>`;
    tbody.appendChild(tr);
  }
  document.querySelectorAll(".del-route").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const res = await fetch("/api/routing", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ destination: btn.dataset.dest, interface: btn.dataset.iface }),
      });
      if (res.ok) { setStatus("Removed", true); loadRoutes(); }
      else { const body = await res.json(); setStatus(body.detail || "Failed", false); }
    });
  });
}
loadRoutes();

document.getElementById("route-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const payload = { destination: form.get("destination"), interface: form.get("interface") };
  if (form.get("gateway")) payload.gateway = form.get("gateway");
  const res = await fetch("/api/routing", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.ok) { setStatus("Added", true); loadRoutes(); e.target.reset(); }
  else { const body = await res.json(); setStatus(body.detail || "Failed", false); }
});
