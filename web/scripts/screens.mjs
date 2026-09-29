// Presentation screenshots (version 2, docs/product/presentation.md): PNG 1920×1080, light theme, into docs/product/screens/.
//
// Needs the UI with the integration stand behind it, e.g. from web/:
//   VITE_API_PROXY=http://localhost:18000 npm run dev
//   npm run screens -- --base http://localhost:5173 [--only feed-journal,models] [--scale 2]
// or straight against the stand's web: npm run screens -- --base http://localhost:18080
// Browser: the installed Google Chrome (playwright-core, nothing is downloaded); CHROME_PATH overrides it.
// Runs are looked up by scenario — the latest finished one; RUN_<SCENARIO> (e.g. RUN_PPR_FAILURES=10) pins a run.
// Nothing is started on the stand: the script only reads.

import { mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

import { chromium } from 'playwright-core'

const arg = (name) => {
  const i = process.argv.indexOf(name)
  return i > 0 ? process.argv[i + 1] : undefined
}

const BASE = (arg('--base') ?? process.env.SCREENS_BASE ?? 'http://localhost:5173').replace(/\/$/, '')
const ONLY = arg('--only')?.split(',')
const SCALE = Number(arg('--scale') ?? 1)
const OUT = fileURLToPath(new URL('../../docs/product/screens/', import.meta.url))

const SUB_OBJECT = 3356 // ПС объект Тау — the sub-object of solitary-fault and ppr-failures

/** A screen cannot be taken on this stand (no such run) — skipped, not failed. */
class Skip extends Error {}

async function api(path) {
  const res = await fetch(`${BASE}/api/v1${path}`)
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`)
  return res.json()
}

let runs
async function runOf(scenario) {
  const pinned = process.env[`RUN_${scenario.toUpperCase().replaceAll('-', '_')}`]
  if (pinned) return Number(pinned)
  runs ??= (await api('/runs?size=100')).items
  // Newest first: the latest finished recording of the scenario.
  const run = runs.find((r) => r.scenario === scenario && r.status === 'finished')
  if (!run) throw new Skip(`на стенде нет завершённого прогона ${scenario}`)
  return run.id
}

/** The newest finished run of the scenarios (in order) that has daily forecasts; else the first scenario's run. */
async function runWithDaily(...scenarios) {
  const pinned = scenarios.map((s) => process.env[`RUN_${s.toUpperCase().replaceAll('-', '_')}`]).find(Boolean)
  if (pinned) return Number(pinned)
  runs ??= (await api('/runs?size=100')).items
  for (const scenario of scenarios)
    for (const r of runs.filter((x) => x.scenario === scenario && x.status === 'finished')) {
      const { total } = await api(`/predictions?run=${r.id}&model=daily&size=1`)
      if (total > 0) return r.id
    }
  return runOf(scenarios[0])
}

/** Id of the first item of an API list; no items — the screen is skipped. */
async function firstId(path, what) {
  const { items } = await api(path)
  if (!items.length) throw new Skip(`нет ${what}: ${path}`)
  return items[0].id
}

// The organizers' journal of 01.08.2026 uploaded through the UI (13.1) — its preview is the «own journal» screen
const UPLOADED_SAMPLE = process.env.UPLOADED_SAMPLE ?? 'upload-20260927-84b954'

/** Screens in presentation order (docs/product/presentation.md, version 2). `url` looks ids up in the API;
 * `prepare` runs once the data is on screen; `clip` — the part of the 1920×1080 page to keep. */
const SHOTS = [
  {
    name: 'feed-journal',
    title: 'Лента прогона журнала организаторов 01.08.2026: тревоги и эпизоды оборудования',
    url: async () => `/?run=${await runOf('sample-2026-08-01')}`,
  },
  {
    name: 'dashboard',
    title: 'Дашборд',
    url: async () => `/dashboard?run=${await runWithDaily('ppr-failures', 'subobject-week')}`,
  },
  {
    name: 'graph-subobject',
    title: 'Карта объектов: раскрытый подобъект',
    url: async () => `/objects/${SUB_OBJECT}?run=${await runOf('ppr-failures')}`,
    settle: 8000,
  },
  {
    name: 'journal',
    title: 'Журнал прогнозов: кто решил — v3 или v0 за неё',
    url: async () => `/predictions?run=${await runOf('ppr-failures')}`,
  },
  {
    name: 'work-orders',
    title: 'Заявки на ТО с подобъектом',
    url: async () => `/work-orders?run=${await runOf('sample-2026-08-01')}`,
  },
  {
    name: 'upload',
    title: 'Загрузка своего журнала',
    url: async () => '/emulator/upload',
    clip: { x: 680, y: 56, width: 820, height: 490 }, // only the form: the rest of the page is empty
  },
  {
    name: 'scenario-preview',
    title: 'Просмотр сценария до запуска: журнал, загруженный диспетчером',
    url: async () => `/emulator/scenarios/${UPLOADED_SAMPLE}`,
  },
  {
    name: 'feed-journal-alarm',
    title: 'Одиночная дымовая тревога журнала 01.08.2026 — решила v0 за пропустившую v3',
    url: async () => {
      const run = await runOf('sample-2026-08-01')
      const alarm = await firstId(`/alarms?run=${run}&verdict=sensor_fault&size=1`, 'тревоги «неисправность датчика»')
      return `/?run=${run}&alarm=${alarm}`
    },
  },
  {
    name: 'access-panel',
    title: 'Вскрытие под охраной: строка ленты и панель инцидента',
    url: async () => {
      const run = await runOf('guard-late-disarm')
      const { items } = await api(`/access-incidents?run=${run}&size=50`)
      const own = items.find((i) => i.run_id === run) ?? items[0]
      if (!own) throw new Skip(`в прогоне ${run} нет инцидентов`)
      return `/?run=${run}&kind=access&access=${own.id}`
    },
  },
  {
    name: 'feed-disagree',
    title: 'Лента: оценки двух моделей, где они расходятся',
    url: async () => {
      const run = await runOf('ppr-failures')
      const alarm = await firstId(`/alarms?run=${run}&disagree=true&size=1`, 'тревоги с расхождением моделей')
      return `/?run=${run}&disagree=true&alarm=${alarm}`
    },
  },
  {
    name: 'models',
    title: 'Модели: v0, v3 и связка рядом',
    url: async () => `/models?run=${await runOf('ppr-failures')}`,
    clip: { x: 440, y: 630, width: 1300, height: 310 }, // the three event model cards
  },
  {
    name: 'dashboard-pdm',
    title: 'Дашборд прогона Azure PdM: модели набора и живые P / R по разметке',
    url: async () => `/dashboard?run=${await runOf('ext-pdm-week')}`,
  },
  {
    name: 'blind-spot',
    title: 'Слепая зона модели: у объекта нет истории исходов',
    url: async () => {
      const run = await runOf('new-object')
      const { sim_start: from } = await api(`/runs/${run}`)
      for (let page = 1; page <= 20; page++) {
        const { items } = await api(`/alarms?run=${run}&from=${encodeURIComponent(from)}&page=${page}&size=50`)
        if (!items.length) break
        if (items[0].prediction?.object_history === false)
          return `/?run=${run}${page > 1 ? `&page=${page}` : ''}&alarm=${items[0].id}`
      }
      throw new Skip(`в прогоне ${run} нет страницы ленты с тревогами без истории объекта`)
    },
  },
  // Only in the site guide (docs/product/site-guide.html) — after the deck's screens, so their numbers stay
  {
    name: 'channel-card',
    title: 'Карточка канала: датчик одиночной тревоги журнала 01.08.2026',
    url: async () => {
      const run = await runOf('sample-2026-08-01')
      const { items } = await api(`/alarms?run=${run}&verdict=sensor_fault&size=1`)
      if (!items.length) throw new Skip(`в прогоне ${run} нет тревоги «неисправность датчика»`)
      return `/channels/${items[0].channel_id}?run=${run}`
    },
  },
  {
    name: 'emulator',
    title: 'Эмулятор СМВУ: пульт, сценарии, внешние данные',
    url: async () => '/emulator',
  },
]

async function main() {
  await mkdir(OUT, { recursive: true })
  const browser = await chromium.launch(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' })
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: SCALE, colorScheme: 'light' })
  await context.addInitScript(() => localStorage.setItem('lct-theme', 'light'))

  const shots = ONLY ? SHOTS.filter((s) => ONLY.includes(s.name)) : SHOTS
  let failed = 0
  for (const shot of shots) {
    const file = `${String(SHOTS.indexOf(shot) + 1).padStart(2, '0')}-${shot.name}.png`
    const page = await context.newPage()
    const errors = []
    page.on('pageerror', (e) => errors.push(e.message))
    page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
    const t0 = Date.now()
    try {
      const url = await shot.url()
      // The page keeps an SSE stream open, so «network idle» never comes: wait for the data instead.
      await page.goto(BASE + url, { waitUntil: 'domcontentloaded' })
      await page.waitForSelector('main')
      await page.waitForTimeout(1500)
      await page.waitForFunction(() => !document.querySelector('[data-slot=skeleton]'), null, { timeout: 20_000 })
      await page.waitForTimeout(shot.settle ?? 2000)
      if (shot.prepare) {
        await shot.prepare(page)
        await page.waitForTimeout(800)
      }
      await page.screenshot({ path: OUT + file, ...(shot.clip ? { clip: shot.clip } : {}) })
      const note = errors.length ? `\n  console: ${errors.join(' | ')}` : ''
      console.log(`${file}  ${((Date.now() - t0) / 1000).toFixed(1)} s  ${url}  ${shot.title}${note}`)
    } catch (e) {
      if (e instanceof Skip) console.log(`${file}  пропущен: ${e.message}`)
      else {
        failed++
        console.error(`${file}  FAILED: ${e.message}`)
      }
    } finally {
      await page.close()
    }
  }
  await browser.close()
  if (failed) process.exit(1)
}

await main()
