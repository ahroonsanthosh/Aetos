import { chromium } from 'playwright'
const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium' })
const page = await browser.newPage({ viewport: { width: 1600, height: 900 } })
const errors = []
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()) })
page.on('pageerror', (e) => errors.push(String(e)))
await page.goto('http://127.0.0.1:8300/', { waitUntil: 'networkidle' })
await page.waitForTimeout(7000)
console.log('MODE    :', await page.textContent('.mode').catch(() => null))
console.log('CHIPS   :', JSON.stringify(await page.$$eval('.chip', (e) => e.map((x) => x.textContent.trim()))))
console.log('MARKERS :', await page.$$eval('.track-marker', (e) => e.length))
console.log('ROWS    :', await page.$$eval('.track-list tbody tr', (e) => e.length))
console.log('ERRORS  :', errors.length ? errors : 'none')
await page.screenshot({ path: '/tmp/aetos-single.png' })
// Hard refresh on a client-side route: the server must return the shell, not a 404.
await page.goto('http://127.0.0.1:8300/health', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
console.log('DEEP LINK /health rows:', await page.$$eval('.health__row', (e) => e.length))
await browser.close()
