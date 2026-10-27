/** 排队/未读提醒：短提示音 + 标题闪烁（不依赖浏览器 Notification 授权）。 */

const TITLE_BASE = '智能客服'

let flashTimer: ReturnType<typeof setInterval> | null = null
let audioCtx: AudioContext | null = null

function beep() {
  try {
    const Ctx = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext
    if (!Ctx) return
    if (!audioCtx) audioCtx = new Ctx()
    const osc = audioCtx.createOscillator()
    const gain = audioCtx.createGain()
    osc.type = 'sine'
    osc.frequency.value = 880
    gain.gain.value = 0.08
    osc.connect(gain)
    gain.connect(audioCtx.destination)
    osc.start()
    osc.stop(audioCtx.currentTime + 0.12)
  } catch {
    /* 浏览器可能拦截自动播放 */
  }
}

export function flashTitle(hint = '新消息') {
  if (flashTimer) return
  let on = false
  const origin = document.title.includes(TITLE_BASE) ? document.title : TITLE_BASE
  flashTimer = setInterval(() => {
    on = !on
    document.title = on ? `【${hint}】${TITLE_BASE}` : origin
  }, 800)
}

export function stopTitleFlash() {
  if (flashTimer) {
    clearInterval(flashTimer)
    flashTimer = null
  }
  if (document.title.includes('【')) document.title = TITLE_BASE
}

export function notifyInbox(kind: 'pending' | 'unread') {
  beep()
  flashTitle(kind === 'pending' ? '排队待接入' : '新消息')
}
