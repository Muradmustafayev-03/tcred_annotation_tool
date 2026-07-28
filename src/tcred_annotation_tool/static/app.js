const SERVER_AUTOSAVE_DELAY_MS = 1500;
const NARROW_WORKSPACE = window.matchMedia("(max-width: 980px)");

const state = {
  annotatorId: null,
  annotatorKey: "",
  assignmentId: "",
  collectionId: "",
  assignmentToken: "",
  debug: false,
  rows: [],
  protocol: null,
  currentIndex: 0,
  initialUnitIndex: 0,
  initialFocus: "",
  mode: "instructions",
  showOpenOnly: false,
  saveTimer: null,
  localKey: "",
  instructionsKey: "",
  tutorialKey: "",
  tutorialVersion: "",
  tutorialItems: [],
  tutorialIndex: 0,
  tutorialStatus: {},
  tutorialCompleted: false,
  tutorialMessage: "",
  outputSavedAt: null,
  revision: "none",
  submittedAt: null,
};

const el = {
  assignmentMeta: document.querySelector("#assignmentMeta"),
  modeBadge: document.querySelector("#modeBadge"),
  clearDebugProgressButton: document.querySelector("#clearDebugProgressButton"),
  instructionsView: document.querySelector("#instructionsView"),
  annotationLayout: document.querySelector("#annotationLayout"),
  reviewView: document.querySelector("#reviewView"),
  submittedView: document.querySelector("#submittedView"),
  startTutorialButton: document.querySelector("#startTutorialButton"),
  progressText: document.querySelector("#progressText"),
  progressBar: document.querySelector("#progressBar"),
  saveState: document.querySelector("#saveState"),
  unitList: document.querySelector("#unitList"),
  allUnitsButton: document.querySelector("#allUnitsButton"),
  openUnitsButton: document.querySelector("#openUnitsButton"),
  reviewButton: document.querySelector("#reviewButton"),
  prevButton: document.querySelector("#prevButton"),
  nextButton: document.querySelector("#nextButton"),
  saveButton: document.querySelector("#saveButton"),
  unitTitle: document.querySelector("#unitTitle"),
  unitSubtitle: document.querySelector("#unitSubtitle"),
  workspaceScroll: document.querySelector(".workspace-grid"),
  reviewNav: document.querySelector(".review-nav"),
  stageNumber: document.querySelector("#stageNumber"),
  stageTitle: document.querySelector("#stageTitle"),
  stageDescription: document.querySelector("#stageDescription"),
  stageStatus: document.querySelector("#stageStatus"),
  questionText: document.querySelector("#questionText"),
  questionScopeNote: document.querySelector("#questionScopeNote"),
  contextNote: document.querySelector("#contextNote"),
  questionReminder: document.querySelector("#questionReminder"),
  referencePanel: document.querySelector("#referencePanel"),
  referenceText: document.querySelector("#referenceText"),
  referenceReminderBlock: document.querySelector("#referenceReminderBlock"),
  referenceReminder: document.querySelector("#referenceReminder"),
  answerText: document.querySelector("#answerText"),
  answerReminder: document.querySelector("#answerReminder"),
  evidenceList: document.querySelector("#evidenceList"),
  graphPathList: document.querySelector("#graphPathList"),
  lockedJudgments: document.querySelector("#lockedJudgments"),
  labelControls: document.querySelector("#labelControls"),
  logicalConflict: document.querySelector("#logicalConflict"),
  issueBlock: document.querySelector("#issueBlock"),
  issueSelect: document.querySelector("#issueSelect"),
  issueDetail: document.querySelector("#issueDetail"),
  commentBlock: document.querySelector("#commentBlock"),
  commentInput: document.querySelector("#commentInput"),
  revealReferenceButton: document.querySelector("#revealReferenceButton"),
  stageActionHelp: document.querySelector("#stageActionHelp"),
  tutorialCoach: document.querySelector("#tutorialCoach"),
  tutorialStage: document.querySelector("#tutorialStage"),
  tutorialCoachTitle: document.querySelector("#tutorialCoachTitle"),
  tutorialCoachInstruction: document.querySelector("#tutorialCoachInstruction"),
  tutorialCoachReason: document.querySelector("#tutorialCoachReason"),
  tutorialFeedback: document.querySelector("#tutorialFeedback"),
  tutorialActionButton: document.querySelector("#tutorialActionButton"),
  backToAssignmentButton: document.querySelector("#backToAssignmentButton"),
  reviewSummary: document.querySelector("#reviewSummary"),
  submitReadiness: document.querySelector("#submitReadiness"),
  submitButton: document.querySelector("#submitButton"),
  submittedMessage: document.querySelector("#submittedMessage"),
};

function init() {
  const params = new URLSearchParams(window.location.search);
  const annotatorId = annotatorIdFromParams(params);
  state.initialUnitIndex = initialUnitIndexFromParams(params);
  state.initialFocus = params.get("focus") || "";
  bindEvents();
  if (annotatorId === null) {
    showError(new Error("Open this page from the annotator id screen."));
    window.setTimeout(() => {
      window.location.href = "/";
    }, 1200);
    return;
  }
  loadAssignment(annotatorId).catch(showError);
}

function bindEvents() {
  el.startTutorialButton.addEventListener("click", startTutorial);
  el.clearDebugProgressButton.addEventListener("click", clearDebugProgress);
  el.prevButton.addEventListener("click", () => goToRelative(-1));
  el.nextButton.addEventListener("click", () => goToRelative(1));
  el.saveButton.addEventListener("click", () => saveNow().catch(showSaveError));
  el.tutorialActionButton.addEventListener("click", handleTutorialAction);
  el.revealReferenceButton.addEventListener("click", handleStageAction);
  el.allUnitsButton.addEventListener("click", () => setUnitFilter(false));
  el.openUnitsButton.addEventListener("click", () => setUnitFilter(true));
  el.reviewButton.addEventListener("click", openReview);
  el.backToAssignmentButton.addEventListener("click", closeReview);
  el.submitButton.addEventListener("click", () => submitAssignment().catch(showSaveError));
  document.querySelectorAll("[data-section-target]").forEach((button) => {
    button.addEventListener("click", () => focusWorkspaceSection(button.dataset.sectionTarget));
  });
  el.commentInput.addEventListener("input", () => updateTextField("comment", el.commentInput.value));
  el.issueSelect.addEventListener("change", () => {
    const row = currentRow();
    if (!row) return;
    row.annotation.issue_code = el.issueSelect.value;
    if (!row.annotation.issue_code) row.annotation.issue_detail = "";
    annotationChanged();
  });
  el.issueDetail.addEventListener("input", () => updateTextField("issue_detail", el.issueDetail.value));
  window.addEventListener("beforeunload", () => {
    persistLocal();
    persistTutorialLocal();
  });
  NARROW_WORKSPACE.addEventListener("change", handleWorkspaceBreakpointChange);
  document.addEventListener("keydown", handleKeys);
}

async function loadAssignment(annotatorId) {
  setSaveState("Loading");
  const payload = await fetchJson(`/api/assignment?annotator_id=${annotatorId}`);
  state.annotatorId = payload.annotator_id;
  state.annotatorKey = payload.annotator_key;
  state.assignmentId = payload.assignment_id;
  state.collectionId = payload.collection_id || "legacy";
  state.assignmentToken = payload.assignment_token || "";
  state.debug = Boolean(payload.debug);
  state.rows = payload.rows || [];
  state.protocol = payload.protocol;
  state.currentIndex = clampIndex(state.initialUnitIndex, state.rows.length);
  state.localKey = `tcred.annotation.v3.${state.collectionId}.${state.annotatorKey}.${state.assignmentId}`;
  state.instructionsKey = `tcred.instructions.${state.collectionId}.${state.annotatorKey}.${state.assignmentId}.${state.protocol.version}`;
  state.tutorialVersion = String(payload.tutorial?.version || "1");
  state.tutorialItems = structuredClone(payload.tutorial?.items || []);
  state.tutorialKey = `tcred.tutorial.${state.collectionId}.${state.annotatorKey}.${state.assignmentId}.${state.tutorialVersion}`;
  state.outputSavedAt = payload.output_saved_at || null;
  state.revision = payload.revision || "none";
  state.submittedAt = payload.submitted_at || null;
  el.clearDebugProgressButton.hidden = !state.debug;
  ensureAnnotationStates(state.rows);
  ensureAnnotationStates(state.tutorialItems);
  mergeLocal(payload);
  restoreTutorialLocal();

  if (payload.submitted) {
    state.mode = "submitted";
  } else if (!localStorage.getItem(state.instructionsKey)) {
    state.mode = "instructions";
  } else if (!state.tutorialCompleted && state.tutorialItems.length) {
    state.mode = "tutorial";
  } else {
    state.mode = "assignment";
  }
  setSaveState(state.mode === "assignment" ? "Loaded" : "Saved locally");
  renderAll();
  focusInitialSection();
  if (state.tutorialCompleted) {
    reportActivity("tutorial_completed", { tutorial_summary: tutorialSummary() }).catch(() => {});
  }
}

