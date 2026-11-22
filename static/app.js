/**
 * LLM Eval Platform — Frontend Controller (Pure Vanilla JS)
 * Vindex 1–6 Standard · LLM-as-a-Judge Evaluation Platform
 */

const state = {
  rubric: null,
  engine: null,
  simulateBias: false,
  compareRatings: {}, // { criterionKey: { a: number|null, b: number|null, evidenceA: string, evidenceB: string } }
  compareResults: null,
  blindHumanRatings: {}, // { criterionKey: { a: number|null, b: number|null } }
  blindRevealedResult: null,
  historyFilter: "all",
  historySearch: "",
  historyItems: [],
};

const $ = (id) => document.getElementById(id);

function escapeHtml(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function showCompareResults(show) {
  const top = $("compare-results-top");
  const bottom = $("compare-results-bottom");
  if (top) top.classList.toggle("hidden", !show);
  if (bottom) bottom.classList.toggle("hidden", !show);
}

async function apiJson(url, options = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || `Request failed (${res.status})`);
  }
  return data;
}

function updateHistoryCount(count) {
  $("history-count-pill").textContent = String(count);
}

// ---------------------------------------------------------------------------
// Tab Navigation
// ---------------------------------------------------------------------------

function setupTabs() {
  document.querySelectorAll(".nav-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.tab;
      document.querySelectorAll(".nav-tab").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.classList.toggle("active", panel.id === `tab-${target}`);
      });
      if (target === "history") {
        loadHistory();
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Tab 1: Compare Two Responses + 2-Pass Swap Test + Vindex 1–6 Rating Boxes
// ---------------------------------------------------------------------------

function initCompareRatings() {
  state.compareRatings = {};
  (state.rubric?.criteria || []).forEach((c) => {
    state.compareRatings[c.key] = { a: null, b: null, evidenceA: "", evidenceB: "" };
  });
}

function renderCompareCriteria() {
  const container = $("criteria-list");
  if (!container || !state.rubric?.criteria) return;

  container.innerHTML = state.rubric.criteria
    .map((c) => {
      const rating = state.compareRatings[c.key] || { a: null, b: null, evidenceA: "", evidenceB: "" };
      const scoreA = rating.a;
      const scoreB = rating.b;

      const makeButtons = (side) => {
        const selectedVal = side === "a" ? scoreA : scoreB;
        const selectedCls = side === "a" ? "selected-a" : "selected-b";
        return [1, 2, 3, 4, 5, 6]
          .map(
            (val) => `
              <button type="button"
                class="rate-btn ${selectedVal === val ? selectedCls : ""}"
                data-crit="${escapeHtml(c.key)}"
                data-side="${side}"
                data-val="${val}"
                title="Rate ${val} / 6">
                ${val}
              </button>
            `
          )
          .join("");
      };

      const scoreLabelA = scoreA !== null ? `${scoreA} / 6` : "— / 6";
      const scoreLabelB = scoreB !== null ? `${scoreB} / 6` : "— / 6";
      const defaultHint = 'Click "Run evaluation" for AI rating or click a box to rate manually.';

      return `
        <div class="criterion-card" data-key="${escapeHtml(c.key)}">
          <div class="crit-info-col">
            <div class="criterion-info-head">
              <span class="criterion-name">${escapeHtml(c.name)}</span>
              <span class="weight-pill">${c.weight_percent}%</span>
            </div>
            <div class="criterion-anchor">${escapeHtml(c.anchor)}</div>
          </div>

          <div class="crit-side-col">
            <div class="score-header-row">
              <div class="score-header-left">
                <span class="badge-a">A</span>
                <span class="badge-label-text">Response A</span>
              </div>
              <span class="score-badge ${scoreA !== null ? "badge-val-a" : ""}">Score: ${scoreLabelA}</span>
            </div>
            <div class="rating-buttons" role="group" aria-label="${escapeHtml(c.name)} score for Response A">
              ${makeButtons("a")}
            </div>
            <p class="evidence-text">${escapeHtml(rating.evidenceA || defaultHint)}</p>
          </div>

          <div class="crit-side-col">
            <div class="score-header-row">
              <div class="score-header-left">
                <span class="badge-b">B</span>
                <span class="badge-label-text">Response B</span>
              </div>
              <span class="score-badge ${scoreB !== null ? "badge-val-b" : ""}">Score: ${scoreLabelB}</span>
            </div>
            <div class="rating-buttons" role="group" aria-label="${escapeHtml(c.name)} score for Response B">
              ${makeButtons("b")}
            </div>
            <p class="evidence-text">${escapeHtml(rating.evidenceB || defaultHint)}</p>
          </div>
        </div>
      `;
    })
    .join("");

  container.querySelectorAll(".rate-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const { crit, side, val } = btn.dataset;
      if (!state.compareRatings[crit]) {
        state.compareRatings[crit] = { a: null, b: null, evidenceA: "", evidenceB: "" };
      }
      state.compareRatings[crit][side] = Number(val);
      if (side === "a") {
        state.compareRatings[crit].evidenceA = `Manual rating: ${val} / 6 assigned.`;
      } else {
        state.compareRatings[crit].evidenceB = `Manual rating: ${val} / 6 assigned.`;
      }
      renderCompareCriteria();
      recalcManualCompareScores();
    });
  });
}

