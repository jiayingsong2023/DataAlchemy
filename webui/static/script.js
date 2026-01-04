document.addEventListener('DOMContentLoaded', () => {
    const chatForm = document.getElementById('chat-form');
    const userInput = document.getElementById('user-input');
    const chatMessages = document.getElementById('chat-messages');
    const sendBtn = document.getElementById('send-btn');

    let socket = null;

    function initWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws/chat`;

        socket = new WebSocket(wsUrl);

        socket.onopen = () => {
            console.log('[WebUI] WebSocket connected');
            document.querySelector('.status').innerHTML = '<span class="status-dot"></span> Online';
        };

        socket.onmessage = (event) => {
            const data = JSON.parse(event.data);

            if (data.error) {
                addMessage('抱歉，发生了错误：' + data.error, 'assistant');
                setLoading(false);
                return;
            }

            if (data.type === 'status') {
                updateStatus(data.content);
            } else if (data.type === 'answer') {
                removeStatus();
                addMessage(data.content, 'assistant', data.feedback_id);
                setLoading(false);
            }
        };

        socket.onclose = () => {
            console.log('[WebUI] WebSocket disconnected, retrying...');
            document.querySelector('.status').innerHTML = '<span class="status-dot offline"></span> Offline (Retrying...)';
            setTimeout(initWebSocket, 3000);
        };

        socket.onerror = (error) => {
            console.error('[WebUI] WebSocket error:', error);
        };
    }

    initWebSocket();

    // Auto-resize textarea
    userInput.addEventListener('input', () => {
        userInput.style.height = 'auto';
        userInput.style.height = userInput.scrollHeight + 'px';
    });

    // Handle form submission
    chatForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const query = userInput.value.trim();
        if (!query) return;

        // Add user message to UI
        addMessage(query, 'user');
        userInput.value = '';
        userInput.style.height = 'auto';

        // Disable input while waiting
        setLoading(true);

        if (socket && socket.readyState === WebSocket.OPEN) {
            socket.send(JSON.stringify({ query }));
        } else {
            addMessage('抱歉，连接已断开，请刷新页面。', 'assistant');
            setLoading(false);
        }
    });

    // Enter to submit (Shift+Enter for newline)
    userInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            chatForm.dispatchEvent(new Event('submit'));
        }
    });

    let currentStatusMsg = null;

    function updateStatus(text) {
        if (!currentStatusMsg) {
            currentStatusMsg = document.createElement('div');
            currentStatusMsg.className = 'message assistant status-msg';
            currentStatusMsg.innerHTML = `
                <div class="avatar"><i class="fas fa-spinner fa-spin"></i></div>
                <div class="content-container">
                    <div class="content status-text">${text}</div>
                </div>
            `;
            chatMessages.appendChild(currentStatusMsg);
        } else {
            currentStatusMsg.querySelector('.status-text').innerText = text;
        }
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    function removeStatus() {
        if (currentStatusMsg) {
            currentStatusMsg.remove();
            currentStatusMsg = null;
        }
    }

    function addMessage(text, role, feedbackId = null) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${role}`;

        const avatar = document.createElement('div');
        avatar.className = 'avatar';
        avatar.innerHTML = role === 'user' ? '<i class="fas fa-user"></i>' : '<i class="fas fa-robot"></i>';

        const contentContainer = document.createElement('div');
        contentContainer.className = 'content-container';

        const content = document.createElement('div');
        content.className = 'content';
        content.innerText = text;

        contentContainer.appendChild(content);

        if (role === 'assistant' && feedbackId) {
            const feedbackArea = document.createElement('div');
            feedbackArea.className = 'feedback-area';

            const upBtn = document.createElement('button');
            upBtn.className = 'feedback-btn active'; // Default is good
            upBtn.innerHTML = '<i class="far fa-thumbs-up"></i>';
            upBtn.onclick = () => updateFeedback(feedbackId, 'good', upBtn, downBtn);

            const downBtn = document.createElement('button');
            downBtn.className = 'feedback-btn';
            downBtn.innerHTML = '<i class="far fa-thumbs-down"></i>';
            downBtn.onclick = () => updateFeedback(feedbackId, 'bad', downBtn, upBtn);

            feedbackArea.appendChild(upBtn);
            feedbackArea.appendChild(downBtn);
            contentContainer.appendChild(feedbackArea);
        }

        messageDiv.appendChild(avatar);
        messageDiv.appendChild(contentContainer);
        chatMessages.appendChild(messageDiv);

        // Scroll to bottom
        chatMessages.scrollTop = chatMessages.scrollHeight;
    }

    async function updateFeedback(feedbackId, status, activeBtn, otherBtn) {
        try {
            const response = await fetch('/api/feedback', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ feedback_id: feedbackId, feedback: status }),
            });

            if (response.ok) {
                activeBtn.classList.add('active');
                otherBtn.classList.remove('active');
            }
        } catch (error) {
            console.error('Error updating feedback:', error);
        }
    }

    function setLoading(isLoading) {
        userInput.disabled = isLoading;
        sendBtn.disabled = isLoading;
        if (isLoading) {
            sendBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
        } else {
            sendBtn.innerHTML = '<i class="fas fa-paper-plane"></i>';
            userInput.focus();
        }
    }
});
