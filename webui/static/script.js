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

    let socket = null;
    let token = localStorage.getItem('token');

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

    // --- App Logic ---

    const initApp = () => {
        connectWebSocket();
        fetchHistory();
    };

    const fetchHistory = async () => {
        try {
            const response = await fetch('/api/history', {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (response.ok) {
                const data = await response.json();
                renderHistory(data.history);
            }
        } catch (e) {
            console.error('Failed to fetch history', e);
        }
    };

    const renderHistory = (history) => {
        if (!history || history.length === 0) {
            historyList.innerHTML = '<div class="history-empty">No history yet</div>';
            return;
        }

        historyList.innerHTML = '';
        history.reverse().forEach(item => {
            const div = document.createElement('div');
            div.className = 'history-item';
            div.textContent = item.query;
            div.title = item.query;
            div.onclick = () => {
                userInput.value = item.query;
                userInput.focus();
            };
            historyList.appendChild(div);
        });
    };

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
                addMessage('assistant', data.content, data.feedback_id);
            } else if (data.error) {
                addMessage('assistant', `Error: ${data.error}`);
            }
        };

        socket.onclose = () => {
            console.log('WebSocket disconnected');
            sendBtn.disabled = true;
            // Try to reconnect after 3 seconds
            setTimeout(connectWebSocket, 3000);
        };
    };

    // --- UI Helpers ---

    function addMessage(role, content, feedbackId = null) {
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
        chatMessages.scrollTop = chatMessages.scrollHeight;

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
        socket.send(JSON.stringify({ query }));
        userInput.value = '';
        userInput.style.height = 'auto';
    });

    // Auto-resize textarea
    userInput.addEventListener('input', () => {
        userInput.style.height = 'auto';
        userInput.style.height = userInput.scrollHeight + 'px';
    });

    // Handle Enter key
    userInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            chatForm.dispatchEvent(new Event('submit'));
        }
    });

    // Initial check
    checkAuth();
});

// Global feedback function
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
            // Toggle active state
            const parent = btn.parentElement;
            parent.querySelectorAll('.feedback-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
        }
    } catch (e) {
        console.error('Feedback failed', e);
    }
}
