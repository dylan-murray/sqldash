import { apiToken } from "/static/js/token.js";

const token = apiToken();
const modal = document.getElementById("settings-modal");
const content = document.getElementById("settings-content");

async function openSettings() {
  modal.hidden = false;
  content.innerHTML = '<p class="quiet">Loading…</p>';
  const response = await fetch("/settings/panel");
  content.innerHTML = response.ok
    ? await response.text()
    : '<p class="quiet">Could not load settings.</p>';
}

function closeSettings() {
  modal.hidden = true;
}

document.getElementById("settings-open")?.addEventListener("click", openSettings);
modal?.addEventListener("click", (e) => {
  if (e.target === modal || e.target.closest("#settings-close")) closeSettings();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !modal.hidden) closeSettings();
});

function showRepoError(message) {
  const box = document.getElementById("repo-error");
  if (!box) return;
  box.textContent = message;
  box.hidden = false;
}

document.addEventListener("submit", async (e) => {
  if (e.target.id !== "repo-add-form") return;
  e.preventDefault();
  const input = document.getElementById("repo-add-target");
  const target = input.value.trim();
  if (!target) return;
  const button = e.target.querySelector('button[type="submit"]');
  button.disabled = true;
  button.textContent = "Syncing…";
  try {
    const response = await fetch("/api/repos", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Sqldash-Token": token },
      body: JSON.stringify({ target }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      showRepoError(payload.detail ?? `add failed (${response.status})`);
      return;
    }
    window.location.reload();
  } finally {
    button.disabled = false;
    button.textContent = "Add repo";
  }
});

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".repo-remove");
  if (!btn) return;
  if (!window.confirm(`Stop serving '${btn.dataset.name}' and remove it from the registry? The repo itself is untouched.`)) return;
  const response = await fetch(`/api/repos/${encodeURIComponent(btn.dataset.name)}`, {
    method: "DELETE",
    headers: { "X-Sqldash-Token": token },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    showRepoError(payload.detail ?? `remove failed (${response.status})`);
    return;
  }
  window.location.reload();
});
