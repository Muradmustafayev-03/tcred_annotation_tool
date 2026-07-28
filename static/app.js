const state = {
  config: null,
  status: null,
  clearChallenge: null,
  loading: false,
};

const el = Object.fromEntries(
  [
    "connectionBadge", "refreshButton", "baseUrl", "docsLink", "lastRefresh",
    "startedMetric", "startedDetail", "tutorialMetric", "activeMetric", "readyMetric",
    "finishedMetric", "finishedDetail", "answersMetric", "answersDetail",
    "completionSummary", "annotatorProgress", "annotatorPercent", "answerProgress",
    "answerPercent", "backupSummary", "downloadButton", "backupNowButton",
    "scheduleSelect", "saveScheduleButton", "nextBackup", "backupList", "annotatorRows",
    "openClearButton", "clearDialog", "closeClearButton", "cancelClearButton",
    "clearLoading", "clearContent", "clearWarning", "clearFiles", "clearBytes",
    "clearStarted", "clearFinished", "recoveryBackup", "confirmationPhrase",
    "confirmationInput", "clearAcknowledgement", "confirmClearButton", "toast",
  ].map((id) => [id, document.querySelector(`#${id}`)]),
);

document.addEventListener("DOMContentLoaded", initialize);

async function initialize() {
  bindEvents();
  try {
    state.config = await requestJson("/api/config");
    el.baseUrl.textContent = state.config.annotation_base_url;
    el.docsLink.href = state.config.annotation_docs_url;
    renderScheduleOptions(state.config.schedule_interval_hours);
    await refreshStatus();
    window.setInterval(refreshStatus, 30_000);
  } catch (error) {
    showToast(error.message, true);
    renderDisconnected(error.message);
  }
}

function bindEvents() {
  el.refreshButton.addEventListener("click", refreshStatus);
  el.downloadButton.addEventListener("click", downloadSnapshot);
  el.backupNowButton.addEventListener("click", runBackup);
  el.saveScheduleButton.addEventListener("click", saveSchedule);
  el.openClearButton.addEventListener("click", openClearDialog);
  el.closeClearButton.addEventListener("click", closeClearDialog);
  el.cancelClearButton.addEventListener("click", closeClearDialog);
  el.confirmationInput.addEventListener("input", updateClearButton);
  el.clearAcknowledgement.addEventListener("change", updateClearButton);
  el.confirmClearButton.addEventListener("click", confirmClear);
  el.clearDialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeClearDialog();
  });
}

async function refreshStatus() {
  if (state.loading) return;
  state.loading = true;
  setButtonBusy(el.refreshButton, true, "Refreshing...");
  try {
    state.status = await requestJson("/api/status");
    renderStatus();
  } catch (error) {
    renderDisconnected(error.message);
    showToast(error.message, true);
  } finally {
    state.loading = false;
    setButtonBusy(el.refreshButton, false, "Refresh");
  }
}

function renderStatus() {
  const connection = state.status.connection || {};
  if (!connection.connected) {
    renderDisconnected(connection.error || "Annotation service unavailable");
  } else {
    el.connectionBadge.className = "status-badge connected";
    el.connectionBadge.textContent = "Service connected";
  }
  const progress = state.status.progress;
  if (progress?.summary) {
    renderSummary(progress.summary);
    renderAnnotators(progress.annotators || []);
    el.lastRefresh.textContent = `Updated ${formatDate(progress.generated_at)}`;
  }
  renderBackups(state.status.backups || {});
}

function renderSummary(summary) {
  const expected = number(summary.expected_annotators);
  el.startedMetric.textContent = number(summary.started);
  el.startedDetail.textContent = `of ${expected} annotators`;
  el.tutorialMetric.textContent = number(summary.tutorial_completed);
  el.activeMetric.textContent = number(summary.research_started);
  el.readyMetric.textContent = number(summary.ready_to_submit);
  el.finishedMetric.textContent = number(summary.finished);
  el.finishedDetail.textContent = `of ${expected} annotators`;
  el.answersMetric.textContent = number(summary.answers_completed);
  el.answersDetail.textContent = `of ${number(summary.answers_total)} answers`;
  const annotatorPercent = number(summary.annotator_completion_percent);
  const answerPercent = number(summary.answer_completion_percent);
  setProgress(el.annotatorProgress, el.annotatorPercent, annotatorPercent);
  setProgress(el.answerProgress, el.answerPercent, answerPercent);
  el.completionSummary.textContent =
    `${number(summary.in_progress)} in progress · ${number(summary.not_started)} not started`;
}

