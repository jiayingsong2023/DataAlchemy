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
    const taskList = document.getElementById('task-list');
    const taskDetails = document.getElementById('task-details');
    const newTaskBtn = document.getElementById('new-task-btn');
    const ingestDocumentBtn = document.getElementById('ingest-document-btn');
    const connectorRuns = document.getElementById('connector-runs');
    const refreshConnectorsBtn = document.getElementById('refresh-connectors-btn');
    const memorySearchForm = document.getElementById('memory-search-form');
    const memoryQuery = document.getElementById('memory-query');
    const memoryResults = document.getElementById('memory-results');
    const auditEvents = document.getElementById('audit-events');
    const refreshAuditBtn = document.getElementById('refresh-audit-btn');

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
        fetchTasks();
        fetchConnectorRuns();
        fetchAuditEvents();
    };

    const fetchConnectorRuns = async () => {
        const response = await fetch('/api/connectors/runs', { headers: apiHeaders() });
        if (!response.ok) return;
        const { runs } = await response.json();
        connectorRuns.innerHTML = '';
        if (!runs.length) {
            connectorRuns.textContent = 'No connector runs yet';
            return;
        }
        runs.forEach((run) => {
            const item = document.createElement('div');
            item.className = 'task-item';
            item.textContent = run.connector_id;
            const state = document.createElement('span');
            state.className = 'task-state';
            state.textContent = run.state;
            item.appendChild(state);
            item.title = run.error_summary || `Cursor: ${run.cursor_after || run.cursor_before || 'new'}`;
            connectorRuns.appendChild(item);
        });
    };

    refreshConnectorsBtn.addEventListener('click', fetchConnectorRuns);

    memorySearchForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        const query = memoryQuery.value.trim();
        if (!query) return;
        const response = await fetch(`/api/memories?query=${encodeURIComponent(query)}`, {
            headers: apiHeaders()
        });
        if (!response.ok) return;
        const { memories } = await response.json();
        memoryResults.textContent = memories.length
            ? memories.map((item) => `${item.kind}: ${item.content}`).join('\n')
            : 'No approved memory found';
    });

    const fetchAuditEvents = async () => {
        const response = await fetch('/api/audit-events', { headers: apiHeaders() });
        if (response.status === 403) {
            auditEvents.textContent = 'Administrator access required';
            return;
        }
        if (!response.ok) return;
        const { events } = await response.json();
        auditEvents.textContent = events.length
            ? events.slice(0, 10).map((event) => `${event.action}: ${event.outcome}`).join('\n')
            : 'No audit events yet';
    };

    refreshAuditBtn.addEventListener('click', fetchAuditEvents);

    const apiHeaders = () => ({
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`
    });

    const fetchTasks = async () => {
        const response = await fetch('/api/tasks', { headers: apiHeaders() });
        if (response.ok) renderTasks((await response.json()).tasks);
    };

    const renderTasks = (tasks) => {
        taskList.innerHTML = '';
        if (!tasks.length) {
            taskList.textContent = 'No tasks yet';
            return;
        }
        tasks.forEach((task) => {
            const item = document.createElement('button');
            item.className = 'task-item';
            item.textContent = task.goal;
            const state = document.createElement('span');
            state.className = 'task-state';
            state.textContent = task.state;
            item.appendChild(state);
            item.onclick = () => showTask(task.task_id);
            taskList.appendChild(item);
        });
    };

    const showTask = async (taskId) => {
        const [taskResponse, eventResponse, verificationResponse] = await Promise.all([
            fetch(`/api/tasks/${taskId}`, { headers: apiHeaders() }),
            fetch(`/api/tasks/${taskId}/events`, { headers: apiHeaders() }),
            fetch(`/api/tasks/${taskId}/verifications`, { headers: apiHeaders() })
        ]);
        if (!taskResponse.ok || !eventResponse.ok || !verificationResponse.ok) return;
        const task = await taskResponse.json();
        const events = (await eventResponse.json()).events;
        const verifications = (await verificationResponse.json()).verifications;
        taskDetails.innerHTML = '';
        taskDetails.append(`Goal: ${task.goal}\nState: ${task.state}\n`);
        taskDetails.append(`Run: ${task.run_id} · Plan v${task.plan_version} · ${task.task_spec.execution_mode}\n`);
        taskDetails.append(`Plan: ${task.plan.map((step) => step.tool).join(' → ')}\n`);
        taskDetails.append(`Events: ${events.map((event) => event.event_type).join(' → ')}`);
        if (verifications.length) {
            taskDetails.append(
                `\nVerification: ${verifications.map((item) =>
                    `${item.criterion_id} ${item.status} (${item.verifier}@${item.verifier_version} #${item.attempt}, ${item.tool_result_digest.slice(0, 12)})`
                ).join(' · ')}`
            );
        }
        const actions = document.createElement('div');
        actions.className = 'task-actions';
        const action = (label, path, body = null) => {
            const button = document.createElement('button');
            button.className = 'task-action';
            button.textContent = label;
            button.onclick = async () => {
                await fetch(`/api/tasks/${taskId}/${path}`, {
                    method: 'POST', headers: apiHeaders(), body: body && JSON.stringify(body)
                });
                await fetchTasks();
                await showTask(taskId);
            };
            actions.appendChild(button);
        };
        if (task.state === 'waiting_approval') {
            action('Approve', 'approval', { approved: true, expected_version: task.version });
            action('Reject', 'approval', { approved: false, expected_version: task.version });
        } else if (task.state === 'paused') {
            action('Resume', 'resume', { expected_version: task.version });
            if (task.task_spec.execution_mode === 'strict') {
                const replan = document.createElement('button');
                replan.className = 'task-action';
                replan.textContent = 'Replan';
                replan.onclick = async () => {
                    const source = window.prompt('Replacement steps as JSON');
                    const reason = window.prompt('Reason for replan');
                    if (!source || !reason) return;
                    await fetch(`/api/tasks/${taskId}/replan`, {
                        method: 'POST', headers: apiHeaders(),
                        body: JSON.stringify({ remaining_steps: JSON.parse(source), reason, expected_version: task.version })
                    });
                    await fetchTasks();
                    await showTask(taskId);
                };
                actions.appendChild(replan);
            }
        } else if (task.state === 'failed') {
            action('Retry', 'retry', { expected_version: task.version });
        } else if (task.state === 'verification_blocked') {
            action('Retry verification', 'retry-verification', { expected_version: task.version });
        } else if (!['succeeded', 'failed', 'verification_failed', 'cancelled'].includes(task.state)) {
            action('Pause', 'pause', { expected_version: task.version });
            action('Cancel', 'cancel', { expected_version: task.version });
        }
        taskDetails.appendChild(actions);
    };

    newTaskBtn.addEventListener('click', async () => {
        const goal = window.prompt('Task goal');
        if (!goal) return;
        const response = await fetch('/api/tasks', {
            method: 'POST', headers: apiHeaders(),
            body: JSON.stringify({ goal, tool: 'rag_chat', arguments: { query: goal } })
        });
        if (response.ok) {
            const task = await response.json();
            await fetchTasks();
            await showTask(task.task_id);
        }
    });

    ingestDocumentBtn.addEventListener('click', async () => {
        const objectKey = window.prompt('MinIO object key (for example: raw/documents/pilot.md)');
        if (!objectKey) return;
        const query = window.prompt('A phrase expected in the document, for retrieval verification');
        if (!query) return;
        const sourceRef = `raw:document:${objectKey.replace(/^raw\/documents\//, '')}`;
        const response = await fetch('/api/tasks', {
            method: 'POST', headers: apiHeaders(),
            body: JSON.stringify({
                goal: `Import ${objectKey}`,
                execution_mode: 'strict',
                steps: [{
                    tool: 'ingest_document', arguments: { object_key: objectKey }, scope_refs: [sourceRef],
                    verifier_refs: ['ingest', 'retrieval']
                }],
                success_criteria: [
                    { criterion_id: 'ingest', verifier: 'verify_ingest', version: 1, parameters: {}, phase: 'after_step', required: true },
                    { criterion_id: 'retrieval', verifier: 'verify_retrieval', version: 1, parameters: { query }, phase: 'after_step', required: true }
                ],
                data_scope: { source_refs: [sourceRef] },
                limits: { max_steps: 1, deadline_seconds: 300 }
            })
        });
        if (response.ok) {
            const task = await response.json();
            await fetchTasks();
            await showTask(task.task_id);
        }
    });

    const fetchSessions = async () => {
        console.log('API Request: Fetching sessions...');
        try {
            const response = await fetch('/api/sessions', {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (response.ok) {
                const data = await response.json();
                console.log(`Backend returned ${data.sessions.length} sessions`);
                renderSessions(data.sessions);
            } else {
                console.error('API Error:', response.status);
            }
        } catch (e) {
            console.error('Network Error fetching sessions:', e);
        }
    };

    const renderSessions = (sessions) => {
        if (!historyList) return;
        
        if (!sessions || sessions.length === 0) {
            historyList.innerHTML = '<div class="history-empty">No history yet</div>';
            return;
        }

        historyList.innerHTML = '';
        sessions.forEach(session => {
            const div = document.createElement('div');
            div.className = `history-item ${session.id === currentSessionId ? 'active' : ''}`;
            div.setAttribute('data-id', session.id);
            div.innerHTML = `<i class="far fa-comment-alt"></i> <span>${session.title}</span>`;
            div.title = session.title;
            div.onclick = () => loadSession(session.id);
            historyList.appendChild(div);
        });
    };

    const loadSession = async (sessionId) => {
        if (currentSessionId === sessionId) return;

        currentSessionId = sessionId;
        chatMessages.innerHTML = ''; 

        // Update UI state
        document.querySelectorAll('.history-item').forEach(item => {
            item.classList.toggle('active', item.getAttribute('data-id') === sessionId);
        });

        try {
            const response = await fetch(`/api/sessions/${sessionId}`, {
                headers: { 'Authorization': `Bearer ${token}` }
            });
            if (response.ok) {
                const data = await response.json();
                if (data.messages && data.messages.length > 0) {
                    data.messages.forEach(msg => {
                        addMessage('user', msg.query, null, false);
                        addMessage('assistant', msg.answer, msg.feedback_id, false);
                    });
                    chatMessages.scrollTop = chatMessages.scrollHeight;
                } else {
                    addMessage('assistant', 'No messages in this session.', null, false);
                }
            }
        } catch (e) {
            console.error('Failed to load session history', e);
        }
    };

    newChatBtn.addEventListener('click', () => {
        chatMessages.innerHTML = `
            <div class="message assistant">
                <div class="avatar"><i class="fas fa-robot"></i></div>
                <div class="content">
                    New session started. How can I help you today?
                </div>
            </div>
        `;
        currentSessionId = null;
        document.querySelectorAll('.history-item').forEach(i => i.classList.remove('active'));
        userInput.focus();
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
                    fetchSessions(); 
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