function startTutorial() {
  localStorage.setItem(state.instructionsKey, new Date().toISOString());
  state.mode = state.tutorialCompleted ? "assignment" : "tutorial";
  renderAll();
  focusWorkspaceSection("#questionPanel");
}

async function clearDebugProgress() {
  if (!state.debug) return;
  const confirmed = window.confirm(
    "Clear annotator 0 practice and research progress? This affects only the debug assignment.",
  );
  if (!confirmed) return;
  el.clearDebugProgressButton.disabled = true;
  el.clearDebugProgressButton.textContent = "Clearing";
  try {
    await fetchJson("/api/debug/reset-progress", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        annotator_id: state.annotatorId,
        assignment_token: state.assignmentToken,
      }),
    });
    [state.localKey, state.instructionsKey, state.tutorialKey]
      .filter(Boolean)
      .forEach((key) => localStorage.removeItem(key));
    window.location.replace("/annotate?annotator_id=0");
  } catch (error) {
    el.clearDebugProgressButton.disabled = false;
    el.clearDebugProgressButton.textContent = "Clear debug progress";
    showError(error);
  }
}

function renderAll() {
  renderMode();
  if (state.mode === "review") {
    renderReview();
    return;
  }
  if (!["tutorial", "assignment"].includes(state.mode)) return;
  renderProgress();
  renderUnitList();
  renderCurrentUnit();
}

function renderMode() {
  el.instructionsView.hidden = state.mode !== "instructions";
  el.annotationLayout.hidden = !["tutorial", "assignment"].includes(state.mode);
  el.reviewView.hidden = state.mode !== "review";
  el.submittedView.hidden = state.mode !== "submitted";
  const labels = {
    instructions: "Study instructions",
    tutorial: "Practice tutorial",
    assignment: "Research assignment",
    review: "Final review",
    submitted: "Submitted",
  };
  el.modeBadge.textContent = labels[state.mode] || "Loading";
  el.modeBadge.classList.toggle("tutorial", ["instructions", "tutorial"].includes(state.mode));
  el.assignmentMeta.textContent = `${state.annotatorKey}${state.debug ? " / debug" : ""}`;
  if (state.mode === "submitted") {
    el.submittedMessage.textContent = state.submittedAt
      ? `Submitted at ${new Date(state.submittedAt).toLocaleString()}.`
      : "Submission recorded.";
  }
  if (!["tutorial", "assignment"].includes(state.mode)) return;
  const tutorialMode = state.mode === "tutorial";
  el.tutorialCoach.hidden = !tutorialMode;
  el.saveButton.hidden = tutorialMode;
  el.prevButton.hidden = tutorialMode;
  el.nextButton.hidden = tutorialMode;
  el.allUnitsButton.parentElement.hidden = tutorialMode;
  el.reviewButton.parentElement.hidden = tutorialMode;
  el.assignmentMeta.textContent = tutorialMode
    ? `${state.annotatorKey}${state.debug ? " / debug" : ""} / ${state.tutorialItems.length} practice answers / not recorded`
    : `${state.annotatorKey}${state.debug ? " / debug" : ""} / ${state.rows.length} research answers`;
  if (tutorialMode) renderTutorialCoach();
}

function renderProgress() {
  if (state.mode === "tutorial") {
    const completed = state.tutorialItems.filter(tutorialItemPassed).length;
    const total = state.tutorialItems.length;
    el.progressText.textContent = `Practice ${state.tutorialIndex + 1} / ${total}`;
    el.progressBar.style.width = `${((completed + tutorialItemProgress(currentTutorialItem())) / Math.max(total, 1)) * 100}%`;
    return;
  }
  const completed = state.rows.filter(annotationComplete).length;
  const total = state.rows.length;
  el.progressText.textContent = `${completed} / ${total}`;
  el.progressBar.style.width = total ? `${(completed / total) * 100}%` : "0";
}

function renderUnitList() {
  if (state.mode === "tutorial") {
    el.unitList.innerHTML = `<div class="tutorial-step-list">
      ${state.tutorialItems.map((item, index) => tutorialStep(index, item)).join("")}
      <div class="tutorial-lock-note">
        <strong>${state.rows.length} research answers</strong>
        <span>Unlocked after every practice answer is correct</span>
      </div>
    </div>`;
    return;
  }
  const rows = state.showOpenOnly ? state.rows.filter((row) => !annotationComplete(row)) : state.rows;
  if (!rows.length) {
    el.unitList.innerHTML = `<div class="empty">No open annotations</div>`;
    return;
  }
  el.unitList.innerHTML = rows.map((row) => {
    const index = state.rows.indexOf(row);
    const complete = annotationComplete(row);
    return `<button type="button" class="unit-button ${complete ? "complete" : "open"} ${index === state.currentIndex ? "current" : ""}" data-index="${index}">
      <span class="unit-topline">
        <strong>${index + 1}</strong>
        <span><span class="unit-progress">${annotationProgress(row)}</span><span class="status-dot"></span></span>
      </span>
      <span class="unit-id">${row.annotation.workflow_stage === "evidence_review" ? "Evidence review" : complete ? "Complete" : "Answer review"}</span>
    </button>`;
  }).join("");
    el.unitList.querySelectorAll("button").forEach((button) => {
      button.addEventListener("click", () => {
        state.currentIndex = Number(button.dataset.index);
        renderAll();
        focusWorkspaceSection("#questionPanel");
      });
    });
}

function tutorialStep(index, item) {
  const complete = tutorialItemPassed(item);
  const current = index === state.tutorialIndex;
  const locked = index > 0 && !state.tutorialItems.slice(0, index).every(tutorialItemPassed);
  const status = complete ? "Complete" : locked ? "Locked" : current ? "In progress" : "Ready";
  return `<div class="tutorial-step ${current ? "current" : ""} ${complete ? "complete" : ""} ${locked ? "locked" : ""}">
    <span class="tutorial-step-number">${complete ? "OK" : index + 1}</span>
    <span><strong>${escapeHtml(item.title || `Practice ${index + 1}`)}</strong><small>${status}</small></span>
  </div>`;
}

function renderCurrentUnit() {
  const row = currentRow();
  if (!row) return;
  const tutorialMode = state.mode === "tutorial";
  const index = tutorialMode ? state.tutorialIndex : state.currentIndex;
  const total = tutorialMode ? state.tutorialItems.length : state.rows.length;
  el.unitTitle.innerHTML = `${tutorialMode ? escapeHtml(row.title) : `Answer ${index + 1} of ${total}`} <span class="completion-chip">${annotationProgress(row)}</span>`;
  el.unitSubtitle.innerHTML = [
    tutorialMode ? metaChip("practice - excluded from results") : "",
    metaChip(`${(row.displayed_evidence || []).length} evidence`),
    metaChip(`${(row.graph_paths || []).length} graph paths`),
    metaChip(`${applicableFields(row).length} judgments`),
  ].join("");
  el.questionText.textContent = row.question || "";
  renderQuestionScope(row.question || "");
  el.answerText.textContent = row.answer_text || "";
  el.contextNote.hidden = !row.context_note;
  el.contextNote.textContent = row.context_note || "";
  el.questionReminder.textContent = row.question || "";
  el.answerReminder.textContent = row.answer_text || "";
  const referenceRevealed = row.annotation.workflow_stage === "answer_review";
  el.referencePanel.hidden = !referenceRevealed;
  el.referenceText.textContent = referenceRevealed ? row.reference_answer || "" : "";
  el.referenceReminderBlock.hidden = !referenceRevealed;
  el.referenceReminder.textContent = referenceRevealed ? row.reference_answer || "" : "";
  renderStage(row);
  renderEvidence(
    row.displayed_evidence || [],
    row.cited_evidence_ids || (row.cited_evidence || []).map((item) => item.evidence_id),
    row.unresolved_citation_ids || [],
    applicableFields(row),
  );
  renderGraphPaths(row.graph_paths || []);
  renderLockedJudgments(row);
  renderLabels(row);
  renderIssueAndComment(row);
  renderStageAction(row);
  if (!tutorialMode) {
    el.prevButton.disabled = state.currentIndex <= 0;
    el.nextButton.disabled = state.currentIndex >= state.rows.length - 1;
  }
}