function renderAnnotators(rows) {
  if (!rows.length) {
    el.annotatorRows.innerHTML =
      '<tr><td colspan="6" class="empty">No annotator assignments are available.</td></tr>';
    return;
  }
  el.annotatorRows.innerHTML = rows.map((row) => `
    <tr>
      <td><strong>${escapeHtml(row.annotator_key)}</strong></td>
      <td><span class="row-status ${statusClass(row.status)}">${statusLabel(row.status)}</span></td>
      <td>${number(row.answers_completed)} / ${number(row.answers_total)}</td>
      <td>
        <progress class="table-progress" aria-label="${number(row.completion_percent)} percent complete"
          max="100" value="${clampPercent(row.completion_percent)}"></progress>
      </td>
      <td>${formatDate(row.last_activity_at)}</td>
      <td>${formatDate(row.submitted_at)}</td>
    </tr>
  `).join("");
}

function renderBackups(backups) {
  const interval = number(backups.interval_hours);
  el.scheduleSelect.value = String(interval);
  el.backupSummary.textContent =
    `${number(backups.archive_count)} archives · ${formatBytes(backups.total_bytes)}`;
  el.nextBackup.textContent = backups.enabled
    ? `Next scheduled backup: ${formatDate(backups.next_run_at)}`
    : "Automatic backups are disabled.";
  if (backups.last_error) {
    el.nextBackup.textContent += ` Last error: ${backups.last_error}`;
  }
  const recent = backups.recent || [];
  el.backupList.innerHTML = recent.length
    ? recent.map((backup) => `
      <li>
        <span>${escapeHtml(backup.filename)}</span>
        <small>${formatBytes(backup.bytes)} · ${formatDate(backup.created_at)}</small>
      </li>
    `).join("")
    : '<li class="empty">No backups have been stored by this dashboard.</li>';
}

function renderDisconnected(message) {
  el.connectionBadge.className = "status-badge disconnected";
  el.connectionBadge.textContent = "Service unavailable";
  el.lastRefresh.textContent = message || "Could not load status";
  el.annotatorRows.innerHTML =
    '<tr><td colspan="6" class="empty">Connect the annotation service to load progress.</td></tr>';
}

