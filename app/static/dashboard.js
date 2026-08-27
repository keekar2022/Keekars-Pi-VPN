// Concept: Mukesh Kesharwani
// Contact: mukesh.kesharwani@adobe.com

const POLL_INTERVAL_MS = 5000;
const MAX_POINTS = 60;
const cpuData = { labels: [], datasets: [{ label: "CPU %", data: [], borderColor: "#2563eb", fill: false }] };
const cpuChart = new Chart(document.getElementById("cpuChart"), {
  type: "line",
  data: cpuData,
  options: { animation: false, scales: { y: { min: 0, max: 100 } } },
});

function formatRate(bytesPerSec) {
  const kb = bytesPerSec / 1024;
  return kb >= 1024 ? (kb / 1024).toFixed(2) + " MB/s" : kb.toFixed(1) + " KB/s";
}

// Largest-two-units display (e.g. "1d 4h", "3h 12m", "45s"). The underlying
// figure is only accurate to the ~60s heartbeat interval on the server, so
// sub-minute precision isn't shown once the duration exceeds a minute.
function formatDuration(seconds) {
  seconds = Math.max(Math.floor(seconds), 0);
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

function renderRollbackAlert(alert) {
  const banner = document.getElementById("rollback-banner");
  if (!alert) {
    banner.classList.add("hidden");
    return;
  }
  let msg = `Previous update crashed the system — ${alert.reason}`;
  if (alert.packages_reverted && alert.packages_reverted.length) {
    msg += ` Reverted: ${alert.packages_reverted.join(", ")}.`;
  }
  if (alert.packages_unavailable && alert.packages_unavailable.length) {
    msg += ` Could not auto-fix: ${alert.packages_unavailable.join(", ")}.`;
  }
  document.getElementById("rollback-banner-text").textContent = msg;
  banner.classList.remove("hidden");
}

document.getElementById("rollback-banner-dismiss").addEventListener("click", async () => {
  try {
    await fetch("/api/monitor/rollback-alert/dismiss", { method: "POST" });
  } catch (err) {
    // best-effort — if this failed, the next poll will just show it again
  }
  document.getElementById("rollback-banner").classList.add("hidden");
});

let pollTimer = null;

async function pollStats() {
  const statusEl = document.getElementById("monitor-status");
  try {
    // redirect: "manual" is deliberate: if the session has expired, the
    // server responds with a redirect to /auth/login. Letting fetch follow
    // that silently would start a brand-new OAuth handshake in the
    // background on every 5s tick, clobbering the `state` of any login the
    // user is actually in the middle of via the shared session cookie.
    const res = await fetch("/api/monitor/stats", { redirect: "manual" });
    if (res.type === "opaqueredirect" || res.status === 0) {
      statusEl.textContent = "Session expired — reload the page to log in again.";
      statusEl.className = "error";
      if (pollTimer) clearInterval(pollTimer);
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    document.getElementById("cpu-value").textContent = data.cpu_percent.toFixed(1);
    document.getElementById("mem-value").textContent = data.mem_percent.toFixed(1);
    document.getElementById("disk-value").textContent = data.disk_percent.toFixed(1);
    document.getElementById("downtime-value").textContent =
      data.last_downtime_seconds != null ? formatDuration(data.last_downtime_seconds) : "—";
    renderRollbackAlert(data.rollback_alert);
    statusEl.textContent = "";

    const t = new Date().toLocaleTimeString();
    cpuData.labels.push(t);
    cpuData.datasets[0].data.push(data.cpu_percent);
    if (cpuData.labels.length > MAX_POINTS) {
      cpuData.labels.shift();
      cpuData.datasets[0].data.shift();
    }
    cpuChart.update();

    const tbody = document.querySelector("#iface-table tbody");
    tbody.innerHTML = "";
    for (const [name, stats] of Object.entries(data.interfaces)) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${name}</td><td>${formatRate(stats.bytes_sent_per_sec)}</td><td>${formatRate(stats.bytes_recv_per_sec)}</td>`;
      tbody.appendChild(tr);
    }

    const cpuTbody = document.querySelector("#top-cpu-table tbody");
    cpuTbody.innerHTML = "";
    for (const p of data.top_cpu) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${p.pid}</td><td>${p.name}</td><td>${p.cpu_percent.toFixed(1)}</td>`;
      cpuTbody.appendChild(tr);
    }

    const memTbody = document.querySelector("#top-mem-table tbody");
    memTbody.innerHTML = "";
    for (const p of data.top_mem) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${p.pid}</td><td>${p.name}</td><td>${p.mem_mb.toFixed(1)} MB</td>`;
      memTbody.appendChild(tr);
    }
  } catch (err) {
    statusEl.textContent = "Unable to reach the server for live stats.";
    statusEl.className = "error";
  }
}

pollStats();
pollTimer = setInterval(pollStats, POLL_INTERVAL_MS);