function renderQuestionScope(question) {
  const lowered = String(question || "").toLowerCase();
  let note = "";
  if (lowered.includes("wikidata snapshot")) {
    note = (
      "Snapshot scope: judge what the dated Wikidata snapshot records. "
      + "Do not interpret the question as asking what is true today."
    );
  } else if (lowered.includes("document revision")) {
    note = (
      "Revision scope: judge what the named dated revision states. "
      + "The revision date is not proof that the statement was world-valid on that date."
    );
  }
  el.questionScopeNote.hidden = !note;
  el.questionScopeNote.textContent = note;
}

function renderStage(row) {
  const evidenceStage = row.annotation.workflow_stage === "evidence_review";
  const evidenceFields = fieldsForStage(row, "evidence_review");
  el.stageNumber.textContent = evidenceStage ? "1" : "2";
  el.stageTitle.textContent = evidenceStage ? "Independent evidence review" : "Reference-based answer review";
  el.stageDescription.textContent = evidenceStage
    ? evidenceFields.length
      ? "Judge each visible support component independently. The reference is withheld so it cannot influence these judgments."
      : "No evidence-stage field applies to this card. Review the intentionally absent support, then reveal the reference."
    : "The evidence judgments are locked. Use the newly revealed reference to judge answer correctness and the answer/refusal decision.";
  el.stageStatus.textContent = evidenceStage ? "Reference withheld" : "Reference revealed";
}

function renderLockedJudgments(row) {
  const locked = row.annotation.workflow_stage === "answer_review";
  el.lockedJudgments.hidden = !locked;
  if (!locked) return;
  const fields = fieldsForStage(row, "evidence_review");
  el.lockedJudgments.innerHTML = fields.map((field) => `<div class="locked-judgment">
    <span>${escapeHtml(fieldSpec(field).label)}</span>
    <strong>${escapeHtml(optionLabel(row.annotation.labels[field]))}</strong>
  </div>`).join("");
}

function renderLabels(row) {
  const fields = fieldsForStage(row, row.annotation.workflow_stage);
  const tutorial = state.mode === "tutorial";
  const status = tutorial ? tutorialStatus(currentTutorialItem()) : null;
  const guided = tutorial && row.mode === "guided";
  const guidedField = guided ? guidedFieldFor(row, status) : "";
  const checkedCurrentStage = status?.last_checked_stage === row.annotation.workflow_stage;
  const independentStagePassed = tutorial
    && row.mode === "independent"
    && tutorialStagePassed(status, row.annotation.workflow_stage);
  el.labelControls.innerHTML = fields.map((field) => {
    const spec = fieldSpec(field);
    const currentValue = row.annotation.labels[field] || "";
    const locked = (guided && field !== guidedField) || independentStagePassed;
    const mismatched = Boolean(
      checkedCurrentStage && status?.mismatches?.includes(field),
    );
    const feedback = mismatched ? row.feedback?.[field] || "Review this judgment." : "";
    const guidedHelp = guided && field === guidedField ? row.guidance?.[field] || null : null;
    const reasonOptions = spec.reasons?.[currentValue] || [];
    return `<div class="field-group ${currentValue ? "filled" : "missing"} ${field === guidedField ? "tutorial-focus" : ""} ${locked ? "tutorial-locked" : ""} ${mismatched ? "tutorial-incorrect" : ""}" data-field-group="${field}" ${mismatched ? 'aria-invalid="true"' : ""}>
      <div class="field-heading"><label>${escapeHtml(spec.label)}</label><span>${currentValue ? escapeHtml(optionLabel(currentValue)) : "missing"}</span></div>
      <p class="field-help">${escapeHtml(spec.prompt)}</p>
      <p class="field-scope-note">${escapeHtml(spec.scope_note || "")}</p>
      ${guidedHelp ? `<div class="field-guidance"><strong>Practice guidance</strong><span>${escapeHtml(guidedHelp.instruction || "")}</span><small>${escapeHtml(guidedHelp.reason || "")}</small></div>` : ""}
      <div class="segmented" data-field="${field}">
        ${state.protocol.visible_label_options.map((option) => `<button type="button" class="${option} ${currentValue === option ? "active" : ""}" data-value="${option}" ${locked ? "disabled" : ""}>${escapeHtml(optionLabel(option))}</button>`).join("")}
      </div>
      ${reasonOptions.length ? `<div class="reason-block">
        <label for="reason-${field}">Why is this ${escapeHtml(optionLabel(currentValue).toLowerCase())}?</label>
        <select id="reason-${field}" data-reason-field="${field}" ${locked ? "disabled" : ""}>
          <option value="">Select one reason</option>
          ${reasonOptions.map((reason) => `<option value="${escapeHtml(reason.code)}" ${row.annotation.reasons[field] === reason.code ? "selected" : ""}>${escapeHtml(reason.label)}</option>`).join("")}
        </select>
      </div>` : ""}
      ${feedback ? `<div class="field-feedback" role="alert"><strong>Why this needs another look</strong><span>${escapeHtml(feedback)}</span></div>` : ""}
      <details class="field-rules"><summary>Decision rules</summary><dl>
        ${state.protocol.visible_label_options.map((option) => `<dt>${escapeHtml(optionLabel(option))}</dt><dd>${escapeHtml(spec.definitions[option] || "")}</dd>`).join("")}
      </dl>
        ${spec.decision_notes?.length ? `<strong class="field-rule-notes-title">Apply these rules</strong><ul class="field-rule-notes">
          ${spec.decision_notes.map((note) => `<li>${escapeHtml(note)}</li>`).join("")}
        </ul>` : ""}
      </details>
    </div>`;
  }).join("");

  el.labelControls.querySelectorAll(".segmented button").forEach((button) => {
    button.addEventListener("click", () => handleLabelChoice(button.parentElement.dataset.field, button.dataset.value));
  });
  el.labelControls.querySelectorAll("select[data-reason-field]").forEach((select) => {
    select.addEventListener("change", () => handleReasonChoice(select.dataset.reasonField, select.value));
  });
  const conflicts = logicalConflicts(row);
  el.logicalConflict.hidden = !conflicts.length;
  el.logicalConflict.textContent = conflicts.map((conflict) => conflict.message).join(" ");
}