function recalcManualCompareScores() {
  const criteria = state.rubric?.criteria || [];
  let totalScoreA = 0;
  let totalScoreB = 0;
  let hasA = false;
  let hasB = false;

  for (const c of criteria) {
    const r = state.compareRatings[c.key];
    const w = (c.weight_percent || 20) / 100;
    if (r?.a !== null && r?.a !== undefined) {
      totalScoreA += r.a * w;
      hasA = true;
    }
    if (r?.b !== null && r?.b !== undefined) {
      totalScoreB += r.b * w;
      hasB = true;
    }
  }

  if (hasA) {
    const valA = totalScoreA.toFixed(2);
    if ($("banner-score-a")) $("banner-score-a").textContent = valA;
    if ($("overall-score-a")) $("overall-score-a").textContent = valA;
  }
  if (hasB) {
    const valB = totalScoreB.toFixed(2);
    if ($("banner-score-b")) $("banner-score-b").textContent = valB;
    if ($("overall-score-b")) $("overall-score-b").textContent = valB;
  }
}

function loadSampleById(sampleId) {
  const samples = state.rubric?.samples || [];
  const sample = samples.find((s) => s.id === sampleId);
  if (!sample) return;
  $("compare-prompt").value = sample.prompt;
  $("compare-a").value = sample.response_a;
  $("compare-b").value = sample.response_b;
  syncBlindFromCompare(false);
}

async function evaluateCompare({ save = true } = {}) {
  const errorBox = $("compare-error");
  errorBox.classList.add("hidden");

  const prompt = $("compare-prompt").value.trim();
  const response_a = $("compare-a").value.trim();
  const response_b = $("compare-b").value.trim();

  if (!prompt || !response_a || !response_b) {
    errorBox.textContent = "Please fill in Prompt, Response A, and Response B before running an evaluation.";
    errorBox.classList.remove("hidden");
    return;
  }

  const runBtn = $("run-eval-btn");
  runBtn.disabled = true;
  runBtn.textContent = "Evaluating...";

  try {
    const result = await apiJson("/api/compare", {
      method: "POST",
      body: JSON.stringify({
        prompt,
        response_a,
        response_b,
        simulate_position_bias: state.simulateBias,
        save,
      }),
    });
    renderCompareResults(result);
    if (typeof result.history_count === "number") {
      updateHistoryCount(result.history_count);
    }
  } catch (err) {
    errorBox.textContent = err.message;
    errorBox.classList.remove("hidden");
  } finally {
    runBtn.disabled = false;
    runBtn.textContent = "Run evaluation";
  }
}

