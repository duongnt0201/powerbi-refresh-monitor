/**
 * Power BI Refresh Dashboard - History Raw Data Page
 * Displays all refresh events in a flat, filterable, sortable table.
 */

class HistoryPage {
    constructor() {
        this.data = null;
        this.allEntries = [];
        this.filteredEntries = [];
        this.currentRange = 'today';
        this.statusFilter = 'all';
        this.searchQuery = '';
        this.sortKey = 'start';
        this.sortAsc = false;
        this.page = 1;
        this.pageSize = 50;

        this.init();
    }

    async init() {
        this.bindEvents();
        this.showLoading();
        await this.fetchAndRender();
    }

    // ─── Events ──────────────────────────────────
    bindEvents() {
        document.getElementById('btnRefresh').addEventListener('click', () => this.fetchAndRender());

        // Mobile refresh button
        const mobileRefresh = document.getElementById('mobileRefresh');
        if (mobileRefresh) {
            mobileRefresh.addEventListener('click', () => {
                this.fetchAndRender();
            });
        }

        // Time range buttons
        document.querySelectorAll('.time-btn:not(#btnApplyCustom)').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.currentRange = btn.dataset.range;
                this.page = 1;
                this.applyFilters();
            });
        });

        // Custom date apply
        document.getElementById('btnApplyCustom').addEventListener('click', () => {
            document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
            document.getElementById('btnApplyCustom').classList.add('active');
            this.currentRange = 'custom';
            this.page = 1;
            this.applyFilters();
        });

        // Status filter
        document.querySelectorAll('.status-filter-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.status-filter-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                this.statusFilter = btn.dataset.status;
                this.page = 1;
                this.applyFilters();
            });
        });

        // Search
        document.getElementById('searchInput').addEventListener('input', (e) => {
            this.searchQuery = e.target.value.toLowerCase();
            this.page = 1;
            this.applyFilters();
        });

        // Sort headers
        document.querySelectorAll('th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const key = th.dataset.sort;
                if (this.sortKey === key) {
                    this.sortAsc = !this.sortAsc;
                } else {
                    this.sortKey = key;
                    this.sortAsc = true;
                }
                this.applyFilters();
            });
        });

        // Export
        document.getElementById('btnExport').addEventListener('click', () => this.exportCSV());

        // Keyboard
        document.addEventListener('keydown', (e) => {
            if (e.key === 'r' && !e.ctrlKey && !e.metaKey && document.activeElement.tagName !== 'INPUT') {
                this.fetchAndRender();
            }
        });
    }

    // ─── Data ────────────────────────────────────
    async fetchAndRender() {
        try {
            const response = await fetch('/api/status');
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            this.data = await response.json();
            this.flattenEntries();
            this.applyFilters();
            this.updateTimestamp(this.data.timestamp);
            this.showToast('Da cap nhat du lieu', 'success');
        } catch (error) {
            console.error('Failed to fetch:', error);
            this.showToast(`Loi ket noi: ${error.message}`, 'error');
        }
    }

    flattenEntries() {
        this.allEntries = [];
        if (!this.data || !this.data.datasets) return;

        for (const ds of this.data.datasets) {
            if (!ds.history) continue;
            for (const h of ds.history) {
                this.allEntries.push({
                    dataset_name: ds.dataset_name,
                    workspace_name: ds.workspace_name,
                    dataset_id: ds.dataset_id,
                    status: h.status,
                    start_time: h.start_time,
                    end_time: h.end_time,
                    duration_seconds: h.duration_seconds,
                    refresh_type: h.refresh_type,
                    error: h.error
                });
            }
        }
    }

    // ─── Filtering ───────────────────────────────
    applyFilters() {
        let entries = [...this.allEntries];

        // Time range filter
        const now = new Date();
        let fromDate = null;

        if (this.currentRange === 'today') {
            fromDate = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        } else if (this.currentRange === '24h') {
            fromDate = new Date(now - 24 * 60 * 60 * 1000);
        } else if (this.currentRange === '7d') {
            fromDate = new Date(now - 7 * 24 * 60 * 60 * 1000);
        } else if (this.currentRange === '30d') {
            fromDate = new Date(now - 30 * 24 * 60 * 60 * 1000);
        } else if (this.currentRange === 'custom') {
            const fromInput = document.getElementById('dateFrom').value;
            const toInput = document.getElementById('dateTo').value;
            if (fromInput) fromDate = new Date(fromInput);
            if (toInput) {
                const toDate = new Date(toInput);
                entries = entries.filter(e => {
                    if (!e.start_time) return false;
                    return new Date(e.start_time) <= toDate;
                });
            }
        }

        if (fromDate && this.currentRange !== 'all') {
            entries = entries.filter(e => {
                if (!e.start_time) return false;
                return new Date(e.start_time) >= fromDate;
            });
        }

        // Status filter
        if (this.statusFilter !== 'all') {
            entries = entries.filter(e => e.status === this.statusFilter);
        }

        // Search filter
        if (this.searchQuery) {
            entries = entries.filter(e =>
                e.dataset_name.toLowerCase().includes(this.searchQuery) ||
                e.workspace_name.toLowerCase().includes(this.searchQuery)
            );
        }

        // Sort
        entries.sort((a, b) => {
            let va, vb;
            switch (this.sortKey) {
                case 'dataset': va = a.dataset_name; vb = b.dataset_name; break;
                case 'workspace': va = a.workspace_name; vb = b.workspace_name; break;
                case 'status': va = a.status; vb = b.status; break;
                case 'start': va = a.start_time || ''; vb = b.start_time || ''; break;
                case 'end': va = a.end_time || ''; vb = b.end_time || ''; break;
                case 'duration': va = a.duration_seconds || 0; vb = b.duration_seconds || 0; break;
                case 'type': va = a.refresh_type || ''; vb = b.refresh_type || ''; break;
                default: va = a.start_time || ''; vb = b.start_time || '';
            }
            if (typeof va === 'string') {
                const cmp = va.localeCompare(vb);
                return this.sortAsc ? cmp : -cmp;
            }
            return this.sortAsc ? va - vb : vb - va;
        });

        this.filteredEntries = entries;
        this.updateStats();
        this.renderTable();
        this.renderPagination();
    }

    // ─── Stats ───────────────────────────────────
    updateStats() {
        const total = this.filteredEntries.length;
        const success = this.filteredEntries.filter(e => e.status === 'Completed').length;
        const failed = this.filteredEntries.filter(e => e.status === 'Failed').length;
        const rate = total > 0 ? Math.round(success / total * 1000) / 10 : 0;

        document.getElementById('rawTotal').textContent = total;
        document.getElementById('rawSuccess').textContent = success;
        document.getElementById('rawFailed').textContent = failed;
        document.getElementById('rawRate').textContent = rate + '%';
    }

    // ─── Render Table ────────────────────────────
    renderTable() {
        const tbody = document.getElementById('rawTableBody');
        const start = (this.page - 1) * this.pageSize;
        const pageEntries = this.filteredEntries.slice(start, start + this.pageSize);

        if (pageEntries.length === 0) {
            tbody.innerHTML = `
                <tr>
                    <td colspan="9">
                        <div class="empty-state">
                            <div class="empty-state-icon">📭</div>
                            <h3>Khong co du lieu trong khung gio nay</h3>
                            <p>Thu thay doi khung gio hoac tu khoa tim kiem</p>
                        </div>
                    </td>
                </tr>`;
            return;
        }

        tbody.innerHTML = pageEntries.map((entry, idx) => {
            const rowNum = start + idx + 1;
            const statusClass = this.getStatusClass(entry.status);
            const statusLabel = this.getStatusLabel(entry.status);

            const startStr = entry.start_time ? this.formatDateTime(entry.start_time) : '\u2014';
            const endStr = entry.end_time ? this.formatDateTime(entry.end_time) : '\u2014';
            const durStr = entry.duration_seconds ? this.formatDuration(entry.duration_seconds) : '\u2014';

            const typeLabel = entry.refresh_type === 'Scheduled' ? '⏰ Scheduled'
                : entry.refresh_type === 'OnDemand' ? '👆 On Demand'
                : (entry.refresh_type || '\u2014');

            const errorStr = entry.error
                ? `<div class="error-cell" title="${this.escapeHtml(entry.error)}">${this.escapeHtml(entry.error.substring(0, 80))}${entry.error.length > 80 ? '...' : ''}</div>`
                : '<span class="text-muted">\u2014</span>';

            return `
                <tr class="${entry.status === 'Failed' ? 'has-error' : ''}">
                    <td>
                        <div class="dataset-info">
                            <span class="dataset-name">${this.escapeHtml(entry.dataset_name)}</span>
                            <span class="workspace-name">${this.escapeHtml(entry.workspace_name)}</span>
                        </div>
                    </td>
                    <td>
                        <span class="status-badge ${statusClass}">
                            <span class="status-indicator"></span>
                            ${statusLabel}
                        </span>
                    </td>
                    <td>
                        <div class="time-display">
                            <div class="relative">${startStr === '\u2014' ? 'N/A' : 'Start: ' + startStr}</div>
                            <div class="absolute">${endStr === '\u2014' ? '' : 'End: ' + endStr}</div>
                        </div>
                    </td>
                    <td class="col-duration">
                        <div class="card-meta-grid">
                            <div class="meta-item">
                                <span class="meta-label">Duration</span>
                                <span class="duration-display">${durStr}</span>
                            </div>
                            <div class="meta-item">
                                <span class="meta-label">Type</span>
                                <span class="refresh-type">${typeLabel}</span>
                            </div>
                        </div>
                    </td>
                    <td class="col-error">
                        <span class="meta-label">System Message</span>
                        ${errorStr}
                    </td>
                </tr>`;
        }).join('');
    }

    // ─── Pagination ──────────────────────────────
    renderPagination() {
        const container = document.getElementById('pagination');
        const totalPages = Math.ceil(this.filteredEntries.length / this.pageSize);

        if (totalPages <= 1) {
            container.innerHTML = '';
            return;
        }

        let html = '';
        // Prev
        html += `<button class="page-btn ${this.page <= 1 ? 'disabled' : ''}" data-page="${this.page - 1}">&laquo;</button>`;

        // Page numbers
        const maxVisible = 7;
        let startPage = Math.max(1, this.page - Math.floor(maxVisible / 2));
        let endPage = Math.min(totalPages, startPage + maxVisible - 1);
        if (endPage - startPage < maxVisible - 1) {
            startPage = Math.max(1, endPage - maxVisible + 1);
        }

        if (startPage > 1) {
            html += `<button class="page-btn" data-page="1">1</button>`;
            if (startPage > 2) html += `<span class="page-ellipsis">...</span>`;
        }

        for (let i = startPage; i <= endPage; i++) {
            html += `<button class="page-btn ${i === this.page ? 'active' : ''}" data-page="${i}">${i}</button>`;
        }

        if (endPage < totalPages) {
            if (endPage < totalPages - 1) html += `<span class="page-ellipsis">...</span>`;
            html += `<button class="page-btn" data-page="${totalPages}">${totalPages}</button>`;
        }

        // Next
        html += `<button class="page-btn ${this.page >= totalPages ? 'disabled' : ''}" data-page="${this.page + 1}">&raquo;</button>`;

        container.innerHTML = html;

        container.querySelectorAll('.page-btn:not(.disabled)').forEach(btn => {
            btn.addEventListener('click', () => {
                this.page = parseInt(btn.dataset.page);
                this.renderTable();
                this.renderPagination();
                window.scrollTo({ top: 0, behavior: 'smooth' });
            });
        });
    }

    // ─── CSV Export ──────────────────────────────
    exportCSV() {
        if (this.filteredEntries.length === 0) {
            this.showToast('Khong co du lieu de xuat', 'error');
            return;
        }

        const headers = ['STT', 'Dataset', 'Workspace', 'Trang thai', 'Bat dau', 'Ket thuc', 'Thoi gian (s)', 'Loai', 'Loi'];
        const rows = this.filteredEntries.map((e, i) => [
            i + 1,
            `"${(e.dataset_name || '').replace(/"/g, '""')}"`,
            `"${(e.workspace_name || '').replace(/"/g, '""')}"`,
            e.status,
            e.start_time || '',
            e.end_time || '',
            e.duration_seconds || '',
            e.refresh_type || '',
            `"${(e.error || '').replace(/"/g, '""')}"`
        ]);

        const bom = '\uFEFF';
        const csv = bom + [headers.join(','), ...rows.map(r => r.join(','))].join('\n');
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `powerbi_refresh_history_${new Date().toISOString().slice(0, 10)}.csv`;
        a.click();
        URL.revokeObjectURL(url);
        this.showToast(`Da xuat ${this.filteredEntries.length} dong ra CSV`, 'success');
    }

    // ─── Helpers ─────────────────────────────────
    showLoading() {
        document.getElementById('rawTableBody').innerHTML = `
            <tr>
                <td colspan="9">
                    <div class="loading-overlay">
                        <div class="loading-spinner">
                            <div class="spinner"></div>
                            <span class="loading-text">Dang tai du lieu tu Power BI...</span>
                        </div>
                    </div>
                </td>
            </tr>`;
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

    getStatusClass(status) {
        const map = { 'Completed': 'completed', 'Failed': 'failed', 'Cancelled': 'cancelled', 'Disabled': 'disabled', 'Unknown': 'unknown' };
        return map[status] || 'in-progress';
    }

    getStatusLabel(status) {
        const map = { 'Completed': 'Thanh cong', 'Failed': 'That bai', 'Cancelled': 'Da huy', 'Disabled': 'Tat', 'Unknown': 'Khong ro' };
        return map[status] || 'Dang chay';
    }

    formatDuration(seconds) {
        if (!seconds && seconds !== 0) return '\u2014';
        if (seconds < 60) return `${Math.round(seconds)}s`;
        if (seconds < 3600) {
            const min = Math.floor(seconds / 60);
            const sec = Math.round(seconds % 60);
            return sec > 0 ? `${min}m ${sec}s` : `${min}m`;
        }
        const hr = Math.floor(seconds / 3600);
        const min = Math.round((seconds % 3600) / 60);
        return min > 0 ? `${hr}h ${min}m` : `${hr}h`;
    }

    formatDateTime(isoString) {
        if (!isoString) return '\u2014';
        const date = new Date(isoString);
        return date.toLocaleString('vi-VN', {
            day: '2-digit', month: '2-digit', year: 'numeric',
            hour: '2-digit', minute: '2-digit', second: '2-digit'
        });
    }

    escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }
}

// ─── Initialize ──────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    window.historyPage = new HistoryPage();
});
