/**
 * RaheeCraft Dashboard (Stage 2)
 *
 * Power actions queue on the backend and execute on the Kaggle agent.
 * The UI polls the resulting action until it reaches a terminal state —
 * "queued" is never rendered as success.
 */
const Dashboard = {
    API_BASE: '/api',
    refreshInterval: null,
    monitorInterval: null,
    consoleInterval: null,
    actionPollTimer: null,
    currentSection: 'overview',

    // Console state (sequence-based tailing: reconnects never replay history)
    consoleSeq: 0,
    consolePaused: false,
    consoleAutoScroll: true,
    consoleClearedAtSeq: null,
    consoleBusy: false,

    // Current in-flight power action (if any)
    activeAction: null,

    /**
     * Initialize dashboard
     */
    init() {
        this.checkLogin();
        this.setupNavigation();
        this.setupActions();
        this.setupConsole();
        this.startPolling();
    },

    /**
     * Check if user is logged in
     */
    checkLogin() {
        const cookie = document.cookie;
        if (!cookie.includes('raheecraft_session=')) {
            window.location.href = '/login';
            return;
        }
        document.getElementById('username').textContent = 'Admin';
    },

    /**
     * Setup navigation + section switching
     */
    setupNavigation() {
        document.querySelectorAll('.nav-item[data-section]').forEach(item => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                this.switchSection(item.dataset.section);
            });
        });

        // Mobile bottom nav
        document.querySelectorAll('.mob-nav-overview').forEach(el =>
            el.addEventListener('click', (e) => { e.preventDefault(); this.switchSection('overview'); }));
        document.querySelectorAll('.mob-nav-console').forEach(el =>
            el.addEventListener('click', (e) => { e.preventDefault(); this.switchSection('console'); }));

        document.getElementById('menuToggle')?.addEventListener('click', () => {
            document.getElementById('sidebar').classList.toggle('closed');
        });

        document.getElementById('logoutBtn')?.addEventListener('click', () => {
            Auth.logout();
        });
    },

    /**
     * Switch active section (overview / console)
     */
    switchSection(section) {
        this.currentSection = section;

        document.querySelectorAll('.nav-item[data-section]').forEach(item => {
            item.classList.toggle('active', item.dataset.section === section);
        });

        const consoleCard = document.getElementById('section-console');
        if (consoleCard) {
            // Console card lives in the overview flow; scrolling to it counts
            // as visiting the section on both layouts.
            if (section === 'console') {
                consoleCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
                document.getElementById('console-input')?.focus();
            } else if (section === 'overview') {
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }
        }
    },

    /**
     * Setup power + misc action buttons
     */
    setupActions() {
        document.getElementById('btn-start')?.addEventListener('click', () => {
            this.powerAction('start');
        });

        document.getElementById('btn-stop')?.addEventListener('click', () => {
            if (confirm('Stop the server? The world will be saved first (Minecraft "stop").')) {
                this.powerAction('stop');
            }
        });

        document.getElementById('btn-restart')?.addEventListener('click', () => {
            if (confirm('Restart the server? It will stop gracefully (world saved), then start again.')) {
                this.powerAction('restart');
            }
        });

        document.getElementById('btn-refresh')?.addEventListener('click', () => {
            this.fetchStatus();
            this.fetchMonitoring();
            this.showMessage('info', 'Status refreshed');
        });

        document.getElementById('btn-backup-now')?.addEventListener('click', () => {
            this.showMessage('info', 'Backup is triggered from the Kaggle notebook (Stage 1)');
        });

        // Copy buttons
        document.querySelectorAll('.copy-btn').forEach(btn => {
            btn.addEventListener('click', async () => {
                const targetId = btn.dataset.copy;
                const content = document.getElementById(targetId)?.textContent;
                if (content && content !== '-') {
                    try {
                        await navigator.clipboard.writeText(content);
                        this.showMessage('success', 'Address copied to clipboard');
                    } catch (error) {
                        this.showMessage('error', 'Failed to copy');
                    }
                }
            });
        });
    },

    // =====================================================================
    // Power controls
    // =====================================================================

    /**
     * Queue a power action and poll its real state until terminal.
     * @param {'start'|'stop'|'restart'} action
     */
    async powerAction(action) {
        if (this.activeAction) {
            this.showMessage('info', 'An action is already in progress');
            return;
        }

        const btn = document.getElementById(`btn-${action}`);
        if (btn) btn.disabled = true;
        this.setPowerState('pending', 'Sending request...');

        try {
            const resp = await fetch(`${this.API_BASE}/admin/server/${action}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}'
            });
            const data = await resp.json().catch(() => ({}));

            if (resp.status === 503) {
                this.setPowerState('error', data.error || 'Agent offline');
                this.showMessage('error', data.error || 'Agent is offline');
                this.fetchStatus();
                return;
            }
            if (!resp.ok) {
                const msg = data.error || data.message || `Request failed (${resp.status})`;
                this.setPowerState('error', msg);
                this.showMessage('error', msg);
                this.fetchStatus();
                return;
            }
            if (data.status === 'busy') {
                // Duplicate/conflicting request → attach to existing action
                this.showMessage('info', data.message || 'Action already in progress');
                this.watchAction(data.action_id, action);
                return;
            }
            if (data.state === 'pending' && data.action_id) {
                // Queued — NOT success. Watch until the agent finishes.
                this.showMessage('info', `${action} queued — waiting for agent...`);
                this.watchAction(data.action_id, action);
                return;
            }
            this.setPowerState('error', 'Unexpected response');
        } catch (err) {
            this.setPowerState('error', 'Backend unreachable');
            this.showMessage('error', 'Backend unreachable');
        } finally {
            if (!this.activeAction) this.fetchStatus();
        }
    },

    /**
     * Poll an action by ID until it reaches a terminal state.
     */
    watchAction(actionId, label) {
        this.activeAction = actionId;
        const pendingEl = document.getElementById('pending-action');
        const textEl = document.getElementById('pending-action-text');
        const idEl = document.getElementById('pending-action-id');
        if (pendingEl) pendingEl.style.display = 'flex';
        if (textEl) textEl.textContent = `${label} in progress...`;
        if (idEl) idEl.textContent = actionId.slice(0, 8);
        this.setPowerState(label === 'start' ? 'starting'
            : label === 'stop' ? 'stopping' : 'restarting',
            'Waiting for the agent to execute...');

        clearInterval(this.actionPollTimer);
        const started = Date.now();
        const TIMEOUT_MS = 180000; // action timeout + margin

        this.actionPollTimer = setInterval(async () => {
            if (Date.now() - started > TIMEOUT_MS) {
                clearInterval(this.actionPollTimer);
                this.activeAction = null;
                if (pendingEl) pendingEl.style.display = 'none';
                this.setPowerState('error', 'Timed out waiting for agent');
                this.showMessage('error', `${label} timed out`);
                this.fetchStatus();
                return;
            }

            try {
                const resp = await fetch(`${this.API_BASE}/admin/server/actions/${actionId}`);
                if (!resp.ok) return;
                const action = await resp.json();

                if (action.state === 'success') {
                    clearInterval(this.actionPollTimer);
                    this.activeAction = null;
                    if (pendingEl) pendingEl.style.display = 'none';
                    this.setPowerState(label === 'stop' ? 'offline' : 'online',
                        this.describeResult(action));
                    this.showMessage('success',
                        label === 'start' ? 'Server started'
                        : label === 'stop' ? 'Server stopped (world saved)'
                        : 'Server restarted');
                    this.fetchStatus();
                    this.fetchMonitoring();
                } else if (action.state === 'failed' || action.state === 'timeout'
                        || action.state === 'cancelled') {
                    clearInterval(this.actionPollTimer);
                    this.activeAction = null;
                    if (pendingEl) pendingEl.style.display = 'none';
                    const msg = action.error || `${label} ${action.state}`;
                    this.setPowerState('error', msg);
                    this.showMessage('error', msg);
                    this.fetchStatus();
                } else {
                    // pending/running — keep waiting
                    if (textEl) {
                        textEl.textContent = action.state === 'running'
                            ? `Agent executing ${label}...`
                            : `Queued — waiting for agent...`;
                    }
                }
            } catch (err) {
                // Transient poll failure — keep trying until timeout
            }
        }, 2000);
    },

    describeResult(action) {
        const r = action.result || {};
        if (r.already_running) return 'Already running';
        if (r.already_stopped) return 'Already stopped';
        if (r.method) return `${r.method} (saved: ${r.saved ? 'yes' : 'no'})`;
        if (r.started) return 'Started';
        return '';
    },

    /**
     * Render the power state machine: offline | starting | online |
     * stopping | error | pending.
     */
    setPowerState(state, note) {
        const el = document.getElementById('power-state');
        const noteEl = document.getElementById('power-state-note');
        if (el) {
            el.dataset.state = state;
            el.textContent = {
                offline: 'Offline', starting: 'Starting', online: 'Online',
                stopping: 'Stopping', restarting: 'Restarting',
                error: 'Error', pending: 'Pending', unknown: 'Unknown'
            }[state] || state;
        }
        if (noteEl) noteEl.textContent = note || '';
    },

    // =====================================================================
    // Status + monitoring
    // =====================================================================

    startPolling() {
        clearInterval(this.refreshInterval);
        clearInterval(this.monitorInterval);
        clearInterval(this.consoleInterval);

        this.refreshInterval = setInterval(() => this.fetchStatus(), 15000);
        this.monitorInterval = setInterval(() => this.fetchMonitoring(), 10000);
        this.consoleInterval = setInterval(() => this.fetchConsole(), 3000);

        this.fetchStatus();
        this.fetchMonitoring();
        this.fetchConsole();
    },

    async fetchStatus() {
        try {
            const response = await fetch(`${this.API_BASE}/status`);
            if (!response.ok) {
                if (response.status === 401) { window.location.href = '/login'; return; }
                this.showOffline();
                return;
            }
            const data = await response.json();
            this.renderStatus(data);
        } catch (error) {
            console.error('Status fetch error:', error);
            this.showOffline();
        }
    },

    renderStatus(data) {
        const server = data.server || {};
        const statusBadge = document.getElementById('server-status-badge');
        const serverStatusEl = document.getElementById('server-status');
        const serverVersionEl = document.getElementById('server-version');
        const serverPlayersEl = document.getElementById('server-players');
        const serverWorldEl = document.getElementById('server-world');

        const agent = data.agent || {};
        const agentOnline = !!(agent.online && agent.agents && agent.agents.length);

        if (this.activeAction) {
            // A power action is in flight — keep the derived state
        } else if (agentOnline && server.paper_running) {
            this.setPowerState('online', 'Paper process running');
        } else if (agentOnline) {
            this.setPowerState('offline', 'Agent connected, server not running');
        } else {
            this.setPowerState('error', 'Agent offline — start the Kaggle notebook');
        }

        if (server.exists) {
            statusBadge.className = 'status-badge ' + (server.paper_running ? 'online' : 'offline');
            statusBadge.querySelector('.status-text').textContent =
                server.paper_running ? 'Online' : 'Offline';
            serverStatusEl.textContent = server.paper_running ? 'Running' : 'Stopped';
            serverStatusEl.style.color = server.paper_running ? 'var(--success)' : 'var(--danger)';

            const busy = !!this.activeAction;
            document.getElementById('btn-start').disabled = busy || server.paper_running || !agentOnline;
            document.getElementById('btn-stop').disabled = busy || !server.paper_running || !agentOnline;
            document.getElementById('btn-restart').disabled = busy || !server.paper_running || !agentOnline;

            serverVersionEl.textContent = server.version || '-';
            serverPlayersEl.textContent = '-';
            serverWorldEl.textContent = server.world_exists ? 'Exists' : 'No world';
            serverWorldEl.style.color = server.world_exists ? 'var(--success)' : 'var(--text-muted)';
        } else {
            statusBadge.className = 'status-badge offline';
            statusBadge.querySelector('.status-text').textContent = 'Not Set Up';
            serverStatusEl.textContent = 'No server';
            ['btn-start', 'btn-stop', 'btn-restart'].forEach(id => {
                document.getElementById(id).disabled = true;
            });
        }

        // Agent card
        const agentStatusEl = document.getElementById('agent-status');
        const agentHeartbeatEl = document.getElementById('agent-heartbeat');
        const agentIdEl = document.getElementById('agent-id');
        const agentUptimeEl = document.getElementById('agent-uptime');

        if (agentOnline) {
            const a = agent.agents[0];
            agentStatusEl.innerHTML = '<span class="status-dot" style="background: var(--success)"></span> Connected';
            agentHeartbeatEl.textContent = this.formatTime(a.last_heartbeat);
            agentIdEl.textContent = a.agent_id || '-';
            agentUptimeEl.textContent = this.formatUptime(a.uptime_seconds);
        } else {
            agentStatusEl.innerHTML = '<span class="status-dot" style="background: var(--danger)"></span> Disconnected';
            agentHeartbeatEl.textContent = '-';
            agentIdEl.textContent = '-';
            agentUptimeEl.textContent = '-';
        }

        // Playit
        const playit = data.playit || {};
        const playitStatusEl = document.getElementById('playit-status');
        const playitAddressEl = document.getElementById('playit-java-address');
        if (playit.running && playit.hostname) {
            playitStatusEl.innerHTML = '<span class="status-dot" style="background: var(--success)"></span> Active';
            playitAddressEl.textContent = `${playit.hostname}:${playit.port}`;
        } else {
            playitStatusEl.innerHTML = '<span class="status-dot" style="background: var(--text-muted)"></span> Not connected';
            playitAddressEl.textContent = '-';
        }

        // Backup
        const backup = data.backup || {};
        const backupStatusEl = document.getElementById('backup-status');
        const backupLastEl = document.getElementById('backup-last');
        const backupSizeEl = document.getElementById('backup-size');
        const backupEnabledEl = document.getElementById('backup-enabled');
        if (backup.last_backup) {
            backupStatusEl.innerHTML = '<span class="status-dot" style="background: var(--success)"></span> OK';
            backupLastEl.textContent = backup.last_backup.name || backup.last_backup;
            backupSizeEl.textContent = backup.last_backup.size_mb
                ? `${backup.last_backup.size_mb.toFixed(1)} MB` : '-';
        } else {
            backupStatusEl.innerHTML = '<span class="status-dot" style="background: var(--text-muted)"></span> No backups';
            backupLastEl.textContent = 'Never';
            backupSizeEl.textContent = '-';
        }
        backupEnabledEl.textContent = backup.enabled ? 'Yes' : 'No';
        backupEnabledEl.style.color = backup.enabled ? 'var(--success)' : 'var(--text-muted)';

        document.getElementById('loading-state').style.display = 'none';
        document.getElementById('dashboard-content').style.display = 'block';
    },

    /**
     * Fetch real measurements for the monitoring row.
     * Null values render as "n/a" — never invented.
     */
    async fetchMonitoring() {
        try {
            const resp = await fetch(`${this.API_BASE}/admin/monitoring`);
            if (!resp.ok) return;
            const m = await resp.json();
            this.renderMonitoring(m);
        } catch (e) { /* offline — status poll handles the banner */ }
    },

    renderMonitoring(m) {
        const set = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = (value === null || value === undefined || value === '')
                ? 'n/a' : value;
        };

        set('mon-uptime', m.uptime_seconds ? this.formatUptime(m.uptime_seconds)
            : (m.process && m.process.state === 'offline' ? '0s' : null));

        const players = m.players || {};
        set('mon-players', players.online !== null && players.online !== undefined
            ? `${players.online}${players.max ? ' / ' + players.max : ''}`
            : null);

        const cpu = m.cpu || {};
        set('mon-cpu', cpu.percent !== null && cpu.percent !== undefined
            ? `${cpu.percent.toFixed(0)}%` : null);

        const mem = m.memory || {};
        set('mon-memory', mem.memory_percent !== null && mem.memory_percent !== undefined
            ? `${mem.memory_percent.toFixed(0)}%` : null);

        const ag = m.agent || {};
        set('mon-heartbeat', ag.heartbeat_age_seconds !== null
            && ag.heartbeat_age_seconds !== undefined
            ? `${ag.heartbeat_age_seconds}s ago` : null);

        const logAct = m.log_activity || {};
        const lastLog = document.getElementById('mon-lastlog');
        if (lastLog) {
            lastLog.textContent = logAct.last_line || (logAct.last_agent_push ? 'none yet' : 'n/a');
            lastLog.title = logAct.last_line || '';
        }
    },

    // =====================================================================
    // Live console
    // =====================================================================

    setupConsole() {
        const pauseBtn = document.getElementById('console-pause');
        pauseBtn?.addEventListener('click', () => {
            this.consolePaused = !this.consolePaused;
            pauseBtn.textContent = this.consolePaused ? 'Resume' : 'Pause';
            const conn = document.getElementById('console-conn');
            if (conn && this.consolePaused) {
                conn.innerHTML = '<span class="status-dot" style="background: var(--text-muted)"></span> paused';
            }
        });

        document.getElementById('console-clear')?.addEventListener('click', () => {
            const box = document.getElementById('console-lines');
            if (box) box.innerHTML = '<p class="empty-state">View cleared — streaming continues.</p>';
            // Only clear the local view; future lines append from current seq
            this.consoleClearedAtSeq = this.consoleSeq;
        });

        document.getElementById('console-autoscroll')?.addEventListener('change', (e) => {
            this.consoleAutoScroll = e.target.checked;
        });

        let searchTimer = null;
        document.getElementById('console-search')?.addEventListener('input', () => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => this.refetchConsole(), 400);
        });
        document.getElementById('console-level')?.addEventListener('change', () => {
            this.refetchConsole();
        });

        document.getElementById('console-form')?.addEventListener('submit', (e) => {
            e.preventDefault();
            this.sendConsoleCommand();
        });
    },

    consoleQuery() {
        const level = document.getElementById('console-level')?.value || '';
        const search = document.getElementById('console-search')?.value || '';
        const params = new URLSearchParams();
        params.set('since', String(this.consoleSeq));
        params.set('limit', '300');
        if (level) params.set('level', level);
        if (search) params.set('search', search);
        return params.toString();
    },

    /** Re-fetch from seq 0 with current filters (filter changes). */
    async refetchConsole() {
        const level = document.getElementById('console-level')?.value || '';
        const search = document.getElementById('console-search')?.value || '';
        const params = new URLSearchParams();
        params.set('since', '0');
        params.set('limit', '300');
        if (level) params.set('level', level);
        if (search) params.set('search', search);
        const prevSeq = this.consoleSeq;
        this.consoleSeq = 0;
        const box = document.getElementById('console-lines');
        if (box) box.innerHTML = '';
        const ok = await this.fetchConsoleNow(params.toString());
        if (!ok) this.consoleSeq = prevSeq;
    },

    async fetchConsole() {
        if (this.consolePaused) return;
        await this.fetchConsoleNow(this.consoleQuery());
    },

    async fetchConsoleNow(query) {
        if (this.consoleBusy) return false;
        this.consoleBusy = true;
        try {
            const resp = await fetch(`${this.API_BASE}/admin/console/tail?${query}`);
            if (!resp.ok) return false;
            const data = await resp.json();
            const lines = data.lines || [];

            if (data.seq !== undefined) this.consoleSeq = data.seq;

            if (lines.length === 0) return true;

            const box = document.getElementById('console-lines');
            if (!box) return true;

            const empty = box.querySelector('.empty-state');
            if (empty) empty.remove();

            const frag = document.createDocumentFragment();
            lines.forEach(l => frag.appendChild(this.renderLogLine(l)));
            box.appendChild(frag);

            // Cap DOM nodes so long sessions don't slow the page
            while (box.children.length > 800) box.removeChild(box.firstChild);

            if (this.consoleAutoScroll) box.scrollTop = box.scrollHeight;

            const conn = document.getElementById('console-conn');
            if (conn && !this.consolePaused) {
                conn.innerHTML = '<span class="status-dot" style="background: var(--success)"></span> live';
            }
            return true;
        } catch (e) {
            const conn = document.getElementById('console-conn');
            if (conn) conn.innerHTML = '<span class="status-dot" style="background: var(--danger)"></span> disconnected';
            return false;
        } finally {
            this.consoleBusy = false;
        }
    },

    renderLogLine(l) {
        const div = document.createElement('div');
        div.className = `console-line level-${l.level || 'info'}`;
        const time = new Date((l.ts || Date.now() / 1000) * 1000);
        const timeEl = document.createElement('span');
        timeEl.className = 'console-time';
        timeEl.textContent = time.toLocaleTimeString();
        const textEl = document.createElement('span');
        textEl.className = 'console-text';
        // textContent — no HTML injection from server logs
        textEl.textContent = l.text || '';
        div.appendChild(timeEl);
        div.appendChild(textEl);
        return div;
    },

    async sendConsoleCommand() {
        const input = document.getElementById('console-input');
        const sendBtn = document.getElementById('console-send');
        if (!input) return;
        const command = input.value.trim();
        if (!command) return;

        sendBtn.disabled = true;
        try {
            const resp = await fetch(`${this.API_BASE}/admin/server/console`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ command })
            });
            const data = await resp.json().catch(() => ({}));

            if (resp.status === 503) {
                this.showMessage('error', data.error || 'Agent offline');
            } else if (!resp.ok) {
                this.showMessage('error', data.error || 'Command rejected');
            } else if (data.state === 'pending') {
                this.showMessage('success', 'Command queued for the server');
                input.value = '';
                // Show acknowledgement when the agent reports the result
                this.awaitCommandAck(data.action_id);
            } else {
                this.showMessage('info', data.message || 'Sent');
                input.value = '';
            }
        } catch (e) {
            this.showMessage('error', 'Backend unreachable');
        } finally {
            sendBtn.disabled = false;
            input.focus();
        }
    },

    awaitCommandAck(actionId) {
        let tries = 0;
        const timer = setInterval(async () => {
            tries++;
            if (tries > 30) { clearInterval(timer); return; }
            try {
                const resp = await fetch(`${this.API_BASE}/admin/server/actions/${actionId}`);
                if (!resp.ok) return;
                const action = await resp.json();
                if (action.state === 'success') {
                    clearInterval(timer);
                    this.showMessage('success', 'Command executed');
                } else if (action.state === 'failed' || action.state === 'timeout') {
                    clearInterval(timer);
                    this.showMessage('error', action.error || 'Command failed');
                }
            } catch (e) { /* keep trying */ }
        }, 2000);
    },

    // =====================================================================
    // States + helpers
    // =====================================================================

    showOffline() {
        document.getElementById('loading-state').style.display = 'none';
        document.getElementById('dashboard-content').style.display = 'none';
        document.getElementById('offline-state').style.display = 'flex';
    },

    showError(message) {
        document.getElementById('loading-state').style.display = 'none';
        document.getElementById('dashboard-content').style.display = 'none';
        document.getElementById('error-state').style.display = 'flex';
        document.getElementById('error-message').textContent = message;
    },

    showMessage(type, text) {
        let msgEl = document.getElementById('toast-message');
        if (!msgEl) {
            msgEl = document.createElement('div');
            msgEl.id = 'toast-message';
            msgEl.className = 'toast';
            document.body.appendChild(msgEl);
        }
        msgEl.textContent = text;
        msgEl.className = `toast toast-${type}`;
        msgEl.style.display = 'block';
        clearTimeout(msgEl._hideTimer);
        msgEl._hideTimer = setTimeout(() => {
            msgEl.style.display = 'none';
        }, 4000);
    },

    formatTime(timestamp) {
        if (!timestamp) return '-';
        const date = new Date(timestamp * 1000);
        return date.toLocaleTimeString();
    },

    formatUptime(seconds) {
        if (!seconds && seconds !== 0) return '-';
        if (seconds === 0) return '0s';
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = Math.floor(seconds % 60);
        if (h > 0) return `${h}h ${m}m`;
        if (m > 0) return `${m}m ${s}s`;
        return `${s}s`;
    }
};

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    Dashboard.init();
});
