const $ = (selector) => document.querySelector(selector);

const toast = (message) => {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  window.setTimeout(() => node.classList.remove("show"), 2600);
};

const api = async (url, options = {}) => {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.detail || `Request failed: ${response.status}`);
  }
  return response.json();
};

const escapeHtml = (value = "") =>
  String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

const riskClass = (risk = "") => `risk-${risk.toLowerCase()}`;

const renderEmpty = (text) => `<div class="empty-state">${escapeHtml(text)}</div>`;

const renderReview = (items) => {
  const root = $("#reviewList");
  if (!items.length) {
    root.innerHTML = renderEmpty("No queued reviews. MailAI is calm right now.");
    return;
  }

  root.innerHTML = items
    .map(
      (item) => `
        <article class="review-card">
          <h3 class="card-title">${escapeHtml(item.subject || "(no subject)")}</h3>
          <p class="card-meta">${escapeHtml(item.sender)}<br>${escapeHtml(item.review_reason || "Needs approval")}</p>
          <div class="pill-row">
            <span class="pill ${riskClass(item.risk_category)}">${escapeHtml(item.risk_category)}</span>
            <span class="pill">${escapeHtml(item.job_category)}</span>
            <span class="pill">${Math.round((item.confidence || 0) * 100)}% confidence</span>
          </div>
          ${
            item.draft_body
              ? `<div class="draft-preview">${escapeHtml(item.draft_body).slice(0, 900)}</div>`
              : `<div class="reasoning">No draft body was generated. This item is queued because the policy blocked automatic action.</div>`
          }
          <div class="review-actions">
            <button class="approve" data-approve="${item.id}">Approve Gmail Draft</button>
            <button class="reject" data-reject="${item.id}">Reject</button>
            <button class="reject" data-never-draft="${item.id}" data-sender="${escapeHtml(item.sender_email)}">Reject + Never Draft Domain</button>
          </div>
        </article>
      `
    )
    .join("");
};

const renderAudit = (items) => {
  const root = $("#auditList");
  if (!items.length) {
    root.innerHTML = renderEmpty("No audit events yet. Run MailAI once to populate the trust trail.");
    return;
  }

  root.innerHTML = items
    .map(
      (item) => `
        <article class="audit-card">
          <h3 class="card-title">${escapeHtml(item.subject || "(no subject)")}</h3>
          <p class="card-meta">${escapeHtml(item.sender)}<br>${escapeHtml(item.reasoning || "No reasoning captured")}</p>
          <div class="pill-row">
            <span class="pill ${riskClass(item.risk_category)}">${escapeHtml(item.risk_category)}</span>
            <span class="pill">${escapeHtml(item.policy_action)}</span>
            <span class="pill">${escapeHtml(item.job_category)}</span>
            <span class="pill">${Math.round((item.confidence || 0) * 100)}%</span>
          </div>
          <div class="review-actions">
            ${
              item.latest_action_id && item.reversible
                ? `<button class="undo" data-undo="${item.latest_action_id}">Undo Latest Action</button>`
                : ""
            }
          </div>
        </article>
      `
    )
    .join("");
};

const renderDigest = (digest) => {
  $("#dailyDigest").innerHTML = `
    <p>${escapeHtml(digest.summary)}</p>
    <div>
      <span class="digest-number">${digest.handled}</span> handled
      <span class="digest-number">${digest.queued_for_review}</span> queued
      <span class="digest-number">${digest.high_risk_blocked}</span> high-risk blocked
    </div>
  `;
};

const renderPreferences = (items) => {
  const root = $("#preferenceList");
  if (!items.length) {
    root.innerHTML = renderEmpty("No learned preference rules yet.");
    return;
  }

  root.innerHTML = items
    .map(
      (item) => `
        <article class="preference-card">
          <h3 class="card-title">${escapeHtml(item.key)}</h3>
          <p class="card-meta">${escapeHtml(item.scope_type)}: ${escapeHtml(item.scope_value || "global")}<br>Source: ${escapeHtml(item.source)}</p>
        </article>
      `
    )
    .join("");
};

const loadDashboard = async () => {
  $("#reviewList").innerHTML = `<div class="loading-state">Loading review queue...</div>`;
  $("#auditList").innerHTML = `<div class="loading-state">Loading audit trail...</div>`;
  const [reviews, audits, digest, preferences] = await Promise.all([
    api("/api/review"),
    api("/api/audit?limit=40"),
    api("/api/digest/daily"),
    api("/api/preferences"),
  ]);
  renderReview(reviews.items || []);
  renderAudit(audits.items || []);
  renderDigest(digest);
  renderPreferences(preferences.items || []);
};

document.addEventListener("click", async (event) => {
  const approveId = event.target?.dataset?.approve;
  const rejectId = event.target?.dataset?.reject;
  const neverDraftId = event.target?.dataset?.neverDraft;
  const undoId = event.target?.dataset?.undo;

  try {
    if (approveId) {
      await api(`/api/review/${approveId}/approve`, { method: "POST" });
      toast("Approved. Gmail draft created.");
      await loadDashboard();
    }
    if (rejectId) {
      await api(`/api/review/${rejectId}/reject`, {
        method: "POST",
        body: JSON.stringify({ note: "Rejected from dashboard" }),
      });
      toast("Rejected and logged.");
      await loadDashboard();
    }
    if (neverDraftId) {
      const sender = event.target.dataset.sender || "";
      const domain = sender.includes("@") ? sender.split("@").pop() : sender;
      await api(`/api/review/${neverDraftId}/reject`, {
        method: "POST",
        body: JSON.stringify({
          note: "Rejected and converted to never_draft domain preference",
          preference: {
            key: "never_draft",
            value: true,
            scope_type: "domain",
            scope_value: domain,
          },
        }),
      });
      toast("Rejected. Future drafts blocked for that domain.");
      await loadDashboard();
    }
    if (undoId) {
      await api(`/api/actions/${undoId}/undo`, { method: "POST" });
      toast("Undo completed.");
      await loadDashboard();
    }
    if (event.target?.dataset?.refresh !== undefined) {
      await loadDashboard();
      toast("Dashboard refreshed.");
    }
  } catch (error) {
    toast(error.message);
  }
});

$("#preferenceForm")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    await api("/api/preferences", {
      method: "POST",
      body: JSON.stringify({
        key: form.get("key"),
        value: true,
        scope_type: form.get("scope_type"),
        scope_value: form.get("scope_value"),
      }),
    });
    event.currentTarget.reset();
    toast("Preference saved.");
    await loadDashboard();
  } catch (error) {
    toast(error.message);
  }
});

loadDashboard().catch((error) => toast(error.message));