function renderIssueAndComment(row) {
  const tutorial = state.mode === "tutorial";
  el.issueBlock.hidden = tutorial;
  el.commentBlock.hidden = tutorial;
  if (tutorial) return;
  el.issueSelect.innerHTML = `<option value="">No problem to report</option>${state.protocol.issue_options.map((option) => `<option value="${escapeHtml(option.code)}" ${row.annotation.issue_code === option.code ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}`;
  const selectedIssue = state.protocol.issue_options.find((option) => option.code === row.annotation.issue_code);
  el.issueDetail.hidden = !selectedIssue;
  el.issueDetail.placeholder = selectedIssue?.requires_detail
    ? "Describe the other data or display problem"
    : "Optional detail about the reported problem";
  el.issueDetail.value = row.annotation.issue_detail || "";
  el.commentInput.value = row.annotation.comment || "";
}

function renderStageAction(row) {
  const tutorial = state.mode === "tutorial";
  el.revealReferenceButton.hidden = tutorial;
  if (tutorial) {
    el.stageActionHelp.textContent = el.tutorialActionButton.hidden
      ? "Complete the highlighted practice field or every visible judgment. The checkpoint control appears here when ready."
      : "Use this checkpoint after reviewing the judgments immediately above.";
    return;
  }
  el.tutorialActionButton.hidden = true;
  const evidenceStage = row.annotation.workflow_stage === "evidence_review";
  el.revealReferenceButton.hidden = !evidenceStage;
  if (!evidenceStage) {
    el.stageActionHelp.textContent = annotationComplete(row)
      ? "This annotation is complete."
      : "Complete the answer-stage judgments and any required reasons.";
    return;
  }
  const complete = stageComplete(row, "evidence_review");
  const evidenceFields = fieldsForStage(row, "evidence_review");
  el.revealReferenceButton.textContent = evidenceFields.length
    ? "Lock evidence judgments and reveal reference"
    : "Reveal reference answer";
  el.revealReferenceButton.disabled = !complete;
  el.stageActionHelp.textContent = complete
    ? evidenceFields.length
      ? "These judgments cannot be changed after the reference is shown."
      : "No evidence-stage judgment applies; continue to the reference-based fields."
    : "Complete every visible judgment and required reason first.";
}

function renderTutorialCoach() {
  const item = currentTutorialItem();
  if (!item) return;
  const status = tutorialStatus(item);
  const guided = item.mode === "guided";
  const stage = item.annotation.workflow_stage;
  const field = guided ? guidedFieldFor(item, status) : "";
  el.tutorialStage.textContent = `Practice ${state.tutorialIndex + 1} of ${state.tutorialItems.length}`;
  el.tutorialFeedback.className = "tutorial-feedback";
  el.tutorialFeedback.textContent = "";
  el.tutorialActionButton.hidden = true;
  el.tutorialActionButton.disabled = false;

  if (tutorialItemPassed(item)) {
    el.tutorialCoachTitle.textContent = "Practice answer complete";
    el.tutorialCoachInstruction.textContent = "Your labels match the calibration key.";
    el.tutorialCoachReason.textContent = state.tutorialIndex + 1 === state.tutorialItems.length
      ? "All practice answers are complete. You can now start the research assignment."
      : "Continue to the next practice answer.";
    el.tutorialFeedback.classList.add("success");
    el.tutorialFeedback.textContent = "Practice labels are not part of the research results.";
    el.tutorialActionButton.textContent = state.tutorialIndex + 1 === state.tutorialItems.length
      ? `Start ${state.rows.length} research answers`
      : "Continue to next practice answer";
    el.tutorialActionButton.hidden = false;
    return;
  }

  if (guided && field) {
    const guide = item.guidance?.[field] || {};
    const expected = item.expected_labels?.[field] || "";
    const expectedReason = item.expected_reasons?.[field] || "";
    const labelSelected = item.annotation.labels[field] === expected;
    el.tutorialCoachTitle.textContent = `${fieldSpec(field).label}`;
    el.tutorialCoachInstruction.textContent = expectedReason && labelSelected
      ? `Now select the required reason: ${reasonOptionLabel(field, expected, expectedReason)}.`
      : `${guide.instruction || fieldSpec(field).prompt} Select ${optionLabel(expected)}.`;
    el.tutorialCoachReason.textContent = expectedReason && labelSelected
      ? "Partial and Cannot judge always require a reason. The reason records why that label applies."
      : guide.reason || "";
    if (state.tutorialMessage) {
      el.tutorialFeedback.classList.add("error");
      el.tutorialFeedback.textContent = state.tutorialMessage;
    }
    return;
  }

  if (guided && stage === "evidence_review" && stageComplete(item, "evidence_review")) {
    const hasEvidenceFields = fieldsForStage(item, "evidence_review").length > 0;
    el.tutorialCoachTitle.textContent = hasEvidenceFields
      ? "Evidence review complete"
      : "No evidence-stage judgments apply";
    el.tutorialCoachInstruction.textContent = hasEvidenceFields
      ? "Lock these judgments before seeing the reference answer."
      : "The empty evidence and graph sections are intentional. Continue to the reference.";
    el.tutorialCoachReason.textContent = hasEvidenceFields
      ? "This separation prevents the reference from influencing evidence, citation, time, or path judgments."
      : "A missing component is not automatically an error. Its judgment field is omitted when it is not applicable.";
    el.tutorialActionButton.textContent = hasEvidenceFields
      ? "Lock judgments and reveal reference"
      : "Reveal reference and continue";
    el.tutorialActionButton.hidden = false;
    return;
  }

  if (guided && stage === "answer_review" && !field) {
    markTutorialPassed(item);
    renderAll();
    return;
  }

  const evidenceStage = stage === "evidence_review";
  const stagePassed = tutorialStagePassed(status, stage);
  const checkedCurrentStage = status.last_checked_stage === stage;
  const ready = stageComplete(item, stage);
  el.tutorialCoachTitle.textContent = evidenceStage
    ? "Evidence-stage practice checkpoint"
    : "Answer-stage practice checkpoint";
  el.tutorialCoachInstruction.textContent = evidenceStage
    ? "Complete the evidence, time, citation, and path judgments, then check this stage before the reference is revealed."
    : "Use the revealed reference to complete the answer judgments, then check this stage.";
  el.tutorialCoachReason.textContent = "If a judgment is wrong, its explanation appears inside that field and the control stays editable.";

  if (checkedCurrentStage && status.mismatches.length) {
    el.tutorialFeedback.classList.add("error");
    el.tutorialFeedback.textContent = `${status.mismatches.length} ${status.mismatches.length === 1 ? "judgment needs" : "judgments need"} another look. Correct the highlighted ${status.mismatches.length === 1 ? "field" : "fields"}, then check this stage again.`;
  } else if (checkedCurrentStage && status.needs_recheck) {
    el.tutorialFeedback.textContent = "Correction recorded. Check this stage again to confirm it.";
  } else if (stagePassed) {
    el.tutorialFeedback.classList.add("success");
    el.tutorialFeedback.textContent = evidenceStage
      ? "The evidence-stage judgments are correct. They will be locked when you reveal the reference."
      : "The answer-stage judgments are correct.";
  }

  el.tutorialActionButton.textContent = stagePassed && evidenceStage
    ? "Lock correct judgments and reveal reference"
    : checkedCurrentStage
      ? "Check corrected judgments"
      : evidenceStage
        ? "Check evidence judgments"
        : "Check answer judgments";
  el.tutorialActionButton.disabled = !ready && !stagePassed;
  el.tutorialActionButton.hidden = false;
}

function handleTutorialAction() {
  const item = currentTutorialItem();
  if (!item) return;
  if (tutorialItemPassed(item)) {
    if (state.tutorialIndex + 1 < state.tutorialItems.length) {
      state.tutorialIndex += 1;
    } else {
      state.tutorialCompleted = true;
      state.mode = "assignment";
      state.currentIndex = state.rows.findIndex((row) => !annotationComplete(row));
      if (state.currentIndex < 0) state.currentIndex = 0;
      setSaveState("Research assignment ready");
      reportActivity("tutorial_completed", { tutorial_summary: tutorialSummary() }).catch(() => {
        setSaveState("Research ready; progress signal will retry on reload");
      });
    }
    persistTutorialLocal();
    renderAll();
    focusWorkspaceSection("#questionPanel");
    return;
  }

  const status = tutorialStatus(item);
  const stage = item.annotation.workflow_stage;
  if (
    stage === "evidence_review"
    && stageComplete(item, "evidence_review")
    && (item.mode === "guided" || tutorialStagePassed(status, stage))
  ) {
    item.annotation.workflow_stage = "answer_review";
    status.guided_field_index = 0;
    status.checked = false;
    status.mismatches = [];
    status.needs_recheck = false;
    persistTutorialLocal();
    renderAll();
    focusWorkspaceSection("#referencePanel");
    return;
  }

  if (item.mode === "independent" && stageComplete(item, stage)) {
    checkTutorialStage(item, stage);
    persistTutorialLocal();
    renderAll();
    if (tutorialStatus(item).mismatches.length) focusFirstTutorialMismatch();
  }
}

function handleStageAction() {
  const row = currentRow();
  if (!row || row.annotation.workflow_stage !== "evidence_review") return;
  if (state.mode === "tutorial") {
    handleTutorialAction();
    return;
  }
  revealReference(row).catch(showSaveError);
}

function handleLabelChoice(field, value) {
  const row = currentRow();
  if (!row) return;
  if (state.mode === "tutorial" && row.mode === "guided") {
    const status = tutorialStatus(row);
    const expectedField = guidedFieldFor(row, status);
    const expectedValue = row.expected_labels?.[field];
    if (field !== expectedField) return;
    if (value !== expectedValue) {
      state.tutorialMessage = `For this guided step, select ${optionLabel(expectedValue)} and use the explanation above to verify why.`;
      persistTutorialLocal();
      renderAll();
      return;
    }
    state.tutorialMessage = "";
  }
  row.annotation.labels[field] = value;
  delete row.annotation.reasons[field];
  if (state.mode === "tutorial") {
    const status = tutorialStatus(row);
    if (row.mode === "guided") {
      status.checked = false;
      status.mismatches = [];
      const expectedReason = row.expected_reasons?.[field] || "";
      if (!expectedReason) status.guided_field_index += 1;
    } else {
      invalidateTutorialStage(status, row.annotation.workflow_stage, field);
    }
    persistTutorialLocal();
    renderAll();
    return;
  }
  annotationChanged();
}

function handleReasonChoice(field, value) {
  const row = currentRow();
  if (!row) return;
  if (state.mode === "tutorial") {
    const status = tutorialStatus(row);
    if (row.mode === "guided") {
      const expectedField = guidedFieldFor(row, status);
      const expectedReason = row.expected_reasons?.[field] || "";
      if (field !== expectedField || !expectedReason) return;
      if (value !== expectedReason) {
        state.tutorialMessage = (
          `For this guided step, select "${reasonOptionLabel(
            field,
            row.expected_labels?.[field] || "",
            expectedReason,
          )}".`
        );
        persistTutorialLocal();
        renderAll();
        return;
      }
      row.annotation.reasons[field] = value;
      state.tutorialMessage = "";
      status.checked = false;
      status.mismatches = [];
      status.guided_field_index += 1;
    } else {
      if (value) row.annotation.reasons[field] = value;
      else delete row.annotation.reasons[field];
      invalidateTutorialStage(status, row.annotation.workflow_stage, field);
    }
    persistTutorialLocal();
    renderAll();
    return;
  }
  if (value) row.annotation.reasons[field] = value;
  else delete row.annotation.reasons[field];
  annotationChanged();
}

