document.addEventListener('DOMContentLoaded', () => {
    const chatMessages = document.getElementById('chat-messages');
    const chatForm = document.getElementById('chat-form');
    const userInput = document.getElementById('user-input');
    const sendBtn = document.getElementById('send-btn');
    const loginModal = document.getElementById('login-modal');
    const loginForm = document.getElementById('login-form');
    const loginError = document.getElementById('login-error');
    const userProfile = document.getElementById('user-profile');
    const usernameDisplay = document.getElementById('username-display');
    const logoutBtn = document.getElementById('logout-btn');
    const historyList = document.getElementById('history-list');
    const newChatBtn = document.getElementById('new-chat-btn');

    let socket = null;
    let token = localStorage.getItem('token');
    let currentSessionId = null;

    // --- Authentication ---

    const checkAuth = async () => {
        if (!token) {
            showLogin();
            return;
        }

        try {
            const response = await fetch('/api/auth/me', {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (response.ok) {
                const user = await response.json();
                usernameDisplay.textContent = user.username;
                userProfile.style.display = 'flex';
                loginModal.style.display = 'none';
                initApp();
            } else {
                showLogin();
            }
        } catch (e) {
            showLogin();
        }
    };

    const showLogin = () => {
        token = null;
        localStorage.removeItem('token');
        loginModal.style.display = 'flex';
        userProfile.style.display = 'none';
    };

    loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const username = document.getElementById('username').value;
        const password = document.getElementById('password').value;

        const formData = new FormData();
        formData.append('username', username);
        formData.append('password', password);

        try {
            const response = await fetch('/api/auth/login', {
                method: 'POST',
                body: formData
            });

            if (response.ok) {
                const data = await response.json();
                token = data.access_token;
                localStorage.setItem('token', token);
                loginError.style.display = 'none';
                checkAuth();
            } else {
                loginError.textContent = 'Invalid username or password';
                loginError.style.display = 'block';
            }
        } catch (err) {
            loginError.textContent = 'Connection failed';
            loginError.style.display = 'block';
        }
    });

    logoutBtn.addEventListener('click', () => {
        showLogin();
        location.reload();
    });

    // --- Session Management ---

    const initApp = () => {
        connectWebSocket();
        fetchSessions();
    };

    const fetchSessions = async () => {
        try {
            const response = await fetch('/api/sessions', {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (response.ok) {
                const data = await response.json();
                renderSessions(data.sessions);
            }
        } catch (e) {
            console.error('Failed to fetch sessions', e);
        }
    };

    const renderSessions = (sessions) => {
        if (!sessions || sessions.length === 0) {
            historyList.innerHTML = '<div class="history-empty">No history yet</div>';
            return;
        }

        historyList.innerHTML = '';
        sessions.forEach(session => {
            const div = document.createElement('div');
            div.className = `history-item ${session.id === currentSessionId ? 'active' : ''}`;
            div.textContent = session.title;
            div.title = session.title;
            div.onclick = () => loadSession(session.id);
            historyList.appendChild(div);
        });
    };

    const loadSession = async (sessionId) => {
        if (currentSessionId === sessionId) return;

        currentSessionId = sessionId;
        chatMessages.innerHTML = ''; // Clear chat window

        // Update active state in sidebar
        document.querySelectorAll('.history-item').forEach(item => {
            item.classList.toggle('active', item.textContent === sessionId); // This is wrong, should use data-id
        });
        // Re-render to be safe
        fetchSessions();

        try {
            const response = await fetch(`/api/sessions/${sessionId}`, {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (response.ok) {
                const data = await response.json();
                data.messages.forEach(msg => {
                    addMessage('user', msg.query, null, false);
                    addMessage('assistant', msg.answer, msg.feedback_id, false);
                });
            }
        } catch (e) {
            console.error('Failed to load session history', e);
        }
    };

    newChatBtn.addEventListener('click', async () => {
        chatMessages.innerHTML = `
            <div class="message assistant">
                <div class="avatar"><i class="fas fa-robot"></i></div>
                <div class="content">
                    New session started. How can I help you today?
                </div>
            </div>
        `;
        currentSessionId = null;
        userInput.focus();
        fetchSessions(); // Refresh sidebar
    });

    // --- WebSocket & Chat ---

    const connectWebSocket = () => {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/chat?token=${token}`;

        socket = new WebSocket(wsUrl);

        socket.onopen = () => {
            console.log('WebSocket connected');
            sendBtn.disabled = false;
        };

        socket.onmessage = (event) => {
            const data = JSON.parse(event.data);
            if (data.type === 'status') {
                updateLastMessageStatus(data.content);
            } else if (data.type === 'answer') {
                if (!currentSessionId) {
                    currentSessionId = data.session_id;
                    fetchSessions(); // Refresh sidebar to show new session
                }
                addMessage('assistant', data.content, data.feedback_id);
            } else if (data.error) {
                addMessage('assistant', `Error: ${data.error}`);
            }
        };

        socket.onclose = () => {
            console.log('WebSocket disconnected');
            sendBtn.disabled = true;
            setTimeout(connectWebSocket, 3000);
        };
    };

    function addMessage(role, content, feedbackId = null, scroll = true) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${role}`;

        const avatar = document.createElement('div');
        avatar.className = 'avatar';
        avatar.innerHTML = role === 'user' ? '<i class="fas fa-user"></i>' : '<i class="fas fa-robot"></i>';

        const contentDiv = document.createElement('div');
        contentDiv.className = 'content';
        contentDiv.textContent = content;

        messageDiv.appendChild(avatar);
        messageDiv.appendChild(contentDiv);

        if (role === 'assistant' && feedbackId) {
            const feedbackArea = document.createElement('div');
            feedbackArea.className = 'feedback-area';
            feedbackArea.innerHTML = `
                <button class="feedback-btn" onclick="sendFeedback('${feedbackId}', 'good', this)">
                    <i class="fas fa-thumbs-up"></i>
                </button>
                <button class="feedback-btn" onclick="sendFeedback('${feedbackId}', 'bad', this)">
                    <i class="fas fa-thumbs-down"></i>
                </button>
            `;
            contentDiv.appendChild(feedbackArea);
        }

        chatMessages.appendChild(messageDiv);
        if (scroll) chatMessages.scrollTop = chatMessages.scrollHeight;

        // Remove status message if it exists
        const statusMsg = document.querySelector('.message.status-msg');
        if (statusMsg) statusMsg.remove();
    }

    function updateLastMessageStatus(status) {
        let statusMsg = document.querySelector('.message.status-msg');
        if (!statusMsg) {
            statusMsg = document.createElement('div');
            statusMsg.className = 'message assistant status-msg';
            statusMsg.innerHTML = `
                <div class="avatar"><i class="fas fa-spinner fa-spin"></i></div>
                <div class="content" style="opacity: 0.7; font-style: italic;">${status}</div>
            `;
            chatMessages.appendChild(statusMsg);
        } else {
            statusMsg.querySelector('.content').textContent = status;
        }
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    chatForm.addEventListener('submit', (e) => {
        e.preventDefault();
        const query = userInput.value.trim();
        if (!query || !socket || socket.readyState !== WebSocket.OPEN) return;

        addMessage('user', query);
        socket.send(JSON.stringify({
            query,
            session_id: currentSessionId
        }));
        userInput.value = '';
        userInput.style.height = 'auto';
    });

    // Auto-resize textarea
    userInput.addEventListener('input', () => {
        userInput.style.height = 'auto';
        userInput.style.height = userInput.scrollHeight + 'px';
    });

    userInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            chatForm.dispatchEvent(new Event('submit'));
        }
    });

    checkAuth();
});

async function sendFeedback(feedbackId, feedback, btn) {
    const token = localStorage.getItem('token');
    try {
        const response = await fetch('/api/feedback', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': `Bearer ${token}`
            },
            body: JSON.stringify({ feedback_id: feedbackId, feedback: feedback })
        });

        if (response.ok) {
            const parent = btn.parentElement;
            parent.querySelectorAll('.feedback-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
        }
    } catch (e) {
        console.error('Feedback failed', e);
    }
}
