import type { CSSProperties } from 'react'

import { cx } from '../cx'
import styles from './Avatar.module.css'

export type AvatarSize = 'sm' | 'md' | 'lg'

export interface AvatarProps {
  /** The person's display name. Drives the initials, the hue and the label. */
  name: string
  /** Profile image. Falls back to initials while absent. */
  src?: string
  size?: AvatarSize
  /**
   * Set when the name is already on screen beside the avatar, which is the
   * common case in an assignee cell. The avatar then adds nothing a screen
   * reader has not already heard, and announcing it twice is noise.
   */
  decorative?: boolean
  className?: string
}

/**
 * Up to two initials, taken from the first and last whitespace-separated part.
 *
 * `Array.from` rather than `[0]`, because indexing a string returns a UTF-16
 * code unit: for a name beginning with an emoji or an astral-plane character
 * that is half a surrogate pair, which renders as a replacement glyph.
 */
function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  const first = parts.at(0)
  const last = parts.length > 1 ? parts.at(-1) : undefined

  const letters = [first, last]
    .filter((part): part is string => part !== undefined)
    .map((part) => Array.from(part).at(0) ?? '')
    .join('')

  return letters.toLocaleUpperCase()
}

/**
 * A stable hue per name.
 *
 * djb2, because the requirement is only "the same name always gets the same
 * colour and different names usually differ" -- there is no security or
 * distribution property to protect here, and a cryptographic hash would be
 * slower for no benefit. `>>> 0` keeps it unsigned after the 32-bit overflow
 * that `| 0` introduces.
 */
function hueOf(name: string): number {
  let hash = 5381
  for (const char of name) {
    hash = ((hash << 5) + hash + (char.codePointAt(0) ?? 0)) | 0
  }
  return (hash >>> 0) % 360
}

/**
 * A person.
 *
 * The initials fallback is the primary path, not the error path: most users in
 * a young workspace have no avatar image, and a grey silhouette for all of
 * them makes an assignee column unreadable. Saturation and lightness come from
 * theme tokens and only the hue varies, so every possible name lands on a
 * legible pair in both themes without any of them being checked.
 */
export function Avatar({
  name,
  src,
  size = 'md',
  decorative = false,
  className,
}: AvatarProps) {
  const style = { '--avatar-hue': `${String(hueOf(name))}deg` } as CSSProperties

  return (
    <span
      className={cx(styles.avatar, styles[size], className)}
      style={style}
      role={decorative ? undefined : 'img'}
      aria-label={decorative ? undefined : name}
      aria-hidden={decorative || undefined}
    >
      {src === undefined ? (
        initialsOf(name)
      ) : (
        /* `alt=""` because the wrapper already carries the name; a second copy
         * on the image would announce the person twice. */
        <img src={src} alt="" className={styles.image} loading="lazy" />
      )}
    </span>
  )
}