function renderCompareResults(res) {
  state.compareResults = res;

  // Populate compareRatings with AI judge 1–6 scores & evidence
  (res.criteria || []).forEach((item) => {
    state.compareRatings[item.key] = {
      a: item.a.score,
      b: item.b.score,
      evidenceA: item.a.evidence,
      evidenceB: item.b.evidence,
    };
  });

  // Re-render criteria cards to highlight selected 1–6 buttons with AI scores
  renderCompareCriteria();

  // Show top (Winner banner + Swap audit) and bottom (Overall card + Export)
  showCompareResults(true);

  // 1. Winner banner
  const banner = $("winner-banner");
  const isConsistent = Boolean(res.consistent);
  banner.className = `winner-banner ${isConsistent ? "consistent" : "bias"}`;
  $("winner-icon").textContent = isConsistent ? "✓" : "!";

  if (!isConsistent) {
    $("winner-title").textContent = "Position bias detected · low confidence";
    $("winner-sub").textContent =
      "The preferred response flipped when Response A and Response B swapped positions. Treat this verdict as low confidence.";
  } else if (res.final_winner === "Tie") {
    $("winner-title").textContent = "The responses are approximately equivalent";
    $("winner-sub").textContent = "Both passes scored Response A and Response B with comparable weighted quality.";
  } else {
    $("winner-title").textContent = `${res.final_winner} wins · high confidence`;
    $("winner-sub").textContent =
      "The judge picked the same answer across both A vs B and B vs A passes, confirming position did not decide the outcome.";
  }

  $("banner-score-a").textContent = Number(res.score_a).toFixed(2);
  $("banner-tier-a").textContent = res.tier_a_label || "";
  $("banner-score-b").textContent = Number(res.score_b).toFixed(2);
  $("banner-tier-b").textContent = res.tier_b_label || "";

  // 2. 3-Step Swap Audit cards
  const p1PosText = res.pass1_position ? `(shown in position ${res.pass1_position})` : "(equal score)";
  const p2PosText = res.pass2_position ? `(shown in position ${res.pass2_position})` : "(equal score)";
  $("audit-pass1").innerHTML = `Picked <strong>${escapeHtml(res.pass1_winner)}</strong> ${escapeHtml(p1PosText)}`;
  $("audit-pass2").innerHTML = `Picked <strong>${escapeHtml(res.pass2_winner)}</strong> ${escapeHtml(p2PosText)}`;

  const card3 = $("audit-card-3");
  card3.classList.toggle("bias-warn", !isConsistent);
  $("audit-pass3").innerHTML = isConsistent
    ? `<strong>Same winner</strong> · confidence high`
    : `<strong>Flipped on swap</strong> · position bias flagged`;

  // 3. Overall card (Vindex Weighted Quality & SxS Justification)
  $("overall-score-a").textContent = Number(res.score_a).toFixed(2);
  $("overall-tier-a").textContent = res.tier_a_label || "";
  $("overall-score-b").textContent = Number(res.score_b).toFixed(2);
  $("overall-tier-b").textContent = res.tier_b_label || "";
  $("overall-preference").textContent = res.preference_label || "";
  $("overall-headline").textContent = res.synthesis?.headline || "";
  $("overall-summary").textContent = res.synthesis?.summary || "";
  $("overall-confidence").textContent = isConsistent ? "High" : "Low (Position bias)";
  $("overall-strengths").textContent = res.synthesis?.strengths || "";
  $("overall-issues").textContent = res.synthesis?.issues || "";

  // 4. Update engine label to show which engine actually ran
  if (res.engine) {
    $("engine-label").textContent = `Judge engine: ${res.engine}`;
  }

  // 5. Persistence footer status
  const engineNote = res.engine && res.engine.includes("fallback")
    ? ` ⚠️ API failed — used ${res.engine}.`
    : "";
  $("compare-save-note").textContent = res.saved
    ? `Saved to local SQLite history. Weighted formula: IF(25%) + Truth(25%) + Correctness(20%) + Writing(15%) + Verbosity(15%).${engineNote}`
    : `Preview mode (not saved yet) · click Run evaluation to save.${engineNote}`;
}

