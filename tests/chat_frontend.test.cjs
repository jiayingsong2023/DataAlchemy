// Browser protocol test only: DOM, transport, and authentication are substitutes.
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { randomUUID } = require('node:crypto');
const { test } = require('node:test');
const vm = require('node:vm');

async function browser(sessionMessages = []) {
    const elements = new Map();
    const sockets = [];
    const timers = [];
    const requests = [];
    class Element {
        constructor() {
            this.children = [];
            this.handlers = {};
            this.style = {};
            this.value = '';
            this.classList = { toggle() {}, remove() {} };
        }
        set innerHTML(value) { this.html = value; this.children = []; }
        get innerHTML() { return this.html; }
        appendChild(child) { this.children.push(child); }
        setAttribute(name, value) { this[name] = value; }
        getAttribute(name) { return this[name]; }
        addEventListener(name, handler) { this.handlers[name] = handler; }
        dispatchEvent(event) { return this.handlers[event.type](event); }
        focus() {}
    }
    const get = (id) => {
        if (!elements.has(id)) elements.set(id, new Element());
        return elements.get(id);
    };
    let ready;
    class WebSocket {
        static OPEN = 1;
        constructor() { this.sent = []; this.readyState = 1; sockets.push(this); }
        send(payload) { this.sent.push(payload); }
        receive(payload) { this.onmessage({ data: JSON.stringify(payload) }); }
    }
    vm.runInNewContext(readFileSync(require.resolve('../webui/static/script.js'), 'utf8'), {
        document: {
            getElementById: get,
            createElement: () => new Element(),
            querySelector: () => null,
            querySelectorAll: () => get('history-list').children,
            addEventListener: (_name, handler) => { ready = handler; },
        },
        window: { location: { protocol: 'https:', host: 'test.invalid' } },
        localStorage: { getItem: () => 'token', removeItem() {} },
        crypto: { randomUUID }, WebSocket,
        console: { log() {}, error() {} },
        setTimeout: (handler) => timers.push(handler),
        fetch: async (url) => {
            requests.push(url);
            return {
                ok: url === '/api/auth/me' || url === '/api/sessions' || url === '/api/sessions/other-session',
                json: async () => url === '/api/auth/me'
                    ? { username: 'alice' }
                    : url === '/api/sessions/other-session' ? { messages: sessionMessages }
                    : { sessions: [{ session_id: 'other-session', title: 'Other' }] },
            };
        },
    });
    ready();
    await new Promise(setImmediate);
    sockets[0].onopen();
    const submit = (query) => {
        get('user-input').value = query;
        get('chat-form').handlers.submit({ preventDefault() {} });
    };
    return { get, sockets, timers, requests, submit };
}

test('pending chat keeps its key across reconnect, blocks navigation, and renders once', async () => {
    const { get, sockets, timers, requests, submit } = await browser();
    submit('question');
    const first = sockets[0].sent[0];
    const request = JSON.parse(first);
    assert.match(request.request_id, /^[0-9a-f-]{36}$/);
    assert.equal(request.session_id, null);
    assert.equal(get('send-btn').disabled, true);
    const count = get('chat-messages').children.length;
    get('new-chat-btn').handlers.click();
    await get('history-list').children[0].onclick();
    assert.equal(get('chat-messages').children.length, count);
    assert.equal(requests.includes('/api/sessions/other-session'), false);
    sockets[0].onclose();
    timers.shift()();
    sockets[1].onopen();
    assert.equal(sockets[1].sent[0], first);
    const reply = {
        type: 'answer', content: 'answer', request_id: request.request_id,
        run_id: 'run', session_id: 'returned-session', citations: [],
    };
    sockets[1].receive({ ...reply, request_id: randomUUID() });
    assert.equal(get('chat-messages').children.length, count);
    sockets[1].receive(reply);
    sockets[1].receive(reply);
    assert.equal(get('chat-messages').children.length, count + 1);
    assert.equal(get('send-btn').disabled, false);
    submit('next question');
    const next = JSON.parse(sockets[1].sent[1]);
    assert.notEqual(next.request_id, request.request_id);
    assert.equal(next.session_id, 'returned-session');
});

test('transient errors retry identical payload while terminal errors release pending chat', async () => {
    const { sockets, submit } = await browser();
    const socket = sockets[0];
    submit('question');
    const first = socket.sent[0];
    for (const error of [
        { error: 'chat_execution_failed', status_code: 500 },
        { error: 'chat_request_in_progress', status_code: 409 },
    ]) {
        socket.receive(error);
        submit('must not replace pending request');
        assert.equal(socket.sent.at(-1), first);
    }
    socket.receive({ error: 'chat_request_failed', status_code: 409 });
    submit('new question');
    const next = JSON.parse(socket.sent.at(-1));
    assert.equal(next.query, 'new question');
    assert.notEqual(next.request_id, JSON.parse(first).request_id);
});

test('v2 status and quotes render as text for live replies and durable history', async () => {
    const contract = { answer_status: 'answered', support_status: 'extract_verified' };
    const citations = [{ source_uri: 'test://source', quote: '<script>untrusted</script>' }];
    const { get, sockets, submit } = await browser([
        { event_type: 'assistant_message', content: { content: 'saved answer', citations, answer_contract: contract } },
    ]);
    await get('history-list').children[0].onclick();
    let content = get('chat-messages').children.at(-1).children[1];
    assert.equal(content.textContent, 'saved answer');
    assert.match(content.children[0].textContent, /不代表语义质量已验收/);
    assert.match(content.children[1].textContent, /<script>untrusted<\/script>/);
    assert.equal(content.children[1].innerHTML, undefined);
    submit('question');
    const request = JSON.parse(sockets[0].sent[0]);
    sockets[0].receive({ type: 'answer', request_id: request.request_id, content: 'refusal',
        answer_status: 'abstained', reason_code: 'no_document_evidence', citations: [] });
    content = get('chat-messages').children.at(-1).children[1];
    assert.match(content.children[0].textContent, /未作答：no_document_evidence/);
});
