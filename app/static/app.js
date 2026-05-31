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
})();
