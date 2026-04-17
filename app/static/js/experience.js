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

  function showSuccess(payload) {
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
        countdown.textContent = '倒计时结束，系统将自动移出（最多约 1 分钟内完成）';
        return;
      }
      countdown.textContent = `剩余时间：${formatTime(remain)}`;
    }, 1000);
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

    showSuccess(data);
    refreshSpots();
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

  // 初次刷新一次席位
  refreshSpots();
})();