function guidedFieldFor(item, status) {
  const fields = fieldsForStage(item, item.annotation.workflow_stage);
  return fields[status.guided_field_index] || "";
}

function checkTutorialStage(item, stage) {
  const status = tutorialStatus(item);
  const fields = fieldsForStage(item, stage);
  const mismatches = fields.filter((field) => {
    if (item.annotation.labels[field] !== item.expected_labels?.[field]) return true;
    const expectedReason = item.expected_reasons?.[field] || "";
    return expectedReason && item.annotation.reasons[field] !== expectedReason;
  });
  status.attempts += 1;
  if (mismatches.length) status.failed_attempts += 1;
  status.checked = true;
  status.last_checked_stage = stage;
  status.mismatches = mismatches;
  status.needs_recheck = false;
  setTutorialStagePassed(status, stage, mismatches.length === 0);
  status.passed = status.evidence_review_passed && status.answer_review_passed;
}

function markTutorialPassed(item) {
  const status = tutorialStatus(item);
  status.checked = true;
  status.mismatches = [];
  status.evidence_review_passed = true;
  status.answer_review_passed = true;
  status.passed = true;
}

function invalidateTutorialStage(status, stage, field) {
  setTutorialStagePassed(status, stage, false);
  status.passed = false;
  if (status.last_checked_stage !== stage) return;
  status.mismatches = status.mismatches.filter((item) => item !== field);
  status.needs_recheck = true;
}

function tutorialStagePassed(status, stage) {
  return Boolean(status?.[`${stage}_passed`]);
}

function setTutorialStagePassed(status, stage, passed) {
  status[`${stage}_passed`] = passed;
}

function tutorialStatus(item) {
  if (!item) return {
    guided_field_index: 0,
    checked: false,
    passed: false,
    mismatches: [],
    last_checked_stage: "",
    needs_recheck: false,
    evidence_review_passed: false,
    answer_review_passed: false,
    attempts: 0,
    failed_attempts: 0,
  };
  if (!state.tutorialStatus[item.tutorial_id]) {
    state.tutorialStatus[item.tutorial_id] = {
      guided_field_index: 0,
      checked: false,
      passed: false,
      mismatches: [],
      last_checked_stage: "",
      needs_recheck: false,
      evidence_review_passed: false,
      answer_review_passed: false,
      attempts: 0,
      failed_attempts: 0,
    };
  }
  const status = state.tutorialStatus[item.tutorial_id];
  status.attempts = Number.isInteger(status.attempts) ? status.attempts : 0;
  status.failed_attempts = Number.isInteger(status.failed_attempts) ? status.failed_attempts : 0;
  status.mismatches = Array.isArray(status.mismatches) ? status.mismatches : [];
  status.last_checked_stage = String(status.last_checked_stage || "");
  status.needs_recheck = Boolean(status.needs_recheck);
  status.evidence_review_passed = Boolean(status.evidence_review_passed);
  status.answer_review_passed = Boolean(status.answer_review_passed);
  return status;
}

function tutorialSummary() {
  const items = state.tutorialItems
    .filter((item) => item.mode === "independent")
    .map((item) => {
      const status = tutorialStatus(item);
      const attempts = Math.max(1, status.attempts);
      const failedAttempts = Math.max(0, Math.min(status.failed_attempts, attempts - 1));
      return [item.tutorial_id, { attempts, failed_attempts: failedAttempts }];
    });
  return { version: state.tutorialVersion, items: Object.fromEntries(items) };
}

function tutorialItemPassed(item) {
  return Boolean(tutorialStatus(item).passed);
}

function tutorialItemProgress(item) {
  if (!item) return 0;
  if (tutorialItemPassed(item)) return 1;
  const applicable = applicableFields(item);
  const complete = applicable.filter((field) => fieldComplete(item, field)).length;
  return complete / Math.max(applicable.length, 1);
}

function openReview() {
  if (state.mode !== "assignment") return;
  persistLocal();
  state.mode = "review";
  renderAll();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function closeReview() {
  state.mode = "assignment";
  renderAll();
  focusWorkspaceSection("#judgmentsPanel");
}

function renderReview() {
  const problems = [];
  el.reviewSummary.innerHTML = state.rows.map((row, index) => {
    const conflicts = logicalConflicts(row);
    const complete = annotationComplete(row);
    let detail = "Ready to submit";
    if (!complete) detail = incompleteSummary(row);
    if (conflicts.length) detail = conflicts.map((item) => item.message).join(" ");
    if (!complete || conflicts.length) problems.push(index);
    return `<button type="button" class="review-row ${complete && !conflicts.length ? "ready" : "problem"}" data-index="${index}">
      <span class="review-row-number">${index + 1}</span>
      <span><strong>Answer ${index + 1}</strong><small>${escapeHtml(detail)}</small></span>
      <span>${complete && !conflicts.length ? "Ready" : "Review"}</span>
    </button>`;
  }).join("");
  el.reviewSummary.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      state.currentIndex = Number(button.dataset.index);
      closeReview();
    });
  });
  el.submitButton.disabled = problems.length > 0;
  el.submitReadiness.textContent = problems.length
    ? `${problems.length} ${problems.length === 1 ? "annotation requires" : "annotations require"} review before submission.`
    : `All ${state.rows.length} annotations are complete and logically consistent.`;
}

async function submitAssignment() {
  if (!state.rows.every(annotationComplete)) {
    renderReview();
    return;
  }
  el.submitButton.disabled = true;
  el.submitReadiness.textContent = "Submitting final annotations";
  await saveNow();
  const response = await fetchJson("/api/submit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      annotator_id: state.annotatorId,
      assignment_token: state.assignmentToken,
      revision: state.revision,
    }),
  });
  state.submittedAt = response.submitted_at || new Date().toISOString();
  state.mode = "submitted";
  localStorage.removeItem(state.localKey);
  renderAll();
}

function renderEvidence(evidenceRows, citedIds, unresolvedIds, fields) {
  const cited = new Set(citedIds || []);
  if (!evidenceRows.length && !unresolvedIds.length) {
    const evidenceFieldVisible = fields.includes("evidence_supports_answer")
      || fields.includes("citation_temporally_valid");
    el.evidenceList.innerHTML = evidenceFieldVisible
      ? `<div class="empty empty-explained"><strong>No readable evidence is supplied</strong><span>An evidence judgment still applies on this card. Use Cannot judge when the missing material prevents a decision.</span></div>`
      : `<div class="empty empty-explained"><strong>No evidence is supplied for this candidate</strong><span>This is intentional. Evidence support and citation-time fields are omitted; continue to the reference-based answer judgment.</span></div>`;
    return;
  }
  const evidenceHtml = evidenceRows.map((evidence) => `<article class="evidence-card">
    <p>${escapeHtml(evidence.text || "")}</p>
    <div class="meta-row">
      ${badge(evidence.evidence_id)}
      ${badge(cited.has(evidence.evidence_id) ? "cited" : "retrieved")}
      ${badge(evidence.publication_time ? `published ${evidence.publication_time}` : "")}
      ${badge(evidence.valid_time ? `valid ${formatValidTime(evidence.valid_time)}` : "")}
    </div>
  </article>`).join("");
  const unresolvedHtml = unresolvedIds.length
    ? `<article class="evidence-card unresolved-citation">
        <p>Unresolved citation identifiers</p>
        <div class="meta-row">${unresolvedIds.map((value) => badge(value)).join("")}</div>
      </article>`
    : "";
  el.evidenceList.innerHTML = evidenceHtml + unresolvedHtml;
}

