import { Fragment } from 'react'
import type { ReactNode } from 'react'

import type { ContentNode } from '../lib/content'
import { safeHref } from '../lib/content'
import styles from '../documents.module.css'

/**
 * A stored document, drawn.
 *
 * ## Why this exists rather than `dangerouslySetInnerHTML`
 *
 * Because there is no HTML to set. `migrations/023_documents.sql` opens with
 * the argument: a `body TEXT` column holding rendered HTML is a stored
 * `<script>` aimed at everyone in the workspace, and sanitising on the way in
 * is a blocklist that has to agree with every browser's parser forever. The
 * column is JSONB holding a closed vocabulary of node types instead, and a
 * client BUILDS the rendering from it. This is that build. There is no path
 * here by which a value from the database becomes markup: every element below
 * is a React element this function created, and every user value lands as a
 * text child, which React escapes.
 *
 * The one attribute that carries a user value is a link's `href`, which is
 * checked by `safeHref` -- see the note there for why it is checked again
 * here when the server already refused anything but http, https and mailto.
 *
 * ## Unknown nodes are shown, not dropped
 *
 * A node type this renderer does not know means the server's vocabulary has
 * grown ahead of this client. Rendering nothing would make a document
 * silently shorter, which for a spec is a paragraph that quietly disappeared.
 * It renders a marker saying something is there instead.
 */
export interface DocumentBodyProps {
  /** The document's top-level blocks, from `readDocument`. */
  nodes: readonly ContentNode[]
}

export function DocumentBody({ nodes }: DocumentBodyProps) {
  return <div className={styles.prose}>{renderNodes(nodes)}</div>
}

function renderNodes(nodes: readonly ContentNode[]): ReactNode {
  return nodes.map((node, index) => (
    // Index keys: these nodes have no identity of their own -- the tree is
    // stored as a whole and replaced as a whole -- and the list is never
    // reordered in place, only re-rendered from a new document.
    <Fragment key={index}>{renderNode(node)}</Fragment>
  ))
}

function renderNode(node: ContentNode): ReactNode {
  const children = node.content ?? []

  switch (node.type) {
    case 'text':
      return renderText(node)

    case 'paragraph':
      return <p>{renderNodes(children)}</p>

    case 'heading':
      return renderHeading(node, children)

    case 'bulletList':
      return <ul className={styles.proseList}>{renderNodes(children)}</ul>

    case 'orderedList':
      return (
        <ol
          className={styles.proseList}
          // `start` is the vocabulary's one list attribute, and it is what a
          // numbered list continued after a quote needs.
          start={typeof node.attrs?.['start'] === 'number' ? node.attrs['start'] : undefined}
        >
          {renderNodes(children)}
        </ol>
      )

    case 'listItem':
      return <li>{renderNodes(children)}</li>

    case 'taskList':
      // `role="list"` is restated because `list-style: none` removes list
      // semantics in Safari -- the same WebKit behaviour `components/List`
      // documents.
      return (
        <ul className={styles.taskList} role="list">
          {renderNodes(children)}
        </ul>
      )

    case 'taskItem':
      return (
        <li className={styles.taskItem}>
          {/*
            A real disabled checkbox rather than a glyph. This is a rendering
            of somebody's document, not a control -- there is no mutation that
            ticks one box, only `documentEdit` replacing the whole tree -- and
            a disabled checkbox is what announces "checkbox, checked" to a
            screen reader without offering an interaction that does nothing.
          */}
          <input checked={node.attrs?.['checked'] === true} disabled readOnly type="checkbox" />
          <span>{renderNodes(children)}</span>
        </li>
      )

    case 'blockquote':
      return <blockquote className={styles.quote}>{renderNodes(children)}</blockquote>

    case 'codeBlock':
      return (
        <pre className={styles.code}>
          <code>{renderNodes(children)}</code>
        </pre>
      )

    case 'horizontalRule':
      return <hr className={styles.rule} />

    case 'hardBreak':
      return <br />

    default:
      return (
        <p className={styles.unknownNode}>
          This document contains something Vector cannot display yet.
        </p>
      )
  }
}

function renderHeading(node: ContentNode, children: readonly ContentNode[]): ReactNode {
  const raw = node.attrs?.['level']
  const level = typeof raw === 'number' && raw >= 1 && raw <= 6 ? raw : 2

  /*
    Shifted down by one. The page's `<h1>` is its title and the document's
    own title is an `<h2>` in the panel, so a level-1 heading *inside* the
    body is a level-3 element -- otherwise a document with a title heading
    would put a second `<h1>` on the page and break the outline the whole
    screen is built on. The `attrs.level` is preserved in storage and only
    the rendering is offset.
  */
  const tag = `h${String(Math.min(level + 2, 6))}` as 'h3' | 'h4' | 'h5' | 'h6'
  const Heading = tag

  return <Heading className={styles.proseHeading}>{renderNodes(children)}</Heading>
}

/**
 * One text run, wrapped in whatever marks it carries.
 *
 * Marks nest outward-in in array order, so `[bold, link]` renders as a link
 * inside a `<strong>`. Which way round that nests is not visible in the
 * output and does not matter; what matters is that every mark in the array
 * ends up applied rather than the last one winning.
 */
function renderText(node: ContentNode): ReactNode {
  let rendered: ReactNode = node.text ?? ''

  for (const mark of node.marks ?? []) {
    switch (mark.type) {
      case 'bold':
        rendered = <strong>{rendered}</strong>
        break

      case 'italic':
        rendered = <em>{rendered}</em>
        break

      case 'strike':
        rendered = <s>{rendered}</s>
        break

      case 'code':
        rendered = <code className={styles.inlineCode}>{rendered}</code>
        break

      case 'link': {
        const href = safeHref(mark.attrs?.['href'])

        rendered =
          href === null ? (
            /*
              A link whose target this client will not follow. The text is
              still shown -- it is what the author wrote -- and it is marked
              so a reader can see that something was meant to be a link and
              is not. Dropping the text would edit the document; rendering
              the link would be the thing `safeHref` exists to refuse.
            */
            <span className={styles.blockedLink} title="This link was not rendered">
              {rendered}
            </span>
          ) : (
            <a
              className={styles.proseLink}
              href={href}
              // `noreferrer` implies `noopener`; both are stated because the
              // pair is what stops a target page reaching back through
              // `window.opener`, and dropping either by tidying is a
              // regression nobody sees.
              rel="noopener noreferrer"
              target="_blank"
            >
              {rendered}
            </a>
          )
        break
      }

      default:
        // An unknown mark: the text is kept, the formatting is not applied,
        // and nothing is silently deleted.
        break
    }
  }

  return rendered
}
