import { Link } from 'react-router-dom'

import { VectorMark } from '../../components'
import { publicPaths } from '../../app/routes/paths'
import styles from './landing.module.css'

/**
 * The ruler across the top: 24 marks, every fourth one twice as tall.
 *
 * A count rather than a measurement, which is why it lives here and not in
 * the stylesheet -- the ticks flex to whatever width the viewport is, so the
 * only fixed thing about the ruler is how many of them there are. Ids rather
 * than array indices as keys, because nothing about this list is ever
 * reordered but a lint rule should not have to know that.
 */
const TICKS = Array.from({ length: 24 }, (_, index) => ({
  id: `tick-${String(index)}`,
  major: index % 4 === 0,
}))

/**
 * Vector's public front door.
 *
 * The mark, the wordmark, one line, and the two ways in. That is the whole
 * screen, and the emptiness is the design rather than a gap in it: the nine
 * feature cards and the second call to action that used to sit below the fold
 * were cut when this direction was approved. A visitor who has arrived at a
 * tracker's front page already knows what a tracker is; what they are looking
 * for is the sign-in button, and every section between them and it was a
 * section they had to scroll past.
 *
 * The only page in the product a visitor with no session is expected to see,
 * so it is deliberately standalone: no application shell, no sidebar, no
 * workspace. It renders its own `<main>` and its own `<h1>`. There is no skip
 * link because there is nothing to skip -- `AppLayout` owns that control, and
 * it owns it because it is the screen with navigation in front of the
 * content.
 *
 * Motion is limited to the hover and focus transitions in the stylesheet,
 * which `base.css` already collapses under `prefers-reduced-motion: reduce`.
 * No scroll animation, no parallax, no autoplay -- the things that setting
 * exists to switch off are simply not here, which is a stronger guarantee
 * than an override.
 */
export function LandingPage() {
  return (
    <div className={styles.page}>
      {/*
       * The ruler, and the grid behind it, say the same thing the product
       * says: this is an instrument. Neither has anything to announce, so the
       * grid is drawn in pseudo-elements (never in the accessibility tree at
       * all) and the ruler -- which needs real boxes to flex -- is hidden.
       */}
      <div aria-hidden="true" className={styles.ruler}>
        {TICKS.map((tick) => (
          <span
            className={tick.major ? styles.tickMajor : styles.tick}
            key={tick.id}
          />
        ))}
      </div>

      <main className={styles.entry}>
        <div className={styles.identity}>
          {/*
           * The mark goes home. A self-link on the page it points at is a
           * little odd, but it is the affordance every visitor already has a
           * habit for, and it is the one control on this screen whose name is
           * not its visible text -- the glyph is decorative, so `aria-label`
           * is the whole accessible name.
           */}
          <Link
            aria-label="Vector home"
            className={styles.mark}
            to={publicPaths.landing()}
          >
            <span className={styles.markRule} />
            <VectorMark className={styles.markGlyph} />
            <span className={styles.markRule} />
          </Link>

          <h1 className={styles.wordmark}>Vector</h1>

          <p className={styles.tagline}>Issue tracking, precisely</p>
        </div>

        <div className={styles.ways}>
          <nav aria-label="Account" className={styles.doors}>
            {/*
             * Anchors dressed as buttons, not `<Button>`s with an onClick.
             * Navigation belongs to a link: it is what middle-click, "open in
             * new tab" and a screen reader's link list all rely on.
             */}
            <Link className={styles.primaryCta} to={publicPaths.login()}>
              Sign in
            </Link>
            <Link className={styles.secondaryCta} to={publicPaths.register()}>
              Create account
            </Link>
          </nav>

          {/*
           * A readout, not a tagline: the machine reporting its own state.
           *
           * ponytail: the state is a constant. Nothing on this page asks the
           * server whether it is true, so this is a claim rather than a
           * measurement, and the day it is wrong is the day it matters most.
           * The upgrade is one fetch of the REST health endpoint behind a
           * three-state readout (operational / degraded / unreachable); it is
           * not here because a signed-out page that pings the API on every
           * visit is a decision worth making deliberately rather than as a
           * side effect of styling the front door.
           */}
          <p className={styles.readout}>
            <span aria-hidden="true" className={styles.readoutDot} />
            All systems operational
          </p>
        </div>
      </main>
    </div>
  )
}
