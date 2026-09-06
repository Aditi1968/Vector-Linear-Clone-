import type { CSSProperties } from 'react'

import { cx } from '../cx'
import styles from './Skeleton.module.css'

export interface SkeletonProps {
  /** Any CSS length. Defaults to filling the container. */
  width?: string
  /** Any CSS length. Defaults to one line of body text. */
  height?: string
  className?: string
}

/**
 * A placeholder for content that is on its way.
 *
 * Always `aria-hidden`. A skeleton is a picture of text that does not exist
 * yet; announcing it gives a screen-reader user a stack of blank items to walk
 * through and no indication anything is loading. The announcement is the
 * caller's job and belongs on one live region for the whole list -- `Spinner`
 * with a `label`, or a `role="status"` element -- not on each grey bar.
 */
export function Skeleton({ width, height, className }: SkeletonProps) {
  const style = {
    ...(width !== undefined && { '--skeleton-width': width }),
    ...(height !== undefined && { '--skeleton-height': height }),
  } as CSSProperties

  return (
    <span aria-hidden="true" style={style} className={cx(styles.skeleton, className)} />
  )
}
