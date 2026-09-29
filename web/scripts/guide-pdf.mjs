// Site guide PDF: docs/product/site-guide.html → site-guide.pdf, A4 with the page's own @page margins.
//   npm run guide-pdf
// Screens come from docs/product/screens/ (npm run screens). Browser: the installed Google Chrome; CHROME_PATH overrides it.

import { fileURLToPath } from 'node:url'

import { chromium } from 'playwright-core'

const HTML = new URL('../../docs/product/site-guide.html', import.meta.url)
const PDF = fileURLToPath(new URL('../../docs/product/site-guide.pdf', import.meta.url))

const browser = await chromium.launch(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : { channel: 'chrome' })
const page = await browser.newPage()
await page.goto(HTML.href, { waitUntil: 'load' })
await page.pdf({ path: PDF, format: 'A4', printBackground: true, preferCSSPageSize: true })
await browser.close()
console.log(PDF)
