// Demo video (docs/PLAN.md 12.2, docs/product/demo.md): MP4 1920×1080, no sound, captions on screen, ~5 min.
//
// Needs the UI with a stand behind it, e.g. from web/:
//   VITE_API_PROXY=http://localhost:18000 npm run dev
//   npm run demo-video -- --base http://localhost:5173 [--out ../data/interim/demo.mp4]
// Browser: the installed Google Chrome (playwright-core, nothing is downloaded); CHROME_PATH overrides it.
// Video: CDP screencast frames → the system ffmpeg (H.264), constant 25 fps.
// The script STARTS a run of solitary-fault on the stand (×60, ~4 min of stream); the stand must be free.
// The recorded run of ppr-failures (feed, journal, dashboard) is the latest finished one; RUN_PPR_FAILURES=10 pins a run.

import { spawn } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from 'playwright-core'

const arg = (name) => {
  const i = process.argv.indexOf(name)
  return i > 0 ? process.argv[i + 1] : undefined
}

const BASE = (arg('--base') ?? process.env.DEMO_BASE ?? 'http://localhost:5173').replace(/\/$/, '')
const OUT = resolve(arg('--out') ?? fileURLToPath(new URL('../../docs/product/demo.mp4', import.meta.url)))
const FPS = 25
const W = 1920
const H = 1080

const CHANNEL = 93527 // ДД ПК432 ур.4 — the noisy detector of solitary-fault
const CHANNEL_NAME = 'ДД ПК432'

