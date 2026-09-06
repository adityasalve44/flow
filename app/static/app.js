/**
 * Flow WhatsApp Simulator & Agent Intelligence Cockpit
 * Frontend Application Logic (Vanilla JS)
 */

// Application State
const state = {
  candidateId: null,
  phoneNumber: '+919876543210',
  contactName: 'Aditya Salve',
  currentPersona: 'custom',
  isThinking: false,
};

// DOM Element Selectors
const elements = {
  // Simulator Controls
  personaSelect: document.getElementById('persona-select'),
  btnNewCandidate: document.getElementById('btn-new-candidate'),
  btnRefreshInspector: document.getElementById('btn-refresh-inspector'),
  chatThread: document.getElementById('chat-thread'),
  messageInput: document.getElementById('message-input'),
  sendBtn: document.getElementById('send-btn'),
  thinkingBubble: document.getElementById('thinking-bubble'),
  thinkingText: document.getElementById('thinking-text'),
  quickPromptsBar: document.getElementById('quick-prompts-bar'),
  waContactName: document.getElementById('wa-contact-name'),
  waContactPhone: document.getElementById('wa-contact-phone'),
  badgeMode: document.getElementById('badge-mode'),

  // Cockpit Header & Badges
  inspCandidateId: document.getElementById('insp-candidate-id'),
  badgeLifecycle: document.getElementById('badge-lifecycle'),
  badgeConsent: document.getElementById('badge-consent'),
  completenessNum: document.getElementById('completeness-num'),
  completenessBar: document.getElementById('completeness-bar'),
  completenessStatus: document.getElementById('completeness-status'),

  // Tab 1: Intelligence
  metricDirective: document.getElementById('metric-directive'),
  metricDirectiveDesc: document.getElementById('metric-directive-desc'),
  metricMode: document.getElementById('metric-mode'),
  metricDeflections: document.getElementById('metric-deflections'),
  metricAbuse: document.getElementById('metric-abuse'),
  tStatus: document.getElementById('t-status'),
  tDecision: document.getElementById('t-decision'),
  tMsgId: document.getElementById('t-msg-id'),
  tConvId: document.getElementById('t-conv-id'),

  // Tab 2: Profile Projection
  profFullName: document.getElementById('prof-full_name'),
  profCurrentRole: document.getElementById('prof-current_role'),
  profCurrentCompany: document.getElementById('prof-current_company'),
  profExperienceYears: document.getElementById('prof-experience_years'),
  profCurrentCtc: document.getElementById('prof-current_ctc'),
  profExpectedCtc: document.getElementById('prof-expected_ctc'),
  profNoticePeriod: document.getElementById('prof-notice_period'),
  profWorkMode: document.getElementById('prof-work_mode'),
  profEducation: document.getElementById('prof-education'),

  // Tab 3: Attributes
  attrCount: document.getElementById('attr-count'),
  attrTableBody: document.getElementById('attr-table-body'),

  // Tab 4: Preferences
  skillsCloud: document.getElementById('skills-cloud'),
  rolesCloud: document.getElementById('roles-cloud'),
  locationsCloud: document.getElementById('locations-cloud'),

  // Tab 5: Backlog
  btnRefreshBacklog: document.getElementById('btn-refresh-backlog'),
  backlogTableBody: document.getElementById('backlog-table-body'),

  // Tabs
  tabBtns: document.querySelectorAll('.tab-btn'),
  tabContents: document.querySelectorAll('.tab-content'),
};

// Directive Descriptions for the Live Cockpit
const DIRECTIVE_DESCRIPTIONS = {
  consent_gate: 'Enforcing WhatsApp consent before storing persistent facts (Q4 gate).',
  acknowledge_consent: 'Warmly acknowledged consent; transitioning conversation to intake mode.',
  ask_next: 'Extractor scored missing fields; replier asking for highest priority operational gap.',
  acknowledge_profile_ready: 'Six baseline fields satisfied! Profile is ready; stops interrogation.',
  offer_call: 'Second deflection detected; gently offering human recruiter phone outreach.',
  disengage_silent: 'Third deflection reached; conversation closed silently with zero model calls.',
  redirect: 'Handling candidate inquiry or off-topic turn, redirecting back to intake.',
  error_fallback: 'Safe system fallback activated.',
};

// Initialization
document.addEventListener('DOMContentLoaded', async () => {
  setupEventHandlers();
  await initializeSession();
});

