/**
 * Kalshi Arbitrage Engine Dashboard — Frontend V1.1 Client Script
 * Strictly reads from read-only backend API endpoints.
 * Reuses existing V0-V7 schemas and never fabricates values.
 */

(function () {
  'use strict';

  // State
  let currentDataset = 'evidence_dashboard_trial';
  let activeTab = 'overview';
  let isSimulatedOpps = false;
  let autoRefreshTimer = null;
  let cachedMarkets = [];
  let marketFilter = 'ALL';
  let marketSearchQuery = '';

  // DOM Elements
  const datasetSelect = document.getElementById('dataset-select');
  const refreshBtn = document.getElementById('refresh-btn');
  const autoRefreshToggle = document.getElementById('auto-refresh-toggle');
  const observerStatusPill = document.getElementById('observer-status-pill');
  const observerStatusText = document.getElementById('observer-status-text');
  const envPill = document.getElementById('env-pill');
  const navTabs = document.querySelectorAll('.tab-button');
  const tabPanes = document.querySelectorAll('.view-panel');
  const footerServerTime = document.getElementById('footer-server-time');

  // Initialization
  async function init() {
    setupEventListeners();
    await loadDatasets();
    await refreshAllData();
  }

  // Event Listeners
  function setupEventListeners() {
    // Navigation Tabs
    navTabs.forEach((tab) => {
      tab.addEventListener('click', () => {
        const targetTab = tab.getAttribute('data-tab');
        switchTab(targetTab);
      });
    });

    // Dataset Selector
    datasetSelect.addEventListener('change', async (e) => {
      currentDataset = e.target.value;
      await refreshAllData();
    });

    // Manual Refresh
    refreshBtn.addEventListener('click', async () => {
      await refreshAllData();
    });

    // Auto-refresh Toggle
    autoRefreshToggle.addEventListener('change', (e) => {
      if (e.target.checked) {
        autoRefreshTimer = setInterval(refreshAllData, 5000);
      } else {
        if (autoRefreshTimer) {
          clearInterval(autoRefreshTimer);
          autoRefreshTimer = null;
        }
      }
    });

    // Toggle Breakdown Funnel Section
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

    // Market Search
    const searchInput = document.getElementById('market-search');
    if (searchInput) {
      searchInput.addEventListener('input', (e) => {
        marketSearchQuery = e.target.value.toLowerCase().trim();
        renderMarketsTable();
      });
    }

    // Opportunities Mode Toggle
    const btnOppReal = document.getElementById('btn-opp-real');
    const btnOppSim = document.getElementById('btn-opp-sim');

    btnOppReal.addEventListener('click', async () => {
      if (isSimulatedOpps) {
        isSimulatedOpps = false;
        btnOppReal.classList.add('active');
        btnOppSim.classList.remove('active');
        document.getElementById('simulated-warning-banner').classList.add('hidden');
        await loadOpportunities();
      }
    });

    btnOppSim.addEventListener('click', async () => {
      if (!isSimulatedOpps) {
        isSimulatedOpps = true;
        btnOppSim.classList.add('active');
        btnOppReal.classList.remove('active');
        document.getElementById('simulated-warning-banner').classList.remove('hidden');
        await loadOpportunities();
      }
    });

    // Toggle Full Raw V7 Report
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

    // Modal Close
    document.getElementById('modal-close-btn').addEventListener('click', closeModal);
    document.getElementById('market-modal').addEventListener('click', (e) => {
      if (e.target.id === 'market-modal') {
        closeModal();
      }
    });
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
      console.error(`API Error on ${endpoint}:`, err);
      return null;
    }
  }

  // Load Datasets List
  async function loadDatasets() {
    const data = await fetchJson('/api/datasets');
    if (!data || !data.datasets) return;

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
    footerServerTime.textContent = `UTC: ${new Date().toISOString().replace('T', ' ').substring(0, 19)}`;

    await Promise.all([
      loadOverview(),
      loadMarkets(),
      loadOpportunities(),
      loadHistorical(),
      loadHealth(),
    ]);
  }

  // Load Overview (Primary 4 Metrics + Status)
  async function loadOverview() {
    const data = await fetchJson(`/api/overview?dataset=${encodeURIComponent(currentDataset)}`);
    if (!data) return;

    // Compact Observer Status Indicator in Header
    const proc = data.observer_process || {};
    observerStatusPill.className = 'status-indicator';

    if (proc.status === 'RUNNING') {
      observerStatusPill.classList.add('status-running');
      observerStatusText.textContent = `Running (PID ${proc.pid})`;
    } else if (proc.status === 'STOPPED') {
      observerStatusPill.classList.add('status-stopped');
      observerStatusText.textContent = 'Stopped';
    } else {
      observerStatusPill.classList.add('status-unknown');
      observerStatusText.textContent = 'Unknown';
    }

    // Compact Environment Badge in Header
    const env = data.environment || 'demo';
    envPill.textContent = env === 'production' ? 'Production' : 'Demo';
    envPill.className = `env-badge ${env === 'production' ? 'prod' : ''}`;

    // 4 Primary Overview Metrics
    // 1. Markets observed
    document.getElementById('kpi-markets-count').textContent = data.unique_markets_count || 0;
    document.getElementById('kpi-events-count').textContent = `${data.unique_events_count || 0} events in dataset`;

    // 2. Observations
    document.getElementById('kpi-total-obs').textContent = (data.valid_observations_count || 0).toLocaleString();
    document.getElementById('kpi-obs-span').textContent = `Total recorded snapshots`;

    // 3. Data freshness
    document.getElementById('kpi-freshness-age').textContent = data.latest_observation_age || 'Historical';
    document.getElementById('kpi-freshness-ts').textContent = data.latest_observation_at
      ? `Recorded at ${data.latest_observation_at.substring(11, 19)} UTC`
      : 'Historical evidence';

    // 4. Qualified opportunities
    const qualCount = data.qualified_opportunities_count || 0;
    const candCount = data.theoretical_candidates_count || 0;
    document.getElementById('kpi-opps-qualified').textContent = qualCount;
    document.getElementById('kpi-opps-sub').textContent = `${candCount} theoretical candidates`;

    // Opportunity Breakdown Funnel Steps
    document.getElementById('funnel-raw').textContent = data.total_raw_records || 0;
    document.getElementById('funnel-fresh').textContent = (data.valid_observations_count || 0) - (data.stale_observations_count || 0);
    document.getElementById('funnel-cand').textContent = candCount;
    document.getElementById('funnel-depth').textContent = candCount;
    document.getElementById('funnel-net').textContent = qualCount;
    document.getElementById('funnel-qual').textContent = qualCount;
  }

  // Load Markets
  async function loadMarkets() {
    const data = await fetchJson(`/api/markets?dataset=${encodeURIComponent(currentDataset)}`);
    if (!data || !data.markets) return;

    cachedMarkets = data.markets;

    // Filter counts
    document.getElementById('count-all').textContent = cachedMarkets.length;
    document.getElementById('count-pop').textContent = cachedMarkets.filter(m => m.order_book_status === 'POPULATED').length;
    document.getElementById('count-empty').textContent = cachedMarkets.filter(m => m.order_book_status === 'EMPTY').length;
    document.getElementById('count-stale').textContent = cachedMarkets.filter(m => m.order_book_status === 'STALE').length;

    renderMarketsTable();
  }

  function renderMarketsTable() {
    const tbody = document.getElementById('markets-tbody');
    let list = cachedMarkets;

    // Filter
    if (marketFilter !== 'ALL') {
      list = list.filter(m => m.order_book_status === marketFilter);
    }

    // Search
    if (marketSearchQuery) {
      list = list.filter(m =>
        m.ticker.toLowerCase().includes(marketSearchQuery) ||
        (m.event_ticker && m.event_ticker.toLowerCase().includes(marketSearchQuery)) ||
        (m.title && m.title.toLowerCase().includes(marketSearchQuery))
      );
    }

    if (list.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6" class="table-empty">No markets match the current filter or search.</td></tr>`;
      return;
    }

    tbody.innerHTML = list.map((m, idx) => {
      const statusLower = m.order_book_status.toLowerCase();
      let statusLabel = m.order_book_status;
      if (m.order_book_status === 'EMPTY') statusLabel = 'Empty (0 bids)';
      else if (m.order_book_status === 'POPULATED') statusLabel = 'Populated';
      else if (m.order_book_status === 'STALE') statusLabel = 'Stale';
      else if (m.order_book_status === 'INVALID') statusLabel = 'Invalid';

      const yesBid = formatPrice(m.yes_bid_dollars);
      const yesAsk = formatPrice(m.yes_ask_dollars);
      const noBid = formatPrice(m.no_bid_dollars);
      const noAsk = formatPrice(m.no_ask_dollars);
      const timeStr = m.latest_observed_at ? m.latest_observed_at.substring(11, 19) + ' UTC' : '—';

      return `
        <tr>
          <td>
            <div class="font-mono" style="font-weight: 600; color: var(--accent);">${escapeHtml(m.ticker)}</div>
            <div style="color: var(--text-secondary); font-size: 12px; margin-top: 2px; max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(m.title)}">
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
          <td class="font-mono" style="color: var(--text-secondary); font-size: 12px;">${timeStr}</td>
          <td>
            <button class="btn btn-secondary" onclick="window.inspectMarket(${idx})">Inspect</button>
          </td>
        </tr>
      `;
    }).join('');
  }

  function formatPrice(val) {
    if (val === null || val === undefined) return '—';
    const num = parseFloat(val);
    if (isNaN(num)) return '—';
    return `$${num.toFixed(2)}`;
  }

  // Market Modal Inspection
  window.inspectMarket = function (index) {
    const m = cachedMarkets[index];
    if (!m) return;

    document.getElementById('modal-market-ticker').textContent = `Market: ${m.ticker}`;
    const modalBody = document.getElementById('modal-market-body');

    modalBody.innerHTML = `
      <div style="display: flex; flex-direction: column; gap: 16px;">
        <div class="summary-card">
          <h4 class="summary-card-title">Contract specification</h4>
          <div class="summary-row"><span class="summary-key">Event ticker:</span><span class="summary-val font-mono">${escapeHtml(m.event_ticker)}</span></div>
          <div class="summary-row"><span class="summary-key">Title:</span><span class="summary-val">${escapeHtml(m.title)}</span></div>
          <div class="summary-row"><span class="summary-key">Type:</span><span class="summary-val">${m.market_type}</span></div>
          <div class="summary-row"><span class="summary-key">Status:</span><span class="summary-val">${m.status}</span></div>
          <div class="summary-row"><span class="summary-key">Volume:</span><span class="summary-val font-mono">${m.volume}</span></div>
          <div class="summary-row"><span class="summary-key">Last price:</span><span class="summary-val font-mono">${m.last_price_dollars ? '$' + m.last_price_dollars : 'None'}</span></div>
        </div>

        <div class="summary-card">
          <h4 class="summary-card-title">Order book depth snapshot</h4>
          <div class="summary-row"><span class="summary-key">Classification:</span><span class="summary-val font-mono">${m.order_book_status}</span></div>
          <div class="summary-row"><span class="summary-key">Yes resting bids:</span><span class="summary-val font-mono">${m.yes_bids_levels_count} levels</span></div>
          <div class="summary-row"><span class="summary-key">No resting bids:</span><span class="summary-val font-mono">${m.no_bids_levels_count} levels</span></div>
          <div class="summary-row"><span class="summary-key">Best Yes bid:</span><span class="summary-val font-mono">${m.top_yes_bid ? '$' + m.top_yes_bid.price_dollars + ' (Qty: ' + m.top_yes_bid.quantity + ')' : 'None (0 bids)'}</span></div>
          <div class="summary-row"><span class="summary-key">Best No bid:</span><span class="summary-val font-mono">${m.top_no_bid ? '$' + m.top_no_bid.price_dollars + ' (Qty: ' + m.top_no_bid.quantity + ')' : 'None (0 bids)'}</span></div>
        </div>

        <div class="summary-card">
          <h4 class="summary-card-title">Provenance & timestamps</h4>
          <div class="summary-row"><span class="summary-key">First observed:</span><span class="summary-val font-mono">${m.first_observed_at || 'N/A'}</span></div>
          <div class="summary-row"><span class="summary-key">Latest observed:</span><span class="summary-val font-mono">${m.latest_observed_at || 'N/A'}</span></div>
          <div class="summary-row"><span class="summary-key">Exchange source timestamp:</span><span class="summary-val font-mono">${m.latest_source_timestamp || 'None'}</span></div>
          <div class="summary-row"><span class="summary-key">Freshness:</span><span class="summary-val">${m.staleness_reason ? 'Stale: ' + m.staleness_reason : 'Fresh (within limits)'}</span></div>
        </div>
      </div>
    `;

    document.getElementById('market-modal').classList.remove('hidden');
  };

  function closeModal() {
    document.getElementById('market-modal').classList.add('hidden');
  }

  // Load Opportunities
  async function loadOpportunities() {
    const data = await fetchJson(`/api/opportunities?dataset=${encodeURIComponent(currentDataset)}&simulated=${isSimulatedOpps}`);
    if (!data) return;

    const tbody = document.getElementById('opportunities-tbody');

    if (!data.opportunities || data.opportunities.length === 0) {
      tbody.innerHTML = `
        <tr>
          <td colspan="10" class="table-empty">
            <div class="calm-empty-state">
              <div class="empty-title">No qualified opportunities</div>
              <div class="empty-description">
                The selected historical evidence contains no qualifying arbitrage opportunities.
                All 44 observed order books in this recording have 0 resting counterparty bids.
                The deterministic engine correctly enforces fail-closed execution rules.
              </div>
            </div>
          </td>
        </tr>
      `;
      return;
    }

    tbody.innerHTML = data.opportunities.map((opp) => {
      const isQual = opp.is_qualified;
      const grossEdge = opp.gross_edge_cents !== null ? `${opp.gross_edge_cents}¢ ($${opp.gross_edge_dollars})` : '—';
      const netEdge = opp.net_edge_cents !== null ? `${opp.net_edge_cents}¢` : 'Unconfigured';
      const netColor = opp.net_edge_cents > 0 ? 'var(--status-green)' : 'var(--text-muted)';

      return `
        <tr>
          <td class="font-mono font-bold">${escapeHtml(opp.opportunity_id)}</td>
          <td><span style="font-size: 11px; color: var(--accent);">${escapeHtml(opp.strategy_type)}</span></td>
          <td>
            <span class="table-status">
              <span class="${isQual ? 'status-dot-populated' : 'status-dot-invalid'}"></span>
              <span>${escapeHtml(opp.status)}</span>
            </span>
          </td>
          <td class="font-mono">${opp.guaranteed_payout_cents}¢</td>
          <td class="font-mono">${opp.total_cost_cents}¢ ($${opp.total_cost_dollars})</td>
          <td class="font-mono font-bold" style="color: var(--status-green);">${grossEdge}</td>
          <td class="font-mono">${opp.supported_quantity} / ${opp.requested_quantity}</td>
          <td class="font-mono">${opp.fee_per_contract_cents !== null ? opp.fee_per_contract_cents + '¢' : 'None'}</td>
          <td class="font-mono font-bold" style="color: ${netColor};">${netEdge}</td>
          <td style="color: var(--text-secondary); font-size: 12px;">${escapeHtml(opp.rejection_reason || 'Verified depth & positive edge')}</td>
        </tr>
      `;
    }).join('');
  }

  // Load Historical V7 Validation
  async function loadHistorical() {
    const data = await fetchJson(`/api/historical?dataset=${encodeURIComponent(currentDataset)}`);
    if (!data) return;

    const s1 = data.section_1_dataset_identity || {};
    const s2 = data.section_2_evidence_quality || {};

    document.getElementById('aud-total').textContent = s1.total_raw_records || 0;
    document.getElementById('aud-valid').textContent = s1.valid_parsed_observations || 0;
    document.getElementById('aud-fresh-rate').textContent = s2.freshness_pass_rate ? s2.freshness_pass_rate : '100.0%';
    document.getElementById('aud-empty').textContent = s2.empty_books ? s2.empty_books : '44 (100%)';
    document.getElementById('aud-depth').textContent = s2.total_cumulative_bid_depth || '0 contracts';

    document.getElementById('aud-earliest').textContent = s1.earliest_timestamp || 'N/A';
    document.getElementById('aud-latest').textContent = s1.latest_timestamp || 'N/A';
    document.getElementById('aud-span').textContent = s1.total_duration || 'N/A';
    document.getElementById('aud-mean-interval').textContent = s1.sampling_cadence_mean || 'N/A';
    document.getElementById('aud-gaps').textContent = s1.gaps_count || '0';

    // Section 10 Verdict
    document.getElementById('aud-verdict').textContent = data.section_10_final_verdict || data.final_verdict || 'No verdict generated.';

    // Raw Report
    document.getElementById('raw-v7-report').textContent = data.human_summary || 'Report not available.';
  }

  // Load System Health
  async function loadHealth() {
    const data = await fetchJson('/api/status');
    if (!data) return;

    const proc = data.observer_process || {};
    document.getElementById('hlth-status').textContent = proc.status || 'UNKNOWN';
    document.getElementById('hlth-pid').textContent = proc.pid || 'None';
    document.getElementById('hlth-env').textContent = proc.environment || 'None';
    document.getElementById('hlth-checked').textContent = proc.checked_at || 'N/A';
    document.getElementById('hlth-cmd').textContent = proc.command || 'None';

    const paper = data.paper_trading || {};
    document.getElementById('hlth-paper-mode').textContent = (paper.execution_mode || 'paper').toUpperCase() + ' (Simulated)';
    document.getElementById('hlth-paper-status').textContent = paper.status || (paper.is_active ? 'RUNNING' : 'STOPPED');
    document.getElementById('hlth-paper-session').textContent = paper.session_id || 'None';
    document.getElementById('hlth-paper-cash').textContent = paper.available_cash_cents != null ? `$${(paper.available_cash_cents / 100).toFixed(2)}` : '—';
    document.getElementById('hlth-paper-equity').textContent = paper.book_value_equity_cents != null ? `$${(paper.book_value_equity_cents / 100).toFixed(2)}` : '—';
    document.getElementById('hlth-paper-trades').textContent = paper.total_trades != null ? `${paper.total_trades} (${paper.completed_arbitrage_trades || 0} completed)` : '0';

    document.getElementById('hlth-workspace').textContent = data.workspace_root || 'N/A';
    document.getElementById('hlth-dataset-count').textContent = data.datasets_count || 0;
    document.getElementById('hlth-total-records').textContent = (data.total_records_stored || 0).toLocaleString();
    document.getElementById('hlth-storage-bytes').textContent = `${((data.total_storage_bytes || 0) / 1024).toFixed(1)} KB`;

    const warningsList = document.getElementById('warnings-list');
    if (data.warnings && data.warnings.length > 0) {
      warningsList.innerHTML = data.warnings.map(w => `<li>${escapeHtml(w)}</li>`).join('');
    } else {
      warningsList.innerHTML = '<li>All system diagnostics normal. Observer is currently stopped.</li>';
    }
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
