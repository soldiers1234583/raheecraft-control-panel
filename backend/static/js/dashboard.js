/**
 * RaheeCraft Dashboard
 */
const Dashboard = {
    API_BASE: '/api',
    refreshInterval: null,
    currentSection: 'overview',
    
    /**
     * Initialize dashboard
     */
    init() {
        this.checkLogin();
        this.setupNavigation();
        this.setupActions();
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
     * Setup navigation
     */
    setupNavigation() {
        // Sidebar navigation
        document.querySelectorAll('.nav-item[data-section]').forEach(item => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                const section = item.dataset.section;
                this.switchSection(section);
            });
        });
        
        // Mobile menu toggle
        document.getElementById('menuToggle')?.addEventListener('click', () => {
            document.getElementById('sidebar').classList.toggle('closed');
        });
        
        // Logout
        document.getElementById('logoutBtn')?.addEventListener('click', () => {
            Auth.logout();
        });
    },
    
    /**
     * Switch active section
     */
    switchSection(section) {
        this.currentSection = section;
        
        // Update nav items
        document.querySelectorAll('.nav-item[data-section]').forEach(item => {
            item.classList.toggle('active', item.dataset.section === section);
        });
        
        // Show/hide content sections (future)
        console.log(`Switched to section: ${section}`);
    },
    
    /**
     * Setup action buttons
     */
    setupActions() {
        // Start server
        document.getElementById('btn-start')?.addEventListener('click', () => {
            this.showMessage('info', 'Start command sent (not yet implemented)');
        });
        
        // Stop server
        document.getElementById('btn-stop')?.addEventListener('click', () => {
            if (confirm('Are you sure you want to stop the server?')) {
                this.showMessage('info', 'Stop command sent (not yet implemented)');
            }
        });
        
        // Restart server
        document.getElementById('btn-restart')?.addEventListener('click', () => {
            if (confirm('Are sure you want to restart the server?')) {
                this.showMessage('info', 'Restart command sent (not yet implemented)');
            }
        });
        
        // Create backup
        document.getElementById('btn-backup-now')?.addEventListener('click', () => {
            this.showMessage('info', 'Backup initiated (not yet implemented)');
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
    
    /**
     * Start polling for updates
     */
    startPolling() {
        if (this.refreshInterval) {
            clearInterval(this.refreshInterval);
        }
        
        this.refreshInterval = setInterval(() => {
            this.fetchStatus();
        }, 30000); // Poll every 30 seconds
        
        // Initial fetch
        this.fetchStatus();
    },
    
    /**
     * Fetch status from API
     */
    async fetchStatus() {
        try {
            const response = await fetch(`${this.API_BASE}/status`, {
                headers: {
                    'Cookie': document.cookie
                }
            });
            
            if (!response.ok) {
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
    
    /**
     * Render status data
     */
    renderStatus(data) {
        // Server status
        const server = data.server || {};
        const statusBadge = document.getElementById('server-status-badge');
        const serverStatusEl = document.getElementById('server-status');
        const serverVersionEl = document.getElementById('server-version');
        const serverPlayersEl = document.getElementById('server-players');
        const serverWorldEl = document.getElementById('server-world');
        
        if (server.exists) {
            // Server files exist
            if (server.paper_running) {
                // Server is running
                statusBadge.className = 'status-badge online';
                statusBadge.querySelector('.status-text').textContent = 'Online';
                serverStatusEl.textContent = 'Running';
                serverStatusEl.style.color = 'var(--success)';
                document.getElementById('btn-start').disabled = true;
                document.getElementById('btn-stop').disabled = false;
                document.getElementById('btn-restart').disabled = false;
            } else {
                // Server not running
                statusBadge.className = 'status-badge offline';
                statusBadge.querySelector('.status-text').textContent = 'Offline';
                serverStatusEl.textContent = 'Stopped';
                serverStatusEl.style.color = 'var(--danger)';
                document.getElementById('btn-start').disabled = false;
                document.getElementById('btn-stop').disabled = true;
                document.getElementById('btn-restart').disabled = true;
            }
            
            serverVersionEl.textContent = server.version || '-';
            
            // Players (not available in Stage 1)
            serverPlayersEl.textContent = '-';
            
            // World
            serverWorldEl.textContent = server.world_exists ? 'Exists' : 'No world';
            serverWorldEl.style.color = server.world_exists ? 'var(--success)' : 'var(--text-muted)';
        } else {
            // Server directory doesn't exist
            statusBadge.className = 'status-badge offline';
            statusBadge.querySelector('.status-text').textContent = 'Not Set Up';
            serverStatusEl.textContent = 'No server';
            serverVersionEl.textContent = '-';
            serverPlayersEl.textContent = '-';
            serverWorldEl.textContent = '-';
            document.getElementById('btn-start').disabled = true;
            document.getElementById('btn-stop').disabled = true;
            document.getElementById('btn-restart').disabled = true;
        }
        
        // Agent status
        const agent = data.agent || {};
        const agentStatusEl = document.getElementById('agent-status');
        const agentHeartbeatEl = document.getElementById('agent-heartbeat');
        const agentIdEl = document.getElementById('agent-id');
        const agentUptimeEl = document.getElementById('agent-uptime');
        
        if (agent.online && agent.agents && agent.agents.length > 0) {
            const a = agent.agents[0];
            agentStatusEl.className = 'connection-status';
            agentStatusEl.innerHTML = `
                <span class="status-dot" style="background: var(--success)"></span>
                Connected
            `;
            agentHeartbeatEl.textContent = this.formatTime(a.last_heartbeat);
            agentIdEl.textContent = a.agent_id || '-';
            agentUptimeEl.textContent = this.formatUptime(a.uptime_seconds);
        } else {
            agentStatusEl.className = 'connection-status';
            agentStatusEl.innerHTML = `
                <span class="status-dot" style="background: var(--danger)"></span>
                Disconnected
            `;
            agentHeartbeatEl.textContent = '-';
            agentIdEl.textContent = '-';
            agentUptimeEl.textContent = '-';
        }
        
        // Playit status
        const playit = data.playit || {};
        const playitStatusEl = document.getElementById('playit-status');
        const playitAddressEl = document.getElementById('playit-java-address');
        const playitBedrockEl = document.getElementById('playit-bedrock');
        
        if (playit.running && playit.hostname) {
            playitStatusEl.className = 'connection-status';
            playitStatusEl.innerHTML = `
                <span class="status-dot" style="background: var(--success)"></span>
                Active
            `;
            playitAddressEl.textContent = `${playit.hostname}:${playit.port}`;
            playitBedrockEl.textContent = 'Not available on free tier';
        } else {
            playitStatusEl.className = 'connection-status';
            playitStatusEl.innerHTML = `
                <span class="status-dot" style="background: var(--text-muted)"></span>
                Not connected
            `;
            playitAddressEl.textContent = '-';
            playitBedrockEl.textContent = 'Configure Playit';
        }
        
        // Backup status
        const backup = data.backup || {};
        const backupStatusEl = document.getElementById('backup-status');
        const backupLastEl = document.getElementById('backup-last');
        const backupSizeEl = document.getElementById('backup-size');
        const backupEnabledEl = document.getElementById('backup-enabled');
        
        if (backup.last_backup) {
            backupStatusEl.className = 'connection-status';
            backupStatusEl.innerHTML = `
                <span class="status-dot" style="background: var(--success)"></span>
                OK
            `;
            backupLastEl.textContent = backup.last_backup.name || backup.last_backup;
            backupSizeEl.textContent = backup.last_backup.size_mb ? `${backup.last_backup.size_mb.toFixed(1)} MB` : '-';
        } else {
            backupStatusEl.className = 'connection-status';
            backupStatusEl.innerHTML = `
                <span class="status-dot" style="background: var(--text-muted)"></span>
                No backups
            `;
            backupLastEl.textContent = 'Never';
            backupSizeEl.textContent = '-';
        }
        
        backupEnabledEl.textContent = backup.enabled ? 'Yes' : 'No';
        backupEnabledEl.style.color = backup.enabled ? 'var(--success)' : 'var(--text-muted)';
        
        // Show dashboard
        document.getElementById('loading-state').style.display = 'none';
        document.getElementById('dashboard-content').style.display = 'block';
    },
    
    /**
     * Show offline state
     */
    showOffline() {
        document.getElementById('loading-state').style.display = 'none';
        document.getElementById('dashboard-content').style.display = 'none';
        document.getElementById('offline-state').style.display = 'flex';
    },
    
    /**
     * Show error state
     */
    showError(message) {
        document.getElementById('loading-state').style.display = 'none';
        document.getElementById('dashboard-content').style.display = 'none';
        document.getElementById('error-state').style.display = 'flex';
        document.getElementById('error-message').textContent = message;
    },
    
    /**
     * Show temporary message
     */
    showMessage(type, text) {
        // Create or get message element
        let msgEl = document.getElementById('toast-message');
        if (!msgEl) {
            msgEl = document.createElement('div');
            msgEl.id = 'toast-message';
            msgEl.className = 'toast';
            document.body.appendChild(msgEl);
        }
        
        msgEl.textContent = text;
        msgEl.className = `toast toast-${type}`;
        
        // Show and hide
        msgEl.style.display = 'block';
        setTimeout(() => {
            msgEl.style.display = 'none';
        }, 3000);
    },
    
    /**
     * Format timestamp
     */
    formatTime(timestamp) {
        if (!timestamp) return '-';
        const date = new Date(timestamp * 1000);
        return date.toLocaleTimeString();
    },
    
    /**
     * Format uptime
     */
    formatUptime(seconds) {
        if (!seconds) return '-';
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const secs = seconds % 60;
        return `${hours}h ${minutes}m ${secs}s`;
    }
};

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    Dashboard.init();
});