// Setup UI Events
function setupEventHandlers() {
  // Tab Switching
  elements.tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      elements.tabBtns.forEach(b => b.classList.remove('active'));
      elements.tabContents.forEach(c => c.classList.remove('active'));

      btn.classList.add('active');
      const targetTab = document.getElementById(btn.dataset.tab);
      if (targetTab) targetTab.classList.add('active');

      if (btn.dataset.tab === 'tab-backlog') {
        loadBacklog();
      }
    });
  });

  // Message Send Events
  elements.sendBtn.addEventListener('click', handleSend);
  elements.messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  });

  // Quick Prompt Chips
  elements.quickPromptsBar.addEventListener('click', (e) => {
    const chip = e.target.closest('.prompt-chip');
    if (!chip) return;
    elements.messageInput.value = chip.dataset.text;
    handleSend();
  });

  // Controls
  elements.btnNewCandidate.addEventListener('click', () => createNewSession());
  elements.btnRefreshInspector.addEventListener('click', () => {
    if (state.candidateId) inspectCandidate(state.candidateId);
  });
  elements.btnRefreshBacklog.addEventListener('click', loadBacklog);

  // Persona Selector
  elements.personaSelect.addEventListener('change', (e) => {
    handlePersonaChange(e.target.value);
  });
}

// Session Initialization
async function initializeSession() {
  const savedId = localStorage.getItem('flow_sim_candidate_id');
  const savedPhone = localStorage.getItem('flow_sim_phone');

  if (savedId && savedPhone) {
    state.candidateId = savedId;
    state.phoneNumber = savedPhone;
    updateContactDisplay();
    await inspectCandidate(state.candidateId);
  } else {
    await createNewSession();
  }
}

// Create Fresh Candidate Session
async function createNewSession(customPhone = null, customName = null) {
  try {
    const res = await fetch('/api/simulator/reset', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        phone_number: customPhone,
        display_name: customName,
      }),
    });
    const data = await res.json();
    state.candidateId = data.candidate_id;
    state.phoneNumber = data.phone_number;
    state.contactName = data.display_name;

    localStorage.setItem('flow_sim_candidate_id', state.candidateId);
    localStorage.setItem('flow_sim_phone', state.phoneNumber);

    updateContactDisplay();
    elements.chatThread.innerHTML = '<div class="chat-timestamp-badge">Today</div>';
    await inspectCandidate(state.candidateId);
  } catch (err) {
    console.error('Failed to create candidate session:', err);
  }
}

function updateContactDisplay() {
  elements.waContactName.textContent = state.contactName;
  elements.waContactPhone.textContent = state.phoneNumber;
}

// Handle Persona Presets
async function handlePersonaChange(persona) {
  state.currentPersona = persona;
  if (persona === 'experienced_dev') {
    await createNewSession(null, 'Vikram Sharma (Senior Dev)');
    elements.messageInput.value = 'Hi! I am Vikram, a backend engineer with 7 years experience in Python and distributed systems.';
  } else if (persona === 'junior_dev') {
    await createNewSession(null, 'Priya Patel (Junior Dev)');
    elements.messageInput.value = 'Hello! I recently graduated with a B.Tech in CS and I am looking for junior software developer roles.';
  } else if (persona === 'evasive') {
    await createNewSession(null, 'Rahul Kumar (Evasive)');
    elements.messageInput.value = 'Hi. Are you a bot or a real human?';
  } else {
    await createNewSession();
  }
}

// Send Message Flow
async function handleSend() {
  const text = elements.messageInput.value.trim();
  if (!text || state.isThinking) return;

  elements.messageInput.value = '';
  appendMessage(text, 'inbound');

  // Set thinking state
  state.isThinking = true;
  elements.thinkingBubble.style.display = 'flex';
  elements.sendBtn.disabled = true;

  const channelMessageId = 'sim_' + Date.now() + '_' + Math.random().toString(36).substring(2, 7);

  try {
    const res = await fetch('/webhook', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        phone_number: state.phoneNumber,
        contact_name: state.contactName,
        message: text,
        channel_message_id: channelMessageId,
      }),
    });

    const data = await res.json();

    // Update Telemetry
    elements.tStatus.textContent = data.status || 'ok';
    elements.tDecision.textContent = data.decision || 'proceed';
    elements.tMsgId.textContent = data.channel_message_id || channelMessageId;
    elements.tConvId.textContent = data.conversation_id || '-';

    if (data.candidate_id) {
      state.candidateId = data.candidate_id;
      localStorage.setItem('flow_sim_candidate_id', state.candidateId);
    }

    if (data.reply_text) {
      appendMessage(data.reply_text, 'outbound');
    }

    if (data.directive) {
      updateDirectiveDisplay(data.directive);
    }

    if (data.mode) {
      elements.badgeMode.textContent = data.mode;
      elements.metricMode.textContent = data.mode;
    }

    // Refresh Live Inspector Cockpit
    if (state.candidateId) {
      await inspectCandidate(state.candidateId);
    }
  } catch (err) {
    console.error('Webhook turn error:', err);
    appendMessage('⚠️ Network or server error processing turn.', 'outbound');
  } finally {
    state.isThinking = false;
    elements.thinkingBubble.style.display = 'none';
    elements.sendBtn.disabled = false;
    elements.messageInput.focus();
  }
}

