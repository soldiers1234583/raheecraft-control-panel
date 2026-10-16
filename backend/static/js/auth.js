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
            
            // The session cookie is set by the server's Set-Cookie on this
            // response (HttpOnly, so scripts cannot read it). A line here that
            // "set" the cookie with HttpOnly was a no-op the browser ignored
            // and could only create a second, script-readable cookie.
            return { success: true, user: data.username };
        } catch (error) {
            return { success: false, error: error.message };
        }
    },
    
    /**
     * Logout
     *
     * The session cookie is HttpOnly, so the server is the only side that can
     * end it: /api/auth/logout destroys the session and deletes the cookie.
     * Sending `document.cookie` as a request header (which would just be the
     * empty script-visible cookie jar) and "expiring" the cookie from here
     * both looked like they were doing something and were not.
     */
    async logout() {
        try {
            await fetch(`${this.API_BASE}/auth/logout`, { method: 'POST' });
        } catch (error) {
            console.error('Logout error:', error);
        }
        window.location.href = '/login';
    },

    /**
     * Check if user is logged in — ask the server, never the cookie jar.
     */
    async isLoggedIn() {
        try {
            const response = await fetch(`${this.API_BASE}/status`);
            return response.status !== 401;
        } catch (error) {
            console.error('Auth check failed:', error);
            return false;
        }
    }
};
