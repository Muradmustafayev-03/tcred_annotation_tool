const form = document.querySelector("#annotatorForm");
const input = document.querySelector("#annotatorInput");
const error = document.querySelector("#entryError");
let annotatorCount = 36;

async function initEntry() {
  try {
    const response = await fetch("/api/config", { cache: "no-store" });
    if (response.ok) {
      const config = await response.json();
      if (Number.isInteger(config.annotator_count) && config.annotator_count > 0) {
        annotatorCount = config.annotator_count;
      }
    }
  } catch (_error) {
    // The server validates the id again; retain the production default if config loading fails.
  }
  const params = new URLSearchParams(window.location.search);
  input.value = params.get("annotator_id") || "";
  input.addEventListener("input", () => {
    input.value = input.value.replaceAll(/\D/g, "");
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const rawId = input.value.trim();
    const id = Number(rawId);
    if (!rawId || !Number.isInteger(id) || id < 0 || id > annotatorCount) {
      error.textContent = `Enter an annotator id from 0 to ${annotatorCount}.`;
      input.focus();
      return;
    }
    window.location.href = `/annotate?annotator_id=${id}`;
  });
}

initEntry();
