(function () {
  const config = window.CPM;
  if (!config) return;

  const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));

  const titleCase = (s) => String(s || "").toLowerCase().replace(/(^|_)\w/g, x => x.toUpperCase());

  function updateCounts(counts) {
    Object.entries(counts || {}).forEach(([key, value]) => {
      const node = document.getElementById("count-" + key);
      if (node) node.textContent = value;
    });
  }

  function updatePrinters(printers) {
    const root = document.getElementById("printer-list");
    if (!root || !printers) return;
    const existing = new Map([...root.querySelectorAll("[data-printer-id]")].map(el => [String(el.dataset.printerId), el]));
    printers.forEach(p => {
      const node = existing.get(String(p.id));
      if (!node) return; // Newly added printer appears after a normal page refresh.
      const status = node.querySelector(".status");
      if (status) {
        status.className = "status status-" + p.status.toLowerCase();
        status.textContent = p.status;
      }
    });
  }

  function updateJobs(jobs) {
    const root = document.getElementById("jobs-table");
    if (!root || !jobs) return;
    const existing = new Map([...root.querySelectorAll("tr[data-job-id]")].map(el => [String(el.dataset.jobId), el]));
    jobs.forEach(j => {
      const row = existing.get(String(j.id));
      if (!row) return; // Refresh to see newly submitted jobs.
      const status = row.querySelector(".status");
      if (status) {
        status.className = "status status-" + j.status.toLowerCase();
        status.textContent = j.status;
      }
      const progress = row.querySelector(".progress-track span");
      if (progress) {
        const total = Number(j.page_count) * Number(j.copies);
        const pct = total ? Math.min(100, Math.round(100 * Number(j.progress_pages) / total)) : 0;
        progress.style.width = pct + "%";
      }
      const progressText = row.querySelector(".progress-cell small");
      if (progressText) progressText.textContent = `${j.progress_pages}/${Number(j.page_count) * Number(j.copies)} pages`;
      // Printer column differs by role.
      const cells = row.querySelectorAll("td");
      if (config.role === "student" && cells.length > 3) cells[3].textContent = j.printer_name || "—";
      if (config.role === "admin" && cells.length > 4) cells[4].textContent = j.printer_name || "—";
    });
  }

  async function tick() {
    try {
      await fetch(config.tickUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin"
      });
    } catch (_) { /* A refresh will retry; keep the dashboard usable offline. */ }
  }

  async function refresh() {
    try {
      const response = await fetch(config.statusUrl, { credentials: "same-origin" });
      if (!response.ok) return;
      const data = await response.json();
      updateCounts(data.counts);
      updatePrinters(data.printers);
      updateJobs(data.jobs);
    } catch (_) { /* Ignore transient polling errors. */ }
  }

  const fileInput = document.getElementById("document");
  if (fileInput) {
    fileInput.addEventListener("change", () => {
      const label = document.getElementById("file-name");
      if (label) label.textContent = fileInput.files.length ? fileInput.files[0].name : "No file selected";
    });
  }

  // A single page view advances the shared simulation. This is suitable for a local class demo;
  // for a deployed multi-user system, move ticking to a dedicated background worker.
  if (config.tickUrl) {
    window.setInterval(async () => {
      await tick();
      await refresh();
    }, 1000);
    refresh();
  }
})();
