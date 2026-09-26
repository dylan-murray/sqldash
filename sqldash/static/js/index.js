import { dashboardPath } from "/static/js/paths.js";
import { apiToken } from "/static/js/token.js";

const token = apiToken();
const modal = document.getElementById("new-dash-modal");
const form = document.getElementById("new-dash-form");
const titleInput = document.getElementById("new-dash-title");
const errorBox = document.getElementById("new-dash-error");

function openModal() {
  errorBox.hidden = true;
  modal.hidden = false;
  titleInput.value = "";
  titleInput.focus();
}

function closeModal() {
  modal.hidden = true;
}

document.getElementById("new-dashboard")?.addEventListener("click", openModal);
document.getElementById("new-dash-cancel")?.addEventListener("click", closeModal);
modal?.addEventListener("click", (e) => {
  if (e.target === modal) closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !modal.hidden) closeModal();
});

form?.addEventListener("submit", async (e) => {
  e.preventDefault();
  errorBox.hidden = true;
  const title = titleInput.value.trim();
  if (!title) return;
  const repo = document.getElementById("new-dash-repo")?.value;
  const response = await fetch("/api/dashboards", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Sqldash-Token": token },
    body: JSON.stringify(repo ? { title, repo } : { title }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    errorBox.textContent = payload.detail ?? `create failed (${response.status})`;
    errorBox.hidden = false;
    return;
  }
  window.location.href = `/d/${dashboardPath(payload.name)}/workspace`;
});

const deleteModal = document.getElementById("delete-modal");
const deleteMessage = document.getElementById("delete-message");
const deleteError = document.getElementById("delete-error");
let pendingDelete = null;

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".card-delete");
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  pendingDelete = btn.dataset;
  deleteMessage.textContent =
    `Delete “${pendingDelete.title}”? This deletes ${pendingDelete.file} from disk — ` +
    "if the project is in git, you can restore it from history.";
  deleteError.hidden = true;
  deleteModal.hidden = false;
});

document.getElementById("delete-cancel")?.addEventListener("click", () => {
  deleteModal.hidden = true;
  pendingDelete = null;
});
deleteModal?.addEventListener("click", (e) => {
  if (e.target === deleteModal) deleteModal.hidden = true;
});

document.getElementById("delete-confirm")?.addEventListener("click", async () => {
  if (!pendingDelete) return;
  const response = await fetch(`/api/dashboards/${dashboardPath(pendingDelete.name)}`, {
    method: "DELETE",
    headers: { "X-Sqldash-Token": token, "If-Match": pendingDelete.etag },
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    deleteError.textContent = payload.detail ?? `delete failed (${response.status})`;
    deleteError.hidden = false;
    return;
  }
  window.location.reload();
});

const filterInput = document.getElementById("browser-filter");

function restripe() {
  for (const browser of document.querySelectorAll(".browser")) {
    let i = 0;
    for (const row of browser.querySelectorAll(".drow")) {
      const visible = !row.hidden && !row.closest("[data-repo-group]")?.hidden;
      row.classList.toggle("alt", visible && i % 2 === 1);
      if (visible) i += 1;
    }
  }
}

function applyFilter() {
  const query = filterInput.value.trim().toLowerCase();
  for (const browser of document.querySelectorAll(".browser")) {
    let anyVisible = false;
    for (const row of browser.querySelectorAll(".drow")) {
      const visible = !query || row.dataset.search.includes(query);
      row.hidden = !visible;
      if (visible) anyVisible = true;
    }
    for (const folder of browser.querySelectorAll(".dfolder")) {
      const group = browser.querySelector(`[data-repo-group="${folder.dataset.repo}"]`);
      if (!group) continue;
      const groupHasVisible = [...group.querySelectorAll(".drow")].some((r) => !r.hidden);
      folder.hidden = query ? !groupHasVisible : false;
      if (query) group.hidden = !groupHasVisible;
      else group.hidden = folder.getAttribute("aria-expanded") === "false";
    }
    const empty = browser.querySelector(".browser-empty");
    if (empty) empty.hidden = anyVisible || !query;
  }
  restripe();
}

filterInput?.addEventListener("input", applyFilter);
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement?.tagName !== "INPUT"
      && document.activeElement?.tagName !== "TEXTAREA") {
    e.preventDefault();
    filterInput?.focus();
  }
  if (e.key === "Escape" && document.activeElement === filterInput) {
    filterInput.value = "";
    applyFilter();
    filterInput.blur();
  }
});

const COLLAPSE_KEY = "sqldash-collapsed";
const collapsed = new Set(JSON.parse(localStorage.getItem(COLLAPSE_KEY) ?? "[]"));

for (const folder of document.querySelectorAll(".dfolder")) {
  const group = document.querySelector(`[data-repo-group="${folder.dataset.repo}"]`);
  if (!group) continue;
  const applyState = () => {
    const open = !collapsed.has(folder.dataset.repo);
    folder.setAttribute("aria-expanded", String(open));
    group.hidden = !open;
  };
  applyState();
  folder.addEventListener("click", () => {
    if (collapsed.has(folder.dataset.repo)) collapsed.delete(folder.dataset.repo);
    else collapsed.add(folder.dataset.repo);
    localStorage.setItem(COLLAPSE_KEY, JSON.stringify([...collapsed]));
    applyState();
    restripe();
  });
}
restripe();

const exploreDialog=document.getElementById('explore-dialog');
const exploreButton=document.getElementById('home-explore');
if(exploreButton?.tagName==='BUTTON')exploreButton.onclick=()=>{
  if(exploreDialog)exploreDialog.showModal();else openModal();
};
document.getElementById('explore-open')?.addEventListener('click',()=>{
  location.href=document.getElementById('explore-dashboard').value;
});
