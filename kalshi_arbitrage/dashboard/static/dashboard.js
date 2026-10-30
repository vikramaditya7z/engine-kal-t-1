/**
 * Kalshi Arbitrage Engine Dashboard — Frontend V23 Live Client Script
 * Strictly reads from read-only backend API endpoints.
 * Integrates live continuous collector, evaluator, and V22 execution qualification.
 */

(function () {
  'use strict';

  // State Management
  let currentMode = 'live'; // 'live' or 'historical'
  let currentDataset = 'evidence_dashboard_trial';
  let activeTab = 'overview';
  let isSimulatedOpps = false;
  let autoRefreshTimer = null;
  let autoRefreshIntervalMs = 3000;
  let isRefreshing = false;

  // Cached Data
  let cachedLiveSummary = null;
  let cachedQualifiedCandidates = [];
  let cachedAllCandidates = [];
  let cachedHistoryRecords = [];
  let cachedMarkets = [];
  let marketFilter = 'ALL';
  let marketSearchQuery = '';

  // Candidate Filters
  const candidateFilters = {
    strategy: 'ALL',
    outcome: 'ALL',
    state: 'ALL',
    search: '',
    onlyQualified: false,
  };

  // DOM Elements
  const modeSelect = document.getElementById('mode-select');
  const datasetPickerWrap = document.getElementById('dataset-picker-wrap');
  const datasetSelect = document.getElementById('dataset-select');
  const provenanceBadge = document.getElementById('provenance-badge');
  const collectorStatusPill = document.getElementById('collector-status-pill');
  const collectorStatusText = document.getElementById('collector-status-text');
  const evaluatorStatusPill = document.getElementById('evaluator-status-pill');
  const evaluatorStatusText = document.getElementById('evaluator-status-text');
  const pipelineAlertBanner = document.getElementById('pipeline-alert-banner');
  const pipelineAlertText = document.getElementById('pipeline-alert-text');
  const alertDismissBtn = document.getElementById('alert-dismiss-btn');
  const refreshBtn = document.getElementById('refresh-btn');
  const autoRefreshToggle = document.getElementById('auto-refresh-toggle');
  const refreshIntervalSelect = document.getElementById('refresh-interval-select');
  const lastUiRefreshLabel = document.getElementById('last-ui-refresh');
  const navTabs = document.querySelectorAll('.tab-button');
  const tabPanes = document.querySelectorAll('.view-panel');
  const footerServerTime = document.getElementById('footer-server-time');

  // Initialization
  async function init() {
    setupEventListeners();
    await loadDatasets();
    restartAutoRefreshTimer();
    await refreshAllData();
  }

  // Event Listeners
  function setupEventListeners() {
    // Mode Switcher (Live vs Historical)
    if (modeSelect) {
      modeSelect.addEventListener('change', async (e) => {
        currentMode = e.target.value;
        if (currentMode === 'live') {
          datasetPickerWrap.classList.add('hidden');
          provenanceBadge.textContent = 'LIVE PIPELINE';
          provenanceBadge.className = 'provenance-tag tag-live';
        } else {
          datasetPickerWrap.classList.remove('hidden');
          provenanceBadge.textContent = 'HISTORICAL EVIDENCE';
          provenanceBadge.className = 'provenance-tag tag-historical';
        }
        await refreshAllData();
      });
    }

    // Dataset Selector (Historical Mode)
    if (datasetSelect) {
      datasetSelect.addEventListener('change', async (e) => {
        currentDataset = e.target.value;
        await refreshAllData();
      });
    }

    // Navigation Tabs
    navTabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        const targetTab = tab.getAttribute('data-tab');
        switchTab(targetTab);
      });
    });

    // Manual Refresh
    if (refreshBtn) {
      refreshBtn.addEventListener('click', async () => {
        await refreshAllData();
      });
    }

    // Auto-refresh Toggle & Cadence
    if (autoRefreshToggle) {
      autoRefreshToggle.addEventListener('change', () => {
        restartAutoRefreshTimer();
      });
    }
    if (refreshIntervalSelect) {
      refreshIntervalSelect.addEventListener('change', (e) => {
        autoRefreshIntervalMs = parseInt(e.target.value, 10) || 3000;
        restartAutoRefreshTimer();
      });
    }

    // Page Visibility (Pause polling when tab is hidden)
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        if (autoRefreshTimer) {
          clearInterval(autoRefreshTimer);
          autoRefreshTimer = null;
        }
      } else {
        refreshAllData();
        restartAutoRefreshTimer();
      }
    });

    // Alert Dismiss
    if (alertDismissBtn && pipelineAlertBanner) {
      alertDismissBtn.addEventListener('click', () => {
        pipelineAlertBanner.classList.add('hidden');
      });
    }

    // Funnel Breakdown Toggle
    const toggleFunnelBtn = document.getElementById('toggle-funnel-btn');
    const funnelBody = document.getElementById('funnel-breakdown-body');
    if (toggleFunnelBtn && funnelBody) {
      toggleFunnelBtn.addEventListener('click', () => {
        if (funnelBody.classList.contains('hidden')) {
          funnelBody.classList.remove('hidden');
          toggleFunnelBtn.textContent = 'Collapse';
        } else {
          funnelBody.classList.add('hidden');
          toggleFunnelBtn.textContent = 'Expand';
        }
      });
    }

    // Candidate Search & Filters
    const candSearchInput = document.getElementById('candidate-search');
    if (candSearchInput) {
      candSearchInput.addEventListener('input', (e) => {
        candidateFilters.search = e.target.value.toLowerCase().trim();
        renderCandidatesTable();
      });
    }

    const filterStrat = document.getElementById('filter-strategy');
    if (filterStrat) {
      filterStrat.addEventListener('change', (e) => {
        candidateFilters.strategy = e.target.value;
        renderCandidatesTable();
      });
    }

    const filterOut = document.getElementById('filter-outcome');
    if (filterOut) {
      filterOut.addEventListener('change', (e) => {
        candidateFilters.outcome = e.target.value;
        renderCandidatesTable();
      });
    }

    const filterSt = document.getElementById('filter-state');
    if (filterSt) {
      filterSt.addEventListener('change', (e) => {
        candidateFilters.state = e.target.value;
        renderCandidatesTable();
      });
    }

    const filterQualOnly = document.getElementById('filter-only-qualified');
    if (filterQualOnly) {
      filterQualOnly.addEventListener('change', (e) => {
        candidateFilters.onlyQualified = e.target.checked;
        renderCandidatesTable();
      });
    }

    // Breadth Scanner Filters (Phase 2E)
    const bscanFilterStrat = document.getElementById('bscan-filter-strat');
    if (bscanFilterStrat) {
      bscanFilterStrat.addEventListener('change', () => {
        loadBreadthScannerData();
      });
    }

    const bscanFilterOutcome = document.getElementById('bscan-filter-outcome');
    if (bscanFilterOutcome) {
      bscanFilterOutcome.addEventListener('change', () => {
        loadBreadthScannerData();
      });
    }

    // Candidate History Toggle
    const toggleHistBtn = document.getElementById('toggle-history-btn');
    const histContainer = document.getElementById('history-container');
    if (toggleHistBtn && histContainer) {
      toggleHistBtn.addEventListener('click', () => {
        if (histContainer.classList.contains('hidden')) {
          histContainer.classList.remove('hidden');
          toggleHistBtn.textContent = 'Collapse';
        } else {
          histContainer.classList.add('hidden');
          toggleHistBtn.textContent = 'Expand';
        }
      });
    }

    // Market Filters
    const filterBtns = document.querySelectorAll('.filter-btn');
    filterBtns.forEach((btn) => {
      btn.addEventListener('click', () => {
        filterBtns.forEach((b) => b.classList.remove('active'));
        btn.classList.add('active');
        marketFilter = btn.getAttribute('data-filter');
        renderMarketsTable();
      });
    });

    const marketSearchInput = document.getElementById('market-search');
    if (marketSearchInput) {
      marketSearchInput.addEventListener('input', (e) => {
        marketSearchQuery = e.target.value.toLowerCase().trim();
        renderMarketsTable();
      });
    }

    // Historical Simulation Segmented Toggle
    const btnOppReal = document.getElementById('btn-opp-real');
    const btnOppSim = document.getElementById('btn-opp-sim');
    if (btnOppReal && btnOppSim) {
      btnOppReal.addEventListener('click', async () => {
        if (isSimulatedOpps) {
          isSimulatedOpps = false;
          btnOppReal.classList.add('active');
          btnOppSim.classList.remove('active');
          document.getElementById('simulated-warning-banner').classList.add('hidden');
          await loadHistoricalOpportunities();
        }
      });

      btnOppSim.addEventListener('click', async () => {
        if (!isSimulatedOpps) {
          isSimulatedOpps = true;
          btnOppSim.classList.add('active');
          btnOppReal.classList.remove('active');
          document.getElementById('simulated-warning-banner').classList.remove('hidden');
          await loadHistoricalOpportunities();
        }
      });
    }

    // Raw V7 Report Toggle
    const toggleReportBtn = document.getElementById('toggle-full-report-btn');
    const rawReportPre = document.getElementById('raw-v7-report');
    if (toggleReportBtn && rawReportPre) {
      toggleReportBtn.addEventListener('click', () => {
        if (rawReportPre.classList.contains('hidden')) {
          rawReportPre.classList.remove('hidden');
          toggleReportBtn.textContent = 'Hide complete audit report';
        } else {
          rawReportPre.classList.add('hidden');
          toggleReportBtn.textContent = 'View complete 10-section audit report';
        }
      });
    }

    // Modals Close
    const closeCandModalBtn = document.getElementById('modal-cand-close-btn');
    if (closeCandModalBtn) {
      closeCandModalBtn.addEventListener('click', closeCandidateModal);
    }
    const candModal = document.getElementById('candidate-modal');
    if (candModal) {
      candModal.addEventListener('click', (e) => {
        if (e.target.id === 'candidate-modal') closeCandidateModal();
      });
    }

    const closeMktModalBtn = document.getElementById('modal-close-btn');
    if (closeMktModalBtn) {
      closeMktModalBtn.addEventListener('click', closeMarketModal);
    }
    const mktModal = document.getElementById('market-modal');
    if (mktModal) {
      mktModal.addEventListener('click', (e) => {
        if (e.target.id === 'market-modal') closeMarketModal();
      });
    }
  }

  function restartAutoRefreshTimer() {
    if (autoRefreshTimer) {
      clearInterval(autoRefreshTimer);
      autoRefreshTimer = null;
    }
    if (autoRefreshToggle && autoRefreshToggle.checked && !document.hidden) {
      autoRefreshTimer = setInterval(refreshAllData, autoRefreshIntervalMs);
    }
  }

  function switchTab(tabId) {
    activeTab = tabId;
    navTabs.forEach((t) => {
      t.classList.toggle('active', t.getAttribute('data-tab') === tabId);
    });
    tabPanes.forEach((p) => {
      p.classList.toggle('active', p.id === `tab-${tabId}`);
    });
  }

  // API Call Helpers
  async function fetchJson(endpoint) {
    try {
      const response = await fetch(endpoint);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}: ${response.statusText}`);
      }
      return await response.json();
    } catch (err) {
      console.warn(`API Error on ${endpoint}:`, err);
      return null;
    }
  }

  // Load Datasets List
  async function loadDatasets() {
    const data = await fetchJson('/api/datasets');
    if (!data || !data.datasets || !datasetSelect) return;

    datasetSelect.innerHTML = '';
    data.datasets.forEach((ds) => {
      const opt = document.createElement('option');
      opt.value = ds.id;
      opt.textContent = `${ds.name} (${ds.record_count} records)`;
      if (ds.id === currentDataset || (ds.is_default && !currentDataset)) {
        opt.selected = true;
        currentDataset = ds.id;
      }
      datasetSelect.appendChild(opt);
    });
  }

  // Master Refresh
  async function refreshAllData() {
    if (isRefreshing) return; // Prevent overlapping requests
    isRefreshing = true;

    try {
      const nowIso = new Date().toISOString();
      const timeStr = nowIso.substring(11, 19);
      if (lastUiRefreshLabel) {
        lastUiRefreshLabel.textContent = `UI: ${timeStr} UTC`;
      }
      if (footerServerTime) {
        footerServerTime.textContent = `Server UTC: ${timeStr}`;
      }

      // Always load live pipeline summary to update header status
      await loadLivePipelineSummary();

      if (currentMode === 'live') {
        await Promise.all([
          loadLiveQualifiedOpportunities(),
          loadLiveCandidates(),
          loadLiveHistory(),
          loadLiveMarkets(),
          loadLiveAlerts(),
          loadBreadthScannerData(),
          loadHealth(),
        ]);
      } else {
        await Promise.all([
          loadHistoricalOverview(),
          loadHistoricalMarkets(),
          loadHistoricalOpportunities(),
          loadHistoricalAudit(),
          loadBreadthScannerData(),
          loadHealth(),
        ]);
      }
    } finally {
      isRefreshing = false;
    }
  }

  // ---------------------------------------------------------------------------
  // Live Pipeline Summary & Header Status
  // ---------------------------------------------------------------------------

  async function loadLivePipelineSummary() {
    const data = await fetchJson('/api/live/summary');
    if (!data) return;
    cachedLiveSummary = data;

    const coll = data.collector || {};
    const evalState = data.evaluator || {};

    // 1. Collector Status Pill
    if (collectorStatusPill && collectorStatusText) {
      collectorStatusPill.className = 'status-indicator';
      if (coll.status === 'RUNNING') {
        collectorStatusPill.classList.add('status-running');
        collectorStatusText.textContent = `Collector: Running (PID ${coll.process_pid || coll.pid || '—'})`;
      } else if (coll.status === 'DEGRADED') {
        collectorStatusPill.classList.add('status-unknown');
        collectorStatusText.textContent = `Collector: Degraded`;
      } else {
        collectorStatusPill.classList.add('status-stopped');
        collectorStatusText.textContent = `Collector: Stopped`;
      }
    }

    // 2. Evaluator Status Pill
    if (evaluatorStatusPill && evaluatorStatusText) {
      evaluatorStatusPill.className = 'status-indicator';
      if (evalState.status === 'RUNNING') {
        evaluatorStatusPill.classList.add('status-running');
        evaluatorStatusText.textContent = `Evaluator: Running (PID ${evalState.process_pid || evalState.pid || '—'})`;
      } else if (evalState.status === 'DEGRADED') {
        evaluatorStatusPill.classList.add('status-unknown');
        evaluatorStatusText.textContent = `Evaluator: Degraded`;
      } else {
        evaluatorStatusPill.classList.add('status-stopped');
        evaluatorStatusText.textContent = `Evaluator: Stopped`;
      }
    }

    // 2b. Alert Engine Status Pill (V24)
    const alertState = data.alerts || {};
    const alertPill = document.getElementById('alert-status-pill');
    const alertText = document.getElementById('alert-status-text');
    if (alertPill && alertText) {
      alertPill.className = 'status-indicator';
      if (alertState.status === 'RUNNING') {
        alertPill.classList.add('status-running');
        alertText.textContent = `Alerts: Active (${alertState.alerts_delivered_count || 0} sent)`;
      } else if (alertState.status === 'DISABLED') {
        alertPill.classList.add('status-stopped');
        alertText.textContent = `Alerts: Disabled`;
      } else {
        alertPill.classList.add('status-stopped');
        alertText.textContent = `Alerts: Stopped`;
      }
    }

    // 3. Provenance Badge Update
    if (currentMode === 'live') {
      if (coll.status === 'RUNNING' && evalState.status === 'RUNNING' && coll.is_data_fresh) {
        provenanceBadge.textContent = 'LIVE PIPELINE';
        provenanceBadge.className = 'provenance-tag tag-live';
      } else if (coll.status === 'RUNNING' || evalState.status === 'RUNNING') {
        provenanceBadge.textContent = 'LIVE (STALE / DEGRADED)';
        provenanceBadge.className = 'provenance-tag tag-live stale';
      } else {
        provenanceBadge.textContent = 'LIVE INACTIVE';
        provenanceBadge.className = 'provenance-tag tag-historical';
      }
    }

    // 4. Alert Banner logic
    if (pipelineAlertBanner && pipelineAlertText) {
      const isCollRunning = coll.status === 'RUNNING';
      const isEvalRunning = evalState.status === 'RUNNING';
      const isFresh = coll.is_data_fresh;
      const snapAge = coll.latest_snapshot_age_seconds;

      if (!isCollRunning && !isEvalRunning) {
        pipelineAlertBanner.className = 'alert-banner banner-warning';
        pipelineAlertText.textContent = 'Continuous collector and evaluator services are currently stopped. Displaying last persisted state.';
        pipelineAlertBanner.classList.remove('hidden');
      } else if (!isCollRunning) {
        pipelineAlertBanner.className = 'alert-banner banner-warning';
        pipelineAlertText.textContent = 'Continuous live collector is stopped. Evaluator has no incoming live stream.';
        pipelineAlertBanner.classList.remove('hidden');
      } else if (!isEvalRunning) {
        pipelineAlertBanner.className = 'alert-banner banner-warning';
        pipelineAlertText.textContent = 'Continuous evaluator is stopped. New live observations are not being evaluated for arbitrage.';
        pipelineAlertBanner.classList.remove('hidden');
      } else if (!isFresh && snapAge !== null && snapAge > 60.0) {
        pipelineAlertBanner.className = 'alert-banner banner-warning';
        pipelineAlertText.textContent = `Market data is stale (age: ${snapAge.toFixed(1)}s > 60.0s). Static resting books may not reflect current exchange pricing.`;
        pipelineAlertBanner.classList.remove('hidden');
      } else if (data.recent_errors && data.recent_errors.length > 0) {
        pipelineAlertBanner.className = 'alert-banner banner-danger';
        pipelineAlertText.textContent = data.recent_errors.join(' | ');
        pipelineAlertBanner.classList.remove('hidden');
      } else {
        pipelineAlertBanner.classList.add('hidden');
      }
    }

    // 5. Update Live Overview KPI Cards
    renderLiveKpis(data);
  }

  function renderLiveKpis(data) {
    const coll = data.collector || {};
    const evalState = data.evaluator || {};

    // 1. Collector Card
    const collBadge = document.getElementById('kpi-collector-badge');
    if (collBadge) {
      collBadge.textContent = coll.status || 'STOPPED';
      collBadge.className = `badge-status-pill ${coll.status === 'RUNNING' ? 'badge-running' : 'badge-stopped'}`;
    }
    const collMkts = document.getElementById('kpi-collector-markets');
    if (collMkts) {
      collMkts.textContent = coll.active_monitored_markets_count || 0;
    }
    const legacyMkts = document.getElementById('kpi-markets-count');
    if (legacyMkts) {
      legacyMkts.textContent = coll.active_monitored_markets_count || 0;
    }
    const collSub = document.getElementById('kpi-collector-sub');
    if (collSub) {
      collSub.textContent = `Poll interval: ${coll.poll_interval_seconds || 2.0}s`;
    }

    // 2. Evaluator Card
    const evalBadge = document.getElementById('kpi-evaluator-badge');
    if (evalBadge) {
      evalBadge.textContent = evalState.status || 'STOPPED';
      evalBadge.className = `badge-status-pill ${evalState.status === 'RUNNING' ? 'badge-running' : 'badge-stopped'}`;
    }
    const evalObs = document.getElementById('kpi-evaluator-obs');
    if (evalObs) {
      evalObs.textContent = (evalState.processed_observations_count || 0).toLocaleString();
    }
    const legacyObs = document.getElementById('kpi-total-obs');
    if (legacyObs) {
      legacyObs.textContent = (evalState.processed_observations_count || 0).toLocaleString();
    }
    const evalSub = document.getElementById('kpi-evaluator-sub');
    if (evalSub) {
      evalSub.textContent = `${evalState.evaluations_count || 0} evaluation cycles`;
    }

    // 3. Data Freshness Card
    const freshAge = document.getElementById('kpi-freshness-age');
    const freshPill = document.getElementById('kpi-freshness-pill');
    const freshTs = document.getElementById('kpi-freshness-ts');

    const ageSec = coll.latest_snapshot_age_seconds;
    if (freshAge) {
      if (ageSec === null || ageSec === undefined) {
        freshAge.textContent = 'None';
      } else if (ageSec < 60) {
        freshAge.textContent = `${ageSec.toFixed(1)}s ago`;
      } else {
        freshAge.textContent = `${(ageSec / 60).toFixed(1)}m ago`;
      }
    }
    if (freshPill) {
      if (coll.is_data_fresh) {
        freshPill.textContent = 'FRESH';
        freshPill.className = 'badge-status-pill badge-running';
      } else if (ageSec !== null && ageSec > 60.0) {
        freshPill.textContent = 'STALE';
        freshPill.className = 'badge-status-pill badge-degraded';
      } else {
        freshPill.textContent = 'NO STREAM';
        freshPill.className = 'badge-status-pill badge-neutral';
      }
    }
    if (freshTs) {
      freshTs.textContent = coll.latest_snapshot_timestamp
        ? `Snapshot: ${coll.latest_snapshot_timestamp.substring(11, 19)} UTC`
        : 'Snapshot: None';
    }

    // 4. Active Candidates Card
    const activeCandEl = document.getElementById('kpi-active-candidates');
    if (activeCandEl) {
      activeCandEl.textContent = evalState.active_candidates_count || 0;
    }
    const totalCandEl = document.getElementById('kpi-total-candidates');
    if (totalCandEl) {
      totalCandEl.textContent = `${evalState.total_candidates_detected || 0} detected in session`;
    }

    // 5. Qualified Count Card
    const qualCountEl = document.getElementById('kpi-qualified-count');
    if (qualCountEl) {
      qualCountEl.textContent = evalState.qualified_candidates_count || 0;
    }
    const legacyQual = document.getElementById('kpi-opps-qualified');
    if (legacyQual) {
      legacyQual.textContent = evalState.qualified_candidates_count || 0;
    }

    // 6. Rejected Count Card
    const rejCountEl = document.getElementById('kpi-rejected-count');
    if (rejCountEl) {
      rejCountEl.textContent = evalState.rejected_candidates_count || 0;
    }

    // Funnel Breakdown Items
    const outcomes = evalState.qualification_summary?.outcomes_breakdown || {};
    document.getElementById('funnel-obs').textContent = evalState.processed_observations_count || 0;
    document.getElementById('funnel-detected').textContent = evalState.total_candidates_detected || 0;
    document.getElementById('funnel-depth-rej').textContent = outcomes['REJECTED_INSUFFICIENT_DEPTH'] || 0;
    document.getElementById('funnel-fee-rej').textContent = outcomes['REJECTED_UNPROFITABLE_FEES'] || 0;
    document.getElementById('funnel-edge-rej').textContent = outcomes['REJECTED_NO_GROSS_EDGE'] || 0;
    document.getElementById('funnel-timing-rej').textContent = (outcomes['REJECTED_STALE'] || 0) + (outcomes['REJECTED_LEG_SKEW'] || 0);
    document.getElementById('funnel-qualified').textContent = evalState.qualified_candidates_count || 0;
  }

  // ---------------------------------------------------------------------------
  // Live Qualified Opportunities (V22)
  // ---------------------------------------------------------------------------

  async function loadLiveQualifiedOpportunities() {
    const data = await fetchJson('/api/evaluator/qualified');
    if (!data) return;

    cachedQualifiedCandidates = data.qualified_candidates || [];
    renderLiveQualifiedTable(cachedQualifiedCandidates);
  }

  function renderLiveQualifiedTable(candidates) {
    const tbody = document.getElementById('qualified-tbody');
    if (!tbody) return;

    if (!candidates || candidates.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="12" class="table-empty">
            <div class="calm-empty-state">
              <div class="empty-title">Zero Qualified Arbitrage Opportunities</div>
              <div class="empty-description">
                The live market order books currently exhibit non-arbitrage pricing or insufficient counterparty liquidity.
                The V22 qualification engine enforces multi-level depth traversal, multi-leg bottleneck alignment, and CFTC quadratic taker fees (fail-closed integrity).
              </div>
            </div>
          </td>
        </tr>
      `;
      return;
    }

    tbody.innerHTML = candidates.map((cand) => {
      const q = cand.qualification || {};
      const candId = cand.candidate_id || q.candidate_id || '—';
      const strat = cand.strategy_type || q.strategy_type || 'binary_parity';
      const markets = (cand.market_tickers || [cand.market_ticker]).filter(Boolean).join(', ');

      const reqQty = q.requested_quantity || cand.target_quantity || 1;
      const execQty = q.executable_quantity || cand.supported_quantity || 0;
      const qtyStr = q.is_partial_fill ? `${execQty} / ${reqQty} (Partial)` : `${execQty}`;

      const costCents = q.total_cost_cents ?? cand.total_cost_cents;
      const payoutCents = q.guaranteed_payout_cents ?? cand.guaranteed_payout_cents;
      const grossCents = q.gross_edge_cents ?? cand.gross_edge_cents;
      const feeCents = q.total_fees_cents ?? cand.estimated_fees_cents;
      const netCents = q.net_edge_cents ?? cand.net_edge_cents;

      const rocPct = q.return_on_capital_pct != null ? `${parseFloat(q.return_on_capital_pct).toFixed(2)}%` : '—';
      const freshness = q.data_freshness_seconds != null ? `${q.data_freshness_seconds.toFixed(1)}s` : 'Fresh';

      return `
        <tr>
          <td class="font-mono font-bold" style="color: var(--accent);">${escapeHtml(candId)}</td>
          <td><span style="font-size: 11px; color: var(--accent); font-weight: 500;">${escapeHtml(formatStrategy(strat))}</span></td>
          <td class="font-mono font-bold" style="font-size: 11px;">${escapeHtml(markets)}</td>
          <td class="font-mono">${qtyStr}</td>
          <td class="font-mono">${costCents != null ? `${costCents}¢ ($${(costCents / 100).toFixed(2)})` : '—'}</td>
          <td class="font-mono">${payoutCents != null ? `${payoutCents}¢ ($${(payoutCents / 100).toFixed(2)})` : '—'}</td>
          <td class="font-mono" style="color: var(--status-green); font-weight: 600;">+${grossCents}¢</td>
          <td class="font-mono text-muted">${feeCents != null ? `${feeCents}¢` : '0¢'}</td>
          <td class="font-mono font-bold" style="color: var(--status-green); font-size: 13px;">+${netCents}¢</td>
          <td class="font-mono" style="color: var(--status-green); font-weight: 600;">${rocPct}</td>
          <td class="font-mono text-muted" style="font-size: 11px;">${freshness}</td>
          <td>
            <button class="btn btn-secondary" onclick="window.inspectCandidate('${escapeHtml(candId)}')">Inspect</button>
          </td>
        </tr>
      `;
    }).join('');
  }

  // ---------------------------------------------------------------------------
  // Candidate Monitoring & Rejection Analysis
  // ---------------------------------------------------------------------------

  async function loadLiveCandidates() {
    const data = await fetchJson('/api/evaluator/candidates');
    if (!data) return;

    cachedAllCandidates = data.candidates || [];
    renderCandidatesTable();
  }

  function renderCandidatesTable() {
    const tbody = document.getElementById('candidates-tbody');
    if (!tbody) return;

    let list = cachedAllCandidates;

    // Filters
    if (candidateFilters.strategy !== 'ALL') {
      list = list.filter((c) => (c.strategy_type || c.qualification?.strategy_type) === candidateFilters.strategy);
    }
    if (candidateFilters.state !== 'ALL') {
      list = list.filter((c) => c.state === candidateFilters.state);
    }
    if (candidateFilters.outcome !== 'ALL') {
      list = list.filter((c) => {
        const out = c.qualification?.outcome || c.status;
        return out === candidateFilters.outcome;
      });
    }
    if (candidateFilters.onlyQualified) {
      list = list.filter((c) => c.is_qualified === true || c.qualification?.is_qualified === true);
    }
    if (candidateFilters.search) {
      const q = candidateFilters.search;
      list = list.filter((c) => {
        const idMatch = String(c.candidate_id || '').toLowerCase().includes(q);
        const tickerMatch = String(c.market_ticker || '').toLowerCase().includes(q);
        const tickersMatch = Array.isArray(c.market_tickers) && c.market_tickers.some((t) => String(t).toLowerCase().includes(q));
        return idMatch || tickerMatch || tickersMatch;
      });
    }

    if (list.length === 0) {
      tbody.innerHTML = `<tr><td colspan="11" class="table-empty">No candidates match current filters.</td></tr>`;
      return;
    }

    tbody.innerHTML = list.map((cand) => {
      const q = cand.qualification || {};
      const candId = cand.candidate_id || q.candidate_id || '—';
      const strat = cand.strategy_type || q.strategy_type || '—';
      const state = cand.state || '—';
      const outcome = q.outcome || cand.status || 'UNKNOWN';
      const markets = (cand.market_tickers || [cand.market_ticker]).filter(Boolean).join(', ');

      const reqQty = q.requested_quantity || cand.target_quantity || 1;
      const execQty = q.executable_quantity != null ? q.executable_quantity : (cand.supported_quantity != null ? cand.supported_quantity : 0);
      const gross = q.gross_edge_cents != null ? `${q.gross_edge_cents}¢` : (cand.gross_edge_cents != null ? `${cand.gross_edge_cents}¢` : '—');
      const net = q.net_edge_cents != null ? `${q.net_edge_cents}¢` : (cand.net_edge_cents != null ? `${cand.net_edge_cents}¢` : '—');
      const reason = q.rejection_reason || cand.rejection_reason || (q.is_qualified ? 'Verified Depth & Net Profit' : 'Evaluated');
      const timeStr = (cand.updated_at || cand.detected_at || q.evaluated_at || '').substring(11, 19);

      // Outcome badge class
      let outcomeClass = 'tag-outcome-neutral';
      if (outcome === 'QUALIFIED') outcomeClass = 'tag-outcome-qual';
      else if (outcome === 'REJECTED_STALE' || outcome === 'REJECTED_LEG_SKEW') outcomeClass = 'tag-outcome-stale';
      else if (outcome.startsWith('REJECTED_')) outcomeClass = 'tag-outcome-rej';

      // State tag class
      let stateClass = 'tag-state-new';
      if (state === 'UPDATED') stateClass = 'tag-state-updated';
      else if (state === 'INVALIDATED') stateClass = 'tag-state-invalidated';
      else if (state === 'DISQUALIFIED') stateClass = 'tag-state-disqualified';
      else if (state === 'DISAPPEARED') stateClass = 'tag-state-disappeared';

      return `
        <tr>
          <td class="font-mono font-bold" style="color: var(--accent);">${escapeHtml(candId)}</td>
          <td><span style="font-size: 11px;">${escapeHtml(formatStrategy(strat))}</span></td>
          <td><span class="tag-state ${stateClass}">${escapeHtml(state)}</span></td>
          <td><span class="tag-outcome ${outcomeClass}">${escapeHtml(outcome)}</span></td>
          <td class="font-mono" style="font-size: 11px;">${escapeHtml(markets)}</td>
          <td class="font-mono">${execQty} / ${reqQty}</td>
          <td class="font-mono">${gross}</td>
          <td class="font-mono font-bold">${net}</td>
          <td style="color: var(--text-secondary); font-size: 11px; max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(reason)}">
            ${escapeHtml(reason)}
          </td>
          <td class="font-mono text-muted" style="font-size: 11px;">${timeStr ? timeStr + ' UTC' : '—'}</td>
          <td>
            <button class="btn btn-secondary" onclick="window.inspectCandidate('${escapeHtml(candId)}')">Inspect</button>
          </td>
        </tr>
      `;
    }).join('');
  }

  // ---------------------------------------------------------------------------
  // Candidate Lifecycle Transition History Log
  // ---------------------------------------------------------------------------

  async function loadLiveHistory() {
    const data = await fetchJson('/api/evaluator/history?limit=100');
    if (!data || !data.history) return;

    cachedHistoryRecords = data.history;
    renderCandidateHistoryTable(cachedHistoryRecords);
  }

  function renderCandidateHistoryTable(records) {
    const tbody = document.getElementById('history-tbody');
    if (!tbody) return;

    if (!records || records.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="table-empty">No candidate transitions logged yet.</td></tr>`;
      return;
    }

    tbody.innerHTML = records.map((rec) => {
      const timeStr = (rec.transitioned_at || '').substring(11, 19) + ' UTC';
      const fromSt = rec.from_state || 'NONE';
      const toSt = rec.to_state || '—';
      const outcome = rec.qualification_outcome || '—';
      const gross = rec.gross_edge_cents != null ? `${rec.gross_edge_cents}¢` : '—';
      const net = rec.net_edge_cents != null ? `${rec.net_edge_cents}¢` : '—';
      const reason = rec.reason || '—';

      return `
        <tr>
          <td class="font-mono text-muted" style="font-size: 11px;">${timeStr}</td>
          <td class="font-mono font-bold" style="color: var(--accent);">${escapeHtml(rec.candidate_id)}</td>
          <td><span class="font-mono font-bold">${escapeHtml(fromSt)}</span> &rarr; <span class="font-mono font-bold" style="color: var(--accent);">${escapeHtml(toSt)}</span></td>
          <td><span class="tag-outcome ${outcome === 'QUALIFIED' ? 'tag-outcome-qual' : 'tag-outcome-neutral'}">${escapeHtml(outcome)}</span></td>
          <td class="font-mono">${gross} / ${net}</td>
          <td style="color: var(--text-secondary); font-size: 11px;">${escapeHtml(reason)}</td>
        </tr>
      `;
    }).join('');
  }

  // ---------------------------------------------------------------------------
  // Live Opportunity Alerts (V24)
  // ---------------------------------------------------------------------------

  async function loadLiveAlerts() {
    const data = await fetchJson('/api/alerts/history?limit=50');
    const tbody = document.getElementById('alerts-tbody');
    if (!tbody) return;

    if (!data || !data.alerts || data.alerts.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="table-empty">No opportunity alerts emitted yet.</td></tr>';
      return;
    }

    tbody.innerHTML = data.alerts.map((alt) => {
      const timeStr = alt.timestamp ? alt.timestamp.substring(11, 19) + ' UTC' : '—';
      const netStr = alt.net_edge_cents != null ? `${alt.net_edge_cents}¢` : '—';
      const qtyStr = alt.executable_quantity != null ? alt.executable_quantity : '—';

      let eventBadgeClass = 'badge-stopped';
      if (alt.event_type === 'NEW_QUALIFIED') eventBadgeClass = 'badge-running';
      else if (alt.event_type === 'MATERIAL_CHANGE') eventBadgeClass = 'badge-neutral';

      return `
        <tr>
          <td class="font-mono" style="font-size: 11px;">${escapeHtml(alt.alert_id)}</td>
          <td><span class="badge-status-pill ${eventBadgeClass}">${escapeHtml(alt.event_type)}</span></td>
          <td class="font-mono font-bold" style="color: var(--accent);">${escapeHtml(alt.candidate_id)}</td>
          <td class="font-mono" style="color: var(--accent); font-weight: 600;">${netStr}</td>
          <td class="font-mono">${qtyStr}</td>
          <td style="color: var(--text-secondary); font-size: 11px;">${escapeHtml(alt.message || '')}</td>
          <td class="font-mono text-muted" style="font-size: 11px;">${timeStr}</td>
        </tr>
      `;
    }).join('');
  }

  // ---------------------------------------------------------------------------
  // Candidate Inspection Modal
  // ---------------------------------------------------------------------------

  window.inspectCandidate = function (candidateId) {
    // Find candidate in qualified or all candidates
    const cand = cachedQualifiedCandidates.find(c => c.candidate_id === candidateId) ||
                 cachedAllCandidates.find(c => c.candidate_id === candidateId);

    if (!cand) return;

    const modal = document.getElementById('candidate-modal');
    const modalTitle = document.getElementById('modal-candidate-id');
    const modalBody = document.getElementById('modal-candidate-body');

    modalTitle.textContent = `Candidate Details: ${cand.candidate_id}`;

    const q = cand.qualification || {};
    const legs = q.leg_details || [];

    const legsHtml = legs.length > 0
      ? `
        <div class="summary-card" style="margin-top: 14px;">
          <h4 class="summary-card-title">Leg-by-Leg Execution & Liquidity Traversal</h4>
          <table class="data-table" style="margin-top: 8px;">
            <thead>
              <tr>
                <th>Leg Ticker</th>
                <th>Side</th>
                <th>Req / Exec</th>
                <th>Book Depth</th>
                <th>Top Price</th>
                <th>VWAP Price</th>
                <th>Leg Cost</th>
                <th>CFTC Fee</th>
              </tr>
            </thead>
            <tbody>
              ${legs.map(l => `
                <tr>
                  <td class="font-mono font-bold">${escapeHtml(l.market_ticker)}</td>
                  <td><span class="font-mono font-bold">${escapeHtml(l.side)}</span></td>
                  <td class="font-mono">${l.requested_quantity} / ${l.executable_quantity}</td>
                  <td class="font-mono">${l.available_depth_contracts} contracts</td>
                  <td class="font-mono">${l.top_of_book_price_cents != null ? l.top_of_book_price_cents + '¢' : '—'}</td>
                  <td class="font-mono">${l.average_price_cents != null ? parseFloat(l.average_price_cents).toFixed(1) + '¢' : '—'}</td>
                  <td class="font-mono font-bold">${l.total_cost_cents}¢</td>
                  <td class="font-mono text-muted">${l.fee_cents}¢</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        </div>
      `
      : `<div class="summary-card" style="margin-top: 14px;"><div class="summary-row"><span class="summary-key">Leg Details:</span><span class="summary-val text-muted">No multi-leg depth breakdown stored.</span></div></div>`;

    modalBody.innerHTML = `
      <div style="display: flex; flex-direction: column; gap: 14px;">
        <div class="summary-card">
          <h4 class="summary-card-title">Candidate Specification & Financial Summary</h4>
          <div class="summary-row"><span class="summary-key">Candidate ID:</span><span class="summary-val font-mono">${escapeHtml(cand.candidate_id)}</span></div>
          <div class="summary-row"><span class="summary-key">Strategy Type:</span><span class="summary-val font-mono">${escapeHtml(cand.strategy_type || q.strategy_type || '—')}</span></div>
          <div class="summary-row"><span class="summary-key">Qualification Outcome:</span><span class="summary-val font-mono font-bold" style="color: ${q.is_qualified ? 'var(--status-green)' : 'var(--status-red)'};">${escapeHtml(q.outcome || cand.status || 'UNKNOWN')}</span></div>
          <div class="summary-row"><span class="summary-key">Lifecycle State:</span><span class="summary-val font-mono">${escapeHtml(cand.state || '—')}</span></div>
          <div class="summary-row"><span class="summary-key">Guaranteed Payout:</span><span class="summary-val font-mono font-bold">${q.guaranteed_payout_cents != null ? q.guaranteed_payout_cents + '¢' : '—'}</span></div>
          <div class="summary-row"><span class="summary-key">Total Acquisition Cost:</span><span class="summary-val font-mono font-bold">${q.total_cost_cents != null ? q.total_cost_cents + '¢' : '—'}</span></div>
          <div class="summary-row"><span class="summary-key">Total CFTC Quadratic Fees:</span><span class="summary-val font-mono">${q.total_fees_cents != null ? q.total_fees_cents + '¢' : '—'}</span></div>
          <div class="summary-row"><span class="summary-key">Expected Net Profit:</span><span class="summary-val font-mono font-bold" style="color: ${q.is_qualified ? 'var(--status-green)' : 'var(--text-muted)'}; font-size: 14px;">${q.net_edge_cents != null ? q.net_edge_cents + '¢' : '—'}</span></div>
          <div class="summary-row"><span class="summary-key">Return on Capital:</span><span class="summary-val font-mono font-bold" style="color: var(--status-green);">${q.return_on_capital_pct != null ? parseFloat(q.return_on_capital_pct).toFixed(2) + '%' : '—'}</span></div>
          <div class="summary-row"><span class="summary-key">Data Freshness:</span><span class="summary-val font-mono">${q.data_freshness_seconds != null ? q.data_freshness_seconds.toFixed(2) + 's' : '—'}</span></div>
          <div class="summary-row"><span class="summary-key">Rejection / Invalidation Reason:</span><span class="summary-val" style="color: var(--status-amber);">${escapeHtml(q.rejection_reason || cand.rejection_reason || 'None (Passing)')}</span></div>
        </div>

        ${legsHtml}

        <div class="callout callout-subtle">
          <span class="callout-tag">Execution Disclaimers</span>
          <span>Qualification is evaluated against resting limit orders at observation time. Does not guarantee live matching engine queue priority, fill speed, or non-atomic multi-leg fills. Strictly read-only research interface.</span>
        </div>
      </div>
    `;

    modal.classList.remove('hidden');
  };

  function closeCandidateModal() {
    const modal = document.getElementById('candidate-modal');
    if (modal) modal.classList.add('hidden');
  }

  // ---------------------------------------------------------------------------
  // Live & Historical Markets
  // ---------------------------------------------------------------------------

  async function loadLiveMarkets() {
    const data = await fetchJson('/api/markets?dataset=live');
    if (!data || !data.markets) return;

    cachedMarkets = data.markets;
    updateMarketFilterCounts();
    renderMarketsTable();
  }

  async function loadHistoricalMarkets() {
    const data = await fetchJson(`/api/markets?dataset=${encodeURIComponent(currentDataset)}`);
    if (!data || !data.markets) return;

    cachedMarkets = data.markets;
    updateMarketFilterCounts();
    renderMarketsTable();
  }

  function updateMarketFilterCounts() {
    const countAll = document.getElementById('count-all');
    if (countAll) countAll.textContent = cachedMarkets.length;
    const countPop = document.getElementById('count-pop');
    if (countPop) countPop.textContent = cachedMarkets.filter(m => m.order_book_status === 'POPULATED').length;
    const countEmpty = document.getElementById('count-empty');
    if (countEmpty) countEmpty.textContent = cachedMarkets.filter(m => m.order_book_status === 'EMPTY').length;
    const countStale = document.getElementById('count-stale');
    if (countStale) countStale.textContent = cachedMarkets.filter(m => m.order_book_status === 'STALE').length;
  }

  function renderMarketsTable() {
    const tbody = document.getElementById('markets-tbody');
    if (!tbody) return;

    let list = cachedMarkets;
    if (marketFilter !== 'ALL') {
      list = list.filter(m => m.order_book_status === marketFilter);
    }
    if (marketSearchQuery) {
      list = list.filter(m =>
        m.ticker.toLowerCase().includes(marketSearchQuery) ||
        (m.event_ticker && m.event_ticker.toLowerCase().includes(marketSearchQuery)) ||
        (m.title && m.title.toLowerCase().includes(marketSearchQuery))
      );
    }

    if (list.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="table-empty">No markets match current filters.</td></tr>`;
      return;
    }

    tbody.innerHTML = list.map((m, idx) => {
      const statusLower = m.order_book_status ? m.order_book_status.toLowerCase() : 'empty';
      let statusLabel = m.order_book_status;
      if (m.order_book_status === 'EMPTY') statusLabel = 'Empty (0 bids)';
      else if (m.order_book_status === 'POPULATED') statusLabel = 'Populated';
      else if (m.order_book_status === 'STALE') statusLabel = 'Stale';

      const yesBid = formatPrice(m.yes_bid_dollars);
      const yesAsk = formatPrice(m.yes_ask_dollars);
      const noBid = formatPrice(m.no_bid_dollars);
      const noAsk = formatPrice(m.no_ask_dollars);
      const timeStr = m.latest_observed_at ? m.latest_observed_at.substring(11, 19) + ' UTC' : '—';

      return `
        <tr>
          <td>
            <div class="font-mono font-bold" style="color: var(--accent);">${escapeHtml(m.ticker)}</div>
            <div style="color: var(--text-secondary); font-size: 11px; margin-top: 2px; max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(m.title)}">
              ${escapeHtml(m.title)}
            </div>
          </td>
          <td class="font-mono">${yesBid} / ${yesAsk}</td>
          <td class="font-mono">${noBid} / ${noAsk}</td>
          <td>
            <span class="table-status">
              <span class="status-dot-${statusLower}"></span>
              <span>${statusLabel}</span>
            </span>
          </td>
          <td class="font-mono text-muted" style="font-size: 11px;">${timeStr}</td>
          <td>
            <button class="btn btn-secondary" onclick="window.inspectMarket(${idx})">Inspect</button>
          </td>
        </tr>
      `;
    }).join('');
  }

  window.inspectMarket = function (index) {
    const m = cachedMarkets[index];
    if (!m) return;

    document.getElementById('modal-market-ticker').textContent = `Market: ${m.ticker}`;
    const modalBody = document.getElementById('modal-market-body');

    modalBody.innerHTML = `
      <div style="display: flex; flex-direction: column; gap: 14px;">
        <div class="summary-card">
          <h4 class="summary-card-title">Contract Specification</h4>
          <div class="summary-row"><span class="summary-key">Event Ticker:</span><span class="summary-val font-mono">${escapeHtml(m.event_ticker)}</span></div>
          <div class="summary-row"><span class="summary-key">Title:</span><span class="summary-val">${escapeHtml(m.title)}</span></div>
          <div class="summary-row"><span class="summary-key">Status:</span><span class="summary-val">${m.status}</span></div>
          <div class="summary-row"><span class="summary-key">Volume:</span><span class="summary-val font-mono">${m.volume}</span></div>
          <div class="summary-row"><span class="summary-key">Last Price:</span><span class="summary-val font-mono">${m.last_price_dollars ? '$' + m.last_price_dollars : 'None'}</span></div>
        </div>

        <div class="summary-card">
          <h4 class="summary-card-title">Order Book Depth Snapshot</h4>
          <div class="summary-row"><span class="summary-key">Classification:</span><span class="summary-val font-mono">${m.order_book_status}</span></div>
          <div class="summary-row"><span class="summary-key">Yes Resting Bids:</span><span class="summary-val font-mono">${m.yes_bids_levels_count} levels</span></div>
          <div class="summary-row"><span class="summary-key">No Resting Bids:</span><span class="summary-val font-mono">${m.no_bids_levels_count} levels</span></div>
          <div class="summary-row"><span class="summary-key">Best Yes Bid:</span><span class="summary-val font-mono">${m.top_yes_bid ? '$' + m.top_yes_bid.price_dollars + ' (Qty: ' + m.top_yes_bid.quantity + ')' : 'None (0 bids)'}</span></div>
          <div class="summary-row"><span class="summary-key">Best No Bid:</span><span class="summary-val font-mono">${m.top_no_bid ? '$' + m.top_no_bid.price_dollars + ' (Qty: ' + m.top_no_bid.quantity + ')' : 'None (0 bids)'}</span></div>
        </div>

        <div class="summary-card">
          <h4 class="summary-card-title">Provenance & Timestamps</h4>
          <div class="summary-row"><span class="summary-key">First Observed:</span><span class="summary-val font-mono">${m.first_observed_at || 'N/A'}</span></div>
          <div class="summary-row"><span class="summary-key">Latest Observed:</span><span class="summary-val font-mono">${m.latest_observed_at || 'N/A'}</span></div>
          <div class="summary-row"><span class="summary-key">Exchange Timestamp:</span><span class="summary-val font-mono">${m.latest_source_timestamp || 'None'}</span></div>
          <div class="summary-row"><span class="summary-key">Freshness:</span><span class="summary-val">${m.staleness_reason ? 'Stale: ' + m.staleness_reason : 'Fresh (within limits)'}</span></div>
        </div>
      </div>
    `;

    document.getElementById('market-modal').classList.remove('hidden');
  };

  function closeMarketModal() {
    document.getElementById('market-modal').classList.add('hidden');
  }

  // ---------------------------------------------------------------------------
  // Historical Mode Handlers
  // ---------------------------------------------------------------------------

  async function loadHistoricalOverview() {
    const data = await fetchJson(`/api/overview?dataset=${encodeURIComponent(currentDataset)}`);
    if (!data) return;

    // Overwrite overview metrics for historical replay
    document.getElementById('kpi-collector-markets').textContent = data.unique_markets_count || 0;
    document.getElementById('kpi-collector-sub').textContent = `${data.unique_events_count || 0} unique events`;

    document.getElementById('kpi-evaluator-obs').textContent = (data.valid_observations_count || 0).toLocaleString();
    document.getElementById('kpi-evaluator-sub').textContent = `Historical dataset: ${currentDataset}`;

    document.getElementById('kpi-freshness-age').textContent = data.latest_observation_age || 'Historical';
    document.getElementById('kpi-freshness-ts').textContent = data.latest_observation_at
      ? `Recorded ${data.latest_observation_at.substring(11, 19)} UTC`
      : 'Historical evidence';

    document.getElementById('kpi-active-candidates').textContent = data.theoretical_candidates_count || 0;
    document.getElementById('kpi-qualified-count').textContent = data.qualified_opportunities_count || 0;
    document.getElementById('kpi-rejected-count').textContent = data.rejected_opportunities_count || 0;
  }

  async function loadHistoricalOpportunities() {
    const data = await fetchJson(`/api/opportunities?dataset=${encodeURIComponent(currentDataset)}&simulated=${isSimulatedOpps}`);
    if (!data) return;

    renderLiveQualifiedTable(data.opportunities || []);
  }

  async function loadHistoricalAudit() {
    const data = await fetchJson(`/api/historical?dataset=${encodeURIComponent(currentDataset)}`);
    if (!data) return;

    const s1 = data.section_1_dataset_identity || {};
    const s2 = data.section_2_evidence_quality || {};

    const audTotal = document.getElementById('aud-total');
    if (audTotal) audTotal.textContent = s1.total_raw_records || 0;
    const audValid = document.getElementById('aud-valid');
    if (audValid) audValid.textContent = s1.valid_parsed_observations || 0;
    const audFresh = document.getElementById('aud-fresh-rate');
    if (audFresh) audFresh.textContent = s2.freshness_pass_rate ? s2.freshness_pass_rate : '100.0%';
    const audEmpty = document.getElementById('aud-empty');
    if (audEmpty) audEmpty.textContent = s2.empty_books ? `${s2.empty_books} (${s2.empty_books_pct || 0}%)` : '0 (0%)';
    const audDepth = document.getElementById('aud-depth');
    if (audDepth) audDepth.textContent = s2.total_cumulative_bid_depth || '0 contracts';

    const audEarliest = document.getElementById('aud-earliest');
    if (audEarliest) audEarliest.textContent = s1.earliest_timestamp || 'N/A';
    const audLatest = document.getElementById('aud-latest');
    if (audLatest) audLatest.textContent = s1.latest_timestamp || 'N/A';
    const audSpan = document.getElementById('aud-span');
    if (audSpan) audSpan.textContent = s1.total_duration || 'N/A';
    const audMeanInt = document.getElementById('aud-mean-interval');
    if (audMeanInt) audMeanInt.textContent = s1.sampling_cadence_mean || 'N/A';
    const audGaps = document.getElementById('aud-gaps');
    if (audGaps) audGaps.textContent = s1.gaps_count || '0';

    const audVerdict = document.getElementById('aud-verdict');
    if (audVerdict) {
      audVerdict.textContent = data.section_10_final_verdict || data.final_verdict || 'No verdict generated.';
    }

    const rawReport = document.getElementById('raw-v7-report');
    if (rawReport) {
      rawReport.textContent = data.human_summary || 'Report not available.';
    }
  }

  // ---------------------------------------------------------------------------
  // System Health & Diagnostics
  // ---------------------------------------------------------------------------

  async function loadHealth() {
    const data = await fetchJson('/api/status');
    if (!data) return;

    const coll = data.continuous_collector || {};
    const evalState = data.continuous_evaluator || {};
    const evalCfg = evalState.config || {};
    const qualSummary = evalState.qualification_summary || {};
    const outcomes = qualSummary.outcomes_breakdown || {};

    // 1. Continuous Collector Card
    document.getElementById('hlth-coll-status').textContent = coll.status || 'STOPPED';
    document.getElementById('hlth-coll-proc').textContent = coll.process_running ? `RUNNING (PID ${coll.process_pid})` : 'STOPPED';
    document.getElementById('hlth-coll-pid').textContent = coll.process_pid || coll.pid || 'None';
    document.getElementById('hlth-coll-session').textContent = coll.session_id || 'None';
    document.getElementById('hlth-coll-last-time').textContent = coll.last_successful_collection_time || 'None';
    document.getElementById('hlth-coll-age').textContent = coll.latest_snapshot_age_seconds != null ? `${coll.latest_snapshot_age_seconds.toFixed(1)}s` : 'N/A';
    document.getElementById('hlth-coll-fresh').textContent = coll.is_data_fresh ? 'Fresh (< 60s)' : 'Stale / Inactive';
    document.getElementById('hlth-coll-markets').textContent = coll.active_monitored_markets_count || 0;
    document.getElementById('hlth-coll-cycles').textContent = `${coll.total_cycles_count || 0} (${coll.successful_cycles_count || 0} success / ${coll.failed_cycles_count || 0} fail)`;
    document.getElementById('hlth-coll-ratelimits').textContent = coll.rate_limit_count || 0;
    document.getElementById('hlth-coll-error').textContent = coll.last_error || 'None';

    // 2. Continuous Evaluator Card
    document.getElementById('hlth-eval-status').textContent = evalState.status || 'STOPPED';
    document.getElementById('hlth-eval-proc').textContent = evalState.process_running ? `RUNNING (PID ${evalState.process_pid})` : 'STOPPED';
    document.getElementById('hlth-eval-pid').textContent = evalState.process_pid || evalState.pid || 'None';
    document.getElementById('hlth-eval-session').textContent = evalState.session_id || 'None';
    document.getElementById('hlth-eval-last-time').textContent = evalState.last_evaluation_time || 'None';
    document.getElementById('hlth-eval-cycles').textContent = evalState.evaluations_count || 0;
    document.getElementById('hlth-eval-obs').textContent = (evalState.processed_observations_count || 0).toLocaleString();
    document.getElementById('hlth-eval-active').textContent = evalState.active_candidates_count || 0;
    document.getElementById('hlth-eval-total').textContent = evalState.total_candidates_detected || 0;
    document.getElementById('hlth-eval-trans').textContent = evalState.candidate_transitions_count || 0;
    document.getElementById('hlth-eval-error').textContent = evalState.last_error || 'None';

    // 3. Execution Qualification Card
    document.getElementById('hlth-qual-target-qty').textContent = `${evalCfg.target_quantity || 1} contracts`;
    document.getElementById('hlth-qual-require-full').textContent = evalCfg.require_full_quantity ? 'Enforced (100% depth)' : 'Partial fills permitted';
    document.getElementById('hlth-qual-min-profit').textContent = `${evalCfg.min_net_profit_cents || 1}¢`;
    document.getElementById('hlth-qual-count').textContent = evalState.qualified_candidates_count || 0;
    document.getElementById('hlth-qual-rej-count').textContent = evalState.rejected_candidates_count || 0;

    // Top rejection cause
    let topCause = 'None';
    let maxRej = 0;
    for (const [cause, cnt] of Object.entries(outcomes)) {
      if (cause.startsWith('REJECTED_') && cnt > maxRej) {
        maxRej = cnt;
        topCause = `${cause} (${cnt})`;
      }
    }
    document.getElementById('hlth-qual-top-cause').textContent = topCause;

    // 4. Alert & Delivery Engine (V24) Card
    const altHealth = data.alert_service || {};
    const hlthAlertStatus = document.getElementById('hlth-alert-status');
    if (hlthAlertStatus) hlthAlertStatus.textContent = altHealth.status || 'STOPPED';
    const hlthAlertChannels = document.getElementById('hlth-alert-channels');
    if (hlthAlertChannels) hlthAlertChannels.textContent = (altHealth.enabled_channels || []).join(', ') || 'local_history';
    const hlthAlertCreated = document.getElementById('hlth-alert-created');
    if (hlthAlertCreated) hlthAlertCreated.textContent = altHealth.alerts_created_count || 0;
    const hlthAlertDelivered = document.getElementById('hlth-alert-delivered');
    if (hlthAlertDelivered) hlthAlertDelivered.textContent = altHealth.alerts_delivered_count || 0;
    const hlthAlertFailed = document.getElementById('hlth-alert-failed');
    if (hlthAlertFailed) hlthAlertFailed.textContent = altHealth.alerts_failed_count || 0;
    const hlthAlertQueue = document.getElementById('hlth-alert-queue');
    if (hlthAlertQueue) hlthAlertQueue.textContent = altHealth.pending_queue_depth || 0;
    const hlthAlertLastTime = document.getElementById('hlth-alert-last-time');
    if (hlthAlertLastTime) hlthAlertLastTime.textContent = altHealth.last_alert_time ? altHealth.last_alert_time.substring(11, 19) + ' UTC' : 'None';
    const hlthAlertLastDelivery = document.getElementById('hlth-alert-last-delivery');
    if (hlthAlertLastDelivery) hlthAlertLastDelivery.textContent = altHealth.last_delivery_time ? altHealth.last_delivery_time.substring(11, 19) + ' UTC' : 'None';
    const hlthAlertTracked = document.getElementById('hlth-alert-tracked');
    if (hlthAlertTracked) hlthAlertTracked.textContent = altHealth.tracked_candidates_count || 0;

    // 4b. Breadth Scanner Engine (Phase 2) Card
    const bscanHealth = data.breadth_scanner || {};
    const hlthBscanStatus = document.getElementById('hlth-bscan-status');
    if (hlthBscanStatus) {
      hlthBscanStatus.textContent = bscanHealth.status || 'STOPPED';
      const hlthBscanProc = document.getElementById('hlth-bscan-proc');
      if (hlthBscanProc) hlthBscanProc.textContent = bscanHealth.process_running ? `RUNNING (PID ${bscanHealth.process_pid})` : 'STOPPED';
      const hlthBscanSession = document.getElementById('hlth-bscan-session');
      if (hlthBscanSession) hlthBscanSession.textContent = bscanHealth.session_id || 'None';
      const hlthBscanCatalog = document.getElementById('hlth-bscan-catalog');
      if (hlthBscanCatalog) hlthBscanCatalog.textContent = bscanHealth.last_catalog_complete ? 'Complete' : ((bscanHealth.total_cycles_count || 0) > 0 ? 'Partial / Truncated' : 'None');
      const hlthBscanMarkets = document.getElementById('hlth-bscan-markets');
      if (hlthBscanMarkets) hlthBscanMarkets.textContent = `${(bscanHealth.total_unique_markets_discovered || 0).toLocaleString()} / ${(bscanHealth.total_unique_markets_screened || 0).toLocaleString()}`;
      const hlthBscanCycles = document.getElementById('hlth-bscan-cycles');
      if (hlthBscanCycles) hlthBscanCycles.textContent = `${bscanHealth.total_cycles_count || 0} (${bscanHealth.successful_cycles_count || 0} succ / ${bscanHealth.failed_cycles_count || 0} fail)`;
      const hlthBscanPrelim = document.getElementById('hlth-bscan-prelim');
      if (hlthBscanPrelim) hlthBscanPrelim.textContent = bscanHealth.total_preliminary_candidates_generated || 0;
      const hlthBscanQual = document.getElementById('hlth-bscan-qual');
      if (hlthBscanQual) hlthBscanQual.textContent = bscanHealth.qualified_candidates_count || 0;
      const hlthBscanQueue = document.getElementById('hlth-bscan-queue');
      if (hlthBscanQueue) hlthBscanQueue.textContent = `${bscanHealth.candidates_pending || 0} / ${bscanHealth.queue_overflow_count || 0}`;
      const hlthBscanFreshness = document.getElementById('hlth-bscan-freshness');
      if (hlthBscanFreshness) {
        if (bscanHealth.is_data_fresh) {
          hlthBscanFreshness.textContent = 'Fresh (< 60s)';
          hlthBscanFreshness.className = 'summary-val font-mono text-accent';
        } else if (bscanHealth.is_stale) {
          hlthBscanFreshness.textContent = `Stale (${Math.round(bscanHealth.staleness_seconds || 0)}s)`;
          hlthBscanFreshness.className = 'summary-val font-mono';
        } else {
          hlthBscanFreshness.textContent = 'Inactive';
          hlthBscanFreshness.className = 'summary-val font-mono text-muted';
        }
      }
    }

    // 5. Storage Card
    document.getElementById('hlth-workspace').textContent = data.workspace_root || 'N/A';
    document.getElementById('hlth-dataset-count').textContent = data.datasets_count || 0;
    document.getElementById('hlth-total-records').textContent = (data.total_records_stored || 0).toLocaleString();
    document.getElementById('hlth-storage-bytes').textContent = `${((data.total_storage_bytes || 0) / 1024).toFixed(1)} KB`;

    // 5. Notices / Warnings
    const warningsList = document.getElementById('warnings-list');
    if (data.warnings && data.warnings.length > 0) {
      warningsList.innerHTML = data.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('');
    } else {
      warningsList.innerHTML = '<li>All system diagnostics normal. Zero active pipeline warnings.</li>';
    }
  }

  // ---------------------------------------------------------------------------
  // Breadth-First Scanner Integration (Phase 2E)
  // ---------------------------------------------------------------------------

  async function loadBreadthScannerData() {
    const stratFilterEl = document.getElementById('bscan-filter-strat');
    const outcomeFilterEl = document.getElementById('bscan-filter-outcome');
    const selectedStrat = stratFilterEl ? stratFilterEl.value : 'ALL';
    const selectedOutcome = outcomeFilterEl ? outcomeFilterEl.value : 'ALL';

    const urlParams = new URLSearchParams(window.location.search);
    const activeScannerDir = urlParams.get('dir') || urlParams.get('scanner_dir');
    const dirParam = activeScannerDir ? `&dir=${encodeURIComponent(activeScannerDir)}` : '';

    let statusUrl = activeScannerDir ? `/api/scanner/status?dir=${encodeURIComponent(activeScannerDir)}` : '/api/scanner/status';
    let summariesUrl = `/api/scanner/summaries?limit=50${dirParam}`;

    let candUrl = `/api/scanner/candidates?limit=100${dirParam}`;
    if (selectedStrat !== 'ALL') {
      candUrl += `&strategy=${encodeURIComponent(selectedStrat)}`;
    }

    let qualUrl = `/api/scanner/qualifications?limit=100${dirParam}`;
    if (selectedStrat !== 'ALL') {
      qualUrl += `&strategy=${encodeURIComponent(selectedStrat)}`;
    }
    if (selectedOutcome !== 'ALL' && selectedOutcome !== 'PRELIMINARY') {
      if (selectedOutcome === 'QUALIFIED') {
        qualUrl += `&qualified=true`;
      } else {
        qualUrl += `&outcome=${encodeURIComponent(selectedOutcome)}`;
      }
    }

    const [statusData, summariesData, candData, qualData] = await Promise.all([
      fetchJson(statusUrl),
      fetchJson(summariesUrl),
      fetchJson(candUrl),
      fetchJson(qualUrl),
    ]);

    const status = statusData || {};
    const summaries = (summariesData && summariesData.summaries) || [];
    const candidates = (candData && candData.candidates) || [];
    const qualifications = (qualData && qualData.qualifications) || [];
    const latestSummary = summaries.length > 0 ? summaries[0] : null;

    // 1. Header Status Pill
    const scannerPill = document.getElementById('scanner-status-pill');
    const scannerText = document.getElementById('scanner-status-text');
    if (scannerPill && scannerText) {
      scannerPill.className = 'status-indicator';
      if (status.status === 'RUNNING') {
        if (status.is_stale) {
          scannerPill.classList.add('status-unknown');
          scannerText.textContent = `Scanner: Stale (${Math.round(status.staleness_seconds || 0)}s)`;
        } else {
          scannerPill.classList.add('status-running');
          scannerText.textContent = `Scanner: Running (PID ${status.process_pid || '—'})`;
        }
      } else if (status.status === 'INITIALIZING') {
        scannerPill.classList.add('status-unknown');
        scannerText.textContent = `Scanner: Initializing`;
      } else if (status.status === 'ERROR') {
        scannerPill.classList.add('status-stopped');
        scannerText.textContent = `Scanner: Error`;
      } else {
        scannerPill.classList.add('status-stopped');
        scannerText.textContent = `Scanner: Stopped`;
      }
    }

    // 2. Alert Banners
    const staleBanner = document.getElementById('bscan-stale-banner');
    const staleText = document.getElementById('bscan-stale-text');
    if (staleBanner) {
      if (status.is_stale) {
        staleBanner.classList.remove('hidden');
        if (staleText) {
          staleText.textContent = `⚠️ Stale Telemetry: Breadth Scanner has not updated within 60s (${Math.round(status.staleness_seconds || 0)}s old). Old data is not live.`;
        }
      } else {
        staleBanner.classList.add('hidden');
      }
    }

    const incompleteBanner = document.getElementById('bscan-incomplete-banner');
    if (incompleteBanner) {
      if ((status.total_cycles_count || 0) > 0 && !status.last_catalog_complete) {
        incompleteBanner.classList.remove('hidden');
      } else {
        incompleteBanner.classList.add('hidden');
      }
    }

    const errorBanner = document.getElementById('bscan-error-banner');
    const errorText = document.getElementById('bscan-error-text');
    if (errorBanner) {
      if (status.status === 'ERROR' || status.last_error) {
        errorBanner.classList.remove('hidden');
        if (errorText) {
          errorText.textContent = `Scanner Operational Error: ${status.last_error || status.error || 'Encountered operational error'}`;
        }
      } else {
        errorBanner.classList.add('hidden');
      }
    }

    const stoppedBanner = document.getElementById('bscan-stopped-banner');
    if (stoppedBanner) {
      if (status.status === 'STOPPED') {
        stoppedBanner.classList.remove('hidden');
      } else {
        stoppedBanner.classList.add('hidden');
      }
    }

    // 3. Primary KPI Cards
    const kpiDisc = document.getElementById('bscan-kpi-disc');
    const kpiScreen = document.getElementById('bscan-kpi-screen');
    if (kpiDisc && kpiScreen) {
      kpiDisc.textContent = (status.total_unique_markets_discovered || 0).toLocaleString();
      kpiScreen.textContent = (status.total_unique_markets_screened || 0).toLocaleString();
    }

    const cycleBadge = document.getElementById('bscan-cycle-badge');
    const kpiCycleScreen = document.getElementById('bscan-kpi-cycle-screen');
    const cycleSub = document.getElementById('bscan-cycle-sub');
    if (kpiCycleScreen) {
      kpiCycleScreen.textContent = latestSummary ? latestSummary.markets_screened.toLocaleString() : (status.total_unique_markets_screened || 0).toLocaleString();
    }
    if (cycleBadge) {
      cycleBadge.textContent = latestSummary ? `CYCLE #${latestSummary.cycle_number}` : `CYCLE #${status.current_cycle || 0}`;
    }
    if (cycleSub && latestSummary) {
      cycleSub.textContent = `New: ${latestSummary.new_markets_count || 0} • Changed: ${latestSummary.changed_markets_count || 0} • Closed: ${latestSummary.closed_or_settled_markets_count || 0}`;
    }

    const catalogPill = document.getElementById('bscan-catalog-pill');
    const catalogStatus = document.getElementById('bscan-catalog-status');
    const lastDiscTime = document.getElementById('bscan-last-discovery-time');
    if (catalogStatus && catalogPill) {
      if (status.last_catalog_complete) {
        catalogStatus.textContent = 'Complete';
        catalogPill.textContent = 'COMPLETE';
        catalogPill.className = 'badge-status-pill badge-catalog-complete';
      } else if ((status.total_cycles_count || 0) > 0) {
        catalogStatus.textContent = 'Partial / Truncated';
        catalogPill.textContent = 'PARTIAL';
        catalogPill.className = 'badge-status-pill badge-catalog-partial';
      } else {
        catalogStatus.textContent = 'None';
        catalogPill.textContent = 'NO DATA';
        catalogPill.className = 'badge-status-pill badge-neutral';
      }
    }
    if (lastDiscTime) {
      if (status.last_successful_discovery_time) {
        lastDiscTime.textContent = `Last refresh: ${status.last_successful_discovery_time.substring(11, 19)} UTC`;
      } else {
        lastDiscTime.textContent = 'Last refresh: None';
      }
    }

    const healthPill = document.getElementById('bscan-health-pill');
    const healthStatus = document.getElementById('bscan-health-status');
    const healthSub = document.getElementById('bscan-health-sub');
    if (healthStatus && healthPill) {
      healthStatus.textContent = status.status || 'STOPPED';
      healthPill.textContent = status.status || 'STOPPED';
      healthPill.className = 'badge-status-pill';
      if (status.status === 'RUNNING') {
        healthPill.classList.add(status.is_stale ? 'badge-degraded' : 'badge-running');
      } else {
        healthPill.classList.add('badge-stopped');
      }
    }
    if (healthSub) {
      const avgDur = latestSummary ? `${latestSummary.duration_seconds.toFixed(2)}s` : '0s';
      healthSub.textContent = `${status.total_cycles_count || 0} cycles (${avgDur} last duration)`;
    }

    const kpiPrelim = document.getElementById('bscan-kpi-prelim');
    if (kpiPrelim) {
      kpiPrelim.textContent = (status.total_preliminary_candidates_generated || 0).toLocaleString();
    }

    const kpiQual = document.getElementById('bscan-kpi-qual');
    if (kpiQual) {
      kpiQual.textContent = (status.qualified_candidates_count || 0).toLocaleString();
    }

    // 4. Breakdown Panels
    const stratCounts = status.screening_strategy_counts;
    const qualStratCounts = status.qualification_strategy_counts || {};

    let stratBp = 0, stratLy = 0, stratLn = 0, stratCb = 0;
    let hasValidStratCounts = false;

    if (stratCounts && typeof stratCounts === 'object') {
      stratBp = stratCounts.binary_parity ?? 0;
      stratLy = stratCounts.mece_basket_long_yes ?? stratCounts.mece_long_yes ?? 0;
      stratLn = stratCounts.mece_basket_long_no ?? stratCounts.mece_long_no ?? 0;
      stratCb = stratCounts.crossed_book ?? 0;
      hasValidStratCounts = true;
    } else if (candidates && candidates.length > 0) {
      // Fallback: aggregate from candidate list with deduplication & normalized string match
      const seenCandKeys = new Set();
      candidates.forEach(c => {
        const key = c.candidate_id || c.stable_key;
        if (key && seenCandKeys.has(key)) return;
        if (key) seenCandKeys.add(key);

        const st = (c.strategy_type || '').toLowerCase();
        const isCrossed = Boolean(c.is_crossed_book_signal) || st === 'crossed_book';
        if (isCrossed) stratCb++;
        else if (st === 'binary_parity' || st === 'parity') stratBp++;
        else if (st === 'mece_basket_long_yes' || st === 'mece_long_yes') stratLy++;
        else if (st === 'mece_basket_long_no' || st === 'mece_long_no') stratLn++;
      });
      hasValidStratCounts = true;
    }

    const elStratBp = document.getElementById('bscan-strat-bp');
    const elStratLy = document.getElementById('bscan-strat-ly');
    const elStratLn = document.getElementById('bscan-strat-ln');
    const elStratCb = document.getElementById('bscan-strat-cb');
    const elStratDedup = document.getElementById('bscan-strat-dedup');
    const elStratPending = document.getElementById('bscan-strat-pending');

    const elStratBpSub = document.getElementById('bscan-strat-bp-sub');
    const elStratLySub = document.getElementById('bscan-strat-ly-sub');
    const elStratLnSub = document.getElementById('bscan-strat-ln-sub');

    if (hasValidStratCounts) {
      if (elStratBp) elStratBp.textContent = stratBp;
      if (elStratLy) elStratLy.textContent = stratLy;
      if (elStratLn) elStratLn.textContent = stratLn;
      if (elStratCb) elStratCb.textContent = stratCb;

      const qLy = qualStratCounts.mece_basket_long_yes || qualStratCounts.mece_long_yes;
      if (elStratLySub) {
        elStratLySub.textContent = (qLy && (qLy.qualified > 0 || qLy.rejected > 0)) ? `(${qLy.qualified || 0} qual / ${qLy.rejected || 0} rej)` : '';
      }
      const qLn = qualStratCounts.mece_basket_long_no || qualStratCounts.mece_long_no;
      if (elStratLnSub) {
        elStratLnSub.textContent = (qLn && (qLn.qualified > 0 || qLn.rejected > 0)) ? `(${qLn.qualified || 0} qual / ${qLn.rejected || 0} rej)` : '';
      }
      const qBp = qualStratCounts.binary_parity;
      if (elStratBpSub) {
        elStratBpSub.textContent = (qBp && (qBp.qualified > 0 || qBp.rejected > 0)) ? `(${qBp.qualified || 0} qual / ${qBp.rejected || 0} rej)` : '';
      }
    } else if ((status.total_preliminary_candidates_generated || 0) > 0) {
      if (elStratBp) elStratBp.textContent = 'N/A';
      if (elStratLy) elStratLy.textContent = 'N/A';
      if (elStratLn) elStratLn.textContent = 'N/A';
      if (elStratCb) elStratCb.textContent = 'N/A';
    } else {
      if (elStratBp) elStratBp.textContent = '0';
      if (elStratLy) elStratLy.textContent = '0';
      if (elStratLn) elStratLn.textContent = '0';
      if (elStratCb) elStratCb.textContent = '0';
    }

    if (elStratDedup) elStratDedup.textContent = status.candidates_deduplicated || 0;
    if (elStratPending) elStratPending.textContent = status.candidates_pending || 0;

    const qualCounts = status.qualification_counts || {};
    const elTqualQual = document.getElementById('bscan-tqual-qual');
    const elRejStale = document.getElementById('bscan-rej-stale');
    const elRejFees = document.getElementById('bscan-rej-fees');
    const elRejDepth = document.getElementById('bscan-rej-depth');
    const elRejSkew = document.getElementById('bscan-rej-skew');
    const elRejEmpty = document.getElementById('bscan-rej-empty');
    const elRejUnsupported = document.getElementById('bscan-rej-unsupported');
    const elRejOverflow = document.getElementById('bscan-rej-overflow');
    if (elTqualQual) elTqualQual.textContent = qualCounts['QUALIFIED'] || 0;
    if (elRejStale) elRejStale.textContent = qualCounts['REJECTED_STALE'] || 0;
    if (elRejFees) elRejFees.textContent = (qualCounts['REJECTED_UNPROFITABLE_FEES'] || 0) + (qualCounts['REJECTED_FEE_DEFICIT'] || 0);
    if (elRejDepth) elRejDepth.textContent = qualCounts['REJECTED_INSUFFICIENT_DEPTH'] || 0;
    if (elRejSkew) elRejSkew.textContent = (qualCounts['REJECTED_LEG_SKEW'] || 0) + (qualCounts['REJECTED_NON_CANONICAL'] || 0);
    if (elRejEmpty) elRejEmpty.textContent = (qualCounts['REJECTED_EMPTY_BOOK'] || 0) + (qualCounts['REJECTED_MISSING_MARKET'] || 0);
    if (elRejUnsupported) elRejUnsupported.textContent = (qualCounts['REJECTED_UNSUPPORTED'] || 0) + (qualCounts['REJECTED_INVALID_BASKET'] || 0);
    if (elRejOverflow) elRejOverflow.textContent = status.queue_overflow_count || 0;

    const elApiReq = document.getElementById('bscan-api-req');
    const elApiFail = document.getElementById('bscan-api-fail');
    const elApiRetries = document.getElementById('bscan-api-retries');
    const elApiRatelimit = document.getElementById('bscan-api-ratelimit');
    const elLastDuration = document.getElementById('bscan-last-duration');
    const elFreshnessVal = document.getElementById('bscan-freshness-val');
    const elSessionId = document.getElementById('bscan-session-id');
    if (elApiReq) elApiReq.textContent = status.api_requests_count || 0;
    if (elApiFail) elApiFail.textContent = status.api_failures_count || 0;
    if (elApiRetries) elApiRetries.textContent = status.api_retries_count || 0;
    if (elApiRatelimit) elApiRatelimit.textContent = status.rate_limit_events_count || 0;
    if (elLastDuration) elLastDuration.textContent = latestSummary ? `${latestSummary.duration_seconds.toFixed(3)}s` : '—';
    if (elFreshnessVal) {
      if (status.is_data_fresh) {
        elFreshnessVal.textContent = 'Fresh (< 60s)';
        elFreshnessVal.className = 'summary-val font-mono text-accent';
      } else if (status.is_stale) {
        elFreshnessVal.textContent = `Stale (${Math.round(status.staleness_seconds || 0)}s)`;
        elFreshnessVal.className = 'summary-val font-mono';
      } else {
        elFreshnessVal.textContent = 'Stopped';
        elFreshnessVal.className = 'summary-val font-mono text-muted';
      }
    }
    if (elSessionId) elSessionId.textContent = status.session_id || '—';

    // 5. Render Candidates & Qualifications Table
    renderBreadthCandidatesTable(qualifications, candidates, selectedOutcome);

    // 6. Render Cycle History Table
    renderBreadthCyclesTable(summaries);
  }

  function renderBreadthCandidatesTable(qualifications, candidates, selectedOutcome) {
    const tbody = document.getElementById('bscan-candidates-tbody');
    if (!tbody) return;

    let itemsToRender = [];
    if (selectedOutcome === 'PRELIMINARY') {
      itemsToRender = candidates;
    } else if (selectedOutcome !== 'ALL') {
      itemsToRender = qualifications;
    } else {
      // Show qualifications, plus any preliminary candidates not yet in qualifications
      itemsToRender = [...qualifications];
      const qualCandIds = new Set(qualifications.map(q => q.candidate_id));
      for (const c of candidates) {
        if (!qualCandIds.has(c.candidate_id)) {
          itemsToRender.push(c);
        }
      }
    }

    if (itemsToRender.length === 0) {
      tbody.innerHTML = `<tr><td colspan="10" class="text-center text-muted" style="padding: 24px;">No candidates matching the current filters.</td></tr>`;
      return;
    }

    let html = '';
    for (const item of itemsToRender) {
      const isQualRecord = (item.qualification_id != null);
      const prelimCand = isQualRecord ? (item.preliminary_candidate || {}) : item;
      const stratType = item.strategy_type || prelimCand.strategy_type || '';
      const recTime = item.recorded_at || item.evaluated_at || prelimCand.screening_timestamp || '';
      const timeStr = recTime ? recTime.substring(11, 19) + ' UTC' : '—';
      const cycleNum = item.cycle_number != null ? `#${item.cycle_number}` : (prelimCand.cycle_number != null ? `#${prelimCand.cycle_number}` : '—');
      const marketTickers = prelimCand.market_tickers || [];
      const eventTicker = prelimCand.event_ticker || '';
      const constituentQuotes = prelimCand.constituent_quotes || {};
      const legCount = marketTickers.length;

      let mktCellHtml = '';
      if (legCount > 1) {
        const sampleCount = Math.min(3, legCount);
        const sampleTickers = marketTickers.slice(0, sampleCount).map(t => {
          if (eventTicker && t.startsWith(eventTicker + '-')) {
            return '-' + t.substring(eventTicker.length + 1);
          }
          return t;
        });
        const remaining = legCount - sampleCount;
        const sampleText = sampleTickers.join(', ') + (remaining > 0 ? ` (+${remaining} more)` : '');

        let legChipsHtml = '';
        marketTickers.forEach(t => {
          const q = constituentQuotes[t] || {};
          let quoteParts = [];
          if (q.yes_ask != null) quoteParts.push(`YES Ask: $${q.yes_ask}`);
          if (q.no_ask != null) quoteParts.push(`NO Ask: $${q.no_ask}`);
          if (quoteParts.length === 0) {
            if (q.yes_bid != null) quoteParts.push(`YES Bid: $${q.yes_bid}`);
            if (q.no_bid != null) quoteParts.push(`NO Bid: $${q.no_bid}`);
          }
          const quoteStr = quoteParts.join(' | ') || 'No top quote';
          legChipsHtml += `<div class="leg-item font-mono"><span class="leg-ticker">${escapeHtml(t)}</span><span class="leg-quote text-muted">${escapeHtml(quoteStr)}</span></div>`;
        });

        mktCellHtml = `
          <div class="mkt-cell-container">
            <div class="mkt-event-header">
              <span class="mkt-event-ticker font-mono font-bold">${escapeHtml(eventTicker || 'Multi-Leg Event')}</span>
              <span class="badge-legs-count">${legCount} legs</span>
            </div>
            <div class="mkt-sample-preview font-mono text-muted" title="${escapeHtml(marketTickers.join(', '))}">${escapeHtml(sampleText)}</div>
            <details class="legs-disclosure">
              <summary class="legs-summary">View all ${legCount} legs &amp; quotes</summary>
              <div class="legs-expanded-box">
                <div class="legs-scroll-list">
                  ${legChipsHtml}
                </div>
              </div>
            </details>
          </div>
        `;
      } else if (legCount === 1) {
        mktCellHtml = `<div class="font-mono">${escapeHtml(marketTickers[0])}</div>`;
      } else {
        mktCellHtml = `<div class="font-mono text-muted">${escapeHtml(eventTicker || '—')}</div>`;
      }

      const grossEdge = prelimCand.gross_edge_cents != null
        ? `${prelimCand.gross_edge_cents}¢`
        : (prelimCand.gross_edge_dollars ? `$${prelimCand.gross_edge_dollars}` : '—');

      const fees = item.total_fees_cents != null
        ? `${item.total_fees_cents}¢`
        : (prelimCand.estimated_fees_cents != null ? `${prelimCand.estimated_fees_cents}¢` : '—');

      const netEdge = item.net_edge_cents != null ? `${item.net_edge_cents}¢` : '—';
      const execQty = item.executable_quantity != null ? item.executable_quantity : '—';

      let statusBadge = '';
      let outcomeHtml = '';

      if (stratType === 'crossed_book') {
        statusBadge = `<span class="badge-signal-only">Crossed Book (Signal Only)</span>`;
        outcomeHtml = `<span class="text-muted">Unverified signal (never executable)</span>`;
      } else if (isQualRecord) {
        if (item.is_qualified) {
          statusBadge = `<span class="badge-qual-success">V22 QUALIFIED</span>`;
          outcomeHtml = `<span class="text-accent font-mono">Net Profit +${item.net_edge_cents}¢</span>`;
        } else {
          statusBadge = `<span class="badge-qual-rejected">${escapeHtml(item.outcome || 'REJECTED')}</span>`;
          outcomeHtml = `<span class="text-muted" title="${escapeHtml(item.rejection_reason || '')}">${escapeHtml(item.rejection_reason || item.outcome || 'Rejected')}</span>`;
        }
      } else {
        statusBadge = `<span class="badge-preliminary-signal">PRELIMINARY SIGNAL</span>`;
        outcomeHtml = `<span class="text-muted">Awaiting Order-Book Qualification</span>`;
      }

      html += `<tr>
        <td class="font-mono">${escapeHtml(timeStr)}</td>
        <td class="font-mono">${escapeHtml(cycleNum)}</td>
        <td>${escapeHtml(formatStrategy(stratType))}</td>
        <td class="mkt-column">${mktCellHtml}</td>
        <td>${statusBadge}</td>
        <td class="font-mono">${escapeHtml(grossEdge)}</td>
        <td class="font-mono">${escapeHtml(fees)}</td>
        <td class="font-mono ${item.is_qualified ? 'text-accent' : ''}">${escapeHtml(netEdge)}</td>
        <td class="font-mono">${escapeHtml(execQty)}</td>
        <td>${outcomeHtml}</td>
      </tr>`;
    }
    tbody.innerHTML = html;
  }

  function renderBreadthCyclesTable(summaries) {
    const tbody = document.getElementById('bscan-cycles-tbody');
    if (!tbody) return;

    if (!summaries || summaries.length === 0) {
      tbody.innerHTML = `<tr><td colspan="11" class="text-center text-muted" style="padding: 24px;">No scan cycles completed yet.</td></tr>`;
      return;
    }

    let html = '';
    for (const s of summaries) {
      const compTime = s.completed_at ? s.completed_at.substring(11, 19) + ' UTC' : '—';
      const durStr = s.duration_seconds != null ? `${s.duration_seconds.toFixed(3)}s` : '—';
      const catalogState = s.is_catalog_complete
        ? `<span class="badge-status-pill badge-catalog-complete">COMPLETE</span>`
        : `<span class="badge-status-pill badge-catalog-partial">PARTIAL</span>`;

      const qualHtml = (s.qualified_count > 0)
        ? `<span class="text-accent font-mono">${s.qualified_count}</span>`
        : '0';

      html += `<tr>
        <td class="font-mono font-bold">#${s.cycle_number}</td>
        <td class="font-mono">${escapeHtml(compTime)}</td>
        <td class="font-mono">${escapeHtml(durStr)}</td>
        <td>${catalogState}</td>
        <td class="font-mono">${(s.markets_discovered || 0).toLocaleString()}</td>
        <td class="font-mono">${(s.eligible_markets || 0).toLocaleString()}</td>
        <td class="font-mono">${(s.markets_screened || 0).toLocaleString()}</td>
        <td class="font-mono">${s.preliminary_candidates_count || 0}</td>
        <td>${qualHtml}</td>
        <td class="font-mono">${s.rejected_count || 0}</td>
        <td class="font-mono">${s.requests_made || 0} / ${s.requests_failed || 0}</td>
      </tr>`;
    }
    tbody.innerHTML = html;
  }

  // ---------------------------------------------------------------------------
  // Utility Functions
  // ---------------------------------------------------------------------------

  function formatPrice(val) {
    if (val === null || val === undefined) return '—';
    const num = parseFloat(val);
    if (isNaN(num)) return '—';
    return `$${num.toFixed(2)}`;
  }

  function formatStrategy(strat) {
    if (strat === 'binary_parity') return 'Binary Parity';
    if (strat === 'mece_long_yes' || strat === 'mece_basket_long_yes') return 'MECE Long YES';
    if (strat === 'mece_long_no' || strat === 'mece_basket_long_no') return 'MECE Long NO';
    if (strat === 'crossed_book') return 'Crossed Book';
    return strat || 'Unknown';
  }

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // DOM Content Loaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
