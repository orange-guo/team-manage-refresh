(() => {
  const form = document.getElementById('experienceForm');
  const emailInput = document.getElementById('email');
  const submitBtn = document.getElementById('submitBtn');
  const resultBox = document.getElementById('resultBox');
  const resultIcon = document.getElementById('resultIcon');
  const resultMessage = document.getElementById('resultMessage');
  const teamInfo = document.getElementById('teamInfo');
  const countdown = document.getElementById('countdown');
  const remainingSpots = document.getElementById('remainingSpots');

  const activeListWrap = document.getElementById('activeListWrap');
  const activeListRows = document.getElementById('activeListRows');
  const activeListEmpty = document.getElementById('activeListEmpty');

  const queueListWrap = document.getElementById('queueListWrap');
  const queueListRows = document.getElementById('queueListRows');
  const queueListEmpty = document.getElementById('queueListEmpty');

  let timer = null;

  function clearTimer() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  function formatTime(sec) {
    const s = Math.max(0, Number(sec || 0));
    const mm = String(Math.floor(s / 60)).padStart(2, '0');
    const ss = String(s % 60).padStart(2, '0');
    return `${mm}:${ss}`;
  }

  function formatDateTime(value) {
    if (!value) return '-';
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return '-';
    try {
      return d.toLocaleString('zh-CN', { hour12: false });
    } catch (_) {
      return '-';
    }
  }

  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function setResultStyle(type) {
    if (!resultBox) return;

    if (type === 'success') {
      resultBox.style.display = 'flex';
      resultBox.style.borderColor = 'rgba(34, 197, 94, 0.35)';
      resultBox.style.background = 'rgba(34, 197, 94, 0.12)';
      resultBox.style.color = '#86efac';
      resultIcon.className = 'fa-solid fa-circle-check';
    } else {
      resultBox.style.display = 'flex';
      resultBox.style.borderColor = 'rgba(245, 158, 11, 0.3)';
      resultBox.style.background = 'rgba(245, 158, 11, 0.1)';
      resultBox.style.color = '#facc74';
      resultIcon.className = 'fa-solid fa-triangle-exclamation';
    }
  }

  function showActiveResult(payload) {
    clearTimer();
    setResultStyle('success');

    resultMessage.textContent = payload.message || '拉入成功';

    const t = payload.team_info || {};
    const teamLabel = [t.team_name, t.team_email].filter(Boolean).join(' / ');
    teamInfo.textContent = teamLabel ? `账号：${teamLabel}` : '';

    let remain = Number(payload.seconds_remaining || 0);
    countdown.textContent = `剩余时间：${formatTime(remain)}`;

    timer = setInterval(() => {
      remain -= 1;
      if (remain <= 0) {
        clearTimer();
        countdown.textContent = '倒计时结束，系统将自动移出并按队列顺序补位（最多约 1 分钟内完成）';
        return;
      }
      countdown.textContent = `剩余时间：${formatTime(remain)}`;
    }, 1000);
  }

  function showQueuedResult(payload) {
    clearTimer();
    setResultStyle('success');

    resultMessage.textContent = payload.message || '已加入排队';

    const pos = Number(payload.queue_position || 0);
    teamInfo.textContent = pos > 0 ? `当前排位：第 ${pos} 位` : '当前已在队列中';
    countdown.textContent = '满位释放后会自动补位，无需重复提交。';
  }

  function showError(message) {
    clearTimer();
    setResultStyle('error');
    resultMessage.textContent = message || '操作失败，请稍后重试';
    teamInfo.textContent = '';
    countdown.textContent = '';
  }

  async function refreshSpots() {
    try {
      const res = await fetch('/free/spots', { method: 'GET' });
      if (!res.ok) return;
      const data = await res.json();
      if (data && data.success && typeof data.remaining_spots !== 'undefined') {
        remainingSpots.textContent = String(data.remaining_spots);
      }
    } catch (_) {
      // 静默
    }
  }

  function renderActiveList(items) {
    if (!activeListRows || !activeListWrap || !activeListEmpty) return;

    if (!items || items.length === 0) {
      activeListRows.innerHTML = '';
      activeListWrap.style.display = 'none';
      activeListEmpty.style.display = 'block';
      return;
    }

    activeListWrap.style.display = 'block';
    activeListEmpty.style.display = 'none';

    activeListRows.innerHTML = items.map((item) => {
      const email = escapeHtml(item.email || '');
      const teamEmail = escapeHtml(item.team_email || '-');
      const secs = Math.max(0, Number(item.seconds_remaining || 0));
      return `
        <div class="active-list-row">
          <span>${email}</span>
          <span>${teamEmail}</span>
          <span class="active-countdown" data-seconds="${secs}">${formatTime(secs)}</span>
        </div>
      `;
    }).join('');
  }

  function renderQueueList(items) {
    if (!queueListRows || !queueListWrap || !queueListEmpty) return;

    if (!items || items.length === 0) {
      queueListRows.innerHTML = '';
      queueListWrap.style.display = 'none';
      queueListEmpty.style.display = 'block';
      return;
    }

    queueListWrap.style.display = 'block';
    queueListEmpty.style.display = 'none';

    queueListRows.innerHTML = items.map((item) => {
      const pos = Number(item.position || 0);
      const email = escapeHtml(item.email || '');
      const queuedAt = escapeHtml(formatDateTime(item.queued_at));
      return `
        <div class="queue-list-row">
          <span>第 ${pos || '-'} 位</span>
          <span>${email}</span>
          <span>${queuedAt}</span>
        </div>
      `;
    }).join('');
  }

  function tickActiveCountdowns() {
    document.querySelectorAll('.active-countdown[data-seconds]').forEach((el) => {
      const curr = Math.max(0, Number(el.getAttribute('data-seconds') || 0));
      const next = Math.max(0, curr - 1);
      el.setAttribute('data-seconds', String(next));
      el.textContent = formatTime(next);
    });
  }

  async function refreshActiveList() {
    try {
      const res = await fetch('/free/active', { method: 'GET' });
      if (!res.ok) return;
      const data = await res.json();
      if (!data || !data.success) return;
      renderActiveList(Array.isArray(data.items) ? data.items : []);
    } catch (_) {
      // 静默
    }
  }

  async function refreshQueueList() {
    try {
      const res = await fetch('/free/queue', { method: 'GET' });
      if (!res.ok) return;
      const data = await res.json();
      if (!data || !data.success) return;
      renderQueueList(Array.isArray(data.items) ? data.items : []);
    } catch (_) {
      // 静默
    }
  }

  async function submit(email) {
    const res = await fetch('/free/join', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email })
    });

    let data = {};
    try {
      data = await res.json();
    } catch (_) {
      // ignore
    }

    if (!res.ok || !data.success) {
      const msg = data.error || data.detail || '提交失败，请稍后重试';
      showError(msg);
      return;
    }

    if (data.status === 'queued') {
      showQueuedResult(data);
    } else {
      showActiveResult(data);
    }

    refreshSpots();
    refreshActiveList();
    refreshQueueList();
  }

  form?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = (emailInput?.value || '').trim();
    if (!email) {
      showError('请输入邮箱');
      return;
    }

    submitBtn.disabled = true;
    const oldText = submitBtn.innerHTML;
    submitBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> 处理中...';

    try {
      await submit(email);
    } finally {
      submitBtn.disabled = false;
      submitBtn.innerHTML = oldText;
    }
  });

  // 初始加载 + 实时刷新
  refreshSpots();
  refreshActiveList();
  refreshQueueList();

  setInterval(refreshSpots, 15000);
  setInterval(refreshActiveList, 5000);
  setInterval(refreshQueueList, 5000);
  setInterval(tickActiveCountdowns, 1000);
})();