async function downloadSnapshot() {
  setButtonBusy(el.downloadButton, true, "Preparing...");
  try {
    const response = await fetch("/api/download", {
      method: "POST",
      headers: mutationHeaders(false),
    });
    if (!response.ok) throw new Error(await responseError(response));
    const blob = await response.blob();
    const filename = responseFilename(response) || "tcred-annotations.zip";
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
    showToast(`Downloaded ${filename}`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setButtonBusy(el.downloadButton, false, "Download snapshot");
  }
}

async function runBackup() {
  setButtonBusy(el.backupNowButton, true, "Backing up...");
  try {
    const result = await requestJson("/api/backup/run", {method: "POST"});
    showToast(`Stored ${result.filename}`);
    await refreshStatus();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setButtonBusy(el.backupNowButton, false, "Back up now");
  }
}

async function saveSchedule() {
  setButtonBusy(el.saveScheduleButton, true, "Applying...");
  try {
    await requestJson("/api/backup/schedule", {
      method: "POST",
      body: {interval_hours: Number(el.scheduleSelect.value)},
    });
    showToast(
      Number(el.scheduleSelect.value)
        ? `Automatic backups set to every ${el.scheduleSelect.value} hour(s)`
        : "Automatic backups disabled",
    );
    await refreshStatus();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setButtonBusy(el.saveScheduleButton, false, "Apply");
  }
}

async function openClearDialog() {
  state.clearChallenge = null;
  el.clearLoading.hidden = false;
  el.clearContent.hidden = true;
  el.confirmationInput.value = "";
  el.clearAcknowledgement.checked = false;
  el.recoveryBackup.checked = true;
  updateClearButton();
  el.clearDialog.showModal();
  try {
    const challenge = await requestJson("/api/clear/prepare", {method: "POST"});
    state.clearChallenge = challenge;
    el.clearWarning.textContent = challenge.warning;
    el.clearFiles.textContent = number(challenge.live_file_count);
    el.clearBytes.textContent = formatBytes(challenge.live_bytes);
    el.clearStarted.textContent = number(challenge.progress_summary?.started);
    el.clearFinished.textContent = number(challenge.progress_summary?.finished);
    el.confirmationPhrase.textContent = challenge.confirmation_phrase;
    el.clearLoading.hidden = true;
    el.clearContent.hidden = false;
    el.confirmationInput.focus();
  } catch (error) {
    closeClearDialog();
    showToast(error.message, true);
  }
}

function closeClearDialog() {
  state.clearChallenge = null;
  if (el.clearDialog.open) el.clearDialog.close();
}

function updateClearButton() {
  const phrase = state.clearChallenge?.confirmation_phrase || "";
  el.confirmClearButton.disabled =
    !phrase
    || el.confirmationInput.value !== phrase
    || !el.clearAcknowledgement.checked;
}

async function confirmClear() {
  if (!state.clearChallenge || el.confirmClearButton.disabled) return;
  setButtonBusy(el.confirmClearButton, true, "Clearing...");
  try {
    const result = await requestJson("/api/clear/confirm", {
      method: "POST",
      body: {
        challenge_token: state.clearChallenge.challenge_token,
        state_sha256: state.clearChallenge.state_sha256,
        confirmation_phrase: el.confirmationInput.value,
        backup_before_clear: el.recoveryBackup.checked,
      },
    });
    closeClearDialog();
    const recovery = result.recovery_backup?.filename
      ? ` Recovery snapshot: ${result.recovery_backup.filename}.`
      : "";
    showToast(`Cleared ${number(result.deleted_files)} live files.${recovery}`);
    await refreshStatus();
  } catch (error) {
    showToast(error.message, true);
    state.clearChallenge = null;
    el.confirmClearButton.disabled = true;
    el.clearWarning.textContent =
      `${error.message} Close this dialog and prepare a new deletion challenge.`;
  } finally {
    el.confirmClearButton.textContent = "Permanently clear live data";
    updateClearButton();
  }
}

async function requestJson(path, options = {}) {
  const method = options.method || "GET";
  const headers = method === "GET" ? {} : mutationHeaders(options.body !== undefined);
  const response = await fetch(path, {
    method,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: "no-store",
  });
  if (!response.ok) throw new Error(await responseError(response));
  return response.json();
}

function mutationHeaders(hasJson) {
  const headers = {"X-Tcred-Admin-Csrf": state.config?.csrf_token || ""};
  if (hasJson) headers["Content-Type"] = "application/json";
  return headers;
}

async function responseError(response) {
  try {
    const payload = await response.json();
    return payload.error || `Request failed (${response.status})`;
  } catch {
    return `Request failed (${response.status})`;
  }
}

function renderScheduleOptions(intervals) {
  el.scheduleSelect.innerHTML = intervals.map((hours) => `
    <option value="${hours}">${hours === 0 ? "Disabled" : `Every ${hours} hour${hours === 1 ? "" : "s"}`}</option>
  `).join("");
}

function setProgress(bar, label, value) {
  const percent = clampPercent(value);
  bar.value = percent;
  bar.setAttribute("aria-valuenow", String(percent));
  label.textContent = `${percent}%`;
}

function setButtonBusy(button, busy, label) {
  button.disabled = busy;
  button.textContent = label;
}

function showToast(message, isError = false) {
  el.toast.textContent = message;
  el.toast.className = `toast ${isError ? "error" : "success"}`;
  el.toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    el.toast.hidden = true;
  }, 5000);
}

function responseFilename(response) {
  const disposition = response.headers.get("Content-Disposition") || "";
  return disposition.match(/filename="([^"]+)"/)?.[1] || null;
}

function statusLabel(value) {
  return {
    not_started: "Not started",
    opened: "Opened",
    tutorial_completed: "Tutorial complete",
    research_in_progress: "Research in progress",
    ready_to_submit: "Ready to submit",
    finished: "Finished",
    assignment_missing: "Assignment missing",
  }[value] || "Unknown";
}

function statusClass(value) {
  return String(value || "unknown")
    .replaceAll("_", "-")
    .replace(/[^a-z0-9-]/gi, "");
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return "—";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function formatBytes(value) {
  const bytes = number(value);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
}

function number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function clampPercent(value) {
  return Math.max(0, Math.min(100, number(value)));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
