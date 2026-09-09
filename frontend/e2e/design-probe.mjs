/**
 * Read the design the browser actually paints, not the one the CSS declares.
 *
 * Run with the local stack up:
 *   node e2e/design-probe.mjs
 *
 * This is a probe rather than a test: it prints computed styles so a human
 * can compare them to the approved design files, and it asserts only the two
 * things the design brief states outright -- the public site is light and the
 * authenticated app is dark navy. Everything finer than that (surface
 * hierarchy, spacing rhythm, which grey a border is) is a judgement a
 * screenshot comparison answers and a script does not.
 *
 * Computed styles rather than the stylesheet, because a token defined and
 * never applied looks identical to one applied everywhere when you grep for
 * it -- and that exact mistake is why this file exists.
 */

import { chromium } from '@playwright/test'

const BASE = process.env.VECTOR_BASE_URL ?? 'http://localhost:5173'
const EMAIL = 'demo@vector.local'
const PASSWORD = 'vector-local-demo'

/** sRGB relative luminance, for deciding "is this light or dark" honestly. */
function luminance(rgb) {
  const [r, g, b] = rgb.match(/\d+/g).slice(0, 3).map(Number)
  const channel = (c) => {
    const s = c / 255
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

async function probe(page, label, url) {
  await page.goto(url, { waitUntil: 'networkidle' })

  const style = await page.evaluate(() => {
    const body = getComputedStyle(document.body)
    const root = getComputedStyle(document.documentElement)
    const h1 = document.querySelector('h1')
    // Found by the SHAPE of its text, not by class name: CSS Modules hash
    // every class, so `[class*="identifier"]` matches nothing in a built
    // app and silently reports the body font instead -- which reads as "the
    // mono face was never applied" when it was applied all along.
    const mono =
      [...document.querySelectorAll('span, code, kbd')].find((el) =>
        /^[A-Z][A-Z0-9]*-\d+$/.test((el.textContent ?? '').trim()),
      ) ?? null
    return {
      background: body.backgroundColor,
      color: body.color,
      font: body.fontFamily,
      headingFont: h1 ? getComputedStyle(h1).fontFamily : '(no h1)',
      monoFont: mono ? getComputedStyle(mono).fontFamily : '(none found)',
      accent: root.getPropertyValue('--color-text-accent').trim() || '(unset)',
      appBg: root.getPropertyValue('--color-bg-app').trim() || '(unset)',
      title: document.title,
    }
  })

  const lum = luminance(style.background)
  console.log(`\n=== ${label} — ${url} ===`)
  console.log(`  background   ${style.background}   luminance ${lum.toFixed(3)} -> ${lum > 0.5 ? 'LIGHT' : 'DARK'}`)
  console.log(`  text         ${style.color}`)
  console.log(`  body font    ${style.font}`)
  console.log(`  heading font ${style.headingFont}`)
  console.log(`  mono font    ${style.monoFont}`)
  console.log(`  --color-bg-app      ${style.appBg}`)
  console.log(`  --color-text-accent ${style.accent}`)

  return { ...style, luminance: lum }
}

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const results = {}

results.landing = await probe(page, 'Landing (public)', `${BASE}/`)
results.login = await probe(page, 'Login (public)', `${BASE}/login`)

// Sign in through the real form, so the authenticated probe reads the app a
// user actually gets rather than a route rendered without a session.
await page.goto(`${BASE}/login`, { waitUntil: 'networkidle' })
await page.getByLabel('Email').fill(EMAIL)
await page.getByLabel('Password').fill(PASSWORD)
await page.getByRole('button', { name: 'Sign in' }).click()
await page.waitForURL((url) => !url.pathname.startsWith('/login'), { timeout: 20000 })

console.log(`\n  signed in, landed on ${new URL(page.url()).pathname}`)

results.app = await probe(page, 'Authenticated app', page.url())

const rogue = await page.evaluate(() => {
  // Colours from the Organic direction that the brief says must not leak
  // into the final application. Checked against what is painted, since a
  // stale stylesheet that nothing imports would not show up here.
  const organic = ['rgb(233, 231, 226)', 'rgb(224, 122, 95)', 'rgb(244, 241, 235)']
  const hits = []
  for (const el of document.querySelectorAll('*')) {
    const s = getComputedStyle(el)
    for (const value of [s.backgroundColor, s.color, s.borderColor]) {
      if (organic.includes(value)) hits.push(`${el.tagName}.${el.className} -> ${value}`)
    }
  }
  return [...new Set(hits)].slice(0, 10)
})

console.log(`\n=== Organic (obsolete) palette leakage ===`)
console.log(rogue.length === 0 ? '  none found' : rogue.map((r) => `  ${r}`).join('\n'))

await browser.close()

const landingLight = results.landing.luminance > 0.5
const appDark = results.app.luminance < 0.5

console.log('\n=== VERDICT ===')
console.log(`  Landing is LIGHT              ${landingLight ? 'PASS' : 'FAIL'}`)
console.log(`  Authenticated app is DARK     ${appDark ? 'PASS' : 'FAIL'}`)
console.log(`  Space Grotesk in UI           ${results.app.font.includes('Space Grotesk') ? 'PASS' : 'FAIL'}`)
console.log(`  IBM Plex Mono present         ${results.app.monoFont.includes('IBM Plex Mono') ? 'PASS' : 'CHECK'}`)
console.log(`  No Organic leakage            ${rogue.length === 0 ? 'PASS' : 'FAIL'}`)

process.exit(landingLight && appDark ? 0 : 1)