function setupCompareListeners() {
  $("sample-select").addEventListener("change", (e) => {
    const val = e.target.value;
    if (val) {
      loadSampleById(val);
    } else {
      $("compare-prompt").value = "";
      $("compare-a").value = "";
      $("compare-b").value = "";
    }
    // Reset ratings and keep results hidden until user clicks Run evaluation!
    initCompareRatings();
    renderCompareCriteria();
    showCompareResults(false);
  });

  $("run-eval-btn").addEventListener("click", () => {
    evaluateCompare({ save: true });
  });

  document.querySelectorAll(".seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.simulateBias = btn.dataset.bias === "true";
      document.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
      const isTopVisible = !$("compare-results-top").classList.contains("hidden");
      if (isTopVisible) {
        evaluateCompare({ save: false });
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Tab 2: Blind Review Mode (Human 10 Ratings vs Hidden AI Judge)
// ---------------------------------------------------------------------------

function syncBlindFromCompare(resetRatings = true) {
  const prompt = $("compare-prompt").value;
  const respA = $("compare-a").value;
  const respB = $("compare-b").value;

  $("blind-prompt-input").value = prompt;
  $("blind-a-input").value = respA;
  $("blind-b-input").value = respB;
  $("blind-prompt-display").textContent = prompt || "No prompt entered in Compare tab yet. Click 'Edit text' to enter one.";
  $("blind-a-display").textContent = respA || "(Empty)";
  $("blind-b-display").textContent = respB || "(Empty)";

  if (resetRatings) {
    resetBlindRatings();
  }
}

function resetBlindRatings() {
  state.blindHumanRatings = {};
  state.blindRevealedResult = null;
  (state.rubric?.criteria || []).forEach((c) => {
    state.blindHumanRatings[c.key] = { a: null, b: null };
  });
  $("blind-overall-card").classList.add("hidden");
  $("blind-error").classList.add("hidden");
  renderBlindCriteria();
  updateBlindProgress();
}

function countRatingsGiven() {
  return Object.values(state.blindHumanRatings).reduce((acc, pair) => {
    return acc + (pair.a ? 1 : 0) + (pair.b ? 1 : 0);
  }, 0);
}

function updateBlindProgress() {
  const total = countRatingsGiven();
  $("blind-progress-pill").textContent = `${total}/10 ratings given`;
  const ready = total === 10;
  $("blind-reveal-btn").disabled = !ready;
  $("blind-reveal-hint").textContent = ready
    ? "All 10 ratings given (1–6 scale) — click to reveal AI judge scores!"
    : "Rate all 10 scores (5 criteria × A & B on 1–6 scale) to unlock reveal.";
}

function renderBlindCriteria() {
  const container = $("blind-criteria-list");
  const revealed = state.blindRevealedResult;
  const compMap = {};
  if (revealed?.blind_comparisons) {
    revealed.blind_comparisons.forEach((c) => {
      compMap[c.key] = c;
    });
  }

  container.innerHTML = (state.rubric?.criteria || [])
    .map((c) => {
      const rating = state.blindHumanRatings[c.key] || { a: null, b: null };
      const comp = compMap[c.key];

      const makeBtns = (side) => {
        const selected = rating[side];
        const cls = side === "a" ? "selected-a" : "selected-b";
        // Vindex 1 to 6 scale
        return [1, 2, 3, 4, 5, 6]
          .map(
            (val) => `
              <button type="button"
                class="rate-btn ${selected === val ? cls : ""}"
                data-crit="${c.key}" data-side="${side}" data-val="${val}">
                ${val}
              </button>
            `
          )
          .join("");
      };

      const makeAiPill = (side) => {
        if (!comp) return "";
        const aiVal = side === "a" ? comp.ai_a : comp.ai_b;
        const diff = side === "a" ? comp.diff_a : comp.diff_b;
        return diff === 0
          ? `<div class="ai-pill match">AI gave ${aiVal}/6 · Match</div>`
          : `<div class="ai-pill off">AI gave ${aiVal}/6 · Off by ${diff}</div>`;
      };

      return `
        <div class="blind-criterion-card">
          <div class="blind-crit-head">
            <div class="criterion-info-head">
              <span class="criterion-name">${escapeHtml(c.name)}</span>
              <span class="weight-pill">${c.weight_percent}%</span>
            </div>
            <span class="criterion-anchor">${escapeHtml(c.anchor)}</span>
          </div>

          <div class="blind-rate-cols">
            <div>
              <div class="badge-label"><span class="badge-a">A</span> Response A</div>
              <div class="rating-buttons">${makeBtns("a")}</div>
              ${makeAiPill("a")}
            </div>
            <div>
              <div class="badge-label"><span class="badge-b">B</span> Response B</div>
              <div class="rating-buttons">${makeBtns("b")}</div>
              ${makeAiPill("b")}
            </div>
          </div>
        </div>
      `;
    })
    .join("");

  container.querySelectorAll(".rate-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (state.blindRevealedResult) return; // Lock after reveal until Start over
      const { crit, side, val } = btn.dataset;
      state.blindHumanRatings[crit][side] = Number(val);
      renderBlindCriteria();
      updateBlindProgress();
    });
  });
}

