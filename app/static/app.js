(function () {
  const tables = document.querySelectorAll("table");
  tables.forEach((table) => {
    if (!table.parentElement.classList.contains("table-wrap")) {
      const wrapper = document.createElement("div");
      wrapper.className = "table-wrap";
      table.parentNode.insertBefore(wrapper, table);
      wrapper.appendChild(table);
    }
  });

  document.body.addEventListener("htmx:beforeRequest", (event) => {
    const form = event.target.closest("form");
    if (!form) return;
    form.querySelectorAll("button").forEach((button) => {
      button.disabled = true;
    });
  });

  document.body.addEventListener("htmx:afterRequest", (event) => {
    const form = event.target.closest("form");
    if (!form) return;
    form.querySelectorAll("button").forEach((button) => {
      button.disabled = false;
    });
  });

  // "Data as of …" topbar indicator: format the refresh timestamp in local time and a live
  // "N min ago", flagging staleness (> 25 min) client-side so it stays current between requests.
  function renderDataAsOf() {
    const el = document.getElementById("data-asof");
    if (!el) return;
    const iso = el.getAttribute("data-ts");
    if (!iso) return; // no refresh yet — leave the "Awaiting first market refresh" label
    const ts = new Date(iso);
    if (isNaN(ts.getTime())) return;
    const ageMin = Math.max(0, Math.round((Date.now() - ts.getTime()) / 60000));
    let ago;
    if (ageMin < 1) ago = "just now";
    else if (ageMin === 1) ago = "1 min ago";
    else if (ageMin < 90) ago = ageMin + " min ago";
    else ago = Math.round(ageMin / 60) + " hr ago";
    const time = ts.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    el.textContent = "Data as of " + time + " · " + ago;
    el.classList.toggle("stale", ageMin > 25);
  }
  renderDataAsOf();
  setInterval(renderDataAsOf, 30000);
})();
