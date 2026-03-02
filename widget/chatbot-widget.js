/**
 * AI Chatbot Widget — встраиваемый виджет для сайта
 *
 * Встраивается одной строкой в HTML:
 * <script src="https://your-domain.com/widget/chatbot-widget.js"
 *   data-api-url="https://your-api.com"
 *   data-company="MyCompany"
 *   data-color="#4F46E5"
 *   data-greeting="Привет! Чем могу помочь?">
 * </script>
 */

(function () {
  "use strict";

  // ── Конфиг из data-атрибутов ─────────────────────────────────────────────
  const scriptTag = document.currentScript;
  const API_URL = (scriptTag && scriptTag.getAttribute("data-api-url")) || "http://localhost:8000";
  const COMPANY = (scriptTag && scriptTag.getAttribute("data-company")) || "Chatbot";
  const COLOR = (scriptTag && scriptTag.getAttribute("data-color")) || "#4F46E5";
  const GREETING = (scriptTag && scriptTag.getAttribute("data-greeting")) || "Привет! Чем могу помочь?";

  // ── Session ID (persist в localStorage) ──────────────────────────────────
  let sessionId = localStorage.getItem("chatbot_session_id");
  if (!sessionId) {
    sessionId = "w_" + Math.random().toString(36).slice(2) + Date.now();
    localStorage.setItem("chatbot_session_id", sessionId);
  }

  // ── Стили ─────────────────────────────────────────────────────────────────
  const css = `
    #cb-widget-btn {
      position: fixed; bottom: 24px; right: 24px; z-index: 9999;
      width: 56px; height: 56px; border-radius: 50%;
      background: ${COLOR}; border: none; cursor: pointer;
      box-shadow: 0 4px 16px rgba(0,0,0,0.25);
      display: flex; align-items: center; justify-content: center;
      transition: transform 0.2s;
    }
    #cb-widget-btn:hover { transform: scale(1.08); }
    #cb-widget-btn svg { width: 26px; height: 26px; fill: white; }

    #cb-widget-box {
      position: fixed; bottom: 90px; right: 24px; z-index: 9998;
      width: 360px; max-height: 520px;
      background: #fff; border-radius: 16px;
      box-shadow: 0 8px 32px rgba(0,0,0,0.18);
      display: none; flex-direction: column;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      font-size: 14px; overflow: hidden;
    }
    #cb-widget-box.cb-open { display: flex; }

    #cb-header {
      background: ${COLOR}; color: white;
      padding: 14px 18px; font-weight: 600;
      display: flex; align-items: center; justify-content: space-between;
    }
    #cb-header-info { display: flex; align-items: center; gap: 10px; }
    #cb-avatar {
      width: 32px; height: 32px; border-radius: 50%;
      background: rgba(255,255,255,0.3);
      display: flex; align-items: center; justify-content: center;
      font-size: 16px;
    }
    #cb-close {
      background: none; border: none; color: white;
      cursor: pointer; font-size: 20px; line-height: 1; opacity: 0.8;
    }
    #cb-close:hover { opacity: 1; }

    #cb-messages {
      flex: 1; overflow-y: auto; padding: 16px 14px;
      display: flex; flex-direction: column; gap: 10px;
      background: #f8f8fb;
    }
    .cb-msg {
      max-width: 82%; padding: 10px 14px; border-radius: 12px;
      line-height: 1.45; word-break: break-word;
    }
    .cb-msg.cb-bot {
      background: white; color: #222;
      border: 1px solid #e8e8f0; align-self: flex-start;
      border-bottom-left-radius: 4px;
    }
    .cb-msg.cb-user {
      background: ${COLOR}; color: white;
      align-self: flex-end; border-bottom-right-radius: 4px;
    }
    .cb-typing {
      display: flex; gap: 4px; align-items: center;
      padding: 10px 14px; background: white;
      border: 1px solid #e8e8f0; border-radius: 12px;
      border-bottom-left-radius: 4px; align-self: flex-start;
    }
    .cb-typing span {
      width: 6px; height: 6px; border-radius: 50%;
      background: #aaa; animation: cb-bounce 1.2s infinite;
    }
    .cb-typing span:nth-child(2) { animation-delay: 0.2s; }
    .cb-typing span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes cb-bounce {
      0%, 80%, 100% { transform: translateY(0); }
      40% { transform: translateY(-6px); }
    }

    #cb-input-area {
      display: flex; padding: 10px 12px; gap: 8px;
      border-top: 1px solid #eee; background: white;
    }
    #cb-input {
      flex: 1; border: 1px solid #ddd; border-radius: 20px;
      padding: 8px 14px; font-size: 14px; outline: none;
      font-family: inherit; resize: none; max-height: 100px;
    }
    #cb-input:focus { border-color: ${COLOR}; }
    #cb-send {
      width: 38px; height: 38px; border-radius: 50%;
      background: ${COLOR}; border: none; cursor: pointer;
      display: flex; align-items: center; justify-content: center;
      flex-shrink: 0; transition: opacity 0.15s;
    }
    #cb-send:hover { opacity: 0.85; }
    #cb-send svg { width: 18px; height: 18px; fill: white; }
    #cb-send:disabled { opacity: 0.4; cursor: default; }

    #cb-footer {
      text-align: center; font-size: 11px; color: #bbb;
      padding: 6px; background: white;
    }

    @media (max-width: 420px) {
      #cb-widget-box { width: calc(100vw - 16px); right: 8px; bottom: 80px; }
    }
  `;

  const styleEl = document.createElement("style");
  styleEl.textContent = css;
  document.head.appendChild(styleEl);

  // ── HTML виджета ──────────────────────────────────────────────────────────
  const container = document.createElement("div");
  container.innerHTML = `
    <button id="cb-widget-btn" aria-label="Открыть чат">
      <svg viewBox="0 0 24 24"><path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z"/></svg>
    </button>

    <div id="cb-widget-box" role="dialog" aria-label="Чат с поддержкой">
      <div id="cb-header">
        <div id="cb-header-info">
          <div id="cb-avatar">🤖</div>
          <div>
            <div style="font-size:15px">${COMPANY}</div>
            <div style="font-size:11px;opacity:0.8">Обычно отвечаем мгновенно</div>
          </div>
        </div>
        <button id="cb-close" aria-label="Закрыть">✕</button>
      </div>

      <div id="cb-messages"></div>

      <div id="cb-input-area">
        <textarea id="cb-input" rows="1" placeholder="Напишите сообщение..." aria-label="Сообщение"></textarea>
        <button id="cb-send" aria-label="Отправить">
          <svg viewBox="0 0 24 24"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/></svg>
        </button>
      </div>
      <div id="cb-footer">Powered by AI</div>
    </div>
  `;
  document.body.appendChild(container);

  // ── Элементы ──────────────────────────────────────────────────────────────
  const btn = document.getElementById("cb-widget-btn");
  const box = document.getElementById("cb-widget-box");
  const messages = document.getElementById("cb-messages");
  const input = document.getElementById("cb-input");
  const sendBtn = document.getElementById("cb-send");
  const closeBtn = document.getElementById("cb-close");

  let isOpen = false;
  let isLoading = false;
  let typingEl = null;

  // ── Helpers ───────────────────────────────────────────────────────────────
  function addMessage(text, role) {
    const el = document.createElement("div");
    el.className = "cb-msg cb-" + role;
    // Простой рендер переносов строк
    el.innerHTML = text.replace(/\n/g, "<br>");
    messages.appendChild(el);
    messages.scrollTop = messages.scrollHeight;
    return el;
  }

  function showTyping() {
    typingEl = document.createElement("div");
    typingEl.className = "cb-typing";
    typingEl.innerHTML = "<span></span><span></span><span></span>";
    messages.appendChild(typingEl);
    messages.scrollTop = messages.scrollHeight;
  }

  function hideTyping() {
    if (typingEl) { typingEl.remove(); typingEl = null; }
  }

  function setLoading(state) {
    isLoading = state;
    sendBtn.disabled = state;
    input.disabled = state;
  }

  // ── Toggle ────────────────────────────────────────────────────────────────
  function openChat() {
    isOpen = true;
    box.classList.add("cb-open");
    input.focus();

    // Показываем приветствие только первый раз
    if (messages.children.length === 0) {
      addMessage(GREETING, "bot");
    }
  }

  function closeChat() {
    isOpen = false;
    box.classList.remove("cb-open");
  }

  btn.addEventListener("click", () => isOpen ? closeChat() : openChat());
  closeBtn.addEventListener("click", closeChat);

  // ── Send ──────────────────────────────────────────────────────────────────
  async function sendMessage() {
    const text = input.value.trim();
    if (!text || isLoading) return;

    input.value = "";
    input.style.height = "auto";
    addMessage(text, "user");
    setLoading(true);
    showTyping();

    try {
      const res = await fetch(`${API_URL}/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, session_id: sessionId }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();

      hideTyping();
      addMessage(data.response, "bot");
    } catch (err) {
      hideTyping();
      addMessage("Не удалось получить ответ. Попробуйте ещё раз.", "bot");
      console.error("[chatbot-widget] error:", err);
    } finally {
      setLoading(false);
      input.focus();
    }
  }

  sendBtn.addEventListener("click", sendMessage);

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  // Auto-resize textarea
  input.addEventListener("input", function () {
    this.style.height = "auto";
    this.style.height = Math.min(this.scrollHeight, 100) + "px";
  });
})();