async function revealBlindScores() {
  const errBox = $("blind-error");
  errBox.classList.add("hidden");

  const isEditing = !$("blind-edit-view").classList.contains("hidden");
  const prompt = (isEditing ? $("blind-prompt-input").value : $("blind-prompt-display").textContent).trim();
  const response_a = (isEditing ? $("blind-a-input").value : $("blind-a-display").textContent).trim();
  const response_b = (isEditing ? $("blind-b-input").value : $("blind-b-display").textContent).trim();

  if (!prompt || !response_a || !response_b) {
    errBox.textContent = "Prompt, Response A, and Response B cannot be empty.";
    errBox.classList.remove("hidden");
    return;
  }

  const btn = $("blind-reveal-btn");
  btn.disabled = true;
  btn.textContent = "Scoring...";

  try {
    const res = await apiJson("/api/blind", {
      method: "POST",
      body: JSON.stringify({
        prompt,
        response_a,
        response_b,
        human_ratings: state.blindHumanRatings,
        save: true,
      }),
    });

    state.blindRevealedResult = res;
    renderBlindCriteria();

    $("blind-human-a").textContent = Number(res.human_score_a).toFixed(2);
    $("blind-human-tier-a").textContent = res.human_tier_a || "";
    $("blind-human-b").textContent = Number(res.human_score_b).toFixed(2);
    $("blind-human-tier-b").textContent = res.human_tier_b || "";
    $("blind-human-prefer").textContent = res.human_preference || res.human_prefer;

    $("blind-ai-a").textContent = Number(res.score_a).toFixed(2);
    $("blind-ai-tier-a").textContent = res.tier_a_label || "";
    $("blind-ai-b").textContent = Number(res.score_b).toFixed(2);
    $("blind-ai-tier-b").textContent = res.tier_b_label || "";
    $("blind-ai-prefer").textContent = res.preference_label || res.ai_prefer;

    $("blind-exact-matches").textContent = `${res.exact_matches}/10`;

    const agreeBox = $("blind-agree-banner");
    agreeBox.className = `agree-box ${res.winner_agreement ? "agree" : "disagree"}`;
    $("blind-agree-title").textContent = res.winner_agreement
      ? "You and the AI agree on the preferred response"
      : "You and the AI picked different preferred responses";
    $("blind-agree-sub").textContent = res.winner_agreement
      ? `Both agreed on ${res.ai_prefer}.`
      : `You chose ${res.human_prefer}, while the AI judge chose ${res.ai_prefer}.`;

    $("blind-overall-card").classList.remove("hidden");
    if (typeof res.history_count === "number") {
      updateHistoryCount(res.history_count);
    }
  } catch (err) {
    errBox.textContent = err.message;
    errBox.classList.remove("hidden");
  } finally {
    btn.textContent = "Reveal AI judge scores";
    updateBlindProgress();
  }
}

