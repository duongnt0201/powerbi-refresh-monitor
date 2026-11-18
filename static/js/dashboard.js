/**
 * Power BI Refresh Dashboard - Frontend Logic
 * Handles API calls, rendering, auto-refresh, filtering, and modal interactions.
 */

class Dashboard {
    constructor(refreshInterval) {
        this.refreshInterval = refreshInterval;
        this.countdown = refreshInterval;
        this.autoRefreshEnabled = true;
        this.countdownTimer = null;
        this.data = null;
        this.currentFilter = 'all';
        this.searchQuery = '';
        this.timeFilter = '30d';
        this.typeFilter = 'all';
        this.workspaceFilter = [];  // [] = tất cả, ['ws1','ws2'] = chỉ hiển thị các workspace đã chọn
        this.allWorkspaces = [];    // Danh sách tên workspace unique
        this.processedData = null;
        this.processedSummary = null;
        this.isLoading = true;

        this.init();
    }

    async init() {
        this.bindEvents();
        this.showLoading();
        await this.fetchAndRender();
        this.startAutoRefresh();
    }

    // ─── Event Bindings ─────────────────────────────
    bindEvents() {
        // Manual refresh button
        document.getElementById('btnRefresh').addEventListener('click', () => {
            this.manualRefresh();
        });

        // Mobile refresh button
        const mobileRefresh = document.getElementById('mobileRefresh');
        if (mobileRefresh) {
            mobileRefresh.addEventListener('click', () => {
                this.manualRefresh();
            });
        }

        // Search input
        document.getElementById('searchInput').addEventListener('input', (e) => {
            this.searchQuery = e.target.value.toLowerCase();
            this.renderTable();
        });

        // Filter buttons
        document.querySelectorAll('.filter-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                this.currentFilter = btn.dataset.filter;
                document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.renderTable();
            });
        });

        // Time filter buttons
        document.querySelectorAll('.time-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                this.timeFilter = btn.dataset.time;
                document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');

                if (this.data) {
                    this.processData();
                    this.renderSummaryCards(this.processedSummary);
                    this.renderTable();
                    this.renderCharts();
                }
            });
        });

        // Type filter buttons
        document.querySelectorAll('.type-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                this.typeFilter = btn.dataset.type;
                document.querySelectorAll('.type-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');

                if (this.data) {
                    this.processData();
                    this.renderSummaryCards(this.processedSummary);
                    this.renderTable();
                    this.renderCharts();
                }
            });
        });

        // Modal close
        document.getElementById('modalOverlay').addEventListener('click', (e) => {
            if (e.target === e.currentTarget) this.closeModal();
        });

        document.getElementById('btnCloseModal').addEventListener('click', () => {
            this.closeModal();
        });

        // Keyboard shortcut
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                this.closeModal();
                this.closeWorkspaceDropdown();
            }
            if (e.key === 'r' && !e.ctrlKey && !e.metaKey && document.activeElement.tagName !== 'INPUT') {
                this.manualRefresh();
            }
        });

        // Workspace dropdown toggle
        this.bindWorkspaceDropdown();
    }

    // ─── Workspace Dropdown ─────────────────────────
    bindWorkspaceDropdown() {
        const toggle = document.getElementById('wsDropdownToggle');
        const dropdown = document.getElementById('wsDropdown');

        toggle.addEventListener('click', (e) => {
            e.stopPropagation();
            dropdown.classList.toggle('open');
            if (dropdown.classList.contains('open')) {
                document.getElementById('wsSearchInput').focus();
            }
        });

        // Đóng dropdown khi click ra ngoài
        document.addEventListener('click', (e) => {
            if (!dropdown.contains(e.target)) {
                dropdown.classList.remove('open');
            }
        });

        // Search trong dropdown
        document.getElementById('wsSearchInput').addEventListener('input', (e) => {
            const q = e.target.value.toLowerCase();
            document.querySelectorAll('#wsDropdownList .ws-dropdown-item').forEach(item => {
                const name = item.dataset.wsName.toLowerCase();
                item.classList.toggle('hidden', !name.includes(q));
            });
        });

        // Chọn tất cả
        const selectAllLabel = document.getElementById('wsSelectAll');
        const selectAllCb = selectAllLabel.querySelector('input');

        selectAllCb.addEventListener('change', () => {
            const checked = selectAllCb.checked;
            document.querySelectorAll('#wsDropdownList input[type="checkbox"]').forEach(cb => {
                cb.checked = checked;
            });
            this.workspaceFilter = checked ? [] : ['__NONE__'];
            this.onWorkspaceFilterChange();
        });
    }

    buildWorkspaceDropdown() {
        if (!this.data || !this.data.datasets) return;

        // Lấy danh sách workspace unique, sắp xếp A-Z
        const wsSet = new Set();
        for (const ds of this.data.datasets) {
            if (ds.workspace_name) wsSet.add(ds.workspace_name);
        }
        this.allWorkspaces = [...wsSet].sort();

        const listEl = document.getElementById('wsDropdownList');
        listEl.innerHTML = this.allWorkspaces.map(name => {
            const isChecked = this.workspaceFilter.length === 0 || this.workspaceFilter.includes(name);
            return `<label class="ws-dropdown-item" data-ws-name="${this.escapeHtml(name)}">
                <input type="checkbox" value="${this.escapeHtml(name)}" ${isChecked ? 'checked' : ''}> ${this.escapeHtml(name)}
            </label>`;
        }).join('');

        // Bind change events
        listEl.querySelectorAll('input[type="checkbox"]').forEach(cb => {
            cb.addEventListener('change', () => this.onWorkspaceCheckboxChange());
        });

        this.updateWorkspaceLabel();
    }

    onWorkspaceCheckboxChange() {
        const checkboxes = document.querySelectorAll('#wsDropdownList input[type="checkbox"]');
        const checked = [...checkboxes].filter(cb => cb.checked).map(cb => cb.value);
        const allChecked = checked.length === this.allWorkspaces.length;

        // Cập nhật "Chọn tất cả"
        document.querySelector('#wsSelectAll input').checked = allChecked;

        // Nếu chọn hết → mảng rỗng (= tất cả)
        this.workspaceFilter = allChecked ? [] : checked;
        this.onWorkspaceFilterChange();
    }

    onWorkspaceFilterChange() {
        this.updateWorkspaceLabel();
        if (this.data) {
            this.processData();
            this.renderSummaryCards(this.processedSummary);
            this.renderTable();
            this.renderCharts();
        }
    }

    updateWorkspaceLabel() {
        const label = document.getElementById('wsDropdownLabel');
        if (this.workspaceFilter.length === 0) {
            label.textContent = `Tất cả workspace (${this.allWorkspaces.length})`;
        } else if (this.workspaceFilter[0] === '__NONE__') {
            label.textContent = 'Chưa chọn workspace nào';
        } else {
            label.textContent = `${this.workspaceFilter.length}/${this.allWorkspaces.length} workspace`;
        }
    }

    closeWorkspaceDropdown() {
        document.getElementById('wsDropdown').classList.remove('open');
    }

    // ─── Data Fetching ─────────────────────────────
    async fetchStatus() {
        try {
            const response = await fetch('/api/status');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            this.data = await response.json();
            this.updateConnectionStatus('connected');
            return this.data;
        } catch (error) {
            console.error('Failed to fetch status:', error);
            this.updateConnectionStatus('disconnected');
            this.showToast(`Không thể kết nối server: ${error.message}`, 'error');
            return null;
        }
    }

    async fetchAndRender() {
        this.setRefreshing(true);
        const data = await this.fetchStatus();
        if (data) {
            this.buildWorkspaceDropdown();
            this.processData();
            this.renderSummaryCards(this.processedSummary);
            this.renderTable();
            this.renderCharts();
            this.updateTimestamp(data.timestamp);
            this.hideError();
        }
        this.setRefreshing(false);
        this.isLoading = false;
    }

    async manualRefresh() {
        this.countdown = this.refreshInterval;
        await this.fetchAndRender();
        this.showToast('Đã cập nhật dữ liệu', 'success');
    }

    // ─── Auto Refresh ─────────────────────────────
    startAutoRefresh() {
        if (this.countdownTimer) clearInterval(this.countdownTimer);
        this.countdown = this.refreshInterval;

        this.countdownTimer = setInterval(() => {
            if (!this.autoRefreshEnabled) return;

            this.countdown--;
            this.updateCountdown();

            if (this.countdown <= 0) {
                this.countdown = this.refreshInterval;
                this.fetchAndRender();
            }
        }, 1000);
    }

    updateCountdown() {
        const numberEl = document.getElementById('countdownNumber');
        const progressEl = document.getElementById('countdownProgress');

        if (numberEl) numberEl.textContent = this.countdown;

        if (progressEl) {
            const circumference = 2 * Math.PI * 14; // radius = 14
            const progress = (this.countdown / this.refreshInterval) * circumference;
            progressEl.style.strokeDasharray = circumference;
            progressEl.style.strokeDashoffset = circumference - progress;
        }
    }

    // ─── Data Processing ────────────────────────
    processData() {
        if (!this.data) return;

        const processedDatasets = [];

        const now = new Date();
        let fromDate = null;
        if (this.timeFilter === 'today') {
            fromDate = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        } else if (this.timeFilter === '24h') {
            fromDate = new Date(now - 24 * 60 * 60 * 1000);
        } else if (this.timeFilter === '7d') {
            fromDate = new Date(now - 7 * 24 * 60 * 60 * 1000);
        } else if (this.timeFilter === '30d') {
            fromDate = new Date(now - 30 * 24 * 60 * 60 * 1000);
        }

        let totalSuccess = 0;
        let totalFailed = 0;
        let totalInProgress = 0;
        let totalRefreshSuccess = 0;
        let totalRefreshCount = 0;
        let allDurations = [];

        for (const ds of this.data.datasets) {
            // Workspace filter
            if (this.workspaceFilter.length > 0 && !this.workspaceFilter.includes(ds.workspace_name)) {
                continue;
            }

            let filteredHistory = ds.history || [];

            if (fromDate) {
                filteredHistory = filteredHistory.filter(h => {
                    if (!h.start_time) return false;
                    return new Date(h.start_time) >= fromDate;
                });
            }

            // Apply type filter
            if (this.typeFilter !== 'all') {
                filteredHistory = filteredHistory.filter(h => {
                    if (this.typeFilter === 'Other') {
                        return h.refresh_type !== 'Scheduled' && h.refresh_type !== 'OnDemand';
                    }
                    return h.refresh_type === this.typeFilter;
                });
            }

            const latest = filteredHistory.length > 0 ? filteredHistory[0] : null;
            const dsTotal = filteredHistory.length;
            const dsSuccess = filteredHistory.filter(h => h.status === 'Completed').length;
            const dsFailed = filteredHistory.filter(h => h.status === 'Failed').length;

            let dsAvgDuration = null;
            const completedWithDur = filteredHistory.filter(h => h.status === 'Completed' && h.duration_seconds != null);
            if (completedWithDur.length > 0) {
                const totalDur = completedWithDur.reduce((sum, h) => sum + h.duration_seconds, 0);
                dsAvgDuration = totalDur / completedWithDur.length;
                allDurations.push(dsAvgDuration);
            }

            const stats = {
                total: dsTotal,
                success: dsSuccess,
                failed: dsFailed,
                success_rate: dsTotal > 0 ? Math.round(dsSuccess / dsTotal * 100 * 10) / 10 : 0,
                avg_duration_seconds: dsAvgDuration
            };

            processedDatasets.push({
                ...ds,
                latest: latest,
                stats: stats
            });

            if (latest) {
                if (latest.status === 'Completed') totalSuccess++;
                else if (latest.status === 'Failed') totalFailed++;
                else if (!['Cancelled', 'Disabled', 'Unknown'].includes(latest.status)) totalInProgress++;
            }

            totalRefreshSuccess += dsSuccess;
            totalRefreshCount += dsTotal;
        }

        const totalDatasets = processedDatasets.length;
        this.processedData = {
            ...this.data,
            datasets: processedDatasets
        };

        const overallAvg = allDurations.length > 0 ? allDurations.reduce((a, b) => a + b, 0) / allDurations.length : null;

        this.processedSummary = {
            total_datasets: totalDatasets,
            success: totalSuccess,
            failed: totalFailed,
            in_progress: totalInProgress,
            overall_success_rate: totalDatasets > 0 ? Math.round(totalSuccess / totalDatasets * 100 * 10) / 10 : 0,
            avg_duration_seconds: overallAvg,
            total_refresh_success: totalRefreshSuccess,
            total_refresh_count: totalRefreshCount
        };
    }

    // ─── Rendering: Summary Cards ─────────────────
    renderSummaryCards(summary) {
        if (!summary) return;

        this.animateValue('totalDatasets', summary.total_datasets);

        const successRate = summary.overall_success_rate;
        document.getElementById('successRate').textContent = successRate + '%';
        document.getElementById('successSubtitle').innerHTML =
            `<span class="trend ${successRate >= 90 ? 'up' : 'down'}">${summary.success}/${summary.total_datasets}</span> datasets OK`;

        // Refresh ratio card (total success / total refreshes)
        const rSuccess = summary.total_refresh_success || 0;
        const rTotal = summary.total_refresh_count || 0;
        document.getElementById('refreshRatio').textContent = `${rSuccess}/${rTotal}`;
        const rRate = rTotal > 0 ? Math.round(rSuccess / rTotal * 100 * 10) / 10 : 0;
        document.getElementById('refreshRatioSubtitle').textContent =
            `thanh cong / tong luot (${rRate}%)`;

        this.animateValue('failedCount', summary.failed);
        document.getElementById('failedSubtitle').textContent =
            summary.failed === 0 ? 'Khong co loi nao' : `${summary.failed} dataset(s) bi loi`;

        if (summary.avg_duration_seconds) {
            document.getElementById('avgDuration').textContent = this.formatDuration(summary.avg_duration_seconds);
        } else {
            document.getElementById('avgDuration').textContent = '\u2014';
        }

        // Update in_progress if any
        if (summary.in_progress > 0) {
            document.getElementById('totalSubtitle').textContent =
                `${summary.in_progress} dang refresh`;
        } else {
            document.getElementById('totalSubtitle').textContent = 'datasets dang theo doi';
        }
    }

    animateValue(elementId, value) {
        const el = document.getElementById(elementId);
        if (!el) return;

        const current = parseInt(el.textContent) || 0;
        if (current === value) {
            el.textContent = value;
            return;
        }

        const duration = 400;
        const start = performance.now();

        const step = (timestamp) => {
            const progress = Math.min((timestamp - start) / duration, 1);
            const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
            el.textContent = Math.round(current + (value - current) * eased);
            if (progress < 1) requestAnimationFrame(step);
        };

        requestAnimationFrame(step);
    }

    // ─── Rendering: Charts ──────────────────────────
    renderCharts() {
        if (!this.processedData || !this.processedData.datasets) return;
        if (!window.Chart) return;

        const now = new Date();
        let fromDate = null;
        if (this.timeFilter === 'today') {
            fromDate = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        } else if (this.timeFilter === '24h') {
            fromDate = new Date(now - 24 * 60 * 60 * 1000);
        } else if (this.timeFilter === '7d') {
            fromDate = new Date(now - 7 * 24 * 60 * 60 * 1000);
        } else if (this.timeFilter === '30d') {
            fromDate = new Date(now - 30 * 24 * 60 * 60 * 1000);
        }

        const statsByDate = {};
        const failuresByDataset = {};
        const avgDurationByDataset = {};

        this.processedData.datasets.forEach(ds => {
            // Workspace filter
            if (this.workspaceFilter.length > 0 && !this.workspaceFilter.includes(ds.workspace_name)) return;

            let filteredHistory = ds.history || [];
            if (fromDate) {
                filteredHistory = filteredHistory.filter(h => h.start_time && new Date(h.start_time) >= fromDate);
            }
            if (this.typeFilter !== 'all') {
                filteredHistory = filteredHistory.filter(h => {
                    if (this.typeFilter === 'Other') return h.refresh_type !== 'Scheduled' && h.refresh_type !== 'OnDemand';
                    return h.refresh_type === this.typeFilter;
                });
            }

            let dsFailedCount = 0;

            let dsTotalDur = 0;
            let dsCountDur = 0;

            filteredHistory.forEach(h => {
                const dateObj = new Date(h.start_time);
                let key = '';
                if (this.timeFilter === 'today' || this.timeFilter === '24h') {
                    key = `${dateObj.getDate().toString().padStart(2, '0')}/${(dateObj.getMonth() + 1).toString().padStart(2, '0')} ${dateObj.getHours().toString().padStart(2, '0')}:00`;
                } else {
                    key = `${dateObj.getDate().toString().padStart(2, '0')}/${(dateObj.getMonth() + 1).toString().padStart(2, '0')}`;
                }

                if (!statsByDate[key]) {
                    statsByDate[key] = { success: 0, failed: 0, duration_sum: 0, duration_count: 0, timestamp: dateObj.getTime() };
                }

                if (h.status === 'Completed') {
                    statsByDate[key].success++;
                    if (h.duration_seconds != null) {
                        statsByDate[key].duration_sum += h.duration_seconds;
                        statsByDate[key].duration_count++;

                        dsTotalDur += h.duration_seconds;
                        dsCountDur++;
                    }
                } else if (h.status === 'Failed') {
                    statsByDate[key].failed++;
                    dsFailedCount++;
                }
            });

            if (dsFailedCount > 0) {
                failuresByDataset[ds.dataset_name] = dsFailedCount;
            }

            if (dsCountDur > 0) {
                avgDurationByDataset[ds.dataset_name] = dsTotalDur / dsCountDur / 60; // Tinh bang phut
            }
        });

        const timeLabels = Object.keys(statsByDate).sort((a, b) => statsByDate[a].timestamp - statsByDate[b].timestamp);
        const srData = [];
        const avgDurationData = [];

        timeLabels.forEach(k => {
            const d = statsByDate[k];
            const total = d.success + d.failed;
            const sr = total > 0 ? (d.success / total * 100) : 0;
            const avgDurSec = d.duration_count > 0 ? (d.duration_sum / d.duration_count) : 0;
            const avgDurMin = avgDurSec / 60; // Doi sang phut

            srData.push(sr.toFixed(1));
            avgDurationData.push(avgDurMin.toFixed(1));
        });

        const sortedFailures = Object.keys(failuresByDataset).sort((a, b) => failuresByDataset[b] - failuresByDataset[a]).slice(0, 10);
        const topFailedCounts = sortedFailures.map(k => failuresByDataset[k]);

        // Top 10 longest duration
        const sortedDurations = Object.keys(avgDurationByDataset).sort((a, b) => avgDurationByDataset[b] - avgDurationByDataset[a]).slice(0, 10);
        const topDurations = sortedDurations.map(k => parseFloat(avgDurationByDataset[k].toFixed(1)));

        // Biểu đồ Rate (Line with Gradient)
        this.initChart('successRateChart', 'line', timeLabels, [
            {
                label: 'Tỉ Lệ HT (%)',
                data: srData,
                borderColor: '#10b981',
                borderWidth: 3,
                pointBackgroundColor: '#10b981',
                pointBorderColor: 'rgba(255,255,255,0.2)',
                pointHoverRadius: 6,
                backgroundColor: (context) => {
                    const chart = context.chart;
                    const { ctx, chartArea } = chart;
                    if (!chartArea) return null;
                    const gradient = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
                    gradient.addColorStop(0, 'rgba(16, 185, 129, 0.2)');
                    gradient.addColorStop(1, 'rgba(16, 185, 129, 0)');
                    return gradient;
                },
                fill: true,
                tension: 0.4
            }
        ]);

        // Biểu đồ Thất bại (Bar with rounded corners)
        const shortNamesFail = sortedFailures.map(name => name.length > 25 ? name.substring(0, 22) + '...' : name);
        this.initChart('failedChart', 'bar', shortNamesFail, [
            {
                label: 'Số Lần Lỗi',
                data: topFailedCounts,
                backgroundColor: '#f43f5e',
                borderRadius: 20, // Bo tròn cực đại
                barThickness: 12,
            }
        ], { indexAxis: 'y' }, { y: { display: true }, x: { display: false } });

        // Biểu đồ Thời gian (Line with Blue Glow)
        this.initChart('durationChart', 'line', timeLabels, [
            {
                label: 'TG Trung bình (Phút)',
                data: avgDurationData,
                borderColor: '#0ea5e9',
                borderWidth: 3,
                pointRadius: 0, // Không show point ở trạng thái tĩnh
                pointHoverRadius: 6,
                backgroundColor: (context) => {
                    const chart = context.chart;
                    const { ctx, chartArea } = chart;
                    if (!chartArea) return null;
                    const gradient = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
                    gradient.addColorStop(0, 'rgba(14, 165, 233, 0.2)');
                    gradient.addColorStop(1, 'rgba(14, 165, 233, 0)');
                    return gradient;
                },
                fill: true,
                tension: 0.4
            }
        ]);

        // Biểu đồ Top Lâu nhất (Bar with Amber color)
        const shortNamesDur = sortedDurations.map(name => name.length > 25 ? name.substring(0, 22) + '...' : name);
        this.initChart('topDurationChart', 'bar', shortNamesDur, [
            {
                label: 'Thời gian (Phút)',
                data: topDurations,
                backgroundColor: '#f59e0b',
                borderRadius: 20,
                barThickness: 12,
            }
        ], { indexAxis: 'y' }, { y: { display: true }, x: { display: false } });
    }

    initChart(canvasId, type, labels, datasets, extraOptions = {}, tickOptions = {}) {
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;

        if (this.chartInstances && this.chartInstances[canvasId]) {
            this.chartInstances[canvasId].destroy();
        }

        if (!this.chartInstances) this.chartInstances = {};

        // Modern Chart Defaults
        Chart.defaults.color = 'rgba(255, 255, 255, 0.5)';
        Chart.defaults.font.family = "'Inter', system-ui, sans-serif";
        Chart.defaults.font.size = 11;

        const defaultScales = {
            x: {
                grid: { display: false }, // Ẩn lưới dọc cho hiện đại
                ticks: { padding: 10 }
            },
            y: {
                grid: { color: 'rgba(255,255,255,0.03)', drawBorder: false },
                beginAtZero: true,
                ticks: { padding: 10 }
            }
        };

        if (tickOptions.x) Object.assign(defaultScales.x, tickOptions.x);
        if (tickOptions.y) {
            // Đặc biệt cho Bar chart nằm ngang (indexAxis: 'y')
            if (extraOptions.indexAxis === 'y') {
                defaultScales.x.grid.display = false;
                defaultScales.y.grid.display = false;
            }
            Object.assign(defaultScales.y, tickOptions.y);
        }

        this.chartInstances[canvasId] = new Chart(ctx, {
            type: type,
            data: { labels: labels, datasets: datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: {
                    duration: 1500,
                    easing: 'easeOutQuart'
                },
                ...extraOptions,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: '#1e293b',
                        titleFont: { size: 13, weight: 'bold' },
                        bodyFont: { size: 12 },
                        padding: 12,
                        cornerRadius: 8,
                        displayColors: true,
                        usePointStyle: true
                    }
                },
                scales: defaultScales
            }
        });
    }

    // ─── Rendering: Dataset Table ─────────────────
    renderTable() {
        if (!this.data) return;

        const tbody = document.getElementById('datasetTableBody');
        const datasets = this.getFilteredDatasets();

        // Update filter counts
        this.updateFilterCounts();

        // Update dataset count
        const countEl = document.querySelector('.dataset-count');
        if (countEl) countEl.textContent = datasets.length;

        if (datasets.length === 0) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="8">
                        <div class="empty-state">
                            <div class="empty-state-icon">📊</div>
                            <h3>${this.searchQuery ? 'Không tìm thấy kết quả' : 'Chưa có dữ liệu'}</h3>
                            <p>${this.searchQuery
                    ? 'Thử thay đổi từ khóa tìm kiếm'
                    : 'Kiểm tra pbi_config.yaml và đảm bảo đã thêm dataset cần theo dõi'}</p>
                        </div>
                    </td>
                </tr>`;
            return;
        }

        tbody.innerHTML = datasets.map(ds => this.renderTableRow(ds)).join('');

        // Bind history buttons
        tbody.querySelectorAll('.btn-history').forEach(btn => {
            btn.addEventListener('click', () => {
                const dsId = btn.dataset.datasetId;
                const wsId = btn.dataset.workspaceId;
                const ds = this.data.datasets.find(
                    d => d.dataset_id === dsId && d.workspace_id === wsId
                );
                if (ds) this.showHistoryModal(ds);
            });
        });
    }

    renderTableRow(ds) {
        const latest = ds.latest;
        const stats = ds.stats;
        const hasError = latest && latest.status === 'Failed';

        const statusClass = latest ? this.getStatusClass(latest.status) : 'unknown';
        const statusLabel = latest ? this.getStatusLabel(latest.status) : 'N/A';

        // Time display
        let timeHtml = '<span class="text-muted">—</span>';
        if (latest && latest.end_time) {
            const relative = this.getRelativeTime(latest.end_time);
            const absolute = this.formatDateTime(latest.end_time);
            timeHtml = `
                <div class="time-display">
                    <div class="relative">${relative}</div>
                    <div class="absolute">${absolute}</div>
                </div>`;
        } else if (latest && latest.start_time && !latest.end_time) {
            timeHtml = `
                <div class="time-display">
                    <div class="relative" style="color: var(--info)">Đang chạy...</div>
                    <div class="absolute">Bắt đầu: ${this.formatDateTime(latest.start_time)}</div>
                </div>`;
        }

        // Duration
        let durationHtml = '<span class="text-muted">—</span>';
        if (latest && latest.duration_seconds) {
            durationHtml = `<span class="duration-display">${this.formatDuration(latest.duration_seconds)}</span>`;
        }

        // Refresh type
        let typeHtml = '<span class="text-muted">—</span>';
        if (latest && latest.refresh_type) {
            const typeLabel = latest.refresh_type === 'Scheduled' ? '⏰ Scheduled'
                : latest.refresh_type === 'OnDemand' ? '👆 On Demand'
                    : latest.refresh_type;
            typeHtml = `<span class="refresh-type">${typeLabel}</span>`;
        }

        // Success rate with count
        const rate = stats.success_rate;
        const rateClass = rate >= 90 ? 'high' : rate >= 70 ? 'medium' : 'low';
        const rateHtml = `
            <div class="success-rate-bar">
                <div class="rate-bar-container">
                    <div class="rate-bar-fill ${rateClass}" style="width: ${rate}%"></div>
                </div>
                <span class="rate-text ${rateClass}">${rate}% <small>(${stats.success}/${stats.total})</small></span>
            </div>`;

        return `
            <tr class="${hasError ? 'has-error' : ''}">
                <td>
                    <div class="dataset-info">
                        <span class="dataset-name">${this.escapeHtml(ds.dataset_name)}</span>
                        <span class="workspace-name">${this.escapeHtml(ds.workspace_name)}</span>
                    </div>
                </td>
                <td>
                    <span class="status-badge ${statusClass}">
                        <span class="status-indicator"></span>
                        ${statusLabel}
                    </span>
                </td>
                <td>${timeHtml}</td>
                <td class="col-duration">
                    <div class="card-meta-grid">
                        <div class="meta-item">
                            <span class="meta-label">Duration</span>
                            ${durationHtml}
                        </div>
                        <div class="meta-item">
                            <span class="meta-label">Type</span>
                            ${typeHtml}
                        </div>
                    </div>
                </td>
                <td class="col-refresh-type" style="display:none"></td> <!-- Hidden for grid use above -->
                <td class="col-success-rate">${rateHtml}</td>
                <td>
                    <button class="btn-history" data-dataset-id="${ds.dataset_id}" data-workspace-id="${ds.workspace_id}" title="Xem lịch sử refresh">
                        📋 Chi tiết
                    </button>
                </td>
            </tr>`;
    }

    getFilteredDatasets() {
        if (!this.processedData || !this.processedData.datasets) return [];

        let datasets = [...this.processedData.datasets];

        // Workspace filter (already applied in processData, but safety check)
        if (this.workspaceFilter.length > 0) {
            datasets = datasets.filter(d => this.workspaceFilter.includes(d.workspace_name));
        }

        // Apply filter
        if (this.currentFilter === 'success') {
            datasets = datasets.filter(d => d.latest && d.latest.status === 'Completed');
        } else if (this.currentFilter === 'failed') {
            datasets = datasets.filter(d => d.latest && d.latest.status === 'Failed');
        } else if (this.currentFilter === 'running') {
            datasets = datasets.filter(d => d.latest && !['Completed', 'Failed', 'Cancelled'].includes(d.latest.status));
        }

        // Apply search
        if (this.searchQuery) {
            datasets = datasets.filter(d =>
                d.dataset_name.toLowerCase().includes(this.searchQuery) ||
                d.workspace_name.toLowerCase().includes(this.searchQuery)
            );
        }

        return datasets;
    }

    updateFilterCounts() {
        if (!this.processedData || !this.processedData.datasets) return;

        const all = this.processedData.datasets.length;
        const success = this.processedData.datasets.filter(d => d.latest && d.latest.status === 'Completed').length;
        const failed = this.processedData.datasets.filter(d => d.latest && d.latest.status === 'Failed').length;
        const running = this.processedData.datasets.filter(d => d.latest && !['Completed', 'Failed', 'Cancelled'].includes(d.latest.status)).length;

        const counts = { all, success, failed, running };
        document.querySelectorAll('.filter-btn').forEach(btn => {
            const countSpan = btn.querySelector('.count');
            if (countSpan && counts[btn.dataset.filter] !== undefined) {
                countSpan.textContent = `(${counts[btn.dataset.filter]})`;
            }
        });
    }

    // ─── History Modal ─────────────────────────────
    showHistoryModal(dataset) {
        const overlay = document.getElementById('modalOverlay');
        const title = document.getElementById('modalTitle');
        const subtitle = document.getElementById('modalSubtitle');
        const body = document.getElementById('modalBody');

        title.textContent = dataset.dataset_name;
        subtitle.textContent = `${dataset.workspace_name} • ${dataset.stats.total} refreshes gần nhất`;

        if (!dataset.history || dataset.history.length === 0) {
            body.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">📭</div>
                    <h3>Chưa có lịch sử refresh</h3>
                    <p>Dataset này chưa có bản ghi refresh nào</p>
                </div>`;
        } else {
            body.innerHTML = `
                <div class="history-timeline">
                    ${dataset.history.map(h => this.renderHistoryItem(h)).join('')}
                </div>`;
        }

        overlay.classList.add('active');
        document.body.style.overflow = 'hidden';
    }

    renderHistoryItem(item) {
        const statusClass = this.getStatusClass(item.status);
        const statusIcon = item.status === 'Completed' ? '✓'
            : item.status === 'Failed' ? '✗'
                : item.status === 'Cancelled' ? '!' : '?';

        let timeStr = '';
        if (item.start_time) {
            timeStr = this.formatDateTime(item.start_time);
        }

        let durationStr = '';
        if (item.duration_seconds) {
            durationStr = `⏱️ ${this.formatDuration(item.duration_seconds)}`;
        }

        let typeStr = '';
        if (item.refresh_type) {
            typeStr = item.refresh_type;
        }

        let errorHtml = '';
        if (item.error) {
            errorHtml = `<div class="history-error">❌ ${this.escapeHtml(item.error)}</div>`;
        }

        return `
            <div class="history-item">
                <div class="history-status-icon ${statusClass}">${statusIcon}</div>
                <div class="history-content">
                    <div class="history-meta">
                        <span class="history-time">${timeStr}</span>
                        ${durationStr ? `<span class="history-duration">${durationStr}</span>` : ''}
                        ${typeStr ? `<span class="history-type">${typeStr}</span>` : ''}
                    </div>
                    ${errorHtml}
                </div>
            </div>`;
    }

    closeModal() {
        document.getElementById('modalOverlay').classList.remove('active');
        document.body.style.overflow = '';
    }

    // ─── UI State Helpers ─────────────────────────
    showLoading() {
        const tbody = document.getElementById('datasetTableBody');
        tbody.innerHTML = `
            <tr>
                <td colspan="8">
                    <div class="loading-overlay">
                        <div class="loading-spinner">
                            <div class="spinner"></div>
                            <span class="loading-text">Đang tải dữ liệu từ Power BI...</span>
                        </div>
                    </div>
                </td>
            </tr>`;
    }

    setRefreshing(isRefreshing) {
        const btn = document.getElementById('btnRefresh');
        if (isRefreshing) {
            btn.classList.add('refreshing');
            btn.disabled = true;
        } else {
            btn.classList.remove('refreshing');
            btn.disabled = false;
        }
    }

    updateConnectionStatus(status) {
        const el = document.getElementById('connectionStatus');
        const label = document.getElementById('connectionLabel');

        el.className = 'connection-status ' + status;

        if (status === 'connected') {
            label.textContent = 'Connected';
        } else if (status === 'disconnected') {
            label.textContent = 'Disconnected';
        } else {
            label.textContent = 'Connecting...';
        }
    }

    updateTimestamp(isoString) {
        const el = document.getElementById('lastUpdated');
        if (el && isoString) {
            const date = new Date(isoString);
            el.textContent = date.toLocaleTimeString('vi-VN', {
                hour: '2-digit', minute: '2-digit', second: '2-digit'
            });
        }
    }

    showError(message) {
        const banner = document.getElementById('errorBanner');
        const text = document.getElementById('errorText');
        if (banner && text) {
            text.textContent = message;
            banner.classList.add('visible');
        }
    }

    hideError() {
        const banner = document.getElementById('errorBanner');
        if (banner) banner.classList.remove('visible');
    }

    showToast(message, type = 'info') {
        const container = document.getElementById('toastContainer');
        const toast = document.createElement('div');
        toast.className = `toast ${type}`;

        const icon = type === 'success' ? '✅' : type === 'error' ? '❌' : 'ℹ️';
        toast.innerHTML = `<span>${icon}</span><span>${message}</span>`;

        container.appendChild(toast);

        setTimeout(() => {
            toast.classList.add('hiding');
            setTimeout(() => toast.remove(), 300);
        }, 3000);
    }

    async loadHistory() {
        try {
            const response = await fetch('/api/status');
            const data = await response.json();
            
            const tableBody = document.getElementById('historyTableBody');
            if (!tableBody) return;
            
            if (!data.datasets) return;
            
            // Flatten and sort history from all datasets
            let allHistory = [];
            data.datasets.forEach(ds => {
                if (ds.history) {
                    ds.history.forEach(h => {
                        allHistory.push({
                            ...h,
                            workspace_name: ds.workspace_name,
                            dataset_name: ds.dataset_name
                        });
                    });
                }
            });
            
            // Sort by time
            allHistory.sort((a, b) => new Date(b.start_time) - new Date(a.start_time));
            
            tableBody.innerHTML = '';
            
            if (allHistory.length === 0) {
                tableBody.innerHTML = '<tr><td colspan="9" style="text-align: center; padding: 40px; color: var(--text-secondary);">Chưa có dữ liệu lịch sử trong API.</td></tr>';
                return;
            }

            allHistory.forEach((h, index) => {
                const tr = document.createElement('tr');
                
                // Format duration
                let durationStr = 'N/A';
                if (h.duration_seconds) {
                    const mins = Math.floor(h.duration_seconds / 60);
                    const secs = Math.round(h.duration_seconds % 60);
                    durationStr = `${mins}m ${secs}s`;
                }
                
                // Format status class
                const statusClass = h.status === 'Completed' ? 'status-success' : (h.status === 'Failed' ? 'status-failed' : 'status-pending');
                
                tr.innerHTML = `
                    <td>${index + 1}</td>
                    <td style="font-family: monospace; font-size: 11px;">${h.request_id || 'N/A'}</td>
                    <td style="font-weight: 500;">${h.dataset_name || 'Unknown'}</td>
                    <td>${h.workspace_name || 'N/A'}</td>
                    <td><span class="status-badge ${statusClass}">${h.status || 'Unknown'}</span></td>
                    <td>${h.refresh_type || 'N/A'}</td>
                    <td>${h.start_time || 'N/A'}</td>
                    <td>${durationStr}</td>
                    <td class="error-cell" title="${h.error || ''}">${h.error || '-'}</td>
                `;
                tableBody.appendChild(tr);
            });
            
        } catch (error) {
            console.error('Error loading history:', error);
        }
    }

    // ─── Formatting Helpers ─────────────────────────
    getStatusClass(status) {
        const map = {
            'Completed': 'completed',
            'Failed': 'failed',
            'Cancelled': 'cancelled',
            'Disabled': 'disabled',
            'Unknown': 'unknown'
        };
        return map[status] || 'in-progress';
    }

    getStatusLabel(status) {
        const map = {
            'Completed': 'Thành công',
            'Failed': 'Thất bại',
            'Cancelled': 'Đã hủy',
            'Disabled': 'Tắt',
            'Unknown': 'Không rõ'
        };
        return map[status] || 'Đang chạy';
    }

    formatDuration(seconds) {
        if (!seconds && seconds !== 0) return '—';

        if (seconds < 60) {
            return `${Math.round(seconds)}s`;
        } else if (seconds < 3600) {
            const min = Math.floor(seconds / 60);
            const sec = Math.round(seconds % 60);
            return sec > 0 ? `${min}m ${sec}s` : `${min}m`;
        } else {
            const hr = Math.floor(seconds / 3600);
            const min = Math.round((seconds % 3600) / 60);
            return min > 0 ? `${hr}h ${min}m` : `${hr}h`;
        }
    }

    formatDateTime(isoString) {
        if (!isoString) return '—';
        const date = new Date(isoString);
        return date.toLocaleString('vi-VN', {
            day: '2-digit',
            month: '2-digit',
            year: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
        });
    }

    getRelativeTime(isoString) {
        if (!isoString) return '—';

        const date = new Date(isoString);
        const now = new Date();
        const diffMs = now - date;
        const diffSec = Math.floor(diffMs / 1000);
        const diffMin = Math.floor(diffSec / 60);
        const diffHour = Math.floor(diffMin / 60);
        const diffDay = Math.floor(diffHour / 24);

        if (diffSec < 60) return 'Vừa xong';
        if (diffMin < 60) return `${diffMin} phút trước`;
        if (diffHour < 24) return `${diffHour} giờ trước`;
        if (diffDay < 7) return `${diffDay} ngày trước`;
        return this.formatDateTime(isoString);
    }

    escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }
}

// ─── Initialize Dashboard ─────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    const refreshInterval = parseInt(document.body.dataset.refreshInterval) || 60;
    window.dashboard = new Dashboard(refreshInterval);
});
