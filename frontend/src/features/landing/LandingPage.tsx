import { Link } from 'react-router-dom'
import type { ReactNode } from 'react'

import {
  CommentIcon,
  CycleIcon,
  IssuesIcon,
  Kbd,
  LabelIcon,
  ProjectIcon,
  RelationIcon,
  SearchIcon,
  SubIssueIcon,
  TeamIcon,
  VectorMark,
} from '../../components'
import { publicPaths } from '../../app/routes/paths'
import styles from './landing.module.css'

interface Capability {
  icon: ReactNode
  title: string
  body: string
}

/**
 * What Vector does, as the product actually does it.
 *
 * Every entry describes a real capability of this codebase -- the schema has
 * the type, the migration has the table. Nothing here is aspirational, and
 * there is no invented customer, metric or testimonial anywhere on this page:
 * a landing page for a product that does not ship yet can be honest and still
 * be useful, and the version that is not honest is the one somebody has to
 * quietly delete later.
 */
const CAPABILITIES: readonly Capability[] = [
  {
    icon: <IssuesIcon />,
    title: 'Issues that carry state',
    body: 'Every piece of work is an issue with a status, a priority and a place in the list. Statuses belong to a team, and each one sits in a fixed category -- backlog, unstarted, started, completed, canceled -- so a team can rename its board without breaking anything that reads it.',
  },
  {
    icon: <ProjectIcon />,
    title: 'Projects and milestones',
    body: 'Group issues into a project with a lead, a target date and a state. Milestones divide it into the checkpoints the team actually talks about, and each issue takes its place against one.',
  },
  {
    icon: <CycleIcon />,
    title: 'Cycles',
    body: 'Time-boxed iterations per team, with a database constraint that stops two cycles from overlapping. What is in the current cycle is a question with one answer.',
  },
  {
    icon: <SubIssueIcon />,
    title: 'Sub-issues',
    body: 'Break a large issue into the smaller ones it is really made of. Parents track their children, so progress on the whole is the sum of progress on the parts rather than a number somebody maintains by hand.',
  },
  {
    icon: <RelationIcon />,
    title: 'Relations',
    body: 'Mark an issue as blocking, blocked by, related to or a duplicate of another. Blocking relationships are the ones worth seeing before a cycle starts, not after it stalls.',
  },
  {
    icon: <LabelIcon />,
    title: 'Labels',
    body: 'Cross-cutting tags that do not care which team, project or cycle an issue belongs to. Filter by them, and combine them with anything else.',
  },
  {
    icon: <CommentIcon />,
    title: 'Comments',
    body: 'The discussion lives on the issue, where the decision it produced can still be found six months later.',
  },
  {
    icon: <TeamIcon />,
    title: 'Teams and workspaces',
    body: 'Each team owns its issue prefix -- the ENG in ENG-42 -- and its own workflow states. Workspaces keep organisations apart, with membership and roles enforced on the server rather than hidden in the interface.',
  },
  {
    icon: <SearchIcon />,
    title: 'Search and the command palette',
    body: 'Find an issue by what you remember of it. The command palette puts navigation and the actions you repeat all day one keystroke away, so the mouse is optional.',
  },
]

/**
 * Vector's public front door.
 *
 * The only page in the product a visitor with no session is expected to
 * spend time on, so it is deliberately standalone: no application shell, no
 * sidebar, no workspace. It renders its own header, `<main>` and `<h1>`.
 *
 * Motion is limited to hover and focus transitions, which `base.css` already
 * collapses under `prefers-reduced-motion: reduce`. There is no scroll
 * animation, no parallax and no autoplaying anything -- the things that
 * setting exists to switch off are simply not here, which is a stronger
 * guarantee than an override.
 */
export function LandingPage() {

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Link to={publicPaths.landing()} className={styles.brand} aria-label="Vector home">
          <VectorMark className={styles.brandMark} />
          <span>Vector</span>
        </Link>

        <nav className={styles.headerActions} aria-label="Account">
          <Link to={publicPaths.login()} className={styles.headerLink}>
            Sign in
          </Link>
          {/* A `<Link>` styled as a button, not a `<Button>` with an onClick.
              Navigation belongs to an anchor: it is what middle-click, "open
              in new tab" and a screen reader's link list all rely on. */}
          <Link to={publicPaths.register()} className={styles.primaryCta}>
            Get started
          </Link>
        </nav>
      </header>

      <main>
        <section className={styles.hero}>
          <p className={styles.eyebrow}>Issue tracking for software teams</p>

          <h1 className={styles.heroTitle}>
            The issue tracker that keeps up with the work
          </h1>

          <p className={styles.heroLead}>
            Vector is a fast, keyboard-first tracker for teams who ship. Issues,
            projects, cycles and relations in one model, so planning a release
            and working through it are the same list rather than two systems
            that disagree.
          </p>

          <div className={styles.heroActions}>
            <Link to={publicPaths.register()} className={styles.primaryCta}>
              Get started
            </Link>
            <Link to={publicPaths.login()} className={styles.secondaryCta}>
              Sign in
            </Link>
          </div>

          <p className={styles.heroNote}>
            Press <Kbd>⌘</Kbd> <Kbd>K</Kbd> anywhere in Vector for the command
            palette.
          </p>
        </section>

        <section className={styles.capabilities} aria-labelledby="capabilities-heading">
          <h2 id="capabilities-heading" className={styles.sectionTitle}>
            What is in the box
          </h2>

          <ul className={styles.grid} role="list">
            {CAPABILITIES.map((capability) => (
              <li key={capability.title} className={styles.capability}>
                <span className={styles.capabilityIcon}>{capability.icon}</span>
                <h3 className={styles.capabilityTitle}>{capability.title}</h3>
                <p className={styles.capabilityBody}>{capability.body}</p>
              </li>
            ))}
          </ul>
        </section>

        <section className={styles.closing}>
          <h2 className={styles.sectionTitle}>Start with one issue</h2>
          <p className={styles.closingLead}>
            Create a workspace, invite your team, and file the first thing that
            is bothering you. Everything else follows from there.
          </p>
          <Link to={publicPaths.register()} className={styles.primaryCta}>
            Create your account
          </Link>
        </section>
      </main>

      <footer className={styles.footer}>
        <p>Vector — project management for software teams.</p>
      </footer>
    </div>
  )
}