function setupBlindListeners() {
  $("blind-sync-btn").addEventListener("click", () => syncBlindFromCompare(true));

  $("blind-edit-toggle").addEventListener("click", () => {
    const ro = $("blind-readonly-view");
    const ed = $("blind-edit-view");
    const nowEditing = ed.classList.contains("hidden");
    if (!nowEditing) {
      $("blind-prompt-display").textContent = $("blind-prompt-input").value;
      $("blind-a-display").textContent = $("blind-a-input").value;
      $("blind-b-display").textContent = $("blind-b-input").value;
    }
    ro.classList.toggle("hidden", nowEditing);
    ed.classList.toggle("hidden", !nowEditing);
    $("blind-edit-toggle").textContent = nowEditing ? "Done editing" : "Edit text";
  });

  $("blind-reveal-btn").addEventListener("click", revealBlindScores);
  $("blind-reset-btn").addEventListener("click", resetBlindRatings);
}

// ---------------------------------------------------------------------------
// Settings Modal (API Keys & Engine Switcher)
// ---------------------------------------------------------------------------

function setupSettingsListeners() {
  const modal = $("settings-modal");
  const pill = $("engine-status-pill");
  const closeBtn = $("settings-close-btn");
  const saveBtn = $("settings-save-btn");
  const providerSelect = $("settings-provider");
  const keyGroup = $("api-key-group");
  const keyInput = $("settings-key");
  const msg = $("settings-msg");

  pill.addEventListener("click", () => {
    providerSelect.value = state.engine?.id || "heuristic";
    keyGroup.classList.toggle("hidden", providerSelect.value === "heuristic");
    keyInput.value = "";
    msg.textContent = "";
    modal.classList.remove("hidden");
  });

  providerSelect.addEventListener("change", () => {
    keyGroup.classList.toggle("hidden", providerSelect.value === "heuristic");
    msg.textContent = "";
  });

  closeBtn.addEventListener("click", () => modal.classList.add("hidden"));
  modal.addEventListener("click", (e) => {
    if (e.target.id === "settings-modal") modal.classList.add("hidden");
  });

  saveBtn.addEventListener("click", async () => {
    msg.textContent = "Saving...";
    try {
      const res = await apiJson("/api/settings", {
        method: "POST",
        body: JSON.stringify({
          provider: providerSelect.value,
          api_key: keyInput.value.trim(),
        }),
      });
      state.engine = res.engine;
      $("engine-label").textContent = `Judge engine: ${res.engine.label}`;
      msg.textContent = "Settings saved successfully!";
      setTimeout(() => modal.classList.add("hidden"), 700);
    } catch (err) {
      msg.textContent = `Error: ${err.message}`;
    }
  });
}

// ---------------------------------------------------------------------------
// Tab 3: History (SQLite Persistence + Inspection Modal + Delete)
// ---------------------------------------------------------------------------

async function loadHistory() {
  const params = new URLSearchParams();
  if (state.historyFilter === "compare" || state.historyFilter === "blind") {
    params.set("mode", state.historyFilter);
  } else if (state.historyFilter === "consistent" || state.historyFilter === "bias") {
    params.set("consistency", state.historyFilter);
  }
  if (state.historySearch.trim()) {
    params.set("q", state.historySearch.trim());
  }

  const data = await apiJson(`/api/history?${params.toString()}`);
  state.historyItems = data.items || [];
  renderHistoryList();
}