async function api(path, init) {
  const res = await fetch(`${BASE}/api/v1${path}`, init)
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status} ${await res.text()}`)
  return res.json()
}

let runs
async function runOf(scenario) {
  const pinned = process.env[`RUN_${scenario.toUpperCase().replaceAll('-', '_')}`]
  if (pinned) return Number(pinned)
  runs ??= (await api('/runs?size=100')).items
  const run = runs.find((r) => r.scenario === scenario && r.status === 'finished')
  if (!run) throw new Error(`на стенде нет завершённого прогона ${scenario} — запишите его: make e2e SCENARIO=${scenario}`)
  return run.id
}

// --- recording ------------------------------------------------------------------------------------------------------

/** CDP screencast → ffmpeg. Frames come only on repaint, so the last frame is repeated at a constant rate. */
async function startRecording(page) {
  await mkdir(dirname(OUT), { recursive: true })
  const ffmpeg = spawn(
    'ffmpeg',
    [
      '-y',
      '-loglevel',
      'error',
      '-f',
      'image2pipe',
      '-framerate',
      String(FPS),
      '-i',
      '-',
      '-vf',
      `scale=${W}:${H}:force_original_aspect_ratio=decrease,pad=${W}:${H}:(ow-iw)/2:(oh-ih)/2:color=white`,
      '-c:v',
      'libx264',
      '-preset',
      'medium',
      '-crf',
      '26',
      '-pix_fmt',
      'yuv420p',
      '-movflags',
      '+faststart',
      OUT,
    ],
    { stdio: ['pipe', 'inherit', 'inherit'] },
  )
  const cdp = await page.context().newCDPSession(page)
  let last = null
  cdp.on('Page.screencastFrame', async ({ data, sessionId }) => {
    last = Buffer.from(data, 'base64')
    await cdp.send('Page.screencastFrameAck', { sessionId }).catch(() => {})
  })
  await cdp.send('Page.startScreencast', {
    format: 'jpeg',
    quality: 88,
    maxWidth: W,
    maxHeight: H,
    everyNthFrame: 1,
  })
  let frames = 0
  const started = Date.now()
  // Wall-clock pacing: the video runs at real speed even when the page repaints rarely.
  const timer = setInterval(
    () => {
      const due = Math.floor(((Date.now() - started) / 1000) * FPS)
      while (last && frames < due) {
        ffmpeg.stdin.write(last)
        frames++
      }
    },
    1000 / FPS / 2,
  )
  return async () => {
    clearInterval(timer)
    await cdp.send('Page.stopScreencast').catch(() => {})
    ffmpeg.stdin.end()
    await new Promise((ok, fail) => ffmpeg.on('close', (code) => (code ? fail(new Error(`ffmpeg: ${code}`)) : ok())))
    return frames / FPS
  }
}

// --- on-screen helpers: captions, title cards, cursor, highlight ----------------------------------------------------

const OVERLAY = () => {
  const css = `
    #demo-caption{position:fixed;left:50%;bottom:36px;transform:translateX(-50%);max-width:1480px;z-index:2147483646;
      background:rgba(15,23,42,.9);color:#fff;font:500 25px/1.35 Inter,system-ui,sans-serif;padding:16px 26px;
      border-radius:14px;box-shadow:0 8px 30px rgba(0,0,0,.25);pointer-events:none;transition:opacity .35s}
    #demo-caption small{display:block;font-size:19px;font-weight:400;opacity:.8;margin-top:4px}
    #demo-card{position:fixed;inset:0;z-index:2147483647;background:#0f172a;color:#fff;display:flex;flex-direction:column;
      justify-content:center;padding:0 180px;font:400 30px/1.45 Inter,system-ui,sans-serif;transition:opacity .5s}
    #demo-card h1{font-size:60px;font-weight:700;margin:0 0 28px;line-height:1.15}
    #demo-card li{margin:8px 0}
    #demo-cursor{position:fixed;width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;z-index:2147483647;
      background:rgba(239,68,68,.35);border:2px solid rgba(239,68,68,.9);pointer-events:none;transition:transform .12s}
    #demo-cursor.down{transform:scale(.7)}
    .demo-spot{outline:4px solid rgba(239,68,68,.85)!important;outline-offset:4px;border-radius:8px;
      transition:outline-color .3s}`
  const mount = () => {
    if (document.getElementById('demo-style')) return
    const style = Object.assign(document.createElement('style'), {
      id: 'demo-style',
      textContent: css,
    })
    document.head.append(style)
    const cursor = Object.assign(document.createElement('div'), {
      id: 'demo-cursor',
    })
    document.body.append(cursor)
    addEventListener(
      'mousemove',
      (e) =>
        Object.assign(cursor.style, {
          left: `${e.clientX}px`,
          top: `${e.clientY}px`,
        }),
      true,
    )
    addEventListener('mousedown', () => cursor.classList.add('down'), true)
    addEventListener('mouseup', () => cursor.classList.remove('down'), true)
    const saved = sessionStorage.getItem('demo-caption')
    if (saved) window.__demoCaption(...JSON.parse(saved))
  }
  window.__demoCaption = (text, sub) => {
    sessionStorage.setItem('demo-caption', JSON.stringify([text, sub]))
    let el = document.getElementById('demo-caption')
    if (!text) return el?.remove()
    if (!el) {
      el = Object.assign(document.createElement('div'), { id: 'demo-caption' })
      document.body.append(el)
    }
    el.innerHTML = ''
    el.append(text)
    if (sub) el.append(Object.assign(document.createElement('small'), { textContent: sub }))
  }
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', mount)
  else mount()
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function helpers(page) {
  const caption = (text, sub) => page.evaluate(([t, s]) => window.__demoCaption(t, s), [text ?? '', sub ?? ''])

  async function card(title, lines, ms) {
    await page.evaluate(
      ([t, ls]) => {
        const el = Object.assign(document.createElement('div'), {
          id: 'demo-card',
        })
        el.innerHTML = `<h1></h1><ul></ul>`
        el.querySelector('h1').textContent = t
        for (const l of ls) el.querySelector('ul').append(Object.assign(document.createElement('li'), { textContent: l }))
        document.body.append(el)
      },
      [title, lines],
    )
    await sleep(ms)
    await page.evaluate(() => document.getElementById('demo-card')?.remove())
  }

  /** In-app navigation without a reload: the router follows popstate, captions and the cursor stay. */
  async function go(path) {
    await page.evaluate((p) => {
      history.pushState({}, '', p)
      dispatchEvent(new PopStateEvent('popstate'))
    }, path)
    await page.waitForTimeout(600)
    await page.waitForFunction(() => !document.querySelector('[data-slot=skeleton]'), null, { timeout: 30_000 }).catch(() => {})
  }

  /** Visible cursor travel, then click. */
  async function click(locator) {
    await locator.scrollIntoViewIfNeeded()
    const box = await locator.boundingBox()
    if (box) {
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, {
        steps: 25,
      })
      await sleep(250)
    }
    await locator.click()
  }

  async function spot(locator, ms = 2500) {
    await locator.scrollIntoViewIfNeeded()
    const box = await locator.boundingBox()
    if (box)
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, {
        steps: 20,
      })
    await locator.evaluate((el) => el.classList.add('demo-spot'))
    await sleep(ms)
    await locator.evaluate((el) => el.classList.remove('demo-spot')).catch(() => {})
  }

  return { caption, card, go, click, spot }
}

// --- the show -------------------------------------------------------------------------------------------------------

async function show(page) {
  const { caption, card, go, click, spot } = helpers(page)
  const pprRun = await runOf('ppr-failures')
  const state = await api('/emulator/state')
  if (state.state === 'running' || state.state === 'paused') {
    throw new Error(`эмулятор занят: ${state.scenario}, прогон ${state.run_id} — стенд общий, дождитесь конца`)
  }

  // 0:00 — title
  await card(
    'Прогноз отказов датчиков по потоку СМВУ',
    [
      'В момент тревоги сервис говорит, что это: плановая проверка, вероятная неисправность датчика или повод реагировать.',
      'Раз в сутки — какое оборудование откажет в ближайшие 1–30 суток; заявки на ТО заводятся сами.',
      'Данные приходят как от настоящей СМВУ — по OPC UA.',
      'ТЗ: Precision > 0.7, Recall > 0.5, горизонт ≥ 24 ч, прогноз < 5 мин.',
    ],
    11_000,
  )

  // 0:11 — emulator
  await caption(
    'Эмулятор СМВУ транслирует реальные события 2026 года по OPC UA',
    'Сервис подключён тем же адаптером, что и к настоящей СМВУ. Этих событий модели при обучении не видели',
  )
  const scenarioCard = page
    .locator('[data-slot=card]', {
      hasText: 'Одиночная тревога шумящего датчика',
    })
    .filter({ has: page.getByRole('button', { name: /Запустить · ×/ }) })
  await spot(scenarioCard, 4500)
  await click(scenarioCard.getByRole('button', { name: /Запустить/ }))
  await caption(
    'Запуск сценария «Одиночная тревога шумящего датчика» на ×60 — минута за секунду',
    'Каждый запуск — отдельный прогон, прошлые остаются',
  )
  await sleep(2500)
  await click(page.getByRole('dialog').getByRole('button', { name: 'Запустить', exact: true }))
  await page.waitForURL(/[?&]run=\d+/)
  const run = Number(new URL(page.url()).searchParams.get('run'))
  const t0 = Date.now()
  const at = (s) => sleep(Math.max(0, t0 + s * 1000 - Date.now()))

  // 0:20 — feed while the stream goes
  await caption(
    'Лента тревог. В шапке — прогон, часы прогона, целевая и фактическая скорость',
    '28 марта 2026, 05:00, объект «Тау». Ждём одиночную тревогу в 05:56 по часам прогона',
  )
  await sleep(12_000)

  // map in follow mode — catches the alarm at 05:56:40 (~57 s after start)
  await go(`/objects?run=${run}`)
  await caption('Карта объектов — живой граф: район → объекты → подобъекты → каналы')
  await sleep(4000)
  await click(page.getByRole('button', { name: 'Следить за тревогами' }).or(page.getByLabel('Следить за тревогами')).first())
  await caption('Режим «Следить»: новая тревога раскрывает подобъект, камера летит к каналу')
  await page.waitForFunction((name) => document.body.innerText.includes(name), CHANNEL_NAME, { timeout: 90_000 }).catch(() => {})
  await at(58)
  await caption(
    '05:56 — сработал «ДД ПК432 ур.4», один на весь объект',
    'Соседи молчат. За три минуты датчик пять раз переключится между «Обнаружен дым» и «Дыма нет»',
  )
  await sleep(12_000)

  // feed: verdict and work order
  await go(`/?run=${run}`)
  const row = page.locator('tbody tr', { hasText: CHANNEL_NAME }).first()
  await row.waitFor({ timeout: 60_000 })
  await page
    .locator('tbody tr', { hasText: 'Неисправность' })
    .first()
    .waitFor({ timeout: 60_000 })
    .catch(() => {})
  await caption(
    'Модель оценила тревогу за секунды: выше порога — «вероятна неисправность датчика»',
    'Одно решение на канал в сутки: следующие переключения наследуют его',
  )
  await spot(row, 3000)
  await click(row)
  await sleep(2500)
  const panel = page.locator('[data-slot=sheet-content]').first()
  await caption('Оценка против порога, факторы решения и заявка на проверку датчика — создана автоматически')
  await spot(panel.getByText('Заявка').first(), 3500)
  await sleep(4000)

  // work order
  await click(panel.getByRole('link', { name: /№\d+/ }).first())
  await caption(
    'Заявка на ТО: основание — прогноз модели, приоритет высокий, срок — сутки',
    'Одна открытая заявка на канал и правило; есть и правила без ML: одиночные тревоги, долгая неисправность',
  )
  await sleep(11_000)

  // channel card
  await go(`/channels/${CHANNEL}?run=${run}`)
  await caption(
    'Карточка канала: состояние, лента состояний, прогнозы с исходами',
    'Почему модель так решила — факторы: соседи молчат, история канала и шлейфа',
  )
  await sleep(5000)
  const factors = page.getByText(/Факторы/).first()
  if (await factors.count()) await spot(factors, 4000)
  await page.mouse.wheel(0, 500)
  await sleep(9000)

  // work orders of the run — while the stream goes towards 08:01
  await go(`/work-orders?run=${run}`)
  await caption(
    'Заявки прогона: по модели и по правилам без ML',
    'Повторные одиночные тревоги, неисправность дольше 72 ч — заявки заводятся и без модели',
  )
  await sleep(12_000)

  // the control panel: the run goes on, effective speed
  // a full load: the panel shows the sent-events counter fresh (the stream updates only the clock)
  await page.goto(`${BASE}/emulator?run=${run}`, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => !document.querySelector('[data-slot=skeleton]'), null, { timeout: 30_000 })
  await caption(
    'Пульт: прогон идёт, фактическая скорость ×60',
    'Во всплесках время замедляется, события не теряются; прогон можно поставить на паузу',
  )
  await sleep(10_000)

  // journal: the outcome closes live at 08:01:55 (~3 min after start)
  await go(`/predictions?run=${run}`)
  await caption('Журнал прогнозов: каждый прогноз сверяется с фактом', 'Прогноз по «ДД ПК432 ур.4» ждёт исхода — горизонт 24 ч от тревоги')
  await sleep(15_000)
  await caption('Метрики считаются только по закрытым прогнозам', 'Ждём 08:01 по часам прогона — через два часа после тревоги')
  await page.getByText('Сбылся').first().waitFor({ timeout: 120_000 })
  await caption(
    'В 08:01 датчик ушёл в «Неисправен» — с ближайшим тактом у прогноза исход «Сбылся», упреждение 2 часа',
    'Исходы закрываются раз в 15 с — при ×60 это до четверти часа по часам прогона',
  )
  await spot(page.getByText('Сбылся').first(), 4000)
  await sleep(5000)

  // recorded run ppr-failures
  await go(`/?run=${pprRun}&ppr_alert=1`)
  await caption(
    'Записанный прогон: плановая проверка на объекте «Тау», 3 февраля 2026',
    'Сервис узнаёт проверку — лента не требует реакции на каждую тревогу',
  )
  await sleep(6000)
  await caption(
    'Внутри проверки модель отмечает датчики, которые вероятно не пройдут её',
    '50 отметок — заявки созданы; вердикт остаётся «плановая проверка»',
  )
  await sleep(8000)
  await go(`/predictions?run=${pprRun}`)
  await caption('Журнал прогона: 47 из 50 отмеченных датчиков ушли в «Неисправен»', 'Живая Precision — не ниже 94%')
  const precision = page.getByText(/не ниже/).first()
  if (await precision.count()) await spot(precision, 5000)
  await sleep(6000)

  // dashboard of the recorded ppr-failures run: the daily forecast is made at the run start
  await go(`/dashboard?run=${pprRun}`)
  await caption(
    'Дашборд: ежедневный прогноз — топ каналов по риску отказа на 1–30 суток',
    'Заявки по ежедневной модели — не больше 20 в сутки, лучшие по оценке',
  )
  await sleep(14_000)

  // model page
  await go(`/models?run=${pprRun}`)
  await caption('Модели: версии, пороги, офлайн-метрики на I полугодии 2026 против порогов ТЗ')
  await sleep(4000)
  const quality = page.getByText('Качество: офлайн и живое').first()
  if (await quality.count()) {
    await quality.scrollIntoViewIfNeeded()
    await page.evaluate(() => window.scrollBy(0, 360))
  }
  await sleep(9000)
  await caption('')

  // outro
  await card(
    'Итог',
    [
      'Событийная модель: P 85.0%, R 81.6% — триаж тревоги, упреждение — минуты.',
      'Ежедневная модель: P 77.7%, R 52.2%, медиана упреждения 8.4 суток — насосы, вентиляторы, фазы.',
      'Проверка — I полугодие 2026, обучение на 2022–2024. Каналы без истории модель не распознаёт — их закрывают правила заявок.',
      'OPC UA → шлюз → рабочая база и архив → ML-сервис → лента, карта, журнал, заявки.',
    ],
    12_000,
  )
  return run
}

async function main() {
  const browser = await chromium.launch(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' })
  const context = await browser.newContext({
    viewport: { width: W, height: H },
    colorScheme: 'light',
  })
  await context.addInitScript(() => localStorage.setItem('lct-theme', 'light'))
  await context.addInitScript(OVERLAY)
  const page = await context.newPage()
  page.on('pageerror', (e) => console.error(`pageerror: ${e.message}`))
  await page.goto(`${BASE}/emulator`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('main')
  await page.waitForFunction(() => !document.querySelector('[data-slot=skeleton]'), null, { timeout: 30_000 })
  const stop = await startRecording(page)
  const t = Date.now()
  let run
  try {
    run = await show(page)
  } finally {
    const seconds = await stop()
    await browser.close()
    console.log(`${OUT}: ${seconds.toFixed(0)} с видео за ${((Date.now() - t) / 1000).toFixed(0)} с${run ? `, прогон ${run}` : ''}`)
  }
}

await main()
