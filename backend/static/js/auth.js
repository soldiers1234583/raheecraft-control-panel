/**
 * RaheeCraft Auth Module
 */
const Auth = {
    API_BASE: '/api',
    
    /**
     * Login with credentials
     */
    async login(username, password) {
        try {
            const response = await fetch(`${this.API_BASE}/auth/login`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ username, password })
            });
            
            const data = await response.json();
            
            if (!response.ok) {
                throw new Error(data.error || 'Login failed');
            }
            
            // Store session
            document.cookie = `raheecraft_session=${data.session_id}; Path=/; HttpOnly; SameSite=Lax`;
            
            return { success: true, user: data.username };
        } catch (error) {
            return { success: false, error: error.message };
        }
    },
    
    /**
     * Logout
     */
    async logout() {
        try {
            await fetch(`${this.API_BASE}/auth/logout`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Cookie': document.cookie
                }
            });
        } catch (error) {
            console.error('Logout error:', error);
        }
        
        // Clear cookie
        document.cookie = 'raheecraft_session=; Path=/; Max-Age=0';
        
        // Redirect to login
        window.location.href = '/login';
    },
    
    /**
     * Check if user is logged in
     */
    isLoggedIn() {
        const cookie = document.cookie;
        return cookie.includes('raheecraft_session=');
    },
    
    /**
     * Get current user from cookie
     */
    getCurrentUser() {
        const cookie = document.cookie;
        const match = cookie.match(/raheecraft_session=([^;]+)/);
        if (!match) return null;
        
        // For now, we don't expose username in cookie
        // The backend will provide it
        return null;
    }
};