function renderHistoryList() {
  const container = $("history-list");
  if (!state.historyItems.length) {
    container.innerHTML = `
      <div class="card empty-state">
        No saved evaluations match your filter yet. Enter a prompt and click <strong>Run evaluation</strong>
        in Compare or complete a <strong>Blind review</strong> to record history.
      </div>
    `;
    return;
  }

  container.innerHTML = state.historyItems
    .map((item) => {
      const modeBadge = item.mode === "blind"
        ? `<span class="pill mode-blind">Blind review</span>`
        : `<span class="pill">Compare</span>`;
      const consBadge = item.consistent
        ? `<span class="pill consistent">Consistent · ${escapeHtml(item.final_winner)}</span>`
        : `<span class="pill bias">Position bias</span>`;
      const blindInfo = item.mode === "blind" && item.exact_matches !== null
        ? `<span class="pill">Exact matches: ${item.exact_matches}/10</span>`
        : "";
      const when = new Date(item.created_at).toLocaleString();

      return `
        <div class="history-card">
          <div class="history-main">
            <div class="history-badges">
              ${modeBadge}
              ${consBadge}
              ${blindInfo}
              <span class="hint-text">${escapeHtml(when)} · ${escapeHtml(item.engine)}</span>
            </div>
            <div class="history-prompt">${escapeHtml(item.prompt)}</div>
            <div class="history-meta">
              Pass 1: ${escapeHtml(item.pass1_winner)} · Pass 2: ${escapeHtml(item.pass2_winner)} · Preference: ${escapeHtml(item.preference_label || "SxS evaluated")}
            </div>
          </div>

          <div class="history-right">
            <div class="history-scores">
              <span class="text-a">A: ${Number(item.score_a).toFixed(2)}</span>
              &nbsp;vs&nbsp;
              <span class="text-b">B: ${Number(item.score_b).toFixed(2)}</span>
            </div>
            <button type="button" class="btn-secondary view-history-btn" data-id="${item.id}">Inspect</button>
            <button type="button" class="btn-danger-outline del-history-btn" data-id="${item.id}">Delete</button>
          </div>
        </div>
      `;
    })
    .join("");

  container.querySelectorAll(".view-history-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const record = state.historyItems.find((i) => i.id === Number(btn.dataset.id));
      if (record) openHistoryModal(record);
    });
  });


  container.querySelectorAll(".del-history-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.id;

      // Step 1: Confirm before deleting
      const confirmed = confirm(
        `Are you sure you want to delete history record ${id}?`
      );

      if (!confirmed) {
        return;
      }

      // Step 2: Disable button to prevent double clicks
      btn.disabled = true;
      btn.textContent = "Deleting...";

      try {
        // Step 3: Send DELETE request to backend
        const res = await apiJson(
          `/api/history/${id}`,
          {
            method: "DELETE"
          }
        );

        // Step 4: Update history count
        if (res.history_count !== undefined) {
          updateHistoryCount(res.history_count);
        }

        console.log(
          `History record ${id} deleted successfully`
        );

        // Step 5: Refresh history
        await loadHistory();

      } catch (error) {
        console.error(
          `Failed to delete history record ${id}:`,
          error
        );

        // Step 6: Handle 404 error
        if (error.message.includes("404")) {
          alert(
            "This history record was not found or was already deleted."
          );
        } else {
          alert(
            `Failed to delete history: ${error.message}`
          );
        }

        // Step 7: Refresh history after error
        try {
          await loadHistory();
        } catch (refreshError) {
          console.error(
            "Failed to refresh history:",
            refreshError
          );
        }

      } finally {
        // Step 8: Restore button if still present
        if (btn.isConnected) {
          btn.disabled = false;
          btn.textContent = "Delete";
        }
      }
    });
  });

}