function renderGraphPaths(paths) {
  if (!paths.length) {
    el.graphPathList.innerHTML = `<div class="empty">No graph evidence path is supplied. The graph-evidence judgment is automatically not applicable and is not shown.</div>`;
    return;
  }
  const multiplePaths = paths.length > 1;
  const setNote = multiplePaths
    ? `<div class="path-set-note">
        <strong>Judge these ${paths.length} paths together as one evidence set.</strong>
        A sufficient set stays sufficient when it also contains an irrelevant extra path.
      </div>`
    : `<div class="path-set-note">
        <strong>Judge this path as graph evidence for the candidate answer.</strong>
      </div>`;
  const pathCards = paths.map((path, index) => {
    const edgeCount = normalizeGraphPath(path).edges.length;
    return `<article class="path-card">
      <div class="path-summary">
        ${badge(`Path ${index + 1} of ${paths.length}`)}
        ${badge(`${edgeCount} ${edgeCount === 1 ? "edge" : "edges"}`)}
      </div>
      ${graphPathSvg(path)}
      <details class="path-evidence-details" ${multiplePaths ? "" : "open"}>
        <summary>Graph edge details</summary>
        <div class="path-evidence-list">${graphPathEvidence(path)}</div>
      </details>
    </article>`;
  }).join("");
  el.graphPathList.innerHTML = `${setNote}<div class="path-grid">${pathCards}</div>`;
}

function graphPathSvg(path) {
  const graph = normalizeGraphPath(path);
  if (!graph.nodes.length) return `<div class="empty">No path nodes available</div>`;
  const compact = graph.nodes.length <= 2;
  const width = Math.max(440, graph.nodes.length * 200);
  const height = compact ? 150 : 230;
  const nodeWidth = compact ? 150 : 170;
  const nodeHeight = 52;
  const gap = graph.nodes.length > 1 ? (width - nodeWidth - 60) / (graph.nodes.length - 1) : 0;
  const markerId = `arrow-${String(path.path_id || "path").replaceAll(/[^a-zA-Z0-9_-]/g, "-")}`;
  const positions = new Map(graph.nodes.map((node, index) => [
    node.id,
    { x: 30 + index * gap, y: compact ? 48 : index % 2 === 0 ? 34 : 120 },
  ]));
  const edgeOffsets = edgeLayoutOffsets(graph.edges);
  const edges = graph.edges.map((edge, index) => {
    const source = positions.get(edge.source.id);
    const target = positions.get(edge.target.id);
    if (!source || !target) return "";
    const geometry = edgeGeometry({
      source: { x: source.x + nodeWidth / 2, y: source.y + nodeHeight / 2 },
      target: { x: target.x + nodeWidth / 2, y: target.y + nodeHeight / 2 },
      offset: edgeOffsets.get(index) || 0,
      halfWidth: nodeWidth / 2,
      halfHeight: nodeHeight / 2,
    });
    const symmetricMarker = edge.symmetric ? `marker-start="url(#${markerId}-${index})"` : "";
    const relationText = edge.symmetric
      ? `bidirectional: ${edge.relation_label || edge.relation || ""}`
      : edge.relation_label || edge.relation || "";
    const edgeTitle = `${edge.source.label} ${edge.symmetric ? "<->" : "->"} ${edge.target.label}; ${relationText}; ${formatValidTime(edge.valid_time)}`;
    const labelX = compact ? width / 2 : geometry.labelX;
    const labelY = compact ? 116 : geometry.labelY;
    return `<g class="graph-edge" tabindex="0" role="group" aria-label="${escapeHtml(edgeTitle)}">
      <title>${escapeSvg(edgeTitle)}</title>
      <path class="edge-line" d="${geometry.path}" ${symmetricMarker} marker-end="url(#${markerId}-${index})"></path>
      <text x="${labelX}" y="${labelY}">${escapeSvg(shorten(relationText, 42))}</text>
      <text class="edge-time" x="${labelX}" y="${labelY + 16}">${escapeSvg(shorten(formatValidTime(edge.valid_time), 28))}</text>
    </g>`;
  }).join("");
  const nodes = graph.nodes.map((node) => {
    const position = positions.get(node.id);
    const nodeType = humanizeIdentifier(node.type || node.id);
    const nodeTitle = `${node.label || node.id} (${nodeType})`;
    return `<g class="graph-node" transform="translate(${position.x}, ${position.y})" tabindex="0" role="group" aria-label="${escapeHtml(nodeTitle)}">
      <title>${escapeSvg(nodeTitle)}</title>
      <rect width="${nodeWidth}" height="${nodeHeight}" rx="6"></rect>
      <text x="10" y="22">${escapeSvg(shorten(node.label || node.id, compact ? 19 : 22))}</text>
      <text class="node-type" x="10" y="40">${escapeSvg(shorten(nodeType, compact ? 21 : 24))}</text>
    </g>`;
  }).join("");
  return `<svg class="graph-path-svg" viewBox="0 0 ${width} ${height}" role="img" aria-label="Candidate graph path">
    <defs>${graph.edges.map((_edge, index) => `<marker id="${markerId}-${index}" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto-start-reverse">
      <path d="M0,0 L0,6 L7,3 z"></path>
    </marker>`).join("")}</defs>
    ${edges}${nodes}
  </svg>`;
}

function edgeLayoutOffsets(edges) {
  const groups = new Map();
  edges.forEach((edge, index) => {
    const endpoints = [String(edge.source?.id || ""), String(edge.target?.id || "")].sort();
    const key = endpoints.join("::");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(index);
  });
  const offsets = new Map();
  groups.forEach((indexes) => {
    indexes.forEach((edgeIndex, groupIndex) => {
      offsets.set(edgeIndex, (groupIndex - (indexes.length - 1) / 2) * 32);
    });
  });
  return offsets;
}

function edgeGeometry({ source, target, offset, halfWidth, halfHeight }) {
  if (source.x === target.x && source.y === target.y) {
    const loopX = source.x + halfWidth + 36;
    const loopY = source.y - halfHeight - 28;
    return {
      path: `M ${source.x + halfWidth} ${source.y} C ${loopX} ${loopY}, ${loopX} ${source.y + halfHeight + 28}, ${source.x + halfWidth} ${source.y + halfHeight}`,
      labelX: loopX,
      labelY: loopY + 20,
    };
  }
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const horizontal = Math.abs(dx) >= Math.abs(dy);
  const direction = horizontal ? Math.sign(dx || 1) : Math.sign(dy || 1);
  const start = {
    x: source.x + (horizontal ? direction * halfWidth : 0),
    y: source.y + (horizontal ? 0 : direction * halfHeight),
  };
  const end = {
    x: target.x - (horizontal ? direction * halfWidth : 0),
    y: target.y - (horizontal ? 0 : direction * halfHeight),
  };
  const lineDx = end.x - start.x;
  const lineDy = end.y - start.y;
  const length = Math.hypot(lineDx, lineDy) || 1;
  const normalX = -lineDy / length;
  const normalY = lineDx / length;
  const control = {
    x: (start.x + end.x) / 2 + normalX * offset,
    y: (start.y + end.y) / 2 + normalY * offset,
  };
  return {
    path: `M ${start.x} ${start.y} Q ${control.x} ${control.y} ${end.x} ${end.y}`,
    labelX: control.x,
    labelY: control.y - 8,
  };
}

