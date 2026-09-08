/**
 * Reading and writing a document's body.
 *
 * `Document.content` is the `JSON` scalar, which codegen types as `unknown` --
 * correctly, because JSON says nothing about shape. Behind it is a
 * ProseMirror/TipTap tree with a CLOSED vocabulary that
 * `app/domain/documents.py` parses in both directions: on the way in from a
 * mutation, and on the way out of the repository, so a row written by a
 * hand-run UPDATE is refused rather than rendered.
 *
 * That the server parses is why this module can be small. It is not a second
 * security boundary and does not pretend to be one -- but it does not TRUST
 * either, because `unknown` is `unknown` and a client that assumed the shape
 * would crash the screen on the first surprise instead of showing what it
 * can. Everything below narrows before it reads.
 *
 * ## The vocabulary
 *
 * Nodes: paragraph, text, heading, bulletList, orderedList, listItem,
 * taskList, taskItem, blockquote, codeBlock, horizontalRule, hardBreak.
 * Marks: bold, italic, strike, code, link.
 *
 * A node type this module does not know is NOT dropped. Dropping is how a
 * spec silently becomes a shorter spec; the renderer shows a placeholder
 * saying something is there that it cannot draw. The same rule the server's
 * parser follows, for the same reason.
 */

/** One formatting mark, as far as anything here needs to know. */
export interface ContentMark {
  type: string
  attrs?: Record<string, unknown>
}

/** One node of the tree. */
export interface ContentNode {
  type: string
  attrs?: Record<string, unknown>
  content?: ContentNode[]
  marks?: ContentMark[]
  text?: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function readMarks(value: unknown): ContentMark[] | undefined {
  if (!Array.isArray(value)) {
    return undefined
  }

  const marks: ContentMark[] = []

  for (const entry of value) {
    if (isRecord(entry) && typeof entry['type'] === 'string') {
      const attrs = entry['attrs']

      marks.push({
        type: entry['type'],
        ...(isRecord(attrs) ? { attrs } : {}),
      })
    }
  }

  return marks.length === 0 ? undefined : marks
}

function readNode(value: unknown): ContentNode | null {
  if (!isRecord(value) || typeof value['type'] !== 'string') {
    return null
  }

  const children = value['content']
  const attrs = value['attrs']
  const text = value['text']
  const marks = readMarks(value['marks'])

  return {
    type: value['type'],
    ...(isRecord(attrs) ? { attrs } : {}),
    ...(typeof text === 'string' ? { text } : {}),
    ...(Array.isArray(children) ? { content: readNodes(children) } : {}),
    ...(marks === undefined ? {} : { marks }),
  }
}

function readNodes(values: readonly unknown[]): ContentNode[] {
  const nodes: ContentNode[] = []

  for (const value of values) {
    const node = readNode(value)

    if (node !== null) {
      nodes.push(node)
    }
  }

  return nodes
}

/**
 * The top-level blocks of a document, or null when the value is not a
 * document at all.
 *
 * Null and an empty array are different answers. An empty array is
 * `{"type":"doc","content":[]}` -- somebody made a document and has not
 * written in it yet, which the server's `empty_content()` produces on
 * purpose. Null is a value that is not a document tree, which the renderer
 * reports rather than drawing as an empty page.
 */
export function readDocument(content: unknown): ContentNode[] | null {
  if (!isRecord(content) || content['type'] !== 'doc') {
    return null
  }

  const children = content['content']

  return Array.isArray(children) ? readNodes(children) : []
}

/**
 * Whether this document survives a round trip through a plain text box.
 *
 * The editor below is a `<textarea>`, so it can carry paragraphs and line
 * breaks and nothing else. A document holding a heading, a list, a quote, a
 * code block or a single bold word would come back out of a textarea with all
 * of that gone -- which is the template feature's whole-row-replace bug in a
 * different costume: an edit to one part of a document silently deleting
 * another.
 *
 * So the editor asks first. False means the screen renders the document and
 * refuses to offer a body editor, saying why. The title stays editable
 * either way, because `DocumentEditInput` is a patch and a rename touches no
 * content at all.
 *
 * ponytail: the upgrade path is a real rich-text editor bound to the same
 * closed vocabulary -- TipTap is the obvious one, since the schema is already
 * its document shape. That is a dependency and a week; refusing to destroy
 * formatting costs one function.
 */
export function isPlainTextDocument(nodes: readonly ContentNode[]): boolean {
  return nodes.every(
    (node) =>
      node.type === 'paragraph' &&
      node.marks === undefined &&
      (node.content ?? []).every(
        (child) =>
          (child.type === 'text' && child.marks === undefined) ||
          child.type === 'hardBreak',
      ),
  )
}

/**
 * A plain-text document as text.
 *
 * Blocks are separated by a blank line and a `hardBreak` by a single newline,
 * which is exactly what `fromPlainText` reads back -- the two are one
 * bijection, and only for documents `isPlainTextDocument` accepts. Calling
 * this on anything else loses what it cannot carry, which is why nothing
 * does.
 */
export function toPlainText(nodes: readonly ContentNode[]): string {
  return nodes
    .map((node) =>
      (node.content ?? [])
        .map((child) => (child.type === 'hardBreak' ? '\n' : (child.text ?? '')))
        .join(''),
    )
    .join('\n\n')
}

/**
 * Text as a document tree the server will accept.
 *
 * Blank lines separate paragraphs; a single newline inside one becomes a
 * `hardBreak`, because a paragraph per line would turn a pasted address into
 * four paragraphs. Empty blocks are dropped rather than stored as empty
 * paragraphs -- trailing blank lines are an artefact of typing, not content.
 *
 * The shape is the server's own: `{"type":"doc","content":[...]}`, with text
 * runs as `{"type":"text","text":"..."}` and breaks as
 * `{"type":"hardBreak"}`. Empty text gives `content: []`, which is what
 * `empty_content()` produces.
 */
export function fromPlainText(text: string): unknown {
  const blocks = text
    .split(/\n{2,}/)
    .map((block) => block.replace(/\r/g, ''))
    .filter((block) => block.trim() !== '')

  return {
    type: 'doc',
    content: blocks.map((block) => {
      const lines = block.split('\n')
      const children: unknown[] = []

      lines.forEach((line, index) => {
        if (index > 0) {
          children.push({ type: 'hardBreak' })
        }

        if (line !== '') {
          children.push({ type: 'text', text: line })
        }
      })

      return { type: 'paragraph', content: children }
    }),
  }
}

/**
 * A link's href, or null when it is one this client will not render.
 *
 * The server already refuses anything outside http, https and mailto -- see
 * `ALLOWED_URL_SCHEMES` in `app/domain/documents.py`. This checks again
 * anyway, and the duplication is deliberate: the value reaching this function
 * has been through a database, a cache and a JSON parse since that check, and
 * an `href` is the one attribute in this vocabulary that becomes executable
 * if it is wrong. Two cheap checks, one of which cannot be removed by a
 * change on the other side of the wire.
 */
const ALLOWED_SCHEMES = new Set(['http:', 'https:', 'mailto:'])

export function safeHref(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null
  }

  try {
    // Parsed rather than pattern-matched: `URL` resolves the escapes,
    // whitespace and case tricks (`java\tscript:`, `JaVaScRiPt:`) that a
    // regexp over the raw string has to enumerate and eventually misses.
    // A relative href has no scheme to abuse, so any base will do.
    const parsed = new URL(value, 'https://vector.invalid/')

    return ALLOWED_SCHEMES.has(parsed.protocol) ? value : null
  } catch {
    return null
  }
}