function openHistoryModal(item) {
  $("modal-title").textContent = item.mode === "blind" ? "Saved Blind Review" : "Saved Vindex Evaluation";
  $("modal-meta").textContent = `${new Date(item.created_at).toLocaleString()} · ${item.engine}`;

  const criteriaRows = (item.criteria || [])
    .map(
      (c) => `
        <div class="card" style="padding:12px 14px">
          <strong>${escapeHtml(c.name)} (${c.weight_percent}%)</strong>
          <div style="margin-top:6px">
            <span class="text-a"><strong>Response A (${c.a.score}/6):</strong></span> ${escapeHtml(c.a.evidence)}
          </div>
          <div style="margin-top:4px">
            <span class="text-b"><strong>Response B (${c.b.score}/6):</strong></span> ${escapeHtml(c.b.evidence)}
          </div>
        </div>
      `
    )
    .join("");

  const blindBlock =
    item.mode === "blind"
      ? `<div class="card" style="padding:12px 14px; background:#eef2ff">
           <strong>Human Blind Review vs AI Judge:</strong><br/>
           Your score: A ${Number(item.human_score_a).toFixed(2)} vs B ${Number(item.human_score_b).toFixed(2)} (${escapeHtml(item.human_preference || item.human_prefer)})<br/>
           AI score: A ${Number(item.score_a).toFixed(2)} vs B ${Number(item.score_b).toFixed(2)} (${escapeHtml(item.preference_label || item.ai_prefer)})<br/>
           Exact criterion matches: <strong>${item.exact_matches}/10</strong>
         </div>`
      : "";

  $("modal-body").innerHTML = `
    <div><strong>Prompt:</strong><br/>${escapeHtml(item.prompt)}</div>
    <div class="two-col-inputs" style="margin-top:0">
      <div class="blind-resp-box">
        <strong class="text-a">Response A (${Number(item.score_a).toFixed(2)}/6.00 · ${escapeHtml(item.tier_a_label || "")})</strong>
        <p style="margin:6px 0 0">${escapeHtml(item.response_a)}</p>
      </div>
      <div class="blind-resp-box">
        <strong class="text-b">Response B (${Number(item.score_b).toFixed(2)}/6.00 · ${escapeHtml(item.tier_b_label || "")})</strong>
        <p style="margin:6px 0 0">${escapeHtml(item.response_b)}</p>
      </div>
    </div>
    ${blindBlock}
    <div>
      <strong>Swap Test Audit:</strong>
      Pass 1 picked <em>${escapeHtml(item.pass1_winner)}</em> ·
      Pass 2 picked <em>${escapeHtml(item.pass2_winner)}</em> ·
      Verdict: <strong>${escapeHtml(item.final_winner)}</strong>
    </div>
    ${criteriaRows}
    <div class="strengths-issues-grid">
      <div class="si-box strengths-box"><div class="si-title">Strengths</div><p>${escapeHtml(item.synthesis?.strengths)}</p></div>
      <div class="si-box issues-box"><div class="si-title">Issues</div><p>${escapeHtml(item.synthesis?.issues)}</p></div>
    </div>
  `;

  $("history-modal").classList.remove("hidden");
}

function setupHistoryListeners() {
  document.querySelectorAll(".filter-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      state.historyFilter = chip.dataset.filter;
      document.querySelectorAll(".filter-chip").forEach((c) => c.classList.toggle("active", c === chip));
      loadHistory();
    });
  });

  $("history-search").addEventListener("input", (e) => {
    state.historySearch = e.target.value;
    loadHistory();
  });

  $("clear-history-btn").addEventListener("click", async () => {
    if (!confirm("Delete all saved evaluation history?")) return;
    await apiJson("/api/history", { method: "DELETE" });
    updateHistoryCount(0);
    loadHistory();
  });

  $("modal-close-btn").addEventListener("click", () => $("history-modal").classList.add("hidden"));
  $("history-modal").addEventListener("click", (e) => {
    if (e.target.id === "history-modal") $("history-modal").classList.add("hidden");
  });
}

// ---------------------------------------------------------------------------
// App Bootstrap
// ---------------------------------------------------------------------------

async function init() {
  setupTabs();
  setupCompareListeners();
  setupBlindListeners();
  setupSettingsListeners();
  setupHistoryListeners();

  try {
    const cfg = await apiJson("/api/config");
    state.rubric = cfg.rubric;
    state.engine = cfg.engine;

    $("engine-label").textContent = `Judge engine: ${cfg.engine.label}`;
    updateHistoryCount(cfg.history_count || 0);

    const select = $("sample-select");
    select.innerHTML =
      `<option value="">-- Load a sample prompt (optional) --</option>` +
      (cfg.rubric.samples || [])
        .map((s) => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.label)}</option>`)
        .join("");

    // Point 1: Keep inputs empty on page load! Do NOT auto-evaluate!
    $("compare-prompt").value = "";
    $("compare-a").value = "";
    $("compare-b").value = "";
    showCompareResults(false);

    // Render Vindex 1–6 criteria cards with interactive rating boxes immediately!
    initCompareRatings();
    renderCompareCriteria();

    resetBlindRatings();
  } catch (err) {
    $("compare-error").textContent = `Failed to connect to backend: ${err.message}`;
    $("compare-error").classList.remove("hidden");
  }
}

document.addEventListener("DOMContentLoaded", init);
