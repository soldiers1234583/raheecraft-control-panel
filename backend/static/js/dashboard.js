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
        this.setupPlayers();
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
        document.querySelectorAll('.mob-nav-players').forEach(el =>
            el.addEventListener('click', (e) => { e.preventDefault(); this.switchSection('players'); }));

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

        // Toggle real view containers (Stage 3)
        const overview = document.getElementById('overview-view');
        const players = document.getElementById('players-view');
        if (overview && players) {
            const toPlayers = section === 'players';
            overview.style.display = toPlayers ? 'none' : 'block';
            players.style.display = toPlayers ? 'block' : 'none';
            if (toPlayers) {
                this.refreshPlayers();
                this.refreshWhitelist();
                this.refreshBans();
                this.refreshOperators();
                this.refreshHistory();
            }
        }

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
        // Stage 3: refresh player data only while the Players section is open
        this.playersInterval = setInterval(() => {
            if (this.currentSection === 'players') {
                this.refreshPlayers();
                this.refreshWhitelist();
                this.refreshBans();
                this.refreshOperators();
            }
        }, 20000);

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


    // =====================================================================
    // Stage 3 — Players, moderation, whitelist, operators
    // =====================================================================

    setupPlayers() {
        const search = document.getElementById('players-search');
        let t = null;
        search?.addEventListener('input', () => {
            clearTimeout(t);
            t = setTimeout(() => this.renderPlayers(), 250);
        });

        document.getElementById('players-refresh')?.addEventListener('click', () => {
            this.refreshPlayers();
            this.showMessage('info', 'Player list refreshed');
        });
        document.getElementById('bans-refresh')?.addEventListener('click', () => {
            this.refreshBans();
        });

        // Whitelist toggle (confirmation for the destructive direction)
        document.getElementById('whitelist-toggle')?.addEventListener('click', () => {
            const btn = document.getElementById('whitelist-toggle');
            const enabling = btn?.dataset.action === 'enable';
            const label = enabling ? 'enable' : 'disable';
            if (!confirm(`Really ${label} the whitelist?`)) return;
            this.playerMutation(enabling ? 'whitelist/enable' : 'whitelist/disable', {},
                `${label === 'enable' ? 'Whitelist enabled' : 'Whitelist disabled'}`);
        });

        // Whitelist add form
        document.getElementById('whitelist-add-form')?.addEventListener('submit', (e) => {
            e.preventDefault();
            const input = document.getElementById('whitelist-add-input');
            const username = (input?.value || '').trim();
            if (!username) return;
            this.playerMutation('whitelist/add', { username },
                `${username} added to whitelist`, input);
        });

        // OP grant (step-up: modal asks for admin password)
        document.getElementById('op-grant-form')?.addEventListener('submit', (e) => {
            e.preventDefault();
            const input = document.getElementById('op-grant-input');
            const username = (input?.value || '').trim();
            if (!username) return;
            this.confirmModal({
                title: `Grant operator to ${username}?`,
                message: 'Operator privileges allow full server control. This requires your admin password.',
                showPassword: true,
                danger: true,
            }).then((res) => {
                if (!res) return;
                this.playerMutation('operators', {
                    username, confirm_password: res.password,
                }, `${username} is now an operator`, input);
            });
        });

        // Drawer close
        document.getElementById('drawer-close')?.addEventListener('click',
            () => this.closeDrawer());
        document.getElementById('player-drawer-backdrop')?.addEventListener('click',
            () => this.closeDrawer());
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') this.closeDrawer();
        });
    },

    /** Generic player mutation with pending state, toasts, and refresh. */
    async playerMutation(path, body, successMsg, clearInput) {
        try {
            const resp = await fetch(`${this.API_BASE}/admin/${path}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                this.showMessage('error', data.error || `Request failed (${resp.status})`);
                return null;
            }
            if (data.status === 'busy') {
                this.showMessage('info', data.message || 'Already in progress');
                return data;
            }
            if (data.status === 'queued') {
                if (clearInput) clearInput.value = '';
                this.showMessage('info', `${data.target}: queued for agent...`);
                this.watchPlayerAction(data.action_id, successMsg);
                return data;
            }
            return data;
        } catch (e) {
            this.showMessage('error', 'Backend unreachable');
            return null;
        }
    },

    /** Poll a player action to its terminal state; toast + refresh on done. */
    watchPlayerAction(actionId, successMsg) {
        let tries = 0;
        const timer = setInterval(async () => {
            tries++;
            if (tries > 45) {
                clearInterval(timer);
                this.showMessage('error', 'Action timed out waiting for agent');
                return;
            }
            try {
                const resp = await fetch(`${this.API_BASE}/admin/server/actions/${actionId}`);
                if (!resp.ok) return;
                const action = await resp.json();
                if (action.state === 'success') {
                    clearInterval(timer);
                    const note = action.result?.note;
                    this.showMessage('success', note ? `${successMsg} (${note})` : successMsg);
                    this.refreshPlayers();
                    this.refreshWhitelist();
                    this.refreshBans();
                    this.refreshOperators();
                    this.refreshHistory();
                } else if (['failed', 'timeout', 'cancelled'].includes(action.state)) {
                    clearInterval(timer);
                    this.showMessage('error', action.error || `Action ${action.state}`);
                    this.refreshPlayers();
                    this.refreshWhitelist();
                    this.refreshBans();
                    this.refreshOperators();
                    this.refreshHistory();
                }
            } catch (e) { /* keep trying */ }
        }, 2000);
    },

    async refreshPlayers() {
        const tbody = document.getElementById('players-tbody');
        const stateEl = document.getElementById('players-state');
        try {
            const resp = await fetch(`${this.API_BASE}/admin/players`);
            if (resp.status === 401) { window.location.href = '/login'; return; }
            const data = await resp.json();
            if (!data.available) {
                if (stateEl) {
                    stateEl.style.display = 'block';
                    stateEl.className = 'state-inline warn';
                    stateEl.textContent = data.reason === 'agent_offline'
                        ? 'Agent is offline — start the Kaggle notebook to see players.'
                        : 'Waiting for the first player snapshot from the agent...';
                }
                if (tbody) tbody.innerHTML =
                    '<tr><td colspan="5" class="empty-state">No data</td></tr>';
                const badge = document.getElementById('players-count');
                if (badge) badge.textContent = '-';
                return;
            }
            if (stateEl) stateEl.style.display = 'none';
            this._playerSnapshot = data;
            this.renderPlayers();
        } catch (e) {
            if (stateEl) {
                stateEl.style.display = 'block';
                stateEl.className = 'state-inline error';
                stateEl.textContent = 'Backend unreachable';
            }
        }
    },

    renderPlayers() {
        const data = this._playerSnapshot;
        const tbody = document.getElementById('players-tbody');
        const badge = document.getElementById('players-count');
        if (!tbody) return;
        if (!data || !data.available) return;

        const q = (document.getElementById('players-search')?.value || '')
            .trim().toLowerCase();
        const players = (data.online || []).filter(
            p => !q || (p.username || '').toLowerCase().includes(q));

        if (badge) badge.textContent = String(data.count ?? players.length);

        if (players.length === 0) {
            tbody.innerHTML = q
                ? '<tr><td colspan="5" class="empty-state">No players match the search</td></tr>'
                : '<tr><td colspan="5" class="empty-state">No players online</td></tr>';
            return;
        }

        tbody.innerHTML = '';
        players.forEach(p => {
            const tr = document.createElement('tr');
            const nameTd = document.createElement('td');
            nameTd.textContent = p.username || '(unknown)';

            const uuidTd = document.createElement('td');
            uuidTd.className = 'mono hide-mobile';
            uuidTd.textContent = p.uuid ? p.uuid.slice(0, 13) + '\u2026' : 'n/a';
            uuidTd.title = p.uuid || '';

            const sessTd = document.createElement('td');
            sessTd.textContent = p.session_seconds != null
                ? this.formatUptime(p.session_seconds) : 'unknown';

            const statTd = document.createElement('td');
            statTd.className = 'hide-mobile';
            statTd.innerHTML = '<span class="status-dot" style="background: var(--success)"></span> Online';

            const actTd = document.createElement('td');
            actTd.className = 'row-actions';

            const detailBtn = document.createElement('button');
            detailBtn.className = 'btn btn-secondary btn-sm';
            detailBtn.textContent = 'Details';
            detailBtn.addEventListener('click', () => this.openDrawer(p));

            const kickBtn = document.createElement('button');
            kickBtn.className = 'btn btn-secondary btn-sm';
            kickBtn.textContent = 'Kick';
            kickBtn.addEventListener('click', () => {
                this.confirmModal({
                    title: `Kick ${p.username}?`,
                    message: 'The player will be disconnected. They can rejoin unless banned.',
                    showReason: true, danger: true,
                }).then((res) => {
                    if (!res) return;
                    this.playerMutation('players/kick',
                        { username: p.username, reason: res.reason },
                        `${p.username} kicked`);
                });
            });

            const banBtn = document.createElement('button');
            banBtn.className = 'btn btn-danger btn-sm';
            banBtn.textContent = 'Ban';
            banBtn.addEventListener('click', () => {
                this.confirmModal({
                    title: `Ban ${p.username}?`,
                    message: 'The player will be disconnected and cannot rejoin until unbanned.',
                    showReason: true, danger: true,
                }).then((res) => {
                    if (!res) return;
                    this.playerMutation('players/ban',
                        { username: p.username, reason: res.reason },
                        `${p.username} banned`);
                });
            });

            actTd.append(detailBtn, kickBtn, banBtn);
            tr.append(nameTd, uuidTd, sessTd, statTd, actTd);
            tbody.appendChild(tr);
        });
    },

    async refreshWhitelist() {
        const statusEl = document.getElementById('whitelist-status');
        const toggleBtn = document.getElementById('whitelist-toggle');
        const list = document.getElementById('whitelist-entries');
        try {
            const resp = await fetch(`${this.API_BASE}/admin/whitelist`);
            const data = await resp.json();
            if (!data.available) {
                if (statusEl) statusEl.innerHTML =
                    '<span class="status-dot" style="background: var(--text-muted)"></span> unavailable';
                if (toggleBtn) toggleBtn.disabled = true;
                if (list) list.innerHTML =
                    '<li class="empty-state">Agent offline — no whitelist data</li>';
                return;
            }
            if (statusEl) {
                statusEl.innerHTML = data.enabled
                    ? '<span class="status-dot" style="background: var(--success)"></span> Enabled'
                    : '<span class="status-dot" style="background: var(--text-muted)"></span> Disabled';
            }
            if (toggleBtn) {
                toggleBtn.disabled = false;
                toggleBtn.textContent = data.enabled ? 'Disable' : 'Enable';
                toggleBtn.dataset.action = data.enabled ? 'disable' : 'enable';
                toggleBtn.className = data.enabled
                    ? 'btn btn-danger btn-sm' : 'btn btn-secondary btn-sm';
            }
            if (list) {
                list.innerHTML = '';
                const entries = data.entries || [];
                if (entries.length === 0) {
                    list.innerHTML = '<li class="empty-state">Whitelist is empty</li>';
                } else {
                    entries.forEach(e => {
                        const li = document.createElement('li');
                        li.className = 'entry-row';
                        const name = document.createElement('span');
                        name.textContent = e.username;
                        const rm = document.createElement('button');
                        rm.className = 'btn btn-secondary btn-sm';
                        rm.textContent = 'Remove';
                        rm.addEventListener('click', () => {
                            if (!confirm(`Remove ${e.username} from the whitelist?`)) return;
                            this.playerMutation('whitelist/remove',
                                { username: e.username },
                                `${e.username} removed from whitelist`);
                        });
                        li.append(name, rm);
                        list.appendChild(li);
                    });
                }
            }
        } catch (e) { /* status handled elsewhere */ }
    },

    async refreshBans() {
        const list = document.getElementById('bans-list');
        const badge = document.getElementById('bans-count');
        try {
            const resp = await fetch(`${this.API_BASE}/admin/players/bans`);
            const data = await resp.json();
            if (badge) badge.textContent = data.available ? String((data.bans || []).length) : '-';
            if (!list) return;
            if (!data.available) {
                list.innerHTML = '<li class="empty-state">Agent offline — no ban data</li>';
                return;
            }
            list.innerHTML = '';
            const bans = data.bans || [];
            if (bans.length === 0) {
                list.innerHTML = '<li class="empty-state">No banned players</li>';
                return;
            }
            bans.forEach(b => {
                const li = document.createElement('li');
                li.className = 'entry-row';
                const info = document.createElement('div');
                info.className = 'entry-info';
                const name = document.createElement('strong');
                name.textContent = b.username || (b.uuid ? b.uuid.slice(0, 13) + '\u2026' : '(unknown)');
                const reason = document.createElement('span');
                reason.className = 'entry-sub';
                reason.textContent = b.reason || 'No reason';
                info.append(name, reason);
                const unban = document.createElement('button');
                unban.className = 'btn btn-secondary btn-sm';
                unban.textContent = 'Unban';
                unban.addEventListener('click', () => {
                    const target = b.username || b.uuid;
                    if (!target || !b.username) {
                        this.showMessage('error',
                            'This ban has no cached username — unban by UUID is not supported here');
                        return;
                    }
                    if (!confirm(`Unban ${b.username}?`)) return;
                    this.playerMutation('players/unban', { username: b.username },
                        `${b.username} unbanned`);
                });
                li.append(info, unban);
                list.appendChild(li);
            });
        } catch (e) { /* handled */ }
    },

    async refreshOperators() {
        const list = document.getElementById('ops-list');
        const lpBadge = document.getElementById('luckperms-badge');
        const lpHint = document.getElementById('luckperms-hint');
        try {
            const resp = await fetch(`${this.API_BASE}/admin/operators`);
            const data = await resp.json();
            const lp = data.luckperms || {};
            if (lpBadge) {
                if (!data.available) {
                    lpBadge.textContent = 'LuckPerms: unknown (agent offline)';
                } else if (lp.operational) {
                    lpBadge.textContent = `LuckPerms: operational${lp.version ? ' v' + lp.version : ''}`;
                    lpBadge.className = 'lp-badge ok';
                } else if (lp.installed) {
                    lpBadge.textContent = 'LuckPerms: installed but not confirmed running';
                    lpBadge.className = 'lp-badge warn';
                } else {
                    lpBadge.textContent = 'LuckPerms: not installed';
                    lpBadge.className = 'lp-badge';
                }
            }
            if (lpHint) lpHint.style.display =
                (data.available && !lp.installed) ? 'block' : 'none';
            if (!list) return;
            if (!data.available) {
                list.innerHTML = '<li class="empty-state">Agent offline — no operator data</li>';
                return;
            }
            list.innerHTML = '';
            const ops = data.operators || [];
            if (ops.length === 0) {
                list.innerHTML = '<li class="empty-state">No operators</li>';
                return;
            }
            ops.forEach(o => {
                const li = document.createElement('li');
                li.className = 'entry-row';
                const info = document.createElement('div');
                info.className = 'entry-info';
                const name = document.createElement('strong');
                name.textContent = o.username || (o.uuid ? o.uuid.slice(0, 13) + '\u2026' : '(unknown)');
                const sub = document.createElement('span');
                sub.className = 'entry-sub';
                sub.textContent = `level ${o.level ?? 'n/a'}`;
                info.append(name, sub);
                const rm = document.createElement('button');
                rm.className = 'btn btn-danger btn-sm';
                rm.textContent = 'Revoke OP';
                if (!o.username) rm.disabled = true;
                rm.addEventListener('click', () => {
                    this.confirmModal({
                        title: `Revoke operator from ${o.username}?`,
                        message: 'They will lose all operator privileges. Requires your admin password.',
                        showPassword: true, danger: true,
                    }).then((res) => {
                        if (!res) return;
                        this.playerMutationRaw('DELETE',
                            `operators/${o.username}`,
                            { confirm_password: res.password },
                            `${o.username} is no longer an operator`);
                    });
                });
                li.append(info, rm);
                list.appendChild(li);
            });
        } catch (e) { /* handled */ }
    },

    /** DELETE helper for op revoke. */
    async playerMutationRaw(method, path, body, successMsg) {
        try {
            const resp = await fetch(`${this.API_BASE}/admin/${path}`, {
                method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                this.showMessage('error', data.error || 'Request failed');
                return null;
            }
            this.showMessage('info', `${data.target}: queued for agent...`);
            this.watchPlayerAction(data.action_id, successMsg);
            return data;
        } catch (e) {
            this.showMessage('error', 'Backend unreachable');
            return null;
        }
    },

    async refreshHistory() {
        const list = document.getElementById('history-list');
        if (!list) return;
        try {
            const resp = await fetch(`${this.API_BASE}/admin/players/history`);
            const data = await resp.json();
            list.innerHTML = '';
            const entries = (data.entries || []).slice(-30).reverse();
            if (entries.length === 0) {
                list.innerHTML = '<li class="empty-state">No moderation actions recorded</li>';
                return;
            }
            entries.forEach(e => {
                const li = document.createElement('li');
                li.className = 'entry-row';
                const info = document.createElement('div');
                info.className = 'entry-info';
                const main = document.createElement('strong');
                const d = e.details || {};
                main.textContent = `${e.action.replace(/^player_/, '').replace(/^outcome_/, '')} \u2192 ${d.target || '?'}`;
                const sub = document.createElement('span');
                sub.className = 'entry-sub';
                const when = new Date(e.timestamp * 1000).toLocaleString();
                sub.textContent = `${when} \u00b7 by ${e.user_id} \u00b7 ` +
                    (e.success ? 'success' : (d.state || 'requested')) +
                    (d.reason ? ` \u00b7 "${d.reason}"` : '') +
                    (d.error ? ` \u00b7 ${d.error}` : '');
                info.append(main, sub);
                li.appendChild(info);
                list.appendChild(li);
            });
        } catch (e) { /* handled */ }
    },

    // --- Player detail drawer ---------------------------------------------

    openDrawer(player) {
        const drawer = document.getElementById('player-drawer');
        const backdrop = document.getElementById('player-drawer-backdrop');
        const nameEl = document.getElementById('drawer-name');
        const body = document.getElementById('drawer-body');
        if (!drawer || !body) return;

        nameEl.textContent = player.username || 'Player';
        body.innerHTML = '';

        const rows = [
            ['Username', player.username || 'n/a'],
            ['UUID', player.uuid || 'unknown (not in usercache yet)'],
            ['Online', 'Yes'],
            ['Session started', player.session_started_at
                ? new Date(player.session_started_at * 1000).toLocaleString()
                : 'unknown (agent started after join)'],
            ['Session length', player.session_seconds != null
                ? this.formatUptime(player.session_seconds) : 'unknown'],
        ];
        // operator/whitelist state from the cached snapshot if present
        const snap = this._operatorSnapshot;
        if (snap) {
            rows.push(['Operator', (snap.operators || []).some(
                o => o.username === player.username) ? 'Yes' : 'No']);
            rows.push(['Whitelisted', (snap.whitelistEntries || []).some(
                e => e.username === player.username) ? 'Yes' : 'No']);
        }

        rows.forEach(([label, value]) => {
            const row = document.createElement('div');
            row.className = 'drawer-row';
            const l = document.createElement('span');
            l.className = 'drawer-label';
            l.textContent = label;
            const v = document.createElement('span');
            v.className = 'drawer-value';
            v.textContent = value;
            row.append(l, v);
            body.appendChild(row);
        });

        // Note about stats/first-seen: honest unavailability
        const note = document.createElement('p');
        note.className = 'hint-text';
        note.textContent = 'Statistics and first-seen times are not exposed by ' +
            'Paper without additional plugins, so they are not shown.';
        body.appendChild(note);

        drawer.style.display = 'block';
        backdrop.style.display = 'block';
    },

    closeDrawer() {
        const drawer = document.getElementById('player-drawer');
        const backdrop = document.getElementById('player-drawer-backdrop');
        if (drawer) drawer.style.display = 'none';
        if (backdrop) backdrop.style.display = 'none';
    },

    // --- Confirmation modal ------------------------------------------------

    /**
     * Promise-based confirm modal.
     * opts: {title, message, showReason, showPassword, danger}
     * Resolves {reason, password} or null on cancel.
     */
    confirmModal(opts) {
        return new Promise((resolve) => {
            const modal = document.getElementById('confirm-modal');
            const backdrop = document.getElementById('modal-backdrop');
            if (!modal) { resolve(null); return; }

            document.getElementById('modal-title').textContent = opts.title || 'Confirm';
            document.getElementById('modal-message').textContent = opts.message || '';
            const reasonLabel = document.getElementById('modal-reason-label');
            const pwLabel = document.getElementById('modal-password-label');
            const reasonInput = document.getElementById('modal-reason');
            const pwInput = document.getElementById('modal-password');
            reasonLabel.style.display = opts.showReason ? 'block' : 'none';
            pwLabel.style.display = opts.showPassword ? 'block' : 'none';
            reasonInput.value = '';
            pwInput.value = '';
            const confirmBtn = document.getElementById('modal-confirm');
            const cancelBtn = document.getElementById('modal-cancel');
            confirmBtn.className = opts.danger ? 'btn btn-danger' : 'btn btn-primary';

            modal.style.display = 'block';
            backdrop.style.display = 'block';
            (opts.showPassword ? pwInput : (opts.showReason ? reasonInput : confirmBtn))?.focus();

            const cleanup = (result) => {
                modal.style.display = 'none';
                backdrop.style.display = 'none';
                confirmBtn.removeEventListener('click', onConfirm);
                cancelBtn.removeEventListener('click', onCancel);
                backdrop.removeEventListener('click', onCancel);
                resolve(result);
            };
            const onConfirm = () => cleanup({
                reason: reasonInput.value.trim() || undefined,
                password: pwInput.value || undefined,
            });
            const onCancel = () => cleanup(null);

            confirmBtn.addEventListener('click', onConfirm);
            cancelBtn.addEventListener('click', onCancel);
            backdrop.addEventListener('click', onCancel);
        });
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