// Append Bubble to Chat Window
function appendMessage(text, direction) {
  const bubble = document.createElement('div');
  bubble.className = `chat-bubble bubble-${direction}`;

  const textElem = document.createElement('div');
  textElem.className = 'bubble-text';
  textElem.textContent = text;

  const metaElem = document.createElement('div');
  metaElem.className = 'bubble-meta';

  const timeStr = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  metaElem.innerHTML = `<span>${timeStr}</span>` + (direction === 'inbound' ? ' <span class="check-marks">✓✓</span>' : '');

  bubble.appendChild(textElem);
  bubble.appendChild(metaElem);
  elements.chatThread.appendChild(bubble);

  // Scroll to bottom
  elements.chatThread.scrollTop = elements.chatThread.scrollHeight;
}

function updateDirectiveDisplay(directive) {
  elements.metricDirective.textContent = directive;
  elements.metricDirectiveDesc.textContent = DIRECTIVE_DESCRIPTIONS[directive] || `Directive: ${directive}`;
}

// Inspect Candidate State
async function inspectCandidate(candidateId) {
  try {
    const res = await fetch(`/api/simulator/inspect/${candidateId}`);
    if (!res.ok) return;
    const data = await res.json();

    renderCockpit(data);
  } catch (err) {
    console.error('Failed to inspect candidate:', err);
  }
}

// Render Inspector Cockpit UI
function renderCockpit(data) {
  const c = data.candidate;
  const p = data.profile;

  // Header
  elements.inspCandidateId.textContent = c.id;
  elements.badgeLifecycle.textContent = c.lifecycle_status;
  elements.badgeConsent.textContent = c.consent_status;
  if (c.consent_status === 'granted') {
    elements.badgeConsent.classList.add('granted');
  } else {
    elements.badgeConsent.classList.remove('granted');
  }

  // Completeness score
  const comp = p.completeness || 0;
  const pct = Math.round(comp * 100);
  elements.completenessNum.textContent = `${pct}%`;
  elements.completenessBar.style.width = `${pct}%`;

  if (pct >= 100) {
    elements.completenessStatus.textContent = '🎉 Profile Ready! (All core fields satisfied)';
    elements.completenessStatus.style.color = 'var(--accent-emerald)';
  } else {
    const missing = Math.max(0, 6 - Math.round(comp * 6));
    elements.completenessStatus.textContent = `Missing ${missing} core baseline fields`;
    elements.completenessStatus.style.color = 'var(--text-dim)';
  }

  // Tab 1: Intelligence
  if (data.conversations && data.conversations.length > 0) {
    const activeConv = data.conversations[0];
    elements.metricMode.textContent = activeConv.mode;
    elements.badgeMode.textContent = activeConv.mode;
    elements.metricDeflections.textContent = `${activeConv.deflection_count} / 2`;
    elements.metricAbuse.textContent = `${activeConv.abuse_count} / 2`;
  }

  // Tab 2: Profile Projection Cards
  renderProfileField(elements.profFullName, p.full_name);
  renderProfileField(elements.profCurrentRole, p.current_role);
  renderProfileField(elements.profCurrentCompany, p.current_company);
  renderProfileField(elements.profExperienceYears, p.experience_years ? `${p.experience_years} years` : null);
  renderProfileField(elements.profCurrentCtc, p.current_ctc_annual ? `₹${p.current_ctc_annual.toLocaleString()} (${p.currency || 'INR'})` : null);
  renderProfileField(elements.profExpectedCtc, p.expected_ctc_annual ? `₹${p.expected_ctc_annual.toLocaleString()} (${p.currency || 'INR'})` : null);
  renderProfileField(elements.profNoticePeriod, p.notice_period_days ? `${p.notice_period_days} days` : null);
  renderProfileField(elements.profWorkMode, p.work_mode);
  renderProfileField(elements.profEducation, p.education_level);

  // Tab 3: Attribute Store
  renderAttributes(data.attributes);

  // Tab 4: Preferences
  renderPreferences(data.skills, data.role_prefs, data.location_prefs);
}