function normalizeGraphPath(path) {
  const nodes = [];
  const nodeById = new Map();
  const addNode = (raw) => {
    const node = typeof raw === "object" && raw !== null
      ? { id: String(raw.id || ""), label: String(raw.label || raw.id || ""), type: String(raw.type || "") }
      : { id: String(raw || ""), label: String(raw || ""), type: "" };
    if (node.id && !nodeById.has(node.id)) {
      nodeById.set(node.id, node);
      nodes.push(node);
    }
    return node;
  };
  (path.nodes || []).forEach(addNode);
  const edges = (path.edges || []).map((edge, index) => {
    const fallbackSource = nodes[index] || addNode(`node_${index}`);
    const fallbackTarget = nodes[index + 1] || fallbackSource;
    return {
      ...edge,
      source: edge.source ? addNode(edge.source) : fallbackSource,
      target: edge.target ? addNode(edge.target) : fallbackTarget,
    };
  });
  return { nodes, edges };
}

function graphPathEvidence(path) {
  return normalizeGraphPath(path).edges.map(graphEdgeEvidence).join("");
}

function graphEdgeEvidence(edge) {
  const source = edge.source?.label || edge.source?.id || "";
  const target = edge.target?.label || edge.target?.id || "";
  const relation = edge.symmetric
    ? `Bidirectional: ${edge.relation_label || edge.relation || ""}`
    : edge.relation_label || edge.relation || "";
  return `<div class="path-edge-evidence">
    <div class="path-edge-title">${escapeHtml(source)} ${edge.symmetric ? "<->" : "->"} ${escapeHtml(target)}</div>
    <div class="path-edge-relation">${escapeHtml(relation)}</div>
    <p>${escapeHtml(edge.evidence_text || edge.fact_id || "")}</p>
    <div class="meta-row">${badge(formatValidTime(edge.valid_time))}</div>
  </div>`;
}

function ensureAnnotationStates(rows) {
  rows.forEach((row) => {
    row.annotation = row.annotation || {};
    row.annotation.labels = row.annotation.labels || {};
    row.annotation.reasons = row.annotation.reasons || {};
    row.annotation.issue_code = row.annotation.issue_code || "";
    row.annotation.issue_detail = row.annotation.issue_detail || "";
    row.annotation.comment = row.annotation.comment || "";
    row.annotation.workflow_stage = row.annotation.workflow_stage || "evidence_review";
    const applicable = new Set(applicableFields(row));
    Object.keys(state.protocol.fields).forEach((field) => {
      if (!applicable.has(field)) row.annotation.labels[field] = "not_applicable";
      else if (row.annotation.labels[field] === undefined) row.annotation.labels[field] = "";
    });
  });
}

function applicableFields(row) {
  const declared = Array.isArray(row?.applicable_fields) ? row.applicable_fields : [];
  const known = new Set(Object.keys(state.protocol?.fields || {}));
  const fields = declared.filter((field) => known.has(field));
  return fields.length ? fields : [...known];
}

function fieldsForStage(row, stage) {
  const configured = stage === "evidence_review"
    ? state.protocol.evidence_stage_fields
    : state.protocol.answer_stage_fields;
  const applicable = new Set(applicableFields(row));
  return configured.filter((field) => applicable.has(field));
}

function fieldSpec(field) {
  return state.protocol.fields[field];
}

function reasonOptionLabel(field, label, reasonCode) {
  const options = fieldSpec(field)?.reasons?.[label] || [];
  return options.find((option) => option.code === reasonCode)?.label || reasonCode;
}

function fieldComplete(row, field) {
  const label = row.annotation.labels[field] || "";
  if (!state.protocol.visible_label_options.includes(label)) return false;
  if (["partial", "unjudgeable"].includes(label)) {
    const allowed = fieldSpec(field).reasons?.[label] || [];
    return allowed.some((reason) => reason.code === row.annotation.reasons[field]);
  }
  return true;
}

function stageComplete(row, stage) {
  return fieldsForStage(row, stage).every((field) => fieldComplete(row, field));
}

function annotationComplete(row, options = {}) {
  const issue = state.protocol.issue_options.find((item) => item.code === row.annotation.issue_code);
  const issueComplete = !issue?.requires_detail || Boolean(row.annotation.issue_detail.trim());
  const conflicts = options.ignoreConflicts ? [] : logicalConflicts(row);
  return row.annotation.workflow_stage === "answer_review"
    && stageComplete(row, "evidence_review")
    && stageComplete(row, "answer_review")
    && issueComplete
    && conflicts.length === 0;
}

function annotationProgress(row) {
  const fields = applicableFields(row);
  const completed = fields.filter((field) => fieldComplete(row, field)).length;
  return `${completed}/${fields.length}`;
}

function logicalConflicts(row) {
  const applicable = new Set(applicableFields(row));
  if (
    applicable.has("answer_correct")
    && applicable.has("temporal_correct")
    && row.annotation.labels.answer_correct === "yes"
    && row.annotation.labels.temporal_correct === "no"
  ) {
    return [{
      code: "fully_correct_but_temporally_incorrect",
      message: "Answer correct is Yes while Temporal correct is No. A fully correct answer to this time-specific question cannot also be temporally incorrect.",
    }];
  }
  return [];
}

function incompleteSummary(row) {
  if (row.annotation.workflow_stage === "evidence_review") return "Evidence review is not locked yet";
  const missing = applicableFields(row).filter((field) => !fieldComplete(row, field));
  if (missing.length) return `${missing.length} required judgment or reason ${missing.length === 1 ? "is" : "are"} incomplete`;
  const issue = state.protocol.issue_options.find((item) => item.code === row.annotation.issue_code);
  if (issue?.requires_detail && !row.annotation.issue_detail.trim()) return "The reported Other problem needs a description";
  return "Annotation is incomplete";
}

function annotationChanged() {
  persistLocal();
  scheduleSave();
  renderAll();
}

function updateTextField(field, value) {
  if (state.mode !== "assignment") return;
  const row = currentRow();
  if (!row) return;
  row.annotation[field] = value;
  persistLocal();
  scheduleSave();
  renderProgress();
}

function persistLocal() {
  if (!state.localKey || !state.rows.length || state.submittedAt) return;
  localStorage.setItem(state.localKey, JSON.stringify({
    protocol_version: state.protocol.version,
    saved_at: new Date().toISOString(),
    annotations_by_unit_id: annotationsByUnitId(),
  }));
}

function mergeLocal(payload) {
  const raw = localStorage.getItem(state.localKey);
  if (!raw) return;
  try {
    const local = JSON.parse(raw);
    if (local.protocol_version !== state.protocol.version) return;
    if (payload.output_saved_at && local.saved_at) {
      const serverTime = Date.parse(payload.output_saved_at);
      const localTime = Date.parse(local.saved_at);
      if (!Number.isNaN(serverTime) && !Number.isNaN(localTime) && localTime <= serverTime) return;
    }
    const localAnnotations = local.annotations_by_unit_id || {};
    state.rows.forEach((row) => {
      const localAnnotation = localAnnotations[row.unit_id];
      if (!localAnnotation) return;
      const serverStage = row.annotation.workflow_stage;
      if (serverStage === "evidence_review") {
        row.annotation = structuredClone(localAnnotation);
        row.annotation.workflow_stage = "evidence_review";
        return;
      }
      const merged = structuredClone(row.annotation);
      state.protocol.answer_stage_fields.forEach((field) => {
        if (localAnnotation.labels?.[field] !== undefined) {
          merged.labels[field] = localAnnotation.labels[field];
        }
        if (localAnnotation.reasons?.[field]) merged.reasons[field] = localAnnotation.reasons[field];
        else delete merged.reasons[field];
      });
      merged.issue_code = localAnnotation.issue_code || "";
      merged.issue_detail = localAnnotation.issue_detail || "";
      merged.comment = localAnnotation.comment || "";
      row.annotation = merged;
    });
    setSaveState("Restored local autosave");
  } catch {
    localStorage.removeItem(state.localKey);
  }
}

function persistTutorialLocal() {
  if (!state.tutorialKey) return;
  localStorage.setItem(state.tutorialKey, JSON.stringify({
    version: state.tutorialVersion,
    completed: state.tutorialCompleted,
    current_index: state.tutorialIndex,
    status: state.tutorialStatus,
    annotations_by_tutorial_id: Object.fromEntries(
      state.tutorialItems.map((item) => [item.tutorial_id, item.annotation]),
    ),
  }));
}

function restoreTutorialLocal() {
  const raw = localStorage.getItem(state.tutorialKey);
  if (!raw) return;
  try {
    const local = JSON.parse(raw);
    if (String(local.version || "") !== state.tutorialVersion) return;
    state.tutorialCompleted = Boolean(local.completed);
    state.tutorialIndex = clampIndex(Number(local.current_index || 0), state.tutorialItems.length);
    state.tutorialStatus = local.status || {};
    const annotations = local.annotations_by_tutorial_id || {};
    state.tutorialItems.forEach((item) => {
      if (annotations[item.tutorial_id]) item.annotation = annotations[item.tutorial_id];
    });
  } catch {
    localStorage.removeItem(state.tutorialKey);
  }
}

