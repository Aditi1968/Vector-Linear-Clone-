import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useDensity, useGroupMode } from './preferences'

const root = document.documentElement

afterEach(() => {
  vi.restoreAllMocks()
})

/**
 * The two view preferences.
 *
 * What is worth pinning is not that a `useState` holds a value. It is the
 * three things that are easy to get wrong and silent when they are: the
 * attribute density writes is the one `src/styles/tokens.css` reads, the
 * choice survives a remount, and a browser that refuses storage does not take
 * the application down with it.
 */
describe('useDensity', () => {
  /*
    `data-density='comfortable'` is the exact selector tokens.css uses to
    re-point `--row-height`. A different attribute, or the right attribute
    with the wrong value, changes nothing on screen and breaks nothing in a
    way a render test would notice -- the rows just stay dense.
  */
  it('writes the attribute the stylesheet reads', () => {
    const { result } = renderHook(() => useDensity())

    expect(root.hasAttribute('data-density')).toBe(false)

    act(() => {
      result.current[1]('comfortable')
    })

    expect(root.getAttribute('data-density')).toBe('comfortable')
  })

  /*
    Dense removes the attribute rather than setting `data-density='dense'`, so
    the document's default state is the stylesheet's default state and there
    is no second spelling of "dense" to keep in sync.
  */
  it('returns to the default by removing the attribute', () => {
    const { result } = renderHook(() => useDensity())

    act(() => {
      result.current[1]('comfortable')
    })
    act(() => {
      result.current[1]('dense')
    })

    expect(root.hasAttribute('data-density')).toBe(false)
  })

  /*
    The point of storing it at all. A second `renderHook` is a remount, which
    is what a navigation between two screens does to the control.
  */
  it('survives a remount', () => {
    const first = renderHook(() => useDensity())

    act(() => {
      first.result.current[1]('comfortable')
    })
    first.unmount()

    const { result } = renderHook(() => useDensity())

    expect(result.current[0]).toBe('comfortable')
    expect(root.getAttribute('data-density')).toBe('comfortable')
  })
})

describe('useGroupMode', () => {
  it('survives a remount', () => {
    const first = renderHook(() => useGroupMode())

    expect(first.result.current[0]).toBe('flat')

    act(() => {
      first.result.current[1]('grouped')
    })
    first.unmount()

    expect(renderHook(() => useGroupMode()).result.current[0]).toBe('grouped')
  })

  /*
    Grouping does not touch the document. It changes what a screen renders,
    so a screen reads the value -- and a stray attribute here would be a
    second, silent source of truth.
  */
  it('leaves the document alone', () => {
    const { result } = renderHook(() => useGroupMode())

    act(() => {
      result.current[1]('grouped')
    })

    expect(root.hasAttribute('data-group')).toBe(false)
    expect(root.hasAttribute('data-density')).toBe(false)
  })
})

/**
 * Storage that throws.
 *
 * Not a hypothetical: a browser set to block site data throws from
 * `localStorage` on *read* as well as on write, and these hooks are mounted
 * by chrome that every screen renders under. An unguarded `getItem` here is a
 * blank application for those users, which is the failure this suite exists
 * to prevent -- and it is invisible in any environment where storage works.
 */
describe('when the browser refuses storage', () => {
  function refuseStorage(): void {
    const boom = (): never => {
      throw new DOMException('The operation is insecure.', 'SecurityError')
    }

    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(boom)
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(boom)
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(boom)
  }

  it('still mounts, and still applies the choice for this session', () => {
    refuseStorage()

    const { result } = renderHook(() => useDensity())

    expect(result.current[0]).toBe('dense')

    act(() => {
      result.current[1]('comfortable')
    })

    // The preference is not remembered, which is the user's browser's call.
    // It still takes effect while they are here, which is ours.
    expect(result.current[0]).toBe('comfortable')
    expect(root.getAttribute('data-density')).toBe('comfortable')
  })

  it('does not throw out of the grouping hook either', () => {
    refuseStorage()

    const { result } = renderHook(() => useGroupMode())

    act(() => {
      result.current[1]('grouped')
    })

    expect(result.current[0]).toBe('grouped')
  })
})