function renderProfileField(elem, value) {
  if (value !== null && value !== undefined && value !== '') {
    elem.textContent = value;
    elem.classList.add('filled');
  } else {
    elem.textContent = '-';
    elem.classList.remove('filled');
  }
}

// Render Attribute Store Table
function renderAttributes(attrs) {
  elements.attrCount.textContent = attrs.length;
  if (!attrs || attrs.length === 0) {
    elements.attrTableBody.innerHTML = '<tr><td colspan="6" class="empty-state">No attributes extracted yet</td></tr>';
    return;
  }

  let html = '';
  for (const a of attrs) {
    const valStr = typeof a.value === 'object' ? JSON.stringify(a.value) : String(a.value);
    html += `
      <tr>
        <td><code>${escapeHtml(a.key)}</code></td>
        <td><strong>${escapeHtml(valStr)}</strong></td>
        <td><span class="badge-source">${escapeHtml(a.source)}</span></td>
        <td><span class="badge-confidence">${escapeHtml(a.confidence)}</span></td>
        <td>${escapeHtml(a.status)}</td>
        <td><span class="badge-class ${escapeHtml(a.data_class)}">${escapeHtml(a.data_class)}</span></td>
      </tr>
    `;
  }
  elements.attrTableBody.innerHTML = html;
}

// Render Preferences Cloud
function renderPreferences(skills, roles, locations) {
  // Skills
  if (skills && skills.length > 0) {
    elements.skillsCloud.innerHTML = skills.map(s => `
      <span class="pref-tag">⚡ ${escapeHtml(s.skill_raw)} <small>(${s.confidence})</small></span>
    `).join('');
  } else {
    elements.skillsCloud.innerHTML = '<span class="empty-tag">No skills captured yet</span>';
  }

  // Roles
  if (roles && roles.length > 0) {
    elements.rolesCloud.innerHTML = roles.map(r => `
      <span class="pref-tag">💼 ${escapeHtml(r.role_raw)}</span>
    `).join('');
  } else {
    elements.rolesCloud.innerHTML = '<span class="empty-tag">No role preferences captured yet</span>';
  }

  // Locations
  if (locations && locations.length > 0) {
    elements.locationsCloud.innerHTML = locations.map(l => `
      <span class="pref-tag">📍 ${escapeHtml(l.location_raw)}</span>
    `).join('');
  } else {
    elements.locationsCloud.innerHTML = '<span class="empty-tag">No location preferences captured yet</span>';
  }
}

// Load FLOW-039 Backlog View
async function loadBacklog() {
  elements.backlogTableBody.innerHTML = '<tr><td colspan="6" class="empty-state">Querying backlog...</td></tr>';
  try {
    const res = await fetch('/api/simulator/backlog');
    if (!res.ok) throw new Error('Failed to load backlog');
    const data = await res.json();

    if (!data.items || data.items.length === 0) {
      elements.backlogTableBody.innerHTML = '<tr><td colspan="6" class="empty-state">No incomplete candidates in backlog.</td></tr>';
      return;
    }

    let html = '';
    for (const item of data.items) {
      const compPct = Math.round((item.completeness || 0) * 100);
      const isCurrent = item.candidate_id === state.candidateId;
      html += `
        <tr style="${isCurrent ? 'background: rgba(88, 166, 255, 0.08);' : ''}">
          <td>
            <strong>${escapeHtml(item.full_name || item.phone_number)}</strong>
            ${isCurrent ? ' <span class="badge-source">Active in Simulator</span>' : ''}
          </td>
          <td>${escapeHtml(item.current_role || '-')}</td>
          <td>
            <div style="display:flex; align-items:center; gap:6px;">
              <div class="progress-track" style="width:60px; height:4px;">
                <div class="progress-bar" style="width: ${compPct}%;"></div>
              </div>
              <span>${compPct}%</span>
            </div>
          </td>
          <td>${item.days_inactive}d ago</td>
          <td><strong style="color: var(--accent-cyan);">${item.value_score}</strong></td>
          <td><span class="badge badge-lifecycle">${item.lifecycle_status}</span></td>
        </tr>
      `;
    }
    elements.backlogTableBody.innerHTML = html;
  } catch (err) {
    console.error('Backlog load error:', err);
    elements.backlogTableBody.innerHTML = '<tr><td colspan="6" class="empty-state">Error loading backlog view.</td></tr>';
  }
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