function annotationsByUnitId() {
  return Object.fromEntries(state.rows.map((row) => [row.unit_id, row.annotation]));
}

function scheduleSave() {
  setSaveState("Autosaving");
  if (state.saveTimer) window.clearTimeout(state.saveTimer);
  state.saveTimer = window.setTimeout(() => saveNow().catch(showSaveError), SERVER_AUTOSAVE_DELAY_MS);
}

async function saveNow(allowSessionRefresh = true) {
  if (!["assignment", "review"].includes(state.mode) || !state.rows.length || state.submittedAt) return;
  if (state.saveTimer) window.clearTimeout(state.saveTimer);
  state.saveTimer = null;
  persistLocal();
  setSaveState("Saving");
  try {
    const response = await fetchJson("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        annotator_id: state.annotatorId,
        assignment_token: state.assignmentToken,
        annotations_by_unit_id: annotationsByUnitId(),
        revision: state.revision,
      }),
    });
    state.outputSavedAt = response.output_saved_at || state.outputSavedAt;
    state.revision = response.revision || state.revision;
    setSaveState(`Saved ${new Date().toLocaleTimeString()}`);
  } catch (error) {
    if (allowSessionRefresh && error.status === 401) {
      await refreshAssignmentToken();
      return saveNow(false);
    }
    throw error;
  }
}

async function revealReference(row, allowSessionRefresh = true) {
  if (!stageComplete(row, "evidence_review")) return;
  if (state.saveTimer) window.clearTimeout(state.saveTimer);
  state.saveTimer = null;
  setSaveState("Locking evidence judgments");
  try {
    const response = await fetchJson("/api/reveal-reference", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        annotator_id: state.annotatorId,
        assignment_token: state.assignmentToken,
        annotations_by_unit_id: annotationsByUnitId(),
        revision: state.revision,
        unit_id: row.unit_id,
      }),
    });
    const index = state.rows.findIndex((candidate) => candidate.unit_id === row.unit_id);
    state.rows[index] = response.revealed_row;
    ensureAnnotationStates([state.rows[index]]);
    state.revision = response.revision || state.revision;
    state.outputSavedAt = response.output_saved_at || state.outputSavedAt;
    persistLocal();
    setSaveState("Evidence judgments locked");
    renderAll();
    focusWorkspaceSection("#referencePanel");
  } catch (error) {
    if (allowSessionRefresh && error.status === 401) {
      await refreshAssignmentToken();
      return revealReference(row, false);
    }
    throw error;
  }
}

async function reportActivity(event, details = {}, allowSessionRefresh = true) {
  try {
    await fetchJson("/api/activity", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        annotator_id: state.annotatorId,
        assignment_token: state.assignmentToken,
        event,
        ...details,
      }),
    });
  } catch (error) {
    if (allowSessionRefresh && error.status === 401) {
      await refreshAssignmentToken();
      return reportActivity(event, details, false);
    }
    throw error;
  }
}

async function refreshAssignmentToken() {
  const payload = await fetchJson(`/api/assignment?annotator_id=${state.annotatorId}`);
  state.assignmentToken = payload.assignment_token || "";
  if (!state.assignmentToken) throw new Error("Could not refresh the assignment session");
}

function currentRow() {
  return state.mode === "tutorial" ? currentTutorialItem() : state.rows[state.currentIndex];
}

function currentTutorialItem() {
  return state.tutorialItems[state.tutorialIndex];
}

function setUnitFilter(openOnly) {
  state.showOpenOnly = openOnly;
  el.allUnitsButton.classList.toggle("active", !openOnly);
  el.openUnitsButton.classList.toggle("active", openOnly);
  renderUnitList();
}

function goToRelative(delta) {
  if (state.mode !== "assignment") return;
  state.currentIndex = Math.max(0, Math.min(state.rows.length - 1, state.currentIndex + delta));
  renderAll();
  focusWorkspaceSection("#questionPanel");
}

function handleKeys(event) {
  if (["TEXTAREA", "INPUT", "SELECT"].includes(event.target.tagName)) return;
  if (state.mode !== "assignment") return;
  if (event.key === "ArrowLeft") goToRelative(-1);
  if (event.key === "ArrowRight") goToRelative(1);
}

function annotatorIdFromParams(params) {
  const rawId = params.get("annotator_id");
  if (rawId === null || rawId.trim() === "") return null;
  const id = Number(rawId);
  return Number.isInteger(id) && id >= 0 ? id : null;
}

function initialUnitIndexFromParams(params) {
  const rawIndex = params.get("unit");
  if (rawIndex === null || rawIndex.trim() === "") return 0;
  const index = Number(rawIndex);
  return Number.isInteger(index) && index >= 0 ? index : 0;
}

function clampIndex(index, total) {
  if (!total) return 0;
  return Math.max(0, Math.min(total - 1, Number.isFinite(index) ? index : 0));
}

function focusInitialSection() {
  if (state.initialFocus !== "graph_paths") return;
  window.setTimeout(() => focusWorkspaceSection("#graphPanel"), 100);
}

function focusWorkspaceSection(selector) {
  const navTarget = sectionForTarget(selector);
  el.reviewNav?.querySelectorAll("button").forEach((button) => {
    button.classList.toggle("active", button.dataset.sectionTarget === navTarget);
  });
  window.requestAnimationFrame(() => {
    const target = document.querySelector(selector);
    if (!target) return;
    target.scrollIntoView({
      block: "start",
      behavior: selector.startsWith("[data-field-group=") ? "smooth" : "auto",
    });
  });
}

function sectionForTarget(selector) {
  if (selector === "#evidencePanel") return "#evidencePanel";
  if (selector === "#graphPanel") return "#graphPanel";
  if (
    selector === "#judgmentsPanel"
    || selector === "#stageBanner"
    || selector === "#tutorialCoach"
    || selector.startsWith("[data-field-group=")
  ) return "#judgmentsPanel";
  return "#questionPanel";
}

function handleWorkspaceBreakpointChange() {
  const activeSection = el.reviewNav?.querySelector("button.active")?.dataset.sectionTarget
    || "#questionPanel";
  window.requestAnimationFrame(() => {
    el.workspaceScroll.scrollTop = 0;
    window.scrollTo({ top: 0, left: 0, behavior: "auto" });
    if (["tutorial", "assignment"].includes(state.mode)) {
      focusWorkspaceSection(activeSection);
    }
  });
}

function focusFirstTutorialMismatch() {
  const field = tutorialStatus(currentTutorialItem()).mismatches[0];
  if (!field) return;
  focusWorkspaceSection(`[data-field-group="${field}"]`);
}

function optionLabel(value) {
  if (value === "unjudgeable") return "Cannot judge";
  if (value === "not_applicable") return "Not applicable";
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : "";
}

function formatValidTime(value) {
  if (!value) return "";
  if (value.type === "unknown") return "unknown time";
  if (value.start && value.end && value.start !== value.end) return `${value.start} to ${value.end}`;
  return value.start || "";
}

function badge(value) {
  return value ? `<span class="badge">${escapeHtml(value)}</span>` : "";
}

function metaChip(value) {
  return value ? `<span class="meta-chip">${escapeHtml(value)}</span>` : "";
}

function shorten(value, maxLength) {
  const text = String(value || "");
  return text.length > maxLength ? `${text.slice(0, maxLength - 3)}...` : text;
}

function humanizeIdentifier(value) {
  return String(value || "").replaceAll("_", " ");
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeSvg(value) {
  return escapeHtml(value);
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json();
  if (!response.ok) {
    const error = new Error(payload.error || response.statusText);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function showSaveError(error) {
  setSaveState(`Save failed: ${error.message}`);
  if (state.mode === "review") {
    el.submitReadiness.textContent = `Save failed: ${error.message}`;
    el.submitButton.disabled = false;
  }
}

function setSaveState(text) {
  el.saveState.textContent = text;
}

function showError(error) {
  state.mode = "instructions";
  el.instructionsView.hidden = false;
  el.annotationLayout.hidden = true;
  el.modeBadge.textContent = "Error";
  el.assignmentMeta.textContent = error.message;
}

init();
